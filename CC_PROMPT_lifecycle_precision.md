# Claude Code — lifecycle precision, scalar conflicts, resolve latency

Repo: `C:\Users\admin\Desktop\competitor_analysis-agent`

## Decision, recorded

**Only `new_launch` and `under_construction` are competitors.** `ready`, `completed` and
`resale` are never shown, never in `competitors`, never in a second band. This is settled —
do not add a config flag to relax it, and do not propose one.

## What that changes

The lifecycle call is now **load-bearing**. Before, a wrong "ready" was a cosmetic
misordering. Now it silently deletes a real competitor and nobody can tell.

Borivali cold: 55 candidates dropped, **45 of them as ready or resale**. If even ten of
those forty-five are misclassified, the table is missing ten genuine competitors and there
is no signal anywhere that says so.

So: do not touch the policy. Attack the accuracy of the classification, in this order.

## Scope discipline

Unchanged, and it keeps working:

- **No new modules.** This document names none.
- **No new abstractions** — no strategy classes, no rule engines, no config frameworks.
- **No refactoring outside the tasks below.**
- Smallest change that moves the number.
- **No telemetry.** Separate task, separate scope.

Frozen: `app/logic/compare.py`'s output shape, the graph topology, and the rule that no
value is ever written that was not read from a source. **Refuse, never correct.**

---

## Task 1 — Audit the lifecycle call. Change nothing yet.

**Do this first and report before writing any fix.**

For every candidate dropped as `ready`, `completed` or `resale` in the Borivali West and
Kandivali East runs, dump one row:

```
name | lifecycle | the exact source text that produced it | source domain | possession value (or absent)
```

I want to see the evidence, not a count. The question this answers is whether the drops
are correct. If forty-five of forty-five are genuinely finished buildings, the funnel is
working and the table is just honestly small in a mature suburb — that is a finding and I
want to hear it plainly. If a third of them are portal badges on cached pages, tasks 2 and
3 are the whole job.

**Do not fix anything based on what you find until the table is in front of me.**

---

## Task 2 — A future possession date beats a status string

A portal's "Ready to Move" badge is marketing copy on a page that may be two years stale.
A possession date is a stated fact with a source.

**Rule, applied in the extract/merge path where lifecycle is decided:**

- Possession is in the future by more than `lifecycle_possession_margin_months` **and**
  status says ready/completed → the status is stale. Classify `under_construction`, and
  record the override in provenance so the card can show it.
- Possession and status agree → no change.
- Possession is absent → the status stands. **Do not infer a lifecycle from nothing.**
- Possession is in the past and status says under construction → leave it. A delayed
  project is still a competitor, and the date is the thing more likely to be stale here.

```python
lifecycle_possession_margin_months: int = 6
```

**Acceptance:** a fixture with `possession = Dec 2028` and `status = "Ready to Move"` comes
out `under_construction` with the override visible in provenance. A fixture with no
possession and `status = "Ready to Move"` stays `ready`.

---

## Task 3 — Unknown is not ready

Any candidate whose lifecycle could not be determined must **not** be swept into the
ready/resale drop. That is the same failure as inventing a value, pointed the other way.

Route it to `also_found` with a new reason:

```python
"LIFECYCLE_UNKNOWN"   # no lifecycle could be read from any source; not classified,
                      # not dropped as finished
```

It stays visible and countable. Report the count for both localities — if it is large,
the classifier has a coverage problem rather than an accuracy one, and that is a different
fix.

**Acceptance:** a candidate with no status and no possession lands in `also_found` with
this reason, not in `dropped`.

---

## Task 4 — Scalar conflicts pick the best source instead of withdrawing

`app/logic/conflicts.py`.

The set-union fix rescued 62 projects' configurations because sets union — two sources
saying "1, 2 BHK" and "2, 3 BHK" were never disagreeing. **Scalars are different: they can
genuinely conflict, and today we withdraw the field entirely.**

That is over-cautious for `possession` and `rate_psf`. `SOURCE_PRIORITY` already exists and
already encodes which sources we trust.

**Rule:** when two or more sources give different scalar values, take the value from the
highest-priority source and publish it, with the disagreement stated beside it:

```
Possession: Dec 2028   (rera; squareyards says Jun 2029)
```

This is not correcting. Every published number is still a number a named source actually
stated — that is the honesty rule intact.

**Keep the refusal for exactly two cases:**

1. The top two sources are the **same priority** and disagree beyond the ratio threshold —
   there is no basis to pick, so `SOURCES_DISAGREE` stands.
2. The existing `IMPLAUSIBLE_RATE` and `SHARED_ACROSS_PROJECTS` rules, which are about the
   value being wrong rather than the sources differing. Those are untouched.

**Acceptance:** a possession conflict between `rera` and `squareyards` publishes the RERA
date with the disagreement noted. A conflict between two equal-priority portals still
returns `SOURCES_DISAGREE`. Report how many fields this rescues in each locality.

---

## Task 5 — `resolve` still costs 45–65s

`app/logic/resolve.py`.

The prefilter took Worli from 99.4s to 33.5s, but it climbed back to 64.9s on Kandivali at
the wider radius. It scales with the candidate list and the whole Mumbai suburbs is the
target.

The surviving pairs after the prefilter are independent LLM calls run one at a time. Run
them concurrently, bounded by a new setting:

```python
resolve_concurrency: int = 8
```

Nothing else changes — same prefilter, same cap, same "when in doubt, treat as different".

**Target: `resolve` under 20s on a cold locality.**

---

## Verify

```bash
uv run pytest -q                # all green; no test weakened to pass
```

Then **one cold run** on a suburb neither of us has touched — Goregaon West or Chembur.
Cold is the only number that means anything; every production scan is cold.

Confirm the frozen contract survives: three columns, `rate_axis.off_axis[0].reason`,
`config_matrix.types`.

## Report back with

1. **The Task 1 audit table**, before any fix — this is the one I most want to see
2. How many candidates Task 2 reclassified from ready to under construction, **by name**
3. The `LIFECYCLE_UNKNOWN` count in each locality
4. How many scalar fields Task 4 rescued, and which sources won
5. Per-stage cold seconds, `resolve` before and after

Report the numbers as they came out. If Task 2 reclassifies nothing because the audit shows
the drops were all correct, say so — that is the answer, and it means the table is honestly
small rather than broken.
