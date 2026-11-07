"""jobs.db: every posting ever fetched, when it was first and last seen, and when it was sent."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from jobfeed.models import Job

DEFAULT_PATH = Path("jobs.db")
SCHEMA_VERSION = 2  # 2: board_runs

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id          TEXT PRIMARY KEY,
  dedupe_key  TEXT NOT NULL,     -- normalized company + title
  first_seen  TEXT NOT NULL,
  last_seen   TEXT NOT NULL,
  matched     INTEGER NOT NULL,  -- 1 if it passed the filters on its last fetch
  emailed_at  TEXT,              -- NULL until it appears in a digest
  data        TEXT NOT NULL      -- the Job as JSON
);
CREATE INDEX IF NOT EXISTS jobs_dedupe ON jobs(dedupe_key);
CREATE TABLE IF NOT EXISTS board_runs (
  label     TEXT NOT NULL,      -- "greenhouse:stripe"
  run_at    TEXT NOT NULL,
  postings  INTEGER NOT NULL,
  error     TEXT,               -- NULL when the fetch worked
  PRIMARY KEY (label, run_at)
);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _ts(moment: datetime) -> str:
    """One fixed ISO format, so timestamps compare correctly as text in SQL."""
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Row:
    job: Job
    dedupe_key: str
    first_seen: datetime
    last_seen: datetime
    matched: bool
    emailed_at: datetime | None


class StoreError(Exception):
    """jobs.db is unreadable or from a newer version of jobfeed."""


class Store:
    def __init__(self, conn: sqlite3.Connection, path: Path, readonly: bool):
        self.conn = conn
        self.path = path
        self.readonly = readonly

    # --- opening ------------------------------------------------------------

    @classmethod
    def open(cls, path: Path = DEFAULT_PATH) -> Store:
        """Open for writing, creating the file and schema if needed."""
        try:
            conn = sqlite3.connect(path)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                conn.close()
                raise StoreError(f"{path} was written by a newer jobfeed (schema {version}); upgrade jobfeed")
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        except sqlite3.DatabaseError as exc:
            raise StoreError(f"can't open {path}: {exc}") from exc
        return cls(conn, path, readonly=False)

    @classmethod
    def open_readonly(cls, path: Path = DEFAULT_PATH) -> Store | None:
        """Open without any chance of writing; None when the file doesn't exist yet."""
        if not path.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
            conn.execute("SELECT 1 FROM jobs LIMIT 1")
        except sqlite3.DatabaseError as exc:
            raise StoreError(f"can't read {path}: {exc}") from exc
        return cls(conn, path, readonly=True)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- reads ----------------------------------------------------------------

    def _rows(self, sql: str, params: Iterable = ()) -> list[Row]:
        out = []
        for id_, key, first, last, matched, emailed, data in self.conn.execute(sql, tuple(params)):
            out.append(Row(
                job=Job.from_dict(json.loads(data)),
                dedupe_key=key,
                first_seen=datetime.fromisoformat(first),
                last_seen=datetime.fromisoformat(last),
                matched=bool(matched),
                emailed_at=datetime.fromisoformat(emailed) if emailed else None,
            ))
        return out

    _COLUMNS = "id, dedupe_key, first_seen, last_seen, matched, emailed_at, data"

    def get(self, ids: Iterable[str]) -> dict[str, Row]:
        ids = list(ids)
        found: dict[str, Row] = {}
        for i in range(0, len(ids), 500):  # stay under SQLite's parameter limit
            chunk = ids[i: i + 500]
            marks = ",".join("?" * len(chunk))
            for row in self._rows(f"SELECT {self._COLUMNS} FROM jobs WHERE id IN ({marks})", chunk):
                found[row.job.id] = row
        return found

    def emailed_keys_since(self, keys: Iterable[str], since: datetime) -> set[str]:
        keys = list(set(keys))
        hit: set[str] = set()
        for i in range(0, len(keys), 500):
            chunk = keys[i: i + 500]
            marks = ",".join("?" * len(chunk))
            sql = f"SELECT DISTINCT dedupe_key FROM jobs WHERE emailed_at >= ? AND dedupe_key IN ({marks})"
            hit.update(k for (k,) in self.conn.execute(sql, (_ts(since), *chunk)))
        return hit

    def all_rows(self, *, seen_since: datetime | None = None) -> list[Row]:
        if seen_since is None:
            return self._rows(f"SELECT {self._COLUMNS} FROM jobs ORDER BY first_seen, id")
        return self._rows(f"SELECT {self._COLUMNS} FROM jobs WHERE last_seen >= ? ORDER BY first_seen, id",
                          (_ts(seen_since),))

    def previous_counts(self, labels: Iterable[str]) -> dict[str, int]:
        """Postings each board had on its most recent successful run."""
        labels = list(set(labels))
        found: dict[str, int] = {}
        try:
            for i in range(0, len(labels), 500):
                chunk = labels[i: i + 500]
                marks = ",".join("?" * len(chunk))
                sql = f"""
                    SELECT label, postings FROM board_runs AS b
                    WHERE error IS NULL AND label IN ({marks}) AND run_at = (
                      SELECT MAX(run_at) FROM board_runs WHERE label = b.label AND error IS NULL)"""
                found.update(dict(self.conn.execute(sql, chunk)))
        except sqlite3.OperationalError:
            return {}  # a read-only copy of a schema-1 database has no board history yet
        return found

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    # --- writes ---------------------------------------------------------------

    def _writable(self) -> None:
        if self.readonly:
            raise StoreError("store was opened read-only")

    def save_run(self, jobs: list[tuple[Job, str, bool]], sent_ids: Iterable[str], now: datetime,
                 boards: Iterable[tuple[str, int, str | None]] = ()) -> None:
        """Upsert this run's postings and mark the sent ones, in one transaction.

        jobs: (job, dedupe_key, matched). New rows get first_seen = now; known rows
        keep first_seen and emailed_at and get the latest content and last_seen.
        """
        self._writable()
        stamp = _ts(now)
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO jobs (id, dedupe_key, first_seen, last_seen, matched, emailed_at, data)
                VALUES (?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(id) DO UPDATE SET
                  dedupe_key = excluded.dedupe_key,
                  last_seen  = excluded.last_seen,
                  matched    = excluded.matched,
                  data       = excluded.data
                """,
                [(job.id, key, stamp, stamp, int(matched), json.dumps(job.to_dict(), ensure_ascii=False))
                 for job, key, matched in jobs],
            )
            self.conn.executemany(
                "UPDATE jobs SET emailed_at = ? WHERE id = ? AND emailed_at IS NULL",
                [(stamp, id_) for id_ in sent_ids],
            )
            self.conn.executemany(
                "INSERT OR REPLACE INTO board_runs (label, run_at, postings, error) VALUES (?, ?, ?, ?)",
                [(label, stamp, postings, error) for label, postings, error in boards],
            )
