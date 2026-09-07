"""A missing field must say why it is missing. All six reasons, and both refusals."""
from datetime import date

import pytest
from pydantic import ValidationError

from app.logic import completeness, conflicts
from app.models.schema import AbsenceReason, FieldReport, RateValue, absence_label
from tests.conftest import fv

REASONS = ("NOT_PUBLISHED", "NO_RERA_ON_FILE", "RATE_NOT_PUBLISHED", "UNPARSEABLE_DATE",
           "SOURCES_DISAGREE", "NOT_FOUND")


def test_the_six_reasons_are_the_whole_set():
    assert set(AbsenceReason.__args__) == set(REASONS)


@pytest.mark.parametrize("reason", REASONS)
def test_every_reason_produces_a_sentence_the_ui_can_print(reason):
    span = [1, 2] if reason == "SOURCES_DISAGREE" else None
    r = FieldReport(field="rate_psf", absent=reason, span=span)
    assert r.absent == reason
    assert r.label and r.label.endswith(".")
    assert r.value is None
    assert r.display() is None


def test_labels_are_generated_not_hand_written():
    """One function owns the wording, so it cannot drift between fields."""
    for reason in REASONS:
        span = [1, 2] if reason == "SOURCES_DISAGREE" else None
        assert FieldReport(field="possession", absent=reason, span=span).label == absence_label("possession", reason, span)


def test_the_label_names_the_field():
    assert "carpet area" in FieldReport(field="carpet_sqft", absent="NOT_PUBLISHED").label
    assert "possession date" in FieldReport(field="possession", absent="NOT_FOUND").label


def test_the_disagree_label_carries_the_spread():
    label = FieldReport(field="rate_psf", absent="SOURCES_DISAGREE", span=[29100, 38700]).label
    assert "29100 to 38700" in label


# --- the two refusals -------------------------------------------------------

def test_no_values_and_no_reason_is_refused():
    """There is no third state: an empty field either says why, or is a defect."""
    with pytest.raises(ValidationError):
        FieldReport()


def test_values_and_a_reason_together_are_refused():
    with pytest.raises(ValidationError):
        FieldReport(values=[fv([2, 3])], absent="NOT_PUBLISHED")


def test_disagreement_without_a_span_is_refused():
    with pytest.raises(ValidationError):
        FieldReport(field="rate_psf", absent="SOURCES_DISAGREE")


def test_mark_absent_refuses_a_spanless_disagreement():
    r = FieldReport(field="rate_psf", values=[fv(RateValue(min_psf=1, max_psf=2))])
    with pytest.raises(ValueError):
        r.mark_absent("SOURCES_DISAGREE")


# --- state transitions keep the invariant -----------------------------------

def test_observing_a_value_clears_the_absence():
    r = FieldReport(field="configurations", absent="NOT_FOUND")
    r.observe(fv([2, 3]))
    assert (r.absent, r.label, r.value) == (None, None, [2, 3])


def test_marking_absent_keeps_the_observations_as_disputed():
    """The card still shows the span and where each number came from."""
    r = FieldReport(field="rate_psf", values=[fv(RateValue(min_psf=29100, max_psf=29100))])
    r.mark_absent("SOURCES_DISAGREE", span=[29100, 38700])
    assert r.values == []
    assert len(r.disputed) == 1 and len(r.observations) == 1


# --- absence gets decided in conflicts --------------------------------------

def test_disagreeing_sources_make_the_field_absent_with_its_spread(full_project):
    full_project.rate_psf.observe(fv(RateValue(min_psf=29100, max_psf=29100, basis="undisclosed"), source="squareyards"))
    conflicts.apply(full_project)
    r = full_project.rate_psf
    assert r.absent == "SOURCES_DISAGREE"
    assert r.span == [29100, 36000]
    assert r.value is None
    assert [c.field for c in full_project.conflicts] == ["rate_psf"]   # the Conflict entry is kept as-is


def test_a_disagreeing_possession_carries_iso_dates_in_its_span(full_project):
    full_project.possession.observe(fv(date(2030, 6, 1), source="housing"))
    conflicts.apply(full_project)
    assert full_project.possession.span == ["2029-09-01", "2030-06-01"]


def test_a_thin_project_names_a_reason_on_every_empty_field(full_project):
    from app.config import COMPLETENESS_FIELDS
    from app.models.schema import Project

    p = Project(id="thin", name="Nothing Known")
    completeness.apply(p)
    assert p.label == "THIN"
    for f in COMPLETENESS_FIELDS:
        assert p.report(f).absent == "NOT_FOUND"
        assert p.report(f).label
