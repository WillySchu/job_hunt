"""A run's fit scoring: keywords for every new digest posting, then the LLM for those and any backlog.

The LLM backlog is every posting the digest has sent that has no LLM score
yet, newest first: postings from before the scorer existed, and ones a past
run couldn't finish (the per-run cap, the monthly budget, an API hiccup). So
backfilling happens by itself, a run at a time; `jobfeed fit backfill` does it
all at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from jobfeed import fit, llm
from jobfeed.config import Config
from jobfeed.models import Job
from jobfeed.store import Store


@dataclass
class Report:
    keywords: int = 0
    llm: llm.Batch | None = None
    llm_off: str | None = None     # why the LLM didn't run
    problem: str | None = None     # why nothing ran

    def note(self) -> str | None:
        if self.problem:
            return f"Fit scores: none this run; {self.problem}."
        parts = []
        if self.keywords:
            parts.append(f"{self.keywords} posting{'' if self.keywords == 1 else 's'} scored by keywords")
        b = self.llm
        if b and (b.scored or b.failed or b.left or b.stopped):
            text = f"{b.scored} by the LLM (${b.cost:.3f}; ${b.month:.2f} this month)"
            if b.failed:
                text += f", {b.failed} failed"
            if b.left:
                text += f", {b.left} left for later"
            if b.stopped:
                text += f"; paused: {b.stopped}"
            parts.append(text)
        if self.llm_off and parts:
            parts.append(f"LLM scoring off: {self.llm_off}")
        if not parts:
            return None
        return "Fit scores: " + "; ".join(parts) + " (hidden for now)."


def run(store: Store, digest_jobs: list[Job], config: Config, now: datetime, client: httpx.Client, cache_dir: Path,
        env_path: Path, *, everything: bool = False, sleep=None) -> Report:
    """Score this run's digest postings (or, with everything, every posting sent). Never raises."""
    report = Report()
    sent = fit.sent_postings(store)
    pool = sent if everything else digest_jobs
    kw_todo_ids = set(fit.unscored(store, [j.id for j in pool], fit.KEYWORDS))
    kw_todo = [j for j in pool if j.id in kw_todo_ids]

    key = None
    llm_todo: list[Job] = []
    if not config.fit.llm:
        report.llm_off = None
    else:
        key = llm.load_key(env_path)
        if key is None:
            report.llm_off = f"no {llm.KEY_NAME} in {env_path}"
        else:
            # This run's postings first, then the backlog, newest first.
            seen: set[str] = set()
            undated = datetime(1970, 1, 1, tzinfo=timezone.utc)
            ordered = list(digest_jobs) + sorted(sent, key=lambda j: j.posted_at or undated, reverse=True)
            ordered = [j for j in ordered if not (j.id in seen or seen.add(j.id))]
            todo_ids = set(fit.unscored(store, [j.id for j in ordered], llm.LLM))
            llm_todo = [j for j in ordered if j.id in todo_ids]

    if not kw_todo and not llm_todo:
        return report
    try:
        resume = fit.load_resume(config.fit, cache_dir, client)
    except fit.ResumeError as exc:
        report.problem = str(exc)
        return report
    if kw_todo:
        scorer = fit.Scorer(config, resume, now)
        with store.conn:
            for job in kw_todo:
                fit.save_score(store, job.id, scorer.score(job), resume, now)
        report.keywords = len(kw_todo)
    if llm_todo and key:
        kwargs = {"sleep": sleep} if sleep else {}
        report.llm = llm.score_batch(store, llm_todo, resume, config, key, client, now, **kwargs)
    return report


# A made-up posting for `jobfeed fit check`: it tests the key and the reply without scoring a real one.
CHECK_POSTING = Job(
    id="check:1", source="check", company="Example Co", title="Senior Backend Engineer",
    url="https://example.com/jobs/1", location_raw="Remote (US)", work_mode="remote", seniority="senior",
    description=("We're hiring a senior backend engineer to build Python and Go services on Kubernetes in AWS.\n\n"
                 "Requirements:\n- 5+ years building backend services\n- PostgreSQL and Redis\n"
                 "- Experience operating distributed systems in production\n\nNice to have:\n- Kafka\n- Terraform"))
