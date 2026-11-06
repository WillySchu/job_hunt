"""Growing companies.yaml from aggregator postings that link to a company's own board."""

from __future__ import annotations

from jobfeed.companies import CompanyList
from jobfeed.config import Config
from jobfeed.discover import Discovery
from jobfeed.filters import Rules
from jobfeed.models import Job
from jobfeed.sources import AGGREGATORS
from jobfeed.sources.common import board_of


def harvest(jobs: list[Job], companies: CompanyList, config: Config) -> list[Discovery]:
    """Boards worth adding: one per company, from aggregator postings for engineering roles.

    Skipped: boards already in companies.yaml under any name, and companies
    already pointing at some other board. A company listed without a board
    yet (ats null or unsupported) gets this one.
    """
    rules = Rules(config)
    known_boards = {(c.ats, (c.slug or "").casefold()) for c in companies.companies if c.ats and c.slug}
    by_name = {c.name.casefold(): c for c in companies.companies}
    found: dict[tuple[str, str], Discovery] = {}
    taken_names: set[str] = set()

    for job in jobs:
        if job.source not in AGGREGATORS or not job.apply_url:
            continue
        if rules.check_role(job) is not None:
            continue  # only engineering roles earn a company a place on the poll list
        board = board_of(job.apply_url)
        if board is None:
            continue
        ats, slug = board
        key = (ats, slug.casefold())
        if key in known_boards or key in found:
            continue
        name = job.company.strip()
        existing = by_name.get(name.casefold())
        if existing and existing.ats not in (None, "unsupported"):
            continue
        if name.casefold() in taken_names:
            continue
        taken_names.add(name.casefold())
        found[key] = Discovery(name=existing.name if existing else name, ats=ats, slug=slug)
    return list(found.values())
