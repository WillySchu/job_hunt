"""Fit scores: how well you match what a posting asks for, 0 to 100.

This module holds the resume, the skills profile drawn from it, the keyword
scorer, and the fit_scores table. Scores are frozen: a posting is scored once
per scorer, against the resume as it was that day, and the score is hidden
until `fit.show` turns it on (FIT_SCORER_SPEC.md).

Keyword score, out of 100:
  skills 60  the share of the technical terms a posting names that you have;
             terms under a requirements heading count double those under
             nice-to-have, a basic skill counts half, and a posting that
             names few known terms is pulled toward half marks
  level  20  the posting's seniority against seniority.prefer
  years  10  a stated "N+ years" against your years of experience
  focus  10  backend terms against frontend terms in the description
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

from jobfeed.config import Config, FitRules
from jobfeed.http import FetchError, NotFound, get_text
from jobfeed.models import Job
from jobfeed.normalize.seniority import RANKS
from jobfeed.store import Store, _ts

KEYWORDS = "keywords"
KEYWORDS_VERSION = "keywords-1"
# Skills shrink toward half marks by this much weight (two required terms' worth): a posting naming
# only a few known terms is less certain than one naming many, whichever way they point.
PRIOR_WEIGHT = 4
# A skill you have that implies another the resume may not spell out.
IMPLIES = {"PostgreSQL": ["SQL"], "MySQL": ["SQL"], "BigQuery": ["SQL"], "Snowflake": ["SQL"],
           "Kubernetes": ["Docker"], "Grafana": ["Observability"], "Prometheus": ["Observability"],
           "Datadog": ["Observability"]}
RESUME_CACHE = "resume-cache.md"


class ResumeError(Exception):
    """No resume to score against: it couldn't be fetched and there's no local copy."""


# --- the resume ---------------------------------------------------------------------------------

# An email address, or a phone number (+1 (206) 555-0100, 206.555.0100); not a date range like 2022-11 - 2025-05.
_CONTACT = re.compile(r"\S@\S|(?<!\d)(?:\+\d{1,3}[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]\d{4}(?!\d)")


@dataclass(frozen=True)
class Resume:
    text: str          # without the contact line
    hash: str          # 12 hex characters of its sha256
    source: str        # where this copy came from


def clean(text: str) -> str:
    """The resume minus any line with an email address or phone number."""
    return "\n".join(line for line in text.splitlines() if not _CONTACT.search(line)).strip() + "\n"


def load_resume(rules: FitRules, cache_dir: Path, client: httpx.Client) -> Resume:
    """Fetch the resume, keeping a local copy; fall back to that copy when GitHub can't be reached."""
    cache = cache_dir / RESUME_CACHE
    try:
        text = clean(get_text(client, rules.resume_url, retries=1))
        try:
            if not cache.exists() or cache.read_text() != text:
                cache.write_text(text)
        except OSError:
            pass   # a read-only directory (a dry run elsewhere) just means no copy
        source = rules.resume_url
    except (FetchError, NotFound) as exc:
        if not cache.exists():
            raise ResumeError(f"couldn't fetch the resume ({exc}) and there's no local copy") from exc
        text, source = cache.read_text(), f"{cache} (couldn't fetch: {exc})"
    if not text.strip():
        raise ResumeError("the resume is empty")
    return Resume(text=text, hash=hashlib.sha256(text.encode()).hexdigest()[:12], source=source)


# --- terms and your skills -------------------------------------------------------------------------

@dataclass(frozen=True)
class Term:
    name: str
    pattern: re.Pattern


def parse_terms(entries: list[str]) -> list[Term]:
    out = []
    for entry in entries:
        name, _, pattern = entry.partition(": ") if ": " in entry else (entry, "", re.escape(entry))
        out.append(Term(name.strip(), re.compile(rf"(?<![A-Za-z0-9])(?:{pattern})(?![A-Za-z0-9])", re.I)))
    return out


_BASIC_LINE = re.compile(r"\b(basic|familiar|exposure|some)\b", re.I)   # "Basic knowledge of C", "some front end work"
_DATES = re.compile(r"\b((?:19|20)\d\d)-\d\d\b")


def entries(text: str) -> list[str]:
    """The resume as whole entries: a bullet, a skill line or a paragraph, however it wraps."""
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append("")
        elif s.startswith(("-", ":", "*", "#", ">", "|")) or not out or not out[-1]:
            out.append(s)
        else:
            out[-1] += " " + s
    return [e for e in out if e]


@dataclass
class Profile:
    skills: dict[str, float]          # term name -> 1.0 strong, 0.5 basic
    years: int

    def describe(self) -> list[str]:
        strong = sorted(n for n, v in self.skills.items() if v == 1.0)
        basic = sorted(n for n, v in self.skills.items() if v < 1.0)
        return [f"Experience: {self.years} years",
                f"Strong ({len(strong)}): {', '.join(strong) or 'none'}",
                f"Basic ({len(basic)}): {', '.join(basic) or 'none'}"]


def build_profile(resume: Resume, terms: list[Term], rules: FitRules, now: datetime) -> Profile:
    """Your skills: the terms the resume names, basic where its line says so; then the config's overrides."""
    skills: dict[str, float] = {}
    for line in entries(resume.text):
        basic = bool(_BASIC_LINE.search(line))
        for term in terms:
            if term.pattern.search(line):
                level = 0.5 if basic else 1.0
                skills[term.name] = max(skills.get(term.name, 0.0), level)
    known = {t.name for t in terms}
    for have, implied in IMPLIES.items():
        for name in implied:
            if have in skills and name in known:
                skills[name] = max(skills.get(name, 0.0), skills[have])
    by_key = {t.name.casefold(): t.name for t in terms}
    for name in rules.extra_skills + rules.strong:
        skills[by_key[name.casefold()]] = 1.0
    for name in rules.basic:
        skills[by_key[name.casefold()]] = 0.5
    years = rules.experience_years
    if not years:
        starts = [int(y) for y in _DATES.findall(resume.text)]
        years = max(0, now.year - min(starts)) if starts else 0
    return Profile(skills=skills, years=years)


# --- reading a posting --------------------------------------------------------------------------------

_REQUIRED_HEAD = re.compile(r"requirement|qualification|what you('ll)? (need|bring)|must[- ]have|you (have|bring)|"
                            r"about you|you should have|what we('re)? look(ing)? for|who you are|skills", re.I)
_NICE_HEAD = re.compile(r"nice[- ]to[- ]have|bonus|preferred|pluses|plus if|extra credit|good to have|ideally", re.I)
_NICE_INLINE = re.compile(r"\b(nice to have|a plus|bonus|preferred|ideally)\b", re.I)
_YEARS = re.compile(r"\b(\d{1,2})\s*\+?\s*(?:or more\s+)?years?\b", re.I)
REQUIRED, PLAIN, NICE = 2, 1, 1


def _is_heading(line: str) -> bool:
    if line.lstrip().startswith(("- ", "• ", "* ", "+ ", "· ")):
        return False      # a bullet, however short
    s = line.strip().rstrip(":").strip("#*_ ")
    return bool(s) and len(s) <= 60 and (line.rstrip().endswith(":") or line.lstrip().startswith(("#", "**"))
                                         or (len(s.split()) <= 6 and not s.endswith(".")))


def weighted_lines(job: Job) -> list[tuple[str, int]]:
    """Each line of the posting with its weight: requirements count double, nice-to-haves once."""
    lines: list[tuple[str, int]] = [(job.title, REQUIRED)]
    section = PLAIN
    for raw in (job.description or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if _is_heading(line):
            if _NICE_HEAD.search(line):
                section = NICE
            elif _REQUIRED_HEAD.search(line):
                section = REQUIRED
            else:
                section = PLAIN
        weight = NICE if (section == REQUIRED and _NICE_INLINE.search(line)) else section
        lines.append((line, weight))
    return lines


# --- the keyword score -----------------------------------------------------------------------------------

@dataclass
class Score:
    scorer: str
    version: str
    score: int
    reason: str | None = None
    working: dict = field(default_factory=dict)


def _level_points(seniority: str | None, prefer: list[str]) -> tuple[int, str]:
    if not seniority:
        return 12, "unstated"
    if seniority in prefer:
        return 20, seniority
    gap = min(abs(RANKS.index(seniority) - RANKS.index(p)) for p in prefer) if prefer else 9
    return (10 if gap == 1 else 0), seniority


def keyword_score(job: Job, profile: Profile, terms: list[Term], config: Config,
                  frontend: re.Pattern | None, backend: re.Pattern | None) -> Score:
    lines = weighted_lines(job)
    weights: dict[str, int] = {}
    for term in terms:
        for line, w in lines:
            if term.pattern.search(line):
                weights[term.name] = max(weights.get(term.name, 0), w)
    total = sum(weights.values())
    have = sum(w * profile.skills.get(name, 0.0) for name, w in weights.items())
    skills = round(60 * (have + PRIOR_WEIGHT * 0.5) / (total + PRIOR_WEIGHT))   # no terms at all: 30
    level, level_seen = _level_points(job.seniority, config.seniority.prefer)
    asked = [int(n) for line, _ in lines for n in _YEARS.findall(line) if 1 <= int(n) <= 20]
    need = max(asked) if asked else None
    years = 10 if need is None or profile.years >= need else 0
    desc = job.description or ""
    b = len(backend.findall(desc)) if backend else 0
    f = len(frontend.findall(desc)) if frontend else 0
    focus = 8 if b + f == 0 else round(10 * b / (b + f))
    working = {
        "parts": {"skills": skills, "level": level, "years": years, "focus": focus},
        "matched": sorted(n for n in weights if profile.skills.get(n, 0) >= 1.0),
        "basic": sorted(n for n in weights if 0 < profile.skills.get(n, 0) < 1.0),
        "missing": sorted(n for n in weights if not profile.skills.get(n)),
        "required": sorted(n for n, w in weights.items() if w == REQUIRED),
        "level": level_seen, "years_asked": need, "backend_frontend": [b, f], "chars": len(desc),
    }
    return Score(KEYWORDS, KEYWORDS_VERSION, max(0, min(100, skills + level + years + focus)), working=working)


# --- storing ------------------------------------------------------------------------------------------------

def save_score(store: Store, job_id: str, s: Score, resume: Resume, now: datetime) -> None:
    store.conn.execute(
        "INSERT OR REPLACE INTO fit_scores (job_id, scorer, version, resume_hash, score, reason, working, scored_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, s.scorer, s.version, resume.hash, s.score, s.reason, json.dumps(s.working, sort_keys=True),
         _ts(now)))


def unscored(store: Store, job_ids: list[str], scorer: str) -> list[str]:
    if not job_ids:
        return []
    done = set()
    for i in range(0, len(job_ids), 500):
        chunk = job_ids[i:i + 500]
        done |= {j for (j,) in store.conn.execute(
            f"SELECT job_id FROM fit_scores WHERE scorer = ? AND job_id IN ({','.join('?' * len(chunk))})",
            (scorer, *chunk))}
    return [j for j in job_ids if j not in done]


def counts(store: Store) -> dict[str, int]:
    return dict(store.conn.execute("SELECT scorer, COUNT(*) FROM fit_scores GROUP BY scorer"))


# --- a run's scoring -----------------------------------------------------------------------------------------

@dataclass
class Scored:
    scored: int = 0
    resume: Resume | None = None
    problem: str | None = None

    def note(self) -> str | None:
        if self.problem:
            return f"Fit scores: none this run; {self.problem}."
        if self.scored:
            return f"Fit scores: {self.scored} posting{'' if self.scored == 1 else 's'} scored (hidden for now)."
        return None


class Scorer:
    """Everything the keyword scorer needs, loaded once per run."""

    def __init__(self, config: Config, resume: Resume, now: datetime):
        from jobfeed.filters import _words
        self.config = config
        self.resume = resume
        self.terms = parse_terms(config.fit.terms + config.fit.extra_terms)
        self.profile = build_profile(resume, self.terms, config.fit, now)
        self.frontend = _words(config.role.frontend_terms)
        self.backend = _words(config.role.backend_terms)

    def score(self, job: Job) -> Score:
        return keyword_score(job, self.profile, self.terms, self.config, self.frontend, self.backend)


def score_jobs(store: Store, jobs: list[Job], config: Config, now: datetime, client: httpx.Client,
               cache_dir: Path) -> Scored:
    """Keyword-score the postings that have no score yet. Never raises: a problem comes back as a note."""
    result = Scored()
    todo_ids = set(unscored(store, [j.id for j in jobs], KEYWORDS))
    todo = [j for j in jobs if j.id in todo_ids]
    if not todo:
        return result
    try:
        result.resume = load_resume(config.fit, cache_dir, client)
    except ResumeError as exc:
        result.problem = str(exc)
        return result
    scorer = Scorer(config, result.resume, now)
    with store.conn:
        for job in todo:
            save_score(store, job.id, scorer.score(job), result.resume, now)
            result.scored += 1
    return result


def sent_postings(store: Store) -> list[Job]:
    """Every posting a digest has carried."""
    return [r.job for r in store.all_rows() if r.code is not None]
