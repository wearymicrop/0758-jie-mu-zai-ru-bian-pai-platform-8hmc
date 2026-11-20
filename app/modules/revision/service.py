"""修订联单服务：草稿重排、完整核对、原子发布，全程保留旧版联单。"""
from time import perf_counter

from app.errors import DomainError
from app.manifest import canonical, manifest, sha
from app.modules.calendars.registry import load
from app.modules.conflicts.occupancy import audit_holds
from app.modules.conflicts.inspection import order_issues
from app.modules.dependencies.checks import inspect
from app.modules.dispatch.checklist import build
from app.modules.orders.intake import collect
from app.modules.reconcile.audit import reconcile
from app.modules.revision.diff import diff_orders, impacted_set
from app.modules.revision.replan import replan
from app.modules.revision.verify import verify_revision
from app.store import fetch_revision, save_revision, update_revision_status

REVISION_RULE = "tandem-stage-revision-1.0"


def revision_id(baseline_id, revised_source):
    """同一基线 + 同一修订输入必然得到同一编号：重跑不重复生成联单。"""
    digest = manifest(revised_source)
    return "rev-" + sha(canonical([baseline_id, REVISION_RULE, digest["digest"]]))[:24]


def build_revision(baseline_record, revised_source):
    """只计算修订结果，不落库——供试排预览与草稿共用同一版逻辑。"""
    baseline = baseline_record["result"]
    orders = collect(revised_source["orders"])
    calendar = load(revised_source["calendar"])
    dependencies = inspect(orders["orders"])
    conflicts = audit_holds(calendar)
    conflicts["order_issues"] = order_issues(orders["orders"], calendar)

    order_diff = diff_orders(baseline["orders"]["orders"], orders["orders"])
    if not (order_diff["changed"] or order_diff["added"] or order_diff["removed"]):
        raise DomainError("REVISION_EMPTY", "修订批次与基线工单完全一致，没有可发布的变更")

    calendar_changed = canonical(baseline["calendars"]) != canonical(calendar)
    old_placed = {job["id"]: job for job in baseline["schedule"]["jobs"]
                  if job["status"] == "scheduled"}
    impacted, impact_meta = impacted_set(order_diff, baseline["schedule"]["jobs"], calendar_changed)

    jobs, annotations = replan(orders["orders"], dependencies, calendar, conflicts,
                               impacted, old_placed,
                               set(order_diff["added"]) | {c["id"] for c in order_diff["changed"]})
    schedule = {"jobs": jobs}
    result = {
        "orders": orders, "dependencies": dependencies, "calendars": calendar,
        "conflicts": conflicts, "schedule": schedule,
        "reconcile": reconcile(orders, schedule, conflicts),
        "dispatch": build(schedule, calendar),
    }
    report = verify_revision(baseline_record, revised_source, result,
                             annotations, impacted, order_diff, calendar_changed)
    return {
        "result": result,
        "revision": {
            "diff": {k: v for k, v in order_diff.items() if k != "old" and k != "new"},
            "impacted": impacted,
            "impact_meta": impact_meta,
            "annotations": annotations,
            "verify": report,
            "rule_version": REVISION_RULE,
        },
    }


def create_draft(baseline_id, revised_source, label):
    """创建（或幂等取回）修订草稿。草稿不改变已发布联单。

    同编号并发提交时，每条都完成计算，但数据库主键只接受一条插入，
    其余原样取回——同一修订重新运行不会重复生成联单。
    """
    baseline = _baseline(baseline_id)
    rev_id = revision_id(baseline_id, revised_source)

    started = perf_counter()
    package = build_revision(baseline, revised_source)
    digest = manifest(revised_source)
    record = {
        "id": rev_id,
        "baseline_id": baseline_id,
        "manifest": digest,
        "rule_version": REVISION_RULE,
        "sample": label,
        "status": "DRAFT",
        "elapsed_ms": round((perf_counter() - started) * 1000, 2),
        "source": revised_source,
        "result": package["result"],
        "revision": package["revision"],
    }
    record["revision"]["verify"]["status"] = "DRAFT"
    kept, inserted = save_revision(record)
    return {**kept, "reused": not inserted}


def verify(rev_id):
    """重新计算并与草稿比对；核对失败保留草稿与旧版联单，状态置 FAILED。"""
    record = _revision(rev_id)
    baseline = _baseline(record["baseline_id"])
    if manifest(record["source"]) != record["manifest"]:
        raise DomainError("REVISION_VERSION_CHANGED", "修订输入与归档摘要不一致", 409)
    fresh = build_revision(baseline, record["source"])
    if (fresh["result"] != record["result"]
            or fresh["revision"]["verify"]["checks"] != record["revision"]["verify"]["checks"]):
        update_revision_status(rev_id, "FAILED", verify_status="FAILED")
        raise DomainError("REVISION_DRIFT", "复算结果与修订草稿不一致，已保留旧版联单", 409)
    status = "PASSED" if fresh["revision"]["verify"]["status"] == "PASSED" else "FAILED"
    update_revision_status(rev_id, status, verify_status=status)
    return {**_revision(rev_id), "reused": True}


def publish(rev_id):
    """仅当核对通过才原子发布；失败时旧版联单原样保留。"""
    record = _revision(rev_id)
    if record["status"] not in {"PASSED", "PUBLISHED"}:
        raise DomainError("REVISION_NOT_VERIFIED",
                          "修订核对未通过，不能切换；旧版联单继续生效", 409)
    if record["status"] == "PUBLISHED":
        return {**record, "reused": True}
    update_revision_status(rev_id, "PUBLISHED")
    return {**_revision(rev_id), "reused": False}


def _baseline(baseline_id):
    from app.store import fetch
    source, record = fetch(baseline_id)
    if "revision_of" in record:
        raise DomainError("BASELINE_NOT_TICKET", "修订必须基于已归档的执行联单", 422)
    return record


def _revision(rev_id):
    record = fetch_revision(rev_id)
    return record
