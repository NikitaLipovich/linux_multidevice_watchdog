# svc_watch — Configuration Guide

`svc_watch.conf` is the **single source of truth** for the whole supervision framework.
The exact same file is read by two sides:

- the **observer** — a small C daemon (`daemon/svc_watchdog.c`) that *judges* health and
  issues actions;
- the **emission / supervision** side — the Python library (`svc_watch`) that services use
  to *emit* signals and to run the in-process supervisor.

Both sides validate the file independently and **fail closed**: a missing key, an unknown
key, a broken `@`-reference or a value out of range is a loud rejection at startup (non-zero
exit), never a silent default. Fix every reported problem in one pass — the loader reports
them all at once, each with the exact key path.

> Truth lives in the code. If this guide and `svc_watch/config.py` (Python) or
> `svc_watch/daemon/svc_watchdog.c` (C) disagree, the code wins.

---

## 1. Top-level shape

```jsonc
{
  "schema": 2,          // config format version; anything but 2 is rejected
  "framework": { … },   // the machinery: transport, observer, resource gates, pacing
  "actions":   { … },   // catalog of SERVICE-level actions (map: name → action)
  "ladders":   { … },   // catalog of reusable escalation policies (map: name → steps)
  "processes": { … }    // topology: process ⊃ services ⊃ { signals, watch }
}
```

Three reading conventions:

1. **Entity name = the left-hand key.** `processes`, `services`, `actions`, `ladders`,
   `signals`, `watch` are maps; the key *is* the name.
2. **`@` means a link** to an entity declared elsewhere; every `@`-ref is checked at load.
3. **Meaning comes from the path.** Keys are short; nesting supplies the context.

---

## 2. `framework` — the machinery

### 2.1 `framework.transport` — how every signal reaches the observer
```jsonc
"transport": {
  "type": "unix_datagram",                         // adapter: unix_datagram | inmemory
  "unix_datagram": {                               // params live UNDER the type name
    "socket": "/var/run/svc_wd.sock",              // datagram socket the observer binds
    "format": "text_v1"                            // wire format (only text_v1 today)
  }
}
```
The only nested-variant block in the schema: params sit under the key that equals `type`.
For `type: "inmemory"` there is no params block (no dead `socket`/`format`). See PROTOCOL.md
for `text_v1`.

### 2.2 `framework.observer` — the C daemon
```jsonc
"observer": {
  "mode": "act",                    // act | log — the MASTER safety switch (log = observe-only)
  "tick_ms": 1000,                  // judgment period. sane range 200..5000
  "pause_file": "/tmp/wd_pause",    // if this file exists → full pause (touch/rm by hand)
  "oom_adj": -1000,                 // written to /proc/self/oom_score_adj (survive OOM killer)
  "log": {
    "file": "/mnt/sda1/crash_logs/wd.log",   // observer journal (on SD, NOT tmpfs — see LOGGING.md)
    "rotate_kb": 1024, "keep": 7,            // rotate at N KB, keep N old files
    "fsync": true                            // flush each line (survives kill -9)
  },
  "quiet": { "paused_ms": 60000, "unknown_ms": 60000 }  // rate-limit for paused / unknown-name logs (≥1)
}
```

### 2.3 `framework.gates` — resource gate before every action
```jsonc
"gates": {
  "min_free_mb": 8,          // below this much free RAM → wait (must be > alarm_mb)
  "max_load1": 3.0,          // loadavg(1m) above this → wait
  "recheck_ms": 5000,        // re-check cadence while waiting
  "force_after_ms": 300000,  // waited longer than this → act anyway (starvation won't self-heal)
  "alarm_mb": 5              // low-memory alarm line in the log
}
```

### 2.4 `framework.pacing` — pause between repeated action attempts (typed)
```jsonc
"pacing": { "type": "fixed", "delay_ms": 5000 }
// type "backoff" → { "type":"backoff", "start_ms":2000, "factor":2, "cap_ms":60000 }
```
`fixed` carries only `delay_ms`; `backoff` carries only `start_ms`/`factor`/`cap_ms`. A param
from the other variant is an unknown key (Rule 8) — no dead placeholder blocks.

---

## 3. `actions` — catalog of SERVICE-level actions

Actions act **on a single service** (parameterised by `{service}`). Restarting a *process*
is **not** an action — it is the built-in verb `restart_process` (see §4).

```jsonc
"actions": {
  "recreate": {
    "type": "request_file",                 // observer drops a file → supervisor eats it → recreates svc
    "file": "/tmp/svc_crash_{service}",      // {service} is substituted; tmpfs is fine (0-byte flag)
    "eat_within_ms": 10000,                  // not eaten in time → P2_request_stuck (supervisor dead)
    "startup_ms": 60000,                     // after eaten, don't judge the service this long (factory warmup)
    "rate_limit": {                          // terminal actions must not hammer forever (Rule 12)
      "max": 6, "per_ms": 600000,
      "on_exceeded": "cooldown", "cooldown_ms": 300000
    }
  }
}
```

---

## 4. `ladders` — reusable escalation policies

A ladder is an ordered list of steps. Each level (§6) points at one via `"@name"`.

```jsonc
"ladders": {
  "soft_only":    [ { "do": "@recreate" } ],                       // recreate forever, never touch process
  "escalate_std": [ { "do": "@recreate", "tries": 3 },             // 3 recreates without recovery…
                    { "do": "restart_process" } ],                 // …then restart the OWNER process
  "process_only": [ { "do": "restart_process" } ]                  // straight to process (for P-levels)
}
```

Step semantics:
- `"do": "@name"` → a **service action** from the `actions` catalog (targets `{service}`).
- `"do": "restart_process"` → the **built-in verb**: restart the process that *owns* this
  service, resolved from nesting context. Cross-process misfire is structurally impossible
  (you cannot name another process's restart). Its mechanism and rate-limit live on the
  process (`launch.type` / `launch.restart_rate_limit`).
- `"tries": N` → run this step up to N times (paced by `pacing`, gated by `gates`); if the
  target has not recovered, advance to the next step. The **last** step may omit `tries`
  (it repeats until recovery). A non-last step without `tries` is rejected (Rule 15).
- **Recovery resets the ladder to step 0** and clears the attempt counters (Rule/FR-36).

Ladders referenced by **P-levels** (P1/P2) must contain only `restart_process` steps — a
P-level has no single service target for an `@action`.

---

## 5. `processes` — topology

```jsonc
"processes": {
  "unified": {
    "launch": {                              // how the process starts & how restart_process restarts it
      "type": "init_script",                 // init_script | inmemory | external
      "script": "/etc/init.d/rut_services",  // restart_process runs: sh <script> restart
      "pidfile": "/tmp/rut_services.pid",    // written by launcher; observer reads age → fresh-gate
      "grace": { "type": "fixed", "ms": 90000 },  // blindness after (re)start (typed; see Rule 13)
      "restart_rate_limit": {                // crash-loop guard for the process
        "max": 5, "per_ms": 600000,
        "on_exceeded": "cooldown", "cooldown_ms": 300000   // "stop" is FORBIDDEN here (Rule 12)
      }
    },
    "supervisor": {                          // the Python in-process supervisor (emission side)
      "poll_ms": 1000,                       // request-file poll cadence (≤ min(eat_within_ms)/5)
      "stop_timeout_ms": 20000,              // bounded teardown ceiling
      "min_stable_ms": 5000,                 // "up" = lived this long; earlier death = failed start
      "max_consecutive_start_failures": 5,   // N failures in a row → stop recreating (let L1 escalate)
      "backoff": { "start_ms": 1000, "factor": 2, "cap_ms": 10000 },
      "log": { "file": "/mnt/sda1/crash_logs/unified.log", "rotate_kb": 1024, "keep": 7,
               "fallbacks": ["…/services_runtime.log", "/tmp/services_runtime.log"] }
    },
    "watch": {                               // PROCESS-level levels
      "P1_all_pulses_lost": { "mode": "act", "ladder": "@process_only" },
      "P2_request_stuck":   { "mode": "act", "ladder": "@process_only" }
    },
    "services": {                            // SERVICES nested in the process
      "udp_logger": {
        "start": { "type": "python", "entry": "run_services:start_udp_logger" },
        "signals": { "pulse": { "from": "loop", "every_ms": 5000 } },
        "watch":   { "L1_pulse_lost": { "mode": "act", "dead_after_ms": 15000, "ladder": "@soft_only" } }
      },
      "config_server": {
        "start": { "type": "python", "entry": "run_services:start_config_server" },
        "port": 8000,                        // single truth: the factory binds it AND the probe uses it
        "signals": {
          "pulse":    { "from": "probe", "every_ms": 5000,
                        "probe": { "type": "tcp", "port": "@port", "timeout_ms": 2000 } },
          "activity": { "tick_ms": 2000 }    // worker-thread liveness counter (feeds L2)
        },
        "watch": {
          "L1_pulse_lost":      { "mode": "act", "dead_after_ms": 30000, "ladder": "@escalate_std" },
          "L2_activity_frozen": { "mode": "act", "frozen_after_ms": 60000, "ladder": "@escalate_std" }
        }
      }
    }
  }
}
```

---

## 6. Signals ↔ watch levels (paired by name)

A service **emits** `signals` (Python side) and the observer **judges** them via `watch`
levels (C side). The level key names its signal — visible without reading the body.

| Signal (emit, py)   | Level (judge, C)                    | Fires when |
|---|---|---|
| `signals.pulse`     | `watch.L1_pulse_lost`               | no pulse for `dead_after_ms` → the loop/listener is dead |
| `signals.activity`  | `watch.L2_activity_frozen`          | counter frozen while `active` for `frozen_after_ms` → worker thread hung |
| all services' pulse | `process.watch.P1_all_pulses_lost`  | ALL services silent at once → the process event loop hung (≥2 services) |
| request-file uptake | `process.watch.P2_request_stuck`    | a request file lingers > `eat_within_ms` → the supervisor is dead |

**Pulse form** decides where the pulse comes from:
- `from: "loop"` — the service's own work loop calls emit; the very act of sending proves
  life. A `probe` block is **forbidden** here (Rule 4).
- `from: "probe"` — the library sends the pulse, but only after a `probe` succeeds
  (e.g. TCP-connect to `@port`); probe fails → no pulse → L1 will see the silence.

No signal ⇒ no level, and vice-versa (Rule 5 requires the pairing). A service without a
worker thread simply has no `activity` and no L2. Adding a new deep level = a new
`signals.X` + `watch.LN_X` pair.

---

## 7. Validator rules (all enforced, fail-closed, all problems at once)

Each rule below is enforced in `config.py` (and, for its C-readable parts, in
`svc_watchdog.c`). Every violation names the offending key path.

1. **`schema` known; every required key present.** All missing keys are listed together.
2. **Every `type` comes from an adapter dictionary.** Unknown type → reject.
   (`transport`, `probe`, `start`, `action`, `launch` types.)
3. **`@`-refs resolve.** `watch.*.ladder` `"@x"` → `ladders.x`; a step `"do": "@y"` → `actions.y`;
   `probe.port` `"@port"` → this service's `port`. A `do` **without** `@` must be a built-in
   verb from the closed set `{restart_process}`; anything else → reject.
4. **Pulse form.** `from:"loop"` ⇒ no `probe`; `from:"probe"` ⇒ `probe` required.
5. **Pairing.** Every service: `signals.pulse` + `watch.L1_pulse_lost`;
   `signals.activity` ⟺ `watch.L2_activity_frozen` (both or neither).
6. **Numeric relations.** `dead_after_ms ≥ 3×every_ms` · `frozen_after_ms ≥ 20×tick_ms` ·
   `probe.timeout_ms < every_ms` · `min_free_mb > alarm_mb` · `poll_ms ≤ min(eat_within_ms)/5` ·
   `grace.until_ready ⇒ max_ms` · `rate_limit.cooldown_ms > per_ms/max` ·
   **request_file reachability:** `per_ms/max ≥ startup_ms + min(dead_after_ms of services
   using the action)` (otherwise the limiter is inert).
7. **Unique names.** Names unique within a map; **service names unique GLOBALLY across all
   processes** (the `text_v1` datagram carries no process qualifier); string lengths within
   the C capacities (`NAME 47`, `STATE 23`, `PATH 256` bytes).
8. **Unknown key = reject.** Typo protection: `"trys": 3` is not silently ignored.
9. **Degenerate `0` forbidden** for rate-limits and log intervals (must be ≥ 1).
10. **`request_file` `file` templates are unique** (otherwise ambiguous uptake).
11. **WARNING (not error):** a single-service process with `P1_all_pulses_lost: act` — P1 is
    inert there (indistinguishable from that service's L1). Coverage = L1 ladder + P2.
12. **Every action / `restart_rate_limit` carries `rate_limit`;** `on_exceeded ∈ {cooldown, stop}`;
    **`stop` is FORBIDDEN on `launch.restart_rate_limit`** (the only way to save an unattended
    box must never give up permanently).
13. **`grace.type ∈ {fixed, until_ready}`;** `fixed ⇒ ms`; `until_ready ⇒ max_ms` **and** a
    declared process ready-signal (none exists yet → only `fixed` is legal for now).
14. **`pacing.type ∈ {fixed, backoff}`;** `fixed ⇒ delay_ms`; `backoff ⇒ {start_ms, factor, cap_ms}`
    (typed variant, only the active params — no dead block).
15. **Ladder `tries`:** every `tries` (if present) ≥ 1; **only the last step may omit `tries`**;
    a non-last step without `tries` is rejected (the next step would be unreachable).
16. **watch levels from a closed set:** service `{L1_pulse_lost, L2_activity_frozen}`,
    process `{P1_all_pulses_lost, P2_request_stuck}`. Unknown level key → reject. Ladders are
    referenced by `"@name"` only (inline arrays are not accepted — Python and C agree).
    P-level ladders must be `restart_process`-only.
17. **Collection capacities (compile-time C):** `≤ MAX_PROCESSES` · each process `1..MAX_SERVICES` ·
    `≤ MAX_ACTIONS` · `≤ MAX_LADDERS` · each ladder `≤ MAX_LADDER_STEPS`; also `rate_limit.max ≤
    MAX_FIRES` (32) on the C side. Exceeding a capacity is a rejection, never truncation.
    (Defaults: `MAX_PROCESSES 8`, `MAX_SERVICES 30`, `MAX_ACTIONS 16`, `MAX_LADDERS 16`,
    `MAX_LADDER_STEPS 8` — declared in **both** `config.py` and the C `#define`s, which must
    stay in lock-step. `MAX_WATCH_LEVELS 4` is a `config.py`-only guard; the C daemon hard-codes
    the four levels `{L1, L2, P1, P2}` rather than defining a capacity.)

---

## 8. Sequences (the observer's fixed logic — NOT configurable)

### 8.1 Judgment order (each tick)
```
floor/alarm log → pause_file? (skip) → per process:
  fresh-gate (pidfile age < grace → full grace + reset, skip)
  → in grace window? (skip)
  → P1 (all services silent, ≥2)  → if it fires, restart process, done with this process
  → P2 (a request file stuck)     → if it fires, restart process, done
  → per service: L1 (pulse silence) then L2 (activity frozen)
       alive → reset that level's ladder to step 0 (log "recovered")
       firing & mode=act → run the ladder step
```
A never-seen service anchors its silence to the process grace anchor, so a service that
starts and hangs *before its first pulse* is still escalated (parity between the C daemon and
the Python core).

### 8.2 One ladder step (`_run_level` / `run_ladder`)
```
mode=log?            → log only, no action
in cooldown?         → skip
pacing not elapsed?  → skip  (fixed delay_ms, or backoff start·factor^(n-1) capped at cap_ms)
resource gate denies?→ wait; force after force_after_ms
rate window ≥ max?   → cooldown (+ alarm) [or stop, where allowed]; skip
otherwise EXECUTE:
  @action  → run action on {service}; suppress judging for startup_ms
  verb     → restart owner process; set its grace; reset its services
increment tries; tries exhausted → advance to next step (reset per-step counters)
```

### 8.3 Recovery
A returning pulse (L1) or an advancing counter (L2) resets that level's ladder to step 0 and
clears the attempt/rate state, logging `recovered`.

---

## 9. Quick reference — ranges & defaults

| Key | Range / rule | Production value |
|---|---|---|
| `observer.tick_ms` | 200..5000 | 1000 |
| `observer.quiet.{paused_ms,unknown_ms}` | ≥ 1 | 60000 |
| `gates.min_free_mb` | > `alarm_mb` | 8 (alarm 5) |
| `pacing.delay_ms` (fixed) | 2000..60000 | 5000 |
| `signals.pulse.every_ms` | per service | 5000 |
| `watch.L1.dead_after_ms` | ≥ 3×`every_ms` | 15000 / 30000 |
| `watch.L2.frozen_after_ms` | ≥ 20×`activity.tick_ms` | 60000 |
| `probe.timeout_ms` | < `every_ms` | 2000 |
| `launch.grace.ms` | covers process boot | 90000 |
| `supervisor.poll_ms` | ≤ min(`eat_within_ms`)/5 | 1000 |
| `actions.*.rate_limit.max` | ≤ 32, reachable (Rule 6) | 6 |

The canonical, always-valid example is `conf/svc_watch.conf`. For the *why* behind these
choices, read `docs/design/` (DESIGN, FRAMEWORK_RULES, TRACE_V1_TO_V2).
