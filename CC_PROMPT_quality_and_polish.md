# Claude Code — data quality, table shape, latency

Repo: `C:\Users\admin\Desktop\competitor_analysis-agent`

## Where we are

The last live run fixed the research bottleneck. Coverage went from **1 candidate at ≥4 of
6 fields to 26**, and the run went from 499s to 200s. The five real launches we had been
losing are now in the table, five of them at 6/6.

**The failure mode has changed.** The agent used to show nothing. It now shows numbers,
and some of them are wrong:

```
AJMERA BOULEVARD     32,015 – 31,884      min > max
Shreeji Atlantis      3,000/sqft          Malad West market is ~35,000. Off by 10x.
Raghav UTOPIA           295 – 26,412      not a rate at all
```

An empty cell is honest. **₹3,000/sqft in Malad West, shown to a builder, is the agent
lying** — and nobody double-checks a number that is already on the screen.

That is the top risk, and most of this task is about it.

## Cancelled

**Fix 3 from the previous brief (priority research budget) is cancelled. Do not build it.**
It solved a starvation problem that no longer exists — 26 of 38 candidates are now filled.

## Scope discipline

Same as last time, and it held up well:

- **No new modules.** This document names none.
- **No new abstractions** — no strategy classes, no rule engines, no config frameworks.
- **No refactoring outside the tasks below.**
- Smallest change that moves the number. A "more extensible" five-line fix is wrong.
- **No telemetry.** Separate task, separate scope.

Frozen: `app/logic/compare.py`'s output shape (`rate_axis`, `off_axis`, `config_matrix`,
`columns`, …), the graph topology, and the rule that no value is ever written that was not
read from a source.

**One principle governs tasks 2–4: refuse, never correct.** Declining to state a value you
cannot trust is the honesty rule doing its job. Adjusting a number toward what it "should"
be is the one thing this codebase must never do.

---

## Task 1 — Sort the table and cap it at ten

**Do this first — it changes what everyone sees.**

`app/service.py`. Sort `competitors` by:

1. label — `LABEL_ORDER` already exists (`COMPARABLE`, `PARTIAL`, `THIN`, `UNVERIFIED`)
2. **coverage descending** — the fix
3. distance ascending

`app/config.py`: `max_table_rows: int = 10`.

Payload:

```json
{
  "competitors":      [ ...at most 10... ],
  "also_found":       [ ...the rest: name, distance_km, label, completeness only... ],
  "register_filings": [ ...unchanged... ],
  "dropped":          [ ...unchanged, with causes... ]
}
```

**Acceptance:** in the current run this pushes `Kamla Jainson` (1/6) and
`Pradeep Parlikar` (1/6) out of the visible ten and pulls the filled projects up.

---

## Task 2 — Ordering validator on ranges

`app/models/schema.py`, on `RateValue` and `CarpetRange`.

`AJMERA BOULEVARD` shipped `32,015 – 31,884`. Min above max passes straight through to the
payload today.

**Do not silently swap.** If min > max you do not know which number is which. Branch on the
size of the gap, using the threshold that already exists:

- **Spread within `rate_conflict_ratio`** (Ajmera's is 0.4%) — a min/max assignment slip.
  Sort the pair and continue.
- **Spread beyond it** — two sources genuinely disagree. The field becomes
  `SOURCES_DISAGREE` with `span=[lo, hi]`, using the machinery already built for it.

**Acceptance:** a test for each branch. Ajmera's exact pair sorts; a 2x inverted pair
becomes `SOURCES_DISAGREE`.

---

## Task 3 — A project with an unresolved field cannot be COMPARABLE

`app/logic/completeness.py`.

`Raghav UTOPIA` is currently **COMPARABLE at 6/6 with blank configurations and blank
possession**, because completeness counts fields that were withdrawn as
`SOURCES_DISAGREE`. That was a deliberate decision and it stays — dropping the count would
punish a project for us finding more sources.

But the label must not promise what it cannot deliver. `match_score` runs only at
`COMPARABLE`, and a disagreeing dimension cannot contribute to a score, so COMPARABLE
currently advertises a score that silently omits a weight.

**Rule:** a project with one or more unresolved fields is capped at `PARTIAL`, whatever its
count. Add `unresolved: list[str]` to the project payload so the card can say why it
stopped short.

**Acceptance:** Raghav UTOPIA comes out `PARTIAL` with
`unresolved: ["configurations", "possession"]`, and its coverage still reads 6.

---

## Task 4 — Plausibility band on rates: refuse, never correct

`app/logic/conflicts.py` — the existing home for cross-source sanity.

**Anchor:** the subject's own base-rate midpoint. If the subject has no rate, the median of
the eligible set's rates. If neither exists, the check does not run — no anchor, no
judgement.

`app/config.py`:

```python
rate_plausible_min_ratio: float = 0.25
rate_plausible_max_ratio: float = 4.00
```

**Apply to both bounds. If either falls outside the band, the whole rate is refused** —
`Raghav UTOPIA` is `295 – 26,412`, where the max is plausible and the min is not, and half
a rate is not a rate.

A refused rate becomes absent with a new reason:

```python
"IMPLAUSIBLE_RATE"   # a rate was read, but it is outside a believable band for this market
```

The observed number goes in the evidence and the label, so the card can say *"a source
quoted ₹3,000/sqft, which we could not credit against a ₹38,000 market."* **Nothing is
adjusted, nothing is hidden — it is stated and not used.**

Against the current run this should refuse Shreeji Atlantis (0.08x) and Raghav UTOPIA
(0.008x on the lower bound), and **leave Whispering Heights at ₹68,157 alone** (1.8x — a
premium project can legitimately be twice the market, and refusing it would be us deciding
what the market is).

**Acceptance:** tests at 0.08x, 0.008x, 1.8x and 3.9x — the last two must pass through
untouched. A test that the anchor-absent case runs no check at all.

---

## Task 5 — Publish `score_max` beside `score`

`app/logic/match_score.py`.

Rate carries 20 of 100 points and scores zero unless **both** sides quote a base rate. Four
of five comparables in the last run were `all_in` or `undisclosed`. So a score of 38 is
really 38 out of 80 — and a rep reading 38/100 concludes the project is weak when it is
actually unpriceable against theirs.

Return `score` and `score_max`, where `score_max` is the sum of the weights that could
actually be evaluated for this pair. Add `score_excluded: list[str]` naming the dimensions
that were skipped.

**Do not renormalise to 100.** That would make scores from different projects silently
incomparable, which is worse than a small number honestly explained.

**Acceptance:** a pair with no comparable basis returns `score_max: 80` and
`score_excluded: ["rate"]`.

---

## Task 6 — Concurrency and hard timeouts

(This was Fix 4 in the previous brief. Still wanted.)

`app/config.py`:

```python
extract_concurrency: int = 10       # was 4; this work is network-bound, not CPU
fetch_timeout_s: int = 8            # per HTTP fetch
candidate_budget_s: int = 30        # total per candidate, then move on
```

Enforce `candidate_budget_s` with `asyncio.wait_for` around each candidate's research. A
candidate that runs out keeps what it got and takes an absence reason for the rest.
**A timeout is a normal outcome, not an error** — it must never fail the run.

**Target: 200s → about 120s**, which is gate 5.

---

## Task 7 — Clean the display name

`app/logic/resolve.py`.

The table currently reads:

```
Raghav UTOPIA New Launch Project Goregaon W
Shreeji Atlantis by Shreeji Group
AJMERA BOULEVARD
```

Those are SEO page titles. Add `clean_project_name(name, builder, locality) -> str`:
strip `New Launch Project`, `New Launch`, a trailing `by <builder>`, a trailing locality
that duplicates the locality field, and title-case a name that is entirely upper.

**Two guards.** Never return an empty or single-token name — if cleaning would strip it to
nothing, keep the original. And **cleaning is display only**: keep the raw name in
`name_raw` and keep every identity, dedup and matching path on the raw slug, so a cosmetic
change can never merge two different projects.

**Acceptance:** the three names above clean correctly, and a name that is only a builder
brand is returned untouched.

---

## Verify

```bash
uv run pytest -q                # all green; no test weakened to pass
# replay run — the cache is warm and valid again after the last live run
```

Confirm the frozen contract: compare still returns three columns,
`rate_axis.off_axis[0].reason`, and `config_matrix.types`.

Then **two live runs**:

1. Marina64, 1.5 km — gates 1, 2, 3, 5
2. A second locality, Bhandup West or Worli — **gate 4**, which is the one that proves this
   was not tuned to Malad West

## Report back with

1. Coverage histogram, before and after
2. The top 10 table for both localities
3. Per-stage seconds and total, both runs
4. How many rates the plausibility band refused, and which — **with the numbers, so I can
   check the band is not eating good data**
5. Gate results 1–5, as they came out

Report the numbers as they came out. If the band refuses something it should not have, that
is a finding and I want the number, not a widened threshold.
