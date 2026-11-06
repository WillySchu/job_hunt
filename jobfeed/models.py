"""The normalized Job record every source adapter returns."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from typing import Any


@dataclass
class Job:
    id: str                         # "{source}:{native_id}", e.g. "greenhouse:4412983"
    source: str                     # greenhouse | lever | ashby | remotive | remoteok | himalayas
    company: str
    title: str
    url: str                        # the source's own page (keeps attribution terms)
    apply_url: str | None = None    # a company board link, when an aggregator posting has one
    location_raw: str = ""          # exactly as posted
    work_mode: str = "unknown"      # remote | hybrid | onsite | unknown
    remote_region: str | None = None  # us | americas | global | other; None = remote, region unstated
    cities: list[str] = field(default_factory=list)  # "Seattle, WA", "Seattle", "London, UK"
    remote_states: list[str] = field(default_factory=list)    # US states a remote hire may live in; [] = no limit stated
    excluded_states: list[str] = field(default_factory=list)  # US states the posting says it can't hire in
    salary_min: int | None = None   # annual USD
    salary_max: int | None = None
    salary_currency: str | None = None  # "USD"; another code means min/max are None and salary_raw has the figure
    salary_raw: str | None = None
    seniority: str | None = None    # junior | mid | senior | staff | principal
    posted_at: datetime | None = None
    description: str = ""           # plain text, HTML stripped

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["posted_at"] = self.posted_at.isoformat() if self.posted_at else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Job:
        known = {f.name for f in fields(cls)}
        d = {k: v for k, v in d.items() if k in known}  # rows stored by older versions still load
        posted = d.get("posted_at")
        d["posted_at"] = datetime.fromisoformat(posted) if posted else None
        for key in ("cities", "remote_states", "excluded_states"):
            d[key] = list(d.get(key) or [])
        return cls(**d)
