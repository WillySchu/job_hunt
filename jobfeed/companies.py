"""Loading the seed list and watchlist from companies.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from jobfeed.sources.base import Board

DEFAULT_PATH = Path("companies.yaml")


@dataclass
class Company:
    name: str
    bucket: str | None = None
    origin: str = "seed"
    ats: str | None = None
    slug: str | None = None
    watch: bool = False

    def board(self) -> Board | None:
        """The board to poll, or None until `discover` has filled in ats and slug."""
        if not self.ats or not self.slug or self.ats == "unsupported":
            return None
        return Board(source=self.ats, slug=self.slug, company=self.name, watch=self.watch)


@dataclass
class CompanyList:
    companies: list[Company] = field(default_factory=list)
    manual_check: list[str] = field(default_factory=list)

    def boards(self, source: str | None = None) -> list[Board]:
        boards = [b for c in self.companies if (b := c.board())]
        return [b for b in boards if source is None or b.source == source]


def load_companies(path: Path = DEFAULT_PATH) -> CompanyList:
    if not path.exists():
        return CompanyList()
    data = yaml.safe_load(path.read_text()) or {}
    companies = []
    for i, entry in enumerate(data.get("companies") or []):
        if not isinstance(entry, dict) or not entry.get("name"):
            raise ValueError(f"{path}: company #{i + 1} needs a name")
        companies.append(
            Company(
                name=str(entry["name"]),
                bucket=entry.get("bucket"),
                origin=entry.get("origin") or "seed",
                ats=entry.get("ats"),
                slug=None if entry.get("slug") is None else str(entry["slug"]),
                watch=bool(entry.get("watch", False)),
            )
        )
    manual = [str(m) for m in (data.get("manual_check") or [])]
    return CompanyList(companies=companies, manual_check=manual)
