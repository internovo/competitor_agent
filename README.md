# propOG Competitor Analysis Agent

Finds every *new* residential project within a radius of a builder's own project, fills a fixed six-field form for each
from MahaRERA, the builder's site, portals and web search, and lets the sales team open one competitor in detail or
compare their project against up to two of them side by side.

**The idea in one breath.** Draw a circle on the map. List every new building inside it. Merge duplicates. For each
building read every page we can find and fill the form, noting where each answer came from and when. Drop the ones that
don't qualify using plain rules. Count the six facts, flag disagreements, score the complete ones, and let Claude write
one sentence per card from numbers we already computed. Claude never decides who qualifies or what the numbers are.

## Run the demo (no keys needed)

```bash
brew install uv                 # once
uv sync                         # creates .venv with Python 3.12
uv run pytest -q                # fixture mode, no network, no keys
uv run pytest -q -m slow        # replays the frozen suburbs, incl. 10 concurrent (~7 min)
uv run uvicorn app.main:app --reload

# the competitor table for a frozen suburb, off the page cache, no keys, no model
uv run python scripts/replay.py --list
uv run python scripts/replay.py borivali-west-2026-09-08
```

## The propOG entry point

`POST /scan` is the route the propOG backend calls, and its body is that client's, not ours:

```
POST /scan            {run_id, project_id, builder_id, radius_km, subject}    -> 202
  headers             content-type: application/json
                      x-agent-token: <AGENT_TOKEN>   checked only when AGENT_TOKEN is set here
                      x-request-id: <id>             recorded in the run log when sent
```

The run is keyed by the caller's `run_id`, so `GET /scans/{run_id}` is addressable with the id
Node already holds. The POST returns in milliseconds because that client aborts after 10 seconds
and reads only the status code.

`subject` is used for what it carries and nothing more. It needs `name` and either coordinates or
a city. Without `carpet_sqft`, `rate_psf`, `possession`, `structure` or `configurations` the scan
still runs; those dimensions are excluded from the match score and its denominator, and the compare
column prints null rather than a default.

Then:

```bash
# A scan is asynchronous: POST returns a run_id and the client polls until status is done.
curl -X POST 'localhost:8000/scans' -H 'content-type: application/json' \
     -d @data/fixtures/scan_request.json                                       # -> {"run_id": ..., "status": "queued"}
curl        'localhost:8000/scans/<run_id>'                                    # screen 1: ranked list, once done
curl        'localhost:8000/scans/<run_id>/competitors/P51800048221'           # screen 2: one competitor in detail
curl -X POST 'localhost:8000/scans/<run_id>/compare' -H 'content-type: application/json' \
     -d '{"competitor_ids":["P51800048221","rustomjee-crest"]}'                # screens 3-4: side by side

# The same comparison with no run in memory. A run lives here two hours and is lost on
# restart; propOG keeps every finished scan forever, so last month's scan compares too.
# `columns` is compare_columns from the stored scan: own first, then one or two competitors.
curl -X POST 'localhost:8000/compare' -H 'content-type: application/json' \
     -d '{"columns":[<own>,<competitor>],"radius_km":1.5}'                     # screens 3-4, statelessly
curl -X POST 'localhost:8000/scans/<run_id>/competitors/sheth-nova/fields' -H 'content-type: application/json' \
     -d '{"field":"rera_phases","value":["P51800077777"]}'                     # a rep records a fact from a site visit

# Or, with no server and no keys: the same four responses for a frozen suburb.
uv run python scripts/replay.py malad-west-2026-09-08 --json marina64.json
```

Interactive docs at `http://localhost:8000/docs`.

## Modes

| `FETCH_MODE` | Network | LLM | What it does |
|---|---|---|---|
| `fixture` (default) | none | not needed | Hand-written Malad West competitor set in `data/fixtures/`. Every stage after discovery runs for real. |
| `replay` | none | optional | Serves every HTTP call from `data/cache/`. With a model, Claude still extracts from the cached pages; with `llm=None` the deterministic half runs alone and the scan costs nothing. A cache miss is a loud error. |
| `live` | yes | needed | Hits MahaRERA, Google Places, Tavily, SquareYards, Housing.com and builder sites, and writes `data/cache/`. |

Copy `.env.example` to `.env` and add `ANTHROPIC_API_KEY`, `GOOGLE_MAPS_API_KEY`, `TAVILY_API_KEY` for replay/live.
Run a live scan once, commit `data/cache/`, and the demo can replay it forever.

## Pipeline

```
geocode -> discover -> resolve -> [extract per project, in parallel] -> filter -> score
        -> (any THIN project? one more search pass -> extract -> score) -> narrate -> persist
```

| Stage | Who does the work | What it does |
|---|---|---|
| discover | MahaRERA register, Places, Tavily, propOG | Candidate names inside the circle (plus a margin) |
| resolve | code, Claude only for ambiguous pairs | "Runwal Vertex" and "Vertex by Runwal" become one project |
| extract | regex extractors first, Claude for the rest | Each page -> `ExtractedFacts`; each fact -> `FieldValue{value, source, url, fetched_at, method}` |
| filter | code | inside radius, new launch / under construction only, possession after today, some BHK overlap |
| score | code | completeness x/6 -> COMPARABLE / PARTIAL / THIN; conflicts; match score for 6/6 only |
| narrate | Claude, template fallback | One sentence per card and per comparison section, from computed numbers only |

A finished scan carries `incomplete` and `sources_unavailable` -- `[{source, status, reason}]` -- when a paid API
(Tavily, Google Places) refused our key or quota. The scan still returns what the other sources found; the client
must show that the list is short because a source did not run, not because the neighbourhood is empty.

A finished scan also carries `compare_columns` -- `{own, competitors:{<id>: column}}` -- the comparable fact set
per project, for the projects a rep can actually analyse. The cards cannot stand in for these: a card carries no
amenities at any depth, no RERA phase list, and two of structure's seven fields, so a comparison built from cards
would quietly answer a different question. Both compare routes run the same code under `columns`, and a test
asserts the two return the same body. Every compare response carries `insight_source` (`llm` or `template`) and a
`cost` block, on the happy path too: an absent field would read as the pessimistic answer and label a real model
insight a template.

The six fields that make a project COMPARABLE: configurations, carpet area, rate per sq ft (with basis), possession,
structure, RERA phase numbers. A field is a `FieldReport`: a **list** of observations, one per source, **or** a reason
there are none. A cell cannot be constructed as unknown without carrying which reason - `NOT_PUBLISHED`,
`NO_RERA_ON_FILE`, `RATE_NOT_PUBLISHED`, `UNPARSEABLE_DATE`, `SOURCES_DISAGREE`, `NOT_FOUND` - so the UI never has to
print one "Not on file" for three different facts. Missing means missing, never estimated.

## Extraction: regexes first, Claude second

An LLM prompt that says "never estimate" is a request. A regex with a test is a guarantee, so `app/extract/` reads every
page first - a portal's own spec table before its prose - and only the fields it could not prove are sent to Claude,
with the known ones named in the prompt. Anything the model answers outside that set is dropped: deterministic wins on
conflict. `FieldValue.method` records which reader produced each value, and every scan reports the split.

```bash
uv run python scripts/extraction_split.py    # how much of the form the regexes fill, per field
```

Match score weights (in `app/config.py`): configuration overlap 25, carpet overlap 20, rate proximity 20 (base rates
only), possession proximity 15, distance 10, structure 10.

## What a run cost

Every completed run reports it, alongside the scan payload:

```json
"cost":    {"llm_calls": 14, "input_tokens": 41200, "output_tokens": 3100,
            "searches": 22, "pages_fetched": 47, "places_calls": 3, "estimated_inr": 112.40,
            "model": "claude-opus-5", "candidates": 39, "tokens_per_candidate": 1136,
            "by_stage": {"extract": {"calls": 12, "input_tokens": 39000, "output_tokens": 1800,
                                     "estimated_inr": 4.22}, "...": {}},
            "note": "estimated from call counts and the configured price table, not a bill"},
"extraction": {"deterministic_fields": 31, "llm_fields": 9},
"timing":  {"started_at": "...", "finished_at": "...", "duration_s": 89,
            "per_stage_s": {"extract": 71.3, "discover": 4.1, ...}}
```

Each stage is priced at the model that ran it: extraction is on `EXTRACT_MODEL` (Haiku 4.5, because reading a labelled
number off a page is not a judgement call) and matching and narration are on `CLAUDE_MODEL`. `estimated_inr` is local
arithmetic from call counts and the price table in `app/config.py`. **It is an estimate, not a bill** - the agent never sees an invoice, so it cannot know about cached-token discounts or a call that was charged for
and then failed. Edit the prices when they move. `timing.started_at` is also what dates a stored analysis: search
results vary between runs, and two scans returning different competitors reads as a broken product unless the report
says when it was taken.

## Layout

```
app/
  main.py            FastAPI routes            app/service.py   scan runner + list/detail/compare payloads
  config.py          settings, weights         app/models/schema.py   the fixed form (Pydantic)
  graph/             LangGraph state, nodes, wiring
  sources/           one adapter per source + fetch.py (live/replay cache)
  llm/               Claude client, prompts, template fallbacks
  logic/             geo, eligibility, completeness, conflicts, match_score, resolve, merge, compare
  extract/           deterministic.py (regex extractors), portals.py (spec tables)
  storage/runs.py    in-memory run store: bounded, TTL, lost on restart
data/fixtures/       marina64.json, competitors_malad_west.json, propog_projects.json, scan_request.json,
                     pages/ (prose pages the extractors are measured on)
data/cache/          every response the live runs fetched; what replay serves
scripts/replay.py    one frozen suburb, offline, no keys, no model -> table + cost + API payloads
tests/fixtures/replay/  one subject per frozen suburb; the free regression surface
docs/                reference screenshots
tests/
```

## Source notes (verified Sep 2026)

- **MahaRERA**: filing moved to MahaCRITI (JS app) in May 2026, and by Sep 2026 the legacy search at
  `projects-search-result` answers a GET with the empty form - it is a Drupal POST flow now, and its "location" field
  wants a pincode, not a locality. Discovery therefore reads `/map-projects-search-result`, which ships every registered
  project (~52.5k rows) in one `#mapLocationData` JSON blob with name, registration number, district and coordinates:
  the only key-free source of *pinned* candidates. Two caveats: rows the promoter never pinned share placeholder
  coordinates (0,0 and one per district office), so a coordinate used by more than three projects is discarded; and
  `projectName` is often the promoter's company name rather than the marketed project name. The summary report behind
  `project-summary-report-detail` answers 200 for any number and echoes it back, so a page is only kept when it does
  not carry the "No records found" banner - in practice it rarely serves one, and facts come from the portals instead.
- **Places API (New)**: types `apartment_building`, `apartment_complex`, `housing_complex`, `condominium_complex`.
  Weak at finding under-construction projects; used for coordinates, pin accuracy and nearest metro.
- **SquareYards** is plain HTML. **Housing.com** needs Chrome TLS impersonation (`curl_cffi`). **99acres** and
  **MagicBricks** block plain requests; they reach us through Tavily extract, or a Playwright adapter can be added
  behind a flag (`uv sync --extra browser`).
- **Google Places** needs a valid key; without one the pipeline falls through to **OpenStreetMap Nominatim**
  (`sources/osm.py`, no key, one call a second) for geocoding only. Nominatim knows finished buildings, not launches,
  so it never discovers candidates.
- **Groq free tier** allows 8k tokens/minute and 200k tokens/day. A scan of eight projects costs roughly 50k, so a few
  runs exhaust the day; the failure shows up as `RateLimitError` on every extraction and an all-`unknown` project list.
  `GROQ_MAX_OUTPUT_TOKENS` (default 2048) keeps a single request from exceeding the per-minute ceiling on its own.
- Scraping portals may conflict with their terms of service. This is a demo.
