"""Find fields where the sources we consulted don't agree, and refuse to pick one.

Detection produces the absence, it does not merely flag it: a field whose sources
contradict each other becomes `absent="SOURCES_DISAGREE"` carrying the spread, and
the `Conflict` entry is kept alongside for the detail card. The observations move
to `disputed` so the card can still show the span and its provenance.
"""
from __future__ import annotations

from app.config import settings
from app.models.schema import Conflict, Project


def _months_between(a, b) -> int:
    return abs((a.year - b.year) * 12 + (a.month - b.month))


def detect(project: Project) -> list[Conflict]:
    """(conflict, field, span) for every field whose sources disagree."""
    return [c for c, _ in _detect_with_spans(project)]


def _detect_with_spans(project: Project) -> list[tuple[Conflict, list]]:
    out: list[tuple[Conflict, list]] = []

    rates = project.rate_psf.observations
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
            out.append((Conflict(field="rate_psf", n_sources=len(rates), detail=detail), [min(lows), max(highs)]))

    poss = project.possession.observations
    if len(poss) > 1:
        dates = [fv.value for fv in poss]
        gap = _months_between(min(dates), max(dates))
        if gap > settings.possession_conflict_months:
            out.append((Conflict(field="possession", n_sources=len(poss), detail=f"{len(poss)} sources differ by {gap} months"),
                        [min(dates).isoformat(), max(dates).isoformat()]))

    carpets = project.carpet_sqft.observations
    if len(carpets) > 1:
        mins = {fv.value.min_sqft for fv in carpets}
        maxs = {fv.value.max_sqft for fv in carpets}
        if len(mins) > 1 or len(maxs) > 1:
            lo, hi = min(mins), max(mins)
            if hi / max(1, lo) > settings.rate_conflict_ratio:
                out.append((Conflict(field="carpet_sqft", n_sources=len(carpets), detail=f"{len(carpets)} sources give different carpet ranges"),
                            [min(mins), max(maxs)]))

    configs = project.configurations.observations
    if len(configs) > 1:
        sets = {tuple(sorted(fv.value)) for fv in configs}
        if len(sets) > 1:
            union = sorted({b for fv in configs for b in fv.value})
            out.append((Conflict(field="configurations", n_sources=len(configs), detail=f"{len(configs)} sources list different configurations"),
                        [union[0], union[-1]]))

    return out


def apply(project: Project) -> Project:
    found = _detect_with_spans(project)
    project.conflicts = [c for c, _ in found]
    for conflict, span in found:
        # We saw several values and will not publish one of them as the answer.
        project.report(conflict.field).mark_absent("SOURCES_DISAGREE", span=span)
    return project
