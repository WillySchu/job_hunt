"""Deciding what a run sends: skip what's been sent, and send each role once across sources."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from jobfeed.config import Config
from jobfeed.filters import SECTION_ORDER, Digest, Rules, build_digest
from jobfeed.models import Job
from jobfeed.store import Store

DEDUPE_WINDOW = timedelta(days=30)
ATS_SOURCES = {"greenhouse", "lever", "ashby"}

_COMPANY_SUFFIX = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|labs|hq)\b\.?", re.IGNORECASE)
# Location tags aggregators bolt onto titles: "(Remote)", "- Remote US", "[USA]".
_TITLE_LOCATION = re.compile(
    r"\s*[\(\[]\s*(?:fully\s+)?(?:remote|hybrid|on-?site|us|usa|u\.s\.|united states|north america|americas|"
    r"worldwide|anywhere)[^\)\]]*[\)\]]"
    r"|\s+[-–—|]\s*(?:fully\s+)?(?:remote|hybrid|on-?site)\b.*$",
    re.IGNORECASE,
)


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9+#]+", " ", text.casefold()).strip()


def dedupe_key(job: Job) -> str:
    """'acme|senior backend engineer' for 'Acme, Inc.' + 'Senior Backend Engineer (Remote - US)'."""
    company = _squash(_COMPANY_SUFFIX.sub(" ", job.company)) or _squash(job.company)
    title = _squash(_TITLE_LOCATION.sub("", job.title)) or _squash(job.title)
    return f"{company}|{title}"


@dataclass
class Plan:
    digest: Digest
    keys: dict[str, str]                       # job id -> dedupe key, for every job this run
    matched: dict[str, bool]                   # job id -> passed the filters
    already_sent: list[Job] = field(default_factory=list)
    duplicates: list[Job] = field(default_factory=list)

    @property
    def to_send(self) -> list[str]:
        """Ids shown in the digest. Overflow past max_per_section waits for the next run."""
        return [e.job.id for entries in self.digest.sections.values() for e in entries]


def plan_run(jobs: list[Job], config: Config, *, watched: set[str], store: Store | None, now: datetime) -> Plan:
    """What this run's digest holds, given what jobs.db says has been sent.

    1. Filter every posting.
    2. Skip matches already sent.
    3. Skip matches whose role (same dedupe key) was sent in the last 30 days.
    4. Among this run's copies of one role, keep one: an ATS copy over an
       aggregator's, then the better section (watchlist first).
    """
    rules = Rules(config)
    keys = {j.id: dedupe_key(j) for j in jobs}
    verdicts = {j.id: rules.evaluate(j, watch=j.company.casefold() in watched) for j in jobs}
    matched = {id_: v.passed for id_, v in verdicts.items()}
    passing = [j for j in jobs if matched[j.id]]
    failing = [j for j in jobs if not matched[j.id]]

    known = store.get(j.id for j in passing) if store else {}
    already = [j for j in passing if (row := known.get(j.id)) and row.emailed_at]
    fresh = [j for j in passing if not ((row := known.get(j.id)) and row.emailed_at)]

    recent = store.emailed_keys_since((keys[j.id] for j in fresh), now - DEDUPE_WINDOW) if store else set()
    duplicates = [j for j in fresh if keys[j.id] in recent]
    fresh = [j for j in fresh if keys[j.id] not in recent]

    section_rank = {s: i for i, s in enumerate(SECTION_ORDER)}
    best: dict[str, Job] = {}
    for job in fresh:
        key = keys[job.id]

        def rank(j: Job) -> tuple:
            return (j.source not in ATS_SOURCES, section_rank[verdicts[j.id].section], j.id)

        if key not in best:
            best[key] = job
        elif rank(job) < rank(best[key]):
            duplicates.append(best[key])
            best[key] = job
        else:
            duplicates.append(job)

    digest = build_digest(list(best.values()) + failing, config, watched=watched)
    return Plan(digest=digest, keys=keys, matched=matched, already_sent=already, duplicates=duplicates)
