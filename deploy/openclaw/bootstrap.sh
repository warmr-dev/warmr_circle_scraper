#!/bin/sh
# Системные пакеты ставятся в overlay контейнера, а он не переживает замену
# инстанса -- в отличие от ~/.openclaw. Этот скрипт возвращает их на место.
# Зовётся сторожем перед запуском служб; если всё на месте, ничего не делает.
W=$HOME/.openclaw/warmr
need=""
python3 -c "import ensurepip" 2>/dev/null || need="$need python3.11-venv"
command -v xvfb-run >/dev/null 2>&1 || need="$need xvfb"
[ -n "$need" ] || exit 0
echo "$(date -u +%FT%TZ) ставлю заново:$need" >> $W/log/supervisor.log
sudo apt-get update -qq && sudo apt-get install -y -qq $need
$W/venv/bin/playwright install-deps chromium >/dev/null 2>&1
