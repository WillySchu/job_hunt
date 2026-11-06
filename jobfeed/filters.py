"""The digest rules: role, then location, then salary. Seniority only sorts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import cached_property

from jobfeed.config import Config
from jobfeed.models import Job
from jobfeed.normalize import format_salary

WATCHLIST = "watchlist"
MATCHES = "matches"
NO_SALARY = "no_salary"
SECTION_ORDER = [WATCHLIST, MATCHES, NO_SALARY]
SECTION_TITLES = {WATCHLIST: "Watchlist", MATCHES: "New matches", NO_SALARY: "No salary listed"}

FULL_STACK_TAG = "possibly full stack"


@dataclass
class Verdict:
    passed: bool
    section: str | None = None          # watchlist | matches | no_salary, when passed
    reason: str | None = None           # why it was dropped, when not passed
    stage: str | None = None            # role | location | salary, when dropped
    tags: list[str] = field(default_factory=list)


def _words(patterns: list[str]) -> re.Pattern[str] | None:
    if not patterns:
        return None
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(f"(?:{p})" for p in patterns) + r")(?![A-Za-z0-9])",
                      re.IGNORECASE)


class Rules:
    """Compiled form of a Config."""

    def __init__(self, config: Config):
        self.config = config
        role, loc = config.role, config.location
        self.include = _words(role.include)
        self.exclude = _words(role.exclude)
        self.frontend = _words(role.frontend_terms)
        self.backend = _words(role.backend_terms)
        self.metro = {m.casefold() for m in loc.metro}
        self.metro_re = _words([re.escape(m) for m in loc.metro])
        self.regions = set(loc.remote_regions)

    @cached_property
    def rank(self) -> dict[str, int]:
        return {level: i for i, level in enumerate(self.config.seniority.prefer)}

    # --- stages -----------------------------------------------------------

    def check_role(self, job: Job) -> str | None:
        if self.include and not self.include.search(job.title):
            return "title has no engineering keyword"
        if self.exclude and (m := self.exclude.search(job.title)):
            return f"title says '{m.group(0)}'"
        return None

    def full_stack_tag(self, job: Job) -> bool:
        if not self.frontend or not job.description:
            return False
        fe = len(self.frontend.findall(job.description))
        be = len(self.backend.findall(job.description)) if self.backend else 0
        return fe > be

    def in_metro(self, job: Job) -> bool:
        home = self.config.location.home_state
        for city in job.cities:
            name, _, qual = city.partition(", ")
            if name.casefold() in self.metro and qual in ("", home):
                return True
        return False

    def check_location(self, job: Job) -> str | None:
        home = self.config.location.home_state
        if job.work_mode == "remote":
            region = job.remote_region or "unknown"
            problem = None
            if region not in self.regions:
                problem = f"remote is limited to region '{region}'"
            elif job.remote_states and home not in job.remote_states:
                problem = f"remote is limited to {', '.join(job.remote_states)}"
            elif home in job.excluded_states:
                problem = f"posting says it can't hire in {home}"
            if problem is None or self.in_metro(job):
                # A Seattle-area office is still an option when the remote one isn't.
                return None
            return problem
        if job.work_mode in ("hybrid", "onsite"):
            if self.in_metro(job):
                return None
            where = "; ".join(job.cities[:3]) or job.location_raw or "no city given"
            return f"{job.work_mode} outside the Seattle area ({where})"
        # unknown: pass if the text names remote or a metro city
        raw = job.location_raw or ""
        if re.search(r"\bremote\b", raw, re.IGNORECASE) or (self.metro_re and self.metro_re.search(raw)):
            return None
        return f"location unclear ({raw or 'none given'})"

    def usd_pay(self, job: Job) -> int | None:
        if job.salary_currency not in (None, "USD"):
            return None
        low, high = job.salary_min, job.salary_max
        if self.config.salary.compare == "max":
            return high if high is not None else low
        return low if low is not None else high

    def check_salary(self, job: Job) -> str | None:
        pay = self.usd_pay(job)
        floor = self.config.salary.floor_usd
        if pay is None:
            return None if self.config.salary.keep_missing else "no USD salary listed"
        if pay < floor:
            edge = "tops out at" if self.config.salary.compare == "max" else "starts at"
            return f"pay {edge} {format_salary(pay, pay)} (floor {format_salary(floor, floor)})"
        return None

    # --- verdict ----------------------------------------------------------

    def evaluate(self, job: Job, *, watch: bool = False) -> Verdict:
        for stage, check in (("role", self.check_role), ("location", self.check_location)):
            if reason := check(job):
                return Verdict(passed=False, reason=reason, stage=stage)
        if reason := self.check_salary(job):
            if not (watch and self.usd_pay(job) is None):
                return Verdict(passed=False, reason=reason, stage="salary")
        tags = [FULL_STACK_TAG] if self.full_stack_tag(job) else []
        if watch:
            section = WATCHLIST
        elif self.usd_pay(job) is not None:
            section = MATCHES
        else:
            section = NO_SALARY
        return Verdict(passed=True, section=section, tags=tags)

    def sort_key(self, job: Job) -> tuple[int, float]:
        rank = self.rank.get(job.seniority or "", len(self.rank))
        posted = job.posted_at or datetime.min.replace(tzinfo=timezone.utc)
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return (rank, -posted.timestamp())


@dataclass
class Entry:
    job: Job
    verdict: Verdict


@dataclass
class Digest:
    sections: dict[str, list[Entry]]
    dropped: list[Entry]
    hidden: dict[str, int]              # entries past max_per_section, per section

    @property
    def matched(self) -> int:
        return sum(len(v) for v in self.sections.values()) + sum(self.hidden.values())


def build_digest(jobs: list[Job], config: Config, *, watched: set[str] | None = None) -> Digest:
    """Filter, section and sort. `watched` holds casefolded company names on the watchlist."""
    rules = Rules(config)
    watched = watched or set()
    sections: dict[str, list[Entry]] = {s: [] for s in SECTION_ORDER}
    dropped: list[Entry] = []
    for job in jobs:
        verdict = rules.evaluate(job, watch=job.company.casefold() in watched)
        (sections[verdict.section] if verdict.passed else dropped).append(Entry(job, verdict))
    cap = config.digest.max_per_section
    hidden: dict[str, int] = {}
    for name, entries in sections.items():
        entries.sort(key=lambda e: rules.sort_key(e.job))
        if len(entries) > cap:
            hidden[name] = len(entries) - cap
            del entries[cap:]
    return Digest(sections=sections, dropped=dropped, hidden=hidden)
