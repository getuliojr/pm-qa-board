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

# Bump when the shape of any cached row changes (card dicts, git rows, analytics payloads):
# a database stamped with another version is wiped and rebuilt on open.
SCHEMA_VERSION = 2

_CACHE_TABLES = ("card_cache", "git_commits", "git_sync", "analytics_cache")


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
                    current = conn.execute("PRAGMA user_version").fetchone()[0]
                    if current != SCHEMA_VERSION:
                        for table in _CACHE_TABLES:
                            conn.execute(f"DROP TABLE IF EXISTS {table}")
                        conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
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
                        CREATE TABLE IF NOT EXISTS git_commits (
                            sha TEXT PRIMARY KEY,
                            date TEXT NOT NULL,
                            subject TEXT NOT NULL,
                            author TEXT NOT NULL,
                            added INTEGER NOT NULL DEFAULT 0,
                            deleted INTEGER NOT NULL DEFAULT 0,
                            counted INTEGER NOT NULL DEFAULT 0,
                            reachable INTEGER NOT NULL DEFAULT 1
                        )
                        """
                    )
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_git_date ON git_commits(date)")
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_git_reachable_date ON git_commits(reachable, date DESC, sha)"
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_git_uncounted ON git_commits(counted, date DESC, sha) "
                        "WHERE reachable = 1 AND counted = 0"
                    )
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS git_sync (id INTEGER PRIMARY KEY CHECK (id = 1), bound TEXT, head TEXT)"
                    )
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS analytics_cache (
                            window TEXT NOT NULL,
                            scope TEXT NOT NULL,
                            key_hash TEXT NOT NULL,
                            created_at REAL NOT NULL,
                            payload_json TEXT NOT NULL,
                            PRIMARY KEY (window, scope)
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
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Git commit cache (one row per commit; commits are immutable)
    # ------------------------------------------------------------------
    def _run(self, fn):
        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    return fn(conn)
            finally:
                conn.close()

    def get_git_sync(self) -> tuple[str, str] | None:
        """(bound, head) the commit table was last synced against, if any."""
        row = self._run(lambda c: c.execute("SELECT bound, head FROM git_sync WHERE id = 1").fetchone())
        return (row[0] or "", row[1] or "") if row else None

    def sync_git_reachable(self, bound: str, head: str, commits: list[tuple[str, str, str, str]]) -> None:
        """Record the commits reachable from ``head`` since ``bound``: [(sha, date, subject, author), ...].

        Known SHAs keep their line counts; new ones start uncounted. Every other row is marked
        unreachable (kept so a checkout back needs no re-read) and rows older than ``bound`` go.
        """

        def op(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE git_commits SET reachable = 0")
            conn.executemany(
                """
                INSERT INTO git_commits (sha, date, subject, author, reachable) VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(sha) DO UPDATE SET reachable = 1
                """,
                commits,
            )
            conn.execute("DELETE FROM git_commits WHERE date < ?", (bound,))
            conn.execute(
                "INSERT INTO git_sync (id, bound, head) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET bound = excluded.bound, head = excluded.head",
                (bound, head),
            )

        self._run(op)

    def get_uncounted_shas(self, limit: int = 100) -> list[str]:
        """Newest-first reachable SHAs whose line counts have not been read yet."""
        rows = self._run(
            lambda c: c.execute(
                "SELECT sha FROM git_commits WHERE reachable = 1 AND counted = 0 ORDER BY date DESC, sha LIMIT ?",
                (int(limit),),
            ).fetchall()
        )
        return [r[0] for r in rows]

    def set_commit_stats(self, stats: list[tuple[str, int, int]]) -> None:
        """Mark commits counted: [(sha, added, deleted), ...]."""
        if not stats:
            return
        self._run(
            lambda c: c.executemany(
                "UPDATE git_commits SET added = ?, deleted = ?, counted = 1 WHERE sha = ?",
                [(a, d, sha) for sha, a, d in stats],
            )
        )

    def get_git_commits(self, since: str) -> list[dict[str, Any]]:
        """Reachable commits dated on/after ``since`` (YYYY-MM-DD), newest first."""
        rows = self._run(
            lambda c: c.execute(
                "SELECT sha, date, subject, author, added, deleted FROM git_commits "
                "WHERE reachable = 1 AND date >= ? ORDER BY date DESC, sha",
                (since,),
            ).fetchall()
        )
        return [
            {"sha": r[0][:7], "date": r[1], "subject": r[2], "author": r[3], "added": r[4], "deleted": r[5]}
            for r in rows
        ]

    def git_progress(self, since: str) -> tuple[int, int]:
        """(counted, total) reachable commits dated on/after ``since``."""
        row = self._run(
            lambda c: c.execute(
                "SELECT COALESCE(SUM(counted), 0), COUNT(*) FROM git_commits WHERE reachable = 1 AND date >= ?",
                (since,),
            ).fetchone()
        )
        return int(row[0]), int(row[1])

    # ------------------------------------------------------------------
    # Analytics payload cache (latest row per window + scope)
    # ------------------------------------------------------------------
    def get_analytics(
        self, window: str, scope: str, key_hash: str, max_age: float, now: float
    ) -> tuple[float, dict[str, Any]] | None:
        """(created_at, payload) when the stored key matches and the row is younger than ``max_age``."""
        row = self._run(
            lambda c: c.execute(
                "SELECT key_hash, created_at, payload_json FROM analytics_cache WHERE window = ? AND scope = ?",
                (window, scope),
            ).fetchone()
        )
        if not row or row[0] != key_hash or (now - row[1]) >= max_age:
            return None
        try:
            return row[1], json.loads(row[2])
        except Exception:
            return None

    def put_analytics(self, window: str, scope: str, key_hash: str, now: float, payload_json: str) -> None:
        self._run(
            lambda c: c.execute(
                """
                INSERT INTO analytics_cache (window, scope, key_hash, created_at, payload_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(window, scope) DO UPDATE SET
                    key_hash = excluded.key_hash,
                    created_at = excluded.created_at,
                    payload_json = excluded.payload_json
                """,
                (window, scope, key_hash, now, payload_json),
            )
        )
