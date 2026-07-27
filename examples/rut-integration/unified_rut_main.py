"""unified_rut — consumer A: a thin composition root on top of svc_watch.

Replaces the hand-wired plumbing of the old run_services.py. All watchdog machinery comes from
the library, assembled from svc_watch.conf; this file only does the ASSEMBLY plus the
service-specific factories/teardown (service code stays service code).

Role on the live router: this process is the SUPERVISED side (it emits signals and runs the
in-process supervisor). A separate C daemon (see daemon/) is the observer and reads the same
config. Layering: config → build_transport / emit / run_service.

MIGRATION ON THE BENCH (see docs/INSTALL_ROUTER.md — the flash path is production-critical):
  - move service emission from wd_beat.beat to this shim (BeatBridge.beat);
  - config_server: activity via emit.ActivityEmitter (tick_s from the config) — the work code
    (flash) receives tick as a PARAMETER, so the client protocol does not change;
  - deploy the pair (v2 daemon + /etc/svc_watch.conf) with v1.2 backups, negative test, smoke.
"""

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# unified_rut -> examples -> watchdog-v2 -> artifacts -> repo root; package lives under external-storage-contents
_LIB = _HERE.parent.parent.parent.parent / "external-storage-contents"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from svc_watch import config as cfgmod        # noqa: E402
from svc_watch import emit, runtime           # noqa: E402

logger = logging.getLogger("unified_rut")
CONFIG_PATH = os.environ.get("WD_CONFIG", "/etc/svc_watch.conf")


class BeatBridge:
    """Emission shim for service code: beat(name) / activity(name).

    Replaces v1.2 wd_beat — services call this instead of a raw sendto. transport.emit never
    raises (FR-11), so beat is safe on the hot path."""

    def __init__(self, cfg):
        self._cfg = cfg
        self._tx = runtime.build_transport(cfg)
        self._activity = {}
        # pre-build activity emitters for services that declare signals.activity
        for p in cfg.processes.values():
            for name, svc in p.services.items():
                if svc.activity is not None:
                    self._activity[name] = emit.ActivityEmitter(
                        name, self._tx, svc.activity.tick_ms / 1000.0)

    def beat(self, name, state=None, counter=None):
        from svc_watch.adapters.wire import format_activity, format_pulse, format_state
        if counter is not None:
            self._tx.emit(format_activity(name, counter, state or "active"))
        elif state is not None:
            self._tx.emit(format_state(name, state))
        else:
            self._tx.emit(format_pulse(name))

    def activity(self, name):
        return self._activity.get(name)

    @property
    def transport(self):
        return self._tx


async def _run_process(cfg, process_name, factories, teardown):
    """Assemble and run one process from the config: transport, probes, pulses, supervision.
    `factories[name]` is the service's async factory; `teardown(name, resource)`."""
    from svc_watch.adapters.clock_monotonic import MonotonicClock
    bridge = BeatBridge(cfg)
    sup = runtime.build_supervisor(cfg, process_name, start_mech=None,
                                   clock=MonotonicClock(), logger=_LibLog())
    proc = cfg.processes[process_name]
    stop = asyncio.Event()
    crash_tmpl = None
    for a in cfg.actions.values():
        if a.type == "request_file":
            crash_tmpl = a.params["file"]
            break

    tasks = []
    for name, svc in proc.services.items():
        factory = factories.get(name)
        if factory is None:
            logger.error("no factory for service %s — NOT started", name)
            continue
        # from:probe pulse — a library coroutine; from:loop — the service code sends beat itself
        probe = runtime.build_pulse_probe(svc)
        if probe is not None:
            tasks.append(asyncio.ensure_future(
                emit.pulse_loop(name, bridge.transport, svc.pulse.every_ms / 1000.0, probe=probe)))
        crash_path = (crash_tmpl or "/tmp/svc_crash_{service}").format(service=name)
        tasks.append(asyncio.ensure_future(runtime.run_service(
            sup, name, factory, teardown, crash_path=crash_path, stop_event=stop,
            poll_s=proc.supervisor.poll_ms / 1000.0,
            stop_timeout_s=proc.supervisor.stop_timeout_ms / 1000.0)))

    def _sig(_s, _f):
        stop.set()
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    await stop.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return bridge


class _LibLog:
    """Bridge the library Logger contract onto the standard logging module."""
    def log(self, event, **fields):
        logger.info("%s %s", event, " ".join("%s=%s" % kv for kv in fields.items()))


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    try:
        cfg = cfgmod.load(CONFIG_PATH)
    except cfgmod.WdConfigError as e:
        print("FATAL unified_rut: %s — refusing to start (fail-closed)" % e, file=sys.stderr)
        sys.exit(1)

    # Service factories are service-specific code (on the live bench they are imported from the
    # run_services modules). Here they are injected by the owner; the library itself never learns
    # service names (FR-42). An empty set = validate only.
    factories = {}
    teardown = None
    if not factories:
        print("unified_rut: config valid (%d processes, %d services). "
              "Service factories are wired in on the bench." %
              (len(cfg.processes), sum(len(p.services) for p in cfg.processes.values())))
        return

    asyncio.run(_run_process(cfg, "unified", factories, teardown))  # pragma: no cover


if __name__ == "__main__":
    main()
