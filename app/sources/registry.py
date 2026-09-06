from __future__ import annotations

from app.config import settings
from app.sources.base import Source
from app.sources.fixture import FixtureSource
from app.sources.maharera_legacy import MahaReraSource
from app.sources.osm import OsmSource
from app.sources.places import PlacesSource
from app.sources.portals import BuilderSiteSource, HousingSource, SquareYardsSource
from app.sources.propog import PropOGSource
from app.sources.tavily_web import TavilySource


def build_sources(mode: str, names: list[str] | None = None) -> list[Source]:
    if mode == "fixture":
        return [FixtureSource(), PropOGSource()]
    names = names or settings.source_list
    tavily = TavilySource()
    table = {
        "maharera": MahaReraSource(), "places": PlacesSource(), "osm": OsmSource(), "tavily": tavily, "squareyards": SquareYardsSource(),
        "housing": HousingSource(), "builder_site": BuilderSiteSource(tavily), "propog": PropOGSource(),
    }
    return [table[n] for n in names if n in table]
