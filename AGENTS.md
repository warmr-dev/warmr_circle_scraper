# Repository instructions

## Documentation before PR and merge

At task start, identify the canonical runbook and delivery record, and maintain
them alongside functional changes (including CI, infrastructure and configuration).
Read `docs/documentation-local-gate.md` for the canonical process.

Work only in a clean isolated `codex/` task worktree from current `origin/main`.
Preserve unrelated work in existing checkouts. Publish/update only with
`bash scripts/pr-publish.sh --body-file <file> [--pr <number>]`, which validates
the exact body before push, reruns the same validator against remote head/base/body,
and read-backs `documentation-local`. Do not replace it with raw push/create/edit.
Every functional PR needs changed canonical documentation and a changed delivery
record. Non-runtime exceptions require a precise reason; placeholders do not pass.

Inspect the full PR discussion, reviews, inline comments, CI and preview status.
Report findings and obtain an explicit operator merge decision for the exact
reviewed head. Do not automatically patch review findings, reply, create issues,
merge or deploy. After approval, merge only using
`bash scripts/pr-merge.sh <number> --expected-head <full-reviewed-sha>`.
It revalidates fresh remote inputs before the exact-head GitHub merge API call.
An old green status is insufficient after body/base/head changes. Never chain a
publisher and raw merge command. CI billing/startup failures are infrastructure
gaps, not documentation failures; never disable or fake checks to hide them.

Use `warmr-dev <warmr@rapidevelopers.com>` for author/committer and GitHub account
`warmr-dev` for publishing. Verify effective identities before committing. Report
Local, Committed, PR, Merged, Deployed/Applied, Live verified and Workspace closed
separately. Do not imply local or PR changes are already on main or in production.
