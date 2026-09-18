#!/bin/zsh
# Installs (or re-installs, verifies, or removes) the com.warmr.autojoin
# LaunchAgent.
#
# The repo is the source of truth for the launcher and the plist, but neither
# can be USED from the repo: it lives under ~/Documents, and a launchd-spawned
# process gets no TCC grant for that folder, so the first seven scheduled runs
# died with exit 127 before zsh could even read the script. This installer
# therefore copies the launcher to ~/Library/Application Support/warmr/ and
# renders the plist template with absolute paths that all sit outside
# ~/Documents. Re-run it after editing either file in the repo -- a copy goes
# stale silently, which is the price of not being able to symlink (a symlink
# resolves to the same protected target, so TCC would refuse it just the same).
#
# Installing does not start a join. The agent is RunAtLoad=false, and the
# verification step below is a separate one-shot job that runs the launcher in
# dry-run mode: same interpreter, same launchd domain, same TCC identity, but
# it stops before any browser automation.
#
# NAMING RULE: every function here is prefixed warmr_, and every external tool
# used inside one is called by absolute path. This is not decoration. An
# earlier revision had a function named install() whose body called
# `install -m 755 ...` meaning /usr/bin/install; the function shadowed the
# binary, recursed into itself until zsh hit FUNCNEST, and aborted with the
# launcher never copied -- i.e. auto-join could not be scheduled at all.
# warmr_audit_function_names() (run by --check) enforces the rule so the next
# unprefixed helper is caught by a test rather than by a dead scheduler.
set -euo pipefail

# $0 inside a zsh function is the function name, so the program name has to
# be captured out here, while it still means the script.
PROG="${0:t}"
SELF="${0:A}"
SCRIPT_DIR="${0:A:h}"
REPO_DIR="${SCRIPT_DIR:h}"
LABEL="com.warmr.autojoin"
SELFTEST_LABEL="$LABEL.selftest"

# Every installed path hangs off one root so that --check can render a complete
# install into a throwaway tree and exercise the same code a real install runs.
# Only --check may point the root anywhere but $HOME (enforced below).
INSTALL_ROOT="${WARMR_INSTALL_ROOT:-$HOME}"
INSTALL_DIR="$INSTALL_ROOT/Library/Application Support/warmr"
LOG_DIR="$INSTALL_ROOT/Library/Logs/warmr"
AGENT_DIR="$INSTALL_ROOT/Library/LaunchAgents"
PROGRAM="$INSTALL_DIR/auto_join_launchd.sh"
PLIST="$AGENT_DIR/$LABEL.plist"
DOMAIN="gui/$(id -u)"

warmr_usage() {
  cat <<USAGE
usage: $PROG [--check | --selftest-only | --no-selftest | --uninstall]

  (no flag)        install/refresh the agent, then verify it with a dry run
  --check          render + copy into a throwaway tree and validate; never
                   touches ~/Library or launchctl. Set WARMR_INSTALL_ROOT to
                   choose the tree, otherwise a mktemp dir is used.
  --selftest-only  only re-run the verification against what is installed
  --no-selftest    install/refresh only
  --uninstall      unload the agent and delete the installed copy
USAGE
}

mode="install"
case "${1:-}" in
  "")               ;;
  --check)          mode="check" ;;
  --selftest-only)  mode="selftest" ;;
  --no-selftest)    mode="install-only" ;;
  --uninstall)      mode="uninstall" ;;
  -h|--help)        warmr_usage; exit 0 ;;
  *)                warmr_usage >&2; exit 64 ;;
esac

# A redirected root is a test fixture. Letting a real install use one would
# write a LaunchAgent nobody loads and leave the live agent pointing at the
# stale copy, so refuse it outside --check rather than half-install.
if [[ "$mode" != "check" && "$INSTALL_ROOT" != "$HOME" ]]; then
  print -u2 "error: WARMR_INSTALL_ROOT is only honoured by --check"
  exit 64
fi

# Guard against installing from a directory that merely looks like the repo:
# the launcher is handed this path and will run whatever venv it finds there.
if [[ ! -f "$REPO_DIR/pyproject.toml" || ! -d "$REPO_DIR/circle_leads" ]]; then
  print -u2 "error: $REPO_DIR does not look like the warmr checkout"
  exit 66
fi

# The entire point of copying out of the repo is to land somewhere launchd can
# read. Re-assert it on the rendered paths, because a wrong root turns the
# whole exercise back into the exit-127 loop this script exists to end.
warmr_assert_readable_by_launchd() {
  local p
  for p in "$INSTALL_DIR" "$LOG_DIR" "$AGENT_DIR"; do
    case "$p" in
      "$HOME"/Documents|"$HOME"/Documents/*|"$HOME"/Desktop|"$HOME"/Desktop/*|"$HOME"/Downloads|"$HOME"/Downloads/*)
        print -u2 "error: $p is TCC-protected; a launchd job cannot read it"
        exit 78
        ;;
    esac
  done
}

# Reads this file rather than zsh's runtime function table on purpose: the
# table also holds anything ~/.zshenv defined, which is not ours to police.
warmr_audit_function_names() {
  local fn
  local -a clash=()
  for fn in ${(f)"$(grep -oE '^[A-Za-z_][A-Za-z0-9_]*\(\)' -- "$SELF" || true)"}; do
    fn="${fn%()}"
    if whence -p -- "$fn" >/dev/null 2>&1; then
      clash+=("$fn (shadows $(whence -p -- "$fn"))")
    fi
  done
  if (( ${#clash} )); then
    print -u2 "error: function names shadow PATH commands: ${(j:, :)clash}"
    print -u2 "       prefix them warmr_ -- see the NAMING RULE at the top."
    exit 70
  fi
  echo "check: no function name shadows a PATH command"
}

warmr_uninstall() {
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  /bin/rm -f "$PLIST" "$PROGRAM"
  echo "removed $LABEL (logs kept in $LOG_DIR)"
}

# The copy + render half of an install, with no launchctl in it, so --check can
# run exactly this and still be safe to execute anywhere.
warmr_stage_files() {
  warmr_assert_readable_by_launchd
  /bin/mkdir -p "$INSTALL_DIR" "$LOG_DIR" "$AGENT_DIR"

  # Write beside the target and rename: the destination may be executing right
  # now (a scheduled run), where an in-place write fails with ETXTBSY, while a
  # rename just swaps the inode. Absolute paths so that neither a shell
  # function nor a hijacked PATH can stand in for these three tools.
  /bin/cp -f "$SCRIPT_DIR/auto_join_launchd.sh" "$PROGRAM.new"
  /bin/chmod 755 "$PROGRAM.new"
  /bin/mv -f "$PROGRAM.new" "$PROGRAM"

  sed -e "s|__WARMR_PROGRAM__|$PROGRAM|g" \
      -e "s|__WARMR_REPO_DIR__|$REPO_DIR|g" \
      -e "s|__WARMR_LOG_DIR__|$LOG_DIR|g" \
      "$SCRIPT_DIR/$LABEL.plist" > "$PLIST.new"

  # A leftover placeholder means the template grew a knob this installer does
  # not know about. That plist still lints, still loads, and then points at a
  # path like __WARMR_PROGRAM__ forever -- silent failure every 4h, which is
  # the exact class of bug this script exists to end.
  local leftover
  leftover="$(grep -o '__[A-Z0-9_]*__' "$PLIST.new" | sort -u | tr '\n' ' ' || true)"
  if [[ -n "$leftover" ]]; then
    /bin/rm -f "$PLIST.new"
    print -u2 "error: plist template has unrendered placeholders: $leftover"
    exit 70
  fi

  # A malformed plist is accepted silently by some launchctl versions and then
  # never runs, so fail loudly here instead.
  plutil -lint "$PLIST.new" >/dev/null
  /bin/mv -f "$PLIST.new" "$PLIST"
}

warmr_install() {
  warmr_stage_files

  # bootout first: bootstrap refuses a label that is already loaded, and a
  # stale definition would keep pointing at the old, unreadable path.
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$PLIST"

  echo "installed $LABEL"
  echo "  program: $PROGRAM"
  echo "  repo:    $REPO_DIR"
  echo "  logs:    $LOG_DIR"
}

# Everything an install does except talking to launchd, plus the assertions a
# human would otherwise have to make by eye. Safe on any machine: it writes
# only under its own root and never loads, bootstraps or kickstarts a job.
warmr_check() {
  local created_root=""
  if [[ "$INSTALL_ROOT" == "$HOME" ]]; then
    created_root="$(mktemp -d -t warmr-autojoin-check)" || exit 70
    INSTALL_ROOT="$created_root"
    INSTALL_DIR="$INSTALL_ROOT/Library/Application Support/warmr"
    LOG_DIR="$INSTALL_ROOT/Library/Logs/warmr"
    AGENT_DIR="$INSTALL_ROOT/Library/LaunchAgents"
    PROGRAM="$INSTALL_DIR/auto_join_launchd.sh"
    PLIST="$AGENT_DIR/$LABEL.plist"
  fi

  warmr_audit_function_names
  warmr_stage_files

  # The copy is the whole product of an install; assert it rather than trust
  # that no command in warmr_stage_files silently did nothing.
  if [[ ! -x "$PROGRAM" ]]; then
    print -u2 "error: launcher was not copied to $PROGRAM"
    exit 70
  fi
  if ! cmp -s "$SCRIPT_DIR/auto_join_launchd.sh" "$PROGRAM"; then
    print -u2 "error: $PROGRAM differs from the repo launcher"
    exit 70
  fi
  echo "check: launcher copied to $PROGRAM (executable, matches repo)"

  if ! grep -q "<string>$PROGRAM</string>" "$PLIST"; then
    print -u2 "error: rendered plist does not point at $PROGRAM"
    exit 70
  fi
  echo "check: plist rendered at $PLIST (lints, no placeholders)"

  # Run the installed copy the way the launchd self-test job would, minus
  # launchd: proves the copied file is executable zsh that reaches the repo and
  # the venv. It stops before any browser automation (DRY_RUN=1).
  local rc=0 reported=""
  WARMR_AUTOJOIN_REPO_DIR="$REPO_DIR" \
  WARMR_AUTOJOIN_LOG_DIR="$LOG_DIR" \
  WARMR_AUTOJOIN_DRY_RUN=1 "$PROGRAM" || rc=$?
  # Plain `[[ ... ]] && x` would take the whole script down under `set -e`
  # when the test is false, so these stay full if-statements.
  if [[ -f "$LOG_DIR/selftest.status" ]]; then
    reported="$(<"$LOG_DIR/selftest.status")"
  fi
  if (( rc != 0 )) || [[ "$reported" != "PASS" ]]; then
    print -u2 "check: FAILED -- the copied launcher did not reach the repo/venv"
    if [[ -f "$LOG_DIR/auto_join.log" ]]; then
      print -u2 "$(<"$LOG_DIR/auto_join.log")"
    fi
    exit 77
  fi
  echo "check: copied launcher runs (dry run reached repo + venv)"

  echo "CHECK PASS (root: $INSTALL_ROOT; nothing was loaded into launchd)"
  if [[ -n "$created_root" ]]; then
    /bin/rm -rf "$created_root"
  fi
  return 0
}

warmr_selftest() {
  if [[ ! -x "$PROGRAM" ]]; then
    print -u2 "error: nothing installed at $PROGRAM -- run $PROG first"
    exit 66
  fi

  local status_file="$LOG_DIR/selftest.status"
  local tmp_plist
  tmp_plist="$(mktemp -t warmr-autojoin-selftest)" || exit 70
  /bin/rm -f "$status_file"

  # Kept out of ~/Library/LaunchAgents on purpose: bootstrap accepts a plist
  # from any path, and this job must not survive the check that used it.
  cat > "$tmp_plist" <<SELFTEST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$SELFTEST_LABEL</string>
    <key>ProgramArguments</key><array><string>$PROGRAM</string></array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>WARMR_AUTOJOIN_REPO_DIR</key><string>$REPO_DIR</string>
        <key>WARMR_AUTOJOIN_LOG_DIR</key><string>$LOG_DIR</string>
        <key>WARMR_AUTOJOIN_DRY_RUN</key><string>1</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>$LOG_DIR/selftest.out</string>
    <key>StandardErrorPath</key><string>$LOG_DIR/selftest.err</string>
</dict>
</plist>
SELFTEST

  plutil -lint "$tmp_plist" >/dev/null
  launchctl bootout "$DOMAIN/$SELFTEST_LABEL" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$tmp_plist"

  local waited=0
  while [[ ! -f "$status_file" && $waited -lt 30 ]]; do
    sleep 1
    (( waited += 1 ))
  done

  launchctl bootout "$DOMAIN/$SELFTEST_LABEL" 2>/dev/null || true
  /bin/rm -f "$tmp_plist"

  if [[ ! -f "$status_file" ]]; then
    print -u2 "SELF-TEST INCONCLUSIVE: the launcher never reported in ${waited}s."
    print -u2 "Check $LOG_DIR/selftest.err -- if it holds a \"can't open input file\""
    print -u2 "line, even the installed copy is unreadable to launchd."
    exit 75
  fi

  if [[ "$(<"$status_file")" == "PASS" ]]; then
    echo "SELF-TEST PASS: a launchd-spawned run reached the repo and the venv."
    echo "The next scheduled run (every 4h) will do a real pass."
  else
    print -u2 "SELF-TEST FAIL: launchd can start the launcher, but it cannot read"
    print -u2 "the repo. Remedy is in $LOG_DIR/auto_join.log (tail it)."
    exit 77
  fi
}

case "$mode" in
  uninstall)    warmr_uninstall ;;
  check)        warmr_check ;;
  install-only) warmr_install ;;
  install)      warmr_install; warmr_selftest ;;
  selftest)     warmr_selftest ;;
esac
