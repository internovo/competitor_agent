"""What propOG shares, and what has to be true before it is scored.

Two separate guards, and 21 September needed both. The first decides which propOG
rows are allowed to be candidates at all -- published, and not the asking builder's.
The second decides whether a candidate that got through may be scored: something
outside propOG has to name it in the locality propOG claims for it.
"""
import asyncio
import json

import pytest

from app.graph.nodes import pipeline
from app.graph.state import ExtractInput
from app.logic import completeness, match_score
from app.models.schema import Page, Project
from app.sources.base import ScanContext
from app.sources.propog import PropOGSource, published_only


def row(pid, name, status="PUBLISHED", builder_id="b-other", **kw):
    """One entry of the `nearby` list propOG sends, 0.4-ish km from Marina64."""
    return {"id": pid, "name": name, "status": status, "builder_id": builder_id,
            "lat": 19.1895, "lng": 72.841, "locality": "Malad West", **kw}


# --- which rows are allowed to be candidates --------------------------------

@pytest.mark.parametrize("status", ["DRAFT", "ARCHIVED", "DEMO", "TEST", "", None])
def test_only_a_published_row_is_accepted(status):
    """A flag the builder sets deliberately, never a guess from the name.

    Excluding "anything that looks like a test" would have missed the next demo row
    and would drop a real project unlucky enough to be called Test Towers.
    """
    assert published_only([row("p1", "Somewhere Heights", status=status)], "b-me") == []


def test_a_published_row_is_accepted_and_becomes_a_candidate(own):
    kept = published_only([row("p1", "Somewhere Heights")], "b-me")
    assert [p.id for p in kept] == ["p1"]
    cands = asyncio.run(PropOGSource(kept).discover(
        ScanContext(own=own, radius_km=1.5, fetcher=None)))
    assert [(c.name, c.source, c.on_propog) for c in cands] == [("Somewhere Heights", "propog", True)]


def test_the_asking_builders_own_other_projects_are_excluded(own):
    """A builder's second tower is their inventory, not their competition."""
    rows = [row("mine", "My Other Tower", builder_id="b-me"), row("theirs", "Their Tower", builder_id="b-rival")]
    assert [p.id for p in published_only(rows, "b-me")] == ["theirs"]
    # And with the builder unknown, nothing is silently let through on a name match.
    assert [p.id for p in published_only(rows, None)] == ["mine", "theirs"]


def test_a_row_that_is_not_the_shape_we_expect_is_dropped_not_fatal():
    """One malformed entry must not cost the whole scan its propOG competitors."""
    assert [p.id for p in published_only([{"name": "no id at all"}, row("p1", "Fine")], "b-me")] == ["p1"]


def test_nothing_sent_means_no_propog_competitors_not_a_fixture(own):
    """The 21 September failure in one line: an empty wire must read as empty."""
    cands = asyncio.run(PropOGSource().discover(ScanContext(own=own, radius_km=1.5, fetcher=None)))
    assert cands == []


def test_a_row_is_read_the_way_propog_spells_it(own):
    """propOG says `latitude`/`longitude` and "2 BHK", the same as for the subject.

    Neither mistake would have raised: the row would have validated with no
    coordinates and been dropped a step later as out of radius, which reads as
    "no propOG competitors nearby" rather than "we could not read the wire".
    """
    wire = {"id": "p1", "name": "Somewhere Heights", "status": "PUBLISHED", "builder_id": "b-other",
            "latitude": 19.1895, "longitude": 72.841, "configurations": ["2 BHK", "3 BHK", "penthouse"]}
    p = published_only([wire], "b-me")[0]
    assert (p.lat, p.lng) == (19.1895, 72.841)
    assert p.configurations == [2, 3]       # "penthouse" states no number and is not guessed at


def test_a_published_project_with_gaps_still_hands_over_what_it_has(own):
    """A real project is not a hand-written fixture: it fills in what the builder did.

    Every field used to be dereferenced unconditionally, so one published project
    without a carpet range would have thrown inside the source and cost the scan
    every propOG fact it had.
    """
    kept = published_only([row("p1", "Somewhere Heights", possession="2029-06-01")], "b-me")
    pages = asyncio.run(PropOGSource(kept).pages_for(
        Project(id="p1", name="Somewhere Heights"), ScanContext(own=own, radius_km=1.5, fetcher=None)))
    facts = json.loads(pages[0].text)
    assert facts["possession"] == "2029-06-01"
    assert facts["carpet_min_sqft"] is None and facts["rate_min_psf"] is None
    assert "towers" not in facts            # no structure sent; every ExtractedFacts key is optional


# --- whether a candidate that got through may be scored ---------------------

class PageSource:
    """Serves whatever pages the test wants, the way a portal source would."""
    name = "squareyards"

    def __init__(self, *pages):
        self.pages = list(pages)

    async def discover(self, ctx):
        return []

    async def pages_for(self, project, ctx):
        return list(self.pages)


def page(text, source="squareyards"):
    return Page(url=f"https://example.test/{source}/{abs(hash(text)) % 9999}", source=source, text=text, kind="html")


def corroborate(own, *pages, name="Somewhere Heights", locality="Malad West"):
    """Run the real extract node over a propOG candidate and these pages."""
    project = Project(id="p1", name=name, on_propog=True, locality=locality, status="under_construction")
    state = ExtractInput(own=own, radius_km=1.5, project=project, retry=False, extra_queries=[])
    config = {"configurable": {"sources": [PageSource(*pages)], "fetcher": None, "llm": None}}
    return asyncio.run(pipeline.extract(state, config))["projects"][0]


def test_an_outside_page_naming_it_in_the_locality_corroborates_it(own):
    p = corroborate(own, page("Somewhere Heights, Malad West. 2 and 3 BHK apartments under construction."))
    assert p.propog_corroborated is True


def test_the_same_name_in_another_locality_does_not(own):
    """The Kalpataru case exactly: the name is real, the building is 150 km away.

    Pages for "Kalpataru Aurum" exist. Every one of them is about the Baner, Pune
    project, and none of them is evidence of a Malad West tower.
    """
    p = corroborate(own, page("Somewhere Heights, Baner, Pune. 3 and 4 BHK under construction."))
    assert p.propog_corroborated is False


def test_propog_cannot_corroborate_itself(own):
    """The record is the claim, not the check."""
    p = corroborate(own, page("Somewhere Heights, Malad West, a fine building.", source="propog"))
    assert p.propog_corroborated is False


def test_no_page_at_all_does_not(own):
    assert corroborate(own).propog_corroborated is False


# --- what the label does about it -------------------------------------------

def _as_propog(project, corroborated):
    project.on_propog, project.propog_corroborated = True, corroborated
    return project


def test_an_uncorroborated_propog_project_is_unverified_and_unscored(full_project, own):
    p = completeness.apply(_as_propog(full_project, corroborated=False))
    match_score.apply(p, own, 1.5)
    assert p.completeness == 6              # a full form, and it still does not count
    assert p.label == "UNVERIFIED"
    assert p.match_score is None


def test_a_corroborated_propog_project_is_scored_exactly_as_before(full_project, own):
    """No silent regression on real data: a published project an outside source
    confirms scores the way it did before any of this existed."""
    p = completeness.apply(_as_propog(full_project, corroborated=True))
    match_score.apply(p, own, 1.5)
    assert p.label == "COMPARABLE"
    assert p.match_score is not None and p.match_score > 0
