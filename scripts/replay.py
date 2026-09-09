"""The competitor table for a frozen suburb, from the page cache, with no network and no model.

    uv run python scripts/replay.py borivali-west-2026-09-08
    uv run python scripts/replay.py --list
    uv run python scripts/replay.py borivali-west-2026-09-08 --json out.json

Every fixture under tests/fixtures/replay/ is a subject project whose scan was run
live once; `data/cache/` holds the pages that run fetched. Replay mode reads only
from that cache -- a miss raises rather than fetching -- and `llm=None` means no
model is constructed, so this runs with every API key unset and costs nothing.

What it does NOT cover: the model's half of extraction and the model's half of
entity resolution. Some fields are absent here that a live run would have filled.
The deterministic layers below them -- conflicts, merge, lifecycle, completeness,
match score, eligibility, the sort and the compare payload -- are fully exercised.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

from app import service
from app.config import COMPLETENESS_FIELDS, ROOT, settings
from app.cost import classify
from app.models.schema import OwnProject

FIXTURES = ROOT / "tests" / "fixtures" / "replay"
# The reasons that mean "a source stated something and we refused to publish it",
# as opposed to "nobody stated anything".
WITHHELD = ("SOURCES_DISAGREE", "IMPLAUSIBLE_RATE", "SHARED_ACROSS_PROJECTS", "IMPLAUSIBLE_CARPET")


def suburbs() -> list[str]:
    return sorted(p.name for p in FIXTURES.iterdir() if (p / "subject.json").exists())


def _n(x) -> str:
    return f"{x:,}"


def _cell(p, field: str) -> str:
    """The value, or the reason code standing in for it. Never a blank."""
    r = p.report(field)
    if r.absent:
        return r.absent
    v = r.value
    if field == "configurations":
        return "/".join(str(c) for c in v) + " BHK"
    if field == "carpet_sqft":
        return f"{_n(v.min_sqft)}-{_n(v.max_sqft)}"
    if field == "rate_psf":
        span = _n(v.min_psf) if v.min_psf == v.max_psf else f"{_n(v.min_psf)}-{_n(v.max_psf)}"
        return f"{span} ({v.basis})"
    if field == "possession":
        return v.isoformat()
    if field == "structure":
        return f"{v.towers} towers" if v.towers else (v.building_type or "towers not stated")
    if field == "rera_phases":
        return ", ".join(ph.number for ph in v if ph.number) or "NO_RERA_ON_FILE"
    return str(v)


def _domain(url: str | None) -> str:
    if not url:
        return "-"
    return url.split("//")[-1].split("/")[0].removeprefix("www.")


def table(own: OwnProject, ranked, rows: int) -> str:
    cfg = "/".join(str(c) for c in own.configurations)
    out = [
        f"**Subject: {own.name}, {own.locality}  |  {cfg} BHK  |  "
        f"{_n(own.carpet_sqft.min_sqft)}-{_n(own.carpet_sqft.max_sqft)} sqft  |  "
        f"Rs {_n(own.rate_psf.min_psf)}-{_n(own.rate_psf.max_psf)} ({own.rate_psf.basis})  |  "
        f"{own.possession.strftime('%b %Y')}**", "",
        "| # | Project | Dist | Config | Carpet (sqft) | Rate /sqft | Possession | Structure | RERA | Cov | Label | Score |",
        "|---|---------|------|--------|---------------|------------|------------|-----------|------|-----|-------|-------|",
    ]
    for i, p in enumerate(ranked[:rows], 1):
        score = f"{p.match_score}/{p.score_max}" if p.match_score is not None else "-"
        cells = [_cell(p, f) for f in COMPLETENESS_FIELDS]
        out.append(f"| {i} | {p.name} | {p.distance_km} km | " + " | ".join(cells)
                   + f" | {p.completeness}/6 | {p.label} | {score} |")
    return "\n".join(out)


def provenance(p) -> str:
    out = [f"### Provenance - {p.name} ({p.completeness}/6)", "",
           "| Field | Source domain | Source | Method |", "|---|---|---|---|"]
    for f in COMPLETENESS_FIELDS:
        r = p.report(f)
        fv = r.display()
        if fv is None:
            out.append(f"| {f} | - | - | `{r.absent}` |")
        else:
            out.append(f"| {f} | {_domain(fv.prov.url)} | {fv.prov.source} | {fv.method} |")
    return "\n".join(out)


def _observed(field: str, fv) -> str:
    v = fv.value
    if field == "rate_psf":
        return f"{v.min_psf}-{v.max_psf}"
    if field == "carpet_sqft":
        return f"{v.min_sqft}-{v.max_sqft}"
    if field == "possession":
        return v.isoformat()
    if field == "rera_phases":
        return ",".join(ph.number for ph in v if ph.number)
    return str(v)


def refusals(projects) -> str:
    out = ["### What was refused, and why", "", "```"]
    n = 0
    for p in sorted(projects, key=lambda x: x.name):
        for f in COMPLETENESS_FIELDS:
            r = p.report(f)
            if r.absent not in WITHHELD:
                continue
            n += 1
            seen = "; ".join(f"{fv.prov.source} {_observed(f, fv)}" for fv in r.observations[:4])
            span = f"  span {r.span}" if r.span else ""
            out.append(f"{p.name[:32]:<32} {f:<15} {r.absent:<22} read {seen}{span}"[:200])
    if not n:
        out.append("nothing withheld: no field in this run had an observation behind it that the honesty rules refused")
    out.append("```")
    return "\n".join(out)


def geocode_audit(rec, meta) -> str:
    """Every candidate dropped for want of coordinates, and whether anyone was asked.

    Attempted-and-failed is a source problem; never-issued is ours. They are
    different fixes, so the report separates them rather than counting a total.
    """
    unplaced = {p.id for p in rec.projects if p.drop_reason == "no coordinates"}
    lines = [l for l in meta["log"] if l.startswith("geocode-audit[")
             and l.split("[", 1)[1].split("]", 1)[0] in unplaced]
    out = ["### Geocode audit - candidates dropped as `no coordinates`", "", "```"]
    out += lines or [f"none: every one of {len(rec.projects)} researched candidates was placed"]
    out.append("```")
    return "\n".join(out)


def stats(rec, meta, payload, stage_s: dict[str, float], total: float) -> str:
    researched = [p for p in rec.projects if p.pages_seen]
    hist = Counter(p.completeness for p in rec.projects)
    drops = Counter(re.sub(r"\d{4}-\d{2}-\d{2}", "<date>", (p.drop_reason or "")).split(" (")[0]
                    for p in rec.projects if not p.eligible)
    also = Counter(a.get("reason") or "outside the top rows" for a in payload["also_found"])
    c = payload["counts"]
    return "\n".join([
        "### Run stats", "", "```",
        f"discovered {c['candidates_seen']}  .  register-only {len(meta['register_filings'])}"
        f"  .  societies {len(meta['societies'])}  .  researched {len(rec.projects)}"
        f"  .  read pages {meta['pages_fetched']}",
        f"eligible {c['eligible']}  .  comparable {c['comparable']}  .  partial {c['partial']}"
        f"  .  thin {c['thin']}  .  unverified {c['unverified']}"
        f"  .  also_found {len(payload['also_found'])}  .  dropped {len(payload['dropped'])}",
        "",
        "coverage   " + "   ".join(f"{k}/6:{hist.get(k, 0)}" for k in range(7)),
        f"           {len(researched)} of {len(rec.projects)} researched candidates produced a page",
        "",
        " . ".join(f"{k} {v}s" for k, v in sorted(stage_s.items(), key=lambda kv: -kv[1])),
        f"total {total:.1f}s . REPLAY (cache only, no network, no model)",
        "",
        "drops        " + " . ".join(f"{k} {v}" for k, v in drops.most_common()),
        "also_found   " + " . ".join(f"{k[:44]} {v}" for k, v in also.most_common()),
        "```",
    ])


# A replay spends nothing, so the only honest cost it can report is what the same
# scan WOULD cost with a model attached. The variable half is measured -- the exact
# characters extraction put in front of the model. The constants below are the fixed
# prompt scaffolding, and the output sizes from the two live runs whose token counts
# were recorded (39 calls / 4,161 output and 32 calls / 6,299 output).
CHARS_PER_TOKEN = 4          # English prose and stripped HTML; the usual rule of thumb
EXTRACT_OUT_TOKENS = 150
RESOLVE_IN_TOKENS = 400      # two candidate records, no page text
RESOLVE_OUT_TOKENS = 60
NARRATE_OUT_TOKENS = 60      # per eligible card


def cost_projection(meta: dict, payload: dict) -> str:
    from app.config import llm_price
    from app.cost import tokens_inr
    from app.llm import prompts

    model = settings.claude_model
    scaffold = (len(prompts.EXTRACT_SYSTEM) + len(prompts.EXTRACT_USER)) / CHARS_PER_TOKEN
    pages, eligible = meta["model_pages"], payload["counts"]["eligible"]
    pairs = meta["ambiguous_pairs"]
    stages = {
        "extract": (pages, round(meta["prompt_chars"] / CHARS_PER_TOKEN + scaffold * pages), pages * EXTRACT_OUT_TOKENS),
        "resolve": (pairs, pairs * RESOLVE_IN_TOKENS, pairs * RESOLVE_OUT_TOKENS),
        "narrate": (1 if eligible else 0, 250 * eligible, NARRATE_OUT_TOKENS * eligible),
    }
    rows = ["### Cost, if this scan ran with a model attached", "",
            f"Model `{model}` at {llm_price(model)[0]:g}/{llm_price(model)[1]:g} USD per Mtok, "
            f"Rs {settings.inr_per_usd:g} to the dollar.", "",
            "| Node | Model calls | Input tokens | Output tokens | Rs |", "|---|---:|---:|---:|---:|"]
    tot_in = tot_out = 0.0
    for node, (calls, tin, tout) in stages.items():
        tot_in, tot_out = tot_in + tin, tot_out + tout
        rows.append(f"| {node} | {calls} | {tin:,} | {tout:,} | {tokens_inr(model, tin, tout):,.2f} |")
    searches, places, _ = classify(meta["urls"])
    http = round((searches * settings.search_usd_per_call + places * settings.places_usd_per_call) * settings.inr_per_usd, 2)
    llm_inr = tokens_inr(model, round(tot_in), round(tot_out))
    researched = meta["candidates_researched"]
    rows += [
        f"| search + places | {searches} searches, {places} places | - | - | {http:,.2f} |",
        f"| **total** | | **{round(tot_in):,}** | **{round(tot_out):,}** | **{llm_inr + http:,.2f}** |", "",
        "```",
        f"tokens per researched candidate   {round((tot_in + tot_out) / max(1, researched)):,}  ({researched} candidates)",
        f"cost per scan                     Rs {llm_inr + http:,.2f}   (model Rs {llm_inr:,.2f} + search/places Rs {http:,.2f})",
        f"one builder, 40 projects monthly  Rs {(llm_inr + http) * 40:,.0f} / month",
        "```",
        "",
        "Measured: the page characters extraction put in front of the model, the calls it",
        f"would make, and the {pairs} resolve pairs the deterministic rules could not settle.",
        f"Assumed: {CHARS_PER_TOKEN} characters to the token, {EXTRACT_OUT_TOKENS} output tokens per extraction.",
    ]
    return "\n".join(rows)


async def run(name: str, radius: float, rows: int) -> tuple[str, dict]:
    own = OwnProject(**json.loads((FIXTURES / name / "subject.json").read_text(encoding="utf-8")))
    stage_s: dict[str, float] = {}
    last = time.perf_counter()

    def on_stage(node: str, update: dict) -> None:
        nonlocal last
        now = time.perf_counter()
        if node:
            stage_s[node] = round(stage_s.get(node, 0.0) + now - last, 1)
        last = now

    t0 = time.perf_counter()
    rec, meta = await service.run_scan(own, radius, "replay", llm=None, on_stage=on_stage)
    total = time.perf_counter() - t0
    payload = service.list_payload(rec, own, meta)
    ranked = service.rank(rec.projects)
    best = max(ranked, key=lambda p: p.completeness, default=None)

    parts = [f"## {own.locality} - replayed from `tests/fixtures/replay/{name}` + `data/cache/`", "",
             table(own, ranked, rows), ""]
    if best is not None:
        parts += [provenance(best), ""]
    parts += [refusals(rec.projects), "", geocode_audit(rec, meta), "",
              stats(rec, meta, payload, stage_s, total), "", cost_projection(meta, payload)]
    return "\n".join(parts), payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("suburb", nargs="?", help=f"one of: {', '.join(suburbs())}")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--radius", type=float, default=settings.default_radius_km)
    ap.add_argument("--rows", type=int, default=settings.max_table_rows)
    ap.add_argument("--json", dest="json_out", help="also write the list payload here")
    a = ap.parse_args()
    if a.list or not a.suburb:
        print("\n".join(suburbs()))
        return 0
    if a.suburb not in suburbs():
        print(f"no fixture '{a.suburb}'; have: {', '.join(suburbs())}", file=sys.stderr)
        return 2
    report, payload = asyncio.run(run(a.suburb, a.radius, a.rows))
    keys = [k for k in ("anthropic_api_key", "groq_api_key", "tavily_api_key", "google_maps_api_key")
            if getattr(settings, k)]
    print(report)
    print(f"\n_API keys present in this process: {', '.join(keys) if keys else 'none'}._")
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
