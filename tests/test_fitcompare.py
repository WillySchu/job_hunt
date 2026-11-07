from __future__ import annotations

import io
import random
from datetime import datetime, timedelta, timezone

import pytest

from jobfeed import fitcompare
from jobfeed.cli import main
from jobfeed.fit import Resume, Score, save_score
from jobfeed.fitcompare import paired_difference, pearson, ranks, report, rho_range, spearman, strength
from jobfeed.models import Job
from jobfeed.store import Store, _ts

NOW = datetime(2026, 12, 20, 19, 0, tzinfo=timezone.utc)


# --- the statistics (expected values from scipy.stats.spearmanr) -----------------------------------------

def test_ranks_and_spearman():
    assert ranks([3, 1, 3, 2]) == [3.5, 1.0, 3.5, 2.0]
    assert spearman([1, 2, 3, 4, 5], [5, 6, 7, 8, 7]) == pytest.approx(0.8207826816681233)
    assert spearman([1, 1, 2, 3, 5, 5], [10, 20, 20, 40, 30, 60]) == pytest.approx(0.8508410434878082)
    assert pearson([1, 1, 1], [1, 2, 3]) is None and spearman([1, 2], [2, 1]) is None


def test_rho_range_and_words():
    lo, hi = rho_range(0.6, 40)
    assert lo < 0.6 < hi and hi - lo < rho_range(0.6, 15)[1] - rho_range(0.6, 15)[0]
    assert [strength(r) for r in (0.8, 0.5, 0.25, 0.1, -0.5)] == [
        "strong", "moderate", "weak", "little or none", "moderate (and backwards)"]


def test_paired_difference_finds_the_better_scorer():
    rng = random.Random(1)
    rows = []
    for _ in range(40):
        truth = rng.random()
        rows.append((round(1 + 4 * truth), truth * 100 + rng.gauss(0, 10), rng.random() * 100))
    d, lo, hi = paired_difference(rows)
    assert d > 0 and lo > 0 and hi > lo
    same = [(r, a, a) for r, a, _ in rows]
    d, lo, hi = paired_difference(same)
    assert d == 0 and lo == 0 and hi == 0


# --- reports over a jobs.db ---------------------------------------------------------------------------

def _seed(db, n=40, rated=0, sent_days_ago=lambda i: 20, llm=None, kw=None, seed=5):
    """n sent postings with both scores; the first `rated` rated in line with the LLM score."""
    rng = random.Random(seed)
    r = Resume("resume", "h", "s")
    with Store.open(db) as st:
        for i in range(n):
            sent = NOW - timedelta(days=sent_days_ago(i))
            job = Job(id=f"greenhouse:{i}", source="greenhouse", company=f"Co {i}", title="Senior Backend Engineer",
                      url=f"https://example.com/{i}", location_raw="Remote (US)", work_mode="remote",
                      seniority="senior", description="About us\nWe build things.\nRequirements:\n- Go\n- Kubernetes")
            st.save_jobs([(job, f"k{i}", True)], sent)
            st.mark_sent([job.id], sent)
            st.assign_codes([(job.id, "matches")])
            truth = rng.random()
            with st.conn:
                save_score(st, job.id, Score("llm", "v", llm(i) if llm else round(truth * 100), f"why {i}", {}), r,
                           sent)
                save_score(st, job.id, Score("keywords", "k", kw(i) if kw else rng.randint(40, 90), None,
                                             {"matched": ["Go", "Kubernetes"], "missing": ["Rust"]}), r, sent)
                if i < rated:
                    st.conn.execute("INSERT INTO fit_ratings VALUES (?, ?, ?)",
                                    (job.id, max(1, min(5, round(1 + 4 * truth))), _ts(NOW)))


def _compare(db):
    out = io.StringIO()
    assert main(["fit", "compare", "--db", str(db)], out=out, err=io.StringIO(), now=NOW) == 0
    return out.getvalue().splitlines()


def test_compare_with_ratings(tmp_path):
    db = tmp_path / "jobs.db"
    _seed(db, rated=40)
    lines = _compare(db)
    assert lines[0] == "Agreement between the scorers (40 postings with both scores)"
    assert lines[1].startswith("  rank correlation ") and lines[2].startswith("  LLM:      median ")
    assert lines[5] == "Against your ratings (40 rated)"
    assert lines[6].startswith("  LLM       0.9") and lines[6].endswith(": strong")
    assert lines[8].startswith("  → the LLM tracks your ratings better: by ")


def test_compare_needs_ratings_and_hides_recent_disagreements(tmp_path):
    db = tmp_path / "jobs.db"
    _seed(db, n=20, rated=3, sent_days_ago=lambda i: 3 if i < 10 else 20,
          llm=lambda i: 10 if i in (0, 15) else 60, kw=lambda i: 90 if i in (0, 15) else 60)
    lines = _compare(db)
    assert "  too few to tell: 15 ratings are needed; `jobfeed fit rate` asks for them" in lines
    i = lines.index("Where they disagree most (postings first sent over 14 days ago; 10 so far)")
    assert lines[i + 1] == "  Senior Backend Engineer — Co 15 (sent Nov 30)"           # Co 0 is too recent
    assert lines[i + 2:i + 4] == ["    LLM 10: why 15", "    keywords 90: found Go, Kubernetes; missing Rust"]
    assert "Co 0 " not in "\n".join(lines)


def test_compare_with_nothing(tmp_path):
    lines = _compare(tmp_path / "jobs.db")
    assert lines[1] == "  not enough postings with both scores yet" and lines[-1] == "  none yet"


# --- rating -------------------------------------------------------------------------------------------------

def _rate(db, answers, count=40):
    out = io.StringIO()
    code = main(["fit", "rate", "--count", str(count), "--db", str(db)], out=out, err=io.StringIO(), now=NOW,
                inp=io.StringIO(answers))
    assert code == 0
    return out.getvalue()


def _ratings(db):
    import sqlite3
    return dict(sqlite3.connect(db).execute("SELECT job_id, rating FROM fit_ratings"))


def test_rating_session(tmp_path):
    db = tmp_path / "jobs.db"
    _seed(db, n=6, sent_days_ago=lambda i: 40 if i == 5 else 5)          # #5 is too old to rate
    out = _rate(db, "4\nmaybe\ns\n2\nq\n")
    assert out.startswith("How strong a candidate are you for each? 1 (not at all) to 5 (an excellent fit).")
    assert "[1/5] Senior Backend Engineer — Co " in out and "  | Requirements:" in out
    assert out.count("rating (1-5, s, q): ") == 5                         # "maybe" asks again
    assert out.rstrip().endswith("Rated 2 this time, 2 in all. `jobfeed fit compare` shows how the scorers track "
                                 "your ratings.")
    assert sorted(_ratings(db).values()) == [2, 4]
    assert "why " not in out and "LLM" not in out and "keywords" not in out    # no scores, no reasons
    out = _rate(db, "")                                                     # end of input: stops cleanly
    assert "Rated 0 this time, 2 in all." in out
    _rate(db, "1\n1\n1\n1\n")
    assert len(_ratings(db)) == 5
    assert _rate(db, "").startswith("Nothing to rate: every posting sent in the last 30 days has a rating.")


def test_rating_order_is_not_the_score_order(tmp_path):
    db = tmp_path / "jobs.db"
    _seed(db, n=30, sent_days_ago=lambda i: 5, llm=lambda i: i * 3)
    with Store.open(db) as st:
        order = [p.llm for p in fitcompare.to_rate(fitcompare.load(st), NOW)]
    assert sorted(order) == list(range(0, 90, 3)) and order != sorted(order) and order != sorted(order, reverse=True)
