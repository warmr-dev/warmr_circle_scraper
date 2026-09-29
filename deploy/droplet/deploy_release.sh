#!/usr/bin/env bash
# Deploy a commit (default: origin/main) to warmr-1, from a Mac with the repo.
#
#   deploy/droplet/deploy_release.sh            # origin/main
#   deploy/droplet/deploy_release.sh <sha|ref>
#
# 1. Unpack the tree into /opt/warmr/releases/<sha> and build its venv
#    (with Playwright: the join run drives warmr-browser over CDP).
# 2. Refuse to go on if the database lacks a column this code maps
#    (`circle-leads check-schema`): with SKIP_DB_INIT=true a missing migration
#    only shows up as every query failing.
# 3. Install the join units, switch the symlinks, restart worker and watcher.
#
# The join timer is NOT enabled here: whether joins run is the user's call.
# The script prints the commands for a one-community test and for the timer.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
REF="${1:-origin/main}"
git -C "$REPO" fetch --quiet origin
SHA=$(git -C "$REPO" rev-parse --short=7 "$REF")
echo "deploying $SHA: $(git -C "$REPO" log -1 --format=%s "$SHA")"

git -C "$REPO" archive --format=tar "$SHA" | ssh warmr-1 \
  "install -d -o warmr -g warmr /opt/warmr/releases/$SHA && tar -x -C /opt/warmr/releases/$SHA && echo $SHA > /opt/warmr/releases/$SHA/COMMIT"

ssh warmr-1 bash -s -- "$SHA" <<'REMOTE'
set -euo pipefail
SHA="$1"
REL=/opt/warmr/releases/$SHA
cd "$REL"
python3 -m venv venv
venv/bin/pip install -q --upgrade pip
# browser = the playwright library only; the browser itself is warmr-browser's.
venv/bin/pip install -q '.[llm,crypto,browser]'
chown -R warmr:warmr "$REL"

# The schema check reads the catalogue only. It runs before anything switches,
# with the services' own environment (systemd parses the file, not a shell).
systemd-run --quiet --wait --pipe --collect -p User=warmr \
  -p EnvironmentFile=/etc/warmr/warmr.env "$REL/venv/bin/circle-leads" check-schema

install -m 0644 "$REL/deploy/droplet/warmr-join.service" /etc/systemd/system/warmr-join.service
install -m 0644 "$REL/deploy/droplet/warmr-join.timer" /etc/systemd/system/warmr-join.timer
install -d -o warmr -g warmr /var/lib/warmr/join-shots /var/lib/warmr/data
systemctl daemon-reload

ln -sfn "$REL" /opt/warmr/current
ln -sfn "$REL" /opt/warmr/watcher
systemctl restart warmr-worker warmr-watcher
sleep 20
systemctl is-active warmr-worker warmr-watcher warmr-browser
readlink -f /opt/warmr/current
journalctl -u warmr-worker -u warmr-watcher --since '-1 min' --no-pager | tail -15
echo
echo "join timer: $(systemctl is-enabled warmr-join.timer 2>/dev/null || echo disabled)"
cat <<'NEXT'
One community, by hand, first (replace <slug>) -- the same environment the timer uses:
  systemd-run --wait --pipe --collect -p User=warmr -p WorkingDirectory=/var/lib/warmr \
    -p EnvironmentFile=/etc/warmr/warmr.env -p EnvironmentFile=/etc/warmr/warmr-join.env \
    -p Environment=CIRCLE_GOVERNOR_FILE=/var/lib/warmr/circle-governor.json \
    /opt/warmr/current/venv/bin/circle-leads auto-join --driver cdp --account 4 \
    --host <slug> --screenshot-dir /var/lib/warmr/join-shots
Then every 4 hours:
  systemctl enable --now warmr-join.timer
NEXT
REMOTE
