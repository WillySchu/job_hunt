# jobfeed

A daily email digest of new software engineering postings that are remote or in the Seattle area, built to run from cron on a Raspberry Pi. The full design is in [JOB_FEED_SPEC.md](JOB_FEED_SPEC.md).

## Status

| Milestone | State |
| --- | --- |
| 1. Skeleton and Greenhouse | **Done** — `Job` model, Greenhouse adapter, `run --dry-run` |
| 2. Normalizers | **Done** — location, salary and seniority parsing |
| 3. Filters | **Done** — rules from `config.yaml`, digest preview, `replay` |
| 4. Store and dedupe | **Built** — `jobs.db`, sent tracking, 30-day cross-source dedupe |
| 5. Lever, Ashby and discover | Not started |
| 6. Aggregators | Not started |
| 7. Email | Not started |
| 8. Deploy | Not started |

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

The board slug is the part after `/boards/` in a company's Greenhouse URL. A wrong slug shows up as `FAILED greenhouse:SLUG: board not found` and doesn't stop the other boards.

Without `--board`, `run` polls every company in `companies.yaml` that has `ats: greenhouse` and a `slug`. `jobfeed discover` will fill those in at milestone 5. Until then you can add a few by hand:

```yaml
  - { name: Stripe, bucket: seattle-hiring, origin: seed, ats: greenhouse, slug: stripe, watch: true }
```

Companies with `watch: true` go to the Watchlist section and are never dropped for a missing salary.

### Run for real

```sh
.venv/bin/jobfeed run --board greenhouse:stripe --board greenhouse:databricks
```

The same digest, plus three writes to `jobs.db`:

- every fetched posting is saved, with when it was first and last seen and whether it passed the rules;
- every posting shown in the digest is marked as sent;
- a posting is only ever sent once. A role sent in the last 30 days isn't sent again from another source or office: the same title at the same company counts as the same role, and when an aggregator and the company's own board both have it, the board's copy wins.

So a second run the same day prints "Nothing new matched." Postings past `digest.max_per_section` aren't marked, and lead the next run instead.

**Until milestone 7 the "send" is the printout.** A posting printed by `run` counts as sent, so don't add `run` to cron yet unless you read its output. To start over, delete `jobs.db`.

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

`jobfeed discover` exits with a note saying it arrives in milestone 5.

## Layout

```
jobfeed/
  cli.py            # argparse entry point; digest preview rendering
  config.py         # config.yaml loader and validation
  store.py          # jobs.db: postings, first/last seen, sent
  dedupe.py         # what a run sends: skip sent, one copy per role, 30-day window
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
    __init__.py     # source registry; fetches 4 boards at a time
tests/              # pytest, no network: httpx.MockTransport + fixtures
config.yaml         # filter rules
companies.yaml      # seed list and watchlist
JOB_FEED_SPEC.md    # design
```
