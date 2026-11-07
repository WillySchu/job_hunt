from __future__ import annotations

import io
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from jobfeed import weekly
from jobfeed.cli import main
from jobfeed.config import ConfigError, TrackingRules, load_config
from jobfeed.store import Store
from jobfeed.mailer import SendError
from tests.conftest import router
from tests.test_email import CapturingMailer

SAT = datetime(2026, 11, 7, 14, 0, tzinfo=timezone.utc)      # Sat Nov 7, 6:00 in Seattle
MON = SAT + timedelta(days=2)
RULES = TrackingRules()


# --- when -----------------------------------------------------------------------------------------

def test_report_day():
    assert weekly.report_day(date(2026, 11, 9), RULES) == date(2026, 11, 9)       # a Monday
    assert weekly.report_day(date(2026, 11, 7), RULES) == date(2026, 11, 2)
    assert weekly.report_day(date(2026, 11, 7), TrackingRules(weekly_day="friday")) == date(2026, 11, 6)


def test_report_due_waits_for_the_first_monday_then_catches_up(tmp_path):
    with Store.open(tmp_path / "jobs.db") as store:
        assert weekly.report_due(store, SAT, RULES) is None                    # never sent: wait for Monday
        assert weekly.report_due(store, MON, RULES) == date(2026, 11, 9)
        weekly.mark_sent(store, date(2026, 11, 9))
        assert weekly.report_due(store, MON + timedelta(hours=3), RULES) is None   # once a week
        assert weekly.report_due(store, MON + timedelta(days=6), RULES) is None
        # the next Monday's run was missed: Tuesday catches up
        assert weekly.report_due(store, MON + timedelta(days=8), RULES) == date(2026, 11, 16)
        assert weekly.report_due(store, MON + timedelta(days=7), TrackingRules(weekly_report=False)) is None


def test_backup_copies_consistently_and_keeps_the_newest(tmp_path):
    db = tmp_path / "jobs.db"
    with Store.open(db) as store:
        store.set_meta("probe", "1")
        assert weekly.backup_due(store, SAT, RULES)                  # the first one is right away
        for day in range(10):
            weekly.backup(store, db, SAT + timedelta(days=day), keep=3)
        assert not weekly.backup_due(store, SAT + timedelta(days=9), RULES)
        assert not weekly.backup_due(store, SAT, TrackingRules(backups_kept=0))
    names = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert names == ["jobs-2026-11-14.db", "jobs-2026-11-15.db", "jobs-2026-11-16.db"]
    copy = sqlite3.connect(tmp_path / "backups" / names[-1])
    assert copy.execute("SELECT value FROM meta WHERE key = 'probe'").fetchone() == ("1",)
    with Store.open(db) as store:
        weekly.backup(store, db, SAT + timedelta(days=20), keep=0)       # 0: delete nothing
    assert len(list((tmp_path / "backups").iterdir())) == 4


@pytest.mark.parametrize("text, message", [
    ("tracking:\n  weekly_day: someday\n", "must be a day of the week"),
    ("tracking:\n  backups_kept: -1\n", "can't be negative"),
])
def test_weekly_settings_are_checked(tmp_path, text, message):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(text)
    with pytest.raises(ConfigError, match=message):
        load_config(cfg)
    cfg.write_text("tracking:\n  weekly_day: Friday\n")
    assert load_config(cfg).tracking.weekly_day == "friday"


# --- through `jobfeed run` --------------------------------------------------------------------------

class PickyMailer(CapturingMailer):
    """Sends everything except the weekly report."""

    def send(self, msg):
        if msg["Subject"].startswith("Job search weekly"):
            raise SendError("Gmail is down")
        self.sent.append(msg)


@pytest.fixture
def pi(tmp_path, greenhouse_jobs):
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies: []\n")
    env = tmp_path / ".env"
    env.write_text("GMAIL_USER=me@gmail.com\nGMAIL_APP_PASSWORD=pw\nDIGEST_TO=me@gmail.com\n")
    env.chmod(0o600)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("tracking:\n  read_mail: false\n")
    transport = router({"/v1/boards/examplecorp/jobs": lambda r: httpx.Response(200, json=greenhouse_jobs)})

    class Pi:
        db = tmp_path / "jobs.db"
        backups = tmp_path / "backups"
        args = ["--db", str(tmp_path / "jobs.db")]
        common = ["--env", str(env), "--config", str(cfg), "--companies", str(companies), "--db", str(db),
                  "--board", "greenhouse:examplecorp"]

        def main(self, argv, when, mailer=None):
            out, err = io.StringIO(), io.StringIO()
            code = main(argv, transport=transport, out=out, err=err, now=when, mailer=mailer)
            return code, out.getvalue(), err.getvalue()

        def run(self, when, mailer, *extra):
            return self.main(["run", *extra, *self.common], when, mailer)

    p = Pi()
    p.env, p.cfg = env, cfg
    return p


def test_monday_sends_the_report_after_the_digest_and_backs_up(pi):
    mailer = CapturingMailer()
    code, _, err = pi.run(SAT, mailer)
    assert code == 0 and "Backed up" in err and "jobs-2026-11-07.db" in err
    pi.main(["apply", "101", "--date", "2026-11-04", *pi.args], SAT)
    pi.main(["apply", "102", *pi.args], SAT)
    pi.main(["mark", "101", "screen", *pi.args], SAT + timedelta(hours=1))
    pi.run(SAT + timedelta(days=1), mailer)
    assert [m["Subject"] for m in mailer.sent] == ["Job feed · Sat Nov 7 · 2 new (0 watchlist)",
                                                   "Job feed · Sun Nov 8 · 0 new (0 watchlist)"]
    code, _, err = pi.run(MON, mailer)
    assert code == 0
    assert [m["Subject"] for m in mailer.sent[2:]] == ["Job feed · Mon Nov 9 · 0 new (0 watchlist)",
                                                       "Job search weekly · Nov 2–8 · 2 applied (2 so far)"]
    digest_text = mailer.sent[2].get_body(("plain",)).get_content()
    assert "Backed up" in digest_text and "jobs-2026-11-09.db" in digest_text        # the footer says so
    text = mailer.sent[3].get_body(("plain",)).get_content()
    assert text.splitlines()[:5] == [
        "Job search weekly · Nov 2–8", "",
        "Last week: 2 applications (2 so far) · news from companies: 1 screen",
        "  #101 Example Corp — Senior Backend Engineer, Platform",
        "  #102 Example Corp — Staff Infrastructure Engineer",
    ]
    assert "Funnel" in text and "Longest without news" in text
    assert "Digest section" not in text                     # nothing counted yet: no empty tables
    assert "<pre" in mailer.sent[3].get_body(("html",)).get_content()
    pi.run(MON + timedelta(days=1), mailer)
    assert len(mailer.sent) == 5                              # Tuesday: just the digest
    assert sorted(p.name for p in pi.backups.iterdir()) == ["jobs-2026-11-07.db", "jobs-2026-11-09.db"]


def test_a_failed_report_never_fails_the_run_and_waits_for_the_next(pi):
    pi.run(SAT, CapturingMailer())
    picky = PickyMailer()
    code, _, err = pi.run(MON, picky)
    assert code == 0 and "weekly report not sent: Gmail is down" in err
    mailer = CapturingMailer()
    pi.run(MON + timedelta(days=1), mailer)
    assert [m["Subject"].split(" · ")[0] for m in mailer.sent] == ["Job feed", "Job search weekly"]


def test_no_report_when_the_digest_itself_failed(pi):
    pi.run(SAT, CapturingMailer())
    code, _, _ = pi.run(MON, CapturingMailer(fail=True))
    assert code == 1
    mailer = CapturingMailer()
    pi.run(MON + timedelta(minutes=10), mailer)            # systemd's retry
    assert [m["Subject"].split(" · ")[0] for m in mailer.sent] == ["Job feed", "Job search weekly"]


def test_no_email_runs_print_the_report_and_dry_runs_do_nothing(pi):
    pi.run(SAT, None, "--no-email")
    before = sorted(pi.backups.iterdir())
    code, out, _ = pi.run(MON, None, "--dry-run")
    assert "Job search weekly" not in out and sorted(pi.backups.iterdir()) == before
    code, out, _ = pi.run(MON, None, "--no-email")
    assert "\nJob search weekly · Nov 2–8\n" in out


def test_report_and_backups_can_be_turned_off(pi):
    pi.cfg.write_text("tracking:\n  read_mail: false\n  weekly_report: false\n  backups_kept: 0\n")
    mailer = CapturingMailer()
    pi.run(SAT, mailer)
    pi.run(MON, mailer)
    assert len(mailer.sent) == 2 and not pi.backups.exists()


# --- the commands -----------------------------------------------------------------------------------------

def test_weekly_command_covers_the_last_seven_days_and_leaves_monday_alone(pi):
    pi.run(SAT, CapturingMailer())
    pi.main(["apply", "101", *pi.args], SAT)
    code, out, _ = pi.main(["weekly", "--no-email", "--config", str(pi.cfg), *pi.args], SAT)
    assert code == 0 and out.startswith("\nJob search weekly · Nov 1–7\n")
    mailer = CapturingMailer()
    code, _, err = pi.main(["weekly", "--env", str(pi.env), "--config", str(pi.cfg), *pi.args], SAT, mailer)
    assert code == 0 and mailer.sent[0]["Subject"] == "Job search weekly · Nov 1–7 · 1 applied (1 so far)"
    mailer = CapturingMailer()
    pi.run(MON, mailer)
    assert len(mailer.sent) == 2                              # Monday's report still goes out
    code, _, err = pi.main(["weekly", "--env", str(pi.env), "--config", str(pi.cfg), *pi.args], SAT, PickyMailer())
    assert code == 1 and "not sent" in err


def test_backup_command(pi, tmp_path):
    code, _, err = pi.main(["backup", "--config", str(pi.cfg), *pi.args], SAT)
    assert code == 1 and "doesn't exist yet" in err
    pi.run(SAT, CapturingMailer())
    code, out, _ = pi.main(["backup", "--config", str(pi.cfg), "--db", str(pi.db)], SAT + timedelta(days=1))
    assert code == 0 and out.startswith("Backed up") and "jobs-2026-11-08.db" in out


def test_last_week_news_words():
    from jobfeed.stats import facts_for
    from tests.test_stats import _app
    now = datetime(2026, 12, 1, 19, tzinfo=timezone.utc)
    facts = [facts_for(_app("rejected", days_ago=8, events=[("reply", 1, "cli", None), ("rejected", 3, "email", "x")]),
                       None, now, RULES),
             facts_for(_app("screen", days_ago=9, events=[("reply", 2, "cli", None), ("screen", 3, "cli", None)]),
                       None, now, RULES)]
    lines = weekly.last_week(facts, now - timedelta(days=7), now)
    assert lines == ["Last week: 0 applications (2 so far) · news from companies: 2 replies, 1 screen, 1 rejection"]


# --- the attached backup --------------------------------------------------------------------------

def _weekly_message(mailer):
    [msg] = [m for m in mailer.sent if m["Subject"].startswith("Job search weekly")]
    return msg


def test_the_report_carries_a_working_copy_of_jobs_db(pi, tmp_path):
    import gzip
    pi.run(SAT, CapturingMailer())
    pi.main(["apply", "101", *pi.args], SAT)
    mailer = CapturingMailer()
    pi.run(MON, mailer)
    msg = _weekly_message(mailer)
    [part] = list(msg.iter_attachments())
    assert (part.get_filename(), part.get_content_type()) == ("jobs-2026-11-09.db.gz", "application/gzip")
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(part.get_content()))
    with Store.open(restored) as store:
        assert store.by_code(101) is not None
        assert store.conn.execute("SELECT COUNT(*) FROM applications").fetchone() == (1,)
    text = msg.get_body(("plain",)).get_content()
    assert re.search(r"Backup attached: jobs-2026-11-09\.db\.gz \(\d+ KB; gunzip it to get jobs\.db\)\.$", text.rstrip())
    assert "!!" not in text


def _size_of_copy(pi):
    with Store.open(pi.db) as store:
        return weekly.pack_backup(store, MON, limit=10**9).size


def test_a_copy_nearing_the_limit_warns_at_the_top(pi, monkeypatch):
    pi.run(SAT, CapturingMailer())
    monkeypatch.setattr(weekly, "ATTACH_LIMIT", int(_size_of_copy(pi) / 0.8))
    mailer = CapturingMailer()
    _, _, err = pi.run(MON, mailer)
    msg = _weekly_message(mailer)
    assert len(list(msg.iter_attachments())) == 1
    lines = msg.get_body(("plain",)).get_content().splitlines()
    assert lines[2].startswith("!! Backup attached (jobs-2026-11-09.db.gz,") and "nearing the" in lines[2]
    assert "weekly report: Backup attached" in err


def test_a_copy_too_big_to_attach_says_so_at_the_top(pi, monkeypatch):
    pi.run(SAT, CapturingMailer())
    monkeypatch.setattr(weekly, "ATTACH_LIMIT", 100)
    mailer = CapturingMailer()
    code, _, err = pi.run(MON, mailer)
    assert code == 0
    msg = _weekly_message(mailer)
    assert list(msg.iter_attachments()) == []
    lines = msg.get_body(("plain",)).get_content().splitlines()
    assert lines[2].startswith("!! BACKUP NOT ATTACHED: the compressed copy of jobs.db is ")
    assert "copy them off another way (README → Backups)" in lines[2]
    html = msg.get_body(("html",)).get_content()
    assert "background:#fff8c5" in html and "BACKUP NOT ATTACHED" in html
    assert "weekly report: BACKUP NOT ATTACHED" in err


def test_a_failed_copy_still_sends_the_report_with_an_alert(pi, monkeypatch):
    pi.run(SAT, CapturingMailer())

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(weekly, "pack_backup", broken)
    mailer = CapturingMailer()
    pi.run(MON, mailer)
    text = _weekly_message(mailer).get_body(("plain",)).get_content()
    assert "!! BACKUP NOT ATTACHED: copying jobs.db failed (disk full)." in text


def test_attaching_can_be_turned_off(pi):
    pi.cfg.write_text("tracking:\n  read_mail: false\n  attach_backup: false\n")
    pi.run(SAT, CapturingMailer())
    mailer = CapturingMailer()
    pi.run(MON, mailer)
    msg = _weekly_message(mailer)
    assert list(msg.iter_attachments()) == [] and "Backup" not in msg.get_body(("plain",)).get_content()


def test_weekly_command_attaches_too(pi):
    pi.run(SAT, CapturingMailer())
    mailer = CapturingMailer()
    pi.main(["weekly", "--env", str(pi.env), "--config", str(pi.cfg), *pi.args], SAT, mailer)
    assert [p.get_filename() for p in mailer.sent[0].iter_attachments()] == ["jobs-2026-11-07.db.gz"]
