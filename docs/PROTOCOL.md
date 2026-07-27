# svc_watch — Wire Protocol `text_v1`

Services send heartbeats to the observer over a **unix datagram socket**
(`framework.transport.unix_datagram.socket`, production `/var/run/svc_wd.sock`). The observer
creates it (unlink → bind → `chmod 0666`, so non-root senders work); senders `sendto()`
without connecting. One datagram = one message, best-effort: losing a single datagram is not
an event (all thresholds are ≥ 3× the send period).

**Sender contract:** `emit` never raises and is thread-safe (it runs on the hot path of every
service loop and from flash executor threads). No socket / no listener → the message is
silently dropped; the service is unaffected.

## The three forms of one ASCII line (no `\0`, no `\n`)

| Form | Example | Meaning |
|---|---|---|
| `<service>` | `udp_logger` | **pulse** — "my loop is turning"; refreshes `last_seen` |
| `<service> <state>` | `config_server idle` | pulse + state change |
| `<service> <state> <counter>` | `config_server active 1042` | pulse + state + **activity counter** |

Fields are single-space separated. `service ≤ 47` bytes, `state ≤ 23` bytes, `counter` is a
signed 64-bit decimal (≤ 20 chars). Max legal datagram = 92 bytes; the receive buffer is 128.

**Single global namespace:** the datagram carries only `<service>` — no process qualifier.
Therefore a service name must be unique **globally** across all processes (validator Rule 7):
two services sharing a name would collide on `last_seen` and on the crash-file
`/tmp/svc_crash_<service>`.

## State vocabulary

- `active` — a work operation is running; L2 is judged **only** in this state.
- `idle` — the work operation ended (**closes the L2 episode**); sent from the work code's
  `finally`. Dying without `idle` leaves the counter frozen while `active` → L2 fires (desired).
- Other words are allowed (the daemon stores and logs the change) but carry no level semantics.

## Observer-side semantics

- **pulse** (any form, including form 3 with a counter): `last_seen[service] = now`. Silence
  longer than `watch.L1_pulse_lost.dead_after_ms` → L1. An `activity` datagram also refreshes
  `last_seen` — not a hole: it comes from actually-executing work code, which proves life more
  strongly than a port probe.
- **counter**: the first counter-pulse opens an episode; a changed value sets
  `last_progress = now`. Episode ∧ `state == active` ∧ counter unchanged for
  `frozen_after_ms` → L2. The value is opaque — only "changed or not" is judged. The episode
  resets on a state change, a recreate of the service, a process restart, or the fresh-gate.
- **watch levels are a closed set** (Rule 16): service `{L1_pulse_lost, L2_activity_frozen}`,
  process `{P1_all_pulses_lost, P2_request_stuck}`. Unknown level key → reject.
- **unknown name** (not in the config) → an `unknown_name` log line, rate-limited by
  `observer.quiet.unknown_ms`.
- **ready** (form 2, `<process> ready`) — reserved: a process announcing readiness so
  `launch.grace.type:"until_ready"` can end blindness before `max_ms`. Not emitted yet;
  `until_ready` without a declared ready-signal is rejected (Rule 13).

## Who emits what (matches `signals` in the config)

- `signals.pulse from:"loop"` — the service's own work loop calls emit (form 1).
- `signals.pulse from:"probe"` — a library coroutine: probe (e.g. TCP-connect to `@port`
  within `timeout_ms`) → success = emit, failure = stay silent (L1 will see it).
- `signals.activity` — the work code calls `tick()` each iteration; the library throttles to
  one datagram per `tick_ms` (form 3, `state=active`); end of work emits `idle` (form 2). The
  `tick_ms` value is passed to the work code as a parameter (it slices its own waits) — the
  work code never imports the config or the watchdog.

## Action protocol constants

- `request_file`: `creat(file from template, {service} substituted)`, mode 0644; consumption =
  the supervisor `unlink`s it. Not eaten within `eat_within_ms` → `P2_request_stuck`.
- `init_script`: `fork` → `execl("/bin/sh", "sh", <launch.script>, "restart")`. The verb
  `restart` is fixed. Signals to the process are forbidden (races procd respawn). The observer
  does **not** block waiting on the script (fire-and-forget; `SIGCHLD` auto-reaped).

## Observer journal line

`<wall-ISO-time> up=<monotonic_seconds> <event> k=v …`. The `up=` field is mandatory: the box's
wall clock jumps (time-sync / RTC), so event order is only readable by the monotonic stamp.

## Test hooks (protocol-level, outside the config)

- `/tmp/wd_test_mute_<service>` — if it exists, that service's `emit` stays silent (simulate a
  quiet death without stopping the service).
- `/tmp/wd_test_hang` — if it exists, the Python consumer blocks the whole event loop for 90 s
  (inject `P1_all_pulses_lost`).
- env `WD_PROC_MEMINFO` / `WD_PROC_LOADAVG` — substitute `/proc` files for host tests of the gates.
- env `WD_CONFIG` — config path (Python bootstrap; the C daemon takes it as `argv[1]`).
