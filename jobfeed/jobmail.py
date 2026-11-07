"""Tying a job email to an application, and recording what it says.

Matching, strongest signal first:
  1. a Greenhouse, Lever or Ashby job link in the message, matched to the board
     of an application's posting;
  2. the sender's own domain (or Reply-To, for mail sent through an ATS),
     matched to the company's name: jobs@duckduckgo.com -> DuckDuckGo;
  3. the company's name in the sender's name, subject or opening lines.
When that leaves more than one application, the role title in the message
decides; when it still can't, or finds none, the message goes to "Check these".

What each kind does:
  confirmation  logs the application if it isn't logged yet (from the stored
                posting it matches), else is noted on it; an earlier
                confirmation time corrects the applied date
  rejection     rejected, recording the stage it had reached
  interview     screen; interview if already past the screen
  offer         offer
  reply         a reply; the stage stays
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from jobfeed.classify import JobMail, Reading, domain, is_ats
from jobfeed.config import TrackingRules
from jobfeed.sources.common import board_of
from jobfeed.store import Row, Store, _ts, normalize_url
from jobfeed import tracker
from jobfeed.tracker import Application, LogOptions, TrackerError

ORIGIN = "email"
_SUFFIXES = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "technologies", "technology", "labs",
             "hq", "group", "plc", "gmbh", "the", "limited", "holdings"}
_FILLER = {"and", "of", "the", "for", "a", "an", "to", "in", "at", "with"}
CLOSED_FOR_MAIL = ("rejected", "withdrawn", "accepted", "declined")
# Mail can arrive before you log an application (a confirmation, minutes after applying), so an
# application logged up to this long after a message can still be the one it's about.
LOGGED_LATE = timedelta(days=7)


# --- names, domains, titles ------------------------------------------------------------

def name_words(name: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", name.lower())
    kept = [w for w in words if w not in _SUFFIXES]
    return kept or words


def mentions(name: str, text: str) -> bool:
    words = name_words(name)
    if not words:
        return False
    pattern = r"\b" + r"[\s\-.]*".join(map(re.escape, words)) + r"\b"
    return re.search(pattern, text.lower()) is not None


def domain_label(address: str) -> str:
    """mail.duckduckgo.com -> duckduckgo; acme.co.uk -> acme."""
    parts = domain(address).split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov") and len(parts[-1]) == 2:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else parts[0]


def domain_matches(address: str, company: str) -> bool:
    if not address:
        return False
    label = domain_label(address)
    words = name_words(company)
    joined = "".join(words)
    return bool(words) and (label == joined or (len(words[0]) >= 4 and label == words[0])
                            or (len(joined) >= 4 and label.startswith(joined)))


def title_score(title: str, text: str) -> float:
    words = [w for w in re.findall(r"[a-z0-9+#]+", title.lower()) if w not in _FILLER]
    if not words:
        return 0.0
    low = text.lower()
    return sum(1 for w in words if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", low)) / len(words)


def boards_in(links: list[str]) -> set[tuple[str, str]]:
    found = set()
    for url in links:
        b = board_of(url)
        if b:
            found.add((b[0], b[1].lower()))
    return found


def _boards(*urls: str | None) -> set[tuple[str, str]]:
    return boards_in([u for u in urls if u])


def links_to(links: list[str], *urls: str | None) -> bool:
    """Whether any of the message's links is the posting itself (its page, or a page under it)."""
    targets = [normalize_url(u) for u in urls if u and board_of(u)]
    return any(normalize_url(l).startswith(t) for l in links for t in targets if t)


# --- matching ------------------------------------------------------------------------------

@dataclass
class Match:
    app: Application | None
    why: str
    by_link: bool = False      # matched by the posting's own job link, the surest sign


def _narrow(cands: list, title_of, text: str, label: str) -> tuple[object | None, str]:
    if len(cands) == 1:
        return cands[0], label
    scored = sorted(((title_score(title_of(c), text), i) for i, c in enumerate(cands)), reverse=True)
    best, i = scored[0]
    if best >= 0.6 and (len(scored) == 1 or best > scored[1][0]):
        return cands[i], f"{label}, and the role title"
    return None, f"{label}, but it fits {len(cands)} of them and the role title doesn't settle which"


def company_domains(mail: JobMail, *, from_ats: bool, ats_domains: list[str]) -> list[str]:
    """Addresses that may be the company's own: the sender (unless an ATS sent it) and Reply-To."""
    return [d for d in ([] if from_ats else [mail.sender]) + [mail.reply_to] if d and not is_ats(d, ats_domains)]


def match_application(store: Store, mail: JobMail, *, domains: list[str], at: datetime) -> Match:
    apps = [a for a in tracker.list_applications(store, include_closed=True) if a.applied_at <= at + LOGGED_LATE]
    if not apps:
        return Match(None, "you have no applications it could be about")
    text = mail.name + "\n" + mail.opening
    rows = store.get([a.job_id for a in apps if a.job_id])

    def urls(a: Application) -> tuple:
        row = rows.get(a.job_id) if a.job_id else None
        return (a.url, row.job.url if row else None, row.job.apply_url if row else None)

    mail_boards = boards_in(mail.links)
    for label, cands in [
        ("its job link", [a for a in apps if links_to(mail.links, *urls(a))]),
        ("a link to the company's job board", [a for a in apps if mail_boards and _boards(*urls(a)) & mail_boards]),
        ("the sender's domain", [a for a in apps if any(domain_matches(d, a.company) for d in domains)]),
        ("the company's name", [a for a in apps if mentions(a.company, text)]),
    ]:
        if cands:
            app, why = _narrow(cands, lambda a: a.title, text, f"matched by {label}")
            return Match(app, why, by_link=label == "its job link")  # type: ignore[arg-type]
    return Match(None, "it doesn't name a company or role you've applied to")


def other_role(store: Store, mail: JobMail, app: Application) -> Row | None:
    """A posting at the same company that the message names more clearly than `app`: mail about a
    role you haven't logged, which would otherwise land on the one you have."""
    text = mail.name + "\n" + mail.opening
    mine = title_score(app.title, text)
    company = name_words(app.company)
    best, best_score = None, 0.0
    for row in store.all_rows():
        if row.code is None or row.job.id == app.job_id or name_words(row.job.company) != company:
            continue
        score = title_score(row.job.title, text)
        if score > best_score:
            best, best_score = row, score
    return best if best is not None and best_score >= 0.6 and best_score > mine else None


def match_posting(store: Store, mail: JobMail, *, domains: list[str]) -> tuple[Row | None, str]:
    """For a confirmation of something you never logged: the emailed posting it's about."""
    text = mail.name + "\n" + mail.opening
    mail_boards = boards_in(mail.links)
    rows = [r for r in store.all_rows() if r.code is not None]
    for label, cands in [
        ("its job link", [r for r in rows if links_to(mail.links, r.job.url, r.job.apply_url)]),
        ("a link to the company's job board",
         [r for r in rows if mail_boards and _boards(r.job.url, r.job.apply_url) & mail_boards]),
        ("the sender's domain", [r for r in rows if any(domain_matches(d, r.job.company) for d in domains)]),
        ("the company's name", [r for r in rows if mentions(r.job.company, text)]),
    ]:
        if cands:
            row, why = _narrow(cands, lambda r: r.job.title, text, f"a posting matched by {label}")
            return row, why  # type: ignore[return-value]
    return None, "no posting jobfeed has emailed matches it"


# --- acting ------------------------------------------------------------------------------------

@dataclass
class Result:
    reading: Reading
    code: int | None
    action: str        # what was done, or why nothing was
    done: bool         # False: it goes to "Check these"


def _has_confirmation(app: Application) -> bool:
    return any(e.kind == "confirmation" for e in app.events)


def _confirm(store: Store, app: Application, at: datetime, message_id: str) -> str:
    with store.conn:
        tracker._add_event(store, app.code, "confirmation", at, ORIGIN, message_id=message_id)
        moved = ""
        if timedelta(0) < app.applied_at - at <= timedelta(days=7):
            # The confirmation shows when you actually applied.
            store.conn.execute("UPDATE events SET at = ? WHERE code = ? AND kind = 'applied'", (_ts(at), app.code))
            store.conn.execute("UPDATE applications SET applied_at = ? WHERE code = ?", (_ts(at), app.code))
            moved = f"; applied date corrected to {tracker.local_day(at)}"
        tracker._refresh(store, app.code)
    return f"confirmation recorded on #{app.code}{moved}"


def act(store: Store, mail: JobMail, reading: Reading, *, at: datetime, message_id: str, from_ats: bool,
        rules: TrackingRules) -> Result:
    if reading.kind is None:
        return Result(reading, None, reading.why, False)
    domains = company_domains(mail, from_ats=from_ats, ats_domains=rules.ats_senders)
    m = match_application(store, mail, domains=domains, at=at)
    app = tracker.get_application(store, m.app.code, with_events=True) if m.app else None
    named: Row | None = None
    if app is not None and not m.by_link:
        named = other_role(store, mail, app)
        if named is not None:
            app = None

    if app is None:
        if reading.kind != "confirmation":
            if named is not None:
                return Result(reading, named.code, f"it's about {named.job.title} (#{named.code}), which you haven't "
                                                   f"logged; `jobfeed apply {named.code}`, then record what it says",
                              False)
            return Result(reading, None, m.why, False)
        if named is not None:
            row, why = named, "a posting named in it"
        else:
            row, why = match_posting(store, mail, domains=domains)
        if row is None:
            return Result(reading, None, f"a confirmation for a role you haven't logged, and {why}; log it with "
                                         "`jobfeed apply --url`", False)
        opts = LogOptions(applied_at=at, resume_version=rules.resume_version, cover_letter=rules.cover_letter,
                          logged_via="confirmation", origin=ORIGIN, message_id=message_id)
        try:
            new = tracker.log_posting(store, row, opts)
        except TrackerError as exc:
            return Result(reading, row.code, str(exc), False)
        with store.conn:
            tracker._add_event(store, new.code, "confirmation", at, ORIGIN, message_id=message_id)
        return Result(reading, new.code, f"logged #{new.code} {new.company} — {new.title} ({why})", True)

    label = f"#{app.code} {app.company} — {app.title}"
    kind = reading.kind
    if kind == "confirmation":
        if _has_confirmation(app):
            return Result(reading, app.code, f"{label}: already confirmed", True)
        return Result(reading, app.code, f"{label}: {_confirm(store, app, at, message_id)}", True)
    if kind == "interview":
        if app.stage in CLOSED_FOR_MAIL:
            return Result(reading, app.code, f"an interview request for {label}, which is {tracker.stage_label(app.stage)}",
                          False)
        kind = {"applied": "screen", "no_response": "screen", "screen": "interview"}.get(app.stage, "reply")
    elif kind == "offer" and app.stage in ("offer", "accepted"):
        return Result(reading, app.code, f"{label}: already at {app.stage}", True)
    elif kind == "rejection":
        if app.stage in CLOSED_FOR_MAIL:
            return Result(reading, app.code, f"{label}: already {tracker.stage_label(app.stage)}", True)
        kind = "rejected"
    try:
        updated = tracker.mark(store, app.code, kind, at, origin=ORIGIN, message_id=message_id)
    except TrackerError as exc:
        return Result(reading, app.code, f"{label}: {exc}", False)
    what = "a reply" if kind == "reply" else tracker.stage_label(kind)
    return Result(reading, app.code, f"{label}: {what}, now at {tracker.stage_label(updated.stage)} ({m.why})", True)
