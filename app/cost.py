"""What a run cost, where it was spent, and how long it took.

Three decisions depend on the number. Caching policy: at Rs 110 a run you cache, at
Rs 2 you do not. Whether the next month of scanning fits a budget, which needs the
cost per scan and the tokens per candidate, not a monthly total after the fact. And
the UI needs "as of 7 Sep 2026" on a stored analysis, because search results vary
between runs -- two scans returning different competitors reads as a broken product
unless the report is dated.

`estimated_inr` is local arithmetic from call counts and the price table in
config.py. It is an ESTIMATE, not a bill: it does not see the provider's invoice,
cached-token discounts, or a page fetch that failed after we were charged for it.

Tracing lives here too, because it carries the same numbers. Spans are emitted only
when OTEL_EXPORTER_OTLP_ENDPOINT is set; with it unset the SDK's no-op tracer runs
and nothing is exported, so pointing a collector at this needs no code change.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler
from opentelemetry import trace

from app.config import llm_price, settings

TAVILY_HOSTS = ("api.tavily.com",)
PLACES_HOSTS = ("places.googleapis.com", "maps.googleapis.com")

# The three jobs the model does, named for the graph node that asks for each.
LLM_STAGES = ("resolve", "extract", "narrate")


class UsageCounter(AsyncCallbackHandler):
    """Token usage, read off whatever the provider sent back.

    A callback rather than `include_raw=True` so the structured-output parsing path
    is untouched: if a provider reports nothing, the count stays 0 and says so
    rather than the extraction breaking.

    One counter per stage, handed to the calls of that stage, so the split is exact
    under concurrency instead of depending on a shared "current stage".
    """

    def __init__(self) -> None:
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    async def on_llm_end(self, response, **kwargs) -> None:  # noqa: ANN001
        usage = (getattr(response, "llm_output", None) or {}).get("usage") or {}
        if not usage:
            for gen in getattr(response, "generations", []) or []:
                for g in gen:
                    usage = getattr(getattr(g, "message", None), "usage_metadata", None) or {}
                    if usage:
                        break
                if usage:
                    break
        self.input_tokens += int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        self.output_tokens += int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)

    def body(self, model: str) -> dict[str, Any]:
        return {"calls": self.calls, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "estimated_inr": tokens_inr(model, self.input_tokens, self.output_tokens)}


def tokens_inr(model: str, input_tokens: int, output_tokens: int) -> float:
    in_usd, out_usd = llm_price(model)
    return round((input_tokens / 1_000_000 * in_usd + output_tokens / 1_000_000 * out_usd) * settings.inr_per_usd, 2)


@dataclass
class Cost:
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    searches: int = 0
    pages_fetched: int = 0
    places_calls: int = 0
    model: str = ""
    candidates: int = 0                                        # how many the tokens were spent on
    by_stage: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def estimated_inr(self) -> float:
        usd = (self.searches * settings.search_usd_per_call + self.places_calls * settings.places_usd_per_call)
        return round(tokens_inr(self.model, self.input_tokens, self.output_tokens) + usd * settings.inr_per_usd, 2)

    @property
    def tokens_per_candidate(self) -> int:
        """The number that projects: a scan's cost scales with how many candidates it
        researched, not with the suburb."""
        return round((self.input_tokens + self.output_tokens) / self.candidates) if self.candidates else 0

    def body(self) -> dict[str, Any]:
        return {
            "llm_calls": self.llm_calls, "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens, "searches": self.searches,
            "pages_fetched": self.pages_fetched, "places_calls": self.places_calls,
            "estimated_inr": self.estimated_inr,
            "model": self.model, "candidates": self.candidates,
            "tokens_per_candidate": self.tokens_per_candidate,
            "by_stage": self.by_stage,
            "note": "estimated from call counts and the configured price table, not a bill",
        }


def classify(urls: list[str]) -> tuple[int, int, int]:
    """(searches, places_calls, other_http) from the one door every call goes through."""
    searches = sum(1 for u in urls if any(h in u for h in TAVILY_HOSTS))
    places = sum(1 for u in urls if any(h in u for h in PLACES_HOSTS))
    return searches, places, len(urls) - searches - places


# ----------------------------------------------------------------- tracing
GRAPH_NODES = ("geocode", "discover", "resolve", "extract", "filter", "score",
               "retry_thin", "narrate", "persist")


def tracer():
    """A real tracer when a collector endpoint is configured, the API's no-op otherwise.

    Called once per run, so an endpoint added to the environment is picked up on the
    next scan without a restart of anything but the process that reads it.
    """
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") and not getattr(tracer, "_installed", False):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": "competitor-analysis-agent"}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        tracer._installed = True                    # noqa: SLF001 - one provider per process
    return trace.get_tracer("competitor-analysis-agent")


class NodeSpans(AsyncCallbackHandler):
    """One span per graph node, with the candidate it ran on and the tokens it spent.

    LangGraph tags every node's chain run with `langgraph_node` in its metadata, so
    one handler passed to the graph covers all nine nodes -- and the fan-out gives
    one `extract` span per candidate, which is the per-candidate attribute the cost
    question needs.
    """

    def __init__(self, llm=None, scan_id: str = "", locality: str = "") -> None:
        self.llm = llm
        self.scan_id = scan_id
        self.locality = locality
        self._open: dict[Any, Any] = {}

    async def on_chain_start(self, serialized, inputs, *, run_id=None, metadata=None, **kwargs) -> None:  # noqa: ANN001
        node = (metadata or {}).get("langgraph_node")
        if node not in GRAPH_NODES:
            return
        span = tracer().start_span(f"node.{node}")
        span.set_attribute("scan.id", self.scan_id)
        span.set_attribute("scan.locality", self.locality)
        span.set_attribute("node.name", node)
        project = (inputs or {}).get("project") if isinstance(inputs, dict) else None
        if project is not None:
            span.set_attribute("candidate.id", getattr(project, "id", ""))
            span.set_attribute("candidate.name", getattr(project, "name", ""))
        self._open[run_id] = (span, node, self._tokens(node))

    async def on_chain_end(self, outputs, *, run_id=None, **kwargs) -> None:  # noqa: ANN001
        entry = self._open.pop(run_id, None)
        if entry is None:
            return
        span, node, before = entry
        now = self._tokens(node)
        span.set_attribute("llm.input_tokens", now[0] - before[0])
        span.set_attribute("llm.output_tokens", now[1] - before[1])
        span.set_attribute("llm.model", getattr(self.llm, "model", "") or "none")
        span.end()

    async def on_chain_error(self, error, *, run_id=None, **kwargs) -> None:  # noqa: ANN001
        entry = self._open.pop(run_id, None)
        if entry is not None:
            entry[0].record_exception(error)
            entry[0].end()

    def _tokens(self, node: str) -> tuple[int, int]:
        counter = getattr(self.llm, "usage", {}).get(node) if self.llm is not None else None
        return (counter.input_tokens, counter.output_tokens) if counter else (0, 0)
