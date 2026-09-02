# Circle Lead Discovery

Finds posts where **someone wants to hire** across Circle.so communities you are
authorized to read, and filters out **people looking for work**.

```
"We are looking for a backend developer."      -> LEAD
"I am looking for a job as a software engineer." -> NOT_LEAD
```

---

## Read this first: how this differs from a browser scraper

The original brief asked for a Playwright scraper driving a logged-in Circle
session. **This project does not do that**, because it cannot be done within
Circle's rules:

- Circle's [Platform Terms](https://circle.so/terms) prohibit third-party
  applications and scripts that scrape or extract data without Circle's prior
  written consent.
- A member account is not an API credential. Circle deliberately separates
  admin automation (Admin API tokens, created by a community admin) from
  member-side access (Headless JWTs, minted by the community's own Headless
  Auth token). One personal login is not designed to become a cross-community
  extraction key.
- `robots.txt` permitting crawlers does not override an account-level
  contractual restriction, and it says nothing about logged-in member content.

So ingestion runs on Circle's **official APIs**, authorized per community by
that community's operator. Everything else in the brief — discovery, relevance
filtering, incremental collection, the hiring-vs-seeking classifier, extraction,
scoring, deduplication, search, and export — is implemented as specified.

The trade-off is real: this takes longer to set up, and it will not let you
point the tool at an arbitrary community and start reading. What it gives you is
access that does not break, does not risk your account, and produces data you
can actually act on.

### What the tool will and will not do

| | |
|---|---|
| Discover communities from public listings and your own lists | Yes |
| Check a public landing page to see whether a community exists | Yes |
| Read posts, comments, and group chats you are **approved** to read | Yes |
| Classify, score, deduplicate, search, and export | Yes |
| Reuse your browser cookies or automate your logged-in session | **No** |
| Call undocumented endpoints, or bypass a login, paywall, or CAPTCHA | **No** |
| Read direct messages | **No** — excluded by an allowlist that fails closed |
| Ingest a community without recorded operator approval | **No** |
| Automate signup, join requests, or repeated join attempts | **No** |

---

## Install

Requires Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .            # add '.[llm]' for semantic classification
```

## Quick start

```bash
# 1. Record candidate communities (public metadata only)
circle-leads discover --from-file my_communities.txt
circle-leads communities

# 2. Get operator approval  (see docs/operator_request.md), then create
#    circle_leads/config/communities/<community>.yaml from the template
#    and set permission_status: approved

# 3. Put the operator-issued tokens in your environment
cp .env.example .env && $EDITOR .env && set -a && source .env && set +a

# 4. Collect, classify, score
circle-leads run --use-llm

# 5. Review and export
circle-leads search --role "Backend Developer" --skills "Python,AWS"
circle-leads export --format csv --priority HIGH -o exports/leads.csv
```

Every command accepts `--db` and `--config`. Run `circle-leads -h` for the full
list.

**New here? Read [docs/HOWTO.md](docs/HOWTO.md)** — how each stage works, plus
setup, daily use, tuning, and troubleshooting.

---

## Architecture

```
circle_leads/
├── discovery/         discover_communities.py, validate_community.py
├── authentication/    browser_session.py   (token resolution, JWT minting)
├── scraper/           community_scraper.py, posts_scraper.py,
│                      comments_scraper.py, chat_scraper.py,
│                      normalize.py, pagination.py
├── classifier/        lead_classifier.py, keyword_rules.py,
│                      ai_classifier.py, extraction.py
├── storage/           database.py, models.py
├── scoring/           lead_scoring.py
├── export/            exporters.py
├── config/            requirements.yaml, settings.py, communities/
├── cli/               main.py
└── pipeline.py        orchestration
```

Pipeline:

```
discover -> validate -> check relevance -> [operator approval]
   -> ingest approved spaces -> normalize -> deduplicate
   -> rule classification -> LLM escalation (ambiguous only)
   -> extract -> score -> store -> export -> human review
```

---

## Classification

The hard part is that hiring and job-seeking use nearly identical words. The
system decides by asking **who is being searched for** and **who performs the
work**, not by keyword presence.

**Layer 1 — rules** (deterministic, explainable, always runs). Weighted patterns
for hiring intent, job-seeking intent, negation, and hypotheticals. The decisive
test is whether the *object of the search* is a person or employment:

```
"looking for a software engineer"          object = a person      -> +
"looking for a job as a software engineer" object = employment    -> −
```

Negation ("we are not hiring", "role has been filled") is a hard disqualifier.
Confident scores settle the case without spending an API call.

**Layer 2 — LLM** (only for genuinely ambiguous posts). Two guardrails:

- `evidence_quote` must be an **exact substring** of the source. A LEAD verdict
  the model cannot ground in real words is downgraded to `UNCERTAIN`, not
  trusted.
- Extracted `budget` and `company` are discarded unless they appear in the
  source, so a reviewer never sees an invented number.

**Layer 3 — extraction.** Job title, skills, employment type, engagement type
(individual / agency / freelancer / contractor / technical cofounder /
full-time / part-time), company, budget, location, urgency.

Every stored lead keeps its evidence quote, reason, rule score breakdown, and
which layer decided — so a reviewer can see *why*.

## Scoring

```
hiring intent  +40    budget mentioned    +10
target role    +20    company identified   +5
target skill   +15    recent post         +10
```

Scaled by classifier confidence, clamped to 0–100.
`HIGH 80–100 · MEDIUM 50–79 · LOW 0–49`. All weights and thresholds live in
`requirements.yaml`.

## Deduplication

Three layers: `(community, source_content_id, content_type)` identity for
idempotent re-runs, a normalized content hash for exact duplicates, and a
token-shingle SimHash for near-duplicates (reposts, light edits). The SimHash
tolerance scales with text length, since one added word shifts a larger share of
a short post's shingles. Edited content is re-queued for classification rather
than keeping a stale verdict.

## Configuration

All lead requirements live in `circle_leads/config/requirements.yaml`: target
roles and skills, confidence threshold, keyword lists, scoring weights,
priority bands, excluded content types, retention, and rate limits. Change them
and re-run `classify` — no code changes.

Per-community authorization lives in `circle_leads/config/communities/*.yaml`,
one file each, holding the approved spaces and rooms and the **names** of the
environment variables that hold the tokens.

---

## Security

- **No credentials in source or Git.** Tokens are read from the environment at
  call time. Permission files store variable *names*, never values. `.env`,
  `data/`, and `exports/` are gitignored.
- **Nothing is logged.** `AdminCredentials` and `MemberSession` redact their
  tokens in `repr()`; a `redact()` helper strips `Authorization` and `Cookie`
  before any header reaches a log.
- **DMs are excluded by an allowlist that fails closed.** Circle's Headless
  `/messages` endpoint returns direct messages by default and offers no
  server-side filter, so rooms are collected only when positively identified as
  `group_chat`. An unrecognized or missing room kind is treated as a DM. Filtering
  happens *before* any message fetch, so DM content is never requested.
- **401/403 is a stop condition, not a retry.** Retrying an authorization
  failure would also burn the monthly API allowance.
- **Rate limits.** Circle documents 2,000 requests / 5 min per IP, a monthly
  allowance as low as 5,000 on Business plans, and counts 429s against it. The
  client uses a token bucket, exponential backoff with jitter, a 60-second
  cooldown on 429, and an optional `--request-budget` hard stop.
- **PII.** Emails and phone numbers are stripped at ingestion on every path.
  Phone matching requires positive evidence (an E.164 `+`, parenthesized area
  code, separator-joined national form, or a `phone`/`call`/`whatsapp` cue) so
  it does not shred the budget figures the scorer depends on.
- **Exports are formula-escaped.** Author names and post bodies are written by
  community members; a cell starting `=`, `+`, `-`, `@`, tab, or CR is quoted so
  a spreadsheet cannot execute it.
- **Deduplication is scoped per community**, so separately consented datasets
  are never linked.
- **Retention and kill switch.** `circle-leads purge --expired` drops content
  past the configured window; `purge --community <slug>` deletes a community's
  stored content and marks it revoked.

## Review before outreach

Leads are candidates, not verdicts. "Looking for a developer" can be a genuine
request, self-promotion, a joke, or a quotation. Review the permalink and the
surrounding thread before contacting anyone, prefer replying in the original
thread where community norms allow it, and follow the operator's rules on
commercial replies.

## Tests

```bash
pytest -q      # 166 tests
```

Covers every LEAD/NOT_LEAD example in the specification, the authorization
boundaries (DM exclusion, space allowlisting, permission gating, credential
redaction), the LLM guardrails, rate limiting and retry behavior, deduplication,
and a full ingest-classify-export run against a mocked API.

## Limitations

- Requires per-community operator cooperation. There is no path here that reads
  a community you have not been approved for.
- Chat collection needs the operator's Circle plan to include Headless
  (Business and above).
- The Admin API has no documented chat-read endpoint; posts and comments come
  from Admin v2, chats from Headless.
- Circle's docs disagree on the Admin v2 auth scheme (`Bearer` vs `Token`).
  Default is `Bearer`; override with `CIRCLE_ADMIN_AUTH_SCHEME=Token`.
- `per_page` has no documented ceiling. Default is 100; lower it if you see
  errors.
- The rule layer alone is tuned for English-language posts.
