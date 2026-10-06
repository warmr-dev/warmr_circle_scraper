# October 6 runtime recovery

## Observed causes

Read-only inspection of the production Needs attention page and durable state:

- The worker was inside harvest while enrichment and join-type finish timestamps
  remained on October 5. Scheduled community work and classification retry shared
  the same blocking loop with harvest and scan jobs.
- One attempted ingest returned a 30-second read timeout. This is unknown delivery,
  not a confirmed rejection. Retry must reconcile the same stable message identity.
- FreelanceMVP returned feed HTTP 403. The last connected scan was about 36 hours
  old and said `No readable posts (10 space(s) visible)`. Its stored three cookies
  were readable on the new host, last refreshed September 18. Key rotation was not
  the cause of cookie decryption failure for this community. Current authenticated
  membership/read access remains unverified; no reauthentication was performed.
- The generic `error: post <id>` card grouped distinct classification failures.
  Most were inconsistent demand ownership/signal, some unverified source evidence.
  Errors remained retryable and did not create exportable candidates.

## Safe validation

The existing Circle OpenAI backend and existing key were reused: `gpt-4o-mini`,
no separate key. Three currently failing captured posts were evaluated read-only,
with complete available thread context. Two supplier replies became NOT_LEAD on
repeat evaluation. A short acknowledgement inherited another participant's
interview-feedback request and failed the current-post evidence check. After the
prompt clarification, all three returned grounded NOT_LEAD with current-source
excerpts. No production decision or lead was written by these evaluations.

The final prompt (`ai-v3.1`, policy `commercial-demand-v1`) was also evaluated on
all nine source fixtures from the prior approved evaluation. All nine matched the
explicit labels, with no model errors. The five required positives and full Civic
Shout created candidates and locally serializable export payloads; the AI tutorial
and the two negative submissions created none. Local SQLite replay used the exact
recorded model replies, blocked ingest/notifications/HTTP, and froze capture time.
All historical source examples are expired at current time; timestamps were not
rewritten and nothing was replayed into production.

| Source case | Decision | Local export at capture |
| --- | --- | --- |
| Denver videographer | LEAD | eligible |
| PLG/product consultant | LEAD | eligible |
| Specialist/leadership hiring | LEAD | eligible |
| Webflow | LEAD | eligible |
| Part-time webinar support | LEAD | eligible |
| Civic Shout full captured post | LEAD | eligible |
| AI tutorial captured post | NOT_LEAD | excluded |
| AI tutorial submission | NOT_LEAD | excluded |
| Civic Shout short submission | NOT_LEAD | excluded |

Original private content/model replies remain ignored local evidence. Existing
100-post cohort results belong to the previous `ai-v3` evaluation; that full
cohort has not been rerun for this focused runtime patch. These model checks are
bounded observations, not a guarantee that future provider replies are valid.

Verification: targeted classifier/pipeline/worker/attention/export checks passed
(206 tests); full Python suite passed (1568 tests, 5 pre-existing optional skips).
Python compile, shell syntax and final diff checks passed. The new systemd lanes
are prepared in code; merge, service installation, rollout and live recovery have
not yet been performed for this patch.
