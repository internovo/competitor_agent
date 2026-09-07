"""Portal spec tables. Ported from competitor-agent-demo/agent/portals.py.

Free-text regex over a whole page is the weakest way to read a portal, because
portals do not write prose -- they render **spec tables**:

    Project Status      Under Construction
    Possession Date     Dec 2026
    Configurations      2, 3 BHK
    Total Units         169
    Land Area           2.1 acres
    RERA                P51800054569

A label sitting next to its value is far stronger evidence than the same number
floating in a paragraph, and it removes the "which project is this sentence
about" problem entirely: the label belongs to the page's own project. So a spec
value is read FIRST and marked high confidence; free-text regex fills only the
labels the page did not publish.

It adds no new trust: a spec-table value is still a value we observed on a page
and carries its own evidence quote.
"""
from __future__ import annotations

import re

# Portals that publish structured project pages for Indian real estate.
PORTAL_DOMAINS = [
    "housing.com",
    "www.99acres.com",
    "www.squareyards.com",
    "www.magicbricks.com",
    "housiey.com",
    "www.propertypistol.com",
    "www.homebazaar.com",
]

# A portal page is worth more than a blog; used to raise confidence.
FIRST_PARTY_HINTS = ("housing.com", "99acres", "squareyards", "magicbricks")

# label: canonical key
SPEC_LABELS = {
    "project status": "status",
    "status": "status",
    "possession": "possession",
    "possession date": "possession",
    "possession starts": "possession",
    "possession start date": "possession",
    "completion date": "possession",
    "avg price": "rate",
    "average price": "rate",
    "price per sqft": "rate",
    "rate": "rate",
    "configurations": "configurations",
    "configuration": "configurations",
    "unit configuration": "configurations",
    "bhk": "configurations",
    "property type": "property_type",
    "carpet area": "carpet",
    "area": "carpet",
    "total units": "total_units",
    "no of units": "total_units",
    "units": "total_units",
    "total towers": "towers",
    "towers": "towers",
    "no of towers": "towers",
    "project size": "land_area",
    "land area": "land_area",
    "total area": "land_area",
    "launch date": "launch",
    "launched": "launch",
    "rera": "rera",
    "rera id": "rera",
    "rera number": "rera",
    "maharera": "rera",
    "developer": "developer",
    "builder": "developer",
    "promoter": "developer",
}

# "Label: value", "Label | value", "Label   value" on one line.
_ROW_RE = re.compile(
    r"^[\s|*·\-]*([A-Za-z][A-Za-z /&.]{2,28}?)\s*[:|]\s*([^\n|]{1,90})\s*$",
    re.MULTILINE,
)
# Table rows rendered as "| Label | Value |"
_PIPE_RE = re.compile(r"\|\s*([A-Za-z][A-Za-z /&.]{2,28}?)\s*\|\s*([^\n|]{1,90})\s*\|")

_NOISE_VALUE = re.compile(r"^(?:-+|n/?a|na|--|not available|tbd)$", re.IGNORECASE)


def parse_spec_table(text: str) -> dict[str, str]:
    """label -> value for the labels we know how to use.

    First occurrence wins: portals repeat the spec block in a footer, and the
    first render is the one beside the heading.
    """
    found: dict[str, str] = {}
    for pattern in (_PIPE_RE, _ROW_RE):
        for m in pattern.finditer(text or ""):
            label = " ".join(m.group(1).split()).strip(" :|*·-").lower()
            value = " ".join(m.group(2).split()).strip(" :|*·-")
            key = SPEC_LABELS.get(label)
            if not key or not value or _NOISE_VALUE.match(value):
                continue
            if len(value) < 2:
                continue
            found.setdefault(key, value)
    return found


# Scraped portal HTML flattens to a run-on line with no pipes or colons:
#   "Status Under Construction Possession Starting From Dec 2030 Unit Config
#    1, 2 BHK Flats Size 408 to 636 Sq. Ft."
# so the label must be matched inline, with the value bounded by length.
_INLINE = [
    ("status", r"(?:Project\s+)?Status\s+(Under\s+Construction|Ready\s+To\s+Move|"
               r"New\s+Launch|Completed|Ready\s+Possession)"),
    ("possession", r"(?:Target\s+)?Possession(?:\s+Status|\s+Starting\s+From|\s+Date|\s+By)?"
                   r"\s+((?:Q[1-4]\s+)?(?:[A-Z][a-z]{2,8}\s+)?20\d{2})"),
    ("configurations", r"(?:Unit\s+Config(?:uration)?s?|Configurations?)\s+"
                       r"([\d\s,&/and-]{1,24}?BHK)"),
    ("carpet", r"(?:Flats?\s+)?Sizes?\s+([\d,]{3,6}\s*(?:to|-|–)\s*[\d,]{3,6}\s*"
               r"Sq\.?\s*Ft)"),
    ("rera", r"(?:MahaRERA|RERA)\s*(?:No\.?|Number|ID)?\s*[:\-]?\s*([AP]\d{8,14})"),
    ("total_units", r"(?:Total\s+)?Units?\s+([\d,]{2,6})"),
    ("towers", r"([\d]{1,2})\s+Towers?"),
    ("land_area", r"built\s+across\s+([\d.]{1,5}\s*acres?)"),
]
_INLINE_RE = [(k, re.compile(v, re.IGNORECASE)) for k, v in _INLINE]


def parse_inline_specs(text: str) -> dict[str, str]:
    """Labels found inline in flattened portal text."""
    found: dict[str, str] = {}
    for key, pattern in _INLINE_RE:
        m = pattern.search(text or "")
        if m:
            value = " ".join(m.group(1).split()).strip(" :|*·-")
            if value and not _NOISE_VALUE.match(value):
                found.setdefault(key, value)
    return found


def specs(text: str) -> dict[str, str]:
    """Everything we can read as a label->value pair, table or inline."""
    found = parse_spec_table(text)
    for key, value in parse_inline_specs(text).items():
        found.setdefault(key, value)
    return found


def is_portal(url: str) -> bool:
    host = re.sub(r"^https?://", "", (url or "")).split("/")[0].lower()
    return any(d.replace("www.", "") in host for d in PORTAL_DOMAINS)


def is_first_party(url: str) -> bool:
    """A portal project page speaks about its own project; a blog does not."""
    return any(h in (url or "").lower() for h in FIRST_PARTY_HINTS)


# A rent or resale listing describes a TRANSACTION, not the project, and its page
# says "rent"/"resale" all over -- which then wins the lifecycle vote and excludes
# a live project. A portal run without this filter took RENTAL exclusions from 4
# to 10 and left zero candidates.
_DENY_URL = re.compile(
    r"/resale/|/rent(?:al)?[/-]|for-rent|/pg/|/plots?/|/commercial/"
    r"|instagram\.com|youtube\.com|youtu\.be|linkedin\.com|facebook\.com"
    r"|twitter\.com|x\.com/|pinterest\.",
    re.IGNORECASE,
)
# housing.com/in/buy/projects/... and squareyards /project are the real thing.
_PROJECT_URL = re.compile(r"/projects?/|/project$|-project", re.IGNORECASE)


def is_listing_noise(url: str) -> bool:
    """True for pages that describe a transaction or a social post."""
    return bool(_DENY_URL.search(url or ""))


def is_project_page(url: str) -> bool:
    return bool(_PROJECT_URL.search(url or ""))
