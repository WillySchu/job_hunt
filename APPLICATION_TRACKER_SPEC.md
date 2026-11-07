# Application Tracker Spec

Nov 7, 2026 · @user

jobfeed records every application, what you did for it and what happened, so each change you make to how you apply can be measured against the ones before.

## Goal and scope

The tracker answers one question: did a change to how you apply get more replies, screens and offers? It runs inside jobfeed on the Pi, keeps its data in `jobs.db`, and updates itself from your Gmail.

**In scope for v1**

- Logging applications four ways: a code from the digest, a reply to the digest, manual entry, and Greenhouse, Lever and Ashby confirmation emails as a safety net
- Reading job mail over IMAP to record rejections, interview requests, offers and replies
- A dated history of stages for every application
- Follow-up reminders and a 21-day "no response" rule
- Metrics on the command line (`jobfeed stats`) and in a weekly email
- Fields for the experiments ahead: form filling, fit score and resume version, recorded from day one

**Out of scope for v1**

- Form filling itself, which is its own project (see Experiment plan)
- Calendar integration
- A web dashboard
- Offer comparison (item 7)

## What each application records

For a role from the digest, most fields are copied from the posting jobfeed already stored, so logging one takes a code and nothing else. The fields for later experiments exist from the start and stay blank until those tools are built.

| Field | Holds | Filled from |
| --- | --- | --- |
| Code | `#412`, the same number shown next to the posting in the digest | jobfeed, when logged |
| Company, title, link | The role | The posting, or typed for roles found elsewhere |
| Found via | The digest source (Greenhouse, Remotive, …), referral, LinkedIn, recruiter or other | The posting, or `--via` |
| Digest section | Watchlist, new matches or no salary listed | The posting |
| Referral | Yes or no | `--referral` |
| Resume version | A label such as `v1` | `tracking.resume_version` in config.yaml, or `--resume` |
| Cover letter | Yes or no | `tracking.cover_letter` in config.yaml, or `--cover-letter` |
| Form filled automatically | Yes, no, or blank before form filling exists | The form filler, once built |
| Fit score | 0–100, blank before the scorer exists | The scorer (item 2), once built |
| Posted, first seen | Dates | The posting |
| Days to apply | Applied date minus the day jobfeed first saw the posting | Computed |
| Applied | Date | When logged, or the confirmation email's date |
| Logged via | Code, reply, manual or confirmation email | jobfeed |
| Pay, work mode, seniority | As read by the normalizers | The posting |
| Stage | The current stage (next section) | The latest event |
| Notes | Free text | `jobfeed note` or a reply |

## Stages

&#91;embedded content: application stages · five in order, three early endings\]

Every move is a dated event, so the time between stages is measurable too. Only Applied closes on silence, because interview loops often pause for weeks; later stages get a follow-up reminder instead.

## Logging applications

There are four ways in, and all four lead to the same record. Anything logged since the last run is confirmed at the top of the next digest.

1. **A code from the digest.** Each digest entry shows a number such as `#412`, assigned when the posting is first emailed. On the Pi, `jobfeed apply 412` logs it. Several at once: `jobfeed apply 412 418`. Options: `--resume v2`, `--cover-letter` / `--no-cover-letter`, `--referral`, `--date 2026-11-05` for an application made on an earlier day, and `--note`. The resume and cover-letter defaults come from the `tracking` section of `config.yaml`.
2. **A reply to the digest.** Reply to a morning email with one command per line, and the next run reads it:

   ```
   applied 412 418
   applied 430 resume v2 referral
   screen 412
   rejected 418
   withdrew 430
   note 412 recruiter call Tue 2pm
   ```

   Only replies from your own addresses (`GMAIL_USER` or `DIGEST_TO`) count, and only when Gmail sent them from your account or verified the sender (DMARC), so a forged "From: you" does nothing. The quoted original below your reply, signatures, and lines that are plainly not commands ("Thanks!") are ignored. Any line it can't read or carry out is quoted back in the next digest, with the reason. Any CLI stage works (`interview 412 onsite`, with the words after the codes kept as a note), as do the `applied` options, and a date (`on 2026-11-05`, `yesterday`); without one, entries are dated when the reply was sent. `jobfeed mail` reads replies on demand, and `jobfeed mail --dry-run` shows what they would do.
3. **Manual entry** for roles found elsewhere: `jobfeed apply --url https://… --company Acme --title "Senior Backend Engineer" --via referral`. It gets the next code. If the link matches a posting jobfeed has stored (ignoring any `?query` or `#fragment`), the posting's fields are copied in and `--company`, `--title` and `--via` aren't needed.
4. **Confirmation emails.** When a Greenhouse, Lever or Ashby "thanks for applying" email arrives for a role you haven't logged, jobfeed creates the application, matched to a stored posting where it can. The digest lists these so you can add a resume version or fix a wrong match.

A role logged twice, say by code and then by its confirmation email, stays one application; the confirmation becomes an event on it.

The other commands: `jobfeed mark 412 screen` sets any stage (with `--date` and `--note`), `jobfeed note 412 …` adds a note, `jobfeed apps` lists open applications (`--all` includes closed ones), `jobfeed apps 412` shows one with its history, and `jobfeed undo 412` removes the last entry made for an application, or the application itself when that's all there is.

## Reading Gmail

Each run reads new job mail before building the digest. Access is read-only and limited to job mail, and jobfeed stores only what it concluded from each message, never the message itself.

**Access.** IMAP at `imap.gmail.com`, port 993, using the same `GMAIL_USER` and `GMAIL_APP_PASSWORD` that send the digest. IMAP has to be turned on once in Gmail (Settings → See all settings → Forwarding and POP/IMAP). The mailbox is opened read-only, so nothing is marked read, moved or deleted.

**What it reads.** Gmail runs the search on its side, over All Mail so archived messages count, starting a day before the last successful check:

- mail from the applicant-tracking systems' sending domains in `tracking.ats_senders` (Greenhouse, Lever and Ashby to start; more can be added as real mail shows up);
- mail from any company you have an open application with, searched by its name, which Gmail matches in sender names and addresses;
- your replies to the digest.

Nothing else in the inbox is fetched. Each message considered is remembered by its Gmail ID, so none is counted twice.

**How it classifies.** Rules on the sender, subject and opening lines decide what a message means:

| Kind | Typical wording | Effect |
| --- | --- | --- |
| Confirmation | "Thank you for applying", "we've received your application" | Logs the application if it's new; otherwise a `confirmation` event, which also corrects an applied date logged later |
| Rejection | "not moving forward", "decided to pursue other candidates" | Rejected, recording the stage it had reached |
| Interview request | "your availability", "I'd love to set up a call", a scheduling link | Screen, or interview if already past the screen; a reply if already interviewing |
| Offer | "pleased to offer", "offer letter" | Offer |
| Other reply | A person, not a no-reply address, at the company's domain | Records a first reply; stage unchanged |

**Matching.** A message is tied to an application by a link to the posting itself first, then a link to the company's job board, then the company's email domain (the sender's, or Reply-To for mail sent through an ATS), then the company's name in the sender name, subject or opening lines. The role title settles between several applications at one company. A message that matches no application, or two, isn't acted on, and mail that names a role you haven't logged is never put on one you have. Newsletters and mailing lists, and automated company mail with none of the job wording, are passed over.

**Wording.** Rejections win over the "thank you for applying" they usually open with. An email that reads as both a confirmation and an interview request is an invitation when a person wrote it, and goes to "Check these" when an automated address did.

**When it's unsure.** Unmatched messages, and ones whose wording fits no kind or more than one, go in a "Check these" list in the next digest, with sender and subject. Reply with a command (`screen 412`, `rejected 412`) or leave it. Once the scorer exists (item 2), it can take a first pass at these.

**What it keeps.** For each message it acted on: the Gmail ID, the date, the kind it was read as, and the application. No bodies or attachments are stored.

**Checking the rules.** `jobfeed mail --dry-run` prints each new message with what it was read as and the phrase that decided it, and `--reread --days N` runs the current rules over mail already read. `jobfeed edit CODE --resume v2` (also `--cover-letter`, `--referral`, `--via`) fixes the details of an application logged from its confirmation.

## Metrics and reports

Every rate is shown as a count with a likely range, such as "3 of 24 replied (13%, likely 5–27%)", because at these volumes a bare percentage misleads.

**Metrics**

- **Reply rate:** applications that got any response from the company, rejections included.
- **Screen, interview and offer rates:** applications that reached that stage or further.
- **Days to first reply:** the median, from applying to the company's first response.
- **Open:** applications still waiting.

Rates only count applications that have had time to resolve: at least 21 days old, or already closed. Otherwise a week of fresh applications would drag every rate down.

**Cuts.** Any metric can be split by found via, digest section, referral, resume version, cover letter, form filled automatically, fit score band, days to apply (0–1, 2–3, 4–7, 8+), seniority, work mode, or the week applied.

**Small numbers.** The likely range is a 90% interval for each rate (a Wilson interval, which stays sensible at small counts and at 0% or 100%). A comparison is marked "too few to tell" when either group has fewer than 15 applications or the ranges overlap heavily, meaning either group's rate falls inside the other's range. Each split compares its highest and lowest group on one metric, replies by default.

**What counts as what.** A reply is any answer from the company, rejections included; a confirmation email and the no-reply rule's closure are not. Reaching a stage includes applications rejected or withdrawn after it. Days to first reply runs from applying to the first answer. Fit score bands are high (0.7+), medium (0.4+) and low, and unscored until the scorer exists.

**On demand.** `jobfeed stats` prints the overall rates and every split with data in at least two groups; `--by CUT` picks splits, `--metric` sets what the verdicts compare, and `--since` limits it to recent applications.

**Daily digest.** A short "Your applications" block at the top, left out when empty:

- logged since the last run, and any reply lines it couldn't read;
- changes since the last run, such as "#412 Stripe, Senior Backend Engineer: screen (from email)";
- follow up: applications with no reply after 10 days, and later stages quiet for 10 days, each listed once;
- check these: mail it couldn't place.

An application with no response 21 days after applying moves to "no response". Later stages aren't closed automatically, because interview loops often pause for weeks; they get the 10-day follow-up instead. Both thresholds are settings in config.yaml.

How the rules work in detail:

- "Since the last run" means since the last digest that was actually sent: each event is marked reported when an email carrying it goes out, so a failed send leaves everything for the next one.
- A follow-up is listed once per quiet stretch and kept as a `follow_up` event. Any new entry, a note included, starts a new stretch. An application with news in the same digest waits a day.
- The no-response closure is dated the day the limit passed, not the day of the run. `jobfeed mark CODE reopen` restarts the clock, and a reply reopens it. `undo` refuses to remove the rule's closure, because the rule would only close it again.
- A dry run shows what the rule would close without saving it.

**Weekly email.** Sent by the Monday 6:00 run as a separate message:

- this week's applications and the total so far;
- the stage-by-stage funnel, for all time and the last 4 weeks;
- every cut that has data in at least two groups, with counts and ranges;
- open applications that have gone longest without news.

**On the command line.** `jobfeed stats` prints the same report at any time. `--by resume` shows one cut, and `--since 2026-11-15` limits it to applications from that date. `jobfeed weekly` emails the report now, covering the last 7 days.

**When it goes out.** The weekly email follows the digest on the run on `tracking.weekly_day`, and covers the seven days before that day. The first goes out on the first such day after installing; after that a missed run or a failed send is made up by the next run. A failed report never fails the run, since a retried run would send the digest twice, and no report goes out when the digest itself couldn't be sent.

## Data model

Three new tables join `jobs` in `jobs.db`, and jobfeed upgrades an existing database on its next run. The events table is the record; an application's current stage is kept alongside it only for quick listing.

```sql
-- schema 3 (schema 4 adds events.reported; schema 5 adds meta and notices)
ALTER TABLE jobs ADD COLUMN code INTEGER;      -- digest number, set when a posting is first emailed
ALTER TABLE jobs ADD COLUMN section TEXT;      -- the digest section it was emailed in
CREATE UNIQUE INDEX jobs_code ON jobs(code);

CREATE TABLE applications (
  code            INTEGER PRIMARY KEY,         -- the posting's digest number, or the next free one
  job_id          TEXT REFERENCES jobs(id),    -- NULL for roles found outside jobfeed
  company         TEXT NOT NULL,
  title           TEXT NOT NULL,
  url             TEXT,
  found_via       TEXT NOT NULL,   -- greenhouse | remotive | ... | referral | linkedin | recruiter | other
  digest_section  TEXT,            -- watchlist | matches | no_salary
  referral        INTEGER NOT NULL DEFAULT 0,
  resume_version  TEXT,
  cover_letter    INTEGER NOT NULL DEFAULT 0,
  autofilled      INTEGER,         -- NULL until form filling exists
  fit_score       REAL,            -- NULL until the scorer exists
  first_seen      TEXT,            -- copied from jobs, for days to apply
  applied_at      TEXT NOT NULL,
  logged_via      TEXT NOT NULL,   -- code | reply | manual | confirmation
  stage           TEXT NOT NULL,   -- latest stage, from events
  stage_at        TEXT NOT NULL,
  notes           TEXT
);

CREATE TABLE events (
  id          INTEGER PRIMARY KEY,
  code        INTEGER NOT NULL REFERENCES applications(code),
  at          TEXT NOT NULL,
  kind        TEXT NOT NULL,   -- applied | reply | screen | interview | offer | accepted | declined
                               -- | rejected | withdrawn | no_response | reopened | note | follow_up
                               -- | confirmation
  origin      TEXT NOT NULL,   -- cli | reply | email | rule
  message_id  TEXT,            -- the Gmail message it came from
  detail      TEXT,            -- the stage a rejection came at, or a note's text
  reported    TEXT             -- when a digest first carried it; NULL until then (schema 4)
);
CREATE INDEX events_code ON events(code, at);

CREATE TABLE meta (              -- schema 5
  key    TEXT PRIMARY KEY,        -- mail_checked: the last successful Gmail check
  value  TEXT NOT NULL
);

CREATE TABLE notices (           -- schema 5: reply lines not used, until a digest reports them
  id        INTEGER PRIMARY KEY,
  at        TEXT NOT NULL,
  kind      TEXT NOT NULL,        -- reply_line | unverified | check
  text      TEXT NOT NULL,
  reported  TEXT
);

CREATE TABLE mail_seen (
  message_id  TEXT PRIMARY KEY,   -- Gmail's ID, so a message is never counted twice
  seen_at     TEXT NOT NULL,
  kind        TEXT,               -- what it was read as; NULL if ignored
  code        INTEGER             -- the application it was tied to
);
```

Pay, work mode and seniority aren't copied: stats read them from `jobs` through `job_id`, so manually logged roles simply have none.

**Backups.** Until now `jobs.db` held postings that could be fetched again; application history can't be. The Monday run copies the database to `backups/jobs-YYYY-MM-DD.db` and keeps the last 8 (`tracking.backups_kept`). The copy uses SQLite's backup API, so it's consistent even mid-write, and the first one is made by the first run after installing. `jobfeed backup` makes one on demand. Copying `backups/` off the Pi now and then guards against a failed SD card.

## Experiment plan

Each phase changes one thing, so its effect can be read against the applications before it. The tracker records every field below from day one, so no phase needs a change to the tracker.

| Phase | What changes | Recorded as | Question it answers |
| --- | --- | --- | --- |
| 1. Baseline | Nothing: your current resume, applying by hand | `resume_version: v1`, `autofilled: no` | Your starting reply and screen rates |
| 2. Form filling (optional, if built next) | Applications take less effort, so you can send more | `autofilled: yes` | Do the extra applications reply as often? |
| 3. Fit score (item 2) | Every posting gets a score; you apply as usual | `fit_score` | Do higher-scored roles reply more? If so, sort the digest by score |
| 4. Resume versions (item 3) | Two versions, alternated application by application | `resume_version` | Does the new version get more replies or screens? |

**Baseline first.** Every later comparison is against these applications, so collect at least 30 before changing anything.

**Form filling changes more than effort.** If it also leads you to apply to weaker matches, a lower reply rate could reflect the roles rather than the forms. Once the fit score exists, comparing within score bands separates the two.

**Alternate resume versions; don't switch.** If v2 replaces v1 on a given date, a quiet hiring month looks like a worse resume. Alternating keeps both versions in the same weeks. The digest can suggest a version for each posting, assigned by its code so the split is effectively random, and logging defaults to the suggestion.

**Expect to need volume.** Telling a 10% reply rate from a 20% one with the usual statistical confidence takes about 200 applications per version, or about 150 at a looser standard. Small tweaks won't be measurable at all; test changes big enough to double a rate. That is also the case for form filling: more applications make every other comparison readable sooner.

## Build plan

Tracking starts with milestone 1, so the baseline can begin while the rest is built. Milestones 1 and 2 need no inbox access. By milestone 4, a few weeks of real job mail will have arrived to test the rules against.

- [x] **1. Applications and codes.** Schema 3, digest codes (`#412`), and `jobfeed apply`, `mark`, `note`, `apps` and `undo`. Done when you can log today's applications.
- [x] **2. Daily follow-ups.** The "Your applications" block in the digest, the 10-day follow-up and the 21-day "no response" rule.
- [x] **3. Gmail reader and replies.** Read-only IMAP, `mail_seen`, and digest replies run as commands. Done when a reply to the digest logs an application.
- [x] **4. Mail classifier.** Confirmation, rejection, interview, offer and other reply; matching mail to applications; the "Check these" list; and `jobfeed mail --dry-run`, which shows how each recent message would be read without acting on it.
- [x] **5. Stats.** `jobfeed stats` with cuts, likely ranges and "too few to tell".
- [x] **6. Weekly email and backups.** The Monday report, and the weekly copy of `jobs.db`.
- [ ] **7. Two weeks of checking.** Compare the tracker's stages with your inbox, and add rules for any mail it misread.
- [ ] **8. Follow-up: LinkedIn confirmations.** Read LinkedIn's "your application was sent" emails as confirmations, so Easy Apply applications are logged too.

## Risks and open questions

The biggest risk is misread mail. A missed rejection makes the funnel look better than it is, and a wrongly matched email records an event on the wrong application.

| Risk | Effect | Mitigation |
| --- | --- | --- |
| Rejection and interview wording varies widely | Stages missed or wrong | Only clear matches act; the rest go to "Check these"; `mail --dry-run`; two weeks of checking |
| Recruiters writing from personal or agency addresses | Their mail isn't read at all | Record the stage by reply or `jobfeed mark` |
| Applications through company career sites or LinkedIn | No ATS confirmation, so a forgotten one goes unlogged | Log by code or reply; LinkedIn confirmations are a planned follow-up (milestone 8) |
| Google retires app passwords | Neither reading nor sending works | Switch to Google sign-in (OAuth), which needs a one-time Google Cloud project |
| Small samples | Early numbers over-read | Likely ranges, "too few to tell", alternating versions |
| Application history on one SD card | Losing it loses the baseline | Weekly backups, copied off the Pi now and then |

**Open questions**

- [ ] The exact sending addresses for Greenhouse, Lever and Ashby mail, to confirm from your first confirmations with `jobfeed mail --dry-run --reread`. They're a setting (`tracking.ats_senders`), so a new one is a config change. Ashby is confirmed: DuckDuckGo's confirmation came from `no-reply@ashbyhq.com`, with the company in the sender name ("DuckDuckGo Hiring Team") and the role in the subject. Greenhouse and Lever are still to see.
- [x] LinkedIn's "your application was sent" emails: deferred to a short follow-up after the tracking work (milestone 8).
- [x] Thresholds: 10 days for a follow-up and 21 days for "no response" to start, both adjustable in config.yaml.
- [x] Resume `v1` is your one current resume. Each version's file gets stored with its label when resume versions (item 3) begin.

## Sources

- [Gmail SMTP, IMAP & POP3 Settings (2026)](https://smtpedia.com/gmail) — app passwords for IMAP; turning IMAP on
- [Gmail OAuth 2.0 Changes 2026](https://www.getmailbird.com/gmail-oauth-changes-app-password-phase-out/) — Mailbird, on app passwords as a transitional option
