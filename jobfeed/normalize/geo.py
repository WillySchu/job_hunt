"""Place-name tables used by the location normalizer."""

from __future__ import annotations

import re

US_STATES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
STATE_BY_NAME: dict[str, str] = {name.lower(): code for code, name in US_STATES.items()}
STATE_BY_NAME.update({"washington dc": "DC", "washington d.c.": "DC", "d.c.": "DC"})

# Longest names first so "West Virginia" wins over "Virginia".
_STATE_NAME_RE = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in sorted(STATE_BY_NAME, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
_STATE_CODE_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(US_STATES) + r")(?![A-Za-z])")

# No bare "america": it would match inside "Latin America" or "South America".
US_TERMS = [
    "united states of america", "united states", "u.s.a.", "u.s.a", "u.s.", "u.s", "usa", "us",
    "east coast", "west coast", "midwest", "pacific northwest", "pnw", "continental us", "contiguous us",
]
AMERICAS_TERMS = ["americas", "north america", "north american"]
GLOBAL_TERMS = ["worldwide", "anywhere", "global", "globally", "international", "any location", "world"]
OTHER_REGION_TERMS = [
    "canada", "mexico", "brazil", "argentina", "colombia", "chile", "peru", "latam", "latin america",
    "south america", "central america", "uk", "u.k.", "united kingdom", "england", "scotland",
    "ireland", "germany", "france", "spain", "portugal", "italy", "netherlands", "belgium",
    "switzerland", "austria", "poland", "romania", "ukraine", "sweden", "norway", "denmark",
    "finland", "estonia", "lithuania", "czech republic", "czechia", "hungary", "greece", "europe",
    "european union", "eu", "emea", "apac", "asia", "asia pacific", "india", "japan", "korea",
    "south korea", "china", "singapore", "hong kong", "taiwan", "philippines", "vietnam",
    "indonesia", "malaysia", "thailand", "australia", "new zealand", "israel", "turkey", "uae",
    "united arab emirates", "saudi arabia", "egypt", "nigeria", "kenya", "south africa", "africa",
    "middle east", "cet", "gmt", "ist",
]

# Canadian provinces, so "Toronto, ON" isn't read as a US state.
CA_PROVINCES = {"ON", "BC", "QC", "AB", "MB", "SK", "NS", "NB", "NL", "PE"}

# Alias -> (city, state). Matched after filler words like "area" are trimmed,
# so "Greater Seattle Area" arrives here as "greater seattle".
CITY_ALIASES: dict[str, tuple[str, str]] = {
    "greater seattle": ("Seattle", "WA"), "seattle metropolitan": ("Seattle", "WA"),
    "puget sound": ("Seattle", "WA"), "seattle-tacoma-bellevue": ("Seattle", "WA"),
    "sf": ("San Francisco", "CA"), "sf bay": ("San Francisco", "CA"), "bay": ("San Francisco", "CA"),
    "san francisco bay": ("San Francisco", "CA"), "nyc": ("New York", "NY"),
    "new york city": ("New York", "NY"), "washington dc": ("Washington", "DC"),
    "washington d.c": ("Washington", "DC"),
}

# Words that name no city: generic location labels and scheduling filler.
NOT_A_CITY = {
    "multiple locations", "various locations", "various", "multiple", "flexible", "hq", "headquarters",
    "office", "home", "home based", "home-based", "field", "n/a", "tbd", "nationwide", "in-person",
    "in person", "location", "locations", "remote", "hybrid", "onsite", "on-site", "in-office",
    "in office", "distributed", "virtual", "us-perm", "perm", "day", "days", "week", "weekly",
    "month", "required", "preferred", "optional", "friendly", "first", "eligible", "full-time",
    "full time", "part-time", "contract", "only", "time zones", "timezone", "time zone", "zones",
}


def _term_re(terms: list[str]) -> re.Pattern[str]:
    alts = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(r"(?<![a-z])(" + alts + r")(?![a-z])", re.IGNORECASE)


US_RE = _term_re(US_TERMS)
AMERICAS_RE = _term_re(AMERICAS_TERMS)
GLOBAL_RE = _term_re(GLOBAL_TERMS)
OTHER_RE = _term_re(OTHER_REGION_TERMS)
REGION_WORDS = {t.lower() for t in US_TERMS + AMERICAS_TERMS + GLOBAL_TERMS + OTHER_REGION_TERMS}


def state_code(piece: str) -> str | None:
    """'WA', 'wa', 'Washington' -> 'WA'; anything else -> None."""
    p = piece.strip().rstrip(".")
    if p.upper() in US_STATES and (p.isupper() or len(p) == 2):
        return p.upper()
    return STATE_BY_NAME.get(p.lower())


def find_states(text: str, *, codes: bool = True) -> list[str]:
    """Every US state named or abbreviated in text, in order of first mention."""
    found: list[tuple[int, str]] = []
    for m in _STATE_NAME_RE.finditer(text):
        found.append((m.start(), STATE_BY_NAME[m.group(1).lower()]))
    if codes:
        for m in _STATE_CODE_RE.finditer(text):
            found.append((m.start(), m.group(1)))
    seen: dict[str, None] = {}
    for _, code in sorted(found):
        seen.setdefault(code, None)
    return list(seen)
