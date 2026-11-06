"""Fill in a Job's work mode, region, cities, states, salary and seniority.

Adapters set whatever their source states outright (Ashby's workplace type,
an aggregator's salary field); the normalizer only fills fields still at
their defaults, so structured data always beats text parsing.
"""

from __future__ import annotations

from dataclasses import replace

from jobfeed.models import Job
from jobfeed.normalize.location import LocationInfo, parse_location
from jobfeed.normalize.salary import SalaryInfo, format_salary, parse_salary
from jobfeed.normalize.seniority import parse_seniority

__all__ = [
    "LocationInfo", "SalaryInfo", "format_salary", "normalize_job", "parse_location", "parse_salary",
    "parse_seniority",
]


def normalize_job(job: Job) -> Job:
    loc = parse_location(job.location_raw, job.description, assume_remote=job.work_mode == "remote")
    work_mode = job.work_mode if job.work_mode != "unknown" else loc.work_mode
    remote = work_mode == "remote"

    updates: dict = {
        "work_mode": work_mode,
        "remote_region": job.remote_region or (loc.remote_region if remote else None),
        "cities": job.cities or loc.cities,
        "remote_states": job.remote_states or (loc.remote_states if remote else []),
        "excluded_states": job.excluded_states or loc.excluded_states,
        "seniority": job.seniority or parse_seniority(job.title),
    }

    if job.salary_min is None and job.salary_max is None:
        pay = parse_salary(job.salary_raw, job.description)
        if pay.found:
            updates.update(salary_min=pay.min, salary_max=pay.max, salary_currency=pay.currency,
                           salary_raw=pay.raw)
    elif job.salary_currency is None:
        updates["salary_currency"] = "USD"

    return replace(job, **updates)
