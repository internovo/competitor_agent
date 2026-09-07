"""Turn ExtractedFacts from one page into FieldValues on a Project. Nothing is overwritten; values accumulate."""
from __future__ import annotations

import re
from datetime import date

from app.models.schema import (
    Amenity, CarpetRange, ExtractedFacts, FieldValue, Page, Project, Provenance, RateValue, ReraPhase, Structure, Timeline,
)

RERA_RE = re.compile(r"\bP\d{11}\b")


def parse_month(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip()
    m = re.match(r"^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?$", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3) or 1)
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    return None


def merge_facts(project: Project, facts: ExtractedFacts, page: Page, verified_sources: set[str] = frozenset({"maharera"}),
                method: str = "llm", confidence: dict[str, str] | None = None,
                evidence: dict[str, str] | None = None) -> Project:
    prov = Provenance(source=page.source, url=page.url, fetched_at=page.fetched_at)
    confidence, evidence = confidence or {}, evidence or {}

    def fv(field: str, value):
        return FieldValue(value=value, prov=prov, method=method,
                          confidence=confidence.get(field, "medium"), evidence=evidence.get(field))

    if page.source not in project.sources_consulted:
        project.sources_consulted.append(page.source)
    if page.url not in project.pages_seen:
        project.pages_seen.append(page.url)

    if facts.builder and not project.builder:
        project.builder = facts.builder
    if facts.status and (project.status == "unknown" or page.source == "maharera"):
        project.status = facts.status
    if facts.locality and not project.locality:
        project.locality = facts.locality
    if facts.address and not project.address:
        project.address = facts.address
    if facts.lat is not None and facts.lng is not None and project.lat is None:
        project.lat, project.lng = facts.lat, facts.lng
        project.pin_accuracy = f"{page.source} - address"

    if facts.configurations:
        project.configurations.observe(fv("configurations", sorted(set(facts.configurations))))
    if facts.carpet_min_sqft and facts.carpet_max_sqft:
        project.carpet_sqft.observe(fv("carpet_sqft", CarpetRange(min_sqft=facts.carpet_min_sqft, max_sqft=facts.carpet_max_sqft)))
    if facts.rate_min_psf or facts.rate_max_psf:
        lo = facts.rate_min_psf or facts.rate_max_psf
        hi = facts.rate_max_psf or facts.rate_min_psf
        project.rate_psf.observe(fv("rate_psf", RateValue(min_psf=lo, max_psf=hi, basis=facts.rate_basis or "undisclosed")))
    poss = parse_month(facts.possession)
    if poss:
        project.possession.observe(fv("possession", poss))
    if any(v is not None for v in (facts.building_type, facts.towers, facts.floors_min, facts.land_acres, facts.total_units)):
        bt = facts.building_type
        if bt is None and facts.towers:
            bt = "single" if facts.towers == 1 else "multi_tower"
        project.structure.observe(fv("structure", Structure(
            building_type=bt, towers=facts.towers, floors_min=facts.floors_min, floors_max=facts.floors_max,
            land_acres=facts.land_acres, total_units=facts.total_units, open_space_pct=facts.open_space_pct,
        )))
    if facts.rera_numbers:
        nums = [n.strip().upper() for n in facts.rera_numbers if RERA_RE.search(n.strip().upper())]
        already = {(v.prov.source, tuple(ph.number for ph in v.value)) for v in project.rera_phases.values}
        if nums and (page.source, tuple(dict.fromkeys(nums))) not in already:
            phases = [ReraPhase(number=n, verified=page.source in verified_sources, label=f"Phase {i + 1}") for i, n in enumerate(dict.fromkeys(nums))]
            project.rera_phases.observe(fv("rera_phases", phases))
    if facts.amenities:
        seen: dict[str, Amenity] = {}
        for a in facts.amenities:
            key = a.name.strip().lower()
            if key and key not in seen:
                seen[key] = Amenity(name=a.name.strip().title(), category=a.category, scope=a.scope)
        project.amenities.observe(fv("amenities", list(seen.values())))
    launched = parse_month(facts.launched)
    if launched or facts.extensions_filed is not None or facts.construction_stage:
        project.timeline.observe(fv("timeline", Timeline(
            launched=launched, extensions_filed=facts.extensions_filed, construction_stage=facts.construction_stage,
        )))
    return project


def consolidate_rera(project: Project) -> Project:
    """If several sources mention RERA numbers, a number seen on MahaRERA counts as verified everywhere."""
    verified = {ph.number for v in project.rera_phases.values if v.prov.source == "maharera" for ph in v.value}
    for v in project.rera_phases.values:
        for ph in v.value:
            if ph.number in verified:
                ph.verified = True
    return project
