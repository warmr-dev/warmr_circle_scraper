#!/bin/zsh
# Runs one auto-join pass, meant to be invoked periodically by launchd (see
# com.warmr.autojoin.plist in this directory) rather than run by hand.
#
# Ego lite is macOS-only (confirmed against lite.ego.app: no Windows/Linux/
# headless support) and needs the operator's own already-authenticated
# session, so this can't run on the Railway worker -- it stays on this Mac,
# scheduled locally instead. The daily join cap (JoinPacingConfig.
# max_joins_per_day) already prevents runaway joins across repeated runs; a
# handoff (needs_login/challenge/unclear) just leaves that candidate
# not_attempted for the next scheduled run or for you to resolve by hand.
set -uo pipefail

REPO_DIR="/Users/erkeblanzappar/Documents/Work/Projects/warmr"
SPACE_ID="${WARMR_AUTOJOIN_SPACE_ID:-2}"
LOG_DIR="$REPO_DIR/logs"
mkdir -p "$LOG_DIR"

export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$REPO_DIR/.venv/bin:$PATH"
cd "$REPO_DIR"

{
  echo "=== $(date -u '+%Y-%m-%dT%H:%M:%SZ') ==="
  circle-leads auto-join --account main --space-id "$SPACE_ID"
} >> "$LOG_DIR/auto_join.log" 2>&1
