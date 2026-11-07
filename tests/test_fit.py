from __future__ import annotations

import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from jobfeed import fit
from jobfeed.cli import main
from jobfeed.config import Config, ConfigError, load_config
from jobfeed.fit import (Resume, ResumeError, Scorer, build_profile, clean, entries, keyword_score, load_resume,
                         parse_terms, weighted_lines)
from jobfeed.http import make_client
from jobfeed.models import Job
from jobfeed.normalize import normalize_job
from jobfeed.store import Store
from tests.conftest import router

NOW = datetime(2026, 11, 7, 19, 0, tzinfo=timezone.utc)
RESUME_PATH = "/WillySchu/pandoc_resume/master/markdown/resume.md"

# A made-up resume in the same shape as the real one (pandoc Markdown).
RESUME = """Pat Example
============

> <pat@example.com> • +1 (206) 555-0100 • Seattle, WA

**Senior Software Engineer at Acme - Seattle, WA (2019-03 - 2025-05)**

- Built Go microservices on Kubernetes in AWS, backed by PostgreSQL and
  Redis, with Grafana dashboards.

**Software Engineer at Initech - Seattle, WA (2016-08 - 2019-03)**

- Wrote Python data pipelines.

Programming Languages

:   **Python:** 10+ years.

:   **JavaScript:** 3 years with some front end work in Angular and
    React as well as Node.

:   Basic knowledge of **C**, **Ruby**
"""


def resume(text=RESUME) -> Resume:
    return Resume(clean(text), "abc123", "test")


def scorer(config: Config | None = None) -> Scorer:
    return Scorer(config or Config(), resume(), NOW)


def job(title="Senior Backend Engineer", description="", **kw) -> Job:
    return normalize_job(Job(id=kw.pop("id", "greenhouse:1"), source="greenhouse", company="Co", title=title,
                             url="https://example.com/1", description=description, **kw))


# --- the resume ----------------------------------------------------------------------------------

def test_clean_drops_contact_lines_but_keeps_dates():
    text = clean(RESUME)
    assert "pat@example.com" not in text and "555-0100" not in text
    assert "(2019-03 - 2025-05)" in text and "(2016-08 - 2019-03)" in text


def test_entries_join_wrapped_lines():
    joined = entries(clean(RESUME))
    assert "- Built Go microservices on Kubernetes in AWS, backed by PostgreSQL and Redis, with Grafana dashboards." \
        in joined
    assert ":   **JavaScript:** 3 years with some front end work in Angular and React as well as Node." in joined


def test_profile_reads_skills_strength_and_years():
    p = build_profile(resume(), parse_terms(Config().fit.terms), Config().fit, NOW)
    assert p.years == 10                                   # from the 2016 start date
    strong = {n for n, v in p.skills.items() if v == 1.0}
    basic = {n for n, v in p.skills.items() if v == 0.5}
    assert {"Go", "Kubernetes", "AWS", "PostgreSQL", "Redis", "Grafana", "Python", "Microservices",
            "Data pipelines", "SQL", "Docker", "Observability"} <= strong      # the last three implied
    assert {"C", "Ruby", "Angular", "React", "JavaScript"} <= basic           # "Basic knowledge", "some front end"


def test_profile_overrides(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("fit:\n  strong: [javascript]\n  basic: [Go]\n  extra_skills: [Kafka]\n  experience_years: 12\n")
    config = load_config(cfg)
    p = build_profile(resume(), parse_terms(config.fit.terms), config.fit, NOW)
    assert (p.skills["JavaScript"], p.skills["Go"], p.skills["Kafka"], p.years) == (1.0, 0.5, 1.0, 12)


@pytest.mark.parametrize("text, kind", [
    ("fit:\n  terms: ['Bad: (unclosed']\n", "bad pattern"),
    ("fit:\n  strong: [COBOL]\n", "aren't in `fit.terms`"),
])
def test_fit_config_is_checked(tmp_path, text, kind):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(text)
    with pytest.raises(ConfigError, match=kind):
        load_config(cfg)


@pytest.mark.parametrize("text, found", [
    ("We write Go and Python", {"Go", "Python"}),
    ("Ready to go to market fast", set()),                    # go, the verb
    ("golang services", {"Go"}),
    ("C++ and C# and C", {"C++", "C#", "C"}),
    ("our Series C round, C-level support", set()),
    ("the rest of the team", set()),
    ("RESTful APIs", {"REST", "API design"}),
    ("PostgreSQL, not MySQL", {"PostgreSQL", "MySQL"}),
    ("JavaScript (Node.js)", {"JavaScript"}),
    ("Java 17", {"Java"}),
])
def test_term_patterns(text, found):
    terms = parse_terms(Config().fit.terms)
    assert {t.name for t in terms if t.pattern.search(text)} == found


def test_weighted_lines_follow_headings():
    lines = dict(weighted_lines(job(description=(
        "About us\nWe build things with Kafka.\n\nRequirements:\n- Go\n- Rust is a plus\n\n"
        "Nice to have:\n- Terraform\n"))))
    assert lines["Senior Backend Engineer"] == 2                  # the title counts as a requirement
    assert (lines["We build things with Kafka."], lines["- Go"], lines["- Rust is a plus"],
            lines["- Terraform"]) == (1, 2, 1, 1)


# --- the score ------------------------------------------------------------------------------------

def test_a_strong_match_and_a_poor_one():
    s = scorer()
    good = s.score(job(description="Requirements:\n- 5+ years of Go and Python\n- PostgreSQL, Redis, Kubernetes, AWS\n"
                                   "Nice to have:\n- Kafka"))
    poor = s.score(job(title="Senior Engineer", description="Requirements:\n- 10+ years of C++ and Rust\n- Linux kernel, "
                                                         "Kafka, Spark"))
    assert good.score > 80 and good.score - poor.score > 30
    # level, years and focus give a C++ role 40 of its points; its skills part is what sinks it
    assert poor.working["parts"]["skills"] < 15
    assert good.working["matched"] == ["AWS", "Go", "Kubernetes", "PostgreSQL", "Python", "Redis"]
    assert good.working["missing"] == ["Kafka"] and good.working["required"][0] == "AWS"
    # skills: 60 x (12 had + 4 x 0.5) / (13 asked + 4) = 49.4; focus: 4 backend terms, no frontend
    assert good.working["parts"] == {"skills": 49, "level": 20, "years": 10, "focus": 10}
    assert poor.working["years_asked"] == 10 and poor.working["parts"]["years"] == 10   # 10 years: enough
    assert (good.scorer, good.version, good.reason) == ("keywords", "keywords-1", None)


@pytest.mark.parametrize("title, points", [
    ("Senior Backend Engineer", 20), ("Staff Engineer", 20), ("Backend Engineer II", 10),
    ("Principal Engineer", 10), ("Junior Developer", 0), ("Backend Engineer", 12),
])
def test_level_points(title, points):
    assert scorer().score(job(title=title)).working["parts"]["level"] == points


def test_years_focus_and_thin_evidence():
    s = scorer()
    assert s.score(job(description="12+ years of experience")).working["parts"]["years"] == 0
    assert s.score(job(description="3 years of experience")).working["parts"]["years"] == 10
    assert s.score(job(description="")).working["parts"] == {"skills": 30, "level": 20, "years": 10, "focus": 8}
    mixed = s.score(job(description="backend and frontend: React, CSS, Go, kubernetes"))
    assert mixed.working["backend_frontend"] == [2, 3] and mixed.working["parts"]["focus"] == 4
    one = s.score(job(title="Senior Engineer", description="Requirements:\n- Go")).working["parts"]["skills"]
    many = s.score(job(title="Senior Engineer", description="Requirements:\n- Go\n- Python\n- AWS\n- Redis\n- Kubernetes"
                       "\n- PostgreSQL")).working["parts"]["skills"]
    assert 30 < one < many < 60          # one matching term says less than six


# --- fetching the resume -----------------------------------------------------------------------------

def _transport(text: str | None = RESUME, status: int = 200):
    return router({RESUME_PATH: lambda r: httpx.Response(status, text=text or "")})


def test_load_resume_fetches_cleans_and_caches(tmp_path, no_sleep):
    with make_client(_transport()) as client:
        r = load_resume(Config().fit, tmp_path, client)
    assert "pat@example.com" not in r.text and len(r.hash) == 12
    assert (tmp_path / "resume-cache.md").read_text() == r.text
    with make_client(_transport(status=503)) as client:
        again = load_resume(Config().fit, tmp_path, client)          # GitHub down: the local copy
    assert again.hash == r.hash and "couldn't fetch" in again.source
    with make_client(_transport(status=404)) as client:
        with pytest.raises(ResumeError, match="no local copy"):
            load_resume(Config().fit, tmp_path / "elsewhere", client)


# --- in the run, and the commands ------------------------------------------------------------------------

@pytest.fixture
def pi(tmp_path, greenhouse_jobs):
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies: []\n")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("tracking:\n  read_mail: false\n")
    state = {"resume": 200}

    def handle_resume(r):
        return httpx.Response(state["resume"], text=RESUME)

    transport = router({"/v1/boards/examplecorp/jobs": lambda r: httpx.Response(200, json=greenhouse_jobs),
                        RESUME_PATH: handle_resume})

    class Pi:
        db = tmp_path / "jobs.db"
        args = ["--db", str(tmp_path / "jobs.db"), "--config", str(cfg)]

        def main(self, argv, day=0):
            out, err = io.StringIO(), io.StringIO()
            code = main(argv, transport=transport, out=out, err=err, now=NOW + timedelta(days=day))
            return code, out.getvalue(), err.getvalue()

        def run(self, day=0, *extra):
            return self.main(["run", "--no-email", *extra, "--companies", str(companies), *self.args,
                              "--board", "greenhouse:examplecorp"], day)

        def scores(self):
            conn = sqlite3.connect(self.db)
            return {(j, s): (v, sc, h, json.loads(w)) for j, s, v, sc, h, w in conn.execute(
                "SELECT job_id, scorer, version, score, resume_hash, working FROM fit_scores")}

    p = Pi()
    p.cfg, p.state = cfg, state
    return p


def test_the_run_scores_the_digest_postings_once_and_hides_them(pi, no_sleep):
    code, out, err = pi.run()
    assert code == 0
    assert "Fit scores: 2 postings scored by keywords; LLM scoring off: no ANTHROPIC_API_KEY in .env (hidden for now)." \
        in err
    scores = pi.scores()
    assert set(scores) == {("greenhouse:4412983", "keywords"), ("greenhouse:4412990", "keywords")}
    version, score, resume_hash, working = scores[("greenhouse:4412983", "keywords")]
    assert version == "keywords-1" and 0 <= score <= 100 and len(resume_hash) == 12 and "parts" in working
    assert "score" not in out.lower() and "/100" not in out             # nothing shown in the digest
    _, _, err = pi.run(1)
    assert "Fit scores" not in err                                    # already scored: nothing to do
    assert (pi.db.parent / "resume-cache.md").exists()


def test_a_dry_run_scores_nothing_and_a_missing_resume_never_stops_the_run(pi, no_sleep):
    pi.run(0, "--dry-run")
    assert not pi.db.exists()
    pi.state["resume"] = 404
    code, _, err = pi.run()
    assert code == 0 and "Fit scores: none this run; couldn't fetch the resume" in err
    assert pi.scores() == {}
    pi.state["resume"] = 200
    pi.main(["fit", "backfill", *pi.args])
    assert len(pi.scores()) == 2


def test_scoring_can_be_turned_off(pi):
    pi.cfg.write_text("tracking:\n  read_mail: false\nfit:\n  enabled: false\n")
    pi.run()
    assert pi.scores() == {}


def test_fit_commands(pi, no_sleep):
    pi.state["resume"] = 404
    pi.run()
    pi.state["resume"] = 200
    _, out, _ = pi.main(["fit", "status", *pi.args])
    assert out.splitlines() == ["Postings sent: 2", "  keywords  0 scored", "  llm       0 scored",
                                "  2 sent postings have no keyword score yet; `jobfeed fit backfill` scores them",
                                "LLM spend this month: $0.00 of $5.00"]
    code, out, _ = pi.main(["fit", "backfill", *pi.args])
    assert code == 0 and out.startswith("Scored 2 of 2 postings sent by keywords")
    _, out, _ = pi.main(["fit", "backfill", *pi.args])
    assert out.startswith("Scored 0 of 2 postings sent by keywords")
    _, out, _ = pi.main(["fit", "status", *pi.args])
    assert "  keywords  2 scored" in out and out.rstrip().splitlines()[-1].startswith("Resume ")
    code, out, _ = pi.main(["fit", "profile", *pi.args])
    assert code == 0
    assert out.splitlines()[1:3] == [
        "Experience: 10 years",
        "Strong (12): AWS, Data pipelines, Docker, Go, Grafana, Kubernetes, Microservices, Observability, PostgreSQL, "
        "Python, Redis, SQL"]
    for argv in (["fit", "status"], ["fit", "backfill"], ["fit", "profile"]):
        assert "keyword score:" not in pi.main([*argv, *pi.args])[1]      # never a posting's score


def test_schema_6(tmp_path):
    with Store.open(tmp_path / "jobs.db"):
        pass
    conn = sqlite3.connect(tmp_path / "jobs.db")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"fit_scores", "fit_ratings"} <= tables
    assert "fit_keywords" in {r[1] for r in conn.execute("PRAGMA table_info(applications)")}


def test_extra_terms_add_to_the_list(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("fit:\n  extra_terms: ['Ray: ray']\n  extra_skills: [Ray]\n")
    config = load_config(cfg)
    s = Scorer(config, resume(), NOW)
    assert s.profile.skills["Ray"] == 1.0
    assert "Ray" in s.score(job(description="Requirements:\n- Ray clusters")).working["matched"]
    assert load_config(Path("config.yaml")).fit.resume_url.endswith("/markdown/resume.md")   # the repo's own
