"""Regex extractors, ported from competitor-agent-demo/agent/extract.py.

Pure functions from text to FieldReport. No network, no I/O, microseconds. Every
rule here has a unit test in tests/test_extract_*.py, and that is only possible
because nothing in this module reaches outside itself.

The rule that outranks every other goal in this file:

    Never write a value that was not observed in the text.

An absence with a reason is a correct answer. An estimate is a defect.

An LLM prompt that says "never estimate" is a request. A regex with a test is a
guarantee, so extraction asks these first and only sends what is still missing to
Claude.
"""
from __future__ import annotations

import re
from datetime import date

from app.models.schema import (
    AbsenceReason, Amenity, AmenityCategory, Confidence, FieldReport, FieldValue, Provenance, Source, absent_report,
)

# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

EVIDENCE_CONTEXT = 60
EVIDENCE_MAX_CHARS = 200


def _evidence(text: str, start: int, end: int) -> str:
    """A verbatim slice around a match. Verbatim is the whole point -- this is
    never a paraphrase, never a reconstruction, just text[a:b]."""
    a = max(0, start - EVIDENCE_CONTEXT)
    b = min(len(text), end + EVIDENCE_CONTEXT)
    return text[a:b].strip()[:EVIDENCE_MAX_CHARS]


def found(field: str, value, *, source: Source = "tavily", url: str | None = None,
          confidence: Confidence = "medium", evidence: str | None = None) -> FieldReport:
    """One observation of one field, read off one page."""
    return FieldReport(field=field, values=[FieldValue(
        value=value, prov=Provenance(source=source, url=url), method="deterministic",
        confidence=confidence, evidence=evidence[:EVIDENCE_MAX_CHARS] if evidence else None)])


def missing(field: str, reason: AbsenceReason) -> FieldReport:
    return absent_report(field, reason)


def _conf(first_party: bool) -> Confidence:
    return "high" if first_party else "medium"


# ---------------------------------------------------------------------------
# 1. Configurations
# ---------------------------------------------------------------------------

BHK_TOKEN = r"B\.?H\.?K\.?"
LIST_JOIN = r",|&|and|/"
RANGE_JOIN = r"to|-|–|—"

# Three details are load-bearing:
#   \b at the start    -- stops "in 2024, 3 BHK" matching "4, 3 BHK" -> [3, 4]
#   (?<![\d.]) as well -- stops "2.5 BHK" matching "5 BHK" -> 5. A dot is a
#       NON-word char, so \b happily matches between "." and "5"; the guard
#       that catches 2024 does nothing here. A live run turned every half-BHK
#       on a builder's own floor-plan page ("2.5 BHK Jodi Unit", "3.5 BHK")
#       into a phantom 5 BHK, which then read as a full configuration match.
#   single \d per slot -- stops "35 BHK" silently becoming [5]
_CLUSTER = rf"(?<![\d.])\b\d(?:\s*(?:{LIST_JOIN}|{RANGE_JOIN})\s*\d)*"
_MENTION_RE = re.compile(rf"({_CLUSTER})\s*(?:{BHK_TOKEN})", re.IGNORECASE)
_SPLIT_RE = re.compile(rf"\s*({LIST_JOIN}|{RANGE_JOIN})\s*", re.IGNORECASE)
_RANGE_TOKENS = {"to", "-", "–", "—"}

VALID_BHK = range(1, 6)  # 1-5. Anything outside is noise, not a configuration.

# A single project offering four or more distinct BHK sizes is rare; a page whose
# "similar projects" block offers that is common. Above this an UNDECLARED set is
# refused rather than published.
UNDECLARED_CONFIG_LIMIT = 4


def _expand_cluster(cluster: str) -> list[int]:
    """Walk alternating numbers and joiners; expand ranges, append lists."""
    parts = _SPLIT_RE.split(cluster.strip())
    nums = [int(p) for p in parts[0::2] if p.strip().isdigit()]
    joiners = [p.strip().lower() for p in parts[1::2]]
    if not nums:
        return []
    out = [nums[0]]
    for i, joiner in enumerate(joiners):
        if i + 1 >= len(nums):
            break
        if joiner in _RANGE_TOKENS:
            out.extend(range(nums[i] + 1, nums[i + 1] + 1))
        else:
            out.append(nums[i + 1])
    return out


def extract_configurations(text: str, *, source: Source = "tavily", url: str | None = None,
                           first_party: bool = False) -> FieldReport:
    """BHK mentions -> a sorted, deduplicated set of 1-5.

    Refuses to: infer a configuration from a carpet area, clamp an out-of-range
    count into range, or return [] for 'we found nothing'.
    """
    if not text:
        return missing("configurations", "NOT_PUBLISHED")

    seen: set[int] = set()
    first_match = None
    for m in _MENTION_RE.finditer(text):
        values = [n for n in _expand_cluster(m.group(1)) if n in VALID_BHK]
        if values and first_match is None:
            first_match = m
        seen.update(values)

    if not seen:
        return missing("configurations", "NOT_PUBLISHED")

    values = sorted(seen)
    # A set spanning four or more sizes that the page did not DECLARE in a spec
    # table is several projects' listings merged by a "similar projects" block,
    # not one building. A live run published "1,2,3,4,5 BHK" for a single tower
    # and the match badge then read "all 3 of yours" -- arithmetically right on a
    # set no page ever stated.
    if len(values) >= UNDECLARED_CONFIG_LIMIT and not first_party:
        return absent_report("configurations", "SOURCES_DISAGREE", span=[values[0], values[-1]])

    return found("configurations", values, source=source, url=url, confidence=_conf(first_party),
                 evidence=_evidence(text, first_match.start(), first_match.end()))


# ---------------------------------------------------------------------------
# 2. Pricing basis -- the most important rule in the module
# ---------------------------------------------------------------------------

RATE_TOKEN_RE = re.compile(r"₹|\bRs\.?|\bINR\b|per\s*sq|psf|/\s*sq", re.IGNORECASE)
RATE_ADJACENCY_CHARS = 200

ALL_INCLUSIVE_PATTERNS = [
    re.compile(r"all[-\s]inclusive", re.IGNORECASE),
    re.compile(r"inclusive\s+of\s+(?:GST|stamp\s*duty|all\s+charges)", re.IGNORECASE),
    re.compile(r"all\s+inclusive\s+price", re.IGNORECASE),
]
NO_HIDDEN_RE = re.compile(r"no\s+hidden\s+charges", re.IGNORECASE)

BASE_PATTERNS = [
    re.compile(r"base\s+(?:price|rate)", re.IGNORECASE),
    re.compile(r"basic\s+rate", re.IGNORECASE),
    re.compile(r"\+\s*GST", re.IGNORECASE),
    re.compile(r"excl(?:uding|\.)?\s+GST", re.IGNORECASE),
    re.compile(r"plus\s+(?:applicable\s+)?(?:taxes|charges)", re.IGNORECASE),
]
STARTING_RE = re.compile(r"starting\s+(?:price|from)", re.IGNORECASE)
FOOTNOTE_RE = re.compile(
    r"\*[^\n]{0,200}?(?:GST|stamp\s*duty|registration|charges|extra|additional)",
    re.IGNORECASE,
)


def _near_a_rate(text: str, span: tuple[int, int]) -> bool:
    """Is this phrase adjacent to something that looks like a rate?

    'No hidden charges' next to a per-sqft figure is a claim about that figure.
    On its own it is marketing copy about brokerage.
    """
    lo = max(0, span[0] - RATE_ADJACENCY_CHARS)
    hi = min(len(text), span[1] + RATE_ADJACENCY_CHARS)
    return bool(RATE_TOKEN_RE.search(text, lo, hi))


def extract_price_basis(text: str, *, declared_basis: str | None = None, first_party: bool = False,
                        source: Source = "tavily", url: str | None = None) -> FieldReport:
    """Four rules, in order, first match wins.

        1. declared on a first-party record  -> verbatim
        2. an explicit all-inclusive phrase  -> all_in
        3. an explicit base-rate phrase      -> base
        4. anything else                     -> undisclosed

    THE RULE THAT MUST NEVER BE BROKEN: basis is never inferred from the number.
    This function is not given the rate and never reads one. A rate being low is
    not evidence of a base rate.
    """
    text = text or ""

    # Rule 1 -- declared on a first-party record.
    if first_party and declared_basis:
        return _with_rule(found("rate_basis", declared_basis, source=source, url=url, confidence="high",
                                evidence=f"declared: {declared_basis}"), 1)

    # Rule 2 -- an explicit all-inclusive phrase.
    for pattern in ALL_INCLUSIVE_PATTERNS:
        m = pattern.search(text)
        if m:
            return _basis_report("all_in", text, m, 2, first_party, source, url)
    m = NO_HIDDEN_RE.search(text)
    if m and _near_a_rate(text, m.span()):
        return _basis_report("all_in", text, m, 2, first_party, source, url)

    # Rule 3 -- an explicit base-rate phrase.
    for pattern in BASE_PATTERNS:
        m = pattern.search(text)
        if m:
            return _basis_report("base", text, m, 3, first_party, source, url)
    m = STARTING_RE.search(text)
    if m and FOOTNOTE_RE.search(text):
        # 'starting from' counts only when a * footnote names extra charges.
        return _basis_report("base", text, m, 3, first_party, source, url)

    # Rule 4 -- anything else. A value, not an absence: every consumer downstream
    # has to name this case rather than let a falsy value slide past an `if`.
    return _with_rule(found("rate_basis", "undisclosed", source=source, url=url,
                            confidence="low", evidence=None), 4)


def _with_rule(report: FieldReport, rule: int) -> FieldReport:
    """Which of the four rules fired, kept for the trace."""
    report.rule_matched = rule
    return report


def _basis_report(value: str, text: str, m: re.Match, rule: int, first_party: bool,
                  source: Source, url: str | None) -> FieldReport:
    return _with_rule(found("rate_basis", value, source=source, url=url, confidence=_conf(first_party),
                            evidence=_evidence(text, m.start(), m.end())), rule)


# ---------------------------------------------------------------------------
# 3. Possession
# ---------------------------------------------------------------------------

POSSESSION_KEYWORD_RE = re.compile(
    r"possession|completion|hand\s?over|ready\s+by|OC\s+expected", re.IGNORECASE
)
WINDOW_CHARS = 160

# Checked BEFORE any date pattern, per window: "possession within 36 months from
# Dec 2024" must not yield 2024-12-01.
REFUSE_PATTERNS = [
    re.compile(r"\d+\s*months?\s+from", re.IGNORECASE),
    re.compile(r"on\s+completion", re.IGNORECASE),
    re.compile(r"\bsoon\b", re.IGNORECASE),
]
UNDER_CONSTRUCTION_RE = re.compile(r"under\s+construction", re.IGNORECASE)
ANY_YEAR_RE = re.compile(r"\b20\d{2}\b")

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
MONTH_YEAR_RE = re.compile(
    r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?[\s,]*(?:\d{1,2}(?:st|nd|rd|th)?[\s,]+)?(20\d{2})\b",
    re.IGNORECASE,
)
# The separator is not always a slash. Squareyards writes a RERA possession date
# as "12-2026", which fell through to the bare-year branch and became 2026-01-01
# -- eleven months early, and enough to flip a live project to possession-past
# and delete it from the report.
# "2024-2026" cannot match: \b forbids starting mid-token, so the left side
# never binds to the tail of a four-digit year.
NUMERIC_MY_RE = re.compile(r"\b(0?[1-9]|1[0-2])[/\-.](20\d{2})\b")
QUARTER_RE = re.compile(r"\bQ([1-4])\s*[-\s]?\s*(20\d{2})\b", re.IGNORECASE)
QUARTER_START_MONTH = {1: 1, 2: 4, 3: 7, 4: 10}


def _windows(text: str) -> list[tuple[int, int]]:
    return [
        (max(0, m.start() - WINDOW_CHARS), min(len(text), m.end() + WINDOW_CHARS))
        for m in POSSESSION_KEYWORD_RE.finditer(text)
    ]


def _window_refuses(window: str) -> bool:
    if any(p.search(window) for p in REFUSE_PATTERNS):
        return True
    # "under construction" with no year is not evidence of any date.
    return bool(UNDER_CONSTRUCTION_RE.search(window) and not ANY_YEAR_RE.search(window))


def extract_possession(text: str, *, first_party: bool = False, source: Source = "tavily",
                       url: str | None = None) -> FieldReport:
    """A possession date, or absent with UNPARSEABLE_DATE.

    Refuses to: carry a relative duration into a date, invent a month for a bare
    year beyond the January convention, invent a day, or read a date from outside
    a possession context (a RERA registration date is a date too).
    """
    text = text or ""

    for lo, hi in _windows(text):
        window = text[lo:hi]
        if _window_refuses(window):
            continue

        m = MONTH_YEAR_RE.search(window)
        if m:
            month = MONTHS[m.group(1).lower()]
            return _date_report(int(m.group(2)), month, text, lo, m, _conf(first_party), source, url)

        m = NUMERIC_MY_RE.search(window)
        if m:
            return _date_report(int(m.group(2)), int(m.group(1)), text, lo, m, "medium", source, url)

        m = QUARTER_RE.search(window)
        if m:
            month = QUARTER_START_MONTH[int(m.group(1))]
            return _date_report(int(m.group(2)), month, text, lo, m, "medium", source, url)

        m = ANY_YEAR_RE.search(window)
        if m:
            # The single sanctioned invention: a bare year means January. Marked
            # low, so nothing downstream can speak it aloud as a known month.
            return _date_report(int(m.group(0)), 1, text, lo, m, "low", source, url)

    return missing("possession", "UNPARSEABLE_DATE")


def _date_report(year: int, month: int, text: str, offset: int, m: re.Match,
                 confidence: Confidence, source: Source, url: str | None) -> FieldReport:
    value = date(year, month, 1)   # every parsed date is the 1st
    return found("possession", value, source=source, url=url, confidence=confidence,
                 evidence=_evidence(text, offset + m.start(), offset + m.end()))


# ---------------------------------------------------------------------------
# Supporting extractors -- the other fields in the form.
# ---------------------------------------------------------------------------

RATE_RE = re.compile(
    r"(?:₹|\bRs\.?|\bINR)\s*([\d,]{3,12})\s*(?:/-)?\s*"
    r"(?:per\s*sq\.?\s*(?:ft|feet)|/\s*sq\.?\s*(?:ft|feet)|psf)"
    r"|([\d,]{3,12})\s*(?:per\s*sq\.?\s*(?:ft|feet)|/\s*sq\.?\s*(?:ft|feet)|psf)",
    re.IGNORECASE,
)
PLAUSIBLE_RATE = range(1_000, 200_001)


def extract_rate(text: str, *, source: Source = "tavily", url: str | None = None,
                 first_party: bool = False) -> FieldReport:
    for m in RATE_RE.finditer(text or ""):
        raw = m.group(1) or m.group(2)
        try:
            value = int(raw.replace(",", ""))
        except ValueError:
            continue
        if value in PLAUSIBLE_RATE:
            return found("rate_psf", value, source=source, url=url, confidence=_conf(first_party),
                         evidence=_evidence(text, m.start(), m.end()))
    return missing("rate_psf", "RATE_NOT_PUBLISHED")


CARPET_RE = re.compile(
    r"carpet\s*(?:area)?[^\d]{0,20}([\d,]{3,6})\s*(?:-|to|–)?\s*([\d,]{3,6})?\s*sq",
    re.IGNORECASE,
)
PLAUSIBLE_CARPET = range(150, 10_001)


def extract_carpet(text: str, *, source: Source = "tavily", url: str | None = None,
                   first_party: bool = False) -> tuple[FieldReport, FieldReport]:
    """(min, max). The absence reason is NO_RERA_ON_FILE: carpet is a RERA field
    in practice, and a portal that does not print it is not the authority."""
    values: list[int] = []
    match = None
    for m in CARPET_RE.finditer(text or ""):
        for raw in (m.group(1), m.group(2)):
            if not raw:
                continue
            try:
                v = int(raw.replace(",", ""))
            except ValueError:
                continue
            if v in PLAUSIBLE_CARPET:
                values.append(v)
                match = match or m
    if not values:
        return (missing("carpet_min_sqft", "NO_RERA_ON_FILE"), missing("carpet_max_sqft", "NO_RERA_ON_FILE"))
    ev = _evidence(text, match.start(), match.end())
    conf = _conf(first_party)
    return (found("carpet_min_sqft", min(values), source=source, url=url, confidence=conf, evidence=ev),
            found("carpet_max_sqft", max(values), source=source, url=url, confidence=conf, evidence=ev))


TOWER_RE = re.compile(r"\b(\d{1,2})\s+(?:towers?|wings?|buildings?)\b", re.IGNORECASE)


def extract_tower_count(text: str, *, source: Source = "tavily", url: str | None = None,
                        first_party: bool = False) -> FieldReport:
    m = TOWER_RE.search(text or "")
    if not m:
        return missing("tower_count", "NOT_PUBLISHED")
    return found("tower_count", int(m.group(1)), source=source, url=url, confidence=_conf(first_party),
                 evidence=_evidence(text, m.start(), m.end()))


BUILDING_TYPE_PATTERNS = [
    # Bare "commercial" excluded live residential projects on phrases like
    # "delivering exceptional residential and commercial spaces" in a builder
    # blurb, and "a busy commercial and shopping scene" describing the
    # NEIGHBOURHOOD. Require the word to be attached to the product.
    ("commercial", re.compile(
        r"\b(?:commercial\s+(?:project|complex|tower|building|property|space)"
        r"|office\s+space|retail\s+shop|showroom|shop\s+cum\s+office)\b", re.I)),
    ("plotted", re.compile(r"\b(?:plotted\s+development|residential\s+plots?|land\s+parcel)\b", re.I)),
    # "\d towers?" used to sit here and matched "The project comprises 1 towers",
    # classifying a single-tower project as multi-tower. Two or more is spelled
    # out rather than left to \d.
    ("multi_tower", re.compile(
        r"\b(?:township|gated\s+community|multiple\s+(?:towers|wings|buildings)"
        r"|(?:[2-9]|[1-9]\d)\s+(?:towers|wings|buildings))\b", re.I)),
    ("single", re.compile(
        r"\b(?:standalone|single\s+(?:tower|building)|solitaire\s+tower"
        r"|1\s+(?:tower|building))\b", re.I)),
]

# A commercial property does not sell 2 BHK flats. If the page prices BHK units or
# calls itself a residential project, a stray commercial phrase is describing a
# filter chip ("Office Space") or one building inside a residential township --
# both excluded live residential projects on a run.
RESIDENTIAL_MARKER_RE = re.compile(
    r"\bBHK\b|residential\s+(?:project|complex|apartment|tower|property)"
    r"|apartments?\s+for\s+sale|carpet\s+area", re.IGNORECASE)

# This repo's BuildingType has no slot for a commercial or plotted development; a
# residential competitor set has no use for one, and inventing "single" for a row
# of plots would be writing a value nobody stated.
UNMAPPED_BUILDING_TYPES = {"commercial", "plotted"}


def extract_building_type(text: str, *, source: Source = "tavily", url: str | None = None,
                          first_party: bool = False) -> FieldReport:
    residential = bool(RESIDENTIAL_MARKER_RE.search(text or ""))
    for value, pattern in BUILDING_TYPE_PATTERNS:
        if value == "commercial" and residential:
            continue
        m = pattern.search(text or "")
        if m:
            return found("building_type", value, source=source, url=url, confidence=_conf(first_party),
                         evidence=_evidence(text, m.start(), m.end()))
    return missing("building_type", "NOT_PUBLISHED")


def reconcile_structure(building_type: FieldReport, tower_count: FieldReport) -> FieldReport:
    """A counted tower beats a phrase found near one.

    `tower_count` comes off a portal spec table or an explicit "N towers"
    sentence; `building_type` comes off whatever adjective was lying around. When
    the count is known it settles the question -- 1 is single, 2+ is multi-tower
    -- so the two can never contradict each other in the report.

    commercial and plotted are left alone: those say what is being SOLD, and a
    commercial project with three towers is still commercial.
    """
    count = tower_count.value
    if count is None or building_type.value in UNMAPPED_BUILDING_TYPES:
        return building_type
    value = "single" if count == 1 else "multi_tower"
    if building_type.value == value:
        return building_type
    fv = tower_count.display()
    return found("building_type", value, source=fv.prov.source, url=fv.prov.url, confidence=fv.confidence,
                 evidence=f"{count} tower{'s' if count != 1 else ''} on the page"
                          + (f"; {fv.evidence}" if fv.evidence else ""))


# Aliases collapse to one canonical name. Without this a page saying "Gymnasium"
# matches both "Gym" and "Gymnasium" and the report lists the same facility twice.
AMENITY_ALIASES = {"Gym": "Gymnasium", "Swimming pool": "Swimming Pool",
                   "Kids Play Area": "Kids Play Area", "Club House": "Clubhouse"}

AMENITY_VOCAB = [
    "Swimming Pool", "Clubhouse", "Club House", "Gymnasium", "Gym", "Badminton Court",
    "Amphitheatre",
    "Jogging Track", "Kids Play Area", "Landscaped Garden", "Yoga Deck", "Indoor Games",
    "Multipurpose Hall", "Squash Court", "Tennis Court", "Cricket Net", "Spa",
    "Co-working", "Library", "Sky Lounge", "Party Lawn", "Senior Citizen Area",
]

# Word-boundary matching, not substring. "Spa" matched "space" and "Spacious", so
# a phantom Spa appeared on nearly every candidate in a live report.
_AMENITY_RE = {
    a: re.compile(r"\b" + re.escape(a).replace(r"\ ", r"\s+") + r"\b", re.IGNORECASE)
    for a in AMENITY_VOCAB
}

AMENITY_CATEGORIES: dict[str, AmenityCategory] = {
    "Swimming Pool": "sport_fitness", "Gymnasium": "sport_fitness", "Jogging Track": "sport_fitness",
    "Cricket Net": "sport_fitness", "Yoga Deck": "sport_fitness", "Badminton Court": "sport_fitness",
    "Squash Court": "sport_fitness", "Tennis Court": "sport_fitness",
    "Clubhouse": "social_leisure", "Sky Lounge": "social_leisure", "Multipurpose Hall": "social_leisure",
    "Kids Play Area": "social_leisure", "Senior Citizen Area": "social_leisure",
    "Amphitheatre": "social_leisure", "Party Lawn": "social_leisure", "Indoor Games": "social_leisure",
    "Spa": "social_leisure", "Library": "social_leisure", "Landscaped Garden": "social_leisure",
    "Co-working": "convenience_safety",
}
DEFAULT_AMENITY_CATEGORY: AmenityCategory = "convenience_safety"


def extract_amenity_names(text: str) -> list[str]:
    return sorted({AMENITY_ALIASES.get(a, a) for a in AMENITY_VOCAB if _AMENITY_RE[a].search(text or "")})


def extract_amenities(text: str, *, source: Source = "tavily", url: str | None = None,
                      first_party: bool = False) -> FieldReport:
    """NOT_PUBLISHED when nothing matched -- 'we read a page and it listed none'
    and 'we never found a page' are different facts."""
    names = extract_amenity_names(text)
    if not names:
        return missing("amenities", "NOT_PUBLISHED")
    value = [Amenity(name=n, category=AMENITY_CATEGORIES.get(n, DEFAULT_AMENITY_CATEGORY), scope="project")
             for n in names]
    return found("amenities", value, source=source, url=url, confidence=_conf(first_party))


DEV_SUFFIX = (r"(?:Developers?|Group|Realty|Realtors?|Lifespaces?|Builders?|"
              r"Constructions?|Estates?|Infrastructures?|Properties|Ventures|Homes|"
              r"Enterprises?|Creators?|Associates)")
DEVELOPER_LABEL_RE = re.compile(
    r"(?:Developer|Builder|Promoter)\s*(?:Name)?\s*[:\-]\s*([A-Z][^\n,|]{2,48})")
DEVELOPER_SUFFIX_RE = re.compile(
    r"\b((?:[A-Z][A-Za-z&'.]+\s+){0,2}[A-Z][A-Za-z&'.]+\s+" + "(?i:" + DEV_SUFFIX + ")" + r")\b")
# A developer name that is really the project name, or boilerplate.
_DEV_STOP = {"real estate group", "property group", "the group"}
# "Constructions?" in DEV_SUFFIX means "Under Construction" parses as a developer.
# It is a STATUS, and it reached a live report's Developer column.
_DEV_STOP_LEAD = {
    "under", "ready", "new", "possession", "project", "similar", "this", "the",
    "a", "an", "our", "their", "its", "residential", "commercial", "luxury",
    "ongoing", "completed", "upcoming", "premium", "affordable",
    # "2 BHK Homes" is inventory, not a builder; "Malad West By Chandak Group"
    # captured the locality. Both reached a live report's Developer column.
    "bhk", "west", "east", "north", "south", "malad", "mumbai", "by", "at", "in",
    "spacious", "sq", "sqft", "carpet", "starting", "book", "explore",
    # Section headings on portal pages: "Compare Similar Projects Developer"
    "compare", "similar", "related", "view", "explore", "about", "top", "best",
    "projects", "project", "developer", "developers", "builder", "promoter",
    # Portal chrome: "Squareyards Logo Metro Associates"
    "squareyards", "99acres", "magicbricks", "housing", "housiey", "nobroker",
    "homebazaar", "propertypistol", "logo", "image", "img", "icon", "photo",
}
# A developer name may not CONTAIN these anywhere. Stripping a stop-word prefix
# off "Ready To Move Homes" leaves "To Move Homes", which is still a status; a
# lifecycle word anywhere is the reliable signal.
_DEV_STOP_ANY = {"bhk", "sqft", "carpet", "move", "construction", "launch",
                 "possession", "resale", "rent", "sale", "available"}


def extract_developer(text: str, *, source: Source = "tavily", url: str | None = None,
                      first_party: bool = False) -> FieldReport:
    """Who is building it. Label first, then the '<Name> Developers' shape."""
    for pattern, confidence in ((DEVELOPER_LABEL_RE, "high"), (DEVELOPER_SUFFIX_RE, "medium")):
        # EVERY match, not just the first: "... is a Under Construction project by
        # EMBASSY ENTERPRISES" offers a rejected candidate before the real one,
        # and stopping at the first cost us the developer entirely.
        for m in pattern.finditer(text or ""):
            value = " ".join(m.group(1).split()).strip(" .,-")
            if len(value) < 4 or value.lower() in _DEV_STOP:
                continue
            parts = value.split()
            # Strip a locality or filler prefix rather than losing the builder:
            # "Malad West By Chandak Group" -> "Chandak Group".
            while parts and parts[0].lower() in _DEV_STOP_LEAD:
                parts = parts[1:]
            if len(parts) < 2 or _DEV_STOP_ANY & {w.lower() for w in parts}:
                continue
            return found("developer", " ".join(parts), source=source, url=url, confidence=confidence,
                         evidence=_evidence(text, m.start(), m.end()))
    return missing("developer", "NOT_PUBLISHED")


LAND_AREA_RE = re.compile(r"([\d.]{1,6})\s*(?:acre|acres)\b", re.IGNORECASE)


def extract_land_area(text: str, *, source: Source = "tavily", url: str | None = None,
                      first_party: bool = False) -> FieldReport:
    for m in LAND_AREA_RE.finditer(text or ""):
        try:
            value = float(m.group(1))
        except ValueError:
            continue
        if 0.05 <= value <= 500:
            return found("land_area_acres", value, source=source, url=url, confidence=_conf(first_party),
                         evidence=_evidence(text, m.start(), m.end()))
    return missing("land_area_acres", "NOT_PUBLISHED")


FLOORS_RE = re.compile(
    r"\b(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*(?:floors?|storey|storeys|storied)"
    r"|\b(\d{1,2})\s*(?:floors?|storey|storeys|storied)\b",
    re.IGNORECASE,
)


def extract_floors(text: str, *, source: Source = "tavily", url: str | None = None,
                   first_party: bool = False) -> tuple[FieldReport, FieldReport]:
    """(min, max) floors per tower."""
    for m in FLOORS_RE.finditer(text or ""):
        lo, hi, single = m.group(1), m.group(2), m.group(3)
        try:
            values = (int(lo), int(hi)) if lo else (int(single), int(single))
        except (TypeError, ValueError):
            continue
        if all(2 <= v <= 99 for v in values) and values[0] <= values[1]:
            ev = _evidence(text, m.start(), m.end())
            conf = _conf(first_party)
            return (found("floors_min", values[0], source=source, url=url, confidence=conf, evidence=ev),
                    found("floors_max", values[1], source=source, url=url, confidence=conf, evidence=ev))
    return missing("floors_min", "NOT_PUBLISHED"), missing("floors_max", "NOT_PUBLISHED")


UNITS_RE = re.compile(r"\b(\d{2,5})\s*(?:units|apartments|flats|homes)\b", re.IGNORECASE)


def extract_total_units(text: str, *, source: Source = "tavily", url: str | None = None,
                        first_party: bool = False) -> FieldReport:
    for m in UNITS_RE.finditer(text or ""):
        value = int(m.group(1))
        if 4 <= value <= 20_000:
            return found("total_units", value, source=source, url=url, confidence=_conf(first_party),
                         evidence=_evidence(text, m.start(), m.end()))
    return missing("total_units", "NOT_PUBLISHED")


# MahaRERA registration: P + 11 digits, sometimes spaced or hyphenated.
RERA_RE = re.compile(r"\bP\s?5?\d{10,13}\b", re.IGNORECASE)


def extract_rera(text: str, *, source: Source = "tavily", url: str | None = None,
                 first_party: bool = False) -> FieldReport:
    """MahaRERA registration numbers. A verified number is what separates
    'RERA VERIFIED' from 'builder-declared'."""
    numbers, first = [], None
    for m in RERA_RE.finditer(text or ""):
        value = m.group(0).replace(" ", "").upper()
        if value not in numbers:
            numbers.append(value)
            first = first or m
    if not numbers:
        return missing("rera_numbers", "NO_RERA_ON_FILE")
    return found("rera_numbers", numbers, source=source, url=url, confidence="high",
                 evidence=_evidence(text, first.start(), first.end()))


# "New Launch" is a STATUS, and matching bare "launch" put the possession date in
# the launch slot -- a live run reported a project launched Dec 2031 with
# possession Dec 2027. Require a label form.
LAUNCH_KEYWORD_RE = re.compile(
    r"launch(?:ed)?\s*(?:date|on|in)\b|launch\s*date\b", re.IGNORECASE)


def extract_launch_date(text: str, *, source: Source = "tavily", url: str | None = None,
                        first_party: bool = False) -> FieldReport:
    """Launch date, windowed exactly like possession so a possession date is never
    read as a launch date or the reverse."""
    for m in LAUNCH_KEYWORD_RE.finditer(text or ""):
        lo = max(0, m.start() - 60)
        hi = min(len(text), m.end() + 120)
        window = text[lo:hi]
        d = MONTH_YEAR_RE.search(window)
        if d:
            value = date(int(d.group(2)), MONTHS[d.group(1).lower()], 1)
            return found("launch_date", value, source=source, url=url, confidence=_conf(first_party),
                         evidence=_evidence(text, lo + d.start(), lo + d.end()))
    return missing("launch_date", "NOT_PUBLISHED")


# ---------------------------------------------------------------------------
# Lifecycle -- voted across pages, never taken from the first match
# ---------------------------------------------------------------------------

LIFECYCLE_PATTERNS = [
    ("resale", re.compile(r"\b(?:resale|second\s+sale|pre[-\s]?owned)\b", re.I)),
    ("rental", re.compile(r"\b(?:for\s+rent|rental|on\s+lease|to\s+let)\b", re.I)),
    ("ready", re.compile(r"\b(?:ready\s+to\s+move|ready\s+possession|OC\s+received)\b", re.I)),
    ("new_launch", re.compile(r"\b(?:new\s+launch|pre[-\s]?launch|now\s+launching)\b", re.I)),
    ("under_construction", re.compile(r"\bunder\s+construction\b", re.I)),
]
# This repo's Status has no "rental": a rent listing is a transaction, not a
# project lifecycle, and the page it came off says nothing about construction.
UNMAPPED_LIFECYCLES = {"rental"}


def classify_lifecycle(text: str) -> tuple[str, str | None]:
    """(lifecycle, evidence) for ONE page. unknown is a real answer."""
    for value, pattern in LIFECYCLE_PATTERNS:
        m = pattern.search(text or "")
        if m:
            return value, _evidence(text, m.start(), m.end())
    return "unknown", None


FORWARD_LIFECYCLES = {"under_construction", "new_launch"}


def vote_lifecycle(texts: list[str]) -> tuple[str, str | None, dict[str, int]]:
    """Lifecycle across ALL of a project's pages, by majority.

    Never first-match-wins. A live run showed why: 8 of 11 lifecycle exclusions
    were contradicted by another page for the same candidate. One resale listing
    on a portal -- and every under-construction project has those, because people
    sell allotments -- was beating five pages saying the project is still being
    built, purely because resale is checked first.

    Ties break toward a FORWARD lifecycle. Excluding on a minority signal deletes
    a real competitor invisibly; keeping one extra costs a reader two seconds.
    """
    votes: dict[str, int] = {}
    evidence: dict[str, str | None] = {}
    for text in texts:
        value, ev = classify_lifecycle(text)
        if value == "unknown":
            continue
        votes[value] = votes.get(value, 0) + 1
        evidence.setdefault(value, ev)

    if not votes:
        return "unknown", None, {}

    top = max(votes.values())
    leaders = [v for v, n in votes.items() if n == top]
    winner = next((v for v in leaders if v in FORWARD_LIFECYCLES), leaders[0])
    return winner, evidence[winner], votes


def spec_lifecycle(spec: dict[str, str]) -> str | None:
    """A portal's own status field, which is worth more than a phrase found
    somewhere in the body."""
    status = (spec.get("status") or "").lower()
    if not status:
        return None
    for value, pattern in LIFECYCLE_PATTERNS:
        if pattern.search(status):
            return value
    return None


# ---------------------------------------------------------------------------
# Proximity focus -- which part of a page is about THIS project?
# ---------------------------------------------------------------------------

FOCUS_WINDOW = 500
FOCUS_MIN_CHARS = 300
_NAME_STOP = {"the", "and", "by", "at", "in", "of", "malad", "west", "east",
              "mumbai", "group", "developers", "developer", "realty", "project",
              "projects", "apartments", "residences", "phase", "tower", "towers"}


def _significant(name: str) -> list[str]:
    return [t for t in re.findall(r"[A-Za-z]{3,}", (name or "").lower())
            if t not in _NAME_STOP]


def mentions_project(text: str, name: str) -> bool:
    """Does this page mention the project at all?

    Portal searches return pages about OTHER projects -- a live run excluded both
    Chandak Treesourus and Ajmera Boulevard as rental on the same quote, which
    came from a page about K Raheja Interface Heights.
    """
    tokens = _significant(name)
    if not tokens or not text:
        return False
    full = re.compile(r"\W{0,3}".join(re.escape(t) for t in tokens), re.IGNORECASE)
    if full.search(text):
        return True
    rarest = max(tokens, key=len)
    return len(rarest) >= 5 and bool(re.search(re.escape(rarest), text, re.IGNORECASE))


def relevant_pages(pages: list, name: str | None, text_of=lambda p: p.text) -> list:
    """Pages that actually mention the project.

    If NONE do, return them all rather than nothing: a builder's own site may
    spell the name differently, and losing every page is worse than admitting a
    few foreign ones.
    """
    if not name:
        return pages
    kept = [p for p in pages if mentions_project(text_of(p), name)]
    return kept or pages


def focus_on_project(text: str, name: str, window: int = FOCUS_WINDOW) -> str:
    """Narrow a page to the parts that mention THIS project.

    Portal pages carry "similar projects" sidebars and comparison tables, so a
    page about project A also states project B's configurations, rate and
    possession date. Reading the whole page mixes them: a live run produced
    [1,2,3,4,5] BHK for a 1/2/3 BHK project, and possession dates that disagreed
    because they belonged to different buildings.

    If the name never appears we return the WHOLE page rather than nothing,
    because the alternative is silently discarding a page that may be the
    project's own site under a different spelling.
    """
    tokens = _significant(name)
    if not tokens or not text:
        return text

    full = re.compile(r"\W{0,3}".join(re.escape(t) for t in tokens), re.IGNORECASE)
    spans = [m.span() for m in full.finditer(text)]
    if not spans:
        rarest = max(tokens, key=len)
        spans = [m.span() for m in re.finditer(re.escape(rarest), text, re.IGNORECASE)]
    if not spans:
        return text

    windows: list[list[int]] = []
    for start, end in spans:
        lo, hi = max(0, start - window), min(len(text), end + window)
        if windows and lo <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], hi)
        else:
            windows.append([lo, hi])

    focused = " ".join(text[lo:hi] for lo, hi in windows)
    return focused if len(focused) >= FOCUS_MIN_CHARS else text


# ---------------------------------------------------------------------------
# One page -> the whole form
# ---------------------------------------------------------------------------

from app.extract import portals  # noqa: E402
from app.models.schema import CONFIDENCE_RANK, ExtractedFacts, Page, Status  # noqa: E402

# Everything this module can read off a page, in the donor's field names.
FIELD_ORDER = (
    "configurations", "carpet_min_sqft", "carpet_max_sqft", "rate_psf", "rate_basis",
    "possession", "building_type", "tower_count", "floors_min", "floors_max",
    "land_area_acres", "total_units", "rera_numbers", "amenities", "developer", "launch_date",
)


def best_basis(reports: list[FieldReport]) -> FieldReport:
    """`undisclosed` is a VALUE, so the first page's silence must not bury a later
    page that states the basis."""
    stated = [r for r in reports if r.value in ("base", "all_in")]
    return stated[0] if stated else (reports[0] if reports else missing("rate_basis", "NOT_PUBLISHED"))


def _spec_reports(spec: dict[str, str], source: Source, url: str | None) -> dict[str, FieldReport]:
    """Cells read off a portal's own label->value table.

    Stronger than free-text regex for two reasons: the label states which field
    the value is, and the table belongs to the page's own project, so the "which
    project is this sentence about" problem does not arise. Read as first-party so
    they come out high confidence, and consulted before the regex extractors.
    """
    out: dict[str, FieldReport] = {}
    fp = {"source": source, "url": url, "first_party": True}

    def put(name: str, report: FieldReport) -> None:
        if report.values:
            fv = report.display()
            fv.evidence = f"spec table: {fv.evidence or ''}".strip()[:EVIDENCE_MAX_CHARS]
            out[name] = report

    if "possession" in spec:
        put("possession", extract_possession(f"Possession {spec['possession']}", **fp))
    if "configurations" in spec:
        put("configurations", extract_configurations(spec["configurations"], **fp))
    if "rate" in spec:
        put("rate_psf", extract_rate(spec["rate"], **fp))
    if "total_units" in spec:
        put("total_units", extract_total_units(f"{spec['total_units']} units", **fp))
    if "towers" in spec:
        put("tower_count", extract_tower_count(f"{spec['towers']} towers", **fp))
    if "land_area" in spec:
        put("land_area_acres", extract_land_area(spec["land_area"], **fp))
    if "launch" in spec:
        put("launch_date", extract_launch_date(f"Launch date {spec['launch']}", **fp))
    if "rera" in spec:
        put("rera_numbers", extract_rera(spec["rera"], **fp))
    if "developer" in spec:
        put("developer", extract_developer(f"Developer: {spec['developer']}", **fp))
    if "carpet" in spec:
        lo, hi = extract_carpet(f"Carpet area {spec['carpet']}", **fp)
        put("carpet_min_sqft", lo)
        put("carpet_max_sqft", hi)
    return out


def extract_page(text: str, *, source: Source = "tavily", url: str | None = None,
                 project_name: str | None = None, first_party: bool | None = None
                 ) -> tuple[dict[str, FieldReport], str, str | None]:
    """(fields, lifecycle, lifecycle_evidence) read off ONE page.

    A portal's own spec table outranks anything read out of prose, so it is
    consulted first and the regex extractors fill only what it did not publish.
    """
    if first_party is None:
        first_party = portals.is_first_party(url or "")
    spec = portals.specs(text or "")
    # Only the parts of the page that are about THIS project.
    focused = focus_on_project(text or "", project_name) if project_name else (text or "")
    kw = {"source": source, "url": url, "first_party": first_party}

    fields: dict[str, FieldReport] = _spec_reports(spec, source, url)

    def fill(name: str, report: FieldReport) -> None:
        fields.setdefault(name, report)

    fill("configurations", extract_configurations(focused, **kw))
    lo, hi = extract_carpet(focused, **kw)
    fill("carpet_min_sqft", lo)
    fill("carpet_max_sqft", hi)
    fill("rate_psf", extract_rate(focused, **kw))
    fill("rate_basis", extract_price_basis(focused, first_party=first_party, source=source, url=url))
    fill("possession", extract_possession(focused, **kw))
    fill("building_type", extract_building_type(focused, **kw))
    fill("tower_count", extract_tower_count(focused, **kw))
    f_lo, f_hi = extract_floors(focused, **kw)
    fill("floors_min", f_lo)
    fill("floors_max", f_hi)
    fill("land_area_acres", extract_land_area(focused, **kw))
    fill("total_units", extract_total_units(focused, **kw))
    fill("rera_numbers", extract_rera(focused, **kw))
    fill("amenities", extract_amenities(focused, **kw))
    fill("developer", extract_developer(focused, **kw))
    fill("launch_date", extract_launch_date(focused, **kw))

    fields["building_type"] = reconcile_structure(fields["building_type"], fields["tower_count"])

    lifecycle, evidence = classify_lifecycle(focused)
    declared = spec_lifecycle(spec)
    if declared:
        # A portal's own "Project Status" field beats a phrase found in the body:
        # it is a declaration about THIS project, not a word near it.
        lifecycle, evidence = declared, f"portal status field: {declared}"

    # A project cannot be launched after it is handed over. When the two disagree
    # the launch date is the unreliable one -- possession is stated far more often
    # and more consistently -- so it is dropped, not corrected.
    launch, possession = fields["launch_date"].value, fields["possession"].value
    if launch and possession and launch >= possession:
        fields["launch_date"] = missing("launch_date", "NOT_PUBLISHED")

    return fields, lifecycle, evidence


# Which of this repo's fields each donor field feeds.
TARGET_OF = {
    "configurations": "configurations",
    "carpet_min_sqft": "carpet_sqft", "carpet_max_sqft": "carpet_sqft",
    "rate_psf": "rate_psf", "rate_basis": "rate_psf",
    "possession": "possession",
    "building_type": "structure", "tower_count": "structure", "floors_min": "structure",
    "floors_max": "structure", "land_area_acres": "structure", "total_units": "structure",
    "rera_numbers": "rera_phases",
    "amenities": "amenities",
    "launch_date": "timeline",
    "developer": "builder",
}


def to_facts(fields: dict[str, FieldReport], lifecycle: str
             ) -> tuple[ExtractedFacts, dict[str, Confidence], dict[str, str], set[str]]:
    """The form this repo merges, plus per-field confidence, evidence, and which of
    its fields the regexes actually filled."""
    def v(name):
        return fields[name].value if name in fields else None

    carpet_lo, carpet_hi = v("carpet_min_sqft"), v("carpet_max_sqft")
    rate = v("rate_psf")
    building_type = v("building_type")
    if building_type in UNMAPPED_BUILDING_TYPES:
        building_type = None
    status: Status | None = lifecycle if lifecycle not in UNMAPPED_LIFECYCLES | {"unknown"} else None
    possession = v("possession")
    launch = v("launch_date")

    facts = ExtractedFacts(
        builder=v("developer"),
        status=status,
        configurations=v("configurations"),
        carpet_min_sqft=carpet_lo, carpet_max_sqft=carpet_hi,
        rate_min_psf=rate, rate_max_psf=rate,
        rate_basis=v("rate_basis") if rate else None,
        possession=possession.isoformat() if possession else None,
        building_type=building_type,
        towers=v("tower_count"), floors_min=v("floors_min"), floors_max=v("floors_max"),
        land_acres=v("land_area_acres"), total_units=v("total_units"),
        rera_numbers=v("rera_numbers"),
        amenities=v("amenities"),
        launched=launch.isoformat() if launch else None,
    )

    confidence: dict[str, Confidence] = {}
    evidence: dict[str, str] = {}
    filled: set[str] = set()
    for donor, target in TARGET_OF.items():
        report = fields.get(donor)
        if report is None or not report.values:
            continue
        if donor == "rate_basis" and rate is None:
            continue          # a basis with no rate fills nothing
        if target == "carpet_sqft" and not (carpet_lo and carpet_hi):
            continue
        if donor == "building_type" and building_type is None:
            continue
        fv = report.display()
        filled.add(target)
        # A composite field is only as strong as its strongest part.
        if CONFIDENCE_RANK[fv.confidence] < CONFIDENCE_RANK[confidence.get(target, "low")]:
            confidence[target] = fv.confidence
        confidence.setdefault(target, fv.confidence)
        if fv.evidence:
            evidence.setdefault(target, fv.evidence)
    if status:
        filled.add("status")
    return facts, confidence, evidence, filled


def read(page: Page, project_name: str | None = None
         ) -> tuple[ExtractedFacts, dict[str, Confidence], dict[str, str], set[str], str]:
    """One page -> (facts, confidence, evidence, fields filled, lifecycle)."""
    fields, lifecycle, _ = extract_page(page.text, source=page.source, url=page.url,
                                        project_name=project_name)
    facts, confidence, evidence, filled = to_facts(fields, lifecycle)
    return facts, confidence, evidence, filled, lifecycle
