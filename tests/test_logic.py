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
    full_project.possession.observe(fv(date(2032, 1, 1)))   # 28 months apart: sources disagree
    conflicts.apply(full_project)
    completeness.apply(full_project)
    assert full_project.completeness == 6
    assert full_project.unresolved == ["possession"]
    assert full_project.label == "COMPARABLE"
    score, _, score_max, excluded = match_score.compute(full_project, own, 1.5)
    assert score is not None and excluded == ["possession"] and score_max == 100 - settings.w_possession


def test_two_unresolved_dimensions_are_not_comparable(full_project, own):
    full_project.configurations.observe(fv([1, 5]))         # union 1-5: too wide for one building
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


# --- the carpet band: same shape as the rate band, anchored on the subject ---
CARPET_ANCHOR = 1050.0   # Marina64's own 450-1650 midpoint


def _carpet_at(project, lo, hi):
    from app.models.schema import CarpetRange

    project.carpet_sqft = fr("carpet_sqft", fv(CarpetRange(min_sqft=lo, max_sqft=hi)))
    return project


def test_a_carpet_range_four_times_the_subject_is_refused(full_project, own):
    """Narang Valora published 1,360-4,218 sqft beside a 430-1,180 sqft subject."""
    conflicts.apply(_carpet_at(full_project, 1360, 4218), carpet_anchor=CARPET_ANCHOR)
    assert full_project.carpet_sqft.absent == "IMPLAUSIBLE_CARPET"
    assert full_project.carpet_sqft.span == [1360, 4218]
    assert "not used" in full_project.carpet_sqft.label


def test_half_a_carpet_range_is_not_a_carpet_range(full_project):
    """The top is credible and the bottom is a quarter of the floor; the range goes."""
    conflicts.apply(_carpet_at(full_project, 60, 1400), carpet_anchor=CARPET_ANCHOR)
    assert full_project.carpet_sqft.absent == "IMPLAUSIBLE_CARPET"


def test_a_genuinely_larger_competitor_is_a_real_comparison_and_is_kept(full_project):
    """3.5x the subject's midpoint is a bigger building, not a bad reading. Deleting
    it would delete the comparison the rep most wants."""
    conflicts.apply(_carpet_at(full_project, 1200, 3600), carpet_anchor=CARPET_ANCHOR)
    assert full_project.carpet_sqft.absent is None
    assert full_project.carpet_sqft.value.max_sqft == 3600


def test_without_a_carpet_anchor_no_judgement_is_made(full_project):
    conflicts.apply(_carpet_at(full_project, 60, 90), carpet_anchor=None)
    assert full_project.carpet_sqft.absent is None


def test_the_carpet_anchor_is_the_subjects_own_midpoint(own):
    assert conflicts.carpet_anchor_for(own) == CARPET_ANCHOR


def test_an_implausible_carpet_is_set_aside_and_the_credible_one_stands(full_project):
    """One bad page is a fact about that page. Withdrawing the field for it made every
    page read past the first one more chance to lose a value."""
    from app.models.schema import CarpetRange

    full_project.carpet_sqft = fr("carpet_sqft", fv(CarpetRange(min_sqft=400, max_sqft=900)),
                                  fv(CarpetRange(min_sqft=1360, max_sqft=4218), source="tavily"))
    conflicts.apply(full_project, carpet_anchor=CARPET_ANCHOR)
    assert full_project.carpet_sqft.absent is None
    assert full_project.carpet_sqft.value.max_sqft == 900
    assert [fv.value.max_sqft for fv in full_project.carpet_sqft.disputed] == [4218]
    assert not [c for c in full_project.conflicts if c.field == "carpet_sqft"]


def test_one_off_band_rate_does_not_withdraw_the_projects_own(full_project):
    """Ami One: its SquareYards page quotes 32,750, a comparison page 2,533."""
    full_project.rate_psf = fr("rate_psf", fv(RateValue(min_psf=32750, max_psf=32750), source="squareyards"),
                               fv(RateValue(min_psf=2533, max_psf=2533), source="tavily"))
    conflicts.apply(full_project, ANCHOR)
    assert full_project.rate_psf.absent is None and full_project.rate_psf.value.min_psf == 32750


def test_a_shared_rate_is_set_aside_when_the_project_quotes_its_own():
    """Mangalesh: 35,800 on its own page, the locality's 25,003 on MagicBricks."""
    ps = [_at(f"p{i}", 25003) for i in range(3)]
    ps[0].rate_psf.observe(fv(RateValue(min_psf=35800, max_psf=35800, basis="base"), source="squareyards"))
    conflicts.apply(ps[0], 38000.0, conflicts.shared_rates(ps))
    assert ps[0].rate_psf.absent is None and ps[0].rate_psf.value.min_psf == 35800


def test_agreeing_top_sources_are_not_a_tie(full_project):
    """Two SquareYards pages saying Dec 2028 beside aggregators saying 2028 and 2029
    were withdrawn as a tie. Agreement at the top is the strongest case there is."""
    full_project.possession = fr("possession", fv(date(2028, 12, 1), source="squareyards"),
                                 fv(date(2028, 12, 1), source="squareyards"),
                                 fv(date(2028, 1, 1), source="tavily"), fv(date(2029, 1, 1), source="tavily"))
    conflicts.apply(full_project)
    assert full_project.possession.value == date(2028, 12, 1)
    assert "tavily says 2028-01-01" in full_project.conflicts[0].detail


def test_overlapping_carpet_ranges_publish_the_better_source(full_project):
    from app.models.schema import CarpetRange

    full_project.carpet_sqft = fr("carpet_sqft", fv(CarpetRange(min_sqft=425, max_sqft=834), source="squareyards"),
                                  fv(CarpetRange(min_sqft=726, max_sqft=877), source="tavily"))
    conflicts.apply(full_project)
    assert full_project.carpet_sqft.value.min_sqft == 425


def test_disjoint_carpet_ranges_are_still_withdrawn(full_project):
    """Ranges that never meet may be two buildings; no priority makes one of them ours."""
    from app.models.schema import CarpetRange

    full_project.carpet_sqft = fr("carpet_sqft", fv(CarpetRange(min_sqft=400, max_sqft=600), source="squareyards"),
                                  fv(CarpetRange(min_sqft=900, max_sqft=1400), source="tavily"))
    conflicts.apply(full_project)
    assert full_project.carpet_sqft.absent == "SOURCES_DISAGREE"


def test_a_sidebar_rera_number_is_dropped_and_a_corroborated_one_kept():
    from app.logic.merge import strip_uncorroborated_rera
    from app.models.schema import Project, Provenance, ReraPhase

    def page(url, *nums, source="squareyards"):
        return FieldValue(value=[ReraPhase(number=n) for n in nums], prov=Provenance(source=source, url=url),
                          method="deterministic")

    p = Project(id="h", name="Hirani Dollars Avenue", rera_phases=[
        page("sy/hirani", "P51800076786", "P51800019624", "P51800048237"),
        page("houssed/hirani", "P51800076786", "P51800048237", source="tavily")])
    strip_uncorroborated_rera(p)
    assert [[ph.number for ph in v.value] for v in p.rera_phases.values] == [
        ["P51800076786", "P51800048237"], ["P51800076786", "P51800048237"]]


def test_a_comparison_page_is_recognised_by_its_url():
    from app.extract.deterministic import is_comparison_page

    assert is_comparison_page("https://www.mumbaipropertyexchange.com/compare/one-borivali-vs-kamla-rajesh/1-2")
    assert not is_comparison_page("https://www.squareyards.com/mumbai-residential-property/kamla-rajesh/234573/project")


# --- promoter rows vs projects ---------------------------------------------

def test_the_register_forms_borivali_uses_are_recognised_as_promoters():
    from app.logic.resolve import looks_like_company

    for name in ["New India Construction Company", "K Mehta And Company", "Chheda Group",
                 "Atithi Builders And Constructors Private Lim", "Ekta Housing",
                 "Sumukh Ventures", "Kothari Contractors", "Bhoomi Estates Limite"]:
        assert looks_like_company(name), name


def test_a_real_project_is_never_a_promoter_row():
    """The false positive costs a competitor silently, so this is the direction that
    matters. 'X by Y' is judged on X: 'group' in the builder half must not delete it."""
    from app.logic.resolve import looks_like_company

    for name in ["Airavat By Bhoomi Group", "Shreeji Atlantis by Shreeji Group",
                 "Laxmi Shrushti - THE LAXMI GROUP",
                 "Lodha Amara", "Ajmera Realty Heights", "Kalpataru Horizon Apartments",
                 "Rustomjee Summit", "Neev Horizon", "Sanghvi Horizon", "73 East",
                 "Ekta Tripolis", "Runwal Vertex", "Godrej Properties Prime"]:
        assert not looks_like_company(name), name


def test_a_page_title_is_cut_at_its_first_separator():
    from app.logic.resolve import clean_project_name

    assert clean_project_name("Evoke Residential project by Arkade | Goregaon West | Mumbai") == "Evoke by Arkade"
    assert clean_project_name("Paradigm Anantaara - Shimpoli Borivali West") == "Paradigm Anantaara"
    assert clean_project_name("Rustomjee Summit") == "Rustomjee Summit"


def test_cleaning_never_cuts_a_hyphenated_name_in_half():
    """The separator needs spaces around it; 'Sun-Rise Heights' is one name."""
    from app.logic.resolve import clean_project_name

    assert clean_project_name("Sun-Rise Heights") == "Sun-Rise Heights"


# --- what the model is shown -----------------------------------------------

def test_the_trimmer_keeps_the_region_around_a_wanted_field():
    from app.extract.deterministic import spans_for_fields

    page = ("Home About Contact " + "nav junk " * 400
            + " Possession: December 2028 for this tower. "
            + "footer cross-sell " * 400)
    out = spans_for_fields(page, ["possession"])
    assert "December 2028" in out
    assert len(out) < len(page) / 3


def test_a_page_with_nothing_relevant_is_sent_whole_rather_than_emptied():
    """The trimmer never gets to decide a page says nothing -- that judgement
    belongs to extraction, and an emptied page would read as an absence."""
    from app.extract.deterministic import spans_for_fields

    page = "Contact us for details about our upcoming developments."
    assert spans_for_fields(page, ["rate_psf"]) == page
    assert spans_for_fields(page, []) == page


def test_distant_fragments_cannot_read_as_adjacent():
    """Joining two far-apart regions could put a number next to a label it does not
    belong to. The gap is marked so it cannot."""
    from app.extract.deterministic import spans_for_fields

    page = "Carpet area 640 sq ft. " + "x " * 5000 + " Possession December 2028."
    out = spans_for_fields(page, ["carpet_sqft", "possession"])
    assert "[…]" in out


# --- rows that are not projects at all -------------------------------------

def test_places_says_what_the_pin_is_and_a_shop_is_not_a_competitor():
    from app.logic.resolve import not_residential

    assert not_residential(["clothing_store", "store", "point_of_interest"]) == "clothing_store"
    assert not_residential(["school"]) == "school"
    assert not_residential(["apartment_complex", "premise"]) is None
    assert not_residential([]) is None


def test_a_tower_with_a_shop_downstairs_is_still_a_tower():
    """Places tags a residential pin with its ground-floor retail too. Dropping on
    the retail tag alone would cost a real competitor."""
    from app.logic.resolve import not_residential

    assert not_residential(["apartment_building", "clothing_store"]) is None


def test_a_candidate_carrying_a_registers_worth_of_rera_numbers_is_a_listing(full_project):
    """Many numbers AND a form nobody could fill. A portal's project page lists the
    builder's other work down the side, so the count alone hid three genuine 6/6
    launches in Borivali West; what a register listing cannot do is carry one carpet
    range, one rate and one possession date."""
    from app.logic import eligibility
    from app.models.schema import ReraPhase

    assert eligibility.not_a_project(full_project) is None      # one phase, a real project
    full_project.rera_phases = fr("rera_phases", fv([ReraPhase(number=f"P5180000000{i}") for i in range(6)]))
    assert eligibility.not_a_project(full_project) is None      # six, but the form is filled

    listing = full_project.model_copy(deep=True)
    listing.carpet_sqft = fr("carpet_sqft")     # nothing published a carpet range
    assert "register listing" in eligibility.not_a_project(listing)


# --- a subject propOG has not filled in yet ---------------------------------

def test_a_subject_with_no_scoring_fields_scores_nothing_rather_than_ten_out_of_ten(full_project, own):
    """propOG's subject arrives without carpet, rate, possession, structure or
    configurations. Distance is proximity, not similarity: with everything else
    excluded the arithmetic still produced 9/10, which reads as a strong match on a
    competitor nothing about the subject had been compared to."""
    thin = own.model_copy(update={"carpet_sqft": None, "rate_psf": None, "possession": None,
                                  "structure": None, "configurations": []})
    completeness.apply(full_project)
    match_score.apply(full_project, thin, 1.5)
    assert full_project.label == "COMPARABLE"
    assert full_project.match_score is None and full_project.score_max is None
    assert "distance" in full_project.score_excluded


def test_one_real_dimension_is_enough_to_score(full_project, own):
    """Only carpet survives, and the denominator says so rather than hiding it."""
    thin = own.model_copy(update={"rate_psf": None, "possession": None, "structure": None,
                                  "configurations": []})
    completeness.apply(full_project)
    match_score.apply(full_project, thin, 1.5)
    assert full_project.match_score is not None
    assert full_project.score_max == settings.w_carpet + settings.w_distance
    assert set(full_project.score_excluded) == {"config", "rate", "possession", "structure"}


def test_the_compare_column_for_an_unfilled_subject_reads_like_an_unpublished_one(own):
    """Null, not a dict of nulls -- so the same downstream code says why it is missing."""
    from app.logic import compare as compare_logic

    thin = own.model_copy(update={"carpet_sqft": None, "rate_psf": None, "possession": None,
                                  "structure": None})
    col = compare_logic._column_from_own(thin)
    assert (col["carpet"], col["rate"], col["possession"], col["structure"]) == (None, None, None, None)
