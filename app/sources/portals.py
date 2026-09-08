"""Direct portal fetchers. SquareYards is plain HTML; Housing.com needs Chrome TLS impersonation.
Both are 'find the project page via a site search, then hand the page text to Claude'."""
from __future__ import annotations

import re
from urllib.parse import quote_plus

from app.config import settings
from app.models.schema import Page, Project
from app.sources.base import NullSource, ScanContext
from app.sources.fetch import html_to_text


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


class BuilderSiteSource(NullSource):
    """Finds the builder's own project page through Tavily (excluding portals) and fetches it."""
    name = "builder_site"

    def __init__(self, tavily):
        self.tavily = tavily

    async def pages_for(self, project: Project, ctx: ScanContext) -> list[Page]:
        if not settings.tavily_api_key:
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
