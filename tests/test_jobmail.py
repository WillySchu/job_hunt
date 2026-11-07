from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from jobfeed import tracker
from jobfeed.classify import classify, read_message
from jobfeed.cli import main
from jobfeed.jobmail import domain_label, domain_matches, mentions, name_words, title_score
from jobfeed.store import Store
from tests.conftest import router
from tests.fake_gmail import ME, FakeGmail, reply
from tests.job_mail_samples import (ashby_offer, ats_unknown, duckduckgo_confirmation, greenhouse_confirmation,
                                    greenhouse_rejection, job_mail, newsletter, recruiter_invite)

NOW = datetime(2026, 11, 6, 19, 0, tzinfo=timezone.utc)   # Fri Nov 6, 11am in Seattle
INBOX = ("\\Inbox",)


def _read(msg, ats=("greenhouse-mail.io", "lever.co", "ashbyhq.com")):
    from jobfeed.classify import is_ats
    jm = read_message(msg)
    return classify(jm, from_ats=is_ats(jm.sender, list(ats)))


# --- the classifier ------------------------------------------------------------------------

@pytest.mark.parametrize("make, kind, why", [
    (greenhouse_confirmation, "confirmation", "Thank you for applying"),
    (greenhouse_rejection, "rejection", "decided to move forward with other"),
    (recruiter_invite, "interview", "your availability"),
    (ashby_offer, "offer", "offer letter"),
    (ats_unknown, None, "no wording it knows"),
    (newsletter, None, "a newsletter or mailing list"),
])
def test_classify_samples(make, kind, why):
    reading = _read(make(NOW))
    assert reading.kind == kind and why in reading.why


@pytest.mark.parametrize("body, kind", [
    ("Unfortunately we will not be moving forward with your application.", "rejection"),
    ("Thank you for applying. We regret to inform you that the position has been filled.", "rejection"),
    ("We have received your application for Backend Engineer.", "confirmation"),
    ("Thanks for your interest in Acme! We'd love to chat. Here is my calendly.com/jane link.", None),  # robot: unclear
    ("We're pleased to extend an offer for the role.", "offer"),
    ("Your application was submitted successfully.", "confirmation"),
    ("Please use the link below to pick a time slot for your technical interview.", "interview"),
])
def test_classify_wording_from_an_ats(body, kind):
    msg = job_mail("Acme <no-reply@hire.lever.co>", "Acme", body, sent=NOW)
    assert _read(msg).kind == kind


def test_a_person_writing_is_a_reply_or_an_invitation():
    assert _read(job_mail("Jane <jane@acme.com>", "Re: your application", "Quick question about your Go "
                          "experience.", sent=NOW)).kind == "reply"
    assert _read(job_mail("Jane <jane@acme.com>", "Hi", "Thanks for applying! I'd love to chat; when would you be "
                          "free?", sent=NOW)).kind == "interview"
    assert _read(job_mail("Acme <noreply@acme.com>", "Hi", "Your order shipped.", sent=NOW)).kind is None


def test_quoted_text_is_not_read():
    msg = job_mail("Jane <jane@acme.com>", "Re: hello", "Sounds good, talk soon.\n\n"
                   "On Thu, Nov 5, 2026 at 9:00 AM Me <me@gmail.com> wrote:\n> we regret to inform you", sent=NOW)
    assert _read(msg).kind == "reply"


def test_name_and_domain_helpers():
    assert name_words("Example Corp") == ["example"] and name_words("The Co") == ["the", "co"]
    assert domain_label("jane@mail.duckduckgo.com") == "duckduckgo" and domain_label("a@acme.co.uk") == "acme"
    assert domain_matches("jobs@duckduckgo.com", "DuckDuckGo")
    assert domain_matches("jane@examplecorp.com", "Example Corp")
    assert domain_matches("t@motive.com", "Motive Analytics") and not domain_matches("t@motor.com", "Motive Analytics")
    assert mentions("Motive Analytics", "Thanks from the motive-analytics team")
    assert not mentions("Orbit", "orbital mechanics")
    assert title_score("Senior Backend Engineer, Platform", "the Senior Backend Engineer role") == 0.75


# --- through `jobfeed run` and `jobfeed mail` ------------------------------------------------------

@pytest.fixture
def pi(tmp_path, greenhouse_jobs):
    """A first digest: #101 Senior Backend Engineer, Platform and #102 Staff Infrastructure Engineer, both
    at Example Corp (greenhouse:examplecorp). Nothing logged yet."""
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies: []\n")
    env = tmp_path / ".env"
    env.write_text(f"GMAIL_USER={ME}\nGMAIL_APP_PASSWORD=pw\nDIGEST_TO={ME}\n")
    env.chmod(0o600)
    transport = router({"/v1/boards/examplecorp/jobs": lambda r: httpx.Response(200, json=greenhouse_jobs)})

    class Pi:
        db = tmp_path / "jobs.db"
        gmail = FakeGmail()
        args = ["--db", str(tmp_path / "jobs.db"), "--env", str(env)]

        def main(self, argv, day=0):
            out, err = io.StringIO(), io.StringIO()
            code = main(argv, transport=transport, out=out, err=err, now=NOW + timedelta(days=day),
                        imap_factory=self.gmail.connect)
            return code, out.getvalue(), err.getvalue()

        def run(self, day):
            code, out, err = self.main(["run", "--no-email", "--companies", str(companies), *self.args,
                                        "--board", "greenhouse:examplecorp"], day)
            assert code == 0, err
            block = out.split("== Your applications\n", 1)[1].split("\n\n", 1)[0] if "Your applications" in out else ""
            return block, err

        def cli(self, day, *argv):
            code, out, err = self.main([*argv, "--db", str(self.db)], day)
            assert code == 0, err
            return out

        def app(self, code):
            with Store.open(self.db) as store:
                return tracker.get_application(store, code, with_events=True)

        def add(self, msg, labels=INBOX):
            return self.gmail.add(msg, labels)

    p = Pi()
    p.run(0)
    return p


def test_a_morning_of_job_mail(pi):
    pi.cli(0, "apply", "102")
    pi.add(greenhouse_confirmation(NOW + timedelta(hours=1)))     # for #101, never logged
    pi.add(newsletter(NOW + timedelta(hours=2)))
    pi.add(recruiter_invite(NOW + timedelta(days=1)))             # about #101
    pi.add(greenhouse_rejection(NOW + timedelta(days=1, hours=2)))   # #102, by its job link
    pi.add(ats_unknown(NOW + timedelta(days=1, hours=3)))
    pi.add(ashby_offer(NOW + timedelta(days=1, hours=4)))         # a company you never applied to
    block, err = pi.run(2)
    assert "Gmail: 5 job emails (1 confirmation, 1 interview, 1 rejection, 2 to check)." in err
    assert block.splitlines() == [
        "  Logged since the last digest",
        "    #102 Example Corp — Staff Infrastructure Engineer · applied Nov 6 · resume v1",
        "    #101 Example Corp — Senior Backend Engineer, Platform · applied Nov 6 · resume v1 · "
        "from its confirmation email (`jobfeed edit 101 --resume V` if you sent another)",
        "  Updates",
        "    #101 Example Corp — Senior Backend Engineer, Platform · confirmation email Nov 6",
        "    #101 Example Corp — Senior Backend Engineer, Platform · screen Nov 7 (from email)",
        "    #102 Example Corp — Staff Infrastructure Engineer · rejected Nov 7 (at applied) (from email)",
        "  Check these (job mail it couldn't place; reply with a command such as `screen 412`, or leave it)",
        "    Example Corp <no-reply@us.greenhouse-mail.io>, “Please complete your profile” (Nov 7): "
        "no wording it knows",
        "    Orbit Hiring <no-reply@ashbyhq.com>, “Your offer from Orbit” (Nov 7): "
        "it doesn't name a company or role you've applied to",
    ]
    app = pi.app(101)
    assert (app.logged_via, app.applied_at, app.stage) == ("confirmation", NOW + timedelta(hours=1), "screen")
    assert [(e.kind, e.origin) for e in app.events] == [("applied", "email"), ("confirmation", "email"),
                                                        ("screen", "email")]
    assert all(e.message_id for e in app.events)
    assert pi.app(102).stage == "rejected"
    # every message is read once
    block, err = pi.run(3)
    assert "Gmail: no new replies or job mail." in err and block == ""


def test_searches_cover_ats_senders_and_open_applications(pi, tmp_path):
    pi.cli(0, "apply", "--url", "https://x.example/1", "--company", "Motive Analytics, Inc.", "--title", "SRE")
    pi.cli(0, "apply", "--url", "https://x.example/2", "--company", "Closed Co", "--title", "SRE")
    pi.cli(0, "mark", "104", "rejected")
    pi.run(1)
    assert pi.gmail.queries[-2:] == [
        '"from:(greenhouse-mail.io OR greenhouse.io OR lever.co OR ashbyhq.com) after:2026/11/05"',
        '"from:(\\"motive analytics\\" OR motiveanalytics) after:2026/11/05"',
    ]
    cfg = tmp_path / "config.yaml"
    cfg.write_text("tracking:\n  ats_senders: [myworkday.com]\n")
    pi.main(["mail", "--config", str(cfg), *pi.args], 2)
    assert '"from:(myworkday.com) after:2026/11/06"' in pi.gmail.queries


def test_a_confirmation_corrects_a_late_applied_date_and_counts_once(pi):
    pi.add(greenhouse_confirmation(NOW + timedelta(hours=1)))
    pi.cli(2, "apply", "101")                       # logged two days later, no --date
    assert pi.app(101).applied_at == NOW + timedelta(days=2)
    pi.run(2)
    assert pi.app(101).applied_at == NOW + timedelta(hours=1)
    pi.add(greenhouse_confirmation(NOW + timedelta(hours=2)))
    _, out, _ = pi.main(["mail", *pi.args], 3)
    assert "done: #101 Example Corp — Senior Backend Engineer, Platform: already confirmed" in out


@pytest.mark.parametrize("stage_cmds, expect_stage, expect_kind", [
    ([], "screen", "screen"),
    ([("mark", "101", "screen")], "interview", "interview"),
    ([("mark", "101", "interview")], "interview", "reply"),
])
def test_interview_requests_move_one_stage(pi, stage_cmds, expect_stage, expect_kind):
    pi.cli(0, "apply", "101")
    for cmd in stage_cmds:
        pi.cli(0, *cmd)
    pi.add(recruiter_invite(NOW + timedelta(days=1)))
    pi.run(2)
    app = pi.app(101)
    assert app.stage == expect_stage and app.events[-1].kind == expect_kind


def test_mail_about_closed_or_unknown_applications_goes_to_check(pi):
    pi.cli(0, "apply", "101")
    pi.cli(0, "mark", "101", "withdrew")
    pi.add(recruiter_invite(NOW + timedelta(days=1)))
    pi.add(greenhouse_rejection(NOW + timedelta(days=1)))   # about #102, which you never logged
    block, _ = pi.run(2)
    assert "which is withdrawn" in block
    assert "“Your application to Example Corp” (Nov 7): it's about Staff Infrastructure Engineer (#102), which you " \
           "haven't logged; `jobfeed apply 102`, then record what it says" in block
    assert pi.app(101).stage == "withdrawn" and pi.app(102) is None


def test_a_rejection_of_a_rejected_application_changes_nothing(pi):
    pi.cli(0, "apply", "102")
    pi.cli(0, "mark", "102", "rejected")
    pi.add(greenhouse_rejection(NOW + timedelta(days=1)))
    _, out, _ = pi.main(["mail", *pi.args], 2)
    assert "done: #102 Example Corp — Staff Infrastructure Engineer: already rejected" in out
    assert [e.kind for e in pi.app(102).events] == ["applied", "rejected"]


def test_your_own_mail_is_never_job_mail(pi):
    pi.cli(0, "apply", "101")
    pi.add(job_mail(f"Me <{ME}>", "Re: next steps", "Thanks for applying to my own thing", sent=NOW), ("\\Sent",))
    _, out, _ = pi.main(["mail", *pi.args], 1)
    assert out.startswith("Nothing new since")


def test_dry_run_shows_each_message_and_reread_shows_them_again(pi):
    pi.cli(0, "apply", "102")
    pi.add(greenhouse_rejection(NOW + timedelta(days=1)))
    pi.add(newsletter(NOW + timedelta(days=1)))
    pi.add(reply("note 102 sad", sent=NOW + timedelta(days=1)))
    before = pi.db.read_bytes()
    code, out, err = pi.main(["mail", "--dry-run", *pi.args], 2)
    assert code == 0 and pi.db.read_bytes() == before
    assert out.splitlines()[-4:] == [
        "Nov 7, 11:00  Example Corp <no-reply@us.greenhouse-mail.io>  “Your application to Example Corp”",
        "  read as: rejection (“decided to move forward with other”)",
        "  done: #102 Example Corp — Staff Infrastructure Engineer: rejected, now at rejected (matched by its job link)",
        "Passed over 1 other company email (newsletters and the like).",
    ]
    pi.main(["mail", *pi.args], 2)
    _, out, _ = pi.main(["mail", "--dry-run", *pi.args], 2)
    assert out.startswith("Nothing new")
    _, out, _ = pi.main(["mail", "--dry-run", "--reread", *pi.args], 2)
    assert "read as: rejection" in out and "already rejected" in out
    code, _, err = pi.main(["mail", "--reread", *pi.args], 2)
    assert code == 2 and "only works with --dry-run" in err


# --- jobfeed edit ------------------------------------------------------------------------------

def test_edit(pi):
    pi.cli(0, "apply", "101")
    out = pi.cli(0, "edit", "101", "--resume", "v2", "--cover-letter", "--referral")
    assert out == ("#101 Example Corp — Senior Backend Engineer, Platform: resume v2, cover letter, referral, "
                   "found via greenhouse\n")
    app = pi.app(101)
    assert (app.resume_version, app.cover_letter, app.referral) == ("v2", True, True)
    pi.cli(0, "edit", "101", "--no-cover-letter")
    assert pi.app(101).cover_letter is False
    assert pi.main(["edit", "101", "--db", str(pi.db)])[0] == 2                       # nothing to change
    code, _, err = pi.main(["edit", "101", "--via", "referral", "--db", str(pi.db)])
    assert code == 1 and "already known" in err
    pi.cli(0, "apply", "--url", "https://x.example/1", "--company", "Foo", "--title", "SRE")
    pi.cli(0, "edit", "103", "--via", "linkedin")
    assert pi.app(103).found_via == "linkedin"
    assert pi.main(["edit", "555", "--resume", "v2", "--db", str(pi.db)])[0] == 1


# --- the real DuckDuckGo confirmation (Ashby, Nov 7, 2026) ----------------------------------------------

def test_duckduckgo_confirmation_reads_as_a_confirmation_and_nothing_else():
    reading = _read(duckduckgo_confirmation(NOW))
    assert reading.kind == "confirmation" and reading.why == "Confirmation of your application"
    # "we'll set up a time to chat" isn't an invitation, and "if we don't see a fit" isn't a rejection
    assert reading.kinds == ["confirmation"]


def test_duckduckgo_confirmation_finds_the_application_by_name_and_fixes_its_date(pi):
    sent = NOW + timedelta(hours=1)
    pi.add(duckduckgo_confirmation(sent))
    pi.cli(1, "apply", "--url", "https://remoteok.com/remote-jobs/remote-senior-backend-engineer-duckduckgo-1",
           "--company", "DuckDuckGo", "--title", "Senior Backend Engineer")
    _, out, _ = pi.main(["mail", *pi.args], 1)
    assert out.splitlines()[-2:] == [
        "  read as: confirmation (“Confirmation of your application”)",
        "  done: #103 DuckDuckGo — Senior Backend Engineer: confirmation recorded on #103; applied date corrected to Nov 6",
    ]
    assert pi.app(103).applied_at == sent
    assert '"from:(duckduckgo) after:2026/11/05"' in pi.gmail.queries     # searched by name, as well as by Ashby
