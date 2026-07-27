# svc_watch — Embedding the framework in Python

This is the **emission + supervision** side. On the real router the *observer* is the C daemon
(`daemon/svc_watchdog.c`); your Python process only has to (1) **emit heartbeats** the observer
can judge, and (2) optionally **run the in-process supervisor** that recreates a service when
the observer asks for it. Both are driven from the same `svc_watch.conf`.

The library never learns your service names — you pass them in. The config's *links*
(`signals ↔ watch`, `ladders`, `@`-refs, `restart_process`) are judged by the observer; your
job is to emit the signals those links refer to.

> Full working reference: `examples/unified_rut/main.py` (the `BeatBridge` shim + wiring) and
> `examples/inmemory_demo/run.py` (everything on simulated time).

---

## 1. Import & load the config

Put the parent of the `svc_watch` package on `sys.path`, then load fail-closed:

```python
import sys
sys.path.insert(0, "external-storage-contents")   # parent of the svc_watch package

from svc_watch import config

try:
    cfg = config.load("/etc/svc_watch.conf")       # or os.environ["WD_CONFIG"]
except config.WdConfigError as e:
    print("FATAL: %s" % e, file=sys.stderr)        # every problem, one list, with key paths
    raise SystemExit(1)                            # fail closed — start NOTHING on a bad config
```

`cfg` is a typed model (`cfg.processes["unified"].services["config_server"].port`, etc.), not a
raw dict.

---

## 2. Build the transport (once per process)

```python
from svc_watch import runtime

transport = runtime.build_transport(cfg)           # unix_datagram on the router; inmemory in tests
```

`transport.emit(msg)` **never raises** and is thread-safe — safe on the hot path of any loop or
executor thread. No listener → the datagram is silently dropped.

---

## 3. Emit the base pulse (L1)

The config decides *who* sends the pulse (`signals.pulse.from`):

**`from: "loop"`** — your own work loop sends it. Call the tiny wire helper (or the
`BeatBridge.beat` shim from `unified_rut`):

```python
import time
from svc_watch.adapters.wire import format_pulse

def run_udp_logger(transport, name="udp_logger"):
    while True:
        handle_one_datagram()                      # your real work
        transport.emit(format_pulse(name))         # proof of life = the send itself
        time.sleep(5)                              # ~ every_ms from the config
```

**`from: "probe"`** — the library sends the pulse, but only after a probe succeeds. Use the
async helpers; nothing is emitted while the probe fails, so L1 will see a genuinely dead port:

```python
import asyncio
from svc_watch import emit

async def pulse_config_server(transport, port=8000):
    probe = emit.tcp_probe("127.0.0.1", port, timeout_s=2.0)   # connect-and-close
    await emit.pulse_loop("config_server", transport, every_s=5.0, probe=probe)
```

---

## 4. Emit activity (L2) — worker-thread liveness

For a service with a `signals.activity` block, hand an `ActivityEmitter` to the work code. The
work code calls `tick()` each iteration and `idle()` in its `finally`; the library throttles to
one datagram per `tick_ms`. The tick period is a **parameter to the work code** — it never
imports the config or the watchdog:

```python
from svc_watch import emit

def flash_job(transport, tick_s):
    activity = emit.ActivityEmitter("config_server", transport, tick_s=tick_s)
    try:
        for chunk in stream_firmware():
            write_chunk(chunk)
            activity.tick()                        # "config_server active N", throttled
    finally:
        activity.idle()                            # closes the L2 episode
```

If the worker deadlocks mid-flash, the counter stops changing while `state=active` → the
observer's L2 fires. Normal long operations keep ticking, so L2 does not false-fire.

---

## 5. Run the in-process supervisor (recreate on request)

The observer requests a recreate by dropping `/tmp/svc_crash_<service>`. `runtime.run_service`
consumes it, tears the service down (bounded), and rebuilds it — while `core.Supervisor` keeps
the `min_stable_ms` / `max_consecutive_start_failures` bookkeeping (FR-32/37):

```python
import asyncio
from svc_watch import runtime
from svc_watch.adapters.clock_monotonic import MonotonicClock

class _Log:
    def log(self, event, **f):
        print(event, f)

async def supervise_one(cfg, name, factory, teardown, crash_path):
    sup = runtime.build_supervisor(cfg, "unified", start_mech=None,
                                   clock=MonotonicClock(), logger=_Log())
    stop = asyncio.Event()
    await runtime.run_service(sup, name, factory, teardown,
                              crash_path=crash_path, stop_event=stop,
                              poll_s=1.0, stop_timeout_s=20.0)
```

A `factory` is an `async` callable returning the service's resource handle; `teardown(name,
resource)` releases it. Both are **your** code — the library stays service-agnostic. Make the
factory atomic: if it raises after binding a port, release the port before propagating, or
every retry hits "address in use".

```python
async def config_server_factory():
    server = await start_aiohttp_server(port=8000)
    try:
        return server
    except BaseException:
        await server.cleanup()                     # atomicity: no orphaned listener
        raise

async def config_server_teardown(name, server):
    await server.cleanup()
```

---

## 6. Wire a whole process (composition root)

A composition root is thin: load config → build transport → for each service, start its
factory, its pulse (loop or probe), its activity, and its supervise loop. The library provides
the wiring; you provide the factories. See `examples/unified_rut/main.py` — it exposes a
`BeatBridge` (the `wd_beat` replacement: `bridge.beat(name)` / `bridge.activity(name)`), builds
one transport, and calls `emit.pulse_loop` for `from:"probe"` services and `runtime.run_service`
for supervision. To add a **new** process, copy `examples/new_process_template/`.

---

## 7. How config links map to code

| Config link | Who acts on it | Your Python job |
|---|---|---|
| `signals.pulse` ↔ `watch.L1_pulse_lost` | observer judges; you emit | send `format_pulse(name)` (loop) or use `emit.pulse_loop` (probe) |
| `signals.activity` ↔ `watch.L2_activity_frozen` | observer judges; you emit | `ActivityEmitter.tick()/idle()` from the work code |
| `ladders` + `@recreate` | observer drops the request file | `runtime.run_service` consumes it and recreates the service |
| `restart_process` verb | observer runs `sh init.d restart` | nothing — the process is restarted for you |
| `services.*.port` / `probe.port @port` | you bind; probe checks | bind the same port your factory uses |
| global service-name uniqueness (Rule 7) | validator enforces | name each service uniquely across ALL processes |

You never call "L1" or "ladder" in code — you emit signals; the observer maps them to levels
and ladders exactly as the config declares.
