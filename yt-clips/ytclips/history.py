"""Historique durable des envois (SQLite) pour empêcher les doublons."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

PENDING, UPLOADED, FAILED = "pending", "uploaded", "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL,
    clip_sha256 TEXT NOT NULL,
    source_name TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    privacy TEXT,
    video_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS uploads_dedupe ON uploads (dedupe_key);
CREATE INDEX IF NOT EXISTS uploads_clip ON uploads (clip_sha256);
"""


def dedupe_key(source_sha256: str, start_ms: int, end_ms: int) -> str:
    """Un même extrait (même source, mêmes bornes) n'est envoyé qu'une fois."""
    return f"{source_sha256}:{start_ms}:{end_ms}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DuplicateError(Exception):
    def __init__(self, row: sqlite3.Row):
        self.row = row
        super().__init__(f"extrait déjà présent dans l'historique (n° {row['id']}, état {row['status']})")


class History:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def find_blocking(self, key: str, clip_sha256: str) -> sqlite3.Row | None:
        with closing(self._connect()) as db:
            return self._find_blocking(db, key, clip_sha256)

    @staticmethod
    def _find_blocking(db: sqlite3.Connection, key: str, clip_sha256: str) -> sqlite3.Row | None:
        return db.execute(
            "SELECT * FROM uploads WHERE (dedupe_key = ? OR clip_sha256 = ?) AND status IN (?, ?) "
            "ORDER BY id LIMIT 1",
            (key, clip_sha256, PENDING, UPLOADED),
        ).fetchone()

    def claim(
        self, key: str, clip_sha256: str, source_name: str, start_ms: int, end_ms: int,
        title: str, force: bool = False,
    ) -> int:
        """Réserve l'extrait avant l'envoi ; lève DuplicateError s'il est déjà pris."""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = None if force else self._find_blocking(db, key, clip_sha256)
                if existing is not None:
                    raise DuplicateError(existing)
                now = _now()
                cursor = db.execute(
                    "INSERT INTO uploads (dedupe_key, clip_sha256, source_name, start_ms, end_ms, title, "
                    "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, clip_sha256, source_name, start_ms, end_ms, title, PENDING, now, now),
                )
                db.execute("COMMIT")
                return cursor.lastrowid
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def _update(self, row_id: int, **fields) -> None:
        fields["updated_at"] = _now()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with closing(self._connect()) as db:
            db.execute(f"UPDATE uploads SET {assignments} WHERE id = ?", (*fields.values(), row_id))

    def mark_uploaded(self, row_id: int, video_id: str, privacy: str) -> None:
        self._update(row_id, status=UPLOADED, video_id=video_id, privacy=privacy, error=None)

    def mark_failed(self, row_id: int, error: str) -> None:
        self._update(row_id, status=FAILED, error=error[:1000])

    def note_error(self, row_id: int, error: str) -> None:
        """Issue inconnue : l'entrée reste « pending » et continue de bloquer."""
        self._update(row_id, error=error[:1000])

    def release(self, row_id: int) -> bool:
        with closing(self._connect()) as db:
            cursor = db.execute(
                "UPDATE uploads SET status = ?, updated_at = ?, error = COALESCE(error, '') || ' [libéré à la main]' "
                "WHERE id = ? AND status = ?",
                (FAILED, _now(), row_id, PENDING),
            )
            return cursor.rowcount == 1

    def rows(self) -> list[sqlite3.Row]:
        with closing(self._connect()) as db:
            return db.execute("SELECT * FROM uploads ORDER BY id").fetchall()
