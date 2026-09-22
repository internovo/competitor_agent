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
    # Counted for coverage, but withdrawn as contradictory, so there is no value to show
    # and no dimension to score. Runs after conflicts.apply, which is what sets these.
    project.unresolved = [f for f in COMPLETENESS_FIELDS if project.report(f).absent == "SOURCES_DISAGREE"]
    label = label_for(project.completeness)
    # One unresolved dimension is survivable: the score reports its own denominator
    # (score_max / score_excluded), so nothing is over-promised. Capping on ANY unresolved
    # field made COMPARABLE rarer the better the research got -- more sources, more
    # disagreement -- which is the wrong direction for a label to move in.
    if len(project.unresolved) > 1 and label == "COMPARABLE":
        label = "PARTIAL"
    # A lifecycle nobody stated outranks coverage: six filled fields say nothing about
    # whether the building is still selling, so it is not COMPARABLE with one that is.
    project.label = "UNVERIFIED" if project.status == "unknown" else label
    # Same rule, different gap: a propOG row nothing outside propOG has heard of is not
    # a competitor anyone has checked, however complete its form is. UNVERIFIED keeps it
    # on the screen -- a rep may know it is real -- while match_score refuses to score a
    # label below COMPARABLE and the sort puts it last.
    if project.on_propog and not project.propog_corroborated:
        project.label = "UNVERIFIED"
    # "Could not verify" means we never saw it at all. A disagreement is a different
    # fact and travels in `conflicts`.
    project.could_not_verify = [f for f in COMPLETENESS_FIELDS if not project.report(f).observations]
    return project
