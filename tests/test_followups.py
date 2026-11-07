from __future__ import annotations

import io
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from jobfeed import tracker
from jobfeed.cli import main
from jobfeed.config import ConfigError, TrackingRules, load_config
from jobfeed.followups import gather
from jobfeed.store import Store
from tests.conftest import router
from tests.test_email import CapturingMailer

NOW = datetime(2026, 11, 6, 19, 0, tzinfo=timezone.utc)   # Fri Nov 6, 11am in Seattle


def _run(argv, transport=None, *, day=0, mailer=None):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, transport=transport, out=out, err=err, now=NOW + timedelta(days=day), mailer=mailer)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def pi(tmp_path, greenhouse_jobs):
    """The first run (#101 and #102 emailed), then an application to each on the same day."""
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies: []\n")
    db = tmp_path / "jobs.db"
    transport = router({"/v1/boards/examplecorp/jobs": lambda r: httpx.Response(200, json=greenhouse_jobs)})
    common = ["--companies", str(companies), "--db", str(db), "--board", "greenhouse:examplecorp"]

    class Pi:
        args = ["--db", str(db)]

        def run(self, day, *extra, mailer=None):
            """A --no-email run (or an emailed one with `mailer`); returns (exit code, the block's text, stderr)."""
            argv = ["run", *extra, *common]
            if mailer is None and "--dry-run" not in extra:
                argv.insert(1, "--no-email")
            if mailer is not None:
                env = tmp_path / ".env"
                env.write_text("GMAIL_USER=me@gmail.com\nGMAIL_APP_PASSWORD=pw\nDIGEST_TO=me@gmail.com\n")
                env.chmod(0o600)
                argv += ["--env", str(env)]
            code, out, err = _run(argv, transport, day=day, mailer=mailer)
            return code, block(out), err

        def cmd(self, day, *argv):
            code, out, err = _run([*argv, *self.args], day=day)
            assert code == 0, err
            return out

    p = Pi()
    p.db = db
    p.run(0)
    p.cmd(0, "apply", "101")
    p.cmd(0, "apply", "102")
    return p


def block(digest_text: str) -> str:
    """The "Your applications" block of a printed digest, or "" when it has none."""
    if "== Your applications" not in digest_text:
        return ""
    rest = digest_text.split("== Your applications\n", 1)[1]
    return rest.split("\n\n", 1)[0]


def _events(db, code):
    with Store.open(db) as store:
        return [(e.kind, e.origin, e.detail) for e in tracker.events_for(store, code)]


# --- logged and updates -----------------------------------------------------------------

def test_logged_applications_are_reported_once(pi):
    code, text, err = pi.run(1)
    assert code == 0
    assert text.splitlines() == [
        "  Logged since the last digest",
        "    #101 Example Corp — Senior Backend Engineer, Platform · applied Nov 6 · resume v1",
        "    #102 Example Corp — Staff Infrastructure Engineer · applied Nov 6 · resume v1",
    ]
    assert "applications: 2 logged · 0 updates · 0 to follow up" in err
    assert pi.run(2)[1] == ""          # nothing new: no block at all


def test_block_sits_above_the_postings(pi, tmp_path):
    _, out, _ = _run(["replay", "--companies", str(tmp_path / "companies.yaml"), *pi.args], day=1)
    assert "Your applications" not in out          # replay never shows it
    code, out, _ = _run(["run", "--no-email", "--companies", str(tmp_path / "companies.yaml"), *pi.args,
                         "--board", "greenhouse:examplecorp"],
                        router({"/v1/boards/examplecorp/jobs": lambda r: httpx.Response(200, json={"jobs": []})}),
                        day=1)
    assert out.splitlines()[:3] == ["Job feed · Sat Nov 7 · 0 new (0 watchlist)", "", "== Your applications"]
    assert out.rstrip().endswith("Nothing new matched.")


def test_updates_say_what_happened(pi):
    pi.run(1)
    pi.cmd(2, "mark", "101", "replied")
    pi.cmd(3, "mark", "101", "screen")
    pi.cmd(3, "note", "101", "notes aren't listed")
    pi.cmd(4, "mark", "101", "rejected")
    with Store.open(pi.db) as store:
        tracker.mark(store, 102, "interview", NOW + timedelta(days=4), origin="email")
    _, text, err = pi.run(5)
    assert text.splitlines() == [
        "  Updates",
        "    #101 Example Corp — Senior Backend Engineer, Platform · reply Nov 8",
        "    #101 Example Corp — Senior Backend Engineer, Platform · screen Nov 9",
        "    #101 Example Corp — Senior Backend Engineer, Platform · rejected Nov 10 (at screen)",
        "    #102 Example Corp — Staff Infrastructure Engineer · interview Nov 10 (from email)",
    ]
    assert "0 logged · 4 updates" in err


def test_an_undone_entry_is_never_reported(pi):
    pi.cmd(0, "mark", "101", "screen")
    pi.cmd(0, "undo", "101")
    _, text, _ = pi.run(1)
    assert "Updates" not in text and "Logged" in text


# --- the email ----------------------------------------------------------------------------

def test_emailed_digest_carries_the_block(pi):
    mailer = CapturingMailer()
    code, _, _ = pi.run(1, mailer=mailer)
    assert code == 0
    [msg] = mailer.sent
    text = msg.get_body(("plain",)).get_content()
    html = msg.get_body(("html",)).get_content()
    assert "== Your applications\n  Logged since the last digest\n    #101 Example Corp" in text
    assert "Your applications</div>" in html and "#101 Example Corp — Senior Backend Engineer, Platform" in html


def test_a_failed_email_reports_nothing_so_the_next_one_repeats_it(pi):
    pi.run(1)
    pi.cmd(1, "mark", "101", "screen")
    code, _, err = pi.run(12, mailer=CapturingMailer(fail=True))
    assert code == 1 and "applications:" not in err
    assert [k for k, _, _ in _events(pi.db, 102)] == ["applied"]          # no reminder kept
    mailer = CapturingMailer()
    pi.run(12, mailer=mailer)
    text = mailer.sent[0].get_body(("plain",)).get_content()
    assert "screen Nov 7" in text and "#102 Example Corp — Staff Infrastructure Engineer · applied Nov 6 · quiet 12 days" in text


# --- follow-ups ---------------------------------------------------------------------------

def test_follow_up_after_ten_quiet_days_listed_once_per_quiet_stretch(pi):
    pi.run(1)
    pi.cmd(3, "mark", "101", "screen")
    pi.run(4)
    assert pi.run(9)[1] == ""                                 # #102 quiet 9 days: not yet
    _, text, err = pi.run(10)
    assert text.splitlines() == [
        "  Follow up (nothing new in 10+ days)",
        "    #102 Example Corp — Staff Infrastructure Engineer · applied Nov 6 · quiet 10 days",
    ]
    assert "0 logged · 0 updates · 1 to follow up" in err
    assert pi.run(11)[1] == ""                                # listed once
    assert pi.run(13)[1].splitlines()[1:] == [
        "    #101 Example Corp — Senior Backend Engineer, Platform · screen since Nov 9 · quiet 10 days"]
    pi.cmd(15, "note", "101", "emailed the recruiter")        # any entry starts a new quiet stretch
    assert "#102" in pi.run(21)[1] and "#101" not in pi.run(21)[1]    # (#102 closes with no reply)
    assert pi.run(24)[1] == ""
    assert "#101" in pi.run(25)[1]
    assert [k for k, _, _ in _events(pi.db, 101)] == ["applied", "screen", "follow_up", "note", "follow_up"]


def test_follow_up_line_shows_the_last_entry_when_it_isnt_the_stage(pi):
    pi.run(1)
    pi.cmd(2, "mark", "102", "reply")
    pi.run(3)
    _, text, _ = pi.run(12)
    assert "#102 Example Corp — Staff Infrastructure Engineer · applied Nov 6 · last entry Nov 8 · quiet 10 days" in text


def test_just_logged_applications_wait_a_day_for_their_follow_up(pi):
    pi.run(1)
    pi.cmd(12, "apply", "--url", "https://x.example/1", "--company", "Late Co", "--title", "SRE",
           "--date", "2026-11-01")
    text = pi.run(12)[1]
    assert "#103 Late Co — SRE · applied Nov 1" in text and text.count("#103") == 1
    assert "#103 Late Co — SRE · applied Nov 1 · quiet" in pi.run(13)[1]


def test_closed_applications_get_no_follow_up(pi):
    pi.cmd(1, "mark", "101", "rejected")
    pi.cmd(1, "mark", "102", "offer")
    pi.run(1)
    assert "#101" not in pi.run(15)[1]


# --- the no-response rule -----------------------------------------------------------------

def test_no_reply_in_21_days_closes_it(pi):
    pi.run(1)
    pi.cmd(3, "mark", "101", "screen")
    pi.run(4)
    assert pi.run(13)[1].count("quiet") == 2                 # follow-ups out of the way
    assert pi.run(20)[1] == ""
    _, text, err = pi.run(21)
    assert text.splitlines() == [
        "  Updates",
        "    #102 Example Corp — Staff Infrastructure Engineer · no response: no reply in 21 days, closed Nov 27",
    ]
    assert "1 closed with no reply" in err
    with Store.open(pi.db) as store:
        app = tracker.get_application(store, 102)
        assert tracker.get_application(store, 101).stage == "screen"   # later stages never close
    assert (app.stage, app.stage_at) == ("no_response", NOW + timedelta(days=21))   # dated when the limit passed
    assert pi.run(40)[1] == ""


def test_a_late_run_dates_the_closure_by_the_limit(pi):
    pi.run(1)
    pi.run(30)
    with Store.open(pi.db) as store:
        assert tracker.get_application(store, 101).stage_at == NOW + timedelta(days=21)


def test_any_reply_stops_the_rule(pi):
    pi.cmd(5, "mark", "102", "reply")
    pi.run(40)
    with Store.open(pi.db) as store:
        assert tracker.get_application(store, 102).stage == "applied"
        assert tracker.get_application(store, 101).stage == "no_response"


def test_reopen_restarts_the_clock_and_undo_points_to_it(pi):
    pi.run(21)
    code, _, err = _run(["undo", "101", *pi.args], day=22)
    assert code == 1 and "jobfeed mark 101 reopen" in err
    pi.cmd(22, "mark", "101", "reopen")
    assert "reopened Nov 28" in pi.run(22)[1]
    pi.run(42)
    with Store.open(pi.db) as store:
        assert tracker.get_application(store, 101).stage == "applied"
    pi.run(43)
    with Store.open(pi.db) as store:
        assert tracker.get_application(store, 101).stage_at == NOW + timedelta(days=43)
    assert _events(pi.db, 101)[-1] == ("no_response", "rule", "no reply in 21 days")


def test_a_reply_after_the_rule_reopens_it(pi):
    pi.run(21)
    pi.cmd(25, "mark", "101", "reply")
    with Store.open(pi.db) as store:
        assert tracker.get_application(store, 101).stage == "applied"


def test_dry_run_shows_closures_and_follow_ups_but_saves_nothing(pi):
    pi.run(1)
    pi.cmd(1, "mark", "102", "screen")
    pi.run(2)
    before = pi.db.read_bytes()
    code, text, _ = pi.run(21, "--dry-run")
    assert code == 0
    assert text.splitlines() == [
        "  Updates",
        "    #101 Example Corp — Senior Backend Engineer, Platform · no response: no reply in 21 days, closed Nov 27",
        "  Follow up (nothing new in 10+ days)",
        "    #102 Example Corp — Staff Infrastructure Engineer · screen since Nov 7 · quiet 20 days",
    ]
    assert pi.db.read_bytes() == before


def test_thresholds_come_from_config(pi, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("tracking:\n  follow_up_days: 3\n  no_response_days: 5\n")
    pi.run(1)
    _, text, _ = pi.run(3, "--config", str(cfg))
    assert "Follow up (nothing new in 3+ days)" in text
    _, text, _ = pi.run(5, "--config", str(cfg))
    assert "no response: no reply in 5 days" in text


@pytest.mark.parametrize("text", ["tracking:\n  follow_up_days: 0\n", "tracking:\n  no_response_days: -1\n"])
def test_thresholds_must_be_positive(tmp_path, text):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(text)
    with pytest.raises(ConfigError, match="at least 1"):
        load_config(cfg)


# --- history and upgrades -----------------------------------------------------------------

def test_history_shows_reminders_and_rule_closures_and_undo_skips_reminders(pi):
    pi.run(1)
    pi.run(10)
    pi.cmd(11, "note", "102", "pinged on LinkedIn")
    assert pi.cmd(11, "undo", "102").startswith("Removed note 'pinged on LinkedIn'")
    assert pi.cmd(11, "undo", "102").startswith("Removed #102")   # its reminder isn't yours to undo
    pi.run(21)
    out = pi.cmd(22, "apps", "101")
    assert "2026-11-16  follow-up reminder                 via rule" in out
    assert "2026-11-27  no response (no reply in 21 days)  via rule" in out


def test_version_3_database_gets_reported_and_its_entries_go_in_the_next_digest(pi):
    conn = sqlite3.connect(pi.db)
    conn.executescript("""
        CREATE TABLE events_old AS SELECT id, code, at, kind, origin, message_id, detail FROM events;
        DROP TABLE events;
        ALTER TABLE events_old RENAME TO events;
        PRAGMA user_version = 3;
    """)
    conn.close()
    with Store.open_readonly(pi.db) as store:
        assert not store.has_tracker
        assert gather(store, TrackingRules(), NOW, write=False) is None
    assert pi.run(1, "--dry-run")[0] == 0                    # an un-upgraded jobs.db still previews
    _, text, _ = pi.run(1)
    assert "Logged since the last digest" in text and "#102" in text
