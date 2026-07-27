#!/bin/bash
# verify_g1.sh — DoD-проверка G1 (Python-сторона WATCHDOG MINIMAL v1).
# Запускается с лаптопа; всё на RUT — по ssh. Печатает "G1 OK" + exit 0 при успехе.
set -u
KEY=/tmp/rut_key
RUT=root@192.168.1.1
SSH="ssh -i $KEY -oStrictHostKeyChecking=no -oUserKnownHostsFile=/dev/null -oConnectTimeout=8 $RUT"
ULOG=/mnt/sda1/crash_logs/unified.log
CONF=/etc/svc_watchdog.conf

pass=0; step=""
fail() { echo "G1 FAIL [$step]: $1"; exit 1; }
note() { echo "  [$step] $1"; }

# [.] в паттернах — защита от self-match: busybox pgrep -f видит собственную ash-обёртку
py_count() { $SSH "pgrep -f 'python /usr/local/home/root/external-storage-contents/run_services[.]py' | wc -l" | tr -d '[:space:]'; }
health()   { $SSH "curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8000/health" ; }

wait_healthy() { # $1 = seconds budget
    local t=0
    while [ $t -lt "$1" ]; do
        [ "$(health)" = "200" ] && [ "$(py_count)" = "1" ] && return 0
        sleep 5; t=$((t+5))
    done
    return 1
}

step="1-python-count"
[ "$(py_count)" = "1" ] || fail "ожидался ровно 1 run_services.py, получили $(py_count)"
total=$($SSH "pgrep -f 'python .*[.]py' | wc -l" | tr -d '[:space:]')
[ "$total" = "1" ] || fail "лишние python-процессы: всего $total"
note "1 python (run_services.py)"

step="2-health"
[ "$(health)" = "200" ] || fail "/health != 200"
note "/health 200"

step="3-ws81"
$SSH "grep -qi ':0051 .* 0A ' /proc/net/tcp" || fail "порт :81 не слушается"
note "ws :81 LISTEN"

step="4-heartbeats"
names=$($SSH "rm -f /var/run/svc_wd.sock; python - <<'EOF'
import socket, time
s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
s.bind('/var/run/svc_wd.sock'); s.settimeout(1.0)
seen, t0 = set(), time.time()
while time.time() - t0 < 16:
    try:
        d, _ = s.recvfrom(64)
        seen.add(d.decode().split()[0])
    except socket.timeout:
        pass
print(' '.join(sorted(seen)))
EOF
rm -f /var/run/svc_wd.sock")
for svc in udp_logger data_uploader config_server ws_bridge; do
    echo "$names" | grep -qw "$svc" || fail "нет пульса от $svc (видели: '$names')"
done
note "4 пульса: $names"

step="5-crash-injection"
before=$($SSH "grep -c 'udp_logger up (start' $ULOG" | tr -d '[:space:]')
cs_before=$($SSH "grep -c 'config_server up (start' $ULOG" | tr -d '[:space:]')
$SSH "touch /tmp/svc_crash_udp_logger"
sleep 6
after=$($SSH "grep -c 'udp_logger up (start' $ULOG" | tr -d '[:space:]')
cs_after=$($SSH "grep -c 'config_server up (start' $ULOG" | tr -d '[:space:]')
[ "$after" -gt "$before" ] || fail "udp_logger не перезапустился (start-строк $before -> $after)"
[ "$cs_after" = "$cs_before" ] || fail "config_server перезапущен зря"
[ "$(py_count)" = "1" ] || fail "python-процессов стало $(py_count)"
note "точечный рестарт udp_logger работает"

step="6-lazy"
$SSH "grep -q 'flash tools loaded' $ULOG" && fail "флеш-модули загрузились на старте (lazy сломан)"
note "flash tools НЕ загружены (lazy ok)"

step="7-initd-restart"
$SSH "/etc/init.d/rut_services restart" >/dev/null 2>&1
wait_healthy 90 || fail "после init.d restart не поднялся за 90с"
note "init.d restart поднимает unified"

step="8-fail-open"
$SSH "mv $CONF /tmp/wdconf.g1bak && /etc/init.d/rut_services restart" >/dev/null 2>&1
wait_healthy 90 || { $SSH "mv /tmp/wdconf.g1bak $CONF; /etc/init.d/rut_services restart"; fail "fail-open: не поднялся без конфига"; }
$SSH "grep -q 'FAIL-OPEN' $ULOG" || { $SSH "mv /tmp/wdconf.g1bak $CONF"; fail "нет строки FAIL-OPEN в логе"; }
note "без конфига: поднялся + FAIL-OPEN в логе"

step="9-bad-entry"
$SSH "sed 's/run_services:start_udp_logger/run_services:no_such_function/' /tmp/wdconf.g1bak > $CONF && /etc/init.d/rut_services restart" >/dev/null 2>&1
wait_healthy 90 || { $SSH "mv /tmp/wdconf.g1bak $CONF; /etc/init.d/rut_services restart"; fail "с битым entry не поднялись соседи"; }
$SSH "grep -q 'SERVICE NOT STARTED' $ULOG" || { $SSH "mv /tmp/wdconf.g1bak $CONF"; fail "нет строки SERVICE NOT STARTED"; }
note "битый entry: сервис не стартовал, соседи живы"

step="10-restore"
$SSH "mv /tmp/wdconf.g1bak $CONF && /etc/init.d/rut_services restart" >/dev/null 2>&1
wait_healthy 120 || fail "финальное восстановление не поднялось"
note "конфиг восстановлен, стенд здоров"

echo "G1 OK"
exit 0
