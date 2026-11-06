"""Seniority from a job title. Used for sorting only, never for excluding."""

from __future__ import annotations

import re

RANKS = ["junior", "mid", "senior", "staff", "principal"]

# "Member of Technical Staff" is a title, not the Staff level.
_MTS = re.compile(r"\bmember of (the )?technical staff\b", re.IGNORECASE)
_WORDS = [
    ("principal", re.compile(r"\b(principal|distinguished|fellow)\b", re.IGNORECASE)),
    ("staff", re.compile(r"\bstaff\b\+?", re.IGNORECASE)),
    ("senior", re.compile(r"\b(senior|sr\.?|lead)(?=[\s,/-]|$)", re.IGNORECASE)),
    ("mid", re.compile(r"\b(mid[- ]?level|mid|intermediate)\b", re.IGNORECASE)),
    ("junior", re.compile(
        r"\b(junior|jr\.?|entry[- ]level|early[- ]career|new[- ]grad(uate)?|graduate|associate|apprentice|"
        r"intern|internship)(?=[\s,/()-]|$)",
        re.IGNORECASE,
    )),
]
_ROMAN = {"i": "junior", "ii": "mid", "iii": "senior", "iv": "staff", "v": "principal"}
_ROLE = r"(?:engineer|developer|swe|sde|programmer|architect|scientist)"
_ROMAN_RE = re.compile(rf"\b{_ROLE}\s+(i|ii|iii|iv|v)\b", re.IGNORECASE)
_DIGIT_RE = re.compile(rf"\b{_ROLE}\s+([1-5])\b", re.IGNORECASE)
_LEVEL_RE = re.compile(r"\b[lL]([3-7])\b|\b(?:ic|IC)([2-6])\b")
_LEVEL_MAP = {"3": "junior", "4": "mid", "5": "senior", "6": "staff", "7": "principal"}
_IC_MAP = {"2": "junior", "3": "mid", "4": "senior", "5": "staff", "6": "principal"}
_DIGIT_MAP = {"1": "junior", "2": "mid", "3": "senior", "4": "staff", "5": "principal"}


def parse_seniority(title: str) -> str | None:
    """junior | mid | senior | staff | principal, or None when the title doesn't say."""
    if not title:
        return None
    t = _MTS.sub(" ", title)
    hits = [rank for rank, pattern in _WORDS if pattern.search(t)]
    if hits:
        # "Senior Staff" -> staff: the highest rank named wins.
        return max(hits, key=RANKS.index)
    if m := _ROMAN_RE.search(t):
        return _ROMAN[m.group(1).lower()]
    if m := _DIGIT_RE.search(t):
        return _DIGIT_MAP[m.group(1)]
    if m := _LEVEL_RE.search(t):
        return _LEVEL_MAP[m.group(1)] if m.group(1) else _IC_MAP[m.group(2)]
    return None
