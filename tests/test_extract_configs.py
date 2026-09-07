"""Configurations extractor, ported from competitor-agent-demo/tests/test_extract_configs.py.

Assertions are rewritten against FieldReport; the extractor logic is the donor's.
Test the refusals, not just the hits -- every real bug here was a false positive.
"""

import pytest

from app.extract.deterministic import extract_configurations


def cfg(text):
    return extract_configurations(text).value


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2 & 3 BHK", [2, 3]),
        ("2, 3 and 4 BHK", [2, 3, 4]),
        ("2 to 4 BHK", [2, 3, 4]),
        ("3 BHK & 4 BHK residences", [3, 4]),
        ("1/2/3 BHK", [1, 2, 3]),
        ("2 BHK + study", [2]),
        ("Luxurious 2 BHK, 2 BHK and 3 BHK homes", [2, 3]),
        ("2-3 BHK", [2, 3]),
        ("1 to 3 bhk apartments", [1, 2, 3]),
        ("Choose from 2 B.H.K or 3 B.H.K", [2, 3]),
    ],
)
def test_parses_and_expands(text, expected):
    assert cfg(text) == expected


def test_range_expansion_is_inclusive():
    assert cfg("2 to 4 BHK") == [2, 3, 4]


def test_deduplicates_and_sorts():
    assert cfg("4 BHK, 2 BHK, 3 BHK, 2 BHK") == [2, 3, 4]


def test_qualifier_after_bhk_is_ignored():
    assert cfg("2 BHK + study room and 3 BHK + servant") == [2, 3]


# --- the refusals -----------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "Spacious homes in Malad West",
        "7 BHK penthouse",          # outside 1-5, so noise
        "0 BHK studio",             # outside 1-5
        "35 BHK",                   # two digits: must not become [5]
        "",
    ],
)
def test_no_configuration_is_an_absence_not_an_empty_list(text):
    r = extract_configurations(text)
    assert r.value is None
    assert r.absent == "NOT_PUBLISHED"
    assert r.label


def test_year_adjacent_to_bhk_does_not_leak_a_digit():
    """The \\b guard. Without it '2024, 3 BHK' matches '4, 3 BHK' -> [3, 4]."""
    assert cfg("Launched in 2024, 3 BHK homes") == [3]


def test_carpet_area_is_not_a_configuration():
    assert cfg("Carpet area 736 sq.ft.") is None


def test_pincode_and_phone_are_not_configurations():
    assert cfg("Malad West 400064, call 9820012345") is None


def test_out_of_range_is_dropped_not_clamped():
    """9 BHK must vanish, never become 5."""
    assert cfg("9 BHK and 3 BHK") == [3]


def test_evidence_is_verbatim_and_capped():
    fv = extract_configurations("Now launching 2 & 3 BHK residences").display()
    assert fv.evidence is not None
    assert "2 & 3 BHK" in fv.evidence
    assert len(fv.evidence) <= 200


def test_a_found_value_carries_no_absence_reason():
    r = extract_configurations("2 & 3 BHK")
    assert r.absent is None and r.label is None
    assert r.display().method == "deterministic"


# --- half-BHK must not become a phantom 5 -----------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "Floor-Plan-2.5 BHK-Jodi-Unit-970 Sqft",
        "Jodi Unit 3.5 BHK 1175 Sqft",
        "1.5 BHK available",
    ],
)
def test_decimal_bhk_does_not_leak_its_fractional_digit(text):
    """A dot is a NON-word char, so \\b matches between '.' and '5' -- the guard
    that catches 2024 does nothing here. A live run turned every '2.5 BHK' on a
    builder's floor-plan page into a 5 BHK."""
    assert extract_configurations(text).value is None


def test_a_half_bhk_next_to_whole_ones_keeps_only_the_whole_ones():
    assert cfg("2 BHK, 2.5 BHK and 3 BHK") == [2, 3]


# --- an undeclared set that spans too many sizes is refused -----------------

def test_an_undeclared_five_size_set_is_refused():
    """A live run published "1,2,3,4,5 BHK" for one building, and the match badge
    then read "all 3 of yours" -- arithmetically right on a set no page ever
    stated. The whole set comes off a "similar projects" block."""
    r = extract_configurations("1 BHK, 2 BHK, 3 BHK, 4 BHK and 5 BHK")
    assert r.value is None
    assert r.absent == "SOURCES_DISAGREE"
    assert r.span == [1, 5]


def test_a_declared_wide_set_is_kept():
    """A portal spec table stating four sizes is the project speaking about
    itself, and is not second-guessed."""
    assert extract_configurations("1, 2, 3, 4 BHK", first_party=True).value == [1, 2, 3, 4]


def test_a_normal_three_size_set_survives():
    assert cfg("Narang Vivenda offers 2, 3 and 4 BHK homes") == [2, 3, 4]


# --- cross-source disagreement is decided in conflicts, not here ------------

def test_two_sources_listing_different_bhk_subsets_are_both_kept(full_project):
    """Both portals are describing parts of the same inventory. Every sighting
    stays on file with its provenance; conflicts decides what to report."""
    from tests.conftest import fv

    full_project.configurations.observe(fv([3, 4], source="housing"))
    assert len(full_project.configurations.observations) == 2


def test_sources_that_list_different_configurations_are_flagged(full_project):
    from app.logic import conflicts
    from tests.conftest import fv

    full_project.configurations.observe(fv([3, 4], source="housing"))
    conflicts.apply(full_project)
    assert full_project.configurations.absent == "SOURCES_DISAGREE"
    assert full_project.configurations.span == [2, 4]
    assert [c.field for c in full_project.conflicts] == ["configurations"]


# --- developer --------------------------------------------------------------

from app.extract.deterministic import extract_developer  # noqa: E402


@pytest.mark.parametrize(
    "text, expected",
    [
        ("A project by Mahindra Lifespaces Developers Ltd", "Mahindra Lifespaces Developers"),
        ("Developer: Transcon Sheth Creators", "Transcon Sheth Creators"),
        ("Built by Arkade Developers Pvt Ltd", "Arkade Developers"),
        ("Marketed by RUPAREL REALTY", "RUPAREL REALTY"),
    ],
)
def test_developer_is_read_from_the_page(text, expected):
    assert extract_developer(text).value == expected


@pytest.mark.parametrize(
    "text",
    [
        "Possession from Apr 2024. Under Construction Possession by Dec, 2026.",
        "Ready To Move Homes available",
        "This is a Residential Group housing scheme",
    ],
)
def test_a_project_status_is_not_a_developer(text):
    """DEV_SUFFIX contains 'Constructions?', so 'Under Construction' parses as a
    developer name -- and it reached a live report's Developer column."""
    assert extract_developer(text).value is None


def test_a_rejected_candidate_does_not_hide_the_real_one():
    """'... is a Under Construction project by EMBASSY ENTERPRISES' offers a bad
    match before the good one; stopping at the first lost the developer."""
    text = ("Embassy Marquis Residences is a Under Construction project by "
            "EMBASSY ENTERPRISES in Malad West Mumbai")
    assert extract_developer(text).value == "EMBASSY ENTERPRISES"


def test_portal_chrome_is_stripped_from_a_developer_name():
    assert extract_developer("Squareyards Logo Metro Associates").value == "Metro Associates"


# --- amenities: word boundaries, not substrings -----------------------------

from app.extract.deterministic import extract_amenities  # noqa: E402


def _names(report):
    return [a.name for a in (report.value or [])]


@pytest.mark.parametrize(
    "text",
    ["The project offers ample open space", "Spacious homes in Malad",
     "A spacious layout with open spaces"],
)
def test_spa_does_not_match_space_or_spacious(text):
    """Substring matching put a phantom Spa on nearly every candidate in a live
    report."""
    assert "Spa" not in _names(extract_amenities(text))


def test_real_amenities_still_match():
    r = extract_amenities("Amenities: Spa, Yoga Deck, Kids Play Area, Jogging Track")
    assert set(_names(r)) == {"Spa", "Yoga Deck", "Kids Play Area", "Jogging Track"}


def test_a_page_listing_no_amenities_is_an_absence_with_a_reason():
    """'We read a page and it listed none' and 'we never found a page' are
    different facts."""
    r = extract_amenities("Spacious homes in Malad West")
    assert r.value is None and r.absent == "NOT_PUBLISHED"


def test_amenities_carry_the_category_the_ui_groups_by():
    by_name = {a.name: a.category for a in extract_amenities("Swimming Pool, Clubhouse, Gymnasium").value}
    assert by_name == {"Swimming Pool": "sport_fitness", "Gymnasium": "sport_fitness",
                       "Clubhouse": "social_leisure"}


# --- page relevance ---------------------------------------------------------

from app.extract.deterministic import mentions_project, relevant_pages  # noqa: E402


def test_a_page_about_another_project_is_not_relevant():
    """A live run excluded BOTH Chandak Treesourus and Ajmera Boulevard as rental
    on the same quote -- from a page about K Raheja Interface Heights."""
    foreign = "How many properties of 3 BHK in K Raheja Interface Heights are on rent?"
    assert mentions_project(foreign, "Chandak Treesourus") is False


def test_a_page_naming_the_project_is_relevant():
    assert mentions_project("Chandak Treesourus in Malad West", "Chandak Treesourus")


def test_relevant_pages_drops_the_foreign_ones():
    pages = ["Chandak Treesourus offers 2 and 3 BHK", "K Raheja Interface Heights flats on rent"]
    assert len(relevant_pages(pages, "Chandak Treesourus", text_of=lambda p: p)) == 1


def test_when_no_page_names_the_project_we_keep_them_all():
    """A builder's own site may spell it differently; losing every page is worse
    than admitting a few foreign ones."""
    pages = ["some page", "another"]
    assert len(relevant_pages(pages, "Totally Absent Name", text_of=lambda p: p)) == 2


def test_focus_narrows_a_page_to_its_own_project():
    from app.extract.deterministic import focus_on_project

    text = ("Narang Vivenda offers 2 and 3 BHK. " + "filler. " * 200
            + "Similar projects: Metro Excellency offers 5 BHK.")
    assert "5 BHK" not in focus_on_project(text, "Narang Vivenda")


# --- commercial must attach to the product ----------------------------------

from app.extract.deterministic import extract_building_type  # noqa: E402


@pytest.mark.parametrize(
    "text",
    ["delivering exceptional residential and commercial spaces since 1985",
     "a lively suburb known for its busy commercial and shopping scene",
     "close to the commercial hub of Andheri"],
)
def test_commercial_in_a_blurb_is_not_the_product(text):
    """Both phrasings excluded live residential projects on a live run."""
    assert extract_building_type(text).value != "commercial"


@pytest.mark.parametrize(
    "text",
    ["a premium commercial tower with office space",
     "retail shop units available", "shop cum office spaces"],
)
def test_a_genuinely_commercial_product_is_still_caught(text):
    assert extract_building_type(text).value == "commercial"


def test_one_tower_is_not_a_complex():
    """A bare "\\d towers?" once matched "The project comprises 1 towers" and
    classified a single-tower project as multi-tower. A complex needs at least
    two, so the count is spelled out rather than left to \\d."""
    assert extract_building_type("The project comprises 1 towers").value != "multi_tower"
    assert extract_building_type("The project comprises 4 towers").value == "multi_tower"


def test_a_counted_tower_beats_a_phrase_found_near_one():
    from app.extract.deterministic import extract_tower_count, reconcile_structure

    bt = extract_building_type("a gated community by the lake")
    tc = extract_tower_count("1 tower of 40 floors")
    assert reconcile_structure(bt, tc).value == "single"


def test_a_commercial_classification_survives_its_tower_count():
    """COMMERCIAL says what is being SOLD; a commercial project with three towers
    is still commercial."""
    from app.extract.deterministic import extract_tower_count, reconcile_structure

    bt = extract_building_type("a premium commercial tower with office space")
    tc = extract_tower_count("3 towers")
    assert reconcile_structure(bt, tc).value == "commercial"
