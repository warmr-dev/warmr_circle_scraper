#!/bin/sh
# Воркер: очередь задач, плановый сбор, обогащение, ICP.
# Свой файл бюджета запросов -- общий с слежкой файл означал бы, что
# трёхчасовой сбор съедает её долю.
W=$HOME/.openclaw/warmr
set -a; . $W/etc/warmr.env; set +a
CIRCLE_GOVERNOR_FILE=$W/state/circle-governor.json; export CIRCLE_GOVERNOR_FILE
cd $W/state
exec $W/venv/bin/circle-leads worker
