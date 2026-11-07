"""Command-line entry point: `jobfeed run`, `replay`, `discover` and `send-test`."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TextIO

import httpx

from jobfeed import __version__
from jobfeed.companies import DEFAULT_PATH as COMPANIES_PATH
from jobfeed.companies import load_companies
from jobfeed.config import DEFAULT_PATH as CONFIG_PATH
from jobfeed.config import Config, ConfigError, load_config
from jobfeed.dedupe import Plan, plan_run
from jobfeed.digest import Health, build_health, render_html, render_text, subject
from jobfeed.discover import HARVEST_HEADER, Discovery, EditError, discover_all, update_companies_file
from jobfeed.harvest import harvest
from jobfeed.filters import Digest
from jobfeed.http import make_client
from jobfeed.models import Job
from jobfeed.mailer import DEFAULT_ENV, GmailMailer, MailConfigError, Mailer, SendError, build_message, load_settings
from jobfeed.sources import SOURCES, Board, FetchResult, fetch_all
from jobfeed.store import DEFAULT_PATH as DB_PATH
from jobfeed.store import Store, StoreError, utc_now

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
    shared.add_argument("--db", type=Path, default=DB_PATH, help="path to jobs.db")

    run = sub.add_parser("run", parents=[shared], help="fetch, filter, save to jobs.db and email the digest")
    run.add_argument("--dry-run", action="store_true",
                     help="print the digest this run would send; read jobs.db but write and send nothing")
    run.add_argument("--no-email", action="store_true",
                     help="print the digest instead of emailing it; its postings still count as sent")
    run.add_argument("--html", type=Path, metavar="FILE", help="also write the email's HTML to FILE, to preview it")
    run.add_argument("--env", type=Path, default=DEFAULT_ENV, help="file with the Gmail settings (default .env)")
    run.add_argument(
        "--board",
        action="append",
        type=_parse_board,
        metavar="SOURCE:SLUG",
        help="poll this board instead of companies.yaml; repeatable (e.g. greenhouse:anthropic)",
    )
    run.add_argument("--no-aggregators", action="store_true",
                     help="poll only companies.yaml boards, not Remotive, Remote OK and Himalayas")
    run.add_argument("--json", action="store_true",
                     help="print every fetched posting as one JSON Job per line, unfiltered; writes nothing")

    replay = sub.add_parser("replay", parents=[shared],
                            help="re-run the current filters over postings in jobs.db (or a --json dump)")
    replay.add_argument("--from", dest="source_file", type=Path, metavar="FILE",
                        help="replay a `run --json` dump instead of jobs.db")
    replay.add_argument("--days", type=int, default=None,
                        help="only postings seen on a board in the last N days (jobs.db only)")

    discover = sub.add_parser("discover", help="find which ATS each company uses and write it to companies.yaml")
    discover.add_argument("names", nargs="*", metavar="NAME",
                          help="companies to look up (default: every company in companies.yaml without an ats)")
    discover.add_argument("--companies", type=Path, default=COMPANIES_PATH, help="path to companies.yaml")
    discover.add_argument("--retry", action="store_true",
                          help="also re-check companies already marked `ats: unsupported`")
    discover.add_argument("--dry-run", action="store_true", help="print what was found and change nothing")

    test = sub.add_parser("send-test", help="email a short test message, to check the Gmail settings")
    test.add_argument("--env", type=Path, default=DEFAULT_ENV, help="file with the Gmail settings (default .env)")
    return parser


# --- rendering -------------------------------------------------------------

def render_digest(digest: Digest, *, show_dropped: bool, today: date, out: TextIO, noun: str = "new") -> None:
    print(render_text(digest, today, show_dropped=show_dropped, noun=noun), file=out)


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


def _summary(plan: Plan, args: argparse.Namespace, err: TextIO, noun: str = "new") -> None:
    digest = plan.digest
    parts = [f"{digest.matched} {noun}"]
    if plan.duplicates:
        parts.append(f"{len(plan.duplicates)} duplicates skipped")
    if plan.already_sent:
        parts.append(f"{len(plan.already_sent)} already sent")
    parts.append(_drop_summary(digest) + ("" if args.all or not digest.dropped else " (add --all to see why)"))
    print(" · ".join(parts), file=err)


def _store_error(exc: StoreError, err: TextIO) -> int:
    print(f"jobs.db error: {exc}", file=err)
    return 2


def cmd_run(args: argparse.Namespace, transport: httpx.BaseTransport | None, out: TextIO, err: TextIO,
            today: date, now: datetime, mailer: Mailer | None = None) -> int:
    config = _load_config(args.config, err)
    if config is None:
        return 2
    _apply_limit(config, args.limit)
    write = not args.dry_run and not args.json
    email = write and not args.no_email

    # Check the Gmail settings before fetching anything, so a typo fails in seconds.
    settings = None
    if email:
        try:
            settings = load_settings(args.env)
        except (MailConfigError, OSError) as exc:
            print(f"email settings: {exc}.\nUse --no-email to print the digest instead, "
                  "or --dry-run to preview it.", file=err)
            return 2
        if settings.warning:
            print(f"warning: {settings.warning}", file=err)
        mailer = mailer or GmailMailer(settings)

    if args.board:
        boards = args.board
    else:
        boards = [b for b in load_companies(args.companies).boards() if b.source in SOURCES]
        if not args.no_aggregators:
            boards += aggregator_boards(config)
        if not boards:
            print(f"No boards to poll: no company in {args.companies} has `ats` and `slug` filled in, "
                  "and the aggregators are off.\n"
                  "Run `jobfeed discover`, or try: jobfeed run --dry-run --board greenhouse:SLUG", file=err)
            return 2

    with make_client(transport) as client:
        results = fetch_all(client, boards)
    jobs = [job for r in results if r.ok for job in r.jobs]
    status = 0 if any(r.ok for r in results) else 1

    if args.json:
        for job in jobs:
            print(json.dumps(job.to_dict(), ensure_ascii=False), file=out)
        _print_fetch_summary(results, err)
        return status

    try:
        store = Store.open(args.db) if write else Store.open_readonly(args.db)
    except StoreError as exc:
        return _store_error(exc, err)
    try:
        plan = plan_run(jobs, config, watched=_watched(args.companies, boards), store=store, now=now)
        previous = store.previous_counts(r.board.label for r in results) if store else {}
        health = build_health(results, plan, previous)
        if not email:
            render_digest(plan.digest, show_dropped=args.all, today=today, out=out)
        _summary(plan, args, err)
        _print_fetch_summary(results, err)
        for label, had in health.went_empty:
            print(f"  NOW EMPTY {label}: had {had} postings last run", file=err)
        if args.html:
            args.html.write_text(render_html(plan.digest, today, health=health))
            print(f"wrote the email's HTML to {args.html}", file=err)

        sent = plan.to_send
        if email:
            assert mailer is not None and settings is not None
            title = subject(plan.digest, today)
            to = ", ".join(settings.to)
            msg = build_message(settings, title,
                                render_text(plan.digest, today, health=health, overflow_hint="; they lead the next digest"),
                                render_html(plan.digest, today, health=health))
            try:
                mailer.send(msg)
                print(f"emailed \"{title}\" to {to}", file=err)
            except SendError as exc:
                print(f"EMAIL FAILED: {exc}\nNothing was marked as sent; the next run will include these postings.",
                      file=err)
                sent, status = [], 1
        if write:
            board_log = [(r.board.label, len(r.jobs), r.error) for r in results]
            store.save_run([(j, plan.keys[j.id], plan.matched[j.id]) for j in jobs], sent, now, board_log)
            print(f"saved {len(jobs)} postings to {args.db} · marked {len(sent)} as sent", file=err)
        if config.aggregators.harvest:
            _harvest(jobs, config, args, write=write, err=err)
    finally:
        if store:
            store.close()
    return status


def cmd_send_test(args: argparse.Namespace, err: TextIO, mailer: Mailer | None, now: datetime) -> int:
    try:
        settings = load_settings(args.env)
    except (MailConfigError, OSError) as exc:
        print(f"email settings: {exc}", file=err)
        return 2
    if settings.warning:
        print(f"warning: {settings.warning}", file=err)
    msg = build_message(settings, "jobfeed test email",
                        f"This is a test from jobfeed, sent {now.astimezone():%Y-%m-%d %H:%M %Z}.\n"
                        "If you're reading it, the Gmail settings in .env work.")
    try:
        (mailer or GmailMailer(settings)).send(msg)
    except SendError as exc:
        print(f"EMAIL FAILED: {exc}", file=err)
        return 1
    print(f"sent a test email to {', '.join(settings.to)}", file=err)
    return 0


def aggregator_boards(config: Config) -> list[Board]:
    agg = config.aggregators
    boards: list[Board] = []
    if agg.remotive:
        boards.append(Board("remotive", agg.remotive_category.strip()))
    if agg.remoteok:
        boards.append(Board("remoteok", ",".join(agg.remoteok_tags) or "all"))
    if agg.himalayas:
        options = (("pages", str(agg.himalayas_pages)),)
        if agg.himalayas_country.strip():
            options += (("country", agg.himalayas_country.strip()),)
        boards += [Board("himalayas", q.strip(), options=options) for q in agg.himalayas_queries if q.strip()]
    return boards


def _harvest(jobs: list[Job], config: Config, args: argparse.Namespace, *, write: bool, err: TextIO) -> None:
    try:
        found = harvest(jobs, load_companies(args.companies), config)
    except ValueError as exc:
        print(f"harvest skipped: can't read {args.companies}: {exc}", file=err)
        return
    if not found:
        return
    listed = ", ".join(f"{d.name} ({d.ats}:{d.slug})" for d in found)
    if not write:
        print(f"would add {len(found)} companies from aggregator links: {listed}", file=err)
        return
    try:
        update_companies_file(args.companies, found, origin="harvested", header=HARVEST_HEADER)
    except (EditError, OSError) as exc:
        print(f"harvest found {len(found)} companies but {args.companies} wasn't changed: {exc}", file=err)
        return
    print(f"added {len(found)} companies to {args.companies} from aggregator links (polled from the next run): "
          f"{listed}", file=err)


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


def cmd_replay(args: argparse.Namespace, out: TextIO, err: TextIO, today: date, now: datetime) -> int:
    config = _load_config(args.config, err)
    if config is None:
        return 2
    _apply_limit(config, args.limit)

    sent_on: dict[str, datetime] = {}
    if args.source_file is not None:
        try:
            jobs = read_jobs(args.source_file)
        except (OSError, ValueError) as exc:
            print(f"can't read postings: {exc}", file=err)
            return 2
        origin = str(args.source_file)
    else:
        try:
            store = Store.open_readonly(args.db)
        except StoreError as exc:
            return _store_error(exc, err)
        if store is None:
            print(f"No {args.db} yet. Run `jobfeed run` to start one, or replay a saved dump:\n"
                  "  jobfeed run --json --board greenhouse:SLUG > postings.jsonl\n"
                  "  jobfeed replay --from postings.jsonl", file=err)
            return 2
        with store:
            since = now - timedelta(days=args.days) if args.days else None
            rows = store.all_rows(seen_since=since)
        jobs = [r.job for r in rows]
        sent_on = {r.job.id: r.emailed_at for r in rows if r.emailed_at}
        origin = str(args.db) + (f" (seen in the last {args.days} days)" if args.days else "")

    # Replay shows the rules' full picture: sent postings stay in, tagged with their send date.
    plan = plan_run(jobs, config, watched=_watched(args.companies, []), store=None, now=now)
    for entries in plan.digest.sections.values():
        for entry in entries:
            if sent := sent_on.get(entry.job.id):
                entry.verdict.tags.append(f"sent {sent.date().isoformat()}")
    render_digest(plan.digest, show_dropped=args.all, today=today, out=out, noun="matched")
    _summary(plan, args, err, noun="matched")
    print(f"{len(jobs)} postings replayed from {origin}", file=err)
    return 0


def _discovery_line(d: Discovery, width: int) -> str:
    if d.error:
        return f"  {d.name:<{width}}  ?  couldn't check: {d.error}"
    if not d.found:
        tried = ", ".join(sorted({t.partition(":")[2] for t in d.tried}))
        return f"  {d.name:<{width}}  -  not on Greenhouse, Lever or Ashby (tried {tried})"
    line = f"  {d.name:<{width}}  ✓  {d.ats}:{d.slug} · {d.postings} postings"
    return line + "".join(f"\n  {'':<{width}}     check: {w}" for w in d.warnings)


def cmd_discover(args: argparse.Namespace, transport: httpx.BaseTransport | None, out: TextIO,
                 err: TextIO) -> int:
    try:
        companies = load_companies(args.companies)
    except ValueError as exc:
        print(f"can't read {args.companies}: {exc}", file=err)
        return 2
    if args.names:
        names = list(dict.fromkeys(args.names))
    else:
        names = [c.name for c in companies.companies
                 if c.ats is None or (args.retry and c.ats == "unsupported")]
        if not names:
            print(f"Every company in {args.companies} already has an ats"
                  + ("" if args.retry else " (add --retry to re-check the unsupported ones)") + ".", file=err)
            return 0

    print(f"Checking {len(names)} companies on Greenhouse, Lever and Ashby…", file=err)
    with make_client(transport) as client:
        found = discover_all(client, names)

    width = min(max(len(n) for n in names), 28)
    for d in sorted(found, key=lambda d: (d.error is not None, not d.found, d.name.casefold())):
        print(_discovery_line(d, width), file=out)

    hits = [d for d in found if d.found]
    by_ats = {a: sum(1 for d in hits if d.ats == a) for a in SOURCES}
    misses = sum(1 for d in found if not d.found and not d.error)
    errors = sum(1 for d in found if d.error)
    checks = sum(1 for d in hits if d.warnings)
    parts = [f"{len(found)} checked", f"{len(hits)} found ("
             + ", ".join(f"{n} {a}" for a, n in by_ats.items() if n) + ")" if hits else "0 found",
             f"{misses} unsupported"]
    if errors:
        parts.append(f"{errors} couldn't be checked (left as they were)")
    if checks:
        parts.append(f"{checks} worth a look (see 'check:')")
    print("\n" + " · ".join(parts), file=out)

    if args.dry_run:
        print("Dry run: companies.yaml not changed.", file=err)
    else:
        try:
            added = update_companies_file(args.companies, found)
        except (EditError, OSError) as exc:
            print(f"companies.yaml not changed: {exc}", file=err)
            return 2
        note = f" (added {len(added)} new: {', '.join(added)})" if added else ""
        print(f"Updated {args.companies}{note}; the previous version is in {args.companies.name}.bak", file=err)
    return 1 if found and errors == len(found) else 0


def main(
    argv: list[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    now: datetime | None = None,
    mailer: Mailer | None = None,
) -> int:
    """`mailer` replaces Gmail in tests; settings are still read from --env."""
    out = out or sys.stdout
    err = err or sys.stderr
    now = now or utc_now()
    today = now.astimezone().date()
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return cmd_run(args, transport, out, err, today, now, mailer)
    if args.command == "replay":
        return cmd_replay(args, out, err, today, now)
    if args.command == "send-test":
        return cmd_send_test(args, err, mailer, now)
    return cmd_discover(args, transport, out, err)


if __name__ == "__main__":
    sys.exit(main())
