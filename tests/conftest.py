from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture
def greenhouse_jobs() -> dict:
    return json.loads((FIXTURES / "greenhouse_jobs.json").read_text())


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record retry delays instead of sleeping."""
    delays: list[float] = []
    monkeypatch.setattr("jobfeed.http.time.sleep", delays.append)
    return delays


def router(routes: dict[str, Callable[[httpx.Request], httpx.Response]]) -> httpx.MockTransport:
    """A transport that dispatches on URL path; unknown paths return 404."""

    def handle(request: httpx.Request) -> httpx.Response:
        handler = routes.get(request.url.path)
        return handler(request) if handler else httpx.Response(404, json={"status": 404})

    return httpx.MockTransport(handle)
