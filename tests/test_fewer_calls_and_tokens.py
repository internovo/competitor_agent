"""Doing less work without finding less.

Every rule here was measured on the two frozen corpora before it was kept, and two
were thrown away for costing a competitor. The numbers are in the PR body; these
tests pin the behaviour the numbers came from.
"""
import asyncio
from datetime import date, timedelta


from app.cost import TokenBudget
from app.graph.nodes import pipeline
from app.graph.state import ExtractInput
from app.models.schema import Candidate, Page, Project


# --- B5: a hard ceiling on what one scan may send ---------------------------

def test_a_budget_lets_work_through_until_it_is_spent():
    b = TokenBudget(100)
    assert b.take(200) and b.used == 50          # 4 chars to a token
    assert b.take(200) and b.used == 100
    assert not b.take(4)
    assert b.stopped and b.skipped == 1


def test_a_budget_of_zero_is_no_budget_at_all():
    """0 disables the ceiling rather than refusing everything."""
    b = TokenBudget(0)
    assert b.take(10_000_000) and not b.stopped


def test_refusing_a_candidate_does_not_lose_what_it_already_read():
    b = TokenBudget(1)
    b.take(4)
    assert not b.take(40_000)
    assert b.used == 1                            # the refused page is not billed


# --- B5: what is worth waking a model for -----------------------------------

def test_the_six_dimensions_and_the_two_that_decide_the_row_are_worth_asking():
    worth = pipeline.MODEL_WORTH_WAKING_FOR
    for f in ("configurations", "carpet_sqft", "rate_psf", "possession", "structure",
              "rera_phases", "status", "builder"):
        assert f in worth, f


def test_timeline_alone_is_not_worth_a_model_call():
    """109 of 247 pages on the Kandivali corpus -- 891,036 characters, 41% of the
    prompt bill -- were sent asking for timeline and nothing else. It is in neither
    the completeness count nor the score."""
    assert "timeline" not in pipeline.MODEL_WORTH_WAKING_FOR


class _Pages:
    name = "squareyards"

    def __init__(self, *texts):
        self.pages = [Page(url=f"https://x/{i}", source="squareyards", text=t, kind="html")
                      for i, t in enumerate(texts)]

    async def discover(self, ctx):
        return []

    async def pages_for(self, project, ctx):
        return list(self.pages)


class _CountingLLM:
    def __init__(self):
        self.asked = []

    async def extract_facts(self, page, project, locality, wanted=None):
        from app.models.schema import ExtractedFacts
        self.asked.append(sorted(wanted or []))
        return ExtractedFacts()


def _extract(own, project, *texts, llm=None, budget=None):
    state = ExtractInput(own=own, radius_km=1.5, project=project, retry=False, extra_queries=[])
    deps = {"sources": [_Pages(*texts)], "fetcher": None, "llm": llm}
    if budget is not None:
        deps["token_budget"] = budget
    return asyncio.run(pipeline.extract(state, {"configurable": deps}))["projects"][0]


PAGE = ("Somewhere Heights, Malad West. 2 and 3 BHK apartments of carpet area 650 to 900 sq ft. "
        "Price Rs 29,900 per sq ft. Possession December 2028. MahaRERA P51800012345. "
        "Under construction. Three towers of 22 floors. Swimming Pool, Gymnasium.")


def test_no_page_is_ever_sent_for_timeline_alone(own):
    """The invariant, whatever else a page leaves outstanding: every request the model
    receives contains at least one field that can move the tier, the score or the row.
    On the Kandivali corpus 109 of 247 pages failed that test and carried 41% of the
    prompt bill."""
    llm = _CountingLLM()
    _extract(own, Project(id="p", name="Somewhere Heights"), PAGE, llm=llm)
    for asked in llm.asked:
        assert set(asked) - {"timeline"}, asked


def test_a_page_that_could_still_fill_a_scored_field_is_sent(own):
    """The cut must not become "never ask": a missing rate is worth the call."""
    llm = _CountingLLM()
    p = Project(id="p", name="Somewhere Heights", builder="Somewhere Group",
                status="under_construction")
    _extract(own, p, "Somewhere Heights, Malad West. Possession December 2028.", llm=llm)
    assert llm.asked and "rate_psf" in llm.asked[0]


def test_a_spent_budget_stops_the_asking_and_keeps_the_reading(own):
    """The scan that died on Groq kept nothing. A scan that stops asking keeps it all."""
    llm, budget = _CountingLLM(), TokenBudget(1)
    budget.take(4)
    p = _extract(own, Project(id="p", name="Nowhere Tower"), "Nowhere Tower, Malad West. 2 BHK.",
                 llm=llm, budget=budget)
    assert llm.asked == []
    assert budget.skipped >= 1
    assert p.value("configurations") == [2]       # the regex result survives


# --- B1: checks that can be made before paying for research -----------------

def _c(name, **kw) -> Candidate:
    return Candidate(name=name, source=kw.pop("source", "squareyards"), **kw)


def test_a_possession_date_already_past_is_not_researched():
    yesterday = (date.today() - timedelta(days=400)).isoformat()[:7]
    assert pipeline._as_date(yesterday) is not None


def test_a_date_we_cannot_read_never_drops_a_candidate():
    """A candidate is never dropped on a date that did not parse."""
    for junk in (None, "", "soon", "20xx-13"):
        assert pipeline._as_date(junk) is None


def test_both_date_shapes_the_listing_uses_are_understood():
    assert pipeline._as_date("2029-12") == date(2029, 12, 1)
    assert pipeline._as_date("2029-12-05") == date(2029, 12, 5)


def test_the_listing_hands_over_what_it_already_knows():
    """`status` and `possession` were parsed out of the JSON-LD and then dropped, so
    every finished building in the circle was researched in full before being thrown
    away. The status flag is NOT acted on -- see the PR body."""
    c = _c("Somewhere", status="ready", possession="2020-06")
    assert c.status == "ready" and c.possession == "2020-06"


# --- B4: what a failed run still knows --------------------------------------

def test_a_failed_run_still_reports_what_it_spent():
    """Three runs on 22-23 Sep reported no Places and no Tavily calls at all, because
    the only place the counts lived was a local the exception unwound past."""
    from app import service

    class _Fetcher:
        calls = ["https://places.googleapis.com/v1/places:searchText",
                 "https://api.tavily.com/search", "https://example.com/a"]
        refused: dict = {}

    meta = service._partial_meta(_Fetcher(), TokenBudget(100))
    assert meta["places_calls"] == 1 and meta["searches"] == 1
    assert meta["http_calls"] == 3
    assert meta["token_budget"]["max_tokens"] == 100


def test_a_dump_is_off_unless_a_directory_is_configured():
    from app.config import settings
    assert settings.run_dump_dir is None, "the canonical copy is Postgres, not a file"
