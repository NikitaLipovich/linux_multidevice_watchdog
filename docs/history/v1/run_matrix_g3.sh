#!/bin/bash
# run_matrix_g3.sh v2 — матрица G3 (шаги 0..5 и 7). Шаг 6 (реальный флеш) — с пользователем.
# v2: все грепы ВРЕМЯ-СКОУПЛЕНЫ (офсет по строкам wd.log) — уроки первого прогона;
#     вис (шаг 2) легально детектится ЛЮБОЙ из двух цепочек (см. ниже); grace=90с.
set -u
KEY=/tmp/rut_key
RUT=root@192.168.1.1
SSH="ssh -i $KEY -oStrictHostKeyChecking=no -oUserKnownHostsFile=/dev/null -oConnectTimeout=8 $RUT"
WDLOG=/mnt/sda1/crash_logs/wd.log
HERE="$(cd "$(dirname "$0")" && pwd)"
FAILS=0; OFFS=0
ok()  { echo "  ok: $1"; }
bad() { echo "  FAIL: $1"; FAILS=$((FAILS+1)); }
mark(){ $SSH "echo \"\$(date +%Y-%m-%dT%H:%M:%S) $1\" >> $WDLOG"; }
snap(){ OFFS=$($SSH "wc -l < $WDLOG" | tr -d '[:space:]'); }              # точка отсчёта шага
since(){ $SSH "tail -n +$((OFFS+1)) $WDLOG | grep -q \"$1\""; }           # только НОВЫЕ строки
count_since(){ $SSH "tail -n +$((OFFS+1)) $WDLOG | grep -c \"$1\"" | tr -d '[:space:]'; }
wait_since(){ local n=0; until since "$1"; do sleep 5; n=$((n+1)); if [ $n -ge ${2:-24} ]; then return 1; fi; done; return 0; }
health(){ $SSH "curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8000/health"; }
wait_health(){ local n=0; until [ "$(health)" = "200" ]; do sleep 5; n=$((n+1)); if [ $n -ge ${1:-36} ]; then return 1; fi; done; return 0; }
set_actions(){ $SSH "sed 's/\"action\": \"restart\"/\"action\": \"$1\"/' \
    /usr/local/home/root/external-storage-contents/svc_watchdog.conf > /etc/svc_watchdog.conf && /etc/init.d/svc_watchdog restart"; }

echo "=== ШАГ 0: observe-прогон 10 мин под боевой нагрузкой ==="
snap
set_actions log
sleep 5
wait_since "wd_start" 6 || { bad "wd не стартовал в observe"; exit 1; }
python "$HERE/../svc-consolidation/load/bench_load.py" --duration 600 >/tmp/matrix_load.log 2>&1 &
LOAD_PID=$!
for i in $(seq 1 60); do
    sleep 10
    MEM=$($SSH "awk '/MemAvailable/{print int(\$2/1024)}' /proc/meminfo" 2>/dev/null || echo 99)
    if [ "${MEM:-99}" -lt 6 ]; then bad "CRITICAL-RAM ${MEM}MB — стоп"; kill $LOAD_PID 2>/dev/null; exit 1; fi
done
kill $LOAD_PID 2>/dev/null
if since "no_pulse"; then bad "шаг0: ложные no_pulse в observe"; $SSH "tail -n +$((OFFS+1)) $WDLOG | grep no_pulse | head -5"
else ok "шаг0: 10 мин нагрузки — ноль ложных no_pulse"; mark "matrix0_observe_clean"; fi

echo "=== переключение в enforce ==="
snap; set_actions restart; sleep 5
wait_since "wd_start" 6 || bad "wd не перезапустился в enforce"

echo "=== ШАГ 1: тихая смерть udp_logger (mute-хук); ждём конец grace 90с ==="
sleep 95
snap
$SSH "touch /tmp/wd_test_mute_udp_logger"
wait_since "service_restart name=udp_logger" 24 && ok "no_pulse -> service_restart" || bad "нет service_restart"
$SSH "rm -f /tmp/wd_test_mute_udp_logger"
wait_since "pulse_back name=udp_logger" 12 && ok "pulse_back" || bad "нет pulse_back"
[ "$(count_since 'service_restart name=ws_bridge\|service_restart name=data_uploader\|service_restart name=config_server')" = "0" ] \
    && ok "соседи не тронуты" || bad "задеты соседи"

echo "=== ШАГ 2: вис loop (hang-хук). Легальны ДВЕ цепочки: no_pulse_all->restart ИЛИ эскалация несъеденных crash-файлов->restart (супервизор завис вместе с loop) ==="
snap
$SSH "touch /tmp/wd_test_hang"
wait_since "process_restart" 40 && ok "process_restart пришёл" || bad "нет process_restart на вис"
since "no_pulse_all" && ok "цепочка: no_pulse_all" || { since "escalation" && ok "цепочка: escalation (crash-файл не съеден зависшим loop)" || bad "process_restart без понятной причины"; }
wait_health 40 || bad "unified не поднялся после process_restart"
wait_since "pulse_back\|wd_start" 40 || true
ok "unified снова здоров (health 200)"
sleep 100   # дождаться конца grace, чтобы шаг 3 стартовал с чистого листа

echo "=== ШАГ 3: kill -9 unified — procd respawn, wd НЕ дублирует ==="
snap
PID=$($SSH "cat /tmp/rut_services.pid")
$SSH "kill -9 $PID"
sleep 60
wait_health 40 || bad "unified не поднялся после kill -9"
if [ "$(count_since 'process_restart')" = "0" ]; then
    FR=$(count_since "fresh_process_detected")
    ok "двойного рестарта НЕТ (fresh-детектов: $FR)"
else
    bad "wd навесил рестарт поверх procd"
fi
sleep 100   # конец grace

echo "=== ШАГ 4: pause_file ==="
snap
$SSH "touch /tmp/wd_pause; touch /tmp/wd_test_mute_udp_logger"
wait_since "paused" 24 && ok "paused в логе" || bad "нет paused"
sleep 25
[ "$(count_since 'service_restart')" = "0" ] && ok "под паузой не действует" || bad "действие под паузой!"
$SSH "rm /tmp/wd_pause"
wait_since "service_restart name=udp_logger" 24 && ok "после rm pause — действует" || bad "не действует после rm pause"
$SSH "rm -f /tmp/wd_test_mute_udp_logger"
wait_since "pulse_back name=udp_logger" 12 || bad "пульс не вернулся после шага 4"

echo "=== ШАГ 5: resources-wait (порог 25МБ, БЕЗ голодания) ==="
snap
$SSH "sed 's/\"min_free_mb\": 8/\"min_free_mb\": 25/' /usr/local/home/root/external-storage-contents/svc_watchdog.conf > /etc/svc_watchdog.conf && /etc/init.d/svc_watchdog restart"
sleep 95    # grace нового wd
$SSH "touch /tmp/wd_test_mute_udp_logger"
wait_since "waiting_resources" 24 && ok "waiting_resources (порог-трюк)" || bad "нет waiting_resources"
[ "$(count_since 'service_restart name=udp_logger')" = "0" ] && ok "рестарт удержан при «нехватке»" || bad "рестартит при «нехватке»"
snap
$SSH "cp /usr/local/home/root/external-storage-contents/svc_watchdog.conf /etc/svc_watchdog.conf && /etc/init.d/svc_watchdog restart"
sleep 95    # grace
wait_since "service_restart name=udp_logger" 24 && ok "рестарт после возврата порога" || bad "нет рестарта после возврата"
$SSH "rm -f /tmp/wd_test_mute_udp_logger"
wait_since "pulse_back name=udp_logger" 12 || bad "пульс не вернулся после шага 5"

echo "=== ШАГ 7: смерть самого watchdog ==="
snap
WDPID=$($SSH "pgrep -f 'svc_watchdog /etc' | head -1")
$SSH "kill -9 $WDPID"
sleep 15
wait_since "wd_start" 6 && ok "procd переродил watchdog" || bad "wd не переродился"
sleep 100   # grace нового wd — не должно быть рестартов
[ "$(count_since 'process_restart\|service_restart')" = "0" ] && ok "после перерождения — тишина (grace)" || bad "ложные действия после перерождения wd"

echo
echo "=== стенд после матрицы ==="
H=$(health); P=$($SSH "pgrep -f 'run_services[.]py' | wc -l" | tr -d '[:space:]')
echo "health=$H pythons=$P"
[ "$H" = "200" ] || bad "стенд нездоров после матрицы"
if [ $FAILS -eq 0 ]; then echo "MATRIX 0-5,7: ALL OK (остался шаг 6 — флеш с пользователем)"; exit 0
else echo "MATRIX: $FAILS FAIL(s)"; exit 1; fi
