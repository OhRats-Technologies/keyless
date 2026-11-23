"""Local action history (SQLite) for the history surface."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from keyless.config.paths import AppPaths, current_paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    transcript TEXT NOT NULL DEFAULT '',
    route TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    latency_ms REAL NOT NULL DEFAULT 0,
    tier TEXT NOT NULL DEFAULT '',
    app TEXT NOT NULL DEFAULT '',
    success INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS actions_created_at ON actions(created_at DESC);
"""


@dataclass(frozen=True)
class ActionRecord:
    transcript: str
    route: str = ""
    target: str = ""
    result: str = ""
    latency_ms: float = 0.0
    tier: str = ""
    app: str = ""
    success: bool = True
    created_at: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "created_at": self.created_at,
            "transcript": self.transcript,
            "route": self.route,
            "target": self.target,
            "result": self.result,
            "latency_ms": self.latency_ms,
            "tier": self.tier,
            "app": self.app,
            "success": self.success,
        }


class HistoryStore:
    """Retention is user-configurable later; the schema stays small."""

    def __init__(self, paths: AppPaths | None = None, db_path: Path | None = None) -> None:
        self.paths = paths or current_paths()
        self.db_path = db_path or self.paths.history_db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def add(self, record: ActionRecord) -> int:
        created = record.created_at or datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO actions
                    (created_at, transcript, route, target, result, latency_ms, tier, app, success)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created,
                    record.transcript,
                    record.route,
                    record.target,
                    record.result,
                    float(record.latency_ms),
                    record.tier,
                    record.app,
                    1 if record.success else 0,
                ),
            )
            return int(cursor.lastrowid)

    def recent(self, limit: int = 100) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM actions ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
            ).fetchall()
        return [
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "transcript": row["transcript"],
                "route": row["route"],
                "target": row["target"],
                "result": row["result"],
                "latency_ms": row["latency_ms"],
                "tier": row["tier"],
                "app": row["app"],
                "success": bool(row["success"]),
            }
            for row in rows
        ]

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM actions")

    def delete(self, action_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM actions WHERE id = ?", (int(action_id),))
