"""Where a run lives while it is running, and nowhere else.

The agent must never learn what a builder is. Tenancy is enforced in the Node API,
in one function, against Postgres. If the agent also stored projects there would be
two places a builder's data lives and two places that boundary can be got wrong.
The API asks a question, the agent answers it, the API decides who may see the
answer and stores it.

So this is a bounded in-memory dict with a TTL. Runs are lost on restart, which is
correct: the canonical copy lives in Postgres.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from app.cost import Cost, classify
from app.models.schema import OwnProject, ScanRecord, now_utc

RunStatus = Literal["queued", "running", "done", "failed"]


@dataclass
class RunRecord:
    run_id: str
    own: OwnProject
    radius_km: float
    mode: str
    status: RunStatus = "queued"
    stage: str | None = None
    created_at: datetime = field(default_factory=now_utc)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    log: list[str] = field(default_factory=list)
    stage_seconds: dict[str, float] = field(default_factory=dict)
    rec: ScanRecord | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] | None = None
    error: dict[str, str] | None = None
    cost: Cost = field(default_factory=Cost)
    extraction: dict[str, int] = field(default_factory=lambda: {"deterministic_fields": 0, "llm_fields": 0})
    # The published propOG projects the caller sent with the subject. Held for the
    # length of the run like everything else here, never written anywhere.
    nearby: list = field(default_factory=list)
    llm: Any = None

    @property
    def elapsed_s(self) -> float:
        start = self.started_at or self.created_at
        return round(((self.finished_at or now_utc()) - start).total_seconds(), 1)

    def begin(self, stage: str) -> None:
        self.status, self.stage = "running", stage

    def note(self, lines: list[str]) -> None:
        self.log.extend(l for l in lines if l not in self.log)

    def finish(self, rec: ScanRecord, meta: dict, payload: dict) -> None:
        self.status, self.stage = "done", None
        self.rec, self.meta, self.payload = rec, meta, payload
        self.finished_at = now_utc()

    def fail(self, exc: BaseException, stage: str | None = None) -> None:
        self.status = "failed"
        self.finished_at = now_utc()
        self.error = {
            "type": getattr(exc, "type", None) or _slug(type(exc).__name__),
            "message": str(exc) or type(exc).__name__,
            "stage": getattr(exc, "stage", None) or stage or self.stage or "unknown",
        }

    def tally(self, llm, meta: dict) -> None:
        """Counted during the run, reported at the end. Never a bill."""
        searches, places, _ = classify(meta.get("urls", []))
        self.cost = Cost(
            llm_calls=getattr(llm, "calls_attempted", 0),
            input_tokens=getattr(llm, "input_tokens", 0),
            output_tokens=getattr(llm, "output_tokens", 0),
            searches=searches, places_calls=places,
            pages_fetched=meta.get("pages_fetched", 0),
            model=getattr(llm, "model", "") or "",
            candidates=meta.get("candidates_researched", 0),
            by_stage=llm.usage_by_stage() if llm is not None else {},
        )
        self.cost.cache_hits = meta.get("cache_hits", 0)
        self.extraction = meta.get("extraction", self.extraction)

    def timing(self) -> dict[str, Any]:
        return {
            "started_at": (self.started_at or self.created_at).isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_s": self.elapsed_s,
            "per_stage_s": dict(sorted(self.stage_seconds.items(), key=lambda kv: -kv[1])),
        }

    def body(self) -> dict[str, Any]:
        """What GET /scans/{run_id} returns. The node names are already meaningful,
        so a loading screen can show real progress instead of a spinner."""
        out: dict[str, Any] = {
            "run_id": self.run_id,
            "status": self.status,
            "stage": self.stage,
            "stages_done": list(self.log),
            "elapsed_s": self.elapsed_s,
            "own": {"id": self.own.id, "name": self.own.name, "locality": self.own.locality},
            "radius_km": self.radius_km,
            "mode": self.mode,
            "created_at": self.created_at.isoformat(),
            "cost": self.cost.body(),
            "extraction": self.extraction,
            "timing": self.timing(),
        }
        if self.error:
            out["error"] = self.error
        if self.payload:
            out |= self.payload
        return out


def _slug(name: str) -> str:
    import re
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name.replace("Error", "")).lower().strip("_") or "error"


class RunStore:
    """dict[run_id, RunRecord], bounded and time-limited. Lost on restart."""

    def __init__(self, max_runs: int | None = None, ttl_hours: float | None = None):
        from app.config import settings

        max_runs = settings.max_runs_held if max_runs is None else max_runs
        ttl_hours = settings.run_ttl_hours if ttl_hours is None else ttl_hours
        self.max_runs = max_runs
        self.ttl = timedelta(hours=ttl_hours)
        self._runs: dict[str, RunRecord] = {}

    def create(self, own: OwnProject, radius_km: float, mode: str, run_id: str | None = None,
               nearby: list | None = None) -> RunRecord:
        """`run_id` lets the caller keep its own identifier.

        propOG inserts its row first and sends that UUID; its client reads no id back,
        so a run it cannot address by the id it already holds is a run it can never
        poll. Keying on theirs is what makes GET /scans/{run_id} usable from Node.
        """
        self._prune()
        run = RunRecord(run_id=run_id or uuid.uuid4().hex[:12], own=own, radius_km=radius_km, mode=mode,
                        nearby=list(nearby or []))
        self._runs[run.run_id] = run
        while len(self._runs) > self.max_runs:
            self._runs.pop(next(iter(self._runs)))
        return run

    def get(self, run_id: str) -> RunRecord | None:
        self._prune()
        return self._runs.get(run_id)

    def __len__(self) -> int:
        self._prune()
        return len(self._runs)

    def _prune(self) -> None:
        cutoff = datetime.now(timezone.utc) - self.ttl
        for run_id in [k for k, v in self._runs.items() if v.created_at < cutoff]:
            del self._runs[run_id]
