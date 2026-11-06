"""Salary parsing: pay ranges from structured fields or description text."""

from __future__ import annotations

import re
from dataclasses import dataclass

HOURS_PER_YEAR = 2080
MIN_ANNUAL = 25_000
MAX_ANNUAL = 2_000_000

_NUM = r"\d{1,3}(?:[,.]\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d+)?"
_CUR = (r"US\$|USD\s*\$?|CA\$|C\$|CAD\s*\$?|AU\$|A\$|AUD\s*\$?|NZ\$|S\$|HK\$|MX\$|\$|£|GBP\s*£?|€|EUR\s*€?")
_CUR_AFTER = r"USD|CAD|AUD|NZD|GBP|EUR|SGD"


def _amount(p: str) -> str:
    # [ \t] not \s: an amount never spans lines, so a match can't swallow the next line's label.
    return (rf"(?P<{p}cur>{_CUR})?[ \t]*(?P<{p}num>{_NUM})[ \t]*(?P<{p}suf>[kKmM](?![a-zA-Z]))?"
            rf"(?:[ \t]*(?P<{p}cur2>{_CUR_AFTER})\b)?")


_RANGE_RE = re.compile(
    rf"(?:between[ \t]+)?{_amount('a')}[ \t]*(?:-|–|—|‒|to|and|through)[ \t]*{_amount('b')}",
    re.IGNORECASE,
)
_SINGLE_RE = re.compile(_amount("a"), re.IGNORECASE)

_PERIOD_AFTER = re.compile(
    r"^\s*(?:" + _CUR_AFTER + r")?\s*(?:/|per|an|a|each)?\s*"
    r"(?P<period>hour|hr|hrly|month|mo|year|yr|annum|annually|annual|yearly)\b",
    re.IGNORECASE,
)
_HOURLY_BEFORE = re.compile(r"\bhourly\b|\bper hour\b", re.IGNORECASE)
_SALARY_CONTEXT = re.compile(
    r"\b(salary|salaries|base|pay|compensation|ote|annual|annually|per year|range|wage|earn|starting|up to)\b",
    re.IGNORECASE,
)
_NOT_SALARY_BEFORE = re.compile(
    r"\b(stipend|bonus|signing|sign-on|relocation|budget|allowance|reimburse\w*|funding|funded|raised|raise|"
    r"revenue|valuation|series|arr|credit|match|grant|donat\w*|spend|savings|worth|assets|insurance|401)\b",
    re.IGNORECASE,
)
_NOT_SALARY_AFTER = re.compile(
    r"^\s*(?:\+\s*)?(million|billion|mm|bn|b\b|employees|customers|users|people|members|companies|years?|"
    r"in funding|in revenue|arr\b)"
    # "$10,000 signing bonus" is not pay, but "$180k - $220k + bonus" is.
    r"|^\s*(stipend|bonus|signing|sign-on|relocation)",
    re.IGNORECASE,
)
_MIN_ONLY = re.compile(r"(\bstarting (?:at|from)|\bfrom|\bat least|\bminimum of)\s*$", re.IGNORECASE)
_MIN_ONLY_AFTER = re.compile(r"^\s*(\+|or more|and up|\band above)", re.IGNORECASE)
_MAX_ONLY = re.compile(r"(\bup to|\bmaximum of|\bas much as)\s*$", re.IGNORECASE)
_SEATTLE = re.compile(r"seattle|washington|\bWA\b|bellevue|redmond|kirkland|puget", re.IGNORECASE)

_CURRENCY_OF = {
    "$": "USD", "US$": "USD", "USD": "USD", "CA$": "CAD", "C$": "CAD", "CAD": "CAD", "AU$": "AUD", "A$": "AUD",
    "AUD": "AUD", "NZ$": "NZD", "NZD": "NZD", "S$": "SGD", "SGD": "SGD", "HK$": "HKD", "MX$": "MXN", "£": "GBP",
    "GBP": "GBP", "€": "EUR", "EUR": "EUR",
}


@dataclass
class SalaryInfo:
    min: int | None = None       # annual, in `currency`
    max: int | None = None
    currency: str | None = None  # "USD", "CAD", ... ; None when nothing was found
    raw: str | None = None       # the matched text, for display

    @property
    def found(self) -> bool:
        return self.raw is not None


@dataclass
class _Candidate:
    low: float | None
    high: float | None
    currency: str
    raw: str
    seattle: bool


def _number(s: str) -> float:
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):
        return float(s.replace(".", ""))   # "170.000" — dots as thousands
    return float(s.replace(",", ""))


def _scale(num: float, suffix: str | None) -> float:
    if suffix and suffix.lower() == "k":
        return num * 1_000
    if suffix and suffix.lower() == "m":
        return num * 1_000_000
    return num


def _currency(*marks: str | None) -> str | None:
    """The first recognizable currency mark; callers pass explicit codes before bare '$'."""
    for mark in marks:
        if not mark:
            continue
        key = re.sub(r"[^A-Za-z$£€]", "", mark).upper()
        if key not in _CURRENCY_OF:
            key = key.rstrip("$£€")  # "USD$" -> "USD"
        if key in _CURRENCY_OF:
            return _CURRENCY_OF[key]
    return None


def _line_of(text: str, start: int, end: int) -> str:
    left = text.rfind("\n", 0, start) + 1
    right = text.find("\n", end)
    return text[left: right if right != -1 else len(text)]


def _annualize(values: list[float], period: str | None, before: str) -> list[float] | None:
    p = (period or "").lower()
    if p in ("hour", "hr", "hrly") or (not p and _HOURLY_BEFORE.search(before)):
        return [v * HOURS_PER_YEAR for v in values]
    if p in ("month", "mo"):
        return [v * 12 for v in values]
    if not p and max(values) < 20_000:
        return None  # bare small numbers: hourly or monthly can't be told apart from noise
    return values


def _candidates(text: str) -> list[_Candidate]:
    found: list[_Candidate] = []
    taken: list[tuple[int, int]] = []

    def accept(m: re.Match[str], low: float | None, high: float | None, cur: str | None) -> None:
        after = text[m.end(): m.end() + 40]
        period_m = _PERIOD_AFTER.match(after)
        end = m.end() + (period_m.end() if period_m else 0)
        before = text[max(0, m.start() - 80): m.start()]
        values = [v for v in (low, high) if v is not None]
        scaled = _annualize(values, period_m.group("period") if period_m else None, before)
        if scaled is None or not all(MIN_ANNUAL <= v <= MAX_ANNUAL for v in scaled):
            return
        it = iter(scaled)
        low2 = next(it) if low is not None else None
        high2 = next(it) if high is not None else None
        found.append(_Candidate(
            low=low2, high=high2, currency=cur or "USD",
            raw=text[m.start(): end].strip(),
            seattle=bool(_SEATTLE.search(_line_of(text, m.start(), m.end()))),
        ))
        taken.append((m.start(), end))

    def explicit_single(m: re.Match[str]) -> bool:
        """'$60/hr', '$200K+', 'up to $180,000' say they're pay without a 'salary' word nearby."""
        after = text[m.end(): m.end() + 40]
        before = text[max(0, m.start() - 30): m.start()]
        return bool(_PERIOD_AFTER.match(after) or _MIN_ONLY_AFTER.match(after)
                    or _MAX_ONLY.search(before) or _MIN_ONLY.search(before))

    def plausible(m: re.Match[str], explicit: bool) -> bool:
        # The veto words only count on the amount's own line: a "Bonus:" line
        # above must not knock out the "Base:" line below it.
        line_start = text.rfind("\n", 0, m.start()) + 1
        before = text[max(line_start, m.start() - 40): m.start()]
        after = text[m.end(): m.end() + 25]
        if _NOT_SALARY_BEFORE.search(before) or _NOT_SALARY_AFTER.match(after):
            return False
        return explicit or bool(_SALARY_CONTEXT.search(text[max(0, m.start() - 80): m.start()]))

    for m in _RANGE_RE.finditer(text):
        # Claim the span even if it's rejected, so "$10,000 - $20,000 bonus" can't
        # come back as two single amounts.
        taken.append((m.start(), m.end()))
        g = m.groupdict()
        trailing = re.match(rf"\s*({_CUR_AFTER})\b", text[m.end(): m.end() + 8])
        # Codes beat symbols: "$150,000 - $190,000 CAD" is Canadian.
        cur = _currency(g["bcur2"], g["acur2"], trailing.group(1) if trailing else None, g["acur"], g["bcur"])
        has_k = bool(g["asuf"] or g["bsuf"])
        if not plausible(m, explicit=bool(cur) or has_k):
            continue
        a, b = _number(g["anum"]), _number(g["bnum"])
        # "$150-190k": a suffix on one side applies to both.
        a = _scale(a, g["asuf"] or (g["bsuf"] if a < 1000 else None))
        b = _scale(b, g["bsuf"] or (g["asuf"] if b < 1000 else None))
        if a > b:
            a, b = b, a
        accept(m, a, b, cur)

    for m in _SINGLE_RE.finditer(text):
        if any(s <= m.start() < e for s, e in taken):
            continue
        g = m.groupdict()
        cur = _currency(g["acur2"], g["acur"])
        if not cur and not g["asuf"]:
            continue
        if not plausible(m, explicit=explicit_single(m)):
            continue
        value = _scale(_number(g["anum"]), g["asuf"])
        before = text[max(0, m.start() - 30): m.start()]
        after = text[m.end(): m.end() + 15]
        if _MAX_ONLY.search(before):
            accept(m, None, value, cur)
        elif _MIN_ONLY.search(before) or _MIN_ONLY_AFTER.match(after):
            accept(m, value, None, cur)
        else:
            accept(m, value, value, cur)
    return found


def parse_salary(*texts: str | None) -> SalaryInfo:
    """Parse the first text that contains a pay figure; pass structured fields before the description.

    Several ranges (pay zones, levels) are merged into one span. A range on a
    line that mentions Seattle or Washington wins over the others. Ranges in
    USD win over other currencies; a posting with only non-USD pay keeps its
    currency and raw text but no USD figures.
    """
    for text in texts:
        if not text:
            continue
        cands = _candidates(text)
        if not cands:
            continue
        local = [c for c in cands if c.seattle]
        cands = local or cands
        usd = [c for c in cands if c.currency == "USD"]
        if not usd:
            c = cands[0]
            return SalaryInfo(min=None, max=None, currency=c.currency, raw=c.raw)
        lows = [c.low for c in usd if c.low is not None]
        highs = [c.high for c in usd if c.high is not None]
        return SalaryInfo(
            min=round(min(lows)) if lows else None,
            max=round(max(highs)) if highs else None,
            currency="USD",
            raw="; ".join(dict.fromkeys(c.raw for c in usd[:3])),
        )
    return SalaryInfo()


def format_salary(low: int | None, high: int | None) -> str:
    """$170K–$215K, $180K+, up to $200K."""
    def k(v: int) -> str:
        return f"${v / 1000:.0f}K" if v % 1000 < 500 or v >= 100_000 else f"${v / 1000:.1f}K"
    if low and high and low != high:
        return f"{k(low)}–{k(high)}"
    if low and high:
        return k(low)
    if low:
        return f"{k(low)}+"
    if high:
        return f"up to {k(high)}"
    return ""
