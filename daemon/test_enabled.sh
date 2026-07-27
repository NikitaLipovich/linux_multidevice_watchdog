#!/bin/bash
# test_enabled.sh — host tests for the config-native `enabled` toggles (observer + per-service).
# Runs the native ./svc_watchdog against crafted configs and checks: observer.enabled accepted,
# a service with enabled=false is NOT watched (services count drops, no no_pulse), non-bool rejected.
set -u
BIN=./svc_watchdog
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
F=0; fail(){ echo "  EN-FAIL: $1"; F=1; }; pass(){ echo "  ok: $1"; }

python3 - "$W" <<'PY'
import json,sys,copy
W=sys.argv[1]
c={
 "schema":2,
 "framework":{
  "transport":{"type":"unix_datagram","unix_datagram":{"socket":W+"/wd.sock","format":"text_v1"}},
  "observer":{"mode":"act","tick_ms":100,"pause_file":W+"/pause","oom_adj":0,
    "log":{"file":W+"/wd.log","rotate_kb":100,"keep":2,"fsync":False},
    "quiet":{"paused_ms":60000,"unknown_ms":60000}},
  "gates":{"min_free_mb":8,"max_load1":500.0,"recheck_ms":100,"force_after_ms":2000,"alarm_mb":5},
  "pacing":{"type":"fixed","delay_ms":100}},
 "actions":{"recreate":{"type":"request_file","file":W+"/crash_{service}","eat_within_ms":400,"startup_ms":300,
    "rate_limit":{"max":6,"per_ms":100000,"on_exceeded":"cooldown","cooldown_ms":60000}}},
 "ladders":{"soft":[{"do":"@recreate"}],"po":[{"do":"restart_process"}]},
 "processes":{"unified":{
   "launch":{"type":"init_script","script":W+"/initd","pidfile":W+"/u.pid",
     "grace":{"type":"fixed","ms":300},
     "restart_rate_limit":{"max":5,"per_ms":600000,"on_exceeded":"cooldown","cooldown_ms":300000}},
   "supervisor":{"poll_ms":100,"stop_timeout_ms":2000,"min_stable_ms":500,
     "max_consecutive_start_failures":3,"backoff":{"start_ms":100,"factor":2,"cap_ms":1000},
     "log":{"file":W+"/u.log","rotate_kb":100,"keep":2,"fallbacks":[]}},
   "watch":{"P2_request_stuck":{"mode":"act","ladder":"@po"}},
   "services":{
     "alpha":{"start":{"type":"python","entry":"m:f"},"signals":{"pulse":{"from":"loop","every_ms":100}},
       "watch":{"L1_pulse_lost":{"mode":"act","dead_after_ms":400,"ladder":"@soft"}}},
     "beta":{"start":{"type":"python","entry":"m:f"},"signals":{"pulse":{"from":"loop","every_ms":100}},
       "watch":{"L1_pulse_lost":{"mode":"act","dead_after_ms":400,"ladder":"@soft"}}}}
 }}}
json.dump(c,open(W+"/good.conf","w"))
a=copy.deepcopy(c);  a["framework"]["observer"]["enabled"]=False; json.dump(a, open(W+"/a.conf","w"))
a2=copy.deepcopy(c); a2["framework"]["observer"]["enabled"]=True;  json.dump(a2,open(W+"/a2.conf","w"))
b=copy.deepcopy(c);  b["processes"]["unified"]["services"]["beta"]["enabled"]=False; json.dump(b,open(W+"/b.conf","w"))
cc=copy.deepcopy(c); cc["processes"]["unified"]["services"]["beta"]["enabled"]="yes"; json.dump(cc,open(W+"/c.conf","w"))
PY
echo 1 > "$W/u.pid"; printf '#!/bin/sh\ntrue\n' > "$W/initd"; chmod +x "$W/initd"

run_brief(){ rm -f "$W/wd.log"; "$BIN" "$1" >/dev/null 2>&1 & local wd=$!; sleep 0.6; kill $wd 2>/dev/null; wait $wd 2>/dev/null; }

# sanity: base = 2 watched services
run_brief "$W/good.conf"; grep -q "wd_start .*services=2" "$W/wd.log" && pass "base services=2" || fail "base not services=2: $(grep wd_start "$W/wd.log")"
# A: observer.enabled=false → config ACCEPTED (C accepts key; startup.sh is the real gate)
run_brief "$W/a.conf";    grep -q wd_start "$W/wd.log" && pass "observer.enabled=false accepted" || fail "observer.enabled=false rejected"
run_brief "$W/a2.conf";   grep -q wd_start "$W/wd.log" && pass "observer.enabled=true accepted"  || fail "observer.enabled=true rejected"
# B: service beta enabled=false → services=1, and beta NEVER gets no_pulse even while silent; alpha does
rm -f "$W/wd.log"; "$BIN" "$W/b.conf" >/dev/null 2>&1 & wd=$!; sleep 2.5; kill $wd 2>/dev/null; wait $wd 2>/dev/null
grep -q "wd_start .*services=1" "$W/wd.log" && pass "disabled beta: services=1 (not watched)" || fail "disabled beta still counted: $(grep wd_start "$W/wd.log")"
grep -q "no_pulse name=beta"  "$W/wd.log" && fail "disabled beta wrongly got no_pulse" || pass "disabled beta: 0 no_pulse"
grep -q "no_pulse name=alpha" "$W/wd.log" && pass "alpha still watched (no_pulse fired)" || fail "alpha not watched"
# C: non-bool enabled → reject (typo/type protection intact)
out=$("$BIN" "$W/c.conf" 2>&1); rc=$?
{ [ $rc -eq 1 ] && echo "$out" | grep -q "enabled must be bool"; } && pass "non-bool enabled rejected" || fail "non-bool enabled not rejected (rc=$rc): $out"

echo "=========================================="
[ $F -eq 0 ] && { echo "ENABLED TESTS: ALL OK"; exit 0; } || { echo "ENABLED TESTS: FAILED"; exit 1; }
