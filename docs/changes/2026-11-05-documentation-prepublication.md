# Documentation validation before publication

Date: 2026-11-05

## Behavior and reason

Validate the exact saved PR body and current committed diff before push/create/edit.
Use one canonical documentation validator locally, remotely and in Actions.
After publication, attest and read back current head/base/body. Guard merge with
fresh validation and the explicit reviewed source SHA; never rely on an old status.
Runtime/configuration changes require a separate changed runbook and delivery record.
Legacy declaration syntax is preserved. Hosted CI startup failures are reported
separately from documentation errors.

## Verification

- Existing documentation fixtures and 23 new offline workflow/validator tests passed.
- A real temporary Git repository proved malformed publication stops before push/GitHub.
- Shell syntax and final diff checks passed.
- Guarded publisher created https://github.com/warmr-dev/warmr_circle_scraper/pull/61 at source
  `971c0ce6e8573ab123748e27f04fd400cc598ac3`; `documentation-local` was posted and read back successfully.
- `gh pr checks 61 --repo warmr-dev/warmr_circle_scraper --required` identified `documentation-local`
  as required and passing. Strict base freshness and admin enforcement were
  verified via branch protection API; force pushes/deletions remain prohibited.
- Warmr protection was preserved. Scout/Circle protection was added only after
  the respective publisher succeeded. No existing checks or reviews were removed.
- PR body updates and merge dry-run are revalidated on the final PR head; see
  current PR verification and immutable commit/status receipts.

## Delivery

- Local and committed: implemented as `warmr-dev <warmr@rapidevelopers.com>`.
- PR: https://github.com/warmr-dev/warmr_circle_scraper/pull/61; remains open for review/decision.
- Merged: not done; requires explicit exact-head operator decision.
- Deployed/Applied: GitHub protection applied/read back; application deployment
  and migrations not required and not performed.
- Live verified: real GitHub publication, required status and protection readback
  passed. This is tooling verification, not application production verification.
- Workspace closed: not done; preserved while the PR remains open.

## Review and verification limits

Hosted Actions startup failures due to billing remain infrastructure gaps; no
success was fabricated. CodeRabbit review of the common Warmr implementation was
attempted repeatedly and remained rate-limited, including after the provider's
cooldown. No paid credits used and no completed CodeRabbit review claimed.
Warmr/Openclaw additionally has two unrelated broad contract warnings (missing
local runtime config, existing Edge tests missing Deno env permissions) and its
worktree audit stops on an unrelated invalid registry entry. These were preserved,
not repaired or hidden by this tooling change. The required tooling tests passed.

## Rollback

Reviewed revert of workflow tooling; preserve other branch checks and review
policies. Never remove documentation protection or change billing automatically.
