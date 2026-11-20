"""修订联单 API：试排预览、草稿、核对、发布、取证，全程不覆盖旧版联单。"""
from fastapi import APIRouter, Response

from app.manifest import canonical
from app.modules.revision.service import (
    build_revision, create_draft, publish, revision_id, verify,
)
from app.sources import choose_source, load_sample
from app.store import fetch, fetch_revision, list_revisions

router = APIRouter()


def _resolve_source(payload):
    if set(payload) == {"sample"} and isinstance(payload["sample"], str):
        return load_sample(payload["sample"]), payload["sample"]
    return choose_source(payload)


@router.post("/api/runs/{run_id}/revisions/preview")
def preview_revision(run_id: str, payload: dict):
    """不落库试排：让值班组在生成联单前看清每项沿用/改期依据。"""
    baseline = fetch(run_id)[1]
    source, label = _resolve_source(payload)
    package = build_revision(baseline, source)
    return {"id": revision_id(run_id, source), "baseline_id": run_id, "sample": label,
            "status": "PREVIEW", **package}


@router.post("/api/runs/{run_id}/revisions")
def draft_revision(run_id: str, payload: dict):
    """生成修订草稿联单。同一修订重复提交返回同一编号，不会重复生成。"""
    source, label = _resolve_source(payload)
    return create_draft(run_id, source, label)


@router.get("/api/runs/{run_id}/revisions")
def revisions_of(run_id: str):
    fetch(run_id)
    return {"baseline_id": run_id, "revisions": list_revisions(run_id)}


@router.get("/api/revisions/{rev_id}")
def revision_detail(rev_id: str):
    return fetch_revision(rev_id)


@router.post("/api/revisions/{rev_id}/verify")
def verify_revision_route(rev_id: str):
    return verify(rev_id)


@router.post("/api/revisions/{rev_id}/publish")
def publish_revision(rev_id: str):
    return publish(rev_id)


@router.get("/api/revisions/{rev_id}/export")
def export_revision(rev_id: str):
    record = fetch_revision(rev_id)
    return Response(canonical(record), media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="revision-{record["id"]}.json"',
        "Cache-Control": "no-store",
    })
