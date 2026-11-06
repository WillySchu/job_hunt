from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from jobfeed.http import FetchError, NotFound, USER_AGENT, make_client
from jobfeed.sources import fetch_board
from jobfeed.sources.base import Board
from jobfeed.sources.greenhouse import GreenhouseSource, parse_job
from tests.conftest import router

JOBS_PATH = "/v1/boards/examplecorp/jobs"
BOARD_PATH = "/v1/boards/examplecorp"


def test_parse_job_maps_core_fields(greenhouse_jobs):
    job = parse_job(greenhouse_jobs["jobs"][0], "Example Corp")
    assert job.id == "greenhouse:4412983"
    assert job.source == "greenhouse"
    assert job.company == "Example Corp"
    assert job.title == "Senior Backend Engineer, Platform"
    assert job.url == "https://job-boards.greenhouse.io/examplecorp/jobs/4412983"
    assert job.location_raw == "Seattle, WA; Remote (US)"
    # first_published wins over updated_at, and keeps its offset
    assert job.posted_at == datetime(2026, 10, 28, 9, 0, tzinfo=timezone(timedelta(hours=-4)))
    # the adapter leaves normalized fields at their defaults; fetch_board fills them
    assert job.work_mode == "unknown" and job.salary_max is None and job.cities == []


def test_description_is_unescaped_plain_text(greenhouse_jobs):
    text = parse_job(greenhouse_jobs["jobs"][0], "Example Corp").description
    assert "<" not in text and "&lt;" not in text and "&amp;" not in text
    assert "We build distributed systems at scale." in text
    assert "What you'll do" in text
    assert "What you'll do\n\n- Own Go and Rust services\n- Run Kafka and Postgres\n\nThe base" in text
    assert "$170,000—$215,000 USD" in text


def test_location_falls_back_to_offices_and_dates_to_updated_at(greenhouse_jobs):
    job = parse_job(greenhouse_jobs["jobs"][1], "Example Corp")
    assert job.location_raw == "Bellevue, WA; New York, NY"
    assert job.posted_at == datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    assert job.description == ""


def test_bad_date_becomes_none(greenhouse_jobs):
    assert parse_job(greenhouse_jobs["jobs"][2], "Example Corp").posted_at is None


def _without_company_name(payload: dict) -> dict:
    return {**payload, "jobs": [{k: v for k, v in j.items() if k != "company_name"} for j in payload["jobs"]]}


def test_fetch_uses_content_param_and_posting_company_name(greenhouse_jobs):
    seen = []

    def jobs(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=greenhouse_jobs)

    # No board route: the posting's company_name means no lookup is needed.
    with make_client(router({JOBS_PATH: jobs})) as client:
        result = GreenhouseSource().fetch(client, Board("greenhouse", "examplecorp"))

    assert len(result) == 3
    assert {j.company for j in result} == {"Example Corp"}
    assert len(seen) == 1
    assert seen[0].url.params["content"] == "true"
    assert seen[0].headers["User-Agent"] == USER_AGENT


def test_board_name_lookup_when_postings_lack_company_name(greenhouse_jobs):
    transport = router({
        JOBS_PATH: lambda r: httpx.Response(200, json=_without_company_name(greenhouse_jobs)),
        BOARD_PATH: lambda r: httpx.Response(200, json={"name": "Example Corp Inc"}),
    })
    with make_client(transport) as client:
        result = GreenhouseSource().fetch(client, Board("greenhouse", "examplecorp"))
    assert {j.company for j in result} == {"Example Corp Inc"}


def test_known_company_name_skips_board_lookup(greenhouse_jobs):
    transport = router({JOBS_PATH: lambda r: httpx.Response(200, json=greenhouse_jobs)})
    with make_client(transport) as client:
        result = GreenhouseSource().fetch(client, Board("greenhouse", "examplecorp", company="Example"))
    assert result[0].company == "Example"


def test_board_name_falls_back_to_slug(greenhouse_jobs):
    transport = router({
        JOBS_PATH: lambda r: httpx.Response(200, json=_without_company_name(greenhouse_jobs)),
        BOARD_PATH: lambda r: httpx.Response(500),
    })
    with make_client(transport) as client:
        result = GreenhouseSource().fetch(client, Board("greenhouse", "examplecorp"))
    assert result[0].company == "examplecorp"


def test_unknown_slug_raises_not_found():
    with make_client(router({})) as client, pytest.raises(NotFound):
        GreenhouseSource().fetch(client, Board("greenhouse", "nope"))


def test_retries_503_then_succeeds(greenhouse_jobs, no_sleep):
    calls = iter([httpx.Response(503), httpx.Response(429, headers={"Retry-After": "3"}),
                  httpx.Response(200, json=greenhouse_jobs)])
    transport = router({JOBS_PATH: lambda r: next(calls)})
    with make_client(transport) as client:
        result = GreenhouseSource().fetch(client, Board("greenhouse", "examplecorp", company="X"))
    assert len(result) == 3
    assert no_sleep == [1.0, 3.0]  # backoff, then Retry-After


def test_gives_up_after_retries(no_sleep):
    transport = router({JOBS_PATH: lambda r: httpx.Response(502)})
    with make_client(transport) as client, pytest.raises(FetchError, match="after 3 attempts"):
        GreenhouseSource().fetch(client, Board("greenhouse", "examplecorp", company="X"))
    assert len(no_sleep) == 2


def test_fetch_board_turns_errors_into_results(no_sleep):
    def boom(request):
        raise httpx.ConnectError("connection refused", request=request)

    with make_client(router({JOBS_PATH: boom})) as client:
        missing = fetch_board(client, Board("greenhouse", "nope"))
        broken = fetch_board(client, Board("greenhouse", "examplecorp", company="X"))
    assert missing.error == "board not found (check the slug)"
    assert broken.error and "ConnectError" in broken.error
