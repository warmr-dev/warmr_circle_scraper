# Commercial demand policy

Circle uses one semantic call for the complete current post and available thread
context. Policy `commercial-demand-v1` follows Daniel's Scout classifier and
Council criteria verified at `c4d9dfffa193aea05e1929668dec34946f79f5d5`.
It does not adopt Council's multi-model topology.

## Included demand

Current or future buyer demand in any commercial category: provider requests,
recommendations, comparisons, switching, concrete commercial problems, solution
exploration, and specialist/leadership hiring that creates recruiting, staffing
or service demand. Marketing, SEO, creative, product/UX, Webflow, webinar support,
videography and recruiting qualify. Budget, timeline, vendor availability,
freelance/part-time format and early-stage readiness do not determine rejection.

Informational tutorials, supplier promotion, showcases, social discussion,
career advice, closed needs, and generic in-house hiring without a commercial
service signal remain excluded. Keywords are diagnostic signals; configured
exclude keywords, job-seeker words and negation cannot reject mixed posts before
semantic evaluation. The existing exact recruitment-scam template remains
excluded. Roles, skills and category affect routing/priority rather than admission.
The configured confidence floor still applies; its outcome is
`filtered_confidence`, distinct from `not_lead`.

## Context and evidence

`thread_id` associates posts within the same community. Legacy Circle `/c/`
permalinks associate the root and comment fragments within that community.
Triage includes available records before all replies have been persisted.
Current and context sources carry post/record identifiers, author identifiers
and URLs. A supplier replying to buyer demand is evaluated as that supplier's
record and cannot inherit the root's buyer attribution.

The complete input is serialized without the former 6,000-character truncation.
A 100,000-character conservative input guard yields `context_limit_exceeded`;
provider context-limit responses also become processing errors. Inputs are not
silently shortened. This guard is a character bound, not a tokenizer-derived
context-window guarantee.

Both positive and negative model responses need a reason and grounded excerpts,
including an excerpt from the current record. Quotes must occur in their named
source (case/whitespace normalization is allowed). Semantic `service_direction` and `post_purpose` distinguish buyer requests from
supplier offers, tutorials, surveys, networking and career advice. Mixed content
requires an actual current-author need. Positive responses include
`demand_signal` and awareness 1–5. Descriptive fields and extracted facts remain;
budget and company must be supported by the current text.

## Durable decisions and retry

`posts.classification_audit` stores the latest outcome, reason/error, source
references/excerpts, policy/classifier/model versions, content/context hashes,
confidence, awareness and attempt time. Every attempt also appends a `classify`
activity receipt in the same transaction, regardless of `verbose_log`.

Provider failures, timeouts, malformed responses and unsupported excerpts leave
`classified=false`, preserve an existing successful lead and prevent its export
until a successful current classification. `classification_retry_at` schedules
1, 5, 15, then 60 minutes between attempts. The existing worker checks once per
minute and processes at most 25 due error rows in oldest-due order. No model key
when semantic processing is requested also remains retryable. Without a requested
semantic backend, only conservative explicit commercial-request grammar can
create a lead; ambiguous records require semantic processing.

The additive migration has no backfill and does not reset historical decisions.
Policy-version differences alone never enqueue old captures. Changed content
clears its old retry delay and can be evaluated normally. Export payload,
identity, attribution, freshness contract and duplicate reconciliation remain
unchanged. A new audit must be a successful eligible lead; legacy null-audit
rows retain their existing export contract.

## Local verification and release boundary

`reports/commercial-demand/private/regressions.json` contains retrieved source
texts, context, provenance and explicit human labels. This repository is public:
private transcripts and raw model responses are gitignored.
`tests/fixtures/commercial_demand.json` contains synthetic paraphrases for public CI.
Local tests additionally load the private source fixtures when present. Database lead statuses are not labels.
Five confirmed positives are included, along with the tutorial and short Civic
Shout submissions as negatives. The **full captured Civic Shout post** is a
separate positive: a 1099 freelance growth-marketing contract, 10–15 hours/week,
$100/hour. Its short submission only contains an informational introduction.

Mock tests verify response validation, both pipelines, candidate creation,
payload serialization, context attribution, retry, negative audit, confidence,
additive schema handling and existing duplicate behavior. Mock responses do not
establish real-model semantic accuracy.

`scripts/evaluate_commercial_demand.py` compares the pinned baseline with the new
classifier on the nine fixtures and frozen 100-root cohort, using one provider and
model configuration. It records raw responses, errors and old/new eligibility.
A temporary SQLite database runs real candidate creation and payload serialization;
ingest and notification functions are blocked, `export=False`, and source times
are preserved. Decision time is frozen at original capture for regressions.
`--local-only` replays already cached model responses without provider calls.
Current-time expired ingress is reported separately; no old timestamp is rewritten
to admit historical demand into live ingestion.

Run only after authorization to disclose these private texts to the chosen model
provider. Reuse Circle's existing model and credential; a separate API key is not
required. Worker runtime uses `openai/gpt-4o-mini` via OpenRouter; the Vercel
validator uses `gpt-4o-mini` directly via OpenAI. Match or explicitly authorize
the chosen route before sending private texts. For example, with the existing
Circle OpenAI credential available in the shell:

```sh
.venv/bin/python scripts/evaluate_commercial_demand.py \
  --provider openai --model gpt-4o-mini
```

Alternatively, `--vercel-project PROJECT_ID --vercel-team TEAM_ID` reads the
selected Circle project production credential in memory using Vercel CLI GET
requests; it never persists or prints the key. This requires existing CLI access
and does not grant permission to disclose post content to a different route.

Provider access is checked before cohort inference requests. `--retry-errors`
retries cached processing failures while retaining previous receipts; it cannot
be combined with local-only replay. Caches require matching model, provider,
baseline, classifier hash and input hash. A classifier change archives the old
receipt and reevaluates only the new policy, reusing the successful fixed
baseline. Local-only mode refuses stale-policy caches.
Review every changed cohort decision, including excerpts and reviewer assessment,
and separate provider errors from semantic changes before marking the PR ready.
No merge, deployment, production schema application or replay is part of this PR.
