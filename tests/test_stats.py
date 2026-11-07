from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pytest

from jobfeed import stats, tracker
from jobfeed.cli import main
from jobfeed.config import TrackingRules
from jobfeed.stats import CUTS, Rate, Summary, facts_for, pct, summarize, verdict, wilson
from jobfeed.store import Store
from jobfeed.tracker import Application, Event, LogOptions

NOW = datetime(2026, 12, 1, 19, 0, tzinfo=timezone.utc)
RULES = TrackingRules()


# --- the numbers -------------------------------------------------------------------------------

def test_wilson_matches_the_spec_example_and_the_edges():
    assert Rate(3, 24).text("replied") == "3 of 24 replied (13%, likely 5–27%)"
    lo, hi = wilson(0, 10)
    assert lo == 0 and pct(hi) == 21
    lo, hi = wilson(10, 10)
    assert pct(lo) == 79 and hi == 1
    assert wilson(0, 0) == (0.0, 1.0) and Rate(0, 0).text() == "none counted yet"
    assert pct(0.125) == 13 and pct(0.124) == 12


def _app(stage="applied", days_ago=30, events=(), **kw) -> Application:
    applied = NOW - timedelta(days=days_ago)
    base = dict(code=101, job_id=None, company="Acme", title="SRE", url=None, found_via="other",
                digest_section=None, referral=False, resume_version="v1", cover_letter=False, autofilled=None,
                fit_score=None, first_seen=None, applied_at=applied, logged_via="code", stage=stage,
                stage_at=applied, notes=None)
    app = Application(**{**base, **kw})
    app.events = [Event(id=1, code=101, at=applied, kind="applied", origin="cli")] + [
        Event(id=i + 2, code=101, at=applied + timedelta(days=d), kind=k, origin=o, detail=det)
        for i, (k, d, o, det) in enumerate(events)]
    return app


@pytest.mark.parametrize("stage, events, replied, reached, days", [
    ("applied", [], False, 0, None),
    ("applied", [("reply", 4, "cli", None)], True, 0, 4),
    ("applied", [("confirmation", 0, "email", None)], False, 0, None),            # a robot's thanks isn't a reply
    ("no_response", [("no_response", 21, "rule", "no reply in 21 days")], False, 0, None),
    ("rejected", [("rejected", 9, "email", "applied")], True, 0, 9),              # a rejection is a reply
    ("rejected", [("screen", 5, "cli", None), ("rejected", 12, "cli", "screen")], True, 1, 5),
    ("withdrawn", [("withdrawn", 3, "cli", "interview")], False, 2, None),
    ("accepted", [("offer", 20, "cli", None), ("accepted", 22, "cli", None)], True, 3, 20),
])
def test_facts(stage, events, replied, reached, days):
    f = facts_for(_app(stage, events=events), None, NOW, RULES)
    assert (f.replied, f.reached, f.days_to_reply) == (replied, reached, days)


def test_only_resolved_applications_count():
    assert facts_for(_app("applied", days_ago=21), None, NOW, RULES).counted
    assert not facts_for(_app("applied", days_ago=20), None, NOW, RULES).counted
    assert not facts_for(_app("screen", days_ago=5), None, NOW, RULES).counted
    assert facts_for(_app("rejected", days_ago=5), None, NOW, RULES).counted
    assert not facts_for(_app("applied", days_ago=8), None, NOW, TrackingRules(no_response_days=10)).counted


def test_summary():
    facts = [facts_for(_app("rejected", events=[("rejected", 3, "cli", "applied")]), None, NOW, RULES),
             facts_for(_app("interview", events=[("screen", 6, "cli", None), ("interview", 9, "cli", None)]),
                       None, NOW, RULES),
             facts_for(_app("applied"), None, NOW, RULES),
             facts_for(_app("applied", days_ago=2), None, NOW, RULES)]
    s = summarize(facts)
    assert (s.total, s.open, s.counted) == (4, 3, 3)
    assert {k: (r.k, r.n) for k, r in s.rates.items()} == {
        "replied": (2, 3), "screen": (1, 3), "interview": (1, 3), "offer": (0, 3)}
    assert s.median_days == 4.5


def _summary(k, n):
    return Summary(total=n, open=0, counted=n, rates={"replied": Rate(k, n), "screen": Rate(0, n),
                                                      "interview": Rate(0, n), "offer": Rate(0, n)})


@pytest.mark.parametrize("groups, says", [
    ([("v1", _summary(3, 14)), ("v2", _summary(13, 16))], "too few to tell: v1 has 14 counted; 15 each are needed"),
    ([("v1", _summary(5, 20)), ("v2", _summary(7, 20))], "too few to tell: v2 (35%) and v1 (25%) are within each "
                                                          "other's likely ranges"),
    ([("v1", _summary(3, 30)), ("v2", _summary(15, 30))], "v2 does better on replied: 50% vs 10% for v1"),
    ([("v1", _summary(3, 30))], "too few to tell: only one group has applications old enough to count"),
    ([("v1", _summary(3, 30)), ("v2", _summary(0, 0))], "too few to tell: only one group"),
])
def test_verdicts(groups, says):
    assert verdict(groups, "replied").startswith(says)


def test_cut_groups():
    by = {c.key: c for c in CUTS}
    f = facts_for(_app(first_seen=NOW - timedelta(days=35), referral=True, cover_letter=True, resume_version=None,
                       found_via="remoteok", digest_section="no_salary", fit_score=70, fit_keywords=49),
                  None, NOW, RULES)
    assert {k: by[k].group(f) for k in ("via", "section", "referral", "resume", "cover", "autofilled", "fit_llm",
                                        "fit_keywords", "days", "seniority", "mode", "week")} == {
        "via": "Remote OK", "section": "No salary listed", "referral": "yes", "resume": "unrecorded",
        "cover": "yes", "autofilled": "unknown", "fit_llm": "70+", "fit_keywords": "under 50", "days": "4–7 days",
        "seniority": "unknown", "mode": "unknown", "week": "week of Oct 26"}
    assert by["fit_llm"].group(facts_for(_app(), None, NOW, RULES)) == "unscored"


# --- jobfeed stats -------------------------------------------------------------------------------------

NOV10 = datetime(2026, 11, 10, 19, 0, tzinfo=timezone.utc)   # Oct 1 to Oct 20 are 21+ days old


def _run(argv, now=NOW):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, out=out, err=err, now=now)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def search(tmp_path):
    """40 applications from Oct 1, alternating resumes: v2 gets a reply from every second, v1 from every tenth."""
    db = tmp_path / "jobs.db"
    start = datetime(2026, 10, 1, 18, tzinfo=timezone.utc)
    with Store.open(db) as store:
        for i in range(40):
            at = start + timedelta(days=i)
            v2 = i % 2 == 1
            app = tracker.log_manual(store, url=None, company=f"Co{i}", title="Backend Engineer", via="other",
                                     opts=LogOptions(applied_at=at, resume_version="v2" if v2 else "v1",
                                                     cover_letter=False))
            if (v2 and i % 4 == 1) or (not v2 and i % 20 == 0):
                tracker.mark(store, app.code, "screen", at + timedelta(days=3))
    return db


def test_stats_report(search):
    code, out, _ = _run(["stats", "--db", str(search)], NOV10)
    assert code == 0
    lines = out.splitlines()
    # counted: Oct 1-20, i = 0..19; screens: v2 at i = 1, 5, 9, 13, 17 and v1 at i = 0
    assert lines[:6] == [
        "Applications: 40 · 40 open · 20 counted in rates (at least 21 days old, or closed)",
        "  Replied               6 of 20 (30%, likely 16–48%)",
        "  Screen or further     6 of 20 (30%, likely 16–48%)",
        "  Interview or further  0 of 20 (0%, likely 0–12%)",
        "  Offer or further      0 of 20 (0%, likely 0–12%)",
        "  Days to first reply   median 3 (from 12 replies)",
    ]
    assert "Resume version" in lines and "Week applied" in lines
    assert "Referral" not in lines                       # one group only: not shown by default
    assert "  → too few to tell: v2 has 10 and v1 has 10 counted; 15 each are needed" in lines


def test_stats_by_and_metric_and_since(search):
    _, out, _ = _run(["stats", "--by", "resume", "--by", "referral", "--metric", "screen", "--db", str(search)], NOV10)
    assert [l for l in out.splitlines() if l and not l.startswith((" ", "Applications", "Funnel"))] == [
        "Referral", "Resume version"]
    _, out, _ = _run(["stats", "--by", "all", "--db", str(search)])
    assert all(c.label in out for c in CUTS)
    _, out, _ = _run(["stats", "--since", "2026-11-01", "--db", str(search)], NOV10)
    assert out.startswith("Applications: 9 · 9 open · 0 counted in rates")
    assert "None is old enough to count yet; the first counts on Nov 22." in out
    assert _run(["stats", "--since", "Nov 1", "--db", str(search)], NOV10)[0] == 2


def test_stats_with_volume_can_tell(tmp_path):
    db = tmp_path / "jobs.db"
    start = datetime(2026, 9, 1, 18, tzinfo=timezone.utc)
    with Store.open(db) as store:
        for i in range(60):
            v2 = i % 2 == 1
            app = tracker.log_manual(store, url=None, company=f"Co{i}", title="SRE", via="other",
                                     opts=LogOptions(applied_at=start + timedelta(days=i // 3),
                                                     resume_version="v2" if v2 else "v1", cover_letter=False))
            if (v2 and i % 4 != 3) or (not v2 and i % 10 == 0):
                tracker.mark(store, app.code, "reply", start + timedelta(days=i // 3 + 2))
    _, out, _ = _run(["stats", "--by", "resume", "--db", str(db)])
    # v2 replies at odd i with i % 4 == 1 (15 of 30); v1 at i = 0, 10, ..., 50 (6 of 30)
    assert "  → v2 does better on replied: 50% vs 20% for v1" in out


def test_stats_with_nothing(tmp_path):
    _, out, _ = _run(["stats", "--db", str(tmp_path / "jobs.db")])
    assert out.startswith("No applications yet.")


def test_nothing_counted_yet_shows_no_empty_splits_unless_asked(search):
    early = datetime(2026, 10, 15, 19, 0, tzinfo=timezone.utc)
    _, out, _ = _run(["stats", "--db", str(search)], early)
    assert "Resume version" not in out and "Funnel" in out and "Longest without news" in out
    _, out, _ = _run(["stats", "--by", "resume", "--db", str(search)], early)
    assert "  → too few to tell: none is old enough to count yet" in out


# --- fit scores on applications (fit scorer milestone 3) --------------------------------------------------

def _scored(n, *, llm, kw, replied):
    """n counted applications whose scores and replies follow the given functions of i."""
    return [facts_for(_app(fit_score=llm(i), fit_keywords=kw(i),
                           events=[("reply", 3, "cli", None)] if replied(i) else []), None, NOW, RULES)
            for i in range(n)]


def test_fit_comparison_tells_a_predictive_score_from_a_useless_one():
    # the LLM's top half gets replies 40% of the time and its bottom half 10%; keywords split them evenly
    facts = _scored(60, llm=lambda i: 90 - i, kw=lambda i: 40 + (i * 7) % 50,
                    replied=lambda i: (i < 30 and i % 5 < 2) or (i >= 30 and i % 10 == 0))
    lines = stats.fit_lines(facts, "replied")
    assert lines[0] == "Which fit score predicts a reply? (counted applications, split at each score's median)"
    assert lines[1:3] == ["  LLM top half          12/30 40% (27–55%)", "  LLM bottom half       3/30 10% (4–23%)"]
    assert lines[5] == "  → LLM (median 60.5): its top half does better: 40% vs 10%"
    assert lines[6].startswith("  → keywords (median ") and "too few to tell" in lines[6]
    assert lines[7] == "  So far only the LLM score separates the applications that get a reply."


def test_fit_comparison_with_few_applications_or_a_backwards_score():
    few = _scored(20, llm=lambda i: 90 - i, kw=lambda i: 50, replied=lambda i: i < 5)
    assert "too few to tell: 10 and 10 counted; 15 in each half are needed" in stats.fit_lines(few, "replied")[5]
    backwards = _scored(60, llm=lambda i: i, kw=lambda i: 50, replied=lambda i: i < 30 and i % 5 < 2)
    assert "its bottom half does better (40% vs 0%): this score points the wrong way" in \
        stats.fit_lines(backwards, "replied")[5]
    assert stats.fit_lines(_scored(10, llm=lambda i: None, kw=lambda i: None, replied=lambda i: True), "replied") == []


def test_fit_section_appears_in_the_report_once_scores_count():
    facts = _scored(30, llm=lambda i: 90 - i, kw=lambda i: 60, replied=lambda i: i < 10)
    text = "\n".join(stats.report(facts, RULES, now=NOW))
    assert "Fit score (LLM)" in text and "Fit score (keywords)" not in text      # keywords: one band only
    assert "Which fit score predicts a reply?" in text
    fresh = [facts_for(_app(days_ago=2, fit_score=80), None, NOW, RULES)]
    assert "Which fit score" not in "\n".join(stats.report(fresh, RULES, now=NOW))   # nothing counted yet
