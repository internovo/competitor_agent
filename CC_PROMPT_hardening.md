# Claude Code — harden the propOG Competitor Analysis Agent for production

## Where you are

You are working in **`C:\Users\admin\Desktop\competitor_analysis-agent`** — a LangGraph +
FastAPI service that finds residential projects competing with a builder's own project.

There is a **donor repo** at **`C:\Users\admin\Desktop\competitor-agent-demo`**. It is a
throwaway spike that answered one question — *do deterministic field extractors work
against real Indian property pages?* — and answered it **yes**, with 309 passing offline
tests. You will be porting code out of it. Read it, do not modify it.

Both repos were reviewed and the LangGraph one was chosen to carry forward, because its
`/compare` payload already matches the agreed UI: `rate_axis` with `off_axis`,
`config_matrix` across every BHK, three columns (own + two competitors).

Your job is the seven changes below. **The graph topology does not change. The compare
payload shape does not change.**

---

## Non-negotiables

Violating any of these fails the task.

1. **`app/logic/compare.py`'s output shape is frozen.** `rate_axis`, `off_axis`,
   `config_matrix`, `columns`, `possession`, `carpet`, `structure`, `amenities`,
   `insights` keep their exact keys and nesting. The frontend is designed against them.
2. **The graph keeps its nodes and edges.** `geocode → discover → resolve → [fan-out]
   extract → filter → score → [conditional] retry_thin → narrate → persist`. You are
   changing what nodes *do*, never the wiring.
3. **Claude never decides eligibility, never invents a number, never ranks.** It reads
   pages and writes one sentence per card from numbers already computed. That boundary
   exists in the code today — keep it.
4. **Never write a value that was not observed in a source.** An estimate, an inference
   from an adjacent number, or a plausible default is a defect, not a fallback.
5. **Do not tune anything to make a number look better.** If a change makes coverage
   drop, report the drop.

---

## Task 1 — A missing field must say why it is missing

### Why

`FieldValue` is `{value, prov}` today, and an absent field is an empty list. Three
different facts collapse into one:

- we read four pages and none stated the rate
- three sources disagreed by 33% so we will not pick one
- we never found a page for this project at all

The UI has to print "Not on file" for all three, which makes it a guess. The donor repo
solved this: a cell **cannot be constructed** as unknown without carrying a reason.

Read `competitor-agent-demo/agent/models.py` — the `Cell` dataclass and its
`__post_init__`. That is the pattern.

### What to build

In `app/models/schema.py`:

```python
AbsenceReason = Literal[
    "NOT_PUBLISHED",        # we read a source and it does not state this
    "NO_RERA_ON_FILE",      # needs a RERA record and we have no registration number
    "RATE_NOT_PUBLISHED",   # pages found, none quote a per-sqft rate
    "UNPARSEABLE_DATE",     # a possession phrase was found but is not a date
    "SOURCES_DISAGREE",     # values differ beyond the configured threshold
    "NOT_FOUND",            # no source produced anything for this field
]
```

Introduce a wrapper the API returns for every one of `COMPLETENESS_FIELDS` plus
`amenities` and `timeline`:

```python
class FieldReport(BaseModel, Generic[T]):
    values: list[FieldValue[T]] = []      # every observation, one per source
    absent: AbsenceReason | None = None
    label: str | None = None              # human sentence for the UI
    span: list[Any] | None = None         # SOURCES_DISAGREE only: the spread
```

Enforce, with a Pydantic model validator:

- `values` empty **and** `absent is None` → raise. There is no third state.
- `values` non-empty **and** `absent is not None` → raise.
- `absent == "SOURCES_DISAGREE"` **and** `span is None` → raise.

`label` is generated, never hand-written at call sites — one function
`absence_label(field, reason, span)` so the wording cannot drift between fields.

### Where absence gets decided

`app/logic/conflicts.py` already detects disagreement (`rate_conflict_ratio`,
`possession_conflict_months`). Extend it to *produce* the absence rather than only flag
it: when sources disagree beyond threshold, the field becomes
`absent="SOURCES_DISAGREE"` with `span=[min, max]`, and the existing `Conflict` entry is
kept as-is for the detail card.

`Project.display()` and `Project.value()` keep working — they read `values` and return
`None` when absent.

### Acceptance

- Constructing a `FieldReport` with no values and no reason raises `ValidationError`.
- A scan payload for a THIN project shows a reason on every empty field.
- `tests/test_absence.py` covers all six reasons and both validator rejections.

---

## Task 2 — Deterministic extraction first, Claude second

### Why

Every field on every page currently goes through the LLM. The donor repo has regex
extractors for exactly the fields that matter most, with 309 offline tests proving they
handle real Indian portal text — including the traps documented in
`competitor-agent-demo/.context/working.md`:

- `\b` does not guard a decimal, so `2.5 BHK` parsed as `5 BHK`
- substring matching put a phantom "Spa" on nearly every project, from the word "space"
- "residential and commercial spaces" excluded live residential projects
- first-match-wins abandoned the rest of a page after one rejected candidate

An LLM prompt that says "never estimate" is a request. A regex with a test is a
guarantee. Use the guarantee where one exists.

### What to build

Create **`app/extract/deterministic.py`**. Port these from
`competitor-agent-demo/agent/extract.py`, keeping the logic byte-identical wherever
possible and adapting only the return type from the donor's `Cell` to `FieldReport`:

| Donor function | Fills |
|---|---|
| `extract_configurations` | `configurations` |
| `extract_carpet` | `carpet_sqft` |
| `extract_rate` | `rate_psf.min/max` |
| `extract_price_basis` | `rate_psf.basis` |
| `extract_possession` | `possession` |
| `extract_building_type`, `extract_tower_count`, `extract_floors`, `extract_land_area`, `extract_total_units` | `structure` |
| `extract_rera` | `rera_phases` |
| `extract_amenities` | `amenities` |
| `extract_developer` | `builder` |
| `classify_lifecycle`, `vote_lifecycle` | `status` |

Also port `agent/portals.py` — **portal spec tables outrank anything read from prose**,
and that rule is why the donor's numbers are trustworthy.

Then change the `extract` node in `app/graph/nodes/pipeline.py`:

```
for each page:
    1. deterministic.extract(page.text)      # pure, no network, microseconds
    2. fields still absent  →  send to Claude, with the page text
    3. merge:  deterministic wins on conflict, and says so
```

Every `FieldValue` gains `method: Literal["deterministic", "llm", "manual"]`.

**`app/llm/prompts.py` changes too:** the extraction prompt must be told which fields
are already known and asked only for the rest. Do not ask a model for an answer you
already have.

### Acceptance

- A page whose text the donor tests cover produces `method: "deterministic"` for those
  fields and makes **zero** LLM calls for them.
- Scan a fixture set and print the split: how many fields deterministic, how many LLM.
  Expect a large majority deterministic on portal pages.
- Token use per scan drops measurably. Report the before and after.

---

## Task 3 — `/scan` becomes asynchronous

### Why

`POST /own-projects/{id}/scan` blocks until the graph finishes. A live scan takes
minutes. Azure App Service and the browser both time out long before, so the current
endpoint cannot be called from the product at all.

The UI has a running state with an elapsed-seconds counter. It needs something to poll.

### What to build

```
POST /scans                       → 202 { run_id }
GET  /scans/{run_id}              → { status, stage, stages_done, elapsed_s, ... }
```

`status` is `queued | running | done | failed`. While running, return the graph's own
`log` entries as `stages_done` — the node names are already meaningful
(`discover[maharera]: 18 candidates`), so the loading screen can show real progress
instead of a spinner.

When `done`, the same body carries the full scan payload. When `failed`, it carries
`error: { type, message, stage }`.

Run the graph with `asyncio.create_task`; hold state in the run store from Task 6.

### Acceptance

- `POST /scans` returns within 200 ms with a `run_id`.
- Polling shows `stage` advancing through the graph.
- A completed run returns the same payload the old synchronous endpoint returned.

---

## Task 4 — Failure must be loud

### Why

**This is the most dangerous defect in the repo.** If the LLM is rate-limited or the key
is wrong, extraction fails on every page, and the scan **succeeds** — returning projects
with every field unknown.

To a sales rep that reads as "there is no data on these competitors." It is not. It is a
broken run wearing the costume of an empty one. A builder shown that loses trust in the
product, and nobody will know why.

### What to build

In `app/llm/client.py`, distinguish three outcomes and do not conflate them:

| Outcome | Meaning | Effect on the run |
|---|---|---|
| Page genuinely says nothing | a real absence | field absent with a reason |
| Auth error (401/403) | misconfiguration | **fail the run immediately** |
| Rate limit after retries exhausted | capacity | **fail the run** |

Track, per run, `llm_calls_attempted` and `llm_calls_failed`. In the `narrate` node,
before persisting: if `llm_calls_failed > 0` and more than 25% of attempts failed, the
run ends `failed` with `error.type = "extraction_degraded"`.

Never let a partially-extracted run present itself as a complete one.

### Acceptance

- With a deliberately invalid API key, `POST /scans` → run reaches `failed`, not `done`,
  and the error names the cause.
- With a valid key, behaviour is unchanged.
- A page that truly states nothing still yields absence reasons, not a failed run.

---

## Task 5 — Port the donor's extractor tests

### Why

33 tests today, on the code that decides what a builder sees. The donor has 309, all
pure `text → value`, no network, running in half a second.

### What to build

Port from `competitor-agent-demo/tests/`:

- `test_extract_configs.py`
- `test_extract_basis.py`
- `test_extract_possession.py`
- `test_invariants.py` — adapt to `FieldReport`

Rewrite assertions against `FieldReport` rather than the donor's `Cell`. **Do not weaken
a test to make it pass.** If a ported extractor genuinely behaves differently here, fix
the extractor or record why the difference is correct.

Keep the donor's discipline, stated in its own docs: *test the refusals, not just the
hits — every real bug here was a false positive.*

### Acceptance

- `uv run pytest -q` passes with roughly 340 tests, still under two seconds.
- No test was modified to accommodate a failure.

---

## Task 6 — The agent stops owning storage

### Why

The agent has a SQLite database and a table of "own projects". Both are wrong for
production, for one reason:

> **The agent must never learn what a builder is.**

Tenancy is enforced in the Node API, in one function, against Postgres. If the agent also
stores projects, there are two places a builder's data lives and two places that boundary
can be got wrong. The API asks a question; the agent answers it; the API decides who may
see the answer and stores it.

This also settles the SQLite-versus-Postgres question: the agent gets neither.

### What to build

Delete `app/storage/db.py` and every `Database` usage. Replace with
**`app/storage/runs.py`** — an in-memory `RunStore`:

- `dict[run_id, RunRecord]`, bounded (default 50 runs), TTL (default 2 hours)
- holds status, stage log, timings, cost, and the finished payload
- lost on restart, which is correct — the canonical copy lives in Postgres

The subject arrives **in the request**, not from a lookup:

```
POST /scans
{ "own": { ...OwnProject... }, "radius_km": 1.5, "mode": "live" }
```

Endpoints become run-scoped:

```
POST /scans
GET  /scans/{run_id}
GET  /scans/{run_id}/competitors/{cid}
POST /scans/{run_id}/compare                  { "competitor_ids": [...] }
POST /scans/{run_id}/competitors/{cid}/fields { "field": ..., "value": ... }
GET  /health
```

Keep `mode=fixture` working end to end — it is how the demo runs without keys.

Update `README.md` curl examples to the new routes.

### Acceptance

- No `sqlite3` import anywhere in `app/`.
- `mode=fixture` scan → poll → detail → compare all work against the new routes.
- Restarting the process loses runs and returns 404 for an old `run_id`. That is intended;
  say so in the README.

---

## Task 7 — Report cost and timing on every run

### Why

The donor prints `~90 s, ~₹110` after every run. This one prints nothing.

Two decisions depend on that number. Caching policy: at ₹110 a run you cache, at ₹2 you
do not. And the UI needs "as of 7 Sep 2026" on a stored analysis, because search results
vary between runs — two scans returning different competitors reads as a broken product
unless the report is dated.

### What to build

Count during the run, in the graph state, and return in the payload:

```json
"cost": {
  "llm_calls": 14, "input_tokens": 41200, "output_tokens": 3100,
  "searches": 22, "pages_fetched": 47, "places_calls": 3,
  "estimated_inr": 112.40
},
"extraction": { "deterministic_fields": 31, "llm_fields": 9 },
"timing": { "started_at": "...", "finished_at": "...", "duration_s": 89,
            "per_stage_s": { "discover": 4.1, "extract": 71.3, ... } }
```

`estimated_inr` is local arithmetic from call counts and configured unit prices in
`config.py`. **Label it an estimate, not a bill** — the donor does exactly this and the
honesty matters.

### Acceptance

- Every completed run carries `cost`, `extraction` and `timing`.
- `duration_s` matches wall clock within a second.
- The per-stage breakdown makes it obvious that `extract` dominates.

---

## Configuration note

`app/config.py` already defaults `llm_provider` to `"anthropic"` with
`claude_model = "claude-opus-5"`. Confirm `ANTHROPIC_API_KEY` is set and Groq is unused;
leave the Groq code path in place but do not optimise for it. Update `.env.example` to
make Anthropic the documented default and mark Groq as a fallback with its token ceiling
noted.

---

## Order of work

Do them in this order — each depends on the one before.

1. Task 1 (`FieldReport`) — everything else writes into it
2. Task 2 (deterministic extractors) — the largest change
3. Task 5 (port the tests) — immediately, so Task 2 is verified before you build on it
4. Task 4 (loud failure) — small, and it protects everything after
5. Task 6 (run store, new routes) — restructures the API surface
6. Task 3 (async scan) — needs the run store
7. Task 7 (cost and timing) — counters through the whole graph

Commit after each task with a message that says what changed and why.

---

## Verify before you report done

```bash
uv run pytest -q                                    # ~340 tests, under 2s

uv run uvicorn app.main:app --reload
curl -X POST localhost:8000/scans -H 'content-type: application/json' \
     -d @data/fixtures/scan_request.json            # → 202 { run_id }
curl localhost:8000/scans/<run_id>                  # → status advances, then done
curl localhost:8000/scans/<run_id>/competitors/P51800048221
curl -X POST localhost:8000/scans/<run_id>/compare -H 'content-type: application/json' \
     -d '{"competitor_ids":["P51800048221","rustomjee-crest"]}'
```

The compare response must still contain `rate_axis.off_axis` with a `reason`, and
`config_matrix.types` covering every BHK in the set. If either changed shape, you broke
the contract.

Then, with a deliberately wrong `ANTHROPIC_API_KEY`, run a live scan and confirm the run
ends **failed**, not done.

## Report back with

1. Test count before and after
2. Deterministic-vs-LLM field split on a fixture scan
3. Token use per scan, before and after
4. Anything the ported extractors do differently here than in the donor, and why
5. Any of the seven you could not complete, and what blocked it

Report the numbers as they came out. A worse number is a finding, not something to tune
away.
