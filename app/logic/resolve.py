"""Deterministic entity resolution: is 'Runwal Vertex' the same building as 'Vertex by Runwal Group'?"""
from __future__ import annotations

import re

from app.logic.geo import haversine_km
from app.models.schema import Candidate

STOP = {
    "phase", "tower", "towers", "wing", "by", "the", "a", "project", "projects", "residency", "residences", "residence",
    "apartments", "apartment", "flats", "new", "launch", "mumbai", "west", "east", "malad", "goregaon", "kandivali", "and", "ltd",
    "limited", "group", "developers", "developer", "realty", "builders", "construction", "constructions", "pvt", "private",
    "lifespaces", "homes", "infra", "properties", "estates", "estate", "in", "at", "of", "for", "sale",
}


def tokens(s: str | None) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in STOP and len(t) > 1}


def builder_tokens(*builders: str | None) -> set[str]:
    out: set[str] = set()
    for b in builders:
        out |= {t for t in re.findall(r"[a-z0-9]+", (b or "").lower()) if len(t) > 2}
    return out


def core_tokens(c: Candidate, other: Candidate | None = None) -> set[str]:
    return tokens(c.name) - builder_tokens(c.builder, other.builder if other else None)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def decide(a: Candidate, b: Candidate) -> bool | None:
    """True = same, False = different, None = ambiguous (ask the LLM)."""
    if a.rera_no and b.rera_no:
        return a.rera_no == b.rera_no
    ba, bb = builder_tokens(a.builder), builder_tokens(b.builder)
    if ba and bb and not (ba & bb):
        return False  # two known, unrelated builders: never the same building
    ta, tb = core_tokens(a, b), core_tokens(b, a)
    if ta and ta == tb:
        return True
    shared = ta & tb
    if not shared:
        return False
    if None not in (a.lat, a.lng, b.lat, b.lng) and haversine_km(a.lat, a.lng, b.lat, b.lng) < 0.15:
        return True
    jac = len(shared) / len(ta | tb)
    if jac >= 0.25:
        return None  # a shared distinctive token with nothing else to go on: let the LLM look at both records
    return False


def merge_into(group: Candidate, c: Candidate, coord_priority: list[str]) -> Candidate:
    """Fold candidate c into the group's representative, keeping the best-provenance coordinates."""
    if not group.rera_no and c.rera_no:
        group.rera_no = c.rera_no
    if not group.builder and c.builder:
        group.builder = c.builder
    if not group.address and c.address:
        group.address = c.address
    if not group.locality and c.locality:
        group.locality = c.locality
    if not group.nearest_metro and c.nearest_metro:
        group.nearest_metro = c.nearest_metro
    group.on_propog = group.on_propog or c.on_propog
    if c.lat is not None and (group.lat is None or coord_priority.index(c.source) < coord_priority.index(group.source)):
        group.lat, group.lng = c.lat, c.lng
    if len(c.name) < len(group.name) and c.source in ("maharera", "propog"):
        group.name = c.name
    return group
