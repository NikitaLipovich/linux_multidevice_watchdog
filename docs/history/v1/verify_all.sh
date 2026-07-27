#!/bin/bash
# verify_all.sh — агрегатный DoD watchdog-v1: G1 + G2 + G4 + матрица v3 + флеш (№6).
# Печатает "watchdog-v1: DONE" + exit 0 только когда ВСЁ пройдено на живом стенде.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
KEY=/tmp/rut_key
RUT=root@192.168.1.1
SSH="ssh -i $KEY -oStrictHostKeyChecking=no -oUserKnownHostsFile=/dev/null -oConnectTimeout=8 $RUT"
WDLOG='/mnt/sda1/crash_logs/wd.log*'
ULOG='/mnt/sda1/crash_logs/unified.log*'
FAILS=0
chk() { if eval "$2" >/dev/null 2>&1; then echo "  ok: $1"; else echo "  FAIL: $1"; FAILS=$((FAILS+1)); fi; }
L() { $SSH "grep -q \"$1\" $WDLOG"; }

echo "== G2/G5 (бинарь) =="
SIZE=$(stat -c%s "$HERE/src/svc_watchdog.mipsel" 2>/dev/null || echo 999999)
chk "mipsel-бинарь есть и <150КБ ($SIZE)" "[ $SIZE -lt 153600 ]"

echo "== матрица v3 (свидетельства в wd.log на SD) =="
chk "wd работает (wd_start)"                    "L 'wd_start'"
chk "№0 observe-прогон чист"                    "L 'matrix0_observe_clean'"
chk "№1 no_pulse udp_logger"                    "L 'no_pulse name=udp_logger'"
chk "№1 service_restart udp_logger"             "L 'service_restart name=udp_logger'"
chk "№1 pulse_back udp_logger"                  "L 'pulse_back name=udp_logger'"
chk "Б-1 soft_restart_failed(escalate=yes)"     "L 'soft_restart_failed name=data_uploader.*escalate=yes'"
chk "Б-1 process_restart(soft_restart_failed)"  "L 'process_restart reason=soft_restart_failed'"
chk "Б-2 soft_restart_failed(escalate=no)"      "L 'soft_restart_failed name=udp_logger.*escalate=no'"
chk "№2 вис детектирован (no_pulse_all ИЛИ escalation->restart)" \
    "L 'process_restart reason=no_pulse_all' || L 'escalation name='"
chk "№3 fresh-гейт (procd respawn без дубля)"   "L 'fresh_process_detected'"
chk "№4 paused"                                 "L 'paused'"
chk "№5 waiting_resources"                      "L 'waiting_resources'"
chk "№7 wd_start ≥ 2 (respawn wd)" \
    "[ \$($SSH \"grep -hc wd_start $WDLOG | awk '{s+=\\\$1} END{print s}'\") -ge 2 ]"

echo "== №6 флеш под watchdog (совместно с пользователем) =="
chk "active-состояние в wd.log (флеш шёл под wd)" "L 'state name=config_server state=active'"
chk "lazy: flash tools loaded ровно при флеше"    "$SSH \"grep -q 'flash tools loaded' $ULOG\""

echo "== стенд здоров =="
chk "/health 200"  "[ \"\$($SSH 'curl -s -o /dev/null -w %{http_code} --max-time 5 http://127.0.0.1:8000/health')\" = 200 ]"
chk "ws :81"       "$SSH 'grep -qi \":0051 .* 0A \" /proc/net/tcp'"
chk "ровно 1 python" "[ \"\$($SSH 'pgrep -f \"run_services[.]py\" | wc -l' | tr -d '[:space:]')\" = 1 ]"
chk "watchdog под procd (running)" "$SSH 'ubus call service list \"{\\\"name\\\":\\\"svc_watchdog\\\"}\" | grep -q running'"

echo
if [ $FAILS -eq 0 ]; then echo "watchdog-v1: DONE"; exit 0; else echo "watchdog-v1: $FAILS FAIL(s)"; exit 1; fi
