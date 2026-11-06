# Job Feed Spec

Nov 6, 2026 · @user

## Goal and scope

A Python job on the Raspberry Pi emails one digest each morning with new software engineering postings that are remote or in the Seattle area, are not frontend or full stack, and pay at least $150K or list no salary.

**In scope for v1**

- Discovery sources, plus an optional watchlist of companies
- Normalizing every posting into one Job record
- Location, keyword and salary filters; seniority used for sorting only
- Dedupe, so each posting is emailed once
- Daily email through Gmail SMTP

**Out of scope for v1**

- Fit scoring and ranking with an LLM (item 2)
- LinkedIn and Indeed, whose terms prohibit scraping
- Workday and big-tech career sites, which have no stable public API
- Auto-apply

## Data sources

v1 reads six free, keyless JSON sources. Three are remote-job aggregators that need no company list, and three are ATS job boards polled from a seed list of companies.

| Source | Kind | Endpoint | Notes and terms |
| --- | --- | --- | --- |
| [Greenhouse](https://theirstack.com/en/blog/13-ats-platforms-with-public-job-posting-api) | ATS board | `boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` | Whole board per call; no server-side filtering |
| [Lever](https://theirstack.com/en/blog/13-ats-platforms-with-public-job-posting-api) | ATS board | `api.lever.co/v0/postings/{slug}` | Whole board per call; no server-side filtering |
| [Ashby](https://jobspipe.dev/guides/ashby-api) | ATS board | `api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true` | Salary bands when the employer publishes them; no company name in the response |
| [Remotive](https://github.com/remotive-com/remote-jobs-api) | Aggregator | `remotive.com/api/remote-jobs?category=software-dev` | Listings delayed 24 hours; at most 4 calls a day advised, over 2 a minute blocked; link back and credit Remotive |
| [RemoteOK](https://apify.com/jungle_synthesizer/remoteok-jobs-scraper) | Aggregator | `remoteok.com/api` | About 100 newest jobs per call, no paging, so it must run daily; link back required |
| [Himalayas](https://apify.com/himalayas/remote-jobs) | Aggregator | `himalayas.app/jobs/api/search` | Filters by keyword, country, seniority and timezone; 20 jobs per page; 429s on heavy use; credit Himalayas |

The HN "Who's Hiring" thread (monthly, via the [HN Algolia API](https://hn.algolia.com/api)) is a stretch goal for v1. Its posts are free text, so it needs an LLM or a loose parser to extract fields.

**Seed list.** The ATS APIs do not list their customers, so the tool keeps a `companies.yaml` of company slugs. A one-time `discover` command takes plain company names and tries each ATS endpoint to find which one answers. A few hundred boards a day is light work for the Pi at a polite concurrency of 4 requests.

**Watchlist.** Any company in `companies.yaml` can carry `watch: true`. Its matches are listed first in the digest and are never dropped for a missing salary.

Later adapters with the same public pattern: Workable, SmartRecruiters and Recruitee.

## Seed list

The seed list needs breadth more than taste: the filters decide what you see, so the list only has to cover companies that might post a match. v1 starts with about 70 names in three buckets, plus a harvest step that keeps growing the list on its own.

**How the list grows**

1. **Harvest from aggregators (automatic).** When a Remotive, RemoteOK or Himalayas posting links to a Greenhouse, Lever or Ashby board, the tool adds that company's slug to `companies.yaml` with `origin: harvested`. Remote-friendly companies pile up without any picking.
2. **Seattle-area employers (hand-seeded).** Aggregators only carry remote roles, so this bucket is the only way hybrid and onsite Seattle jobs reach the digest.
3. **Remote-friendly infrastructure companies (hand-seeded).** Backend-heavy companies worth covering from day one.

Run `discover` on the starter names below. It records the ATS for each one it finds, and lists the rest as "unsupported ATS" so you know which to check by hand.

**Starter names**

| Bucket | Companies | Basis |
| --- | --- | --- |
| Seattle area: hiring now | Stripe, Databricks, Snowflake, Anduril, Anthropic, Airwallex, Docker, Rippling, Samsara, LiveRamp, Fluidstack, Lambda, Crusoe, CoreWeave, Docusign, Okta, Atlassian, Zoom, CrowdStrike, Writer, SentiLink, Axon, Qualtrics, PitchBook, Superhuman | [Seattle-metro postings ranking, Nov 2026](https://jobspipe.dev/hiring/tech/seattle) |
| Seattle area: AI startups | Gradial, XBOW, Dropzone AI, Auger, Yoodli, Oumi, Howie, Avante, CopilotKit, Overland AI, Shield AI, Union.ai, Rune Technologies | [Seattle AI startups by headcount growth](https://www.landbase.com/blog/seattle-ai-startups-hiring-fastest); [Seattle AI startup job listings](https://www.fastaijobs.com/jobs/role/software-engineer/seattle) |
| Seattle area: local HQs and offices | Zillow, Expedia Group, Remitly, Rover, Outreach, Amperity, Smartsheet, Zuora, Pushpay, Carta, Niantic, ServiceNow, UiPath, Chewy, Snap, FusionAuth, OpenAI, xAI | [Seattle engineering roles roundup](https://jobrise.io/en/blog/software-engineer-jobs-seattle-2026/); [Bellevue tech companies](https://builtin.com/companies/location/na/usa/wa/bellevue); [OpenAI and xAI Bellevue offices](https://karat.com/seattle-top-city-to-hire-software-engineers/) |
| Remote-friendly infrastructure | GitLab, Grafana Labs, Tailscale, PlanetScale, Supabase, Sourcegraph, Temporal, Zapier, Fly.io, Render | From memory; `discover` and a week of digests will confirm |
| Check by hand | Amazon and AWS, Microsoft, Google, Meta, Apple, Salesforce, Oracle, NVIDIA, T-Mobile | Large Seattle-area hiring on their own career sites |

A name on this list says only that the company hires engineers in the area or remotely; whether it has a backend role at your level is the filters' job.

## Data model

Every adapter returns the same `Job` record, so filters, dedupe and the email never know which source a posting came from.

```python
@dataclass
class Job:
    id: str                  # "{source}:{native_id}", e.g. "greenhouse:4412983"
    source: str              # greenhouse | lever | ashby | remotive | remoteok | himalayas
    company: str
    title: str
    url: str                 # the source's own page (keeps attribution terms)
    location_raw: str        # exactly as posted
    work_mode: str           # remote | hybrid | onsite | unknown
    remote_region: str|None  # us | americas | global | other
    cities: list[str]        # normalized, e.g. ["Seattle", "Bellevue"]
    salary_min: int|None     # annual USD
    salary_max: int|None
    salary_raw: str|None
    seniority: str|None      # junior | mid | senior | staff | principal
    posted_at: datetime|None
    description: str         # plain text, HTML stripped
```

State lives in one SQLite file, `jobs.db`. It stores every posting fetched, not only matches, so a changed filter can be replayed against past postings without refetching.

```sql
CREATE TABLE jobs (
  id          TEXT PRIMARY KEY,
  dedupe_key  TEXT NOT NULL,   -- normalized company + title
  first_seen  TEXT NOT NULL,
  last_seen   TEXT NOT NULL,
  matched     INTEGER NOT NULL, -- 1 if it passed the filters
  emailed_at  TEXT,             -- NULL until it appears in a digest
  data        TEXT NOT NULL     -- the Job as JSON
);
CREATE INDEX jobs_dedupe ON jobs(dedupe_key);
```

The same role often appears on an aggregator and on the company's own board. A posting is skipped when another row with the same `dedupe_key` was emailed in the last 30 days, and the ATS copy wins when both arrive together.

## Filters

A posting reaches the digest only if it passes role, location and salary in that order; seniority never removes anything, it only sorts.

**1. Role**

- The title must match an include pattern: engineer, developer, SWE, SRE, backend, platform, infrastructure, data or ML engineer.
- The title is dropped if it matches an exclude pattern: front end, frontend, full stack, fullstack, UI engineer, web developer, plus manager, director and intern.
- Generic titles such as "Software Engineer" pass, but the description is checked for frontend-heavy terms (React, Vue, Angular, CSS, Next.js). If they outnumber backend terms, the posting gets a "possibly full stack" tag instead of being dropped.

**2. Location**

- Remote passes when its region is US, Americas, global or unstated.
- Remote limited to other countries is dropped, such as "Remote – UK" or "Remote, EMEA".
- Some US-remote roles list eligible states. If a list is present and Washington is not on it, the posting is dropped.
- Hybrid and onsite pass only when a listed city is in the Seattle-area set: Seattle, Bellevue, Redmond, Kirkland, Bothell, Issaquah, Renton and Lynnwood.
- An unclear work mode passes if the location text names "remote" or a Seattle-area city; otherwise it is dropped.

**3. Salary**

- Salaries are parsed from structured fields first, then from description text, including formats like $150,000, $150K and 150k–190k.
- Hourly rates are annualized at 2,080 hours. Non-USD figures are kept but not compared.
- A posting passes when the top of its range is at least $150K, so a $130K–$170K range passes.
- A posting with no salary goes to the "No salary listed" section. Washington requires pay ranges in postings, so this section should be mostly remote roles.

**4. Seniority (sort only)**

Seniority is parsed from title words: Senior, Sr, Staff, Principal and level numerals like II or III. Senior roles sort first within each section, then Staff, then the rest.

All rules live in `config.yaml`, so you can tune them without touching code:

```yaml
role:
  include: [engineer, developer, swe, sre, backend, platform, infrastructure]
  exclude: [front ?end, full ?stack, ui engineer, web developer, manager, director, intern]
  frontend_terms: [react, vue, angular, css, next.js, tailwind]
location:
  remote_regions: [us, americas, global, unknown]
  home_state: WA
  metro: [Seattle, Bellevue, Redmond, Kirkland, Bothell, Issaquah, Renton, Lynnwood]
salary:
  floor_usd: 150000
  compare: max          # max | min
  keep_missing: true    # route to "No salary listed"
seniority:
  prefer: [senior, staff]
```

## Digest email

One email a day, sent even when nothing is new, so a missing email means the job broke.

- **Subject:** `Job feed · Fri Nov 6 · 14 new (2 watchlist)`
- **Format:** HTML with a plain-text fallback, so it reads well on a phone.

The body has three sections in this order, each sorted by seniority and then newest first:

1. **Watchlist** — matches from companies marked `watch: true`, with or without a salary
2. **New matches** — salary listed and at or above the floor
3. **No salary listed**

Each posting is one entry:

> **Senior Backend Engineer** — Acme · Remote (US) · $165K–$210K · Senior Tags: possibly full stack · via Greenhouse · View posting

The footer reports run health: postings fetched per source, matches, and any source that errored or returned zero. A board that suddenly returns zero usually means the company changed ATS.

If a day has more than 50 matches, the email shows the top 50 and states how many were left out.

## Deployment on the Raspberry Pi

The tool is a plain Python 3.11+ package in a virtualenv, run once a day by your existing cron, with three small dependencies: `httpx`, `pyyaml` and `beautifulsoup4`.

```
~/jobfeed/
  jobfeed/
    sources/          # one adapter per source: greenhouse.py, lever.py, ...
    normalize.py      # location, salary and seniority parsing
    filters.py
    store.py          # SQLite
    digest.py         # HTML + text email
    cli.py
  config.yaml         # filter rules
  companies.yaml      # seed list and watchlist
  .env                # secrets, chmod 600, never committed
  jobs.db
  logs/
```

**Secrets.** `.env` holds `GMAIL_USER`, `GMAIL_APP_PASSWORD` and `DIGEST_TO`. Mail goes through `smtp.gmail.com` on port 587 with STARTTLS.

**Commands**

- `jobfeed run` — fetch, filter, store and email
- `jobfeed run --dry-run` — print the digest and write nothing
- `jobfeed discover "Stripe" "Datadog"` — find each company's ATS and add it to `companies.yaml`
- `jobfeed replay` — re-run the current filters over stored postings, to check a rule change

**Cron.** Set the Pi's timezone to America/Los\_Angeles, then add:

```
17 7 * * * cd ~/jobfeed && .venv/bin/python -m jobfeed run >> logs/run.log 2>&1
```

**Being a polite client.** Requests have a 20-second timeout, two retries with backoff, at most 4 at a time, and a User-Agent naming the tool. One failing source never stops the others; it shows up in the email footer instead.

## Build plan

Eight milestones, each ending in something you can run. Milestones 1–7 can be built and tested on a laptop; only milestone 8 needs the Pi.

- [ ] **1. Skeleton and Greenhouse.** `Job` model, Greenhouse adapter, `run --dry-run` printing raw postings. Done when it lists jobs for three test companies.
- [ ] **2. Normalizers.** Location, salary and seniority parsing, with unit tests built from 50+ real location and salary strings.
- [ ] **3. Filters.** Rules read from `config.yaml`, plus the `replay` command.
- [ ] **4. Store and dedupe.** `jobs.db`, `first_seen` and `emailed_at` tracking, and the 30-day cross-source dedupe.
- [ ] **5. Lever, Ashby and discover.** Two more adapters and the `discover` command, then discover run on the starter names in the Seed list section.
- [ ] **6. Aggregators.** Remotive, RemoteOK and Himalayas adapters, with their attribution links kept, plus slug harvesting from their apply links.
- [ ] **7. Email.** HTML and text digest sent through Gmail; first real send to yourself.
- [ ] **8. Deploy.** Install on the Pi, add the cron line, then read a week of digests and tune `config.yaml`.

## Risks and open questions

The biggest gap is coverage of big Seattle-area employers: Amazon, Microsoft and Google run their own career sites, so their hybrid and onsite roles will not appear in v1.

**Risks**

| Risk | Effect | Mitigation |
| --- | --- | --- |
| Big-tech career sites not covered | Missing many Seattle onsite and hybrid roles | Custom adapters later, one per company; check those sites by hand until then |
| Messy location strings | Good remote roles dropped, or foreign ones let through | Unit tests from real strings; `replay`; raw location shown in the email |
| An ATS or aggregator changes its API | A source silently returns nothing | Zero-count warning in the email footer |
| Aggregator terms | Access cut off | Daily calls only, link-backs kept, nothing republished |
| Gmail app password revoked | No email | Send failures logged; the daily-email rule makes silence noticeable |

**Open questions**

- [x] Should engineering manager titles stay excluded?
- [x] Is the Seattle-area city list right, or should it add places like Everett, Sammamish or Tacoma?
- [x] Should the $150K floor compare against the top of the range (current default) or the bottom?
- [ ] Prune or add to the starter names in the Seed list section.

**Future work**

- The fit scorer (item 2) slots in after the filters and re-ranks each section.
- HN "Who's Hiring" parsing.
- Workable, SmartRecruiters and Recruitee adapters.
- Big-tech career site adapters.

## Sources

- [13 ATS Platforms with Public Job Posting APIs](https://theirstack.com/en/blog/13-ats-platforms-with-public-job-posting-api) — TheirStack
- [Remotive remote-jobs-api README](https://github.com/remotive-com/remote-jobs-api) — Remotive
- [Ashby job board API guide](https://jobspipe.dev/guides/ashby-api) — JobsPipe
- [Himalayas Remote Jobs API actor](https://apify.com/himalayas/remote-jobs) — Himalayas on Apify
- [RemoteOK Jobs Scraper notes](https://apify.com/jungle_synthesizer/remoteok-jobs-scraper) — Apify
- [Tech companies in Seattle hiring now](https://jobspipe.dev/hiring/tech/seattle) — JobsPipe, Nov 2026
- [Seattle AI Startups Hiring Fastest in 2026](https://www.landbase.com/blog/seattle-ai-startups-hiring-fastest) — Landbase
- [Software Engineer jobs in Seattle](https://www.fastaijobs.com/jobs/role/software-engineer/seattle) — Fast AI Jobs
- [Software Engineer Jobs Seattle 2026](https://jobrise.io/en/blog/software-engineer-jobs-seattle-2026/) — JobRise
- [Top Tech Companies in Bellevue, WA](https://builtin.com/companies/location/na/usa/wa/bellevue) — Built In
- [Why Seattle is the top city for software engineering talent in 2026](https://karat.com/seattle-top-city-to-hire-software-engineers/) — Karat
