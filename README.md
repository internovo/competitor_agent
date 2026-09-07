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
uv run pytest -q                # 33 tests, fixture mode, no network
uv run uvicorn app.main:app --reload
```

Then:

```bash
curl -X POST 'localhost:8000/own-projects/marina64/scan?mode=fixture'          # screen 1: ranked list
curl        'localhost:8000/competitors/P51800048221'                          # screen 2: one competitor in detail
curl -X POST 'localhost:8000/compare' -H 'content-type: application/json' \
     -d '{"own_id":"marina64","competitor_ids":["P51800048221","rustomjee-crest"]}'   # screens 3-4: side by side
curl -X POST 'localhost:8000/competitors/sheth-nova/fields' -H 'content-type: application/json' \
     -d '{"field":"rera_phases","value":["P51800077777"]}'                     # a rep records a fact from a site visit
```

Interactive docs at `http://localhost:8000/docs`.

## Modes

| `FETCH_MODE` | Network | LLM | What it does |
|---|---|---|---|
| `fixture` (default) | none | not needed | Hand-written Malad West competitor set in `data/fixtures/`. Every stage after discovery runs for real. |
| `replay` | none | needed | Serves every HTTP call from `data/cache/`. Claude still extracts from the cached pages. A cache miss is a loud error. |
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
            "note": "estimated from call counts and configured unit prices, not a bill"},
"extraction": {"deterministic_fields": 31, "llm_fields": 9},
"timing":  {"started_at": "...", "finished_at": "...", "duration_s": 89,
            "per_stage_s": {"extract": 71.3, "discover": 4.1, ...}}
```

`estimated_inr` is local arithmetic from call counts and the unit prices in `app/config.py`. **It is an estimate, not a
bill** - the agent never sees an invoice, so it cannot know about cached-token discounts or a call that was charged for
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
