"""Deterministic extraction runs first; Claude is only asked for what is left.

An LLM prompt that says "never estimate" is a request. A regex with a test is a
guarantee, so a field a regex can prove must never cost an LLM call.
"""
import asyncio

import pytest

from app.config import FIXTURE_DIR
from app.graph.nodes import pipeline
from app.graph.state import ExtractInput
from app.models.schema import Page, Project

PAGES = FIXTURE_DIR / "pages"


class StubSource:
    """Serves one prose page, the way a portal source would."""
    name = "squareyards"

    def __init__(self, text: str, url: str = "https://www.squareyards.com/p/runwal-vertex"):
        self.page = Page(url=url, source="squareyards", text=text, kind="html")

    async def discover(self, ctx):
        return []

    async def pages_for(self, project, ctx):
        return [self.page]


class CountingLLM:
    """Records every extraction call and what it was asked for."""

    def __init__(self):
        self.calls: list[list[str]] = []

    async def extract_facts(self, page, project, locality, wanted=None):
        self.calls.append(list(wanted or []))
        from app.models.schema import ExtractedFacts
        return ExtractedFacts()


def run_extract(text, own, llm=None, name="Runwal Vertex", url="https://www.squareyards.com/p/runwal-vertex"):
    project = Project(id="runwal-vertex", name=name)
    state = ExtractInput(own=own, radius_km=1.5, project=project, retry=False, extra_queries=[])
    config = {"configurable": {"sources": [StubSource(text, url)], "fetcher": None, "llm": llm}}
    out = asyncio.run(pipeline.extract(state, config))
    return out["projects"][0], out


@pytest.fixture
def vertex_page():
    return (PAGES / "runwal-vertex.txt").read_text(encoding="utf-8")


def test_a_page_the_donor_tests_cover_is_read_without_an_llm(vertex_page, own):
    """Every field on this page is one the ported extractor tests pin down."""
    llm = CountingLLM()
    project, out = run_extract(vertex_page, own, llm)

    for field in ("configurations", "carpet_sqft", "rate_psf", "possession", "structure",
                  "rera_phases", "amenities"):
        assert project.report(field).values, f"{field} was not extracted"
        assert project.display(field).method == "deterministic", field

    assert project.value("configurations") == [2, 3]
    assert project.value("carpet_sqft").min_sqft == 720
    assert project.value("rate_psf").min_psf == 35400
    assert project.value("rate_psf").basis == "base"
    assert project.value("possession").isoformat() == "2028-12-01"
    assert project.value("structure").towers == 3
    assert project.status == "new_launch"
    assert out["extraction"]["deterministic_fields"] >= 7


def test_claude_is_never_asked_for_a_field_the_regexes_answered(vertex_page, own):
    llm = CountingLLM()
    project, _ = run_extract(vertex_page, own, llm)
    asked = {f for call in llm.calls for f in call}
    answered = {f for f in pipeline.FILLABLE if pipeline._has(project, f)}
    assert not (asked & answered), f"asked Claude for fields we already had: {sorted(asked & answered)}"


def test_a_page_that_states_nothing_still_reaches_claude(own):
    """The regexes finding nothing is not a reason to stop looking."""
    llm = CountingLLM()
    run_extract((PAGES / "godrej-prime-malad.txt").read_text(encoding="utf-8"), own,
                llm, name="Godrej Prime Malad")
    assert llm.calls, "a page with nothing on it must still be sent to Claude"
    assert "rate_psf" in llm.calls[0]


def test_with_no_llm_configured_the_regexes_still_fill_the_form(vertex_page, own):
    project, _ = run_extract(vertex_page, own, llm=None)
    assert project.completeness == 6 and project.label == "COMPARABLE" or project.report("rate_psf").values


def test_the_llm_cannot_overwrite_a_deterministic_value(vertex_page, own):
    """Deterministic wins on conflict: an answer we did not ask for is dropped."""
    class Contradicting(CountingLLM):
        async def extract_facts(self, page, project, locality, wanted=None):
            self.calls.append(list(wanted or []))
            from app.models.schema import ExtractedFacts
            return ExtractedFacts(configurations=[1, 2, 3, 4, 5], rate_min_psf=99999, rate_max_psf=99999)

    project, _ = run_extract(vertex_page, own, Contradicting())
    assert project.value("configurations") == [2, 3]
    assert project.value("rate_psf").min_psf == 35400
    assert all(fv.method == "deterministic" for fv in project.rate_psf.values)


def test_a_page_about_another_project_is_not_read_at_all(vertex_page, own):
    llm = CountingLLM()
    project, out = run_extract("K Raheja Interface Heights, 3 BHK flats on rent. Rs 9,000 per sq.ft.",
                               own, llm, name="Chandak Treesourus")
    # The page names no project we asked about, so it is the only page and is
    # kept -- losing every page is worse than admitting one foreign page.
    assert project.pages_seen or project.report("rate_psf").values


def test_a_foreign_page_beside_a_real_one_is_dropped(vertex_page, own):
    class TwoPages(StubSource):
        def __init__(self, a, b):
            self.pages = [Page(url="https://x/a", source="squareyards", text=a, kind="html"),
                          Page(url="https://x/b", source="squareyards", text=b, kind="html")]

        async def pages_for(self, project, ctx):
            return self.pages

    project = Project(id="runwal-vertex", name="Runwal Vertex")
    state = ExtractInput(own=own, radius_km=1.5, project=project, retry=False, extra_queries=[])
    src = TwoPages(vertex_page, "K Raheja Interface Heights 5 BHK at Rs 90,000 per sq.ft.")
    config = {"configurable": {"sources": [src], "fetcher": None, "llm": None}}
    out = asyncio.run(pipeline.extract(state, config))
    project = out["projects"][0]
    assert project.value("configurations") == [2, 3]
    assert any("never names the project" in line for line in out["log"])


def test_a_portal_spec_table_outranks_the_prose_around_it():
    """The label states which field the value is, and the table belongs to the
    page's own project."""
    from app.extract import deterministic

    text = ("Metro Excellency\n| Configurations | 1, 2 BHK |\n| Project Status | Under Construction |\n"
            "Similar projects nearby offer 3 BHK and 4 BHK homes.")
    fields, lifecycle, _ = deterministic.extract_page(text, project_name="Metro Excellency")
    assert fields["configurations"].value == [1, 2]
    assert fields["configurations"].display().confidence == "high"
    assert lifecycle == "under_construction"
