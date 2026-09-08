from datetime import date

from app.config import settings
from app.logic import completeness, conflicts, eligibility, match_score
from app.logic.geo import haversine_km
from app.models.schema import FieldValue, Provenance, RateValue
from tests.conftest import fr, fv


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
    full_project.possession = fr("possession", fv(date(2026, 9, 3)))
    assert "not after today" in eligibility.check(full_project, own, 1.5, today=date(2026, 9, 3))
    full_project.possession = fr("possession", fv(date(2026, 9, 4)))
    assert eligibility.check(full_project, own, 1.5, today=date(2026, 9, 3)) is None


def test_ready_and_resale_dropped(full_project, own):
    for status in ("ready", "completed", "resale"):
        full_project.status = status
        assert "status" in eligibility.check(full_project, own, 1.5)


def test_an_unread_lifecycle_is_kept_rather_than_dropped(full_project, own):
    """A status nobody published is an absence, not a disqualification. It stays on
    the list as UNVERIFIED so a rep can confirm it, and never scores."""
    full_project.status = "unknown"
    assert eligibility.check(full_project, own, 1.5) is None
    completeness.apply(full_project)
    assert full_project.label == "UNVERIFIED"
    assert match_score.compute(full_project, own, 1.5)[0] is None


def test_no_config_overlap_dropped(full_project, own):
    own.configurations = [4]
    assert "no configuration overlap" in eligibility.check(full_project, own, 1.5)


def test_missing_possession_is_not_a_drop_reason(full_project, own):
    full_project.possession = fr("possession")
    assert eligibility.check(full_project, own, 1.5) is None


# ---- completeness -------------------------------------------------------
def test_completeness_labels(full_project):
    completeness.apply(full_project)
    assert (full_project.completeness, full_project.label, full_project.could_not_verify) == (6, "COMPARABLE", [])
    full_project.rera_phases = fr("rera_phases")
    full_project.carpet_sqft = fr("carpet_sqft")
    completeness.apply(full_project)
    assert (full_project.completeness, full_project.label) == (4, "PARTIAL")
    assert full_project.could_not_verify == ["carpet_sqft", "rera_phases"]
    full_project.rate_psf = fr("rate_psf")
    completeness.apply(full_project)
    assert full_project.label == "THIN"


# ---- conflicts ----------------------------------------------------------
def test_rate_conflict_detected_on_spread_and_basis(full_project):
    full_project.rate_psf.observe(fv(RateValue(min_psf=29100, max_psf=29100, basis="undisclosed"), source="squareyards"))
    found = conflicts.detect(full_project)
    assert any(c.field == "rate_psf" and "2 sources disagree" in c.detail for c in found)
    assert full_project.rate_span() == (29100, 36000, "undisclosed")


def test_close_rates_are_not_a_conflict(full_project):
    full_project.rate_psf.observe(fv(RateValue(min_psf=35000, max_psf=36500, basis="base"), source="squareyards"))
    assert conflicts.detect(full_project) == []


def test_possession_conflict(full_project):
    full_project.possession.observe(fv(date(2030, 6, 1), source="housing"))
    assert any(c.field == "possession" for c in conflicts.detect(full_project))


# ---- match score --------------------------------------------------------
def test_match_score_only_for_comparable(full_project, own):
    completeness.apply(full_project)
    full_project.distance_km = 0.5
    score, breakdown, score_max, excluded = match_score.compute(full_project, own, 1.5)
    assert 0 <= score <= 100
    assert breakdown["config"] == 0.5  # {2,3} vs {1,2,3,4}
    assert breakdown["possession"] == 1 - 3 / 24
    assert breakdown["structure"] == 1.0
    assert (score_max, excluded) == (100, [])
    full_project.label = "PARTIAL"
    assert match_score.compute(full_project, own, 1.5) == (None, {}, None, [])


def test_an_unpriceable_pair_shrinks_the_denominator_rather_than_scoring_zero(full_project, own):
    """20 of 100 points ride on the rate, and they can only be awarded when both sides
    quote a base rate. Scoring 0 there reads as "bad match"; excluding it reads as
    "cannot be priced against yours", which is what actually happened."""
    completeness.apply(full_project)
    full_project.distance_km = 0.5
    full_project.rate_psf = fr("rate_psf", fv(RateValue(min_psf=36000, max_psf=36000, basis="all_in")))
    _, b, score_max, excluded = match_score.compute(full_project, own, 1.5)
    assert "rate" not in b
    assert excluded == ["rate"]
    assert score_max == 100 - settings.w_rate == 80


def test_identical_project_scores_100(own):
    from app.models.schema import Project
    p = Project(id="x", name="Twin", lat=own.lat, lng=own.lng, status="under_construction", distance_km=0.0,
                configurations=[fv(own.configurations)], carpet_sqft=[fv(own.carpet_sqft)], rate_psf=[fv(own.rate_psf)],
                possession=[fv(own.possession)], structure=[fv(own.structure)], rera_phases=[fv(own.rera_phases)])
    completeness.apply(p)
    assert match_score.compute(p, own, 1.5)[0] == 100


# ---- inverted ranges: sort a slip, refuse a disagreement -----------------

def test_a_narrow_inversion_is_a_min_max_slip_and_is_sorted():
    """AJMERA BOULEVARD shipped 32,015-31,884 to the payload. 0.4% apart is one number
    written into the wrong slot, not two sources."""
    assert (RateValue(min_psf=32015, max_psf=31884).min_psf,
            RateValue(min_psf=32015, max_psf=31884).max_psf) == (31884, 32015)


def test_a_wide_inversion_is_two_numbers_and_is_refused(full_project, own):
    """Sorting these would be guessing which end is the floor."""
    full_project.rate_psf = fr("rate_psf", fv(RateValue(min_psf=60000, max_psf=30000, basis="base")))
    conflicts.apply(full_project)
    assert full_project.rate_psf.absent == "SOURCES_DISAGREE"
    assert full_project.rate_psf.span == [30000, 60000]
    assert full_project.rate_psf.value is None


# ---- unresolved caps the label ------------------------------------------

def test_one_unresolved_dimension_still_scores_with_its_denominator_shown(full_project, own):
    """The score reports what it could evaluate, so one gap over-promises nothing.
    Capping here made COMPARABLE rarer the better the research got."""
    full_project.configurations.observe(fv([1]))          # now {2,3} vs {1}: sources disagree
    conflicts.apply(full_project)
    completeness.apply(full_project)
    assert full_project.completeness == 6
    assert full_project.unresolved == ["configurations"]
    assert full_project.label == "COMPARABLE"
    score, _, score_max, excluded = match_score.compute(full_project, own, 1.5)
    assert score is not None and excluded == ["config"] and score_max == 100 - settings.w_config


def test_two_unresolved_dimensions_are_not_comparable(full_project, own):
    full_project.configurations.observe(fv([1]))
    full_project.possession.observe(fv(date(2032, 1, 1)))   # 28 months apart: sources disagree
    conflicts.apply(full_project)
    completeness.apply(full_project)
    assert sorted(full_project.unresolved) == ["configurations", "possession"]
    assert full_project.label == "PARTIAL"
    assert match_score.compute(full_project, own, 1.5)[0] is None


# ---- the plausibility band: refuse, never correct ------------------------

def _rate_at(full_project, psf):
    full_project.rate_psf = fr("rate_psf", fv(RateValue(min_psf=psf, max_psf=psf, basis="base")))
    return full_project


ANCHOR = 38000.0   # Marina64's own base-rate midpoint


def test_a_rate_a_twelfth_of_the_market_is_refused(full_project):
    """Shreeji Atlantis quoted Rs 3,000/sqft in a Rs 38,000 market."""
    conflicts.apply(_rate_at(full_project, 3000), ANCHOR)
    assert full_project.rate_psf.absent == "IMPLAUSIBLE_RATE"
    assert full_project.rate_psf.span == [3000, 3000]
    assert "3,000" in full_project.rate_psf.label and "not used" in full_project.rate_psf.label


def test_half_a_rate_is_not_a_rate(full_project):
    """Raghav UTOPIA quoted 295-26,412. The top is credible, the bottom is 0.008x."""
    full_project.rate_psf = fr("rate_psf", fv(RateValue(min_psf=295, max_psf=26412, basis="base")))
    conflicts.apply(full_project, ANCHOR)
    assert full_project.rate_psf.absent == "IMPLAUSIBLE_RATE"


def test_a_premium_project_at_1_8x_is_left_alone(full_project):
    """Whispering Heights at Rs 68,157. Refusing this would be us deciding what the
    market is, which is exactly what the band must not do."""
    conflicts.apply(_rate_at(full_project, 68157), ANCHOR)
    assert full_project.rate_psf.absent is None
    assert full_project.rate_psf.value.min_psf == 68157


def test_the_top_of_the_band_passes_untouched(full_project):
    conflicts.apply(_rate_at(full_project, int(ANCHOR * 3.9)), ANCHOR)
    assert full_project.rate_psf.absent is None


def test_without_an_anchor_no_judgement_is_made(full_project):
    """No anchor, no opinion: a rate is never refused on a hunch."""
    conflicts.apply(_rate_at(full_project, 3000), None)
    assert full_project.rate_psf.absent is None
    assert full_project.rate_psf.value.min_psf == 3000


def test_the_anchor_prefers_the_subjects_own_base_rate(own, full_project):
    assert conflicts.anchor_for(own, [full_project]) == 38000.0


def test_the_anchor_falls_back_to_the_median_of_the_set(own, full_project):
    own.rate_psf.basis = "all_in"
    full_project.eligible = True
    assert conflicts.anchor_for(own, [full_project]) == 36000.0
    assert conflicts.anchor_for(own, []) is None


# ---- one rate on many projects is a locality average --------------------

def _at(name, psf):
    from app.models.schema import Project
    return Project(id=name, name=name, status="under_construction",
                   rate_psf=[fv(RateValue(min_psf=psf, max_psf=psf, basis="undisclosed"))])


def test_a_rate_quoted_for_five_projects_belongs_to_none_of_them():
    """Simplex KhushAangan, Triumph Tower, Narang Vivenda, Kamla Jainson and Malwadi
    Satyam all came back at exactly 22,130. In band, specific, and false."""
    ps = [_at(f"p{i}", 22130) for i in range(5)]
    shared = conflicts.shared_rates(ps)
    assert shared == {(22130, 22130): 5}
    conflicts.apply(ps[0], 38000.0, shared)
    assert ps[0].rate_psf.absent == "SHARED_ACROSS_PROJECTS"
    assert ps[0].rate_psf.span == [22130, 5]
    assert "22,130" in ps[0].rate_psf.label and "5 projects" in ps[0].rate_psf.label
    assert ps[0].rate_psf.value is None


def test_two_projects_at_the_same_rate_is_ordinary_coincidence():
    ps = [_at("a", 31000), _at("b", 31000)]
    assert conflicts.shared_rates(ps) == {}
    conflicts.apply(ps[0], 38000.0, conflicts.shared_rates(ps))
    assert ps[0].rate_psf.absent is None


def test_an_implausible_shared_rate_is_refused_as_implausible_first():
    ps = [_at(f"p{i}", 1180) for i in range(4)]
    conflicts.apply(ps[0], 38000.0, conflicts.shared_rates(ps))
    assert ps[0].rate_psf.absent == "IMPLAUSIBLE_RATE"


def test_a_withdrawn_rate_still_renders_on_the_card_with_its_reason():
    """A rate we refused is shown with the reason, never as a value and never as a crash."""
    from app import service

    ps = [_at(f"p{i}", 22130) for i in range(4)]
    conflicts.apply(ps[0], 38000.0, conflicts.shared_rates(ps))
    block = service._rate_block(ps[0], None)
    assert block["min"] == 22130 and block["conflict"] and "4 projects" in block["conflict"]
    assert service.card(ps[0], 1)["rate_psf"]["conflict"]
