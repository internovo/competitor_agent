# data/fixtures/pages

Prose pages, copied from the donor spike (`competitor-agent-demo/fixtures/pages/`).
They are the input to `tests/test_deterministic_first.py` and to
`scripts/extraction_split.py`, which reports how much of the form the regexes fill
without an LLM call.

**These files are SYNTHETIC.** They were hand-written to exercise every branch in
the extractors -- a base rate, a rate with no basis phrase at all, an
all-inclusive rate, a past possession, a resale, a commercial unit, a
relative-duration possession, and a page with almost nothing on it.

They are NOT evidence that the extractors work on real pages. Replace them with
saved output from a live run the moment keys exist; `data/cache/` already holds
what a live run fetched.
