# jobfeed

A daily email digest of new software engineering postings that are remote or in the Seattle area, built to run every morning on a Raspberry Pi as a systemd timer, plus a tracker for the applications you send. The designs are in [JOB_FEED_SPEC.md](JOB_FEED_SPEC.md) and [APPLICATION_TRACKER_SPEC.md](APPLICATION_TRACKER_SPEC.md).

## Status

| Milestone | State |
| --- | --- |
| 1. Skeleton and Greenhouse | **Done** — `Job` model, Greenhouse adapter, `run --dry-run` |
| 2. Normalizers | **Done** — location, salary and seniority parsing |
| 3. Filters | **Done** — rules from `config.yaml`, digest preview, `replay` |
| 4. Store and dedupe | **Done** — `jobs.db`, sent tracking, 30-day cross-source dedupe |
| 5. Lever, Ashby and discover | **Done** — two more adapters; `discover` fills in `companies.yaml` |
| 6. Aggregators | **Done** — Remotive, Remote OK and Himalayas; harvesting company boards from their links |
| 7. Email | **Done** — HTML and text digest through Gmail, run-health footer, `send-test` |
| 8. Deploy | **Built** — systemd service and 6:00 timer, `deploy/install.sh`; then a week of tuning |

Application tracker:

| Milestone | State |
| --- | --- |
| 1. Applications and codes | **Done** — schema 3, digest codes, `apply`, `mark`, `note`, `apps`, `undo` |
| 2. Daily follow-ups | **Done** — "Your applications" in the digest, 10-day follow-ups, 21-day no-response rule |
| 3. Gmail reader and replies | **Done** — read-only IMAP; reply to the digest with commands; `jobfeed mail` |
| 4. Mail classifier | **Done** — confirmations, rejections, interview requests and offers read from Gmail; "Check these"; `jobfeed edit` |
| 5. Stats | **Done** — `jobfeed stats`: rates with likely ranges, split by resume, source and more |
| 6. Weekly email and backups | **Done** — Monday report email; weekly copies of `jobs.db` in `backups/` |
| 7. Two weeks of checking | Next: compare what the tracker recorded with your inbox |
| 8. LinkedIn confirmations | After that |

## Setup

Python 3.11 or newer.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

## Usage

### Preview the digest

```sh
.venv/bin/jobfeed run --dry-run \
  --board greenhouse:stripe \
  --board greenhouse:anthropic \
  --board greenhouse:databricks
```

This fetches the boards, applies the rules in `config.yaml` and prints the digest a real run would send. It reads `jobs.db` (if there is one) to leave out what's already been sent, but writes nothing.

```
Job feed · Fri Nov 6 · 2 new (0 watchlist)

== New matches (1)
  Senior Backend Engineer, Platform — Example Corp · Seattle, WA; Remote (US) · $170K–$215K · Senior
      remote (us) · via Greenhouse · posted 2026-10-28
      https://job-boards.greenhouse.io/examplecorp/jobs/4412983

== No salary listed (1)
  Staff Infrastructure Engineer — Example Corp · Bellevue, WA; New York, NY · no salary listed · Staff
      onsite · via Greenhouse · posted 2026-11-05
      https://job-boards.greenhouse.io/examplecorp/jobs/4412990
```

Sections come in the spec's order: Watchlist, New matches, No salary listed. Within each, Senior roles come first, then Staff, then the rest, newest first. The second line of each entry is what the normalizers read: work mode and remote region, eligible states if the posting names them, and tags such as "possibly full stack".

Options:

- `--all` — also list every dropped posting with the rule that dropped it
- `--limit N` — postings per section (default `digest.max_per_section`, 50; `0` shows all)
- `--json` — every fetched posting as one JSON `Job` per line, **unfiltered**; save it for `replay`
- `--config PATH`, `--companies PATH`, `--db PATH` — use other files

Counts, drop totals and board failures go to stderr, so redirecting stdout captures only the digest or the JSON.

`--board` takes `greenhouse:SLUG`, `lever:SLUG` or `ashby:SLUG`. The slug is the company's part of its board URL: `job-boards.greenhouse.io/SLUG`, `jobs.lever.co/SLUG`, `jobs.ashbyhq.com/SLUG`. A wrong slug shows up as `FAILED lever:SLUG: board not found` and doesn't stop the other boards.

Without `--board`, `run` polls every company in `companies.yaml` that has an `ats` and a `slug` (`discover` fills those in), plus the three remote-job aggregators below. `--no-aggregators` leaves those out.

### Aggregators

Remotive, Remote OK and Himalayas list remote jobs across many companies, so they need no company list. Settings are in the `aggregators` section of `config.yaml`:

- **Remotive**: one category (default `software-dev`), one call per run. Remotive allows at most 4 calls a day, and its listings arrive 24 hours late.
- **Remote OK**: the newest ~100 postings, optionally narrowed by tags. There's no paging, so it needs to run daily to catch everything.
- **Himalayas**: a few searches (default "software engineer", "backend engineer" and "platform engineer"), restricted to postings open to the US, 2 pages of 20 each.

They also work with `--board`: `remotive:software-dev`, `remoteok:all` (or `remoteok:backend,golang`), `himalayas:backend engineer`.

All three ask, as a condition of using their APIs, for a link back to the posting on their site and credit as the source. Each aggregator posting in the digest links to its page there and says `via Remotive`, `via Remote OK` or `via Himalayas`. Don't strip those if you change the email.

When an aggregator and a company's own board carry the same role, the company board's copy is the one sent (see Run for real).

**Applying.** Under an aggregator posting, the digest adds "Apply on the company's site" with a link to the company's own Greenhouse, Lever or Ashby posting whenever the aggregator gave one. Applying there is free and goes straight to the company. Remote OK charges to use its Apply button ($14.95 a month as of November 2026), so its postings with no company link get "find it on Company's own careers site" and a search link instead. The aggregator's page stays the main link, as their terms ask.

**Harvesting.** When an aggregator posting for an engineering role links to the company's own Greenhouse, Lever or Ashby board, `run` adds the company to `companies.yaml`. New entries go under a "harvested from aggregator postings" header with `origin: harvested`, and their boards are polled from the next run. `--dry-run` only reports what it would add. A board already on your list under any name is skipped, as is a company already pointing at a different board. A company you listed without a board (`ats: null` or `unsupported`) gets the one found. Set `aggregators.harvest: false` to turn this off.

### Find each company's board

```sh
.venv/bin/jobfeed discover               # every company in companies.yaml without an ats yet
.venv/bin/jobfeed discover Datadog Figma # look up these, adding any that are new
.venv/bin/jobfeed discover --retry       # also re-check ones marked unsupported
.venv/bin/jobfeed discover --dry-run     # report only; change nothing
```

For each name it tries a few slugs (`grafanalabs`, `grafana-labs`, `grafana`) on Greenhouse, then Lever, then Ashby, and takes the first board with open postings:

```
  Stripe             ✓  greenhouse:stripe · 312 postings
  Rune Technologies  ✓  lever:rune · 4 postings
                        check: matched the short name 'rune'; check it's Rune Technologies
  Zillow             -  not on Greenhouse, Lever or Ashby (tried zillow, zillowgroup, zillow-group)

66 checked · 41 found (30 greenhouse, 6 lever, 5 ashby) · 25 unsupported · 3 worth a look (see 'check:')
```

It then writes `ats` and `slug` into `companies.yaml`, editing each line in place so comments and layout survive, and saves the old file as `companies.yaml.bak`. Companies found nowhere get `ats: unsupported`. Network trouble leaves a company untouched, so the next `discover` tries it again.

Read the `check:` lines before trusting a match. They flag a match on a shortened name, a Greenhouse board whose name differs from yours, and a board with no open postings, which can be an old one or a different company's. To fix a wrong match, edit its line by hand. To force a re-check, set it back to `ats: null, slug: null`.

Unsupported companies mostly run their own career sites (often Workday). Those can't be polled yet.

Companies with `watch: true` go to the Watchlist section and are never dropped for a missing salary.

### Set up email

The digest goes out through your Gmail account, using an app password (Google Account → Security → 2-Step Verification → App passwords; 2-Step Verification has to be on).

```sh
cp .env.example .env
chmod 600 .env          # it holds a password; jobfeed warns if others can read it
# fill in GMAIL_USER, GMAIL_APP_PASSWORD (spaces are fine) and DIGEST_TO (comma-separate several)
.venv/bin/jobfeed send-test
```

`send-test` sends a two-line message to `DIGEST_TO` and reports what Gmail said if it fails. A rejected login usually means a normal password was used instead of an app password. Settings in the environment override `.env`, and `--env PATH` reads another file.

Each run also reads your replies to the digest from Gmail, with the same app password. That needs IMAP turned on once: Gmail → Settings → See all settings → Forwarding and POP/IMAP → Enable IMAP. `.venv/bin/jobfeed mail` checks it works. To skip reading, set `tracking.read_mail: false`.

### Run for real

```sh
.venv/bin/jobfeed run
```

This fetches every board, emails the digest, and records three things in `jobs.db`:

- every fetched posting, with when it was first and last seen and whether it passed the rules;
- which postings the email carried, marked as sent;
- each board's posting count, so the footer can say when one suddenly drops to zero.

A posting is only ever sent once. A role sent in the last 30 days isn't sent again from another source or office: the same title at the same company counts as the same role, and when an aggregator and the company's own board both have it, the board's copy wins.

The email goes out every run, even when nothing is new, so a missing email means something broke. Its subject follows the spec (`Job feed · Fri Nov 6 · 14 new (2 watchlist)`). It has an HTML version and a plain-text one, and the footer reports run health: postings per source, what was skipped and why, failed boards, boards that went empty since the last run, and credit to the aggregators. Postings past `digest.max_per_section` aren't marked, and lead the next email.

If Gmail can't be reached, `run` retries twice, then exits with status 1 and marks nothing as sent, so the next run carries those postings. The Gmail settings are checked before anything is fetched, so a typo in `.env` fails in seconds.

- `--no-email` prints the digest instead; its postings still count as sent.
- `--html FILE` also writes the email's HTML, to open in a browser. It works with `--dry-run` too.
- `--dry-run` never sends and never writes. It needs no `.env`.

To start the feed over, delete `jobs.db`, but once you're tracking applications it holds their history too, so move it aside rather than deleting it.

The summary on stderr counts what was skipped and why:

```
2 new · 3 duplicates skipped · 4 already sent · 12 dropped (role 9, location 3) (add --all to see why)
```

### Tune the rules

All rules live in `config.yaml`, with comments on each setting. A bad key, type or pattern stops the run with a message naming the setting.

To see what a change does without refetching, replay what's already in `jobs.db`:

```sh
# edit config.yaml
.venv/bin/jobfeed replay --all            # every stored posting
.venv/bin/jobfeed replay --all --days 7   # only postings seen on a board in the last week
```

`replay` applies the current `config.yaml` and `companies.yaml` to stored postings and prints the digest they'd make, headed `Job feed replay`, including ones already sent, tagged `sent 2026-11-06`. It never writes.

Before there's a `jobs.db`, or to test against a fixed set, save a dump and replay that:

```sh
.venv/bin/jobfeed run --json --board greenhouse:stripe --board greenhouse:databricks > postings.jsonl
.venv/bin/jobfeed replay --from postings.jsonl --all
```

`run --json` prints every fetched posting, unfiltered, and never writes.

To hunt for location strings the normalizers misread, list each distinct one next to its reading:

```sh
jq -r '[.location_raw, .work_mode, (.remote_region // "-"), (.cities | join("; "))] | @tsv' postings.jsonl | sort -u
```

A wrong reading is worth a new case in `tests/test_normalize_location.py` or `tests/test_normalize_salary.py`.

### Track applications

Every posting in the digest has a code, like `#412`, given when it's first emailed and kept for good. When you apply to one, log it on the Pi (from the repo directory, or pass `--db`):

```sh
.venv/bin/jobfeed apply 412                        # one posting
.venv/bin/jobfeed apply 412 418 --date 2026-11-05  # several, applied on an earlier day
.venv/bin/jobfeed apply 430 --resume v2 --cover-letter --referral --note "Sam referred me"
```

Each application records the resume version and whether you sent a cover letter. The defaults are `tracking.resume_version` and `tracking.cover_letter` in `config.yaml`; change `resume_version` when you start sending a new resume, and pass `--resume` to try a version on one application.

For a role you found somewhere else, log it by its link. If jobfeed has the posting stored (the query string doesn't matter), the posting's details are copied in; otherwise give the company and title, and it gets the next free code:

```sh
.venv/bin/jobfeed apply --url https://www.linkedin.com/jobs/view/123 --company Acme --title "Senior Backend Engineer" --via referral
```

`--via` is `referral`, `linkedin`, `recruiter` or `other` (the default, or `referral` with `--referral`).

Then record what happens:

```sh
.venv/bin/jobfeed mark 412 reply         # a first response; the stage stays the same
.venv/bin/jobfeed mark 412 screen        # recruiter or phone screen
.venv/bin/jobfeed mark 412 interview     # onsite and technical count as interview
.venv/bin/jobfeed mark 412 offer
.venv/bin/jobfeed mark 412 rejected --date 2026-11-12 --note "wanted more Go"
.venv/bin/jobfeed note 412 recruiter call Tue 2pm
```

The other stages are `accepted`, `declined`, `withdrawn` (or `withdrew`), `no-response` (or `ghosted`; only from applied) and `reopen`, which returns a closed application to the stage it had reached. A reply to one marked as no response reopens it too.

```sh
.venv/bin/jobfeed apps          # open applications, newest first
.venv/bin/jobfeed apps --all    # closed ones too
.venv/bin/jobfeed apps 412      # one application and its history
.venv/bin/jobfeed undo 412      # remove the last entry made for it, or the application if that's all there is
```

Every change is kept as a dated entry, and the stage is worked out from those entries, so `undo` and a late `--date` always leave it right. `apply` exits with status 1 if any code couldn't be logged (the others are still logged) and 2 for a usage mistake.

#### In the digest

Each morning's digest opens with a "Your applications" block, left out when there's nothing to say:

```
== Your applications
  Logged since the last digest
    #412 Stripe — Senior Backend Engineer · applied Nov 6 · resume v1
  Updates
    #398 Acme — Platform Engineer · screen Nov 6
    #377 Orbit — SRE · no response: no reply in 21 days, closed Nov 5
  Follow up (nothing new in 10+ days)
    #380 Motive — Backend Engineer · applied Oct 27 · quiet 10 days
    #371 Pixel — Staff Engineer · interview since Oct 24 · quiet 13 days
```

- **Logged and Updates** list everything entered since the last digest went out (notes aren't listed). If the email fails, nothing counts as reported, so the next digest carries it.
- **Follow up** lists an open application, at any stage, once nothing has been entered for it in `tracking.follow_up_days` (10). Each quiet stretch is listed once, and the reminder shows in `jobfeed apps CODE`. Any new entry for it, a note included, starts a new stretch. An application with news in the same digest waits a day.
- **No response.** An application still at applied, with no reply `tracking.no_response_days` (21) after you applied, is closed as no response, dated the day the limit passed. Only applied closes this way; interview loops often pause for weeks, so later stages just get follow-ups. If you're still hopeful, `jobfeed mark CODE reopen` puts it back and restarts the 21 days; if they did answer, `jobfeed mark CODE reply` records it and reopens it. (`undo` won't remove the rule's closure, since the rule would only close it again.)

`run --dry-run` shows the block as the run would send it, including what the rule would close, without saving anything. It doesn't read Gmail.

#### Replying to the digest

Instead of logging on the Pi, reply to the morning email with one command per line. The next run reads it, carries it out, and confirms it in that digest:

```
applied 412 418
applied 430 resume v2 referral cover letter
applied 431 yesterday note found it through Sam
screen 412
interview 412 onsite with the platform team
rejected 418 on 2026-11-05
withdrew 430
note 412 recruiter call Tue 2pm
```

- The commands are the CLI's: `applied` with `resume V`, `referral`, `cover letter` / `no cover letter`, a date and `note …`; any stage `jobfeed mark` takes, with an optional date (words after the codes become a note); and `note`.
- Each takes one or more codes. Without a date, an entry is dated when you sent the reply; a date can be `2026-11-05`, `on 2026-11-05`, `today` or `yesterday`.
- The quoted digest under your reply, signatures and lines like "Thanks!" are ignored. A line that can't be read or carried out is quoted back under "Not done from your replies", with the reason.
- Only replies from `GMAIL_USER` or a `DIGEST_TO` address count, and only if Gmail sent them from your account or verified the sender (DMARC), so a forged "From: you" does nothing. Each message is read once.

#### Job mail

Each run also reads mail from companies about your applications, and records what it says:

| It reads as | Typical wording | What it does |
| --- | --- | --- |
| Confirmation | "Thank you for applying", "we've received your application" | Logs the application if you haven't (from the posting it names); otherwise notes it. An earlier confirmation corrects the applied date |
| Rejection | "not moving forward", "decided to move forward with other candidates" | Rejected, recording the stage it had reached |
| Interview request | "your availability", "I'd love to set up a call", a scheduling link | Screen; interview if already past the screen; a reply if already interviewing |
| Offer | "pleased to extend an offer", "offer letter" | Offer |
| Reply | A person at the company, none of the above | A reply; the stage stays |

- **Which mail.** Gmail is asked for mail from the applicant-tracking systems in `tracking.ats_senders` (Greenhouse, Lever and Ashby to start) and from the companies you have open applications with, by name. Newsletters and other automated company mail with none of that wording are passed over.
- **Which application.** A link to the posting itself decides first; then the sender's own domain (jobs@duckduckgo.com is DuckDuckGo) or the company's name in the email, with the role title settling between several roles at one company. Mail about a role you haven't logged is never put on one you have.
- **Check these.** Mail it can't place, or whose wording it can't read, is listed under "Check these" in the next digest with its sender, subject and the reason. Reply with a command (`screen 412`) or leave it.
- An application logged from its confirmation email gets the default resume version; `jobfeed edit 412 --resume v2` changes it. `edit` also sets `--cover-letter` / `--no-cover-letter`, `--referral` / `--no-referral`, and `--via` for roles found elsewhere.

#### Checking what it reads

jobfeed opens All Mail read-only and never marks, moves or deletes anything. It keeps each message's Gmail ID, so it isn't read twice, and what it read the message as; nothing else from it.

```sh
.venv/bin/jobfeed mail                      # read new replies and job mail now, as the next run would
.venv/bin/jobfeed mail --dry-run            # show how each new message reads and what it would do; save nothing
.venv/bin/jobfeed mail --days 30            # look further back than the last check (mail already read is skipped)
.venv/bin/jobfeed mail --dry-run --reread --days 14   # read the last two weeks again with the current rules
```

The dry run prints each job email with what it was read as and the phrase that decided it, which is the way to check a company's mail is understood. If a company's confirmations come from a system that isn't listed, add its sending domain to `tracking.ats_senders`.

If Gmail can't be read, the digest still goes out, and its footer says why; the next run catches up.

#### Stats

```sh
.venv/bin/jobfeed stats                         # overall rates, then every split with at least two groups
.venv/bin/jobfeed stats --by resume             # just one split (repeatable; --by all for every one)
.venv/bin/jobfeed stats --metric screen         # what each split's verdict compares (replied, screen, interview, offer)
.venv/bin/jobfeed stats --since 2026-11-01      # only applications from that day on
```

```
Applications: 52 · 44 open · 30 counted in rates (at least 21 days old, or closed)
  Replied               16 of 30 (53%, likely 39–67%)
  Screen or further     10 of 30 (33%, likely 21–48%)
  Interview or further  3 of 30 (10%, likely 4–23%)
  Offer or further      0 of 30 (0%, likely 0–8%)
  Days to first reply   median 5.5 (from 18 replies)

Resume version
      apps  counted  Replied               Screen                Interview
  v1    26       14  3/14 21% (9–43%)      3/14 21% (9–43%)      1/14 7% (2–27%)
  v2    26       16  13/16 81% (61–92%)    7/16 44% (26–64%)     2/16 13% (4–32%)
  → too few to tell: v1 has 14 counted; 15 each are needed
```

- **Replied** counts any answer from the company, rejections included; a confirmation email or the no-reply rule doesn't count. **Screen or further** and the rest count applications that reached that stage, including ones rejected after it.
- **Counted** applications are at least `tracking.no_response_days` (21) old, or closed, so new applications that haven't had time to hear back don't drag the rates down.
- Each rate has a likely range (a 90% interval). At these numbers a bare percentage misleads; the range is what to read.
- Each split compares its highest and lowest group, and says **too few to tell** when either has fewer than 15 counted, or when either rate falls inside the other's range. Only otherwise does it name a winner. The example above is the point: 21% against 81% looks decisive, but 14 applications can't say so yet.
- The splits: found via, digest section, referral, resume version, cover letter, form filled automatically, fit score, days from posting to applying, seniority, work mode, and week applied. Fit score and form filling read "unscored" and "unknown" until those tools exist. Until something is old enough to count, the splits are left out (`--by` still shows one).
- After the rates come the **funnel**, every application so far stage by stage for all time and the last 4 weeks, and the open applications that have gone **longest without news**.

#### The weekly report

The run on Monday (`tracking.weekly_day`) also emails the report above as a separate message, after the digest: "Job search weekly · Nov 2–8 · 5 applied (34 so far)". It opens with last week's applications and the news from companies (replies, screens, rejections and so on), then the rates, funnel, splits and quiet applications.

- The first one goes out on the first Monday after you install this. If a Monday run is missed or the report can't be sent, the next run sends it.
- A report that can't be sent never fails the run: the digest has already gone, and a failed run is retried, which would send the digest again. It's never sent when the digest itself couldn't be.
- `jobfeed weekly` sends one now, covering the last 7 days (`--no-email` prints it); Monday's still goes out. `tracking.weekly_report: false` turns it off.

#### Backups

`jobs.db` now holds application history that can't be fetched again. Once a week, on the same run, jobfeed copies it to `backups/jobs-YYYY-MM-DD.db` beside it, keeping the newest 8 (`tracking.backups_kept`; 0 turns this off). The first copy is made by the first run after you install this, and the digest's footer says when one was made. The copy is taken with SQLite's own backup, so it's consistent even if something is writing at the time. `jobfeed backup` makes one now.

The weekly report email also carries a gzipped copy of `jobs.db` (`jobs-YYYY-MM-DD.db.gz`), so every week a copy lands in Gmail, off the Pi; `gunzip` it to get `jobs.db` back. Gmail takes 25 MB a message, so a compressed copy over 18 MB isn't attached, and the report says so at the top in a highlighted box. From 75% of that, the report warns that the day is coming. Every fetched posting is kept, so the file grows with time: roughly, 10,000 stored postings come close to the limit. `tracking.attach_backup: false` turns attaching off.

A copy on the same SD card doesn't survive the card failing, so if attaching ever stops, copy `backups/` off the Pi another way, for example from your laptop:

```sh
scp 'pi@raspberrypi.local:job_hunt/backups/*.db' ~/jobfeed-backups/
```

To restore, stop the timer (`sudo systemctl stop jobfeed.timer`), copy a backup over `jobs.db`, and start it again.

## Deploy on the Pi

jobfeed runs as a systemd service, started every morning at 6:00 Seattle time by a timer. It needs Python 3.11 or newer, which Raspberry Pi OS Bookworm has.

```sh
git clone https://github.com/WillySchu/job_hunt.git ~/job_hunt && cd ~/job_hunt
cp .env.example .env && chmod 600 .env    # fill in the three Gmail values
deploy/install.sh
```

Run `install.sh` from your normal account, not with sudo; the service runs as that user, and the script asks for sudo only to install the units. It:

1. builds `.venv` and installs jobfeed into it;
2. checks `.env` and `config.yaml` without sending anything, and makes `.env` private if it isn't;
3. installs `jobfeed.service` and `jobfeed.timer` into `/etc/systemd/system`, filling in your user and the repo path, and has systemd check them;
4. enables the timer and shows the next run.

Nothing is sent until 6:00. To see a run straight away:

```sh
sudo systemctl start jobfeed            # waits until the run finishes
journalctl -u jobfeed -n 50 --no-pager  # what it printed: counts, failures, the send
```

`deploy/install.sh --print` shows the unit files without installing them. `deploy/install.sh --uninstall` removes them and keeps the repo, `.env` and `jobs.db`.

How the timer and service behave:

- **6:00 Pacific, whatever the Pi's clock zone.** The timer names the zone, so daylight saving is handled, and the digest's dates are Seattle dates.
- **Missed mornings catch up.** If the Pi is off at 6:00, the run starts when it next boots.
- **Failures retry.** If every board fails or Gmail can't be reached, often the network right after boot, the service tries again 10 minutes later, up to 4 starts in an hour. A settings error (exit 2) isn't retried. A failed send marks nothing as sent, so nothing is lost.
- **Locked down.** The service runs at low priority, with the system and your home directory read-only except for the repo, where `jobs.db` and `companies.yaml` are written.

**Updating.** After a `git pull`, run `deploy/install.sh` again. Harvesting edits `companies.yaml` on the Pi, so commit it there now and then (or `git stash` before pulling) to keep pulls clean.

**If a morning email doesn't come:**

- `journalctl -u jobfeed -n 100` shows what happened. Add `sudo` if it shows nothing.
- `systemctl status jobfeed` shows the last exit status. Exit 2 means a settings problem; run `.venv/bin/jobfeed send-test` and `.venv/bin/jobfeed run --dry-run` by hand to see it.
- "Start request repeated too quickly" means the retry limit was reached. `sudo systemctl reset-failed jobfeed` clears it.

## Layout

```
jobfeed/
  cli.py            # argparse entry point; digest preview rendering
  config.py         # config.yaml loader and validation
  store.py          # jobs.db: postings, first/last seen, sent, digest codes
  tracker.py        # applications and their events: apply, mark, note, list, undo
  followups.py      # the digest's "Your applications" block; follow-up and no-response rules
  replies.py        # commands in replies to the digest: reading, parsing, carrying out
  mailbox.py        # checking Gmail for replies: search, verify, run, remember
  imap.py           # read-only Gmail IMAP client (X-GM-RAW search, BODY.PEEK)
  classify.py       # what a job email means: confirmation, rejection, interview, offer, reply
  jobmail.py        # tying job mail to an application and recording it
  stats.py          # rates, likely ranges, splits, funnel and verdicts for `jobfeed stats`
  weekly.py         # the Monday report email and the weekly backups of jobs.db
  dedupe.py         # what a run sends: skip sent, one copy per role, 30-day window
  digest.py         # subject, text and HTML bodies, run-health footer
  mailer.py         # .env settings; Gmail SMTP with STARTTLS and retries
  discover.py       # find each company's ATS; edit companies.yaml in place
  harvest.py        # company boards found in aggregator postings
  filters.py        # role, location and salary rules; digest sections and sorting
  models.py         # the Job record
  http.py           # client: 20 s timeout, 2 retries with backoff, named User-Agent
  text.py           # HTML to plain text
  companies.py      # companies.yaml loader
  normalize/
    __init__.py     # normalize_job: fills blank fields, never overwrites adapter data
    location.py     # work mode, remote region, cities, state eligibility
    salary.py       # pay ranges, annualized; currency kept
    seniority.py    # title -> junior | mid | senior | staff | principal
    geo.py          # state, region and city-alias tables
  sources/
    base.py         # Board, FetchResult, Source protocol
    greenhouse.py   # Greenhouse Job Board API
    lever.py        # Lever Postings API (US and EU hosts)
    ashby.py        # Ashby Job Posting API, with compensation
    aggregators.py  # Remotive, Remote OK, Himalayas
    common.py       # shared parsing helpers
    __init__.py     # source registry; fetches 4 boards at a time
deploy/
  jobfeed.service   # systemd unit template (install.sh fills in user and path)
  jobfeed.timer     # 6:00 Pacific daily, catches up after downtime
  install.sh        # install, update or --uninstall
tests/              # pytest, no network: httpx.MockTransport + fixtures
config.yaml         # filter rules
companies.yaml      # seed list and watchlist
JOB_FEED_SPEC.md    # job feed design
APPLICATION_TRACKER_SPEC.md   # application tracker design
```
