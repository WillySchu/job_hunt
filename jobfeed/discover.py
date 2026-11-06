"""`jobfeed discover`: find which ATS each company uses, and write it into companies.yaml."""

from __future__ import annotations

import json
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

from jobfeed.http import FetchError, NotFound
from jobfeed.sources import ATS_SOURCES, SOURCES

MAX_CONCURRENCY = 4

# Words dropped to make a company's short slug: "Grafana Labs" -> "grafana".
_SUFFIXES = {
    "inc", "llc", "ltd", "corp", "corporation", "co", "company", "technologies", "technology", "labs", "lab",
    "ai", "hq", "group", "io", "app", "software", "systems", "the", "and",
}


@dataclass
class Discovery:
    name: str
    ats: str | None = None            # None + no error = on none of the supported boards
    slug: str | None = None
    postings: int = 0
    board_name: str | None = None
    tried: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None          # a network problem: nothing is written for this company

    @property
    def found(self) -> bool:
        return self.ats is not None


def slug_candidates(name: str) -> list[tuple[str, bool]]:
    """Slugs to try, best first, each with whether it spells the whole name.

    'Grafana Labs' -> grafanalabs, grafana-labs, grafana (short)
    'Union.ai'     -> unionai, union-ai, union (short)
    """
    words = re.findall(r"[a-z0-9]+", name.casefold().replace("&", " and "))
    if not words:
        return []
    full = [w for w in words if w != "and"] or words
    core = [w for w in full if w not in _SUFFIXES] or full
    out: list[tuple[str, bool]] = []
    for slug, whole in [("".join(full), True), ("-".join(full), True), ("".join(core), False),
                        ("-".join(core), False), (core[0], False)]:
        if len(slug) >= 2 and slug not in [s for s, _ in out]:
            out.append((slug, whole))
    return out


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.casefold()) if w not in _SUFFIXES}


def discover_company(client: httpx.Client, name: str) -> Discovery:
    """Probe each slug guess on Greenhouse, Lever, then Ashby; the first board with postings wins.

    A board with no open postings counts only if nothing better turns up,
    since an empty board can be an abandoned one or a different company's.
    """
    result = Discovery(name=name)
    fallback: tuple[str, str, bool, str | None] | None = None
    errors: list[str] = []
    for slug, whole in slug_candidates(name):
        for ats in ATS_SOURCES:
            result.tried.append(f"{ats}:{slug}")
            try:
                count, board_name = SOURCES[ats].probe(client, slug)
            except NotFound:
                continue
            except FetchError as exc:
                errors.append(f"{ats}:{slug}: {exc}")
                continue
            if count == 0:
                fallback = fallback or (ats, slug, whole, board_name)
                continue
            return _record(result, ats, slug, whole, board_name, count)
    if fallback:
        ats, slug, whole, board_name = fallback
        _record(result, ats, slug, whole, board_name, 0)
        result.warnings.append("board has no open postings; it may be old or someone else's")
        return result
    if errors:
        result.error = errors[0] + (f" (+{len(errors) - 1} more)" if len(errors) > 1 else "")
    return result


def _record(result: Discovery, ats: str, slug: str, whole: bool, board_name: str | None, count: int) -> Discovery:
    result.ats, result.slug, result.postings, result.board_name = ats, slug, count, board_name
    if not whole:
        result.warnings.append(f"matched the short name '{slug}'; check it's {result.name}")
    if board_name:
        # Word sets, not substrings: "Snap" must not match "Snapdragon Bakery".
        board, ours = _words(board_name), _words(result.name)
        if board and ours and not (board <= ours or ours <= board):
            result.warnings.append(f"the board is named '{board_name}'")
    return result


def discover_all(client: httpx.Client, names: list[str]) -> list[Discovery]:
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as pool:
        return list(pool.map(lambda n: discover_company(client, n), names))


# --- writing companies.yaml -------------------------------------------------------
#
# The file is edited line by line rather than re-dumped, so its comments,
# grouping and one-line-per-company layout survive.

_ADDED_HEADER = "  # --- added by jobfeed discover ---"
HARVEST_HEADER = "  # --- harvested from aggregator postings ---"


class EditError(Exception):
    """companies.yaml couldn't be updated safely; nothing was written."""


def _yaml_scalar(text: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .&'+_-]*", text) and text.lower() not in ("null", "true", "false", "yes", "no"):
        return text
    return json.dumps(text)  # a JSON string is valid YAML


def _entry_re(name: str) -> re.Pattern[str]:
    quoted = rf"(?:{re.escape(name)}|\"{re.escape(name)}\"|'{re.escape(name)}')"
    return re.compile(rf"^\s*-\s*\{{.*?\bname:\s*{quoted}\s*[,}}]", re.IGNORECASE)


def _set_field(line: str, key: str, value: str) -> str:
    pattern = re.compile(rf"(\b{key}:\s*)([^,}}]*?)(\s*[,}}])")
    if pattern.search(line):
        return pattern.sub(lambda m: f"{m.group(1)}{value}{m.group(3)}", line, count=1)
    close = line.rindex("}")
    return line[:close].rstrip() + f", {key}: {value} " + line[close:]


def update_companies_file(path: Path, discoveries: list[Discovery], *, backup: bool = True,
                          origin: str = "seed", header: str = _ADDED_HEADER) -> list[str]:
    """Write each discovery's ats and slug into companies.yaml. Returns the names it added as new entries.

    Companies with a network error are left alone. Companies found nowhere
    get `ats: unsupported`.
    """
    text = path.read_text() if path.exists() else "companies:\n"
    lines = text.split("\n")
    added: list[str] = []
    new_lines: list[str] = []

    for d in discoveries:
        if d.error:
            continue
        ats = d.ats or "unsupported"
        slug = d.slug or "null"
        index = next((i for i, line in enumerate(lines) if _entry_re(d.name).match(line)), None)
        if index is None:
            if any(re.search(rf"\bname:\s*['\"]?{re.escape(d.name)}['\"]?\s*$", ln, re.IGNORECASE) for ln in lines):
                raise EditError(f"{d.name} is written across several lines in {path}; set its ats and slug by hand")
            new_lines.append(f"  - {{ name: {_yaml_scalar(d.name)}, bucket: null, origin: {origin}, ats: {ats}, "
                             f"slug: {slug}, watch: false }}")
            added.append(d.name)
            continue
        lines[index] = _set_field(_set_field(lines[index], "ats", ats), "slug", slug)

    if new_lines:
        lines = _insert_new_entries(lines, new_lines, header)

    updated = "\n".join(lines)
    _check(updated, discoveries, path)
    if backup and path.exists():
        shutil.copyfile(path, path.with_name(path.name + ".bak"))
    path.write_text(updated)
    return added


def _insert_new_entries(lines: list[str], new_lines: list[str], header: str) -> list[str]:
    try:
        start = next(i for i, ln in enumerate(lines) if re.match(r"^companies:\s*(#.*)?$", ln))
    except StopIteration:
        lines = ["companies:"] + lines
        start = 0
    if re.match(r"^companies:\s*\[\s*\]\s*$", lines[start]):
        lines[start] = "companies:"
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "\t", "-"))):
        end += 1
    # back up over trailing blank or comment-only lines that belong to the next section
    insert_at = end
    while insert_at > start + 1 and (not lines[insert_at - 1].strip() or lines[insert_at - 1].lstrip().startswith("#")):
        if lines[insert_at - 1].strip() == header.strip():
            break
        insert_at -= 1
    if header in lines:
        # Under an existing header: add after that group's last entry.
        at = lines.index(header) + 1
        while at < len(lines) and lines[at].lstrip().startswith("- {"):
            at += 1
        return lines[:at] + new_lines + lines[at:]
    block = ["", header, *new_lines]
    return lines[:insert_at] + block + lines[insert_at:]


def _check(text: str, discoveries: list[Discovery], path: Path) -> None:
    """Re-read the edited text and confirm every discovery landed as intended."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise EditError(f"the edit would break {path} ({exc}); nothing was written") from exc
    entries = {str(e.get("name", "")).casefold(): e for e in data.get("companies") or [] if isinstance(e, dict)}
    for d in discoveries:
        if d.error:
            continue
        entry = entries.get(d.name.casefold())
        want = (d.ats or "unsupported", d.slug)
        if entry is None or (entry.get("ats"), entry.get("slug")) != want:
            raise EditError(f"couldn't update {d.name} in {path} cleanly; nothing was written")
