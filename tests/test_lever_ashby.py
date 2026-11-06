from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from jobfeed.http import NotFound, make_client
from jobfeed.sources import fetch_board
from jobfeed.sources.ashby import AshbySource
from jobfeed.sources.base import Board
from jobfeed.sources.lever import PAGE, LeverSource
from tests.conftest import FIXTURES, router


@pytest.fixture
def lever_postings() -> list:
    return json.loads((FIXTURES / "lever_postings.json").read_text())


@pytest.fixture
def ashby_board() -> dict:
    return json.loads((FIXTURES / "ashby_board.json").read_text())


# --- Lever -------------------------------------------------------------------

def _lever(postings, path="/v0/postings/leverco"):
    return router({path: lambda r: httpx.Response(200, json=postings)})


def test_lever_fetch_and_normalize(lever_postings):
    with make_client(_lever(lever_postings)) as client:
        result = fetch_board(client, Board("lever", "leverco", company="Lever Co"))
    assert result.ok
    jobs = {j.id: j for j in result.jobs}
    assert set(jobs) == {"lever:a1b2c3d4-0001", "lever:a1b2c3d4-0002", "lever:a1b2c3d4-0003",
                         "lever:a1b2c3d4-0004"}

    remote = jobs["lever:a1b2c3d4-0001"]
    assert (remote.title, remote.company, remote.source) == ("Senior Backend Engineer", "Lever Co", "lever")
    assert remote.url == "https://jobs.lever.co/leverco/a1b2c3d4-0001"
    assert remote.posted_at == datetime.fromtimestamp(1761750000, tz=timezone.utc)
    assert (remote.work_mode, remote.remote_region) == ("remote", "us")
    assert remote.location_raw == "Remote - United States"
    assert remote.remote_states == ["CA", "CO", "NY", "WA"]       # from additionalPlain
    assert (remote.salary_min, remote.salary_max, remote.salary_currency) == (175_000, 215_000, "USD")
    assert "What you'll do\n- Own the ledger service\n- Run Kafka" in remote.description
    assert remote.seniority == "senior"

    hybrid = jobs["lever:a1b2c3d4-0002"]
    assert hybrid.work_mode == "hybrid"
    assert hybrid.cities == ["Seattle, WA", "Bellevue, WA"]
    assert (hybrid.salary_min, hybrid.salary_max) == (190_000, 240_000)  # parsed from `additional` HTML

    toronto = jobs["lever:a1b2c3d4-0003"]
    assert toronto.location_raw == "Toronto (Canada)"                # country code CA isn't California
    assert toronto.cities == ["Toronto, Canada"] and toronto.work_mode == "onsite"
    assert (toronto.salary_min, toronto.salary_currency, toronto.salary_raw) == (None, "CAD", "CAD 150,000–180,000")

    hourly = jobs["lever:a1b2c3d4-0004"]
    assert (hourly.salary_min, hourly.salary_max) == (187_200, 228_800)  # per-hour-wage x 2,080
    assert hourly.work_mode == "remote"                                 # "unspecified" -> text says Remote


def test_lever_pages_through_large_boards():
    postings = [{"id": f"p{n}", "text": "Backend Engineer", "categories": {}} for n in range(PAGE + 30)]
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        skip, limit = int(request.url.params["skip"]), int(request.url.params["limit"])
        seen.append(skip)
        assert request.url.params["mode"] == "json"
        return httpx.Response(200, json=postings[skip: skip + limit])

    with make_client(router({"/v0/postings/big": handle})) as client:
        jobs = LeverSource().fetch(client, Board("lever", "big", company="Big"))
    assert len(jobs) == PAGE + 30
    assert seen == [0, PAGE]


def test_lever_stops_if_the_api_ignores_skip():
    page = [{"id": f"p{n}", "text": "Backend Engineer", "categories": {}} for n in range(PAGE)]
    calls = []

    def handle(request):
        calls.append(1)
        return httpx.Response(200, json=page)

    with make_client(router({"/v0/postings/stuck": handle})) as client:
        jobs = LeverSource().fetch(client, Board("lever", "stuck"))
    assert len(jobs) == PAGE and len(calls) == 2


def test_lever_falls_back_to_the_eu_host(lever_postings):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.eu.lever.co":
            return httpx.Response(200, json=lever_postings[:1])
        return httpx.Response(404, json={"ok": False, "error": "Document not found"})

    with make_client(httpx.MockTransport(handle)) as client:
        jobs = LeverSource().fetch(client, Board("lever", "euco", company="EU Co"))
    assert [j.id for j in jobs] == ["lever:a1b2c3d4-0001"]


def test_lever_unknown_slug(lever_postings):
    with make_client(router({})) as client, pytest.raises(NotFound):
        LeverSource().fetch(client, Board("lever", "nope"))


def test_lever_probe(lever_postings):
    with make_client(_lever(lever_postings)) as client:
        assert LeverSource().probe(client, "leverco") == (4, None)
    with make_client(_lever([])) as client:
        assert LeverSource().probe(client, "leverco") == (0, None)


# --- Ashby -------------------------------------------------------------------

ASHBY_PATH = "/posting-api/job-board/ashbyco"


def test_ashby_fetch_and_normalize(ashby_board):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=ashby_board)

    with make_client(router({ASHBY_PATH: handle})) as client:
        result = fetch_board(client, Board("ashby", "ashbyco", company="Ashby Co"))
    assert result.ok
    assert seen[0].url.params["includeCompensation"] == "true"
    jobs = {j.id: j for j in result.jobs}
    assert set(jobs) == {"ashby:9f1e0000-0001", "ashby:9f1e0000-0002", "ashby:9f1e0000-0003"}  # unlisted skipped

    hybrid = jobs["ashby:9f1e0000-0001"]
    assert hybrid.location_raw == "Seattle; San Francisco (United States)"
    assert hybrid.work_mode == "hybrid"
    assert hybrid.cities == ["Seattle", "San Francisco"]
    assert (hybrid.salary_min, hybrid.salary_max, hybrid.salary_currency) == (180_000, 230_000, "USD")
    assert hybrid.salary_raw == "$180K - $230K"
    assert hybrid.posted_at == datetime(2026, 10, 20, 14, 2, 11, tzinfo=timezone.utc)
    assert hybrid.url == "https://jobs.ashbyhq.com/ashbyco/9f1e0000-0001"

    remote = jobs["ashby:9f1e0000-0002"]
    assert (remote.work_mode, remote.remote_region) == ("remote", "us")
    assert remote.location_raw == "Remote (United States)"
    assert (remote.salary_min, remote.salary_max) == (150_000, 185_000)  # from the summary text
    assert remote.description == "Rust services."                      # descriptionHtml when no plain text

    london = jobs["ashby:9f1e0000-0003"]
    assert (london.work_mode, london.cities) == ("onsite", ["London, United Kingdom"])
    assert (london.salary_min, london.salary_currency, london.salary_raw) == (None, "GBP", "GBP 90,000–120,000")


def test_ashby_unknown_board_404():
    with make_client(router({})) as client, pytest.raises(NotFound):
        AshbySource().fetch(client, Board("ashby", "nope"))


def test_ashby_unexpected_body_counts_as_not_found():
    transport = router({"/posting-api/job-board/odd": lambda r: httpx.Response(200, json={"error": "no board"})})
    with make_client(transport) as client, pytest.raises(NotFound):
        AshbySource().fetch(client, Board("ashby", "odd"))


def test_ashby_probe_skips_compensation(ashby_board):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=ashby_board)

    with make_client(router({ASHBY_PATH: handle})) as client:
        assert AshbySource().probe(client, "ashbyco") == (3, None)
    assert "includeCompensation" not in seen[0].url.params
