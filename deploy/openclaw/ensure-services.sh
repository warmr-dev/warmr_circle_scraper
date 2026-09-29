#!/bin/sh
# Надзиратель вместо systemd: шлюз зовёт его раз в минуту, а шлюз -- PID 1,
# он жив всегда. Init-системы в контейнере нет, поэтому:
#  * никто не поднимает службы после перезапуска шлюза -- этим занят этот скрипт;
#  * никто не хоронит осиротевшие процессы, и мёртвая служба остаётся зомби.
# Зомби отвечает на kill -0 как живой, поэтому проверка идёт по состоянию в
# /proc, а не по факту существования pid. Измерено 2026-09-29: убитый воркер
# висел зомби, и первая версия сторожа считала его работающим.
W=$HOME/.openclaw/warmr
running() {
  p=$(cat "$W/state/$1.pid" 2>/dev/null) || return 1
  [ -n "$p" ] && [ -d "/proc/$p" ] || return 1
  grep -q "^State:[[:space:]]*Z" "/proc/$p/status" 2>/dev/null && return 1
  tr "\0" " " < "/proc/$p/cmdline" 2>/dev/null | grep -q circle-leads
}
for s in worker watcher; do
  running "$s" && continue
  setsid nohup "$W/bin/run-$s.sh" >> "$W/log/$s.log" 2>&1 &
  echo $! > "$W/state/$s.pid"
  echo "$(date -u +%FT%TZ) поднял $s" >> "$W/log/supervisor.log"
done
