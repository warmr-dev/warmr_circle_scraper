# Commercial-demand boundary validation

## Current preparation - 2026-10-20: BLOCKED, do not merge or deploy

PR #52 is refreshed onto main `7e8e099d68e77227e5b059f94aa03812d988d64c`.
The `ai-v3.3` experiment is **not a release recommendation**. The prior two
failed regression fixtures now pass, but the full repeat rejects this candidate:
**37/40 original regressions**, **47/52 labelled checks**, **5 candidate processing
errors** (4 in the100-root cohort), and new false positives/false negatives.
The previous ai-v3.2 regression result was38/40; this candidate is worse on that
fixed set and must not be promoted despite successful offline unit tests.

### Original six blockers

| Case | Frozen result | Acceptance |
|---|---|---|
| Full Civic contract fixture | LEAD with local audit/payload | PASS as a fixture; cohort22314 independently fails exact quote validation |
| Friend searching for jobs | NOT_LEAD with negative audit/no payload | PASS |
| Supplier audience research22996 | NOT_LEAD | PASS in both targeted and cohort forms |
| Chosen-assistant usage22514 | NOT_LEAD | PASS in both forms |
| Survey22307 | LEAD | FAIL, false positive persists |
| Affiliate tutorial20305 | LEAD | FAIL, false positive persists |

### Complete repeat

152 evaluation records cover52 labelled cases and100 frozen roots. Four hard
negative source cases also appear in the cohort:148 distinct source/metadata/
context inputs,141 distinct text/context combinations, **not152 independent
captures**. Cohort:35 eligible,4 candidate errors,20 changed decisions versus
the prior PR52 response set. Every changed decision has a dated assessment in
`changed-decisions.md`; unchanged/unlabelled rows are not silently promoted to
human ground truth. At least14 policy false positives are identified in the
current35 eligible cohort decisions, plus a separate leadership boundary case.
No complete human-label precision estimate is claimed for the100 roots.

The five labelled failures are executive-publishing strategy (false negative),
a supplier-offer contradiction (error), generic office hiring (false positive),
the survey source and affiliate tutorial source (false positives). The8 new
short paired paraphrases pass, illustrating why paraphrases cannot replace full
captured sources and context. Errors are never accepted as correct negatives.
Historical baseline0268 has one additional baseline-only processing failure;
the evaluator reports6 combined errors, while the candidate has5.

The original PR52 classifier hash is verified as
`c867e3fd9fe0005ed0971467bad5a53413ca066cbad8dec346a320f7b6199ba6`.
Final evaluated classifier hash:
`a973e7384563a58ef040c891913dc50fb98e5a593fe9943e48e16e0c275e2a05`.
See `acceptance-2026-10-20.json` for all152 sanitized result rows/input hashes.
Private full inputs, failed/interrupted variants and raw responses remain
ignored and access restricted. No private text, names, quotes or URLs are added
to the public report.

### Mechanics and boundaries

1611 offline tests passed,5 skipped when excluding the unchanged launchd
installer file. Its1 failure and4 errors reproduce on clean main.147 focused
classifier tests pass. Compilation, workflow YAML and diff whitespace checks
pass. The new workflow exercises classifier/retry/attribution/export mechanics;
it does not run private real-model acceptance or require credentials.

The runtime parser retains exact evidence checks. Strict OpenAI JSON Schema
prevents unsupported enum/excerpts shapes on this one existing model call;
it does not make its commercial judgments reliable. The model/provider,
thresholds, single-call architecture and provider budget are unchanged.
No model upgrade, keyword blacklist, case-ID exceptions or guard relaxation
was used to manufacture a pass. No alternative model benchmark was run.

**Decision:** retain draft and semantic gate failure. Do not merge/deploy this
candidate. The next useful investigation is an explicitly approved bounded
model/contract comparison on the same source/contrast set, not another blind
prompt release. Existing production source/configuration and queues are untouched.
No historical replay, ingestion, Slack message, migration, activation or OpenClaw
routing change occurred. Local/PR preparation only; no new live behavior claim.

Canonical runbook: `docs/commercial-demand.md`.
Delivery record: `docs/changes/2026-10-20-circle-demand-acceptance.md`.

---

## Historical ai-v3.2 evidence (2026-10-06)


The `ai-v3.2` prompt distinguishes buyer solution exploration from generic
professional curiosity, supplier networking and engagement questions appended
to tutorials. It first describes `request_scope` inside the same semantic call;
conflicts between that scope and demand labels are retryable errors. The supplied
scope is retained in the existing audit description, without a schema migration. It preserves free advice about a concrete company project as
commercial demand. Admission rules, confidence floor, model, single-call
architecture, attribution, freshness and export contracts are unchanged.

Eight anonymous adjacent/paraphrase fixtures were added after retrieving the
complete captured posts and available replies. Their provenance contains source
and context hashes; the private originals remain outside Git. A career-only
variant is deliberately different from an actual request to build the marketing
function of an existing team. The latter is a borderline commercial-consulting
case, not an additional human-confirmed label.

## Evaluation method

Use Circle's existing OpenAI `gpt-4o-mini`, temperature 0 and the existing key.
The same 100 unique Exit Five roots captured September 28 through October 5 UTC
are compared with the retained baseline responses from `0268e44670804036cb5faa3a367c5a04bdb960da`.
The cohort window is capture time, not publication time. Full source text and
available context are supplied; no source timestamps are rewritten.

The runner now defaults to one worker. Concurrent evaluations previously hit
the account's 200,000 TPM limit. Those attempts are retained separately as
provider failures, never counted as non-demand. Explicit `--retry-errors` retries
only failures and archives their earlier receipts. This is offline evaluation,
not production retry or replay.

Local SQLite replays the exact model replies through candidate creation and
payload serialization. HTTP, ingest and notifications are forbidden. Historical
fixtures are checked at frozen capture time and separately reported expired at
current time. Private receipts include old/new reasons, literal excerpts and
source references. The accompanying review lists every changed decision and
separates errors from semantic outcomes.

## Already deployed runtime repair

PR #51 was merged at `d1222e518eb1b212251f96fc919d1619d811dcc4` and deployed on the
new NYC host. Worker, watcher and recovery report this release and have active
systemd services. Community maintenance also runs that installed release; its
first long batch started before a stale `WARMR_CODE_VERSION` override was
removed, so its cached version metadata remains old until its next restart.
The HTTP governor remains 20 requests/minute and 600/hour, shared with harvest.
At 18:13:56 UTC the first enrichment batch finished: 200 visited, zero named or
described, 200 nothing-found, cursor advanced to 10653; ICP then began. This
proves schedule execution, not recovered metadata or membership. At 18:17:44 UTC
ICP finished: 180 checked, zero flagged; join-type probing then ran. A running stage
waiting for the shared budget does not prove the backlog drained. Do not reset
last-run markers or stop a batch to hide warnings.

Natural recovery created candidates 264-267: 264 was accepted as a new canonical
Warmr lead; 265-267 reconciled as duplicates. The canonical record
`04e01fca-3b79-446a-ad4e-36fcfee132da` was directly read in downstream Warmr.
A previous transport timeout reconciled before PR #51 rollout and is not credited
to that rollout. No migration or historical replay was performed.

FreelanceMVP still returns HTTP 403 and needs a fresh session from an account
with active membership. Its stored cookies are readable; this is an access issue,
not a missing environment key. The attention page now shows that distinction.
Old classification errors remain in the rolling 24-hour log even after a
successful retry; they are not hidden or marked seen by this work.

The semantic change in this report is local until its own PR is approved and
rolled out. PR #51 approval does not imply this later prompt has been deployed.

## Readiness and live attention readback

This follow-up remains **draft, not approved for deployment**. Unit/mechanics
checks passed: 1589 tests, 5 skipped; `git diff --check` and compilation passed.
Mock success is not evidence of semantic acceptance. The last real-model run
and every changed-decision assessment are recorded below and in
[changed decisions](changed-decisions.md). No private post bodies, author names,
contact URLs or literal community excerpts are published in this report.

Latest browser readback on October 6: critical 0, warning 1, info 4. Both queue
stall warnings cleared after maintenance execution. FreelanceMVP access remains
blocked. A new YourSpinState cookie-expiry entry is a separate capture/access
failure; classifier retries cannot restore that session. Classification cards
are a rolling 24-hour history, not a count of currently outstanding jobs.

The SSH agent lifetime expired during final checks; reloading the existing ops
key requires its passphrase in the user's Terminal. No new access grant or key
rotation is needed. The latest browser state is verified; a newer server/runtime
snapshot and an idle community-service version refresh remain unverified.

## Final offline evidence - not release-ready

Final classifier hash:
`c867e3fd9fe0005ed0971467bad5a53413ca066cbad8dec346a320f7b6199ba6`.
140 paired cases were evaluated: 100 captured roots and 40 source/paraphrase/
adjacent cases. The regression checks pass 38/40. All five original confirmed
positive submissions reach local candidate creation and serialization; both
required negative submissions remain excluded. The separate full Civic contract
has an evidence-validation error, and a job-search-for-a-friend case has a
scope/label conflict. Neither creates an exportable candidate. These are failed
acceptance checks, not successful negative classifications.

The 100 roots yield 26 eligible demand decisions, 72 non-demand and 2 retryable
processing errors; the original baseline had 1 eligible. There are 27 changed
baseline classifications. Reviewing the positive decisions found **4 false
positives**: supplier discovery, research survey participation, a chosen-assistant
showcase and an affiliate tutorial/supplier offer. This is why the follow-up
remains draft despite successful unit tests. One cohort error is expected demand
(specialist hiring); the other is a showcase. Across the entire paired run,
4 candidate-model errors and 1 retained baseline-only error are counted separately.
Earlier failure attempts remain archived privately, including provider throttles.
The changed-decision table also covers changes against deployed ai-v3.1 and every
remaining cohort error; it must not be interpreted as all changes being accepted.

Freshness, capture/session failures and duplicate reconciliation are separate
from these semantic outcomes. A captured root can be recent by capture time yet
expired by publication time. Nothing in this evaluation authorizes current
outbound delivery of the historical cohort.
