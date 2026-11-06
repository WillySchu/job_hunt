"""Lever public Postings API.

GET https://api.lever.co/v0/postings/{slug}?mode=json&skip=N&limit=100
returns a page of published postings. Accounts hosted in the EU live on
api.eu.lever.co; a 404 from the US host is retried there. No auth.
"""

from __future__ import annotations

from typing import Any

import httpx

from jobfeed.http import NotFound, get_json
from jobfeed.models import Job
from jobfeed.sources.base import Board
from jobfeed.sources.common import annual_multiplier, country_name, join_unique, parse_time, with_country
from jobfeed.text import html_to_text

HOSTS = ["https://api.lever.co", "https://api.eu.lever.co"]
PAGE = 100
MAX_PAGES = 50  # 5,000 postings: far past any real board, but a loop can't run forever

_WORK_MODES = {"remote": "remote", "hybrid": "hybrid"}  # "onsite"/"unspecified": let the text decide


def _description(raw: dict[str, Any]) -> str:
    parts = [raw.get("descriptionPlain") or html_to_text(raw.get("description"))]
    for section in raw.get("lists") or []:
        heading = (section.get("text") or "").strip()
        body = html_to_text(section.get("content"))
        parts.append(f"{heading}\n{body}".strip())
    parts.append(raw.get("additionalPlain") or html_to_text(raw.get("additional")))
    if raw.get("salaryDescriptionPlain") or raw.get("salaryDescription"):
        parts.append(raw.get("salaryDescriptionPlain") or html_to_text(raw.get("salaryDescription")))
    return "\n\n".join(p for p in parts if p)


def _salary(raw: dict[str, Any]) -> dict[str, Any]:
    rng = raw.get("salaryRange") or {}
    low, high, currency = rng.get("min"), rng.get("max"), (rng.get("currency") or "").upper() or None
    factor = annual_multiplier(rng.get("interval"))
    if factor is None or not isinstance(low, (int, float)) and not isinstance(high, (int, float)):
        text = raw.get("salaryDescriptionPlain") or html_to_text(raw.get("salaryDescription"))
        return {"salary_raw": text or None}
    low = round(low * factor) if isinstance(low, (int, float)) else None
    high = round(high * factor) if isinstance(high, (int, float)) else None
    if currency in (None, "USD"):
        return {"salary_min": low, "salary_max": high, "salary_currency": "USD"}
    shown = "–".join(f"{v:,}" for v in (low, high) if v is not None)
    return {"salary_currency": currency, "salary_raw": f"{currency} {shown}"}


def parse_job(raw: dict[str, Any], company: str) -> Job:
    cats = raw.get("categories") or {}
    locations = cats.get("allLocations") or [cats.get("location") or ""]
    mode = _WORK_MODES.get(str(raw.get("workplaceType") or "").lower(), "unknown")
    location = with_country(join_unique([str(x) for x in locations]), country_name(raw.get("country")))
    if mode == "remote" and "remote" not in location.lower():
        location = f"Remote - {location}" if location else "Remote"
    return Job(
        id=f"lever:{raw['id']}",
        source="lever",
        company=company,
        title=(raw.get("text") or "").strip(),
        url=raw.get("hostedUrl") or raw.get("applyUrl") or "",
        location_raw=location,
        work_mode=mode,
        posted_at=parse_time(raw.get("createdAt")),
        description=_description(raw),
        **_salary(raw),
    )


class LeverSource:
    name = "lever"

    def _pages(self, client: httpx.Client, slug: str, *, max_pages: int = MAX_PAGES) -> list[dict[str, Any]]:
        last_error: NotFound | None = None
        for host in HOSTS:
            postings: list[dict[str, Any]] = []
            seen: set[str] = set()
            try:
                for page in range(max_pages):
                    data = get_json(client, f"{host}/v0/postings/{slug}",
                                    params={"mode": "json", "skip": str(page * PAGE), "limit": str(PAGE)})
                    if not isinstance(data, list):
                        raise NotFound(f"{host}/v0/postings/{slug}")
                    fresh = [p for p in data if isinstance(p, dict) and p.get("id") and p["id"] not in seen]
                    seen.update(p["id"] for p in fresh)
                    postings += fresh
                    if len(data) < PAGE or not fresh:
                        break
                return postings
            except NotFound as exc:
                last_error = exc
        raise last_error or NotFound(slug)

    def fetch(self, client: httpx.Client, board: Board) -> list[Job]:
        company = board.company or board.slug
        return [parse_job(raw, company) for raw in self._pages(client, board.slug)]

    def probe(self, client: httpx.Client, slug: str) -> tuple[int, str | None]:
        """(postings on the first page, display name or None). Raises NotFound."""
        return len(self._pages(client, slug, max_pages=1)), None
