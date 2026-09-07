"""All graph nodes. Runtime dependencies (sources, fetcher, llm, db) come in through config['configurable']."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date

from langchain_core.runnables import RunnableConfig
from langgraph.types import Send

from app.config import settings
from app.graph.state import ExtractInput, GraphState
from app.llm import templates
from app.logic import completeness, conflicts, eligibility, match_score, resolve
from app.logic.geo import haversine_km
from app.logic.merge import consolidate_rera, merge_facts
from app.models.schema import Candidate, ExtractedFacts, FieldValue, Project, Provenance, ReraPhase, ScanRecord
from app.sources.base import ScanContext
from app.sources.fetch import CacheMiss

COORD_PRIORITY = ["manual", "propog", "places", "maharera", "fixture", "osm", "builder_site", "squareyards", "housing", "99acres", "magicbricks", "tavily"]


def _deps(config: RunnableConfig) -> dict:
    return config["configurable"]


def _geocoders(d: dict) -> list:
    """Every enabled source that can turn an address into coordinates, best first (Places, then OSM)."""
    return [s for s in d["sources"] if hasattr(s, "geocode")]


async def _geocode(sources: list, ctx: ScanContext, query: str) -> tuple[tuple[float, float, str], str] | None:
    """First geocoder with an answer wins; a dead key or a miss falls through to the next."""
    for s in sources:
        try:
            g = await s.geocode(ctx, query)
        except Exception:  # noqa: BLE001 - a geocoder that is down is not a reason to lose the project
            continue
        if g:
            return g, s.name
    return None


def _ctx(state, config, extra: list[str] | None = None) -> ScanContext:
    d = _deps(config)
    return ScanContext(own=state["own"], radius_km=state["radius_km"], fetcher=d["fetcher"], extra_queries=extra or [])


# ---------------------------------------------------------------- geocode
async def geocode(state: GraphState, config: RunnableConfig) -> dict:
    own = state["own"]
    d = _deps(config)
    geocoders = _geocoders(d)
    places = next((s for s in d["sources"] if s.name == "places"), None)
    ctx = _ctx(state, config)
    log = []
    if own.lat is None or own.lng is None:
        if not geocoders:
            raise ValueError("Own project has no coordinates and no geocoding source is enabled")
        hit = await _geocode(geocoders, ctx, f"{own.name} {own.address}")
        if hit is None:
            raise ValueError(f"Could not geocode own project '{own.name}'")
        (own.lat, own.lng, _), via = hit
        log.append(f"geocode: own project placed at {own.lat:.5f},{own.lng:.5f} via {via}")
    metro = None
    if places is not None:
        try:
            metro = await places.nearest_metro(ctx, own.lat, own.lng)
        except (CacheMiss, Exception) as e:  # noqa: BLE001 - a missing metro never blocks a scan
            log.append(f"geocode: nearest metro lookup skipped ({type(e).__name__})")
    return {"own": own, "nearest_metro": metro, "scan_id": state.get("scan_id") or uuid.uuid4().hex[:12], "log": log}


# --------------------------------------------------------------- discover
async def discover(state: GraphState, config: RunnableConfig) -> dict:
    d = _deps(config)
    ctx = _ctx(state, config)
    results = await asyncio.gather(*(s.discover(ctx) for s in d["sources"]), return_exceptions=True)
    cands: list[Candidate] = []
    log = []
    for s, r in zip(d["sources"], results):
        if isinstance(r, Exception):
            log.append(f"discover[{s.name}]: failed ({type(r).__name__}: {r})")
        else:
            cands.extend(r)
            log.append(f"discover[{s.name}]: {len(r)} candidates")
    return {"candidates": cands, "log": log}


# ---------------------------------------------------------------- resolve
async def resolve_node(state: GraphState, config: RunnableConfig) -> dict:
    llm = _deps(config).get("llm")
    own = state["own"]
    groups: list[tuple[Candidate, list[Candidate]]] = []
    log = []
    for c in state["candidates"]:
        placed = False
        for rep, members in groups:
            verdict = resolve.decide(rep, c)
            if verdict is None and llm is not None:
                try:
                    v = await llm.same_project(rep, c)
                    verdict = v.same_project and v.confidence >= 0.6
                    log.append(f"resolve: LLM says {'same' if verdict else 'different'} for '{rep.name}' vs '{c.name}' ({v.reason})")
                except Exception as e:  # noqa: BLE001
                    log.append(f"resolve: LLM match failed ({type(e).__name__}); treating as different")
                    verdict = False
            if verdict:
                resolve.merge_into(rep, c, COORD_PRIORITY)
                members.append(c)
                placed = True
                break
        if not placed:
            groups.append((c.model_copy(), [c]))

    projects: list[Project] = []
    dropped: list[dict] = []
    margin = state["radius_km"] + settings.discovery_margin_km
    own_reras = {ph.number for ph in own.rera_phases}
    own_card = Candidate(name=own.name, builder=own.builder, lat=own.lat, lng=own.lng, address=own.address,
                         locality=own.locality, source="manual")
    for rep, members in groups:
        pid = rep.rera_no or resolve.slug(rep.name)
        # The register and the portals both list the builder's own project; it is the baseline, not a competitor.
        if (rep.rera_no and rep.rera_no in own_reras) or resolve.decide(own_card, rep) is True:
            dropped.append({"id": pid, "name": rep.name, "reason": "own project"})
            continue
        p = Project(id=pid, name=rep.name, builder=rep.builder, lat=rep.lat, lng=rep.lng, address=rep.address,
                    locality=rep.locality, on_propog=rep.on_propog, nearest_metro=rep.nearest_metro,
                    discovered_via=sorted({m.source for m in members}))
        if rep.lat is not None:
            p.pin_accuracy = "Places - rooftop" if rep.source == "places" else f"{rep.source} - address"
            p.distance_km = round(haversine_km(own.lat, own.lng, rep.lat, rep.lng), 2)
            if p.distance_km > margin:
                dropped.append({"id": pid, "name": p.name, "reason": f"outside radius at discovery ({p.distance_km} km)"})
                continue
        rera_members = [m for m in members if m.rera_no and m.source == "maharera"]
        if rera_members:
            m = rera_members[0]
            p.rera_phases.observe(FieldValue(value=[ReraPhase(number=m.rera_no, verified=True, label="Phase 1")],
                                             prov=Provenance(source="maharera", url=m.source_url),
                                             method="deterministic", confidence="high"))
        projects.append(p)
    log.append(f"resolve: {len(state['candidates'])} candidates -> {len(groups)} projects, {len(dropped)} dropped early")
    return {"projects": projects, "dropped": dropped, "log": log}


def fan_out_extract(state: GraphState):
    sends = [Send("extract", ExtractInput(own=state["own"], radius_km=state["radius_km"], project=p, retry=False, extra_queries=[]))
             for p in state["projects"]]
    return sends or "filter"  # nothing discovered: still run the rest so the scan is recorded


# ---------------------------------------------------------------- extract
async def extract(state: ExtractInput, config: RunnableConfig) -> dict:
    d = _deps(config)
    llm = d.get("llm")
    project: Project = state["project"]
    ctx = ScanContext(own=state["own"], radius_km=state["radius_km"], fetcher=d["fetcher"], extra_queries=state["extra_queries"])
    log = []

    sources = d["sources"]
    if state["retry"]:
        sources = [s for s in sources if s.name == "tavily" or s.name not in project.sources_consulted]
        project.retried = True

    page_lists = await asyncio.gather(*(s.pages_for(project, ctx) for s in sources), return_exceptions=True)
    pages = []
    for s, r in zip(sources, page_lists):
        if isinstance(r, Exception):
            log.append(f"extract[{project.id}][{s.name}]: failed ({type(r).__name__}: {r})")
        else:
            pages.extend(pg for pg in r if pg.url not in project.pages_seen)

    sem = asyncio.Semaphore(settings.extract_concurrency)

    async def one(page):
        if page.kind == "json_facts":
            return page, ExtractedFacts(**json.loads(page.text))
        if llm is None:
            return page, None
        async with sem:
            try:
                return page, await llm.extract_facts(page, project, ctx.own.locality)
            except Exception as e:  # noqa: BLE001
                log.append(f"extract[{project.id}]: LLM failed on {page.url} ({type(e).__name__})")
                return page, None

    for page, facts in await asyncio.gather(*(one(pg) for pg in pages)):
        if facts is None:
            if page.source not in project.sources_consulted:
                project.sources_consulted.append(page.source)
            continue
        merge_facts(project, facts, page)
    consolidate_rera(project)

    places = next((s for s in d["sources"] if s.name == "places"), None)
    if project.lat is None:
        # A portal-discovered project often has no address at all; its name plus the locality is still worth a lookup,
        # and a project that stays unpinned is dropped for "no coordinates" rather than compared.
        query = f"{project.name} {project.address}" if project.address else f"{project.name} {ctx.own.locality or ''} Mumbai"
        hit = await _geocode(_geocoders(d), ctx, query)
        if hit is None:
            log.append(f"extract[{project.id}]: no geocoder could place '{query.strip()}'")
        else:
            (project.lat, project.lng, _), via = hit
            project.pin_accuracy = f"{via} - address"
    if project.lat is not None and ctx.own.lat is not None:
        project.distance_km = round(haversine_km(ctx.own.lat, ctx.own.lng, project.lat, project.lng), 2)
    if project.nearest_metro is None and project.lat is not None and places is not None:
        try:
            project.nearest_metro = await places.nearest_metro(ctx, project.lat, project.lng)
        except (CacheMiss, Exception):  # noqa: BLE001
            pass
    log.append(f"extract[{project.id}]: {len(pages)} pages, sources {project.sources_consulted}")
    return {"projects": [project], "log": log}


# ----------------------------------------------------------------- filter
async def filter_node(state: GraphState, config: RunnableConfig) -> dict:
    projects = eligibility.apply(state["projects"], state["own"], state["radius_km"], date.today())
    return {"projects": projects}


# ------------------------------------------------------------------ score
async def score(state: GraphState, config: RunnableConfig) -> dict:
    out = []
    for p in state["projects"]:
        completeness.apply(p)
        conflicts.apply(p)
        match_score.apply(p, state["own"], state["radius_km"])
        out.append(p)
    return {"projects": out}


def route_after_score(state: GraphState) -> str:
    if not state.get("retry_done") and any(p.eligible and p.label == "THIN" and not p.retried for p in state["projects"]):
        return "retry_thin"
    return "narrate"


# ------------------------------------------------------------- retry thin
async def retry_thin(state: GraphState, config: RunnableConfig) -> dict:
    thin = [p.name for p in state["projects"] if p.eligible and p.label == "THIN" and not p.retried]
    return {"retry_done": True, "log": [f"retry: second search pass for {thin}"]}


def fan_out_retry(state: GraphState):
    sends = [Send("extract", ExtractInput(own=state["own"], radius_km=state["radius_km"], project=p, retry=True,
                                          extra_queries=["RERA registration number possession", "carpet area price per sq ft"]))
             for p in state["projects"] if p.eligible and p.label == "THIN" and not p.retried]
    return sends or "narrate"


# ---------------------------------------------------------------- narrate
async def narrate(state: GraphState, config: RunnableConfig) -> dict:
    llm = _deps(config).get("llm")
    own = state["own"]
    eligible = [p for p in state["projects"] if p.eligible]
    for p in eligible:
        p.insight, p.insight_source = templates.card_insight(p, own, eligible), "template"
    log = []
    if llm is not None and eligible:
        set_summary = {
            "n": len(eligible),
            "possession_range": [min(p.value("possession").isoformat() for p in eligible if p.value("possession")),
                                 max(p.value("possession").isoformat() for p in eligible if p.value("possession"))] if any(p.value("possession") for p in eligible) else None,
            "base_rate_range": [min(p.value("rate_psf").min_psf for p in eligible if p.value("rate_psf") and p.value("rate_psf").basis == "base"),
                                max(p.value("rate_psf").max_psf for p in eligible if p.value("rate_psf") and p.value("rate_psf").basis == "base")] if any(p.value("rate_psf") and p.value("rate_psf").basis == "base" for p in eligible) else None,
        }
        try:
            sentences = await llm.narrate_cards(own, eligible, state["radius_km"], set_summary)
            for p in eligible:
                if p.id in sentences and sentences[p.id].strip():
                    p.insight, p.insight_source = sentences[p.id].strip(), "llm"
            log.append("narrate: LLM insights applied")
        except Exception as e:  # noqa: BLE001
            log.append(f"narrate: LLM failed ({type(e).__name__}); template sentences kept")
    return {"projects": state["projects"], "log": log}


# ---------------------------------------------------------------- persist
async def persist(state: GraphState, config: RunnableConfig) -> dict:
    db = _deps(config).get("db")
    if db is None:
        return {}
    record = ScanRecord(scan_id=state["scan_id"], own_id=state["own"].id, radius_km=state["radius_km"], mode=state["mode"],
                        projects=state["projects"], dropped=state.get("dropped", []))
    db.save_scan(record, nearest_metro=state.get("nearest_metro"), log=state.get("log", []))
    return {"log": [f"persist: scan {state['scan_id']} saved with {len(state['projects'])} projects"]}
