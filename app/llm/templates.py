"""Deterministic fallback sentences, used when no LLM is configured or the call fails."""
from __future__ import annotations

from datetime import date

from app.models.schema import OwnProject, Project


def _months(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def card_insight(p: Project, own: OwnProject, eligible: list[Project]) -> str:
    if p.label == "UNVERIFIED":
        return (f"{p.completeness} of 6 fields on file, but no source said whether it is still selling; "
                f"confirm the lifecycle before comparing.")
    if p.label == "THIN":
        missing = ", ".join(f.replace("_", " ") for f in p.could_not_verify[:3])
        return f"Not enough verified data to compare; {missing} still missing. Adding its RERA number would lift it into ranking."
    if p.label == "PARTIAL":
        missing = " and ".join(f.replace("_psf", "").replace("_sqft", " area").replace("_", " ") for f in p.could_not_verify)
        conf = next((c for c in p.conflicts if c.field == "rate_psf"), None)
        if conf:
            return f"Ranked below the comparable projects: {conf.detail.lower()} and {missing} could not be verified."
        return f"Ranked below the comparable projects because {missing} could not be verified."
    # COMPARABLE
    bits = []
    poss = p.value("possession")
    others = [q.value("possession") for q in eligible if q.id != p.id and q.value("possession")]
    if poss and others and poss < min(others):
        bits.append("earliest handover in the set")
    elif poss and own.possession and _months(own.possession, poss) > 12:
        bits.append(f"hands over {_months(own.possession, poss)} months after {own.name}")
    r = p.value("rate_psf")
    if r and r.basis == "base" and own.rate_psf and own.rate_psf.basis == "base":
        mid = (r.min_psf + r.max_psf) / 2
        own_mid = (own.rate_psf.min_psf + own.rate_psf.max_psf) / 2
        pct = round((mid - own_mid) / own_mid * 100)
        if abs(pct) >= 3:
            bits.append(f"priced {abs(pct)}% {'above' if pct > 0 else 'below'} {own.name} on base rate")
    c = p.value("carpet_sqft")
    all_c = [q.value("carpet_sqft") for q in eligible if q.value("carpet_sqft")]
    if c and all_c and c.max_sqft == max(x.max_sqft for x in all_c):
        bits.append("largest flats in the set")
    if p.on_propog:
        bits.append("figures are builder-declared on propOG rather than researched" if p.propog_corroborated
                    else "listed on propOG, but no public source names this project in this locality")
    if not bits:
        bits.append(f"closest like-for-like match at {p.distance_km} km")
    s = "; ".join(bits)
    return s[0].upper() + s[1:] + "."


def compare_insights(payload: dict, own: dict) -> dict[str, str]:
    """`own` is own_facts() output, the same dict the model narration gets, so the
    template and the model describe the same own project on both compare paths."""
    out = {}
    ra = payload["rate_axis"]
    others = [x for x in ra["points"] if x["delta_pct"] is not None and x["id"] != payload["own_id"]]
    if others:
        w = max(others, key=lambda x: abs(x["delta_pct"]))
        out["rate"] = (f"{w['name']} is priced {abs(w['delta_pct'])}% {'above' if w['delta_pct'] > 0 else 'below'} {own['name']} on base rate, "
                       f"the widest on the axis. {len(ra['off_axis'])} project(s) cannot be placed on it.")
    else:
        out["rate"] = f"Only {len(ra['points'])} project has a base rate on record, so rates cannot be compared on one axis."
    ps = payload["possession"]["points"]
    if len(ps) >= 2:
        first, last = ps[0], ps[-1]
        gap = next((g["months"] for g in payload["possession"]["gaps"] if {g["a"], g["b"]} == {first["id"], last["id"]}), None)
        out["possession"] = f"{first['name']} hands over first; {last['name']} is {gap} months behind, a different buying decision rather than a like-for-like alternative."
    else:
        out["possession"] = "Possession dates are not available for enough projects to compare."
    ov = payload["carpet"]["overlap"]
    out["carpet"] = (f"Disclosed carpet ranges overlap only between {ov[0]:,} and {ov[1]:,} sq ft; outside that band they sell to different buyers."
                     if ov else "Disclosed carpet ranges do not overlap, so these projects sell to different buyers.")
    cb = payload["common_bhk"]
    out["configurations"] = (f"Only the {cb} BHK exists in every project; every other comparison here is really a {cb} BHK comparison."
                             if cb else "No configuration is offered by every project, so unit-level comparisons are not like-for-like.")
    st = payload["structure"]
    out["structure"] = (f"All projects are {st['types'][0].replace('_', '-')}; structure does not separate them."
                        if st["all_same_type"] and st["types"] else "Building types differ, which changes the buyer profile as much as price does.")
    am = payload["amenities"]
    counts = [c for c in am["counts"] if c is not None]
    stale = ", ".join(s["id"] for s in am["stale"])
    out["amenities"] = (f"{am['distinct']} distinct amenities across the set; counts range {min(counts)} to {max(counts)}." if counts else "No amenity lists on file.") + (
        f" {stale} data is stale; treat its gaps as unconfirmed, not absent." if stale else "")
    return out
