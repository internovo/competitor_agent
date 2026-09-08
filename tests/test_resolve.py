import pytest

from app.logic import resolve
from app.models.schema import Candidate


def c(name, builder=None, rera=None, lat=None, lng=None, source="tavily"):
    return Candidate(name=name, builder=builder, rera_no=rera, lat=lat, lng=lng, source=source)


def test_same_rera_is_same():
    assert resolve.decide(c("Runwal Vertex", rera="P51800048221"), c("Vertex Phase 2", rera="P51800048221")) is True


def test_different_rera_is_different():
    assert resolve.decide(c("Runwal Vertex", rera="P51800048221"), c("Runwal Vertex", rera="P51800048222")) is False


def test_builder_name_inside_project_name_is_noise():
    assert resolve.decide(c("Runwal Vertex", "Runwal Group"), c("Vertex by Runwal")) is True


def test_same_builder_different_project():
    assert resolve.decide(c("Runwal Vertex", "Runwal Group"), c("Runwal Elegante", "Runwal Group")) is False


def test_different_builders_are_different():
    assert resolve.decide(c("Sky Heights", "Lodha"), c("Sky Heights", "Oberoi Realty")) is False


def test_nearby_with_shared_token_is_same():
    assert resolve.decide(c("Kalpataru Aurum Tower A", lat=19.19, lng=72.84), c("Aurum", lat=19.1905, lng=72.84)) is True


def test_partial_overlap_far_apart_is_ambiguous():
    assert resolve.decide(c("Chandak Highscape City"), c("Highscape Heights")) is None


def test_no_shared_tokens_is_different():
    assert resolve.decide(c("Sheth Nova"), c("Ajmera Skyline")) is False


# --- register rows named for the promoter, not the project -------------------

COMPANY_NAMES = [
    "Jadhwani Infrastructure LLP", "AOP OF BINDESH AND MILIND SOMAIYA", "Mahindra Lifespace Developers limited",
    "Sky Property Developers Private Limited", "RAJVI BUILDERS AND DEVELOPERS", "HARMONY AND PROSPERITY DEVELOPER",
    "PRANAV CONSTRUCTIONS LIMITED", "Indra Realtors", "Lodha Developers", "M/s Gemstar Enterprises",
]

PROJECT_NAMES = [
    "Lodha Amara", "Rustomjee Elanza", "Chandak Treesourus", "Ruparel Stardom Malad West", "Auris Serenity",
    "Runwal Vertex", "Kalpataru Aurum", "Narang Vivenda",
    "Ajmera Realty Heights",   # a suffix word mid-name is not a corporate tail
    "Lodha Developers Park",   # nor is one followed by a real name
]


@pytest.mark.parametrize("name", COMPANY_NAMES)
def test_promoter_names_are_register_only(name):
    assert resolve.looks_like_company(name) is True


@pytest.mark.parametrize("name", PROJECT_NAMES)
def test_project_names_are_never_register_only(name):
    """The false positive is the expensive one: it deletes a real competitor."""
    assert resolve.looks_like_company(name) is False


def test_the_subject_is_excluded_by_identity_not_by_lifecycle():
    """Marina64's own register filings must not come back as competitors."""
    import asyncio, json
    from app.config import FIXTURE_DIR
    from app.graph.nodes import pipeline
    from app.models.schema import OwnProject

    own = OwnProject(**json.loads((FIXTURE_DIR / "marina64.json").read_text(encoding="utf-8")))
    cands = [
        Candidate(name=own.name, lat=own.lat, lng=own.lng, source="tavily"),                       # by name
        Candidate(name="Marina 64 Phase 2", rera_no=own.rera_phases[0].number, lat=own.lat, lng=own.lng, source="maharera"),
        Candidate(name="Mahindra Lifespace Developers limited", rera_no="P51800099999",
                  lat=own.lat, lng=own.lng, source="maharera", register_only=True),
        Candidate(name="Runwal Vertex", lat=own.lat, lng=own.lng, source="tavily"),
    ]
    out = asyncio.run(pipeline.resolve_node({"own": own, "radius_km": 1.5, "candidates": cands},
                                            {"configurable": {"llm": None}}))
    assert [p.name for p in out["projects"]] == ["Runwal Vertex"]
    assert [f["name"] for f in out["register_filings"]] == ["Mahindra Lifespace Developers limited"]
    assert all(d["reason"] == "own project" for d in out["dropped"])


def test_one_shared_word_never_selects_a_portal_page():
    """The Greater Noida bug: a search for a Malad West project must not follow a link
    that happens to share a single token with its name."""
    from app.models.schema import Project
    from app.sources.portals import _matches

    godrej = Project(id="x", name="Godrej Prime")
    wrong = "/godrej-golf-links-crown-residences-sector-27-yamuna-expressway-greater-noida-npd-343924"
    assert _matches(wrong, godrej, "Malad West") is False

    right = "/godrej-prime-malad-west-mumbai-npd-1234"
    assert _matches(right, godrej, "Malad West") is True
    # one distinctive token is enough when the locality agrees
    assert _matches("/prime-malad-west-npd-9", godrej, "Malad West") is True
    assert _matches("/prime-thane-npd-9", godrej, "Malad West") is False


# ---- display names -------------------------------------------------------

@pytest.mark.parametrize("raw,builder,clean", [
    ("Raghav UTOPIA New Launch Project Goregaon West", None, "Raghav UTOPIA Goregaon West"),
    ("Shreeji Atlantis by Shreeji Group", "Shreeji Group", "Shreeji Atlantis"),
    ("AJMERA BOULEVARD", None, "Ajmera Boulevard"),
    ("Harshail Hornbill, Malad West", None, "Harshail Hornbill"),
    ("Ruparel Stardom Malad West", None, "Ruparel Stardom"),
])
def test_seo_titles_clean_up(raw, builder, clean):
    assert resolve.clean_project_name(raw, builder, "Malad West") == clean


@pytest.mark.parametrize("raw", ["Lodha", "Lodha Developers", "Runwal Vertex"])
def test_a_name_cleaning_would_gut_is_returned_untouched(raw):
    assert resolve.clean_project_name(raw, raw, "Malad West") == raw


def test_cleaning_is_display_only_and_identity_stays_on_the_raw_name():
    from app.models.schema import Project

    p = Project(id="x", name=resolve.clean_project_name("AJMERA BOULEVARD", None, "Malad West"),
                name_raw="AJMERA BOULEVARD")
    assert p.name == "Ajmera Boulevard"
    assert p.match_name == "AJMERA BOULEVARD"


# ---- the prefilter: cheap, and it leans toward "different" ---------------

def test_far_apart_with_no_shared_token_never_reaches_the_model():
    assert resolve.could_be_same(c("Desai Oceanic", lat=19.01, lng=72.81),
                                 c("Birla Estates", lat=19.03, lng=72.83)) is False


def test_a_shared_distinctive_token_survives_the_prefilter():
    assert resolve.could_be_same(c("Chandak Highscape City"), c("Highscape Heights")) is True


def test_close_together_survives_the_prefilter():
    assert resolve.could_be_same(c("Aakasa", lat=19.0176, lng=72.8175),
                                 c("Akasa Worli", lat=19.0177, lng=72.8176)) is True


def test_the_builder_brand_alone_is_not_a_shared_token():
    """Two different Runwal projects must not be paired on the word 'Runwal'."""
    assert resolve.could_be_same(c("Runwal Vertex", "Runwal Group"),
                                 c("Runwal Elegante", "Runwal Group")) is False


def test_a_placeholder_pin_falls_through_to_the_token_test():
    """The register pins unlocated rows at the district office. Those coordinates would
    otherwise make every such row 0 km from every other."""
    pin = (19.05, 72.85)
    names = ["Jadhwani Infrastructure LLP", "Pranav Constructions Limited", "Indra Realtors",
             "Dudhal Infra", "Maitri And PP Realtors"]
    rows = [c(n, lat=pin[0], lng=pin[1], source="maharera") for n in names]
    placeholders = resolve.placeholder_pins(rows)
    assert pin in placeholders
    assert resolve.could_be_same(rows[0], rows[1], placeholders) is False
    assert resolve.could_be_same(rows[0], rows[1], set()) is True     # without the signal, they all match


def test_matching_rera_numbers_always_reach_the_comparison():
    a = c("Anything", rera="P51800048221", lat=19.0, lng=72.8)
    b = c("Something Else", rera="P51800048221", lat=19.4, lng=73.2)
    assert resolve.could_be_same(a, b) is True


# ---- societies -----------------------------------------------------------

@pytest.mark.parametrize("name", [
    "Amar Nagar CHS", "Vainganga Worli CHS", "Malwadi Satyam CHSL Malad West",
    "Evershine Grandeur CHSL", "Amar Nagar Co-operative Housing Society",
    "Amar Nagar Co-op Housing Society", "Shree Sahakari Griha Nirman",
])
def test_societies_are_recognised(name):
    assert resolve.looks_like_society(name) is True


@pytest.mark.parametrize("name", [
    "Kalpataru Horizon Apartments", "Nirlon House", "Electric Mansion", "Venus Apartments",
    "Chandak Treesourus", "Amar Nagar Heights", "Chsomething Tower",
])
def test_a_real_project_is_never_taken_for_a_society(name):
    """Apartments / House / Mansion / Nagar all appear in live project names. A false
    positive here deletes a genuine competitor and nobody sees it go."""
    assert resolve.looks_like_society(name) is False


# ---- portal index pages are not candidates -------------------------------

@pytest.mark.parametrize("title", [
    "981+ Flats / Apartments for Sale near Borivali West",
    "Borivali West Mumbai: Map Property Rates",
    "New Launch Projects in Borivali West",
    "Property Rates in Borivali West",
])
def test_a_portal_index_page_is_not_a_project(title):
    """The locality is where we are looking, not what we are looking for."""
    from app.sources.tavily_web import _is_generic

    assert _is_generic(title, "Borivali West") is True


@pytest.mark.parametrize("title", [
    "Chandak Treesourus", "Rustomjee Summit Borivali West", "Kalpataru Horizon Apartments",
])
def test_a_real_project_title_survives_the_filter(title):
    from app.sources.tavily_web import _is_generic

    assert _is_generic(title, "Borivali West") is False
