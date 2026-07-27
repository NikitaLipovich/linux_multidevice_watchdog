# DEPLOY_E5 — migrating the bench to watchdog v2 (OBSERVER swap)

> Strategy (design: phased migration): the `text_v1` protocol, the socket
> `/var/run/svc_wd.sock`, the service names and the timings are IDENTICAL between v1.2 and v2.
> So we migrate ONLY the C daemon (the observer). The Python side (emission via `wd_beat` +
> supervision via `wd_runtime` + the flash path) stays on production v1.2 and is NOT touched →
> "the flash protocol expectations do not change". Migrating the Python side to `unified_rut`
> is a separate, later phase (not required for this observer swap).

Verified on the live bench (read-only): health 200, ws:81, ~27 MB free, socket / names / paths
/ timings all match.

Details: RUT `192.168.1.1`, key `/tmp/rut_key`, `$RD=/usr/local/home/root/external-storage-contents`.
Local (paths from the repo root): `external-storage-contents/svc_watch/daemon/svc_watchdog.mipsel`
(~31.5 KB, built by `daemon/verify.sh`), `external-storage-contents/svc_watch/conf/svc_watch.conf` (valid).

## Steps (run one at a time, verifying each)

```sh
RK=/tmp/rut_key; H=root@192.168.1.1
SSH="ssh -i $RK -o StrictHostKeyChecking=no $H"
RD=/usr/local/home/root/external-storage-contents

# 0. PRECONDITION (health / ws / python / RAM)
$SSH 'wget -qO- http://127.0.0.1:8000/health; echo; netstat -ltn|grep -E ":81 |:8000 "; free|sed -n 2p'

# 1. BACKUPS (snapshot the current production v1.2)
$SSH 'cp -a $RD/svc_watchdog $RD/svc_watchdog.bak_v1.2 && \
      cp -a /etc/svc_watchdog.conf /etc/svc_watchdog.conf.bak_v1.2 && \
      cp -a /etc/init.d/svc_watchdog /etc/init.d/svc_watchdog.bak_v1.2 && echo baks_ok'

# 2. DEPLOY v2 (daemon alongside, config into /etc; production v1.2 still untouched)
scp -i $RK external-storage-contents/svc_watch/daemon/svc_watchdog.mipsel $H:$RD/svc_watchdog.v2
scp -i $RK external-storage-contents/svc_watch/conf/svc_watch.conf        $H:/etc/svc_watch.conf
$SSH 'chmod +x $RD/svc_watchdog.v2 && ls -la $RD/svc_watchdog.v2 && \
      $RD/svc_watchdog.v2 /etc/svc_watch.conf 2>&1 & sleep 1; kill %1 2>/dev/null; echo started_ok'
#   ^ short test-start of v2 on the production config: prints wd_start and lives (killed after 1s)

# 3. NEGATIVE test of the config (fail-closed): a broken config → exit 1 + rejected
$SSH 'sed "s/\"schema\": 2/\"schema\": 9/" /etc/svc_watch.conf > /tmp/bad.conf; \
      $RD/svc_watchdog.v2 /tmp/bad.conf; echo "rc=$?"'   # expect rc=1 + "schema must be 2"

# 4. SWITCH init.d to v2 (PROG+CONF), restart the daemon
$SSH 'sed -i "s#^PROG=.*#PROG=$RD/svc_watchdog.v2#; s#^CONF=.*#CONF=/etc/svc_watch.conf#" \
      /etc/init.d/svc_watchdog && grep -E "^PROG=|^CONF=" /etc/init.d/svc_watchdog && \
      /etc/init.d/svc_watchdog restart && echo restarted'

# 5. VERIFY (the v2 daemon judges the same stream)
$SSH 'sleep 3; ps w|grep -E "svc_watchdog.v2"|grep -v grep; \
      tail -3 /mnt/sda1/crash_logs/wd.log; \
      wget -qO- http://127.0.0.1:8000/health; echo; netstat -ltn|grep -E ":81 |:8000 "'
#   expect: process svc_watchdog.v2, "wd_start processes=1 services=4 ... mode=act",
#   NO no_pulse for 20s (services are pulsing), health 200, ws:81.
```

## SMOKE (after the switch)

```sh
# mute cycle for udp_logger: quiet death → no_pulse → action → v1.2 supervisor recreates → recovered
$SSH 'touch /tmp/wd_test_mute_udp_logger; sleep 20; rm -f /tmp/wd_test_mute_udp_logger; \
      grep -E "no_pulse name=udp_logger|action target=udp_logger|recovered.*udp_logger" /mnt/sda1/crash_logs/wd.log | tail'
# 5 minutes of silence: zero false lines while everything is alive
$SSH 'sleep 300; tail -20 /mnt/sda1/crash_logs/wd.log'
# L2 injection (frozen config_server counter during a flash) — happens at the flash step
```

## LIVE Bundle-flash R4 (final step)
Run a real Bundle-flash of arm R4. During the flash window the v2 daemon must see
`config_server active N` (activity), must NOT false-restart (the L2 episode stays alive, the
counter ticks through erase), and the flash `summary=ok`. Check `wd.log`: exactly one
`active → idle` episode, `0 restart_process`, `0 no_pulse`. (Proven live: `summary=ok`,
1/1 arms, 0 errors, ~5.5 min episode, zero false restarts.)

## ROLLBACK (any time)
```sh
$SSH 'cp -a /etc/init.d/svc_watchdog.bak_v1.2 /etc/init.d/svc_watchdog && \
      /etc/init.d/svc_watchdog restart && sleep 2; ps w|grep svc_watchdog|grep -v grep; \
      tail -2 /mnt/sda1/crash_logs/wd.log'
# init.d.bak_v1.2 points back at the production $RD/svc_watchdog (v1.2, 22924 B) + /etc/svc_watchdog.conf.
# Full bench rollback to the 4-separate-process layout: rc.local (see the project history).
```

## GUARDRAILS
- Do NOT touch the Python process `run_services.py` or `wd_beat`/`wd_runtime` (the flash path is live).
- If the v2 daemon produces false no_pulse / restarts on the live stream → STOP, roll back,
  investigate (a spec-vs-C divergence).
- Any review BLOCKER / MAJOR is fixed BEFORE deploying.
