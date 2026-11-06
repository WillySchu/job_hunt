"""Remote-job aggregators: Remotive, Remote OK and Himalayas.

All three list remote jobs only, across many companies, so there's no slug
per company: a Board's slug is the category, tag list or search query.

Their terms ask for a link back to the posting on their site and for credit
as the source, so Job.url is always the aggregator's page and the digest says
"via Remotive" etc. A company board link found in the posting goes in
Job.apply_url, which is also what slug harvesting reads.
"""

from __future__ import annotations

from typing import Any

import httpx

from jobfeed.http import get_json
from jobfeed.models import Job
from jobfeed.sources.base import Board
from jobfeed.sources.common import annual_multiplier, first_board_link, join_unique, parse_time
from jobfeed.text import html_to_text


def _money(low: Any, high: Any, currency: str | None, period: str | None) -> dict[str, Any]:
    """Structured pay -> Job salary fields; zeros and blanks count as 'not stated'."""
    nums = [v if isinstance(v, (int, float)) and v > 0 else None for v in (low, high)]
    if nums == [None, None]:
        return {}
    factor = annual_multiplier(period)
    if factor is None:
        return {}
    low2, high2 = (round(v * factor) if v is not None else None for v in nums)
    code = (currency or "USD").upper()
    if code == "USD":
        return {"salary_min": low2, "salary_max": high2, "salary_currency": "USD"}
    shown = "–".join(f"{v:,}" for v in (low2, high2) if v is not None)
    return {"salary_currency": code, "salary_raw": f"{code} {shown}"}


# --- Remotive --------------------------------------------------------------------

class RemotiveSource:
    """GET https://remotive.com/api/remote-jobs?category=software-dev

    Remotive asks for at most 4 calls a day and blocks more than 2 a minute;
    one call per run stays well inside that. Listings arrive 24 hours late.
    """

    name = "remotive"
    API = "https://remotive.com/api/remote-jobs"

    def fetch(self, client: httpx.Client, board: Board) -> list[Job]:
        params = {"category": board.slug} if board.slug and board.slug != "all" else None
        data = get_json(client, self.API, params=params)
        return [self.parse(raw) for raw in (data or {}).get("jobs") or [] if isinstance(raw, dict) and raw.get("id")]

    @staticmethod
    def parse(raw: dict[str, Any]) -> Job:
        html = raw.get("description") or ""
        return Job(
            id=f"remotive:{raw['id']}",
            source="remotive",
            company=(raw.get("company_name") or "").strip() or "Unknown company",
            title=(raw.get("title") or "").strip(),
            url=raw.get("url") or "",
            apply_url=first_board_link(html),
            location_raw=(raw.get("candidate_required_location") or "").strip(),
            work_mode="remote",
            posted_at=_utc(parse_time(raw.get("publication_date"))),
            description=html_to_text(html),
            salary_raw=(raw.get("salary") or "").strip() or None,
        )

    def probe(self, client: httpx.Client, slug: str) -> tuple[int, str | None]:
        raise NotImplementedError("aggregators aren't company boards")


# --- Remote OK ---------------------------------------------------------------------

class RemoteOKSource:
    """GET https://remoteok.com/api (optionally ?tags=a,b)

    Returns the newest ~100 postings with no paging, so it has to run daily.
    The first array element is Remote OK's legal notice, not a job.
    """

    name = "remoteok"
    API = "https://remoteok.com/api"

    def fetch(self, client: httpx.Client, board: Board) -> list[Job]:
        params = {"tags": board.slug} if board.slug and board.slug != "all" else None
        data = get_json(client, self.API, params=params)
        rows = data if isinstance(data, list) else []
        return [self.parse(raw) for raw in rows if isinstance(raw, dict) and raw.get("id") and raw.get("position")]

    @staticmethod
    def parse(raw: dict[str, Any]) -> Job:
        html = raw.get("description") or ""
        apply = raw.get("apply_url") or ""
        posted = parse_time(raw.get("date"))
        if posted is None and isinstance(raw.get("epoch"), (int, float)):
            posted = parse_time(raw["epoch"] * 1000)  # Remote OK's epoch is in seconds
        return Job(
            id=f"remoteok:{raw['id']}",
            source="remoteok",
            company=(raw.get("company") or "").strip() or "Unknown company",
            title=(raw.get("position") or "").strip(),
            url=raw.get("url") or "",
            apply_url=first_board_link(apply, html),
            location_raw=(raw.get("location") or "").strip(),
            work_mode="remote",
            posted_at=posted,
            description=html_to_text(html),
            **_money(raw.get("salary_min"), raw.get("salary_max"), raw.get("salary_currency"), "annual"),
        )

    def probe(self, client: httpx.Client, slug: str) -> tuple[int, str | None]:
        raise NotImplementedError("aggregators aren't company boards")


# --- Himalayas -------------------------------------------------------------------

class HimalayasSource:
    """GET https://himalayas.app/jobs/api/search?q=...&sort=recent&page=N[&country=US]

    Pages of up to 20 postings; page until one comes back empty or the board's
    `pages` option is reached. Free without a key; Himalayas asks for credit
    and a link to its page for each job shown.
    """

    name = "himalayas"
    API = "https://himalayas.app/jobs/api/search"

    def fetch(self, client: httpx.Client, board: Board) -> list[Job]:
        pages = max(1, min(int(board.option("pages", "1") or 1), 10))
        country = board.option("country")
        seen: set[str] = set()
        jobs: list[Job] = []
        for page in range(1, pages + 1):
            params = {"q": board.slug, "sort": "recent", "page": str(page)}
            if country:
                params["country"] = country
            data = get_json(client, self.API, params=params)
            rows = [r for r in (data or {}).get("jobs") or [] if isinstance(r, dict) and r.get("guid")]
            fresh = [r for r in rows if r["guid"] not in seen]
            if not fresh:
                break
            seen.update(r["guid"] for r in fresh)
            jobs += [self.parse(r) for r in fresh]
        return jobs

    @staticmethod
    def parse(raw: dict[str, Any]) -> Job:
        html = raw.get("description") or ""
        places = [str(p.get("name") or p.get("alpha2") or "") if isinstance(p, dict) else str(p)
                  for p in raw.get("locationRestrictions") or []]
        return Job(
            id=f"himalayas:{raw['guid']}",
            source="himalayas",
            company=(raw.get("companyName") or "").strip() or "Unknown company",
            title=(raw.get("title") or "").strip(),
            url=raw.get("applicationLink") or "",
            apply_url=first_board_link(html),
            location_raw=join_unique(places) or "Worldwide",  # no restrictions means worldwide
            work_mode="remote",
            posted_at=parse_time(raw.get("pubDate")),          # milliseconds
            description=html_to_text(html) or (raw.get("excerpt") or ""),
            **_money(raw.get("minSalary"), raw.get("maxSalary"), raw.get("currency"),
                     raw.get("salaryPeriod") or "annual"),
        )

    def probe(self, client: httpx.Client, slug: str) -> tuple[int, str | None]:
        raise NotImplementedError("aggregators aren't company boards")


def _utc(moment):
    """Remotive's dates carry no zone; they're UTC."""
    from datetime import timezone

    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


AGGREGATORS = ("remotive", "remoteok", "himalayas")
