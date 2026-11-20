"""修订联单：字段差异、受影响面、锁定重排、核对闸门与幂等发布。"""
from concurrent.futures import ThreadPoolExecutor

from app.sources import load_sample
from app.modules.revision.diff import diff_orders, impacted_set
from app.modules.revision.service import build_revision


def _revision(baseline_result, source):
    return build_revision({"id": "baseline-demo", "result": baseline_result}, source)


def test_field_level_diff_and_impact_closure(clean):
    revised = load_sample("revision")
    package = _revision(clean, revised)
    rev = package["revision"]
    changed = {item["id"]: item["fields"] for item in rev["diff"]["changed"]}
    assert set(changed) == {"A01"}
    assert changed["A01"]["duration"] == {"before": 35, "after": 55, "label": "时长"}
    assert rev["diff"]["added"] == ["D01"]
    assert rev["diff"]["removed"] == []
    # 修订点 + 前序链（A02..A05、C04）+ 新增工序；共享舞台/技师无时段相交，不扩大
    assert set(rev["impacted"]) == {"A01", "A02", "A03", "A04", "A05", "C04", "D01"}
    assert rev["impact_meta"]["calendar"] is False


def test_unaffected_crews_keep_exact_slot(clean):
    package = _revision(clean, load_sample("revision"))
    rev, result = package["revision"], package["result"]
    old = {job["id"]: job for job in clean["schedule"]["jobs"]}
    new = {job["id"]: job for job in result["schedule"]["jobs"]}
    untouched = set(old) - set(rev["impacted"])
    assert untouched  # 未受影响班组确实存在
    for key in untouched:
        ann = rev["annotations"][key]
        assert ann["change"] == "KEPT"
        for field in ("start", "end", "stage_end", "tech"):
            assert new[key][field] == old[key][field]
    # 修订点与下游按依据改期，新增工序完成落位
    assert rev["annotations"]["A01"]["change"] == "MOVED"
    assert "SELF_REVISED" in rev["annotations"]["A01"]["reasons"]
    assert rev["annotations"]["D01"]["change"] == "NEW"
    assert new["D01"]["status"] == "scheduled"


def test_verify_passes_and_three_outputs_share_one_version(clean):
    package = _revision(clean, load_sample("revision"))
    verify = package["revision"]["verify"]
    assert verify["status"] == "PASSED"
    assert all(check["passed"] for check in verify["checks"])
    scheduled = {job["id"] for job in package["result"]["schedule"]["jobs"] if job["status"] == "scheduled"}
    dispatched = {step["id"] for stage in package["result"]["dispatch"]["stages"] for step in stage["steps"]}
    assert scheduled == dispatched
    assert package["result"]["reconcile"]["status"] == "MATCHED"
    assert verify["change_counts"]["kept"] + verify["change_counts"]["moved"] \
        + verify["change_counts"]["new"] == len(scheduled)


def test_revision_is_deterministic(clean):
    source = load_sample("revision")
    first = _revision(clean, source)
    second = _revision(clean, source)
    assert first["result"] == second["result"]
    assert first["revision"]["annotations"] == second["revision"]["annotations"]


def test_removed_order_cascades_to_successors(clean):
    source = load_sample("revision")
    source["orders"] = "\n".join(
        line for line in source["orders"].splitlines()
        if '"id": "A05"' not in line)
    package = _revision(clean, source)
    rev = package["revision"]
    assert rev["diff"]["removed"] == ["A05"]
    assert "C04" in rev["impacted"]  # 被删工序的后继连带
    assert all(job["id"] != "A05" for job in package["result"]["schedule"]["jobs"])
    assert package["revision"]["verify"]["status"] == "PASSED"


def test_calendar_revision_marks_everyone_impacted(clean):
    source = load_sample("revision")
    calendar = source["calendar"]
    calendar["holds"].append({"id": "H09", "type": "stage", "resource": "主舞台",
                              "start": 900, "end": 930})
    package = _revision(clean, source)
    rev = package["revision"]
    assert rev["impact_meta"]["calendar"] is True
    old_placed = {job["id"] for job in clean["schedule"]["jobs"] if job["status"] == "scheduled"}
    assert old_placed <= set(rev["impacted"])


def test_tight_deadline_fails_gate_without_switching(clean):
    source = load_sample("revision")
    source["orders"] = "\n".join(
        line.replace('"deadline": 1140', '"deadline": 600')
        if '"id": "A03"' in line else line
        for line in source["orders"].splitlines())
    package = _revision(clean, source)
    verify = package["revision"]["verify"]
    assert verify["status"] == "FAILED"
    failed = {check["code"] for check in verify["checks"] if not check["passed"]}
    assert "DEADLINE_REGRESSION" in failed
    regression = next(check for check in verify["checks"]
                      if check["code"] == "DEADLINE_REGRESSION")["detail"]["regressions"]
    assert any(row["id"] == "A03" and row["after"] > row["before"] for row in regression)


def test_identical_revision_is_rejected(clean):
    from app.errors import DomainError
    try:
        _revision(clean, load_sample("clean"))
    except DomainError as exc:
        assert exc.code == "REVISION_EMPTY"
    else:
        raise AssertionError("无差异修订应被拒绝")


def test_draft_lifecycle_idempotent_and_keeps_baseline(client):
    baseline = client.post("/api/runs", json={"sample": "clean"}).json()
    base_id = baseline["id"]

    draft = client.post(f"/api/runs/{base_id}/revisions", json={"sample": "revision"}).json()
    assert draft["status"] == "DRAFT"
    # 同一修订重新运行：同编号，不重复生成联单
    again = client.post(f"/api/runs/{base_id}/revisions", json={"sample": "revision"}).json()
    assert again["id"] == draft["id"] and again["reused"] is True
    listed = client.get(f"/api/runs/{base_id}/revisions").json()["revisions"]
    assert len(listed) == 1

    # 未核对不能切换
    blocked = client.post(f"/api/revisions/{draft['id']}/publish")
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "REVISION_NOT_VERIFIED"

    verified = client.post(f"/api/revisions/{draft['id']}/verify").json()
    assert verified["status"] == "PASSED"
    published = client.post(f"/api/revisions/{draft['id']}/publish").json()
    assert published["status"] == "PUBLISHED"
    # 重复发布幂等
    assert client.post(f"/api/revisions/{draft['id']}/publish").json()["reused"] is True

    # 旧版联单原样保留
    old = client.get(f"/api/runs/{base_id}").json()
    assert old["id"] == base_id
    assert old["result"]["reconcile"]["scheduled"] == 15

    export = client.get(f"/api/revisions/{draft['id']}/export")
    assert export.headers["content-disposition"].startswith("attachment;")
    assert export.json()["id"] == draft["id"]


def test_failed_revision_blocks_publish_and_preserves_ticket(client):
    baseline = client.post("/api/runs", json={"sample": "clean"}).json()
    bad = load_sample("revision")
    bad["orders"] = "\n".join(
        line.replace('"deadline": 1140', '"deadline": 600')
        if '"id": "A03"' in line else line
        for line in bad["orders"].splitlines())
    draft = client.post(f"/api/runs/{baseline['id']}/revisions", json=bad).json()
    verify = client.post(f"/api/revisions/{draft['id']}/verify").json()
    assert verify["status"] == "FAILED"
    publish = client.post(f"/api/revisions/{draft['id']}/publish")
    assert publish.status_code == 409
    # 旧版联单仍可读取，修订停留在 FAILED
    assert client.get(f"/api/runs/{baseline['id']}").json()["id"] == baseline["id"]
    assert client.get(f"/api/revisions/{draft['id']}").json()["status"] == "FAILED"


def test_preview_does_not_archive(client):
    baseline = client.post("/api/runs", json={"sample": "clean"}).json()
    preview = client.post(f"/api/runs/{baseline['id']}/revisions/preview",
                          json={"sample": "revision"}).json()
    assert preview["status"] == "PREVIEW"
    assert client.get(f"/api/runs/{baseline['id']}/revisions").json()["revisions"] == []


def test_concurrent_revisions_collapse_to_one_ticket(client):
    baseline = client.post("/api/runs", json={"sample": "clean"}).json()

    def create(_):
        return client.post(f"/api/runs/{baseline['id']}/revisions",
                           json={"sample": "revision"}).json()

    with ThreadPoolExecutor(4) as pool:
        rows = list(pool.map(create, range(4)))
    assert len({row["id"] for row in rows}) == 1
    assert [row["reused"] for row in rows].count(False) == 1


def test_revision_requires_archived_baseline(client):
    response = client.post("/api/runs/unknown-run/revisions", json={"sample": "revision"})
    assert response.status_code == 404
    assert response.json()["code"] == "RUN_NOT_FOUND"


def test_revision_workbench_link(client):
    text = client.get("/").text
    assert text.count("data-view=") == 8
    assert 'data-view="revision"' in text
    assert client.get("/static/revision.js").status_code == 200
