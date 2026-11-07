"""Checking Gmail for your replies to the digest and for job mail, and acting on them.

Each check runs a few Gmail searches, all from a day before the last successful
check (a week, the first time):

- mail from your own addresses with "Job feed" in the subject: your replies to
  the digest. The digests themselves are skipped, and a reply has to be one
  Gmail sent for you (labelled Sent) or one that passed Gmail's sender check
  (DMARC), so a forged "From: you" can't log anything;
- mail from the applicant-tracking systems in `tracking.ats_senders`;
- mail from the companies you have open applications with, by name.

Job mail is read by classify.py and tied to an application by jobmail.py;
anything it can't place goes to "Check these" in the next digest. Newsletters,
and automated company mail with none of the job wording, are passed over.

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

from jobfeed import jobmail, tracker
from jobfeed.classify import classify, is_ats, read_message
from jobfeed.config import TrackingRules
from jobfeed.imap import RawMail
from jobfeed.mailer import MailSettings
from jobfeed.replies import Outcome, run_reply
from jobfeed.store import Store, _ts

CHECKED_KEY = "mail_checked"
FIRST_LOOK_DAYS = 7
SUBJECT = "Job feed"
NAMES_PER_SEARCH = 15


class MailSource(Protocol):
    def find(self, query: str, skip: Iterable[str] = ()) -> list[RawMail]: ...


@dataclass
class Reply:
    gm_id: str
    sent_at: datetime
    subject: str
    outcomes: list[Outcome] = field(default_factory=list)


@dataclass
class JobMailItem:
    gm_id: str
    at: datetime
    who: str
    subject: str
    result: jobmail.Result


@dataclass
class MailCheck:
    since: datetime
    looked_at: int = 0
    replies: list[Reply] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)   # "sender, subject" of replies that were ignored
    job_mail: list[JobMailItem] = field(default_factory=list)
    passed_over: int = 0                                    # newsletters and other company mail

    @property
    def lines_run(self) -> int:
        return sum(1 for r in self.replies for o in r.outcomes if o.ok)

    @property
    def problems(self) -> list[Outcome]:
        return [o for r in self.replies for o in r.outcomes if not o.ok]

    def summary(self) -> str:
        if not self.replies and not self.unverified and not self.job_mail:
            return "Gmail: no new replies or job mail."
        parts = []
        if self.replies or self.unverified:
            n = len(self.replies)
            text = f"{n} {'reply' if n == 1 else 'replies'}, {self.lines_run} line{'' if self.lines_run == 1 else 's'} run"
            if self.problems:
                text += f", {len(self.problems)} not used"
            if self.unverified:
                text += f"; ignored {len(self.unverified)} that Gmail couldn't verify came from you"
            parts.append(text)
        if self.job_mail:
            kinds: dict[str, int] = {}
            for item in self.job_mail:
                key = item.result.reading.kind if item.result.done else "to check"
                kinds[key or "to check"] = kinds.get(key or "to check", 0) + 1
            n = len(self.job_mail)
            parts.append(f"{n} job email{'' if n == 1 else 's'} (" + ", ".join(f"{v} {k}" for k, v in kinds.items()) + ")")
        return "Gmail: " + "; ".join(parts) + "."


def _after(since: datetime) -> str:
    return f"after:{since.astimezone():%Y/%m/%d}"


def query(addresses: Iterable[str], since: datetime) -> str:
    """A Gmail search for mail from these addresses about the digest, on or after `since`'s day."""
    who = " OR ".join(sorted(addresses))
    return f'from:({who}) subject:"{SUBJECT}" {_after(since)}'


def _term(company: str) -> list[str]:
    words = jobmail.name_words(company)
    if not words:
        return []
    if len(words) == 1:
        return [words[0]]
    return [f'"{" ".join(words)}"', "".join(words)]      # "motive analytics" in a name; motiveanalytics in an address


def queries(store: Store, addresses: Iterable[str], rules: TrackingRules, since: datetime) -> list[str]:
    """Every search a check runs: your replies, the ATS senders, and your open applications' companies."""
    out = [query(addresses, since)]
    if rules.ats_senders:
        out.append(f"from:({' OR '.join(rules.ats_senders)}) {_after(since)}")
    terms = sorted({t for app in tracker.list_applications(store) for t in _term(app.company)})
    for i in range(0, len(terms), NAMES_PER_SEARCH):
        out.append(f"from:({' OR '.join(terms[i:i + NAMES_PER_SEARCH])}) {_after(since)}")
    return out


def _addresses(settings: MailSettings) -> set[str]:
    return {a.lower() for a in (settings.user, *settings.to)}


def _is_reply(msg: EmailMessage) -> bool:
    if (msg.get("X-Mailer") or "").strip() == "jobfeed":
        return False     # a digest (or weekly report) itself
    subject = (msg.get("Subject") or "").strip().lower()
    if SUBJECT.lower() not in subject:
        return False     # your own mail about something else
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


def _remember(store: Store, gm_id: str, kind: str | None, now: datetime, code: int | None = None) -> None:
    with store.conn:
        store.conn.execute("INSERT OR IGNORE INTO mail_seen (message_id, seen_at, kind, code) VALUES (?, ?, ?, ?)",
                           (gm_id, _ts(now), kind, code))


def add_notice(store: Store, kind: str, text: str, now: datetime) -> None:
    with store.conn:
        store.conn.execute("INSERT INTO notices (at, kind, text) VALUES (?, ?, ?)", (_ts(now), kind, text))


def _who(msg: EmailMessage) -> str:
    name, address = parseaddr(str(msg.get("From") or ""))
    return f"{name} <{address}>" if name else address


def _job_mail(store: Store, msg: EmailMessage, mail: RawMail, rules: TrackingRules, now: datetime,
              result: MailCheck) -> None:
    jm = read_message(msg)
    from_ats = is_ats(jm.sender, rules.ats_senders)
    reading = classify(jm, from_ats=from_ats)
    if not from_ats and (jm.bulk or reading.kind is None):
        # A company newsletter, a receipt, a product update: not about an application.
        result.passed_over += 1
        _remember(store, mail.gm_id, None, now)
        return
    at = _sent_at(msg, now)
    res = jobmail.act(store, jm, reading, at=at, message_id=mail.gm_id, from_ats=from_ats, rules=rules)
    item = JobMailItem(mail.gm_id, at, _who(msg), jm.subject, res)
    if not res.done:
        add_notice(store, "check", f"{item.who}, “{item.subject}” ({tracker.local_day(at)}): {res.action}", now)
    _remember(store, mail.gm_id, reading.kind if res.done else "check", now, res.code)
    result.job_mail.append(item)


def check(store: Store, source: MailSource, settings: MailSettings, rules: TrackingRules, now: datetime,
          *, since: datetime | None = None, reread: bool = False) -> MailCheck:
    """Read new replies and job mail and act on them. Raises imap.ReadError if Gmail can't be read.
    reread: also messages already read (for a dry run's scratch copy only)."""
    if since is None:
        last = store.get_meta(CHECKED_KEY)
        since = (datetime.fromisoformat(last) - timedelta(days=1)) if last else now - timedelta(days=FIRST_LOOK_DAYS)
    result = MailCheck(since=since)
    addresses = _addresses(settings)
    skip = set() if reread else seen_ids(store)
    found: dict[str, RawMail] = {}
    for q in queries(store, addresses, rules, since):
        for mail in source.find(q, skip=skip | set(found)):
            found[mail.gm_id] = mail
    parsed = [(m, message_from_bytes(m.raw, policy=policy.default)) for m in found.values()]
    parsed.sort(key=lambda pair: (_sent_at(pair[1], now), pair[0].gm_id))
    for mail, msg in parsed:
        result.looked_at += 1
        sender = parseaddr(str(msg.get("From") or ""))[1].lower()
        if sender not in addresses:
            _job_mail(store, msg, mail, rules, now, result)
            continue
        if not _is_reply(msg):
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

