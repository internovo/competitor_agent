"""Find fields where the sources we consulted don't agree. We report, we never pick silently."""
from __future__ import annotations

from app.config import settings
from app.models.schema import Conflict, Project


def _months_between(a, b) -> int:
    return abs((a.year - b.year) * 12 + (a.month - b.month))


def detect(project: Project) -> list[Conflict]:
    out: list[Conflict] = []

    rates = project.rate_psf
    if len(rates) > 1:
        lows = [fv.value.min_psf for fv in rates]
        highs = [fv.value.max_psf for fv in rates]
        bases = {fv.value.basis for fv in rates}
        spread = max(highs) / max(1, min(lows))
        if spread > settings.rate_conflict_ratio or len(bases) > 1:
            pct = round((spread - 1) * 100)
            detail = f"{len(rates)} sources disagree by {pct}%"
            if len(bases) > 1:
                detail += "; basis not consistent (" + ", ".join(sorted(bases)) + ")"
            out.append(Conflict(field="rate_psf", n_sources=len(rates), detail=detail))

    poss = project.possession
    if len(poss) > 1:
        dates = [fv.value for fv in poss]
        gap = _months_between(min(dates), max(dates))
        if gap > settings.possession_conflict_months:
            out.append(Conflict(field="possession", n_sources=len(poss), detail=f"{len(poss)} sources differ by {gap} months"))

    carpets = project.carpet_sqft
    if len(carpets) > 1:
        mins = {fv.value.min_sqft for fv in carpets}
        maxs = {fv.value.max_sqft for fv in carpets}
        if len(mins) > 1 or len(maxs) > 1:
            lo, hi = min(mins), max(mins)
            if hi / max(1, lo) > settings.rate_conflict_ratio:
                out.append(Conflict(field="carpet_sqft", n_sources=len(carpets), detail=f"{len(carpets)} sources give different carpet ranges"))

    configs = project.configurations
    if len(configs) > 1:
        sets = {tuple(sorted(fv.value)) for fv in configs}
        if len(sets) > 1:
            out.append(Conflict(field="configurations", n_sources=len(configs), detail=f"{len(configs)} sources list different configurations"))

    return out


def apply(project: Project) -> Project:
    project.conflicts = detect(project)
    return project
