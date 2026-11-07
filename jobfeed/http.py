"""A polite HTTP client: 20 s timeout, two retries with backoff, a named User-Agent."""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx

from jobfeed import __version__

USER_AGENT = f"jobfeed/{__version__} (personal job digest; +https://github.com/WillySchu/job_hunt)"
TIMEOUT_SECONDS = 20.0
RETRIES = 2
BACKOFF_SECONDS = 1.0
MAX_RETRY_AFTER_SECONDS = 30.0
RETRY_STATUSES = {429, 500, 502, 503, 504}


class NotFound(Exception):
    """The URL returned 404, e.g. a board slug that doesn't exist."""


class FetchError(Exception):
    """A request failed after all retries."""


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=TIMEOUT_SECONDS,
        follow_redirects=True,
        transport=transport,
    )


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After", "")
        if retry_after.isdigit():
            return min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
    return BACKOFF_SECONDS * (2 ** attempt)


def get_json(
    client: httpx.Client,
    url: str,
    params: dict[str, str] | None = None,
    *,
    retries: int = RETRIES,
    sleep: Callable[[float], None] | None = None,
) -> Any:
    """GET a URL and decode JSON, retrying timeouts, 429 and 5xx responses."""
    sleep = sleep or time.sleep
    last_problem = "no attempt made"
    for attempt in range(retries + 1):
        response: httpx.Response | None = None
        try:
            response = client.get(url, params=params)
        except httpx.TransportError as exc:
            last_problem = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code == 404:
                raise NotFound(url)
            if response.status_code not in RETRY_STATUSES:
                if response.is_error:
                    raise FetchError(f"HTTP {response.status_code} from {url}")
                try:
                    return response.json()
                except ValueError as exc:
                    raise FetchError(f"Invalid JSON from {url}") from exc
            last_problem = f"HTTP {response.status_code}"
        if attempt < retries:
            sleep(_retry_delay(response, attempt))
    raise FetchError(f"{last_problem} from {url} after {retries + 1} attempts")


def get_text(client: httpx.Client, url: str, *, retries: int = RETRIES,
             sleep: Callable[[float], None] | None = None) -> str:
    """GET a URL as text, with the same retries as get_json."""
    sleep = sleep or time.sleep
    last_problem = "no attempt made"
    for attempt in range(retries + 1):
        response: httpx.Response | None = None
        try:
            response = client.get(url, headers={"Accept": "text/plain, text/markdown, */*"})
        except httpx.TransportError as exc:
            last_problem = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code == 404:
                raise NotFound(url)
            if response.status_code not in RETRY_STATUSES:
                if response.is_error:
                    raise FetchError(f"HTTP {response.status_code} from {url}")
                return response.text
            last_problem = f"HTTP {response.status_code}"
        if attempt < retries:
            sleep(_retry_delay(response, attempt))
    raise FetchError(f"{last_problem} from {url} after {retries + 1} attempts")
