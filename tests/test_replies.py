from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from jobfeed import tracker
from jobfeed.cli import main
from jobfeed.imap import GmailReader, ReadError, _labels, imap_quote
from jobfeed.mailbox import query
from jobfeed.mailer import MailSettings
from jobfeed.replies import ParseError, command_lines, is_chatter, parse
from jobfeed.store import Store
from tests.conftest import router
from tests.fake_gmail import ME, FakeGmail, digest_copy, reply

NOW = datetime(2026, 11, 6, 19, 0, tzinfo=timezone.utc)   # Fri Nov 6, 11am in Seattle
EVENING = NOW + timedelta(hours=8)                         # Fri Nov 6, 7pm in Seattle
SETTINGS = MailSettings(user=ME, password="pw", to=(ME,))


# --- reading command lines out of a reply -------------------------------------------------

def test_command_lines_stop_at_the_quoted_digest():
    body = ("applied 412 418\n\nscreen 412\n\nSent from my iPhone\n\n"
            "On Fri, Nov 6, 2026 at 6:00 AM jobfeed <me@gmail.com>\nwrote:\n> Job feed · Fri Nov 6\n> applied 999\n")
    assert command_lines(body) == ["applied 412 418", "screen 412"]


@pytest.mark.parametrize("quote", [
    "On Fri, Nov 6, 2026 at 6:00 AM jobfeed <me@gmail.com> wrote:",
    "> applied 999",
    "-- \nWilly",
    "________________________________\nFrom: jobfeed <me@gmail.com>",
    "From: jobfeed <me@gmail.com>\nSent: Friday, November 6, 2026 6:00 AM",
    "-----Original Message-----",
    "Job feed · Fri Nov 6 · 2 new (0 watchlist)",
])
def test_command_lines_know_each_clients_quote_style(quote):
    assert command_lines(f"note 412 call Tue\n{quote}\napplied 999\n") == ["note 412 call Tue"]


def test_html_only_replies_are_read():
    msg = reply("applied 412<br>screen 412", sent=EVENING, html=True)
    from jobfeed.replies import body_text
    assert command_lines(body_text(msg)) == ["applied 412", "screen 412"]


# --- parsing a line ----------------------------------------------------------------------------

def test_parse_applied_with_options():
    cmd = parse("Applied #412, 418 resume v2-short referral cover letter on 2026-11-05 note via Sam")
    assert (cmd.verb, cmd.codes, cmd.resume, cmd.referral, cmd.cover_letter, cmd.day, cmd.note) == (
        "apply", [412, 418], "v2-short", True, True, "2026-11-05", "via Sam")
    assert parse("applied 412 no cover letter yesterday").cover_letter is False
    assert parse("applied 412 no cover letter yesterday").day == "yesterday"


@pytest.mark.parametrize("line, kind, day, note", [
    ("screen 412", "screen", None, None),
    ("phone screen 412", "screen", None, None),
    ("Onsite 412 with the platform team", "interview", None, "with the platform team"),
    ("rejected 412 on 2026-11-05", "rejected", "2026-11-05", None),
    ("withdrew 412 yesterday took another offer", "withdrawn", "yesterday", "took another offer"),
    ("no response 412", "no_response", None, None),
    ("replied 412", "reply", None, None),
    ("reopen 412", "reopened", None, None),
])
def test_parse_marks(line, kind, day, note):
    cmd = parse(line)
    assert (cmd.verb, cmd.codes, cmd.kind, cmd.day, cmd.note) == ("mark", [412], kind, day, note)


@pytest.mark.parametrize("line, message", [
    ("dance 412", "isn't a command"),
    ("screen", "no code"),
    ("rejected 4l8", "no code"),
    ("note 412", "the note is empty"),
    ("applied 412 pronto", "didn't understand 'pronto'"),
])
def test_parse_errors(line, message):
    with pytest.raises(ParseError, match=message):
        parse(line)


def test_chatter_is_not_quoted_back():
    assert is_chatter("Thanks!") and is_chatter("Willy") and is_chatter("Cheers, W")
    assert not is_chatter("screen") and not is_chatter("scren 412") and not is_chatter("rejected four-twelve")


# --- the IMAP client --------------------------------------------------------------------------

def test_reader_opens_all_mail_read_only_and_skips_seen_messages():
    gmail = FakeGmail()
    first = gmail.add(reply("applied 412", sent=EVENING))
    gmail.add(reply("screen 412", sent=EVENING), labels=("\\Inbox", "\\Sent", "Job hunt"))
    with GmailReader(SETTINGS, imap_factory=gmail.connect) as reader:
        mails = reader.find('from:(me@gmail.com) subject:"Job feed"', skip={str(first)})
    assert gmail.selected == [('"[Gmail]/Alle Nachrichten"', True)]       # found by its \All flag, any language
    assert gmail.queries == ['"from:(me@gmail.com) subject:\\"Job feed\\""']
    assert [m.labels for m in mails] == [("\\Inbox", "\\Sent", "Job hunt")]
    assert b"screen 412" in mails[0].raw
    assert gmail.fetches[-1] == ("2", "(X-GM-MSGID X-GM-LABELS BODY.PEEK[])")   # only the unseen one
    assert gmail.logged_out == 1


def test_label_parsing():
    assert _labels(b'1 (X-GM-MSGID 5 X-GM-LABELS ("\\\\Inbox" "\\\\Sent" "Job \\"hunt\\"") UID 2') == (
        "\\Inbox", "\\Sent", 'Job "hunt"')
    assert _labels(b"1 (X-GM-MSGID 5 X-GM-LABELS (\\Sent Work) UID 2") == ("\\Sent", "Work")
    assert _labels(b"1 (X-GM-MSGID 5 X-GM-LABELS () UID 2") == ()
    assert imap_quote('a "b" \\c') == '"a \\"b\\" \\\\c"'


@pytest.mark.parametrize("server, message", [
    (FakeGmail(password="other"), "app password"),
    (FakeGmail(imap_enabled=False), "IMAP is turned off"),
])
def test_reader_explains_login_problems(server, message):
    with pytest.raises(ReadError, match=message):
        with GmailReader(SETTINGS, imap_factory=server.connect):
            pass
    assert server.logged_out == 1


def test_unreachable_gmail_is_a_read_error():
    with pytest.raises(ReadError, match="ConnectionRefusedError"):
        with GmailReader(SETTINGS):           # conftest refuses real connections
            pass


def test_query():
    assert query({"me@gmail.com", "work@example.com"}, NOW) == (
        'from:(me@gmail.com OR work@example.com) subject:"Job feed" after:2026/11/06')


# --- replies through `jobfeed run` and `jobfeed mail` -----------------------------------------------

@pytest.fixture
def pi(tmp_path, greenhouse_jobs):
    """A first digest (#101, #102) and a Gmail account to reply from."""
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies: []\n")
    env = tmp_path / ".env"
    env.write_text(f"GMAIL_USER={ME}\nGMAIL_APP_PASSWORD=pw\nDIGEST_TO={ME}\n")
    env.chmod(0o600)
    transport = router({"/v1/boards/examplecorp/jobs": lambda r: httpx.Response(200, json=greenhouse_jobs)})
    gmail = FakeGmail()

    class Pi:
        db = tmp_path / "jobs.db"
        cfg = tmp_path / "config.yaml"
        args = ["--db", str(tmp_path / "jobs.db"), "--env", str(env)]

        def main(self, argv, day=0, hours=0):
            out, err = io.StringIO(), io.StringIO()
            code = main(argv, transport=transport, out=out, err=err, now=NOW + timedelta(days=day, hours=hours),
                        imap_factory=gmail.connect)
            return code, out.getvalue(), err.getvalue()

        def run(self, day, *extra):
            return self.main(["run", "--no-email", *extra, "--companies", str(companies), *self.args,
                              "--board", "greenhouse:examplecorp"], day)

        def mail(self, day, *extra):
            return self.main(["mail", *extra, *self.args], day)

        def app(self, code):
            with Store.open(self.db) as store:
                return tracker.get_application(store, code, with_events=True)

    p = Pi()
    p.gmail = gmail
    code, _, err = p.run(0)
    assert code == 0 and "Gmail: no new replies." in err
    return p


def test_a_reply_logs_applications_and_the_next_digest_confirms_it(pi):
    pi.gmail.add(digest_copy(NOW))                      # the digest itself is never read as a reply
    pi.gmail.add(reply("applied 101 resume v2\napplied 102 yesterday referral\nscreen 101 recruiter call Tue\n"
                       "Thanks!\nrejected 4l8", sent=EVENING))
    code, out, err = pi.run(1)
    assert code == 0
    assert "Gmail: 1 reply, 3 lines run, 1 not used (see Your applications)." in err
    block = out.split("== Your applications\n", 1)[1].split("\n\n", 1)[0]
    assert block.splitlines() == [
        "  Logged since the last digest",
        "    #102 Example Corp — Staff Infrastructure Engineer · applied Nov 5 · resume v1 · referral · from your reply",
        "    #101 Example Corp — Senior Backend Engineer, Platform · applied Nov 6 · resume v2 · from your reply",
        "  Not done from your replies",
        "    “rejected 4l8”: no code; put the digest number right after the command, like `screen 412`",
        "  Updates",
        "    #101 Example Corp — Senior Backend Engineer, Platform · screen Nov 6 (from your reply)",
    ]
    app = pi.app(101)
    assert (app.applied_at, app.logged_via, app.resume_version, app.stage) == (EVENING, "reply", "v2", "screen")
    assert [(e.kind, e.origin, e.detail) for e in app.events] == [
        ("applied", "reply", None), ("screen", "reply", None), ("note", "reply", "recruiter call Tue")]
    noon_nov5 = datetime(2026, 11, 5, 12).astimezone().astimezone(timezone.utc)
    assert pi.app(102).applied_at == noon_nov5

    # read once: the next run finds nothing new, and searches from a day before the last check
    code, out, err = pi.run(2)
    assert "Gmail: no new replies." in err and "Your applications" not in out
    assert pi.gmail.queries[-1].endswith('after:2026/11/06"')


def test_failed_lines_are_reported_per_code(pi):
    pi.gmail.add(reply("applied 101 999", sent=EVENING))
    _, out, _ = pi.run(1)
    assert "“applied 101 999”: no posting #999;" in out
    assert pi.app(101) is not None


def test_replies_from_strangers_or_unverified_are_ignored(pi):
    pi.gmail.add(reply("applied 101", sent=EVENING, sender="someone@else.com"))
    pi.gmail.add(reply("applied 101", sent=EVENING), labels=("\\Inbox",))                     # forged From: me
    pi.gmail.add(reply("applied 101", sent=EVENING, in_reply_to=None, subject="Job feed ideas"))   # not a reply
    _, out, err = pi.run(1)
    assert pi.app(101) is None
    assert "ignored 1 that Gmail couldn't verify came from you" in err
    assert "Ignored a reply from me@gmail.com" in out


def test_a_verified_reply_from_another_address_counts(pi, tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"GMAIL_USER={ME}\nGMAIL_APP_PASSWORD=pw\nDIGEST_TO=work@example.com\n")
    pi.gmail.add(reply("applied 101", sent=EVENING, sender="work@example.com",
                       auth="mx.google.com; dkim=pass header.i=@example.com; spf=pass; dmarc=pass (p=REJECT) "
                            "header.from=example.com"), labels=("\\Inbox",))
    pi.run(1)
    assert pi.app(101).logged_via == "reply"


def test_mail_command_and_its_dry_run(pi):
    pi.gmail.add(reply("applied 101\nscreen 102", sent=EVENING))
    before = pi.db.read_bytes()
    code, out, err = pi.mail(1, "--dry-run")
    assert code == 0
    assert out.splitlines() == [
        "Reply of Nov 6, 19:00 (“Re: Job feed · Fri Nov 6 · 2 new (0 watchlist)”)",
        "  ok   applied 101  →  logged #101 Example Corp — Senior Backend Engineer, Platform",
        "  NOT  screen 102  →  no application #102; `jobfeed apps --all` lists them",
    ]
    assert err.strip().endswith("Dry run: nothing was saved.")
    assert pi.db.read_bytes() == before
    code, out, _ = pi.mail(1)
    assert code == 0 and pi.app(101) is not None
    _, out, _ = pi.mail(1)
    assert out.startswith("No new replies since")
    pi.mail(1, "--days", "30")
    assert pi.gmail.queries[-1].endswith('after:2026/10/08"')


def test_mail_command_errors(pi, tmp_path):
    assert pi.main(["mail", "--db", str(pi.db), "--env", str(tmp_path / "missing.env")])[0] == 2
    pi.gmail.imap_enabled = False
    code, _, err = pi.mail(1)
    assert code == 1 and "IMAP is turned off" in err


def test_gmail_trouble_never_stops_the_digest(pi):
    pi.gmail.imap_enabled = False
    code, out, err = pi.run(1)
    assert code == 0
    assert "Couldn't read Gmail: IMAP is turned off" in err


def test_no_email_runs_without_gmail_settings_say_so(pi, tmp_path):
    (tmp_path / ".env").unlink()
    code, out, err = pi.run(1)
    assert code == 0 and "Gmail not read: no Gmail settings" in err


def test_reading_can_be_turned_off_and_dry_runs_never_read(pi):
    pi.cfg.write_text("tracking:\n  read_mail: false\n")
    connects = pi.gmail.connects
    _, _, err = pi.run(1, "--config", str(pi.cfg))
    pi.main(["run", "--dry-run", "--companies", str(pi.cfg.parent / "companies.yaml"), *pi.args,
             "--board", "greenhouse:examplecorp"], 1)
    assert pi.gmail.connects == connects and "Gmail" not in err


def test_emailed_run_reads_replies_with_the_same_settings(pi, tmp_path):
    from tests.test_email import CapturingMailer
    pi.gmail.add(reply("applied 102", sent=EVENING))
    mailer = CapturingMailer()
    out, err = io.StringIO(), io.StringIO()
    code = main(["run", "--companies", str(tmp_path / "companies.yaml"), *pi.args, "--board", "greenhouse:examplecorp"],
                transport=router({"/v1/boards/examplecorp/jobs": lambda r: httpx.Response(200, json={"jobs": []})}),
                out=out, err=err, now=NOW + timedelta(days=1), mailer=mailer, imap_factory=pi.gmail.connect)
    assert code == 0
    text = mailer.sent[0].get_body(("plain",)).get_content()
    assert "#102 Example Corp — Staff Infrastructure Engineer · applied Nov 6 · resume v1 · from your reply" in text
    assert "Gmail: 1 reply, 1 line run." in text
