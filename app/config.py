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
    # housing is OFF: housing.com answers every search with an Imperva interstitial
    # (2.7 KB, one <a>, zero project links), so the adapter spent 38 fetches a scan and
    # returned nothing. The adapter is kept; re-enable it behind a real browser.
    sources: str = "maharera,places,osm,tavily,squareyards,builder_site,propog"

    # Which chat model does the extraction / matching / narration work.
    llm_provider: LLMProvider = "anthropic"
    claude_model: str = "claude-opus-5"
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

    # Discovery
    rera_district: str = "Mumbai Suburban"
    discovery_margin_km: float = 0.5  # RERA rows geocoded within radius + margin are kept as candidates
    maharera_max_candidates: int = 25  # nearest N pinned register rows; the circle downtown holds far more

    # Conflict thresholds
    rate_conflict_ratio: float = 1.15  # max/min across sources above this -> "sources disagree"
    possession_conflict_months: int = 3

    # A rate this far from the market anchor is refused, never adjusted. Wide on purpose:
    # a premium tower can legitimately be twice the local median, and narrowing the band
    # would be us deciding what the market is.
    rate_plausible_min_ratio: float = 0.25
    rate_plausible_max_ratio: float = 4.00
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
