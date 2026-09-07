"""What a run cost and how long it took.

Two decisions depend on the number. Caching policy: at Rs 110 a run you cache, at
Rs 2 you do not. And the UI needs "as of 7 Sep 2026" on a stored analysis, because
search results vary between runs -- two scans returning different competitors reads
as a broken product unless the report is dated.

`estimated_inr` is local arithmetic from call counts and the unit prices in
config.py. It is an ESTIMATE, not a bill: it does not see the provider's invoice,
cached-token discounts, or a page fetch that failed after we were charged for it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler

from app.config import settings

TAVILY_HOSTS = ("api.tavily.com",)
PLACES_HOSTS = ("places.googleapis.com", "maps.googleapis.com")


class UsageCounter(AsyncCallbackHandler):
    """Token usage, read off whatever the provider sent back.

    A callback rather than `include_raw=True` so the structured-output parsing path
    is untouched: if a provider reports nothing, the count stays 0 and says so
    rather than the extraction breaking.
    """

    def __init__(self) -> None:
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


@dataclass
class Cost:
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    searches: int = 0
    pages_fetched: int = 0
    places_calls: int = 0

    @property
    def estimated_inr(self) -> float:
        usd = (self.input_tokens / 1_000_000 * settings.llm_input_usd_per_mtok
               + self.output_tokens / 1_000_000 * settings.llm_output_usd_per_mtok
               + self.searches * settings.search_usd_per_call
               + self.places_calls * settings.places_usd_per_call)
        return round(usd * settings.inr_per_usd, 2)

    def body(self) -> dict[str, Any]:
        return {
            "llm_calls": self.llm_calls, "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens, "searches": self.searches,
            "pages_fetched": self.pages_fetched, "places_calls": self.places_calls,
            "estimated_inr": self.estimated_inr,
            "note": "estimated from call counts and configured unit prices, not a bill",
        }


def classify(urls: list[str]) -> tuple[int, int, int]:
    """(searches, places_calls, other_http) from the one door every call goes through."""
    searches = sum(1 for u in urls if any(h in u for h in TAVILY_HOSTS))
    places = sum(1 for u in urls if any(h in u for h in PLACES_HOSTS))
    return searches, places, len(urls) - searches - places
