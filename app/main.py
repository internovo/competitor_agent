"""FastAPI surface. Run: uvicorn app.main:app --reload

The agent owns no storage. The subject arrives in the request, the answer goes
back in the response, and the Node API decides who may see it. Runs live in memory
only long enough to be polled.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from app import service
from app.config import settings
from app.llm.client import get_llm
from app.models.schema import OwnProject, Project
from app.sources.propog import bhk, published_only
from app.storage.runs import RunRecord, RunStore

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Refuse to start a live deployment with the shared secret unset.

    Unset means the header is not checked, which is right for the fixture demo and
    wrong for anything on the network: /scan would accept a scan from anyone and
    spend real money on it. Fail at boot, where a deploy notices, not on the first
    request from a stranger.
    """
    if settings.fetch_mode == "live" and not settings.agent_token:
        raise RuntimeError(
            "FETCH_MODE=live requires AGENT_TOKEN to be set; without it /scan is unauthenticated. "
            "Set the same value here and as COMPETITOR_AGENT_TOKEN in propOG.")
    yield


app = FastAPI(title="propOG Competitor Analysis Agent", version="0.2.0", lifespan=lifespan)
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


# ------------------------------------------------- the propOG entry point
class AgentScanRequest(BaseModel):
    """Exactly the body agent-client.cjs sends, and nothing else.

    Every id is a UUID string there; they are opaque here, so they are typed as
    strings rather than validated as UUIDs -- rejecting a caller's id format is not
    this service's job. `radius_km` is validated 0 to 25 on their side; the ceiling
    here is the same one /scans has always used, because a 25 km circle in Mumbai is
    tens of thousands of register rows and would not finish.
    """
    run_id: str
    project_id: str
    builder_id: str
    radius_km: float = Field(gt=0, le=10)
    subject: dict[str, Any]
    # The published projects propOG holds near the subject, in the subject's own
    # shape plus `status` and `builder_id`. The agent has no database credentials
    # and asks for none: tenancy and the publication flag stay in propOG, and what
    # is not on the wire is not a competitor. Absent means none were sent, which is
    # a scan with no propOG competitors, not an error.
    nearby: list[dict[str, Any]] = Field(default_factory=list)


def _own_from_subject(subject: dict, project_id: str) -> OwnProject:
    """propOG's subject row -> the form the pipeline compares against.

    Only what is on the wire is used. The fields propOG does not send stay unset and
    the pipeline reports them as unavailable rather than scoring against a default.
    """
    return OwnProject(
        id=str(subject.get("id") or project_id),
        name=subject.get("name") or "",
        builder=subject.get("builder"),
        # `city` is the only place propOG puts a place name today, so it stands in for
        # the address discovery searches on. `locality` is what that search wants.
        address=subject.get("address") or subject.get("city"),
        locality=subject.get("locality"),
        lat=subject.get("latitude"), lng=subject.get("longitude"),
        configurations=bhk(subject.get("configurations") or []),
        carpet_sqft=subject.get("carpet_sqft"), rate_psf=subject.get("rate_psf"),
        possession=subject.get("possession"), structure=subject.get("structure"),
    )


@app.post("/scan", status_code=202)
async def scan(body: AgentScanRequest, request: Request) -> dict:
    """The route propOG calls. Returns immediately; the scan runs in the background.

    Their client aborts after 10 seconds and reads only `res.ok`, so this must not
    wait for the graph. The run is keyed by THEIR run_id, so `GET /scans/{run_id}`
    is addressable with the id they already hold.
    """
    if settings.agent_token and request.headers.get("x-agent-token") != settings.agent_token:
        raise HTTPException(401, "x-agent-token missing or wrong")
    if not (body.subject.get("name") or "").strip():
        raise HTTPException(422, "subject.name is required; a project with no name cannot be searched for")
    try:
        own = _own_from_subject(body.subject, body.project_id)
    except ValidationError as e:
        raise HTTPException(422, f"subject could not be read: {e.errors()[:3]}") from e
    if own.lat is None and not own.address:
        raise HTTPException(422, "subject needs latitude and longitude, or a city to geocode from")

    # Checked here rather than deeper in, because this is the door: a row that is not
    # PUBLISHED, or belongs to the builder asking, never becomes a candidate at all.
    nearby = published_only(body.nearby, body.builder_id)
    run = runs.create(own, body.radius_km, settings.fetch_mode, run_id=body.run_id, nearby=nearby)
    request_id = request.headers.get("x-request-id")
    run.note([f"scan: accepted for project {body.project_id}, builder {body.builder_id}"
              + (f", request {request_id}" if request_id else ""),
              f"scan: propOG sent {len(body.nearby)} nearby projects, {len(nearby)} published and not this builder's"])
    task = asyncio.create_task(service.execute(run))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"run_id": run.run_id, "status": run.status}


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


class StatelessCompareRequest(BaseModel):
    """The same comparison, from columns the caller stored, with no run in memory.

    A run lives here for two hours and is lost on restart; propOG keeps every finished
    scan forever. Without this route a rep opening last month's scan and clicking
    Compare would get a 404 from us for a scan we did produce.
    """
    columns: list[dict[str, Any]]
    radius_km: float = Field(gt=0, le=25)


def _guard(col: dict[str, Any]) -> None:
    """The same two refusals as the run-scoped route, off the column's own label."""
    name = col.get("name") or col.get("id") or "this project"
    if col.get("label") == "UNVERIFIED":
        raise HTTPException(422, f"'{name}' has no lifecycle on record; confirm it is still selling before comparing")
    if col.get("label") == "THIN":
        raise HTTPException(422, f"'{name}' is THIN ({col.get('completeness')}/6) and cannot be analysed")


@app.post("/compare")
async def compare_stateless(body: StatelessCompareRequest) -> dict:
    """columns[0] is the own project, then one or two competitors."""
    if not 2 <= len(body.columns) <= 3:
        raise HTTPException(422, "send the own column first, then one or two competitor columns")
    for col in body.columns[1:]:
        _guard(col)
    try:
        return await service.compare_from_columns(body.columns, body.radius_km, get_llm())
    except (KeyError, TypeError) as e:
        # A column that did not come from compare_columns. Name what was missing rather
        # than 500ing, so the caller can see which column it sent is the wrong shape.
        raise HTTPException(400, f"a column is not the shape compare_columns produces: {type(e).__name__} {e}") from e


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
