"""SQLite with JSON blobs. Enough for a demo; the portal would map this onto its own tables."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.models.schema import OwnProject, Project, ScanRecord


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS own_projects (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS scans (
            scan_id TEXT PRIMARY KEY, own_id TEXT NOT NULL, created_at TEXT NOT NULL, body TEXT NOT NULL, meta TEXT NOT NULL)""")
        self.conn.commit()

    # own projects
    def save_own(self, own: OwnProject) -> None:
        self.conn.execute("INSERT OR REPLACE INTO own_projects VALUES (?, ?)", (own.id, own.model_dump_json()))
        self.conn.commit()

    def get_own(self, own_id: str) -> OwnProject | None:
        row = self.conn.execute("SELECT body FROM own_projects WHERE id=?", (own_id,)).fetchone()
        return OwnProject.model_validate_json(row[0]) if row else None

    def list_own(self) -> list[OwnProject]:
        return [OwnProject.model_validate_json(r[0]) for r in self.conn.execute("SELECT body FROM own_projects")]

    # scans
    def save_scan(self, rec: ScanRecord, nearest_metro: str | None = None, log: list[str] | None = None) -> None:
        meta = json.dumps({"nearest_metro": nearest_metro, "log": log or []})
        self.conn.execute("INSERT OR REPLACE INTO scans VALUES (?, ?, ?, ?, ?)",
                          (rec.scan_id, rec.own_id, rec.created_at.isoformat(), rec.model_dump_json(), meta))
        self.conn.commit()

    def get_scan(self, scan_id: str) -> tuple[ScanRecord, dict] | None:
        row = self.conn.execute("SELECT body, meta FROM scans WHERE scan_id=?", (scan_id,)).fetchone()
        return (ScanRecord.model_validate_json(row[0]), json.loads(row[1])) if row else None

    def latest_scan(self, own_id: str) -> tuple[ScanRecord, dict] | None:
        row = self.conn.execute("SELECT body, meta FROM scans WHERE own_id=? ORDER BY created_at DESC LIMIT 1", (own_id,)).fetchone()
        return (ScanRecord.model_validate_json(row[0]), json.loads(row[1])) if row else None

    def find_project(self, project_id: str, own_id: str | None = None) -> tuple[ScanRecord, Project, dict] | None:
        q = "SELECT body, meta FROM scans" + (" WHERE own_id=?" if own_id else "") + " ORDER BY created_at DESC"
        for body, meta in self.conn.execute(q, (own_id,) if own_id else ()):
            rec = ScanRecord.model_validate_json(body)
            for p in rec.projects:
                if p.id == project_id:
                    return rec, p, json.loads(meta)
        return None

    def update_project(self, rec: ScanRecord, project: Project, meta: dict) -> None:
        rec.projects = [project if p.id == project.id else p for p in rec.projects]
        self.conn.execute("UPDATE scans SET body=?, meta=? WHERE scan_id=?", (rec.model_dump_json(), json.dumps(meta), rec.scan_id))
        self.conn.commit()
