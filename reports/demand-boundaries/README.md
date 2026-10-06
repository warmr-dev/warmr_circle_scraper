# Commercial-demand boundary validation

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
