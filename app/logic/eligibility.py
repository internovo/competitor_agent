"""Pure rules that decide whether a project belongs on the competitor list."""
from __future__ import annotations

from datetime import date

from app.logic.geo import haversine_km
from app.models.schema import OwnProject, Project

# Only a lifecycle we positively read as finished disqualifies. "unknown" is not a
# disqualification: it dropped 59 of 68 real candidates on the Malad West live run,
# including launches that are plainly selling. Those surface as UNVERIFIED instead,
# where a rep can see them and confirm the status.
DISQUALIFYING_STATUSES = {"ready", "completed", "resale"}


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

    if project.status in DISQUALIFYING_STATUSES:
        return f"status is {project.status}, only new launch / under construction qualify"

    possession = project.value("possession")
    if possession is not None and possession <= today:
        return f"possession {possession.isoformat()} is not after today"

    configs = project.value("configurations")
    if configs is not None and own.configurations and not set(configs) & set(own.configurations):
        return f"no configuration overlap ({configs} vs {own.configurations})"

    return None


# A project registers one phase, or a handful. A page carrying this many registration
# numbers is a register LISTING, and the candidate's name came off its title. Measured
# before it was picked: across Borivali West, Kandivali West and Goregaon West the
# counts are 0 (52 candidates), 1 (77), 2 (1), then nothing until 6 (3), 8 (1) and 10
# (1). There is no project between 2 and 6, so the threshold is not a judgement call.
MAX_RERA_PHASES = 3


def not_a_project(project: Project) -> str | None:
    """Why this candidate is not a competitor at all, as opposed to a thin one.

    Kept separate from `check`: a drop reason says "a competitor we excluded", and
    this says "never a competitor". They read differently to a rep, and this one has
    to appear beside the table rather than in the dropped list, so it can be argued
    with.
    """
    phases = project.value("rera_phases") or []
    if len(phases) < MAX_RERA_PHASES:
        return None
    # The count was measured when pages came from search snippets, where no real project
    # carried more than two numbers. Reading a portal's own project page changed that: it
    # lists the builder's other work down the side, which put 5, 6 and 7 numbers on three
    # genuine under-construction launches in Borivali West and hid all three.
    #
    # What a register listing cannot do is carry ONE carpet range, ONE rate and ONE
    # possession date, because those belong to individual projects and a listing is a page
    # of many. A candidate holding all three has a form filled from a project's own page,
    # and the extra numbers beside it are a sidebar.
    if all(project.value(f) is not None for f in ("carpet_sqft", "rate_psf", "possession")):
        return None
    return (f"{len(phases)} RERA numbers on one candidate: the page behind this name is a "
            f"register listing, not a project")


def apply(projects: list[Project], own: OwnProject, radius_km: float, today: date | None = None) -> list[Project]:
    out = []
    for p in projects:
        reason = check(p, own, radius_km, today)
        p.eligible = reason is None
        p.drop_reason = reason
        out.append(p)
    return out
