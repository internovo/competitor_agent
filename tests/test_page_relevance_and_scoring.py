"""A page has to be about the project before its numbers count, and a score has to
say what it is out of.

The 23 September Kandivali West scan put a different Ruparel building's
configurations, carpet, rate and possession into the #1 row of a live table. These
are the rules that came out of it. Named page fixtures live in
tests/fixtures/pages/; the full 581-page corpus this was measured on is a replay
job, not a unit test -- see the PR body for where it lives.
"""
from datetime import date

from app.extract.deterministic import mentions_project, relevant_pages
from app.logic import completeness, match_score
from app.models.schema import Project
from app.service import confirmed, hands_over_before, rank, scoring_note
from tests.conftest import fv


# --- A2: does this page name this project -----------------------------------

def test_a_two_character_name_still_identifies_its_pages():
    """"Mumbai XL" reduced to zero searchable tokens: "XL" is under three characters
    and "mumbai" is a stop word. Nothing matched, so nothing looked more relevant
    than anything else."""
    assert mentions_project("Ruparel Mumbai XL, Kandivali West. 1 BHK homes.", "Mumbai XL")


def test_the_same_builders_other_project_is_not_a_match():
    """The exact pair that contaminated the live table."""
    assert not mentions_project("Ruparel Optima, Kandivali West. 2 BHK.", "Mumbai XL")


def test_digits_carry_identity_too():
    assert mentions_project("Mahindra Marina 64 at Malad West", "Marina 64")


def test_nothing_matching_yields_nothing():
    """Reversed deliberately. Reading every page when none matched is what blended
    two buildings into one row; a blank field with a reason cannot do that."""
    assert relevant_pages(["some page", "another"], "Totally Absent Name",
                          text_of=lambda p: p) == []


def test_a_matching_page_beside_a_foreign_one_keeps_only_the_match():
    pages = ["Chandak Treesourus offers 2 and 3 BHK", "K Raheja Interface Heights on rent"]
    assert relevant_pages(pages, "Chandak Treesourus", text_of=lambda p: p) == [pages[0]]


# --- A3: confirmation, with MahaRERA unavailable ----------------------------

def _p(**kw) -> Project:
    return Project(id="x", name="Somewhere Heights", **kw)


def test_two_independent_domains_confirm_a_project():
    assert confirmed(_p(confirmed_by=["squareyards.com", "magicbricks.com"]))


def test_one_domain_is_not_confirmation():
    assert not confirmed(_p(confirmed_by=["squareyards.com"]))
    assert not confirmed(_p(confirmed_by=[]))


def test_an_unconfirmed_row_never_outranks_a_confirmed_one(own):
    """Even when it is the better-covered row. The register cannot be read, so two
    unrelated publishers agreeing is the strongest check left."""
    good = Project(id="a", name="Confirmed", status="under_construction", distance_km=1.4,
                   completeness=4, label="PARTIAL", pages_seen=["u"],
                   confirmed_by=["squareyards.com", "99acres.com"])
    lonely = Project(id="b", name="Unconfirmed", status="under_construction", distance_km=0.1,
                     completeness=6, label="COMPARABLE", pages_seen=["u"],
                     confirmed_by=["squareyards.com"])
    assert [p.id for p in rank([lonely, good])] == ["a", "b"]


# --- A4: early handover is not competition ----------------------------------

def test_a_project_handing_over_long_before_the_subject_is_separated(own):
    own.possession = date(2028, 4, 1)
    early = Project(id="e", name="Early", status="under_construction")
    early.possession.observe(fv(date(2026, 12, 1)))
    assert hands_over_before(early, own) == 16


def test_a_project_handing_over_near_the_subject_is_not(own):
    own.possession = date(2028, 4, 1)
    close = Project(id="c", name="Close", status="under_construction")
    close.possession.observe(fv(date(2027, 9, 1)))
    assert hands_over_before(close, own) is None


def test_no_possession_on_either_side_is_not_a_judgement(own):
    own.possession = None
    p = Project(id="n", name="No date", status="under_construction")
    assert hands_over_before(p, own) is None


# --- A5: a percentage, with its denominator attached ------------------------

def test_the_score_is_a_percentage_of_what_could_be_compared(full_project, own):
    p = match_score.apply(completeness.apply(full_project), own, 1.5)
    assert p.match_score is not None and p.score_max
    assert p.score_100 == round(100 * p.match_score / p.score_max)
    assert p.score_coverage == len(p.score_breakdown)


def test_nothing_compared_is_not_scored_rather_than_zero(own):
    """Zero reads as "scored badly". Not scored reads as what it is."""
    thin = Project(id="t", name="Thin", status="under_construction", distance_km=0.2)
    match_score.apply(completeness.apply(thin), own, 1.5)
    assert thin.match_score is None and thin.score_100 is None and thin.score_coverage == 0


def test_the_weights_are_reported_as_they_are_in_code(own, full_project):
    note = scoring_note(own, [match_score.apply(completeness.apply(full_project), own, 1.5)])
    assert note["total_weight"] == 100
    assert note["weights"] == {"config": 25, "carpet": 20, "rate": 20,
                               "possession": 15, "distance": 10, "structure": 10}
    assert note["sort"]


def test_a_field_the_subject_lacks_is_named_so_the_builder_can_fix_it(own, full_project):
    """A subject with no rate costs every row 20 points of denominator. The portal
    can only ask the builder to fill it in if the payload says which field it was."""
    own.rate_psf = None
    p = match_score.apply(completeness.apply(full_project), own, 1.5)
    note = scoring_note(own, [p])
    assert note["subject_missing"] == ["rate_psf"]
    assert "rate_psf" in note["note"]
