"""A missing field must say why it is missing. Every reason, and both refusals."""
from datetime import date

import pytest
from pydantic import ValidationError

from app.logic import completeness, conflicts
from app.models.schema import AbsenceReason, FieldReport, RateValue, absence_label
from tests.conftest import fv

REASONS = ("NOT_PUBLISHED", "NO_RERA_ON_FILE", "RATE_NOT_PUBLISHED", "UNPARSEABLE_DATE",
           "SOURCES_DISAGREE", "NOT_FOUND", "NOT_RESEARCHED", "RESEARCH_TIMED_OUT", "IMPLAUSIBLE_RATE",
           "SHARED_ACROSS_PROJECTS")


def test_the_listed_reasons_are_the_whole_set():
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

    p = Project(id="thin", name="Nothing Known", status="under_construction")
    completeness.apply(p)
    assert p.label == "THIN"
    for f in COMPLETENESS_FIELDS:
        assert p.report(f).absent == "NOT_FOUND"
        assert p.report(f).label


# --- the reason reflects what the sources actually said ---------------------

def test_a_page_that_quotes_no_rate_says_so_rather_than_not_found(own):
    """Three different facts used to collapse into one 'Not on file'."""
    from tests.test_deterministic_first import run_extract

    project, _ = run_extract("Ajmera Skyline, Malad West. 2 and 3 BHK homes. Book a site visit.",
                             own, llm=None, name="Ajmera Skyline")
    assert project.rate_psf.absent == "RATE_NOT_PUBLISHED"
    assert project.carpet_sqft.absent == "NO_RERA_ON_FILE"
    assert project.possession.absent == "UNPARSEABLE_DATE"
    assert project.rera_phases.absent == "NO_RERA_ON_FILE"


def test_a_project_no_source_ever_produced_a_page_for_stays_not_found(own):
    from tests.test_deterministic_first import run_extract

    class Nothing:
        name = "squareyards"

        async def discover(self, ctx):
            return []

        async def pages_for(self, project, ctx):
            return []

    from app.graph.nodes import pipeline
    from app.graph.state import ExtractInput
    from app.models.schema import Project
    import asyncio

    project = Project(id="ghost", name="Ghost Tower")
    out = asyncio.run(pipeline.extract(
        ExtractInput(own=own, radius_km=1.5, project=project, retry=False, extra_queries=[]),
        {"configurable": {"sources": [Nothing()], "fetcher": None, "llm": None}}))
    assert all(out["projects"][0].report(f).absent == "NOT_FOUND"
               for f in ("configurations", "rate_psf", "possession"))


def test_a_page_whose_configurations_cannot_be_trusted_keeps_its_spread(own):
    from tests.test_deterministic_first import run_extract

    project, _ = run_extract("Metro Excellency offers 1 BHK, 2 BHK, 3 BHK, 4 BHK and 5 BHK homes.",
                             own, llm=None, name="Metro Excellency")
    assert project.configurations.absent == "SOURCES_DISAGREE"
    assert project.configurations.span == [1, 5]


def test_the_label_reads_as_a_sentence_a_rep_could_be_shown():
    from app.models.schema import FieldReport

    assert FieldReport(field="amenities", absent="NOT_FOUND").label == \
        "No source produced an amenity list for this project."
    assert FieldReport(field="configurations", absent="NOT_FOUND").label == \
        "No source produced a configuration mix for this project."
