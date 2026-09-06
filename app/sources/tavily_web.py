"""Tavily: web discovery ("new launch <locality>") and neutral page extraction for anything we can't scrape directly."""
from __future__ import annotations

import re

from app.config import settings
from app.models.schema import Candidate, Page, Project
from app.sources.base import NullSource, ScanContext

SEARCH_URL = "https://api.tavily.com/search"
EXTRACT_URL = "https://api.tavily.com/extract"
PORTAL_DOMAINS = ["99acres.com", "magicbricks.com", "housing.com", "squareyards.com", "proptiger.com", "nobroker.in"]
PORTAL_SOURCE = {"99acres.com": "99acres", "magicbricks.com": "magicbricks", "housing.com": "housing", "squareyards.com": "squareyards"}
YEAR = 2026
# Portal listing pages title themselves "97+ New Launch Projects", "Property Rates", "Flats". They carry no project,
# and each one costs a full extraction pass, so a title made only of these words is dropped at discovery.
GENERIC_TITLE_WORDS = {
    "project", "projects", "property", "properties", "flat", "flats", "apartment", "apartments", "home", "homes",
    "house", "housing", "builder", "builders", "residential", "commercial", "new", "launch", "launches", "under",
    "construction", "ready", "possession", "upcoming", "luxury", "premium", "affordable", "resale", "sale", "buy",
    "rent", "price", "prices", "rate", "rates", "trend", "trends", "review", "reviews", "photo", "photos", "video",
    "videos", "list", "listing", "listings", "result", "results", "page", "of", "for", "in", "near", "by", "with",
    "and", "the", "best", "top", "bhk", "sq", "ft", "mumbai", "india", "south", "north", "east", "west", "central",
    "map", "details", "detail", "info", "guide", "updated", "today",
}


def _auth() -> dict:
    return {"Authorization": f"Bearer {settings.tavily_api_key or ''}", "Content-Type": "application/json"}


def _source_for(url: str) -> str:
    for dom, src in PORTAL_SOURCE.items():
        if dom in url:
            return src
    return "tavily"


def _is_generic(title: str) -> bool:
    """True when nothing in the title names a building - only counts, portal words and the locality."""
    words = [w for w in re.findall(r"[a-z0-9]+", title.lower()) if not w.isdigit()]
    locality_words = {w for w in re.findall(r"[a-z]+", title.lower()) if w in ("malabar", "hill", "hills")}
    return not [w for w in words if w not in GENERIC_TITLE_WORDS and w not in locality_words]


class TavilySource(NullSource):
    name = "tavily"

    async def search(self, ctx: ScanContext, query: str, include_domains: list[str] | None = None, max_results: int = 10) -> list[dict]:
        body = {"query": query, "search_depth": "advanced", "max_results": max_results, "country": "india"}
        if include_domains:
            body["include_domains"] = include_domains
        res = await ctx.fetcher.post(SEARCH_URL, json_body=body, headers=_auth())
        return res.json().get("results") or [] if res.ok else []

    async def extract(self, ctx: ScanContext, urls: list[str]) -> list[dict]:
        if not urls:
            return []
        res = await ctx.fetcher.post(EXTRACT_URL, json_body={"urls": urls[:20], "extract_depth": "basic"}, headers=_auth())
        return res.json().get("results") or [] if res.ok else []

    async def discover(self, ctx: ScanContext) -> list[Candidate]:
        loc = ctx.own.locality or ctx.own.address
        queries = [f"new launch residential project {loc} {YEAR}", f"under construction projects in {loc} possession {YEAR + 2}",
                   f"upcoming projects near {ctx.own.name} {loc}"]
        out: list[Candidate] = []
        seen: set[str] = set()
        for q in queries:
            for r in await self.search(ctx, q, include_domains=PORTAL_DOMAINS):
                title = re.sub(r"\s*[-|–].*$", "", r.get("title", "")).strip()
                title = re.sub(r"\b(in|at)\b.*$", "", title, flags=re.I).strip()
                if len(title) < 4 or title.lower() in seen or _is_generic(title):
                    continue
                seen.add(title.lower())
                out.append(Candidate(name=title, locality=ctx.own.locality, source="tavily", source_url=r.get("url")))
        return out

    async def pages_for(self, project: Project, ctx: ScanContext) -> list[Page]:
        queries = [f"{project.name} {project.builder or ''} {ctx.own.locality or ''} price carpet area possession RERA".strip()]
        queries += [f"{project.name} {q}" for q in ctx.extra_queries]
        urls: list[str] = []
        for q in queries:
            for r in await self.search(ctx, q, max_results=6):
                u = r.get("url")
                if u and u not in urls and u not in project.pages_seen:
                    urls.append(u)
        pages = []
        for r in await self.extract(ctx, urls[:8]):
            text = (r.get("raw_content") or "")[: settings.max_page_chars]
            if len(text) > 200:
                pages.append(Page(url=r["url"], source=_source_for(r["url"]), text=text))
        return pages
