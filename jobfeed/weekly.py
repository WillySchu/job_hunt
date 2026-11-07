"""The weekly report email, and the weekly copy of jobs.db.

Both come with the run on `tracking.weekly_day` (Monday). If that run is
missed, because the Pi was off, say, the next run catches up. The report covers
the seven days before that day: "Nov 2–8" in the Monday Nov 9 report.

The report is `jobfeed stats` with a line about last week on top, sent as its
own email after the digest. It never fails the run: the digest has already
gone, and a failed run is retried, which would send the digest twice. A report
that can't be sent waits for the next run.

Backups are taken with SQLite's backup API, so a copy is consistent even if
something is writing. They go next to jobs.db, in backups/jobs-YYYY-MM-DD.db,
and the newest `tracking.backups_kept` are kept.

The report also carries a gzipped copy of jobs.db as an attachment
(`tracking.attach_backup`), so a copy lives in Gmail, off the Pi. Gmail takes
at most 25 MB a message, and an attachment grows by a third when encoded, so a
copy over ATTACH_LIMIT isn't attached and the report says so at the top; from
WARN_SHARE of the limit it warns that the day is coming.
"""

from __future__ import annotations

import gzip
import html
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from jobfeed import stats
from jobfeed.config import TrackingRules
from jobfeed.store import Store

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
REPORT_KEY = "weekly_report"
BACKUP_KEY = "weekly_backup"
BACKUP_DIR = "backups"
ATTACH_LIMIT = 18 * 1024 * 1024     # compressed bytes; about 24 MB once encoded for email
WARN_SHARE = 0.75


def report_day(today: date, rules: TrackingRules) -> date:
    """The latest weekly day on or before today."""
    wd = WEEKDAYS.index(rules.weekly_day)
    return today - timedelta(days=(today.weekday() - wd) % 7)


def _last(store: Store, key: str) -> date | None:
    value = store.get_meta(key)
    return date.fromisoformat(value) if value else None


def report_due(store: Store, now: datetime, rules: TrackingRules) -> date | None:
    """The weekly day whose report is due, if one is.

    The first call starts the clock: the first report is the next weekly day's (today's, if today is one),
    not a catch-up for a week before jobfeed was tracking anything. From then on a missed or failed
    report is sent by the next run."""
    if not rules.weekly_report:
        return None
    today = now.astimezone().date()
    day = report_day(today, rules)
    last = _last(store, REPORT_KEY)
    if last is None:
        last = day - timedelta(days=7) if today == day else day
        store.set_meta(REPORT_KEY, last.isoformat())
    return day if last < day else None


def backup_due(store: Store, now: datetime, rules: TrackingRules) -> bool:
    if rules.backups_kept < 1:
        return False
    last = _last(store, BACKUP_KEY)
    return last is None or last < report_day(now.astimezone().date(), rules)


def backup(store: Store, db_path: Path, now: datetime, keep: int) -> Path:
    """Copy jobs.db into backups/ beside it, keeping the newest `keep` (0: delete none).
    Raises OSError or sqlite3.Error."""
    folder = db_path.parent / BACKUP_DIR
    folder.mkdir(exist_ok=True)
    dest = folder / f"jobs-{now.astimezone().date().isoformat()}.db"
    partial = dest.with_name(dest.name + ".partial")
    copy = sqlite3.connect(partial)
    try:
        store.conn.backup(copy)
    finally:
        copy.close()
    partial.replace(dest)
    if keep > 0:
        for old in sorted(folder.glob("jobs-*.db"))[:-keep]:
            old.unlink()
    store.set_meta(BACKUP_KEY, now.astimezone().date().isoformat())
    return dest


# --- the attached copy ---------------------------------------------------------------------------

@dataclass
class Packed:
    name: str              # jobs-2026-11-09.db.gz
    data: bytes | None     # None when too big to attach
    size: int              # compressed bytes
    line: str              # what the report says about it
    alert: bool            # the line goes at the top of the report


def _mb(n: int) -> str:
    if n < 1024 * 1024:
        return f"{max(1, round(n / 1024))} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def pack_backup(store: Store, now: datetime, limit: int | None = None) -> Packed:
    """A consistent, gzipped copy of jobs.db, and what to say about it. Raises OSError or sqlite3.Error."""
    limit = ATTACH_LIMIT if limit is None else limit
    with tempfile.TemporaryDirectory() as folder:
        path = f"{folder}/jobs.db"
        copy = sqlite3.connect(path)
        try:
            store.conn.backup(copy)
        finally:
            copy.close()
        with open(path, "rb") as fh:
            data = gzip.compress(fh.read(), compresslevel=9)
    name = f"jobs-{now.astimezone().date().isoformat()}.db.gz"
    size = len(data)
    if size > limit:
        return Packed(name, None, size, (
            f"BACKUP NOT ATTACHED: the compressed copy of jobs.db is {_mb(size)}, over the {_mb(limit)} that fits in "
            "an email. Your weekly copies are still in backups/ on the Pi, but none is leaving it now: copy them off "
            "another way (README → Backups)."), True)
    if size > WARN_SHARE * limit:
        return Packed(name, data, size, (
            f"Backup attached ({name}, {_mb(size)}), but it's nearing the {_mb(limit)} that fits in an email; "
            "once past it, it won't be attached. README → Backups has other ways to keep a copy."), True)
    return Packed(name, data, size, f"Backup attached: {name} ({_mb(size)}; gunzip it to get jobs.db).", False)


# --- the report --------------------------------------------------------------------------------

@dataclass
class Report:
    day: date
    subject: str
    text: str
    html: str


def _span(day: date) -> tuple[datetime, datetime]:
    end = datetime.combine(day, time.min).astimezone()
    return end - timedelta(days=7), end


def _label(start: datetime, end: datetime) -> str:
    last = (end - timedelta(days=1)).date()
    first = start.date()
    if first.month == last.month:
        return f"{first:%b} {first.day}–{last.day}"
    return f"{first:%b} {first.day}–{last:%b} {last.day}"


NEWS_WORDS = {"reply": ("reply", "replies"), "screen": ("screen", "screens"),
              "interview": ("interview", "interviews"), "offer": ("offer", "offers"),
              "rejected": ("rejection", "rejections")}


def last_week(facts: list[stats.Facts], start: datetime, end: datetime) -> list[str]:
    applied = sorted((f for f in facts if start <= f.app.applied_at < end), key=lambda f: f.app.applied_at)
    heard: dict[str, int] = {}
    for f in facts:
        for e in f.app.events:
            if start <= e.at < end and e.kind in NEWS_WORDS and e.origin != "rule":
                heard[e.kind] = heard.get(e.kind, 0) + 1
    news = ", ".join(f"{v} {NEWS_WORDS[k][v != 1]}" for k, v in sorted(heard.items(), key=lambda kv: list(NEWS_WORDS).index(kv[0])))
    n = len(applied)
    lines = [f"Last week: {n} application{'' if n == 1 else 's'} ({len(facts)} so far) · "
             f"news from companies: {news or 'none'}"]
    lines += [f"  #{f.app.code} {f.app.company} — {f.app.title}" for f in applied]
    return lines


def render(facts: list[stats.Facts], rules: TrackingRules, now: datetime, day: date,
           backup_line: str | None = None, alert: bool = False) -> Report:
    """backup_line: what to say about the attached copy; alert puts it at the top, in a box."""
    start, end = _span(day)
    label = _label(start, end)
    n = sum(1 for f in facts if start <= f.app.applied_at < end)
    title = f"Job search weekly · {label}"
    body = last_week(facts, start, end) + [""] + stats.report(facts, rules, now=now)
    top = [f"!! {backup_line}", ""] if backup_line and alert else []
    footer = ["`jobfeed stats` on the Pi shows this any time."] + ([backup_line] if backup_line and not alert else [])
    text = "\n".join([title, ""] + top + body + ["", "-- "] + footer)
    font = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;"
    page = ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{html.escape(title)}</title></head><body style=\"margin:0;padding:0;background:#ffffff\">"
            '<div style="max-width:760px;margin:0 auto;padding:20px 16px">'
            f'<div style="{font}font-size:20px;font-weight:600;color:#1f2328;margin:0 0 12px">{html.escape(title)}</div>'
            + (f'<div style="{font}font-size:14px;line-height:1.45;color:#1f2328;background:#fff8c5;'
               f'border:1px solid #d4a72c;border-radius:6px;padding:10px 14px;margin:0 0 16px">'
               f'{html.escape(backup_line)}</div>' if backup_line and alert else "")
            + '<pre style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;line-height:1.5;'
            f'color:#1f2328;white-space:pre-wrap;margin:0">{html.escape(chr(10).join(body))}</pre>'
            f'<div style="{font}font-size:12px;color:#59636e;margin-top:20px;padding-top:8px;'
            'border-top:1px solid #d1d9e0">'
            + "<br>".join(html.escape(line) for line in footer) + "</div></div></body></html>")
    subject = f"{title} · {n} applied ({len(facts)} so far)"
    return Report(day, subject, text, page)


def mark_sent(store: Store, day: date) -> None:
    store.set_meta(REPORT_KEY, day.isoformat())
