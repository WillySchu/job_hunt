"""Applications and their history: logging, stage changes, notes, listing and undo.

The events table is the record. An application's stage, stage date and notes
are recomputed from its events after every change, so an undo or a
correction can never leave them out of step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone

from jobfeed.store import Row, Store, _ts

OPEN_STAGES = ("applied", "screen", "interview", "offer")
CLOSED_STAGES = ("accepted", "declined", "rejected", "withdrawn", "no_response")
STAGE_EVENTS = ("screen", "interview", "offer")

# What people type -> event kind.
ALIASES = {
    "reply": "reply", "replied": "reply", "response": "reply",
    "screen": "screen", "phone": "screen", "phone-screen": "screen", "recruiter-call": "screen",
    "interview": "interview", "onsite": "interview", "on-site": "interview", "technical": "interview",
    "loop": "interview",
    "offer": "offer",
    "accepted": "accepted", "accept": "accepted",
    "declined": "declined", "decline": "declined",
    "rejected": "rejected", "reject": "rejected", "rejection": "rejected",
    "withdrawn": "withdrawn", "withdraw": "withdrawn", "withdrew": "withdrawn",
    "no-response": "no_response", "no_response": "no_response", "ghosted": "no_response",
    "reopen": "reopened", "reopened": "reopened",
}
MARKABLE = ("reply", "screen", "interview", "offer", "accepted", "declined", "rejected", "withdrawn",
            "no_response", "reopened")
VIA_CHOICES = ("referral", "linkedin", "recruiter", "other")


class TrackerError(Exception):
    """A tracker command that can't be carried out; nothing was changed."""


@dataclass
class Event:
    id: int
    code: int
    at: datetime
    kind: str
    origin: str
    message_id: str | None = None
    detail: str | None = None
    reported: datetime | None = None   # when a digest first carried it


@dataclass
class Application:
    code: int
    job_id: str | None
    company: str
    title: str
    url: str | None
    found_via: str
    digest_section: str | None
    referral: bool
    resume_version: str | None
    cover_letter: bool
    autofilled: bool | None
    fit_score: float | None
    first_seen: datetime | None
    applied_at: datetime
    logged_via: str
    stage: str
    stage_at: datetime
    notes: str | None
    fit_keywords: float | None = None    # fit_score is the LLM's score; both copied from the posting's
    events: list[Event] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.stage in OPEN_STAGES

    @property
    def days_to_apply(self) -> int | None:
        """Days from jobfeed first seeing the posting to applying; 0 if you applied before it did."""
        if self.first_seen is None:
            return None
        return max(0, (self.applied_at.astimezone().date() - self.first_seen.astimezone().date()).days)


# --- dates -------------------------------------------------------------------------

def parse_day(text: str, now: datetime) -> datetime:
    """'2026-11-05' -> noon that day, local time; today -> now. Never in the future."""
    try:
        day = date.fromisoformat(text)
    except ValueError as exc:
        raise TrackerError(f"'{text}' isn't a date like 2026-11-05") from exc
    local_today = now.astimezone().date()
    if day > local_today:
        raise TrackerError(f"{day} is in the future")
    if day == local_today:
        return now
    return datetime.combine(day, time(12, 0)).astimezone().astimezone(timezone.utc)


def stage_label(stage: str) -> str:
    return stage.replace("_", " ")


def local_day(moment: datetime) -> str:
    d = moment.astimezone()
    return f"{d:%b} {d.day}"


# --- reading -----------------------------------------------------------------------

_APP_COLUMNS = ("code, job_id, company, title, url, found_via, digest_section, referral, resume_version, "
                "cover_letter, autofilled, fit_score, first_seen, applied_at, logged_via, stage, stage_at, notes, "
                "fit_keywords")


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _application(row: tuple) -> Application:
    (code, job_id, company, title, url, via, section, referral, resume, cover, autofilled, fit, first_seen,
     applied_at, logged_via, stage, stage_at, notes, fit_keywords) = row
    return Application(
        code=code, job_id=job_id, company=company, title=title, url=url, found_via=via, digest_section=section,
        referral=bool(referral), resume_version=resume, cover_letter=bool(cover),
        autofilled=None if autofilled is None else bool(autofilled), fit_score=fit,
        first_seen=_dt(first_seen), applied_at=_dt(applied_at), logged_via=logged_via, stage=stage,
        stage_at=_dt(stage_at), notes=notes, fit_keywords=fit_keywords,
    )


def events_for(store: Store, code: int) -> list[Event]:
    rows = store.conn.execute(
        "SELECT id, code, at, kind, origin, message_id, detail, reported FROM events WHERE code = ? ORDER BY at, id",
        (code,))
    return [Event(id=i, code=c, at=_dt(at), kind=k, origin=o, message_id=m, detail=d, reported=_dt(r))
            for i, c, at, k, o, m, d, r in rows]


def get_application(store: Store, code: int, *, with_events: bool = False) -> Application | None:
    row = store.conn.execute(f"SELECT {_APP_COLUMNS} FROM applications WHERE code = ?", (code,)).fetchone()
    if row is None:
        return None
    app = _application(row)
    if with_events:
        app.events = events_for(store, code)
    return app


def list_applications(store: Store, *, include_closed: bool = False) -> list[Application]:
    where = "" if include_closed else f"WHERE stage IN ({','.join('?' * len(OPEN_STAGES))})"
    params = () if include_closed else OPEN_STAGES
    rows = store.conn.execute(f"SELECT {_APP_COLUMNS} FROM applications {where} ORDER BY applied_at DESC, code DESC",
                              params)
    return [_application(r) for r in rows]


# --- the stage, from events -----------------------------------------------------------

def derive(events: list[Event]) -> tuple[str, datetime, str | None]:
    """(stage, when it was reached, notes) from an application's events."""
    stage: str | None = None
    stage_at: datetime | None = None
    last_open: tuple[str, datetime] | None = None
    notes: list[str] = []
    for e in sorted(events, key=lambda e: (e.at, e.id)):
        if e.kind == "applied":
            if stage is None or stage == "applied":
                stage, stage_at = "applied", e.at
        elif e.kind in STAGE_EVENTS:
            stage, stage_at = e.kind, e.at
        elif e.kind in CLOSED_STAGES:
            if stage in OPEN_STAGES:
                last_open = (stage, stage_at)
            stage, stage_at = e.kind, e.at
        elif e.kind == "reopened" or (e.kind == "reply" and stage == "no_response"):
            # A reply proves "no response" wrong, so it reopens the application too.
            stage = last_open[0] if last_open else "applied"
            stage_at = e.at
        elif e.kind == "note" and e.detail:
            notes.append(f"{e.at.astimezone():%Y-%m-%d}: {e.detail}")
    if stage is None or stage_at is None or not any(e.kind == "applied" for e in events):
        raise TrackerError("an application needs an 'applied' event")
    return stage, stage_at, "\n".join(notes) or None


def _refresh(store: Store, code: int) -> None:
    stage, stage_at, notes = derive(events_for(store, code))
    store.conn.execute("UPDATE applications SET stage = ?, stage_at = ?, notes = ? WHERE code = ?",
                       (stage, _ts(stage_at), notes, code))


def _add_event(store: Store, code: int, kind: str, at: datetime, origin: str, detail: str | None = None,
               message_id: str | None = None, reported: datetime | None = None) -> int:
    cur = store.conn.execute(
        "INSERT INTO events (code, at, kind, origin, message_id, detail, reported) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (code, _ts(at), kind, origin, message_id, detail, _ts(reported) if reported else None))
    return cur.lastrowid


# --- writing -----------------------------------------------------------------------

@dataclass
class LogOptions:
    applied_at: datetime
    resume_version: str | None
    cover_letter: bool
    referral: bool = False
    note: str | None = None
    logged_via: str = "code"
    origin: str = "cli"
    message_id: str | None = None   # the email it was logged from


def _insert(store: Store, code: int, *, job_id: str | None, company: str, title: str, url: str | None,
            found_via: str, section: str | None, first_seen: datetime | None, opts: LogOptions) -> Application:
    scores = posting_scores(store, job_id)
    store.conn.execute(
        f"INSERT INTO applications ({_APP_COLUMNS}) VALUES ({','.join('?' * 19)})",
        (code, job_id, company, title, url, found_via, section, int(opts.referral), opts.resume_version,
         int(opts.cover_letter), None, scores.get("llm"), _ts(first_seen) if first_seen else None,
         _ts(opts.applied_at), opts.logged_via, "applied", _ts(opts.applied_at), None, scores.get("keywords")),
    )
    _add_event(store, code, "applied", opts.applied_at, opts.origin, message_id=opts.message_id)
    if opts.note:
        _add_event(store, code, "note", opts.applied_at, opts.origin, detail=opts.note)
    _refresh(store, code)
    app = get_application(store, code)
    assert app is not None
    return app


def posting_scores(store: Store, job_id: str | None) -> dict[str, int]:
    """The posting's fit scores, by scorer, as the application copies them."""
    if job_id is None:
        return {}
    return dict(store.conn.execute("SELECT scorer, score FROM fit_scores WHERE job_id = ?", (job_id,)))


def sync_fit_scores(store: Store) -> int:
    """Fill in scores for applications logged before their posting was scored. Returns how many changed."""
    changed = 0
    for scorer, column in (("keywords", "fit_keywords"), ("llm", "fit_score")):
        changed += store.conn.execute(
            f"UPDATE applications SET {column} = (SELECT score FROM fit_scores f WHERE f.job_id = applications.job_id "
            f"AND f.scorer = ?) WHERE {column} IS NULL AND job_id IS NOT NULL AND EXISTS (SELECT 1 FROM fit_scores f "
            f"WHERE f.job_id = applications.job_id AND f.scorer = ?)", (scorer, scorer)).rowcount
    return changed


def _already(store: Store, code: int) -> None:
    existing = get_application(store, code)
    if existing:
        raise TrackerError(f"#{code} is already logged ({existing.company}, applied "
                           f"{local_day(existing.applied_at)}); use `jobfeed mark` or `jobfeed note` to update it")


def log_posting(store: Store, row: Row, opts: LogOptions) -> Application:
    """An application for a stored posting, keyed by its digest code (given one now if it has none)."""
    with store.conn:
        code = row.code if row.code is not None else store.assign_codes([(row.job.id, None)])[row.job.id]
        _already(store, code)
        job = row.job
        return _insert(store, code, job_id=job.id, company=job.company, title=job.title, url=job.url,
                       found_via=job.source, section=row.section, first_seen=row.first_seen, opts=opts)


def log_manual(store: Store, *, url: str | None, company: str, title: str, via: str, opts: LogOptions) -> Application:
    """An application for a role jobfeed never stored."""
    if not company.strip() or not title.strip():
        raise TrackerError("a role found outside jobfeed needs --company and --title")
    with store.conn:
        code = store.next_code()
        return _insert(store, code, job_id=None, company=company.strip(), title=title.strip(), url=url,
                       found_via=via, section=None, first_seen=None, opts=opts)


def mark(store: Store, code: int, kind: str, at: datetime, *, origin: str = "cli", note: str | None = None,
         message_id: str | None = None) -> Application:
    """Record a stage change, a first reply, or a reopening."""
    if kind not in MARKABLE:
        raise TrackerError(f"'{kind}' isn't a stage; use one of: {', '.join(stage_label(k) for k in MARKABLE)}")
    app = get_application(store, code)
    if app is None:
        raise TrackerError(f"no application #{code}; `jobfeed apps --all` lists them")
    if at < app.applied_at:
        raise TrackerError(f"#{code} was applied to on {local_day(app.applied_at)}, after that date")
    if kind == app.stage:
        raise TrackerError(f"#{code} is already at {stage_label(kind)}")
    if kind == "no_response" and app.stage != "applied":
        raise TrackerError(f"#{code} is at {stage_label(app.stage)}, so it did get a response; "
                           "mark it rejected or withdrawn instead")
    if kind == "reopened" and app.is_open:
        raise TrackerError(f"#{code} is still open ({stage_label(app.stage)}); nothing to reopen")
    detail = None
    if kind in ("rejected", "withdrawn") and app.is_open:
        detail = app.stage  # the stage it had reached
    with store.conn:
        _add_event(store, code, kind, at, origin, detail=detail, message_id=message_id)
        if note:
            _add_event(store, code, "note", at, origin, detail=note)
        _refresh(store, code)
    updated = get_application(store, code)
    assert updated is not None
    return updated


def add_note(store: Store, code: int, text: str, at: datetime, *, origin: str = "cli") -> Application:
    if not text.strip():
        raise TrackerError("the note is empty")
    if get_application(store, code) is None:
        raise TrackerError(f"no application #{code}; `jobfeed apps --all` lists them")
    with store.conn:
        _add_event(store, code, "note", at, origin, detail=text.strip())
        _refresh(store, code)
    app = get_application(store, code)
    assert app is not None
    return app


def edit(store: Store, code: int, **changes) -> Application:
    """Change how an application was sent: resume_version, cover_letter, referral, found_via."""
    allowed = {"resume_version", "cover_letter", "referral", "found_via"}
    assert set(changes) <= allowed, changes
    app = get_application(store, code)
    if app is None:
        raise TrackerError(f"no application #{code}; `jobfeed apps --all` lists them")
    if "found_via" in changes and app.job_id is not None:
        raise TrackerError(f"#{code} came from a posting jobfeed found, so where you found it is already known")
    if "resume_version" in changes:
        changes["resume_version"] = changes["resume_version"].strip()
        if not changes["resume_version"]:
            raise TrackerError("the resume version is empty")
    values = {k: (int(v) if isinstance(v, bool) else v) for k, v in changes.items()}
    with store.conn:
        store.conn.execute(f"UPDATE applications SET {', '.join(f'{k} = ?' for k in values)} WHERE code = ?",
                           (*values.values(), code))
    updated = get_application(store, code)
    assert updated is not None
    return updated


def undo(store: Store, code: int) -> str:
    """Remove the last entry made (not the latest-dated one); removing the only entry, the
    application itself, removes the application. Returns what it did."""
    app = get_application(store, code, with_events=True)
    if app is None:
        raise TrackerError(f"no application #{code}; `jobfeed apps --all` lists them")
    yours = [e for e in app.events if e.kind != "follow_up"]   # reminders are the digest's, not yours
    last = max(yours, key=lambda e: e.id)
    if last.kind == "no_response" and last.origin == "rule":
        raise TrackerError(f"#{code} was closed by the no-reply rule, which would just close it again; "
                           f"`jobfeed mark {code} reopen` keeps it open and restarts the clock, and "
                           f"`jobfeed mark {code} reply` records an answer")
    with store.conn:
        if last.kind == "applied" and len(yours) == 1:
            store.conn.execute("DELETE FROM events WHERE code = ?", (code,))
            store.conn.execute("DELETE FROM applications WHERE code = ?", (code,))
            return f"removed #{code} {app.company}, {app.title} from the tracker"
        if last.kind == "applied":
            raise TrackerError(f"#{code}'s last entry is the application itself, but it has other entries")
        store.conn.execute("DELETE FROM events WHERE id = ?", (last.id,))
        _refresh(store, code)
    now_at = get_application(store, code)
    assert now_at is not None
    what = f"note '{last.detail}'" if last.kind == "note" else stage_label(last.kind)
    return f"removed {what} ({local_day(last.at)}) from #{code}; it's now at {stage_label(now_at.stage)}"


# --- formatting --------------------------------------------------------------------

def list_line(app: Application, now: datetime) -> str:
    age = (now.astimezone().date() - app.applied_at.astimezone().date()).days
    return (f"  #{app.code:<5} {stage_label(app.stage):<12} {local_day(app.applied_at):<7} {age:>3}d  "
            f"{app.company} — {app.title}")


def detail_lines(app: Application, source_name: str, section_name: str | None) -> list[str]:
    yes_no = lambda flag, word: word if flag else f"no {word}"  # noqa: E731
    lines = [f"#{app.code} {app.company} — {app.title}"]
    if app.url:
        lines.append(f"  {app.url}")
    found = [f"found via {source_name}"]
    if section_name:
        found.append(section_name)
    found += [f"resume {app.resume_version or 'unrecorded'}", yes_no(app.cover_letter, "cover letter"),
              yes_no(app.referral, "referral")]
    lines.append("  " + " · ".join(found))
    when = []
    if app.first_seen:
        when.append(f"first seen {local_day(app.first_seen)}")
    applied = f"applied {local_day(app.applied_at)}"
    if app.days_to_apply is not None:
        applied += {0: " (same day)", 1: " (1 day later)"}.get(app.days_to_apply, f" ({app.days_to_apply} days later)")
    when.append(applied)
    when.append(f"logged via {app.logged_via}")
    lines.append("  " + " · ".join(when))
    lines.append(f"  Stage: {stage_label(app.stage)} since {local_day(app.stage_at)}")
    lines.append("History")
    for e in app.events:
        text = stage_label(e.kind)
        if e.kind in ("rejected", "withdrawn") and e.detail:
            text += f" (at {stage_label(e.detail)})"
        elif e.kind == "note" and e.detail:
            text = f"note: {e.detail}"
        elif e.kind == "follow_up":
            text = "follow-up reminder"
        elif e.kind == "confirmation":
            text = "confirmation email"
        elif e.kind == "no_response" and e.detail:
            text += f" ({e.detail})"
        lines.append(f"  {e.at.astimezone():%Y-%m-%d}  {text:<34} via {e.origin}")
    return lines
