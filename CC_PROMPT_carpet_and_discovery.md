# Claude Code — carpet plausibility, discovery hygiene

Repo: `C:\Users\admin\Desktop\competitor_analysis-agent`

## First, a correction to how we read the last run

The Goregaon West scan ran on the Groq free-tier fallback and is **not a measurement of the
agent**. 8k tokens/minute against 42 candidates expired the 90s per-candidate budget
repeatedly; `extract` took 228.8s of 281.3s and both table rows carry `RESEARCH_TIMED_OUT`
on `rera_phases`. Your flagging that under "noticed, not fixed" rather than tuning around it
was the right call.

Anthropic credit is being restored. **Do not re-run any measurement until it is back.** Every
number below should be produced on Anthropic.

The v2 brief worked and that is the headline: Borivali West 2 → 5 eligible with 2 COMPARABLE,
Kandivali West 4 → 5 with 3 COMPARABLE.

## Scope discipline

- **No new modules.** This document names none.
- **No new abstractions.**
- **No refactoring outside the tasks below.**
- Smallest change that moves the number.
- **No telemetry.** Separate task, separate scope.

Frozen: `app/logic/compare.py`'s output shape, the graph topology, and the rule that no value
is ever written that was not read from a source. **Refuse, never correct.**

---

## Task 1 — Carpet area has no plausibility band. Give it one.

`app/logic/conflicts.py`, beside the rate band.

From the last run:

```
Narang Valora   carpet  1,360 – 4,218 sqft
Aarey Vista     carpet    430 – 1,180 sqft     (the subject)
```

A 4,218 sqft carpet area next to a 430 sqft subject is not credible, and it published
unchallenged. This is exactly the failure `IMPLAUSIBLE_RATE` exists to catch — specific,
plausible-looking, and wrong — and carpet has no equivalent.

**Anchor, same shape as the rate band:** the subject's own carpet midpoint. If the subject has
no carpet, the median of the eligible set's carpet midpoints. If neither exists, **the check
does not run** — no anchor, no judgement.

```python
carpet_plausible_min_ratio: float = 0.25
carpet_plausible_max_ratio: float = 4.00
```

**Apply to both bounds. If either falls outside, the whole range is refused** — half a range
is not a range, same reasoning as the rate rule.

```python
"IMPLAUSIBLE_CARPET"   # a carpet area was read, but it is outside a believable band
                       # against this project's own sizes
```

The observed numbers stay in the evidence so the card can say what was read and why it was not
used. **Nothing is adjusted, nothing is hidden.**

**One judgement call I want you to make and report, not guess at:** a genuinely larger
competitor is normal — a 4 BHK tower next to a 1–2 BHK project is a real comparison a rep
makes. The band must not delete those. Report every carpet range this rule refuses across
Borivali West and Kandivali West **with the numbers**, so I can check it is not eating good
data. If it refuses something legitimate, that is a finding and I want the number, not a
widened threshold.

---

## Task 2 — `looks_like_company` misses the commonest suffixes

`app/logic/resolve.py`.

21 of 42 researched Goregaon candidates ended at 0/6, and 20 produced no page at all, because
discovery there is dominated by promoter-named register rows that the filter does not catch:

```
Crystal Construction Company
Navkar Builder & Devvelopers
D S M Group
Atithi Builders And Constructors Private Lim
```

`Company`, `Group`, `Builder` (singular) and a truncated `Lim` are not in the suffix list.

Add: `Company`, `Group`, `Builder`, `Constructors`, `Contractors`, `Estates`, `Properties`,
`Ventures`, `Projects`, `Housing`, and a truncated-`Limited` prefix match (`Lim`, `Limite`) so
register truncation does not defeat the check.

**The existing guard stands and matters more with a longer list:** a name is only
company-shaped when the corporate suffix is the *whole* distinguishing tail. `Lodha Group` is
a company; `Lodha Amara` is a project. Adding `Housing` and `Properties` widens the blast
radius, so test the false-positive direction hard — a wrongly filtered name deletes a real
competitor silently, which is the expensive failure.

**Acceptance:** the four names above are filtered. `Kalpataru Horizon Apartments`,
`Rustomjee Crest` and any name where a real project token survives are not. Report how many
candidates this moves to `register_filings` per suburb, and confirm none of them is a marketed
project.

---

## Task 3 — Five candidates dropped for having no coordinates

`no coordinates` was the largest single drop reason in the last run — larger than every
lifecycle cause combined. That is a geocoding gap, not a competitor judgement.

Find out what happened before fixing it: for each candidate dropped this way, log the name,
what discovery gave us (address, locality, source), and whether a geocode was attempted at
all or was never issued.

**Report that list before changing anything.** If the geocode was never attempted, the fix is
one call. If it was attempted and failed, the fix is different and I want to know which.

---

## Task 4 — `clean_project_name` does not strip `|`

```
Evoke Residential project by Arkade | Goregaon West | Mumbai
```

Add `|` to the separators it already handles (`-`, `–`), and strip `Residential project`
alongside the existing `New Launch Project` / `New Launch`.

**Both existing guards stand:** never return an empty or single-token name, and cleaning is
display-only — identity, dedup and matching stay on the raw slug.

**Acceptance:** the name above cleans to `Evoke`. A name that is only a builder brand is
returned untouched.

---

## Verify

```bash
uv run pytest -q                # all green; no test weakened to pass
```

Then **one cold run per suburb on Anthropic** — Borivali West and Kandivali West, so the
numbers sit against the v2 results rather than against a throttled run.

Confirm the frozen contract: three columns, `rate_axis.off_axis[0].reason`,
`config_matrix.types`.

## Report back with

1. Every carpet range Task 1 refused, **with the numbers**, and your judgement on whether any
   was legitimate
2. Candidates Task 2 moved to `register_filings`, per suburb, with a check that none is a real
   project
3. The Task 3 list — dropped-for-no-coordinates candidates and what discovery gave us. **No fix
   yet.**
4. Eligible and COMPARABLE counts per suburb, against the 5/2 and 5/3 from v2

Report the numbers as they came out.
