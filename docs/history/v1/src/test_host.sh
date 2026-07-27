#!/bin/bash
# Юнит-тесты логики svc_watchdog на ХОСТЕ (нативный бинарь, ms-тайминги, fake /proc).
# Прогоняет: старт, пульсы без ложняков, no_pulse→crash-файл→service_restart→pulse_back,
# эскалацию consume_timeout, no_pulse_all, pause_file, ресурсы (floor + min_free),
# backoff-режим, observe (process action=log), unknown_name, state-слово, ротацию,
# L2 progress-stall (T15-T18: замёрзший/растущий счётчик, 0=выкл, idle),
# observe-гейт consume-эскалации (T19, фикс v1.1) и fail-closed конфиг v1.2
# (T20: missing key = отказ, все пропуски разом; T21: длинное имя = отказ).
set -u
BIN=${BIN:-./svc_watchdog}
T=$(mktemp -d)
trap 'kill $WD_PID 2>/dev/null; kill $BEAT_ALL 2>/dev/null; rm -rf $T' EXIT
FAILS=0
ok()   { echo "  ok: $1"; }
bad()  { echo "  FAIL: $1"; FAILS=$((FAILS+1)); }
has()  { grep -q "$1" $T/wd.log; }
wait_for() { # $1=pattern $2=timeout_s
    local n=0; while ! grep -q "$1" $T/wd.log 2>/dev/null; do
        sleep 0.1; n=$((n+1)); [ $n -ge $(( ${2:-5} * 10 )) ] && return 1; done; return 0; }

mkconf() { # $1=delay_mode $2=proc_action $3=soft_enabled(true/false; пусто=false) $4=svc_set(std|esc)
# v1.2 fail-closed: КАЖДЫЙ ключ обязателен, поэтому конфиг всегда полный
# (в т.ч. soft_fail_escalation — «нет секции = выключено» больше не существует).
local SOFT="\"soft_fail_escalation\": {\"enabled\": ${3:-false}, \"after_attempts\": 3},"
local SVCS='
    {"name": "alpha", "wait_pulse_timeout_ms": 400, "action": "restart", "escalate_to_process": true,  "progress_stall_ms": 0},
    {"name": "beta",  "wait_pulse_timeout_ms": 400, "action": "log",     "escalate_to_process": true,  "progress_stall_ms": 0}'
[ "${4:-std}" = "esc" ] && SVCS='
    {"name": "alpha", "wait_pulse_timeout_ms": 400, "action": "restart", "escalate_to_process": true,  "progress_stall_ms": 0},
    {"name": "beta",  "wait_pulse_timeout_ms": 400, "action": "restart", "escalate_to_process": false, "progress_stall_ms": 0}'
[ "${4:-std}" = "stall" ] && SVCS='
    {"name": "alpha", "wait_pulse_timeout_ms": 400, "action": "restart", "escalate_to_process": true,  "progress_stall_ms": 500},
    {"name": "beta",  "wait_pulse_timeout_ms": 400, "action": "restart", "escalate_to_process": true,  "progress_stall_ms": 0}'
cat > $T/wd.conf <<EOF
{
  "socket": "$T/wd.sock", "beat_interval_ms": 100, "tick_ms": 100,
  "pause_file": "$T/pause",
  "log": {"file": "$T/wd.log", "max_bytes": 4000, "keep": 3, "fsync": false},
  "self_oom_adj": 0, "paused_log_every_ms": 60000, "unknown_name_log_every_ms": 0,
  "resources": {"rut_floor_mb": 5, "min_free_mb": 8, "max_load1": 50.0,
                "recheck_ms": 200, "max_wait_ms": 2000},
  "restart_delay": {"mode": "$1", "interval_ms": 300, "initial_ms": 2000, "factor": 2, "max_ms": 8000},
  "process": {"name": "unified", "initd": "$T/fake_initd",
              "pidfile": "$T/proc.pid",
              "crash_file_template": "$T/crash_{name}",
              $SOFT
              "consume_timeout_ms": 500, "service_relaunch_ms": 600, "start_grace_ms": 600, "action": "$2"},
  "services": [$SVCS
  ]
}
EOF
}

echo 1 > $T/proc.pid   # PID 1 = «старый» процесс: fresh-гейт пропускает рестарты

cat > $T/fake_initd <<EOF
#!/bin/bash
echo "\$1 \$(date +%s.%N)" >> $T/initd_calls
EOF
chmod +x $T/fake_initd

# fake /proc: healthy by default
mk_mem() { printf 'MemTotal: 125100 kB\nMemFree: 50000 kB\nMemAvailable: %s kB\n' "$1" > $T/meminfo; }
mk_mem 40000
echo "0.50 0.40 0.30 1/100 999" > $T/loadavg
export WD_PROC_MEMINFO=$T/meminfo WD_PROC_LOADAVG=$T/loadavg

beat() { python3 - "$1" "$T/wd.sock" <<'EOF'
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
try: s.sendto(sys.argv[1].encode(), sys.argv[2])
except OSError: pass
EOF
}
beat_loop() { while true; do beat "$1"; sleep 0.1; done; }

start_wd() { $BIN $T/wd.conf & WD_PID=$!; sleep 0.3; }
stop_wd()  { kill $WD_PID 2>/dev/null; wait $WD_PID 2>/dev/null; }

echo "== T1 старт + T2 пульсы без ложняков =="
mkconf fixed restart; : > $T/initd_calls
start_wd
wait_for "wd_start" 2 && ok "wd_start" || bad "нет wd_start"
beat_loop alpha & A=$!; beat_loop beta & B=$!; BEAT_ALL="$A $B"
sleep 1.5   # переживаем grace (0.6с) + ещё почти секунда под пульсами
has "no_pulse" && bad "ложный no_pulse при живых пульсах" || ok "ложняков нет"

echo "== T3 тихая смерть alpha -> crash-файл -> pulse_back =="
kill $A
wait_for "no_pulse name=alpha" 3 && ok "no_pulse alpha" || bad "нет no_pulse alpha"
wait_for "service_restart name=alpha attempt=1" 3 && ok "service_restart" || bad "нет service_restart"
[ -f $T/crash_alpha ] && ok "crash-файл создан" || bad "crash-файл отсутствует"
rm -f $T/crash_alpha            # супервизор «съел» файл
beat_loop alpha & A=$!; BEAT_ALL="$A $B"
wait_for "pulse_back name=alpha" 3 && ok "pulse_back" || bad "нет pulse_back"
grep -q "no_pulse name=beta" $T/wd.log && bad "beta задет зря" || ok "beta не тронут"

echo "== T4 эскалация: crash-файл никто не съел =="
kill $A
wait_for "service_restart name=alpha attempt=1.*\|service_restart name=alpha" 3 || true
wait_for "escalation name=alpha" 4 && ok "escalation" || bad "нет escalation"
wait_for "process_restart reason=escalation" 3 && ok "process_restart(escalation)" || bad "нет process_restart"
grep -q restart $T/initd_calls && ok "initd вызван" || bad "initd не вызван"

echo "== T5 no_pulse_all (beta log-only тоже участвует) =="
kill $B 2>/dev/null; : > $T/initd_calls
# после эскалации у нас свежий grace: ждём его + таймауты; никто не бьётся
wait_for "no_pulse_all" 6 && ok "no_pulse_all" || bad "нет no_pulse_all"
wait_for "process_restart reason=no_pulse_all" 4 && ok "process_restart(all)" || bad "нет process_restart(all)"

stop_wd

echo "== T5b fresh-процесс (procd respawn) НЕ рестартится повторно =="
# Реальный сценарий: kill -9 → procd моментально переродил процесс → пульсы
# ПРОПАЛИ (новый ещё импортируется), а процесс СВЕЖИЙ. wd должен дать grace,
# а не навешивать второй рестарт поверх procd'шного.
mkconf fixed restart; rm -f $T/wd.log; : > $T/initd_calls
sed -i 's/"start_grace_ms": 600/"start_grace_ms": 3000/' $T/wd.conf
start_wd
beat_loop alpha & A=$!; beat_loop beta & B=$!; BEAT_ALL="$A $B"
sleep 4                                     # переживаем grace на пульсах
kill $A $B 2>/dev/null                      # «процесс умер»: пульсы оборвались...
sleep 300 & FRESH=$!
echo $FRESH > $T/proc.pid                   # ...и procd тут же переродил (свежий pid)
calls_before=$(wc -l < $T/initd_calls)
wait_for "fresh_process_detected" 5 && ok "fresh_process_detected" || bad "нет fresh_process_detected"
sleep 1
calls_now=$(wc -l < $T/initd_calls)
[ "$calls_now" = "$calls_before" ] && ok "двойного рестарта нет" || bad "рестарт поверх свежего процесса!"
wait_for "process_restart reason=no_pulse_all" 8 && ok "постаревший + молчит -> рестарт всё же случился" \
    || bad "рестарт так и не пришёл после старения"
kill $FRESH 2>/dev/null
echo 1 > $T/proc.pid
stop_wd

echo "== T6 pause_file =="
mkconf fixed restart; rm -f $T/wd.log $T/crash_alpha; : > $T/initd_calls
touch $T/pause
start_wd
sleep 1.5   # grace прошёл, все молчат, но стоит пауза
has "paused" && ok "paused в логе" || bad "нет paused"
has "process_restart\|service_restart" && bad "действие под паузой!" || ok "действий нет"
rm $T/pause
wait_for "process_restart" 4 && ok "после снятия паузы действует" || bad "не действует после rm pause"
stop_wd

echo "== T7 ресурсы: floor-сигнал + ожидание min_free =="
mkconf fixed restart; rm -f $T/wd.log $T/crash_alpha; : > $T/initd_calls
mk_mem 3000    # 2.9МБ < floor 5 < min_free 8
start_wd
wait_for "mem_below_floor" 2 && ok "mem_below_floor" || bad "нет mem_below_floor"
beat_loop beta & B=$!; BEAT_ALL="$B"   # alpha молчит -> захочет рестарт, но памяти «нет»
wait_for "waiting_resources target=alpha" 4 && ok "waiting_resources" || bad "нет waiting_resources"
has "service_restart name=alpha" && bad "рестарт при голоде!" || ok "рестарт удержан"
mk_mem 40000   # память «вернулась»
wait_for "service_restart name=alpha" 4 && ok "рестарт после восстановления" || bad "нет рестарта после восстановления"
stop_wd; kill $B 2>/dev/null

echo "== T8 backoff: attempt=2 не раньше initial_ms =="
mkconf backoff restart; rm -f $T/wd.log $T/crash_alpha
start_wd
beat_loop beta & B=$!; BEAT_ALL="$B"
wait_for "service_restart name=alpha attempt=1" 4 || bad "нет attempt=1"
t1=$(date +%s); rm -f $T/crash_alpha    # «съели», но alpha всё молчит
wait_for "service_restart name=alpha attempt=2" 8 && t2=$(date +%s) || { bad "нет attempt=2"; t2=$t1; }
gap=$((t2-t1))
[ $gap -ge 2 ] && ok "backoff-пауза ${gap}s >= 2s (initial=2000)" || bad "backoff-пауза ${gap}s < 2s"
stop_wd; kill $B 2>/dev/null

echo "== T9 observe: process action=log НЕ рестартит =="
mkconf fixed log; rm -f $T/wd.log; : > $T/initd_calls
start_wd
sleep 2      # все молчат: должен быть только лог
has "no_pulse_all" && ok "no_pulse_all в observe" || bad "нет no_pulse_all"
[ -s $T/initd_calls ] && bad "observe вызвал initd!" || ok "initd НЕ вызван"
stop_wd

echo "== T10 unknown_name + state-слово + ротация =="
mkconf fixed restart; rm -f $T/wd.log*
start_wd
beat "ghost"; sleep 0.3
has "unknown_name name=ghost" && ok "unknown_name" || bad "нет unknown_name"
beat "alpha active"; sleep 0.3
has "state name=alpha state=active" && ok "state-слово" || bad "нет state"
beat "alpha active"; sleep 0.3
[ "$(grep -c 'state name=alpha state=active' $T/wd.log)" = "1" ] && ok "state без повтора" || bad "state задублирован"
for i in $(seq 1 200); do beat "ghost"; done; sleep 1
[ -f $T/wd.log.1 ] && ok "ротация (wd.log.1 есть)" || bad "ротации не было"
stop_wd

echo "== T11 митигация «Б»: escalate_to_process=true -> process_restart после 3 попыток =="
mkconf fixed restart true esc; rm -f $T/wd.log $T/crash_*; : > $T/initd_calls
start_wd
beat_loop beta & B=$!; BEAT_ALL="$B"     # beta бьётся (не all-silent), alpha молчит
# «супервизор» ест crash-файлы alpha, но пульс не возвращается (сломанный teardown)
( while true; do rm -f $T/crash_alpha; sleep 0.15; done ) & EATER=$!
wait_for "service_restart name=alpha attempt=3" 8 || bad "T11: нет attempt=3"
wait_for "soft_restart_failed name=alpha.*escalate=yes" 4 && ok "soft_restart_failed(escalate=yes)" || bad "нет soft_restart_failed"
wait_for "process_restart reason=soft_restart_failed" 4 && ok "process_restart(soft_restart_failed)" || bad "нет process_restart"
kill $EATER 2>/dev/null; stop_wd; kill $B 2>/dev/null

echo "== T12 митигация «Б»: escalate_to_process=false -> ТОЛЬКО лог, процесс не тронут =="
mkconf fixed restart true esc; rm -f $T/wd.log $T/crash_*; : > $T/initd_calls
start_wd
beat_loop alpha & A=$!; BEAT_ALL="$A"    # alpha бьётся, beta (esc=false) молчит
( while true; do rm -f $T/crash_beta; sleep 0.15; done ) & EATER=$!
wait_for "soft_restart_failed name=beta.*escalate=no" 10 && ok "soft_restart_failed(escalate=no)" || bad "T12: нет soft_restart_failed(no)"
wait_for "service_restart name=beta attempt=5" 6 && ok "мягкие попытки продолжаются (attempt=5)" || bad "T12: попытки остановились"
grep -q "process_restart" $T/wd.log && bad "T12: процесс тронут при esc=false!" || ok "процесс НЕ тронут"
kill $EATER 2>/dev/null; stop_wd; kill $A 2>/dev/null

echo "== T13 глобальный enabled=false: даже esc=true НЕ эскалирует; сброс счётчика по pulse_back =="
mkconf fixed restart false esc; rm -f $T/wd.log $T/crash_*; : > $T/initd_calls
start_wd
beat_loop beta & B=$!; BEAT_ALL="$B"
( while true; do rm -f $T/crash_alpha; sleep 0.15; done ) & EATER=$!
wait_for "soft_restart_failed name=alpha.*escalate=no" 10 && ok "enabled=false -> escalate=no" || bad "T13: нет soft_restart_failed(no)"
grep -q "process_restart" $T/wd.log && bad "T13: эскалация при enabled=false!" || ok "процесс НЕ тронут (enabled=false)"
kill $EATER 2>/dev/null
beat_loop alpha & A=$!; BEAT_ALL="$A $B"          # пульс вернулся -> счётчик обнулился
wait_for "pulse_back name=alpha" 4 || bad "T13: нет pulse_back"
kill $A 2>/dev/null
( while true; do rm -f $T/crash_alpha; sleep 0.15; done ) & EATER=$!
n=0; until [ "$(grep -c 'service_restart name=alpha attempt=1 ' $T/wd.log)" -ge 2 ]; do
    sleep 0.2; n=$((n+1)); [ $n -ge 30 ] && break; done
[ "$(grep -c 'service_restart name=alpha attempt=1 ' $T/wd.log)" -ge 2 ] \
    && ok "после pulse_back счётчик снова с attempt=1" || bad "T13: счётчик не сброшен"
kill $EATER 2>/dev/null; stop_wd; kill $B 2>/dev/null

echo "== T15 L2: замёрзший счётчик при active -> stalled -> service_restart(stalled) =="
mkconf fixed restart "" stall; rm -f $T/wd.log $T/crash_* $T/ate_alpha; : > $T/initd_calls
start_wd
beat_loop alpha & A=$!; beat_loop beta & B=$!; BEAT_ALL="$A $B"
# «супервизор»: ест crash-файл сразу (beat-спавны медленные — без едока файл
# провисит дольше consume_timeout 500мс и уйдёт в ЛЕГАЛЬНУЮ эскалацию)
( while true; do [ -f $T/crash_alpha ] && { rm -f $T/crash_alpha; touch $T/ate_alpha; }; sleep 0.1; done ) & EATER=$!
sleep 0.8                                    # переживаем grace (0.6с)
for i in $(seq 1 12); do beat "alpha active 7"; sleep 0.12; done   # счётчик ЗАМОРОЖЕН ~1.4с > stall 500мс
wait_for "stalled name=alpha counter=7" 3 && ok "stalled залогирован" || bad "нет stalled"
wait_for "service_restart name=alpha attempt=1 reason=stalled" 3 && ok "service_restart(stalled)" || bad "нет restart(stalled)"
sleep 0.3
[ -f $T/ate_alpha ] && ok "crash-файл создан и съеден" || bad "crash-файл не появился"
n1=$(grep -c "stalled name=alpha" $T/wd.log)
sleep 1.2
n2=$(grep -c "stalled name=alpha" $T/wd.log)
[ "$n1" = "$n2" ] && ok "эпизод сброшен после consume (stalled не повторяется)" || bad "stalled повторился после consume"
grep -q "process_restart" $T/wd.log && bad "T15: процесс тронут" || ok "процесс не тронут"
kill $EATER 2>/dev/null; stop_wd; kill $A $B 2>/dev/null

echo "== T16 L2 выключен (progress_stall_ms=0): замёрзший счётчик НЕ трогается =="
mkconf fixed restart "" stall; rm -f $T/wd.log $T/crash_*
start_wd
beat_loop alpha & A=$!; beat_loop beta & B=$!; BEAT_ALL="$A $B"
sleep 0.8
for i in $(seq 1 12); do beat "beta active 3"; sleep 0.12; done    # beta: stall_ms=0
grep -q "stalled name=beta" $T/wd.log && bad "stalled при stall_ms=0!" || ok "0=выкл работает"
stop_wd; kill $A $B 2>/dev/null

echo "== T17 L2: растущий счётчик -> НЕ stalled =="
mkconf fixed restart "" stall; rm -f $T/wd.log $T/crash_*
start_wd
beat_loop beta & B=$!; BEAT_ALL="$B"
sleep 0.8
i=0; while [ $i -lt 15 ]; do beat "alpha active $i"; i=$((i+1)); sleep 0.12; done  # ~1.8с > stall 500мс
grep -q "stalled name=alpha" $T/wd.log && bad "ложный stalled при растущем счётчике" || ok "растущий счётчик чист"
stop_wd; kill $B 2>/dev/null

echo "== T18 L2: idle закрывает эпизод -> stalled НЕ приходит =="
mkconf fixed restart "" stall; rm -f $T/wd.log $T/crash_*
start_wd
beat_loop alpha & A=$!; beat_loop beta & B=$!; BEAT_ALL="$A $B"
sleep 0.8
beat "alpha active 9"; sleep 0.2; beat "alpha idle"
sleep 1.2                                    # > stall 500мс после заморозки счётчика
grep -q "stalled name=alpha" $T/wd.log && bad "stalled после idle!" || ok "idle закрыл эпизод"
stop_wd; kill $A $B 2>/dev/null

echo "== T19 observe-фикс: process action=log -> consume-эскалация НЕ рестартит процесс =="
mkconf fixed log; rm -f $T/wd.log $T/crash_*; : > $T/initd_calls
start_wd
beat_loop beta & B=$!; BEAT_ALL="$B"         # beta бьётся (не all-silent), alpha молчит, файл никто не ест
wait_for "escalation name=alpha" 6 && ok "escalation залогирована" || bad "нет escalation"
sleep 1
grep -q "process_restart" $T/wd.log && bad "observe рестартнул процесс!" || ok "процесс НЕ тронут"
[ -s $T/initd_calls ] && bad "initd вызван в observe!" || ok "initd не вызван"
stop_wd; kill $B 2>/dev/null

echo "== T20 fail-closed: отсутствие ЛЮБОГО ключа = отказ старта (v1.2) =="
mkconf fixed restart "" std
# по ключу из каждой секции: корень, log, resources, restart_delay, process,
# soft_fail_escalation, services[]
for K in pause_file keep max_load1 mode consume_timeout_ms after_attempts escalate_to_process; do
    python3 - "$T/wd.conf" "$K" > $T/wd_missing.conf <<'EOF'
import json, sys
cfg = json.load(open(sys.argv[1])); key = sys.argv[2]
def strip(o):
    if isinstance(o, dict):
        o.pop(key, None)
        for v in o.values(): strip(v)
    elif isinstance(o, list):
        for v in o: strip(v)
strip(cfg); print(json.dumps(cfg))
EOF
    OUT=$(timeout 2 $BIN $T/wd_missing.conf 2>&1); RC=$?
    if [ $RC -ne 0 ] && [ $RC -ne 124 ] && echo "$OUT" | grep -q "missing key.*$K"; then
        ok "без '$K' не стартует (missing key)"
    else
        bad "T20: без '$K' rc=$RC out=$OUT"
    fi
done
# все проблемы должны печататься РАЗОМ (не по одной за прогон)
python3 - "$T/wd.conf" > $T/wd_missing.conf <<'EOF'
import json, sys
cfg = json.load(open(sys.argv[1]))
del cfg["tick_ms"]; del cfg["log"]["fsync"]; del cfg["process"]["initd"]
print(json.dumps(cfg))
EOF
OUT=$(timeout 2 $BIN $T/wd_missing.conf 2>&1)
N=$(echo "$OUT" | grep -c "missing key")
[ "$N" -ge 3 ] && ok "все пропуски перечислены разом ($N строк)" || bad "T20: перечислено $N/3"

echo "== T21 fail-closed: слишком длинное имя/значение = отказ (не молчаливое усечение) =="
mkconf fixed restart "" std
LONGNAME=$(printf 'x%.0s' $(seq 1 60))     # 60 > NAME_MAX_LEN-1=47
sed "s/\"alpha\"/\"$LONGNAME\"/" $T/wd.conf > $T/wd_long.conf
OUT=$(timeout 2 $BIN $T/wd_long.conf 2>&1); RC=$?
if [ $RC -ne 0 ] && [ $RC -ne 124 ] && echo "$OUT" | grep -q "too long"; then
    ok "имя 60 символов отвергнуто (too long)"
else
    bad "T21: длинное имя rc=$RC out=$OUT"
fi

echo "== T14 up= в каждой строке лога =="
TOTAL=$(wc -l < $T/wd.log); WITH_UP=$(grep -c "up=" $T/wd.log)
[ "$TOTAL" = "$WITH_UP" ] && ok "up= во всех $TOTAL строках" || bad "up= не везде ($WITH_UP/$TOTAL)"

echo
if [ $FAILS -eq 0 ]; then echo "HOST TESTS: ALL OK"; exit 0; else echo "HOST TESTS: $FAILS FAIL(s)"; exit 1; fi
