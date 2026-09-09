# Claude Code — the cost problem, and two rows that cannot be shown

Repo: `C:\Users\admin\Desktop\competitor_analysis-agent`

## The finding

₹2,135 per scan. ₹85,416 for one builder with 40 projects a month. **That is not a viable
feature and everything else waits behind it.**

The shape is favourable:

```
extract   210 calls   1,214,790 input tokens   ₹1,811   85% of the bill
resolve    13 calls       5,200 tokens            ₹12
narrate     1 call        1,250 tokens             ₹4
search + places                                  ₹308
```

One node, one kind of call, 5,785 input tokens each. Whole page text is going to the model so
it can read six fields off it. **Reading named fields out of a page is the cheapest thing an
LLM does** — it does not need the most capable model, and it does not need the whole page.

Two other things from your report, both accepted:

- Your correction on the 5-eligible / 2-COMPARABLE premise is right. That was a no-model
  replay, not an Anthropic run. My brief was wrong about it.
- `run_scan(llm=None)` falling through to `get_llm()` and billing on every "offline" test is
  the best find of the session. It explains where the ₹2,200 test budget actually went.

## Scope discipline

- **No new modules** unless a task below names one. None do.
- **No new abstractions.**
- **No refactoring outside the tasks below.**
- **Do not weaken the honesty rules to save tokens.** Refuse, never correct, stands. If a cost
  saving would make the agent guess, it is not a saving.

Frozen: `app/logic/compare.py`'s output shape, the graph topology, and the rule that no value
is ever written that was not read from a source.

**Budget:** roughly ₹1,000 is available for this brief, so measure on the cheap model, not the
expensive one. Say before you spend it what you are about to spend it on.

---

## Task 1 — Which model is extraction using, and why is it that one?

**Answer this before writing any code.** Report the model id `extract` calls today, its
per-million input price from `app/config.py`, and what the same 210 calls would cost on each
cheaper model available on the same API.

Then move extraction to the cheapest model that can do the job, keeping the current model for
`resolve` — entity matching is a judgement call, field reading is not.

**Measure the quality delta, do not assume it.** Run one scan on the cheap model against a
suburb already in the fixture corpus, and diff the extracted fields against what the
expensive model produced:

- fields the cheap model got that the expensive one also got
- fields it missed
- **fields it got wrong** — this is the only one that matters, because a wrong value is a lie
  and a missing value is an absence reason

If wrong-value count is zero and misses are few, the swap is free money. If it hallucinates a
single figure, stop and report it — cost is not worth the honesty rule.

---

## Task 2 — Stop sending the whole page

The deterministic extractors already fill most fields. The model should see only what they
could not resolve.

Two changes, in this order, measuring after each:

**Send the relevant span, not the document.** The page text handed to the model gets trimmed to
the region around the field terms being looked for. Keep a generous window — the goal is
cutting 20,000 characters of nav, footer and cross-sell, not squeezing the last token.

**Do not call the model for a field a deterministic extractor already resolved.** 18 of 56
Borivali candidates reached 6/6. Report how many model calls that alone removes.

**Report input tokens per call before and after.** Target: under 2,000.

---

## Task 3 — Prompt caching on the stable prefix

210 calls in one scan share the same instructions and the same subject project. That prefix is
identical every time and is being paid for 210 times.

Use the API's prompt caching for the stable portion. **Do not restructure the prompt to make
more of it cacheable** — cache what is already stable, measure, report.

---

## Task 4 — Two rows that cannot be shown to anyone

```
#1  Sanghvi Horizon FAQs      <- page-title debris in the display name
#4  The Pant Project Store    <- a clothing shop, ranked 4th, carrying 6 RERA numbers
```

The second one is the serious one. A retail store in a competitor table is not a ranking bug;
it is the row that makes a reader distrust the other nine.

**Three independent signals, and I want your read on which is load-bearing:**

- **Places type.** If a candidate has a `place_id` and Places reports a non-residential type
  (`clothing_store`, `shopping_mall`, `hospital`, `school`, `restaurant`), it is not a
  competitor. This is the principled fix — it uses data rather than guessing from a name.
  Check whether the type is being captured at all today; if it is discarded at discovery, that
  is the actual bug.
- **RERA count.** Six RERA numbers on one candidate means the page was a register listing, not
  a project page. A project has one or a small number of phases. Report the distribution
  across the corpus before picking a threshold.
- **Name words.** `Store`, `Showroom`, `Mall`, `Hospital`, `School`, `Clinic`. Weakest of the
  three and easiest to get wrong — `Mall` appears in road names. Use it last, if at all.

Route matches to `also_found` with a reason, never to `competitors`, and never silently
dropped.

**Name hygiene**, same file as `clean_project_name`: strip `FAQs`, `Reviews`, `Price`,
`Floor Plan`, `Brochure` and a trailing `|` segment. Display only — identity stays on the raw
slug, both existing guards stand.

**Acceptance:** `The Pant Project Store` leaves the competitor table; `Sanghvi Horizon FAQs`
displays as `Sanghvi Horizon`. Confirm no real project moved with them.

---

## Verify

```bash
uv run pytest -q                       # all green; no test weakened to pass
uv run python scripts/replay.py borivali-west-2026-09-08
```

The replay must still produce the same eligible set apart from the rows Task 4 deliberately
removes. **If a Task 1 or 2 change alters the offline replay output, something that should
have been deterministic was not — that is a finding, report it.**

## Report back with

1. **Cost per scan after each task, cumulative.** One line per task, so the leverage is
   visible. State the target you think is reachable.
2. The cheap-model quality diff — got / missed / **wrong**
3. Input tokens per call, before and after Tasks 2 and 3
4. Which of the three non-residential signals actually caught The Pant Project Store, and
   whether Places type is available at all
5. What you spent producing this report

Report the numbers as they came out. A cost target hit by making the agent guess is a failure,
not a result.
