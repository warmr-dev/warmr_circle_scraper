# Acceptance evidence (local implementation; draft PR)

Baseline Circle: `0268e44670804036cb5faa3a367c5a04bdb960da`.
Scout reference: `c4d9dfffa193aea05e1929668dec34946f79f5d5`;
`classifier.py` and `council.py` verified against available GitHub HEAD and local
reference files. Circle adopts the commercial policy with one semantic call.

## What changes

| Stage | Baseline | New behavior |
|---|---|---|
| Keyword prefilters | Negation, seeker and low score can terminate before LLM | Diagnostic signals; mixed posts reach semantic evaluation |
| Semantic rule | Software work plus restricted engagement description | Buyer with an explicit commercial demand signal in any category |
| Text/context | Root excerpt truncated at 6,000 characters | Complete current post plus available root/replies; inspectable input-limit error |
| Admission | Batch roles/skills gating differs from triage | Shared evaluation and confidence admission; score/category only prioritize |
| Provider failure | Rules fallback, held candidate, classified terminally | Durable error, no new candidate, no retirement, bounded retry |
| Negative receipt | No durable post audit; logging depends on path/config | JSON audit and transactional append-only activity receipt |
| Export | Candidate/legacy state governs retry drain | New audit must be successful/current/eligible; legacy contract preserved |

## Source and cohort provenance

`cohort-manifest.json` pins 100 unique Exit Five root posts, IDs, capture times
and content/context hashes. Capture boundary: 2026-09-28 through 2026-10-05 UTC.
The complete source snapshot and replies were retrieved read-only in small batches.
Private transcripts, source fixtures and raw provider responses remain local in
`private/` and gitignored `decisions/` because this GitHub repository is public.
Public CI uses synthetic paraphrases; local regression tests additionally use
all nine retrieved source cases and their explicit human labels.

The five requested positives must reach candidate creation and payload
serialization with original source times at original capture. AI tutorial and
short Civic Shout submissions remain negatives. The full Civic Shout capture is
separately positive (freelance contract); this is the user-approved distinction.
The tutorial's full capture is also tested as a negative.

## Verification status

Full Python suite on the final local diff: **1544 passed, 5 skipped**. The skips
require an installed Chromium binary. `compileall` and `git diff --check` passed.
Mock tests establish mechanics; the semantic evidence gap below remains.

The existing Circle model is **GPT-4o-mini**. Read-only worker runtime reports
`openai/gpt-4o-mini` through OpenRouter. The existing Circle Vercel validator key
uses direct OpenAI with the default `gpt-4o-mini`; no separate key is required.
The local runner retrieved that existing credential in memory using read-only
Vercel API calls. No credential was saved in fixtures, logs or this repository.

The final classifier was evaluated on **109 cases** using the existing Circle
key: nine retrieved source cases and 100 roots. **7/9 source cases pass** strict
classification and temporary-database export eligibility. Four of five required
positives pass; Morning Consult specialist/leadership and the separately positive
full Civic contract fail exact supporting-excerpt validation. Both required
submission negatives and the full captured tutorial remain excluded. A separate
23-case paraphrase/boundary evaluation passes **23/23** under the new classifier;
one old-baseline processing error is reported separately.

On 100 roots, baseline eligibility is **1**, new eligibility **20**. Every one
of **26 changed outcomes** is reviewed: **19 newly eligible demand cases** are
accepted and **seven processing errors** remain (three expected demand, four
expected excluded). No newly eligible false positive was found in this reviewed
cohort. Errors are not semantic negatives. This is an observed paired evaluation,
not a population accuracy estimate. Original source timestamps remain unchanged;
current-time checks separately report expired ingress.

Final classifier hash and acceptance gap are recorded in `evaluation-status.json`.
Earlier prompt-calibration attempts and raw responses remain archived privately;
successful baseline responses were reused and errors alone retried. A further
retry of the two source errors was rejected by automatic approval review despite
user confirmation, citing recipient-specific authorization for private texts to
direct OpenAI. That rejected call did not execute. PR remains draft until all
required source positives pass local export eligibility. Worker/provider route
parity is not claimed: worker uses OpenRouter, this evaluation uses direct OpenAI.

The latest available rolling 24-hour capture window is
2026-10-04 10:24:07.013272 through 2026-10-05 10:24:07.013272 UTC:
five records (one social root and four replies). Manual source/context review
found no explicit buyer demand. This is anchored to the last available capture,
not today's live 24 hours; capture freshness is a separate issue.

Read-only source state has the seven cited captured posts marked classified and
without local candidates. Historical negative receipts were unavailable, so an
exact original rejection cause cannot be reconstructed from that flag. The
baseline simulation reproduces multiple software-only suppressions; Webflow was
accepted by the simulated baseline, so its historical absence needs separate
capture/processing/duplicate investigation rather than a fabricated explanation.

PR remains draft until final semantic regression/cohort verification passes.
No production database mutations, migrations, deployment, replay, ingest,
notifications, merge or live delivery verification were performed.
