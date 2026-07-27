# svc_watch — Logging: what goes where, and by what rule

## The rule in one line

**Every log path comes from a config key — never hard-coded — and every journal lives on the
SD card (non-volatile), never in `/tmp` (which is tmpfs / RAM).** `/tmp` holds only zero-byte
flag files.

Why: the router has ~10–25 MB free RAM and `/tmp` is a RAM filesystem wiped on reboot. Crash
history you need to read *after* a reboot must survive it, so journals go to the SD card
(`/mnt/sda1`). Putting them in `/tmp` would both burn scarce RAM and lose the evidence.

## Two journals

| Journal | Written by | Path key | Contents |
|---|---|---|---|
| **Observer** `wd.log` | the C daemon | `framework.observer.log.file` (prod `/mnt/sda1/crash_logs/wd.log`) | problems it saw and actions it took: `wd_start`, `no_pulse`, `stalled`, `action`, `request_stuck`, `restart_process`, `recovered`, `paused`, `unknown_name`, `waiting_resources`, `mem_below_alarm` |
| **Supervisor** `unified.log` | the Python process | `processes.<p>.supervisor.log.file` (prod `/mnt/sda1/crash_logs/unified.log`) | service lifecycle & tracebacks: starts, crashes, backoff, give-up, teardown |

They are correlated by the `up=` monotonic stamp (below). Keep them separate: distinct owners,
distinct rotation, one place to read a week later.

### Observer line format
```
<wall-ISO-time> up=<monotonic_seconds> <event> k=v k=v …
```
`up=` is **mandatory**: the box's wall clock jumps (time-sync / RTC), so event order is only
readable by the monotonic seconds. Example:
```
2026-07-25T08:57:23 up=159881 state name=config_server state=active
2026-07-25T09:02:45 up=160202 state name=config_server state=idle
```

## Rotation & durability

Both journals rotate by size, from the same keys:
- `…​.log.rotate_kb` — rotate when the file reaches this many KB;
- `…​.log.keep` — how many old files to keep (`wd.log.1`, `wd.log.2`, …);
- `framework.observer.log.fsync` — if `true`, the observer flushes each line to disk
  immediately (survives `kill -9`).

Supervisor **fallbacks**: `processes.<p>.supervisor.log.fallbacks` is a list of alternative
paths tried in order when the primary directory can't be created (e.g. the SD card isn't
mounted yet at boot). An empty list means "no fallback".

## Log-noise rate limits

Two observer messages are rate-limited so a stuck condition can't flood the journal:
- `framework.observer.quiet.paused_ms` — how often the `paused` reminder is logged while
  `pause_file` exists;
- `framework.observer.quiet.unknown_ms` — how often an `unknown_name` line is logged for
  datagrams whose service isn't in the config.

## `/tmp` — zero-byte flags only (RAM, ephemeral, by design)

| Path | Set/removed by | Meaning |
|---|---|---|
| `actions.recreate.file` → `/tmp/svc_crash_<service>` | observer creates / supervisor unlinks | "recreate this service" request |
| `framework.observer.pause_file` → `/tmp/wd_pause` | operator | pause the observer entirely |
| `processes.<p>.launch.pidfile` → `/tmp/<p>.pid` | launcher/procd | the observer reads its age for the fresh-gate |
| `/tmp/wd_test_mute_<service>` | operator (test) | that service's `emit` stays silent |
| `/tmp/wd_test_hang` | operator (test) | Python side blocks the event loop 90 s (inject P1) |

These are flags, not data — losing them on reboot is harmless (a fresh boot starts clean).

## Quick check

The set of log/flag paths is exactly these config keys — grep them from your live config:
```sh
python -c "import sys,json; c=json.load(open('/etc/svc_watch.conf')); \
o=c['framework']['observer']; p=next(iter(c['processes'].values())); \
print('observer.log :', o['log']); \
print('observer.quiet:', o['quiet']); \
print('supervisor.log:', p['supervisor']['log']); \
print('pause_file    :', o['pause_file']); \
print('crash template:', c['actions']['recreate']['file']); \
print('pidfile       :', p['launch']['pidfile'])"
```
Everything logged or flagged traces back to one of these keys — there are no hidden paths in
the code (`config.py` / `svc_watchdog.c`).
