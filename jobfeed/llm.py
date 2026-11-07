"""The LLM fit scorer: Claude reads a posting against your resume and returns a score and a reason.

One Messages API call per posting, over the same httpx client as everything
else (no SDK on the Pi):

- the system prompt (instructions, rubric, resume, context note) is the same
  for every posting, so it's cached and later calls in a run read it at a
  tenth of the price;
- structured outputs (`output_config.format`) guarantee the reply is JSON
  matching {"score": integer, "reason": string};
- effort `low`: scoring doesn't need long thinking, and thinking is billed as
  output.

Cost comes back with every response (`usage`) and is kept with the score, so
the footer and the monthly budget use what was actually spent. A posting that
can't be scored is left for the next run; scoring never stops the digest.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx

from jobfeed.config import Config
from jobfeed.fit import Resume, Score, save_score
from jobfeed.mailer import read_env_file
from jobfeed.models import Job
from jobfeed.normalize import format_salary
from jobfeed.store import Store, _ts

LLM = "llm"
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2026-10-21"
KEY_NAME = "ANTHROPIC_API_KEY"
MAX_TOKENS = 2000
MAX_DESCRIPTION_CHARS = 15_000
ATTEMPTS = 3
RETRY_STATUSES = {429, 500, 502, 503, 504, 529}
REASON_CHARS = 160

# USD per million tokens for prompts up to 100,000 tokens (Claude Haiku 5.5, November 2026):
# https://platform.claude.com/docs/en/about-claude/pricing
PRICES = {
    "claude-haiku-5-5": {"input": 0.10, "cache_write": 0.125, "cache_read": 0.01, "output": 0.50},
}

SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "0 to 100"},
        "reason": {"type": "string", "description": "one sentence: the main strength and the main gap"},
    },
    "required": ["score", "reason"],
    "additionalProperties": False,
}

INSTRUCTIONS = """You screen software engineering job postings for one candidate. For each posting, judge how well the candidate's background matches what the posting asks for, the way an experienced technical recruiter screening resumes for this role would. The question is whether this resume would be advanced to a first conversation, not whether the candidate would enjoy the job.

Weigh, in this order:
1. The core requirements: the main language and stack, the kind of systems, and the domain. Equivalent experience counts (a different relational database, a comparable cloud), but a different core stack or a specialist domain the resume never touches does not.
2. Level and years: whether the resume's scope of work and years match the level asked for.
3. Nice-to-haves, which matter only a little.

Do not reward keyword overlap alone; read what the work actually is. Ignore location, salary, remote policy and company size: those are already filtered.

Score from 0 to 100:
- 90-100: meets every stated requirement at the right level.
- 70-89: meets the core requirements, with minor gaps.
- 50-69: misses one core requirement, or the level is off by one step.
- 30-49: a different core stack, domain or level.
- 0-29: a different kind of role.

Give the reason as one sentence of at most 25 words naming the main strength and the main gap."""


@dataclass(frozen=True)
class Prompt:
    system: str
    version: str           # model/prompt-<hash of the instructions>


def build_prompt(resume: Resume, config: Config) -> Prompt:
    note = config.fit.context_note.strip()
    system = INSTRUCTIONS + "\n\n<resume>\n" + resume.text.strip() + "\n</resume>"
    if note:
        system += "\n\n<context>\n" + note + "\n</context>"
    tag = hashlib.sha256(INSTRUCTIONS.encode()).hexdigest()[:6]
    return Prompt(system=system, version=f"{config.fit.llm_model}/prompt-{tag}")


def posting_text(job: Job) -> str:
    pay = format_salary(job.salary_min, job.salary_max) if (job.salary_min or job.salary_max) else "not stated"
    desc = (job.description or "").strip() or "(no description)"
    if len(desc) > MAX_DESCRIPTION_CHARS:
        desc = desc[:MAX_DESCRIPTION_CHARS] + "\n[description cut short]"
    return (f"<posting>\nTitle: {job.title}\nCompany: {job.company}\nLocation: {job.location_raw or 'not stated'}"
            f"\nWork mode: {job.work_mode}\nLevel: {job.seniority or 'not stated'}\nPay: {pay}\n\n{desc}\n</posting>")


# --- the key ----------------------------------------------------------------------------------------

def load_key(env_path: Path, environ: dict | None = None) -> str | None:
    environ = os.environ if environ is None else environ
    if environ.get(KEY_NAME, "").strip():
        return environ[KEY_NAME].strip()
    if env_path.exists():
        value = read_env_file(env_path).get(KEY_NAME, "").strip()
        return value or None
    return None


# --- one call ------------------------------------------------------------------------------------------

class LLMError(Exception):
    """This posting couldn't be scored this time."""


class KeyRejected(LLMError):
    """The API key was refused: nothing will score until it's fixed."""


def cost(usage: dict, model: str) -> float:
    p = PRICES.get(model, PRICES["claude-haiku-5-5"])
    return (usage.get("input_tokens", 0) * p["input"] + usage.get("cache_creation_input_tokens", 0) * p["cache_write"]
            + usage.get("cache_read_input_tokens", 0) * p["cache_read"]
            + usage.get("output_tokens", 0) * p["output"]) / 1_000_000


def request_body(prompt: Prompt, job: Job, config: Config) -> dict:
    return {
        "model": config.fit.llm_model,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": prompt.system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": posting_text(job)}],
        "output_config": {"effort": config.fit.llm_effort, "format": {"type": "json_schema", "schema": SCHEMA}},
    }


def _delay(response: httpx.Response | None, attempt: int) -> float:
    if response is not None and response.headers.get("retry-after", "").isdigit():
        return min(float(response.headers["retry-after"]), 60.0)
    return 2.0 * (2 ** attempt)


def call(client: httpx.Client, key: str, body: dict, *, sleep: Callable[[float], None] = time.sleep) -> dict:
    headers = {"x-api-key": key, "anthropic-version": API_VERSION, "content-type": "application/json",
               "accept": "application/json"}
    problem = "no attempt"
    for attempt in range(ATTEMPTS):
        response = None
        try:
            response = client.post(API_URL, headers=headers, json=body, timeout=120)
        except httpx.TransportError as exc:
            problem = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code in (401, 403):
                raise KeyRejected(f"the API refused the key (HTTP {response.status_code}); check {KEY_NAME} in .env")
            if response.status_code == 200:
                return response.json()
            if response.status_code not in RETRY_STATUSES:
                raise LLMError(f"HTTP {response.status_code}: {response.text[:200]}")
            problem = f"HTTP {response.status_code}"
        if attempt < ATTEMPTS - 1:
            sleep(_delay(response, attempt))
    raise LLMError(f"{problem} after {ATTEMPTS} tries")


def parse(reply: dict, prompt: Prompt, model: str) -> Score:
    if reply.get("stop_reason") in ("refusal", "max_tokens"):
        raise LLMError(f"the reply stopped early ({reply.get('stop_reason')})")
    texts = [b.get("text", "") for b in reply.get("content", []) if b.get("type") == "text"]
    try:
        data = json.loads(texts[-1])
        score = int(data["score"])
        reason = str(data["reason"]).strip()
    except (IndexError, ValueError, KeyError, TypeError) as exc:
        raise LLMError(f"the reply wasn't the expected JSON ({exc})") from exc
    usage = reply.get("usage", {})
    if len(reason) > REASON_CHARS:
        reason = reason[:REASON_CHARS - 1].rstrip() + "…"
    working = {"usage": {k: usage.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                       "cache_read_input_tokens", "output_tokens")},
               "cost_usd": round(cost(usage, model), 6), "model": reply.get("model", model)}
    return Score(LLM, prompt.version, max(0, min(100, score)), reason=reason, working=working)


def score_one(client: httpx.Client, key: str, prompt: Prompt, job: Job, config: Config,
              sleep: Callable[[float], None] = time.sleep) -> Score:
    return parse(call(client, key, request_body(prompt, job, config), sleep=sleep), prompt, config.fit.llm_model)


# --- a run's worth ----------------------------------------------------------------------------------------

def spent_this_month(store: Store, now: datetime) -> float:
    start = now.astimezone().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = 0.0
    for (working,) in store.conn.execute("SELECT working FROM fit_scores WHERE scorer = ? AND scored_at >= ?",
                                         (LLM, _ts(start))):
        try:
            total += float(json.loads(working).get("cost_usd", 0))
        except (ValueError, TypeError, AttributeError):
            pass
    return total


@dataclass
class Batch:
    scored: int = 0
    failed: int = 0
    left: int = 0              # not tried: the per-run cap or the budget
    cost: float = 0.0
    month: float = 0.0         # spent this month, after this batch
    stopped: str | None = None
    errors: list[str] = field(default_factory=list)


def score_batch(store: Store, jobs: list[Job], resume: Resume, config: Config, key: str, client: httpx.Client,
                now: datetime, *, sleep: Callable[[float], None] = time.sleep) -> Batch:
    """Score these postings (in order), four at a time, within the per-run cap and the monthly budget."""
    rules = config.fit
    batch = Batch(month=spent_this_month(store, now))
    if batch.month >= rules.llm_monthly_budget:
        batch.left, batch.stopped = len(jobs), f"this month's ${rules.llm_monthly_budget:.2f} budget is spent"
        return batch
    todo = jobs[:rules.llm_max_per_run]
    batch.left = len(jobs) - len(todo)
    prompt = build_prompt(resume, config)
    # The first call writes the cache; the rest read it.
    queue = list(todo)
    while queue:
        if batch.month >= rules.llm_monthly_budget:
            batch.left += len(queue)
            batch.stopped = f"this month's ${rules.llm_monthly_budget:.2f} budget is spent"
            break
        group, queue = (queue[:1], queue[1:]) if batch.scored + batch.failed == 0 else \
            (queue[:rules.llm_concurrency * 4], queue[rules.llm_concurrency * 4:])
        with ThreadPoolExecutor(max_workers=rules.llm_concurrency) as pool:
            futures = [(job, pool.submit(score_one, client, key, prompt, job, config, sleep)) for job in group]
            results = []
            for job, fut in futures:
                try:
                    results.append((job, fut.result()))
                except KeyRejected as exc:
                    batch.stopped = str(exc)
                    batch.failed += 1
                except LLMError as exc:
                    batch.failed += 1
                    batch.errors.append(f"{job.id}: {exc}")
        with store.conn:
            for job, s in results:
                save_score(store, job.id, s, resume, now)
                batch.scored += 1
                batch.cost += s.working["cost_usd"]
                batch.month += s.working["cost_usd"]
        if batch.stopped:
            batch.left += len(queue)
            break
    return batch
