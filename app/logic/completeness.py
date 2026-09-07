from __future__ import annotations

from app.config import COMPLETENESS_FIELDS, settings
from app.models.schema import Label, Project


def count(project: Project) -> int:
    """How many of the six dimensions we have information on.

    A field whose sources disagree counts: we consulted sources and they answered,
    we simply refuse to publish one of the answers as the truth. Dropping the count
    would punish a project for us having found MORE sources than usual, and the
    disagreement is reported in `conflicts` and in the field's absence label.
    """
    return sum(1 for f in COMPLETENESS_FIELDS if project.report(f).observations)


def label_for(n: int) -> Label:
    if n >= settings.comparable_min:
        return "COMPARABLE"
    if n >= settings.partial_min:
        return "PARTIAL"
    return "THIN"


def apply(project: Project) -> Project:
    project.completeness = count(project)
    project.label = label_for(project.completeness)
    # "Could not verify" means we never saw it at all. A disagreement is a different
    # fact and travels in `conflicts`.
    project.could_not_verify = [f for f in COMPLETENESS_FIELDS if not project.report(f).observations]
    return project
