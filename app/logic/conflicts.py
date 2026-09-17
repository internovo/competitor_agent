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
from app.models.schema import CONFIDENCE_RANK, Conflict, FieldReport, FieldValue, OwnProject, Project, _priority

RANGE_FIELDS = (("rate_psf", "min_psf", "max_psf"), ("carpet_sqft", "min_sqft", "max_sqft"))


def _months_between(a, b) -> int:
    return abs((a.year - b.year) * 12 + (a.month - b.month))


def detect(project: Project) -> list[Conflict]:
    """(conflict, field, span) for every field whose sources disagree."""
    return [c for c, _ in _detect_with_spans(project)]


def _disagreement(field: str, obs: list[FieldValue]) -> tuple[Conflict, list] | None:
    """(conflict, span) when these observations of one field do not agree, else None."""
    if len(obs) < 2:
        return None
    if field == "rate_psf":
        lows = [fv.value.min_psf for fv in obs]
        highs = [fv.value.max_psf for fv in obs]
        bases = {fv.value.basis for fv in obs}
        spread = max(highs) / max(1, min(lows))
        if spread > settings.rate_conflict_ratio or len(bases) > 1:
            pct = round((spread - 1) * 100)
            detail = f"{len(obs)} sources disagree by {pct}%"
            if len(bases) > 1:
                detail += "; basis not consistent (" + ", ".join(sorted(bases)) + ")"
            return Conflict(field=field, n_sources=len(obs), detail=detail), [min(lows), max(highs)]
    elif field == "possession":
        dates = [fv.value for fv in obs]
        gap = _months_between(min(dates), max(dates))
        if gap > settings.possession_conflict_months:
            return (Conflict(field=field, n_sources=len(obs), detail=f"{len(obs)} sources differ by {gap} months"),
                    [min(dates).isoformat(), max(dates).isoformat()])
    elif field == "carpet_sqft":
        mins = {fv.value.min_sqft for fv in obs}
        maxs = {fv.value.max_sqft for fv in obs}
        if (len(mins) > 1 or len(maxs) > 1) and max(mins) / max(1, min(mins)) > settings.rate_conflict_ratio:
            return (Conflict(field=field, n_sources=len(obs), detail=f"{len(obs)} sources give different carpet ranges"),
                    [min(mins), max(maxs)])
    elif field == "configurations":
        from app.extract.deterministic import UNDECLARED_CONFIG_LIMIT

        # Partial lists are merged into their union in logic/merge.py. What is left here
        # is a span too wide to be one building's inventory -- the signature of several
        # projects' listings read off one page.
        union = sorted({b for fv in obs for b in fv.value})
        if len(union) >= UNDECLARED_CONFIG_LIMIT:
            return (Conflict(field=field, n_sources=len(obs),
                             detail=f"{len(obs)} sources span {union[0]}-{union[-1]} BHK, too wide for one building"),
                    [union[0], union[-1]])
    return None


def _inverted(project: Project) -> list[tuple[Conflict, list]]:
    """A range that arrived inverted and too wide to be an assignment slip (those are
    sorted in the model). Two numbers from two places, and we cannot tell which is the
    floor, so neither is published -- and no priority can pick a side of it."""
    out = []
    for field, lo_at, hi_at in RANGE_FIELDS:
        for fv in project.report(field).observations:
            lo, hi = getattr(fv.value, lo_at), getattr(fv.value, hi_at)
            if lo > hi:
                out.append((Conflict(field=field, n_sources=1, detail=f"one source gave an inverted range ({hi}-{lo})"),
                            [hi, lo]))
                break
    return out


def _detect_with_spans(project: Project) -> list[tuple[Conflict, list]]:
    out = [d for f in ("rate_psf", "possession", "carpet_sqft")
           if (d := _disagreement(f, project.report(f).observations))]
    out += [(c, s) for c, s in _inverted(project) if not any(x.field == c.field for x, _ in out)]
    if d := _disagreement("configurations", project.configurations.observations):
        out.append(d)
    return out


# Fields SOURCE_PRIORITY can arbitrate. Configuration sets are merged in logic/merge.py
# instead. Carpet is decidable only when the ranges overlap: two overlapping ranges are
# two views of one unit mix, two disjoint ones may be two buildings.
DECIDABLE_FIELDS = ("possession", "rate_psf", "carpet_sqft")


def _show(value) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "min_psf"):
        return f"{value.min_psf:,}" if value.min_psf == value.max_psf else f"{value.min_psf:,}-{value.max_psf:,}"
    if hasattr(value, "min_sqft"):
        return f"{value.min_sqft:,}-{value.max_sqft:,} sqft"
    return str(value)


def _overlap(a, b) -> bool:
    return a.min_sqft <= b.max_sqft and b.min_sqft <= a.max_sqft


def pick_by_priority(report: FieldReport, field: str) -> FieldValue | None:
    """The observation SOURCE_PRIORITY chooses, when it can choose at all.

    Withdrawing a field because two sources differ threw away 36 of 58 possession dates
    on two suburbs, and the lifecycle rules that need those dates never saw them. The
    priority order already records which source we trust; using it is selection, not
    correction, and the published value is still one a named source stated.

    Sources at the top priority that disagree with EACH OTHER give no basis to prefer
    one, so the field stays withdrawn. Sources there that agree are not a tie: a
    SquareYards project page and the SquareYards locality page both saying Dec 2028
    were withdrawn beside two aggregators saying 2028 and 2029, which made every page
    read past the first a reason to publish less.
    """
    ranked = sorted(report.observations, key=lambda fv: (_priority(fv.prov.source), CONFIDENCE_RANK[fv.confidence]))
    if len(ranked) < 2:
        return None
    top = [fv for fv in ranked if _priority(fv.prov.source) == _priority(ranked[0].prov.source)]
    if len(top) == len(ranked) or _disagreement(field, top):
        return None
    if field == "carpet_sqft" and not all(_overlap(ranked[0].value, fv.value) for fv in ranked[1:]):
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


def _outside_band(lo_at: str, hi_at: str, anchor: float | None, min_ratio: float, max_ratio: float):
    """Is this one observation outside the believable band? Never adjusted.

    Both bounds must hold: Raghav UTOPIA quoted 295-26,412, where the top is credible
    and the bottom is not, and half a rate is not a rate. The whole range goes, for the
    same reason -- one end being readable does not make the other one true.
    """
    if anchor is None:
        return lambda fv: False
    floor, ceiling = anchor * min_ratio, anchor * max_ratio

    def bad(fv) -> bool:
        lo, hi = sorted((getattr(fv.value, lo_at), getattr(fv.value, hi_at)))
        return lo < floor or hi > ceiling
    return bad


def _set_aside(report: FieldReport, bad) -> FieldValue | None:
    """Take the observations `bad` rejects out of the published set. The first one, or None.

    A bad reading is a fact about one page, not about the field. Refusing the whole
    field for it meant every page read past the first was one more chance to lose a
    value: Ami One's own SquareYards page quotes 32,750, and a comparison page quoting
    2,533 withdrew it. The rejected readings stay in `disputed`, where the card can
    still show them, and the field is withdrawn only when nothing credible is left.
    """
    gone = [fv for fv in report.values if bad(fv)]
    if gone:
        report.values = [fv for fv in report.values if not bad(fv)]
        report.disputed = gone
    return gone[0] if gone else None


def _span(fv, lo_at: str, hi_at: str) -> list[int]:
    return sorted((getattr(fv.value, lo_at), getattr(fv.value, hi_at)))


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


REFUSALS = ("IMPLAUSIBLE_RATE", "SHARED_ACROSS_PROJECTS", "IMPLAUSIBLE_CARPET")


def apply(project: Project, anchor: float | None = None,
          shared: dict[tuple[int, int], int] | None = None,
          carpet_anchor: float | None = None) -> Project:
    # Readings that cannot be this project's are set aside one by one before anything is
    # compared, so a bad page can neither withdraw a good value nor start a disagreement.
    # Strongest refusal wins when nothing is left: a figure we cannot credit at all, then
    # one that is not this project's to claim.
    rate = project.rate_psf
    if first := _set_aside(rate, _outside_band("min_psf", "max_psf", anchor, settings.rate_plausible_min_ratio,
                                               settings.rate_plausible_max_ratio)):
        if not rate.values:
            rate.mark_absent("IMPLAUSIBLE_RATE", span=_span(first, "min_psf", "max_psf"))
    shared = shared or {}
    if rate.values and (first := _set_aside(rate, lambda fv: (fv.value.min_psf, fv.value.max_psf) in shared)):
        if not rate.values:
            rate.mark_absent("SHARED_ACROSS_PROJECTS", span=[first.value.min_psf, shared[(first.value.min_psf, first.value.max_psf)]])
    carpet = project.carpet_sqft
    if first := _set_aside(carpet, _outside_band("min_sqft", "max_sqft", carpet_anchor, settings.carpet_plausible_min_ratio,
                                                 settings.carpet_plausible_max_ratio)):
        if not carpet.values:
            carpet.mark_absent("IMPLAUSIBLE_CARPET", span=_span(first, "min_sqft", "max_sqft"))

    found = [(c, s) for c, s in _detect_with_spans(project) if project.report(c.field).absent not in REFUSALS]
    inverted = {c.field for c, _ in _inverted(project)}
    project.conflicts = [c for c, _ in found]
    for conflict, span in found:
        report = project.report(conflict.field)
        decidable = conflict.field in DECIDABLE_FIELDS and conflict.field not in inverted
        winner = pick_by_priority(report, conflict.field) if decidable else None
        if winner is not None:
            # The best source's value stands and the disagreement travels beside it.
            conflict.detail = _stated_beside_it(report, winner)
            continue
        # No basis to choose: we saw several values and publish none of them as the answer.
        report.mark_absent("SOURCES_DISAGREE", span=span)
    return project
