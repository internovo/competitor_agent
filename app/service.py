"""Orchestration used by both the API and the tests: run a scan, shape list / detail / compare payloads."""
from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from app.config import settings
from app.graph.build import graph
from app.llm import templates
from app.llm.client import LLM, get_llm
from app.logic import compare as compare_logic
from app.logic import completeness, conflicts, match_score
from app.models.schema import FieldValue, OwnProject, Project, Provenance, ScanRecord
from app.sources.fetch import Fetcher
from app.sources.registry import build_sources
from app.storage.db import Database

LABEL_ORDER = {"COMPARABLE": 0, "PARTIAL": 1, "THIN": 2}
CATEGORY_LABELS = compare_logic.CATEGORY_LABELS


async def run_scan(own: OwnProject, radius_km: float, mode: str, db: Database | None, llm: LLM | None = None,
                   sources: list[str] | None = None) -> tuple[ScanRecord, dict]:
    fetcher = Fetcher(mode)
    deps = {"sources": build_sources(mode, sources), "fetcher": fetcher, "llm": llm if llm is not None else get_llm(), "db": db}
    scan_id = uuid.uuid4().hex[:12]
    final = await graph.ainvoke(
        {"own": own, "radius_km": radius_km, "mode": mode, "scan_id": scan_id, "candidates": [], "projects": [], "dropped": [], "retry_done": False, "log": []},
        config={"configurable": deps, "recursion_limit": 50},
    )
    rec = ScanRecord(scan_id=scan_id, own_id=own.id, radius_km=radius_km, mode=mode, projects=final["projects"], dropped=final.get("dropped", []))
    meta = {"nearest_metro": final.get("nearest_metro"), "log": final.get("log", []), "http_calls": len(fetcher.calls)}
    return rec, meta


def rank(projects: list[Project]) -> list[Project]:
    eligible = [p for p in projects if p.eligible]
    return sorted(eligible, key=lambda p: (LABEL_ORDER[p.label], -(p.match_score or -1), p.distance_km or 99))


# ------------------------------------------------------------------ list
def _status_label(p: Project) -> str:
    return {"new_launch": "New launch", "under_construction": "Under construction"}.get(p.status, p.status.replace("_", " ").title())


def _rate_block(p: Project, conflict) -> dict[str, Any] | None:
    if not p.rate_psf:
        return None
    if conflict:
        lo, hi, basis = p.rate_span()
        return {"min": lo, "max": hi, "basis": basis, "conflict": conflict.detail, "sources": len(p.rate_psf)}
    fv = p.display("rate_psf")
    return {"min": fv.value.min_psf, "max": fv.value.max_psf, "basis": fv.value.basis, "conflict": None, "sources": len(p.rate_psf)}


def card(p: Project, rank_no: int) -> dict[str, Any]:
    carpet, rate, poss, struct, phases = (p.display(f) for f in ("carpet_sqft", "rate_psf", "possession", "structure", "rera_phases"))
    rate_conflict = next((c for c in p.conflicts if c.field == "rate_psf"), None)
    rera_verified = bool(phases and any(ph.verified for ph in phases.value))
    return {
        "rank": rank_no, "id": p.id, "name": p.name, "builder": p.builder, "distance_km": p.distance_km, "status": _status_label(p),
        "rera_verified": rera_verified, "on_propog": p.on_propog,
        "label": p.label, "completeness": p.completeness, "completeness_text": f"{p.label} · {p.completeness} of 6" if p.label != "COMPARABLE" else "COMPARABLE",
        "match_score": p.match_score, "score_note": None if p.match_score is not None else "not scored at this coverage",
        "analyse_enabled": p.label != "THIN",
        "configurations": p.value("configurations"),
        "carpet_sqft": [carpet.value.min_sqft, carpet.value.max_sqft] if carpet else None,
        "rate_psf": _rate_block(p, rate_conflict),
        "possession": poss.value.isoformat() if poss else None,
        "towers": struct.value.towers if struct else None, "building_type": struct.value.building_type if struct else None,
        "insight": p.insight, "insight_source": p.insight_source,
        "data_note": "builder-declared data" if p.on_propog else ("RERA verified" if rera_verified else None),
    }


def list_payload(rec: ScanRecord, own: OwnProject, meta: dict) -> dict[str, Any]:
    ranked = rank(rec.projects)
    return {
        "scan_id": rec.scan_id, "own": {"id": own.id, "name": own.name, "locality": own.locality}, "radius_km": rec.radius_km,
        "mode": rec.mode, "created_at": rec.created_at.isoformat(),
        "counts": {"candidates_seen": len(rec.projects) + len(rec.dropped), "eligible": len(ranked),
                   "comparable": sum(1 for p in ranked if p.label == "COMPARABLE"), "partial": sum(1 for p in ranked if p.label == "PARTIAL"),
                   "thin": sum(1 for p in ranked if p.label == "THIN")},
        "competitors": [card(p, i + 1) for i, p in enumerate(ranked)],
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
        for fv in getattr(p, f):
            if f not in field_sources.setdefault(fv.prov.source, []):
                field_sources[fv.prov.source].append(f)
    if p.lat is not None:
        field_sources.setdefault(p.pin_accuracy.split(" ")[0].lower() if p.pin_accuracy else "unknown", []).append("coordinates")

    phase_rows = [{"label": ph.label or f"Phase {i + 1}", "number": ph.number, "verified": ph.verified} for i, ph in enumerate(phases.value)] if phases else []
    while len(phase_rows) < 3:
        phase_rows.append({"label": f"Phase {len(phase_rows) + 1}", "number": None, "verified": False})

    return {
        "id": p.id, "name": p.name, "builder": p.builder, "status": _status_label(p), "label": p.label, "completeness": p.completeness,
        "match_score": p.match_score, "score_breakdown": p.score_breakdown, "on_propog": p.on_propog, "insight": p.insight,
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
        "values": {
            f: [{"value": fv.value if not hasattr(fv.value, "model_dump") else fv.value.model_dump(), "source": fv.prov.source,
                 "url": fv.prov.url, "fetched_at": fv.prov.fetched_at.isoformat()} for fv in getattr(p, f)]
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
    getattr(p, field).append(FieldValue(value=parsed, prov=Provenance(source="manual", url=None)))
    completeness.apply(p)
    conflicts.apply(p)
    match_score.apply(p, own, radius_km)
    return p


# --------------------------------------------------------------- compare
async def compare_payload(own: OwnProject, competitors: list[Project], radius_km: float, llm: LLM | None) -> dict[str, Any]:
    payload = compare_logic.build(own, competitors, radius_km)
    payload["insights"] = templates.compare_insights(payload, own)
    payload["insight_source"] = "template"
    if llm is not None:
        try:
            payload["insights"] = await llm.narrate_compare(own, payload)
            payload["insight_source"] = "llm"
        except Exception as e:  # noqa: BLE001
            payload["insight_error"] = f"{type(e).__name__}"
    return payload
