"""Commands written in a reply to the digest: reading them out of the email and carrying them out.

One command per line, the same words as the CLI:

    applied 412 418
    applied 430 resume v2 referral cover letter
    applied 431 yesterday note found it through Sam
    screen 412
    rejected 418 on 2026-11-05
    interview 412 onsite with the platform team     (the words after the codes become a note)
    note 412 recruiter call Tue 2pm

The quoted digest below your reply, signatures, and lines that are plainly not
commands ("Thanks!") are ignored. A line that can't be read or carried out is
quoted back, with the reason, in the next digest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.message import EmailMessage

from jobfeed.config import TrackingRules
from jobfeed.store import Store
from jobfeed.text import html_to_text
from jobfeed import tracker
from jobfeed.tracker import ALIASES, MARKABLE, LogOptions, TrackerError

ORIGIN = "reply"

_CODE = re.compile(r"^#?(\d{1,7})$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ParseError(Exception):
    """A reply line that isn't a command jobfeed knows."""


@dataclass
class Command:
    line: str                     # as you wrote it
    verb: str                     # apply | mark | note
    codes: list[int]
    kind: str | None = None       # the stage, for mark
    day: str | None = None        # YYYY-MM-DD, "today" or "yesterday"; None = when the reply was sent
    resume: str | None = None
    cover_letter: bool | None = None
    referral: bool = False
    note: str | None = None


@dataclass
class Outcome:
    line: str
    ok: bool
    text: str                     # what happened, or why not


# --- reading the email ------------------------------------------------------------------

def body_text(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, ValueError):   # an unknown charset
        return ""
    return html_to_text(text) if part.get_content_type() == "text/html" else text


_STOP = [
    re.compile(r"^>"),                                          # quoted text
    re.compile(r"^On\b.*\bwrote:\s*$", re.I),                   # Gmail, Apple Mail: "On Fri, ... wrote:"
    re.compile(r"^-- ?$"),                                      # signature
    re.compile(r"^-{3,}\s*(Original|Forwarded) Message", re.I),
    re.compile(r"^_{8,}$"),                                     # Outlook's rule above the quote
    re.compile(r"^(From|Sent|To|Subject|Date):\s", re.I),       # Outlook's quote header
    re.compile(r"^Job feed · "),                                # the digest itself, quoted without ">"
]
_SKIP = [
    re.compile(r"^Sent from my\b", re.I),
    re.compile(r"^Get Outlook for\b", re.I),
]


def command_lines(body: str) -> list[str]:
    """The lines you wrote: everything above the quoted original, without blanks or sign-offs."""
    lines = [l.replace(" ", " ").replace("​", "").strip() for l in body.splitlines()]
    out = []
    for i, line in enumerate(lines):
        if any(p.search(line) for p in _STOP):
            break
        # "On Fri, Nov 6, 2026 at 6:00 AM jobfeed <me@gmail.com>" wrapped onto two lines
        if line.startswith("On ") and i + 1 < len(lines) and lines[i + 1].rstrip().endswith("wrote:"):
            break
        if not line or any(p.search(line) for p in _SKIP):
            continue
        out.append(line)
    return out


# --- parsing a line ------------------------------------------------------------------------

def _verb(words: list[str]) -> tuple[str, str | None, int]:
    """(verb, stage kind, words used)."""
    first = words[0].lower().rstrip(":")
    if len(words) > 1:
        pair = f"{first}-{words[1].lower()}"
        if pair in ALIASES:
            return "mark", ALIASES[pair], 2
    if first in ("applied", "apply"):
        return "apply", None, 1
    if first in ("note", "notes"):
        return "note", None, 1
    if first in ALIASES:
        return "mark", ALIASES[first], 1
    raise ParseError(f"'{words[0]}' isn't a command; start with applied, a stage such as screen or rejected, or note")


def _day(words: list[str], i: int) -> tuple[str | None, int]:
    """An optional date at words[i]: '2026-11-05', 'on 2026-11-05', 'today' or 'yesterday'."""
    if i < len(words) and words[i].lower() == "on" and i + 1 < len(words) and _DATE.match(words[i + 1]):
        return words[i + 1], i + 2
    if i < len(words) and (_DATE.match(words[i]) or words[i].lower() in ("today", "yesterday")):
        return words[i].lower(), i + 1
    return None, i


def parse(line: str) -> Command:
    words = line.replace(",", " ").split()
    if not words:
        raise ParseError("empty line")
    verb, kind, i = _verb(words)
    codes = []
    while i < len(words) and _CODE.match(words[i]):
        codes.append(int(_CODE.match(words[i]).group(1)))
        i += 1
    if not codes:
        raise ParseError("no code; put the digest number right after the command, like `screen 412`")
    cmd = Command(line=line, verb=verb, codes=codes, kind=kind)
    if verb == "note":
        cmd.note = " ".join(words[i:]) or None
        if not cmd.note:
            raise ParseError("the note is empty")
        return cmd
    if verb == "mark":
        cmd.day, i = _day(words, i)
        cmd.note = " ".join(words[i:]) or None
        return cmd
    while i < len(words):                     # applied: options in any order
        word = words[i].lower()
        if word == "resume" and i + 1 < len(words):
            cmd.resume, i = words[i + 1], i + 2
        elif word in ("referral", "referred"):
            cmd.referral, i = True, i + 1
        elif word in ("cover-letter", "coverletter"):
            cmd.cover_letter, i = True, i + 1
        elif word == "cover" and i + 1 < len(words) and words[i + 1].lower() == "letter":
            cmd.cover_letter, i = True, i + 2
        elif word == "no" and [w.lower() for w in words[i + 1:i + 3]] == ["cover", "letter"]:
            cmd.cover_letter, i = False, i + 3
        elif word == "note":
            cmd.note, i = " ".join(words[i + 1:]) or None, len(words)
        else:
            day, j = _day(words, i)
            if day is None:
                raise ParseError(f"didn't understand '{words[i]}'; applied takes resume V, referral, cover letter, "
                                 "a date, and note …")
            cmd.day, i = day, j
    return cmd


# --- carrying it out -----------------------------------------------------------------------

def _when(cmd: Command, sent_at: datetime, now: datetime) -> datetime:
    if cmd.day is None:
        return min(sent_at, now)
    local_sent = sent_at.astimezone().date()
    if cmd.day == "today":
        return min(sent_at, now)
    text = (local_sent - timedelta(days=1)).isoformat() if cmd.day == "yesterday" else cmd.day
    return tracker.parse_day(text, now)


def run(store: Store, cmd: Command, sent_at: datetime, rules: TrackingRules, now: datetime) -> list[Outcome]:
    try:
        at = _when(cmd, sent_at, now)
    except TrackerError as exc:
        return [Outcome(cmd.line, False, str(exc))]
    outcomes = []
    for code in cmd.codes:
        try:
            if cmd.verb == "apply":
                row = store.by_code(code)
                if row is None:
                    existing = tracker.get_application(store, code)
                    raise TrackerError(f"#{code} is already logged" if existing else
                                       f"no posting #{code}; log roles from elsewhere with `jobfeed apply --url` on the Pi")
                opts = LogOptions(applied_at=at, resume_version=cmd.resume or rules.resume_version,
                                  cover_letter=rules.cover_letter if cmd.cover_letter is None else cmd.cover_letter,
                                  referral=cmd.referral, note=cmd.note, logged_via=ORIGIN, origin=ORIGIN)
                app = tracker.log_posting(store, row, opts)
                text = f"logged #{app.code} {app.company} — {app.title}"
            elif cmd.verb == "mark":
                app = tracker.mark(store, code, cmd.kind, at, origin=ORIGIN, note=cmd.note)
                text = f"#{app.code} {app.company}: {tracker.stage_label(cmd.kind)}, now at {tracker.stage_label(app.stage)}"
            else:
                app = tracker.add_note(store, code, cmd.note or "", at, origin=ORIGIN)
                text = f"noted on #{app.code} {app.company}"
            outcomes.append(Outcome(cmd.line, True, text))
        except TrackerError as exc:
            reason = str(exc)
            if len(cmd.codes) > 1 and f"#{code}" not in reason:
                reason = f"#{code}: {reason}"
            outcomes.append(Outcome(cmd.line, False, reason))
    return outcomes


def is_chatter(line: str) -> bool:
    """A line that was never meant as a command ("Thanks!", a name): no code, and no command word first.
    Every command has a code, so these are skipped rather than quoted back."""
    if any(ch.isdigit() for ch in line):
        return False
    try:
        _verb(line.replace(",", " ").split())
    except (ParseError, IndexError):
        return True
    return False


def run_reply(store: Store, msg: EmailMessage, sent_at: datetime, rules: TrackingRules,
              now: datetime) -> list[Outcome]:
    """Every command line in one reply, in order."""
    outcomes: list[Outcome] = []
    for line in command_lines(body_text(msg)):
        try:
            cmd = parse(line)
        except ParseError as exc:
            if is_chatter(line):
                continue
            outcomes.append(Outcome(line, False, str(exc)))
            continue
        outcomes += run(store, cmd, sent_at, rules, now)
    return outcomes


assert set(MARKABLE) <= set(ALIASES.values())
