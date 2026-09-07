"""MahaRERA legacy public register (server-rendered, no captcha as of Sep 2026).

Discovery: the register's "View All Projects on Map" page ships every registered project as one JSON blob
(`#mapLocationData`) carrying project name, registration number, locality, district and coordinates. That is the only
key-free source of *pinned* candidates we have, so the circle is drawn against it directly. The search form itself
moved to a Drupal POST flow in 2026 and no longer answers GET queries.
Pages: the project summary page, when the register still serves one for that registration number.
The HTML layout is not pinned; we hand the readable text to Claude rather than depend on CSS selectors.
"""
from __future__ import annotations

import html as html_lib
import json
import re
from collections import Counter

from app.config import settings
from app.logic.geo import haversine_km
from app.models.schema import Candidate, Page, Project
from app.sources.base import NullSource, ScanContext
from app.sources.fetch import html_to_text

BASE = "https://maharera.maharashtra.gov.in"
SEARCH = f"{BASE}/projects-search-result"
MAP = f"{BASE}/map-projects-search-result"
DETAIL = f"{BASE}/project-summary-report-detail"
RERA_RE = re.compile(r"\bP[A-Z]?\d{7,15}\b")
MAP_ANCHOR = 'id="mapLocationData"'
# Rows the promoter never pinned share a handful of fallback coordinates (0,0 and one per district office).
# A coordinate used by more than a few projects is a placeholder, not a location.
PLACEHOLDER_PIN_USES = 3
NOT_AVAILABLE = {"", "not available", "-", "."}
NO_RECORDS = "No records found"


def _clean(v: object) -> str | None:
    s = html_lib.unescape(str(v or "")).strip()
    return None if s.lower() in NOT_AVAILABLE else s


def map_records(page_html: str) -> list[dict]:
    """Pull the marker array out of the map page. Returns [] if the register changes the layout."""
    anchor = page_html.find(MAP_ANCHOR)
    start = page_html.find("[", anchor if anchor != -1 else 0)
    if start == -1:
        return []
    try:
        arr, _ = json.JSONDecoder().raw_decode(page_html[start:])
    except ValueError:
        return []
    return arr if isinstance(arr, list) else []


class MahaReraSource(NullSource):
    name = "maharera"

    def __init__(self, max_candidates: int | None = None):
        self.max_candidates = max_candidates or settings.maharera_max_candidates

    async def discover(self, ctx: ScanContext) -> list[Candidate]:
        if ctx.own.lat is None or ctx.own.lng is None:
            return []
        res = await ctx.fetcher.get(MAP)
        if not res.ok:
            return []
        records = map_records(res.text)
        pin_uses = Counter((r.get("Latitude"), r.get("Longitude")) for r in records)
        district = settings.rera_district.strip().lower()
        limit = ctx.radius_km + settings.discovery_margin_km

        near: list[tuple[float, float, float, dict]] = []
        for r in records:
            if (r.get("project_District") or "").strip().lower() != district:
                continue
            if pin_uses[(r.get("Latitude"), r.get("Longitude"))] > PLACEHOLDER_PIN_USES:
                continue
            try:
                lat, lng = float(r["Latitude"]), float(r["Longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            dist = haversine_km(ctx.own.lat, ctx.own.lng, lat, lng)
            if dist <= limit:
                near.append((dist, lat, lng, r))
        near.sort(key=lambda t: t[0])

        out: list[Candidate] = []
        for _, lat, lng, r in near[: self.max_candidates]:
            name = _clean(r.get("projectName"))
            if not name:
                continue
            locality = _clean(r.get("locality")) or _clean(r.get("project_Village"))
            address = ", ".join(x for x in (locality, _clean(r.get("street")), _clean(r.get("pincode"))) if x) or None
            out.append(Candidate(name=name, rera_no=_clean(r.get("CertificateNo")), lat=lat, lng=lng,
                                 address=address, locality=locality or ctx.own.locality, source="maharera", source_url=MAP))
        return out

    async def pages_for(self, project: Project, ctx: ScanContext) -> list[Page]:
        pages: list[Page] = []
        numbers = {ph.number for fv in project.rera_phases.values for ph in fv.value}
        urls = [u for u in project.pages_seen if u.startswith(BASE) and u != MAP]
        for n in list(numbers)[:3]:
            res = await ctx.fetcher.get(DETAIL, params={"registration_no": n})
            # The register answers every registration number with 200 and echoes it back even when it has no report,
            # so the "no records" banner is what separates a real summary page from the empty search form.
            if res.ok and n in res.text and NO_RECORDS not in res.text:
                pages.append(Page(url=str(res.url), source="maharera", text=html_to_text(res.text, settings.max_page_chars), fetched_at=res.fetched_at))
        for u in urls:
            if any(p.url == u for p in pages):
                continue
            res = await ctx.fetcher.get(u)
            if res.ok:
                pages.append(Page(url=u, source="maharera", text=html_to_text(res.text, settings.max_page_chars), fetched_at=res.fetched_at))
        return pages
