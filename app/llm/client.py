"""The model does three jobs here: fill the form from a page, judge whether two names are one building, write insight sentences.

Provider is picked by LLM_PROVIDER (anthropic | groq); the rest of the pipeline never sees the difference.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from app.config import settings
from app.llm import prompts
from app.models.schema import Candidate, ExtractedFacts, OwnProject, Page, Project


class MatchVerdict(BaseModel):
    same_project: bool
    confidence: float = Field(ge=0, le=1)
    reason: str


class CardInsight(BaseModel):
    id: str
    sentence: str


class CardInsights(BaseModel):
    insights: list[CardInsight]


class SectionInsights(BaseModel):
    rate: str
    possession: str
    carpet: str
    configurations: str
    structure: str
    amenities: str


def build_chat(provider: str | None = None, model: str | None = None):
    """One chat model, whichever provider is configured. Both are OpenAI-style structured-output capable."""
    provider = provider or settings.llm_provider
    if provider == "groq":
        from langchain_groq import ChatGroq

        name = model or settings.groq_model
        kw = {"reasoning_effort": settings.groq_reasoning_effort} if "gpt-oss" in name else {}
        return ChatGroq(model=name, max_tokens=settings.groq_max_output_tokens, temperature=0, api_key=settings.groq_api_key,
                        max_retries=settings.groq_max_retries, **kw), "groq"
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(model=model or settings.claude_model, max_tokens=16000,
                         api_key=settings.anthropic_api_key), "anthropic"


class LLM:
    def __init__(self, model: str | None = None, provider: str | None = None):
        self.chat, self.provider = build_chat(provider, model)
        self.model = getattr(self.chat, "model_name", None) or getattr(self.chat, "model", "")
        self._extract = self.chat.with_structured_output(ExtractedFacts, method="json_schema")
        self._match = self.chat.with_structured_output(MatchVerdict, method="json_schema")
        self._cards = self.chat.with_structured_output(CardInsights, method="json_schema")
        self._sections = self.chat.with_structured_output(SectionInsights, method="json_schema")

    async def extract_facts(self, page: Page, project: Project, locality: str | None) -> ExtractedFacts:
        user = prompts.EXTRACT_USER.format(name=project.name, builder=project.builder or "unknown builder", locality=locality or "Mumbai",
                                           source=page.source, url=page.url, text=page.text[: settings.max_page_chars])
        out = await self._extract.ainvoke([("system", prompts.EXTRACT_SYSTEM), ("human", user)])
        return out if isinstance(out, ExtractedFacts) else ExtractedFacts(**out)

    async def same_project(self, a: Candidate, b: Candidate) -> MatchVerdict:
        user = prompts.SAME_PROJECT_USER.format(a_name=a.name, a_builder=a.builder or "", a_rera=a.rera_no or "", a_address=a.address or "",
                                                b_name=b.name, b_builder=b.builder or "", b_rera=b.rera_no or "", b_address=b.address or "")
        out = await self._match.ainvoke([("system", prompts.SAME_PROJECT_SYSTEM), ("human", user)])
        return out if isinstance(out, MatchVerdict) else MatchVerdict(**out)

    async def narrate_cards(self, own: OwnProject, projects: list[Project], radius_km: float, set_summary: dict) -> dict[str, str]:
        items = "\n".join(json.dumps(card_facts(p)) for p in projects)
        user = prompts.NARRATE_CARDS_USER.format(own=json.dumps(own_facts(own)), radius_km=radius_km, set_summary=json.dumps(set_summary), items=items)
        out = await self._cards.ainvoke([("system", prompts.NARRATE_SYSTEM), ("human", user)])
        out = out if isinstance(out, CardInsights) else CardInsights(**out)
        return {i.id: i.sentence for i in out.insights}

    async def narrate_compare(self, own: OwnProject, payload: dict[str, Any]) -> dict[str, str]:
        slim = {k: payload[k] for k in ("headline", "common_bhk", "rate_axis", "possession", "carpet", "config_matrix", "structure", "amenities")}
        slim["amenities"] = {k: v for k, v in slim["amenities"].items() if k != "rows"} | {"top_rows": payload["amenities"]["rows"][:12]}
        user = prompts.NARRATE_COMPARE_USER.format(own=json.dumps(own_facts(own)), payload=json.dumps(slim, default=str))
        out = await self._sections.ainvoke([("system", prompts.NARRATE_SYSTEM), ("human", user)])
        out = out if isinstance(out, SectionInsights) else SectionInsights(**out)
        return out.model_dump()


def own_facts(own: OwnProject) -> dict:
    return {"name": own.name, "builder": own.builder, "configurations": own.configurations,
            "carpet_sqft": [own.carpet_sqft.min_sqft, own.carpet_sqft.max_sqft],
            "rate_psf": [own.rate_psf.min_psf, own.rate_psf.max_psf, own.rate_psf.basis],
            "possession": own.possession.isoformat(), "building_type": own.structure.building_type, "towers": own.structure.towers}


def card_facts(p: Project) -> dict:
    c, r, s = p.value("carpet_sqft"), p.value("rate_psf"), p.value("structure")
    poss = p.value("possession")
    return {
        "id": p.id, "name": p.name, "builder": p.builder, "distance_km": p.distance_km, "status": p.status, "on_propog": p.on_propog,
        "label": p.label, "completeness": f"{p.completeness}/6", "match_score": p.match_score,
        "configurations": p.value("configurations"), "carpet_sqft": [c.min_sqft, c.max_sqft] if c else None,
        "rate_psf": [r.min_psf, r.max_psf, r.basis] if r else None, "possession": poss.isoformat() if poss else None,
        "towers": s.towers if s else None, "building_type": s.building_type if s else None,
        "rera_verified": any(ph.verified for fv in p.rera_phases for ph in fv.value),
        "conflicts": [f"{x.field}: {x.detail}" for x in p.conflicts], "could_not_verify": p.could_not_verify,
    }


def get_llm() -> LLM | None:
    return LLM() if settings.has_llm else None
