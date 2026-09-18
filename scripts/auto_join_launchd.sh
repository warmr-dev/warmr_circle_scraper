#!/bin/zsh
# Runs one auto-join pass, meant to be invoked periodically by launchd rather
# than run by hand. Install it with install_autojoin_launchd.sh -- do not point
# a LaunchAgent at this copy inside the repo.
#
# Ego lite is macOS-only (confirmed against lite.ego.app: no Windows/Linux/
# headless support) and needs the operator's own already-authenticated
# session, so this can't run on the Railway worker -- it stays on this Mac,
# scheduled locally instead. The daily join cap (JoinPacingConfig.
# max_joins_per_day) already prevents runaway joins across repeated runs; a
# handoff (needs_login/challenge/unclear) just leaves that candidate
# not_attempted for the next scheduled run or for you to resolve by hand.
#
# WHY THIS SCRIPT IS COPIED OUT OF THE REPO
# The first seven scheduled runs all died with exit 127 and one line of
# stderr: "/bin/zsh: can't open input file: <repo>/scripts/auto_join_launchd.sh".
# The file was present and -rwxr-xr-x the whole time, and an interactive shell
# read it fine. That split -- zsh got as far as printing its own error, so the
# kernel managed to exec it from the shebang, but zsh's own open() of the
# script was refused -- is the macOS TCC signature: a launchd-spawned process
# inherits no Full Disk Access, and the repo lives under ~/Documents, which is
# one of the TCC-protected folders. The installer therefore copies this script
# to ~/Library/Application Support/warmr/ (not protected) and logs to
# ~/Library/Logs/warmr/. A symlink would not have helped: TCC checks the
# target's real path, which is still inside ~/Documents.
#
# That move only buys the ability to START. The work itself still reads the
# repo (the venv interpreter, circle_leads, the .env), so the run also needs a
# Full Disk Access grant -- see preflight() below, which says so in the log
# instead of failing somewhere deep in Python.
set -uo pipefail

# Baked into the LaunchAgent by the installer so the installed copy carries
# nothing machine-specific; the fallback is for running this straight out of a
# checkout, where the repo root is simply the parent of scripts/.
REPO_DIR="${WARMR_AUTOJOIN_REPO_DIR:-${0:A:h:h}}"
SPACE_ID="${WARMR_AUTOJOIN_SPACE_ID:-2}"
LOG_DIR="${WARMR_AUTOJOIN_LOG_DIR:-$HOME/Library/Logs/warmr}"
DRY_RUN="${WARMR_AUTOJOIN_DRY_RUN:-0}"

mkdir -p "$LOG_DIR" || {
  # Nowhere to write the log means nowhere to explain the failure, so this one
  # case has to shout on stderr and let launchd record it.
  print -u2 "auto_join_launchd: cannot create log dir $LOG_DIR"
  exit 73
}
LOG_FILE="$LOG_DIR/auto_join.log"
STATUS_FILE="$LOG_DIR/selftest.status"

# Checks the things that TCC silently takes away, in the order that makes the
# failure legible: reaching the repo at all, reading a file inside it, and
# actually executing the venv interpreter (which has to read its own stdlib out
# of the same protected folder). Prints its findings on stdout; the caller
# redirects that into the log.
preflight() {
  local failure=""

  if [[ ! -d "$REPO_DIR" ]]; then
    failure="repo dir not found: $REPO_DIR"
  elif ! ls "$REPO_DIR" >/dev/null 2>&1; then
    failure="cannot list $REPO_DIR (EPERM = no Full Disk Access)"
  elif ! head -c 1 "$REPO_DIR/pyproject.toml" >/dev/null 2>&1; then
    failure="cannot read $REPO_DIR/pyproject.toml (EPERM = no Full Disk Access)"
  elif ! "$REPO_DIR/.venv/bin/python" -c "pass" >/dev/null 2>&1; then
    failure="cannot execute $REPO_DIR/.venv/bin/python (venv missing, or EPERM = no Full Disk Access)"
  fi

  if [[ -z "$failure" ]]; then
    echo "preflight: ok (repo readable, venv python runs)"
    return 0
  fi

  echo "preflight: FAILED -- $failure"
  echo "preflight: this process is $(id -un) spawned by launchd, which gets no"
  echo "preflight: TCC grant for ~/Documents. Grant Full Disk Access in"
  echo "preflight: System Settings > Privacy & Security > Full Disk Access to"
  echo "preflight: the shell that runs this job (/bin/zsh), then re-run"
  echo "preflight: scripts/install_autojoin_launchd.sh --selftest-only."
  echo "preflight: The alternative, if you would rather not grant that, is to"
  echo "preflight: move the checkout out of ~/Documents entirely."
  return 78
}

{
  echo "=== $(date -u '+%Y-%m-%dT%H:%M:%SZ') dry_run=$DRY_RUN repo=$REPO_DIR ==="
  preflight
  preflight_rc=$?

  if (( preflight_rc != 0 )); then
    [[ "$DRY_RUN" == "1" ]] && echo "FAIL" > "$STATUS_FILE"
    exit $preflight_rc
  fi

  # The self-test exists so the install can be verified without starting real
  # browser automation against real Circle communities: same interpreter, same
  # launchd domain, same TCC identity as a scheduled run, minus the join.
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "PASS" > "$STATUS_FILE"
    echo "dry run: stopping before auto-join"
    exit 0
  fi

  export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$REPO_DIR/.venv/bin:$PATH"
  cd "$REPO_DIR" || exit 73
  circle-leads auto-join --account main --space-id "$SPACE_ID"
} >> "$LOG_FILE" 2>&1
