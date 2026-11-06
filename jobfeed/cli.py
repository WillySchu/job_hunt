"""Command-line entry point: `jobfeed run --dry-run`, `jobfeed replay`."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import TextIO

import httpx

from jobfeed import __version__
from jobfeed.companies import DEFAULT_PATH as COMPANIES_PATH
from jobfeed.companies import load_companies
from jobfeed.config import DEFAULT_PATH as CONFIG_PATH
from jobfeed.config import Config, ConfigError, load_config
from jobfeed.filters import SECTION_ORDER, SECTION_TITLES, WATCHLIST, Digest, Entry, build_digest
from jobfeed.http import make_client
from jobfeed.models import Job
from jobfeed.normalize import format_salary
from jobfeed.sources import SOURCES, Board, FetchResult, fetch_all

MAX_LOCATION_CHARS = 70


def _parse_board(spec: str) -> Board:
    source, sep, slug = spec.partition(":")
    if not sep or not source or not slug:
        raise argparse.ArgumentTypeError(f"expected SOURCE:SLUG, e.g. greenhouse:anthropic (got '{spec}')")
    if source not in SOURCES:
        known = ", ".join(sorted(SOURCES))
        raise argparse.ArgumentTypeError(f"unknown source '{source}' (available: {known})")
    return Board(source=source, slug=slug.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobfeed", description="Daily digest of new job postings.")
    parser.add_argument("--version", action="version", version=f"jobfeed {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--config", type=Path, default=CONFIG_PATH, help="path to config.yaml")
    shared.add_argument("--companies", type=Path, default=COMPANIES_PATH, help="path to companies.yaml")
    shared.add_argument("--all", action="store_true", help="also list dropped postings and why")
    shared.add_argument("--limit", type=int, default=None,
                        help="postings per section (default: digest.max_per_section; 0 = all)")

    run = sub.add_parser("run", parents=[shared], help="fetch and filter postings (only --dry-run works so far)")
    run.add_argument("--dry-run", action="store_true", help="print the digest and write nothing")
    run.add_argument(
        "--board",
        action="append",
        type=_parse_board,
        metavar="SOURCE:SLUG",
        help="poll this board instead of companies.yaml; repeatable (e.g. greenhouse:anthropic)",
    )
    run.add_argument("--json", action="store_true",
                     help="print every fetched posting as one JSON Job per line, unfiltered (input for replay)")

    replay = sub.add_parser("replay", parents=[shared], help="re-run the current filters over saved postings")
    replay.add_argument("--from", dest="source_file", type=Path, metavar="FILE",
                        help="a `run --dry-run --json` dump (jobs.db becomes the default in milestone 4)")

    sub.add_parser("discover", help="find each company's ATS (coming in milestone 5)")
    return parser


# --- rendering -------------------------------------------------------------

def _pay(job: Job) -> str:
    if job.salary_min is not None or job.salary_max is not None:
        return format_salary(job.salary_min, job.salary_max)
    if job.salary_raw:
        return f"{job.salary_raw} ({job.salary_currency}, not compared)"
    return "no salary listed"


def _mode(job: Job) -> str:
    if job.work_mode == "remote":
        return f"remote ({job.remote_region or 'region unstated'})"
    return job.work_mode


def _location(job: Job) -> str:
    loc = job.location_raw or "no location given"
    return loc if len(loc) <= MAX_LOCATION_CHARS else loc[: MAX_LOCATION_CHARS - 1] + "…"


def _entry_lines(entry: Entry) -> list[str]:
    job = entry.job
    head = f"{job.title} — {job.company} · {_location(job)} · {_pay(job)}"
    if job.seniority:
        head += f" · {job.seniority.title()}"
    detail = [_mode(job)]
    if job.remote_states:
        detail.append("states: " + ", ".join(job.remote_states))
    detail += entry.verdict.tags
    detail.append(f"via {job.source.title()}")
    if job.posted_at:
        detail.append(f"posted {job.posted_at.date().isoformat()}")
    return [f"  {head}", f"      {' · '.join(detail)}", f"      {job.url}"]


def render_digest(digest: Digest, *, show_dropped: bool, today: date, out: TextIO) -> None:
    watch_count = len(digest.sections[WATCHLIST]) + digest.hidden.get(WATCHLIST, 0)
    print(f"Job feed · {today.strftime('%a %b')} {today.day} · {digest.matched} new ({watch_count} watchlist)",
          file=out)
    if not digest.matched:
        print("\nNothing new matched.", file=out)
    for name in SECTION_ORDER:
        entries = digest.sections[name]
        hidden = digest.hidden.get(name, 0)
        if not entries:
            continue
        print(f"\n== {SECTION_TITLES[name]} ({len(entries) + hidden})", file=out)
        for entry in entries:
            print("\n".join(_entry_lines(entry)), file=out)
        if hidden:
            print(f"  … {hidden} more not shown (raise digest.max_per_section or use --limit 0)", file=out)
    if show_dropped and digest.dropped:
        print(f"\n== Dropped ({len(digest.dropped)})", file=out)
        for entry in sorted(digest.dropped, key=lambda e: (e.verdict.stage or "", e.job.company, e.job.title)):
            job = entry.job
            print(f"  {entry.verdict.stage:<8}  {job.title} — {job.company} · {_location(job)}", file=out)
            print(f"            {entry.verdict.reason}", file=out)


def _drop_summary(digest: Digest) -> str:
    counts: dict[str, int] = {}
    for e in digest.dropped:
        counts[e.verdict.stage or "?"] = counts.get(e.verdict.stage or "?", 0) + 1
    parts = ", ".join(f"{stage} {counts[stage]}" for stage in ("role", "location", "salary") if stage in counts)
    return f"{len(digest.dropped)} dropped" + (f" ({parts})" if parts else "")


def _print_fetch_summary(results: list[FetchResult], err: TextIO) -> None:
    ok = [r for r in results if r.ok]
    total = sum(len(r.jobs) for r in ok)
    print(f"{len(results)} boards · {len(ok)} ok · {len(results) - len(ok)} failed · {total} postings", file=err)
    for r in results:
        if not r.ok:
            print(f"  FAILED {r.board.label}: {r.error}", file=err)
        elif not r.jobs:
            print(f"  EMPTY  {r.board.label}: board has no open postings", file=err)


# --- commands --------------------------------------------------------------

def _load_config(path: Path, err: TextIO) -> Config | None:
    try:
        return load_config(path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=err)
        return None


def _apply_limit(config: Config, limit: int | None) -> None:
    if limit is not None:
        config.digest.max_per_section = limit if limit > 0 else 10**9


def _watched(companies_path: Path, boards: list[Board]) -> set[str]:
    names = {c.name.casefold() for c in load_companies(companies_path).companies if c.watch}
    return names | {b.company.casefold() for b in boards if b.watch and b.company}


def _finish(digest: Digest, args: argparse.Namespace, out: TextIO, err: TextIO, today: date) -> None:
    render_digest(digest, show_dropped=args.all, today=today, out=out)
    print(f"{digest.matched} matched · {_drop_summary(digest)}"
          + ("" if args.all else " (add --all to see why)" if digest.dropped else ""), file=err)


def cmd_run(args: argparse.Namespace, transport: httpx.BaseTransport | None, out: TextIO, err: TextIO,
            today: date) -> int:
    if not args.dry_run:
        print("Only `jobfeed run --dry-run` works until the store and email are built "
              "(milestones 4 and 7). Nothing was fetched.", file=err)
        return 2
    config = _load_config(args.config, err)
    if config is None:
        return 2
    _apply_limit(config, args.limit)

    if args.board:
        boards = args.board
    else:
        boards = [b for b in load_companies(args.companies).boards() if b.source in SOURCES]
        if not boards:
            print(f"No boards to poll: no company in {args.companies} has `ats` and `slug` filled in "
                  "for a supported source yet (`discover` arrives in milestone 5).\n"
                  "Try: jobfeed run --dry-run --board greenhouse:SLUG", file=err)
            return 2

    with make_client(transport) as client:
        results = fetch_all(client, boards)
    jobs = [job for r in results if r.ok for job in r.jobs]

    if args.json:
        for job in jobs:
            print(json.dumps(job.to_dict(), ensure_ascii=False), file=out)
        _print_fetch_summary(results, err)
        return 0 if any(r.ok for r in results) else 1

    digest = build_digest(jobs, config, watched=_watched(args.companies, boards))
    _finish(digest, args, out, err, today)
    _print_fetch_summary(results, err)
    return 0 if any(r.ok for r in results) else 1


def read_jobs(path: Path) -> list[Job]:
    jobs = []
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            jobs.append(Job.from_dict(json.loads(line)))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{path}:{n} isn't a Job record ({exc})") from exc
    return jobs


def cmd_replay(args: argparse.Namespace, out: TextIO, err: TextIO, today: date) -> int:
    if args.source_file is None:
        print("Replay reads jobs.db once the store exists (milestone 4). Until then, save postings with\n"
              "  jobfeed run --dry-run --json --board greenhouse:SLUG > postings.jsonl\n"
              "and replay them with\n"
              "  jobfeed replay --from postings.jsonl", file=err)
        return 2
    config = _load_config(args.config, err)
    if config is None:
        return 2
    _apply_limit(config, args.limit)
    try:
        jobs = read_jobs(args.source_file)
    except (OSError, ValueError) as exc:
        print(f"can't read postings: {exc}", file=err)
        return 2
    digest = build_digest(jobs, config, watched=_watched(args.companies, []))
    _finish(digest, args, out, err, today)
    print(f"{len(jobs)} postings replayed from {args.source_file}", file=err)
    return 0


def main(
    argv: list[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    today: date | None = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    today = today or date.today()
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return cmd_run(args, transport, out, err, today)
    if args.command == "replay":
        return cmd_replay(args, out, err, today)
    print(f"`jobfeed {args.command}` isn't built yet (milestone 5).", file=err)
    return 2


if __name__ == "__main__":
    sys.exit(main())
