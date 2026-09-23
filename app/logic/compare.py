"""Build the side-by-side payload: own project + up to two competitors."""
from __future__ import annotations

from datetime import date
from typing import Any

from app.models.schema import Amenity, OwnProject, Project

CATEGORY_LABELS = {
    "sport_fitness": "Sport & fitness",
    "social_leisure": "Social & leisure",
    "convenience_safety": "Convenience & safety",
    "other": "Other",
}


def _months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def _mid(rate: dict[str, Any]) -> float:
    return (rate["min"] + rate["max"]) / 2


def _on_axis(col: dict[str, Any]) -> bool:
    """A rate belongs on the shared axis only if it is a base rate no source disputes."""
    return bool(col["rate"] and col["rate"]["basis"] == "base" and not col["rate"]["conflict"])


def _delta_pct(value: float, baseline: float | None) -> float | None:
    """The one rate delta in this payload, against the one baseline: the own project.

    Pairwise gaps carried no baseline, so every reader picked its own -- the template
    measured against the own project, the model against whichever number it liked, and
    match_score against a third. A percentage nobody can reproduce is worse than none.
    """
    return None if baseline is None else round((value - baseline) / baseline * 100, 1)


def _column_from_own(own: OwnProject) -> dict[str, Any]:
    return {
        "id": own.id, "name": own.name, "builder": own.builder, "is_own": True, "distance_km": 0.0,
        "label": "OWN", "match_score": None, "tags": ["OWN PROJECT"],
        "configurations": own.configurations,
        # A field propOG has not sent reads exactly like a competitor field no source
        # published: the key is None, and the same downstream code says why it is
        # missing. Nulls inside the dict would have travelled as a rate of None-None.
        "carpet": {"min": own.carpet_sqft.min_sqft, "max": own.carpet_sqft.max_sqft,
                   "source": "propog"} if own.carpet_sqft else None,
        "rate": {"min": own.rate_psf.min_psf, "max": own.rate_psf.max_psf, "basis": own.rate_psf.basis,
                 "source": "propog", "conflict": None} if own.rate_psf else None,
        "possession": {"date": own.possession.isoformat(), "source": "propog"} if own.possession else None,
        "structure": own.structure.model_dump() if own.structure else None,
        "rera_phases": [p.model_dump() for p in own.rera_phases],
        "rera_verified_count": sum(1 for p in own.rera_phases if p.verified),
        "amenities": [a.model_dump() for a in own.amenities],
        "amenity_age_days": 0,
    }


def _column_from_project(p: Project) -> dict[str, Any]:
    carpet = p.display("carpet_sqft")
    rate = p.display("rate_psf")
    poss = p.display("possession")
    struct = p.display("structure")
    phases = p.display("rera_phases")
    amen = p.display("amenities")
    rate_conflict = next((c for c in p.conflicts if c.field == "rate_psf"), None)
    if rate_conflict:
        lo, hi, basis = p.rate_span()
        rate_block = {"min": lo, "max": hi, "basis": basis, "source": "multiple", "conflict": rate_conflict.detail}
    elif rate:
        rate_block = {"min": rate.value.min_psf, "max": rate.value.max_psf, "basis": rate.value.basis, "source": rate.prov.source, "conflict": None}
    else:
        rate_block = None
    tags = [f"{p.label} {p.completeness}/6"]
    if phases and any(ph.verified for ph in phases.value):
        tags.append("RERA")
    if p.on_propog:
        tags.append("ON propOG")
    return {
        "id": p.id, "name": p.display_name, "builder": p.builder, "is_own": False, "distance_km": p.distance_km,
        "label": p.label, "completeness": p.completeness, "match_score": p.match_score, "tags": tags,
        "configurations": p.value("configurations"),
        "carpet": {"min": carpet.value.min_sqft, "max": carpet.value.max_sqft, "source": carpet.prov.source} if carpet else None,
        "rate": rate_block,
        "possession": {"date": poss.value.isoformat(), "source": poss.prov.source} if poss else None,
        "structure": struct.value.model_dump() if struct else None,
        "rera_phases": [ph.model_dump() for ph in phases.value] if phases else [],
        "rera_verified_count": sum(1 for ph in phases.value if ph.verified) if phases else 0,
        "amenities": [a.model_dump() for a in amen.value] if amen else None,
        "amenity_age_days": amen.prov.age_days if amen else None,
    }


def build(own: OwnProject, competitors: list[Project], radius_km: float) -> dict[str, Any]:
    """The live path: build the columns from the run still in memory, then compare them."""
    return build_from_columns([_column_from_own(own)] + [_column_from_project(p) for p in competitors], radius_km)


def build_from_columns(cols: list[dict[str, Any]], radius_km: float) -> dict[str, Any]:
    """Everything below this line reads `cols` and nothing else, which is what makes a
    stateless compare possible at all.

    A column is the whole comparable fact set for one project. The card in the scan list
    is NOT one: it carries no amenities at any depth, no rera phase list, and two of
    structure's seven fields. Comparing from cards would quietly answer a different
    question, so the columns are computed once at scan time and stored beside the cards.

    cols[0] is the own project. Every downstream section assumes that.
    """
    own_name, n_comp = cols[0]["name"], len(cols) - 1


    # --- common BHK ---------------------------------------------------------
    sets = [set(c["configurations"]) for c in cols if c["configurations"]]
    common = set.intersection(*sets) if sets else set()
    common_bhk = max(common) if common else None

    # --- rate axis ----------------------------------------------------------
    on_axis = [c for c in cols if _on_axis(c)]
    off_axis = []
    for c in cols:
        if _on_axis(c):
            continue
        if c["rate"] is None:
            off_axis.append({"id": c["id"], "name": c["name"], "reason": "Rate not published"})
        elif c["rate"]["conflict"]:
            off_axis.append({"id": c["id"], "name": c["name"], "range": [c["rate"]["min"], c["rate"]["max"]],
                             "reason": f"{c['rate']['conflict']}. Basis {c['rate']['basis']}. Cannot be placed on the axis."})
        else:
            off_axis.append({"id": c["id"], "name": c["name"], "range": [c["rate"]["min"], c["rate"]["max"]],
                             "reason": f"Basis is '{c['rate']['basis']}', not base rate"})
    baseline = _mid(cols[0]["rate"]) if _on_axis(cols[0]) else None
    axis_points = [{"id": c["id"], "name": c["name"], "value": round(_mid(c["rate"])),
                    "delta_pct": _delta_pct(_mid(c["rate"]), baseline)} for c in on_axis]
    axis_vals = [p["value"] for p in axis_points]
    rate_axis = {
        "basis": "base", "points": axis_points, "off_axis": off_axis,
        "baseline": {"id": cols[0]["id"], "name": cols[0]["name"], "value": round(baseline)} if baseline else None,
        "baseline_note": ("delta_pct is the base-rate midpoint against the own project's, computed here"
                          if baseline else "the own project has no base rate on the axis, so no delta can be stated"),
        "axis_min": (min(axis_vals) // 1000 - 2) * 1000 if axis_vals else None,
        "axis_max": (max(axis_vals) // 1000 + 3) * 1000 if axis_vals else None,
        "coverage": f"{len(axis_points)} of {len(cols)} projects",
    }

    # --- possession timeline ----------------------------------------------
    poss = [{"id": c["id"], "name": c["name"], "date": c["possession"]["date"], "source": c["possession"]["source"]}
            for c in cols if c["possession"]]
    poss.sort(key=lambda x: x["date"])
    poss_gaps = []
    for i in range(len(poss)):
        for j in range(i + 1, len(poss)):
            a, b = date.fromisoformat(poss[i]["date"]), date.fromisoformat(poss[j]["date"])
            poss_gaps.append({"a": poss[i]["id"], "b": poss[j]["id"], "months": _months_between(a, b)})
    poss_sources = {p["source"] for p in poss}

    # --- carpet bars --------------------------------------------------------
    bars = [{"id": c["id"], "name": c["name"], "min": c["carpet"]["min"], "max": c["carpet"]["max"]} if c["carpet"]
            else {"id": c["id"], "name": c["name"], "min": None, "max": None, "reason": "Not disclosed"} for c in cols]
    disclosed = [b for b in bars if b["min"] is not None]
    overlap = None
    if len(disclosed) >= 2:
        lo, hi = max(b["min"] for b in disclosed), min(b["max"] for b in disclosed)
        overlap = [lo, hi] if lo < hi else None
    carpet = {"bars": bars, "overlap": overlap,
              "axis_min": min(b["min"] for b in disclosed) - 100 if disclosed else None,
              "axis_max": max(b["max"] for b in disclosed) + 100 if disclosed else None}

    # --- config matrix ------------------------------------------------------
    all_bhk = sorted(set().union(*sets)) if sets else []
    config_matrix = {"types": all_bhk, "rows": [{"id": c["id"], "name": c["name"],
                     "has": {str(b): (b in c["configurations"]) if c["configurations"] else None for b in all_bhk}} for c in cols]}

    # --- structure table ----------------------------------------------------
    structure_rows = [{"id": c["id"], "name": c["name"], "structure": c["structure"], "rera_verified": c["rera_verified_count"],
                       "rera_total": len(c["rera_phases"])} for c in cols]
    types = {c["structure"]["building_type"] for c in cols if c["structure"] and c["structure"].get("building_type")}

    # --- amenity matrix -----------------------------------------------------
    names: dict[str, Amenity] = {}
    per_col: list[set[str] | None] = []
    for c in cols:
        if c["amenities"] is None:
            per_col.append(None)
            continue
        s = set()
        for a in c["amenities"]:
            names.setdefault(a["name"], Amenity(**a))
            s.add(a["name"])
        per_col.append(s)
    amen_rows = []
    for name in sorted(names, key=lambda n: (-sum(1 for s in per_col if s and n in s), n)):
        amen_rows.append({"name": name, "category": CATEGORY_LABELS[names[name].category],
                          "has": [None if s is None else (name in s) for s in per_col]})
    amenity_matrix = {"columns": [c["id"] for c in cols], "rows": amen_rows, "distinct": len(names),
                      "counts": [None if s is None else len(s) for s in per_col],
                      "stale": [{"id": c["id"], "days": c["amenity_age_days"]} for c in cols if c["amenity_age_days"] and c["amenity_age_days"] > 90]}

    return {
        "own_id": cols[0]["id"], "radius_km": radius_km,
        "headline": {
            "title": f"{n_comp} competitor{'s' if n_comp != 1 else ''} against {own_name}",
            "subtitle": f"All within {radius_km} km of {own_name}, all handing over after today."
                        + (f" Compared on a {common_bhk} BHK basis." if common_bhk else " No configuration is shared by every project."),
        },
        "columns": cols,
        "common_bhk": common_bhk,
        "rate_axis": rate_axis,
        "possession": {"points": poss, "gaps": poss_gaps, "sources": sorted(poss_sources)},
        "carpet": carpet,
        "config_matrix": config_matrix,
        "structure": {"rows": structure_rows, "all_same_type": len(types) == 1, "types": sorted(types)},
        "amenities": amenity_matrix,
        "insights": {},
    }
