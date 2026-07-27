# Examples — one subfolder per task

Grab the right example by folder name, no guessing:

| Folder | Use it to… | Files |
|---|---|---|
| `add-a-service/` | add a service to the existing `unified` process (Recipe A) | `service_block.jsonc` |
| `add-a-process/` | add a **whole new process** supervised by the observer (Recipe B) | `process_block.jsonc`, `vision_process.py`, `init.d-newproc.template` |
| `configs/` | ready-to-load full config examples | `svc_watch.single-service.conf` — one service (`config_server`, `P1` dropped); the shape for Modes 3/4 in `../README.md` → "Operating modes" |

Recipes A and B only touch `svc_watch.conf` + your service code; the observer re-reads the config, no rebuild.

---

## Recipe A — add a service to an existing process  (`add-a-service/`)

**1. Declare it in the config.** Paste the object from `add-a-service/service_block.jsonc` into the
`services` map of the target process in `svc_watch.conf` (e.g. `processes.unified.services`). Strip the
`//` comments — they are for reading only. Validate:

```sh
python -c "import sys; sys.path.insert(0,'src'); \
from svc_watch import config; config.load('examples/configs/svc_watch.single-service.conf'); print('OK')"
```

**2. Write the factory.** Add a `start_telemetry_relay` factory to that process's composition root (for
`unified`, that is `run_services.py` — the same place `start_udp_logger` etc. live). A `from:"loop"`
service **must** send its own pulse every `every_ms`:

```python
from svc_watch.adapters.wire import format_pulse   # or the process's BeatBridge.beat

async def start_telemetry_relay(transport):
    while True:
        relay_one_batch()                 # your work
        transport.emit(format_pulse("telemetry_relay"))   # proof of life = the send
        await asyncio.sleep(5)             # ~ every_ms
    # for an HTTP-style service with no loop, use from:"probe" in the config instead
```

**3. Keep the name globally unique** (Rule 7) and restart the process. Test the wiring with the mute hook:
`touch /tmp/wd_test_mute_telemetry_relay` → the observer logs `no_pulse` → `action` → the supervisor
recreates it → `recovered`; then `rm` the flag.

---

## Recipe B — add a whole new process tied to the observer  (`add-a-process/`)

**1. Declare the process.** Merge `add-a-process/process_block.jsonc` (a `processes.vision` block, two
camera services) into the `processes` map of `svc_watch.conf`. It reuses the existing `ladders`, so its
`restart_process` targets **vision**, never `unified` — escalation is addressed by nesting. Validate as above.

**2. Write the composition root.** Copy `add-a-process/vision_process.py`, fill in the service factories.
It loads the config, builds the transport, and runs each service's pulse + supervise loop (the library
provides the wiring; you provide the factories). See `../README.md` → "How the two sides connect
(embedding the library)".

**3. Install the init script.** Copy `add-a-process/init.d-newproc.template` → `/etc/init.d/vision`,
`chmod +x`, set `PIDFILE` equal to `processes.vision.launch.pidfile`, then
`/etc/init.d/vision enable && /etc/init.d/vision start`.

**4. Verify.** After the process's grace window there should be **no** false `no_pulse` while the services
are alive. Escalation: `@escalate_std` on a camera means 3 recreates → restart **vision**; both cameras
silent at once trips `P1_all_pulses_lost` → restart vision; a stuck request file trips `P2_request_stuck`.
All target this process only.

---

Config reference and the full logic (levels, ladders, pairing, validator rules): see `../README.md`.
