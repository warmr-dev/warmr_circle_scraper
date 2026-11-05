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

Offline validator and PR workflow regression suite; real temporary Git repository
proves a malformed body stops publication before push/GitHub. Existing documentation
fixtures retained. Live publisher/status and branch protection readbacks are
recorded in the PR's verification section after executing the rollout.

## Delivery

- Local: implemented in an isolated task worktree.
- Committed / PR: pending at record creation; see immutable PR head and discussion.
- Merged: not done; requires review and explicit decision.
- Deployed/Applied: application runtime not required; GitHub protection tracked separately.
- Live verified: pending publisher/status/protection readback.
- Workspace closed: not done; preserve while PR remains open.

## Rollback

Reviewed revert of workflow tooling; preserve other branch checks and review
policies. Never remove documentation protection or change billing automatically.
