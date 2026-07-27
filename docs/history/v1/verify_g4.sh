#!/bin/bash
# verify_g4.sh — DoD G4 (teardown-контракт): на живом стенде soak — для КАЖДОГО из 4
# сервисов 5 циклов mute→service_restart→rm→pulse_back; ноль «address in use»;
# NTP-циклы не множатся (частота попыток в 65с-окне до и после — одинаково низкая).
set -u
KEY=/tmp/rut_key
RUT=root@192.168.1.1
SSH="ssh -i $KEY -oStrictHostKeyChecking=no -oUserKnownHostsFile=/dev/null -oConnectTimeout=8 $RUT"
WDLOG=/mnt/sda1/crash_logs/wd.log
ULOG=/mnt/sda1/crash_logs/unified.log
FAILS=0; OFFS=0; UOFFS=0
ok()  { echo "  ok: $1"; }
fail(){ echo "G4 FAIL: $1"; exit 1; }
bad() { echo "  FAIL: $1"; FAILS=$((FAILS+1)); }
snap(){ OFFS=$($SSH "wc -l < $WDLOG" | tr -d '[:space:]'); }
usnap(){ UOFFS=$($SSH "wc -l < $ULOG" | tr -d '[:space:]'); }
since(){ $SSH "tail -n +$((OFFS+1)) $WDLOG | grep -q \"$1\""; }
count_since(){ $SSH "tail -n +$((OFFS+1)) $WDLOG | grep -c \"$1\"" | tr -d '[:space:]'; }
ucount_since(){ $SSH "tail -n +$((UOFFS+1)) $ULOG | grep -c \"$1\"" | tr -d '[:space:]'; }
wait_since(){ local n=0; until since "$1"; do sleep 5; n=$((n+1)); if [ $n -ge ${2:-24} ]; then return 1; fi; done; return 0; }
health(){ $SSH "curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8000/health"; }

echo "== подготовка: свежий wd (enforce, канонический конфиг) =="
$SSH "cp /usr/local/home/root/external-storage-contents/svc_watchdog.conf /etc/svc_watchdog.conf; /etc/init.d/svc_watchdog restart" >/dev/null 2>&1
sleep 95   # grace 90с
[ "$(health)" = "200" ] || fail "стенд нездоров перед soak"
usnap
NTP_BEFORE_WINDOW_START=$UOFFS
sleep 65
NTP_BASE=$(ucount_since "Attempting NTP sync")
ok "базовая частота NTP-попыток за 65с: $NTP_BASE (ожидаем <=3)"
[ "$NTP_BASE" -le 3 ] || bad "уже на старте NTP-шторм ($NTP_BASE за 65с)"
AIU_BASE=$($SSH "grep -c 'address in use' $ULOG" | tr -d '[:space:]')

for SVC in udp_logger data_uploader config_server ws_bridge; do
    echo "== soak $SVC: 5 циклов =="
    for i in 1 2 3 4 5; do
        snap
        $SSH "touch /tmp/wd_test_mute_$SVC"
        wait_since "service_restart name=$SVC" 24 || { $SSH "rm -f /tmp/wd_test_mute_$SVC"; fail "$SVC цикл $i: нет service_restart"; }
        $SSH "rm -f /tmp/wd_test_mute_$SVC"
        wait_since "pulse_back name=$SVC" 18 || fail "$SVC цикл $i: нет pulse_back (teardown не отработал?)"
        [ "$(count_since 'process_restart')" = "0" ] || fail "$SVC цикл $i: случился process_restart — мягкий путь не сработал"
        echo "  цикл $i ok"
    done
    ok "$SVC: 5/5 мягких циклов"
done

echo "== пост-проверки =="
AIU_AFTER=$($SSH "grep -c 'address in use' $ULOG" | tr -d '[:space:]')
[ "$AIU_AFTER" = "$AIU_BASE" ] && ok "ноль новых «address in use» ($AIU_BASE)" || bad "появились address in use: $AIU_BASE -> $AIU_AFTER"
usnap
sleep 65
NTP_AFTER=$(ucount_since "Attempting NTP sync")
[ "$NTP_AFTER" -le 3 ] && ok "NTP-попыток за 65с после soak: $NTP_AFTER (дублей циклов нет)" || bad "NTP-шторм после soak: $NTP_AFTER за 65с"
[ "$(health)" = "200" ] || bad "health != 200 после soak"
$SSH "grep -qi ':0051 .* 0A ' /proc/net/tcp" && ok "ws :81 жив" || bad "ws :81 умер"

if [ $FAILS -eq 0 ]; then echo "G4 OK"; exit 0; else echo "G4: $FAILS FAIL(s)"; exit 1; fi
