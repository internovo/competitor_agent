"""Every completed run reports what it cost and how long it took.

At Rs 110 a run you cache; at Rs 2 you do not. And a stored analysis needs a date
on it, because search results vary between runs -- two scans returning different
competitors reads as a broken product unless the report says when it was taken.
"""
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import FIXTURE_DIR, settings
from app.cost import Cost, UsageCounter, classify
from app.main import app

REQUEST = json.loads((FIXTURE_DIR / "scan_request.json").read_text())


@pytest.fixture(scope="module")
def done():
    with TestClient(app) as c:
        rid = c.post("/scans", json=REQUEST).json()["run_id"]
        for _ in range(300):
            body = c.get(f"/scans/{rid}").json()
            if body["status"] in ("done", "failed"):
                break
            time.sleep(0.02)
        assert body["status"] == "done", body.get("error")
        return body


def test_a_completed_run_carries_cost_extraction_and_timing(done):
    assert set(done["cost"]) >= {"llm_calls", "input_tokens", "output_tokens", "searches",
                                 "pages_fetched", "places_calls", "estimated_inr"}
    assert set(done["extraction"]) == {"deterministic_fields", "llm_fields"}
    assert set(done["timing"]) == {"started_at", "finished_at", "duration_s", "per_stage_s"}


def test_the_cost_is_labelled_an_estimate_not_a_bill(done):
    """The agent never sees an invoice. Saying otherwise would be a lie with a
    decimal point on it."""
    assert "not a bill" in done["cost"]["note"]


def test_duration_matches_the_wall_clock(done):
    from datetime import datetime

    started = datetime.fromisoformat(done["timing"]["started_at"])
    finished = datetime.fromisoformat(done["timing"]["finished_at"])
    assert abs((finished - started).total_seconds() - done["timing"]["duration_s"]) < 1.0


def test_the_per_stage_breakdown_names_every_stage_the_graph_ran(done):
    per_stage = done["timing"]["per_stage_s"]
    assert set(per_stage) == {"geocode", "discover", "resolve", "extract", "filter", "score",
                              "retry_thin", "narrate", "persist"}
    assert sum(per_stage.values()) <= done["timing"]["duration_s"] + 0.5


def test_the_breakdown_shows_which_stage_the_run_was_spent_in(monkeypatch):
    """A fixture run finishes in ~50 ms with no network and no model, so nothing
    dominates it. The stage that does the work is the one that shows: here that is
    forced onto extract, which is where a live run spends its minutes."""
    import asyncio

    from app.sources.fixture import FixtureSource

    original = FixtureSource.pages_for

    async def slow(self, project, ctx):
        await asyncio.sleep(0.05)
        return await original(self, project, ctx)

    monkeypatch.setattr(FixtureSource, "pages_for", slow)
    with TestClient(app) as c:
        rid = c.post("/scans", json=REQUEST).json()["run_id"]
        for _ in range(300):
            body = c.get(f"/scans/{rid}").json()
            if body["status"] in ("done", "failed"):
                break
            time.sleep(0.02)
    per_stage = body["timing"]["per_stage_s"]
    assert max(per_stage, key=per_stage.get) == "extract", per_stage


def test_a_fixture_run_costs_nothing_and_says_so(done):
    """No keys, no network, no model: the estimate must be zero rather than a
    plausible-looking number."""
    assert done["cost"]["estimated_inr"] == 0.0
    assert done["cost"]["llm_calls"] == 0
    assert done["cost"]["searches"] == 0 and done["cost"]["places_calls"] == 0


def test_pages_are_counted(done):
    assert done["cost"]["pages_fetched"] == 13


def test_the_extraction_split_is_reported(done):
    assert done["extraction"]["deterministic_fields"] > 0
    assert done["extraction"]["llm_fields"] == 0     # no model was configured


# --- the arithmetic ---------------------------------------------------------

def test_the_estimate_is_local_arithmetic_from_the_configured_prices():
    c = Cost(llm_calls=14, input_tokens=41_200, output_tokens=3_100, searches=22, places_calls=3)
    expected = (41_200 / 1e6 * settings.llm_input_usd_per_mtok
                + 3_100 / 1e6 * settings.llm_output_usd_per_mtok
                + 22 * settings.search_usd_per_call
                + 3 * settings.places_usd_per_call) * settings.inr_per_usd
    assert c.estimated_inr == round(expected, 2)
    assert c.estimated_inr > 0


def test_calls_are_classified_by_the_one_door_they_all_go_through():
    urls = ["https://api.tavily.com/search", "https://api.tavily.com/extract",
            "https://places.googleapis.com/v1/places:searchNearby",
            "https://www.squareyards.com/x", "https://maharera.maharashtra.gov.in/y"]
    assert classify(urls) == (2, 1, 2)


# --- token counting ---------------------------------------------------------

class Response:
    def __init__(self, llm_output=None, generations=None):
        self.llm_output, self.generations = llm_output, generations or []


def test_usage_is_read_from_llm_output():
    import asyncio

    u = UsageCounter()
    asyncio.run(u.on_llm_end(Response(llm_output={"usage": {"input_tokens": 1200, "output_tokens": 90}})))
    assert (u.input_tokens, u.output_tokens) == (1200, 90)


def test_usage_is_read_from_the_message_when_llm_output_is_empty():
    import asyncio
    from types import SimpleNamespace

    gen = SimpleNamespace(message=SimpleNamespace(usage_metadata={"input_tokens": 5, "output_tokens": 2}))
    u = UsageCounter()
    asyncio.run(u.on_llm_end(Response(generations=[[gen]])))
    assert (u.input_tokens, u.output_tokens) == (5, 2)


def test_a_provider_that_reports_nothing_leaves_the_count_at_zero():
    """Better a zero we can explain than a number we invented."""
    import asyncio

    u = UsageCounter()
    asyncio.run(u.on_llm_end(Response()))
    assert (u.input_tokens, u.output_tokens) == (0, 0)
