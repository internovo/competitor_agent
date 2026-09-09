"""Find fields where the sources we consulted don't agree, and refuse to pick one.

Detection produces the absence, it does not merely flag it: a field whose sources
contradict each other becomes `absent="SOURCES_DISAGREE"` carrying the spread, and
the `Conflict` entry is kept alongside for the detail card. The observations move
to `disputed` so the card can still show the span and its provenance.
"""
from __future__ import annotations

from collections import Counter
from statistics import median

from app.config import settings
from app.models.schema import Conflict, FieldReport, FieldValue, OwnProject, Project, _priority

RANGE_FIELDS = (("rate_psf", "min_psf", "max_psf"), ("carpet_sqft", "min_sqft", "max_sqft"))


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

    # A range that arrived inverted and too wide to be an assignment slip (those are
    # sorted in the model). Two numbers from two places, and we cannot tell which is the
    # floor, so neither is published.
    for field, lo_at, hi_at in RANGE_FIELDS:
        for fv in project.report(field).observations:
            lo, hi = getattr(fv.value, lo_at), getattr(fv.value, hi_at)
            if lo > hi and not any(c.field == field for c, _ in out):
                out.append((Conflict(field=field, n_sources=1, detail=f"one source gave an inverted range ({hi}-{lo})"),
                            [hi, lo]))

    configs = project.configurations.observations
    if len(configs) > 1:
        from app.extract.deterministic import UNDECLARED_CONFIG_LIMIT

        # Partial lists are merged into their union in logic/merge.py. What is left here
        # is a span too wide to be one building's inventory -- the signature of several
        # projects' listings read off one page.
        union = sorted({b for fv in configs for b in fv.value})
        if len(union) >= UNDECLARED_CONFIG_LIMIT:
            out.append((Conflict(field="configurations", n_sources=len(configs),
                                 detail=f"{len(configs)} sources span {union[0]}-{union[-1]} BHK, too wide for one building"),
                        [union[0], union[-1]]))

    return out


# Scalars SOURCE_PRIORITY can arbitrate. Sets are merged in logic/merge.py instead, and
# carpet ranges are left withdrawn: two different ranges are two different unit mixes.
DECIDABLE_FIELDS = ("possession", "rate_psf")


def _show(value) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "min_psf"):
        return f"{value.min_psf:,}" if value.min_psf == value.max_psf else f"{value.min_psf:,}-{value.max_psf:,}"
    return str(value)


def pick_by_priority(report: FieldReport) -> FieldValue | None:
    """The observation SOURCE_PRIORITY chooses, when it can choose at all.

    Withdrawing a field because two sources differ threw away 36 of 58 possession dates
    on two suburbs, and the lifecycle rules that need those dates never saw them. The
    priority order already records which source we trust; using it is selection, not
    correction, and the published value is still one a named source stated.

    Two sources at the same priority give no basis to prefer either, so the field stays
    withdrawn. That is the only case where "we do not know" is still the honest answer.
    """
    ranked = sorted(report.observations, key=lambda fv: _priority(fv.prov.source))
    if len(ranked) < 2 or _priority(ranked[0].prov.source) == _priority(ranked[1].prov.source):
        return None
    return ranked[0]


def _stated_beside_it(report: FieldReport, winner: FieldValue) -> str:
    others = [fv for fv in report.observations if fv is not winner]
    return (f"{winner.prov.source} says {_show(winner.value)}; "
            + ", ".join(f"{fv.prov.source} says {_show(fv.value)}" for fv in others))


def anchor_for(own: OwnProject, projects: list[Project]) -> float | None:
    """What "this market" means for the plausibility band, in one number.

    The subject's own base rate first, because it is builder-declared and the whole
    comparison is against it. Failing that, the median of what the eligible set quotes.
    With neither, there is no anchor and no judgement is made.
    """
    if own.rate_psf and own.rate_psf.basis == "base":
        return (own.rate_psf.min_psf + own.rate_psf.max_psf) / 2
    mids = [(fv.value.min_psf + fv.value.max_psf) / 2
            for p in projects if p.eligible for fv in p.rate_psf.observations]
    return median(mids) if mids else None


def carpet_anchor_for(own: OwnProject) -> float | None:
    """What "a normal flat here" means, in one number: the subject's own carpet midpoint.

    Builder-declared, so unlike the rate anchor there is no fallback to a median of
    what the competitors quote -- that median is exactly the thing being judged.
    """
    c = own.carpet_sqft
    return (c.min_sqft + c.max_sqft) / 2 if c else None


def _outside_band(observations, lo_at: str, hi_at: str, anchor: float | None,
                  min_ratio: float, max_ratio: float) -> list[int] | None:
    """The observed span, when it sits outside the believable band. Never adjusted.

    Both bounds must hold: Raghav UTOPIA quoted 295-26,412, where the top is credible
    and the bottom is not, and half a rate is not a rate. The whole range goes, for the
    same reason -- one end being readable does not make the other one true.
    """
    if anchor is None:
        return None
    floor, ceiling = anchor * min_ratio, anchor * max_ratio
    for fv in observations:
        lo, hi = sorted((getattr(fv.value, lo_at), getattr(fv.value, hi_at)))
        if lo < floor or hi > ceiling:
            return [lo, hi]
    return None


def shared_rates(projects: list[Project]) -> dict[tuple[int, int], int]:
    """Rate ranges quoted for several projects in one run, and how many.

    Five Malad buildings came back at exactly 22,130. One figure on five distinct
    projects is a locality average lifted off a portal page and attributed to each of
    them: inside any plausible band, specific, and false. Two projects sharing a figure
    is ordinary coincidence and is left alone.
    """
    seen: Counter = Counter()
    for p in projects:
        for pair in {(fv.value.min_psf, fv.value.max_psf) for fv in p.rate_psf.observations}:
            seen[pair] += 1
    return {pair: n for pair, n in seen.items() if n >= settings.shared_rate_min_projects}


def _shared(project: Project, shared: dict[tuple[int, int], int]) -> list[int] | None:
    """[value, how many projects quoted it] when this project's rate is not its own."""
    for fv in project.rate_psf.observations:
        pair = (fv.value.min_psf, fv.value.max_psf)
        if pair in shared:
            return [pair[0], shared[pair]]
    return None


def apply(project: Project, anchor: float | None = None,
          shared: dict[tuple[int, int], int] | None = None,
          carpet_anchor: float | None = None) -> Project:
    found = _detect_with_spans(project)
    # Strongest refusal wins: a figure we cannot credit at all, then one that is not this
    # project's to claim, then sources that merely disagree.
    refused = _outside_band(project.rate_psf.observations, "min_psf", "max_psf", anchor,
                            settings.rate_plausible_min_ratio, settings.rate_plausible_max_ratio)
    borrowed = _shared(project, shared or {}) if refused is None else None
    carpet_refused = _outside_band(project.carpet_sqft.observations, "min_sqft", "max_sqft", carpet_anchor,
                                   settings.carpet_plausible_min_ratio, settings.carpet_plausible_max_ratio)
    if refused is not None or borrowed is not None:
        found = [(c, s) for c, s in found if c.field != "rate_psf"]
    if carpet_refused is not None:
        found = [(c, s) for c, s in found if c.field != "carpet_sqft"]
    project.conflicts = [c for c, _ in found]
    for conflict, span in found:
        report = project.report(conflict.field)
        winner = pick_by_priority(report) if conflict.field in DECIDABLE_FIELDS else None
        if winner is not None:
            # The best source's value stands and the disagreement travels beside it.
            conflict.detail = _stated_beside_it(report, winner)
            continue
        # No basis to choose: we saw several values and publish none of them as the answer.
        report.mark_absent("SOURCES_DISAGREE", span=span)
    if refused is not None:
        project.rate_psf.mark_absent("IMPLAUSIBLE_RATE", span=refused)
    elif borrowed is not None:
        project.rate_psf.mark_absent("SHARED_ACROSS_PROJECTS", span=borrowed)
    if carpet_refused is not None:
        project.carpet_sqft.mark_absent("IMPLAUSIBLE_CARPET", span=carpet_refused)
    return project
