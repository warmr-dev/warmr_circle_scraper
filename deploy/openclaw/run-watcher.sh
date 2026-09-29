#!/bin/sh
# Слежка за лентами сообществ.
W=$HOME/.openclaw/warmr
set -a; . $W/etc/warmr.env; set +a
CIRCLE_GOVERNOR_FILE=$W/state/watch-governor.json; export CIRCLE_GOVERNOR_FILE
cd $W/state
exec $W/venv/bin/circle-leads watch
