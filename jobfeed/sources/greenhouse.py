"""Greenhouse public Job Board API.

GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
returns every published job on a board, descriptions included. No auth.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from jobfeed.http import FetchError, get_json
from jobfeed.models import Job
from jobfeed.sources.base import Board
from jobfeed.text import html_to_text

API_BASE = "https://boards-api.greenhouse.io/v1/boards"


def _parse_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _location(raw: dict[str, Any]) -> str:
    name = ((raw.get("location") or {}).get("name") or "").strip()
    if name:
        return name
    offices = [o.get("name", "").strip() for o in raw.get("offices") or [] if o.get("name")]
    return "; ".join(offices)


def parse_job(raw: dict[str, Any], company: str) -> Job:
    return Job(
        id=f"greenhouse:{raw['id']}",
        source="greenhouse",
        company=company,
        title=(raw.get("title") or "").strip(),
        url=raw.get("absolute_url") or "",
        location_raw=_location(raw),
        posted_at=_parse_time(raw.get("first_published")) or _parse_time(raw.get("updated_at")),
        description=html_to_text(raw.get("content")),
    )


class GreenhouseSource:
    name = "greenhouse"

    def fetch(self, client: httpx.Client, board: Board) -> list[Job]:
        data = get_json(client, f"{API_BASE}/{board.slug}/jobs", params={"content": "true"})
        raws = [raw for raw in data.get("jobs") or [] if raw.get("id")]
        # Name priority: companies.yaml, then the posting's own company_name,
        # then one lookup of the board's display name.
        company = board.company or next(
            (str(r["company_name"]).strip() for r in raws if r.get("company_name")), None
        )
        if company is None and raws:
            company = self.board_name(client, board.slug)
        return [parse_job(raw, company or board.slug) for raw in raws]

    def board_name(self, client: httpx.Client, slug: str) -> str:
        """The board's display name, falling back to the slug."""
        try:
            data = get_json(client, f"{API_BASE}/{slug}", retries=0)
        except FetchError:
            return slug
        return (data.get("name") or "").strip() or slug
