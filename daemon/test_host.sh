#!/bin/bash
# test_host.sh — host tests for the C daemon v2 (native build inside the container).
# Validator negatives (fail-closed, unknown key, type, tries, stop-on-process,
# dead<3xevery, dead pacing, duplicate name, broken @-references) + behavioral
# (L1->action, P2->restart, 2 processes: vision goes silent -> restart vision, rate_limit cooldown).
set -u
BIN=./svc_watchdog
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
FAILED=0
fail(){ echo "  HOST-FAIL: $1"; FAILED=1; }
pass(){ echo "  ok: $1"; }

# base VALID config (fast timings)
cat > "$W/good.conf" <<EOF
{
  "schema": 2,
  "framework": {
    "transport": { "type": "unix_datagram", "unix_datagram": { "socket": "$W/wd.sock", "format": "text_v1" } },
    "observer": { "mode": "act", "tick_ms": 100, "pause_file": "$W/pause", "oom_adj": 0,
      "log": { "file": "$W/wd.log", "rotate_kb": 100, "keep": 2, "fsync": false },
      "quiet": { "paused_ms": 60000, "unknown_ms": 60000 } },
    "gates": { "min_free_mb": 8, "max_load1": 500.0, "recheck_ms": 100, "force_after_ms": 2000, "alarm_mb": 5 },
    "pacing": { "type": "fixed", "delay_ms": 100 }
  },
  "actions": {
    "recreate": { "type": "request_file", "file": "$W/crash_{service}", "eat_within_ms": 400, "startup_ms": 300,
      "rate_limit": { "max": 6, "per_ms": 100000, "on_exceeded": "cooldown", "cooldown_ms": 60000 } }
  },
  "ladders": {
    "soft": [ { "do": "@recreate" } ],
    "esc": [ { "do": "@recreate", "tries": 2 }, { "do": "restart_process" } ],
    "po": [ { "do": "restart_process" } ]
  },
  "processes": {
    "unified": {
      "launch": { "type": "init_script", "script": "$W/initd_unified", "pidfile": "$W/unified.pid",
        "grace": { "type": "fixed", "ms": 300 },
        "restart_rate_limit": { "max": 5, "per_ms": 600000, "on_exceeded": "cooldown", "cooldown_ms": 300000 } },
      "supervisor": { "poll_ms": 100, "stop_timeout_ms": 2000, "min_stable_ms": 500,
        "max_consecutive_start_failures": 3, "backoff": { "start_ms": 100, "factor": 2, "cap_ms": 1000 },
        "log": { "file": "$W/u.log", "rotate_kb": 100, "keep": 2, "fallbacks": [] } },
      "watch": { "P1_all_pulses_lost": { "mode": "act", "ladder": "@po" },
                 "P2_request_stuck": { "mode": "act", "ladder": "@po" } },
      "services": {
        "alpha": { "start": { "type": "python", "entry": "m:f" },
          "signals": { "pulse": { "from": "loop", "every_ms": 100 } },
          "watch": { "L1_pulse_lost": { "mode": "act", "dead_after_ms": 400, "ladder": "@soft" } } },
        "beta": { "start": { "type": "python", "entry": "m:f" },
          "signals": { "pulse": { "from": "loop", "every_ms": 100 } },
          "watch": { "L1_pulse_lost": { "mode": "act", "dead_after_ms": 400, "ladder": "@esc" } } }
      }
    }
  }
}
EOF
echo 1 > "$W/unified.pid"       # PID 1 = old: fresh-gate does not interfere
printf '#!/bin/sh\necho called >> %s/initd_unified_calls\n' "$W" > "$W/initd_unified"
chmod +x "$W/initd_unified"

# mutator: applies a python expression to good.conf → t.conf
mutate(){ python3 -c "import json,sys; c=json.load(open('$W/good.conf')); exec(sys.argv[1]); json.dump(c,open('$W/t.conf','w'))" "$1"; }
reject(){ # $1 conf, $2 substr, $3 name
  out=$("$BIN" "$1" 2>&1); rc=$?
  [ $rc -eq 1 ] || { fail "$3: expected exit 1 got $rc"; return; }
  echo "$out" | grep -q "$2" || { fail "$3: stderr lacks '$2': $out"; return; }
  pass "$3 (rejected: $2)"
}

echo "=== validator (fail-closed) ==="
mutate "c['zzz']={}";                                              reject "$W/t.conf" "unknown key zzz" "unknown_top_key"
mutate "c['framework']['observer']['zzz']=1";                      reject "$W/t.conf" "unknown key" "unknown_nested_key"
mutate "c['framework']['transport']['type']='pigeon'";            reject "$W/t.conf" "transport.type unknown" "unknown_type"
mutate "del c['framework']['gates']['min_free_mb']";              reject "$W/t.conf" "missing key" "missing_key"
mutate "c['ladders']['esc'][0]['tries']=0";                       reject "$W/t.conf" "tries must be >=1" "tries_zero"
mutate "c['ladders']['esc']=[{'do':'@recreate'},{'do':'restart_process'}]"; reject "$W/t.conf" "non-last step without tries" "nonlast_no_tries"
mutate "c['processes']['unified']['launch']['restart_rate_limit']['on_exceeded']='stop'"; reject "$W/t.conf" "forbidden on process restart" "stop_on_process"
mutate "c['processes']['unified']['services']['alpha']['watch']['L1_pulse_lost']['dead_after_ms']=100"; reject "$W/t.conf" "dead_after_ms < 3" "dead_lt_3x"
mutate "c['framework']['pacing']={'type':'fixed','delay_ms':100,'start_ms':2}"; reject "$W/t.conf" "unknown key" "dead_pacing_block"
mutate "c['schema']=3";                                           reject "$W/t.conf" "schema must be 2" "bad_schema"
mutate "c['processes']['unified']['services']['alpha']['watch']['L1_pulse_lost']['ladder']='@ghost'"; reject "$W/t.conf" "not found" "dangling_ladder"
mutate "c['ladders']['po']=[{'do':'reboot'}]";                    reject "$W/t.conf" "not @action nor verb" "unknown_verb"
mutate "
import copy
v=copy.deepcopy(c['processes']['unified']); v['services']={'alpha':c['processes']['unified']['services']['alpha']}
c['processes']['vision']=v"; reject "$W/t.conf" "duplicate service name" "dup_service_name"

# pulse sender in the background
sender(){ # $1 sock, $2 dur_s, $3.. names
  local sock="$1" dur="$2"; shift 2
  python3 - "$sock" "$dur" "$@" <<'PY' &
import socket, sys, time
sock, dur = sys.argv[1], float(sys.argv[2]); names=sys.argv[3:]
s=socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM); t0=time.time()
while time.time()-t0 < dur:
    for n in names:
        try: s.sendto(n.encode(), sock)
        except OSError: pass
    time.sleep(0.05)
PY
}

echo "=== behavioral: L1 action + P2 escalation -> restart ==="
rm -f "$W/wd.log" "$W/initd_unified_calls" "$W/crash_"*
"$BIN" "$W/good.conf" & WD=$!
sleep 0.4
sender "$W/wd.sock" 1.0 alpha beta      # 1s both alive
sleep 1.0
sender "$W/wd.sock" 3.0 beta            # then only beta; alpha goes silent
sleep 3.2
kill $WD 2>/dev/null
grep -q "wd_start" "$W/wd.log" || fail "smoke: no wd_start"
grep -q "no_pulse name=alpha" "$W/wd.log" || fail "smoke: no no_pulse alpha"
grep -q "action target=alpha" "$W/wd.log" || fail "smoke: no action alpha"
grep -q "request_stuck" "$W/wd.log" || fail "smoke: no P2 request_stuck"
grep -q "restart_process process=unified" "$W/wd.log" || fail "smoke: no restart unified"
grep -q called "$W/initd_unified_calls" 2>/dev/null || fail "smoke: initd not called"
grep -q "no_pulse name=beta" "$W/wd.log" && fail "smoke: beta wrongly silent" || true
[ $FAILED -eq 0 ] && pass "L1->P2->restart(unified), beta alive"

echo "=== behavioral: 2 processes — vision silent -> restart vision, unified intact ==="
python3 -c "
import json
c=json.load(open('$W/good.conf'))
# unified: remove P2 so it does not interfere; keep alpha/beta
c['processes']['unified']['watch']={'P1_all_pulses_lost':{'mode':'act','ladder':'@po'}}
vis={'launch':{'type':'init_script','script':'$W/initd_vision','pidfile':'$W/vision.pid',
      'grace':{'type':'fixed','ms':300},
      'restart_rate_limit':{'max':5,'per_ms':600000,'on_exceeded':'cooldown','cooldown_ms':300000}},
     'supervisor':c['processes']['unified']['supervisor'],
     'watch':{'P1_all_pulses_lost':{'mode':'act','ladder':'@po'}},
     'services':{
        'cam1':{'start':{'type':'python','entry':'m:f'},'signals':{'pulse':{'from':'loop','every_ms':100}},
                'watch':{'L1_pulse_lost':{'mode':'act','dead_after_ms':400,'ladder':'@soft'}}},
        'cam2':{'start':{'type':'python','entry':'m:f'},'signals':{'pulse':{'from':'loop','every_ms':100}},
                'watch':{'L1_pulse_lost':{'mode':'act','dead_after_ms':400,'ladder':'@soft'}}}}}
c['processes']['vision']=vis
json.dump(c,open('$W/two.conf','w'))
"
echo 1 > "$W/vision.pid"
printf '#!/bin/sh\necho called >> %s/initd_vision_calls\n' "$W" > "$W/initd_vision"; chmod +x "$W/initd_vision"
rm -f "$W/wd.log" "$W/initd_unified_calls" "$W/initd_vision_calls" "$W/crash_"*
"$BIN" "$W/two.conf" & WD=$!
sleep 0.4
sender "$W/wd.sock" 1.0 alpha beta cam1 cam2   # all alive 1s
sleep 1.0
sender "$W/wd.sock" 3.0 alpha beta             # vision goes silent; unified alive
sleep 3.2
kill $WD 2>/dev/null
grep -q "no_pulse_all process=vision" "$W/wd.log" || fail "twoproc: no P1 vision"
grep -q "restart_process process=vision" "$W/wd.log" || fail "twoproc: vision not restarted"
grep -q called "$W/initd_vision_calls" 2>/dev/null || fail "twoproc: initd_vision not called"
grep -q "restart_process process=unified" "$W/wd.log" && fail "twoproc: unified wrongly restarted" || true
[ -f "$W/initd_unified_calls" ] && fail "twoproc: unified initd wrongly called" || true
[ $FAILED -eq 0 ] && pass "vision silent -> restart(vision) only; unified intact"

echo "=== behavioral: rate_limit cooldown (fast-consumed recreates) ==="
python3 -c "
import json
c=json.load(open('$W/good.conf'))
c['actions']['recreate']['startup_ms']=200
c['actions']['recreate']['rate_limit']={'max':3,'per_ms':100000,'on_exceeded':'cooldown','cooldown_ms':60000}
# one process, one service (P1 inert with 1; P2 will not fire — files get consumed), soft
p=c['processes']['unified']
p['watch']={}
p['services']={'solo':{'start':{'type':'python','entry':'m:f'},'signals':{'pulse':{'from':'loop','every_ms':100}},
               'watch':{'L1_pulse_lost':{'mode':'act','dead_after_ms':400,'ladder':'@soft'}}}}
json.dump(c,open('$W/rate.conf','w'))
"
rm -f "$W/wd.log" "$W/crash_"*
# background "supervisor": immediately consumes crash files (unlink) → recreate repeats quickly
( for _ in $(seq 1 200); do rm -f "$W"/crash_solo 2>/dev/null; sleep 0.05; done ) &
EATER=$!
"$BIN" "$W/rate.conf" & WD=$!
sleep 0.4
sender "$W/wd.sock" 0.5 solo    # alive briefly, then goes silent
sleep 3.0
kill $WD $EATER 2>/dev/null
n=$(grep -c "action target=solo" "$W/wd.log" 2>/dev/null || echo 0)
grep -q "rate_limit_cooldown" "$W/wd.log" || fail "rate: no cooldown after max recreates (actions=$n)"
[ $FAILED -eq 0 ] && pass "rate_limit -> cooldown (recreates=$n, then cooldown)"

echo "=========================================="
if [ $FAILED -eq 0 ]; then echo "HOST TESTS: ALL OK"; exit 0; else echo "HOST TESTS: FAILED"; exit 1; fi
