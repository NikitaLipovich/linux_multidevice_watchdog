#!/bin/bash
# run_matrix_g6.sh — матрица v3 (G6). Шаги: 0 observe → enforce → 1 smoke-цикл →
# Б-тест (обе стороны) → 2 вис → 3 kill-9 → 4 pause → 5 resources → 7 смерть wd.
# Шаг 6 (реальный флеш) — НЕ здесь: стоп и позвать пользователя.
# v3-уроки: офсетные грепы; после каждого process_restart ждать health+grace+30;
# бюджеты ×2; тест «Б» учитывает service_relaunch_ms=60с.
set -u
KEY=/tmp/rut_key
RUT=root@192.168.1.1
SSH="ssh -i $KEY -oStrictHostKeyChecking=no -oUserKnownHostsFile=/dev/null -oConnectTimeout=8 $RUT"
WDLOG=/mnt/sda1/crash_logs/wd.log
HERE="$(cd "$(dirname "$0")" && pwd)"
FAILS=0; OFFS=0
ok()  { echo "  ok: $1"; }
bad() { echo "  FAIL: $1"; FAILS=$((FAILS+1)); }
mark(){ $SSH "echo \"\$(date +%Y-%m-%dT%H:%M:%S) up=test $1\" >> $WDLOG"; }
snap(){ OFFS=$($SSH "wc -l < $WDLOG" | tr -d '[:space:]'); }
since(){ $SSH "tail -n +$((OFFS+1)) $WDLOG | grep -q \"$1\""; }
count_since(){ $SSH "tail -n +$((OFFS+1)) $WDLOG | grep -c \"$1\"" | tr -d '[:space:]'; }
wait_since(){ local n=0; until since "$1"; do sleep 5; n=$((n+1)); if [ $n -ge ${2:-24} ]; then return 1; fi; done; return 0; }
health(){ $SSH "curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8000/health"; }
wait_health(){ local n=0; until [ "$(health)" = "200" ]; do sleep 5; n=$((n+1)); if [ $n -ge ${1:-36} ]; then return 1; fi; done; return 0; }
settle(){ wait_health 40 || bad "стенд не ожил ($1)"; sleep 125; }   # health + grace90 + запас
set_actions(){ $SSH "sed 's/\"action\": \"restart\"/\"action\": \"$1\"/' \
    /usr/local/home/root/external-storage-contents/svc_watchdog.conf > /etc/svc_watchdog.conf && /etc/init.d/svc_watchdog restart"; }

echo "=== ШАГ 0: observe-прогон 10 мин под боевой нагрузкой ==="
snap
set_actions log
sleep 8
wait_since "wd_start" 6 || { bad "wd не стартовал в observe"; exit 1; }
python "$HERE/../svc-consolidation/load/bench_load.py" --duration 600 >/tmp/matrix_load.log 2>&1 &
LOAD_PID=$!
for i in $(seq 1 60); do
    sleep 10
    MEM=$($SSH "awk '/MemAvailable/{print int(\$2/1024)}' /proc/meminfo" 2>/dev/null || echo 99)
    if [ "${MEM:-99}" -lt 6 ]; then bad "CRITICAL-RAM ${MEM}MB — стоп"; kill $LOAD_PID 2>/dev/null; exit 1; fi
done
kill $LOAD_PID 2>/dev/null
if since "no_pulse"; then bad "шаг0: ложные no_pulse в observe"
else ok "шаг0: 10 мин нагрузки — ноль ложных no_pulse"; mark "matrix0_observe_clean"; fi

echo "=== enforce + запас после grace ==="
snap; set_actions restart; sleep 8
wait_since "wd_start" 6 || bad "wd не перезапустился в enforce"
sleep 125

echo "=== ШАГ 1 (smoke): одиночный мягкий цикл udp_logger ==="
snap
$SSH "touch /tmp/wd_test_mute_udp_logger"
wait_since "service_restart name=udp_logger" 24 && ok "no_pulse -> service_restart" || bad "нет service_restart"
$SSH "rm -f /tmp/wd_test_mute_udp_logger"
wait_since "pulse_back name=udp_logger" 18 && ok "pulse_back" || bad "нет pulse_back"
[ "$(count_since 'service_restart name=ws_bridge\|service_restart name=data_uploader\|service_restart name=config_server')" = "0" ] \
    && ok "соседи не тронуты" || bad "задеты соседи"

echo "=== ШАГ Б-1: soft_fail c escalate_to_process=TRUE (data_uploader): mute БЕЗ rm -> 3 попытки -> process_restart ==="
snap
$SSH "touch /tmp/wd_test_mute_data_uploader"
# 3 попытки × (детект + relaunch 60с) ≈ 4 мин; бюджет 6 мин
wait_since "soft_restart_failed name=data_uploader.*escalate=yes" 72 && ok "soft_restart_failed(escalate=yes)" || bad "нет soft_restart_failed(yes)"
wait_since "process_restart reason=soft_restart_failed" 12 && ok "process_restart(soft_restart_failed)" || bad "нет process_restart(Б)"
$SSH "rm -f /tmp/wd_test_mute_data_uploader"
settle "после Б-1"
ok "стенд ожил после митигации Б"

echo "=== ШАГ Б-2: escalate_to_process=FALSE (udp_logger): только лог, процесс НЕ трогаем ==="
snap
$SSH "touch /tmp/wd_test_mute_udp_logger"
wait_since "soft_restart_failed name=udp_logger.*escalate=no" 72 && ok "soft_restart_failed(escalate=no)" || bad "нет soft_restart_failed(no)"
sleep 20
[ "$(count_since 'process_restart')" = "0" ] && ok "процесс НЕ тронут (esc=false)" || bad "процесс тронут при esc=false!"
$SSH "rm -f /tmp/wd_test_mute_udp_logger"
wait_since "pulse_back name=udp_logger" 18 || bad "пульс udp_logger не вернулся после Б-2"

echo "=== ШАГ 2: вис loop (hang-хук). Валидны обе цепочки: no_pulse_all ИЛИ эскалации несъеденных файлов ==="
snap
$SSH "touch /tmp/wd_test_hang"
wait_since "process_restart" 48 && ok "process_restart на вис" || bad "нет process_restart на вис"
if since "no_pulse_all"; then ok "цепочка: no_pulse_all"
elif since "escalation"; then ok "цепочка: escalation (loop завис вместе с супервизором)"
else bad "process_restart без понятной причины"; fi
settle "после виса"
ok "unified снова здоров"

echo "=== ШАГ 3: kill -9 unified — procd respawn, wd НЕ дублирует (fresh-гейт) ==="
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

echo "=== ШАГ 4: pause_file ==="
snap
$SSH "touch /tmp/wd_pause; touch /tmp/wd_test_mute_udp_logger"
wait_since "paused" 24 && ok "paused в логе" || bad "нет paused"
sleep 25
[ "$(count_since 'service_restart')" = "0" ] && ok "под паузой не действует" || bad "действие под паузой!"
$SSH "rm /tmp/wd_pause"
wait_since "service_restart name=udp_logger" 24 && ok "после rm pause — действует" || bad "не действует после rm pause"
$SSH "rm -f /tmp/wd_test_mute_udp_logger"
wait_since "pulse_back name=udp_logger" 18 || bad "пульс не вернулся после шага 4"

echo "=== ШАГ 5: resources-wait (порог 60МБ, БЕЗ голодания) ==="
snap
$SSH "sed 's/\"min_free_mb\": 8/\"min_free_mb\": 60/' /usr/local/home/root/external-storage-contents/svc_watchdog.conf > /etc/svc_watchdog.conf && /etc/init.d/svc_watchdog restart"
sleep 125    # grace нового wd + запас
$SSH "touch /tmp/wd_test_mute_udp_logger"
wait_since "waiting_resources" 24 && ok "waiting_resources (порог-трюк)" || bad "нет waiting_resources"
[ "$(count_since 'service_restart name=udp_logger')" = "0" ] && ok "рестарт удержан при «нехватке»" || bad "рестартит при «нехватке»"
snap
$SSH "cp /usr/local/home/root/external-storage-contents/svc_watchdog.conf /etc/svc_watchdog.conf && /etc/init.d/svc_watchdog restart"
sleep 125
wait_since "service_restart name=udp_logger" 24 && ok "рестарт после возврата порога" || bad "нет рестарта после возврата"
$SSH "rm -f /tmp/wd_test_mute_udp_logger"
wait_since "pulse_back name=udp_logger" 18 || bad "пульс не вернулся после шага 5"

echo "=== ШАГ 7: смерть самого watchdog ==="
snap
WDPID=$($SSH "pgrep -f 'svc_watchdog /etc' | head -1")
$SSH "kill -9 $WDPID"
sleep 15
wait_since "wd_start" 8 && ok "procd переродил watchdog" || bad "wd не переродился"
sleep 125   # grace нового wd
[ "$(count_since 'process_restart\|service_restart')" = "0" ] && ok "после перерождения — тишина (grace)" || bad "ложные действия после перерождения wd"

echo
H=$(health); P=$($SSH "pgrep -f 'run_services[.]py' | wc -l" | tr -d '[:space:]')
echo "=== стенд после матрицы: health=$H pythons=$P ==="
[ "$H" = "200" ] || bad "стенд нездоров после матрицы"
if [ $FAILS -eq 0 ]; then echo "MATRIX-V3 0-5,7+Б: ALL OK (остался шаг 6 — флеш с пользователем)"; exit 0
else echo "MATRIX-V3: $FAILS FAIL(s)"; exit 1; fi
