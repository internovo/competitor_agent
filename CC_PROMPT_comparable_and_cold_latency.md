# Claude Code — unblock COMPARABLE, and fix cold-cache latency

Repo: `C:\Users\admin\Desktop\competitor_analysis-agent`

## Where we are

All five gates passed. Malad West reaches 34 of 38 candidates at ≥4 of 6 fields, 16 at 6/6,
none at zero. The plausibility band generalised to Worli without tuning.

Two things are wrong, and the second one I got wrong in the last brief.

**COMPARABLE is zero in both localities.** Sixteen Malad candidates reach 6/6 and every one
is capped to PARTIAL, so `match_score`, `score_max` and `score_excluded` have never run on
live data — which means the comparison screen, the point of the product, has never had a
live input.

**The latency claim was warm-cache.** Malad's 108s came off a cache that two live runs had
already filled. Worli, cold, ran 202s with `resolve` at 99.4s. **This agent is for the whole
of the Mumbai suburbs — every real scan is a cold one**, so Worli's number is the real one
and `resolve` is now the bottleneck, not `extract`.

## Scope discipline

Unchanged, and it has held up well:

- **No new modules.** This document names none.
- **No new abstractions** — no strategy classes, no rule engines, no config frameworks.
- **No refactoring outside the tasks below.**
- Smallest change that moves the number.
- **No telemetry.** Separate task, separate scope.

Frozen: `app/logic/compare.py`'s output shape, the graph topology, and the rule that no
value is ever written that was not read from a source. **Refuse, never correct.**

---

## Task 1 — Replace the COMPARABLE cap

`app/logic/completeness.py`.

I asked for two fixes that solve the same problem, and only noticed after you shipped both.
Task 3 (cap at PARTIAL on any unresolved field) and Task 5 (`score_max` / `score_excluded`)
were both protecting against *"COMPARABLE promising a score that silently omits a weight."*

Task 5 solves it properly — the score now says "38 of 80, price excluded", so nothing is
over-promised. **The cap is redundant, and it costs the entire ranking layer.**

There is also a perverse dynamic in the current rule: more sources produce more
disagreement, so the better the research gets, the rarer COMPARABLE becomes. A label that
degrades as the system improves is the wrong label.

**New rule:**

> COMPARABLE requires 6/6 coverage **and at most one unscoreable dimension.**

Rate is the endemic one — basis mismatch is a fact about the Indian market, not a data
failure — so a project whose only gap is rate should still be COMPARABLE. Two or more, and
PARTIAL is the honest answer.

Keep `unresolved: list[str]` in the payload exactly as it is. Keep `score_max` and
`score_excluded` on every scored project.

**Acceptance, against the current Malad set:**

- `Chandak Treesourus` (unresolved: configurations) → **COMPARABLE**, with a real
  `score` and `score_max`
- `Harshail Hornbill` (rate_psf, possession) → PARTIAL
- `Shreeji Eternity` (configurations, rate_psf, possession) → PARTIAL

---

## Task 2 — `resolve` is the cold-cache bottleneck

`app/logic/resolve.py` and the `resolve` node.

99.4s on Worli, against `extract`'s 75.1s. It is the LLM pair-matcher, asked one question
per ambiguous pair, and the pair count grows with the square of the candidate list.

Three changes, in this order:

**Prefilter before asking the model.** A pair is only a candidate for merging if:

- both have real coordinates and are within `resolve_pair_max_km`, **or**
- their names share at least one distinctive token (not the builder brand alone)

Register rows with placeholder coordinates must fall through to the token test rather than
matching everything — you already discard coordinates shared by more than three projects,
so reuse that signal.

**Cap the model calls.** `resolve_max_llm_pairs`. Beyond the cap, stop asking.

**When in doubt, treat as different.** This is the safe direction and it should be stated
in a comment: a missed merge shows one project twice, which looks untidy. A wrong merge
fuses two projects' data into one row, which is a lie. Never guess toward merging.

`app/config.py`:

```python
resolve_pair_max_km: float = 0.3
resolve_max_llm_pairs: int = 40
```

**Do not batch pairs into one prompt yet.** Measure the prefilter first — if it cuts the
pair count enough, batching is unnecessary complexity.

**Target: `resolve` under 30s on a cold locality.**

---

## Task 3 — Co-operative societies are not new launches

`app/logic/resolve.py`, beside `looks_like_company()`.

Worli's 34 zero-coverage rows were `Amar Nagar CHS`, `Nirlon House`, `Electric Mansion`,
`Venus Apartments` — 1970s co-operative societies with no marketing pages, correctly. They
are not a research failure; discovery in a dense old locality returns *buildings* rather
than *launches*, and every mature Mumbai suburb will do the same.

Add `looks_like_society(name: str) -> bool`, matching `CHS`, `CHSL`,
`Co-operative Housing Society`, `Co-op Housing Society`, `Sahakari`.

Route matches to `also_found` with that reason. **Not researched, not dropped, not in the
competitor table.**

**Do not extend this to `Apartments`, `House`, `Mansion` or `Nagar`.**
`Kalpataru Horizon Apartments` is a real competitor at 4/6, and those other words appear in
live project names. `CHS` is reliable; the rest are not. A false positive here deletes a
genuine competitor silently, which is the expensive failure.

**Acceptance:** `Amar Nagar CHS` and `Vainganga Worli CHS` land in `also_found`;
`Kalpataru Horizon Apartments` stays a competitor.

---

## Task 4 — One rate on many projects is not a project rate

`app/logic/conflicts.py`, alongside the plausibility band.

Five Malad projects came back at exactly **₹22,130**: Simplex KhushAangan, Triumph Tower,
Narang Vivenda, Kamla Jainson, Malwadi Satyam.

One identical figure across five distinct buildings is almost certainly a **locality average
lifted from a portal page and attributed to each project individually**. It is inside the
plausibility band, so that check will never catch it — and it is the dangerous kind of
wrong: specific, plausible, and false.

**Rule:** if an identical rate value appears on `shared_rate_min_projects` or more projects
in one run, none of them may claim it. Each becomes absent with a new reason:

```python
"SHARED_ACROSS_PROJECTS"   # this figure appeared on several projects; it reads as a
                           # locality average, not this project's rate
```

The observed number stays in the evidence with the count, so the card can say *"₹22,130 was
quoted for five projects in this radius and is not treated as this one's rate."*

```python
shared_rate_min_projects: int = 3
```

**Acceptance:** the five 22,130 rows lose their rate with this reason. Two projects sharing
a rate is left alone — coincidence at that size is ordinary.

---

## Verify

```bash
uv run pytest -q                # all green; no test weakened to pass
```

Then **two live runs**, and the second one matters most:

1. **Marina64 / Malad West**, warm cache — proves Tasks 1 and 4 on data we already
   understand
2. **A suburb neither run has touched, cold cache** — Andheri West or Borivali West. **This
   is the real latency number**, because every production scan is a cold one.

Confirm the frozen contract survives: three columns, `rate_axis.off_axis[0].reason`,
`config_matrix.types`.

## Report back with

1. **How many projects reach COMPARABLE**, in each locality, with their `score` and
   `score_max`. **Zero is a failure of Task 1, not a finding.**
2. **Cold-locality per-stage seconds and total** — `resolve` especially, before and after
3. Coverage histogram for the cold locality
4. Which rates the shared-rate rule refused, with the values and counts
5. How many candidates the society filter moved, and a check that no real project went with
   them

Report the numbers as they came out. If the prefilter in Task 2 causes a merge that should
have happened to be missed, I want that in the report rather than a loosened threshold.

## One gate I should have written and didn't

**At least one project must reach COMPARABLE, or the comparison screen never runs.** Add it
to the list. It is the sixth gate and it is the one that was quietly failing while the other
five passed.
