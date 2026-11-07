"""Rates from the applications you've logged, split by what might change them.

Every rate is a count with a likely range ("3 of 24 replied (13%, likely 5–27%)"):
a 90% Wilson interval, which behaves at the small counts a job search has.
Rates only count applications that have had time to resolve, at least
`tracking.no_response_days` old or already closed, so a week of fresh
applications doesn't drag every rate down.

A split compares its highest and lowest groups. It says "too few to tell" when
either has fewer than 15 counted applications, or when one group's rate falls
inside the other's likely range.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

from jobfeed.config import TrackingRules
from jobfeed.filters import SECTION_TITLES
from jobfeed.models import Job
from jobfeed.sources import SOURCE_NAMES
from jobfeed.store import Store
from jobfeed import tracker
from jobfeed.tracker import OPEN_STAGES, Application

Z90 = 1.6448536269514722     # two-sided 90%
MIN_GROUP = 15
RECENT_DAYS = 28             # the funnel's "last 4 weeks"
QUIET_LISTED = 5             # open applications listed as longest without news

RESPONSES = {"reply", "screen", "interview", "offer", "accepted", "declined", "rejected"}
REACHED = {"screen": 1, "interview": 2, "offer": 3, "accepted": 3, "declined": 3}
METRICS = [("replied", "Replied", 0), ("screen", "Screen", 1), ("interview", "Interview", 2), ("offer", "Offer", 3)]


def wilson(k: int, n: int, z: float = Z90) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def pct(x: float) -> int:
    """A share as a whole percent, halves rounded up (12.5% -> 13%)."""
    return int(math.floor(x * 100 + 0.5))


@dataclass(frozen=True)
class Rate:
    k: int
    n: int

    @property
    def p(self) -> float:
        return self.k / self.n if self.n else 0.0

    @property
    def range(self) -> tuple[float, float]:
        return wilson(self.k, self.n)

    def text(self, verb: str = "") -> str:
        if not self.n:
            return "none counted yet"
        lo, hi = self.range
        return f"{self.k} of {self.n}{' ' + verb if verb else ''} ({pct(self.p)}%, likely {pct(lo)}–{pct(hi)}%)"


# --- each application, boiled down -----------------------------------------------------------

@dataclass
class Facts:
    app: Application
    job: Job | None
    replied: bool
    reached: int                   # 0 applied, 1 screen, 2 interview, 3 offer
    days_to_reply: int | None
    counted: bool                  # old enough, or closed, to count in rates

    @property
    def open(self) -> bool:
        return self.app.stage in OPEN_STAGES


def facts_for(app: Application, job: Job | None, now: datetime, rules: TrackingRules) -> Facts:
    reached, first = 0, None
    for e in app.events:
        if e.kind in REACHED:
            reached = max(reached, REACHED[e.kind])
        if e.kind in ("rejected", "withdrawn") and e.detail in REACHED:
            reached = max(reached, REACHED[e.detail])
        if e.kind in RESPONSES and e.origin != "rule" and (first is None or e.at < first):
            first = e.at
    days = max(0, (first - app.applied_at).days) if first else None
    counted = app.stage not in OPEN_STAGES or now - app.applied_at >= timedelta(days=rules.no_response_days)
    return Facts(app, job, first is not None, reached, days, counted)


def gather(store: Store, now: datetime, rules: TrackingRules, *, since: datetime | None = None) -> list[Facts]:
    if not store.readonly:
        with store.conn:
            tracker.sync_fit_scores(store)      # applications logged before their posting was scored
    apps = [tracker.get_application(store, a.code, with_events=True)
            for a in tracker.list_applications(store, include_closed=True)]
    apps = [a for a in apps if a is not None and (since is None or a.applied_at >= since)]
    rows = store.get([a.job_id for a in apps if a.job_id])
    return [facts_for(a, rows[a.job_id].job if a.job_id in rows else None, now, rules) for a in apps]


# --- summaries ---------------------------------------------------------------------------------

@dataclass
class Summary:
    total: int
    open: int
    counted: int
    rates: dict[str, Rate]
    reply_days: list[int] = field(default_factory=list)

    @property
    def median_days(self) -> float | None:
        return statistics.median(self.reply_days) if self.reply_days else None


def summarize(facts: list[Facts]) -> Summary:
    counted = [f for f in facts if f.counted]
    rates = {"replied": Rate(sum(f.replied for f in counted), len(counted))}
    for key, _, level in METRICS[1:]:
        rates[key] = Rate(sum(f.reached >= level for f in counted), len(counted))
    return Summary(total=len(facts), open=sum(f.open for f in facts), counted=len(counted), rates=rates,
                   reply_days=[f.days_to_reply for f in facts if f.days_to_reply is not None])


# --- cuts ------------------------------------------------------------------------------------------

def _days_band(f: Facts) -> str:
    d = f.app.days_to_apply
    if d is None:
        return "unknown"
    return "0–1 days" if d <= 1 else "2–3 days" if d <= 3 else "4–7 days" if d <= 7 else "8+ days"


FIT_BANDS = ("70+", "50–69", "under 50", "unscored")


def _band(score: float | None) -> str:
    if score is None:
        return "unscored"
    return "70+" if score >= 70 else "50–69" if score >= 50 else "under 50"


def _week(f: Facts) -> str:
    day = f.app.applied_at.astimezone().date()
    monday = day - timedelta(days=day.weekday())
    return f"week of {monday:%b} {monday.day}"


def _yes(flag: bool | None) -> str:
    return "unknown" if flag is None else "yes" if flag else "no"


@dataclass(frozen=True)
class Cut:
    key: str
    label: str
    group: Callable[[Facts], str]
    order: tuple[str, ...] = ()      # a natural order for the groups; else largest first


CUTS = [
    Cut("via", "Found via", lambda f: SOURCE_NAMES.get(f.app.found_via, f.app.found_via)),
    Cut("section", "Digest section",
        lambda f: SECTION_TITLES.get(f.app.digest_section, f.app.digest_section) if f.app.digest_section else "not from the digest"),
    Cut("referral", "Referral", lambda f: _yes(f.app.referral), ("yes", "no")),
    Cut("resume", "Resume version", lambda f: f.app.resume_version or "unrecorded"),
    Cut("cover", "Cover letter", lambda f: _yes(f.app.cover_letter), ("yes", "no")),
    Cut("autofilled", "Form filled automatically", lambda f: _yes(f.app.autofilled), ("yes", "no", "unknown")),
    Cut("fit_llm", "Fit score (LLM)", lambda f: _band(f.app.fit_score), FIT_BANDS),
    Cut("fit_keywords", "Fit score (keywords)", lambda f: _band(f.app.fit_keywords), FIT_BANDS),
    Cut("days", "Days from posting to applying", _days_band, ("0–1 days", "2–3 days", "4–7 days", "8+ days", "unknown")),
    Cut("seniority", "Seniority", lambda f: (f.job.seniority or "unstated") if f.job else "unknown",
        ("junior", "mid", "senior", "staff", "principal", "unstated", "unknown")),
    Cut("mode", "Work mode", lambda f: (f.job.work_mode or "unknown") if f.job else "unknown"),
    Cut("week", "Week applied", _week),
]
CUT_KEYS = [c.key for c in CUTS]


def split(facts: list[Facts], cut: Cut) -> list[tuple[str, Summary]]:
    groups: dict[str, list[Facts]] = {}
    for f in facts:
        groups.setdefault(cut.group(f), []).append(f)
    if cut.key == "week":
        keys = sorted(groups, key=lambda g: min(f.app.applied_at for f in groups[g]))
    elif cut.order:
        keys = [k for k in cut.order if k in groups] + sorted(k for k in groups if k not in cut.order)
    else:
        keys = sorted(groups, key=lambda g: (-len(groups[g]), g))
    return [(k, summarize(groups[k])) for k in keys]


def verdict(groups: list[tuple[str, Summary]], metric: str) -> str:
    """Compare the groups with the highest and lowest rate."""
    rated = [(g, s.rates[metric]) for g, s in groups if s.rates[metric].n]
    if not rated:
        return "too few to tell: none is old enough to count yet"
    if len(rated) < 2:
        return "too few to tell: only one group has applications old enough to count"
    hi_g, hi = max(rated, key=lambda x: (x[1].p, x[1].n))
    lo_g, lo = min(rated, key=lambda x: (x[1].p, -x[1].n))
    word = dict((k, label.lower()) for k, label, _ in METRICS)[metric]
    small = [(g, r.n) for g, r in ((hi_g, hi), (lo_g, lo)) if r.n < MIN_GROUP]
    if small:
        return f"too few to tell: {' and '.join(f'{g} has {n}' for g, n in small)} counted; {MIN_GROUP} each are needed"
    if hi.p == lo.p or lo.range[0] <= hi.p <= lo.range[1] or hi.range[0] <= lo.p <= hi.range[1]:
        return f"too few to tell: {hi_g} ({pct(hi.p)}%) and {lo_g} ({pct(lo.p)}%) are within each other's likely ranges"
    return f"{hi_g} does better on {word}: {pct(hi.p)}% vs {pct(lo.p)}% for {lo_g}"


# --- the report ------------------------------------------------------------------------------------

def _rate_cell(r: Rate) -> str:
    if not r.n:
        return "–"
    lo, hi = r.range
    return f"{r.k}/{r.n} {pct(r.p)}% ({pct(lo)}–{pct(hi)}%)"


def overview(s: Summary, rules: TrackingRules, first_counts: date | None) -> list[str]:
    lines = [f"Applications: {s.total} · {s.open} open · {s.counted} counted in rates "
             f"(at least {rules.no_response_days} days old, or closed)"]
    if not s.counted:
        when = f"; the first counts on {first_counts:%b} {first_counts.day}" if first_counts else ""
        lines.append(f"None is old enough to count yet{when}.")
    else:
        for key, label, _ in METRICS:
            lines.append(f"  {label + (' or further' if key != 'replied' else ''):<21} {s.rates[key].text()}")
    if s.median_days is not None:
        n = len(s.reply_days)
        lines.append(f"  {'Days to first reply':<21} median {s.median_days:g} (from {n} {'reply' if n == 1 else 'replies'})")
    return lines


def cut_lines(cut: Cut, groups: list[tuple[str, Summary]], metric: str) -> list[str]:
    width = max(len(g) for g, _ in groups)
    head = f"  {'':<{width}}  {'apps':>4}  {'counted':>7}  " + "  ".join(f"{label:<20}" for _, label, _ in METRICS[:3])
    lines = [cut.label, head.rstrip()]
    for g, s in groups:
        cells = "  ".join(f"{_rate_cell(s.rates[k]):<20}" for k, _, _ in METRICS[:3])
        lines.append(f"  {g:<{width}}  {s.total:>4}  {s.counted:>7}  {cells}".rstrip())
    lines.append(f"  → {verdict(groups, metric)}")
    return lines


def funnel_lines(facts: list[Facts], now: datetime) -> list[str]:
    """Every application so far, stage by stage, each with its share of the stage before."""
    recent = [f for f in facts if now - f.app.applied_at <= timedelta(days=RECENT_DAYS)]

    def column(group: list[Facts]) -> list[int]:
        return [len(group), sum(f.replied for f in group)] + [sum(f.reached >= lvl for f in group)
                                                               for _, _, lvl in METRICS[1:]]

    def cell(counts: list[int], i: int) -> str:
        share = f"{pct(counts[i] / counts[i - 1]):>3}%" if i and counts[i - 1] else "    "
        return f"{counts[i]:>4} {share}"

    labels = ["Applied", "Replied"] + [f"{label} or further" for _, label, _ in METRICS[1:]]
    everything, last4 = column(facts), column(recent)
    lines = [f"{'Funnel':<24}{'all time':<15}last 4 weeks"]
    for i, label in enumerate(labels):
        lines.append(f"  {label:<22}{cell(everything, i)}   {'':>3}{cell(last4, i)}".rstrip())
    lines.append("  (each % is of the row above; the last 4 weeks include applications too new to have heard)")
    return lines


def quiet_lines(facts: list[Facts], now: datetime) -> list[str]:
    from jobfeed.followups import FollowUp, follow_up_line
    open_ = []
    for f in facts:
        if not f.open:
            continue
        since = max(e.at for e in f.app.events if e.kind != "follow_up")
        open_.append(FollowUp(f.app, since, (now - since).days))
    if not open_:
        return []
    open_.sort(key=lambda x: x.quiet_since)
    return ["Longest without news"] + [f"  {follow_up_line(x)}" for x in open_[:QUIET_LISTED]]


SCORERS = (("llm", "LLM", lambda f: f.app.fit_score), ("keywords", "keywords", lambda f: f.app.fit_keywords))


def halves(facts: list[Facts], score) -> tuple[float, list[Facts], list[Facts]] | None:
    """Counted applications with this score, split at the median: (median, top half, bottom half)."""
    scored = sorted((f for f in facts if f.counted and score(f) is not None),
                    key=lambda f: (score(f), f.app.applied_at), reverse=True)
    if len(scored) < 2:
        return None
    cut = len(scored) // 2
    return statistics.median(score(f) for f in scored), scored[:cut], scored[cut:]


OUTCOME_WORDS = {"replied": "a reply", "screen": "a screen", "interview": "an interview", "offer": "an offer"}


def half_verdict(top: Rate, bottom: Rate) -> tuple[str, int]:
    """(verdict, direction): 1 when the top half clearly does better, -1 the bottom half, 0 can't tell."""
    if min(top.n, bottom.n) < MIN_GROUP:
        return f"too few to tell: {top.n} and {bottom.n} counted; {MIN_GROUP} in each half are needed", 0
    if top.p == bottom.p or bottom.range[0] <= top.p <= bottom.range[1] or top.range[0] <= bottom.p <= top.range[1]:
        return (f"too few to tell: {pct(top.p)}% and {pct(bottom.p)}% are within each other's likely ranges", 0)
    if top.p > bottom.p:
        return f"its top half does better: {pct(top.p)}% vs {pct(bottom.p)}%", 1
    return f"its bottom half does better ({pct(bottom.p)}% vs {pct(top.p)}%): this score points the wrong way", -1


def fit_lines(facts: list[Facts], metric: str) -> list[str]:
    """Does each score's top half get answered more than its bottom half? The spec's test of the scorers."""
    found = []
    for key, name, score in SCORERS:
        h = halves(facts, score)
        if h is not None:
            median, top, bottom = h
            found.append((name, median, summarize(top).rates[metric], summarize(bottom).rates[metric]))
    if not found:
        return []
    lines = [f"Which fit score predicts {OUTCOME_WORDS[metric]}? (counted applications, split at each score's median)"]
    width = max(len(f"{name} bottom half") for name, *_ in found)
    for name, median, top, bottom in found:
        lines.append(f"  {name + ' top half':<{width}}  {_rate_cell(top)}")
        lines.append(f"  {name + ' bottom half':<{width}}  {_rate_cell(bottom)}")
    clear = []
    for name, median, top, bottom in found:
        text, direction = half_verdict(top, bottom)
        lines.append(f"  → {name} (median {median:g}): {text}")
        if direction == 1:
            clear.append((name, pct(top.p) - pct(bottom.p)))
    if len(found) == 2 and len(clear) == 1:
        lines.append(f"  So far only the {clear[0][0]} score separates the applications that get {OUTCOME_WORDS[metric]}.")
    elif len(clear) == 2:
        (a, ga), (b, gb) = sorted(clear, key=lambda c: -c[1])
        lines.append(f"  Both scores separate them; the {a} score by {ga} points, the {b} score by {gb}.")
    return lines


def report(facts: list[Facts], rules: TrackingRules, *, cuts: list[str] | None = None, metric: str = "replied",
           now: datetime | None = None) -> list[str]:
    """The whole `jobfeed stats` output, which the weekly email carries too.
    cuts: keys to show; None = every cut with at least two groups."""
    if not facts:
        return ["No applications yet. Log one with `jobfeed apply CODE`, or reply `applied CODE` to the digest."]
    first_counts = None
    if now is not None:
        first = min(f.app.applied_at for f in facts)
        first_counts = (first + timedelta(days=rules.no_response_days)).astimezone().date()
    summary = summarize(facts)
    lines = overview(summary, rules, first_counts)
    if now is not None:
        lines += [""] + funnel_lines(facts, now)
    for cut in CUTS:
        if cuts is not None and cut.key not in cuts:
            continue
        if cuts is None and not summary.counted:
            continue      # every table would be empty; --by still shows one
        groups = split(facts, cut)
        if cuts is None and len(groups) < 2:
            continue
        lines += [""] + cut_lines(cut, groups, metric)
    if cuts is None and summary.counted:
        shown = sum(1 for c in CUTS if len(split(facts, c)) >= 2)
        if not shown:
            lines += ["", "No splits yet: every application so far has the same resume, source, and so on. "
                          "`--by resume` (or another cut) shows one anyway."]
    if cuts is None and summary.counted:
        compare = fit_lines(facts, metric)
        if compare:
            lines += [""] + compare
    if now is not None and cuts is None:
        quiet = quiet_lines(facts, now)
        if quiet:
            lines += [""] + quiet
    return lines
