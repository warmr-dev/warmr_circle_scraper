#!/usr/bin/env bash
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
checker="$root/scripts/check-documentation-impact.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

run_case() {
  local name="$1"
  local expected="$2"
  local files="$3"
  local body="$4"
  local files_path="$tmp/${name}.files"
  local body_path="$tmp/${name}.body"
  printf '%s\n' "$files" > "$files_path"
  printf '%s\n' "$body" > "$body_path"
  if bash "$checker" --changed-files-file "$files_path" --pr-body-file "$body_path" --require-pr-body >/dev/null 2>&1; then
    actual=0
  else
    actual=1
  fi
  if [[ "$actual" != "$expected" ]]; then
    echo "FAIL: $name expected exit $expected, got $actual" >&2
    exit 1
  fi
}

run_case "functional_without_docs" 1 \
  $'app/api/example/route.ts' \
  $'## Documentation impact\n- Documentation added: `docs/example.md`\n- Delivery record: `docs/changes/example.md`\n- Documentation not required reason: N/A'

run_case "functional_with_docs" 0 \
  $'app/api/example/route.ts\ndocs/example.md\ndocs/changes/example.md' \
  $'## Documentation impact\n- Documentation added: `docs/example.md`\n- Delivery record: `docs/changes/example.md`\n- Documentation not required reason: N/A'

run_case "missing_documentation_impact_section" 1 \
  $'app/api/example/route.ts\ndocs/example.md\ndocs/changes/example.md' \
  $'## Summary\n- Adds the endpoint.\n\n## Verification\n- Tests passed.'

run_case "hash_only_with_reason" 0 \
  $'openclaw/context/tools/runtime.hashes.json' \
  $'## Documentation impact\n- Documentation added: N/A\n- Delivery record: N/A\n- Documentation not required reason: hash-only runtime pin; no contract or operational behavior changed.'

run_case "hash_only_without_reason" 1 \
  $'openclaw/context/tools/runtime.hashes.json' \
  $'## Documentation impact\n- Documentation added: N/A\n- Delivery record: N/A\n- Documentation not required reason:'

run_case "unfilled_template" 1 \
  $'docs/changes/example.md' \
  $'## Documentation impact\n- Documentation added: <canonical documentation path, or N/A>\n- Delivery record: <docs/changes/YYYY-MM-DD-short-slug.md, or N/A>\n- Documentation not required reason: <required only when both entries above are N/A>'

run_case "html_footer_outside_documentation_impact" 0 \
  $'app/api/example/route.ts\ndocs/example.md\ndocs/changes/example.md' \
  $'## Documentation impact\n- Documentation added: `docs/example.md`\n- Delivery record: `docs/changes/example.md`\n- Documentation not required reason: N/A\n\n## Rollout state\n- Local: done.\n\n<div><a href="https://cursor.com/agents/example">Open in Web</a></div>'

run_case "declared_doc_not_changed" 1 \
  $'app/api/example/route.ts\ndocs/example.md\ndocs/changes/example.md' \
  $'## Documentation impact\n- Documentation added: `docs/other.md`\n- Delivery record: `docs/changes/example.md`\n- Documentation not required reason: N/A'

run_case "html_footer_with_agent_end_tag" 0 \
  $'app/api/example/route.ts\ndocs/example.md\ndocs/changes/example.md' \
  $'## Documentation impact\n- Documentation added: `docs/example.md`\n- Delivery record: `docs/changes/example.md`\n<!-- CURSOR_AGENT_PR_BODY_END -->\n\n<div><a href="https://cursor.com/agents/example">Open in Web</a></div>'

echo "Documentation impact contract tests passed."
