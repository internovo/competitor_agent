# Claude Code — lifecycle precision, reordered by the audit

Repo: `C:\Users\admin\Desktop\competitor_analysis-agent`

## Your reordering is accepted

Task 4 before Task 2. You are right and the brief was wrong: I ordered the tasks by what
looked important rather than by what unblocks what. Task 2 on 37 projects with a withdrawn
possession date is a three-row fix, exactly as you said.

The chrome finding is promoted. It was not in the brief and it is the largest of the three.

**Order below is the order. Do them in sequence and do not reorder again without saying so.**

## Scope discipline

- **No new modules.** This document names none.
- **No new abstractions** — no strategy classes, no rule engines, no config frameworks.
- **No refactoring outside the tasks below.**
- Smallest change that moves the number.
- **No telemetry.** Separate task, separate scope.

Frozen: `app/logic/compare.py`'s output shape, the graph topology, and the rule that no
value is ever written that was not read from a source. **Refuse, never correct.**

The lifecycle policy is settled and not reopened: **`new_launch` and `under_construction`
only.** No second band, no config flag.

---

## Task 1 — Scalar conflicts pick the best source instead of withdrawing

`app/logic/conflicts.py`. **This is the unblocker. Nothing else runs until it lands.**

36 of 58 finished-drops have had possession withdrawn as `SOURCES_DISAGREE`. The date
exists. Sources stated it. The honesty rule removed it from the project before the
lifecycle rule could read it.

The set-union fix already established the principle for sets. Scalars are different — they
can genuinely conflict — but withdrawal is the wrong default when `SOURCE_PRIORITY` already
encodes which source we trust.

**Rule for `possession` and `rate_psf`:** when sources disagree, publish the value from the
highest-priority source, with the disagreement stated beside it:

```
Possession: Dec 2028   (rera; squareyards says Jun 2029)
```

This is selection, not correction. Every published value is still one a named source
stated — the honesty rule is intact.

**Keep the refusal for exactly two cases:**

1. The top two sources are the **same priority** and disagree beyond the ratio threshold.
   No basis to pick, so `SOURCES_DISAGREE` stands.
2. `IMPLAUSIBLE_RATE` and `SHARED_ACROSS_PROJECTS` — those are about the value being wrong,
   not the sources differing. Untouched.

**Report before moving on:** how many projects regain a possession date, in each suburb.
I expect ~36. If it is materially lower, stop and tell me why.

---

## Task 2 — Only a spec table is evidence about the building

`app/logic/` wherever lifecycle evidence is collected and `vote_lifecycle` is called.

This is the finding you surfaced and it is the one I most want fixed.

```
"Ready To Move Properties"       nav link         -> site furniture
"Rental Yield Calculator"        widget           -> site furniture
"Rental Options Available!"      cross-sell block -> site furniture
"| Status | Ready To Move |"     spec table       -> a statement about this building
```

`vote_lifecycle` counts all four equally. Portals repeat their filter vocabulary in nav,
widgets and cross-sell blocks, while the construction status appears **once**. So the
majority drifts toward whatever word the site's chrome uses most — which is how
`New Gagangiri Society` votes `{rental: 8}` and how `under_construction` lost 6–4 on eight
projects.

**Do not fix this by blacklisting phrases.** The blacklist would need maintaining per
portal and would silently rot. Fix the category:

- A lifecycle signal counts **only** when it comes from a spec table row, a definition list,
  or an explicit labelled status field. You measured 58 such statements across the two
  suburbs, so the evidence is there.
- Prose and link text do not vote. Keep them in the evidence for display, marked as
  non-voting, so the card can still show what the page said.
- **`rental` is not a lifecycle.** It is a listing type. A project whose only signals are
  rental is a project whose page we read wrongly — route it to Task 3's
  `LIFECYCLE_UNKNOWN`, not to a drop.

If a project has no qualifying statement after this, it has no lifecycle. That is Task 3,
not a guess.

**Acceptance:** the eight outvoted projects — `Shraddha Elite`, `Rajputana Paradise`,
`Fortune Avirahi`, `H. Rishabraj Phoenix`, `Siddha Seabrook`, `73 East`, `Rudra Heights`,
`Skylon Spaces C Wing` — are re-decided on spec-table evidence alone. Report each one's new
vote and its outcome, including any that stay `ready`. `Country Park Phase 3` must remain a
correct `ready` drop; its spec table says so.

---

## Task 3 — Unknown is not finished

Any candidate with no qualifying lifecycle statement after Task 2 goes to `also_found`:

```python
"LIFECYCLE_UNKNOWN"   # no lifecycle statement was found in any source; not classified,
                      # not dropped as finished
```

Visible and countable, never swept into the finished-drop pile. Silently dropping an unknown
is the same failure as inventing a value, pointed the other way.

**Report the count per suburb.** A large number means the classifier has a coverage problem
rather than an accuracy one, and that is a different fix.

---

## Task 4 — A future possession date beats a status string

Now, with dates restored by Task 1, this has something to act on.

- Possession is in the future by more than `lifecycle_possession_margin_months` **and**
  status says ready/completed → the status is stale. Classify `under_construction` and
  record the override in provenance.
- They agree → no change.
- Possession absent → the status stands. Infer nothing.
- Possession past, status says under construction → leave it. A delayed project is still a
  competitor, and the date is the likelier stale value here.

```python
lifecycle_possession_margin_months: int = 6
```

The three you already found — `Shreeji Sharan Group of Companies`, `Neev Horizon`,
`RajHill Building No. 2` — are the floor, not the target.

---

## Verify

```bash
uv run pytest -q                # all green; no test weakened to pass
```

Then **re-run the same two suburbs, cold** — Borivali West and Kandivali West — so the
numbers sit directly against the audit you just produced.

Confirm the frozen contract: three columns, `rate_axis.off_axis[0].reason`,
`config_matrix.types`.

## Report back with

1. **The audit table again, after all four tasks** — same rows, same columns, so the delta
   is readable at a glance
2. Projects that regained a possession date (Task 1), per suburb
3. The eight outvoted projects, each with its new spec-table-only vote and outcome
4. `LIFECYCLE_UNKNOWN` count per suburb
5. **Eligible competitor count per suburb, before and after.** Borivali was 2 and Kandivali
   was 4. That pair of numbers is what this whole brief is for.

Report the numbers as they came out. If eligible counts barely move, that is the finding and
it points somewhere we have not looked — say so rather than loosening anything.
