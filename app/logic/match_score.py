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


def compute(project: Project, own: OwnProject, radius_km: float) -> tuple[int | None, dict[str, float]]:
    if project.label != "COMPARABLE":
        return None, {}

    b: dict[str, float] = {}

    b["config"] = _jaccard(set(own.configurations), set(project.value("configurations")))

    c = project.value("carpet_sqft")
    b["carpet"] = _range_overlap(own.carpet_sqft.min_sqft, own.carpet_sqft.max_sqft, c.min_sqft, c.max_sqft)

    r = project.value("rate_psf")
    if r.basis == "base" and own.rate_psf.basis == "base":
        own_mid = (own.rate_psf.min_psf + own.rate_psf.max_psf) / 2
        their_mid = (r.min_psf + r.max_psf) / 2
        b["rate"] = 1 - min(1.0, abs(their_mid - own_mid) / own_mid)
    else:
        b["rate"] = 0.0

    p = project.value("possession")
    b["possession"] = 1 - min(1.0, _months_between(p, own.possession) / settings.possession_horizon_months)

    d = project.distance_km if project.distance_km is not None else radius_km
    b["distance"] = max(0.0, 1 - d / radius_km)

    s = project.value("structure")
    own_t, their_t = own.structure.building_type, s.building_type
    if own_t and their_t and own_t == their_t:
        b["structure"] = 1.0
    elif own_t in ("multi_tower", "complex") and their_t in ("multi_tower", "complex"):
        b["structure"] = 0.5
    else:
        b["structure"] = 0.0

    weights = {
        "config": settings.w_config, "carpet": settings.w_carpet, "rate": settings.w_rate,
        "possession": settings.w_possession, "distance": settings.w_distance, "structure": settings.w_structure,
    }
    total = sum(b[k] * weights[k] for k in weights)
    return int(round(total)), {k: round(v, 3) for k, v in b.items()}


def apply(project: Project, own: OwnProject, radius_km: float) -> Project:
    project.match_score, project.score_breakdown = compute(project, own, radius_km)
    return project
