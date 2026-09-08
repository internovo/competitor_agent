"""Fixture source: hand-written competitor data so the whole pipeline runs with no network and no LLM.

File format (data/fixtures/<name>.json):
{
  "candidates": [Candidate, ...],
  "facts": { "<candidate name or rera_no>": [ {"source": "...", "url": "...", "fetched_at": "...", "facts": ExtractedFacts}, ... ] }
}
Each facts entry becomes one Page of kind json_facts; extraction parses it directly instead of asking Claude.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.config import FIXTURE_DIR
from app.models.schema import Candidate, Page, Project
from app.sources.base import NullSource, ScanContext


class FixtureSource(NullSource):
    name = "fixture"

    def __init__(self, path: Path | None = None):
        self.path = path or (FIXTURE_DIR / "competitors_malad_west.json")
        self._data = json.loads(self.path.read_text())

    async def discover(self, ctx: ScanContext) -> list[Candidate]:
        return [Candidate(**c) for c in self._data["candidates"]]

    async def pages_for(self, project: Project, ctx: ScanContext) -> list[Page]:
        entries = self._data["facts"].get(project.id) or self._data["facts"].get(project.match_name) or []
        pages = []
        for e in entries:
            fetched = datetime.fromisoformat(e["fetched_at"]) if e.get("fetched_at") else datetime.now(timezone.utc)
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=timezone.utc)
            pages.append(Page(url=e.get("url") or f"fixture://{project.id}/{e['source']}", source=e["source"],
                              text=json.dumps(e["facts"]), fetched_at=fetched, kind="json_facts"))
        return pages
