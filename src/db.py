"""
SQLite-backed local cache for synced Rally TestCase data.

Why SQLite: the dashboard needs to read a potentially large TestCase
population fast and repeatedly (every filter change) without hammering the
Rally API. This layer is the "data layer" boundary — sync_service.py writes
to it, metrics.py / app.py only ever read from it. Swapping SQLite for
Postgres later is a matter of changing the connection string + a couple of
`?`-vs-`%s` placeholders; nothing above this layer needs to change.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional

from src.rally_client import TestCaseRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS test_cases (
    formatted_id       TEXT PRIMARY KEY,
    name                TEXT,
    owner               TEXT,
    owner_ref           TEXT,
    project             TEXT,
    project_ref         TEXT,
    workspace           TEXT,
    creation_date       TEXT,
    last_update_date    TEXT,
    tags_json           TEXT,      -- json list of raw tag names
    category             TEXT,      -- "AI-Assisted" | "Manual" | "Unclassified"
    synced_at           TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tc_owner ON test_cases(owner);
CREATE INDEX IF NOT EXISTS idx_tc_project ON test_cases(project);
CREATE INDEX IF NOT EXISTS idx_tc_creation ON test_cases(creation_date);
CREATE INDEX IF NOT EXISTS idx_tc_category ON test_cases(category);

CREATE TABLE IF NOT EXISTS sync_state (
    key         TEXT PRIMARY KEY,
    value       TEXT
);

CREATE TABLE IF NOT EXISTS projects (
    name         TEXT PRIMARY KEY,
    object_id    TEXT,
    parent_name  TEXT,
    state        TEXT,
    synced_at    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_projects_parent ON projects(parent_name);

CREATE TABLE IF NOT EXISTS test_case_results (
    result_object_id        TEXT PRIMARY KEY,
    test_case_formatted_id  TEXT,
    execution_date          TEXT,
    verdict                 TEXT,
    tester                  TEXT,
    build                   TEXT,
    synced_at               TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_results_tc ON test_case_results(test_case_formatted_id);
CREATE INDEX IF NOT EXISTS idx_results_date ON test_case_results(execution_date);
"""


class Database:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    def upsert_test_cases(self, records: Iterable[TestCaseRecord], categories: dict[str, str]):
        """
        categories: mapping formatted_id -> category, computed by the
        classifier at sync time (so the dashboard never re-classifies on
        every page load — re-running a sync is how you pick up config changes).
        """
        rows = []
        for r in records:
            rows.append(
                (
                    r.formatted_id,
                    r.name,
                    r.owner,
                    r.owner_ref,
                    r.project,
                    r.project_ref,
                    r.workspace,
                    r.creation_date,
                    r.last_update_date,
                    json.dumps(r.tags),
                    categories.get(r.formatted_id, "Unclassified"),
                )
            )
        with self._conn() as conn:
            conn.executemany(
                """
                INSERT INTO test_cases (
                    formatted_id, name, owner, owner_ref, project, project_ref,
                    workspace, creation_date, last_update_date, tags_json, category,
                    synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(formatted_id) DO UPDATE SET
                    name=excluded.name,
                    owner=excluded.owner,
                    owner_ref=excluded.owner_ref,
                    project=excluded.project,
                    project_ref=excluded.project_ref,
                    workspace=excluded.workspace,
                    creation_date=excluded.creation_date,
                    last_update_date=excluded.last_update_date,
                    tags_json=excluded.tags_json,
                    category=excluded.category,
                    synced_at=datetime('now')
                """,
                rows,
            )

    def fetch_all(self) -> list[dict]:
        with self._conn() as conn:
            cur = conn.execute("SELECT * FROM test_cases")
            return [dict(row) for row in cur.fetchall()]

    def count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) AS c FROM test_cases").fetchone()["c"]

    # ------------------------------------------------------------------
    # sync_state — tracks last-successful-sync watermark per workspace,
    # enabling incremental refresh instead of a full re-pull every run.
    # ------------------------------------------------------------------
    def get_last_sync(self, workspace_id: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM sync_state WHERE key = ?",
                (f"last_sync::{workspace_id}",),
            ).fetchone()
            return row["value"] if row else None

    def set_last_sync(self, workspace_id: str, iso_timestamp: str):
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO sync_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (f"last_sync::{workspace_id}", iso_timestamp),
            )
# ------------------------------------------------------------------
    # projects — Rally's real parent/child project hierarchy, used to
    # auto-expand a program's listed projects to include sub-projects.
    # ------------------------------------------------------------------
    def upsert_projects(self, projects: list[dict]):
        rows = [
            (p["name"], p.get("object_id"), p.get("parent_name"), p.get("state"))
            for p in projects
        ]
        with self._conn() as conn:
            conn.executemany(
                """
                INSERT INTO projects (name, object_id, parent_name, state, synced_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(name) DO UPDATE SET
                    object_id=excluded.object_id,
                    parent_name=excluded.parent_name,
                    state=excluded.state,
                    synced_at=datetime('now')
                """,
                rows,
            )

    def get_project_hierarchy(self) -> list[dict]:
        with self._conn() as conn:
            cur = conn.execute("SELECT name, parent_name FROM projects")
            return [dict(row) for row in cur.fetchall()]
        
    # ------------------------------------------------------------------
    # test_case_results — actual test EXECUTIONS, distinct from the
    # TestCase definitions in the `test_cases` table above.
    # ------------------------------------------------------------------
    def upsert_test_case_results(self, records) -> None:
        rows = [
            (
                r.result_object_id, r.test_case_formatted_id, r.execution_date,
                r.verdict, r.tester, r.build,
            )
            for r in records
        ]
        with self._conn() as conn:
            conn.executemany(
                """
                INSERT INTO test_case_results (
                    result_object_id, test_case_formatted_id, execution_date,
                    verdict, tester, build, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(result_object_id) DO UPDATE SET
                    test_case_formatted_id=excluded.test_case_formatted_id,
                    execution_date=excluded.execution_date,
                    verdict=excluded.verdict,
                    tester=excluded.tester,
                    build=excluded.build,
                    synced_at=datetime('now')
                """,
                rows,
            )

    def fetch_all_results(self) -> list[dict]:
        with self._conn() as conn:
            cur = conn.execute("SELECT * FROM test_case_results")
            return [dict(row) for row in cur.fetchall()]