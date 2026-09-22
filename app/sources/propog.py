"""Published propOG projects near the subject. Builder-declared, tagged ON propOG, no research needed.

propOG sends these in the scan request, beside the subject. It owns tenancy and the
publication flag, so it decides what is shared and the agent keeps no database
credentials -- the same reason runs are not stored here.

The fixture file is the offline stand-in for that list and is read in fixture and
replay mode only. It used to be read in every mode, which is how a hand-written demo
record for a project that does not exist reached rank 1 of a live Malad West scan on
21 September: nothing on the wire, nothing in the database, one file on a VM.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from pydantic import ValidationError

from app.config import FIXTURE_DIR
from app.logic.geo import haversine_km
from app.models.schema import Candidate, OwnProject, Page, Project
from app.sources.base import NullSource, ScanContext


class NearbyProject(OwnProject):
    """One project propOG shares: the subject's own shape, plus the two fields the
    agent checks at the door. `builder_id` is opaque here and never displayed."""
    status: str | None = None
    builder_id: str | None = None


def bhk(values: list) -> list[int]:
    """propOG's inventory carries "2 BHK" strings; this form counts bedrooms.

    Anything without a number in it is dropped rather than guessed at.
    """
    out = set()
    for v in values or []:
        if isinstance(v, int):
            out.add(v)
            continue
        digits = re.search(r"\d+", str(v))
        if digits:
            out.add(int(digits.group()))
    return sorted(out)


def _from_wire(row: dict) -> NearbyProject:
    """One row as propOG spells it -> the form this pipeline compares against.

    The same two translations the subject already gets on the way in, because a
    nearby project is a subject row: propOG says `latitude`/`longitude`, and its
    configurations are "2 BHK" strings. Getting this wrong would not raise -- the
    row would validate with no coordinates and be dropped as out of radius.
    """
    row = dict(row)
    row.setdefault("lat", row.get("latitude"))
    row.setdefault("lng", row.get("longitude"))
    row["configurations"] = bhk(row.get("configurations") or [])
    return NearbyProject(**row)


def published_only(rows: list[dict], builder_id: str | None) -> list[NearbyProject]:
    """What the agent will accept from the wire. propOG filters; this checks anyway.

    By flag, never by name. "Looks like a test project" would have missed the next
    demo row and would drop a real project unlucky enough to be called Test Towers.
    A builder's own other project is not their competitor, so it goes too.
    """
    out = []
    for row in rows:
        try:
            p = _from_wire(row)
        except (ValidationError, TypeError, AttributeError):
            continue
        if (p.status or "").upper() != "PUBLISHED":
            continue
        if builder_id and p.builder_id == builder_id:
            continue
        out.append(p)
    return out


class PropOGSource(NullSource):
    name = "propog"

    def __init__(self, projects: list[OwnProject] | None = None):
        self._projects = list(projects or [])

    @classmethod
    def from_fixture(cls) -> "PropOGSource":
        """The offline demo's stand-in for the list propOG would have sent."""
        path = FIXTURE_DIR / "propog_projects.json"
        return cls([OwnProject(**p) for p in json.loads(path.read_text())] if path.exists() else [])

    async def discover(self, ctx: ScanContext) -> list[Candidate]:
        own = ctx.own
        out = []
        for p in self._projects:
            if p.id == own.id or p.lat is None or own.lat is None:
                continue
            if haversine_km(own.lat, own.lng, p.lat, p.lng) <= ctx.radius_km + 0.01:
                rera = p.rera_phases[0].number if p.rera_phases else None
                out.append(Candidate(name=p.name, builder=p.builder, rera_no=rera, lat=p.lat, lng=p.lng, address=p.address,
                                     locality=p.locality, source="propog", source_url=f"propog://projects/{p.id}", on_propog=True))
        return out

    async def pages_for(self, project: Project, ctx: ScanContext) -> list[Page]:
        for p in self._projects:
            if p.name.lower() == project.match_name.lower() or (p.rera_phases and p.rera_phases[0].number == project.id):
                # Every field is optional, because a real published project is not a
                # hand-written fixture: it fills in what the builder filled in. A
                # missing one travels as null, which extraction already reads as "this
                # source did not publish it" -- the same fact, not a crash and not a
                # guess. The fixture always carried all six, so this was never exercised.
                facts = {
                    "name": p.name, "builder": p.builder, "status": "under_construction", "configurations": p.configurations,
                    "carpet_min_sqft": p.carpet_sqft.min_sqft if p.carpet_sqft else None,
                    "carpet_max_sqft": p.carpet_sqft.max_sqft if p.carpet_sqft else None,
                    "rate_min_psf": p.rate_psf.min_psf if p.rate_psf else None,
                    "rate_max_psf": p.rate_psf.max_psf if p.rate_psf else None,
                    "rate_basis": p.rate_psf.basis if p.rate_psf else None,
                    "possession": p.possession.isoformat() if p.possession else None,
                    "rera_numbers": [r.number for r in p.rera_phases],
                    "amenities": [a.model_dump() for a in p.amenities], "launched": p.launched.isoformat() if p.launched else None,
                    "address": p.address, "locality": p.locality, "lat": p.lat, "lng": p.lng,
                    **(p.structure.model_dump() if p.structure else {}),
                }
                return [Page(url=f"propog://projects/{p.id}", source="propog", text=json.dumps(facts), kind="json_facts",
                             fetched_at=datetime.now(timezone.utc))]
        return []
