"""How much of the form the regexes fill, and what would be left for Claude.

    uv run python scripts/extraction_split.py

Reads data/fixtures/pages/*.txt, runs the deterministic extractors over each, and
prints the per-field split. Numbers are reported as they come out. A field the
regexes cannot read is not a failure -- it is the work Claude is still paid for.
"""
from __future__ import annotations

from app.config import COMPLETENESS_FIELDS, FIXTURE_DIR
from app.extract import deterministic
from app.graph.nodes.pipeline import FILLABLE
from app.models.schema import Page

PAGES = sorted((FIXTURE_DIR / "pages").glob("*.txt"))


def main() -> None:
    per_field: dict[str, int] = {f: 0 for f in FILLABLE}
    det = llm = 0
    print(f"{'page':<34} {'deterministic':>13} {'left for Claude':>16}")
    for path in PAGES:
        name = path.stem.replace("-", " ").title()
        page = Page(url=f"file://{path.name}", source="tavily", text=path.read_text(encoding="utf-8"))
        _, _, _, filled, lifecycle = deterministic.read(page, name)
        if lifecycle not in deterministic.UNMAPPED_LIFECYCLES | {"unknown"}:
            filled = filled | {"status"}
        for f in filled:
            per_field[f] = per_field.get(f, 0) + 1
        det += len(filled)
        llm += len(FILLABLE) - len(filled)
        print(f"{path.name:<34} {len(filled):>13} {len(FILLABLE) - len(filled):>16}")

    six = sum(per_field[f] for f in COMPLETENESS_FIELDS)
    six_total = len(PAGES) * len(COMPLETENESS_FIELDS)
    total = det + llm
    print(f"\n{len(PAGES)} pages x {len(FILLABLE)} fields = {total} field slots")
    print(f"  deterministic : {det:>4}  ({det / total:.0%})")
    print(f"  left to Claude: {llm:>4}  ({llm / total:.0%})")
    print(f"\nthe six comparison fields only: {six}/{six_total} ({six / six_total:.0%}) deterministic")
    print("\nper field, pages where the regexes answered:")
    for f in FILLABLE:
        print(f"  {f:<16} {per_field[f]}/{len(PAGES)}")


if __name__ == "__main__":
    main()
