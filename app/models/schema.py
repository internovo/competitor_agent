"""The fixed form every competitor is squeezed into, plus the wire types around it."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field, model_validator

from app.config import COMPLETENESS_FIELDS, SOURCE_PRIORITY

Source = Literal[
    "maharera", "builder_site", "places", "osm", "tavily", "squareyards", "housing",
    "99acres", "magicbricks", "propog", "manual", "fixture",
]
Status = Literal["new_launch", "under_construction", "ready", "completed", "resale", "unknown"]
BuildingType = Literal["single", "multi_tower", "complex"]
RateBasis = Literal["base", "all_in", "undisclosed"]
AmenityCategory = Literal["sport_fitness", "social_leisure", "convenience_safety", "other"]
Label = Literal["COMPARABLE", "PARTIAL", "THIN", "UNVERIFIED"]

AbsenceReason = Literal[
    "NOT_PUBLISHED",        # we read a source and it does not state this
    "NO_RERA_ON_FILE",      # needs a RERA record and we have no registration number
    "RATE_NOT_PUBLISHED",   # pages found, none quote a per-sqft rate
    "UNPARSEABLE_DATE",     # a possession phrase was found but is not a date
    "SOURCES_DISAGREE",     # values differ beyond the configured threshold
    "NOT_FOUND",            # no source produced anything for this field
    "NOT_RESEARCHED",       # outside this run's research budget; nobody looked
    "RESEARCH_TIMED_OUT",   # research started and ran out of its wall-clock budget
    "IMPLAUSIBLE_RATE",     # a rate was read, but it is outside a believable band for this market
    "IMPLAUSIBLE_CARPET",   # a carpet range was read, but it is outside a believable band against the subject
    "SHARED_ACROSS_PROJECTS",  # the same figure was quoted for several projects in this run
    "LIFECYCLE_UNKNOWN",       # no source declared a lifecycle; not classified, not dropped as finished
]
ExtractMethod = Literal["deterministic", "llm", "manual"]
Confidence = Literal["high", "medium", "low"]
CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}

T = TypeVar("T")

# What a field is called in a sentence a sales rep reads.
FIELD_LABELS: dict[str, str] = {
    "configurations": "configuration mix",
    "carpet_sqft": "carpet area",
    "carpet_min_sqft": "carpet area",
    "carpet_max_sqft": "carpet area",
    "rate_psf": "rate per sq ft",
    "rate_basis": "rate basis",
    "possession": "possession date",
    "structure": "building structure",
    "rera_phases": "RERA registration",
    "rera_numbers": "RERA registration",
    "amenities": "amenity list",
    "timeline": "project timeline",
    "building_type": "building type",
    "tower_count": "tower count",
    "floors_min": "floor count",
    "floors_max": "floor count",
    "land_area_acres": "land area",
    "total_units": "unit count",
    "builder": "builder",
    "developer": "builder",
    "status": "construction status",
    "launch_date": "launch date",
}


def field_label(field: str | None) -> str:
    if not field:
        return "value"
    return FIELD_LABELS.get(field, field.replace("_", " "))


def _a(name: str) -> str:
    return f"an {name}" if name[:1].lower() in "aeiou" else f"a {name}"


def absence_label(field: str | None, reason: AbsenceReason, span: list[Any] | None = None) -> str:
    """The one place absence is put into words, so the wording cannot drift between fields."""
    name = field_label(field)
    if reason == "LIFECYCLE_UNKNOWN":
        return ("No source stated whether this project is selling, under construction or finished. "
                "It is listed unclassified rather than dropped as finished.")
    if reason == "SHARED_ACROSS_PROJECTS":
        value = f"{span[0]:,}" if span else "the same figure"
        n = span[1] if span and len(span) >= 2 else "several"
        return (f"Rs {value} per sq ft was quoted for {n} projects in this radius. "
                f"It reads as a locality average and is not treated as this project's rate.")
    if reason == "IMPLAUSIBLE_CARPET":
        quoted = f"{span[0]:,} to {span[1]:,}" if span and len(span) >= 2 and span[0] != span[1] else f"{span[0]:,}" if span else "a range"
        return (f"A source gave {quoted} sq ft carpet, outside a believable band against this project. "
                f"It is reported here and not used.")
    if reason == "IMPLAUSIBLE_RATE":
        quoted = f"{span[0]:,} to {span[1]:,}" if span and len(span) >= 2 and span[0] != span[1] else f"{span[0]:,}" if span else "a figure"
        return (f"A source quoted Rs {quoted} per sq ft, outside a believable band for this market. "
                f"It is reported here and not used.")
    if reason == "SOURCES_DISAGREE":
        spread = f"{span[0]} to {span[1]}" if span and len(span) >= 2 else "beyond the tolerated spread"
        return f"Sources disagree on the {name} ({spread}); no single value is reported."
    return {
        "NOT_PUBLISHED": f"Sources were read for this project and none states the {name}.",
        "NO_RERA_ON_FILE": f"The {name} comes off the RERA register and no registration number is on file.",
        "RATE_NOT_PUBLISHED": "Pages were found for this project; none quotes a per-sq-ft rate.",
        "UNPARSEABLE_DATE": "A possession phrase was found but it does not state a date.",
        "NOT_FOUND": f"No source produced {_a(name)} for this project.",
        "NOT_RESEARCHED": f"This project was outside the run's research budget; no page was read for {_a(name)}.",
        "RESEARCH_TIMED_OUT": f"Research for this project ran out of time before {_a(name)} was read.",
    }[reason]


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
    method: ExtractMethod = "llm"
    # A portal's own spec table is stronger evidence than the same number in prose.
    confidence: Confidence = "medium"
    evidence: str | None = None   # the verbatim substring the value was read from


class FieldReport(BaseModel, Generic[T]):
    """Every observation of one field, or the reason there is none.

    Three facts used to collapse into an empty list: nobody published it, the
    sources contradicted each other, and we never found a page at all. A cell
    cannot be constructed as unknown without carrying which one it is.
    """
    values: list[FieldValue[T]] = Field(default_factory=list)
    absent: AbsenceReason | None = None
    label: str | None = None          # generated by absence_label, never hand-written
    span: list[Any] | None = None     # SOURCES_DISAGREE only: the spread
    field: str | None = None          # which field this is, so the label can name it
    rule_matched: int | None = None   # rate_basis only: which of the four rules fired
    # Observations set aside when they contradicted each other. `values` must be
    # empty while `absent` is set, but throwing the sightings away would lose the
    # provenance the detail card is built on, so they move here instead.
    disputed: list[FieldValue[T]] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _accept_a_bare_list(cls, data: Any) -> Any:
        """`FieldReport` where a `list[FieldValue]` used to be, so fixtures and
        call sites can still write the observations directly."""
        if isinstance(data, list):
            return {"values": data} if data else {"absent": "NOT_FOUND"}
        return data

    @model_validator(mode="after")
    def _exactly_one_state(self) -> "FieldReport":
        if not self.values and self.absent is None:
            raise ValueError("a FieldReport with no values must carry an absence reason")
        if self.values and self.absent is not None:
            raise ValueError(f"a FieldReport with values cannot also be absent ({self.absent})")
        if self.absent == "SOURCES_DISAGREE" and self.span is None:
            raise ValueError("SOURCES_DISAGREE requires the span it disagrees across")
        self.label = absence_label(self.field, self.absent, self.span) if self.absent else None
        return self

    # ---- mutation keeps the invariant, so call sites cannot break it -----
    def observe(self, fv: FieldValue[T]) -> "FieldReport":
        self.values.append(fv)
        self.absent, self.label, self.span = None, None, None
        return self

    def mark_absent(self, reason: AbsenceReason, span: list[Any] | None = None) -> "FieldReport":
        if reason == "SOURCES_DISAGREE" and span is None:
            raise ValueError("SOURCES_DISAGREE requires the span it disagrees across")
        self.disputed, self.values = self.values or self.disputed, []
        self.absent, self.span = reason, span
        self.label = absence_label(self.field, reason, span)
        return self

    @property
    def observations(self) -> list[FieldValue[T]]:
        """Everything we saw, whether or not it produced a reported value."""
        return self.values or self.disputed

    def display(self) -> FieldValue[T] | None:
        """The observation the card shows: best source first, its spec table before its prose."""
        if not self.values:
            return None
        return sorted(self.values, key=lambda fv: (_priority(fv.prov.source), CONFIDENCE_RANK[fv.confidence]))[0]

    @property
    def value(self) -> Any | None:
        fv = self.display()
        return fv.value if fv else None

    def __bool__(self) -> bool:
        return bool(self.values)

    def __len__(self) -> int:
        return len(self.values)

    def __iter__(self):
        return iter(self.values)


def absent_report(field: str, reason: AbsenceReason, span: list[Any] | None = None) -> FieldReport:
    return FieldReport(field=field, absent=reason, span=span)


def report_of(field: str, fv: FieldValue) -> FieldReport:
    return FieldReport(field=field, values=[fv])


def _sort_if_it_is_a_slip(lo: int, hi: int) -> tuple[int, int]:
    """A min above a max, close enough together to be a min/max assignment slip, is sorted.

    A wide inversion is two numbers from two places and we do not know which is the
    floor, so it is left inverted here and refused as SOURCES_DISAGREE in
    app/logic/conflicts.py. Never guess which end is which.
    """
    from app.config import settings

    if lo > hi and hi > 0 and lo <= hi * settings.rate_conflict_ratio:
        return hi, lo
    return lo, hi


class RateValue(BaseModel):
    min_psf: int
    max_psf: int
    basis: RateBasis = "undisclosed"

    @model_validator(mode="after")
    def _order(self) -> "RateValue":
        self.min_psf, self.max_psf = _sort_if_it_is_a_slip(self.min_psf, self.max_psf)
        return self


class CarpetRange(BaseModel):
    min_sqft: int
    max_sqft: int

    @model_validator(mode="after")
    def _order(self) -> "CarpetRange":
        self.min_sqft, self.max_sqft = _sort_if_it_is_a_slip(self.min_sqft, self.max_sqft)
        return self


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
    register_only: bool = False   # a MahaRERA row whose name is the promoter, not a project
    # What Google Places calls this pin. Requested in the field mask and then dropped
    # on the floor until now, which is why a clothing shop reached rank 4 of a
    # competitor table: the data saying it was a clothing shop was already in hand.
    place_types: list[str] = Field(default_factory=list)


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


REPORTED_FIELDS: tuple[str, ...] = COMPLETENESS_FIELDS + ("amenities", "timeline")


def _blank(field: str):
    """A field nobody has looked at yet is NOT_FOUND, never a silent empty list."""
    return lambda: FieldReport(field=field, absent="NOT_FOUND")


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
    status_note: str | None = None  # why the lifecycle is not what a page said it was
    source_url: str | None = None   # the page discovery pulled this name from
    researched: bool = True         # False when the run's research budget did not reach it
    # `name` is cleaned for display. Identity, dedup and page matching run on `name_raw`,
    # so a cosmetic change can never merge two different projects.
    name_raw: str | None = None

    configurations: FieldReport[list[int]] = Field(default_factory=_blank("configurations"))
    carpet_sqft: FieldReport[CarpetRange] = Field(default_factory=_blank("carpet_sqft"))
    rate_psf: FieldReport[RateValue] = Field(default_factory=_blank("rate_psf"))
    possession: FieldReport[date] = Field(default_factory=_blank("possession"))
    structure: FieldReport[Structure] = Field(default_factory=_blank("structure"))
    rera_phases: FieldReport[list[ReraPhase]] = Field(default_factory=_blank("rera_phases"))
    amenities: FieldReport[list[Amenity]] = Field(default_factory=_blank("amenities"))
    timeline: FieldReport[Timeline] = Field(default_factory=_blank("timeline"))

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
    score_max: int | None = None            # the weights that could actually be evaluated
    score_excluded: list[str] = Field(default_factory=list)
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    unresolved: list[str] = Field(default_factory=list)   # counted for coverage, no value to show
    # Not a competitor at all -- a shop, a school, a register listing page. Reported
    # beside the table with this reason, never ranked in it and never silently dropped.
    not_a_project: str | None = None
    could_not_verify: list[str] = Field(default_factory=list)
    insight: str | None = None
    insight_source: Literal["llm", "template"] | None = None
    retried: bool = False

    @property
    def match_name(self) -> str:
        """The name every identity and page-matching path uses. Never the display name."""
        return self.name_raw or self.name

    # ---- display helpers -------------------------------------------------
    def report(self, field: str) -> FieldReport:
        return getattr(self, field)

    def display(self, field: str) -> FieldValue | None:
        return self.report(field).display()

    def value(self, field: str) -> Any | None:
        return self.report(field).value

    def rate_span(self) -> tuple[int, int, str] | None:
        """Min-max across every source. When sources disagree the card shows the whole span, not one source's number."""
        values = self.rate_psf.observations
        if not values:
            return None
        lo = min(fv.value.min_psf for fv in values)
        hi = max(fv.value.max_psf for fv in values)
        bases = {fv.value.basis for fv in values}
        return lo, hi, (bases.pop() if len(bases) == 1 else "undisclosed")

    def present_fields(self) -> list[str]:
        return [f for f in COMPLETENESS_FIELDS if self.report(f).values]

    def oldest_source_days(self) -> int | None:
        ages = [fv.prov.age_days for f in REPORTED_FIELDS for fv in self.report(f).values]
        return max(ages) if ages else None


def _priority(source: str) -> int:
    try:
        return SOURCE_PRIORITY.index(source)
    except ValueError:
        return len(SOURCE_PRIORITY)


class OwnProject(BaseModel):
    """The builder's own project as uploaded to propOG. Builder-declared, so no provenance games.

    Only `id` and `name` are required. propOG sends a subject built from a row that may
    still be a draft, and the four scoring fields below are not on the wire at all yet.
    A missing one is carried as missing: the dimension it feeds is excluded from the
    match score and its denominator, and the compare column prints null. Defaulting a
    carpet range or a possession date to make the form look complete would be inventing
    a builder-declared value, which is the one thing this project never does.
    """
    id: str
    name: str
    builder: str | None = None
    address: str | None = None
    locality: str | None = None
    lat: float | None = None
    lng: float | None = None
    configurations: list[int] = Field(default_factory=list)
    carpet_sqft: CarpetRange | None = None
    rate_psf: RateValue | None = None
    possession: date | None = None
    structure: Structure | None = None
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
