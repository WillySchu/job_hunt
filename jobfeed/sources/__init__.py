"""Source adapters and the concurrent fetcher."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import httpx

from jobfeed.http import FetchError, NotFound
from jobfeed.normalize import normalize_job
from jobfeed.sources.base import Board, FetchResult, Source
from jobfeed.sources.aggregators import AGGREGATORS, HimalayasSource, RemoteOKSource, RemotiveSource
from jobfeed.sources.ashby import AshbySource
from jobfeed.sources.greenhouse import GreenhouseSource
from jobfeed.sources.lever import LeverSource

SOURCES: dict[str, Source] = {
    "greenhouse": GreenhouseSource(),
    "lever": LeverSource(),
    "ashby": AshbySource(),
    "remotive": RemotiveSource(),
    "remoteok": RemoteOKSource(),
    "himalayas": HimalayasSource(),
}
# Company boards, in the order `discover` probes them.
ATS_SOURCES = ["greenhouse", "lever", "ashby"]
# How each source is credited in the digest.
SOURCE_NAMES = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "remotive": "Remotive",
                "remoteok": "Remote OK", "himalayas": "Himalayas"}

MAX_CONCURRENCY = 4


def fetch_board(client: httpx.Client, board: Board) -> FetchResult:
    source = SOURCES.get(board.source)
    if source is None:
        return FetchResult(board, error=f"unknown source '{board.source}'")
    try:
        return FetchResult(board, jobs=[normalize_job(j) for j in source.fetch(client, board)])
    except NotFound:
        return FetchResult(board, error="board not found (check the slug)")
    except FetchError as exc:
        return FetchResult(board, error=str(exc))
    except Exception as exc:  # one broken board must never stop the others
        return FetchResult(board, error=f"{type(exc).__name__}: {exc}")


def fetch_all(client: httpx.Client, boards: list[Board]) -> list[FetchResult]:
    """Fetch boards at most MAX_CONCURRENCY at a time, keeping input order."""
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as pool:
        return list(pool.map(lambda b: fetch_board(client, b), boards))


__all__ = ["AGGREGATORS", "ATS_SOURCES", "Board", "FetchResult", "SOURCES", "SOURCE_NAMES", "fetch_all",
           "fetch_board"]
