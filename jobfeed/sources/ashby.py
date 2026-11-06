"""Ashby public Job Posting API.

GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
returns every published posting on the board in one response. No auth, no
paging, and no company name: the display name comes from companies.yaml.
"""

from __future__ import annotations

from typing import Any

import httpx

from jobfeed.http import NotFound, get_json
from jobfeed.models import Job
from jobfeed.sources.base import Board
from jobfeed.sources.common import annual_multiplier, join_unique, parse_time, with_country
from jobfeed.text import html_to_text

API_BASE = "https://api.ashbyhq.com/posting-api/job-board"

_WORK_MODES = {"remote": "remote", "hybrid": "hybrid"}  # "OnSite": let the location text decide


def _country(raw: dict[str, Any]) -> str | None:
    postal = ((raw.get("address") or {}).get("postalAddress") or {})
    country = postal.get("addressCountry")
    return country.strip() if isinstance(country, str) and country.strip() else None


def _location(raw: dict[str, Any], mode: str) -> str:
    places = [str(raw.get("location") or "")]
    for extra in raw.get("secondaryLocations") or []:
        if isinstance(extra, dict):
            places.append(str(extra.get("location") or ""))
        elif isinstance(extra, str):
            places.append(extra)
    location = with_country(join_unique(places), _country(raw))
    if mode == "remote" and "remote" not in location.lower():
        location = f"Remote - {location}" if location else "Remote"
    return location


def _salary(raw: dict[str, Any]) -> dict[str, Any]:
    comp = raw.get("compensation") or {}
    for part in comp.get("summaryComponents") or []:
        if not isinstance(part, dict) or str(part.get("compensationType", "")).lower() != "salary":
            continue
        factor = annual_multiplier(part.get("interval"))
        low, high = part.get("minValue"), part.get("maxValue")
        if factor is None or not isinstance(low, (int, float)) and not isinstance(high, (int, float)):
            continue
        low = round(low * factor) if isinstance(low, (int, float)) else None
        high = round(high * factor) if isinstance(high, (int, float)) else None
        currency = str(part.get("currencyCode") or "USD").upper()
        summary = comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary")
        if currency == "USD":
            return {"salary_min": low, "salary_max": high, "salary_currency": "USD", "salary_raw": summary}
        shown = "–".join(f"{v:,}" for v in (low, high) if v is not None)
        return {"salary_currency": currency, "salary_raw": summary or f"{currency} {shown}"}
    # No structured salary: hand the summary text to the salary parser.
    summary = comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary")
    return {"salary_raw": summary} if summary else {}


def parse_job(raw: dict[str, Any], company: str) -> Job:
    mode = _WORK_MODES.get(str(raw.get("workplaceType") or "").lower(), "unknown")
    if raw.get("isRemote") is True:
        mode = "remote"
    return Job(
        id=f"ashby:{raw['id']}",
        source="ashby",
        company=company,
        title=(raw.get("title") or "").strip(),
        url=raw.get("jobUrl") or raw.get("applyUrl") or "",
        location_raw=_location(raw, mode),
        work_mode=mode,
        posted_at=parse_time(raw.get("publishedAt")),
        description=raw.get("descriptionPlain") or html_to_text(raw.get("descriptionHtml")),
        **_salary(raw),
    )


class AshbySource:
    name = "ashby"

    def _board(self, client: httpx.Client, slug: str, *, compensation: bool) -> list[dict[str, Any]]:
        params = {"includeCompensation": "true"} if compensation else None
        data = get_json(client, f"{API_BASE}/{slug}", params=params)
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise NotFound(f"{API_BASE}/{slug}")
        return [j for j in data["jobs"] if isinstance(j, dict) and j.get("id") and j.get("isListed", True)]

    def fetch(self, client: httpx.Client, board: Board) -> list[Job]:
        company = board.company or board.slug
        return [parse_job(raw, company) for raw in self._board(client, board.slug, compensation=True)]

    def probe(self, client: httpx.Client, slug: str) -> tuple[int, str | None]:
        return len(self._board(client, slug, compensation=False)), None
