from __future__ import annotations

import io
import os
import smtplib
from datetime import date, datetime, timedelta, timezone
from email import message_from_bytes, policy
from pathlib import Path

import httpx
import pytest

from jobfeed.cli import main
from jobfeed.config import Config
from jobfeed.dedupe import plan_run
from jobfeed.digest import Health, build_health, render_html, render_text, subject
from jobfeed.mailer import (GmailMailer, MailConfigError, MailSettings, SendError, build_message, load_settings,
                            read_env_file)
from jobfeed.models import Job
from jobfeed.normalize import normalize_job
from jobfeed.sources.base import Board, FetchResult
from tests.conftest import router

NOW = datetime(2026, 11, 6, 19, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 6)


def job(id_, title="Senior Backend Engineer", company="Acme", **kw) -> Job:
    base = dict(id=id_, source="greenhouse", company=company, title=title, url=f"https://example.com/{id_}",
                location_raw="Remote (US)", description="Base salary: $170,000 - $210,000",
                posted_at=datetime(2026, 11, 1, tzinfo=timezone.utc))
    return normalize_job(Job(**{**base, **kw}))


def _plan(jobs, watched=frozenset(), cap=50):
    cfg = Config()
    cfg.digest.max_per_section = cap
    return plan_run(jobs, cfg, watched=set(watched), store=None, now=NOW)


# --- subject and bodies --------------------------------------------------------------

def test_subject_matches_the_spec():
    plan = _plan([job("greenhouse:1"), job("greenhouse:2", title="Staff Engineer", company="Watched")],
                 watched={"watched"})
    assert subject(plan.digest, TODAY) == "Job feed · Fri Nov 6 · 2 new (1 watchlist)"
    assert subject(_plan([]).digest, TODAY) == "Job feed · Fri Nov 6 · 0 new (0 watchlist)"


def test_text_body_has_sections_entries_and_footer():
    plan = _plan([job("greenhouse:1"), job("greenhouse:2", title="Staff Engineer", description="")])
    health = Health(boards=3, postings_by_source={"greenhouse": 40, "remotive": 12},
                    aggregators_used=["remotive"], dropped={"role": 30})
    text = render_text(plan.digest, TODAY, health=health)
    assert text.splitlines()[0] == "Job feed · Fri Nov 6 · 2 new (0 watchlist)"
    assert "== New matches (1)" in text and "== No salary listed (1)" in text
    assert "  Senior Backend Engineer — Acme · Remote (US) · $170K–$210K · Senior" in text
    assert "remote (us) · via Greenhouse · posted 2026-11-01" in text
    assert "Fetched 52 postings from 3 boards: Greenhouse 40 · Remotive 12." in text
    assert "Skipped: 30 dropped by the rules (role 30)." in text
    assert "Remote listings via Remotive." in text


def test_empty_day_still_renders():
    text = render_text(_plan([]).digest, TODAY, health=Health(boards=5))
    assert "Nothing new matched." in text
    html = render_html(_plan([]).digest, TODAY, health=Health(boards=5))
    assert "Nothing new matched today." in html


def test_html_escapes_and_links():
    plan = _plan([job("greenhouse:<1>", title="Senior <Backend> Engineer & SRE", company="A&B \"Co\"")])
    html = render_html(plan.digest, TODAY)
    assert "Senior &lt;Backend&gt; Engineer &amp; SRE" in html
    assert "A&amp;B &quot;Co&quot;" in html
    assert 'href="https://example.com/greenhouse:&lt;1&gt;"' in html
    assert "<Backend>" not in html
    assert html.startswith("<!doctype html>") and "</html>" in html


def test_overflow_is_stated():
    jobs = [job(f"greenhouse:{n}", title=f"Senior Backend Engineer {n}") for n in range(4)]
    plan = _plan(jobs, cap=3)
    text = render_text(plan.digest, TODAY, overflow_hint="; they lead the next digest")
    assert "== New matches (4)" in text
    assert "… 1 more not shown; they lead the next digest" in text
    assert "1 more not shown; they lead tomorrow" in render_html(plan.digest, TODAY)


def test_health_flags_failures_and_boards_gone_empty():
    results = [
        FetchResult(Board("greenhouse", "stripe"), jobs=[job("greenhouse:1")]),
        FetchResult(Board("greenhouse", "moved"), jobs=[]),
        FetchResult(Board("lever", "quiet"), jobs=[]),
        FetchResult(Board("ashby", "down"), error="HTTP 503"),
        FetchResult(Board("remoteok", "all"), jobs=[]),
    ]
    plan = _plan([job("greenhouse:1")])
    health = build_health(results, plan, previous={"greenhouse:moved": 12, "lever:quiet": 0})
    assert health.failed == [("ashby:down", "HTTP 503")]
    assert health.went_empty == [("greenhouse:moved", 12)]
    lines = "\n".join(health.lines())
    assert "FAILED 1: ashby:down (HTTP 503)." in lines
    assert "Now empty: greenhouse:moved (had 12)." in lines
    assert "2 boards had no open postings." in lines
    assert "Remote listings via Remote OK." in lines


# --- .env and settings -------------------------------------------------------------

def _env(tmp_path, text, mode=0o600) -> Path:
    path = tmp_path / ".env"
    path.write_text(text)
    path.chmod(mode)
    return path


def test_read_env_file(tmp_path):
    path = _env(tmp_path, "# Gmail\nexport GMAIL_USER=me@gmail.com\nGMAIL_APP_PASSWORD='abcd efgh ijkl mnop'\n"
                          "DIGEST_TO=me@gmail.com, other@example.com  # two of us\n\nNOT A LINE\n")
    assert read_env_file(path) == {"GMAIL_USER": "me@gmail.com", "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop",
                                   "DIGEST_TO": "me@gmail.com, other@example.com"}


def test_load_settings(tmp_path):
    path = _env(tmp_path, "GMAIL_USER=me@gmail.com\nGMAIL_APP_PASSWORD=abcd efgh ijkl mnop\n"
                          "DIGEST_TO=me@gmail.com, other@example.com\n")
    s = load_settings(path, environ={})
    assert (s.user, s.password, s.to, s.warning) == ("me@gmail.com", "abcdefghijklmnop",
                                                     ("me@gmail.com", "other@example.com"), None)
    assert "abcd" not in repr(s)


def test_environment_overrides_the_file(tmp_path):
    path = _env(tmp_path, "GMAIL_USER=me@gmail.com\nGMAIL_APP_PASSWORD=x\nDIGEST_TO=me@gmail.com\n")
    assert load_settings(path, environ={"DIGEST_TO": "cron@example.com"}).to == ("cron@example.com",)


def test_missing_settings_are_named(tmp_path):
    path = _env(tmp_path, "GMAIL_USER=me@gmail.com\n")
    with pytest.raises(MailConfigError, match="GMAIL_APP_PASSWORD, DIGEST_TO"):
        load_settings(path, environ={})
    with pytest.raises(MailConfigError, match="doesn't exist"):
        load_settings(tmp_path / "nope.env", environ={})


@pytest.mark.skipif(os.name != "posix", reason="file modes")
def test_readable_env_file_warns(tmp_path):
    path = _env(tmp_path, "GMAIL_USER=a@b.c\nGMAIL_APP_PASSWORD=x\nDIGEST_TO=a@b.c\n", mode=0o644)
    assert "chmod 600" in load_settings(path, environ={}).warning


def test_build_message_has_text_and_html():
    settings = MailSettings(user="me@gmail.com", password="pw", to=("me@gmail.com", "o@example.com"))
    msg = build_message(settings, "Job feed · Fri Nov 6 · 1 new (0 watchlist)", "plain", "<p>html</p>")
    parsed = message_from_bytes(msg.as_bytes(), policy=policy.default)
    assert parsed["From"] == "jobfeed <me@gmail.com>"
    assert parsed["To"] == "me@gmail.com, o@example.com"
    assert parsed["Subject"] == "Job feed · Fri Nov 6 · 1 new (0 watchlist)"
    assert [p.get_content_type() for p in parsed.iter_parts()] == ["text/plain", "text/html"]
    assert parsed.get_body(("plain",)).get_content().strip() == "plain"


# --- SMTP --------------------------------------------------------------------------

class FakeSMTP:
    """Records the SMTP conversation; `script` decides what each connection does."""

    log: list = []
    script: list = []

    def __init__(self, host, port, timeout):
        self.log.append(("connect", host, port, timeout))
        self.behaviour = self.script.pop(0) if self.script else "ok"
        if self.behaviour == "refuse-connect":
            raise ConnectionRefusedError("refused")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        self.log.append(("ehlo",))

    def starttls(self, context):
        self.log.append(("starttls",))

    def login(self, user, password):
        self.log.append(("login", user, password))
        if self.behaviour == "bad-login":
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    def send_message(self, msg):
        if self.behaviour == "disconnect":
            raise smtplib.SMTPServerDisconnected("gone")
        if self.behaviour == "reject":
            raise smtplib.SMTPDataError(552, b"message too big")
        self.log.append(("send", msg["Subject"]))


@pytest.fixture
def fake_smtp():
    FakeSMTP.log, FakeSMTP.script = [], []
    return FakeSMTP


SETTINGS = MailSettings(user="me@gmail.com", password="apppassword", to=("me@gmail.com",))


def _mailer(fake, sleeps=None):
    return GmailMailer(SETTINGS, smtp_factory=fake, sleep=(sleeps.append if sleeps is not None else lambda s: None))


def test_gmail_conversation(fake_smtp):
    _mailer(fake_smtp).send(build_message(SETTINGS, "hello", "body"))
    assert fake_smtp.log == [("connect", "smtp.gmail.com", 587, 30), ("ehlo",), ("starttls",), ("ehlo",),
                             ("login", "me@gmail.com", "apppassword"), ("send", "hello")]


def test_transient_failures_are_retried(fake_smtp):
    fake_smtp.script = ["refuse-connect", "disconnect", "ok"]
    sleeps = []
    _mailer(fake_smtp, sleeps).send(build_message(SETTINGS, "hello", "body"))
    assert ("send", "hello") in fake_smtp.log
    assert sleeps == [5, 10]


def test_gives_up_after_three_tries(fake_smtp):
    fake_smtp.script = ["disconnect"] * 3
    with pytest.raises(SendError, match="after 3 tries"):
        _mailer(fake_smtp).send(build_message(SETTINGS, "hello", "body"))


def test_bad_login_explains_app_passwords_and_isnt_retried(fake_smtp):
    fake_smtp.script = ["bad-login", "ok"]
    with pytest.raises(SendError, match="app password"):
        _mailer(fake_smtp).send(build_message(SETTINGS, "hello", "body"))
    assert sum(1 for e in fake_smtp.log if e[0] == "connect") == 1


def test_permanent_rejection_isnt_retried(fake_smtp):
    fake_smtp.script = ["reject", "ok"]
    with pytest.raises(SendError, match="refused the message"):
        _mailer(fake_smtp).send(build_message(SETTINGS, "hello", "body"))
    assert sum(1 for e in fake_smtp.log if e[0] == "connect") == 1


# --- jobfeed run, with email ------------------------------------------------------------

class CapturingMailer:
    def __init__(self, fail: bool = False):
        self.sent = []
        self.fail = fail

    def send(self, msg):
        if self.fail:
            raise SendError("Gmail is down")
        self.sent.append(msg)


@pytest.fixture
def setup(tmp_path, greenhouse_jobs):
    env = _env(tmp_path, "GMAIL_USER=me@gmail.com\nGMAIL_APP_PASSWORD=pw\nDIGEST_TO=me@gmail.com\n")
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies: []\n")
    argv = ["--env", str(env), "--companies", str(companies), "--db", str(tmp_path / "jobs.db"),
            "--board", "greenhouse:examplecorp"]
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=greenhouse_jobs)

    transport = router({"/v1/boards/examplecorp/jobs": handle})
    return argv, transport, requests, tmp_path


def _run(argv, transport, mailer, now=NOW):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, transport=transport, out=out, err=err, now=now, mailer=mailer)
    return code, out.getvalue(), err.getvalue()


def test_run_emails_the_digest_and_marks_it_sent(setup):
    argv, transport, _, _ = setup
    mailer = CapturingMailer()
    code, out, err = _run(["run", *argv], transport, mailer)
    assert code == 0
    assert out == ""                                            # the email is the output now
    assert 'emailed "Job feed · Fri Nov 6 · 2 new (0 watchlist)" to me@gmail.com' in err
    assert "marked 2 as sent" in err
    [msg] = mailer.sent
    text = msg.get_body(("plain",)).get_content()
    html = msg.get_body(("html",)).get_content()
    assert "Senior Backend Engineer, Platform — Example Corp" in text and "Fetched 3 postings from 1 boards" in text
    assert "Senior Backend Engineer, Platform</a>" in html

    # the next day: an email still goes out, with nothing new in it
    code, _, err = _run(["run", *argv], transport, mailer, now=NOW + timedelta(days=1))
    assert code == 0 and len(mailer.sent) == 2
    assert mailer.sent[1]["Subject"] == "Job feed · Sat Nov 7 · 0 new (0 watchlist)"
    assert "Skipped: 2 already sent; 1 dropped by the rules (role 1)." in mailer.sent[1].get_body(("plain",)).get_content()


def test_failed_send_marks_nothing_and_the_next_run_resends(setup):
    argv, transport, _, _ = setup
    code, _, err = _run(["run", *argv], transport, CapturingMailer(fail=True))
    assert code == 1
    assert "EMAIL FAILED: Gmail is down" in err and "marked 0 as sent" in err
    mailer = CapturingMailer()
    _run(["run", *argv], transport, mailer, now=NOW + timedelta(hours=1))
    assert mailer.sent[0]["Subject"] == "Job feed · Fri Nov 6 · 2 new (0 watchlist)"


def test_missing_settings_stop_the_run_before_fetching(setup, tmp_path):
    argv, transport, requests, _ = setup
    argv = [a if a != str(tmp_path / ".env") else str(tmp_path / "missing.env") for a in argv]
    code, _, err = _run(["run", *argv], transport, CapturingMailer())
    assert code == 2
    assert "email settings: set GMAIL_USER" in err and "--no-email" in err
    assert requests == []


def test_dry_run_and_no_email_need_no_settings(setup, tmp_path):
    argv, transport, _, _ = setup
    argv = [a if a != str(tmp_path / ".env") else str(tmp_path / "missing.env") for a in argv]
    mailer = CapturingMailer()
    code, out, _ = _run(["run", "--dry-run", *argv], transport, mailer)
    assert code == 0 and "2 new" in out
    code, out, err = _run(["run", "--no-email", *argv], transport, mailer)
    assert code == 0 and "2 new" in out and "marked 2 as sent" in err
    assert mailer.sent == []


def test_html_preview_file(setup):
    argv, transport, _, tmp_path = setup
    preview = tmp_path / "preview.html"
    _run(["run", "--dry-run", "--html", str(preview), *argv], transport, CapturingMailer())
    html = preview.read_text()
    assert html.startswith("<!doctype html>") and "Staff Infrastructure Engineer" in html


def test_footer_notices_a_board_that_went_empty(setup, greenhouse_jobs):
    argv, transport, _, _ = setup
    _run(["run", *argv], transport, CapturingMailer())
    empty = router({"/v1/boards/examplecorp/jobs": lambda r: httpx.Response(200, json={"jobs": []})})
    mailer = CapturingMailer()
    _, _, err = _run(["run", *argv], empty, mailer, now=NOW + timedelta(days=1))
    assert "NOW EMPTY greenhouse:examplecorp: had 3 postings last run" in err
    assert "Now empty: greenhouse:examplecorp (had 3)." in mailer.sent[0].get_body(("plain",)).get_content()


def test_send_test(setup):
    argv, _, _, tmp_path = setup
    mailer = CapturingMailer()
    code, _, err = _run(["send-test", "--env", str(tmp_path / ".env")], None, mailer)
    assert code == 0 and "sent a test email to me@gmail.com" in err
    assert mailer.sent[0]["Subject"] == "jobfeed test email"
    code, _, err = _run(["send-test", "--env", str(tmp_path / ".env")], None, CapturingMailer(fail=True))
    assert code == 1 and "EMAIL FAILED" in err
