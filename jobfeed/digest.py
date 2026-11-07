"""Rendering a Digest: the subject line, a plain-text body and an HTML body, plus the run-health footer."""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import date

from jobfeed.filters import SECTION_ORDER, SECTION_TITLES, WATCHLIST, Digest, Entry
from jobfeed.models import Job
from jobfeed.normalize import format_salary
from jobfeed.sources import AGGREGATORS, SOURCE_NAMES

MAX_LOCATION_CHARS = 70
MAX_LISTED_PROBLEMS = 15
APPLY_HINT = "Applied to one? Log it on the Pi with: jobfeed apply CODE"
APPS_TITLE = "Your applications"

# The "Your applications" block: (heading, lines) per part, from followups.blocks.
AppBlock = list[tuple[str, list[str]]]


# --- pieces of an entry --------------------------------------------------------------

def pay(job: Job) -> str:
    if job.salary_min is not None or job.salary_max is not None:
        return format_salary(job.salary_min, job.salary_max)
    if job.salary_raw:
        return f"{job.salary_raw} ({job.salary_currency}, not compared)"
    return "no salary listed"


def mode(job: Job) -> str:
    if job.work_mode == "remote":
        return f"remote ({job.remote_region or 'region unstated'})"
    return job.work_mode


def location(job: Job) -> str:
    loc = job.location_raw or "no location given"
    return loc if len(loc) <= MAX_LOCATION_CHARS else loc[: MAX_LOCATION_CHARS - 1] + "…"


def headline_parts(job: Job) -> list[str]:
    """After the title: company, location, pay, level."""
    parts = [job.company, location(job), pay(job)]
    if job.seniority:
        parts.append(job.seniority.title())
    return parts


def detail_parts(entry: Entry) -> list[str]:
    job = entry.job
    parts = [mode(job)]
    if job.remote_states:
        parts.append("states: " + ", ".join(job.remote_states))
    parts += entry.verdict.tags
    parts.append(f"via {SOURCE_NAMES.get(job.source, job.source)}")
    if job.posted_at:
        parts.append(f"posted {job.posted_at.date().isoformat()}")
    return parts


def date_label(today: date) -> str:
    return f"{today.strftime('%a %b')} {today.day}"


def subject(digest: Digest, today: date) -> str:
    """Job feed · Fri Nov 6 · 14 new (2 watchlist)"""
    watch = len(digest.sections[WATCHLIST]) + digest.hidden.get(WATCHLIST, 0)
    return f"Job feed · {date_label(today)} · {digest.matched} new ({watch} watchlist)"


# --- run health -------------------------------------------------------------------

@dataclass
class Health:
    boards: int = 0
    postings_by_source: dict[str, int] = field(default_factory=dict)
    failed: list[tuple[str, str]] = field(default_factory=list)         # (board, error)
    went_empty: list[tuple[str, int]] = field(default_factory=list)     # (board, postings last run)
    empty: int = 0                                                      # boards with no postings, total
    duplicates: int = 0
    already_sent: int = 0
    dropped: dict[str, int] = field(default_factory=dict)               # stage -> count
    aggregators_used: list[str] = field(default_factory=list)
    send_note: str | None = None
    mail_note: str | None = None      # what reading Gmail found, or why it couldn't

    @property
    def postings(self) -> int:
        return sum(self.postings_by_source.values())

    def lines(self) -> list[str]:
        out = []
        sources = " · ".join(f"{SOURCE_NAMES.get(s, s)} {n:,}" for s, n in self.postings_by_source.items() if n)
        out.append(f"Fetched {self.postings:,} postings from {self.boards} boards"
                   + (f": {sources}" if sources else "") + ".")
        skipped = []
        if self.duplicates:
            skipped.append(f"{self.duplicates:,} duplicates")
        if self.already_sent:
            skipped.append(f"{self.already_sent:,} already sent")
        dropped = sum(self.dropped.values())
        if dropped:
            why = ", ".join(f"{stage} {n:,}" for stage, n in self.dropped.items() if n)
            skipped.append(f"{dropped:,} dropped by the rules ({why})")
        if skipped:
            out.append("Skipped: " + "; ".join(skipped) + ".")
        if self.failed:
            shown = "; ".join(f"{b} ({e})" for b, e in self.failed[:MAX_LISTED_PROBLEMS])
            more = len(self.failed) - MAX_LISTED_PROBLEMS
            out.append(f"FAILED {len(self.failed)}: {shown}" + (f"; +{more} more" if more > 0 else "") + ".")
        if self.went_empty:
            shown = "; ".join(f"{b} (had {n})" for b, n in self.went_empty[:MAX_LISTED_PROBLEMS])
            out.append(f"Now empty: {shown}. A board that drops to zero often means the company changed ATS; "
                       "re-check it with `jobfeed discover NAME`.")
        quiet = self.empty - len(self.went_empty)
        if quiet > 0:
            out.append(f"{quiet} boards had no open postings.")
        if self.aggregators_used:
            names = [SOURCE_NAMES.get(a, a) for a in self.aggregators_used]
            listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
            out.append(f"Remote listings via {listed}.")
        if self.mail_note:
            out.append(self.mail_note)
        if self.send_note:
            out.append(self.send_note)
        return out


def build_health(results, plan, previous: dict[str, int]) -> Health:
    """results: this run's FetchResults; previous: board label -> postings on its last good run."""
    health = Health(boards=len(results), duplicates=len(plan.duplicates), already_sent=len(plan.already_sent))
    for r in results:
        if not r.ok:
            health.failed.append((r.board.label, r.error or "error"))
            continue
        health.postings_by_source[r.board.source] = health.postings_by_source.get(r.board.source, 0) + len(r.jobs)
        if not r.jobs:
            health.empty += 1
            if previous.get(r.board.label, 0) > 0:
                health.went_empty.append((r.board.label, previous[r.board.label]))
        if r.board.source in AGGREGATORS and r.board.source not in health.aggregators_used:
            health.aggregators_used.append(r.board.source)
    for e in plan.digest.dropped:
        stage = e.verdict.stage or "other"
        health.dropped[stage] = health.dropped.get(stage, 0) + 1
    health.dropped = {s: health.dropped[s] for s in ("role", "location", "salary", "other") if s in health.dropped}
    return health


# --- plain text -------------------------------------------------------------------

def render_text(digest: Digest, today: date, *, health: Health | None = None, show_dropped: bool = False,
                noun: str = "new", overflow_hint: str = " (raise digest.max_per_section or use --limit 0)",
                codes: dict[str, int] | None = None, apps: AppBlock | None = None) -> str:
    """codes: job id -> digest code, shown before each title (none in a dry run).
    apps: the "Your applications" block, left out when empty."""
    codes = codes or {}
    watch = len(digest.sections[WATCHLIST]) + digest.hidden.get(WATCHLIST, 0)
    label = "Job feed" if noun == "new" else "Job feed replay"
    lines = [f"{label} · {date_label(today)} · {digest.matched} {noun} ({watch} watchlist)"]
    if apps:
        lines += ["", f"== {APPS_TITLE}"]
        for heading, items in apps:
            lines.append(f"  {heading}")
            lines += [f"    {item}" for item in items]
    if not digest.matched:
        lines += ["", "Nothing new matched." if noun == "new" else "Nothing matched."]
    elif codes and noun == "new":
        lines += ["", APPLY_HINT]
    for name in SECTION_ORDER:
        entries = digest.sections[name]
        hidden = digest.hidden.get(name, 0)
        if not entries:
            continue
        lines += ["", f"== {SECTION_TITLES[name]} ({len(entries) + hidden})"]
        for entry in entries:
            code = codes.get(entry.job.id)
            lines += [f"  {'#' + str(code) + ' ' if code else ''}{entry.job.title} — {' · '.join(headline_parts(entry.job))}",
                      f"      {' · '.join(detail_parts(entry))}",
                      f"      {entry.job.url}"]
        if hidden:
            lines.append(f"  … {hidden} more not shown{overflow_hint}")
    if show_dropped and digest.dropped:
        lines += ["", f"== Dropped ({len(digest.dropped)})"]
        for entry in sorted(digest.dropped, key=lambda e: (e.verdict.stage or "", e.job.company, e.job.title)):
            job = entry.job
            lines += [f"  {entry.verdict.stage:<8}  {job.title} — {job.company} · {location(job)}",
                      f"            {entry.verdict.reason}"]
    if health:
        lines += ["", "-- ", *health.lines()]
    return "\n".join(lines)


# --- HTML -------------------------------------------------------------------------
#
# Email clients ignore <style> blocks and most layout CSS, so everything is
# inline and the layout is one plain column.

_FONT = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;"
_INK = "#1f2328"
_QUIET = "#59636e"
_RULE = "#d1d9e0"
_ACCENT = "#0969da"


def _e(text: str) -> str:
    return html.escape(text, quote=True)


def _entry_html(entry: Entry, code: int | None = None) -> str:
    job = entry.job
    head = " · ".join(_e(p) for p in headline_parts(job))
    detail = " · ".join(_e(p) for p in detail_parts(entry))
    link = (f'<a href="{_e(job.url)}" style="color:{_ACCENT};text-decoration:none;font-weight:600">'
            f'{_e(job.title)}</a>') if job.url else f'<strong>{_e(job.title)}</strong>'
    tag = (f'<span style="color:{_QUIET};font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px">'
           f'#{code}</span> ') if code else ""
    return (f'<tr><td style="padding:10px 0;border-bottom:1px solid {_RULE};{_FONT}">'
            f'<div style="font-size:15px;line-height:1.4;color:{_INK}">{tag}{link} — {head}</div>'
            f'<div style="font-size:13px;line-height:1.4;color:{_QUIET};margin-top:2px">{detail}</div>'
            "</td></tr>")


def _apps_html(apps: AppBlock) -> list[str]:
    out = [f'<div style="{_FONT}background:#f6f8fa;border:1px solid {_RULE};border-radius:6px;'
           f'padding:10px 14px;margin:0 0 16px">',
           f'<div style="font-size:15px;font-weight:600;color:{_INK};margin:0 0 4px">{_e(APPS_TITLE)}</div>']
    for heading, items in apps:
        out.append(f'<div style="font-size:13px;font-weight:600;color:{_QUIET};margin:8px 0 2px">{_e(heading)}</div>')
        out += [f'<div style="font-size:14px;line-height:1.45;color:{_INK};margin:1px 0">{_e(item)}</div>'
                for item in items]
    out.append("</div>")
    return out


def render_html(digest: Digest, today: date, *, health: Health | None = None,
                codes: dict[str, int] | None = None, apps: AppBlock | None = None) -> str:
    codes = codes or {}
    watch = len(digest.sections[WATCHLIST]) + digest.hidden.get(WATCHLIST, 0)
    body: list[str] = [
        f'<div style="{_FONT}font-size:20px;font-weight:600;color:{_INK};margin:0 0 4px">Job feed · '
        f'{_e(date_label(today))}</div>',
        f'<div style="{_FONT}font-size:14px;color:{_QUIET};margin:0 0 16px">{digest.matched} new'
        f' · {watch} on your watchlist</div>',
    ]
    if apps:
        body += _apps_html(apps)
    if not digest.matched:
        body.append(f'<p style="{_FONT}font-size:15px;color:{_INK}">Nothing new matched today.</p>')
    elif codes:
        body.append(f'<p style="{_FONT}font-size:13px;color:{_QUIET};margin:0 0 8px">{_e(APPLY_HINT)}</p>')
    for name in SECTION_ORDER:
        entries = digest.sections[name]
        hidden = digest.hidden.get(name, 0)
        if not entries:
            continue
        body.append(f'<h2 style="{_FONT}font-size:16px;color:{_INK};margin:24px 0 4px;padding-bottom:4px;'
                    f'border-bottom:2px solid {_INK}">{_e(SECTION_TITLES[name])} ({len(entries) + hidden})</h2>')
        body.append('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                    'style="border-collapse:collapse">')
        body += [_entry_html(e, codes.get(e.job.id)) for e in entries]
        body.append("</table>")
        if hidden:
            body.append(f'<p style="{_FONT}font-size:13px;color:{_QUIET}">… {hidden} more not shown; '
                        "they lead tomorrow's digest.</p>")
    if health:
        items = "".join(f'<div style="margin:2px 0">{_e(line)}</div>' for line in health.lines())
        body.append(f'<div style="{_FONT}font-size:12px;line-height:1.5;color:{_QUIET};margin-top:28px;'
                    f'padding-top:8px;border-top:1px solid {_RULE}">{items}</div>')
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{_e(subject(digest, today))}</title></head>"
            f'<body style="margin:0;padding:0;background:#ffffff">'
            f'<div style="max-width:680px;margin:0 auto;padding:20px 16px">{"".join(body)}</div>'
            "</body></html>")
