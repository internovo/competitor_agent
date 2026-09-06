from datetime import date

from app.logic import completeness, conflicts, eligibility, match_score
from app.logic.geo import haversine_km
from app.models.schema import FieldValue, Provenance, RateValue
from tests.conftest import fv


def test_haversine_one_km_north():
    assert abs(haversine_km(19.186, 72.84, 19.186 + 0.008993, 72.84) - 1.0) < 0.01


# ---- eligibility --------------------------------------------------------
def test_eligible_project_passes(full_project, own):
    assert eligibility.check(full_project, own, 1.5, today=date(2026, 9, 3)) is None
    assert full_project.distance_km == 0.5


def test_outside_radius_dropped(full_project, own):
    full_project.lat = own.lat + 0.02
    assert "outside radius" in eligibility.check(full_project, own, 1.5)


def test_possession_today_or_past_dropped(full_project, own):
    full_project.possession = [fv(date(2026, 9, 3))]
    assert "not after today" in eligibility.check(full_project, own, 1.5, today=date(2026, 9, 3))
    full_project.possession = [fv(date(2026, 9, 4))]
    assert eligibility.check(full_project, own, 1.5, today=date(2026, 9, 3)) is None


def test_ready_and_resale_dropped(full_project, own):
    for status in ("ready", "completed", "resale", "unknown"):
        full_project.status = status
        assert "status" in eligibility.check(full_project, own, 1.5)


def test_no_config_overlap_dropped(full_project, own):
    own.configurations = [4]
    assert "no configuration overlap" in eligibility.check(full_project, own, 1.5)


def test_missing_possession_is_not_a_drop_reason(full_project, own):
    full_project.possession = []
    assert eligibility.check(full_project, own, 1.5) is None


# ---- completeness -------------------------------------------------------
def test_completeness_labels(full_project):
    completeness.apply(full_project)
    assert (full_project.completeness, full_project.label, full_project.could_not_verify) == (6, "COMPARABLE", [])
    full_project.rera_phases = []
    full_project.carpet_sqft = []
    completeness.apply(full_project)
    assert (full_project.completeness, full_project.label) == (4, "PARTIAL")
    assert full_project.could_not_verify == ["carpet_sqft", "rera_phases"]
    full_project.rate_psf = []
    completeness.apply(full_project)
    assert full_project.label == "THIN"


# ---- conflicts ----------------------------------------------------------
def test_rate_conflict_detected_on_spread_and_basis(full_project):
    full_project.rate_psf.append(fv(RateValue(min_psf=29100, max_psf=29100, basis="undisclosed"), source="squareyards"))
    found = conflicts.detect(full_project)
    assert any(c.field == "rate_psf" and "2 sources disagree" in c.detail for c in found)
    assert full_project.rate_span() == (29100, 36000, "undisclosed")


def test_close_rates_are_not_a_conflict(full_project):
    full_project.rate_psf.append(fv(RateValue(min_psf=35000, max_psf=36500, basis="base"), source="squareyards"))
    assert conflicts.detect(full_project) == []


def test_possession_conflict(full_project):
    full_project.possession.append(fv(date(2030, 6, 1), source="housing"))
    assert any(c.field == "possession" for c in conflicts.detect(full_project))


# ---- match score --------------------------------------------------------
def test_match_score_only_for_comparable(full_project, own):
    completeness.apply(full_project)
    full_project.distance_km = 0.5
    score, breakdown = match_score.compute(full_project, own, 1.5)
    assert 0 <= score <= 100
    assert breakdown["config"] == 0.5  # {2,3} vs {1,2,3,4}
    assert breakdown["possession"] == 1 - 3 / 24
    assert breakdown["structure"] == 1.0
    full_project.label = "PARTIAL"
    assert match_score.compute(full_project, own, 1.5) == (None, {})


def test_rate_component_zero_when_basis_not_base(full_project, own):
    completeness.apply(full_project)
    full_project.distance_km = 0.5
    full_project.rate_psf = [fv(RateValue(min_psf=36000, max_psf=36000, basis="all_in"))]
    _, b = match_score.compute(full_project, own, 1.5)
    assert b["rate"] == 0.0


def test_identical_project_scores_100(own):
    from app.models.schema import Project
    p = Project(id="x", name="Twin", lat=own.lat, lng=own.lng, status="under_construction", distance_km=0.0,
                configurations=[fv(own.configurations)], carpet_sqft=[fv(own.carpet_sqft)], rate_psf=[fv(own.rate_psf)],
                possession=[fv(own.possession)], structure=[fv(own.structure)], rera_phases=[fv(own.rera_phases)])
    completeness.apply(p)
    assert match_score.compute(p, own, 1.5)[0] == 100
