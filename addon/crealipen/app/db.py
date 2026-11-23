"""SQLite persistence for Drawings and Jobs. Files on disk, metadata in DB."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS drawings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    image_path TEXT NOT NULL,
    width_px INTEGER NOT NULL,
    height_px INTEGER NOT NULL,
    svg TEXT NOT NULL,
    settings TEXT NOT NULL,
    gcode TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    drawing_id INTEGER NOT NULL REFERENCES drawings(id),
    status TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);
"""


class Database:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def create_drawing(
        self, name: str, image_path: str, width_px: int, height_px: int,
        svg: str, settings: dict,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO drawings (name, image_path, width_px, height_px, svg, settings)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (name, image_path, width_px, height_px, svg, json.dumps(settings)),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_drawing(self, drawing_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM drawings WHERE id = ?", (drawing_id,)).fetchone()
            return dict(row) | {"settings": json.loads(row["settings"])} if row else None

    def update_drawing(self, drawing_id: int, *, svg: str, settings: dict, gcode: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE drawings SET svg = ?, settings = ?, gcode = COALESCE(?, gcode) WHERE id = ?",
                (svg, json.dumps(settings), gcode, drawing_id),
            )
            self._conn.commit()

    def set_gcode(self, drawing_id: int, gcode: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE drawings SET gcode = ? WHERE id = ?", (gcode, drawing_id))
            self._conn.commit()

    def create_job(self, drawing_id: int) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO jobs (drawing_id, status) VALUES (?, 'pending')", (drawing_id,)
            )
            self._conn.commit()
            return cur.lastrowid

    def update_job(self, job_id: int, *, status: str, progress: int | None = None, error: str | None = None) -> None:
        with self._lock:
            finished = ", finished_at = datetime('now')" if status in ("done", "failed", "cancelled") else ""
            self._conn.execute(
                f"UPDATE jobs SET status = ?{finished},"
                " progress = COALESCE(?, progress), error = ? WHERE id = ?",
                (status, progress, error, job_id),
            )
            self._conn.commit()

    def get_job(self, job_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row else None

    def fail_stale_jobs(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status = 'failed', error = 'interrupted by app restart',"
                " finished_at = datetime('now')"
                " WHERE status IN ('pending', 'running')"
            )
            self._conn.commit()
            return cur.rowcount
