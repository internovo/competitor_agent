# Claude Code — the Places bill, and one determinism check

Repo: `C:\Users\admin\Desktop\competitor_analysis-agent`

The propOG session is mid-task. Nothing here depends on it, and nothing here spends money.

## Task 1 — Confirm the determinism test exists

The earlier brief asked for a test that replays one fixture ten times and asserts
**byte-identical output**, including every absence reason. Your last two reports covered the
double-work fix and the stage-timing margin, but did not say whether that test exists.

If it does, say so and move on. If it does not, write it — it is the guarantee the whole
no-credit fixture strategy rests on, and without it a real regression and machine noise look
the same.

---

## Task 2 — Audit the Places calls. Measure only, build nothing.

Extraction was the whole problem for two weeks and is now ₹88 of ₹397. **Places is ₹186 at 66
calls and nobody has looked at it once.**

Everything needed is already on disk — the `.cache/places-nearby`, `places-search` and
`places-verify` directories, plus the run logs. Report:

1. **The 66 calls broken down by kind** — nearby, text search, verify, geocode — with the
   per-call price of each and where each is issued from.
2. **Duplicates within one scan.** How many of the 66 are the same query twice? If the answer
   is more than a couple, that is a free saving and worth doing on its own.
3. **Overlap across scans.** Take the Borivali, Kandivali, Goregaon and Malad runs and count
   how many Places queries appear in more than one. **This is the number that matters** — see
   Task 3.
4. **Which calls are locality-scoped and which are project-scoped.** A nearby-search around a
   locality centre is a fact about the locality. A verify of one project's coordinates is a
   fact about that project. They have completely different cache lifetimes.

---

## Task 3 — Then propose a cross-scan Places cache. Do not build it yet.

The hypothesis, and I want it tested against the numbers from Task 2 rather than assumed:

> Places results are locality-scoped and stable. The projects near Marina64 do not change week
> to week, yet every scan pays for them from scratch. A builder with 40 Mumbai projects will
> scan overlapping areas repeatedly, so the overlap grows with the number of projects rather
> than staying flat.

If Task 2 shows real cross-run overlap, propose a cache keyed on the query with a TTL —
**report the TTL you would choose and why**, and what one builder's 40 projects a month would
cost with it versus the ₹15,862 today.

If Task 2 shows little overlap, say so plainly. That is a finding and it ends this line of
work, which is worth more than a cache that saves nothing.

**Constraints on any proposal:**

- The existing page cache already exists. **Reuse its mechanism, do not build a second cache
  layer.**
- A stale Places result must not produce a stale competitor *table*. Say how the TTL interacts
  with a rep clicking "Re-scan" expecting fresh data — a re-scan that returns yesterday's
  answer from cache is a bug, not a saving.
- **No new dependency.**

---

## Not now, listed so it is not forgotten

- **The Haiku quality diff.** First thing when credit returns. Goregaon West, 27 calls, ₹75 on
  Opus and ₹5 on Haiku at the corrected prices. The wrong-value count is the only number that
  matters, and it is checkable against the verbatim evidence substring without a reference
  model.
- **`resolve` concurrency.** Still outstanding, still a latency item rather than a cost one.
- **Name hygiene leftovers** — `FAQs` was fixed; `Reviews`, `Price`, `Floor Plan` and the `|`
  separator were in the same brief. Confirm which landed.

## Report back with

1. Whether the ten-replay determinism test exists and passes
2. The 66 Places calls, by kind, with prices
3. Duplicates within a scan, and across the four scans
4. Your cache proposal with the measured saving — **or a plain statement that the overlap is
   not there**
5. What you spent — I expect nothing
