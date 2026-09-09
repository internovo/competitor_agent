"""All graph nodes. Runtime dependencies (sources, fetcher, llm, db) come in through config['configurable']."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import date

from langchain_core.runnables import RunnableConfig
from langgraph.types import Send

from app.config import settings
from app.graph.state import ExtractInput, GraphState
from app.llm import templates
from app.llm.client import ExtractionDegraded, FatalLLMError
from app.logic import completeness, conflicts, eligibility, match_score, resolve
from app.logic.geo import haversine_km
from app.extract import deterministic
from app.logic.merge import consolidate_configurations, consolidate_rera, merge_facts
from app.models.schema import (
    REPORTED_FIELDS, Candidate, ExtractedFacts, FieldValue, Page, Project, Provenance, ReraPhase, ScanRecord,
)
from app.sources.base import ScanContext
from app.sources.fetch import CacheMiss

COORD_PRIORITY = ["manual", "propog", "places", "maharera", "fixture", "osm", "builder_site", "squareyards", "housing", "99acres", "magicbricks", "tavily"]


def _deps(config: RunnableConfig) -> dict:
    return config["configurable"]


def _geocoders(d: dict) -> list:
    """Every enabled source that can turn an address into coordinates, best first (Places, then OSM)."""
    return [s for s in d["sources"] if hasattr(s, "geocode")]


async def _geocode(sources: list, ctx: ScanContext, query: str,
                   attempts: list[str] | None = None) -> tuple[tuple[float, float, str], str] | None:
    """First geocoder with an answer wins; a dead key or a miss falls through to the next.

    `attempts` collects one note per geocoder actually called, so a caller can tell
    "we asked and nobody knew" from "nobody was asked".
    """
    for s in sources:
        try:
            g = await s.geocode(ctx, query)
        except Exception as e:  # noqa: BLE001 - a geocoder that is down is not a reason to lose the project
            if attempts is not None:
                attempts.append(f"{s.name} errored ({type(e).__name__})")
            continue
        if attempts is not None:
            attempts.append(f"{s.name} {'answered' if g else 'had no match'}")
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
            for c in r:
                c.register_only = c.source == "maharera" and resolve.looks_like_company(c.name)
            cands.extend(r)
            n_reg = sum(1 for c in r if c.register_only)
            log.append(f"discover[{s.name}]: {len(r)} candidates" + (f", {n_reg} register-only" if n_reg else ""))
    return {"candidates": cands, "log": log}


# ---------------------------------------------------------------- resolve
async def resolve_node(state: GraphState, config: RunnableConfig) -> dict:
    llm = _deps(config).get("llm")
    own = state["own"]
    groups: list[tuple[Candidate, list[Candidate]]] = []
    log = []
    placeholders = resolve.placeholder_pins(state["candidates"])
    asked = ambiguous = 0
    for c in state["candidates"]:
        placed = False
        for rep, members in groups:
            if not resolve.could_be_same(rep, c, placeholders):
                continue
            verdict = resolve.decide(rep, c)
            ambiguous += verdict is None
            if verdict is None and llm is not None and asked < settings.resolve_max_llm_pairs:
                asked += 1
                try:
                    v = await llm.same_project(rep, c)
                    verdict = v.same_project and v.confidence >= 0.6
                    log.append(f"resolve: LLM says {'same' if verdict else 'different'} for '{rep.name}' vs '{c.name}' ({v.reason})")
                except FatalLLMError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.append(f"resolve: LLM match failed ({type(e).__name__}); treating as different")
                    verdict = False
            if verdict is None:
                verdict = False   # out of model budget, or no model: never guess toward merging
            if verdict:
                resolve.merge_into(rep, c, COORD_PRIORITY)
                members.append(c)
                placed = True
                break
        if not placed:
            rep = c.model_copy()
            rep.source_url = resolve.page_url(c)
            groups.append((rep, [c]))

    projects: list[Project] = []
    dropped: list[dict] = []
    filings: list[dict] = []
    societies: list[dict] = []
    margin = state["radius_km"] + settings.discovery_margin_km
    own_reras = {ph.number for ph in own.rera_phases}
    own_slug = resolve.slug(own.name)
    own_card = Candidate(name=own.name, builder=own.builder, lat=own.lat, lng=own.lng, address=own.address,
                         locality=own.locality, source="manual")
    for rep, members in groups:
        pid = rep.rera_no or resolve.slug(rep.name)
        # The register and the portals both list the builder's own project; it is the baseline, not a competitor.
        # Identity only -- RERA number or name. A shared lifecycle or builder is not identity.
        if (rep.rera_no and rep.rera_no in own_reras) or resolve.slug(rep.name) == own_slug or resolve.decide(own_card, rep) is True:
            dropped.append({"id": pid, "name": rep.name, "reason": "own project"})
            continue
        # A promoter's register filing is not a competitor a rep can use, and researching
        # it cannot make it one: the name is unsearchable. It is reported, not researched.
        if resolve.looks_like_society(rep.name):
            societies.append({"id": pid, "name": rep.name, "label": "UNVERIFIED", "completeness": 0,
                              "distance_km": round(haversine_km(own.lat, own.lng, rep.lat, rep.lng), 2) if rep.lat is not None else None,
                              "reason": "a registered co-operative housing society, not a project on sale"})
            continue
        if rep.register_only or resolve.looks_like_company(rep.name):
            filings.append({"id": pid, "name": rep.name, "rera_no": rep.rera_no,
                            "distance_km": round(haversine_km(own.lat, own.lng, rep.lat, rep.lng), 2) if rep.lat is not None else None,
                            "reason": "MahaRERA row named for the promoter, not a marketed project"})
            continue
        p = Project(id=pid, name=resolve.clean_project_name(rep.name, rep.builder, own.locality), name_raw=rep.name,
                    builder=rep.builder, lat=rep.lat, lng=rep.lng, address=rep.address,
                    locality=rep.locality, on_propog=rep.on_propog, nearest_metro=rep.nearest_metro,
                    source_url=rep.source_url, discovered_via=sorted({m.source for m in members}))
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
    log.append(f"resolve: {len(state['candidates'])} candidates -> {len(groups)} groups, {len(projects)} to research, "
               f"{len(filings)} register filings, {len(societies)} societies, {len(dropped)} dropped early, "
               f"{asked} model pairs asked of {ambiguous} ambiguous")
    return {"projects": projects, "dropped": dropped, "register_filings": filings,
            "societies": societies, "log": log,
            "ambiguous_pairs": min(ambiguous, settings.resolve_max_llm_pairs)}


def fan_out_extract(state: GraphState):
    sends = [Send("extract", ExtractInput(own=state["own"], radius_km=state["radius_km"], project=p, retry=False, extra_queries=[]))
             for p in state["projects"]]
    return sends or "filter"  # nothing discovered: still run the rest so the scan is recorded


# ---------------------------------------------------------------- extract
# Every field of the form a page can fill.
FILLABLE = REPORTED_FIELDS + ("builder", "status")

# Which keys of ExtractedFacts feed each field of the form.
FACT_KEYS: dict[str, tuple[str, ...]] = {
    "configurations": ("configurations",),
    "carpet_sqft": ("carpet_min_sqft", "carpet_max_sqft"),
    "rate_psf": ("rate_min_psf", "rate_max_psf", "rate_basis"),
    "possession": ("possession",),
    "structure": ("building_type", "towers", "floors_min", "floors_max", "land_acres",
                  "total_units", "open_space_pct"),
    "rera_phases": ("rera_numbers",),
    "amenities": ("amenities",),
    "timeline": ("launched", "extensions_filed", "construction_stage"),
    "builder": ("builder",),
    "status": ("status",),
}


def _has(project: Project, field: str) -> bool:
    if field == "builder":
        return bool(project.builder)
    if field == "status":
        return project.status != "unknown"
    return bool(project.report(field).observations)


def _still_missing(project: Project) -> list[str]:
    """What the regexes could not answer. Only this goes to Claude -- do not ask a
    model for an answer you already have."""
    return [f for f in FILLABLE if not _has(project, f)]


def _fills(facts: ExtractedFacts) -> set[str]:
    return {f for f, keys in FACT_KEYS.items() if any(getattr(facts, k, None) is not None for k in keys)}


def _only(facts: ExtractedFacts, want: set[str]) -> ExtractedFacts:
    """Drop anything the model answered that we did not ask for.

    Deterministic wins on conflict, and this is where it says so: a field the
    regexes already filled is never overwritten by a model's second opinion.
    """
    allowed = {k for f in want for k in FACT_KEYS.get(f, ())}
    # Location is never read deterministically, so it always passes.
    allowed |= {"name", "address", "locality", "lat", "lng"}
    return ExtractedFacts(**{k: v for k, v in facts.model_dump().items() if k in allowed})


async def _seed_page(project: Project, ctx: ScanContext, log: list[str]) -> list[Page]:
    """Fetch the URL discovery pulled this project's name from, if there is one."""
    from app.sources.fetch import html_to_text
    from app.sources.tavily_web import _source_for

    url = project.source_url
    if not url or url in project.pages_seen:
        return []
    try:
        res = await ctx.fetcher.get(url, impersonate=True)
        text = html_to_text(res.text, settings.max_page_chars) if res.ok else ""
    except Exception as e:  # noqa: BLE001 - a dead discovery URL is not a reason to lose the project
        log.append(f"extract[{project.id}]: source_url fetch failed ({type(e).__name__})")
        return []
    if len(text) < 200:
        log.append(f"extract[{project.id}]: source_url returned nothing readable ({url})")
        return []
    log.append(f"extract[{project.id}]: source_url fetched ({url})")
    return [Page(url=url, source=_source_for(url), text=text, fetched_at=res.fetched_at)]


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

    # --- 0. the page discovery already found --------------------------------
    # A candidate found by a portal or a search demonstrably has a page and we already
    # know its URL. Reading it costs one fetch; finding it again costs a search.
    # One wall-clock budget for this candidate's whole research. Running out is a normal
    # outcome: it keeps what it already has and the rest takes an absence reason.
    deadline = time.monotonic() + settings.candidate_budget_s
    left = lambda: max(0.05, deadline - time.monotonic())  # noqa: E731

    seed = [] if state["retry"] else await _seed_page(project, ctx, log)

    page_lists = await asyncio.gather(
        *(asyncio.wait_for(s.pages_for(project, ctx), left()) for s in sources), return_exceptions=True)
    pages = list(seed)
    seen_urls = {p.url for p in seed} | set(project.pages_seen)
    for s, r in zip(sources, page_lists):
        if isinstance(r, Exception):
            log.append(f"extract[{project.id}][{s.name}]: failed ({type(r).__name__}: {r})")
            continue
        for pg in r:
            if pg.url not in seen_urls:
                seen_urls.add(pg.url)
                pages.append(pg)

    # --- 1. deterministic first: pure, no network, microseconds -------------
    prose = [p for p in pages if p.kind == "html"]
    # Why a field is empty, from whatever the pages we read did say. "We read four
    # pages and none quoted a rate" is not the same fact as "we never found a page".
    why: dict[str, tuple[str, list | None]] = {}
    # Pages that never name the project are about a neighbour, and with twenty
    # pages per project those foreign pages decide the answer.
    keep = {p.url for p in deterministic.relevant_pages(prose, project.match_name)}
    n_det = 0
    for page in pages:
        if page.kind == "json_facts":
            # A fixture or a propOG record is already the form, not prose.
            facts = ExtractedFacts(**json.loads(page.text))
            merge_facts(project, facts, page, method="deterministic")
            filled = _fills(facts)
            n_det += len(filled)
            # A form that does not state a field is a source that did not publish it.
            why |= {f: ("NOT_PUBLISHED", None) for f in FILLABLE if f not in filled}
            continue
        if page.url not in keep:
            log.append(f"extract[{project.id}]: {page.url} never names the project; not read")
            continue
        facts, confidence, evidence, filled, _, page_why = deterministic.read(page, project.match_name)
        merge_facts(project, facts, page, method="deterministic", confidence=confidence, evidence=evidence)
        n_det += len(filled)
        why |= page_why

    # Lifecycle across ALL pages, from the pages that DECLARE one. The vote is
    # authoritative rather than first-writer-wins: a status set from prose, or by the model
    # reading prose, is exactly what this is here to overrule.
    read_pages = [p for p in prose if p.url in keep]
    if read_pages:
        lifecycle, evidence, votes = deterministic.vote_lifecycle(
            [deterministic.focus_on_project(p.text, project.match_name) for p in read_pages])
        register_said = project.status != "unknown" and any(pg.source == "maharera" for pg in pages)
        if lifecycle not in deterministic.UNMAPPED_LIFECYCLES | {"unknown"}:
            project.status = lifecycle
            log.append(f"extract[{project.id}]: status {lifecycle} declared, votes {votes} ({evidence})")
        elif not register_said and project.status != "unknown":
            log.append(f"extract[{project.id}]: status '{project.status}' had no declared statement behind it; unknown")
            project.status = "unknown"

    # A possession date well in the future contradicts a "ready" badge, and the date is the
    # value with a source behind it. The reverse is left alone: a delayed project is still
    # a competitor, and there the date is the likelier stale half.
    poss = project.value("possession")
    if poss is not None and project.status in ("ready", "completed"):
        months = (poss.year - date.today().year) * 12 + (poss.month - date.today().month)
        if months > settings.lifecycle_possession_margin_months:
            project.status_note = (f"a source called this {project.status}, but possession is stated as "
                                   f"{poss.isoformat()}, {months} months out; read as under construction")
            project.status = "under_construction"
            log.append(f"extract[{project.id}]: {project.status_note}")

    # --- 2. only what is still missing goes to Claude -----------------------
    want = set(_still_missing(project))
    n_llm = 0
    timed_out = deadline - time.monotonic() <= 0
    if want and llm is not None and read_pages and not timed_out:
        sem = asyncio.Semaphore(settings.extract_concurrency)

        async def one(page):
            async with sem:
                try:
                    return page, await llm.extract_facts(page, project, ctx.own.locality, sorted(want))
                except FatalLLMError:
                    raise            # a bad key fails every page; that is a broken run, not a thin one
                except Exception as e:  # noqa: BLE001
                    log.append(f"extract[{project.id}]: LLM failed on {page.url} ({type(e).__name__})")
                    return page, None

        try:
            done = await asyncio.wait_for(asyncio.gather(*(one(pg) for pg in read_pages)), left())
        except TimeoutError:
            done, timed_out = [], True
            log.append(f"extract[{project.id}]: out of budget after {settings.candidate_budget_s}s; keeping what it has")
        for page, facts in done:
            if facts is None:
                continue
            before = {f for f in FILLABLE if _has(project, f)}
            merge_facts(project, _only(facts, want), page, method="llm")
            n_llm += len({f for f in FILLABLE if _has(project, f)} - before)

    consolidate_rera(project)
    consolidate_configurations(project)
    # An absence keeps the most specific reason any source gave it.
    for f in REPORTED_FIELDS:
        report = project.report(f)
        if report.observations:
            continue
        if timed_out:
            report.mark_absent("RESEARCH_TIMED_OUT")
        elif f in why:
            report.mark_absent(*why[f])
    for page in pages:
        if page.source not in project.sources_consulted:
            project.sources_consulted.append(page.source)

    places = next((s for s in d["sources"] if s.name == "places"), None)
    if project.lat is None:
        # A portal-discovered project often has no address at all; its name plus the locality is still worth a lookup,
        # and a project that stays unpinned is dropped for "no coordinates" rather than compared.
        query = f"{project.match_name} {project.address}" if project.address else f"{project.match_name} {ctx.own.locality or ''} Mumbai"
        attempts: list[str] = []
        hit = await _geocode(_geocoders(d), ctx, query, attempts)
        # One line per unplaced candidate, so "we asked and nobody knew" can be told
        # apart from "nobody was asked". They are different fixes.
        log.append(f"geocode-audit[{project.id}]: {project.name!r} | discovery gave "
                   f"via={'+'.join(project.discovered_via) or 'none'} address={project.address!r} "
                   f"locality={project.locality!r} | query={query.strip()!r} | "
                   + ("no geocoder enabled; none was issued" if not attempts else "; ".join(attempts)))
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
    log.append(f"extract[{project.id}]: {len(pages)} pages, {n_det} fields deterministic, "
               f"{n_llm} from Claude, sources {project.sources_consulted}")
    # The pages the model was given, or would have been given had one been configured.
    # Counted either way, so an offline replay can still answer "what would this cost".
    billable = read_pages if want else []
    return {"projects": [project], "log": log, "pages_fetched": len(pages),
            "model_pages": len(billable),
            "prompt_chars": sum(len(pg.text[: settings.max_page_chars]) for pg in billable),
            "extraction": {"deterministic_fields": n_det, "llm_fields": n_llm}}


# ----------------------------------------------------------------- filter
async def filter_node(state: GraphState, config: RunnableConfig) -> dict:
    projects = eligibility.apply(state["projects"], state["own"], state["radius_km"], date.today())
    return {"projects": projects}


# ------------------------------------------------------------------ score
async def score(state: GraphState, config: RunnableConfig) -> dict:
    # One anchor for the whole set, so the plausibility band cannot mean two things in
    # one scan. Conflicts run before completeness: they are what mark a field unresolved,
    # and the label depends on that.
    anchor = conflicts.anchor_for(state["own"], state["projects"])
    carpet_anchor = conflicts.carpet_anchor_for(state["own"])
    shared = conflicts.shared_rates(state["projects"])
    out = []
    for p in state["projects"]:
        conflicts.apply(p, anchor, shared, carpet_anchor)
        completeness.apply(p)
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
# Above this share of failed model calls the run is not thin, it is broken.
MAX_LLM_FAILURE_RATE = 0.25


async def narrate(state: GraphState, config: RunnableConfig) -> dict:
    llm = _deps(config).get("llm")
    own = state["own"]
    if llm is not None and llm.calls_failed and llm.failure_rate > MAX_LLM_FAILURE_RATE:
        # Never let a partially-extracted run present itself as a complete one.
        raise ExtractionDegraded(
            f"{llm.calls_failed} of {llm.calls_attempted} model calls failed "
            f"({llm.failure_rate:.0%}); the competitor set would read as empty rather than broken",
            stage="narrate")
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
        except FatalLLMError:
            raise
        except Exception as e:  # noqa: BLE001
            log.append(f"narrate: LLM failed ({type(e).__name__}); template sentences kept")
    return {"projects": state["projects"], "log": log}


# ---------------------------------------------------------------- persist
async def persist(state: GraphState, config: RunnableConfig) -> dict:
    """The agent stores nothing. This stage exists to stamp the answer as finished
    and hand it back; the canonical copy is written by the API, against Postgres,
    where tenancy is enforced in one place."""
    return {"log": [f"persist: scan {state['scan_id']} complete with {len(state['projects'])} projects"]}
