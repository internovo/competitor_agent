"""The fixed form every competitor is squeezed into, plus the wire types around it."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field

from app.config import COMPLETENESS_FIELDS, SOURCE_PRIORITY

Source = Literal[
    "maharera", "builder_site", "places", "osm", "tavily", "squareyards", "housing",
    "99acres", "magicbricks", "propog", "manual", "fixture",
]
Status = Literal["new_launch", "under_construction", "ready", "completed", "resale", "unknown"]
BuildingType = Literal["single", "multi_tower", "complex"]
RateBasis = Literal["base", "all_in", "undisclosed"]
AmenityCategory = Literal["sport_fitness", "social_leisure", "convenience_safety", "other"]
Label = Literal["COMPARABLE", "PARTIAL", "THIN"]

T = TypeVar("T")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Provenance(BaseModel):
    source: Source
    url: str | None = None
    fetched_at: datetime = Field(default_factory=now_utc)

    @property
    def age_days(self) -> int:
        fetched = self.fetched_at if self.fetched_at.tzinfo else self.fetched_at.replace(tzinfo=timezone.utc)
        return max(0, (now_utc() - fetched).days)


class FieldValue(BaseModel, Generic[T]):
    value: T
    prov: Provenance


class RateValue(BaseModel):
    min_psf: int
    max_psf: int
    basis: RateBasis = "undisclosed"


class CarpetRange(BaseModel):
    min_sqft: int
    max_sqft: int


class Structure(BaseModel):
    building_type: BuildingType | None = None
    towers: int | None = None
    floors_min: int | None = None
    floors_max: int | None = None
    land_acres: float | None = None
    total_units: int | None = None
    open_space_pct: float | None = None


class ReraPhase(BaseModel):
    number: str
    verified: bool = False
    label: str | None = None  # "Phase 1", "Tower A" ...


class Amenity(BaseModel):
    name: str
    category: AmenityCategory = "other"
    scope: Literal["project", "unit"] = "project"


class Timeline(BaseModel):
    launched: date | None = None
    extensions_filed: int | None = None
    construction_stage: str | None = None


class Conflict(BaseModel):
    field: str
    n_sources: int
    detail: str


class Candidate(BaseModel):
    """What discovery produces: just enough to tell projects apart."""
    name: str
    builder: str | None = None
    rera_no: str | None = None
    lat: float | None = None
    lng: float | None = None
    address: str | None = None
    locality: str | None = None
    source: Source
    source_url: str | None = None
    on_propog: bool = False
    nearest_metro: str | None = None


class Page(BaseModel):
    """A fetched document handed to extraction."""
    url: str
    source: Source
    text: str
    fetched_at: datetime = Field(default_factory=now_utc)
    kind: Literal["html", "json_facts"] = "html"


class ExtractedFacts(BaseModel):
    """What Claude (or a fixture) returns for one page. Everything optional: null means 'not stated'."""
    name: str | None = None
    builder: str | None = None
    status: Status | None = None
    configurations: list[int] | None = Field(None, description="BHK counts offered, e.g. [2, 3]")
    carpet_min_sqft: int | None = None
    carpet_max_sqft: int | None = None
    rate_min_psf: int | None = None
    rate_max_psf: int | None = None
    rate_basis: RateBasis | None = Field(None, description="'base' if the page says base/basic rate, 'all_in' if inclusive of charges, else 'undisclosed'")
    possession: str | None = Field(None, description="Proposed possession as YYYY-MM or YYYY-MM-DD")
    building_type: BuildingType | None = None
    towers: int | None = None
    floors_min: int | None = None
    floors_max: int | None = None
    land_acres: float | None = None
    total_units: int | None = None
    open_space_pct: float | None = None
    rera_numbers: list[str] | None = Field(None, description="MahaRERA registration numbers, e.g. P51800048221")
    amenities: list[Amenity] | None = None
    launched: str | None = Field(None, description="Launch date as YYYY-MM or YYYY-MM-DD")
    extensions_filed: int | None = None
    construction_stage: str | None = None
    address: str | None = None
    locality: str | None = None
    lat: float | None = None
    lng: float | None = None


class Project(BaseModel):
    id: str
    name: str
    builder: str | None = None
    lat: float | None = None
    lng: float | None = None
    pin_accuracy: str | None = None
    locality: str | None = None
    address: str | None = None
    distance_km: float | None = None
    nearest_metro: str | None = None
    status: Status = "unknown"
    on_propog: bool = False

    configurations: list[FieldValue[list[int]]] = Field(default_factory=list)
    carpet_sqft: list[FieldValue[CarpetRange]] = Field(default_factory=list)
    rate_psf: list[FieldValue[RateValue]] = Field(default_factory=list)
    possession: list[FieldValue[date]] = Field(default_factory=list)
    structure: list[FieldValue[Structure]] = Field(default_factory=list)
    rera_phases: list[FieldValue[list[ReraPhase]]] = Field(default_factory=list)
    amenities: list[FieldValue[list[Amenity]]] = Field(default_factory=list)
    timeline: list[FieldValue[Timeline]] = Field(default_factory=list)

    sources_consulted: list[str] = Field(default_factory=list)
    discovered_via: list[str] = Field(default_factory=list)
    pages_seen: list[str] = Field(default_factory=list)

    # computed
    eligible: bool = True
    drop_reason: str | None = None
    completeness: int = 0
    label: Label = "THIN"
    conflicts: list[Conflict] = Field(default_factory=list)
    match_score: int | None = None
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    could_not_verify: list[str] = Field(default_factory=list)
    insight: str | None = None
    insight_source: Literal["llm", "template"] | None = None
    retried: bool = False

    # ---- display helpers -------------------------------------------------
    def display(self, field: str) -> FieldValue | None:
        values: list[FieldValue] = getattr(self, field)
        if not values:
            return None
        return sorted(values, key=lambda fv: _priority(fv.prov.source))[0]

    def value(self, field: str) -> Any | None:
        fv = self.display(field)
        return fv.value if fv else None

    def rate_span(self) -> tuple[int, int, str] | None:
        """Min-max across every source. When sources disagree the card shows the whole span, not one source's number."""
        if not self.rate_psf:
            return None
        lo = min(fv.value.min_psf for fv in self.rate_psf)
        hi = max(fv.value.max_psf for fv in self.rate_psf)
        bases = {fv.value.basis for fv in self.rate_psf}
        return lo, hi, (bases.pop() if len(bases) == 1 else "undisclosed")

    def present_fields(self) -> list[str]:
        return [f for f in COMPLETENESS_FIELDS if getattr(self, f)]

    def oldest_source_days(self) -> int | None:
        ages = [fv.prov.age_days for f in COMPLETENESS_FIELDS + ("amenities", "timeline") for fv in getattr(self, f)]
        return max(ages) if ages else None


def _priority(source: str) -> int:
    try:
        return SOURCE_PRIORITY.index(source)
    except ValueError:
        return len(SOURCE_PRIORITY)


class OwnProject(BaseModel):
    """The builder's own project as uploaded to propOG. Builder-declared, so no provenance games."""
    id: str
    name: str
    builder: str
    address: str
    locality: str | None = None
    lat: float | None = None
    lng: float | None = None
    configurations: list[int]
    carpet_sqft: CarpetRange
    rate_psf: RateValue
    possession: date
    structure: Structure
    rera_phases: list[ReraPhase] = Field(default_factory=list)
    amenities: list[Amenity] = Field(default_factory=list)
    launched: date | None = None


class ScanRecord(BaseModel):
    scan_id: str
    own_id: str
    radius_km: float
    mode: str
    created_at: datetime = Field(default_factory=now_utc)
    projects: list[Project]
    dropped: list[dict[str, Any]] = Field(default_factory=list)
