"""Source adapters. Each knows how to (a) find candidates in a circle and (b) fetch pages about one project."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.models.schema import Candidate, OwnProject, Page, Project
from app.sources.fetch import Fetcher


@dataclass
class ScanContext:
    own: OwnProject
    radius_km: float
    fetcher: Fetcher
    extra_queries: list[str] = field(default_factory=list)  # used by the thin-retry pass


class Source(Protocol):
    name: str

    async def discover(self, ctx: ScanContext) -> list[Candidate]: ...

    async def pages_for(self, project: Project, ctx: ScanContext) -> list[Page]: ...


class NullSource:
    """Adapter base with no-op defaults so a source can implement only one side."""
    name = "null"

    async def discover(self, ctx: ScanContext) -> list[Candidate]:
        return []

    async def pages_for(self, project: Project, ctx: ScanContext) -> list[Page]:
        return []
