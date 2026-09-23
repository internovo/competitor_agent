"""0-100 match score. Only COMPARABLE (6/6) projects get one. Weights live in config."""
from __future__ import annotations

from app.config import settings
from app.models.schema import OwnProject, Project


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def _range_overlap(a_lo, a_hi, b_lo, b_hi) -> float:
    overlap = max(0, min(a_hi, b_hi) - max(a_lo, b_lo))
    union = max(a_hi, b_hi) - min(a_lo, b_lo)
    return overlap / union if union > 0 else 0.0


def _months_between(a, b) -> int:
    return abs((a.year - b.year) * 12 + (a.month - b.month))


def compute(project: Project, own: OwnProject, radius_km: float) -> tuple[int | None, dict[str, float], int | None, list[str]]:
    """(score, breakdown, score_max, excluded).

    `score_max` is the weight actually available for this pair, not always 100. Rate
    carries 20 points and can only be scored when both sides quote a base rate; most
    portals publish an all-in figure, so a project reading 38 is usually 38 out of 80.
    Shown as 38/100 a rep concludes the project is weak, when it is unpriceable against
    theirs. The score is NOT renormalised: that would make two projects' scores silently
    incomparable, which is worse than a small number with its denominator attached.
    """
    if project.label != "COMPARABLE":
        return None, {}, None, []

    b: dict[str, float] = {}
    excluded: list[str] = []

    # Either side missing takes the dimension out of the score AND its denominator.
    # propOG's subject can arrive without the scoring fields, and a zero there would
    # read as "no overlap" rather than "nothing to compare against".
    cfg = project.value("configurations")
    if cfg and own.configurations:
        b["config"] = _jaccard(set(own.configurations), set(cfg))
    else:
        excluded.append("config")

    c = project.value("carpet_sqft")
    if c and own.carpet_sqft:
        b["carpet"] = _range_overlap(own.carpet_sqft.min_sqft, own.carpet_sqft.max_sqft, c.min_sqft, c.max_sqft)
    else:
        excluded.append("carpet")

    r = project.value("rate_psf")
    if r and r.basis == "base" and own.rate_psf and own.rate_psf.basis == "base":
        own_mid = (own.rate_psf.min_psf + own.rate_psf.max_psf) / 2
        their_mid = (r.min_psf + r.max_psf) / 2
        b["rate"] = 1 - min(1.0, abs(their_mid - own_mid) / own_mid)
    else:
        excluded.append("rate")

    p = project.value("possession")
    if p and own.possession:
        b["possession"] = 1 - min(1.0, _months_between(p, own.possession) / settings.possession_horizon_months)
    else:
        excluded.append("possession")

    d = project.distance_km if project.distance_km is not None else radius_km
    b["distance"] = max(0.0, 1 - d / radius_km)

    s = project.value("structure")
    own_t, their_t = (own.structure.building_type if own.structure else None), (s.building_type if s else None)
    if own_t and their_t:
        b["structure"] = 1.0 if own_t == their_t else (0.5 if own_t in ("multi_tower", "complex") and their_t in ("multi_tower", "complex") else 0.0)
    else:
        excluded.append("structure")

    # Distance is proximity, not similarity. With every other dimension excluded the
    # arithmetic still produces a number -- a subject with no carpet, rate, possession
    # or configurations scored 9/10 on a competitor nothing about it had been compared
    # to -- and 9/10 reads as a strong match. Nothing compared means no score.
    if set(b) <= {"distance"}:
        return None, {}, None, sorted(set(excluded) | {"distance"})

    weights = {
        "config": settings.w_config, "carpet": settings.w_carpet, "rate": settings.w_rate,
        "possession": settings.w_possession, "distance": settings.w_distance, "structure": settings.w_structure,
    }
    total = sum(b[k] * weights[k] for k in b)
    score_max = sum(weights[k] for k in b)
    return int(round(total)), {k: round(v, 3) for k, v in b.items()}, score_max, excluded


def apply(project: Project, own: OwnProject, radius_km: float) -> Project:
    project.match_score, project.score_breakdown, project.score_max, project.score_excluded = compute(project, own, radius_km)
    # Presentation only: nothing about how a point is earned changes here. A rep was
    # reading 30/50 beside 48/70 and concluding the first was the weaker match.
    # Nothing scored means no percentage -- never 0, which reads as "scored badly".
    if project.match_score is not None and project.score_max:
        project.score_100 = max(0, min(100, round(100 * project.match_score / project.score_max)))
        project.score_coverage = len(project.score_breakdown)
    else:
        project.score_100, project.score_coverage = None, 0
    return project
