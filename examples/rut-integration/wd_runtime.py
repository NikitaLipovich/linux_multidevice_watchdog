"""Watchdog runtime library for the unified services process (WATCHDOG v1.2).

Everything watchdog-related that used to live inline in run_services.py:
FAIL-CLOSED config loading/validation, the service registry, entry resolution,
beat loops with probe adapters, the per-service supervisor with crash-file
restarts, bounded teardown, process logging setup and bench test hooks.
run_services.py keeps only service logic (factories + their teardown) and
wires it to this library through closures/adapters.

FAIL-CLOSED policy: /etc/svc_watch.conf is the ONLY source of truth.
A missing/unparsable config or a missing required key raises WdConfigError —
the caller reports it loudly and exits non-zero (procd owns the respawn).
There are NO built-in defaults: a value the code needs but the config lacks is
a config bug to fix in the config, never to paper over here. The C daemon
(svc_watchdog.c) enforces the same policy on its side of the same file.
"""

import asyncio
import importlib
import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import wd_beat  # heartbeat sender (same config file); hard import: missing = deploy bug

# Bench test hook (protocol constant, pair of wd_beat's /tmp/wd_test_mute_<name>):
# an existing file blocks the WHOLE event loop for 90s (no_pulse_all injection).
HANG_FILE = "/tmp/wd_test_hang"

CONFIG_PATH = os.environ.get("WD_CONFIG", "/etc/svc_watch.conf")


class WdConfigError(RuntimeError):
    """Config missing/broken/incomplete — the process must refuse to start."""


# Every key the PYTHON side reads (the C daemon validates its own set).
_REQUIRED_KEYS = (
    "socket",
    "beat_interval_ms",
    "process.crash_file_template",
    "process.pidfile",
    "python.teardown_timeout_ms",
    "python.probe_timeout_ms",
    "python.http_shutdown_timeout_ms",
    "python.crash_poll_ms",
    "python.activity_tick_ms",
    "python.config_server_port",
    "python.supervise_backoff.initial_ms",
    "python.supervise_backoff.factor",
    "python.supervise_backoff.max_ms",
    "python.log.file",
    "python.log.max_bytes",
    "python.log.keep",
)
_REQUIRED_SERVICE_KEYS = ("name", "entry", "wait_pulse_timeout_ms")


def _dig(cfg, dotted):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None, False
        node = node[part]
    return node, True


def load_config(path=None):
    """Parse + validate the shared watchdog config. FAIL-CLOSED: any problem
    raises WdConfigError listing EVERY missing key at once (fix in one pass)."""
    path = path or CONFIG_PATH
    try:
        with open(path) as f:
            cfg = json.load(f)
    except OSError as e:
        raise WdConfigError(f"{path}: cannot read config ({e})") from e
    except ValueError as e:
        raise WdConfigError(f"{path}: unparsable JSON ({e})") from e

    # Single source of truth = svc_watch.conf (v2). If we were handed a v2 file, map it to the
    # v1.2-shaped dict the code below expects (svc_watch_compat validates it, fail-closed).
    if isinstance(cfg, dict) and cfg.get("schema") == 2:
        try:
            import svc_watch_compat
            cfg = svc_watch_compat.build_v12_view(path)
        except Exception as e:
            raise WdConfigError(f"{path}: cannot map svc_watch.conf to the v1.2 view ({e})") from e

    missing = [k for k in _REQUIRED_KEYS if not _dig(cfg, k)[1]]
    services = cfg.get("services")
    if not isinstance(services, list) or not services:
        missing.append("services[] (non-empty array)")
    else:
        for i, svc in enumerate(services):
            for k in _REQUIRED_SERVICE_KEYS:
                if not isinstance(svc, dict) or k not in svc:
                    missing.append(f"services[{i}].{k}")
    if missing:
        raise WdConfigError(f"{path}: missing key(s): " + ", ".join(missing))
    return cfg


def resolve_entry(entry, self_globals):
    """'module:function' -> callable. `self_globals` is the caller's globals():
    entries pointing at run_services must resolve from the ALREADY-RUNNING
    module (importing run_services again under __main__ would duplicate it)."""
    mod_name, _, func_name = entry.partition(":")
    if not func_name:
        raise ValueError(f"bad entry '{entry}' (want 'module:function')")
    if mod_name in ("run_services", "__main__"):
        fn = self_globals.get(func_name)
        if fn is None:
            raise ImportError(f"no '{func_name}' in run_services")
        return fn
    return getattr(importlib.import_module(mod_name), func_name)


def setup_process_logging(cfg, fallback_paths=(), silence_propagate=(),
                          warning_only=()):
    """Rotating unified.log per python.log — same directory as the C daemon's
    wd.log: one place to read a week later. `fallback_paths` are tried when the
    primary directory can't be created (e.g. SD not mounted yet).
    `silence_propagate`/`warning_only` are service-specific logger names the
    caller wants kept out of unified.log (adapters: the library does not know
    the services)."""
    lcfg = cfg["python"]["log"]
    handlers = [logging.StreamHandler(sys.stdout)]
    for candidate in (lcfg["file"], *fallback_paths):
        try:
            Path(candidate).parent.mkdir(parents=True, exist_ok=True)
            handlers.append(RotatingFileHandler(
                candidate, maxBytes=int(lcfg["max_bytes"]),
                backupCount=int(lcfg["keep"])))
            break
        except Exception:
            continue
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=handlers,
    )
    for name in silence_propagate:
        logging.getLogger(name).propagate = False
    for name in warning_only:
        logging.getLogger(name).setLevel(logging.WARNING)
    return getattr(handlers[-1], "baseFilename", "stdout-only")


class WdRuntime:
    """One instance per process: validated config + the supervision machinery.

    Adapters in, behaviour out: factories, probes and teardown functions are
    passed as closures by the owner (run_services); this class never contains
    service-specific knowledge."""

    def __init__(self, cfg, logger):
        self.cfg = cfg
        self.log = logger
        p = cfg["python"]
        self.beat_interval_s = float(cfg["beat_interval_ms"]) / 1000.0
        self.probe_timeout_s = float(p["probe_timeout_ms"]) / 1000.0
        self.http_shutdown_s = float(p["http_shutdown_timeout_ms"]) / 1000.0
        self.teardown_timeout_s = float(p["teardown_timeout_ms"]) / 1000.0
        self.crash_poll_s = float(p["crash_poll_ms"]) / 1000.0
        self.activity_tick_s = float(p["activity_tick_ms"]) / 1000.0
        self.config_server_port = int(p["config_server_port"])
        bo = p["supervise_backoff"]
        self.backoff_initial_s = float(bo["initial_ms"]) / 1000.0
        self.backoff_factor = float(bo["factor"])
        self.backoff_max_s = float(bo["max_ms"]) / 1000.0
        self.crash_template = cfg["process"]["crash_file_template"]

    # ---- registry ------------------------------------------------------

    def service_registry(self):
        """[(name, entry)] straight from services[] (validated by load_config).
        Also warns about timeout/beat ratios that make false no_pulse likely."""
        pairs = []
        for svc in self.cfg["services"]:
            timeout_ms = float(svc["wait_pulse_timeout_ms"])
            if timeout_ms < 3 * self.beat_interval_s * 1000.0:
                self.log.warning(
                    f"[config] {svc['name']}: wait_pulse_timeout_ms={timeout_ms:.0f} < "
                    f"3×beat_interval_ms={3 * self.beat_interval_s * 1000.0:.0f} — "
                    f"false no_pulse likely")
            pairs.append((svc["name"], svc["entry"]))
        return pairs

    # ---- pulses / probes -----------------------------------------------

    async def beat_loop(self, name, probe=None):
        """Base pulse for services with no periodic loop of their own. Created
        INSIDE the service's factory so it dies with the service task — a dead
        service must never keep 'beating'.

        `probe` (async -> bool) gates every beat on the service REALLY serving:
        matrix G3 caught config_server dead (health 000) while its sibling
        beat-coroutine kept pulsing. No probe success -> no pulse -> wd acts."""
        while True:
            alive = True
            if probe is not None:
                try:
                    alive = await probe()
                except Exception:
                    alive = False
            if alive:
                wd_beat.beat(name)
            await asyncio.sleep(self.beat_interval_s)

    def beat_task(self, name, probe=None):
        """create_task adapter around beat_loop (callers keep the strong ref)."""
        return asyncio.create_task(self.beat_loop(name, probe=probe),
                                   name=f"beat:{name}")

    def tcp_probe(self, host, port):
        """Closure: True if something ACCEPTS on host:port (the service's own
        listener)."""
        async def probe():
            try:
                _r, w = await asyncio.wait_for(
                    asyncio.open_connection(host, port), self.probe_timeout_s)
            except Exception:
                return False
            w.close()
            try:
                await w.wait_closed()
            except Exception:
                pass
            return True
        return probe

    # ---- supervision ----------------------------------------------------

    async def _bounded_teardown(self, name, resource, teardown):
        """Best-effort release of a service's resources so a restart can rebind
        its ports. Bounded: a hanging teardown must never wedge the supervisor
        (G4 contract). `teardown` is the owner's service-specific closure."""
        if resource is None:
            return
        try:
            await asyncio.wait_for(teardown(name, resource),
                                   timeout=self.teardown_timeout_s)
        except asyncio.TimeoutError:
            self.log.error(f"[supervisor] teardown of {name} TIMED OUT "
                           f"({self.teardown_timeout_s:.0f}s) — proceeding")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.log.warning(f"[supervisor] teardown of {name} raised: {e}")

    async def supervise(self, name, factory, stop_event, teardown):
        """Run one service under a restart-loop with exponential backoff.

        A crash (or failed start) of ONE service restarts ONLY that service —
        the fault isolation the 4 separate processes used to give. The C
        watchdog requests a restart by creating /tmp/svc_crash_<name> (path
        from process.crash_file_template); this loop polls and consumes it.
        NOT TaskGroup (3.11+/all-or-nothing), NOT bare gather (orphans/cascade).
        """
        starts = 0
        backoff = self.backoff_initial_s
        crash_path = Path(self.crash_template.format(name=name))
        while not stop_event.is_set():
            resource = None
            try:
                resource = await factory()
                starts += 1
                backoff = self.backoff_initial_s
                self.log.info(f"[supervisor] {name} up (start #{starts})")
                # Monitor loop: alive until shutdown or an injected crash.
                while not stop_event.is_set():
                    if crash_path.exists():
                        try:
                            crash_path.unlink()
                        except OSError:
                            pass
                        raise RuntimeError(f"injected crash for {name}")
                    await asyncio.sleep(self.crash_poll_s)
                await self._bounded_teardown(name, resource, teardown)
                return
            except asyncio.CancelledError:
                await self._bounded_teardown(name, resource, teardown)
                raise
            except Exception as e:
                self.log.error(f"[supervisor] {name} crashed: {e} — restart in "
                               f"{backoff}s", exc_info=True)
                await self._bounded_teardown(name, resource, teardown)
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                backoff = min(backoff * self.backoff_factor, self.backoff_max_s)
        self.log.info(f"[supervisor] {name} stopped")

    def supervise_all(self, registry, self_globals, stop_event, teardown):
        """Spawn one supervise task per registry entry. An entry that fails to
        resolve kills ONLY that service (loud log) — neighbours still start."""
        tasks = []
        for name, entry in registry:
            try:
                factory = resolve_entry(entry, self_globals)
            except Exception as e:
                self.log.error(f"[config] service '{name}': entry '{entry}' "
                               f"failed to resolve ({e}) — SERVICE NOT STARTED")
                continue
            tasks.append(asyncio.create_task(
                self.supervise(name, factory, stop_event, teardown),
                name=f"supervise:{name}"))
        return tasks

    # ---- bench test hooks ------------------------------------------------

    async def test_hang_watcher(self):
        """Bench-matrix hook (G3 #2): `touch /tmp/wd_test_hang` blocks the WHOLE
        event loop with time.sleep(90) — all pulses stop, the watchdog must see
        no_pulse_all."""
        while True:
            if os.path.exists(HANG_FILE):
                try:
                    os.unlink(HANG_FILE)
                except OSError:
                    pass
                self.log.warning("[test-hook] wd_test_hang: blocking event loop for 90s")
                time.sleep(90)
            await asyncio.sleep(1.0)
