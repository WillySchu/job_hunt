"""A stand-in for imaplib.IMAP4_SSL talking to Gmail, with the response shapes imaplib returns."""

from __future__ import annotations

import imaplib
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

ME = "me@gmail.com"


def reply(body: str, *, sent: datetime, sender: str = ME, subject: str = "Re: Job feed · Fri Nov 6 · 2 new (0 watchlist)",
          html: bool = False, in_reply_to: str | None = "<digest@gmail.com>",
          auth: str | None = None, quoted: bool = True) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"Me <{sender}>"
    msg["To"] = ME
    msg["Subject"] = subject
    msg["Date"] = format_datetime(sent)
    msg["Message-ID"] = make_msgid(domain="gmail.com")
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if auth:
        msg["Authentication-Results"] = auth
    if quoted:
        body += ("\n\nOn Fri, Nov 6, 2026 at 6:00 AM jobfeed <me@gmail.com> wrote:\n"
                 "> Job feed · Fri Nov 6 · 2 new (0 watchlist)\n> applied 999\n")
    if html:
        msg.set_content("<div>" + body.replace("\n", "<br>") + "</div>", subtype="html")
    else:
        msg.set_content(body)
    return msg


def digest_copy(sent: datetime) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"jobfeed <{ME}>"
    msg["To"] = ME
    msg["Subject"] = "Job feed · Fri Nov 6 · 2 new (0 watchlist)"
    msg["Date"] = format_datetime(sent)
    msg["X-Mailer"] = "jobfeed"
    msg.set_content("Job feed · Fri Nov 6\napplied 412\n")
    return msg


@dataclass
class Stored:
    uid: int
    gm_id: int
    labels: tuple[str, ...]
    raw: bytes


@dataclass
class FakeGmail:
    """Pass `server.connect` as imap_factory. Every message is returned by every search;
    the queries are recorded so tests can check them."""
    password: str = "pw"
    imap_enabled: bool = True
    messages: list[Stored] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    fetches: list[tuple[str, str]] = field(default_factory=list)
    selected: list[tuple[str, bool]] = field(default_factory=list)
    connects: int = 0
    logged_out: int = 0

    def add(self, msg: EmailMessage, labels: tuple[str, ...] = ("\\Sent",)) -> int:
        n = len(self.messages) + 1
        self.messages.append(Stored(uid=n, gm_id=1_800_000_000_000_000_000 + n, labels=labels, raw=msg.as_bytes()))
        return self.messages[-1].gm_id

    def connect(self, host, port, timeout=None):
        assert (host, port) == ("imap.gmail.com", 993)
        self.connects += 1
        return _Conn(self)


class _Conn:
    def __init__(self, server: FakeGmail):
        self.s = server

    def login(self, user, password):
        if not self.s.imap_enabled:
            raise imaplib.IMAP4.error("[ALERT] Your account is not enabled for IMAP use.")
        if password != self.s.password:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        return "OK", [b"me authenticated (Success)"]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                      b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
                      b'(\\All \\HasNoChildren) "/" "[Gmail]/Alle Nachrichten"',
                      b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Gesendet"']

    def select(self, mailbox, readonly=False):
        self.s.selected.append((mailbox, readonly))
        return "OK", [str(len(self.s.messages)).encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            assert args[0] == "X-GM-RAW"
            self.s.queries.append(args[1])
            return "OK", [" ".join(str(m.uid) for m in self.s.messages).encode()]
        assert command == "FETCH"
        uids, items = args
        self.s.fetches.append((uids, items))
        wanted = [m for m in self.s.messages if str(m.uid) in uids.split(",")]
        if items == "(X-GM-MSGID)":
            return "OK", [f"{i} (X-GM-MSGID {m.gm_id} UID {m.uid})".encode() for i, m in enumerate(wanted, 1)]
        assert "BODY.PEEK[]" in items, "must never fetch with BODY[], which marks mail read"
        data = []
        for i, m in enumerate(wanted, 1):
            # Gmail quotes each label and escapes its backslash: ("\\Inbox" "\\Sent" "Job hunt")
            labels = " ".join('"' + l.replace("\\", "\\\\") + '"' for l in m.labels)
            head = f"{i} (X-GM-MSGID {m.gm_id} X-GM-LABELS ({labels}) UID {m.uid} BODY[] {{{len(m.raw)}}}"
            data += [(head.encode(), m.raw), b")"]
        return "OK", data

    def logout(self):
        self.s.logged_out += 1
        return "BYE", [b"LOGOUT Requested"]
