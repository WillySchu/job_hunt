"""Command-line entry point: `jobfeed run`, `replay`, `discover`, `send-test`, and the tracker commands
(`apply`, `mark`, `note`, `apps`, `undo`, `edit`, `mail`, `stats`, `weekly`, `backup`), and `fit`."""

from __future__ import annotations

import argparse
import json
import sqlite3
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
from jobfeed.filters import SECTION_TITLES, Digest
from jobfeed.http import make_client
from jobfeed.models import Job
from jobfeed.mailer import DEFAULT_ENV, GmailMailer, MailConfigError, Mailer, SendError, build_message, load_settings
from jobfeed.sources import SOURCE_NAMES, SOURCES, Board, FetchResult, fetch_all
from jobfeed.store import DEFAULT_PATH as DB_PATH
from jobfeed.store import Store, StoreError, utc_now
from jobfeed import fit, followups, llm, mailbox, scoring, stats, tracker, weekly
from jobfeed.imap import GmailReader, ReadError
from jobfeed.tracker import TrackerError

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

    _tracker_parsers(sub)
    return parser


def _code(text: str) -> int:
    value = text.removeprefix("#")
    if not value.isdigit():
        raise argparse.ArgumentTypeError(f"'{text}' isn't a code; codes are the numbers in the digest, like 412")
    return int(value)


def _tracker_parsers(sub) -> None:
    db = argparse.ArgumentParser(add_help=False)
    db.add_argument("--db", type=Path, default=DB_PATH, help="path to jobs.db")
    dated = argparse.ArgumentParser(add_help=False)
    dated.add_argument("--date", metavar="YYYY-MM-DD", help="when it happened, if not today")

    apply = sub.add_parser(
        "apply", parents=[db, dated], help="log an application, by its digest code or with --url",
        description="Log applications. `jobfeed apply 412 415` logs two postings from the digest; "
                    "`jobfeed apply --url URL` logs one by its link, and needs --company and --title "
                    "when jobfeed never saw the posting.")
    apply.add_argument("codes", nargs="*", type=_code, metavar="CODE", help="digest codes, e.g. 412")
    apply.add_argument("--url", help="the posting's link, for a role without a code")
    apply.add_argument("--company", default="", help="company, for a role jobfeed never saw")
    apply.add_argument("--title", default="", help="job title, for a role jobfeed never saw")
    apply.add_argument("--via", choices=tracker.VIA_CHOICES, default=None,
                       help="where you found a role jobfeed never saw (default: referral with --referral, else other)")
    apply.add_argument("--resume", metavar="VERSION", help="resume version sent (default: tracking.resume_version)")
    apply.add_argument("--cover-letter", action=argparse.BooleanOptionalAction, default=None,
                       help="whether you sent a cover letter (default: tracking.cover_letter)")
    apply.add_argument("--referral", action="store_true", help="someone referred you")
    apply.add_argument("--note", help="a note to keep with the application")
    apply.add_argument("--config", type=Path, default=CONFIG_PATH, help="path to config.yaml")

    stages = ", ".join(k.replace("_", "-") for k in tracker.MARKABLE if k != "reopened") + ", reopen"
    mark = sub.add_parser("mark", parents=[db, dated], help="record a reply or a new stage for an application",
                          description=f"Record what happened. STAGE is one of: {stages}. "
                                      "onsite and technical count as interview; ghosted as no-response.")
    mark.add_argument("code", type=_code, metavar="CODE")
    mark.add_argument("stage", metavar="STAGE")
    mark.add_argument("--note", help="a note to keep with it")

    note = sub.add_parser("note", parents=[db], help="add a note to an application")
    note.add_argument("code", type=_code, metavar="CODE")
    note.add_argument("text", nargs="+", metavar="TEXT")

    apps = sub.add_parser("apps", parents=[db], help="list open applications, or show one's history")
    apps.add_argument("code", nargs="?", type=_code, metavar="CODE", help="show this application in full")
    apps.add_argument("--all", action="store_true", help="include closed applications")

    undo = sub.add_parser("undo", parents=[db], help="remove an application's latest entry")
    undo.add_argument("code", type=_code, metavar="CODE")

    edit = sub.add_parser("edit", parents=[db], help="change how an application was sent: resume, cover letter, ...")
    edit.add_argument("code", type=_code, metavar="CODE")
    edit.add_argument("--resume", metavar="VERSION", help="the resume version you sent")
    edit.add_argument("--cover-letter", action=argparse.BooleanOptionalAction, default=None)
    edit.add_argument("--referral", action=argparse.BooleanOptionalAction, default=None)
    edit.add_argument("--via", choices=tracker.VIA_CHOICES, help="where you found a role jobfeed never saw")

    st = sub.add_parser("stats", parents=[db], help="reply, screen, interview and offer rates, and what changes them",
                        description="Rates from your applications, each with a likely range, split by what might "
                                    "change them. Rates count applications at least tracking.no_response_days old, "
                                    "or closed.")
    st.add_argument("--by", action="append", choices=[*stats.CUT_KEYS, "all"], metavar="CUT",
                    help=f"show this split even with one group; repeatable ({', '.join(stats.CUT_KEYS)}, or all). "
                         "Default: every split with at least two groups")
    st.add_argument("--metric", choices=[k for k, _, _ in stats.METRICS], default="replied",
                    help="the rate each split's verdict compares (default replied)")
    st.add_argument("--since", metavar="YYYY-MM-DD", help="only applications from this day on")
    st.add_argument("--config", type=Path, default=CONFIG_PATH, help="path to config.yaml")

    wk = sub.add_parser("weekly", parents=[db], help="send the weekly report now (it also goes out on its own)",
                        description="Email the weekly report now, covering the last 7 days. The Monday report "
                                    "still goes out as usual.")
    wk.add_argument("--no-email", action="store_true", help="print it instead")
    wk.add_argument("--env", type=Path, default=DEFAULT_ENV, help="file with the Gmail settings (default .env)")
    wk.add_argument("--config", type=Path, default=CONFIG_PATH, help="path to config.yaml")

    bk = sub.add_parser("backup", parents=[db], help="copy jobs.db into backups/ now")
    bk.add_argument("--config", type=Path, default=CONFIG_PATH, help="path to config.yaml")

    ft = sub.add_parser("fit", help="fit scores: backfill, your skills profile, status (scores stay hidden)",
                        description="Fit scores are computed for every posting the digest sends and kept hidden "
                                    "during the baseline; none of these commands shows a posting's score.")
    fsub = ft.add_subparsers(dest="fit_command", required=True)
    fshared = argparse.ArgumentParser(add_help=False)
    fshared.add_argument("--db", type=Path, default=DB_PATH, help="path to jobs.db")
    fshared.add_argument("--config", type=Path, default=CONFIG_PATH, help="path to config.yaml")
    fshared.add_argument("--env", type=Path, default=DEFAULT_ENV, help="file with ANTHROPIC_API_KEY (default .env)")
    fsub.add_parser("backfill", parents=[fshared],
                    help="score every posting sent that has no score yet (the LLM within its caps)")
    fsub.add_parser("profile", parents=[fshared], help="the skills and experience read from your resume")
    fsub.add_parser("status", parents=[fshared], help="how many postings are scored, against which resume, and spend")
    fsub.add_parser("check", parents=[fshared],
                    help="score one made-up posting with the LLM, to check the key and the reply")

    mail = sub.add_parser("mail", parents=[db], help="read replies to the digest and job mail from Gmail now",
                          description="Read new replies to the digest, and job mail (confirmations, rejections, "
                                      "interview requests, offers), from Gmail and act on them, as `jobfeed run` "
                                      "does before each digest.")
    mail.add_argument("--dry-run", action="store_true",
                      help="show how each new message would be read and what it would do; save nothing")
    mail.add_argument("--days", type=int, metavar="N",
                      help="look back N days instead of from the last check (mail already read is skipped)")
    mail.add_argument("--reread", action="store_true",
                      help="with --dry-run: include mail already read, to see how the current rules read it")
    mail.add_argument("--env", type=Path, default=DEFAULT_ENV, help="file with the Gmail settings (default .env)")
    mail.add_argument("--config", type=Path, default=CONFIG_PATH, help="path to config.yaml")


# --- rendering -------------------------------------------------------------

def render_digest(digest: Digest, *, show_dropped: bool, today: date, out: TextIO, noun: str = "new",
                  codes: dict[str, int] | None = None, apps=None) -> None:
    print(render_text(digest, today, show_dropped=show_dropped, noun=noun, codes=codes, apps=apps), file=out)


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


def _backup(store: Store, db: Path, now: datetime, keep: int) -> str:
    try:
        dest = weekly.backup(store, db, now, keep)
    except (OSError, sqlite3.Error) as exc:
        return f"Backup of {db} failed: {exc}. The next run tries again."
    return f"Backed up {db} to {dest}" + (f" (keeping the newest {keep})." if keep else ".")


def _send_weekly(store: Store, day: date, config: Config, now: datetime, mailer: Mailer | None, settings,
                 out: TextIO, err: TextIO, *, record: bool = True) -> bool:
    """Email the weekly report (or print it, with no mailer). Never raises; False if it couldn't be sent."""
    packed, line, alert = None, None, False
    if config.tracking.attach_backup and mailer is not None:
        try:
            packed = weekly.pack_backup(store, now)
            line, alert = packed.line, packed.alert
        except (OSError, sqlite3.Error) as exc:
            line, alert = f"BACKUP NOT ATTACHED: copying jobs.db failed ({exc}).", True
    report = weekly.render(stats.gather(store, now, config.tracking), config.tracking, now, day, line, alert)
    if alert:
        print(f"weekly report: {line}", file=err)
    if mailer is None:
        print("\n" + report.text, file=out)
    else:
        attachments = [(packed.name, packed.data, "application", "gzip")] if packed and packed.data else []
        try:
            mailer.send(build_message(settings, report.subject, report.text, report.html, attachments))
        except SendError as exc:
            print(f"weekly report not sent: {exc}. The next run tries again.", file=err)
            return False
        print(f"emailed \"{report.subject}\" to {', '.join(settings.to)}", file=err)
    if record:
        weekly.mark_sent(store, day)
    return True


def _activity_summary(activity: followups.Activity) -> str:
    closed = sum(1 for c in activity.changes if c.event.kind == "no_response" and c.event.origin == followups.RULE)
    updates = len(activity.changes) - closed
    parts = [f"{len(activity.logged)} logged", f"{updates} update{'' if updates == 1 else 's'}"]
    if closed:
        parts.append(f"{closed} closed with no reply")
    parts.append(f"{len(activity.follow_ups)} to follow up")
    return " · ".join(parts)


def _store_error(exc: StoreError, err: TextIO) -> int:
    print(f"jobs.db error: {exc}", file=err)
    return 2


def _read_gmail(store: Store, settings, config: Config, now: datetime, imap_factory, err: TextIO,
                env: Path) -> str:
    """Read replies before the digest is built; returns the footer line. Never stops the run."""
    if settings is None:
        try:
            settings = load_settings(env)
        except (MailConfigError, OSError):
            return f"Gmail not read: no Gmail settings in {env}."
    try:
        with GmailReader(settings, imap_factory=imap_factory) as reader:
            return mailbox.check(store, reader, settings, config.tracking, now).summary()
    except ReadError as exc:
        return f"Couldn't read Gmail: {exc}. The next run will pick up any replies."


def cmd_run(args: argparse.Namespace, transport: httpx.BaseTransport | None, out: TextIO, err: TextIO,
            today: date, now: datetime, mailer: Mailer | None = None, imap_factory=None) -> int:
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

        # The weekly copy of jobs.db, before this run changes anything.
        if write and weekly.backup_due(store, now, config.tracking):
            health.backup_note = _backup(store, args.db, now, config.tracking.backups_kept)
            print(health.backup_note, file=err)

        # Save postings first, so the ones in this digest can get their codes
        # before it's rendered. A dry run writes nothing and shows no codes.
        codes: dict[str, int] = {}
        if write:
            store.save_jobs([(j, plan.keys[j.id], plan.matched[j.id]) for j in jobs], now)
            codes = store.assign_codes([(e.job.id, name) for name, entries in plan.digest.sections.items()
                                        for e in entries])

        # Fit scores for the postings in this digest; stored, not shown (FIT_SCORER_SPEC.md).
        if write and config.fit.enabled:
            digest_jobs = [e.job for entries in plan.digest.sections.values() for e in entries]
            with make_client(transport) as client:
                health.fit_note = scoring.run(store, digest_jobs, config, now, client, args.db.parent,
                                              args.env).note()
            if health.fit_note:
                print(health.fit_note, file=err)

        # Replies to earlier digests first, so what they log shows in this one.
        if write and config.tracking.read_mail:
            health.mail_note = _read_gmail(store, settings, config, now, imap_factory, err, args.env)
            print(health.mail_note, file=err)

        # The "Your applications" block. A real run applies the no-response rule
        # first; a dry run shows what it would close without saving it.
        activity = followups.gather(store, config.tracking, now, write=write)
        apps = followups.blocks(activity, config.tracking.follow_up_days) if activity and not activity.empty else None

        if not email:
            render_digest(plan.digest, show_dropped=args.all, today=today, out=out, codes=codes, apps=apps)
        _summary(plan, args, err)
        _print_fetch_summary(results, err)
        for label, had in health.went_empty:
            print(f"  NOW EMPTY {label}: had {had} postings last run", file=err)
        if args.html:
            args.html.write_text(render_html(plan.digest, today, health=health, codes=codes, apps=apps))
            print(f"wrote the email's HTML to {args.html}", file=err)

        sent = plan.to_send
        if email:
            assert mailer is not None and settings is not None
            title = subject(plan.digest, today)
            to = ", ".join(settings.to)
            msg = build_message(settings, title,
                                render_text(plan.digest, today, health=health, codes=codes, apps=apps,
                                            overflow_hint="; they lead the next digest"),
                                render_html(plan.digest, today, health=health, codes=codes, apps=apps))
            try:
                mailer.send(msg)
                print(f"emailed \"{title}\" to {to}", file=err)
            except SendError as exc:
                print(f"EMAIL FAILED: {exc}\nNothing was marked as sent; the next run will include these postings.",
                      file=err)
                sent, status = [], 1
                activity = None   # nothing reported: the next digest repeats it
        if write:
            board_log = [(r.board.label, len(r.jobs), r.error) for r in results]
            store.mark_sent(sent, now, board_log)
            print(f"saved {len(jobs)} postings to {args.db} · marked {len(sent)} as sent", file=err)
            if activity and not activity.empty:
                followups.record(store, activity, now)
                print("applications: " + _activity_summary(activity), file=err)
            # The weekly report, after the digest; never when the digest itself couldn't be sent.
            day = weekly.report_due(store, now, config.tracking)
            if day is not None and status == 0:
                _send_weekly(store, day, config, now, mailer if email else None, settings, out, err)
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
    codes: dict[str, int] = {}
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
        codes = {r.job.id: r.code for r in rows if r.code is not None}
        origin = str(args.db) + (f" (seen in the last {args.days} days)" if args.days else "")

    # Replay shows the rules' full picture: sent postings stay in, tagged with their send date.
    plan = plan_run(jobs, config, watched=_watched(args.companies, []), store=None, now=now)
    for entries in plan.digest.sections.values():
        for entry in entries:
            if sent := sent_on.get(entry.job.id):
                entry.verdict.tags.append(f"sent {sent.date().isoformat()}")
    render_digest(plan.digest, show_dropped=args.all, today=today, out=out, noun="matched", codes=codes)
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


# --- the tracker ---------------------------------------------------------------

def _open_store(path: Path, err: TextIO) -> Store | None:
    try:
        return Store.open(path)
    except StoreError as exc:
        _store_error(exc, err)
        return None


def _when(args: argparse.Namespace, now: datetime, err: TextIO) -> datetime | None:
    if not getattr(args, "date", None):
        return now
    try:
        return tracker.parse_day(args.date, now)
    except TrackerError as exc:
        print(f"--date: {exc}", file=err)
        return None


def _logged(app: tracker.Application) -> str:
    extras = [f"resume {app.resume_version}"]
    if app.cover_letter:
        extras.append("cover letter")
    if app.referral:
        extras.append("referral")
    return (f"Logged #{app.code} {app.company} — {app.title} "
            f"(applied {tracker.local_day(app.applied_at)}; {', '.join(extras)})")


def cmd_apply(args: argparse.Namespace, out: TextIO, err: TextIO, now: datetime) -> int:
    if args.codes and args.url:
        print("give codes or --url, not both", file=err)
        return 2
    if not args.codes and not args.url:
        print("which application? Give its digest code (`jobfeed apply 412`), or --url for a role without one",
              file=err)
        return 2
    if args.codes and (args.company or args.title or args.via):
        print("--company, --title and --via are for --url; a code already says which posting it is", file=err)
        return 2
    config = _load_config(args.config, err)
    if config is None:
        return 2
    when = _when(args, now, err)
    if when is None:
        return 2
    resume = (args.resume or config.tracking.resume_version).strip()
    cover = config.tracking.cover_letter if args.cover_letter is None else args.cover_letter
    store = _open_store(args.db, err)
    if store is None:
        return 2
    failed = 0
    with store:
        if args.url:
            opts = tracker.LogOptions(applied_at=when, resume_version=resume, cover_letter=cover,
                                      referral=args.referral, note=args.note, logged_via="manual")
            try:
                row = store.by_url(args.url)
                if row is not None:
                    if args.company or args.title or args.via:
                        print("(that link is a posting jobfeed saved, so --company/--title/--via are ignored)",
                              file=err)
                    app = tracker.log_posting(store, row, opts)
                else:
                    via = args.via or ("referral" if args.referral else "other")
                    app = tracker.log_manual(store, url=args.url, company=args.company, title=args.title,
                                             via=via, opts=opts)
            except TrackerError as exc:
                print(f"not logged: {exc}", file=err)
                return 1
            print(_logged(app), file=out)
            if app.job_id is None:
                print(f"  its code is #{app.code}; use it with `jobfeed mark` and `jobfeed note`", file=out)
            return 0
        for code in dict.fromkeys(args.codes):   # once each, in the order given
            opts = tracker.LogOptions(applied_at=when, resume_version=resume, cover_letter=cover,
                                      referral=args.referral, note=args.note)
            try:
                row = store.by_code(code)
                if row is None:
                    existing = tracker.get_application(store, code)
                    if existing is None:
                        raise TrackerError(f"no posting #{code} in {args.db}; codes come from the digest "
                                           "(log other roles with --url)")
                    raise TrackerError(f"#{code} is already logged ({existing.company}, applied "
                                       f"{tracker.local_day(existing.applied_at)})")
                app = tracker.log_posting(store, row, opts)
            except TrackerError as exc:
                print(f"#{code} not logged: {exc}", file=err)
                failed += 1
                continue
            print(_logged(app), file=out)
    return 1 if failed else 0


def cmd_mark(args: argparse.Namespace, out: TextIO, err: TextIO, now: datetime) -> int:
    kind = tracker.ALIASES.get(args.stage.strip().lower().replace(" ", "-"))
    if kind is None:
        print(f"'{args.stage}' isn't a stage; `jobfeed mark --help` lists them", file=err)
        return 2
    when = _when(args, now, err)
    if when is None:
        return 2
    store = _open_store(args.db, err)
    if store is None:
        return 2
    with store:
        try:
            current = tracker.get_application(store, args.code)
            before = current.stage if current else None
            app = tracker.mark(store, args.code, kind, when, note=args.note)
        except TrackerError as exc:
            print(f"not recorded: {exc}", file=err)
            return 1
    day, stage = tracker.local_day(when), tracker.stage_label(app.stage)
    if kind == "reply":
        what = f"reply on {day}" + (f", back at {stage}" if before == "no_response" else f" (still {stage})")
    elif kind == "reopened":
        what = f"reopened on {day}, back at {stage}"
    elif kind in tracker.STAGE_EVENTS:
        what = f"moved to {stage} on {day}"
    else:
        what = f"{stage} on {day}"
    print(f"#{app.code} {app.company} — {app.title}: {what}", file=out)
    return 0


def cmd_note(args: argparse.Namespace, out: TextIO, err: TextIO, now: datetime) -> int:
    store = _open_store(args.db, err)
    if store is None:
        return 2
    with store:
        try:
            app = tracker.add_note(store, args.code, " ".join(args.text), now)
        except TrackerError as exc:
            print(f"not added: {exc}", file=err)
            return 1
    print(f"Noted on #{app.code} {app.company} — {app.title}", file=out)
    return 0


def cmd_apps(args: argparse.Namespace, out: TextIO, err: TextIO, now: datetime) -> int:
    store = _open_store(args.db, err)
    if store is None:
        return 2
    with store:
        if args.code is not None:
            app = tracker.get_application(store, args.code, with_events=True)
            if app is None:
                print(f"no application #{args.code}; `jobfeed apps --all` lists them", file=err)
                return 1
            source = SOURCE_NAMES.get(app.found_via, app.found_via)
            section = SECTION_TITLES.get(app.digest_section) if app.digest_section else None
            print("\n".join(tracker.detail_lines(app, source, section)), file=out)
            return 0
        shown = tracker.list_applications(store, include_closed=args.all)
        total = len(tracker.list_applications(store, include_closed=True))
    if not total:
        print("No applications yet. Log one with `jobfeed apply CODE` (the number before a posting in the digest).",
              file=out)
        return 0
    label = "Applications" if args.all else "Open applications"
    print(f"{label} ({len(shown)})", file=out)
    if shown:
        print(f"  {'code':<6} {'stage':<12} {'applied':<7} {'age':>4}  role", file=out)
        for app in shown:
            print(tracker.list_line(app, now), file=out)
    closed = total - len(shown)
    if closed and not args.all:
        print(f"  … and {closed} closed (add --all)", file=out)
    return 0


def cmd_mail(args: argparse.Namespace, out: TextIO, err: TextIO, now: datetime, imap_factory=None) -> int:
    config = _load_config(args.config, err)
    if config is None:
        return 2
    try:
        settings = load_settings(args.env)
    except (MailConfigError, OSError) as exc:
        print(f"email settings: {exc}", file=err)
        return 2
    try:
        store = Store.scratch_copy(args.db) if args.dry_run else Store.open(args.db)
    except StoreError as exc:
        return _store_error(exc, err)
    if args.reread and not args.dry_run:
        print("--reread only works with --dry-run, since acting on mail twice would record it twice", file=err)
        return 2
    since = now - timedelta(days=args.days) if args.days else None
    with store:
        try:
            with GmailReader(settings, imap_factory=imap_factory) as reader:
                result = mailbox.check(store, reader, settings, config.tracking, now, since=since,
                                       reread=args.reread)
        except ReadError as exc:
            print(f"Couldn't read Gmail: {exc}", file=err)
            return 1
    for reply in result.replies:
        sent = reply.sent_at.astimezone()
        print(f"Reply of {tracker.local_day(sent)}, {sent:%H:%M} (“{reply.subject}”)", file=out)
        if not reply.outcomes:
            print("  (no command lines)", file=out)
        for o in reply.outcomes:
            print(f"  {'ok ' if o.ok else 'NOT'}  {o.line}  →  {o.text}", file=out)
    for who in result.unverified:
        print(f"Ignored, as Gmail couldn't verify it came from you: {who}", file=out)
    for item in result.job_mail:
        r = item.result
        sent = item.at.astimezone()
        read_as = f"{r.reading.kind} (“{r.reading.why}”)" if r.reading.kind else "unclear"
        print(f"{tracker.local_day(sent)}, {sent:%H:%M}  {item.who}  “{item.subject}”", file=out)
        print(f"  read as: {read_as}", file=out)
        print(f"  {'done' if r.done else 'CHECK'}: {r.action}", file=out)
    if result.passed_over:
        print(f"Passed over {result.passed_over} other company email{'' if result.passed_over == 1 else 's'} "
              "(newsletters and the like).", file=out)
    if not result.replies and not result.unverified and not result.job_mail:
        print(f"Nothing new since {tracker.local_day(result.since)}.", file=out)
    print(result.summary() + (" Dry run: nothing was saved." if args.dry_run else ""), file=err)
    return 0


def cmd_edit(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    changes = {k: v for k, v in [("resume_version", args.resume), ("cover_letter", args.cover_letter),
                                 ("referral", args.referral), ("found_via", args.via)] if v is not None}
    if not changes:
        print("nothing to change; give --resume, --cover-letter / --no-cover-letter, --referral / --no-referral "
              "or --via", file=err)
        return 2
    store = _open_store(args.db, err)
    if store is None:
        return 2
    with store:
        try:
            app = tracker.edit(store, args.code, **changes)
        except TrackerError as exc:
            print(f"not changed: {exc}", file=err)
            return 1
    print(f"#{app.code} {app.company} — {app.title}: resume {app.resume_version or 'unrecorded'}, "
          f"{'cover letter' if app.cover_letter else 'no cover letter'}, "
          f"{'referral' if app.referral else 'no referral'}, found via {app.found_via}", file=out)
    return 0


def cmd_stats(args: argparse.Namespace, out: TextIO, err: TextIO, now: datetime) -> int:
    config = _load_config(args.config, err)
    if config is None:
        return 2
    since = None
    if args.since:
        try:
            since = tracker.parse_day(args.since, now)
        except TrackerError as exc:
            print(f"--since: {exc}", file=err)
            return 2
        since = datetime.combine(since.astimezone().date(), datetime.min.time()).astimezone()
    store = _open_store(args.db, err)
    if store is None:
        return 2
    with store:
        facts = stats.gather(store, now, config.tracking, since=since)
    cuts = None if not args.by else (stats.CUT_KEYS if "all" in args.by else args.by)
    print("\n".join(stats.report(facts, config.tracking, cuts=cuts, metric=args.metric, now=now)), file=out)
    return 0


def cmd_weekly(args: argparse.Namespace, out: TextIO, err: TextIO, now: datetime, mailer: Mailer | None) -> int:
    config = _load_config(args.config, err)
    if config is None:
        return 2
    settings = None
    if not args.no_email:
        try:
            settings = load_settings(args.env)
        except (MailConfigError, OSError) as exc:
            print(f"email settings: {exc}.\nUse --no-email to print the report instead.", file=err)
            return 2
        mailer = mailer or GmailMailer(settings)
    store = _open_store(args.db, err)
    if store is None:
        return 2
    with store:
        # The 7 days up to and including today; the regular report isn't affected.
        day = now.astimezone().date() + timedelta(days=1)
        ok = _send_weekly(store, day, config, now, None if args.no_email else mailer, settings, out, err,
                          record=False)
    return 0 if ok else 1


def cmd_backup(args: argparse.Namespace, out: TextIO, err: TextIO, now: datetime) -> int:
    config = _load_config(args.config, err)
    if config is None:
        return 2
    if not args.db.exists():
        print(f"{args.db} doesn't exist yet; nothing to back up", file=err)
        return 1
    store = _open_store(args.db, err)
    if store is None:
        return 2
    with store:
        note = _backup(store, args.db, now, config.tracking.backups_kept)
    print(note, file=out)
    return 1 if "failed" in note else 0


def cmd_fit(args: argparse.Namespace, transport: httpx.BaseTransport | None, out: TextIO, err: TextIO,
            now: datetime) -> int:
    config = _load_config(args.config, err)
    if config is None:
        return 2
    if args.fit_command == "check":
        key = llm.load_key(args.env)
        if key is None:
            print(f"no {llm.KEY_NAME} in {args.env} or the environment", file=err)
            return 2
        with make_client(transport) as client:
            try:
                resume = fit.load_resume(config.fit, args.db.parent, client)
                s = llm.score_one(client, key, llm.build_prompt(resume, config), scoring.CHECK_POSTING, config)
            except (fit.ResumeError, llm.LLMError) as exc:
                print(f"check failed: {exc}", file=err)
                return 1
        u = s.working["usage"]
        print(f"The LLM scorer works ({s.version}). A made-up posting, not one of yours:", file=out)
        print(f"  {scoring.CHECK_POSTING.title} at {scoring.CHECK_POSTING.company}: {s.score}", file=out)
        print(f"  {s.reason}", file=out)
        print(f"  tokens: {u['input_tokens']} in, {u['cache_creation_input_tokens']} cache written, "
              f"{u['cache_read_input_tokens']} cache read, {u['output_tokens']} out · ${s.working['cost_usd']:.5f}",
              file=out)
        return 0
    if args.fit_command == "profile":
        with make_client(transport) as client:
            try:
                resume = fit.load_resume(config.fit, args.db.parent, client)
            except fit.ResumeError as exc:
                print(f"no resume: {exc}", file=err)
                return 1
        scorer = fit.Scorer(config, resume, now)
        print(f"Resume: {resume.source} (hash {resume.hash})", file=out)
        print("\n".join(scorer.profile.describe()), file=out)
        print(f"Terms looked for: {len(scorer.terms)} (fit.terms in config.yaml)", file=out)
        return 0
    store = _open_store(args.db, err)
    if store is None:
        return 2
    with store:
        sent = fit.sent_postings(store)
        if args.fit_command == "backfill":
            with make_client(transport) as client:
                report = scoring.run(store, [], config, now, client, args.db.parent, args.env, everything=True)
            if report.problem:
                print(f"nothing scored: {report.problem}", file=err)
                return 1
            print(f"Scored {report.keywords} of {len(sent)} postings sent by keywords; the rest were already scored.",
                  file=out)
            if report.llm:
                b = report.llm
                print(f"Scored {b.scored} by the LLM (${b.cost:.3f})"
                      + (f"; {b.failed} failed" if b.failed else "")
                      + (f"; {b.left} left for later runs" if b.left else "")
                      + (f"; paused: {b.stopped}" if b.stopped else "") + ".", file=out)
            elif report.llm_off:
                print(f"LLM scoring off: {report.llm_off}.", file=out)
            print("Scores stay hidden.", file=out)
            return 0
        done = fit.counts(store)
        hashes = list(store.conn.execute(
            "SELECT resume_hash, COUNT(*) FROM fit_scores GROUP BY resume_hash ORDER BY MAX(scored_at) DESC"))
        missing = len(fit.unscored(store, [j.id for j in sent], fit.KEYWORDS))
        spent = llm.spent_this_month(store, now)
    print(f"Postings sent: {len(sent)}", file=out)
    for scorer in (fit.KEYWORDS, "llm"):
        print(f"  {scorer:<9} {done.get(scorer, 0)} scored", file=out)
    if missing:
        print(f"  {missing} sent postings have no keyword score yet; `jobfeed fit backfill` scores them", file=out)
    print(f"LLM spend this month: ${spent:.2f} of ${config.fit.llm_monthly_budget:.2f}", file=out)
    for h, n in hashes:
        print(f"Resume {h}: {n} scores", file=out)
    return 0


def cmd_undo(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    store = _open_store(args.db, err)
    if store is None:
        return 2
    with store:
        try:
            result = tracker.undo(store, args.code)
        except TrackerError as exc:
            print(f"nothing undone: {exc}", file=err)
            return 1
    print(result[0].upper() + result[1:], file=out)
    return 0


def main(
    argv: list[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    now: datetime | None = None,
    mailer: Mailer | None = None,
    imap_factory=None,
) -> int:
    """`mailer` and `imap_factory` replace Gmail in tests; settings are still read from --env."""
    out = out or sys.stdout
    err = err or sys.stderr
    now = now or utc_now()
    today = now.astimezone().date()
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return cmd_run(args, transport, out, err, today, now, mailer, imap_factory)
    if args.command == "replay":
        return cmd_replay(args, out, err, today, now)
    if args.command == "send-test":
        return cmd_send_test(args, err, mailer, now)
    if args.command == "apply":
        return cmd_apply(args, out, err, now)
    if args.command == "mark":
        return cmd_mark(args, out, err, now)
    if args.command == "note":
        return cmd_note(args, out, err, now)
    if args.command == "apps":
        return cmd_apps(args, out, err, now)
    if args.command == "undo":
        return cmd_undo(args, out, err)
    if args.command == "edit":
        return cmd_edit(args, out, err)
    if args.command == "stats":
        return cmd_stats(args, out, err, now)
    if args.command == "weekly":
        return cmd_weekly(args, out, err, now, mailer)
    if args.command == "backup":
        return cmd_backup(args, out, err, now)
    if args.command == "fit":
        return cmd_fit(args, transport, out, err, now)
    if args.command == "mail":
        return cmd_mail(args, out, err, now, imap_factory)
    return cmd_discover(args, transport, out, err)


if __name__ == "__main__":
    sys.exit(main())
