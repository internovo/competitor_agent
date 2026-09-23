"""A page has to be about the project before its numbers count, and a score has to
say what it is out of.

The 23 September Kandivali West scan put a different Ruparel building's
configurations, carpet, rate and possession into the #1 row of a live table. These
are the rules that came out of it. Named page fixtures live in
tests/fixtures/pages/; the full 581-page corpus this was measured on is a replay
job, not a unit test -- see the PR body for where it lives.
"""
from datetime import date

from app import service
from app.extract.deterministic import extract_rate, mentions_project, relevant_pages
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


# --- A2 second net: a rate is money, for this building --------------------------

def test_a_size_with_no_currency_is_not_a_rate():
    """'3 BHK+3T Apartments with Size 1630/sqft-carpet' offered Rs 1,630 per sq ft."""
    r = extract_rate("3 BHK+3T Apartments with Size 1630/sqft-carpet for sale at Rs 4.4 Cr "
                     "in Chandak Eden Gardens, Kandivali West Mumbai.")
    assert r.values == []
    assert r.absent == "RATE_NOT_PUBLISHED"


def test_a_city_average_from_a_market_report_is_not_this_buildings_rate():
    """Bangalore, lifted off a trends page a Kandivali candidate legitimately read."""
    r = extract_rate("Average residential prices increased from \u20b96,002 /sq.ft. (2019) "
                     "to \u20b99,963 /sq.ft. (2025) representing 66% appreciation.")
    assert r.values == []


def test_a_zone_average_is_not_this_buildings_rate():
    r = extract_rate("Zone Avg Rate \u20b930,626/sq.ft \u00b7 371 projects in Kandivali West")
    assert r.values == []


def test_an_ordinary_quoted_rate_is_still_read():
    """The guards must not cost a real figure -- with a symbol, with 'Rs', and with
    the currency a few words back."""
    assert extract_rate("Runwal Vertex. \u20b935,400 per sq.ft.").values[0].value == 35400
    assert extract_rate("Price: Rs 29,900 per sq ft").values[0].value == 29900
    assert extract_rate("The quoted rate is about 21,300 per sq ft.").values[0].value == 21300


# --- A4: ready stock, not merely earlier ----------------------------------------

def _at(name: str, when: date) -> Project:
    p = Project(id=name, name=name, status="under_construction", distance_km=0.5,
                completeness=6, label="COMPARABLE", pages_seen=["u"],
                confirmed_by=["a.com", "b.com"])
    p.possession.observe(fv(when))
    return p


def test_earlier_but_years_away_stays_in_the_table(own):
    """A 2028 building is real competition for a 2029 one. The first cut of this rule
    moved 9 of 14 Borivali rows out for exactly this reason."""
    own.possession = date(2029, 12, 1)
    p = _at("2028er", date(2028, 1, 1))
    main, early = service.split_early([p], own, today=date(2026, 9, 23))
    assert [x.id for x in main] == ["2028er"] and early == []
    assert service.hands_over_before(p, own) == 23     # still tagged with the gap


def test_earlier_and_ready_now_is_demoted(own):
    own.possession = date(2029, 12, 1)
    ready = _at("ready", date(2026, 12, 1))
    others = [_at(f"f{i}", date(2029, 1, 1)) for i in range(5)]
    main, early = service.split_early([*others, ready], own, today=date(2026, 9, 23))
    assert [x.id for x in early] == ["ready"]
    assert len(main) == 5


def test_ready_now_but_not_far_ahead_of_the_subject_stays(own):
    """A subject handing over next year is competing with ready stock."""
    own.possession = date(2027, 3, 1)
    main, early = service.split_early([_at("ready", date(2026, 12, 1))], own,
                                      today=date(2026, 9, 23))
    assert early == [] and len(main) == 1


def test_the_table_never_drops_below_five_rows(own):
    """A short table is a worse answer than a tagged one; the strongest come back."""
    own.possession = date(2029, 12, 1)
    rows = [_at(f"r{i}", date(2026, 12, 1)) for i in range(6)]
    main, early = service.split_early(rows, own, today=date(2026, 9, 23))
    assert len(main) == 5 and len(early) == 1
    assert [x.id for x in main] == ["r0", "r1", "r2", "r3", "r4"]   # strongest-first
