"""Reading Gmail over IMAP, read-only.

The mailbox is opened with EXAMINE (read-only) and messages are fetched with
BODY.PEEK, so nothing is marked read, moved or deleted. Gmail runs the search
(X-GM-RAW, the same syntax as the Gmail search box) on its side, over All Mail,
so only matching messages are ever downloaded.
"""

from __future__ import annotations

import imaplib
import re
import socket
import ssl
from dataclasses import dataclass
from typing import Callable, Iterable

from jobfeed.mailer import MailSettings

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
TIMEOUT_SECONDS = 30
FALLBACK_ALL_MAIL = "[Gmail]/All Mail"


class ReadError(Exception):
    """Gmail couldn't be read; the message says what to do about it."""


@dataclass(frozen=True)
class RawMail:
    gm_id: str                 # Gmail's message ID (X-GM-MSGID), the same in every label
    labels: tuple[str, ...]    # e.g. ("\\Sent", "\\Inbox")
    raw: bytes                 # the whole message, RFC 822


def imap_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


_MSGID = re.compile(rb"X-GM-MSGID (\d+)")
_UID = re.compile(rb"UID (\d+)")
_LABELS = re.compile(rb"X-GM-LABELS \(([^)]*)\)")
_LABEL = re.compile(rb'"((?:[^"\\]|\\.)*)"|(\S+)')


def _labels(head: bytes) -> tuple[str, ...]:
    m = _LABELS.search(head)
    if not m:
        return ()
    out = []
    for quoted, bare in _LABEL.findall(m.group(1)):
        value = quoted.replace(b'\\"', b'"').replace(b"\\\\", b"\\") if quoted else bare
        out.append(value.decode("utf-8", "replace"))
    return tuple(out)


def _friendly(exc: Exception) -> str:
    text = str(exc)
    if "not enabled for IMAP" in text or "IMAP access is disabled" in text:
        return ("IMAP is turned off for this Gmail account; turn it on in Gmail → Settings → See all settings → "
                "Forwarding and POP/IMAP")
    if "AUTHENTICATIONFAILED" in text or "Invalid credentials" in text or "Application-specific password" in text:
        return ("Gmail rejected the login; GMAIL_APP_PASSWORD must be an app password, and IMAP must be on "
                "(Gmail → Settings → Forwarding and POP/IMAP)")
    return f"{type(exc).__name__}: {text}"


class GmailReader:
    """with GmailReader(settings) as reader: reader.find('from:me subject:"Job feed"', skip=...)"""

    def __init__(self, settings: MailSettings, *,
                 imap_factory: Callable[..., imaplib.IMAP4] | None = None):
        self.settings = settings
        self.imap_factory = imap_factory or imaplib.IMAP4_SSL
        self.conn: imaplib.IMAP4 | None = None

    def __enter__(self) -> GmailReader:
        try:
            self.conn = self.imap_factory(IMAP_HOST, IMAP_PORT, timeout=TIMEOUT_SECONDS)
            self.conn.login(self.settings.user, self.settings.password)
            typ, data = self.conn.select(imap_quote(self._all_mail()), readonly=True)
            if typ != "OK":
                raise ReadError(f"couldn't open All Mail: {data!r}")
        except ReadError:
            self.close()
            raise
        except (imaplib.IMAP4.error, OSError, socket.timeout, ssl.SSLError) as exc:
            self.close()
            raise ReadError(_friendly(exc)) from exc
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:   # already broken; nothing to clean up
                pass
            self.conn = None

    def _all_mail(self) -> str:
        """All Mail's name, which depends on the account's language: the folder flagged \\All."""
        assert self.conn is not None
        typ, data = self.conn.list()
        for line in data or []:
            if isinstance(line, bytes) and b"\\All" in line.split(b")", 1)[0]:
                name = line.rsplit(b' "/" ', 1)[-1].strip()
                return name.strip(b'"').decode("utf-8", "replace")
        return FALLBACK_ALL_MAIL

    def find(self, query: str, skip: Iterable[str] = ()) -> list[RawMail]:
        """Messages matching a Gmail search, except those whose Gmail ID is in `skip`."""
        assert self.conn is not None, "use GmailReader as a context manager"
        skip = set(skip)
        try:
            typ, data = self.conn.uid("SEARCH", "X-GM-RAW", imap_quote(query))
            if typ != "OK":
                raise ReadError(f"Gmail refused the search: {data!r}")
            uids = (data[0] or b"").split()
            if not uids:
                return []
            typ, data = self.conn.uid("FETCH", b",".join(uids).decode(), "(X-GM-MSGID)")
            wanted = []
            for item in data or []:
                head = item[0] if isinstance(item, tuple) else item
                if not isinstance(head, bytes):
                    continue
                gm, uid = _MSGID.search(head), _UID.search(head)
                if gm and uid and gm.group(1).decode() not in skip:
                    wanted.append(uid.group(1))
            if not wanted:
                return []
            typ, data = self.conn.uid("FETCH", b",".join(wanted).decode(), "(X-GM-MSGID X-GM-LABELS BODY.PEEK[])")
            mails = []
            for item in data or []:
                if isinstance(item, tuple) and len(item) == 2:
                    gm = _MSGID.search(item[0])
                    if gm:
                        mails.append(RawMail(gm_id=gm.group(1).decode(), labels=_labels(item[0]), raw=item[1]))
            return mails
        except (imaplib.IMAP4.error, OSError, socket.timeout, ssl.SSLError) as exc:
            raise ReadError(_friendly(exc)) from exc
