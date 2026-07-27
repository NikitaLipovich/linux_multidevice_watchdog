"""vision_process.py — TEMPLATE composition root for a new process "vision".

The module name matches the `start.entry` prefix in process_block.jsonc
(`vision_process:start_cam_main`). If you rename the process, rename this file to match.

Copy this next to your service code, rename, and fill in the two factories. It shows the whole
wiring: load config → build transport → per service run its pulse + its supervise loop. The
observer (C daemon) judges; this process only emits heartbeats and recreates a service when the
observer asks (via the request file).

Run on the router as its own process under procd (see init.d-newproc.template). Ties to the
daemon purely by emitting text_v1 to the shared socket declared in svc_watch.conf.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

# make `import svc_watch` resolve (parent of the package). In a real deployment your launcher
# puts the package dir on sys.path; this keeps the template runnable from the repo too.
_SRC = Path(__file__).resolve().parents[2] / "src"  # add-a-process -> examples -> repo root, then src/
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from svc_watch import config, runtime               # noqa: E402
from svc_watch.adapters.clock_monotonic import MonotonicClock  # noqa: E402
from svc_watch.adapters.wire import format_pulse    # noqa: E402

PROCESS = "vision"
CONFIG_PATH = os.environ.get("WD_CONFIG", "/etc/svc_watch.conf")


class _Log:
    """Bridge the library Logger contract onto print/your logger."""
    def log(self, event, **fields):
        print(event, " ".join("%s=%s" % kv for kv in fields.items()))


# ── STEP: write your service factories (async: start → return a resource handle) ──
async def start_cam_main(transport):
    """Example from:"loop" service: its own loop sends the pulse (proof of life = the send)."""
    stop = asyncio.Event()

    async def loop():
        while not stop.is_set():
            # ... do one unit of camera work here ...
            transport.emit(format_pulse("cam_main"))   # never raises
            await asyncio.sleep(5.0)                    # ~ every_ms from the config

    task = asyncio.ensure_future(loop())               # strong ref lives as long as the service
    return {"task": task, "stop": stop}


async def start_cam_aux(transport):
    stop = asyncio.Event()

    async def loop():
        while not stop.is_set():
            transport.emit(format_pulse("cam_aux"))
            await asyncio.sleep(5.0)

    task = asyncio.ensure_future(loop())
    return {"task": task, "stop": stop}


async def teardown(name, resource):
    """Release everything the service owns so a recreate can rebind its resources."""
    if resource is None:
        return
    resource["stop"].set()
    resource["task"].cancel()
    try:
        await resource["task"]
    except asyncio.CancelledError:
        pass


async def main():
    try:
        cfg = config.load(CONFIG_PATH)
    except config.WdConfigError as e:
        print("FATAL vision: %s" % e, file=sys.stderr)
        raise SystemExit(1)

    transport = runtime.build_transport(cfg)
    sup = runtime.build_supervisor(cfg, PROCESS, start_mech=None,
                                   clock=MonotonicClock(), logger=_Log())
    proc = cfg.processes[PROCESS]
    factories = {"cam_main": start_cam_main, "cam_aux": start_cam_aux}
    stop = asyncio.Event()

    tasks = []
    for name in proc.services:
        crash_path = "/tmp/svc_crash_%s" % name        # matches actions.recreate.file template
        tasks.append(asyncio.ensure_future(runtime.run_service(
            sup, name,
            factory=lambda n=name: factories[n](transport),
            teardown=teardown,
            crash_path=crash_path, stop_event=stop,
            poll_s=proc.supervisor.poll_ms / 1000.0,
            stop_timeout_s=proc.supervisor.stop_timeout_ms / 1000.0)))

    await stop.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
