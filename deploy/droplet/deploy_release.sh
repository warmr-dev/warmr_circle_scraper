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
# 3. Install runtime units, switch symlinks, restart capture and maintenance.
#
# The join timer is NOT enabled here: whether joins run is the user's call.
# The script prints the commands for a one-community test and for the timer.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
REF="${1:-origin/main}"
DEPLOY_HOST="${CIRCLE_DEPLOY_HOST:-warmr-1}"
SSH=(ssh -o BatchMode=yes "$DEPLOY_HOST")
if [[ -n "${CIRCLE_DEPLOY_SSH_IDENTITY:-}" ]]; then
  SSH=(ssh -o BatchMode=yes -o IdentitiesOnly=yes -i "$CIRCLE_DEPLOY_SSH_IDENTITY" "$DEPLOY_HOST")
fi
git -C "$REPO" fetch --quiet origin
SHA=$(git -C "$REPO" rev-parse --short=7 "$REF")
echo "deploying $SHA: $(git -C "$REPO" log -1 --format=%s "$SHA")"

git -C "$REPO" archive --format=tar "$SHA" | "${SSH[@]}" \
  "install -d -o warmr -g warmr /opt/warmr/releases/$SHA && tar -x -C /opt/warmr/releases/$SHA && echo $SHA > /opt/warmr/releases/$SHA/COMMIT"

"${SSH[@]}" bash -s -- "$SHA" <<'REMOTE'
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

for service in worker watcher recovery communities; do
  install -m 0644 "$REL/deploy/droplet/warmr-$service.service" "/etc/systemd/system/warmr-$service.service"
done
install -m 0644 "$REL/deploy/droplet/warmr-join.service" /etc/systemd/system/warmr-join.service
install -m 0644 "$REL/deploy/droplet/warmr-join.timer" /etc/systemd/system/warmr-join.timer
install -d -o warmr -g warmr /var/lib/warmr/join-shots /var/lib/warmr/data
systemctl daemon-reload

ln -sfn "$REL" /opt/warmr/current
ln -sfn "$REL" /opt/warmr/watcher
# A stop that times out (a worker deep in a long step) makes `systemctl
# restart` fail although the new worker comes up. That stopped this script --
# and whatever was chained after it with && -- on 2026-09-29 and 09-30.
# Whether the services run is what the is-active line below checks.
systemctl enable warmr-recovery warmr-communities
systemctl restart warmr-worker warmr-watcher warmr-recovery warmr-communities || echo "restart reported a failure; checking the services"
sleep 20
systemctl is-active warmr-worker warmr-watcher warmr-recovery warmr-communities
readlink -f /opt/warmr/current
journalctl -u warmr-worker -u warmr-watcher --since '-1 min' --no-pager | tail -15
echo
# is-enabled prints the state itself and exits non-zero when it is not enabled.
echo "join timer: $(systemctl is-enabled warmr-join.timer 2>/dev/null || true)"
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
