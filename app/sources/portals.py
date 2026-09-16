"""Direct portal fetchers. SquareYards is plain HTML; Housing.com needs Chrome TLS impersonation.
Both are 'find the project page via a site search, then hand the page text to Claude'."""
from __future__ import annotations

import json
import re
from urllib.parse import quote_plus

from app.config import settings
from app.models.schema import Candidate, Page, Project
from app.sources.base import NullSource, ScanContext
from app.sources.fetch import CacheMiss, html_to_text


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _matches(href: str, project: Project, locality: str | None) -> bool:
    """Does this link plausibly point at THIS project?

    The old test was the project name's first token appearing anywhere in the href, so
    a search for "Godrej Prime, Malad West" matched
    /godrej-golf-links-crown-residences-sector-27-yamuna-expressway-npd-343924 and
    fetched a Greater Noida project into a Mumbai scan. One shared word is never enough:
    take two distinctive tokens, or one plus the locality.
    """
    from app.logic.resolve import tokens

    hits = sum(1 for t in tokens(project.match_name) if len(t) > 2 and t in href)
    loc = _slug(locality or "")
    return hits >= 2 or (hits >= 1 and bool(loc) and loc in href)


class SquareYardsSource(NullSource):
    name = "squareyards"

    async def pages_for(self, project: Project, ctx: ScanContext) -> list[Page]:
        from bs4 import BeautifulSoup

        q = f"{project.match_name} {ctx.own.locality or 'Mumbai'}"
        res = await ctx.fetcher.get(f"https://www.squareyards.com/search?q={quote_plus(q)}", impersonate=True)
        if not res.ok:
            return []
        soup = BeautifulSoup(res.text, "lxml")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "-npd-" in href and _matches(href, project, ctx.own.locality):
                url = href if href.startswith("http") else "https://www.squareyards.com" + href
                page = await ctx.fetcher.get(url, impersonate=True)
                if page.ok:
                    return [Page(url=url, source="squareyards", text=html_to_text(page.text, settings.max_page_chars), fetched_at=page.fetched_at)]
        return []


class HousingSource(NullSource):
    name = "housing"

    async def pages_for(self, project: Project, ctx: ScanContext) -> list[Page]:
        from bs4 import BeautifulSoup

        q = f"{project.match_name} {ctx.own.locality or 'Mumbai'}"
        res = await ctx.fetcher.get(f"https://housing.com/in/buy/searches/{_slug(q)}", impersonate=True)
        if not res.ok:
            return []
        soup = BeautifulSoup(res.text, "lxml")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/in/buy/projects/page/" in href and _matches(href, project, ctx.own.locality):
                url = href if href.startswith("http") else "https://housing.com" + href
                page = await ctx.fetcher.get(url, impersonate=True)
                if page.ok:
                    return [Page(url=url, source="housing", text=html_to_text(page.text, settings.max_page_chars), fetched_at=page.fetched_at)]
        return []


_SY_STATUS = {"under construction": "under_construction", "new launch": "new_launch",
              "ready to move": "ready", "completed": "completed"}
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _possession(value: str | None) -> str | None:
    """'Dec 2028' -> '2028-12'. Anything else is left for another source to answer."""
    m = re.match(r"([A-Za-z]{3})[a-z]*\s+(20\d{2})$", (value or "").strip())
    month = _MONTHS.get(m.group(1).lower()) if m else None
    return f"{m.group(2)}-{month:02d}" if month else None


def _record(item: dict) -> dict | None:
    """One JSON-LD ApartmentComplex -> the fields we are willing to believe from it.

    Carpet and rate are deliberately absent. `floorSize` never says whether it is
    carpet or saleable -- roughly a 30% difference on the field this form leads with --
    and the listing quotes a ticket price, not a rate. Deriving either would be the
    estimate this whole pipeline refuses to make.
    """
    name = (item.get("name") or "").strip()
    geo = item.get("geo") or {}
    props = {p.get("propertyID"): p for p in (item.get("additionalProperty") or []) if isinstance(p, dict)}
    plans = item.get("accommodationFloorPlan") or []
    area = props.get("totalArea") or {}
    units = item.get("numberOfAccommodationUnits") or {}
    if not name:
        return None
    bhk = sorted({p["numberOfBedrooms"] for p in plans
                  if isinstance(p, dict) and isinstance(p.get("numberOfBedrooms"), int)})
    return {
        "name": name,
        "url": item.get("url") or item.get("mainEntityOfPage"),
        "builder": ((item.get("brand") or {}).get("name") or None),
        "lat": geo.get("latitude"),
        "lng": geo.get("longitude"),
        "status": _SY_STATUS.get(str((props.get("projectStatus") or {}).get("value", "")).strip().lower()),
        "possession": _possession(str((props.get("possessionDate") or {}).get("value", ""))),
        "configurations": bhk or None,
        "total_units": units.get("value") if isinstance(units.get("value"), int) else None,
        "land_acres": area.get("value") if area.get("unitCode") == "ACR" else None,
    }


def parse_listing(html: str) -> list[dict]:
    """Every project the listing page names, from its own JSON-LD."""
    out = []
    for blob in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(blob)
        except ValueError:
            continue   # one malformed block is not a reason to lose the page
        for item in (data.get("@graph") or []) if isinstance(data, dict) else []:
            if not isinstance(item, dict) or "ApartmentComplex" not in str(item.get("@type")):
                continue
            rec = _record(item)
            if rec:
                out.append(rec)
    return out


class SquareYardsListingSource(NullSource):
    """SquareYards' locality page: one fetch, every project it lists in the neighbourhood.

    The search endpoint the other adapter uses answers a name query with a national
    recommendation carousel -- 297 of 303 recorded SquareYards fetches were that page,
    3 were a project, and a Borivali scan got one in-radius project out of the lot.
    `/projects-in-<locality>-<city>` is the page that lists the neighbourhood, and it
    ships a JSON-LD ItemList: coordinates, lifecycle, possession, builder and BHK per
    floor plan, for 36 projects, in one fetch and no model.

    Additive by design. It adds candidates and coordinates; it never drops one, never
    overrules another source, and if the page 404s or the corpus predates it the scan
    is exactly the scan we run today. Kill it by removing `squareyards_list` from
    SOURCES.
    """
    name = "squareyards_list"

    def __init__(self):
        # Parsed once per scan: the fan-out asks for the same 1 MB page 40 times over.
        self._records: dict[str, list[dict]] = {}

    def _url(self, ctx: ScanContext) -> str | None:
        """`/projects-in-borivali-west-mumbai`. A wrong locality slug 404s, which reads
        here as a source that found nothing -- the same as a source that is switched off."""
        loc = ctx.own.locality
        if not loc:
            return None
        # propOG puts the city in `address` and nowhere else; the tail of an Indian
        # address is the city, minus the pincode.
        parts = [p.strip() for p in re.sub(r"\b\d{6}\b", "", ctx.own.address or "").split(",") if p.strip()]
        city = parts[-1] if parts else "Mumbai"
        return f"https://www.squareyards.com/projects-in-{_slug(loc)}-{_slug(city)}"

    async def _load(self, ctx: ScanContext) -> list[dict]:
        url = self._url(ctx)
        if url is None:
            return []
        if url not in self._records:
            try:
                res = await ctx.fetcher.get(url, impersonate=True)
            except CacheMiss:
                # A recording made before this source existed has no listing page in it.
                # That is an older corpus, not a failure, and it must still replay.
                self._records[url] = []
                return []
            self._records[url] = parse_listing(res.text) if res.ok else []
        return self._records[url]

    async def discover(self, ctx: ScanContext) -> list[Candidate]:
        return [Candidate(name=r["name"], builder=r["builder"], lat=r["lat"], lng=r["lng"],
                          locality=ctx.own.locality, source="squareyards", source_url=r["url"])
                for r in await self._load(ctx)]

    async def pages_for(self, project: Project, ctx: ScanContext) -> list[Page]:
        from app.logic.resolve import slug

        for r in await self._load(ctx):
            if r["url"] != project.source_url and slug(r["name"]) != slug(project.match_name):
                continue
            facts = {k: v for k, v in r.items() if k not in ("name", "url", "lat", "lng") and v is not None}
            facts |= {"name": r["name"], "lat": r["lat"], "lng": r["lng"]}
            # The listing page, not the project page. It is where these values were
            # published, and the project page is usually fetched as the seed already --
            # a page carrying its URL is dropped as a duplicate before it is read.
            return [Page(url=self._url(ctx), source="squareyards", kind="json_facts",
                         text=json.dumps({k: v for k, v in facts.items() if v is not None}))]
        return []


class BuilderSiteSource(NullSource):
    """Finds the builder's own project page through Tavily (excluding portals) and fetches it."""
    name = "builder_site"

    def __init__(self, tavily):
        self.tavily = tavily

    async def pages_for(self, project: Project, ctx: ScanContext) -> list[Page]:
        # A missing key only stops a live search. In replay the answer is already in
        # the cache, and gating on the key made an offline run with keys unset return
        # a different competitor set from the same cache -- which is the one thing a
        # frozen regression surface must not do.
        if not settings.tavily_api_key and ctx.fetcher.mode == "live":
            return []
        q = f"{project.match_name} {project.builder or ''} official site amenities".strip()
        from app.sources.tavily_web import PORTAL_DOMAINS

        results = await self.tavily.search(ctx, q, max_results=8)
        for r in results:
            url = r.get("url", "")
            if not url or any(d in url for d in PORTAL_DOMAINS + ["wikipedia.org", "youtube.com", "facebook.com", "instagram.com"]):
                continue
            builder_tok = _slug(project.builder or "").split("-")[0]
            if builder_tok and builder_tok in url or _slug(project.match_name).split("-")[0] in url:
                res = await ctx.fetcher.get(url, impersonate=True)
                if res.ok and len(res.text) > 500:
                    return [Page(url=url, source="builder_site", text=html_to_text(res.text, settings.max_page_chars), fetched_at=res.fetched_at)]
        return []
