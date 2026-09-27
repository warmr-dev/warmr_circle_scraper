#!/usr/bin/env bash
# Does a managed agent host can actually carry this project? Answer before paying.
#
# Four things the vendor does not document decide the whole migration: whether a
# process survives a disconnect, whether the disk survives a restart, what the
# outbound address is, and whether anything supervises processes after a reboot.
# Two more can kill it outright and are on no FAQ: whether non-HTTP egress is
# allowed at all (IMAP 993 for the login code, Postgres 6543 for the database),
# and whether Chrome can keep its sandbox in a container.
#
# Reads only. Writes nothing outside its own working directory, sends no
# credentials anywhere, and touches nothing of ours.
#
#   bash preflight.sh start     # in the free trial, first thing
#   ...log out of the panel completely, wait 3 minutes, come back...
#   bash preflight.sh check     # verdict on the disconnect
#   ...restart the instance from the panel...
#   bash preflight.sh check     # verdict on persistence, boot and the IP
#   bash preflight.sh browser   # last, and only if everything above passed
set -uo pipefail

WORK="${PREFLIGHT_DIR:-$HOME/preflight}"
BEAT="$WORK/beat.txt"
MARK="$WORK/marker.txt"
BLOB="$WORK/blob-200m.bin"
SUMS="$WORK/blob-200m.sha"
IPLOG="$WORK/ip.log"
DB_HOST="${PREFLIGHT_DB_HOST:-aws-0-ap-southeast-1.pooler.supabase.com}"
DB_PORT="${PREFLIGHT_DB_PORT:-6543}"

ok()   { printf '  \033[32mOK\033[0m    %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; }
warn() { printf '  \033[33m?\033[0m     %s\n' "$*"; }
head_() { printf '\n== %s\n' "$*"; }

# A port check that cannot hang: bash's own /dev/tcp behind `timeout` when it
# exists, and a background-kill fallback when it does not.
port_open() {
  local host=$1 port=$2
  if command -v timeout >/dev/null 2>&1; then
    timeout 8 bash -c "exec 3<>/dev/tcp/$host/$port" >/dev/null 2>&1
  else
    ( exec 3<>/dev/tcp/"$host"/"$port" ) >/dev/null 2>&1 &
    local pid=$!
    ( sleep 8; kill -9 $pid 2>/dev/null ) >/dev/null 2>&1 &
    wait $pid 2>/dev/null
  fi
}

public_ip() { curl -s --max-time 10 https://api.ipify.org 2>/dev/null; }

cmd_start() {
  mkdir -p "$WORK" || { echo "cannot write to $WORK"; exit 1; }

  head_ "1. Egress that is not HTTPS  (a failure here ends the migration)"
  if port_open imap.gmail.com 993; then ok "IMAP 993 reachable -- the 2FA code can be read"
  else bad "IMAP 993 BLOCKED -- no login code, joins impossible"; fi
  if port_open "$DB_HOST" "$DB_PORT"; then ok "Postgres $DB_PORT reachable -- the database is usable"
  else bad "Postgres $DB_PORT BLOCKED -- no database, nothing can run here"; fi
  curl -sI --max-time 10 https://openrouter.ai/api/v1/models 2>/dev/null | head -1 | grep -q . \
    && ok "openrouter.ai answers" || warn "openrouter.ai did not answer"
  curl -sI --max-time 10 https://api.telegram.org 2>/dev/null | head -1 | grep -q . \
    && ok "api.telegram.org answers (alerts)" || warn "api.telegram.org did not answer"

  head_ "2. The machine"
  echo "  user:   $(id -un 2>/dev/null) ($(id -u 2>/dev/null))"
  sudo -n true 2>/dev/null && ok "passwordless sudo" || warn "no passwordless sudo -- check whether packages can be installed"
  echo "  os:     $( . /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}" )"
  echo "  cpu:    $(nproc 2>/dev/null) vCPU"
  echo "  ram:    $(free -m 2>/dev/null | awk '/Mem:/{print $2" MB total, "$7" MB available"}')"
  echo "  swap:   $(free -m 2>/dev/null | awk '/Swap:/{print $2" MB"}')"
  echo "  disk:   $(df -h / 2>/dev/null | awk 'NR==2{print $2" total, "$4" free"}')"

  head_ "3. Who supervises processes"
  echo "  pid 1:  $(cat /proc/1/comm 2>/dev/null)"
  if [ -d /run/systemd/system ]; then ok "systemd is running -- the droplet's three units can be copied as they are"
  else warn "no systemd -- supervisord or the platform's own start command will have to do it"; fi
  for c in cron crond supervisord s6-svscan runsv; do
    command -v "$c" >/dev/null 2>&1 && ok "found $c"
  done
  crontab -l >/dev/null 2>&1 && ok "crontab readable (an @reboot line is possible)" \
                             || warn "no crontab -- nothing obvious restarts a process after a reboot"

  head_ "4. Can Chrome keep its sandbox"
  if unshare --user --map-root-user true >/dev/null 2>&1; then
    ok "user namespaces allowed -- Chrome's own sandbox works, no setuid helper needed"
  else
    warn "user namespaces DENIED -- the setuid chrome_sandbox helper is the only way left"
    if mount 2>/dev/null | grep -E ' on (/|/home|/opt) ' | grep -q nosuid; then
      bad "...and the filesystem is mounted nosuid, so setuid cannot work either"
      echo "        => no browser on this host. --no-sandbox is not an option:"
      echo "           this browser opens pages written by strangers."
    else
      ok "no nosuid on the relevant mounts -- the setuid helper should work"
    fi
  fi

  head_ "5. Markers for the disconnect and restart tests"
  public_ip > "$IPLOG"; echo "  outbound IP now: $(cat "$IPLOG")"
  echo "marker-$(date -u +%s)" > "$MARK"
  dd if=/dev/urandom of="$BLOB" bs=1M count=200 status=none 2>/dev/null \
    && sha256sum "$BLOB" > "$SUMS" && ok "200 MB written and checksummed"
  : > "$BEAT"
  setsid nohup sh -c "while :; do date -u +%s >> '$BEAT'; sleep 10; done" \
    >/dev/null 2>&1 </dev/null &
  disown 2>/dev/null || true
  sleep 11
  ok "heartbeat started, $(wc -l < "$BEAT") ticks so far"

  cat <<'NEXT'

  Now, in this order:
    1. Log out of the panel completely and close the tab. Wait 3 minutes.
    2. Come back and run:  bash preflight.sh check
    3. Restart the instance from the panel, then run it again.
NEXT
}

cmd_check() {
  [ -f "$BEAT" ] || { echo "run 'bash preflight.sh start' first"; exit 1; }

  head_ "Did the process survive?"
  local ticks gaps
  ticks=$(wc -l < "$BEAT")
  gaps=$(awk 'NR>1 && $1-p > 25 {c++} {p=$1} END {print c+0}' "$BEAT")
  echo "  ticks: $ticks, gaps longer than 25s: $gaps"
  if pgrep -f "sleep 10" >/dev/null 2>&1 && [ "$gaps" -eq 0 ]; then
    ok "still running, no gaps -- processes survive a disconnect"
  elif [ "$gaps" -gt 0 ]; then
    bad "$gaps gap(s) -- the process was stopped while nobody was connected"
    echo "        => the watcher cannot live here. This is an abort."
  else
    bad "the heartbeat is dead -- nothing restarted it"
    echo "        => an always-on worker is impossible without a supervisor."
  fi

  head_ "Did the disk survive?"
  if [ -f "$MARK" ]; then ok "marker present: $(cat "$MARK")"; else bad "marker GONE -- the filesystem is reset"; fi
  if [ -f "$SUMS" ] && sha256sum -c "$SUMS" >/dev/null 2>&1; then
    ok "200 MB file intact -- real persistent storage"
  else
    bad "the 200 MB file is missing or corrupted"
    echo "        => the Chrome profile and the rate-limit counters would be lost on every restart."
  fi

  head_ "Did the address change?"
  local before now
  before=$(cat "$IPLOG" 2>/dev/null)
  now=$(public_ip)
  echo "  before: ${before:-?}"
  echo "  now:    ${now:-?}"
  if [ -n "$now" ] && [ "$before" = "$now" ]; then
    ok "same outbound address"
  else
    bad "the address changed"
    echo "        => Circle binds its clearance cookie to the address: the session"
    echo "           breaks on every change and each recovery costs an emailed code."
  fi

  echo
  echo "  Clean up when done:  rm -rf '$WORK'"
}

cmd_browser() {
  head_ "Cloudflare, headed under Xvfb  (the measurement that decides joins)"
  echo "  The droplet is challenged 3/3 headless and 0/3 headed under Xvfb."
  echo "  A host that does worse than that is not a migration."
  echo
  set -e
  sudo apt-get update -qq
  sudo apt-get install -y -qq xvfb python3-venv
  python3 -m venv "$WORK/venv"
  "$WORK/venv/bin/pip" -q install playwright
  PLAYWRIGHT_BROWSERS_PATH="$WORK/ms-playwright" "$WORK/venv/bin/playwright" install --with-deps chromium
  set +e
  local chrome
  chrome=$(ls -d "$WORK"/ms-playwright/chromium-*/chrome-linux64/chrome 2>/dev/null | tail -1)
  [ -x "$chrome" ] || { bad "no chromium under $WORK/ms-playwright"; exit 1; }

  # Exactly how warmr-browser.service starts it: headed, in a virtual display,
  # debug port on loopback only. The --server-args value stays one quoted word.
  pkill -f "user-data-dir=$WORK/profile" 2>/dev/null
  nohup xvfb-run -a --server-args="-screen 0 1280x900x24" "$chrome" \
    --user-data-dir="$WORK/profile" \
    --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 \
    --no-first-run --no-default-browser-check --disable-dev-shm-usage \
    --window-size=1280,900 about:blank >"$WORK/chrome.log" 2>&1 &
  local i
  for i in $(seq 1 25); do sleep 1; curl -sf http://127.0.0.1:9222/json/version >/dev/null && break; done
  curl -sf http://127.0.0.1:9222/json/version >/dev/null \
    || { bad "Chrome did not come up; see $WORK/chrome.log"; tail -5 "$WORK/chrome.log"; exit 1; }
  ok "headed Chrome is up with the sandbox on"

  "$WORK/venv/bin/python" - <<'PY'
import json, time
from playwright.sync_api import sync_playwright

TARGETS = ["https://b2b-tactics.circle.so/",
           "https://community.thefpahub.com/",
           "https://my.icecampus.com/"]
MARKERS = ("just a moment", "checking your browser", "verifying you are human",
           "attention required")

def challenged(page):
    title = (page.title() or "").lower()
    if any(m in title for m in MARKERS):
        return True
    try:
        return any(m in (page.inner_text("body") or "")[:2000].lower() for m in MARKERS)
    except Exception:
        return True

bad = 0
with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    for url in TARGETS:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            deadline = time.time() + 30
            while challenged(page) and time.time() < deadline:
                page.wait_for_timeout(2000)
            still = challenged(page)
            bad += still
            print(json.dumps({"url": url,
                              "verdict": "CHALLENGED" if still else "clear",
                              "title": (page.title() or "")[:60]}, ensure_ascii=False))
        except Exception as exc:
            bad += 1
            print(json.dumps({"url": url, "error": str(exc)[:160]}))
    browser.close()
print()
print("VERDICT: %d of 3 challenged -- %s" % (bad, "ABORT" if bad else "this host is as good as the droplet"))
PY
  pkill -f "user-data-dir=$WORK/profile" 2>/dev/null
}

case "${1:-}" in
  start)   cmd_start ;;
  check)   cmd_check ;;
  browser) cmd_browser ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
