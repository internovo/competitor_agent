import json
from datetime import date

import pytest

from app.config import FIXTURE_DIR
from app.models.schema import (
    Amenity, CarpetRange, FieldReport, FieldValue, OwnProject, Project, Provenance, RateValue, ReraPhase, Structure,
)


@pytest.fixture
def own() -> OwnProject:
    return OwnProject(**json.loads((FIXTURE_DIR / "marina64.json").read_text()))


def fv(value, source="maharera", url=None):
    return FieldValue(value=value, prov=Provenance(source=source, url=url))


def fr(field, *values):
    """A FieldReport holding these observations, or NOT_FOUND when given none."""
    return FieldReport(field=field, values=list(values)) if values else FieldReport(field=field, absent="NOT_FOUND")


@pytest.fixture
def full_project(own) -> Project:
    """A 6/6 project 0.5 km away, under construction, handing over 3 months before Marina64."""
    return Project(
        id="P51800000001", name="Test Heights", builder="Test Builders", lat=own.lat + 0.0045, lng=own.lng, status="under_construction",
        configurations=[fv([2, 3])], carpet_sqft=[fv(CarpetRange(min_sqft=650, max_sqft=1100))],
        rate_psf=[fv(RateValue(min_psf=36000, max_psf=36000, basis="base"), source="builder_site")],
        possession=[fv(date(2029, 9, 1))], structure=[fv(Structure(building_type="multi_tower", towers=3))],
        rera_phases=[fv([ReraPhase(number="P51800000001", verified=True)])],
        amenities=[fv([Amenity(name="Swimming Pool", category="sport_fitness")], source="builder_site")],
    )
