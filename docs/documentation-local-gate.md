# Documentation before PR publication and merge

## Contract

At task start, identify the canonical runbook and delivery record. Update them
alongside functional code, migrations, workflows, CI, configuration and deployment
changes. Delivery records under `docs/changes/` do not double as canonical runbooks.
The per-repository policy in `scripts/pr-policy.json` identifies the repository,
base and accepted documentation locations. Unknown runtime/config paths require
docs; the validator is not limited to today's application directories.

Use a clean isolated `codex/` task worktree from current `origin/main`. Preserve
unrelated work; never reset, stash, rebase or overwrite an old mixed checkout just
to satisfy a gate. Requirements: Git, authenticated `gh`, Python 3.9+ (Circle's
application requires 3.11+), and publishing identity
`warmr-dev <warmr@rapidevelopers.com>` with GitHub login `warmr-dev`.

## Publish or update

Save the final PR body **outside the repository**, commit the intended changes,
then run from that task worktree:

```bash
bash scripts/pr-publish.sh --body-file /absolute/path/pr-body.md --title 'Describe the change'
bash scripts/pr-publish.sh --body-file /absolute/path/pr-body.md --pr 123
```

Use `--draft` on creation when review is pending. The title defaults to the latest
commit subject. Update titles may be supplied explicitly. No force push, automatic
rebase, merge or deployment occurs. Do not substitute raw push/create/edit commands.

Publication snapshots the saved body, fetches main and checks the clean branch,
base freshness, full diff, actual changed documents and that exact body **before
push or PR mutation**. An update first marks the prior documentation status pending.
After creation/update it reads remote head/base/body, reruns the same validator,
checks inputs again, publishes `documentation-local`, reads the stored status back,
and checks inputs once more. Any Git/GitHub/validation/readback failure exits
nonzero. If PR creation succeeded but attestation failed, preserve the existing PR
and rerun with `--pr`; do not create a duplicate.

Required body format:

```markdown
## Documentation impact
- Documentation added: `docs/relevant-runbook.md`
- Delivery record: `docs/changes/YYYY-MM-DD-short-slug.md`
- Documentation not required reason: N/A
```

For non-runtime changes, use `N/A` for both paths and a specific reason of at least
15 characters. Legacy Scout plain labels, multiple backticked paths and
`Documentation not required: <specific reason>` remain parseable. Placeholders,
duplicate declarations, declarations outside this section, deleted documents,
symlink documents and unchanged paths fail. Runtime/config files cannot claim an
exemption just by saying "refactor" or "formatting": the conservative validator
cannot establish semantic equivalence, so document those changes normally.

One canonical validator (`scripts/documentation_gate.py`, shell alias
`check-documentation-impact.sh`) serves preflight, remote attestation and Actions.
Run offline checks with:

```bash
bash scripts/check-documentation-impact.test.sh
python3 -B scripts/pr_workflow_test.py
```

## Review and merge

After publication, inspect the full discussion, reviews, inline comments, CI and
preview status. Follow the project's review triage and obtain the operator's
explicit merge decision for the unchanged reviewed head. Then use one command:

```bash
bash scripts/pr-merge.sh 123 --expected-head FULL_40_CHARACTER_REVIEWED_SHA
```

For a fresh validation without any status write or merge, add `--dry-run`.
The entrypoint reruns the validator on current remote inputs, publishes/read-backs
the required status, checks inputs again and calls GitHub's merge API with the
exact reviewed head SHA. A failed gate makes merge unreachable regardless of the
caller's shell settings. Draft PRs cannot merge. No deployment is performed.
`publish-documentation-local.sh 123` remains an explicit validation/readback tool,
not a substitute for guarded merge. Scout's legacy `merge:reviewed` alias remains
available and uses the same canonical documentation validator via `check:pr-ready`.

The required main-branch status is `documentation-local`, with strict base
freshness and admin enforcement. Before adding it, prove the publisher against
the rollout PR and inspect existing branch protection and rulesets. Add the status
without removing other checks/review restrictions; enable strict freshness. Read
back the effective configuration. These settings are rollout operations, not
implicitly applied by a code commit. Warmr's existing protection is preserved.

GitHub commit statuses do **not** expire on PR body edits. Always use the guarded
merge entrypoint, even with an old green status. A final body/base change can race
after the last read: GitHub's merge API atomically compares only the source SHA.
Strict base protection handles base advancement; body-change atomicity and people
bypassing the entrypoint are not guaranteed by this local tool. A status-writing
credential can forge a status; keep that access restricted. No new hosted service
or privileged automation is introduced to pretend these limits do not exist.

## Infrastructure failures and rollout state

Actions `documentation-gate` is supplementary and remains enabled. A billing or
spending-limit startup failure means the validator never ran; it is an
infrastructure gap, not evidence of missing documentation. Local success proves
only documentation validation, not the rest of CI. Do not disable workflows, alter
billing, or manufacture successful statuses to hide failures.

Report Local, Committed, PR, Merged, Deployed/Applied, Live verified and Workspace
closed separately. For this tooling, live verification means actual GitHub
publication/status/protection readback; application production validation is not
required. Scripts and rules become default-branch policy only after merge. Keep
the PR worktrees open until merged and safely closed through project tooling.
Rollback code through a reviewed revert; keep documentation protection in place.
Do not remove required checks or change the Actions app binding as an automatic
rollback. Changes to protection need a deliberate operator decision and readback.
