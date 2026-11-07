# Fit Scorer Spec

Nov 7, 2026 · @user

## Goal and scope

Every posting the digest sends gets two fit scores from 0 to 100: one from keyword rules and one from a language model reading the posting against your resume. Both are stored and kept hidden during the baseline. Once replies accumulate, the stats show which score better predicts which applications get answered.

In scope:

- a keyword scorer: transparent rules, free, instant;
- an LLM scorer: Claude via the API, a score and a one-line reason, the reason kept but not shown;
- both scores frozen on each posting when it's first sent, and copied onto applications;
- comparing the two: how often they disagree, how each matches your own judgment on a sample, and reply rates by score band for each;
- turning scores on in the digest later (shown, then sorted) with a config switch, once the baseline is done.

Out of scope for now: rescoring old postings when the resume changes, scoring postings the filters dropped, and any scoring of companies rather than roles.

## What gets scored and when

Each run scores the postings it is about to send, before the digest is built, and stores the scores on the posting. A score is frozen: it describes the posting as sent, against the resume as it was that day. The digest itself doesn't change while scores are hidden.

&#91;embedded content: scoring flow · two scorers, scores hidden\]

The digest goes out exactly as now; the scores travel only into applications and the comparison tools until they're switched on.

- **Scored:** every posting the digest carries (all three sections). Not the ones the filters drop, and not ones already sent, so a posting is scored once.
- **Order:** the keyword score is computed in the run, instantly. The LLM score is one API call per posting, four at a time, so a 50-posting morning adds about a minute.
- **Failures:** a posting the API couldn't score stays unscored and is retried by the next run. Scoring never delays or blocks the digest.
- **Backfill:** `jobfeed fit backfill` scores postings already sent before this existed, so your first applications get scores too.
- **Applications:** logging an application (by code, reply, URL or confirmation email) copies both scores onto it, as the spec's `fit_score` field always intended. An application for a role jobfeed never saw has no scores.

While hidden, there is no command that shows one posting's score. Seeing it before applying would change what you apply to, which is exactly what the baseline must avoid. The comparison tools below only show scores for postings over two weeks old.

## What it scores against

Both scorers read your resume from its repo: [`markdown/resume.md` in WillySchu/pandoc\_resume](https://github.com/WillySchu/pandoc_resume/blob/master/markdown/resume.md), about 580 words of plain Markdown. Each run fetches the raw file from GitHub (the repo is public, so no token), keeps a local copy for when GitHub can't be reached, and records a short hash of the text with every score. When you change the resume, scores from then on carry the new hash, which also links up with resume versions (item 3) later.

**What "fit" means here: how well you match what the posting asks for, from the employer's side.** The question the tracker answers is which applications get replies, and replies come from a recruiter reading your resume against their requirements. Your own wants (remote or Seattle, salary, no frontend) are already enforced by the filters, so scoring them again would only blur the signal. The LLM gets an optional two-line note of context the resume doesn't say, such as recent work that isn't on it yet, set in `config.yaml`; it is not a list of preferences.

Your contact line (email and phone) is cut from the resume before anything is sent.

## Keyword scorer

A sum of four parts, out of 100, from plain rules anyone can check. It's free, instant and never fails; its weakness is that it sees words, not meaning ("Go" the language and "go" the verb, a skill listed as nice-to-have versus required).

| Part | Points | How it's scored |
| --- | --- | --- |
| Skills | 60 | Of the technical terms the posting names, the share you have, from a built-in list of about 70 terms that `config.yaml` can add to (languages, databases, cloud, infrastructure, domains such as distributed systems). Terms under a requirements heading count double those under nice-to-have. A strong skill counts fully, a basic one (C, Ruby, Lua on your resume) half. A posting that names only a few known terms is pulled toward half marks, so one matching term says less than six. |
| Level | 20 | The posting's seniority against `seniority.prefer` (senior, staff): a match 20, one level off 10, further 0, unstated 12. |
| Years | 10 | A stated "N+ years": 10 if you have them, else 0; 10 when none is stated. |
| Focus | 10 | Backend terms against frontend terms in the description, reusing the role filter's lists: all backend 10, evenly mixed 5, mostly frontend 0. |

Your skills come from the resume: terms from the list that appear in it, with your years where the resume states them (Python 10+, Go 4+, Java 2+). A short override list in `config.yaml` marks skills as strong or basic, and adds any the resume leaves out. Each score keeps its working (which terms matched, which were missing), so a surprising score can be explained.

The rules are versioned (`keywords-1`); changing them starts a new version rather than silently changing old scores.

## LLM scorer

Claude Haiku 5.5 (`claude-haiku-5-5`) reads each posting against your resume and returns a score from 0 to 100 and a one-line reason. Haiku is Anthropic's model for high-volume classification at the lowest price, which is this job exactly; Sonnet 5.5 costs 20 times as much and can be compared on a sample later if Haiku's scores look poor.

- **Prompt:** the instructions, rubric, resume and context note go in the system prompt, which is the same for every posting, so it is cached (it's over the 512-token minimum Haiku caches). The posting goes in the user message: title, company, location, level, pay as normalized, and the description.
- **Rubric:** 90–100 meets every stated requirement at the right level; 70–89 meets the core requirements with minor gaps; 50–69 misses one core requirement; 30–49 a different stack, domain or level; 0–29 not your kind of role. It's told to judge as a recruiter screening resumes would, not to reward keyword overlap.
- **Output:** structured outputs guarantee JSON matching a schema, `{"score": integer, "reason": string}`, so there's no parsing of free text. The score is clamped to 0–100 and the reason trimmed to 160 characters.
- **Thinking:** effort `low`. Scoring doesn't need long reasoning, and thinking tokens are billed as output.
- **Stored, hidden:** the reason is kept with the score for when scores become visible.
- **Versioned:** each score records the model, a hash of the prompt and the resume hash. A new model or prompt is a new version, so old and new scores never mix in a comparison.

## Cost, failures and privacy

**About $0.0003 a posting, so roughly $0.40 a month at 50 postings a day.** The estimate assumes a 1,400-token cached system prompt (instructions plus a 580-word resume), 1,200 tokens of posting and 250 tokens out, thinking included, at [Haiku 5.5's prices](https://platform.claude.com/docs/en/about-claude/pricing):

| Per posting | Tokens | Price per million | Cost |
| --- | --- | --- | --- |
| Posting (input) | 1,200 | $0.10 | $0.00012 |
| Resume and instructions (cache read) | 1,400 | $0.01 | $0.000014 |
| Score, reason, thinking (output) | 250 | $0.50 | $0.000125 |

The cache is written once a run (about $0.0002). Backfilling a few hundred postings already sent costs about $0.10. The real token counts come back with every response, so the footer reports actual cost, not this estimate.

- **Guard rails:** at most `fit.llm_max_per_run` postings a run (300), and scoring stops for the month once `fit.llm_monthly_budget` is reached ($5, about ten times the estimate). Postings left unscored are picked up later.
- **Key:** `ANTHROPIC_API_KEY` in `.env`, beside the Gmail password. The API is billed through the Claude Console, separately from a Claude subscription. Max, Ultra and Team plans include monthly API credits that cover this many times over once a Console organization is linked; Pro has none, so it would mean buying credits.
- **Failures:** rate limits, overloads and server errors are retried (jobfeed calls the API with the httpx client it already uses, so the Pi needs no SDK); anything still failing leaves the posting for the next run. The digest footer says "Fit scores: 48 postings scored by keywords; 46 by the LLM ($0.012; $0.31 this month), 2 left for later (hidden for now)."
- **What leaves the Pi:** each posting's text and your resume without its contact line, to Anthropic's API. Nothing else from `jobs.db`, and nothing about your applications.

## Comparing the two

**Your own blind ratings give the first answer, in days; replies give the real one, in months.** Three checks, in the order they become possible:

1. **Agreement, from the first week.** How closely the two scores rank the same postings (a rank correlation from −1 to 1), and the ten postings where they disagree most, each with the LLM's reason and the keywords that matched. Reading those ten shows quickly which scorer is being fooled and how. Only postings over two weeks old are listed, so this can't steer what you apply to.
2. **Your ratings, after an evening.** `jobfeed fit rate` shows postings one at a time, title, company and link, with no scores, and asks "how strong a candidate are you for this, 1–5?" Rating about 40 gives each scorer a correlation with your judgment. You know the market better than either scorer, so a scorer that can't track your ratings is unlikely to predict replies. Postings are drawn from the last 30 days, both ones you applied to and ones you didn't.
3. **Replies, the real test.** For applications old enough to count, each scorer's reply rate in its top half against its bottom half, with likely ranges and the same "too few to tell" rule as the rest of the stats. Each also gets its own split in `jobfeed stats` (score bands 70+, 50–69, under 50).

How much data the third check needs: if the better scorer's top half replies at 35% and its bottom half at 15%, the stats can tell those apart at about 40 to 60 counted applications; telling a good scorer from a slightly better one takes hundreds. Expect the reply comparison to say "too few to tell" for a few months, and the ratings to carry the decision until then.

One caveat: replies only exist for postings you applied to. Keeping scores hidden is what makes that fair, since your choice of what to apply to can't then depend on the scores.

## Data model

Two new tables and one new column, schema 6. Scores live with postings; applications copy them when logged, so an application's score is the one that was current when you applied.

```sql
-- schema 6
CREATE TABLE fit_scores (
  job_id       TEXT NOT NULL REFERENCES jobs(id),
  scorer       TEXT NOT NULL,   -- keywords | llm
  version      TEXT NOT NULL,   -- keywords-1 | claude-haiku-5-5/prompt-1a2b3c
  resume_hash  TEXT NOT NULL,   -- the resume it was scored against
  score        INTEGER NOT NULL,-- 0-100
  reason       TEXT,            -- the LLM's one line; NULL for keywords
  working      TEXT,            -- JSON: matched and missing terms, or token counts and cost
  scored_at    TEXT NOT NULL,
  PRIMARY KEY (job_id, scorer)
);

CREATE TABLE fit_ratings (       -- your blind 1-5 ratings
  job_id    TEXT PRIMARY KEY REFERENCES jobs(id),
  rating    INTEGER NOT NULL,
  rated_at  TEXT NOT NULL
);

ALTER TABLE applications ADD COLUMN fit_keywords INTEGER;
-- applications.fit_score (already there, empty until now) holds the LLM score
```

The weekly backup and its email attachment cover the new tables, since they're in `jobs.db`. Scores add a few hundred bytes a posting, which barely moves the attachment's size.

## Build plan

Five milestones, built in order. The first two start scoring every posting, hidden, so the baseline is scored from now on; the LLM scorer also backfills every posting already sent, since their text is stored. The rest compare the scorers and, after the baseline, show the scores.

- [x] **1. Resume and keyword scorer.** Fetch the resume, schema 6, the keyword scorer, scoring each run's digest postings, and `jobfeed fit backfill`. Done when every posting sent has a keyword score.
- [x] **2. LLM scorer.** The API key, Haiku 5.5 with structured outputs and a cached system prompt, four calls at a time, the per-run and monthly caps, retries, and the cost line in the footer. Done when every posting sent has both scores, including the backfill.
- [x] **3. Scores on applications and in the stats.** Copy both scores when an application is logged; a split per scorer in `jobfeed stats` and the weekly report; the top-half against bottom-half reply comparison.
- [ ] **4. Ratings and agreement.** `jobfeed fit rate` (blind, 1–5) and `jobfeed fit compare`: agreement between the scorers, each against your ratings, and the ten biggest disagreements over two weeks old.
- [ ] **5. Visible scores, after the baseline.** `fit.show: llm` puts the score and reason on each digest entry; `fit.sort: true` orders each section by it. One config change each; the stats split applications by when it was turned on (phase 3 of the tracker's experiment plan).

## Risks and open questions

| Risk | Effect | Mitigation |
| --- | --- | --- |
| Short descriptions (some aggregator postings carry only an excerpt) | Both scorers judge on too little | Each score records the description's length; the comparison can leave very short ones out |
| LLM scores bunch together (everything 70–80) | It can't separate good matches from fair ones | Anchored rubric; `fit compare` shows each scorer's spread; a new prompt version if needed |
| Keyword list misses a term | A real skill match scores low | Each score lists the terms it found and missed; the list is in `config.yaml` |
| Peeking at scores during the baseline | Your choices follow the score, and the reply test loses its fairness | No per-posting score view while hidden; comparisons show only postings over two weeks old |
| Spend | An unexpected bill | Per-run and monthly caps; actual cost in the footer |
| Model retirement | Haiku 5.5 eventually goes | Not before October 7, 2027; scores are versioned, so a successor starts a new version |

- [x] API access: a Claude Console organization linked to your Max plan, whose $100 of monthly API credits covers the scorer. Set up before milestone 2.
- [x] `markdown/resume.md` on `master` is the resume being sent (`v1`).
- [x] A $5 monthly cap.
- [x] Your ratings ask "how strong a candidate are you for this, 1–5?"
- [x] Both scorers, compared once there's data.
- [x] A single number; the LLM's reason stored but not shown yet.
- [x] Hidden until the baseline (about 30 applications) is done.
- [x] The resume comes from its GitHub repo.

## Sources

- [Pricing, Claude Platform Docs](https://platform.claude.com/docs/en/about-claude/pricing): Haiku 5.5 at $0.10 / $0.50 per million tokens up to 100,000-token prompts, cache reads $0.01, batch 50% off
- [Claude Haiku 5.5 overview](https://platform.claude.com/docs/en/models/haiku-5-5/overview): model ID `claude-haiku-5-5`, 512-token minimum for caching, retirement not before October 7, 2027
- [Structured outputs, Claude Platform Docs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs): `output_config.format` with a JSON schema, no beta header
- [WillySchu/pandoc\_resume](https://github.com/WillySchu/pandoc_resume): `markdown/resume.md`, the resume both scorers read
- [Monthly API credits for Max, Ultra, and Team plans](https://support.claude.com/en/articles/17154008-monthly-api-credits-for-max-ultra-and-team-plans): $100 a month on Max, claimed by linking a Console organization; none on Pro
- [Why is Claude API usage billed separately from my paid Claude plan?](https://support.claude.com/en/articles/9876003-i-have-a-paid-claude-subscription-pro-max-team-or-enterprise-plans-why-is-claude-api-usage-billed-separately-from-my-paid-claude-plan): the API needs a Console account
