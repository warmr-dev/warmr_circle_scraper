#!/usr/bin/env bash
W=$HOME/preflight; mkdir -p "$W"
ok(){ echo "  OK    $*"; }; no(){ echo "  FAIL  $*"; }; q(){ echo "  ?     $*"; }
p(){ if command -v timeout >/dev/null; then timeout 8 bash -c "exec 3<>/dev/tcp/$1/$2" >/dev/null 2>&1
     else ( exec 3<>/dev/tcp/$1/$2 ) >/dev/null 2>&1 & pid=$!; ( sleep 8; kill -9 $pid 2>/dev/null ) & wait $pid 2>/dev/null; fi; }

case "${1:-start}" in
start)
echo "== 1. egress (провал здесь = переезда нет)"
p imap.gmail.com 993 && ok "IMAP 993" || no "IMAP 993 ЗАКРЫТ -> код из почты не прочитать"
p aws-0-ap-southeast-1.pooler.supabase.com 6543 && ok "Postgres 6543" || no "Postgres 6543 ЗАКРЫТ -> базы нет"
echo "  openrouter: $(curl -sI --max-time 10 https://openrouter.ai/api/v1/models | head -1)"
echo "== 2. машина"
echo "  user: $(id -un) uid=$(id -u)"
sudo -n true 2>/dev/null && ok "sudo без пароля" || q "sudo недоступен -> пакеты не поставить"
( . /etc/os-release 2>/dev/null; echo "  os: ${PRETTY_NAME:-?} | cpu: $(nproc) vCPU" )
free -m 2>/dev/null | awk '/Mem:/{print "  ram: "$2" MB, доступно "$7} /Swap:/{print "  swap: "$2" MB"}'
df -h / 2>/dev/null | awk 'NR==2{print "  disk: "$2", свободно "$4}'
echo "== 3. кто поднимает процессы"
echo "  pid1: $(cat /proc/1/comm 2>/dev/null)"
[ -d /run/systemd/system ] && ok "systemd работает" || q "systemd НЕТ -> нужен supervisord"
for c in cron crond supervisord s6-svscan runsv; do command -v $c >/dev/null && ok "есть $c"; done
crontab -l >/dev/null 2>&1 && ok "crontab доступен (@reboot возможен)" || q "crontab недоступен"
echo "== 4. песочница Chrome"
if unshare --user --map-root-user true 2>/dev/null; then ok "user namespaces разрешены -> песочница своя"
else q "user namespaces ЗАПРЕЩЕНЫ"
  if mount 2>/dev/null | grep -E ' on (/|/home|/opt) ' | grep -q nosuid
  then no "и nosuid -> браузер здесь невозможен (--no-sandbox не вариант)"
  else ok "nosuid нет -> setuid-помощник сработает"; fi; fi
echo "== 5. метки"
curl -s --max-time 10 https://api.ipify.org > "$W/ip"; echo "  внешний IP: $(cat "$W/ip")"
echo "marker-$(date -u +%s)" > "$W/mark"
dd if=/dev/urandom of="$W/blob" bs=1M count=200 status=none 2>/dev/null && sha256sum "$W/blob" > "$W/sha" && ok "200 MB записано и посчитано"
: > "$W/beat"
BEATER="while :; do date -u +%s >> '$W/beat'; sleep 10; done"
if command -v setsid >/dev/null; then setsid nohup sh -c "$BEATER" >/dev/null 2>&1 </dev/null &
else nohup sh -c "$BEATER" >/dev/null 2>&1 </dev/null & disown 2>/dev/null; fi
sleep 11; N=$(wc -l < "$W/beat" | tr -d ' ')
[ "${N:-0}" -ge 1 ] && ok "пульс идёт, тиков: $N" || no "пульс не запустился -- проверка отключения работать не будет"
echo; echo "Дальше: выйти из панели на 3 минуты -> bash pf.sh check -> перезапустить инстанс -> bash pf.sh check"
;;
check)
echo "== выжил ли процесс без вас"
T=$(wc -l < "$W/beat"); G=$(awk 'NR>1 && $1-x>25{c++}{x=$1}END{print c+0}' "$W/beat")
echo "  тиков: $T, разрывов больше 25 сек: $G"
if pgrep -f "sleep 10" >/dev/null 2>&1 && [ "$G" -eq 0 ]; then ok "жив, разрывов нет"
elif [ "$G" -gt 0 ]; then no "процесс останавливали, пока никто не смотрел -> слежка тут жить не может"
else no "пульс мёртв, никто его не поднял"; fi
echo "== выжил ли диск"
[ -f "$W/mark" ] && ok "метка на месте: $(cat "$W/mark")" || no "метка ПРОПАЛА -> диск сбрасывается"
sha256sum -c "$W/sha" >/dev/null 2>&1 && ok "200 MB целы -> хранилище настоящее" || no "файл пропал или побит"
echo "== сменился ли адрес"
B=$(cat "$W/ip" 2>/dev/null); N=$(curl -s --max-time 10 https://api.ipify.org)
echo "  было: ${B:-?} / стало: ${N:-?}"
[ -n "$N" ] && [ "$B" = "$N" ] && ok "адрес тот же" || no "адрес сменился -> сессия Circle будет рваться"
;;
esac
