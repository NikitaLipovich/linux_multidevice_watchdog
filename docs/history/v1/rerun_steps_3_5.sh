#!/bin/bash
# Точечный перегон шагов 3 и 5 матрицы v3 (после фиксов: top-tick fresh-гейт; порог 60МБ).
set -u
KEY=/tmp/rut_key
RUT=root@192.168.1.1
SSH="ssh -i $KEY -oStrictHostKeyChecking=no -oUserKnownHostsFile=/dev/null -oConnectTimeout=8 $RUT"
WDLOG=/mnt/sda1/crash_logs/wd.log
FAILS=0; OFFS=0
ok()  { echo "  ok: $1"; }
bad() { echo "  FAIL: $1"; FAILS=$((FAILS+1)); }
snap(){ OFFS=$($SSH "wc -l < $WDLOG" | tr -d '[:space:]'); }
since(){ $SSH "tail -n +$((OFFS+1)) $WDLOG | grep -q \"$1\""; }
count_since(){ $SSH "tail -n +$((OFFS+1)) $WDLOG | grep -c \"$1\"" | tr -d '[:space:]'; }
wait_since(){ local n=0; until since "$1"; do sleep 5; n=$((n+1)); if [ $n -ge ${2:-24} ]; then return 1; fi; done; return 0; }
health(){ $SSH "curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8000/health"; }
wait_health(){ local n=0; until [ "$(health)" = "200" ]; do sleep 5; n=$((n+1)); if [ $n -ge ${1:-36} ]; then return 1; fi; done; return 0; }

echo "=== подготовка: дождаться конца grace нового wd ==="
sleep 100
[ "$(health)" = "200" ] || { echo "FAIL: стенд нездоров"; exit 1; }

echo "=== ШАГ 3 (перегон): kill -9 unified — fresh-гейт в начале тика ==="
snap
PID=$($SSH "cat /tmp/rut_services.pid")
$SSH "kill -9 $PID"
sleep 60
wait_health 40 || bad "unified не поднялся после kill -9"
if [ "$(count_since 'process_restart')" = "0" ]; then
    ok "двойного рестарта НЕТ (fresh-детекты: $(count_since 'fresh_process_detected'))"
else
    bad "wd навесил рестарт поверх procd"
fi
sleep 125

echo "=== ШАГ 5 (перегон): resources-wait, порог 60МБ ==="
snap
$SSH "sed 's/\"min_free_mb\": 8/\"min_free_mb\": 60/' /usr/local/home/root/external-storage-contents/svc_watchdog.conf > /etc/svc_watchdog.conf && /etc/init.d/svc_watchdog restart"
sleep 125
$SSH "touch /tmp/wd_test_mute_udp_logger"
wait_since "waiting_resources" 24 && ok "waiting_resources (порог-трюк 60МБ)" || bad "нет waiting_resources"
[ "$(count_since 'service_restart name=udp_logger')" = "0" ] && ok "рестарт удержан при «нехватке»" || bad "рестартит при «нехватке»"
snap
$SSH "cp /usr/local/home/root/external-storage-contents/svc_watchdog.conf /etc/svc_watchdog.conf && /etc/init.d/svc_watchdog restart"
sleep 125
wait_since "service_restart name=udp_logger" 24 && ok "рестарт после возврата порога" || bad "нет рестарта после возврата"
$SSH "rm -f /tmp/wd_test_mute_udp_logger"
wait_since "pulse_back name=udp_logger" 18 && ok "pulse_back" || bad "пульс не вернулся"

H=$(health)
echo "=== финал: health=$H ==="
[ "$H" = "200" ] || bad "стенд нездоров"
if [ $FAILS -eq 0 ]; then echo "STEPS 3+5: ALL OK"; exit 0; else echo "STEPS 3+5: $FAILS FAIL(s)"; exit 1; fi
