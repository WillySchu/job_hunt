"""jobs.db: every posting ever fetched, when it was first and last seen and sent, plus applications."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

from jobfeed.models import Job

DEFAULT_PATH = Path("jobs.db")
SCHEMA_VERSION = 5  # 2: board_runs; 3: digest codes, applications, events, mail_seen; 4: events.reported;
                    # 5: meta, notices
FIRST_CODE = 101   # digest codes start here, so every code has at least three digits

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
CREATE TABLE IF NOT EXISTS applications (
  code            INTEGER PRIMARY KEY,         -- the posting's digest number, or the next free one
  job_id          TEXT REFERENCES jobs(id),    -- NULL for roles found outside jobfeed
  company         TEXT NOT NULL,
  title           TEXT NOT NULL,
  url             TEXT,
  found_via       TEXT NOT NULL,   -- greenhouse | remotive | ... | referral | linkedin | recruiter | other
  digest_section  TEXT,            -- watchlist | matches | no_salary
  referral        INTEGER NOT NULL DEFAULT 0,
  resume_version  TEXT,
  cover_letter    INTEGER NOT NULL DEFAULT 0,
  autofilled      INTEGER,         -- NULL until form filling exists
  fit_score       REAL,            -- NULL until the scorer exists
  first_seen      TEXT,            -- copied from jobs, for days to apply
  applied_at      TEXT NOT NULL,
  logged_via      TEXT NOT NULL,   -- code | reply | manual | confirmation
  stage           TEXT NOT NULL,   -- latest stage, derived from events
  stage_at        TEXT NOT NULL,
  notes           TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY,
  code        INTEGER NOT NULL REFERENCES applications(code),
  at          TEXT NOT NULL,
  kind        TEXT NOT NULL,   -- applied | reply | screen | interview | offer | accepted | declined
                               -- | rejected | withdrawn | no_response | reopened | note
  origin      TEXT NOT NULL,   -- cli | reply | email | rule
  message_id  TEXT,            -- the Gmail message it came from
  detail      TEXT             -- the stage a rejection came at, or a note's text
);
CREATE INDEX IF NOT EXISTS events_code ON events(code, at);
CREATE TABLE IF NOT EXISTS mail_seen (
  message_id  TEXT PRIMARY KEY,   -- Gmail's ID, so a message is never counted twice
  seen_at     TEXT NOT NULL,
  kind        TEXT,               -- what it was read as; NULL if ignored
  code        INTEGER             -- the application it was tied to
);
CREATE TABLE IF NOT EXISTS meta (
  key    TEXT PRIMARY KEY,   -- e.g. mail_checked
  value  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notices (
  id        INTEGER PRIMARY KEY,
  at        TEXT NOT NULL,
  kind      TEXT NOT NULL,   -- reply_line: a reply line that couldn't be used
  text      TEXT NOT NULL,   -- shown as is in the next digest
  reported  TEXT             -- when a digest carried it
);
"""

# Columns added to tables after they were first created, and when.
_ADDED_COLUMNS = {
    "jobs": [
        ("code", "INTEGER"),     # 3: digest number, set when a posting is first emailed
        ("section", "TEXT"),     # 3: digest section it was emailed in
    ],
    "events": [
        ("reported", "TEXT"),    # 4: when a digest first carried it; NULL until then
    ],
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _job_columns(conn: sqlite3.Connection) -> set[str]:
    return _columns(conn, "jobs")


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
    code: int | None = None
    section: str | None = None


class StoreError(Exception):
    """jobs.db is unreadable or from a newer version of jobfeed."""


def _migrate(conn: sqlite3.Connection, path: Path) -> None:
    """Create the schema, or bring an older one up to date."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        conn.close()
        raise StoreError(f"{path} was written by a newer jobfeed (schema {version}); upgrade jobfeed")
    conn.executescript(_SCHEMA)
    for table, added in _ADDED_COLUMNS.items():
        present = _columns(conn, table)
        for name, kind in added:
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS jobs_code ON jobs(code)")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


class Store:
    def __init__(self, conn: sqlite3.Connection, path: Path, readonly: bool):
        self.conn = conn
        self.path = path
        self.readonly = readonly
        # A read-only open of an older database may lack the newer columns and tables.
        self.has_codes = "code" in _job_columns(conn)
        self.has_tracker = "reported" in _columns(conn, "events") and bool(_columns(conn, "notices"))

    # --- opening ------------------------------------------------------------

    @classmethod
    def open(cls, path: Path = DEFAULT_PATH) -> Store:
        """Open for writing, creating the file and schema if needed."""
        try:
            conn = sqlite3.connect(path)
            _migrate(conn, path)
        except sqlite3.DatabaseError as exc:
            raise StoreError(f"can't open {path}: {exc}") from exc
        return cls(conn, path, readonly=False)

    @classmethod
    def scratch_copy(cls, path: Path = DEFAULT_PATH) -> Store:
        """A writable copy of jobs.db in memory, for trying something out; the file is never touched."""
        try:
            mem = sqlite3.connect(":memory:")
            if path.exists():
                src = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
                src.backup(mem)
                src.close()
            _migrate(mem, path)
        except sqlite3.DatabaseError as exc:
            raise StoreError(f"can't read {path}: {exc}") from exc
        return cls(mem, path, readonly=False)

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
        for id_, key, first, last, matched, emailed, data, code, section in self.conn.execute(sql, tuple(params)):
            out.append(Row(
                job=Job.from_dict(json.loads(data)),
                dedupe_key=key,
                first_seen=datetime.fromisoformat(first),
                last_seen=datetime.fromisoformat(last),
                matched=bool(matched),
                emailed_at=datetime.fromisoformat(emailed) if emailed else None,
                code=code,
                section=section,
            ))
        return out

    @property
    def _COLUMNS(self) -> str:
        extra = "code, section" if self.has_codes else "NULL, NULL"
        return f"id, dedupe_key, first_seen, last_seen, matched, emailed_at, data, {extra}"

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

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._writable()
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    def by_code(self, code: int) -> Row | None:
        if not self.has_codes:
            return None
        rows = self._rows(f"SELECT {self._COLUMNS} FROM jobs WHERE code = ?", (code,))
        return rows[0] if rows else None

    def by_url(self, url: str) -> Row | None:
        """The stored posting whose link, or company-board link, is this URL (query string ignored)."""
        want = normalize_url(url)
        if not want:
            return None
        # Narrow with LIKE on the path, then compare normalized links exactly.
        # (`_` and `%` in the pattern only widen the net; the exact check decides.)
        base = want.split("://", 1)[-1]
        sql = (f"SELECT {self._COLUMNS} FROM jobs WHERE json_extract(data, '$.url') LIKE ? "
               "OR json_extract(data, '$.apply_url') LIKE ? ORDER BY last_seen DESC")
        pattern = f"%{base}%"
        for row in self._rows(sql, (pattern, pattern)):
            if want in (normalize_url(row.job.url), normalize_url(row.job.apply_url or "")):
                return row
        return None

    # --- writes ---------------------------------------------------------------

    def _writable(self) -> None:
        if self.readonly:
            raise StoreError("store was opened read-only")

    def save_run(self, jobs: list[tuple[Job, str, bool]], sent_ids: Iterable[str], now: datetime,
                 boards: Iterable[tuple[str, int, str | None]] = ()) -> None:
        """save_jobs and mark_sent in one transaction."""
        self._writable()
        with self.conn:
            self._save_jobs(jobs, now)
            self._mark_sent(sent_ids, now, boards)

    def save_jobs(self, jobs: list[tuple[Job, str, bool]], now: datetime) -> None:
        """Upsert this run's postings: (job, dedupe_key, matched).

        New rows get first_seen = now; known rows keep first_seen, emailed_at
        and their digest code, and get the latest content and last_seen.
        """
        self._writable()
        with self.conn:
            self._save_jobs(jobs, now)

    def mark_sent(self, sent_ids: Iterable[str], now: datetime,
                  boards: Iterable[tuple[str, int, str | None]] = ()) -> None:
        """Mark postings as emailed (keeping any earlier date) and log each board's count."""
        self._writable()
        with self.conn:
            self._mark_sent(sent_ids, now, boards)

    def next_code(self) -> int:
        top = self.conn.execute(
            "SELECT MAX(c) FROM (SELECT MAX(code) AS c FROM jobs UNION ALL SELECT MAX(code) FROM applications)"
        ).fetchone()[0]
        return max((top or 0) + 1, FIRST_CODE)

    def assign_codes(self, items: list[tuple[str, str | None]]) -> dict[str, int]:
        """Give each posting a digest code, in the order given; postings that have one keep it.

        items: (job id, digest section). Returns job id -> code for every item.
        The postings must already be saved.
        """
        self._writable()
        codes: dict[str, int] = {}
        with self.conn:
            for job_id, section in items:
                row = self.conn.execute("SELECT code FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    raise StoreError(f"posting {job_id} isn't saved yet")
                if row[0] is None:
                    code = self.next_code()
                    self.conn.execute("UPDATE jobs SET code = ?, section = ? WHERE id = ?", (code, section, job_id))
                    codes[job_id] = code
                else:
                    codes[job_id] = row[0]
        return codes

    def _save_jobs(self, jobs: list[tuple[Job, str, bool]], now: datetime) -> None:
        stamp = _ts(now)
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

    def _mark_sent(self, sent_ids: Iterable[str], now: datetime,
                   boards: Iterable[tuple[str, int, str | None]]) -> None:
        stamp = _ts(now)
        self.conn.executemany(
            "UPDATE jobs SET emailed_at = ? WHERE id = ? AND emailed_at IS NULL",
            [(stamp, id_) for id_ in sent_ids],
        )
        self.conn.executemany(
            "INSERT OR REPLACE INTO board_runs (label, run_at, postings, error) VALUES (?, ?, ?, ?)",
            [(label, stamp, postings, error) for label, postings, error in boards],
        )


def normalize_url(url: str | None) -> str:
    """Scheme, host and path only, lower-case host, no trailing slash: for matching links."""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    if not parts.netloc:
        return ""
    return urlunsplit(("https", parts.netloc.lower(), parts.path.rstrip("/"), "", ""))
