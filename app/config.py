"""Runtime settings. Everything tunable for the demo lives here."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
FIXTURE_DIR = DATA_DIR / "fixtures"

FetchMode = Literal["fixture", "replay", "live"]
LLMProvider = Literal["anthropic", "groq"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str | None = None
    groq_api_key: str | None = None
    google_maps_api_key: str | None = None
    tavily_api_key: str | None = None

    fetch_mode: FetchMode = "fixture"
    # The shared secret propOG sends as x-agent-token. Unset means the header is not
    # checked, which mirrors the caller: agent-client.cjs only sends it when its own
    # COMPETITOR_AGENT_TOKEN is non-empty. Set it on both sides in any deployment.
    agent_token: str | None = None
    # housing is OFF: housing.com answers every search with an Imperva interstitial
    # (2.7 KB, one <a>, zero project links), so the adapter spent 38 fetches a scan and
    # returned nothing. The adapter is kept; re-enable it behind a real browser.
    sources: str = "maharera,places,osm,tavily,squareyards,squareyards_list,builder_site,propog"

    # Which chat model does the extraction / matching / narration work.
    llm_provider: LLMProvider = "anthropic"
    claude_model: str = "claude-opus-5"
    # Field reading is not a judgement call: the page either states the number or it
    # does not, and the deterministic extractors have already taken everything they
    # can prove. Entity matching -- "is Vertex by Runwal the same building as Runwal
    # Vertex" -- is a judgement call, and stays on claude_model. Extraction was on
    # Opus only because one client served all three jobs, never because anyone chose it.
    extract_model: str = "claude-haiku-4-5"
    groq_model: str = "openai/gpt-oss-120b"
    # gpt-oss models reason before answering; "low" cuts extraction latency ~10x with no loss of fields.
    groq_reasoning_effort: str = "low"
    # Free-tier Groq throttles on tokens/minute; the SDK honours Retry-After, so ride it out rather than drop the page.
    groq_max_retries: int = 6
    # The free tier's ceiling is 8k tokens/minute counting prompt + completion, so a 16k output reservation
    # rejects every request on its own. The extraction form is a few hundred tokens; 2k is headroom.
    groq_max_output_tokens: int = 2048
    default_radius_km: float = 1.5

    # Runs are held in memory only. The canonical copy of a scan is written by the
    # Node API, against Postgres, where tenancy is enforced in one place.
    max_runs_held: int = 50
    run_ttl_hours: float = 2.0
    # A live scan reuses a cached page only while it is this fresh. Prices and
    # possession dates move; a rep pressing re-scan and being handed last week's
    # table is a bug wearing a saving's clothes. Replay ignores this entirely.
    cache_max_age_hours: float = 24.0
    # Every POST used to spawn a scan immediately. One scan fans out to
    # extract_concurrency fetches per candidate across ~40 candidates, so two reps
    # scanning at once was already hundreds of sockets and no ceiling above that.
    # Scans over the limit wait in `queued`, which is a status the client polls anyway.
    max_concurrent_scans: int = 2

    # Discovery
    rera_district: str = "Mumbai Suburban"
    discovery_margin_km: float = 0.5  # RERA rows geocoded within radius + margin are kept as candidates
    maharera_max_candidates: int = 25  # nearest N pinned register rows; the circle downtown holds far more

    # A possession date this far out contradicts a "ready" badge, and the date is the
    # thing with a source behind it.
    lifecycle_possession_margin_months: int = 6

    # Conflict thresholds
    rate_conflict_ratio: float = 1.15  # max/min across sources above this -> "sources disagree"
    possession_conflict_months: int = 3

    # A rate this far from the market anchor is refused, never adjusted. Wide on purpose:
    # a premium tower can legitimately be twice the local median, and narrowing the band
    # would be us deciding what the market is.
    rate_plausible_min_ratio: float = 0.25
    rate_plausible_max_ratio: float = 4.00
    # Same rule for carpet, anchored on the subject's own midpoint. A genuinely larger
    # competitor is a real comparison, so the band is as wide as the rate band: 4x the
    # subject's midpoint still passes a tower of penthouses next to a compact project.
    # What it catches is a range read off a page that was listing something else.
    carpet_plausible_min_ratio: float = 0.25
    carpet_plausible_max_ratio: float = 4.00
    # One figure quoted for this many projects in a run is a locality average someone
    # attributed to each of them, not any one building's rate.
    shared_rate_min_projects: int = 3

    # Entity resolution. The pair count grows with the square of the candidate list, so
    # the cheap gate runs first and the model only sees what survives it.
    resolve_pair_max_km: float = 0.3
    resolve_max_llm_pairs: int = 40

    # Completeness labels
    comparable_min: int = 6
    partial_min: int = 4

    # Match score weights (sum to 100)
    w_config: int = 25
    w_carpet: int = 20
    w_rate: int = 20
    w_possession: int = 15
    w_distance: int = 10
    w_structure: int = 10
    possession_horizon_months: int = 24

    # Unit prices for the run-cost ESTIMATE. Not a bill: the agent never sees an
    # invoice, so these are list prices at the time of writing and are meant to be
    # edited when they move.
    inr_per_usd: float = 88.0
    # Fallback for a model not in LLM_PRICES below. Pricing an unknown model at zero
    # would read as free, which is the one answer that is certainly wrong.
    llm_input_usd_per_mtok: float = 15.0     # claude-opus-5
    llm_output_usd_per_mtok: float = 75.0
    search_usd_per_call: float = 0.008       # Tavily search / extract
    places_usd_per_call: float = 0.032       # Places API (New)

    # The table a rep reads. Everything else is returned under `also_found`.
    max_table_rows: int = 10

    # Extraction
    max_page_chars: int = 40_000
    extract_concurrency: int = 10     # network-bound, not CPU
    # Measured, not guessed. At 8s/30s a cold Borivali West scan read 1 page for 62
    # candidates -- ConnectTimeout on 37 tavily and 37 builder_site calls -- because 62
    # candidates fan out at once and every clock runs while they queue. At 30s/90s the
    # same locality read 18. Raise the pair together; the fetch timeout alone is not enough.
    fetch_timeout_s: int = 30         # per HTTP fetch
    candidate_budget_s: int = 90      # wall clock per candidate, then move on with what it got
    # How many candidates research at once. Uncapped, ~60 started together and their
    # clocks ran out in our own queue: on 17 Sep, ~370 of ~460 failed fetches across
    # Kandivali, Malad and Goregaon were the 90s budget expiring, and none was a block.
    # A candidate's budget starts when its research does, so waiting here costs nothing.
    candidates_at_once: int = 8

    @property
    def source_list(self) -> list[str]:
        return [s.strip() for s in self.sources.split(",") if s.strip()]

    @property
    def llm_key(self) -> str | None:
        return self.groq_api_key if self.llm_provider == "groq" else self.anthropic_api_key

    @property
    def llm_model(self) -> str:
        return self.groq_model if self.llm_provider == "groq" else self.claude_model

    @property
    def has_llm(self) -> bool:
        return bool(self.llm_key)


settings = Settings()

# Source priority when several sources give a value for the same field. Lower index wins for display.
SOURCE_PRIORITY: list[str] = [
    "manual", "maharera", "propog", "builder_site", "squareyards", "housing",
    "99acres", "magicbricks", "tavily", "places", "osm", "fixture",
]

COMPLETENESS_FIELDS: tuple[str, ...] = (
    "configurations", "carpet_sqft", "rate_psf", "possession", "structure", "rera_phases",
)

# USD per million tokens, (input, output, minimum cacheable prefix in tokens).
# List prices, and meant to be edited when they move -- the agent never sees an
# invoice. The Opus and Sonnet rows were wrong until now: they were written from
# memory at 15/75 and 3/15, which overstated every cost figure this repo has
# reported by roughly 3x. Checked against Anthropic's published table.
LLM_PRICES: dict[str, tuple[float, float, int]] = {
    "claude-opus-5": (5.0, 25.0, 512),
    "claude-sonnet-5": (2.0, 10.0, 1024),
    "claude-haiku-4-5": (1.0, 5.0, 4096),
    "claude-haiku-4-5-20251001": (1.0, 5.0, 4096),   # dated alias of the row above
    "openai/gpt-oss-120b": (0.15, 0.75, 0),
}


def llm_price(model: str) -> tuple[float, float]:
    row = LLM_PRICES.get(model or "")
    return row[:2] if row else (settings.llm_input_usd_per_mtok, settings.llm_output_usd_per_mtok)


def min_cacheable_tokens(model: str) -> int:
    """Below this, a cache breakpoint is accepted and silently does nothing.

    Not monotonic across generations: 512 on Opus 5, 4096 on Haiku 4.5. A prefix
    that caches on the expensive model does not cache on the cheap one.
    """
    row = LLM_PRICES.get(model or "")
    return row[2] if row else 1024


