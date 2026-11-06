"""Command-line entry point: `jobfeed run --dry-run` and friends."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TextIO

import httpx

from jobfeed import __version__
from jobfeed.companies import DEFAULT_PATH, load_companies
from jobfeed.http import make_client
from jobfeed.sources import SOURCES, Board, FetchResult, fetch_all

DEFAULT_TEXT_LIMIT = 25


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

    run = sub.add_parser("run", help="fetch postings (only --dry-run works so far)")
    run.add_argument("--dry-run", action="store_true", help="print postings and write nothing")
    run.add_argument(
        "--board",
        action="append",
        type=_parse_board,
        metavar="SOURCE:SLUG",
        help="poll this board instead of companies.yaml; repeatable (e.g. greenhouse:anthropic)",
    )
    run.add_argument("--companies", type=Path, default=DEFAULT_PATH, help="path to companies.yaml")
    run.add_argument("--json", action="store_true", help="print one JSON Job per line")
    run.add_argument(
        "--limit",
        type=int,
        default=None,
        help=f"postings shown per board (text default {DEFAULT_TEXT_LIMIT}, JSON default all; 0 = all)",
    )

    sub.add_parser("discover", help="find each company's ATS (coming in milestone 5)")
    sub.add_parser("replay", help="re-run filters over stored postings (coming in milestone 3)")
    return parser


def _print_text(results: list[FetchResult], limit: int, out: TextIO) -> None:
    for result in results:
        board = result.board
        if not result.ok:
            continue
        company = result.jobs[0].company if result.jobs else (board.company or board.slug)
        print(f"\n== {company} ({board.label}) · {len(result.jobs)} postings", file=out)
        jobs = sorted(result.jobs, key=lambda j: j.posted_at.timestamp() if j.posted_at else 0, reverse=True)
        shown = jobs if limit == 0 else jobs[:limit]
        for job in shown:
            posted = job.posted_at.date().isoformat() if job.posted_at else "????-??-??"
            print(f"  {posted}  {job.title}", file=out)
            print(f"              {job.location_raw or '(no location)'}", file=out)
            print(f"              {job.url}", file=out)
        hidden = len(jobs) - len(shown)
        if hidden > 0:
            print(f"  … {hidden} more (use --limit 0 to show all)", file=out)


def _print_json(results: list[FetchResult], limit: int, out: TextIO) -> None:
    for result in results:
        jobs = result.jobs if limit == 0 else result.jobs[:limit]
        for job in jobs:
            print(json.dumps(job.to_dict(), ensure_ascii=False), file=out)


def _print_summary(results: list[FetchResult], err: TextIO) -> None:
    ok = [r for r in results if r.ok]
    total = sum(len(r.jobs) for r in ok)
    print(f"\n{len(results)} boards · {len(ok)} ok · {len(results) - len(ok)} failed · {total} postings", file=err)
    for r in results:
        if not r.ok:
            print(f"  FAILED {r.board.label}: {r.error}", file=err)
        elif not r.jobs:
            print(f"  EMPTY  {r.board.label}: board has no open postings", file=err)


def cmd_run(args: argparse.Namespace, transport: httpx.BaseTransport | None, out: TextIO, err: TextIO) -> int:
    if not args.dry_run:
        print("Only `jobfeed run --dry-run` works until the store and email are built "
              "(milestones 4 and 7). Nothing was fetched.", file=err)
        return 2

    if args.board:
        boards = args.board
    else:
        companies = load_companies(args.companies)
        boards = [b for b in companies.boards() if b.source in SOURCES]
        if not boards:
            print(f"No boards to poll: no company in {args.companies} has `ats` and `slug` filled in "
                  "for a supported source yet (`discover` arrives in milestone 5).\n"
                  "Try: jobfeed run --dry-run --board greenhouse:SLUG", file=err)
            return 2

    with make_client(transport) as client:
        results = fetch_all(client, boards)

    if args.json:
        _print_json(results, 0 if args.limit is None else args.limit, out)
    else:
        _print_text(results, DEFAULT_TEXT_LIMIT if args.limit is None else args.limit, out)
    _print_summary(results, err)
    return 0 if any(r.ok for r in results) else 1


def main(
    argv: list[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return cmd_run(args, transport, out, err)
    milestone = {"discover": 5, "replay": 3}[args.command]
    print(f"`jobfeed {args.command}` isn't built yet (milestone {milestone}).", file=err)
    return 2


if __name__ == "__main__":
    sys.exit(main())
