"""受影响面重排：集合外工序锁定旧时段，集合内按既有编排规则重新落位。"""
from collections import defaultdict

from app.modules.conflicts.inspection import invalid_resources
from app.modules.schedule.search import blockers, search


def _lockable(job, calendar, holds):
    """旧时段在修订后的日历与预约下仍然成立，才能沿用。"""
    if job["stage"] not in calendar["effective_stages"]:
        return False
    tech_windows = calendar["effective_tech"].get(job["tech"])
    if not tech_windows:
        return False
    if not any(a <= job["start"] and job["end"] <= b for a, b in calendar["effective_stages"][job["stage"]]):
        return False
    if not any(a <= job["start"] and job["end"] <= b for a, b in tech_windows):
        return False
    if blockers(holds.get("stage:" + job["stage"], []), job["start"], job["stage_end"]):
        return False
    if blockers(holds.get("tech:" + job["tech"], []), job["start"], job["end"]):
        return False
    return True


def replan(orders, dependencies, calendar, conflicts, impacted, old_placed, changed_ids):
    """在锁定旧班组时段的前提下，只重排 impacted 内的工序。

    返回 (schedule_jobs, annotations)：
      annotations[id] 给出 change（KEPT/MOVED/NEW/DEFERRED/RECOVERED）与原因。
    """
    blocked = {**conflicts["order_issues"], **dependencies["issues"]}
    indexed = {item["id"]: item for item in orders}
    holds = defaultdict(list, {key: list(rows) for key, rows in conflicts["holds"].items()})
    uncertain = invalid_resources(conflicts)
    impacted = frozenset(impacted)
    extra_impacted = set()  # 锁定旧时段失效而被临时纳入的工序，不回写调用方的受影响面
    states = {key: {**indexed[key], **issue, "status": "isolated"} for key, issue in blocked.items()}
    annotations = {}

    for key in dependencies["order"]:
        if key in blocked:
            continue
        job = indexed[key]
        parents = [states[parent] for parent in job["needs"] if parent in states]
        missing_parents = sorted(set(job["needs"]) - states.keys())
        missing = missing_parents + [parent["id"] for parent in parents if parent["status"] != "scheduled"]
        old = old_placed.get(key)

        if missing:
            states[key] = {**job, "status": "pending", "code": "DEPENDENCY_BLOCKED",
                           "related": missing, "ready": job["release"]}
            annotations[key] = {"change": "DEFERRED" if old else "NEW",
                                "reasons": [f"DEPENDENCY:{m}" for m in missing],
                                "old": _slot(old) if old else None, "new": None}
            continue

        if key not in impacted and key not in extra_impacted and old is not None:
            # 沿用旧时段：先核验修订后日历/预约仍允许，再登记为锁定占用
            if not _lockable(old, calendar, holds):
                extra_impacted.add(key)  # 旧时段已失效，纳入重排并留下证据
                annotations.setdefault(key, {"change": "MOVED", "reasons": []})
                annotations[key]["reasons"].append("LOCK_INVALID")
            else:
                state = {**job, "status": "scheduled", "start": old["start"], "end": old["end"],
                         "stage_end": old["stage_end"], "tech": old["tech"], "code": "PLACED",
                         "related": [], "ready": old["start"]}
                states[key] = state
                holds["stage:" + job["stage"]].append(
                    {"id": key, "start": old["start"], "end": old["stage_end"]})
                holds["tech:" + old["tech"]].append(
                    {"id": key, "start": old["start"], "end": old["end"]})
                annotations[key] = {"change": "KEPT", "reasons": ["UNCHANGED_LOCKED_SLOT"],
                                    "old": _slot(old), "new": _slot(state)}
                continue

        release = max([job["release"], *(parent["end"] for parent in parents)])
        slot, code, related = search(job, calendar, holds, release, uncertain)
        state = {**job, "status": "pending", "code": code, "related": related, "ready": release}
        if slot:
            start, tech = slot
            end = start + job["duration"]
            stage_end = end + job["reset"]
            state.update(status="scheduled", start=start, end=end, tech=tech, stage_end=stage_end)
            holds["stage:" + job["stage"]].append({"id": key, "start": start, "end": stage_end})
            holds["tech:" + tech].append({"id": key, "start": start, "end": end})
        states[key] = state

        if state["status"] != "scheduled":
            annotations[key] = {"change": "DEFERRED" if old else "NEW",
                                "reasons": [code], "old": _slot(old) if old else None, "new": None}
        elif old is None:
            annotations[key] = {"change": "NEW" if key in changed_ids else "RECOVERED",
                                "reasons": ["NEW_ORDER"] if key in changed_ids else ["REPLACED"],
                                "old": None, "new": _slot(state)}
        elif (old["start"], old["end"], old["tech"]) == (state["start"], state["end"], state["tech"]):
            annotations[key] = {"change": "KEPT", "reasons": ["SELF_CHANGED_SLOT_STABLE"],
                                "old": _slot(old), "new": _slot(state)}
        else:
            reasons = _move_reasons(key, changed_ids, old, state, parents, related)
            annotations[key] = {"change": "MOVED", "reasons": reasons,
                                "old": _slot(old), "new": _slot(state)}

    return [states[key] for key in sorted(states)], annotations


def _slot(job):
    if not job or job.get("start") is None:
        return None
    return {"start": job["start"], "end": job["end"], "stage_end": job["stage_end"], "tech": job["tech"]}


def _move_reasons(key, changed_ids, old, state, parents, related):
    reasons = []
    if key in changed_ids:
        reasons.append("SELF_REVISED")
    if any(parent["end"] > old["start"] for parent in parents):
        reasons.append("CHAIN_SHIFT")
    if state["tech"] != old["tech"]:
        reasons.append("TECH_SWITCH")
    if state["start"] != old["start"] and "CHAIN_SHIFT" not in reasons:
        reasons.append("RESOURCE_CONTENTION")
    if related:
        reasons.append("BLOCKED:" + ",".join(str(item) for item in related[:4]))
    if not reasons:
        reasons.append("REPLAN")
    return reasons
