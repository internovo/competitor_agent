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


# --- the price table and the per-node split ---------------------------------

def test_each_model_is_priced_from_the_table_not_one_global_rate():
    from app.config import LLM_PRICES, llm_price

    assert llm_price("claude-opus-5") == LLM_PRICES["claude-opus-5"][:2]
    assert llm_price("openai/gpt-oss-120b") == (0.15, 0.75)
    opus = Cost(model="claude-opus-5", input_tokens=1_000_000, output_tokens=100_000)
    haiku = Cost(model="claude-haiku-4-5", input_tokens=1_000_000, output_tokens=100_000)
    assert opus.estimated_inr > haiku.estimated_inr * 4


def test_an_unpriced_model_falls_back_rather_than_reading_as_free():
    """A zero would be the one answer that is certainly wrong."""
    from app.config import llm_price

    assert llm_price("some-model-nobody-listed") == (settings.llm_input_usd_per_mtok,
                                                     settings.llm_output_usd_per_mtok)
    assert Cost(model="some-model-nobody-listed", input_tokens=1_000_000).estimated_inr > 0


def test_tokens_land_against_the_stage_that_spent_them():
    """extract, resolve and narrate are billed separately because they scale on
    different things: pages, candidate pairs, and the eligible set."""
    import asyncio

    from app.llm.client import LLM

    class Chat:
        async def ainvoke(self, messages, config=None):
            for cb in (config or {}).get("callbacks", []):
                cb.input_tokens += 1000
                cb.output_tokens += 100
            return "ok"

    llm = LLM.__new__(LLM)
    llm.calls_attempted = llm.calls_failed = 0
    llm.model = "claude-opus-5"
    llm.extract_model = "claude-haiku-4-5"
    llm.stage_model = {"extract": "claude-haiku-4-5", "resolve": "claude-opus-5", "narrate": "claude-opus-5"}
    llm.usage = {s: UsageCounter() for s in ("extract", "resolve", "narrate")}
    asyncio.run(llm._call(Chat(), [], "extract"))
    asyncio.run(llm._call(Chat(), [], "extract"))
    asyncio.run(llm._call(Chat(), [], "narrate"))

    split = llm.usage_by_stage()
    assert set(split) == {"extract", "narrate"}          # resolve made no call, so it is not billed
    assert split["extract"]["calls"] == 2 and split["extract"]["input_tokens"] == 2000
    assert split["narrate"]["input_tokens"] == 1000
    assert llm.input_tokens == 3000 and llm.output_tokens == 300
    # And each stage is priced at the model that ran it: two extraction calls on the
    # cheap model cost less than one narrate call on the expensive one.
    assert split["extract"]["estimated_inr"] < split["narrate"]["estimated_inr"]


def test_the_scan_reports_tokens_per_candidate_not_just_a_total():
    """The number that projects to a monthly bill: cost scales with candidates
    researched, not with the suburb."""
    c = Cost(input_tokens=1_000_000, output_tokens=100_000, candidates=50)
    assert c.tokens_per_candidate == 22_000
    assert Cost(input_tokens=10, candidates=0).tokens_per_candidate == 0
    assert c.body()["tokens_per_candidate"] == 22_000


def test_a_span_is_emitted_per_node_and_per_candidate():
    """The collector is not stood up here -- only the wiring is checked, so pointing
    one at OTEL_EXPORTER_OTLP_ENDPOINT later needs no code change."""
    import asyncio

    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app import service
    from app.models.schema import OwnProject

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    own = OwnProject(**REQUEST["own"])
    try:
        asyncio.run(service.run_scan(own, 1.5, "fixture", llm=None))
    finally:
        # OpenTelemetry's global provider cannot be replaced once set, so leaving this
        # one live had every later test exporting spans it never asked for -- which was
        # enough extra per-node work to flip the stage-timing test when the suite ran
        # in order. Shutting the processor down stops the export.
        provider.shutdown()

    names = [s.name for s in exporter.get_finished_spans()]
    assert {"node.geocode", "node.discover", "node.extract", "node.score", "node.persist"} <= set(names)
    per_candidate = [s for s in exporter.get_finished_spans()
                     if s.name == "node.extract" and s.attributes.get("candidate.id")]
    assert per_candidate, "extract must carry the candidate it ran on"
    assert "llm.input_tokens" in per_candidate[0].attributes


def test_extraction_and_matching_run_on_different_models_by_default():
    """Field reading is not a judgement call; entity matching is. They were on one
    model only because one client served all three jobs."""
    from app.llm.client import LLM

    llm = LLM.__new__(LLM)
    llm.provider = "anthropic"
    assert settings.extract_model == "claude-haiku-4-5"
    assert settings.claude_model == "claude-opus-5"
    from app.config import llm_price

    assert llm_price(settings.extract_model)[0] < llm_price(settings.claude_model)[0]


def test_the_stable_prefix_is_below_the_cache_minimum_on_the_extraction_model():
    """Task 3's answer, pinned so a model change re-raises the question: the prefix
    caches on Opus 5 (512) and does not on Haiku 4.5 (4096), and the tokens saved by
    caching on Opus are worth far less than moving the calls to Haiku."""
    import json as _json

    from app.config import min_cacheable_tokens
    from app.llm import prompts
    from app.models.schema import ExtractedFacts

    prefix = (len(prompts.EXTRACT_SYSTEM) + len(prompts.EXTRACT_USER)
              + len(_json.dumps(ExtractedFacts.model_json_schema()))) // 4
    assert 1000 < prefix < 2000
    assert prefix > min_cacheable_tokens("claude-opus-5")
    assert prefix < min_cacheable_tokens("claude-haiku-4-5")
