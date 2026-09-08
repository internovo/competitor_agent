"""FastAPI surface. Run: uvicorn app.main:app --reload

The agent owns no storage. The subject arrives in the request, the answer goes
back in the response, and the Node API decides who may see it. Runs live in memory
only long enough to be polled.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app import service
from app.config import settings
from app.llm.client import get_llm
from app.models.schema import OwnProject, Project
from app.storage.runs import RunRecord, RunStore

app = FastAPI(title="propOG Competitor Analysis Agent", version="0.2.0")
runs = RunStore()
# asyncio only holds a weak reference to a task, so a scan would be collected
# mid-run without this.
_tasks: set[asyncio.Task] = set()


@app.get("/health")
def health() -> dict:
    return {"ok": True, "mode": settings.fetch_mode, "llm": settings.has_llm,
            "provider": settings.llm_provider, "model": settings.llm_model, "runs_held": len(runs)}


class ScanRequest(BaseModel):
    own: OwnProject
    radius_km: float | None = Field(default=None, gt=0, le=10)
    mode: str | None = Field(default=None, pattern="^(fixture|replay|live)$")


@app.post("/scans", status_code=202)
async def start_scan(body: ScanRequest) -> dict:
    radius_km = body.radius_km or settings.default_radius_km
    mode = body.mode or settings.fetch_mode
    if mode == "live" and not settings.has_llm:
        raise HTTPException(400, f"live mode needs an API key for provider '{settings.llm_provider}' (ANTHROPIC_API_KEY or GROQ_API_KEY)")
    run = runs.create(body.own, radius_km, mode)
    # A live scan takes minutes; Azure App Service and the browser both time out
    # long before. The client polls GET /scans/{run_id} instead.
    task = asyncio.create_task(service.execute(run))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"run_id": run.run_id, "status": run.status}


def _run(run_id: str) -> RunRecord:
    run = runs.get(run_id)
    if run is None:
        raise HTTPException(404, f"run '{run_id}' not found; runs are held in memory only and are lost on restart")
    return run


def _finished(run_id: str) -> RunRecord:
    run = _run(run_id)
    if run.status != "done":
        raise HTTPException(409, f"run '{run_id}' is {run.status}; poll GET /scans/{run_id} until it is done")
    return run


@app.get("/scans/{run_id}")
def scan_status(run_id: str) -> dict:
    return _run(run_id).body()


def _project(run: RunRecord, cid: str) -> Project:
    p = next((x for x in run.rec.projects if x.id == cid), None)
    if p is None:
        raise HTTPException(404, f"competitor '{cid}' not in run '{run.run_id}'")
    return p


@app.get("/scans/{run_id}/competitors/{cid}")
def competitor(run_id: str, cid: str) -> dict:
    run = _finished(run_id)
    return service.detail_payload(_project(run, cid), run.own, run.rec)


class Override(BaseModel):
    field: str
    value: Any


@app.post("/scans/{run_id}/competitors/{cid}/fields")
def override(run_id: str, cid: str, body: Override) -> dict:
    """A rep records a fact from a site visit. It never leaves this run."""
    run = _finished(run_id)
    p = _project(run, cid)
    try:
        p = service.apply_override(p, run.own, run.radius_km, body.field, body.value)
    except (ValueError, TypeError) as e:
        raise HTTPException(422, str(e)) from e
    run.payload = service.list_payload(run.rec, run.own, run.meta)
    return service.detail_payload(p, run.own, run.rec)


class CompareRequest(BaseModel):
    competitor_ids: list[str]


@app.post("/scans/{run_id}/compare")
async def compare(run_id: str, body: CompareRequest) -> dict:
    run = _finished(run_id)
    if not 1 <= len(body.competitor_ids) <= 2:
        raise HTTPException(422, "choose one or two competitors")
    comps = []
    for cid in body.competitor_ids:
        p = _project(run, cid)
        if p.label == "UNVERIFIED":
            raise HTTPException(422, f"'{p.name}' has no lifecycle on record; confirm it is still selling before comparing")
        if p.label == "THIN":
            raise HTTPException(422, f"'{p.name}' is THIN ({p.completeness}/6) and cannot be analysed")
        comps.append(p)
    return await service.compare_payload(run.own, comps, run.radius_km, get_llm())
