"""Location parsing: work mode, remote region, cities and state eligibility."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobfeed.normalize import geo

REMOTE_RE = re.compile(
    r"\b(remote|anywhere|worldwide|work from home|wfh|distributed|telecommut\w*|virtual|home[- ]based)\b",
    re.IGNORECASE,
)
HYBRID_RE = re.compile(r"\bhybrid\b", re.IGNORECASE)
ONSITE_RE = re.compile(r"\b(on[- ]?site|in[- ]office|in[- ]person|office[- ]based)\b", re.IGNORECASE)

# Segment separators: ; | • · newlines, slashes, and lowercase " or " / " and "
# (case-sensitive so "Portland, OR" keeps its state).
_SEGMENT_SPLIT = re.compile(r"\s*(?:;|\||•|·|\n|/|&|\s\+\s)\s*|\s+(?:or|and|Or|And)\s+")
# Within a segment, spaced dashes and colons separate parts ("US - Seattle"); "Winston-Salem" stays whole.
_PART_SPLIT = re.compile(r"\s+[-–—]+\s*|\s*[–—]+\s*|\s*-\s+|:")
_TRAILING_WORDS = re.compile(
    r"[\s-]+(hq|office|offices|headquarters|campus|metro|only|based|preferred|required|eligible|area)$",
    re.IGNORECASE,
)
_TIME_ZONE = re.compile(
    r"\b(pacific|eastern|central|mountain)(?: standard| daylight)? time\b|\btime ?zones?\b"
    # No CT or MT: those are Connecticut and Montana.
    r"|\b(PST|PDT|PT|EST|EDT|ET|CST|CDT|MST|MDT|UTC|GMT|CET|CEST|IST)\b",
    re.IGNORECASE,
)
_US_TIME_ZONE = re.compile(
    r"(?i:\b(pacific|eastern|central|mountain)(?: standard| daylight)? time\b)"
    r"|\b(PST|PDT|EST|EDT|CST|CDT|MST|MDT)\b"  # abbreviations: uppercase only
)
_LEADING_WORDS = re.compile(
    r"^(within|in|from|across|throughout|based in|located in|anywhere in|the|any|all of|all)\s+",
    re.IGNORECASE,
)
_PUNCT = " -–—:,.()[]{}*\"'"


def _trim(piece: str) -> str:
    """Drop filler around a place name: 'within the US' -> 'US', 'US only' -> 'US'."""
    prev = None
    while piece != prev:
        prev = piece
        piece = piece.strip(_PUNCT)
        piece = _LEADING_WORDS.sub("", piece)
        piece = _TRAILING_WORDS.sub("", piece)
    return piece

REGION_PRIORITY = ["us", "americas", "global", "other"]


@dataclass
class LocationInfo:
    work_mode: str = "unknown"
    remote_region: str | None = None
    cities: list[str] = field(default_factory=list)
    remote_states: list[str] = field(default_factory=list)
    excluded_states: list[str] = field(default_factory=list)


@dataclass
class _Segment:
    text: str
    remote: bool
    hybrid: bool
    onsite: bool
    cities: list[str]
    states: list[str]          # states named without a city ("Remote - Washington")
    regions: set[str]


def _title(piece: str) -> str:
    return piece.title() if piece.islower() or (piece.isupper() and len(piece) > 3) else piece


def _pieces(segment: str) -> list[str]:
    cleaned = REMOTE_RE.sub(" ", segment)
    cleaned = HYBRID_RE.sub(" ", cleaned)
    cleaned = ONSITE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[()\[\]{}]", ",", cleaned)
    out: list[str] = []
    for part in _PART_SPLIT.split(cleaned):
        for piece in part.split(","):
            piece = _trim(piece)
            if piece:
                out.append(piece)
    return out


def _read_places(segment: str) -> tuple[list[str], list[str], set[str]]:
    """Cities ("Seattle, WA"), bare states and region signals from one segment."""
    pieces = _pieces(segment)
    cities: list[str] = []
    bare_states: list[str] = []
    regions: set[str] = set()
    pending: list[str] | None = None  # [city, qualifier]

    def flush() -> None:
        nonlocal pending
        if pending:
            city, qual = pending
            cities.append(f"{city}, {qual}" if qual else city)
        pending = None

    for i, piece in enumerate(pieces):
        low = piece.lower()
        nxt = pieces[i + 1] if i + 1 < len(pieces) else ""
        if low in geo.CITY_ALIASES:
            flush()
            pending = list(geo.CITY_ALIASES[low])
            continue
        if low in geo.NOT_A_CITY or any(ch.isdigit() for ch in piece) or _TIME_ZONE.search(piece):
            continue
        code = geo.state_code(piece)
        # "Washington, DC" / "New York, NY": a state name followed by a state code is a city.
        if code and len(piece) > 2 and geo.state_code(nxt) and nxt.strip().isupper():
            code = None
        if code:
            regions.add("us")
            if pending and not pending[1]:
                pending[1] = code
                flush()
            elif low == "new york":
                flush()
                cities.append("New York, NY")
            else:
                flush()
                bare_states.append(code)
            continue
        if piece.upper() in geo.CA_PROVINCES and piece.isupper():
            regions.add("other")
            if pending and not pending[1]:
                pending[1] = "Canada"
                flush()
            continue
        if low in geo.REGION_WORDS:
            region = _region_of_term(low)
            regions.add(region)
            if pending and not pending[1] and region == "other":
                pending[1] = _title(piece) if len(piece) > 3 else piece.upper()
                flush()
            elif pending and region == "us":
                flush()
            continue
        if not re.search(r"[A-Za-z]{2}", piece):
            continue
        flush()
        pending = [_title(piece), ""]
    flush()
    for c in cities:
        qual = c.rpartition(", ")[2] if ", " in c else ""
        if qual in geo.US_STATES:
            regions.add("us")
    return cities, bare_states, regions


def _region_of_term(low: str) -> str:
    if geo.US_RE.fullmatch(low):
        return "us"
    if geo.AMERICAS_RE.fullmatch(low):
        return "americas"
    if geo.GLOBAL_RE.fullmatch(low):
        return "global"
    return "other"


def _regions_in(text: str) -> set[str]:
    regions: set[str] = set()
    if geo.US_RE.search(text) or geo.find_states(text, codes=True) or _US_TIME_ZONE.search(text):
        regions.add("us")
    if geo.AMERICAS_RE.search(text):
        regions.add("americas")
    if geo.GLOBAL_RE.search(text):
        regions.add("global")
    if geo.OTHER_RE.search(text):
        regions.add("other")
    return regions


def _best_region(regions: set[str]) -> str | None:
    return next((r for r in REGION_PRIORITY if r in regions), None)


def _segments(raw: str) -> list[_Segment]:
    segs = []
    for text in _SEGMENT_SPLIT.split(raw):
        text = text.strip(_PUNCT)
        if not text:
            continue
        cities, states, regions = _read_places(text)
        regions |= _regions_in(text)
        segs.append(_Segment(
            text=text,
            remote=bool(REMOTE_RE.search(text)),
            hybrid=bool(HYBRID_RE.search(text)),
            onsite=bool(ONSITE_RE.search(text)),
            cities=cities,
            states=states,
            regions=regions,
        ))
    return segs


# --- description hints ---------------------------------------------------------

_DESC_REMOTE = re.compile(
    r"\b(this (?:is an?|role is|position is|job is) (?:a )?(?:fully |100% )?remote\b"
    r"|fully remote (?:role|position|job)|remote[- ]first|100% remote)",
    re.IGNORECASE,
)
_DESC_NOT_REMOTE = re.compile(r"\bnot (?:a |an )?(?:fully |100% )?remote\b|\bremote work is not\b", re.IGNORECASE)
_DESC_HYBRID = re.compile(r"\bhybrid\b", re.IGNORECASE)
_DESC_NOT_HYBRID = re.compile(r"\bnot (?:a |an )?hybrid\b", re.IGNORECASE)

_ELIGIBLE = re.compile(
    r"\b(reside|resides|residing|resident|residents|living|live|lives|hire|hiring|hires|"
    r"employ|employment|eligible|eligibility|work from|working from|open to candidates)\b",
    re.IGNORECASE,
)
# "located"/"based" count only about people, not offices ("our hubs are located in...").
_LOCATED_PERSON = re.compile(
    r"\b(candidates?|applicants?|you|must|required|employees|team members|individuals)\b.{0,60}"
    r"\b(located|based)\b",
    re.IGNORECASE,
)
_STATES_WORD = re.compile(r"\b(states?|locations?|following)\b", re.IGNORECASE)
_PAY_WORDS = re.compile(r"\$|\b(salary|salaries|pay|compensation|wages?|base|range|benefits?|sick|leave|tax|taxes)\b",
                        re.IGNORECASE)
_NEGATION = re.compile(
    r"\b(unable|cannot|can't|can not|do not|don't|does not|doesn't|are not|is not|aren't|isn't|"
    r"no longer|not (?:able|currently|yet|hiring|eligible|available|open|considering|accepting))\b",
    re.IGNORECASE,
)
_EXCEPT = re.compile(r"\b(except|excluding|with the exception of|other than|outside of)\b", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def state_eligibility(description: str) -> tuple[list[str], list[str]]:
    """(allowed, excluded) US states from hiring-eligibility sentences.

    Only sentences about where people can live or be hired count, and pay-range
    sentences are skipped: "The range for CA and NY candidates is..." names
    states without restricting who can apply.
    """
    if not description:
        return [], []
    allowed: list[str] = []
    excluded: list[str] = []
    lines = [s.strip() for s in _SENTENCE_SPLIT.split(description) if s.strip()]
    i = 0
    while i < len(lines):
        sentence = lines[i]
        i += 1
        about_people = _ELIGIBLE.search(sentence) or _LOCATED_PERSON.search(sentence)
        if not (about_people and _STATES_WORD.search(sentence)) or _PAY_WORDS.search(sentence):
            continue
        # "We can hire in the following states:" then one state per line.
        if sentence.rstrip().endswith(":"):
            while i < len(lines) and len(lines[i]) <= 60 and geo.find_states(lines[i]):
                sentence += " " + lines[i]
                i += 1
        exc = _EXCEPT.search(sentence)
        if exc:
            allowed += geo.find_states(sentence[: exc.start()])
            excluded += geo.find_states(sentence[exc.end():])
        elif _NEGATION.search(sentence):
            excluded += geo.find_states(sentence)
        else:
            allowed += geo.find_states(sentence)
    return _dedupe(allowed), _dedupe(excluded)


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


# --- entry point ---------------------------------------------------------------

def parse_location(raw: str, description: str = "", *, assume_remote: bool = False) -> LocationInfo:
    """Read a posting's location string, using the description only as a tiebreaker.

    assume_remote: the source only lists remote jobs (the aggregators), so a
    bare "USA" or "Canada" is the remote region, not an office.
    """
    raw = (raw or "").strip()
    # "Remote - US (excluding CA, NY)": the excluded states aren't places to read.
    excluded_here: list[str] = []
    if exc := _EXCEPT.search(raw):
        tail = raw[exc.end():]
        close = tail.find(")")
        clause = tail[:close] if close != -1 else tail
        excluded_here = geo.find_states(clause)
        raw = raw[: exc.start()] + tail[len(clause):]
    segs = _segments(raw)
    cities = _dedupe([c for s in segs for c in s.cities])

    any_remote = assume_remote or any(s.remote for s in segs)
    if any_remote:
        mode = "remote"
    elif any(s.hybrid for s in segs):
        mode = "hybrid"
    elif any(s.onsite for s in segs):
        mode = "onsite"
    elif cities:
        mode = "onsite"
        if _DESC_REMOTE.search(description) and not _DESC_NOT_REMOTE.search(description):
            mode = "remote"
        elif _DESC_HYBRID.search(description) and not _DESC_NOT_HYBRID.search(description):
            mode = "hybrid"
    else:
        mode = "unknown"

    region = None
    remote_states: list[str] = []
    if mode == "remote":
        remote_segs = [s for s in segs if s.remote] or segs
        # "Remote / USA" and "Remote (Brazil or United States)": the region can
        # sit in a segment of its own, so those count too.
        region_only = [s for s in segs if not s.cities and not s.remote]
        signals: set[str] = set()
        for s in remote_segs + region_only:
            signals |= s.regions
        region = _best_region(signals)
        if region is None and cities:
            # "Atlanta, GA; Remote": a bare remote option next to US offices is US.
            quals = [c.rpartition(", ")[2] if ", " in c else None for c in cities]
            if all(q in geo.US_STATES for q in quals):
                region = "us"
            elif all(q and q not in geo.US_STATES for q in quals):
                region = "other"
        for s in remote_segs:
            remote_states += s.states
            remote_states += [c.rpartition(", ")[2] for c in s.cities if c.rpartition(", ")[2] in geo.US_STATES]

    allowed, excluded = state_eligibility(description)
    excluded = _dedupe(excluded_here + excluded)
    if mode == "remote" and allowed and not remote_states:
        remote_states = allowed
    if mode == "remote" and (remote_states or excluded) and region is None:
        region = "us"

    return LocationInfo(
        work_mode=mode,
        remote_region=region,
        cities=cities,
        remote_states=_dedupe(remote_states),
        excluded_states=excluded,
    )
