"""FastAPI surface. Run: uvicorn app.main:app --reload"""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from app import service
from app.config import FIXTURE_DIR, settings
from app.llm.client import get_llm
from app.models.schema import OwnProject
from app.storage.db import Database

app = FastAPI(title="propOG Competitor Analysis Agent", version="0.1.0")
db = Database(settings.db_path)


@app.on_event("startup")
def _seed() -> None:
    p = FIXTURE_DIR / "marina64.json"
    if p.exists() and db.get_own("marina64") is None:
        db.save_own(OwnProject(**json.loads(p.read_text())))


@app.get("/health")
def health() -> dict:
    return {"ok": True, "mode": settings.fetch_mode, "llm": settings.has_llm, "provider": settings.llm_provider, "model": settings.llm_model}


@app.get("/own-projects")
def list_own() -> list[dict]:
    return [{"id": o.id, "name": o.name, "builder": o.builder, "locality": o.locality} for o in db.list_own()]


@app.post("/own-projects", status_code=201)
def create_own(own: OwnProject) -> dict:
    db.save_own(own)
    return {"id": own.id}


def _own(own_id: str) -> OwnProject:
    own = db.get_own(own_id)
    if own is None:
        raise HTTPException(404, f"own project '{own_id}' not found")
    return own


@app.post("/own-projects/{own_id}/scan")
async def scan(own_id: str, radius_km: float = Query(None, gt=0, le=10), mode: str = Query(None, pattern="^(fixture|replay|live)$")) -> dict:
    own = _own(own_id)
    radius_km = radius_km or settings.default_radius_km
    mode = mode or settings.fetch_mode
    if mode == "live" and not settings.has_llm:
        raise HTTPException(400, f"live mode needs an API key for provider '{settings.llm_provider}' (ANTHROPIC_API_KEY or GROQ_API_KEY)")
    try:
        rec, meta = await service.run_scan(own, radius_km, mode, db)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"scan failed: {type(e).__name__}: {e}") from e
    return service.list_payload(rec, own, meta)


@app.get("/own-projects/{own_id}/competitors")
def competitors(own_id: str) -> dict:
    own = _own(own_id)
    found = db.latest_scan(own_id)
    if found is None:
        raise HTTPException(404, "no scan yet; POST /own-projects/{id}/scan first")
    rec, meta = found
    return service.list_payload(rec, own, meta)


@app.get("/competitors/{cid}")
def competitor(cid: str, own_id: str | None = None) -> dict:
    found = db.find_project(cid, own_id)
    if found is None:
        raise HTTPException(404, f"competitor '{cid}' not found in any scan")
    rec, p, _ = found
    return service.detail_payload(p, _own(rec.own_id), rec)


class Override(BaseModel):
    field: str
    value: Any


@app.post("/competitors/{cid}/fields")
def override(cid: str, body: Override) -> dict:
    found = db.find_project(cid)
    if found is None:
        raise HTTPException(404, f"competitor '{cid}' not found")
    rec, p, meta = found
    own = _own(rec.own_id)
    try:
        p = service.apply_override(p, own, rec.radius_km, body.field, body.value)
    except (ValueError, TypeError) as e:
        raise HTTPException(422, str(e)) from e
    db.update_project(rec, p, meta)
    return service.detail_payload(p, own, rec)


class CompareRequest(BaseModel):
    own_id: str
    competitor_ids: list[str]


@app.post("/compare")
async def compare(body: CompareRequest) -> dict:
    if not 1 <= len(body.competitor_ids) <= 2:
        raise HTTPException(422, "choose one or two competitors")
    own = _own(body.own_id)
    found = db.latest_scan(body.own_id)
    if found is None:
        raise HTTPException(404, "no scan yet")
    rec, _ = found
    by_id = {p.id: p for p in rec.projects}
    comps = []
    for cid in body.competitor_ids:
        p = by_id.get(cid)
        if p is None:
            raise HTTPException(404, f"competitor '{cid}' not in latest scan")
        if p.label == "THIN":
            raise HTTPException(422, f"'{p.name}' is THIN ({p.completeness}/6) and cannot be analysed")
        comps.append(p)
    return await service.compare_payload(own, comps, rec.radius_km, get_llm())
