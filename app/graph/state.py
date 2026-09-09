from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from app.models.schema import Candidate, OwnProject, Project


def merge_by_id(left: list[Project], right: list[Project]) -> list[Project]:
    d = {p.id: p for p in left}
    for p in right:
        d[p.id] = p
    return list(d.values())


def add_counts(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    return {k: left.get(k, 0) + right.get(k, 0) for k in set(left) | set(right)}


class GraphState(TypedDict, total=False):
    own: OwnProject
    radius_km: float
    mode: str
    scan_id: str
    candidates: Annotated[list[Candidate], operator.add]
    projects: Annotated[list[Project], merge_by_id]
    dropped: Annotated[list[dict[str, Any]], operator.add]
    register_filings: Annotated[list[dict[str, Any]], operator.add]
    societies: Annotated[list[dict[str, Any]], operator.add]
    retry_done: bool
    nearest_metro: str | None
    log: Annotated[list[str], operator.add]
    extraction: Annotated[dict[str, int], add_counts]
    pages_fetched: Annotated[int, operator.add]
    # What extraction put in front of the model, or would have: the input side of the
    # cost question, measured on the real pages rather than guessed from a page count.
    model_pages: Annotated[int, operator.add]
    prompt_chars: Annotated[int, operator.add]
    ambiguous_pairs: Annotated[int, operator.add]


class ExtractInput(TypedDict):
    own: OwnProject
    radius_km: float
    project: Project
    retry: bool
    extra_queries: list[str]
