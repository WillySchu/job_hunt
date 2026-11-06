"""Helpers shared by source adapters."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# Two-letter country codes as names, so "CA" (Canada) isn't read as California.
COUNTRY_NAMES = {
    "US": "United States", "USA": "United States", "CA": "Canada", "GB": "United Kingdom", "UK": "United Kingdom",
    "IE": "Ireland", "DE": "Germany", "FR": "France", "ES": "Spain", "PT": "Portugal", "NL": "Netherlands",
    "PL": "Poland", "IN": "India", "AU": "Australia", "NZ": "New Zealand", "MX": "Mexico", "BR": "Brazil",
    "AR": "Argentina", "CO": "Colombia", "SG": "Singapore", "JP": "Japan", "IL": "Israel", "CH": "Switzerland",
    "SE": "Sweden",
}

# Pay interval -> multiplier to annual.
_INTERVALS = [
    (re.compile(r"year|annual|yr", re.IGNORECASE), 1),
    (re.compile(r"month", re.IGNORECASE), 12),
    (re.compile(r"fortnight|biweek|bi-week", re.IGNORECASE), 26),
    (re.compile(r"week", re.IGNORECASE), 52),
    (re.compile(r"day|daily", re.IGNORECASE), 260),
    (re.compile(r"hour", re.IGNORECASE), 2080),
]


def annual_multiplier(interval: str | None) -> int | None:
    """'per-year-salary' / '1 YEAR' -> 1, '1 HOUR' -> 2080; None for one-time or unknown intervals."""
    if not interval:
        return 1
    for pattern, factor in _INTERVALS:
        if pattern.search(interval):
            return factor
    return None


def country_name(code: Any) -> str | None:
    if not code or not isinstance(code, str):
        return None
    return COUNTRY_NAMES.get(code.strip().upper(), code.strip() if len(code.strip()) > 3 else None)


def with_country(location: str, country: str | None) -> str:
    """Append the country when the location text doesn't already name it."""
    if not country or country.casefold() in location.casefold():
        return location
    return f"{location} ({country})" if location else country


def parse_time(value: Any) -> datetime | None:
    """ISO strings ('2026-10-20T14:02:11.000Z') or epoch milliseconds (Lever)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


# Company job boards on the ATSes jobfeed can poll, as they appear in apply links.
BOARD_LINKS = [
    ("greenhouse", re.compile(
        r"https?://(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)",
        re.IGNORECASE)),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_app\?(?:[^\s\"'<>]*&)?for=([A-Za-z0-9_-]+)",
                              re.IGNORECASE)),
    ("lever", re.compile(r"https?://jobs(?:\.eu)?\.lever\.co/([A-Za-z0-9_.-]+)", re.IGNORECASE)),
    ("ashby", re.compile(r"https?://jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.IGNORECASE)),
]
_NOT_SLUGS = {"embed", "v1", "boards", "jobs", "api", "job_app", "job_board", "search"}
_URL = re.compile(r"https?://[^\s\"'<>)]+", re.IGNORECASE)


def board_of(url: str) -> tuple[str, str] | None:
    """('greenhouse', 'stripe') for a Greenhouse/Lever/Ashby job link, else None."""
    from urllib.parse import unquote

    for ats, pattern in BOARD_LINKS:
        if m := pattern.search(url):
            slug = unquote(m.group(1)).strip()
            if slug and slug.lower() not in _NOT_SLUGS:
                return ats, slug
    return None


def first_board_link(*texts: str | None) -> str | None:
    """The first URL in these texts (raw HTML is fine) that points at a company board."""
    for text in texts:
        for url in _URL.findall(text or ""):
            if board_of(url):
                return url.rstrip(".,;")
    return None


def join_unique(parts: list[str], sep: str = "; ") -> str:
    out: list[str] = []
    for p in parts:
        p = (p or "").strip()
        if p and p not in out:
            out.append(p)
    return sep.join(out)
