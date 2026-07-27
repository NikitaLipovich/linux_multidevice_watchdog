# svc_watch — Architecture

A supervision framework whose behaviour is defined **entirely by one config file**. It has
two runtime sides that read the *same* `svc_watch.conf`:

- the **observer** — a tiny C daemon that judges health and issues actions;
- the **emission / supervision** side — a Python library that services embed to emit
  heartbeats and to run an in-process supervisor.

The core decision machine is **OS-free and clock-injected**, which is what lets the identical
logic run headless in-memory (consumer *B*) and on the real router (consumer *A*).

---

## 1. Components & layers

```mermaid
flowchart TB
  subgraph CFGL["Config layer (declarative, validated)"]
    CFG["config.py — schema + fail-closed validator (rules 1..17)"]
  end
  subgraph CORE["Core — OS-free, clock-injected"]
    CT["contracts.py — Transport, Probe, StartMechanism, ActionExecutor, Clock"]
    CO["core.py — HealthMachine (judge) + Supervisor (min_stable, give-up)"]
  end
  subgraph ADAPT["Adapters — one file per type (registry, no core switch)"]
    RA["real: unix_datagram, tcp, python_factory, request_file"]
    IM["inmemory: all four contracts + FakeClock"]
  end
  RT["runtime.py — composition root + adapter registries"]
  EM["emit.py — pulse loop, tcp probe, ActivityEmitter"]
  DA["daemon/svc_watchdog.c — observer (reads the same config)"]

  CFG --> RT
  CFG --> DA
  CT --> CO
  RT --> CO
  RT --> RA
  RT --> IM
  RA -. implements .-> CT
  IM -. implements .-> CT
  EM --> RA
  EM --> IM
```

`core.py` contains no service names, paths, ports, direct `time.*`, or `switch` on adapter
type — verified by grep in the test gate (FR-42/22/41). New behaviour is a new adapter file
plus a `type` string; core and runtime are untouched (FR-40, proven by the extension test).

---

## 2. Two-sided model — emission (Python) ↔ observer (C)

```mermaid
flowchart LR
  subgraph PY["Python process — emission + in-process supervision"]
    S1["service work loop (from:loop)"] -->|emit pulse| TX
    S2["library coroutine (from:probe)"] -->|probe ok then emit| TX
    FL["flash worker — activity tick()"] -->|emit 'svc active N'| TX
    TX["transport.emit — never-raises, thread-safe"]
    SUP["run_service / Supervisor"]
  end
  TX ==>|text_v1 datagram| SOCK[["/var/run/svc_wd.sock"]]
  SOCK ==> OBS
  subgraph CC["C daemon — observer (judge only)"]
    OBS["judge L1 / L2 / P1 / P2, run ladders"]
    OBS -->|request_file| CF[["/tmp/svc_crash_svc"]]
    OBS -->|restart_process| RS["sh init.d restart"]
  end
  CF -. polled and consumed .-> SUP
```

The wire protocol, socket path, and service names are identical between v1.x and v2, so the
observer can be swapped independently — the Python side keeps emitting to the same socket.

---

## 3. Judgment order (per tick — fixed logic, not configurable)

```mermaid
flowchart TB
  T["tick"] --> A["log floor/alarm"]
  A --> P{"pause_file exists?"}
  P -->|yes| STOP["log paused, skip"]
  P -->|no| L["for each process"]
  L --> F{"pidfile age < grace?"}
  F -->|yes| FR["full grace + reset, skip"]
  F -->|no| G{"in grace window?"}
  G -->|yes| SK["skip process"]
  G -->|no| P1{"P1: all services silent, >=2?"}
  P1 -->|fires| RP["restart process, done"]
  P1 -->|no| P2{"P2: a request file stuck?"}
  P2 -->|fires| RP
  P2 -->|no| SV["for each service"]
  SV --> L1{"L1: pulse silent?"}
  L1 -->|no| REC["reset ladder to step 0 (recovered)"]
  L1 -->|yes| RUN["run L1 ladder step"]
  REC --> L2{"L2: activity frozen?"}
  L2 -->|no| REC2["reset L2 ladder"]
  L2 -->|yes| RUN2["run L2 ladder step"]
```

A never-seen service anchors its silence to the process grace anchor, so a service that hangs
before its first pulse is still escalated (C daemon and Python core agree).

---

## 4. Signals ↔ watch levels (paired by name)

```mermaid
flowchart LR
  subgraph EMIT["Emit (Python side)"]
    PL["signals.pulse"]
    AC["signals.activity"]
    RF["request-file uptake"]
  end
  subgraph JUDGE["Judge (C observer)"]
    L1["watch.L1_pulse_lost"]
    L2["watch.L2_activity_frozen"]
    P1["process.watch.P1_all_pulses_lost"]
    P2["process.watch.P2_request_stuck"]
  end
  PL --> L1
  AC --> L2
  PL -->|all services silent| P1
  RF --> P2
```

`from:"loop"` = the work loop itself sends the pulse; `from:"probe"` = the library sends it
only after a probe (e.g. TCP-connect) succeeds. No signal ⇒ no level, and vice-versa (Rule 5).

---

## 5. Escalation — from silence to recovery / process restart

```mermaid
sequenceDiagram
    participant Svc as Service (Python)
    participant Obs as Observer (C)
    participant Sup as Supervisor (Python)
    participant Proc as Process (init.d)

    Svc-->>Obs: pulses (every_ms)
    Note over Svc: service hangs — pulses stop
    Obs->>Obs: silence > dead_after_ms → L1 fires (ladder @escalate_std)
    Obs->>Sup: step 1: request_file /tmp/svc_crash_svc
    Sup->>Sup: consume file, recreate service (bounded teardown)
    Sup-->>Obs: pulses resume
    Obs->>Obs: recovered → ladder reset to step 0
    Note over Svc: alternatively, recreate does not help (tries=3 exhausted)
    Obs->>Proc: step 2: restart_process → sh init.d restart
    Proc-->>Obs: fresh process → grace, state reset
```

---

## 6. Deployment topology (the live bench)

```mermaid
flowchart TB
  subgraph RUT["RUT956 router (192.168.1.1)"]
    UNI["unified Python process — 4 services + supervisor"]
    WD["svc_watchdog (C observer)"]
    CS["config_server :8000 (flash API)"]
    WSB["ws_bridge :81"]
    UNI --- CS
    UNI --- WSB
    UNI ==>|text_v1| WDSOCK[["/var/run/svc_wd.sock"]]
    WDSOCK ==> WD
    WD -->|wd.log| SD[["/mnt/sda1/crash_logs (SD, non-volatile)"]]
  end
  DASH["dashboard / operator"] -->|HTTP flash API| CS
  CS ==>|FlashBridge :13600| ARM["arm-unit R4 (192.168.1.247)"]
  MAIN["main-unit (192.168.1.10)"] -. peerfreq/status .- WSB
```

During a flash, `config_server` emits an `activity` episode (`active N` → `idle`); the
observer keeps the L2 episode alive as the counter ticks through long operations and does not
false-restart. Proven live: full Bundle-flash R4, `summary=ok`, zero false restarts.

---

See `PROTOCOL.md` for the `text_v1` wire format, `CONFIG_GUIDE.md` for the config contract,
`EMBEDDING.md` for using the library from Python, and `docs/design/` for the rationale.
