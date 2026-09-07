"""How much of the form the regexes fill, and what would be left for Claude.

    uv run python scripts/extraction_split.py            # the synthetic corpus
    uv run python scripts/extraction_split.py --cache     # raw hit rate on real portal HTML

Numbers are reported as they come out. A field the regexes cannot read is not a
failure -- it is the work Claude is still paid for.

Read the caveat printed by --cache before quoting its numbers.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app.config import CACHE_DIR, COMPLETENESS_FIELDS, FIXTURE_DIR, settings
from app.extract import deterministic
from app.graph.nodes.pipeline import FILLABLE
from app.models.schema import Page

PORTAL_HOSTS = ("squareyards.com", "housing.com", "99acres.com", "magicbricks.com")


def synthetic() -> list[tuple[str, str]]:
    """(project name, text) for the hand-written corpus."""
    return [(p.stem.replace("-", " ").title(), p.read_text(encoding="utf-8"))
            for p in sorted((FIXTURE_DIR / "pages").glob("*.txt"))]


def cached_html() -> list[str]:
    """Text of the real portal pages a live run saved."""
    from app.sources.fetch import html_to_text

    out = []
    for meta_path in sorted(CACHE_DIR.glob("*.meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not any(h in meta["url"] for h in PORTAL_HOSTS) or meta["status"] != 200:
            continue
        body = Path(str(meta_path).replace(".meta.json", ".body"))
        if not body.exists():
            continue
        text = html_to_text(body.read_text(encoding="utf-8", errors="ignore"), settings.max_page_chars)
        if len(text) >= 400:
            out.append(text)
    return out


def report_form(pages: list[tuple[str, str]]) -> None:
    per_field: dict[str, int] = {f: 0 for f in FILLABLE}
    det = llm = 0
    print(f"\n=== synthetic corpus (data/fixtures/pages): {len(pages)} pages ===")
    for name, text in pages:
        page = Page(url=f"https://example/{name}", source="squareyards", text=text)
        _, _, _, filled, lifecycle, _why = deterministic.read(page, name)
        if lifecycle not in deterministic.UNMAPPED_LIFECYCLES | {"unknown"}:
            filled = filled | {"status"}
        for f in filled:
            per_field[f] += 1
        det += len(filled)
        llm += len(FILLABLE) - len(filled)
        print(f"  {name:<34} {len(filled):>2} deterministic, {len(FILLABLE) - len(filled):>2} left")

    total = det + llm
    six = sum(per_field[f] for f in COMPLETENESS_FIELDS)
    six_total = len(pages) * len(COMPLETENESS_FIELDS)
    print(f"\n  {len(pages)} pages x {len(FILLABLE)} fields = {total} field slots")
    print(f"    deterministic : {det:>5}  ({det / total:.0%})")
    print(f"    left to Claude: {llm:>5}  ({llm / total:.0%})")
    print(f"  the six comparison fields only: {six}/{six_total} ({six / six_total:.0%}) deterministic")
    print(f"  fields asked of the model, per page: {len(FILLABLE)} before -> {llm / len(pages):.1f} after")
    print("\n  per field, pages where the regexes answered:")
    for f in FILLABLE:
        print(f"    {f:<16} {per_field[f]:>4}/{len(pages)}")


def report_raw(texts: list[str]) -> None:
    """Raw extractor hit rate on real portal HTML.

    NO project focus is applied: data/cache holds the portal SEARCH and listing
    pages a live run fetched, not individual project pages, so there is no project
    name to narrow them to. This measures whether the regexes fire on real Indian
    portal markup -- the donor spike's actual claim -- and NOT per-project coverage.
    """
    d = deterministic
    checks = {
        "configurations": lambda t: d.extract_configurations(t).values,
        "carpet": lambda t: d.extract_carpet(t)[0].values,
        "rate": lambda t: d.extract_rate(t).values,
        "basis stated": lambda t: d.extract_price_basis(t).value in ("base", "all_in"),
        "possession": lambda t: d.extract_possession(t).values,
        "building type": lambda t: d.extract_building_type(t).values,
        "towers": lambda t: d.extract_tower_count(t).values,
        "rera": lambda t: d.extract_rera(t).values,
        "amenities": lambda t: d.extract_amenities(t).values,
        "developer": lambda t: d.extract_developer(t).values,
        "lifecycle": lambda t: d.classify_lifecycle(t)[0] != "unknown",
    }
    print(f"\n=== real portal HTML (data/cache): {len(texts)} pages ===")
    print(report_raw.__doc__.split("\n\n", 1)[1].replace("    ", "  "))
    for label, fn in checks.items():
        hits = sum(1 for t in texts if fn(t))
        print(f"    {label:<16} {hits:>4}/{len(texts)}  ({hits / len(texts):.0%})")
    print("\n  configurations reads 0 because these ARE listing pages: a whole page"
          "\n  offering 1-5 BHK across many projects is refused as SOURCES_DISAGREE"
          "\n  rather than published as one building's inventory. That is the guard"
          "\n  working, not the extractor failing.")


def main() -> None:
    if "--cache" in sys.argv:
        texts = cached_html()
        if not texts:
            sys.exit("no cached portal pages in data/cache")
        report_raw(texts)
    else:
        report_form(synthetic())


if __name__ == "__main__":
    main()
