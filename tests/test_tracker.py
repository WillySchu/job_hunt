from __future__ import annotations

import io
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from jobfeed.cli import main
from jobfeed.models import Job
from jobfeed.store import FIRST_CODE, SCHEMA_VERSION, Store
from jobfeed import tracker
from jobfeed.tracker import Event, TrackerError, derive, parse_day
from tests.conftest import router

NOW = datetime(2026, 11, 6, 19, 0, tzinfo=timezone.utc)   # Fri Nov 6, 11am in Seattle
LATER = NOW + timedelta(days=2)                            # Sun Nov 8


def _run(argv, transport=None, now=NOW, mailer=None):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, transport=transport, out=out, err=err, now=now, mailer=mailer)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def feed(tmp_path, greenhouse_jobs):
    """A jobs.db holding one --no-email run: #101 (New matches) and #102 (No salary listed)."""
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies: []\n")
    db = tmp_path / "jobs.db"
    transport = router({"/v1/boards/examplecorp/jobs": lambda r: httpx.Response(200, json=greenhouse_jobs)})
    run = ["run", "--no-email", "--companies", str(companies), "--db", str(db), "--board", "greenhouse:examplecorp"]
    code, out, _ = _run(run, transport)
    assert code == 0
    cfg = tmp_path / "config.yaml"
    cfg.write_text("tracking:\n  resume_version: v1\n")
    return {"db": db, "out": out, "run": run, "transport": transport, "companies": companies,
            "args": ["--db", str(db)], "cfg": ["--config", str(cfg)], "cfg_path": cfg}


def _apps(db):
    with Store.open(db) as store:
        return {a.code: a for a in tracker.list_applications(store, include_closed=True)}


# --- schema 3 and digest codes ------------------------------------------------------

def test_version_2_database_is_upgraded_in_place(tmp_path):
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE jobs (id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL, first_seen TEXT NOT NULL,
          last_seen TEXT NOT NULL, matched INTEGER NOT NULL, emailed_at TEXT, data TEXT NOT NULL);
        CREATE TABLE board_runs (label TEXT NOT NULL, run_at TEXT NOT NULL, postings INTEGER NOT NULL,
          error TEXT, PRIMARY KEY (label, run_at));
        PRAGMA user_version = 2;
    """)
    job = Job(id="greenhouse:1", source="greenhouse", company="Acme", title="Backend Engineer", url="https://a/1")
    import json
    conn.execute("INSERT INTO jobs VALUES (?, 'k', '2026-11-01T00:00:00+00:00', '2026-11-01T00:00:00+00:00', 1, "
                 "'2026-11-01T00:00:00+00:00', ?)", (job.id, json.dumps(job.to_dict())))
    conn.commit()
    conn.close()

    # read-only opens of the old file still work, without codes
    ro = Store.open_readonly(db)
    assert not ro.has_codes and ro.by_code(101) is None
    assert ro.get([job.id])[job.id].code is None
    ro.close()

    with Store.open(db) as store:
        assert store.has_codes
        row = store.get([job.id])[job.id]
        assert row.job == job and row.code is None
        assert store.next_code() == FIRST_CODE
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 6
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"applications", "events", "mail_seen"} <= tables
    assert "reported" in {r[1] for r in conn.execute("PRAGMA table_info(events)")}


def test_codes_are_numbered_in_digest_order_and_shown(feed):
    out = feed["out"]
    assert "Applied to one? Log it on the Pi with: jobfeed apply CODE" in out
    assert "  #101 Senior Backend Engineer, Platform — Example Corp" in out
    assert "  #102 Staff Infrastructure Engineer — Example Corp" in out
    with Store.open(feed["db"]) as store:
        assert store.by_code(101).job.id == "greenhouse:4412983" and store.by_code(101).section == "matches"
        assert store.by_code(102).section == "no_salary"
        dropped = [r for r in store.all_rows() if not r.matched]
        assert [r.code for r in dropped] == [None]          # only emailed postings get a code


def test_codes_are_kept_across_runs_and_shown_in_replay(feed):
    _run(feed["run"], feed["transport"], now=NOW + timedelta(days=1))
    with Store.open(feed["db"]) as store:
        assert store.by_code(101).job.id == "greenhouse:4412983" and store.next_code() == 103
    _, out, _ = _run(["replay", "--companies", str(feed["companies"]), *feed["args"]])
    assert "#101 Senior Backend Engineer, Platform" in out and "#102 Staff" in out
    assert "Applied to one?" not in out


def test_dry_run_shows_no_codes(tmp_path, greenhouse_jobs):
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies: []\n")
    transport = router({"/v1/boards/examplecorp/jobs": lambda r: httpx.Response(200, json=greenhouse_jobs)})
    _, out, _ = _run(["run", "--dry-run", "--companies", str(companies), "--db", str(tmp_path / "jobs.db"),
                      "--board", "greenhouse:examplecorp"], transport)
    assert "#10" not in out and "Applied to one?" not in out


def test_emailed_digest_has_codes_and_a_failed_send_keeps_them(tmp_path, greenhouse_jobs):
    from tests.test_email import CapturingMailer
    env = tmp_path / ".env"
    env.write_text("GMAIL_USER=me@gmail.com\nGMAIL_APP_PASSWORD=pw\nDIGEST_TO=me@gmail.com\n")
    env.chmod(0o600)
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies: []\n")
    transport = router({"/v1/boards/examplecorp/jobs": lambda r: httpx.Response(200, json=greenhouse_jobs)})
    argv = ["run", "--env", str(env), "--companies", str(companies), "--db", str(tmp_path / "jobs.db"),
            "--board", "greenhouse:examplecorp"]
    code, _, _ = _run(argv, transport, mailer=CapturingMailer(fail=True))
    assert code == 1
    mailer = CapturingMailer()
    _run(argv, transport, now=NOW + timedelta(hours=1), mailer=mailer)
    [msg] = mailer.sent
    text = msg.get_body(("plain",)).get_content()
    html = msg.get_body(("html",)).get_content()
    assert "#101 Senior Backend Engineer, Platform" in text     # same codes as the failed attempt
    assert ">#101</span>" in html and "jobfeed apply CODE" in html


# --- jobfeed apply -------------------------------------------------------------------

def test_apply_by_code_copies_the_posting(feed):
    code, out, err = _run(["apply", "101", *feed["args"], *feed["cfg"]], now=LATER)
    assert code == 0, err
    assert out == "Logged #101 Example Corp — Senior Backend Engineer, Platform (applied Nov 8; resume v1)\n"
    app = _apps(feed["db"])[101]
    assert (app.job_id, app.found_via, app.digest_section, app.logged_via) == (
        "greenhouse:4412983", "greenhouse", "matches", "code")
    assert app.url == "https://job-boards.greenhouse.io/examplecorp/jobs/4412983"
    assert (app.stage, app.stage_at, app.applied_at) == ("applied", LATER, LATER)
    assert (app.resume_version, app.cover_letter, app.referral) == ("v1", False, False)
    assert app.first_seen == NOW and app.days_to_apply == 2


def test_apply_several_codes_with_options(feed):
    code, out, _ = _run(["apply", "#101", "102", "101", "--resume", "v2-short", "--cover-letter", "--referral",
                         "--date", "2026-11-07", "--note", "via Sam", *feed["args"], *feed["cfg"]], now=LATER)
    assert code == 0
    assert out.count("Logged") == 2                       # a repeated code counts once
    assert "resume v2-short, cover letter, referral" in out
    apps = _apps(feed["db"])
    noon = datetime(2026, 11, 7, 12, 0).astimezone().astimezone(timezone.utc)
    assert apps[102].applied_at == noon and apps[102].notes == "2026-11-07: via Sam"
    assert (apps[102].resume_version, apps[102].cover_letter, apps[102].referral) == ("v2-short", True, True)


def test_apply_defaults_come_from_config(feed):
    feed["cfg_path"].write_text("tracking:\n  resume_version: v3\n  cover_letter: true\n")
    _run(["apply", "101", *feed["args"], *feed["cfg"]], now=LATER)
    _run(["apply", "102", "--no-cover-letter", *feed["args"], *feed["cfg"]], now=LATER)
    apps = _apps(feed["db"])
    assert (apps[101].resume_version, apps[101].cover_letter) == ("v3", True)
    assert apps[102].cover_letter is False


def test_apply_reports_unknown_and_repeated_codes_and_logs_the_rest(feed):
    _run(["apply", "101", *feed["args"], *feed["cfg"]], now=LATER)
    code, out, err = _run(["apply", "101", "999", "102", *feed["args"], *feed["cfg"]], now=LATER)
    assert code == 1
    assert "#101 not logged: #101 is already logged (Example Corp, applied Nov 8)" in err
    assert "#999 not logged: no posting #999" in err and "--url" in err
    assert "Logged #102" in out
    assert set(_apps(feed["db"])) == {101, 102}


def test_apply_by_url_finds_a_saved_posting(feed):
    url = "https://job-boards.greenhouse.io/examplecorp/jobs/4412990/?gh_src=linkedin#apply"
    code, out, _ = _run(["apply", "--url", url, *feed["args"], *feed["cfg"]], now=LATER)
    assert code == 0 and "Logged #102 Example Corp — Staff Infrastructure Engineer" in out
    app = _apps(feed["db"])[102]
    assert (app.job_id, app.found_via, app.logged_via) == ("greenhouse:4412990", "greenhouse", "manual")


def test_apply_by_url_gives_a_saved_posting_without_a_code_one(feed):
    with Store.open(feed["db"]) as store:
        dropped = next(r for r in store.all_rows() if not r.matched)
    code, out, _ = _run(["apply", "--url", dropped.job.url, *feed["args"], *feed["cfg"]], now=LATER)
    assert code == 0 and "Logged #103" in out
    with Store.open(feed["db"]) as store:
        assert store.by_code(103).job.id == dropped.job.id


def test_apply_by_url_for_a_role_jobfeed_never_saw(feed):
    argv = ["apply", "--url", "https://www.linkedin.com/jobs/view/123", *feed["args"], *feed["cfg"]]
    code, _, err = _run(argv, now=LATER)
    assert code == 1 and "needs --company and --title" in err
    code, out, _ = _run([*argv, "--company", "Foo", "--title", "SRE", "--referral"], now=LATER)
    assert code == 0 and "Logged #103 Foo — SRE" in out and "its code is #103" in out
    app = _apps(feed["db"])[103]
    assert (app.job_id, app.found_via, app.logged_via, app.first_seen, app.days_to_apply) == (
        None, "referral", "manual", None, None)
    # the next digest skips the number the manual application took
    with Store.open(feed["db"]) as store:
        assert store.next_code() == 104


def test_apply_via_choice(feed):
    _run(["apply", "--url", "https://x.example/1", "--company", "Foo", "--title", "SRE", "--via", "recruiter",
          *feed["args"], *feed["cfg"]], now=LATER)
    assert _apps(feed["db"])[103].found_via == "recruiter"


@pytest.mark.parametrize("argv, message", [
    (["apply"], "which application?"),
    (["apply", "101", "--url", "https://x"], "not both"),
    (["apply", "101", "--company", "Foo"], "are for --url"),
    (["apply", "101", "--date", "2026-11-09"], "--date: 2026-11-09 is in the future"),
    (["apply", "101", "--date", "Nov 5"], "isn't a date"),
])
def test_apply_usage_errors(feed, argv, message):
    code, _, err = _run([*argv, *feed["args"], *feed["cfg"]], now=LATER)
    assert code == 2 and message in err
    assert _apps(feed["db"]) == {}


def test_bad_code_is_an_argparse_error(feed, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["apply", "four-twelve", *feed["args"]])
    assert exc.value.code == 2 and "isn't a code" in capsys.readouterr().err


# --- mark, note, apps, undo -------------------------------------------------------------

@pytest.fixture
def applied(feed):
    _run(["apply", "101", "102", *feed["args"], *feed["cfg"]], now=LATER)
    return feed


def test_mark_moves_through_the_stages(applied):
    args = applied["args"]
    code, out, _ = _run(["mark", "101", "replied", *args], now=LATER + timedelta(days=1))
    assert code == 0 and out.endswith(": reply on Nov 9 (still applied)\n")
    code, out, _ = _run(["mark", "101", "screen", *args], now=LATER + timedelta(days=2))
    assert out.endswith(": moved to screen on Nov 10\n")
    _run(["mark", "101", "onsite", *args], now=LATER + timedelta(days=5))
    code, out, _ = _run(["mark", "101", "reject", "--note", "wanted more Go", *args], now=LATER + timedelta(days=6))
    assert out.endswith(": rejected on Nov 14\n")
    app = _apps(applied["db"])[101]
    assert (app.stage, app.stage_at) == ("rejected", LATER + timedelta(days=6))
    assert app.notes == "2026-11-14: wanted more Go"
    with Store.open(applied["db"]) as store:
        events = tracker.events_for(store, 101)
    assert [(e.kind, e.detail) for e in events] == [
        ("applied", None), ("reply", None), ("screen", None), ("interview", None), ("rejected", "interview"),
        ("note", "wanted more Go")]


def test_mark_errors(applied):
    args = applied["args"]
    assert _run(["mark", "101", "dancing", *args], now=LATER)[0] == 2
    code, _, err = _run(["mark", "555", "screen", *args], now=LATER)
    assert code == 1 and "no application #555" in err
    code, _, err = _run(["mark", "101", "screen", "--date", "2026-11-07", *args], now=LATER)
    assert code == 1 and "after that date" in err
    code, _, err = _run(["mark", "101", "reopen", *args], now=LATER)
    assert code == 1 and "still open" in err
    _run(["mark", "101", "screen", *args], now=LATER)
    code, _, err = _run(["mark", "101", "screen", *args], now=LATER)
    assert code == 1 and "already at screen" in err
    code, _, err = _run(["mark", "101", "ghosted", *args], now=LATER)
    assert code == 1 and "did get a response" in err


def test_reopen_and_a_late_reply_return_to_the_last_open_stage(applied):
    args = applied["args"]
    _run(["mark", "101", "screen", *args], now=LATER + timedelta(days=1))
    _run(["mark", "101", "withdrew", *args], now=LATER + timedelta(days=2))
    code, out, _ = _run(["mark", "101", "reopen", *args], now=LATER + timedelta(days=3))
    assert code == 0 and out.endswith("reopened on Nov 11, back at screen\n")
    _run(["mark", "102", "ghosted", *args], now=LATER + timedelta(days=21))
    code, out, _ = _run(["mark", "102", "reply", *args], now=LATER + timedelta(days=25))
    assert out.endswith("reply on Dec 3, back at applied\n")
    apps = _apps(applied["db"])
    assert (apps[101].stage, apps[102].stage) == ("screen", "applied")


def test_note(applied):
    code, out, _ = _run(["note", "101", "recruiter", "is", "Sam", *applied["args"]], now=LATER)
    assert code == 0 and out == "Noted on #101 Example Corp — Senior Backend Engineer, Platform\n"
    assert _apps(applied["db"])[101].notes == "2026-11-08: recruiter is Sam"
    assert _run(["note", "555", "hi", *applied["args"]])[0] == 1


def test_apps_lists_open_ones_and_counts_closed(applied):
    args = applied["args"]
    _run(["mark", "102", "rejected", *args], now=LATER + timedelta(days=1))
    code, out, _ = _run(["apps", *args], now=LATER + timedelta(days=4))
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == "Open applications (1)"
    assert lines[2] == "  #101   applied      Nov 8     4d  Example Corp — Senior Backend Engineer, Platform"
    assert lines[3] == "  … and 1 closed (add --all)"
    _, out, _ = _run(["apps", "--all", *args], now=LATER + timedelta(days=4))
    assert out.startswith("Applications (2)") and "#102   rejected" in out


def test_apps_with_nothing_logged(feed):
    _, out, _ = _run(["apps", *feed["args"]])
    assert out.startswith("No applications yet.")


def test_apps_code_shows_the_history(applied):
    args = applied["args"]
    _run(["mark", "101", "screen", *args], now=LATER + timedelta(days=1))
    _run(["note", "101", "ask about on-call", *args], now=LATER + timedelta(days=1))
    _run(["mark", "101", "rejected", *args], now=LATER + timedelta(days=3))
    code, out, _ = _run(["apps", "101", *args])
    assert code == 0
    assert out.splitlines() == [
        "#101 Example Corp — Senior Backend Engineer, Platform",
        "  https://job-boards.greenhouse.io/examplecorp/jobs/4412983",
        "  found via Greenhouse · New matches · resume v1 · no cover letter · no referral",
        "  first seen Nov 6 · applied Nov 8 (2 days later) · logged via code",
        "  Stage: rejected since Nov 11",
        "History",
        "  2026-11-08  applied                            via cli",
        "  2026-11-09  screen                             via cli",
        "  2026-11-09  note: ask about on-call            via cli",
        "  2026-11-11  rejected (at screen)               via cli",
    ]
    assert _run(["apps", "555", *args])[0] == 1


def test_undo_removes_the_last_entry_then_the_application(applied):
    args = applied["args"]
    _run(["mark", "101", "interview", *args], now=LATER + timedelta(days=3))
    _run(["mark", "101", "screen", "--date", "2026-11-09", *args], now=LATER + timedelta(days=3))  # entered late
    code, out, _ = _run(["undo", "101", *args])
    assert code == 0 and out == "Removed screen (Nov 9) from #101; it's now at interview\n"
    _, out, _ = _run(["undo", "101", *args])
    assert out == "Removed interview (Nov 11) from #101; it's now at applied\n"
    _, out, _ = _run(["undo", "101", *args])
    assert out == "Removed #101 Example Corp, Senior Backend Engineer, Platform from the tracker\n"
    assert set(_apps(applied["db"])) == {102}
    # the code still belongs to the posting, so it can be logged again
    assert _run(["apply", "101", *args, *applied["cfg"]], now=LATER)[0] == 0
    assert _run(["undo", "555", *args])[0] == 1


# --- the stage, from events ------------------------------------------------------------

def _ev(i, kind, day, detail=None):
    return Event(id=i, code=101, at=datetime(2026, 11, day, 20, tzinfo=timezone.utc), kind=kind, origin="cli",
                 detail=detail)


def test_derive():
    applied = _ev(1, "applied", 1)
    assert derive([applied])[0] == "applied"
    assert derive([applied, _ev(2, "reply", 2)])[:2] == ("applied", applied.at)
    assert derive([applied, _ev(2, "screen", 3), _ev(3, "offer", 9), _ev(4, "accepted", 10)])[0] == "accepted"
    # dated order wins over entry order
    assert derive([applied, _ev(3, "interview", 5), _ev(4, "screen", 3)])[0] == "interview"
    assert derive([applied, _ev(2, "no_response", 22), _ev(3, "reopened", 23)])[0] == "applied"
    with pytest.raises(TrackerError):
        derive([_ev(1, "screen", 1)])


def test_parse_day():
    assert parse_day("2026-11-06", NOW) == NOW
    assert parse_day("2026-11-05", NOW) == datetime(2026, 11, 5, 12).astimezone().astimezone(timezone.utc)
    with pytest.raises(TrackerError):
        parse_day("2026-11-07", NOW)
