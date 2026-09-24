"""A web page is not a building, and the subject is not its own competitor.

On 23 September a live Malad West table opened with
"Priyam Residency vs Silicon Park 3 — Compare Price Location" at 100%, 0.00 km away,
because it was the subject, found through a comparison page's title. A price filter,
"75 Lakhs to 1 Crore", came in the same way.
"""
import asyncio

from app.graph.nodes import pipeline
from app.logic import completeness, match_score
from app.logic.match_score import MIN_SCORED_DIMENSIONS
from app.logic.resolve import looks_like_a_page
from app.models.schema import Candidate, OwnProject


# --- a page title is not a project name -------------------------------------

def test_the_two_titles_that_reached_a_live_table_are_rejected():
    assert looks_like_a_page("Priyam Residency vs Silicon Park 3 — Compare Price Location")
    assert looks_like_a_page("75 Lakhs to 1 Crore")


def test_other_shapes_of_page_title_are_rejected_too():
    for title in ("Compare Lodha Amara and Runwal Vertex",
                  "2 BHK Flats for Sale in Malad West",
                  "Borivali West Mumbai: Map Property Rates",
                  "Kandivali West Price Trends 2026",
                  "Projects from 1 Crore to 3 Crore"):
        assert looks_like_a_page(title), title


def test_real_projects_the_first_version_of_this_rule_threw_away():
    """The rule shipped narrower than it started. "apartments in", "flats in" and
    "projects in" were in it and rejected four real buildings on the Kandivali corpus,
    because that is how portals suffix a perfectly good project title."""
    for title in ("Jaswanti Jewel | Apartments In Kandivali West",
                  "La Serena | Apartments In Kandivali West",
                  "Shreeji Skyrise | Apartments In Kandivali West",
                  "Royal Lagoon | Apartments In Malad West",
                  "Westcenter by Origin Corp | Luxury Apartments in Kandivali West"):
        assert not looks_like_a_page(title), title


def test_a_promoter_whose_name_starts_with_vs_is_not_a_comparison():
    """"VS DEVELOPERS" matched when the pattern allowed "vs" at the start of a name."""
    assert not looks_like_a_page("VS DEVELOPERS")
    assert not looks_like_a_page("Vs Sharma Residency")


def test_ordinary_project_names_survive():
    for title in ("Rustomjee Alpine", "Chandak Eden Garden", "Ruparel Mumbai XL",
                  "Marina64", "Airavat By Bhoomi Group", "Sheth Auris Serenity Tower 4",
                  "Laxmi Shrushti - THE LAXMI GROUP", "Orlem Malad West"):
        assert not looks_like_a_page(title), title


# --- the subject is never its own competitor --------------------------------

def _thin_subject(own: OwnProject) -> OwnProject:
    """A subject with only configurations on file. Every other dimension is then
    excluded for every competitor, which is exactly how a 6/6 row ends up scored on
    two things -- and how Priyam Residency's table was scored on 23 Sep."""
    own.carpet_sqft = own.rate_psf = own.possession = own.structure = None
    return own


def test_a_percentage_needs_more_than_two_dimensions(own, full_project):
    """35/35 on configurations and distance read as a perfect match. Both were
    perfect because the row was the subject; even when it is not, two dimensions
    cannot carry a number a rep reads as 100%."""
    p = match_score.apply(completeness.apply(full_project), _thin_subject(own), 1.5)
    assert p.score_coverage < MIN_SCORED_DIMENSIONS
    assert p.score_100 is None
    # The parts are still published, so the portal can say "compared on 2 of 6".
    assert p.match_score is not None and p.score_max is not None


def test_a_percentage_is_shown_once_enough_was_compared(own, full_project):
    p = match_score.apply(completeness.apply(full_project), own, 1.5)
    assert p.score_coverage >= MIN_SCORED_DIMENSIONS
    assert p.score_100 == round(100 * p.match_score / p.score_max)


def test_the_card_says_why_no_percentage_is_shown(own, full_project):
    from app import service
    p = match_score.apply(completeness.apply(full_project), _thin_subject(own), 1.5)
    card = service.card(p, 1)
    assert card["score_100"] is None
    assert "2 of 6" in card["score_note"]
    assert card["earned"] is not None and card["available"] is not None


# --- the subject, under any name --------------------------------------------

def _resolve(own: OwnProject, *cands: Candidate) -> dict:
    state = {"own": own, "radius_km": 1.5, "candidates": list(cands),
             "mode": "replay", "scan_id": "t", "projects": [], "dropped": [], "log": []}
    return asyncio.run(pipeline.resolve_node(state, {"configurable": {"llm": None}}))


def test_the_subject_under_a_page_title_is_not_its_own_competitor(own):
    """0.00 km away and carrying the subject's own name. Identity by name string alone
    missed it, because "Priyam Residency vs Silicon Park 3 - Compare Price Location"
    slugs to something else entirely."""
    twin = Candidate(name=f"{own.name} vs Silicon Park 3 - Compare Price Location",
                     lat=own.lat, lng=own.lng, source="tavily")
    out = _resolve(own, twin)
    assert [p.name for p in out["projects"]] == []
    assert any(d["reason"] == "own project" for d in out["dropped"])


def test_a_different_building_at_the_same_address_is_still_a_competitor(own):
    """The guard needs BOTH conditions. Two towers share a plot often enough that
    proximity alone must not disqualify one, and a candidate carrying the subject's
    name is already caught by the name rule wherever it stands."""
    neighbour = Candidate(name="Silicon Park 3", lat=own.lat, lng=own.lng, source="tavily")
    out = _resolve(own, neighbour)
    assert [p.name for p in out["projects"]] == ["Silicon Park 3"]
