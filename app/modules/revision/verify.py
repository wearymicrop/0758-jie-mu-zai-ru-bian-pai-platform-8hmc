"""修订核对：同一版结果做守恒、碰撞、前置与截止回归，未通过不允许切换。"""
from app.modules.reconcile.audit import reconcile


def verify_revision(baseline_record, revised_source, revised_result, annotations, impacted, diff, calendar_changed):
    """对修订结果做完整核对，产出可发布的检查清单与切换结论。"""
    baseline_reconcile = baseline_record["result"]["reconcile"]
    audit = reconcile(revised_result["orders"], revised_result["schedule"], revised_result["conflicts"])

    old_jobs = {job["id"]: job for job in baseline_record["result"]["schedule"]["jobs"]}
    new_jobs = {job["id"]: job for job in revised_result["schedule"]["jobs"]}

    # 守恒：输入行仍唯一归类，且两侧键集合与采信工单一致
    accepted = {order["id"] for order in revised_result["orders"]["orders"]}
    key_gap = sorted(accepted ^ new_jobs.keys())

    # 锁定承诺：受影响面之外的已落位工序必须逐字段沿用旧时段
    broken_locks = []
    for key, job in new_jobs.items():
        if key in impacted or job["status"] != "scheduled":
            continue
        old = old_jobs.get(key)
        if old is None or old.get("status") != "scheduled":
            broken_locks.append({"id": key, "reason": "BASELINE_NOT_PLACED"})
            continue
        for field in ("start", "end", "stage_end", "tech"):
            if old[field] != job[field]:
                broken_locks.append({"id": key, "reason": f"FIELD_DRIFT:{field}",
                                     "before": old[field], "after": job[field]})

    # 截止回归：修订不得让任何沿用工序新发生超时，整体超时工时不得恶化
    old_late = {row["id"]: row["minutes"] for row in baseline_reconcile["late"]}
    late_regressions = []
    new_late_total = 0
    for job in revised_result["schedule"]["jobs"]:
        if job["status"] != "scheduled":
            continue
        minutes = job["end"] - job["deadline"]
        if minutes > 0:
            new_late_total += minutes
            if minutes > old_late.get(job["id"], 0):
                late_regressions.append({"id": job["id"],
                                         "before": old_late.get(job["id"], 0), "after": minutes})
    old_late_total = sum(old_late.values())

    # 增删工序必须与字段差异完全对得上
    removed_left = [job for job in revised_result["schedule"]["jobs"] if job["id"] in diff["removed"]]

    change_counts = {"kept": 0, "moved": 0, "new": 0, "recovered": 0, "deferred": 0}
    for item in annotations.values():
        change_counts[{
            "KEPT": "kept", "MOVED": "moved", "NEW": "new",
            "RECOVERED": "recovered", "DEFERRED": "deferred",
        }[item["change"]]] += 1

    checks = [
        {"code": "ROW_BALANCE", "name": "输入行守恒", "passed": audit["row_gap"] == 0,
         "detail": {"row_gap": audit["row_gap"]}},
        {"code": "KEY_SET", "name": "工序键集合", "passed": not key_gap,
         "detail": {"gap": key_gap}},
        {"code": "NO_COLLISION", "name": "舞台/技师碰撞", "passed": not audit["collisions"],
         "detail": {"collisions": audit["collisions"]}},
        {"code": "NO_PREMATURE", "name": "前置顺序", "passed": not audit["premature"],
         "detail": {"premature": audit["premature"]}},
        {"code": "LOCK_KEPT", "name": "未受影响班组沿用旧时段", "passed": not broken_locks,
         "detail": {"broken": broken_locks}},
        {"code": "DEADLINE_REGRESSION", "name": "截止回归", "passed": not late_regressions,
         "detail": {"regressions": late_regressions,
                    "late_minutes_before": old_late_total, "late_minutes_after": new_late_total}},
        {"code": "REMOVAL_ACCOUNTED", "name": "删除工序已出清", "passed": not removed_left,
         "detail": {"leftover": [job["id"] for job in removed_left]}},
    ]
    passed_all = all(check["passed"] for check in checks)

    return {
        "status": "PASSED" if passed_all else "FAILED",
        "checks": checks,
        "reconcile": audit,
        "impacted": impacted,
        "calendar_changed": calendar_changed,
        "change_counts": change_counts,
        "late_minutes": {"before": old_late_total, "after": new_late_total},
        "diff_summary": {
            "changed": [item["id"] for item in diff["changed"]],
            "added": diff["added"], "removed": diff["removed"],
        },
    }
