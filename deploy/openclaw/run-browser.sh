#!/bin/sh
# Браузер для вступлений: headed Chrome в виртуальном экране, порт отладки
# только на localhost. Путь к Chromium берётся маской -- версия Playwright
# меняет номер ревизии, и вписанный в конфиг номер однажды всё уронит.
W=$HOME/.openclaw/warmr
CHROME=$(ls -d $W/browser/ms-playwright/chromium-*/chrome-linux64/chrome | tail -1)
CHROME_DEVEL_SANDBOX=$(dirname $CHROME)/chrome_sandbox
export CHROME_DEVEL_SANDBOX
exec xvfb-run -a --server-args="-screen 0 1280x900x24" "$CHROME" \
  --user-data-dir=$W/browser/profile \
  --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 \
  --no-first-run --no-default-browser-check --disable-dev-shm-usage \
  --window-size=1280,900 about:blank
