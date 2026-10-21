# Bounded classifier repairs

Repeated invalid model responses previously retried every hour without feedback
or a terminal review state. Some posts accumulated hundreds of failed attempts.
This change uses the existing provider and keeps all admission/evidence checks.
The subsequent grounded-evidence response/prompt candidate is documented in
`2026-10-21-grounded-classifier.md`; it has not passed final semantic acceptance.

## Behavior

- At most two model calls per evaluation: the initial response, then one repair
  for invalid JSON, schema, conflicting intent fields or evidence errors.
- The repair gets the original current post, author metadata and full context,
  plus the validation error, bounded rejected excerpts and prior intent fields.
  It must return a complete response. The same validator checks both responses.
- Source IDs, literal quotes, current-post evidence and demand ownership remain
  required. There is no synthesized quote, permissive fallback or automatic
  conversion of a validation failure into a negative decision.
- If adding feedback would exceed the existing input limit, skip the repair and
  retain an explicit diagnostic instead of sending an oversized request.
- Exhausted validation becomes `outcome=error`, `classified=false`,
  `retry_state=needs_review`, with no scheduled retry. It cannot export or become
  a successfully completed classification merely because retries stopped.
- Batch selection excludes held rows before applying its limit. Unchanged
  recaptures also respect the hold. This prevents both repeated spending and
  starvation of other posts. New/updated source content can be evaluated again;
  direct intake also detects changed context. A context-only change does not
  automatically requeue a held row in the scheduled batch.
- Provider/transport errors do not get immediate corrective calls. They retain
  the existing durable 1/5/15/60-minute backoff, including an outage during repair.
  Thus the two-call bound is per evaluation, not a lifetime provider-outage budget.

## Diagnostics and review

The post audit and append-only classification activity retain bounded structured
attempt diagnostics: response SHA-256, error, prior intent/schema fields, rejected
quote/source/reason and excerpt source IDs. Rejected quotes never enter approved
`excerpts`. Full raw responses, provider headers and exception bodies are not
added to diagnostics. Existing top-level provider error reporting is unchanged.
`model_calls` counts calls in this evaluation, separately from the existing
historical `attempt` counter. `validation_policy=corrective-v1` identifies the
new behavior. Activity keeps its credential-key filter and bounds nested details
without turning the whole diagnostics array into a truncated string.

Needs Attention has a persistent classification-review warning, independent of
the 24-hour log window. Inspect `posts.classification_audit` and the post's
classification activity before authorizing any selective release. A reviewed
retry, prompt/model change, or historical replay needs a separate explicit
operational action; this patch does not bulk-release held posts.

## Delivery and verification

No schema migration, credential change, replay or deployment is included. Existing
legacy errors receive the bounded policy on their next normally due evaluation.
Tests use deterministic responses and local SQLite to cover correction, strict
rejection, provider outages, durable holds, batch selection, source updates,
structured diagnostics and persistent review visibility. They do not prove that
all original incident posts will produce valid model responses in production.

After a separately approved deployment, verify real recovery outcomes and actual
Slack publication. Keep held posts visible as unresolved. Rollback is a code
rollback; old code does not honor `needs_review` and may resume retrying held
rows, so review that behavior before rollback rather than automatically replaying.
