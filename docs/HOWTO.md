# How it works, and how to run it

A working guide to the Circle lead discovery system: what happens under the
hood, and the exact commands to operate it.

- [Part 1 — How it works](#part-1--how-it-works)
- [Part 2 — Setup](#part-2--setup)
- [Part 3 — Daily use](#part-3--daily-use)
- [Part 4 — Tuning](#part-4--tuning)
- [Part 5 — Troubleshooting](#part-5--troubleshooting)

---

# Part 1 — How it works

## The one-paragraph version

You give the system a list of Circle communities. It records them and scores
how likely each is to contain hiring conversations. For communities whose
operator has given you permission, it pulls posts and comments through Circle's
official API, decides for each one whether the author **wants to hire** or
**wants a job**, extracts the role, skills, budget and urgency from the hiring
ones, scores them 0–100, removes duplicates, and lets you search and export the
result. You review each lead before contacting anyone.

## The pipeline

```
     you supply URLs                 operator grants permission
            │                                    │
            ▼                                    ▼
   ┌──────────────────┐              ┌────────────────────────┐
   │  1. DISCOVER     │              │  3. INGEST             │
   │  record + score  │              │  Circle official API   │
   │  relevance       │              │  approved spaces only  │
   └────────┬─────────┘              └───────────┬────────────┘
            │                                    │
            │  ┌─────────────────────────────────┘
            │  │
            ▼  ▼
   ┌──────────────────┐
   │  2. VALIDATE     │      ┌──────────────────────────────┐
   │  does it exist?  │      │  4. NORMALIZE + DEDUPE       │
   │  public page?    │      │  strip HTML, redact PII,     │
   └──────────────────┘      │  hash, skip what's stored    │
                             └──────────────┬───────────────┘
                                            ▼
                             ┌──────────────────────────────┐
                             │  5. CLASSIFY                 │
                             │  rules → LLM if ambiguous    │
                             │  LEAD or NOT_LEAD            │
                             └──────────────┬───────────────┘
                                            ▼
                             ┌──────────────────────────────┐
                             │  6. EXTRACT + SCORE          │
                             │  role, skills, budget, 0–100 │
                             └──────────────┬───────────────┘
                                            ▼
                             ┌──────────────────────────────┐
                             │  7. SEARCH / EXPORT          │
                             │  → human review → outreach   │
                             └──────────────────────────────┘
```

## Stage 1 — Discovery

You supply community URLs three ways: a text or CSV file, a saved public HTML
page the system pulls Circle links out of, or `--url` on the command line.

Each is normalized to a slug (`https://Startup-Founders.circle.so/c/general` →
`startup-founders`) and deduplicated. Circle's own infrastructure hosts —
`app`, `api`, `help`, `discover` — are rejected, since they aren't member
communities.

**Subdomain brute-forcing is not implemented.** Guessing at names is not
discovery, and it would hammer infrastructure you have no relationship with.

## Stage 2 — Validation and relevance

For each community the system fetches the **public landing page** — the same
request any visitor's browser makes — to check it exists and read its title and
meta description. It does not try to get behind a login.

That text is then scored for relevance. Startup/SaaS/founder/technology/AI/
agency signals add points; knitting, yoga, and fandom subtract them. A jobs or
marketplace space adds 15. Score ≥ 30 marks it relevant.

This is a **cheap filter to decide where to spend effort**, not permission to
read anything.

## Stage 3 — Permission (the gate)

Nothing is collected until the community's operator says yes. You record that
in a permission file, one per community, and ingestion runs **only** when it
reads exactly:

```yaml
permission_status: "approved"
allowed_space_ids: ["123", "456"]
```

`"Approved"`, `"APPROVED"`, `" approved"` and an empty value all fail. An empty
`allowed_space_ids` collects nothing. The gate is enforced at four independent
points, and the API token is not even read from the environment until the check
passes.

Why this exists: Circle's Platform Terms prohibit third-party scripts that
scrape or extract data without prior written consent, and a member login is not
an API credential. `docs/operator_request.md` has the email template.

## Stage 4 — Ingestion

Content comes from Circle's official APIs:

| Route | Reads | Needs |
|---|---|---|
| `admin_api_v2` | posts, comments | operator's Admin API token |
| `headless_member` | posts, comments, **group chats** | operator's Headless Auth token (Business plan+) |

Only approved spaces are requested. Unapproved ones are never fetched.

**Direct messages are never collected.** Circle's Headless `/messages` endpoint
returns DMs by default with no server-side filter, so the system uses an
*allowlist*: a room is read only if positively identified as `group_chat`. An
unknown or missing room kind is treated as a DM. Filtering happens *before* any
message fetch, so DM content is never requested at all.

Rate limiting throughout: a token bucket, exponential backoff with jitter, a
60-second cooldown on HTTP 429, and an optional `--request-budget` hard stop.
A 401 or 403 **stops the run** rather than retrying — retrying an authorization
failure just burns your monthly API allowance.

## Stage 5 — Normalize and deduplicate

HTML is stripped, entities decoded, whitespace collapsed. Emails and phone
numbers are redacted at ingestion.

Phone matching requires positive evidence — an E.164 `+`, a parenthesized area
code, a separator-joined national form, or a `phone`/`call`/`whatsapp` cue — so
that `Budget is 2024 500 1000` survives intact. A naive `NNN NNN NNNN` pattern
would shred exactly the budget figures the scorer depends on.

Three dedup layers:

1. **Identity** — `(community, source_content_id, content_type)`. Re-running a
   scrape adds nothing.
2. **Content hash** — normalized SHA-256, catches the same text reposted.
3. **SimHash** — token-shingle fingerprint for near-duplicates (light edits,
   reposts). The tolerance scales with length, since one added word shifts a
   larger share of a short post's shingles.

Dedup is scoped per community, so separately-consented datasets are never
linked. Edited content is re-queued for classification rather than keeping a
stale verdict.

## Stage 6 — Classification (the core)

The hard part: hiring and job-seeking use nearly identical words.

```
"I'm looking for a software engineer."              → LEAD
"I'm looking for a job as a software engineer."     → NOT_LEAD
```

Four words apart. Keyword matching cannot separate these — both contain
"looking for" and "software engineer". So the system asks a grammatical
question instead: **what is the object of the search?**

- Object is a *person* → someone is being hired → **LEAD**
- Object is *employment* (job, work, role, position, opportunity) →
  **NOT_LEAD**

### Decision order

```
1. Hard disqualifier?        "not hiring", "role filled", "hiring freeze"
                             → NOT_LEAD, stop.

2. Job-seeker signals only?  "open to work", first-person seeking employment
                             → NOT_LEAD, stop.

3. Rule score decisive?      ≥ 35 → LEAD     ≤ −20 → NOT_LEAD
                             → done, no API call spent.

4. Ambiguous + --use-llm?    escalate to the model.

5. Otherwise                 fall back to the rule verdict.
```

Weighted patterns feed the score: organizational subject seeking a role (+40),
"looking to hire" (+40), "developer wanted" (+38), "need a backend engineer"
(+35). Job-seeking patterns subtract: seeking employment (−45), "open to work"
(−45), "anyone hiring?" (−40). Supporting signals (a named deliverable, budget,
timeline, referral request) only count when hiring language is already present
— otherwise every product announcement would score as a lead.

### Real output

```
LEAD      score= 110 conf=0.95 by=rules  lead_score= 93 HIGH  | We are looking for a backend developer. Python and AWS. Budget $15,000.
NOT_LEAD  score= -90 conf=0.92 by=rules  lead_score= 10 LOW   | I am looking for a job as a software engineer.
NOT_LEAD  score=   0 conf=0.90 by=rules  lead_score= 10 LOW   | We are not hiring developers this quarter.
NOT_LEAD  score=   0 conf=0.50 by=rules  lead_score=  8 LOW   | Anyone know a good dev?
```

Note the third: "not hiring" contains the word *hiring* and is still correctly
rejected. The fourth is genuinely ambiguous — low confidence, and the case
`--use-llm` is for.

### The LLM layer

Only ambiguous posts reach the model, so obvious cases cost nothing. Two
guardrails:

- **`evidence_quote` must be an exact substring of the post.** A LEAD the model
  cannot ground in the actual words is downgraded to UNCERTAIN, not trusted.
  This is the guard against confident hallucination.
- **Extracted `budget` and `company` are discarded unless they appear in the
  source**, so a reviewer never sees an invented number.

If the model errors or returns malformed JSON, the rule verdict stands.

## Stage 7 — Extract, score, review

From each LEAD: job title, skills, employment type, engagement type (individual
/ agency / freelancer / contractor / technical cofounder / full-time /
part-time), company, budget, location, urgency.

```
hiring intent  +40      budget mentioned   +10
target role    +20      company identified  +5
target skill   +15      recent post        +10
```

Scaled by classifier confidence, clamped 0–100.
**HIGH 80–100 · MEDIUM 50–79 · LOW 0–49.** All weights live in YAML.

An agency or cofounder request that names no title and no stack still qualifies
— "we need an agency to rebuild our store" is a hiring lead.

Every stored lead keeps its evidence quote, reason, score breakdown, and which
layer decided, so you can see *why*.

**Leads are candidates, not verdicts.** "Looking for a developer" can be a real
request, self-promotion, a joke, or a quotation. Review the permalink and
surrounding thread before contacting anyone.

---

# Part 2 — Setup

## Install

Requires Python 3.11+.

```bash
cd circle-scraper
python3 -m venv .venv
source .venv/bin/activate
pip install -e .            # add '.[llm]' for the semantic layer
```

Verify:

```bash
circle-leads --help
pytest -q                   # 166 tests
```

## Step 1 — Add communities

```bash
cat > my_communities.txt <<'EOF'
# One URL per line
https://startup-founders.circle.so
https://saas-builders.circle.so
EOF

circle-leads discover --from-file my_communities.txt
circle-leads communities
```

```
SLUG                          REL  ACCESS                 PERMISSION
--------------------------------------------------------------------
startup-founders               85  visited                candidate
saas-builders                  70  visited                candidate
```

`REL` is the relevance score; `PERMISSION: candidate` means not yet approved —
so nothing will be ingested yet.

Other sources: `--url https://x.circle.so` (repeatable), or `--from-html
page.html` to extract Circle links from a saved public page.

## Step 2 — Get operator permission

Join the community normally, take part genuinely, then ask its operator. The
email template is in `docs/operator_request.md`. Ask for: read-only access,
named spaces only, no DMs, 30-day retention, human review before outreach, and
a token they can revoke.

**This step is not optional and cannot be automated.**

## Step 3 — Record the approval

```bash
cp circle_leads/config/communities/example.yaml.template \
   circle_leads/config/communities/startup-founders.yaml
```

Edit it:

```yaml
community_id: "startup-founders"
community_url: "https://startup-founders.circle.so"

permission_status: "approved"        # ← exactly this word enables ingestion

approved_purpose: "Detect posts explicitly requesting software-development help"
ingestion_route: "admin_api_v2"      # or headless_member for chats

allowed_space_ids: ["123", "456"]    # ONLY the spaces they approved
allowed_chat_room_uuids: []

excluded_content: [direct_messages, member_bios, email_addresses, phone_numbers]
retention_days: 30
operator_contact: "operator@example.com"
approval_reference: "Email thread 2026-09-01"

# Variable NAMES, never the tokens themselves
admin_token_env: "CIRCLE_ADMIN_TOKEN__startup_founders"
```

These files are gitignored — they hold consent metadata. Only the template is
tracked.

## Step 4 — Provide the tokens

```bash
cp .env.example .env
$EDITOR .env
set -a && source .env && set +a
```

```bash
CIRCLE_ADMIN_TOKEN__startup_founders=<token the operator issued>
ANTHROPIC_API_KEY=<optional, for --use-llm>
```

For the headless route, set `CIRCLE_HEADLESS_AUTH_TOKEN__<community>` and
`CIRCLE_MEMBER_ID__<community>` (or `CIRCLE_MEMBER_EMAIL__<community>` —
underscores, not hyphens, in the community part).

Tokens live only in the environment. They are never written to the permission
file, the database, an export, or a log. `.env` is gitignored.

---

# Part 3 — Daily use

## The whole thing

```bash
circle-leads run --use-llm
```

Ingests every approved community, then classifies and scores. Equivalent to
`ingest` followed by `classify`.

## Step by step

```bash
circle-leads ingest --request-budget 500     # collect
circle-leads classify --use-llm              # classify + score
circle-leads stats                           # check
```

```
Ingesting startup-founders via admin_api_v2...
  state=COMPLETE seen=142 new=142 updated=0

Classified 142: 11 lead(s), 124 not-lead, 7 filtered by requirements, 3 duplicate(s).

Communities:   2
Content items: 142 (0 unclassified)
Leads:         11 (4 high priority)
```

Runs are **incremental**: a second run collects only what's new. `--full`
ignores the watermark. `--request-budget N` stops after N API calls, which
matters because Circle's Business plan allows only ~5,000 requests/month.

## Search

```bash
circle-leads search                                    # everything
circle-leads search --priority HIGH                    # best first
circle-leads search --role "Backend Developer"
circle-leads search --skills "Python,AWS"
circle-leads search --min-score 70 --community startup-founders
```

```
====================================================================
Community:      startup-founders
Space:          General Discussion
Lead Score:     93  (HIGH)
Classification: LEAD (confidence 0.95, via rules)
Role:           Senior Backend Developer
Skills:         Python, PostgreSQL, AWS
Wants:          individual developer
Budget:         Budget $120,000
Author:         Dana Ops
Posted:         2026-09-02T10:41:40
Post:           "We're looking for a senior Backend Developer to join our startup..."
Evidence:       "We're looking for a senior Backend Developer to join our startup"
URL:            https://startup-founders.circle.so/c/general/post-0
====================================================================

1 lead(s). Review each before any outreach.
```

Only `LEAD` rows are ever returned — job seekers never appear.

## Export

```bash
circle-leads export --format csv --priority HIGH -o exports/high.csv
circle-leads export --format json -o exports/all.json
circle-leads export --format csv --extended -o exports/full.csv
```

Default CSV columns: `community, author, content, classification, confidence,
lead_score, job_title, skills, published_at, url`. `--extended` adds priority,
space, employment type, engagement type, company, budget, location, urgency,
evidence quote, reason, and the permission reference.

Cells are formula-escaped: a member whose display name is `=cmd|'/C calc'!A0`
cannot execute code in your spreadsheet.

## Retention and the kill switch

```bash
circle-leads purge --expired                        # past retention window
circle-leads purge --community startup-founders     # operator asked you to stop
```

The second deletes that community's stored content, marks it `revoked`, and
clears its sync watermark. Both prompt for confirmation; `--yes` skips it.

Run `purge --expired` on a schedule — retention is a commitment you made to the
operator, not housekeeping.

---

# Part 3b — Finding leads in communities you've joined

You don't need to be an admin, and you don't need anyone's API token. If you've
joined a free community as an ordinary member, you can read it in your browser
like any member — and `triage` does the hard part on what you read.

## The workflow

1. Open a community you've joined. Go to the space where people post requests
   — jobs, marketplace, general, introductions.
2. Select the posts on screen and copy them.
3. Paste them in:

```bash
pbpaste | circle-leads triage --community flutter-devs --name "Your Name"
```

or save to a file first:

```bash
circle-leads triage --file posts.txt --community flutter-devs --name "Your Name"
```

## What you get

```
Read 5 post(s): 3 lead(s), 2 not-lead, 0 filtered, 0 duplicate(s), 0 seen before.

====================================================================
82  HIGH
From:      Priya N  (yesterday)
Needs:     Flutter Dev
Skills:    Flutter
Post:      "Anyone know a good Flutter dev? We're rebuilding our booking app
            and our current contractor just dropped out."

  Draft reply:
    Hi Priya — saw you're looking for a flutter dev.
    I work with Flutter and have built and shipped this kind of thing before.
    What does the scope look like, and do you have a budget range?

    — Your Name

====================================================================
74  MEDIUM
From:      Dana Ops  (2h ago)
Skills:    Flutter
Budget:    $20k
Urgency:   High
Post:      "We need someone to build our iOS and Android app. Budget around
            $20k for the first milestone, start ASAP. Flutter preferred."

  Draft reply:
    Hi Dana — saw you're looking for Flutter work.
    I work with Flutter and have built and shipped this kind of thing before.
    How soon do you need someone starting?

    — Your Name
    ! Marked urgent — reply soon or the moment passes.
```

The job seeker ("I'm a Flutter developer looking for work") and the release-note
post were dropped automatically. That filtering is the point: you skim three
ranked leads instead of fifty posts.

## What it handles for you

- **Splits pasted text into posts** using Circle's `Name · 2h ago` bylines,
  explicit `---` separators, or blank lines.
- **Strips UI chrome** — "12 likes  4 comments", "Reply", "Share".
- **Reads relative timestamps** so recency scoring works ("2h ago" → today).
- **Remembers what you've seen.** Paste the same screen twice and it reports
  `5 seen before` rather than duplicating leads. Safe to re-paste a page you
  scrolled further down.
- **Drafts an opening reply** naming what they asked for and asking one real
  question. Edit before sending — it's a starting point, not a send button.
- **Feeds the same database**, so `search`, `export`, `stats` all work on
  triaged leads.

## Useful flags

```bash
--community <slug>     which community this came from (keeps leads separate)
--space <name>         which space, for your own notes
--url <link>           link back to the thread so you can return to it
--name "Your Name"     signs the reply drafts
--min-score 50         only show leads worth your time
--no-replies           just the list, no drafts
--use-llm              escalate ambiguous posts to the model
```

## Two honest limits

**It only sees what you paste.** There is no background collection — this is a
tool for reading faster, not a crawler.

**Reply like a member, not a marketer.** Most communities allow helpful replies
in-thread and dislike cold DMs. Read the room's rules first; a useful answer
that happens to mention you can help beats a pitch every time.

---

# Part 4 — Tuning

Everything lives in `circle_leads/config/requirements.yaml`. No code changes.

```bash
circle-leads config          # show what's active
```

## Change what you're looking for

```yaml
target_roles:
  - Flutter Developer
  - React Native Developer

target_skills:
  - Flutter
  - Dart
  - Firebase
```

Then re-run `classify`. Real output when the demo set is re-classified against
a mobile-only config:

```
default config:      4 lead(s), 3 not-lead, 0 filtered
mobile-only config:  3 lead(s), 3 not-lead, 1 filtered   ← backend/Python lead dropped
```

```
Lead Score: 81 HIGH    Role: Flutter Developer      Wants: individual developer
Lead Score: 58 MEDIUM                               Wants: software agency
Lead Score: 49 LOW                                  Wants: technical cofounder
```

The Flutter role match ranks first. The agency and cofounder requests still
qualify — an engagement request is a hiring lead whether or not it names a
role — but they score lower, so ranking keeps them out of your way rather than
a filter hiding them. To exclude them entirely, filter on
`--min-score` or `--priority HIGH`.

## Loosen or tighten

```yaml
minimum_confidence: 0.80     # lower → more leads, more noise
llm_escalation_threshold: 55 # lower → more posts sent to the model

priority_thresholds:
  high: 80
  medium: 50
```

## Reweight scoring

```yaml
scoring:
  hiring_intent: 40
  target_role_match: 20
  target_skill_match: 15
  budget_mentioned: 10       # raise if budget matters most to you
  company_identified: 5
  recent_post: 10
  recency_days: 7
```

## Extra keyword signals

```yaml
keywords:
  include: [hiring, "need a developer"]
  exclude: ["open to work", "portfolio"]
```

These layer on top of the pattern rules — an excluded keyword subtracts 30.

## Privacy and rate limits

```yaml
excluded_content: [direct_messages, member_bios, email_addresses, phone_numbers]
retention_days: 30

rate_limit:
  requests_per_minute: 60
  max_retries: 5
  backoff_base_seconds: 2.0
```

Re-running `classify` re-scores stored content without re-fetching it, so
tuning is cheap. Use a separate config with `--config other.yaml` to compare.

---

# Part 5 — Troubleshooting

**"No permission files found"** — you haven't created one yet. Copy the
template into `circle_leads/config/communities/`.

**`SKIP <community>: permission_status='candidate' (needs 'approved')`** —
working as designed. Set it to exactly `approved` once the operator agrees.

**"No approved spaces matched"** — `allowed_space_ids` is empty or the IDs
don't match. List them with `circle-leads -v ingest` and check the log, or ask
the operator which spaces they meant.

**"Access denied (401)"** — the token is missing, wrong, or revoked. Confirm
the env var name in the permission file matches your `.env`, and that you
re-sourced it. The run stops rather than retrying, by design.

**"Missing credential"** — the variable named in `admin_token_env` isn't set.
`set -a && source .env && set +a`.

**Rate limited** — lower `requests_per_minute`, use `--request-budget`, and
prefer incremental runs. Circle counts 429s against your monthly allowance, so
an aggressive retry loop costs twice.

**A lead looks wrong** — check `Evidence` and `via rules`/`via llm` in the
search output. If the rules got it wrong, add `--use-llm`. If a pattern is
systematically wrong, the rules are in
`circle_leads/classifier/keyword_rules.py` and every spec example is covered by
`tests/test_classifier.py`.

**Nothing classified** — `circle-leads stats` shows unclassified count. If it's
zero, everything is already done; content is only classified once unless edited.

**Reset everything** — delete `data/circle_leads.db`. It is rebuilt on the next
run.

## Where things live

```
circle_leads/config/requirements.yaml       what you're looking for
circle_leads/config/communities/*.yaml      who approved what (gitignored)
.env                                        tokens (gitignored)
data/circle_leads.db                        SQLite store (gitignored)
exports/                                    CSV/JSON output (gitignored)
docs/operator_request.md                    approval template + stop conditions
```

## Stop conditions

Stop rather than work around any of these:

- Approval was never granted, or has been withdrawn.
- An API returns 401 or 403, or a space shows a lock screen.
- Access would need copied cookies, undocumented endpoints, CAPTCHA bypass, or
  extra accounts.
- The only available content is a DM or a small private group.
- Community rules prohibit solicitation or automated analysis.
- An operator or member asks for deletion — run `purge --community <slug>`.
