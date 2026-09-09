# Claude Code — determinism, the Marina64 payload, and the API contract

Repo: `C:\Users\admin\Desktop\competitor_analysis-agent`

## Accepted, with credit

**Your price-table correction.** You caught your own 3x overstatement and republished the
numbers against the published rates rather than letting the old figure stand. ₹997 → ₹397 is
the real headline and it is a good one.

**Shipping the Haiku swap unvalidated and saying so.** That is the right way to handle "the
brief ordered it but the check could not run." Flagged, one line to revert, not buried.

**Tasks 1 and 3 being mutually exclusive.** Haiku's 4,096-token minimum against our
1,533-token prefix means a cache breakpoint would be a line that reads as a saving and delivers
nothing. Declining to add it, and pinning the finding with a test that fires if the model
changes, is better than doing what the brief said.

**Places types were being discarded in `PlacesSource._add`.** The data identifying a clothing
shop was in the response all along. That is the third time this project has found the answer by
opening the actual payload rather than reading a counter.

**Commit all of it before starting this brief.** 379 tests green and nothing committed since
`33762ac` is too much to be carrying.

---

## Task 1 — The Marina64 payload. Do this first, it is blocking another session.

The propOG backend needs a real competitor payload to seed, so the five frontend screens can be
built against real data — including the refusals — without spending a rupee or waiting for the
agent to be deployed.

**It must be Marina64 / Malad West.** That is the subject the UI design was drawn against.

```bash
uv run python scripts/replay.py --list
```

If Malad West is not among the fixtures, **build it** — that locality was scanned live twice,
so its pages are in the cache. Same shape as the other three subject fixtures.

Then emit the payload exactly as the API would return it:

```bash
uv run python scripts/replay.py <malad-fixture-id> --json marina64.json
```

**It must be the real `/compare` response shape**, not a summary or a report rendering —
`competitors`, `also_found`, `register_filings`, `dropped`, `rate_axis` with `off_axis` and its
reasons, `config_matrix`, the three columns, every absence reason verbatim.

**Fabricate nothing.** If a field would be empty, it stays empty with its reason code. The
other session already refused to invent one, which was correct.

Report the file path and a one-line summary: how many eligible, how many COMPARABLE, and which
absence reasons appear in it.

---

## Task 2 — Reason codes must not depend on machine speed

Your finding:

> `candidate_budget_s` is wall clock and extraction fans out concurrently, so extra CPU spent
> on one candidate eats another candidate's budget. Two runs over identical cached pages can
> print different reason codes on a slower machine.

You removed the double work that exposed it. **The mechanism is still there**, and it matters
more than the instance you fixed:

- **It breaks the fixture corpus.** A frozen replay whose output moves cannot distinguish a
  real regression from machine noise, and the whole no-credit strategy rests on that corpus
  being trustworthy.
- **It is worse in production.** A rep sees `RATE_NOT_PUBLISHED`, refreshes, sees
  `RESEARCH_TIMED_OUT`. A reason code that changes on its own is not a fact about the project;
  it is a fact about server load, presented to a sales rep as a fact about a building.

**In replay, wall-clock budgets must not apply at all.** There is no network and no model, so
there is nothing to time out against — a budget there is measuring the machine and nothing
else. Disable the wall-clock budget when the run has no live sources, and make
`RESEARCH_TIMED_OUT` impossible to emit in replay.

**In live mode, keep the budget** — it is doing a real job against slow sites — but make the
starvation explicit rather than emergent:

- A candidate's budget should not be consumable by another candidate's CPU. Give each candidate
  its own deadline from when *its* work starts, not from a shared clock.
- If that is not achievable without restructuring the fan-out, **say so and stop**. Do not
  restructure the graph for this. A documented limit with a test naming it beats a rewrite.

**Acceptance:** the same fixture replayed ten times in a row produces byte-identical output,
including every absence reason. Make that a test. Run it on a loaded machine if you can.

Also confirm the OpenTelemetry provider shutdown you added holds — a global tracer leaking
spans into every later test was enough to flip a timing assertion once, and that is the same
class of bug as this one.

---

## Task 3 — The API contract, written down

The propOG backend has already built `agent-client.cjs` against an **assumed** interface. You
have not built `/scan` yet. Two sessions, two guesses, and they will not match by luck.

That session is being asked to print its assumed contract verbatim from its code. When it
arrives:

- Implement `/scan` to that contract exactly, or
- **Say precisely where it is wrong and why**, and let the other side change instead.

Do not split the difference or support both shapes.

The parts that must be pinned:

```
POST /scan            request body: every field and type
                      auth: the shared-secret header
                      response: 202 and what it carries
GET  /health          what it returns

Write-back:           the exact columns of builder_project_competitor_runs
                      the agent updates, and the values for each status
                      transition (running -> done, running -> failed)
```

**The write-back is the half most likely to be wrong**, because it is the only part where the
agent touches the other system's schema. Get the column names and types from the propOG
session, not from this brief.

---

## Not in this brief, deliberately

**Places calls, now 77% of the bill at ₹186 of ₹397.** Real, but ₹397 a scan is already
viable, and the Haiku swap you shipped is still unvalidated. Validating what is already
shipped comes before optimising further.

**The Haiku quality diff.** Run it the moment credit returns — Goregaon West, 27 calls, ₹75
on Opus and ₹5 on Haiku at the corrected prices. The wrong-value count is the only number that
matters, and as you noted it can be checked against the verbatim evidence substring without a
reference model at all.

---

## Verify

```bash
uv run pytest -q
# the new determinism test, ten identical replays
uv run python scripts/replay.py <malad-fixture-id> --json marina64.json
```

## Report back with

1. The Marina64 payload — path, eligible count, COMPARABLE count, absence reasons present
2. Whether ten replays are byte-identical, and what you had to change to get there
3. Whether per-candidate deadlines were achievable, or the documented limit if not
4. Where the propOG contract and your implementation disagree, if anywhere
5. What you spent — I expect nothing
