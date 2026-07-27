# svc_watch — Cheat sheet

## Levels at a glance

| Level | Scope | Fires when | Default policy |
|---|---|---|---|
| **L1_pulse_lost** | service | no pulse for `dead_after_ms` | recreate (soft) or escalate |
| **L2_activity_frozen** | service | activity counter frozen while `active` for `frozen_after_ms` | escalate |
| **P1_all_pulses_lost** | process (≥2 svc) | all services silent at once | restart process |
| **P2_request_stuck** | process | a request file lingers > `eat_within_ms` | restart process |

## Ladders (production)

| Ladder | Steps | Use |
|---|---|---|
| `soft_only` | recreate (forever) | services with a proven teardown (udp_logger) |
| `escalate_std` | recreate ×3 → restart_process | heavier services (config_server, ws_bridge, …) |
| `process_only` | restart_process | P-levels |

## Operator commands

```sh
touch /tmp/wd_pause                 # pause the observer   (rm to resume)
touch /tmp/svc_crash_<service>      # manually recreate a service
touch /tmp/wd_test_mute_<service>   # simulate a quiet death (test)
tail -f /mnt/sda1/crash_logs/wd.log        # observer journal
tail -f /mnt/sda1/crash_logs/unified.log   # supervisor journal
```

## Dev commands (from repo root)

```sh
python -m pytest external-storage-contents/svc_watch/tests -q          # 70 passed, 1 skipped
python external-storage-contents/svc_watch/examples/inmemory_demo/run.py   # prints OK
bash    external-storage-contents/svc_watch/daemon/verify.sh           # G2 OK, mipsel <150KB
python -c "import sys;sys.path.insert(0,'external-storage-contents');from svc_watch import config;config.load('.../svc_watch.conf')"
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Config refuses to start, lists missing/unknown keys | fail-closed validation | fix every listed key (path is given); unknown key = a typo (Rule 8) |
| `KeyError`/build error only at runtime | a `@`-ref points at a missing action/ladder | it should be caught at load — re-run `config.load`; if not, the ref name is wrong |
| False `no_pulse name=X` while X is alive | `dead_after_ms` too tight, or the pulse isn't actually sent | ensure `dead_after_ms ≥ 3×every_ms` and the service emits every loop; for `from:probe`, the probe must pass |
| `no_pulse` never fires for a dead service | the service never sent a first pulse | it's still escalated after the process grace (never-seen anchor); check the grace window |
| L2 fires during a long healthy operation | the work code stops calling `tick()` | call `activity.tick()` each iteration (through erase/IO too); `idle()` only in `finally` |
| `request_stuck` (P2) | the supervisor isn't consuming crash files | the Python supervisor died or `poll_ms` too slow (Rule 6: `poll_ms ≤ min(eat_within_ms)/5`) |
| Crash-loop, service keeps restarting | factory fails fast (< `min_stable_ms`) | after `max_consecutive_start_failures` the supervisor gives up and lets L1 escalate; fix the factory (often a non-atomic bind → "address in use") |
| Daemon frozen / not judging | (fixed) a restart script no longer blocks it | restart is fire-and-forget (`SIGCHLD` auto-reaped) — if frozen, check `mode` and `pause_file` |
| Nothing in `wd.log` | wrong path or SD not mounted | `observer.log.file` on `/mnt/sda1`; `mkdir -p` the dir; supervisor has `fallbacks` |

---

## Glossary

- **pulse** — a heartbeat datagram; its arrival refreshes `last_seen`.
- **activity** — a throttled counter proving a *worker thread* is progressing (drives L2).
- **episode** — the window between an `active` counter opening and an `idle` closing it.
- **ladder** — an ordered escalation policy (`recreate`, then `restart_process`, …).
- **step / tries** — one ladder rung; run up to `tries` times, then advance.
- **grace** — a blindness window after a (re)start during which the process isn't judged.
- **fresh-gate** — detecting a just-respawned process (by pidfile age) and giving it full grace.
- **recovery** — a returning pulse / advancing counter resets the ladder to step 0.
- **observer** — the C daemon that judges and acts.
- **supervisor** — the Python in-process loop that recreates a service on request.
- **cooldown** — a terminal action, having hit its rate limit, backs off instead of hammering.
