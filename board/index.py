"""
index.py - Fast SQLite-backed index cache for PM & QA Cockpit.

Provides incremental caching for markdown cards and artifacts:
- Uses standard library sqlite3 only (no external dependencies, zero admin rights).
- Database is stored in user space (~/.pm-qa-cockpit/cache/<slug>.db).
- Tracks file modification times (mtime) and sizes so unchanged markdown files
  are never re-read or re-parsed from disk.
- Automatically recovers / rebuilds if the cache file is deleted or corrupted.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any


class CardIndex:
    """Manages an incremental SQLite cache for card files."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            self.db_path = ":memory:"
        else:
            self.db_path = Path(db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path) if isinstance(self.db_path, Path) else self.db_path,
            check_same_thread=False,
            timeout=10.0,
        )
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS card_cache (
                            file_path TEXT PRIMARY KEY,
                            mtime REAL NOT NULL,
                            size INTEGER NOT NULL,
                            card_id TEXT NOT NULL,
                            status TEXT NOT NULL,
                            sprint TEXT,
                            data_json TEXT NOT NULL
                        )
                        """
                    )
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_card_status ON card_cache(status)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_card_id ON card_cache(card_id)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_card_sprint ON card_cache(sprint)")
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS meta_cache (
                            key TEXT PRIMARY KEY,
                            value TEXT NOT NULL
                        )
                        """
                    )
            finally:
                conn.close()

    def get_all_file_stats(self) -> dict[str, tuple[float, int]]:
        """Returns {file_path: (mtime, size)} for all indexed cards."""
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("SELECT file_path, mtime, size FROM card_cache")
                return {row[0]: (row[1], row[2]) for row in cur.fetchall()}
            finally:
                conn.close()

    def get_all_cards(self) -> dict[str, dict[str, Any]]:
        """Returns {file_path: parsed_card_dict} for all indexed cards."""
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("SELECT file_path, data_json FROM card_cache")
                result: dict[str, dict[str, Any]] = {}
                for f_path, data_json in cur.fetchall():
                    try:
                        result[f_path] = json.loads(data_json)
                    except Exception:
                        pass
                return result
            finally:
                conn.close()

    def get_card(self, file_path: str) -> dict[str, Any] | None:
        """Returns parsed card dictionary for a single file path if present."""
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("SELECT data_json FROM card_cache WHERE file_path = ?", (file_path,))
                row = cur.fetchone()
                if row:
                    try:
                        return json.loads(row[0])
                    except Exception:
                        return None
                return None
            finally:
                conn.close()

    def upsert_cards_batch(self, entries: list[tuple[str, float, int, dict[str, Any]]]) -> None:
        """Batch inserts or updates card cache entries: [(file_path, mtime, size, card_dict), ...]."""
        if not entries:
            return
        records = []
        for file_path, mtime, size, card in entries:
            cid = str(card.get("mn_id") or card.get("id") or "")
            status = str(card.get("status") or "")
            sprint = str(card.get("sprint") or "")
            data_json = json.dumps(card, ensure_ascii=False)
            records.append((file_path, mtime, size, cid, status, sprint, data_json))

        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    conn.executemany(
                        """
                        INSERT INTO card_cache (file_path, mtime, size, card_id, status, sprint, data_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(file_path) DO UPDATE SET
                            mtime = excluded.mtime,
                            size = excluded.size,
                            card_id = excluded.card_id,
                            status = excluded.status,
                            sprint = excluded.sprint,
                            data_json = excluded.data_json
                        """,
                        records,
                    )
            finally:
                conn.close()

    def remove_cards(self, file_paths: list[str]) -> None:
        """Removes deleted files from the cache."""
        if not file_paths:
            return
        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    conn.executemany(
                        "DELETE FROM card_cache WHERE file_path = ?",
                        [(p,) for p in file_paths],
                    )
            finally:
                conn.close()

    def count_by_status(self, status: str) -> int:
        """Fast SQL count of cards by status without loading JSON."""
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM card_cache WHERE status = ?", (status.lower().strip(),))
                row = cur.fetchone()
                return int(row[0]) if row else 0
            finally:
                conn.close()

    def clear(self) -> None:
        """Wipes the card cache completely."""
        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    conn.execute("DELETE FROM card_cache")
                    conn.execute("DELETE FROM meta_cache")
            finally:
                conn.close()
