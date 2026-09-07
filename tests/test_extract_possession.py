"""Possession extractor, ported from competitor-agent-demo/tests/test_extract_possession.py.

Two refusals carry the weight: a relative duration is never a date, and a date
outside a possession context is never a possession date.
"""

from datetime import date

import pytest

from app.extract.deterministic import extract_possession


def poss(text, **kw):
    v = extract_possession(text, **kw).value
    return v.isoformat() if v else None


# --- month-year -------------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        ("Possession: Dec 2028", "2028-12-01"),
        ("Possession by December 2028", "2028-12-01"),
        ("Possession Jan 2030", "2030-01-01"),
        ("Completion: September 2027", "2027-09-01"),
        ("Handover March 2031", "2031-03-01"),
        ("Possession Dec. 2028", "2028-12-01"),
    ],
)
def test_month_year(text, expected):
    assert poss(text) == expected


def test_numeric_month_year():
    assert poss("Completion 12/2028") == "2028-12-01"
    assert poss("Possession 3/2029") == "2029-03-01"


# --- quarters ---------------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        ("Handover Q4 2028", "2028-10-01"),
        ("Possession Q1 2029", "2029-01-01"),
        ("Possession Q2 2029", "2029-04-01"),
        ("Possession Q3 2029", "2029-07-01"),
    ],
)
def test_quarter_maps_to_first_month(text, expected):
    assert poss(text) == expected


def test_quarter_is_medium_confidence():
    assert extract_possession("Handover Q4 2028").display().confidence == "medium"


# --- year only --------------------------------------------------------------

def test_bare_year_uses_the_january_convention():
    assert poss("Possession by 2029") == "2029-01-01"


def test_bare_year_is_low_confidence():
    """low is what stops anything downstream speaking this aloud as a month."""
    assert extract_possession("Possession by 2029").display().confidence == "low"


# --- the refusals -----------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "Possession 36 months from launch",
        "Possession in 42 months from booking",
        "Possession on completion",
        "Possession soon",
        "Possession coming soon",
    ],
)
def test_unparseable_phrases_return_an_absence_with_a_reason(text):
    r = extract_possession(text)
    assert r.value is None
    assert r.absent == "UNPARSEABLE_DATE"
    assert r.label


def test_a_relative_duration_never_absorbs_a_nearby_date():
    """The window guard. Date-first scanning returns 2024-12-01 here -- the LAUNCH
    date wearing the possession date's clothes."""
    r = extract_possession("Possession within 36 months from Dec 2024")
    assert r.value is None
    assert r.absent == "UNPARSEABLE_DATE"


def test_a_date_outside_a_possession_context_is_not_a_possession_date():
    text = "RERA registered Dec 2021. Prices valid till Mar 2026. A great address."
    assert extract_possession(text).absent == "UNPARSEABLE_DATE"


def test_under_construction_with_no_year_is_not_a_date():
    assert extract_possession("Possession status: under construction").absent == "UNPARSEABLE_DATE"


def test_no_month_is_invented_beyond_the_january_convention():
    """Nothing may turn 'sometime in 2029' into June."""
    assert poss("Possession by 2029") == "2029-01-01"


def test_every_parsed_date_is_the_first_of_the_month():
    for text in ["Possession Dec 2028", "Handover Q3 2029", "Possession by 2030"]:
        assert extract_possession(text).value.day == 1


def test_empty_text():
    assert extract_possession("").absent == "UNPARSEABLE_DATE"


# --- windowing --------------------------------------------------------------

def test_a_refusing_window_does_not_poison_a_later_good_window():
    text = ("FAQ: construction typically takes 36 months from launch. "
            + "Filler text. " * 30
            + "Possession: Dec 2028 as per the RERA filing.")
    assert poss(text) == "2028-12-01"


def test_real_page_shape():
    text = ("Runwal Vertex, Chincholi Bunder Road, Malad West. 2 & 3 BHK. "
            "Rs 35,400 per sq.ft. Possession Dec 2028. RERA P51800012345.")
    r = extract_possession(text)
    assert r.value == date(2028, 12, 1)
    assert "Dec 2028" in r.display().evidence


# --- past dates are recorded, not suppressed --------------------------------

def test_a_past_date_is_still_extracted():
    """The extractor records what the page said. Eligibility decides what it
    means -- a past possession is the filter node's job, not this function's."""
    r = extract_possession("Possession Dec 2020")
    assert r.value == date(2020, 12, 1)
    assert r.absent is None


def test_confidence_is_higher_on_a_first_party_page():
    assert extract_possession("Possession Dec 2028", first_party=True).display().confidence == "high"
    assert extract_possession("Possession Dec 2028").display().confidence == "medium"


def test_implausible_years_are_noise():
    assert extract_possession("Possession 1998").value is None


# --- date forms that portals actually emit ----------------------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        ("Completion in Dec, 2026", "2026-12-01"),
        ("Possession by Dec, 2026", "2026-12-01"),
        ("Possession December 31, 2025", "2025-12-01"),
        ("Possessions | December 01, 2029", "2029-12-01"),
        ("The expected possession date is 01 December 2029 as per RERA", "2029-12-01"),
        ("Possession 1st March 2028", "2028-03-01"),
    ],
)
def test_month_with_comma_or_day_keeps_its_month(text, expected):
    """These all fell through to the bare-year rule and came back as JANUARY --
    the extractor inventing a month it had actually been told. Measured on 54 real
    projects, fixing this took possession coverage from 17 to 38."""
    assert poss(text) == expected


def test_a_stated_month_is_never_downgraded_to_january():
    for text in ("Completion in Dec, 2026", "Possession December 31, 2025"):
        assert not poss(text).endswith("-01-01")


# --- MM-YYYY with a non-slash separator -------------------------------------

def test_hyphenated_month_year_is_a_month_not_a_bare_year():
    """Squareyards writes "Rera Possession Date 12-2026". That used to fall
    through to the bare-year branch and become 2026-01-01 -- eleven months early,
    and enough to delete a live project from the report."""
    r = extract_possession("Construction Status\nUnder Construction\n"
                           "Rera Possession Date\n12-2026\nInvestment Rating")
    assert r.value == date(2026, 12, 1)
    assert r.display().confidence == "medium"


def test_dot_and_slash_separators_agree():
    assert poss("Possession by 06.2030") == "2030-06-01"
    assert poss("Possession 6/2030") == "2030-06-01"
    assert poss("Possession 6-2030") == "2030-06-01"


def test_a_full_dmy_date_still_reads_its_month():
    assert poss("Possession date 31-12-2028") == "2028-12-01"


def test_a_year_range_is_still_only_a_year():
    """"2024-2026" must not bind "2024" as a month. \\b forbids starting mid-token,
    so it stays the low-confidence bare-year read."""
    r = extract_possession("Possession 2024-2026 phase window")
    assert r.value == date(2024, 1, 1)
    assert r.display().confidence == "low"


# --- launch date is windowed the same way -----------------------------------

def test_a_possession_date_is_not_read_as_a_launch_date():
    """Matching a bare "launch" reported a project launched Dec 2031 with
    possession Dec 2027."""
    from app.extract.deterministic import extract_launch_date

    assert extract_launch_date("New Launch. Possession Dec 2031.").value is None
    assert extract_launch_date("Launch date Aug 2024").value == date(2024, 8, 1)
