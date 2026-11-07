"""The digest's "Your applications" block, and the two rules behind it.

- No response: an application with no reply `no_response_days` after applying
  (or after it was last reopened) is closed as "no response", dated the day the
  limit passed. Only Applied closes this way; later stages can pause for weeks.
- Follow up: an open application quiet for `follow_up_days` is listed once. The
  listing is kept as a `follow_up` event, so the same quiet stretch isn't listed
  again; any new entry starts a new stretch.

Every event has a `reported` time, set when a digest carrying it is sent.
Until then it is "since the last digest", so an email that fails to send
leaves everything to be reported by the next one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from jobfeed.config import TrackingRules
from jobfeed.store import Store, _ts
from jobfeed.tracker import (OPEN_STAGES, Application, Event, _add_event, _refresh, events_for, get_application,
                             list_applications, local_day, stage_label)

RULE = "rule"


@dataclass
class Change:
    app: Application      # as it stands now
    event: Event


@dataclass
class FollowUp:
    app: Application
    quiet_since: datetime
    days: int


@dataclass
class Activity:
    logged: list[Change] = field(default_factory=list)       # applications logged since the last digest
    changes: list[Change] = field(default_factory=list)      # everything else that happened to them
    follow_ups: list[FollowUp] = field(default_factory=list)
    event_ids: list[int] = field(default_factory=list)       # to mark reported once the digest is sent

    @property
    def empty(self) -> bool:
        return not (self.logged or self.changes or self.follow_ups)


# --- the rules -----------------------------------------------------------------------

def _no_response_due(events: list[Event], days: int, now: datetime) -> datetime | None:
    """When an Applied application with no reply passes the limit, if it has."""
    if any(e.kind == "reply" for e in events):
        return None
    clock = max((e.at for e in events if e.kind in ("applied", "reopened")), default=None)
    if clock is None:
        return None
    due = clock + timedelta(days=days)
    return due if due <= now else None


def _quiet_since(events: list[Event]) -> datetime:
    return max(e.at for e in events if e.kind != "follow_up")


def _reminded(events: list[Event], since: datetime) -> bool:
    return any(e.kind == "follow_up" and e.at >= since for e in events)


def close_unanswered(store: Store, rules: TrackingRules, now: datetime) -> list[int]:
    """Apply the no-response rule; returns the codes it closed."""
    closed = []
    with store.conn:
        for app in list_applications(store):
            if app.stage != "applied":
                continue
            due = _no_response_due(events_for(store, app.code), rules.no_response_days, now)
            if due is not None:
                _add_event(store, app.code, "no_response", due, RULE,
                           detail=f"no reply in {rules.no_response_days} days")
                _refresh(store, app.code)
                closed.append(app.code)
    return closed


# --- the report ----------------------------------------------------------------------

def _unreported(store: Store) -> list[Event]:
    rows = store.conn.execute(
        "SELECT id, code, at, kind, origin, message_id, detail FROM events "
        "WHERE reported IS NULL AND kind NOT IN ('note', 'follow_up') ORDER BY at, id")
    return [Event(id=i, code=c, at=datetime.fromisoformat(at), kind=k, origin=o, message_id=m, detail=d)
            for i, c, at, k, o, m, d in rows]


def gather(store: Store | None, rules: TrackingRules, now: datetime, *, write: bool) -> Activity | None:
    """What the next digest reports. With write, the no-response rule is applied first;
    without (a dry run), its closures are shown but not saved. None when jobs.db has no tracker yet."""
    if store is None or not store.has_tracker:
        return None
    if write:
        close_unanswered(store, rules, now)
    activity = Activity()
    apps: dict[int, Application] = {}

    def app_for(code: int) -> Application:
        if code not in apps:
            app = get_application(store, code)
            assert app is not None
            apps[code] = app
        return apps[code]

    for e in _unreported(store):
        activity.event_ids.append(e.id)
        (activity.logged if e.kind == "applied" else activity.changes).append(Change(app_for(e.code), e))

    touched = {c.event.code for c in activity.logged + activity.changes}
    for app in list_applications(store):
        events = events_for(store, app.code)
        if not write and app.stage == "applied":
            due = _no_response_due(events, rules.no_response_days, now)
            if due is not None:   # what a real run would close
                closed = Application(**{**app.__dict__, "stage": "no_response", "stage_at": due})
                activity.changes.append(Change(closed, Event(
                    id=0, code=app.code, at=due, kind="no_response", origin=RULE,
                    detail=f"no reply in {rules.no_response_days} days")))
                touched.add(app.code)
                continue
        if app.code in touched or app.stage not in OPEN_STAGES:
            continue
        since = _quiet_since(events)
        days = (now - since).days
        if days >= rules.follow_up_days and not _reminded(events, since):
            activity.follow_ups.append(FollowUp(app, since, days))
    activity.changes.sort(key=lambda c: (c.event.at, c.event.id))
    activity.follow_ups.sort(key=lambda f: f.quiet_since)
    return activity


def record(store: Store, activity: Activity, now: datetime) -> None:
    """The digest carrying `activity` was sent: mark its events reported and keep its follow-ups."""
    stamp = _ts(now)
    with store.conn:
        store.conn.executemany("UPDATE events SET reported = ? WHERE id = ? AND reported IS NULL",
                               [(stamp, i) for i in activity.event_ids])
        for f in activity.follow_ups:
            _add_event(store, f.app.code, "follow_up", now, RULE, detail=f"quiet {f.days} days", reported=now)


# --- wording -------------------------------------------------------------------------

def role(app: Application) -> str:
    return f"#{app.code} {app.company} — {app.title}"


def logged_line(c: Change) -> str:
    extras = [f"applied {local_day(c.event.at)}"]
    if c.app.resume_version:
        extras.append(f"resume {c.app.resume_version}")
    if c.app.referral:
        extras.append("referral")
    return f"{role(c.app)} · {' · '.join(extras)}"


def change_line(c: Change) -> str:
    e = c.event
    day = local_day(e.at)
    if e.kind == "no_response" and e.origin == RULE:
        what = f"no response: {e.detail or 'no reply'}, closed {day}"
    elif e.kind == "reply":
        what = f"reply {day}"
    elif e.kind == "reopened":
        what = f"reopened {day}"
    elif e.kind in ("rejected", "withdrawn") and e.detail:
        what = f"{stage_label(e.kind)} {day} (at {stage_label(e.detail)})"
    else:
        what = f"{stage_label(e.kind)} {day}"
    if e.origin == "email":
        what += " (from email)"
    return f"{role(c.app)} · {what}"


def follow_up_line(f: FollowUp) -> str:
    app = f.app
    where = f"applied {local_day(app.applied_at)}" if app.stage == "applied" else \
        f"{stage_label(app.stage)} since {local_day(app.stage_at)}"
    shown = app.applied_at if app.stage == "applied" else app.stage_at
    if local_day(f.quiet_since) != local_day(shown):
        where += f" · last entry {local_day(f.quiet_since)}"
    return f"{role(app)} · {where} · quiet {f.days} days"


def blocks(activity: Activity, follow_up_days: int) -> list[tuple[str, list[str]]]:
    """(heading, lines) for each non-empty part, in digest order."""
    parts = [
        ("Logged since the last digest", [logged_line(c) for c in activity.logged]),
        ("Updates", [change_line(c) for c in activity.changes]),
        (f"Follow up (nothing new in {follow_up_days}+ days)", [follow_up_line(f) for f in activity.follow_ups]),
    ]
    return [(h, lines) for h, lines in parts if lines]
