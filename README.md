# jobfeed

A daily email digest of new software engineering postings that are remote or in the Seattle area, built to run from cron on a Raspberry Pi. The full design is in [JOB_FEED_SPEC.md](JOB_FEED_SPEC.md).

## Status

| Milestone | State |
| --- | --- |
| 1. Skeleton and Greenhouse | **Done** — `Job` model, Greenhouse adapter, `run --dry-run` |
| 2. Normalizers | Not started |
| 3. Filters | Not started |
| 4. Store and dedupe | Not started |
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

Print the open postings on a few Greenhouse boards. Nothing is written anywhere.

```sh
.venv/bin/jobfeed run --dry-run \
  --board greenhouse:stripe \
  --board greenhouse:anthropic \
  --board greenhouse:databricks
```

The board slug is the part after `/boards/` in a company's Greenhouse URL. A wrong slug shows up in the summary as `FAILED greenhouse:SLUG: board not found` and doesn't stop the other boards.

Options:

- `--limit N` — postings shown per board (default 25; `0` shows all)
- `--json` — one `Job` per line as JSON, all postings unless `--limit` is given
- `--companies PATH` — a different `companies.yaml`

Without `--board`, `run --dry-run` polls every company in `companies.yaml` that has `ats: greenhouse` and a `slug`. The seed list has none filled in yet; `jobfeed discover` will do that in milestone 5. Until then you can fill in a few by hand:

```yaml
  - { name: Stripe, bucket: seattle-hiring, origin: seed, ats: greenhouse, slug: stripe, watch: false }
```

`jobfeed run` without `--dry-run`, `discover` and `replay` exit with a note saying which milestone builds them.

The summary line and any failures go to stderr, so `--json > jobs.jsonl` captures only postings.

## Layout

```
jobfeed/
  cli.py            # argparse entry point
  models.py         # the Job record
  http.py           # client: 20 s timeout, 2 retries with backoff, named User-Agent
  text.py           # HTML to plain text
  companies.py      # companies.yaml loader
  sources/
    base.py         # Board, FetchResult, Source protocol
    greenhouse.py   # Greenhouse Job Board API
    __init__.py     # source registry; fetches 4 boards at a time
tests/              # pytest, no network: httpx.MockTransport + fixtures
companies.yaml      # seed list and watchlist
JOB_FEED_SPEC.md    # design
```
