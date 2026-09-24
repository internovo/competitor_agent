"""Orchestration used by both the API and the tests: run a scan, shape list / detail / compare payloads."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import date
from pathlib import Path
from collections.abc import Callable
from typing import Any

from app.config import settings
from app.cost import NodeSpans, TokenBudget, classify, tokens_inr
from app.graph.build import graph
from app.llm import templates
from app.llm.client import LLM, get_llm, own_facts, own_facts_from_column
from app.logic import compare as compare_logic
from app.logic import completeness, conflicts, match_score
from app.models.schema import (REPORTED_FIELDS, FieldValue, OwnProject, Project, Provenance, ScanRecord,
                               absence_label, now_utc)
from app.sources.fetch import Fetcher
from app.sources.registry import build_sources

LABEL_ORDER = {"COMPARABLE": 0, "PARTIAL": 1, "THIN": 2, "UNVERIFIED": 3}
CATEGORY_LABELS = compare_logic.CATEGORY_LABELS


async def _invoke(graph, state, config, on_stage):
    """The graph, streamed or not. Split out so `run_scan` can wrap it in one try."""
    if on_stage is None:
        final = await graph.ainvoke(state, config=config)
    else:
        final = dict(state)
        on_stage("", {})   # start the clock after setup, so the first node is not billed for it
        # not `mode`: that is the fetch mode, and shadowing it recorded every streamed
        # scan as mode "values".
        async for stream, chunk in graph.astream(state, config=config, stream_mode=["updates", "values"]):
            if stream == "values":
                final = chunk
            else:
                for node, update in chunk.items():
                    on_stage(node, update or {})
    return final


def _partial_meta(fetcher, budget, final: dict | None = None) -> dict:
    """The cost and coverage a run had accumulated when it stopped, however it stopped.

    A failed run used to tally against an empty dict, because the only place the call
    counts lived was a local in `run_scan` that the exception unwound past. Three runs
    on 22-23 Sep reported Rs 0.67 of LLM and nothing else while having spent real money
    on Places and Tavily, so a day's true spend could not be reconstructed afterwards.
    """
    searches, places, _ = classify(fetcher.outbound)
    final = final or {}
    return {"http_calls": len(fetcher.calls), "urls": list(fetcher.outbound),
            "cache_hits": fetcher.hits,
            "searches": searches, "places_calls": places,
            "sources_unavailable": list(fetcher.refused.values()),
            "pages_fetched": final.get("pages_fetched", 0),
            "model_pages": final.get("model_pages", 0),
            "prompt_chars": final.get("prompt_chars", 0),
            "extraction": final.get("extraction", {"deterministic_fields": 0, "llm_fields": 0}),
            "token_budget": budget.body()}


async def run_scan(own: OwnProject, radius_km: float, mode: str, llm: LLM | None = None,
                   sources: list[str] | None = None, on_stage: Callable[[str, dict], None] | None = None,
                   nearby: list | None = None) -> tuple[ScanRecord, dict]:
    """Run the graph. Nothing is persisted here -- the caller owns the answer.

    `on_stage(node, update)` is called as each node finishes, so a polling client
    sees real progress rather than a spinner.

    `llm=None` means run with no model at all, and is taken literally: the caller
    that wants one builds it and passes it. Falling back to `get_llm()` here made
    "no model" unaskable -- every fixture test and every offline replay quietly
    constructed a client off whatever key was in .env and spent on it.
    """
    fetcher = Fetcher(mode, timeout=settings.fetch_timeout_s)
    budget = TokenBudget(settings.max_run_tokens)
    deps = {"sources": build_sources(mode, sources, nearby), "fetcher": fetcher, "llm": llm,
            "token_budget": budget}
    scan_id = uuid.uuid4().hex[:12]
    state = {"own": own, "radius_km": radius_km, "mode": mode, "scan_id": scan_id, "candidates": [],
             "projects": [], "dropped": [], "retry_done": False, "log": []}
    # One span per node, one per candidate at the fan-out. Exported only when a
    # collector endpoint is configured; otherwise the no-op tracer costs a dict write.
    # max_concurrency caps the extract fan-out; every other node runs alone anyway.
    config = {"configurable": deps, "recursion_limit": 50, "max_concurrency": settings.candidates_at_once,
              "callbacks": [NodeSpans(llm, scan_id, own.locality or "")]}
    try:
        final = await _invoke(graph, state, config, on_stage)
    except Exception as e:   # noqa: BLE001 - re-raised; this only attaches what was spent
        e.partial_meta = _partial_meta(fetcher, budget)
        raise
    rec = ScanRecord(scan_id=scan_id, own_id=own.id, radius_km=radius_km, mode=mode, projects=final["projects"], dropped=final.get("dropped", []))
    unavailable = list(fetcher.refused.values())
    log = final.get("log", []) + [f"source unavailable: {u['source']} ({u['reason']}, HTTP {u['status']}); "
                                  f"results are incomplete" for u in unavailable]
    meta = {"nearest_metro": final.get("nearest_metro"), "log": log, "http_calls": len(fetcher.calls),
            # Requests served from disk. Reported so the cache can be seen working, and
            # kept out of the cost, because nothing was paid for them.
            "cache_hits": fetcher.hits,
            "sources_unavailable": unavailable,
            "register_filings": final.get("register_filings", []),
            "societies": final.get("societies", []),
            "urls": list(fetcher.outbound), "pages_fetched": final.get("pages_fetched", 0),
            "extraction": final.get("extraction", {"deterministic_fields": 0, "llm_fields": 0}),
            "candidates_researched": len(final["projects"]),
            "model_pages": final.get("model_pages", 0), "prompt_chars": final.get("prompt_chars", 0),
            "untrimmed_chars": final.get("untrimmed_chars", 0),
            "ambiguous_pairs": final.get("ambiguous_pairs", 0),
            "token_budget": budget.body()}
    return rec, meta


# Concurrency is bounded here rather than in the route, so both entry points -- ours
# and propOG's /scan -- queue against the same slots.
_slots = asyncio.Semaphore(settings.max_concurrent_scans)


def _dump(run) -> None:
    """Write the run out, atomically, the moment there is anything to write.

    Off unless RUN_DUMP_DIR is set, and never a second source of truth: the canonical
    copy of a scan is still the Node API's row in Postgres. This is for the case that
    actually bit us -- a finished scan whose payload existed and was then lost, with
    no way to get it back short of paying for the scan again.

    Written to a temporary name in the same directory and moved into place, so a reader
    never sees half a file, and a failure here never fails a scan that has succeeded.
    """
    if not settings.run_dump_dir:
        return
    try:
        out = Path(settings.run_dump_dir)
        out.mkdir(parents=True, exist_ok=True)
        tmp = out / f".{run.run_id}.partial"
        tmp.write_text(json.dumps(run.body(), indent=2, default=str), encoding="utf-8")
        tmp.replace(out / f"run_{run.run_id}_{run.status}.json")
    except Exception as e:  # noqa: BLE001 - a dump that fails must not lose the run
        run.note([f"run dump failed ({type(e).__name__}); the payload is still in memory"])


async def execute(run) -> "RunRecord":
    """Run one scan, waiting for a free slot first. Failure ends the run failed, never done."""
    async with _slots:
        return await _execute(run)


async def _execute(run) -> "RunRecord":
    """Run one scan into its RunRecord. Failure ends the run failed, never done."""
    run.begin("geocode")
    run.started_at = now_utc()
    llm = get_llm()
    run.llm = llm
    last = time.perf_counter()

    def on_stage(node: str, update: dict) -> None:
        # Wall clock, partitioned across the stages: each update closes the slice
        # that ran up to it, so the per-stage seconds sum to the run's duration.
        nonlocal last
        now = time.perf_counter()
        if node:
            run.stage_seconds[node] = round(run.stage_seconds.get(node, 0.0) + now - last, 2)
            run.begin(node)
        last = now
        run.note(update.get("log", []) or [])

    try:
        rec, meta = await run_scan(run.own, run.radius_km, run.mode, llm=llm, on_stage=on_stage, nearby=run.nearby)
    except Exception as e:  # noqa: BLE001 - the record is the only place this can be reported
        # Everything spent before it broke. Without this the run reported no Places
        # and no Tavily calls at all, and the day's real bill could not be added up.
        run.tally(llm, getattr(e, "partial_meta", {}))
        run.fail(e)
        _dump(run)
        return run
    run.tally(llm, meta)
    run.finish(rec, meta, list_payload(rec, run.own, meta))
    _dump(run)
    return run


def unread(projects: list[Project]) -> list[Project]:
    """Eligible, but no source ever produced a page for them.

    A register row nobody has written about is not a competitor a rep can use; it is a
    filing with coordinates. It is still reported -- it may be real and worth a call --
    but it belongs beside the table, not in it.
    """
    return [p for p in projects if p.eligible and not p.pages_seen]


def unclassified(projects: list[Project]) -> list[Project]:
    """Eligible, researched, and no source declared a lifecycle.

    Sweeping these into the finished-drops is the same failure as inventing a value,
    pointed the other way: the project vanishes and nothing says why.
    """
    return [p for p in projects if p.eligible and p.pages_seen and p.status == "unknown"]


# Handing over earlier than the subject is not the same as being out of the market.
# A 2027 project competes perfectly well with a 2028 one -- buyers cross-shop across a
# year or two, and the first cut of this rule moved 9 of 14 Borivali rows out of the
# table for exactly that reason. What a buyer does not cross-shop is a building that
# is ready now: that is a different purchase, not a cheaper version of the same one.
# So both have to be true before a row leaves the main ranking.
EARLY_HANDOVER_MONTHS = 12      # this far ahead of the subject
READY_SOON_MONTHS = 6           # ...and handing over within this long from today
MIN_MAIN_TABLE = 5              # and never leave a rep fewer rows than this


def _months(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def hands_over_before(p: Project, own: OwnProject) -> int | None:
    """Months this project hands over ahead of the subject, if it is a long way ahead.

    Reported for every row that qualifies, whether or not the row is demoted: a rep
    comparing a 2027 building with a 2028 one should see the gap, not lose the row.
    """
    poss, own_poss = p.value("possession"), own.possession
    if poss is None or own_poss is None:
        return None
    months = _months(poss, own_poss)
    return months if months >= EARLY_HANDOVER_MONTHS else None


def is_ready_stock(p: Project, today: date) -> bool:
    """Handing over soon enough that a buyer treats it as finished, not forthcoming."""
    poss = p.value("possession")
    return poss is not None and _months(today, poss) <= READY_SOON_MONTHS


def split_early(ranked: list[Project], own: OwnProject,
                today: date | None = None) -> tuple[list[Project], list[Project]]:
    """(main table, handing over before you). `ranked` arrives strongest-first."""
    today = today or date.today()
    demoted = [p for p in ranked
               if hands_over_before(p, own) is not None and is_ready_stock(p, today)]
    # The floor is on the table a rep reads, not on the rule. If honouring the rule
    # would leave fewer than five rows, the strongest demoted ones come back and keep
    # their tag -- a short table is a worse answer than a tagged one.
    short_by = MIN_MAIN_TABLE - (len(ranked) - len(demoted))
    if short_by > 0:
        demoted = demoted[short_by:]
    return [p for p in ranked if p not in demoted], demoted


def rank(projects: list[Project]) -> list[Project]:
    """Label, then coverage, then distance. Coverage before distance is the point: sorting
    an UNVERIFIED 0/6 register row above a filled launch because it is 200 m closer put
    the emptiest rows at the top of the table."""
    eligible = [p for p in projects if p.eligible and p.pages_seen and p.status != "unknown"
                and not p.not_a_project]
    # Confirmation outranks everything. MahaRERA cannot be read, so "two independent
    # publishers name this building here" is the strongest check available, and a row
    # only one source has heard of must not sit above one that two agree on however
    # complete its form looks.
    return sorted(eligible, key=lambda p: (not confirmed(p), LABEL_ORDER[p.label],
                                           -p.completeness, p.distance_km or 99))


CONFIRMING_SOURCES_MIN = 2


def confirmed(p: Project) -> bool:
    return len(p.confirmed_by) >= CONFIRMING_SOURCES_MIN


# ------------------------------------------------------------------ list
def _status_label(p: Project) -> str:
    return {"new_launch": "New launch", "under_construction": "Under construction"}.get(p.status, p.status.replace("_", " ").title())


def _rate_block(p: Project, conflict) -> dict[str, Any] | None:
    seen = p.rate_psf.observations
    if not seen:
        return None
    fv = p.display("rate_psf")
    if fv is None:
        # Seen and then withdrawn -- sources disagreed, the figure was outside the
        # plausible band, or it was quoted for several projects at once. The card shows
        # the span and says why it is not this project's rate; it never shows it as one.
        lo, hi, basis = p.rate_span()
        return {"min": lo, "max": hi, "basis": basis, "sources": len(seen),
                "conflict": conflict.detail if conflict else p.rate_psf.label}
    return {"min": fv.value.min_psf, "max": fv.value.max_psf, "basis": fv.value.basis,
            "conflict": conflict.detail if conflict else None, "sources": len(seen)}


def absence_block(p: Project) -> dict[str, dict[str, Any]]:
    """Why each empty field is empty. Three different facts used to render as one
    'Not on file', which made every gap read as a guess."""
    out = {}
    for f in REPORTED_FIELDS:
        r = p.report(f)
        if r.absent:
            out[f] = {"reason": r.absent, "label": r.label, "span": r.span}
    return out


def propog_note(p: Project) -> str | None:
    """What the ON propOG badge means for this project, in the rep's words.

    "Builder-declared" was said of every propOG row, including one no outside source
    has ever mentioned. The two are not the same claim and no longer read the same.
    """
    if not p.on_propog:
        return None
    return "builder-declared data" if p.propog_corroborated else "Listed on propOG, not found in public sources"


def card(p: Project, rank_no: int) -> dict[str, Any]:
    carpet, rate, poss, struct, phases = (p.display(f) for f in ("carpet_sqft", "rate_psf", "possession", "structure", "rera_phases"))
    rate_conflict = next((c for c in p.conflicts if c.field == "rate_psf"), None)
    rera_verified = bool(phases and any(ph.verified for ph in phases.value))
    return {
        "rank": rank_no, "id": p.id, "name": p.display_name, "builder": p.builder, "distance_km": p.distance_km, "status": _status_label(p),
        "rera_verified": rera_verified, "on_propog": p.on_propog,
        "label": p.label, "completeness": p.completeness, "completeness_text": f"{p.label} · {p.completeness} of 6" if p.label != "COMPARABLE" else "COMPARABLE",
        "match_score": p.match_score, "score_max": p.score_max, "score_excluded": p.score_excluded,
        # A5. `earned`/`available` are the same two numbers match_score has always
        # produced; score_100 is their ratio and coverage is how many of the six
        # dimensions stand behind it. The portal displays these rather than recomputing.
        "score_100": p.score_100, "earned": p.match_score, "available": p.score_max,
        "coverage": p.score_coverage, "score_breakdown": p.score_breakdown,
        "score_note": ("not scored at this coverage" if p.match_score is None else
                       (f"compared on only {p.score_coverage} of 6 dimensions; no percentage shown"
                        if p.score_100 is None else None)),
        # A3. Who, other than us, says this building exists here.
        "confirmed": confirmed(p), "confirmed_by": p.confirmed_by,
        "confirmed_note": (f"Confirmed by {len(p.confirmed_by)} sources: {', '.join(p.confirmed_by)}"
                           if confirmed(p) else
                           (f"Only {p.confirmed_by[0]} names this project in {p.locality or 'this locality'}"
                            if p.confirmed_by else "No public source names this project in this locality")),
        # RERA numbers read off a listing are not register-verified, and must never read as if they were.
        "rera_note": ("RERA number taken from a listing, not checked on the register"
                      if (phases and not rera_verified) else None),
        "unresolved": p.unresolved,
        "analyse_enabled": p.label not in ("THIN", "UNVERIFIED"),
        "configurations": p.value("configurations"),
        "carpet_sqft": [carpet.value.min_sqft, carpet.value.max_sqft] if carpet else None,
        "rate_psf": _rate_block(p, rate_conflict),
        "possession": poss.value.isoformat() if poss else None,
        "towers": struct.value.towers if struct else None, "building_type": struct.value.building_type if struct else None,
        "insight": p.insight, "insight_source": p.insight_source,
        "conflicts": [{"field": c.field, "detail": c.detail} for c in p.conflicts],
        "status_note": p.status_note,
        "absent": absence_block(p),
        "data_note": propog_note(p) or ("RERA verified" if rera_verified else None),
    }


# Which subject field feeds which scoring dimension, for the note that tells a
# builder why nobody could be scored on price.
OWN_FIELD_FOR_DIM = {"config": "configurations", "carpet": "carpet_sqft", "rate": "rate_psf",
                     "possession": "possession", "structure": "structure"}
SORT_DESCRIPTION = "confirmed first, then tier, then coverage, then distance"


def scoring_note(own: OwnProject, scored: list[Project]) -> dict[str, Any]:
    """The weights, the sort, and which subject fields cost everybody a dimension.

    A dimension is dropped when EITHER side lacks the value, so a subject with no
    rate silently costs 20 points of denominator on every row in the table. The
    portal can only tell the builder to fill it in if we say which field it was.
    """
    always_out = set.intersection(*(set(p.score_excluded) for p in scored)) if scored else set()
    missing = sorted(OWN_FIELD_FOR_DIM[d] for d in always_out
                     if d in OWN_FIELD_FOR_DIM and not getattr(own, OWN_FIELD_FOR_DIM[d], None))
    return {
        "weights": {"config": settings.w_config, "carpet": settings.w_carpet, "rate": settings.w_rate,
                    "possession": settings.w_possession, "distance": settings.w_distance,
                    "structure": settings.w_structure},
        "total_weight": sum((settings.w_config, settings.w_carpet, settings.w_rate,
                             settings.w_possession, settings.w_distance, settings.w_structure)),
        "sort": SORT_DESCRIPTION,
        "subject_missing": missing,
        "note": (f"{own.name} has no {', '.join(missing)} on file, so that dimension is "
                 f"excluded from every score in this table." if missing else None),
    }


def _timed(row: dict[str, Any], p: Project, own: OwnProject) -> dict[str, Any]:
    """Attach the handover gap to a row that has one, wherever it ends up.

    A row that stays in the table still says how much earlier it finishes; that is
    the fact a rep needs, and losing the row was never the way to deliver it.
    """
    months = hands_over_before(p, own)
    return dict(row, months_before=months,
                early_note=f"hands over {months} months before you" if months else None)


# `eligibility.check` returns on the first rule a candidate fails, so a configuration
# reason means every earlier rule passed: it is in radius, still selling, and handing
# over in the future. The only thing wrong with it is that it sells something else.
CONFIG_DROP = "no configuration overlap"


def nearby_other_configurations(rec: ScanRecord) -> list[Project]:
    """Researched, in radius, still selling -- and selling a different size of flat.

    A 1 BHK subject excludes almost everything around it: 10 of 59 candidates on the
    Malad West run, against 0 on Kandivali and 0 on Borivali, both of which have a
    1/2/3 BHK subject. Dropping them silently leaves a rep with a one-row table and no
    way to see the neighbourhood behind it.
    """
    return [p for p in rec.projects
            if not p.eligible and (p.drop_reason or "").startswith(CONFIG_DROP) and p.pages_seen]


def list_payload(rec: ScanRecord, own: OwnProject, meta: dict) -> dict[str, Any]:
    ranked, early = split_early(rank(rec.projects), own)
    other_config = nearby_other_configurations(rec)
    return {
        "scan_id": rec.scan_id, "own": {"id": own.id, "name": own.name, "locality": own.locality}, "radius_km": rec.radius_km,
        "mode": rec.mode, "created_at": rec.created_at.isoformat(),
        # A source whose key or quota was refused did not run. A shorter list is then a
        # fact about our account, not about the neighbourhood, and the client must say so.
        "incomplete": bool(meta.get("sources_unavailable")),
        "sources_unavailable": meta.get("sources_unavailable", []),
        "counts": {"candidates_seen": len(rec.projects) + len(rec.dropped), "eligible": len(ranked),
                   "handing_over_before": len(early),
                   "nearby_other_configurations": len(other_config),
                   "comparable": sum(1 for p in ranked if p.label == "COMPARABLE"), "partial": sum(1 for p in ranked if p.label == "PARTIAL"),
                   "thin": sum(1 for p in ranked if p.label == "THIN"),
                   "unverified": sum(1 for p in ranked if p.label == "UNVERIFIED")},
        "extraction": meta.get("extraction", {}),
        # So the portal can say a row went unasked rather than silently thin.
        "token_budget": meta.get("token_budget", {}),
        "scoring": scoring_note(own, [p for p in ranked if p.match_score is not None]),
        "competitors": [_timed(card(p, i + 1), p, own)
                        for i, p in enumerate(ranked[: settings.max_table_rows])],
        # Real projects, fully researched, that hand over more than a year before the
        # subject does. A rep still wants to see them; they are just not what the
        # subject is competing against for the same buyer.
        "handing_over_before": [_timed(card(p, i + 1), p, own) for i, p in enumerate(early)],
        # Full cards, no rank and no score: they are the neighbourhood, not the ranking.
        # Same shape as `handing_over_before[]` so the portal can render them the same way.
        "nearby_other_configurations": [dict(card(p, 0), rank=None) for p in other_config],
        "nearby_other_configurations_note": (
            f"{len(other_config)} project{'s' if len(other_config) != 1 else ''} within "
            f"{rec.radius_km} km sell no "
            f"{' or '.join(str(b) for b in own.configurations)} BHK, so they are not "
            f"comparable to {own.name} and are not ranked. Shown because they are the "
            f"neighbourhood." if other_config else None),
        # The comparable fact set per project, computed once here so a compare can run
        # months later with no run in memory. The cards cannot stand in for these: they
        # carry no amenities at all, no rera phase list, and two of structure's seven
        # fields. Only projects a rep can actually select and analyse get one, so an
        # absent id and a disabled Analyse button say the same thing.
        "compare_columns": {
            "own": compare_logic._column_from_own(own),
            "competitors": {p.id: compare_logic._column_from_project(p)
                            for p in ranked[: settings.max_table_rows] if p.label not in ("THIN", "UNVERIFIED")},
        },
        "also_found": [{"id": p.id, "name": p.name, "distance_km": p.distance_km, "label": p.label,
                        "completeness": p.completeness, "reason": None} for p in ranked[settings.max_table_rows:]]
                      + [{"id": p.id, "name": p.name, "distance_km": p.distance_km, "label": p.label,
                          "completeness": p.completeness,
                          "reason": "no source produced a page about this project"} for p in unread(rec.projects)]
                      + [{"id": p.id, "name": p.name, "distance_km": p.distance_km, "label": p.label,
                          "completeness": p.completeness,
                          "reason": absence_label(None, "LIFECYCLE_UNKNOWN")} for p in unclassified(rec.projects)]
                      + [{"id": p.id, "name": p.name, "distance_km": p.distance_km, "label": p.label,
                          "completeness": p.completeness, "reason": p.not_a_project}
                         for p in rec.projects if p.eligible and p.not_a_project]
                      + meta.get("societies", []),
        "register_filings": meta.get("register_filings", []),
        "dropped": [{"id": p.id, "name": p.name, "reason": p.drop_reason} for p in rec.projects if not p.eligible] + rec.dropped,
        "log": meta.get("log", []),
    }


# ---------------------------------------------------------------- detail
def _months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def detail_payload(p: Project, own: OwnProject, rec: ScanRecord) -> dict[str, Any]:
    amen = p.display("amenities")
    grouped: dict[str, list[str]] = {}
    unit_level = 0
    if amen:
        for a in amen.value:
            grouped.setdefault(CATEGORY_LABELS[a.category], []).append(a.name)
            unit_level += a.scope == "unit"
    struct = p.display("structure")
    phases = p.display("rera_phases")
    poss = p.display("possession")
    tl = p.display("timeline")
    launched = tl.value.launched if tl else None
    today = date.today()
    months_to_handover = _months_between(today, poss.value) if poss else None
    progress = None
    if launched and poss:
        total = max(1, _months_between(launched, poss.value))
        progress = round(min(1.0, max(0.0, _months_between(launched, today) / total)), 2)

    field_sources: dict[str, list[str]] = {}
    for f in ("possession", "carpet_sqft", "structure", "rera_phases", "rate_psf", "configurations", "amenities", "timeline"):
        for fv in p.report(f).observations:
            if f not in field_sources.setdefault(fv.prov.source, []):
                field_sources[fv.prov.source].append(f)
    if p.lat is not None:
        field_sources.setdefault(p.pin_accuracy.split(" ")[0].lower() if p.pin_accuracy else "unknown", []).append("coordinates")

    phase_rows = [{"label": ph.label or f"Phase {i + 1}", "number": ph.number, "verified": ph.verified} for i, ph in enumerate(phases.value)] if phases else []
    while len(phase_rows) < 3:
        phase_rows.append({"label": f"Phase {len(phase_rows) + 1}", "number": None, "verified": False})

    return {
        "id": p.id, "name": p.name, "builder": p.builder, "status": _status_label(p), "label": p.label, "completeness": p.completeness,
        "match_score": p.match_score, "score_breakdown": p.score_breakdown, "on_propog": p.on_propog,
        "data_note": propog_note(p), "insight": p.insight,
        "amenities": {
            "count": len(amen.value) if amen else 0, "groups": grouped,
            "source": amen.prov.source if amen else None, "age_days": amen.prov.age_days if amen else None,
            "scope_note": (f"All {len(amen.value)} are project-level. No unit-level features published, so in-flat comparisons are not possible."
                           if amen and unit_level == 0 else (f"{unit_level} unit-level features published." if amen else "No amenity list on file.")),
        },
        "building": {
            "type": struct.value.building_type if struct else None, "towers": struct.value.towers if struct else None,
            "floors": [struct.value.floors_min, struct.value.floors_max] if struct and struct.value.floors_min else None,
            "land_acres": struct.value.land_acres if struct else None, "total_units": struct.value.total_units if struct else None,
            "open_space_pct": struct.value.open_space_pct if struct else None, "source": struct.prov.source if struct else None,
        },
        "rera": {"verified_count": sum(1 for r in phase_rows if r["verified"]), "phases": phase_rows,
                 "hint": None if phases else "Add a phase number; possession and carpet areas come straight off the register."},
        "timeline": {
            "launched": launched.isoformat() if launched else None,
            "possession_rera_proposed": poss.value.isoformat() if poss and poss.prov.source == "maharera" else None,
            "possession": poss.value.isoformat() if poss else None, "possession_source": poss.prov.source if poss else None,
            "extensions_filed": tl.value.extensions_filed if tl else None, "construction_stage": tl.value.construction_stage if tl else None,
            "months_to_handover": months_to_handover, "progress": progress,
        },
        "location": {"lat": p.lat, "lng": p.lng, "locality": p.locality, "address": p.address, "distance_km": p.distance_km,
                     "pin_accuracy": p.pin_accuracy, "nearest_metro": p.nearest_metro},
        "provenance": {
            "field_sources": field_sources, "sources_consulted": len(p.sources_consulted), "sources": p.sources_consulted,
            "discovered_via": p.discovered_via, "pages": p.pages_seen, "fields_with_conflict": len(p.conflicts),
            "conflicts": [c.model_dump() for c in p.conflicts], "oldest_source_days": p.oldest_source_days(),
            "could_not_verify": p.could_not_verify,
            "note": "Shown as empty rather than estimated. A rep who learns any of these from a site visit can record it here.",
        },
        "absent": absence_block(p),
        "values": {
            f: [{"value": fv.value if not hasattr(fv.value, "model_dump") else fv.value.model_dump(), "source": fv.prov.source,
                 "url": fv.prov.url, "fetched_at": fv.prov.fetched_at.isoformat(), "method": fv.method} for fv in p.report(f).observations]
            for f in ("configurations", "carpet_sqft", "rate_psf", "possession", "structure", "rera_phases")
        },
    }


# -------------------------------------------------------------- override
MANUAL_FIELDS = {"configurations", "carpet_sqft", "rate_psf", "possession", "structure", "rera_phases"}


def apply_override(p: Project, own: OwnProject, radius_km: float, field: str, value: Any) -> Project:
    if field not in MANUAL_FIELDS:
        raise ValueError(f"field must be one of {sorted(MANUAL_FIELDS)}")
    from app.models.schema import CarpetRange, RateValue, ReraPhase, Structure

    parsed = {
        "configurations": lambda v: sorted({int(x) for x in v}),
        "carpet_sqft": lambda v: CarpetRange(**v),
        "rate_psf": lambda v: RateValue(**v),
        "possession": lambda v: date.fromisoformat(v),
        "structure": lambda v: Structure(**v),
        "rera_phases": lambda v: [ReraPhase(number=x, verified=False, label=f"Phase {i + 1}") if isinstance(x, str) else ReraPhase(**x) for i, x in enumerate(v)],
    }[field](value)
    getattr(p, field).observe(FieldValue(value=parsed, prov=Provenance(source="manual", url=None), method="manual", confidence="high"))
    completeness.apply(p)
    conflicts.apply(p)
    match_score.apply(p, own, radius_km)
    return p


# --------------------------------------------------------------- compare
async def compare_payload(own: OwnProject, competitors: list[Project], radius_km: float, llm: LLM | None) -> dict[str, Any]:
    """The live path, from a run still in memory."""
    cols = [compare_logic._column_from_own(own)] + [compare_logic._column_from_project(p) for p in competitors]
    return await compare_from_columns(cols, radius_km, llm, own_facts(own))


async def compare_from_columns(cols: list[dict[str, Any]], radius_km: float, llm: LLM | None,
                               own: dict[str, Any] | None = None) -> dict[str, Any]:
    """One code path under both compare routes. `cols[0]` is the own project.

    `own` is the narration's view of the own project; from the OwnProject on the live
    path, off cols[0] on the stateless one. Same keys either way.
    """
    own = own if own is not None else own_facts_from_column(cols[0])
    payload = compare_logic.build_from_columns(cols, radius_km)
    payload["insights"] = templates.compare_insights(payload, own)
    # ALWAYS emitted, on both paths. A template sentence and a model sentence are
    # different claims, and an absent field reads as the pessimistic one -- which would
    # label a real model insight a template. The screen shows which it has.
    payload["insight_source"] = "template"
    if llm is not None:
        try:
            payload["insights"] = await llm.narrate_compare(own, payload)
            payload["insight_source"] = "llm"
        except Exception as e:  # noqa: BLE001
            payload["insight_error"] = f"{type(e).__name__}"
    payload["cost"] = compare_cost(llm)
    return payload


def compare_cost(llm: LLM | None) -> dict[str, Any]:
    """What this one compare spent. About Rs 2, and reported for the same reason the
    scan's cost is: a spend nobody counts is a spend nobody notices, and one project's
    forty compares stop being Rs 2 quite quickly. An estimate from the price table,
    never a bill -- same caveat as the scan."""
    if llm is None:
        return {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated_inr": 0.0, "model": None,
                "note": "no model configured; insights are template-written"}
    return {"llm_calls": llm.calls_attempted, "input_tokens": llm.input_tokens, "output_tokens": llm.output_tokens,
            "estimated_inr": tokens_inr(llm.model, llm.input_tokens, llm.output_tokens), "model": llm.model,
            "note": "estimated from token counts and the configured price table, not a bill"}
