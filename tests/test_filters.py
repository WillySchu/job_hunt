from __future__ import annotations

from datetime import datetime, timezone

import pytest

from jobfeed.config import Config
from jobfeed.filters import FULL_STACK_TAG, MATCHES, NO_SALARY, WATCHLIST, Rules, build_digest
from jobfeed.models import Job
from jobfeed.normalize import normalize_job


def job(title="Senior Backend Engineer", location="Remote (US)", description="", company="Acme", **kw) -> Job:
    """A normalized Job, the way fetch_board hands them to the filters."""
    base = dict(id=f"greenhouse:{abs(hash((title, location, description, company)))}", source="greenhouse",
                company=company, title=title, url="https://example.com", location_raw=location,
                description=description)
    return normalize_job(Job(**{**base, **kw}))


@pytest.fixture
def rules() -> Rules:
    return Rules(Config())


# --- role --------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Senior Backend Engineer",
    "Staff Software Engineer, Infrastructure",
    "Site Reliability Engineer",
    "SRE II",
    "Platform Developer",
    "Member of Technical Staff (Software Engineer, API Platform)",
    "Data Engineer",
    "Software Engineer, Distributed Systems (Rust)",
    "Software Engineering Lead, Payments",
])
def test_role_passes(rules, title):
    assert rules.check_role(job(title=title)) is None


@pytest.mark.parametrize("title,reason", [
    ("Frontend Engineer", "title says 'Frontend'"),
    ("Senior Front End Engineer", "title says 'Front End'"),
    ("Full Stack Engineer", "title says 'Full Stack'"),
    ("Staff Software Engineer, Full Stack - Gen AI", "title says 'Full Stack'"),
    ("Fullstack Developer", "title says 'Fullstack'"),
    ("Engineering Manager, Platform", "title says 'Manager'"),
    ("Director, Developer Platform & Experience", "title says 'Director'"),
    ("Software Engineering Intern (2027)", "title says 'Intern'"),
    ("Sales Engineer", "title says 'Sales Engineer'"),
    ("Solutions Engineer", "title says 'Solutions Engineer'"),
    ("Mechanical Engineer", "title says 'Mechanical'"),
    ("Engineering Recruiter", "title says 'Recruiter'"),
    ("Product Designer", "title has no engineering keyword"),
    ("Senior Leadership Recruiter", "title has no engineering keyword"),
])
def test_role_drops(rules, title, reason):
    assert rules.check_role(job(title=title)) == reason


def test_storefront_is_not_frontend(rules):
    assert rules.check_role(job(title="Backend Engineer, Storefront")) is None


def test_full_stack_tag_when_description_leans_frontend(rules):
    heavy = "React, Redux and CSS daily. Next.js and Tailwind too. Some Python."
    assert FULL_STACK_TAG in rules.evaluate(job(title="Software Engineer", description=heavy)).tags
    light = "Go... sorry, Golang and Rust services, Kafka, Postgres, Kubernetes. A little React."
    assert rules.evaluate(job(title="Software Engineer", description=light)).tags == []


# --- location ----------------------------------------------------------------

@pytest.mark.parametrize("location,description", [
    ("Remote (US)", ""),
    ("Remote - North America", ""),
    ("Remote (Anywhere)", ""),
    ("Remote", ""),                                   # region unstated
    ("Seattle, WA", ""),
    ("Hybrid - Bellevue, WA", ""),
    ("Redmond, Washington", ""),
    ("Seattle", ""),                                  # no state is fine
    ("US - Seattle", ""),
    ("Remote - CA, WA, NY", ""),
    ("Remote", "We can hire in the following states: CA, OR, WA."),
    ("Seattle, WA; Remote - Canada", ""),             # remote option fails, Seattle office doesn't
    ("Multiple locations: Seattle, WA; New York, NY", ""),
])
def test_location_passes(rules, location, description):
    assert rules.check_location(job(location=location, description=description)) is None


@pytest.mark.parametrize("location,description,reason", [
    ("Remote - Canada", "", "remote is limited to region 'other'"),
    ("Remote - EMEA", "", "remote is limited to region 'other'"),
    ("London, UK; Remote", "", "remote is limited to region 'other'"),
    ("Remote - CA, CO, NY", "", "remote is limited to CA, CO, NY"),
    ("Remote", "We can only hire in the following states: CA, NY.", "remote is limited to CA, NY"),
    ("Remote - US (excluding WA, NY)", "", "posting says it can't hire in WA"),
    ("Remote (US)", "We are unable to hire in the following states: Washington, Alaska.",
     "posting says it can't hire in WA"),
    ("San Francisco, CA", "", "onsite outside the Seattle area (San Francisco, CA)"),
    ("Hybrid - New York, NY", "", "hybrid outside the Seattle area (New York, NY)"),
    ("Bellevue, NE", "", "onsite outside the Seattle area (Bellevue, NE)"),
    ("Vancouver, BC", "", "onsite outside the Seattle area (Vancouver, Canada)"),
    ("Multiple locations", "", "location unclear (Multiple locations)"),
    ("", "", "location unclear (none given)"),
])
def test_location_drops(rules, location, description, reason):
    assert rules.check_location(job(location=location, description=description)) == reason


def test_unknown_mode_passes_when_text_names_metro_city(rules):
    j = job(location="Seattle office")
    j.work_mode, j.cities = "unknown", []   # as if a parser missed it
    assert rules.check_location(j) is None


def test_home_state_and_metro_come_from_config():
    cfg = Config()
    cfg.location.home_state = "OR"
    cfg.location.metro = ["Portland"]
    r = Rules(cfg)
    assert r.check_location(job(location="Portland, OR")) is None
    assert r.check_location(job(location="Seattle, WA")) is not None
    assert r.check_location(job(location="Remote - CA, OR")) is None


def test_remote_regions_from_config():
    cfg = Config()
    cfg.location.remote_regions = ["us"]
    r = Rules(cfg)
    assert r.check_location(job(location="Remote (US)")) is None
    assert r.check_location(job(location="Remote")) == "remote is limited to region 'unknown'"


# --- salary ------------------------------------------------------------------

@pytest.mark.parametrize("description,reason", [
    ("Base salary: $170,000 - $215,000", None),
    ("Base salary: $130,000 - $170,000", None),           # top of range reaches the floor
    ("Base salary: $150,000", None),                      # exactly the floor
    ("Base salary: $110,000 - $140,000", "pay tops out at $140K (floor $150K)"),
    ("Hourly: $60 - $70 per hour", "pay tops out at $146K (floor $150K)"),
    ("No pay info here.", None),                          # goes to "No salary listed"
    ("Salary: £120,000 - £150,000", None),               # non-USD isn't compared
])
def test_salary(rules, description, reason):
    assert rules.check_salary(job(description=description)) == reason


def test_salary_compare_min():
    cfg = Config()
    cfg.salary.compare = "min"
    r = Rules(cfg)
    assert r.check_salary(job(description="Base salary: $130,000 - $170,000")) == \
        "pay starts at $130K (floor $150K)"
    assert r.check_salary(job(description="Base salary: $150,000 - $170,000")) is None


def test_keep_missing_false_drops_unlisted_pay():
    cfg = Config()
    cfg.salary.keep_missing = False
    assert Rules(cfg).check_salary(job()) == "no USD salary listed"


# --- verdicts and sections -----------------------------------------------------

def test_sections(rules):
    assert rules.evaluate(job(description="Base salary: $180,000 - $220,000")).section == MATCHES
    assert rules.evaluate(job()).section == NO_SALARY
    assert rules.evaluate(job(description="Salary: £120,000")).section == NO_SALARY
    assert rules.evaluate(job(), watch=True).section == WATCHLIST


def test_watchlist_keeps_missing_salary_even_when_config_drops_it():
    cfg = Config()
    cfg.salary.keep_missing = False
    r = Rules(cfg)
    assert r.evaluate(job(), watch=True).section == WATCHLIST
    assert not r.evaluate(job()).passed


def test_watchlist_still_obeys_the_floor_and_other_rules(rules):
    low = rules.evaluate(job(description="Base salary: $100,000 - $120,000"), watch=True)
    assert (low.passed, low.stage) == (False, "salary")
    fe = rules.evaluate(job(title="Frontend Engineer"), watch=True)
    assert (fe.passed, fe.stage) == (False, "role")


def test_role_is_checked_before_location(rules):
    v = rules.evaluate(job(title="Frontend Engineer", location="London, UK"))
    assert (v.stage, v.reason) == ("role", "title says 'Frontend'")


# --- digest ------------------------------------------------------------------

def _dated(title: str, day: int, **kw) -> Job:
    return job(title=title, posted_at=datetime(2026, 11, day, tzinfo=timezone.utc), **kw)


def test_digest_sorts_by_seniority_preference_then_newest():
    jobs = [
        _dated("Software Engineer", 5),
        _dated("Staff Engineer", 4),
        _dated("Senior Engineer", 1),
        _dated("Senior Engineer II", 3),
        _dated("Principal Engineer", 6),
    ]
    digest = build_digest(jobs, Config())
    titles = [e.job.title for e in digest.sections[NO_SALARY]]
    assert titles == ["Senior Engineer II", "Senior Engineer", "Staff Engineer", "Principal Engineer",
                      "Software Engineer"]


def test_digest_watchlist_by_company_name_and_cap():
    cfg = Config()
    cfg.digest.max_per_section = 2
    jobs = [job(title=f"Backend Engineer {n}", company="Acme") for n in "ABC"] + [job(company="Other")]
    digest = build_digest(jobs, cfg, watched={"acme"})
    assert len(digest.sections[WATCHLIST]) == 2
    assert digest.hidden == {WATCHLIST: 1}
    assert [e.job.company for e in digest.sections[NO_SALARY]] == ["Other"]
    assert digest.matched == 4


def test_digest_collects_drops_with_reasons():
    digest = build_digest([job(title="Frontend Engineer"), job(location="Remote - EMEA"), job()], Config())
    assert [(e.verdict.stage, e.verdict.reason) for e in digest.dropped] == [
        ("role", "title says 'Frontend'"),
        ("location", "remote is limited to region 'other'"),
    ]
    assert digest.matched == 1
