from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import httpx
import pytest
import yaml

from jobfeed.cli import aggregator_boards, main
from jobfeed.companies import CompanyList, Company, load_companies
from jobfeed.config import Config
from jobfeed.harvest import harvest
from jobfeed.http import make_client
from jobfeed.normalize import normalize_job
from jobfeed.sources import fetch_board
from jobfeed.sources.base import Board
from tests.conftest import FIXTURES

NOW = datetime(2026, 11, 6, 19, 0, tzinfo=timezone.utc)


def _load(name):
    return json.loads((FIXTURES / name).read_text())


def aggregator_transport(seen: list | None = None) -> httpx.MockTransport:
    pages = {1: _load("himalayas_page1.json"), 2: {"jobs": []}}

    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        host = request.url.host
        if host == "remotive.com":
            return httpx.Response(200, json=_load("remotive.json"))
        if host == "remoteok.com":
            return httpx.Response(200, json=_load("remoteok.json"))
        if host == "himalayas.app":
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json=pages.get(page, {"jobs": []}))
        return httpx.Response(404)

    return httpx.MockTransport(handle)


def _fetch(board: Board, seen=None):
    with make_client(aggregator_transport(seen)) as client:
        result = fetch_board(client, board)
    assert result.ok, result.error
    return {j.id: j for j in result.jobs}


# --- Remotive ------------------------------------------------------------------

def test_remotive():
    seen = []
    jobs = _fetch(Board("remotive", "software-dev"), seen)
    assert seen[0].url.params["category"] == "software-dev"
    assert set(jobs) == {"remotive:1842567", "remotive:1842568", "remotive:1842569"}

    j = jobs["remotive:1842567"]
    assert (j.source, j.company, j.title) == ("remotive", "Acme Payments", "Senior Backend Engineer")
    assert j.url == "https://remotive.com/remote-jobs/software-dev/senior-backend-engineer-1842567"  # attribution
    assert j.apply_url == "https://boards.greenhouse.io/acmepayments/jobs/777?gh_src=remotive"
    assert (j.work_mode, j.remote_region) == ("remote", "us")
    assert (j.salary_min, j.salary_max) == (160_000, 200_000)
    assert j.posted_at == datetime(2026, 11, 4, 10, 23, 26, tzinfo=timezone.utc)
    assert "Apply here" in j.description and "<a" not in j.description

    eu = jobs["remotive:1842569"]
    assert (eu.remote_region, eu.salary_currency, eu.salary_min) == ("other", "EUR", None)


def test_remotive_all_categories_sends_no_category():
    seen = []
    _fetch(Board("remotive", "all"), seen)
    assert "category" not in seen[0].url.params


# --- Remote OK -------------------------------------------------------------------

def test_remoteok_skips_the_legal_notice():
    jobs = _fetch(Board("remoteok", "all"))
    assert set(jobs) == {"remoteok:1137465", "remoteok:1137466"}

    j = jobs["remoteok:1137465"]
    assert (j.company, j.title, j.seniority) == ("Orbit", "Staff Infrastructure Engineer", "staff")
    assert j.url.startswith("https://remoteok.com/remote-jobs/")
    assert j.apply_url == "https://jobs.ashbyhq.com/orbit/5c0ffee0-0000/application"
    assert (j.salary_min, j.salary_max, j.salary_currency) == (190_000, 240_000, "USD")
    assert (j.work_mode, j.remote_region) == ("remote", "us")

    q = jobs["remoteok:1137466"]
    assert q.apply_url is None                       # remoteok.com/l/... isn't a company board
    assert (q.salary_min, q.salary_max) == (None, None)   # zeros mean "not stated"
    assert q.posted_at == datetime.fromtimestamp(1762300000, tz=timezone.utc)   # epoch is in seconds
    assert (q.work_mode, q.remote_region) == ("remote", None)


def test_remoteok_tags():
    seen = []
    _fetch(Board("remoteok", "backend,golang"), seen)
    assert seen[0].url.params["tags"] == "backend,golang"


# --- Himalayas -------------------------------------------------------------------

def test_himalayas_pages_until_empty():
    seen = []
    jobs = _fetch(Board("himalayas", "software engineer", options=(("pages", "5"), ("country", "US"))), seen)
    assert [r.url.params["page"] for r in seen] == ["1", "2"]
    assert seen[0].url.params["q"] == "software engineer"
    assert seen[0].url.params["country"] == "US"
    assert seen[0].url.params["sort"] == "recent"

    j = jobs["himalayas:https://himalayas.app/companies/motive-analytics/jobs/senior-software-engineer-data"]
    assert j.company == "Motive Analytics"
    assert j.url.startswith("https://himalayas.app/companies/motive-analytics/")
    assert j.location_raw == "United States"
    assert (j.work_mode, j.remote_region) == ("remote", "us")
    assert (j.salary_min, j.salary_max) == (176_800, 208_000)   # hourly x 2,080
    assert j.posted_at == datetime.fromtimestamp(1762200000, tz=timezone.utc)   # pubDate is in milliseconds

    anywhere = jobs["himalayas:https://himalayas.app/companies/anywhere/jobs/backend-engineer"]
    assert anywhere.location_raw == "Worldwide" and anywhere.remote_region == "global"
    assert anywhere.description == "Remote anywhere."      # excerpt when there's no description


def test_himalayas_respects_the_page_limit():
    seen = []
    _fetch(Board("himalayas", "backend engineer", options=(("pages", "1"),)), seen)
    assert len(seen) == 1 and "country" not in seen[0].url.params


# --- boards from config ------------------------------------------------------------

def test_aggregator_boards_from_config():
    labels = [(b.label, b.options) for b in aggregator_boards(Config())]
    assert labels == [
        ("remotive:software-dev", ()),
        ("remoteok:all", ()),
        ("himalayas:software engineer", (("pages", "2"), ("country", "US"))),
        ("himalayas:backend engineer", (("pages", "2"), ("country", "US"))),
        ("himalayas:platform engineer", (("pages", "2"), ("country", "US"))),
    ]
    cfg = Config()
    cfg.aggregators.remotive = cfg.aggregators.himalayas = False
    cfg.aggregators.remoteok_tags = ["backend"]
    assert [b.label for b in aggregator_boards(cfg)] == ["remoteok:backend"]


# --- harvest ---------------------------------------------------------------------

def _agg(id_, company, title, apply_url, source="remotive"):
    from jobfeed.models import Job
    return normalize_job(Job(id=id_, source=source, company=company, title=title, url="https://x",
                             apply_url=apply_url, work_mode="remote"))


def test_harvest_finds_new_boards_for_engineering_roles():
    jobs = [
        _agg("remotive:1", "Acme Payments", "Senior Backend Engineer", "https://boards.greenhouse.io/acmepayments/jobs/1"),
        _agg("remotive:2", "Acme Payments", "Staff Engineer", "https://boards.greenhouse.io/acmepayments/jobs/2"),
        _agg("remotive:3", "Pixel Co", "Frontend Engineer", "https://jobs.lever.co/pixelco/abc"),     # role drop
        _agg("remotive:4", "Sales Co", "Account Executive", "https://jobs.lever.co/salesco/abc"),     # not eng
        _agg("remoteok:5", "Orbit", "Staff Infrastructure Engineer", "https://jobs.ashbyhq.com/orbit/x", "remoteok"),
        _agg("remotive:6", "No Link", "Backend Engineer", None),
    ]
    found = harvest(jobs, CompanyList(), Config())
    assert [(d.name, d.ats, d.slug) for d in found] == [
        ("Acme Payments", "greenhouse", "acmepayments"),
        ("Orbit", "ashby", "orbit"),
    ]


def test_harvest_skips_known_boards_and_companies_with_other_boards():
    companies = CompanyList(companies=[
        Company(name="Stripe", ats="greenhouse", slug="stripe"),
        Company(name="Orbit Labs", ats="lever", slug="orbit-labs"),
        Company(name="Pending Co", ats=None, slug=None),
        Company(name="Gave Up Co", ats="unsupported", slug=None),
    ])
    jobs = [
        _agg("remotive:1", "Stripe Payments", "Backend Engineer", "https://boards.greenhouse.io/Stripe/jobs/1"),
        _agg("remotive:2", "Orbit Labs", "Backend Engineer", "https://jobs.ashbyhq.com/orbit/x"),
        _agg("remotive:3", "Pending Co", "Backend Engineer", "https://jobs.lever.co/pendingco/y"),
        _agg("remotive:4", "gave up co", "Backend Engineer", "https://jobs.ashbyhq.com/gaveup/z"),
    ]
    found = harvest(jobs, companies, Config())
    assert [(d.name, d.ats, d.slug) for d in found] == [
        ("Pending Co", "lever", "pendingco"),
        ("Gave Up Co", "ashby", "gaveup"),       # keeps the name as written in companies.yaml
    ]


def test_harvest_ignores_company_board_postings():
    j = normalize_job(__import__("jobfeed.models", fromlist=["Job"]).Job(
        id="greenhouse:1", source="greenhouse", company="Acme", title="Backend Engineer", url="u",
        apply_url="https://jobs.lever.co/other/x"))
    assert harvest([j], CompanyList(), Config()) == []


# --- end to end -------------------------------------------------------------------

def _run(argv, transport, now=NOW):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, transport=transport, out=out, err=err, now=now)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def files(tmp_path):
    companies = tmp_path / "companies.yaml"
    companies.write_text("# seed\ncompanies:\n  - { name: Pending Co, origin: seed, ats: null, slug: null }\n\n"
                         "manual_check:\n  - Microsoft\n")
    return ["--companies", str(companies), "--db", str(tmp_path / "jobs.db")], companies


def test_run_polls_aggregators_credits_them_and_harvests(files):
    argv, companies = files
    code, out, err = _run(["run", "--no-email", *argv], aggregator_transport())
    assert code == 0
    assert "Senior Backend Engineer — Acme Payments" in out
    assert "via Remotive" in out and "via Remote OK" in out and "via Himalayas" in out
    assert "remotive:software-dev" not in err      # boards all succeeded
    assert "added 2 companies to" in err
    assert "Acme Payments (greenhouse:acmepayments)" in err and "Orbit (ashby:orbit)" in err

    text = companies.read_text()
    assert "# --- harvested from aggregator postings ---" in text
    data = yaml.safe_load(text)
    harvested = {c["name"]: c for c in data["companies"] if c.get("origin") == "harvested"}
    assert {n: (c["ats"], c["slug"]) for n, c in harvested.items()} == {
        "Acme Payments": ("greenhouse", "acmepayments"), "Orbit": ("ashby", "orbit")}
    assert data["manual_check"] == ["Microsoft"]
    assert [b.label for b in load_companies(companies).boards()] == ["greenhouse:acmepayments", "ashby:orbit"]


def test_dry_run_only_reports_what_harvest_would_add(files):
    argv, companies = files
    before = companies.read_text()
    _, _, err = _run(["run", "--dry-run", *argv], aggregator_transport())
    assert "would add 2 companies from aggregator links" in err
    assert companies.read_text() == before


def test_harvest_can_be_turned_off(files, tmp_path):
    argv, companies = files
    cfg = tmp_path / "config.yaml"
    cfg.write_text("aggregators:\n  harvest: false\n")
    before = companies.read_text()
    _, _, err = _run(["run", "--no-email", *argv, "--config", str(cfg)], aggregator_transport())
    assert "added" not in err and companies.read_text() == before


def test_no_aggregators_flag(files):
    argv, _ = files
    code, _, err = _run(["run", "--dry-run", "--no-aggregators", *argv], aggregator_transport())
    assert code == 2 and "No boards to poll" in err
