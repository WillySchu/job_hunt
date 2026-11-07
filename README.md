# jobfeed

A daily email digest of new software engineering postings that are remote or in the Seattle area, built to run every morning on a Raspberry Pi as a systemd timer. The full design is in [JOB_FEED_SPEC.md](JOB_FEED_SPEC.md).

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

To start over, delete `jobs.db`.

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
  store.py          # jobs.db: postings, first/last seen, sent
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
JOB_FEED_SPEC.md    # design
```
