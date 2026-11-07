"""Checking Gmail for your replies to the digest, and carrying out what they say.

Each check asks Gmail for mail from your own addresses with "Job feed" in the
subject, since a day before the last successful check (a week, the first time).
Of those, it acts only on replies you wrote: the digests themselves are skipped,
and a reply has to be one Gmail sent for you (labelled Sent) or one that passed
Gmail's sender check (DMARC), so a forged "From: you" can't log anything.

Every message looked at is remembered in mail_seen by its Gmail ID and never
looked at twice. Nothing from the message itself is stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from typing import Iterable, Protocol

from jobfeed.config import TrackingRules
from jobfeed.imap import RawMail
from jobfeed.mailer import MailSettings
from jobfeed.replies import Outcome, run_reply
from jobfeed.store import Store, _ts

CHECKED_KEY = "mail_checked"
FIRST_LOOK_DAYS = 7
SUBJECT = "Job feed"


class MailSource(Protocol):
    def find(self, query: str, skip: Iterable[str] = ()) -> list[RawMail]: ...


@dataclass
class Reply:
    gm_id: str
    sent_at: datetime
    subject: str
    outcomes: list[Outcome] = field(default_factory=list)


@dataclass
class MailCheck:
    since: datetime
    looked_at: int = 0
    replies: list[Reply] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)   # "sender, subject" of replies that were ignored

    @property
    def lines_run(self) -> int:
        return sum(1 for r in self.replies for o in r.outcomes if o.ok)

    @property
    def problems(self) -> list[Outcome]:
        return [o for r in self.replies for o in r.outcomes if not o.ok]

    def summary(self) -> str:
        if not self.replies and not self.unverified:
            return "Gmail: no new replies."
        n = len(self.replies)
        text = f"Gmail: {n} {'reply' if n == 1 else 'replies'}, {self.lines_run} line{'' if self.lines_run == 1 else 's'} run"
        if self.problems:
            text += f", {len(self.problems)} not used (see Your applications)"
        if self.unverified:
            text += f"; ignored {len(self.unverified)} that Gmail couldn't verify came from you"
        return text + "."


def query(addresses: Iterable[str], since: datetime) -> str:
    """A Gmail search for mail from these addresses about the digest, on or after `since`'s day."""
    who = " OR ".join(sorted(addresses))
    return f'from:({who}) subject:"{SUBJECT}" after:{since.astimezone():%Y/%m/%d}'


def _addresses(settings: MailSettings) -> set[str]:
    return {a.lower() for a in (settings.user, *settings.to)}


def _is_reply(msg: EmailMessage) -> bool:
    if (msg.get("X-Mailer") or "").strip() == "jobfeed":
        return False     # a digest (or weekly report) itself
    subject = (msg.get("Subject") or "").strip().lower()
    return bool(msg.get("In-Reply-To")) or subject.startswith(("re:", "re :"))


def _verified(msg: EmailMessage, labels: tuple[str, ...]) -> bool:
    if "\\Sent" in labels:
        return True      # Gmail sent it from your account
    results = " ".join(str(v) for v in msg.get_all("Authentication-Results") or []).lower()
    return "dmarc=pass" in results


def _sent_at(msg: EmailMessage, now: datetime) -> datetime:
    try:
        when = parsedate_to_datetime(msg.get("Date") or "")
    except (TypeError, ValueError):
        return now
    if when is None:
        return now
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return min(when.astimezone(timezone.utc), now)


def seen_ids(store: Store) -> set[str]:
    return {m for (m,) in store.conn.execute("SELECT message_id FROM mail_seen")}


def _remember(store: Store, gm_id: str, kind: str | None, now: datetime) -> None:
    with store.conn:
        store.conn.execute("INSERT OR IGNORE INTO mail_seen (message_id, seen_at, kind, code) VALUES (?, ?, ?, NULL)",
                           (gm_id, _ts(now), kind))


def add_notice(store: Store, kind: str, text: str, now: datetime) -> None:
    with store.conn:
        store.conn.execute("INSERT INTO notices (at, kind, text) VALUES (?, ?, ?)", (_ts(now), kind, text))


def check(store: Store, source: MailSource, settings: MailSettings, rules: TrackingRules, now: datetime,
          *, since: datetime | None = None) -> MailCheck:
    """Read new replies and carry them out. Raises imap.ReadError if Gmail can't be read."""
    if since is None:
        last = store.get_meta(CHECKED_KEY)
        since = (datetime.fromisoformat(last) - timedelta(days=1)) if last else now - timedelta(days=FIRST_LOOK_DAYS)
    result = MailCheck(since=since)
    addresses = _addresses(settings)
    mails = source.find(query(addresses, since), skip=seen_ids(store))
    for mail in sorted(mails, key=lambda m: int(m.gm_id) if m.gm_id.isdigit() else 0):
        result.looked_at += 1
        msg = message_from_bytes(mail.raw, policy=policy.default)
        sender = parseaddr(str(msg.get("From") or ""))[1].lower()
        if not _is_reply(msg) or sender not in addresses:
            _remember(store, mail.gm_id, None, now)
            continue
        subject = str(msg.get("Subject") or "")
        if not _verified(msg, mail.labels):
            result.unverified.append(f"{sender}, “{subject}”")
            add_notice(store, "unverified", f"Ignored a reply from {sender} (“{subject}”) that Gmail couldn't "
                                            "verify came from you; nothing in it was done", now)
            _remember(store, mail.gm_id, "unverified", now)
            continue
        sent_at = _sent_at(msg, now)
        reply = Reply(mail.gm_id, sent_at, subject, run_reply(store, msg, sent_at, rules, now))
        for o in reply.outcomes:
            if not o.ok:
                add_notice(store, "reply_line", f"“{o.line}”: {o.text}", now)
        _remember(store, mail.gm_id, "reply", now)
        result.replies.append(reply)
    store.set_meta(CHECKED_KEY, _ts(now))
    return result

