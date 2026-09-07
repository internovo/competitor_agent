"""Pricing basis extractor, ported from competitor-agent-demo/tests/test_extract_basis.py.

Four rules in priority order, and one rule that must never be broken: basis is
never inferred from the number. The donor's BASE / ALL_INCLUSIVE / NOT_DISCLOSED
are this repo's RateBasis values base / all_in / undisclosed.
"""

import pytest

from app.extract.deterministic import extract_price_basis


def basis(text, **kw):
    return extract_price_basis(text, **kw).value


# --- rule 1: declared on a first-party record -------------------------------

def test_first_party_declaration_wins_verbatim():
    r = extract_price_basis("Base price Rs 31,500 per sq.ft.", declared_basis="all_in", first_party=True)
    assert r.value == "all_in"          # not base, despite the page text
    assert r.display().confidence == "high"
    assert r.rule_matched == 1


def test_declared_basis_is_ignored_when_not_first_party():
    assert extract_price_basis("Base price Rs 31,500 psf", declared_basis="all_in").value == "base"


# --- rule 2: explicit all-inclusive phrase ----------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "All-inclusive price of Rs 42,000 per sq.ft.",
        "All inclusive price Rs 42,000 psf",
        "Rs 38,000 psf, inclusive of GST and stamp duty",
        "Rs 38,000 psf inclusive of all charges",
        "Priced at Rs 40,000/sq.ft, inclusive of stamp duty",
    ],
)
def test_all_inclusive_phrases(text):
    assert basis(text) == "all_in"


def test_no_hidden_charges_counts_when_adjacent_to_a_rate():
    assert basis("Rs 30,000 per sq ft. No hidden charges.") == "all_in"


def test_no_hidden_charges_alone_is_not_a_basis():
    """Marketing copy about brokerage says nothing about what a rate contains."""
    assert basis("No hidden charges on brokerage. Talk to our team today.") == "undisclosed"


# --- rule 3: explicit base-rate phrase --------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "Base price Rs 31,500 per sq.ft.",
        "Base rate 31,500",
        "Basic rate 29,000/sq.ft.",
        "Rs 33,000 per sq.ft. + GST",
        "Rs 33,000 per sq.ft. excluding GST",
        "Rs 33,000 per sq.ft. excl. GST",
        "Rs 27,000 psf plus applicable taxes",
        "Rs 27,000 psf plus charges",
    ],
)
def test_base_phrases(text):
    assert basis(text) == "base"


def test_starting_price_with_a_footnote_naming_extra_charges_is_base():
    assert basis("Starting price Rs 2.1 Cr*\n*GST and registration charges extra") == "base"


def test_starting_price_without_a_footnote_is_not_disclosed():
    """'Starting from' alone says nothing about what is included."""
    assert basis("Starting from Rs 2.1 Cr") == "undisclosed"


def test_starting_price_with_an_unrelated_footnote_is_not_disclosed():
    assert basis("Starting price Rs 2.1 Cr*\n*Images are artist impressions") == "undisclosed"


# --- rule 4, and the rule that must never be broken -------------------------

def test_rate_without_basis_language_is_not_disclosed():
    """THE load-bearing test. A page with a rate and no basis phrase."""
    text = ("Runwal Vertex, Malad West. Rs 35,400 per sq.ft. "
            "3 towers of 42 floors. Possession Dec 2028.")
    r = extract_price_basis(text)
    assert r.value == "undisclosed"
    assert r.rule_matched == 4
    assert r.display().evidence is None      # there was no phrase to quote


@pytest.mark.parametrize("rate", ["Rs 9,500 per sq.ft.", "Rs 95,000 per sq.ft."])
def test_basis_is_never_inferred_from_the_magnitude_of_the_number(rate):
    """A low rate is not evidence of a base rate. A high one is not evidence of
    an all-inclusive one. The number is never consulted at all."""
    assert basis(rate) == "undisclosed"


def test_empty_text_is_not_disclosed():
    assert basis("") == "undisclosed"


def test_not_disclosed_is_a_value_not_an_absence():
    r = extract_price_basis("Rs 35,400 per sq.ft.")
    assert r.value == "undisclosed"
    assert r.absent is None


def test_currency_symbols_and_emi_are_not_basis_language():
    assert basis("EMI starts at Rs 45,000/month. Rs 31,000 psf.") == "undisclosed"


def test_discount_language_is_not_basis_language():
    assert basis("Festive offer: flat 5% off on Rs 31,000 per sq.ft.") == "undisclosed"


# --- ordering ---------------------------------------------------------------

def test_all_inclusive_beats_base_when_both_appear():
    """Rule 2 precedes rule 3, deliberately."""
    assert basis("All-inclusive price. Base rate applies to lower floors.") == "all_in"


def test_rule_matched_is_recorded_for_the_trace():
    assert extract_price_basis("All-inclusive price").rule_matched == 2
    assert extract_price_basis("Base price Rs 1").rule_matched == 3
    assert extract_price_basis("nothing here").rule_matched == 4


def test_evidence_is_the_matched_phrase_verbatim():
    fv = extract_price_basis("Offered at Rs 33,000 per sq.ft. excluding GST, book now").display()
    assert fv.evidence is not None
    assert "excluding GST" in fv.evidence


def test_basis_value_is_always_in_the_closed_set():
    for text in ["", "Rs 1", "all-inclusive", "base price", "no hidden charges"]:
        assert extract_price_basis(text).value in {"base", "all_in", "undisclosed"}


# --- the evidence must belong to the value we report ------------------------

def test_every_reported_basis_carries_its_own_phrase():
    """A live run shipped ALL_INCLUSIVE with no evidence because a merge elected a
    value the quoted cell did not hold. Here the phrase travels ON the
    observation, so a reported basis cannot be separated from what it was read
    from."""
    for text in ("Base price Rs 1", "All-inclusive price of Rs 42,000 psf",
                 "Rs 38,000 psf, inclusive of GST and stamp duty", "Basic rate 29,000/sq.ft."):
        r = extract_price_basis(text)
        assert r.value in ("base", "all_in")
        assert r.display().evidence
        assert r.rule_matched in (1, 2, 3)


def test_a_stated_basis_outranks_undisclosed_across_pages():
    """undisclosed is a VALUE, so a first page saying nothing must not bury a
    later page that states the basis."""
    from app.extract.deterministic import best_basis

    pages = [extract_price_basis("Rs 30,000 per sq.ft."),
             extract_price_basis("Base price Rs 31,500 per sq.ft.")]
    assert best_basis(pages).value == "base"


def test_best_basis_of_nothing_but_silence_stays_undisclosed():
    from app.extract.deterministic import best_basis

    pages = [extract_price_basis("Rs 30,000 psf"), extract_price_basis("nothing")]
    assert best_basis(pages).value == "undisclosed"
