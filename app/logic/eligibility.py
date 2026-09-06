"""Pure rules that decide whether a project belongs on the competitor list."""
from __future__ import annotations

from datetime import date

from app.logic.geo import haversine_km
from app.models.schema import OwnProject, Project

ELIGIBLE_STATUSES = {"new_launch", "under_construction"}


def check(project: Project, own: OwnProject, radius_km: float, today: date | None = None) -> str | None:
    """Return None if eligible, else a short drop reason."""
    today = today or date.today()

    if project.lat is None or project.lng is None:
        return "no coordinates"
    if own.lat is None or own.lng is None:
        return "own project has no coordinates"
    dist = haversine_km(own.lat, own.lng, project.lat, project.lng)
    project.distance_km = round(dist, 2)
    if dist > radius_km:
        return f"outside radius ({dist:.2f} km > {radius_km} km)"

    if project.status not in ELIGIBLE_STATUSES:
        return f"status is {project.status}, only new launch / under construction qualify"

    possession = project.value("possession")
    if possession is not None and possession <= today:
        return f"possession {possession.isoformat()} is not after today"

    configs = project.value("configurations")
    if configs is not None and own.configurations and not set(configs) & set(own.configurations):
        return f"no configuration overlap ({configs} vs {own.configurations})"

    return None


def apply(projects: list[Project], own: OwnProject, radius_km: float, today: date | None = None) -> list[Project]:
    out = []
    for p in projects:
        reason = check(p, own, radius_km, today)
        p.eligible = reason is None
        p.drop_reason = reason
        out.append(p)
    return out
