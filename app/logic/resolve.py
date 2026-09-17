"""Deterministic entity resolution: is 'Runwal Vertex' the same building as 'Vertex by Runwal Group'?"""
from __future__ import annotations

import re

from app.config import settings
from app.logic.geo import haversine_km
from app.models.schema import Candidate

STOP = {
    "phase", "tower", "towers", "wing", "by", "the", "a", "project", "projects", "residency", "residences", "residence",
    "apartments", "apartment", "flats", "new", "launch", "mumbai", "west", "east", "malad", "goregaon", "kandivali", "and", "ltd",
    "limited", "group", "developers", "developer", "realty", "builders", "construction", "constructions", "pvt", "private",
    "lifespaces", "homes", "infra", "properties", "estates", "estate", "in", "at", "of", "for", "sale",
}


# A register row whose projectName is the promoter's company. The words below are
# corporate forms, never the distinguishing part of a marketed project's name.
COMPANY_SUFFIXES = {
    "llp", "ltd", "limited", "pvt", "private", "developers", "developer", "infrastructure", "infra",
    "constructions", "construction", "realtors", "realty", "enterprises", "builders", "associates", "corporation",
    # Borivali West's register is mostly individual redevelopment filings, and these are
    # the forms its promoter rows use. "Company" and "Group" are the two commonest.
    "company", "group", "builder", "constructors", "contractors", "estates", "properties",
    "ventures", "projects", "housing",
    # The register truncates projectName at 44 characters, so "... Private Limited"
    # arrives as "... Private Lim" or "... Private Limite".
    "lim", "limite",
}
COMPANY_CONNECTORS = {"and", "&"}
COMPANY_PREFIXES = ("aop of ", "m/s ", "m/s. ")

# A page title's segment separator. Everything after the first one is the site's own
# breadcrumb -- "Evoke by Arkade | Goregaon West | Mumbai". The dashes need spaces
# around them so a hyphenated name is not cut in half.
_TITLE_TAIL = re.compile(r"\s*[|]|\s+[-–]\s+")


def looks_like_company(name: str) -> bool:
    """A register row whose projectName is the promoter, not a marketed project.

    True only when the corporate suffix is the whole distinguishing tail. 'Lodha
    Developers' is a company; 'Lodha Amara' is a project; and 'Ajmera Realty Heights'
    is a project too, because a real name survives after the suffix. The false
    positive costs a competitor, so the tail rule is what decides, not a word count.

    'X by Y' and 'X - THE Y GROUP' are projects with their builder appended, so only
    the part before the attribution is judged. Without that, adding 'group' to the
    list would have deleted 'Airavat By Bhoomi Group' and 'Laxmi Shrushti - THE LAXMI
    GROUP', which are buildings.
    """
    low = (name or "").strip().lower()
    if any(low.startswith(p) for p in COMPANY_PREFIXES):
        return True
    head = _TITLE_TAIL.split(re.split(r"\bby\b", low, maxsplit=1)[0], maxsplit=1)[0]
    words = [w for w in re.findall(r"[a-z0-9&]+", head or low) if w]
    first = next((i for i, w in enumerate(words) if w in COMPANY_SUFFIXES), None)
    if first is None:
        return False
    return all(w in COMPANY_SUFFIXES | COMPANY_CONNECTORS for w in words[first:])


# Sources whose source_url is a readable page about the project. The register's is the
# 52k-row map blob and Places' is a maps pin; fetching either teaches us nothing.
PAGE_SOURCES = {"tavily", "squareyards", "housing", "99acres", "magicbricks", "builder_site", "propog", "fixture"}


def page_url(c: Candidate) -> str | None:
    """The candidate's source_url when it is a page about the project, else None."""
    return c.source_url if c.source in PAGE_SOURCES else None


# Portal and SEO page titles, not names anyone uses.
_NOISE = re.compile(r"\b(new\s+launch\s+project|new\s+launch|residential\s+project|under\s+construction)\b", re.I)
# The portal tab the name was scraped off -- "Sanghvi Horizon FAQs" is the FAQ page.
# Anchored to the end because that is where a tab name sits; unanchored, "prices"
# turned "Price Waterhouse Towers" into "Waterhouse Towers".
_TAB_SUFFIX = re.compile(
    r"(?:\s*[,:-]?\s*\b(faqs?|reviews?|floor\s+plans?|brochure|price\s+list|prices?)\b)+\s*$", re.I)


def clean_project_name(name: str, builder: str | None = None, locality: str | None = None) -> str:
    """The name a rep should read. DISPLAY ONLY.

    Identity, dedup and page matching stay on the raw name (Project.match_name), so
    tidying a title can never merge two different projects or change what we search for.
    """
    out = _TITLE_TAIL.split(name or "", maxsplit=1)[0] or (name or "")
    out = _TAB_SUFFIX.sub("", _NOISE.sub(" ", out).strip())
    if builder:
        brand = builder.split()[0]
        out = re.sub(rf"\bby\s+{re.escape(builder)}\s*$", "", out.strip(), flags=re.I)
        out = re.sub(rf"\bby\s+{re.escape(brand)}(\s+\w+)?\s*$", "", out.strip(), flags=re.I)
    if locality:
        out = re.sub(rf"[,\s]+{re.escape(locality)}\s*$", "", out.strip(), flags=re.I)
    out = re.sub(r"[\s,]+", " ", out).strip(" ,-")
    if out.isupper():
        out = out.title()
    # Never trade a real name for a tidier empty one.
    if len(out) < 3 or len(out.split()) < 2:
        return name.title() if name.isupper() else name
    return out


def tokens(s: str | None) -> set[str]:
    # Letters and digits split apart: "Marina64" and "Mahindra Marina 64" are one building,
    # and as {marina64} against {marina, 64} they shared nothing -- so on 17 Sep the
    # builder's own project came back 0.12 km away as its closest competitor.
    return {t for t in re.findall(r"[a-z]+|[0-9]+", (s or "").lower()) if t not in STOP and len(t) > 1}


def builder_tokens(*builders: str | None) -> set[str]:
    out: set[str] = set()
    for b in builders:
        out |= {t for t in re.findall(r"[a-z0-9]+", (b or "").lower()) if len(t) > 2}
    return out


def core_tokens(c: Candidate, other: Candidate | None = None) -> set[str]:
    return tokens(c.name) - builder_tokens(c.builder, other.builder if other else None)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# A co-operative society is a building people already live in, not a launch. Every
# mature Mumbai suburb returns dozens of them and none has a marketing page. Only the
# unambiguous markers: "Apartments", "House", "Mansion" and "Nagar" all appear in live
# project names, and a false positive here deletes a real competitor silently.
_SOCIETY = re.compile(r"\b(chsl?|co[\s\-]?op(?:erative)?\s+housing\s+society|sahakari)\b", re.I)


def looks_like_society(name: str) -> bool:
    """A registered co-operative housing society, not a project anyone is selling."""
    return bool(_SOCIETY.search(name or ""))


# What Google Places calls a pin that is not somewhere anyone buys a flat. The point
# of using the type rather than the name is that it is data: Places already knows
# "The Pant Project Store" is a clothing shop, and we were discarding the answer.
NON_RESIDENTIAL_TYPES = {
    "clothing_store", "store", "shopping_mall", "department_store", "furniture_store",
    "hospital", "doctor", "dentist", "pharmacy", "school", "primary_school",
    "secondary_school", "university", "restaurant", "cafe", "bar", "bank", "atm",
    "gym", "hotel", "lodging", "car_dealer", "car_repair", "gas_station",
    "supermarket", "convenience_store", "electronics_store", "jewelry_store",
    "beauty_salon", "hair_care", "movie_theater", "place_of_worship", "park",
}
# Places tags a genuine residential pin with these; when one is present the pin is a
# building people live in and a retail tag beside it is the ground-floor shop.
RESIDENTIAL_TYPES = {"apartment_building", "apartment_complex", "housing_complex",
                     "condominium_complex", "real_estate_agency", "premise"}


def not_residential(place_types: list[str]) -> str | None:
    """The Places type that disqualifies this pin, or None.

    A residential tag wins: a tower with a showroom on the ground floor comes back
    tagged both ways, and dropping it would cost a real competitor.
    """
    types = {t.lower() for t in place_types or ()}
    if types & RESIDENTIAL_TYPES:
        return None
    hit = sorted(types & NON_RESIDENTIAL_TYPES)
    return hit[0] if hit else None


def placeholder_pins(candidates: list[Candidate], max_uses: int = 3) -> set[tuple]:
    """Coordinates so many candidates share that they cannot mean a location.

    The register pins unlocated projects at the district office, so those rows would
    otherwise sit 0 km from each other and match everything within the radius gate.
    """
    from collections import Counter

    used = Counter((c.lat, c.lng) for c in candidates if c.lat is not None)
    return {pin for pin, n in used.items() if n > max_uses}


def could_be_same(a: Candidate, b: Candidate, placeholders: set[tuple] = frozenset()) -> bool:
    """The cheap gate before the O(n^2) comparison. A pair that fails it is different.

    Answering "different" wrongly shows one building twice, which looks untidy.
    Answering "same" wrongly fuses two projects' facts into one row, which is a lie.
    So this gate leans hard toward different and never guesses toward merging.
    """
    if a.rera_no and b.rera_no:
        return True                       # identity: decide() settles it without a model
    pins_usable = (None not in (a.lat, a.lng, b.lat, b.lng)
                   and (a.lat, a.lng) not in placeholders and (b.lat, b.lng) not in placeholders)
    if pins_usable and haversine_km(a.lat, a.lng, b.lat, b.lng) <= settings.resolve_pair_max_km:
        return True
    return bool(core_tokens(a, b) & core_tokens(b, a))


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
    if group.source_url is None:
        group.source_url = page_url(c)
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
