"""Invariants, ported from competitor-agent-demo/tests/test_invariants.py.

These are not unit tests of individual functions. They are assertions about the
output of the WHOLE graph, run offline against fixtures, which is why they catch
the failures that matter.
"""
import asyncio
import json
from datetime import date

import pytest

from app import service
from app.config import COMPLETENESS_FIELDS, FIXTURE_DIR
from app.logic.geo import haversine_km
from app.models.schema import REPORTED_FIELDS, AbsenceReason, OwnProject

RADIUS_KM = 1.5
REASONS = set(AbsenceReason.__args__)
BASES = {"base", "all_in", "undisclosed"}


@pytest.fixture(scope="module")
def scanned():
    own = OwnProject(**json.loads((FIXTURE_DIR / "marina64.json").read_text()))
    rec, meta = asyncio.run(service.run_scan(own, RADIUS_KM, "fixture", db=None, llm=None))
    return own, rec, meta


def all_reports(rec):
    for p in rec.projects:
        for f in REPORTED_FIELDS:
            yield p, f, p.report(f)


# --- 1: the radius, recomputed on the OUTPUT --------------------------------

def test_no_eligible_project_is_beyond_the_radius(scanned):
    """Recomputed rather than trusted. Checking the pipeline's own distance_km
    against itself would pass for any self-consistent bug."""
    own, rec, _ = scanned
    for p in rec.projects:
        if not p.eligible:
            continue
        assert haversine_km(own.lat, own.lng, p.lat, p.lng) <= RADIUS_KM, p.name


def test_the_pipelines_own_distance_matches_a_fresh_computation(scanned):
    own, rec, _ = scanned
    for p in rec.projects:
        if p.lat is None:
            continue
        assert abs(p.distance_km - haversine_km(own.lat, own.lng, p.lat, p.lng)) < 0.01


# --- 2: forward-looking only ------------------------------------------------

def test_no_eligible_project_has_a_past_possession_date(scanned):
    _, rec, _ = scanned
    for p in rec.projects:
        if p.eligible and p.value("possession") is not None:
            assert p.value("possession") > date.today(), p.name


def test_every_eligible_project_is_forward_looking(scanned):
    _, rec, _ = scanned
    for p in rec.projects:
        if p.eligible:
            assert p.status in {"under_construction", "new_launch"}, f"{p.name}: {p.status}"


# --- 3: no unknown without a reason -----------------------------------------

def test_every_empty_field_carries_an_absence_reason(scanned):
    """The whole point of Task 1. There is no third state."""
    _, rec, _ = scanned
    for p, f, r in all_reports(rec):
        if not r.values:
            assert r.absent is not None, f"{p.name}.{f}"
            assert r.label, f"{p.name}.{f} has a reason with no sentence"


def test_no_field_uses_an_empty_value_to_mean_unknown(scanned):
    _, rec, _ = scanned
    for p, f, r in all_reports(rec):
        for fv in r.observations:
            assert fv.value != "" and fv.value != [], f"{p.name}.{f}"


def test_every_absence_reason_is_in_the_closed_set(scanned):
    _, rec, _ = scanned
    for p, f, r in all_reports(rec):
        if r.absent is not None:
            assert r.absent in REASONS, f"{p.name}.{f}"


def test_a_disagreement_always_carries_its_spread(scanned):
    _, rec, _ = scanned
    for p, f, r in all_reports(rec):
        if r.absent == "SOURCES_DISAGREE":
            assert r.span and len(r.span) == 2, f"{p.name}.{f}"
            assert r.disputed, f"{p.name}.{f} lost the observations it disagreed across"


def test_every_value_carries_its_provenance(scanned):
    _, rec, _ = scanned
    for p, f, r in all_reports(rec):
        for fv in r.observations:
            assert fv.prov.source and fv.prov.fetched_at, f"{p.name}.{f}"
            assert fv.method in {"deterministic", "llm", "manual"}, f"{p.name}.{f}"


# --- 4: the basis is a closed set -------------------------------------------

def test_price_basis_is_always_in_the_closed_set(scanned):
    _, rec, _ = scanned
    for p in rec.projects:
        for fv in p.rate_psf.observations:
            assert fv.value.basis in BASES, p.name


# --- 5: a basis is never set without a matching phrase ----------------------

def test_a_deterministic_basis_never_appears_without_the_phrase_it_was_read_from():
    """No phrase, no basis. Asserted directly rather than through a fixture that
    might drift."""
    from app.extract.deterministic import extract_price_basis

    page = ("Ekta Tripolis, Goregaon-Malad link road. 2, 3 and 4 BHK apartments. "
            "Rs 33,900 per sq.ft. Carpet area 745 to 1480 sq ft. 5 towers.")
    r = extract_price_basis(page)
    assert r.value == "undisclosed"
    assert r.rule_matched == 4


def test_every_stated_basis_on_a_fixture_page_carries_its_evidence():
    from app.extract.deterministic import extract_price_basis

    for path in sorted((FIXTURE_DIR / "pages").glob("*.txt")):
        r = extract_price_basis(path.read_text(encoding="utf-8"))
        if r.value in ("base", "all_in"):
            assert r.display().evidence, f"{path.name} claims {r.value} with no evidence"
            assert r.rule_matched in (1, 2, 3), path.name


# --- 6: ordering ------------------------------------------------------------

TIER_RANK = {"COMPARABLE": 0, "PARTIAL": 1, "THIN": 2}


def test_tier_ordering_holds(scanned):
    """No THIN above any COMPARABLE."""
    own, rec, _ = scanned
    ranks = [TIER_RANK[p.label] for p in service.rank(rec.projects)]
    assert ranks == sorted(ranks)


# --- 7: a score only where there is enough to score -------------------------

def test_a_match_score_exists_exactly_for_comparable_projects(scanned):
    _, rec, _ = scanned
    for p in rec.projects:
        if not p.eligible:
            continue
        assert (p.match_score is not None) == (p.label == "COMPARABLE"), p.name


def test_the_tier_matches_its_own_count(scanned):
    _, rec, _ = scanned
    for p in rec.projects:
        known = sum(1 for f in COMPLETENESS_FIELDS if p.report(f).observations)
        expected = "COMPARABLE" if known >= 6 else "PARTIAL" if known >= 4 else "THIN"
        assert p.completeness == known and p.label == expected, f"{p.name}: {known} known, tier {p.label}"


def test_could_not_verify_lists_exactly_the_fields_never_seen(scanned):
    _, rec, _ = scanned
    for p in rec.projects:
        assert p.could_not_verify == [f for f in COMPLETENESS_FIELDS if not p.report(f).observations]


# --- 8: the same fixtures produce the same answer ---------------------------

def test_the_same_fixtures_produce_the_same_projects(scanned):
    own, rec, _ = scanned
    again, _ = asyncio.run(service.run_scan(own, RADIUS_KM, "fixture", db=None, llm=None))
    assert [(p.id, p.completeness, p.match_score) for p in service.rank(rec.projects)] == \
           [(p.id, p.completeness, p.match_score) for p in service.rank(again.projects)]


def test_a_smaller_radius_returns_fewer_projects(scanned):
    own, rec, _ = scanned
    tight, _ = asyncio.run(service.run_scan(own, 0.5, "fixture", db=None, llm=None))
    assert len(service.rank(tight.projects)) <= len(service.rank(rec.projects))


# --- every drop is a claim, and a claim needs its reason --------------------

def test_every_dropped_project_names_a_reason(scanned):
    own, rec, meta = scanned
    payload = service.list_payload(rec, own, meta)
    assert payload["dropped"]
    for d in payload["dropped"]:
        assert d["reason"], d["name"]


def test_nothing_is_dropped_merely_for_being_unknown(scanned):
    """The asymmetry: a project we know nothing about is THIN, and it stays in the
    output where a human can see it."""
    own, rec, meta = scanned
    for d in service.list_payload(rec, own, meta)["dropped"]:
        assert "unknown" not in d["reason"] or "status is unknown" in d["reason"]


# --- Claude never decides eligibility, never invents a number ---------------

def test_a_scan_with_no_llm_still_produces_the_whole_payload(scanned):
    own, rec, meta = scanned
    payload = service.list_payload(rec, own, meta)
    assert payload["counts"]["eligible"] > 0
    assert all(c["insight"] for c in payload["competitors"])
    assert all(c["insight_source"] == "template" for c in payload["competitors"])


def test_every_thin_project_names_a_reason_on_every_empty_field(scanned):
    own, rec, meta = scanned
    payload = service.list_payload(rec, own, meta)
    thin = [c for c in payload["competitors"] if c["label"] == "THIN"]
    assert thin, "the fixture set is supposed to contain a THIN project"
    for card in thin:
        for f in COMPLETENESS_FIELDS:
            if card["absent"].get(f):
                assert card["absent"][f]["reason"] in REASONS
                assert card["absent"][f]["label"]
