"""The normalized Job record every source adapter returns."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Job:
    id: str                         # "{source}:{native_id}", e.g. "greenhouse:4412983"
    source: str                     # greenhouse | lever | ashby | remotive | remoteok | himalayas
    company: str
    title: str
    url: str                        # the source's own page (keeps attribution terms)
    location_raw: str = ""          # exactly as posted
    work_mode: str = "unknown"      # remote | hybrid | onsite | unknown  (filled in milestone 2)
    remote_region: str | None = None  # us | americas | global | other  (milestone 2)
    cities: list[str] = field(default_factory=list)  # normalized city names (milestone 2)
    salary_min: int | None = None   # annual USD (milestone 2)
    salary_max: int | None = None
    salary_raw: str | None = None
    seniority: str | None = None    # junior | mid | senior | staff | principal (milestone 2)
    posted_at: datetime | None = None
    description: str = ""           # plain text, HTML stripped

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["posted_at"] = self.posted_at.isoformat() if self.posted_at else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Job:
        d = dict(d)
        posted = d.get("posted_at")
        d["posted_at"] = datetime.fromisoformat(posted) if posted else None
        d["cities"] = list(d.get("cities") or [])
        return cls(**d)
