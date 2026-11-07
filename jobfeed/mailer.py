"""Sending the digest through Gmail SMTP, with credentials from .env."""

from __future__ import annotations

import os
import smtplib
import socket
import ssl
import stat
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Callable, Mapping, Protocol

DEFAULT_ENV = Path(".env")
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
TIMEOUT_SECONDS = 30
ATTEMPTS = 3
REQUIRED = ("GMAIL_USER", "GMAIL_APP_PASSWORD", "DIGEST_TO")


class MailConfigError(Exception):
    """.env is missing or incomplete."""


class SendError(Exception):
    """The message couldn't be sent."""


@dataclass(frozen=True)
class MailSettings:
    user: str
    password: str
    to: tuple[str, ...]
    warning: str | None = None   # e.g. .env readable by other users

    def __repr__(self) -> str:   # never show the password, even in a traceback
        return f"MailSettings(user={self.user!r}, to={self.to!r})"


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; `export`, quotes, blank lines and # comments are fine."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key.strip()] = value
    return values


def load_settings(path: Path = DEFAULT_ENV, environ: Mapping[str, str] | None = None) -> MailSettings:
    """Values from the environment win over .env, so cron or a shell can override one."""
    environ = os.environ if environ is None else environ
    values = read_env_file(path) if path.exists() else {}
    values.update({k: environ[k] for k in REQUIRED if environ.get(k)})
    missing = [k for k in REQUIRED if not values.get(k, "").strip()]
    if missing:
        where = f"{path}" if path.exists() else f"{path} (it doesn't exist; copy .env.example)"
        raise MailConfigError(f"set {', '.join(missing)} in {where}")
    warning = None
    if path.exists() and os.name == "posix" and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        warning = f"{path} is readable by other users; run `chmod 600 {path}`"
    to = tuple(a.strip() for a in values["DIGEST_TO"].split(",") if a.strip())
    # Google shows app passwords in groups of four; the spaces aren't part of it.
    return MailSettings(user=values["GMAIL_USER"].strip(), password="".join(values["GMAIL_APP_PASSWORD"].split()),
                        to=to, warning=warning)


def build_message(settings: MailSettings, subject: str, text: str, html: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"jobfeed <{settings.user}>"
    msg["To"] = ", ".join(settings.to)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=settings.user.rpartition("@")[2] or None)
    msg["X-Mailer"] = "jobfeed"
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


class Mailer(Protocol):
    def send(self, msg: EmailMessage) -> None: ...


class GmailMailer:
    """STARTTLS on smtp.gmail.com:587; a dropped connection or timeout is retried, a bad login isn't."""

    def __init__(self, settings: MailSettings, *, smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP,
                 sleep: Callable[[float], None] = time.sleep):
        self.settings = settings
        self.smtp_factory = smtp_factory
        self.sleep = sleep

    def send(self, msg: EmailMessage) -> None:
        last: Exception | None = None
        for attempt in range(ATTEMPTS):
            try:
                with self.smtp_factory(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT_SECONDS) as smtp:
                    smtp.ehlo()
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                    smtp.login(self.settings.user, self.settings.password)
                    smtp.send_message(msg)
                return
            except smtplib.SMTPAuthenticationError as exc:
                raise SendError(
                    f"Gmail rejected the login for {self.settings.user} ({exc.smtp_code}). GMAIL_APP_PASSWORD "
                    "must be an app password (Google Account → Security → App passwords, which needs "
                    "2-Step Verification), not your normal password."
                ) from exc
            except smtplib.SMTPRecipientsRefused as exc:
                raise SendError(f"Gmail refused the recipient(s) {', '.join(exc.recipients)}; check DIGEST_TO") from exc
            # SMTPException is an OSError subclass, so the order of these matters.
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError) as exc:
                last = exc
            except smtplib.SMTPException as exc:
                code = getattr(exc, "smtp_code", None)
                if isinstance(code, int) and 400 <= code < 500:
                    last = exc  # temporary failure: retry
                else:
                    raise SendError(f"Gmail refused the message: {exc}") from exc
            except (socket.timeout, ConnectionError, OSError) as exc:
                last = exc
            if attempt < ATTEMPTS - 1:
                self.sleep(5 * (attempt + 1))
        raise SendError(f"couldn't reach {SMTP_HOST} after {ATTEMPTS} tries: {type(last).__name__}: {last}")
