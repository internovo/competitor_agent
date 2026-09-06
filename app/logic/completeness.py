from __future__ import annotations

from app.config import COMPLETENESS_FIELDS, settings
from app.models.schema import Label, Project


def count(project: Project) -> int:
    return sum(1 for f in COMPLETENESS_FIELDS if getattr(project, f))


def label_for(n: int) -> Label:
    if n >= settings.comparable_min:
        return "COMPARABLE"
    if n >= settings.partial_min:
        return "PARTIAL"
    return "THIN"


def apply(project: Project) -> Project:
    project.completeness = count(project)
    project.label = label_for(project.completeness)
    project.could_not_verify = [f for f in COMPLETENESS_FIELDS if not getattr(project, f)]
    return project
