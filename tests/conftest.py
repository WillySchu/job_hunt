from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any client built without a mock transport fails loudly instead of reaching the internet."""
    import jobfeed.cli
    import jobfeed.http

    real = jobfeed.http.make_client

    def guarded(transport: httpx.BaseTransport | None = None) -> httpx.Client:
        if transport is None:
            def refuse(request: httpx.Request) -> httpx.Response:
                raise AssertionError(f"test tried to reach the network: {request.url}")
            transport = httpx.MockTransport(refuse)
        return real(transport)

    monkeypatch.setattr(jobfeed.http, "make_client", guarded)
    monkeypatch.setattr(jobfeed.cli, "make_client", guarded)

    def no_imap(*args, **kwargs):
        raise ConnectionRefusedError("test tried to reach Gmail")

    import imaplib
    monkeypatch.setattr(imaplib, "IMAP4_SSL", no_imap)


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
