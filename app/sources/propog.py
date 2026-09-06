"""Other projects already on propOG. Builder-declared, tagged ON propOG, no research needed.
For the demo this reads data/fixtures/propog_projects.json; in the portal it would query the projects table."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.config import FIXTURE_DIR
from app.logic.geo import haversine_km
from app.models.schema import Candidate, OwnProject, Page, Project
from app.sources.base import NullSource, ScanContext


class PropOGSource(NullSource):
    name = "propog"

    def __init__(self):
        path = FIXTURE_DIR / "propog_projects.json"
        self._projects = [OwnProject(**p) for p in json.loads(path.read_text())] if path.exists() else []

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
            if p.name.lower() == project.name.lower() or (p.rera_phases and p.rera_phases[0].number == project.id):
                facts = {
                    "name": p.name, "builder": p.builder, "status": "under_construction", "configurations": p.configurations,
                    "carpet_min_sqft": p.carpet_sqft.min_sqft, "carpet_max_sqft": p.carpet_sqft.max_sqft,
                    "rate_min_psf": p.rate_psf.min_psf, "rate_max_psf": p.rate_psf.max_psf, "rate_basis": p.rate_psf.basis,
                    "possession": p.possession.isoformat(), "rera_numbers": [r.number for r in p.rera_phases],
                    "amenities": [a.model_dump() for a in p.amenities], "launched": p.launched.isoformat() if p.launched else None,
                    "address": p.address, "locality": p.locality, "lat": p.lat, "lng": p.lng, **p.structure.model_dump(),
                }
                return [Page(url=f"propog://projects/{p.id}", source="propog", text=json.dumps(facts), kind="json_facts",
                             fetched_at=datetime.now(timezone.utc))]
        return []
