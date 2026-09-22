from __future__ import annotations

from app.config import settings
from app.sources.base import Source
from app.sources.fixture import FixtureSource
from app.sources.maharera_legacy import MahaReraSource
from app.sources.osm import OsmSource
from app.sources.places import PlacesSource
from app.sources.portals import BuilderSiteSource, HousingSource, SquareYardsListingSource, SquareYardsSource
from app.sources.propog import PropOGSource
from app.sources.tavily_web import TavilySource


def build_sources(mode: str, names: list[str] | None = None, nearby: list | None = None) -> list[Source]:
    """`nearby` is the published propOG projects the caller was handed in the scan
    request. Offline, there is no request to be handed anything by, so fixture and
    replay fall back to the fixture file; live never does."""
    if mode == "fixture":
        return [FixtureSource(), PropOGSource.from_fixture()]
    names = names or settings.source_list
    tavily = TavilySource()
    table = {
        "maharera": MahaReraSource(), "places": PlacesSource(), "osm": OsmSource(), "tavily": tavily, "squareyards": SquareYardsSource(),
        "squareyards_list": SquareYardsListingSource(),
        "housing": HousingSource(), "builder_site": BuilderSiteSource(tavily),
        "propog": PropOGSource.from_fixture() if mode == "replay" else PropOGSource(nearby),
    }
    return [table[n] for n in names if n in table]
