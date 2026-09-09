# Claude Code — finish what does not need credit

Repo: `C:\Users\admin\Desktop\competitor_analysis-agent`

## The constraint

**The Anthropic budget is exhausted and will not be topped up until the agent is finished.**
No live scan on Anthropic is available. Groq free tier exists but throttles at 8k
tokens/minute and is a smoke test only — **never a measurement**.

This is a real constraint, not a temporary annoyance to work around. Plan for it.

The good news: the agent splits cleanly.

```
Costs credit:   extract (field reading), resolve (pair matching)
Costs nothing:  every deterministic rule, the compare payload, the API surface,
                the sort, the labels, the absence reasons
```

Everything in this brief is in the second column.

## Scope discipline

- **No new modules** except where Task 1 explicitly names one.
- **No new abstractions.**
- **No refactoring outside the tasks below.**
- **Do not spend credit.** If a task looks like it needs a live LLM call, stop and say so
  rather than running it.

Frozen: `app/logic/compare.py`'s output shape, the graph topology, and the rule that no value
is ever written that was not read from a source. **Refuse, never correct.**

---

## Task 1 — Freeze a golden fixture set. Do this first.

**This is the highest-value task in the brief and it should have existed before the budget
went.**

The last Borivali West and Kandivali West runs were on Anthropic and are the best data we
have. The page cache holds ~1,900 real fetched pages. Turn that into a permanent, free
regression surface.

- Capture the **raw LLM responses** from those runs if they are recoverable from the cache,
  keyed the way the extract path keys them, so extraction can be replayed without any API
  call. If POST-body cache keys make that impossible, say so plainly and record the extracted
  `Project` objects as fixtures instead — the deterministic layers downstream are what we need
  to test.
- Store the fixtures under `tests/fixtures/` with the suburb and run date in the path.
- Add a replay entry point that runs the **full deterministic pipeline** — conflicts, merge,
  lifecycle, completeness, compare, sort — over the frozen fixtures with **zero network and
  zero LLM calls**.

**Acceptance:** a command that produces the Borivali West competitor table from fixtures,
offline, in seconds, with no API key set at all. Prove the no-key part — run it with the keys
unset and show it works.

From here on, every task below is verified against this, not against a live run.

---

## Task 2 — Produce the demo table from the stored Borivali run

**No new scan.** The graph has a `persist` node; the Borivali West result is already stored.

Format it exactly as `CC_PROMPT_demo_table.md` specifies — subject row, top 10 with reason
codes in empty cells, provenance for the highest-coverage competitor, the refusals with their
observed numbers, and the run stats.

Use the Anthropic run's numbers: **5 eligible, 2 COMPARABLE.** Label it with the suburb and
the date it was run, so nobody mistakes it for something newer than it is.

If the stored result is not complete enough to build the table, say what is missing rather
than re-running.

---

## Task 3 — Cost accounting, and the telemetry that produces it

This is the number that decides whether more budget arrives. Right now we cannot answer
*"what does one scan cost?"* and that is the first thing anyone will ask.

Add token and cost accounting to the run:

- Input and output tokens per node — `extract` and `resolve` at minimum
- Tokens per candidate
- Total cost per scan, computed from a per-model price table in `app/config.py`
- All of it in the run stats output, and persisted with the run

Then the OpenTelemetry work that has been deferred four times, since it is the same plumbing:
spans per node, per-candidate attributes, and the token counts as span attributes. Wire it so
a collector endpoint can be pointed at it later without further code changes. **Do not stand
up a collector or a Langfuse instance** — that is deployment, not this task.

**Report:** the per-scan cost of the stored Borivali West run, broken down by node, and the
projected cost of one builder with 40 projects scanned monthly.

---

## Task 4 — The four deterministic fixes, verified offline

From `CC_PROMPT_carpet_and_discovery.md`. All four are deterministic and all four are now
verified against Task 1's fixtures instead of a live run.

1. **Carpet plausibility band** — `carpet_plausible_min_ratio: 0.25`,
   `carpet_plausible_max_ratio: 4.00`, anchored on the subject's own carpet midpoint, both
   bounds, whole range refused, `IMPLAUSIBLE_CARPET`. Report every range it refuses **with the
   numbers**, and your judgement on whether any was legitimate — a genuinely larger competitor
   is a real comparison and must not be deleted.
2. **`looks_like_company` suffixes** — add `Company`, `Group`, `Builder`, `Constructors`,
   `Contractors`, `Estates`, `Properties`, `Ventures`, `Projects`, `Housing`, and a truncated
   `Limited` match (`Lim`, `Limite`). The whole-distinguishing-tail guard stands and matters
   more with a longer list. Test the false-positive direction hard.
3. **Geocode audit** — for every candidate dropped as `no coordinates`, log the name, what
   discovery provided, and whether a geocode was attempted or never issued. **Report the list.
   Do not fix it yet** — attempted-and-failed and never-attempted are different fixes.
4. **`clean_project_name`** — strip `|` alongside `-` and `–`, and `Residential project`
   alongside `New Launch Project`. Display only; identity stays on the raw slug.

---

## Task 5 — Say what still needs credit

At the end, list every remaining piece of work that **cannot** be finished without live
Anthropic calls, and for each one say roughly how many scans it needs to validate.

That list is what justifies the next budget request, so make it specific. "Needs more testing"
is not an answer; "three cold scans across two suburbs, ~X tokens each, to confirm the carpet
band does not delete legitimate large competitors" is.

---

## Verify

```bash
uv run pytest -q                # all green; no test weakened to pass
# the Task 1 offline replay, run with API keys unset
```

Confirm the frozen contract on the replayed output: three columns,
`rate_axis.off_axis[0].reason`, `config_matrix.types`.

## Report back with

1. Whether raw LLM responses were recoverable for replay, or whether fixtures start at the
   extracted-`Project` layer — and what that costs us in test coverage
2. The demo table from the stored Borivali West run
3. **Cost per scan**, by node, and the 40-project monthly projection
4. Carpet ranges refused, with numbers
5. The Task 5 list — what still needs credit, and how much

Report the numbers as they came out. **Do not spend credit to produce any of this.**
