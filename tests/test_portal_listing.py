"""The SquareYards locality page, read off three records carved from the real one.

The fixture is verbatim JSON-LD from `/projects-in-borivali-west-mumbai`, so a
change in their schema fails here rather than in a live scan.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models.schema import ExtractedFacts, OwnProject, Project
from app.sources.base import ScanContext
from app.sources.portals import SquareYardsListingSource, parse_listing

FIXTURE = Path(__file__).parent / "fixtures" / "squareyards_listing.json"
PAGE = f'<html><head><script type="application/ld+json">{FIXTURE.read_text(encoding="utf-8")}</script></head></html>'


class _Fetcher:
    """Serves the fixture for any URL, and records what was asked for."""
    mode = "replay"

    def __init__(self):
        self.calls: list[str] = []

    async def get(self, url, **kw):
        self.calls.append(url)
        from app.sources.fetch import FetchResult
        from datetime import datetime, timezone
        return FetchResult(url=url, status=200, text=PAGE, fetched_at=datetime.now(timezone.utc), from_cache=True)


def _ctx(fetcher=None):
    own = OwnProject(id="s", name="Link Horizon", address="Link Road, Borivali West, Mumbai 400091",
                     locality="Borivali West", lat=19.2307, lng=72.8567)
    return ScanContext(own=own, radius_km=1.5, fetcher=fetcher or _Fetcher())


def test_records_carry_coordinates_lifecycle_and_possession():
    by_name = {r["name"]: r for r in parse_listing(PAGE)}
    veena = by_name["Veena Celestia"]
    assert veena["status"] == "new_launch"
    assert veena["possession"] == "2030-12"
    assert veena["configurations"] == [2, 3]   # from numberOfBedrooms on its four floor plans
    assert (round(veena["lat"], 3), round(veena["lng"], 3)) == (19.231, 72.854)
    assert by_name["Nicco Vanashri Heights"]["status"] == "under_construction"


def test_carpet_and_rate_are_never_taken_from_a_listing():
    """`floorSize` does not say carpet or saleable and the price is a ticket price.
    Both would have to be guessed at, so neither is allowed out of this source."""
    for rec in parse_listing(PAGE):
        assert not {k for k in rec if "carpet" in k or "rate" in k or "price" in k}


@pytest.mark.asyncio
async def test_discover_yields_placed_candidates():
    cands = await SquareYardsListingSource().discover(_ctx())
    assert {c.name for c in cands} >= {"Veena Celestia", "Nicco Vanashri Heights"}
    assert all(c.lat and c.lng for c in cands), "a candidate without coordinates costs a Places call"
    assert all(c.source == "squareyards" for c in cands)


@pytest.mark.asyncio
async def test_page_is_a_form_the_extractor_already_understands():
    src = SquareYardsListingSource()
    ctx = _ctx()
    project = Project(id="p", name="Veena Celestia", name_raw="Veena Celestia")
    pages = await src.pages_for(project, ctx)
    assert len(pages) == 1 and pages[0].kind == "json_facts"
    facts = ExtractedFacts(**json.loads(pages[0].text))   # the shape the pipeline merges
    assert facts.status == "new_launch" and facts.possession == "2030-12"
    assert facts.carpet_min_sqft is None and facts.rate_min_psf is None


@pytest.mark.asyncio
async def test_listing_is_fetched_once_for_the_whole_scan():
    """The fan-out asks 40 times; the page is a megabyte."""
    src, fetcher = SquareYardsListingSource(), _Fetcher()
    ctx = _ctx(fetcher)
    await src.discover(ctx)
    for name in ("Veena Celestia", "Nicco Vanashri Heights", "Nobody Here"):
        await src.pages_for(Project(id=name, name=name, name_raw=name), ctx)
    assert len(fetcher.calls) == 1
    assert fetcher.calls[0] == "https://www.squareyards.com/projects-in-borivali-west-mumbai"


@pytest.mark.asyncio
async def test_a_corpus_recorded_before_this_source_existed_still_replays():
    """A cache miss here is an older recording, not a failure, and must not fail a scan."""
    from app.sources.fetch import CacheMiss

    class Missing(_Fetcher):
        async def get(self, url, **kw):
            raise CacheMiss(url)

    src = SquareYardsListingSource()
    ctx = _ctx(Missing())
    assert await src.discover(ctx) == []
    assert await src.pages_for(Project(id="p", name="Veena Celestia", name_raw="Veena Celestia"), ctx) == []
