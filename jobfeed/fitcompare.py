"""Comparing the fit scorers before replies can: with each other, and with your own blind ratings.

`jobfeed fit rate` shows postings one at a time, with no scores, and asks how
strong a candidate you are for each, 1 to 5. `jobfeed fit compare` then reports:

- agreement: how closely the two scorers rank the same postings (Spearman's
  rank correlation, -1 to 1), and each one's spread;
- your ratings: each scorer's rank correlation with them, with a likely range,
  and whether one tracks you better. The two correlations share postings, so
  the difference is judged by resampling the rated postings (a paired
  bootstrap), not by comparing two separate ranges;
- the ten postings where the scorers disagree most, each with the LLM's reason
  and the keywords found, but only postings first sent over two weeks ago, so
  this can't steer what you apply to.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, TextIO

from jobfeed.models import Job
from jobfeed.normalize import format_salary
from jobfeed.store import Store, _ts

Z90 = 1.6448536269514722
MIN_RATED = 15
BOOTSTRAPS = 2000
DISAGREE_AFTER = timedelta(days=14)
DISAGREE_LISTED = 10
RATE_WINDOW = timedelta(days=30)


# --- rank correlation -----------------------------------------------------------------------------

def ranks(values: list[float]) -> list[float]:
    """1-based ranks, ties sharing their average rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def pearson(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3:
        return None
    mx, my = statistics.fmean(x), statistics.fmean(y)
    sx = math.sqrt(sum((a - mx) ** 2 for a in x))
    sy = math.sqrt(sum((b - my) ** 2 for b in y))
    if sx == 0 or sy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy)


def spearman(x: list[float], y: list[float]) -> float | None:
    return pearson(ranks(x), ranks(y))


def rho_range(rho: float, n: int) -> tuple[float, float]:
    """A 90% range for a rank correlation (Fisher's z, with the Spearman correction)."""
    if n < 4:
        return -1.0, 1.0
    r = max(-0.9999, min(0.9999, rho))
    z, se = math.atanh(r), math.sqrt((1 + r * r / 2) / (n - 3))
    return math.tanh(z - Z90 * se), math.tanh(z + Z90 * se)


def strength(rho: float) -> str:
    a = abs(rho)
    word = "strong" if a >= 0.7 else "moderate" if a >= 0.4 else "weak" if a >= 0.2 else "little or none"
    return word + (" (and backwards)" if rho < 0 and a >= 0.2 else "")


def paired_difference(rows: list[tuple[float, float, float]], seed: int = 7) -> tuple[float, float, float] | None:
    """rows: (rating, llm, keywords). The difference in rank correlation with the rating, LLM minus keywords,
    and its 90% range from resampling the rated postings."""
    def diff(sample):
        rating = [r for r, _, _ in sample]
        a = spearman(rating, [l for _, l, _ in sample])
        b = spearman(rating, [k for _, _, k in sample])
        return None if a is None or b is None else a - b

    point = diff(rows)
    if point is None:
        return None
    rng = random.Random(seed)
    draws = []
    for _ in range(BOOTSTRAPS):
        d = diff([rows[rng.randrange(len(rows))] for _ in rows])
        if d is not None:
            draws.append(d)
    if len(draws) < BOOTSTRAPS // 2:
        return None
    draws.sort()
    return point, draws[int(0.05 * len(draws))], draws[int(0.95 * len(draws)) - 1]


# --- the data ----------------------------------------------------------------------------------------

@dataclass
class Posting:
    job: Job
    emailed_at: datetime | None
    llm: int | None = None
    llm_reason: str | None = None
    keywords: int | None = None
    keywords_working: dict | None = None
    rating: int | None = None


def load(store: Store) -> list[Posting]:
    """Every posting the digest has sent, with its scores and your rating."""
    rows = {r.job.id: r for r in store.all_rows() if r.code is not None}
    out = {jid: Posting(r.job, r.emailed_at) for jid, r in rows.items()}
    for jid, scorer, score, reason, working in store.conn.execute(
            "SELECT job_id, scorer, score, reason, working FROM fit_scores"):
        p = out.get(jid)
        if p is None:
            continue
        if scorer == "llm":
            p.llm, p.llm_reason = score, reason
        elif scorer == "keywords":
            p.keywords = score
            p.keywords_working = json.loads(working) if working else {}
    for jid, rating in store.conn.execute("SELECT job_id, rating FROM fit_ratings"):
        if jid in out:
            out[jid].rating = rating
    return list(out.values())


# --- the report ----------------------------------------------------------------------------------------

def _spread(values: list[int]) -> str:
    q = statistics.quantiles(values, n=4) if len(values) >= 4 else [min(values), statistics.median(values), max(values)]
    return (f"median {statistics.median(values):.0f}, middle half {q[0]:.0f}–{q[2]:.0f}, "
            f"range {min(values)}–{max(values)}")


def _day(moment: datetime | None) -> str:
    if moment is None:
        return "date unknown"
    d = moment.astimezone()
    return f"{d:%b} {d.day}"


def report(postings: list[Posting], now: datetime) -> list[str]:
    both = [p for p in postings if p.llm is not None and p.keywords is not None]
    lines = []
    # 1. agreement
    lines.append(f"Agreement between the scorers ({len(both)} postings with both scores)")
    rho = spearman([p.llm for p in both], [p.keywords for p in both]) if len(both) >= 3 else None
    if rho is None:
        lines.append("  not enough postings with both scores yet")
    else:
        lines.append(f"  rank correlation {rho:.2f}: {strength(rho)}")
        lines.append(f"  {'LLM:':<10}{_spread([p.llm for p in both])}")
        lines.append(f"  {'keywords:':<10}{_spread([p.keywords for p in both])}")
    # 2. your ratings
    rated = [p for p in postings if p.rating is not None]
    lines += ["", f"Against your ratings ({len(rated)} rated)"]
    if len(rated) < MIN_RATED:
        lines.append(f"  too few to tell: {MIN_RATED} ratings are needed; `jobfeed fit rate` asks for them")
    else:
        for name, get in (("LLM", lambda p: p.llm), ("keywords", lambda p: p.keywords)):
            pairs = [(p.rating, get(p)) for p in rated if get(p) is not None]
            r = spearman([a for a, _ in pairs], [b for _, b in pairs]) if len(pairs) >= 3 else None
            if r is None:
                lines.append(f"  {name:<10}not enough rated postings with this score")
                continue
            lo, hi = rho_range(r, len(pairs))
            lines.append(f"  {name:<10}{r:.2f} (likely {lo:.2f} to {hi:.2f}, {len(pairs)} postings): {strength(r)}")
        rows = [(p.rating, p.llm, p.keywords) for p in rated if p.llm is not None and p.keywords is not None]
        diff = paired_difference(rows) if len(rows) >= MIN_RATED else None
        if diff is None:
            lines.append("  → too few to tell which tracks you better")
        else:
            d, lo, hi = diff
            if lo > 0:
                lines.append(f"  → the LLM tracks your ratings better: by {d:.2f} (likely {lo:.2f} to {hi:.2f})")
            elif hi < 0:
                lines.append(f"  → the keywords track your ratings better: by {-d:.2f} (likely {-hi:.2f} to {-lo:.2f})")
            else:
                lines.append(f"  → too few to tell which tracks you better: the difference, {d:+.2f}, is likely "
                             f"between {lo:+.2f} and {hi:+.2f}")
    # 3. disagreements, old enough to look at
    old = [p for p in both if p.emailed_at is not None and now - p.emailed_at >= DISAGREE_AFTER]
    old.sort(key=lambda p: (-abs(p.llm - p.keywords), p.job.id))
    lines += ["", f"Where they disagree most (postings first sent over {DISAGREE_AFTER.days} days ago; "
                  f"{len(old)} so far)"]
    if not old:
        lines.append("  none yet")
    for p in old[:DISAGREE_LISTED]:
        w = p.keywords_working or {}
        found = ", ".join(w.get("matched", [])[:6]) or "none"
        missing = ", ".join(w.get("missing", [])[:6]) or "none"
        lines.append(f"  {p.job.title} — {p.job.company} (sent {_day(p.emailed_at)})"
                     + (f" · you rated it {p.rating}" if p.rating is not None else ""))
        lines.append(f"    LLM {p.llm}: {p.llm_reason or ''}")
        lines.append(f"    keywords {p.keywords}: found {found}; missing {missing}")
        lines.append(f"    {p.job.url}")
    return lines


# --- rating ---------------------------------------------------------------------------------------------

def to_rate(postings: list[Posting], now: datetime) -> list[Posting]:
    """Unrated postings from the last 30 days, scored ones first, in an order that doesn't follow the scores."""
    fresh = [p for p in postings if p.rating is None and p.emailed_at is not None
             and now - p.emailed_at <= RATE_WINDOW]
    shuffle = lambda p: hashlib.sha256(p.job.id.encode()).hexdigest()   # noqa: E731  stable, unrelated to score
    return sorted(fresh, key=lambda p: (p.llm is None or p.keywords is None, shuffle(p)))


def excerpt(job: Job, lines: int = 12) -> list[str]:
    """The posting's requirements if it has a section for them, else its opening."""
    from jobfeed.fit import _REQUIRED_HEAD, _is_heading
    text = [l.strip() for l in (job.description or "").splitlines() if l.strip()]
    for i, line in enumerate(text):
        if _is_heading(line) and _REQUIRED_HEAD.search(line):
            return text[i:i + lines + 1]
    out, size = [], 0
    for line in text:
        if size > 700:
            break
        out.append(line)
        size += len(line)
    return out


def card(p: Posting, i: int, n: int) -> list[str]:
    job = p.job
    pay = format_salary(job.salary_min, job.salary_max) if (job.salary_min or job.salary_max) else "no salary listed"
    lines = ["", f"[{i}/{n}] {job.title} — {job.company}",
             f"  {job.location_raw or 'no location given'} · {job.work_mode} · {job.seniority or 'level unstated'} · {pay}"]
    lines += [f"  | {l}" for l in excerpt(job)]
    lines.append(f"  {job.apply_url or job.url}")
    return lines


def rate(store: Store, now: datetime, inp: TextIO, out: TextIO, count: int) -> tuple[int, int]:
    """Ask for up to `count` ratings. Returns (rated this session, rated in all)."""
    queue = to_rate(load(store), now)[:count]
    done = 0
    if not queue:
        print("Nothing to rate: every posting sent in the last 30 days has a rating.", file=out)
    else:
        print("How strong a candidate are you for each? 1 (not at all) to 5 (an excellent fit). "
              "s skips, q stops; no scores are shown.", file=out)
    for i, p in enumerate(queue, 1):
        print("\n".join(card(p, i, len(queue))), file=out)
        while True:
            print("  rating (1-5, s, q): ", end="", file=out)
            out.flush()
            answer = inp.readline()
            if not answer:
                answer = "q"
            answer = answer.strip().lower()
            if answer in ("1", "2", "3", "4", "5", "s", "q"):
                break
        if answer == "q":
            break
        if answer == "s":
            continue
        with store.conn:
            store.conn.execute("INSERT OR REPLACE INTO fit_ratings (job_id, rating, rated_at) VALUES (?, ?, ?)",
                               (p.job.id, int(answer), _ts(now)))
        done += 1
    total = store.conn.execute("SELECT COUNT(*) FROM fit_ratings").fetchone()[0]
    return done, total
