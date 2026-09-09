# Claude Code — produce one presentable competitor table

Repo: `C:\Users\admin\Desktop\competitor_analysis-agent`

This is a **reporting task, not a code task.** Do not change any logic, do not tune any
threshold, do not fix anything you notice along the way. Note it at the end instead.

## What I need

One live scan, printed as a table I can paste into a message to senior engineers. It has to
look like a product output, not a JSON dump.

## Run

One **cold** scan — a locality no recent run has warmed. Malad West / Marina64 is fine if
the cache is cleared, otherwise pick Goregaon West.

Print the subject project's own six fields first, then the competitor table.

## Table format

Markdown. Top 10 rows, in the order `service.py` already sorts them.

```
Subject: <name>, <locality>   |   <configurations>   |   <carpet sqft>   |   <rate/sqft>   |   <possession>

| # | Project | Dist | Config | Carpet (sqft) | Rate ₹/sqft | Possession | Structure | RERA | Cov | Label | Score |
|---|---------|------|--------|---------------|-------------|------------|-----------|------|-----|-------|-------|
```

Rules for the cells:

- An absent value prints its **reason code**, not a blank and not a dash:
  `NOT_PUBLISHED`, `SOURCES_DISAGREE`, `IMPLAUSIBLE_RATE`, `SHARED_ACROSS_PROJECTS`,
  `RATE_NOT_PUBLISHED`, `NO_RERA_ON_FILE`. The reason codes are the point — they are what
  shows the agent refusing rather than guessing.
- `Cov` is `n/6`. `Score` is `score/score_max`, e.g. `38/80`.
- Rate cells carry the basis: `38,000 (base)` / `29,100–38,700 (undisclosed)`.

## Then three short sections under it

**1. Provenance for one row.** Pick the highest-coverage competitor and list each of its six
fields with the source domain the value came from. One line per field. This is the proof
that nothing is invented.

**2. What was refused, and why.** Every field the honesty rules withheld in this run, with
the observed number:

```
Shreeji Atlantis    rate    IMPLAUSIBLE_RATE         read 3,000; market ~35,000
Simplex KhushAangan rate    SHARED_ACROSS_PROJECTS   22,130 appeared on 5 projects
```

**3. Run stats.** Candidates discovered / researched / eligible, coverage histogram
(how many at 0,1,2,3,4,5,6 of 6), per-stage seconds, total seconds, cold or warm.

## Do not

- Do not hand-pick rows to make the table look better. Print what came out.
- Do not fill an empty cell with anything.
- Do not fix a bug you spot. List it at the bottom under "noticed, not fixed".
