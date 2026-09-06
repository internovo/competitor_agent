"""OpenStreetMap Nominatim: a key-free geocoder, used when Places has no key or cannot place an address.

Discovery is not its job - Nominatim knows finished buildings, not launches - so it only answers `geocode`.
Nominatim's usage policy asks for an identifying User-Agent and at most one call a second; both are honoured here.
"""
from __future__ import annotations

import asyncio

from app.models.schema import Candidate, Page, Project
from app.sources.base import NullSource, ScanContext

SEARCH_URL = "https://nominatim.openstreetmap.org/search"
HEADERS = {"User-Agent": "propOG-competitor-agent/0.1 (demo; contact: tech@internovo.in)"}
MIN_INTERVAL_S = 1.1


class OsmSource(NullSource):
    name = "osm"

    def __init__(self) -> None:
        self._gate = asyncio.Lock()

    async def geocode(self, ctx: ScanContext, query: str) -> tuple[float, float, str] | None:
        async with self._gate:
            res = await ctx.fetcher.get(SEARCH_URL, params={"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "in"},
                                        headers=HEADERS)
            if not res.from_cache:
                await asyncio.sleep(MIN_INTERVAL_S)
        if not res.ok:
            return None
        hits = res.json()
        if not hits:
            return None
        return float(hits[0]["lat"]), float(hits[0]["lon"]), hits[0].get("display_name", "")

    async def discover(self, ctx: ScanContext) -> list[Candidate]:
        return []

    async def pages_for(self, project: Project, ctx: ScanContext) -> list[Page]:
        return []
