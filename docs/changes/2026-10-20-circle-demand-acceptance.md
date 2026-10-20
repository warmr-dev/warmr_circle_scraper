# Circle #52 - frozen semantic acceptance refresh

Preparation only. No merge, deployment, migration, production classification,
historical replay, ingest, notification, Slack publication or OpenClaw routing
change is included.

## Source and mechanism

The existing draft's history is preserved, integrated locally with current main
`7e8e099d68e77227e5b059f94aa03812d988d64c`, including the canonical-host fix.
`ai-v3.3` summarizes the complete post and latest situation before assigning
scope. It adds an independent semantic need-state assessment, keeps lifecycle/
scope/description contradictions retryable, and reiterates the fixed assessment
contract after the source/context data. There are no case-ID exceptions, domain
blacklists or category admission whitelists.

The direct OpenAI commercial-demand request uses strict native JSON Schema for
its existing enums and excerpts array. Same `gpt-4o-mini`, temperature0,
45-second provider timeout, output budget, single semantic call and sequential
evaluation. Other OpenAI prompts and other provider request contracts do not
receive that schema. Local validation remains authoritative: exact current-post
quotes, supported facts, confidence, awareness and cross-field consistency.
Native schema prevents shape errors; it does not prove correct buyer intent.

## Failure analysis

- Full Civic contract: prior positive intent was rejected correctly because a
  supporting quote was paraphrased. New prompt asks for one short exact span;
  no fuzzy quote match or guard bypass is added.
- Friend seeking a job: represents a candidate selling labour, not a buyer
  hiring a specialist. Keep this distinct from a friend's employer vacancy.
- Prior four false positives: supplier audience research, survey participation,
  selected-assistant usage and an affiliate tutorial/supplier offer. Preserve
  separate concrete buyer needs in mixed posts; new contrast cases cover them.

## Evaluation provenance

Original40 labelled regressions +4 reviewed false-positive sources +8 paired
paraphrases +the same frozen100 roots =152 records,148 unique inputs (four
source negatives are also in the cohort). The prior PR52 receipts are verified
against the original ca6c567a classifier hash; historical baseline0268 receipts
remain a separate diagnostic comparison, not a newly inferred current-main
baseline. No current database labels are treated as ground truth.

All complete text, source context, identities, URLs, raw model outputs and
unsuccessful prompt receipts remain in ignored private files. Earlier pilots
and the interrupted pre-schema broad run remain unsuccessful/incomplete evidence;
only the final immutable classifier hash is used for the final acceptance count.
Output errors never count as correct negatives. Local candidate/audit/payload
verification uses temporary SQLite at frozen capture time, export=False and
blocked ingest/notifications/requests; expired-now status is separate.

Final results are recorded in the dated acceptance report and summary artifact.
A failed semantic gate leaves this PR draft; passing unit tests cannot override it.

## Verification boundaries

Offline full-suite failures in the macOS launchd installer reproduce on clean
main:1 failed,4 errors. Those files are unchanged by this PR. All other tests
are run separately; exact counts and hosted CI/preview state are reported in
final evidence. No broad OS/service fix is included in this classifier task.

## Release and rollback boundary

Local/committed/PR preparation only. Merged:not done. Deployed/Applied:not done.
Live verified:not done for this change. Workspace closed:pending decision.
Before any separately approved release, semantic blockers must be closed on an
immutable tested head; refresh reviews, main, source/runtime/model configuration
and complete package checks. Do not change model, routing or credentials merely
to make evaluation pass. A new model/provider requires a separate decision.

Use the repository's reviewed Circle deployment path only after exact-head merge
and release approval. No backlog reset, policy-version requeue, timestamp change
or replay is authorized. Verify natural new traffic through durable classifier
receipts, candidate/export status and canonical Warmr acceptance separately.
On regression stop rollout and prepare a scoped source revert on current main;
obtain approval for that rollback. Keep existing access/configuration unchanged.

Canonical runbook:docs/commercial-demand.md.
Acceptance report:reports/demand-boundaries/README.md.

## Final acceptance outcome

BLOCKED:37/40 original regressions,47/52 labelled checks,5 candidate errors
(4 cohort),35 eligible cohort decisions and20 changed decisions versus prior52.
Two original failing fixtures pass; two of the four original source false
positives persist, and new regression cases fail. The original Civic post still
has a quote failure in its separate cohort form.14 policy false positives are
identified among the current cohort positives. This candidate is not recommended
for merge/deploy. The local suite result is1611 passed/5skipped, with the unchanged
launchd1failure/4errors independently reproduced on main;147 focused classifier
checks passed. All prior unsuccessful pilot receipts remain private evidence.
