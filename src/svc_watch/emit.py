"""svc_watch.emit — signal emission on the PROCESS SIDE (consumer A, py).

The observer (C daemon) JUDGES; here the services EMIT. The emit transport never
raises (FR-11). The pulse is sent by the running code ITSELF (its own from:loop) or
by a library coroutine with a probe (from:probe): if the probe fails → we do NOT
send the pulse, and the silence is caught by L1 (FR-18). Activity is a throttled
counter meaning "the thread is spinning".

3.9-compatible (no TaskGroup); loop tasks are returned to the owner (FR-20/21).
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from .adapters.wire import format_activity, format_pulse, format_state
from .contracts import Transport


# ── pulse ──
async def pulse_loop(name: str, transport: Transport, every_s: float,
                     probe: Optional[Callable[[], Awaitable[bool]]] = None) -> None:
    """Basic pulse for a service that has no loop of its own. Created INSIDE the
    service task — it dies together with it (a dead service must not "pulse")."""
    while True:
        alive = True
        if probe is not None:
            try:
                alive = await probe()
            except Exception:
                alive = False
        if alive:
            transport.emit(format_pulse(name))     # never-raises (FR-11)
        await asyncio.sleep(every_s)


def tcp_probe(host: str, port: int, timeout_s: float) -> Callable[[], Awaitable[bool]]:
    """Closure: True if someone is ACCEPTING on host:port (the service's listener)."""
    async def probe() -> bool:
        try:
            _r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout_s)
        except Exception:
            return False
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass
        return True
    return probe


# ── activity (L2): throttled counter meaning "the worker thread is alive" ──
class ActivityEmitter:
    """The worker code calls tick() on each iteration; we emit at most once per
    tick_s with a single datagram `<name> active N` (form 3). The end of work is
    idle() (form 2, closes the L2 episode). The tick_s value is passed to the worker
    code as a PARAMETER (slicing of waits) — the worker code does not import the
    config/wd (FR-16)."""

    def __init__(self, name: str, transport: Transport, tick_s: float,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self._name = name
        self._tx = transport
        self._tick_s = tick_s
        self._clock = clock or _monotonic
        self._counter = 0
        self._last_emit = 0.0
        self._active = False

    @property
    def tick_s(self) -> float:
        return self._tick_s

    def tick(self) -> None:
        """One iteration of the worker loop. Throttled to 1 datagram per tick_s."""
        self._counter += 1
        now = self._clock()
        if not self._active or now - self._last_emit >= self._tick_s:
            self._active = True
            self._last_emit = now
            self._tx.emit(format_activity(self._name, self._counter))

    def idle(self) -> None:
        """The work operation has finished → close the L2 episode (sent from finally)."""
        if self._active:
            self._active = False
            self._tx.emit(format_state(self._name, "idle"))


def _monotonic() -> float:
    import time
    return time.monotonic()
