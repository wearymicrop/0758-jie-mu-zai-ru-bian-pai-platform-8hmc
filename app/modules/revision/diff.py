"""修订差异：字段级变更、增删工序与受影响面（含共享舞台/技师与前序链连带）。"""
from collections import defaultdict

# 修订可能改变的工序字段（line 是源行号，不参与业务差异）
FIELDS = ("show", "title", "stage", "role", "duration", "reset",
          "release", "deadline", "priority", "needs")
FIELD_LABELS = {
    "show": "演出", "title": "标题", "stage": "工位", "role": "工种",
    "duration": "时长", "reset": "清场", "release": "放行", "deadline": "截止",
    "priority": "优先级", "needs": "前置",
}


def diff_orders(baseline_orders, revised_orders):
    """对比两批已采信工单，返回新增、删除、字段变更三类差异。"""
    old = {order["id"]: order for order in baseline_orders}
    new = {order["id"]: order for order in revised_orders}
    added = sorted(new.keys() - old.keys())
    removed = sorted(old.keys() - new.keys())
    changed = []
    for key in sorted(new.keys() & old.keys()):
        fields = {}
        for name in FIELDS:
            before, after = old[key][name], new[key][name]
            if before != after:
                fields[name] = {"before": before, "after": after, "label": FIELD_LABELS[name]}
        if fields:
            changed.append({"id": key, "line": new[key]["line"], "fields": fields})
    return {"added": added, "removed": removed, "changed": changed,
            "old": old, "new": new}


def _successors(orders):
    """工序 -> 直接后继（needs 反向边）。"""
    graph = defaultdict(list)
    for order in orders:
        for parent in order["needs"]:
            graph[parent].append(order["id"])
    return {key: sorted(set(values)) for key, values in graph.items()}


def _overlap(left_start, left_end, right_start, right_end):
    return left_start < right_end and right_start < left_end


def impacted_set(diff, baseline_jobs, calendar_changed):
    """计算必须重排的工序集合（不动该集合之外的班组）。

    种子：直接改期/改字段的工序、新增工序、被删除工序的原后继。
    扩张（迭代到不动点）：
      1. 前序链连带——受影响工序的全部后继（下游会被新时段推移）；
      2. 共享舞台/技师连带——与任一受影响已落位工序在旧时段上
         资源相交的其它已落位工序。
    日历本身被修订时，所有旧已落位工序的资源依据都已失效，整体纳入。
    """
    old_placed = {job["id"]: job for job in baseline_jobs if job["status"] == "scheduled"}
    successors = _successors(diff["old"].values())
    seeds = {item["id"] for item in diff["changed"]} | set(diff["added"])
    for removed_id in diff["removed"]:
        seeds.update(successors.get(removed_id, []))

    if calendar_changed:
        return sorted(seeds | set(old_placed)), {"calendar": True}

    impacted = set(seeds)
    traces = {}
    for key in seeds:
        traces.setdefault(key, []).append("SEED")

    def grow():
        progressed = False
        # 前序链：受影响工序的直接后继全部连带
        for key in list(impacted):
            for child in successors.get(key, []):
                if child not in impacted:
                    impacted.add(child)
                    traces.setdefault(child, []).append(f"CHAIN:{key}")
                    progressed = True
        # 共享舞台 / 共享技师：旧时段相交者连带
        members = [old_placed[key] for key in list(impacted) if key in old_placed]
        for job in members:
            for other in old_placed.values():
                if other["id"] in impacted:
                    continue
                reasons = []
                if (job["stage"] == other["stage"]
                        and _overlap(job["start"], job["stage_end"], other["start"], other["stage_end"])):
                    reasons.append("STAGE")
                if (job["tech"] == other["tech"]
                        and _overlap(job["start"], job["end"], other["start"], other["end"])):
                    reasons.append("TECH")
                if reasons:
                    impacted.add(other["id"])
                    traces.setdefault(other["id"], []).extend(
                        f"{reason}:{job['id']}" for reason in reasons)
                    progressed = True
        return progressed

    while grow():
        pass
    return sorted(impacted), {"calendar": False, "traces": traces}
