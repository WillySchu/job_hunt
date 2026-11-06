"""Shared types for source adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import httpx

from jobfeed.models import Job


@dataclass(frozen=True)
class Board:
    """One thing to poll: a company's board on an ATS."""

    source: str
    slug: str
    company: str | None = None  # display name; looked up from the source when missing
    watch: bool = False

    @property
    def label(self) -> str:
        return f"{self.source}:{self.slug}"


@dataclass
class FetchResult:
    board: Board
    jobs: list[Job] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class Source(Protocol):
    name: str

    def fetch(self, client: httpx.Client, board: Board) -> list[Job]:
        """Return every open posting on the board. Raise on failure."""
        ...
