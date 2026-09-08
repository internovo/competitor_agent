# Claude Code — fix the research bottleneck in the competitor agent

Repo: `C:\Users\admin\Desktop\competitor_analysis-agent`

## The problem, measured

The last live scan of Marina64 (1.5 km) produced this funnel:

```
73 candidates discovered
25 pages fetched          <-- 48 candidates were never read at all
61 "eligible", of which 59 carry 0 or 1 of 6 fields
```

Discovery works. The extractors work on anything they are given. **The agent is
looking up 73 projects and only reading 25 of them.**

The unread 48 split into two groups needing opposite treatment:

| Group | Examples | Why unread |
|---|---|---|
| 24 rows at 1/6 | `Jadhwani Infrastructure LLP`, `AOP OF BINDESH AND MILIND SOMAIYA`, `Mahindra Lifespace Developers limited` ×3 | MahaRERA `projectName` is the promoter's company name. Unsearchable — no retry will ever help. |
| 35 rows at 0/6 | `Rustomjee Elanza`, `Chandak Treesourus`, `Ruparel Stardom`, `Auris Serenity`, `Lodha Raj Infinia`, `Narang Vivenda`, `Vora Centrico` | Real, marketed, searchable. The page budget was exhausted on the group above. |

Five changes below. Nothing else.

---

## Scope discipline — read this before writing code

**Make the smallest change that moves the numbers.** This is a bottleneck fix, not a
refactor.

Specifically:

- **No new modules** unless this document names one. It names none.
- **No new abstractions** — no strategy classes, no registries, no plugin points, no
  config framework. A function is a function.
- **No refactoring of anything not listed here**, however tempting.
- If a fix can be five lines, it is five lines. A "more extensible" version of a five-line
  fix is the wrong answer.
- **Do not add telemetry or tracing.** That is the next task and it has its own scope.

Frozen, as before:

- `app/logic/compare.py`'s output shape — `rate_axis`, `off_axis`, `config_matrix`,
  `columns`, and the rest keep their exact keys.
- The graph topology — same 9 nodes, same edges.
- The honesty rules — no value is ever written that was not read from a source.

---

## Fix 1 — Fetch the page you already found

**~30 minutes.**

`Candidate.source_url` already holds the page discovery pulled the name from. If a
candidate was found by Tavily or a portal, a page mentioning it demonstrably exists and we
already know its URL.

In the `extract` node (`app/graph/nodes/pipeline.py`), **fetch `source_url` first**,
before issuing any search. Only search if it is absent, fails, or leaves fields empty.

Deduplicate: if `source_url` is already among the pages a later search returns, do not
fetch it twice.

**Acceptance:** in replay, log how many candidates had a `source_url` and how many of
those pages were fetched. Both numbers in the report.

---

## Fix 2 — Bucket the register-only filings

**~1 hour.**

A MahaRERA row whose name is the promoter's company is not a competitor a sales rep can
use. It should not consume a page budget and should not sit in the competitor table.

Add to `app/logic/resolve.py` (**no new module**):

```python
def looks_like_company(name: str) -> bool:
    """A register row whose projectName is the promoter, not a marketed project."""
```

Match on: trailing `LLP`, `Ltd`, `Limited`, `Pvt`, `Private`, `Developers`, `Developer`,
`Infrastructure`, `Infra`, `Constructions`, `Construction`, `Realtors`, `Realty`,
`Enterprises`, `Builders`, `Associates`, `Corporation`; or leading `AOP OF`, `M/S`, `M/s`.

**The guard that matters:** `Lodha Developers` is a company, `Lodha Amara` is a project.
A name is only company-shaped when the corporate suffix is the *whole* distinguishing
tail — if any additional token remains after removing the builder brand and the suffix,
it is a project. Test both directions; the false positive is the expensive one.

Then:

- Add `Candidate.register_only: bool = False`, set at discovery.
- Register-only candidates skip research entirely.
- They appear in the payload under a separate `register_filings` array, **never** in
  `competitors`.

**Also fix self-exclusion while you are here.** The subject appeared in its own competitor
set three times as `Mahindra Lifespace Developers limited` — its own register filings.
Exclude by identity: the subject's RERA numbers, its `place_id`, and its name. Not by
lifecycle.

**Acceptance:** `tests/test_resolve.py` gains cases for both directions — company names
matched, project names with builder brands not matched. Marina64's own filings do not
appear as competitors.

---

## CHECKPOINT — stop and measure

**Do Fixes 1 and 2, then run replay and report before continuing.**

```bash
uv run pytest -q
# replay scan of Marina64, 1.5 km, off the warm cache
```

Report:

- candidates discovered / register-only / researched
- pages attempted, fetched, failed — **failures broken down by domain**
- coverage histogram: how many candidates at 0, 1, 2, 3, 4, 5, 6 of 6

**If coverage has not moved at all, stop and say so.** That would mean the pages are
failing to fetch rather than failing to be requested, which is a different fix and I would
rather know than have you build Fix 3 on a wrong diagnosis.

---

## Fix 3 — Spend the page budget by priority

**~2 hours.**

Today ~25 pages are spread thinly across 73 candidates. Concentrate them.

Add to `app/logic/resolve.py`:

```python
def research_priority(c: Candidate) -> int:
    """Higher = more likely to have a readable page. Deterministic, no LLM."""
```

Rank on, in descending weight:

1. discovered from a portal or Tavily (a page provably exists) — strongest signal
2. has a `source_url`
3. name is not company-shaped
4. name contains a recognisable builder brand
5. distance (tie-break only, nearer first)

Then in `app/config.py`:

```python
max_research_candidates: int = 15      # research the top N by priority
pages_per_candidate: int = 4           # was effectively <1
```

Candidates outside the top N are still returned, still labelled `UNVERIFIED`, with an
absence reason saying they were not researched this run. **They are not dropped, and the
reason must say "not researched", not "not published"** — those are different facts and
the whole absence design exists to keep them apart.

---

## Fix 4 — Concurrency and hard timeouts

**~35 minutes.**

The last live run spent 408 of 499 seconds in `extract`, most of it waiting on candidates
that produced nothing.

In `app/config.py`:

```python
extract_concurrency: int = 10          # was 4; this work is network-bound, not CPU
fetch_timeout_s: int = 8               # per HTTP fetch
candidate_budget_s: int = 30           # total wall clock per candidate, then move on
```

Enforce `candidate_budget_s` with `asyncio.wait_for` around each candidate's research.
A candidate that runs out of budget keeps whatever it got and is marked with an absence
reason for the rest. **A timeout is a normal outcome, not an error** — it must not fail
the run.

---

## Fix 5 — Sort and cap the table

**~20 minutes.**

The UI shows ten ranked competitors. Sixty-one rows is a database dump.

Sort order within `competitors`:

1. label — `COMPARABLE`, `PARTIAL`, `THIN`, `UNVERIFIED` (`LABEL_ORDER` already exists)
2. **coverage descending** — this is the fix; right now the emptiest register rows sort
   to the top at 0.06 km while the real launches sit at rank 34–59
3. distance ascending

Then cap:

```python
max_table_rows: int = 10
```

Payload shape after this:

```json
{
  "competitors":       [ ...at most 10... ],
  "also_found":        [ ...the rest, name + distance + label + coverage only... ],
  "register_filings":  [ ...from Fix 2... ],
  "dropped":           [ ...unchanged, with causes... ]
}
```

---

## Verify

```bash
uv run pytest -q                       # all green, no test weakened to pass
# replay scan of Marina64, 1.5 km
```

Then confirm the frozen contract is intact: the compare response still contains
`rate_axis.off_axis[0].reason`, `config_matrix.types`, and three columns.

## Report back with exactly these numbers

1. **Funnel:** discovered / register-only / researched / pages fetched / pages failed
   (by domain)
2. **Coverage histogram** across all candidates: how many at 0, 1, 2, 3, 4, 5, 6 of 6
3. **Top 10 table** — and of those ten: how many are marketed projects rather than
   register rows, and how many carry ≥4 of 6 fields
4. **Per-stage seconds**, replay and (if you run one) live
5. Anything you could not do, and what blocked it

Report the numbers as they came out. **Do not tune anything to make a number look
better** — if coverage is still poor after all five fixes, that is the finding, and it
points somewhere other than where we have been looking.

I am running the latency and field-coverage tests against this afterwards, so leave the
measurement hooks obvious and the working tree clean.
