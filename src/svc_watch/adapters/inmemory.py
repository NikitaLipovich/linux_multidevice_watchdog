"""inmemory — OS-free implementations of ALL contracts + FakeClock (consumer B).

Proves that core is not nailed to unix/procd/files: the same health machine
runs on simulated time and actions recorded into memory.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, List, Optional

from ..contracts import Signal
from .wire import parse_text_v1


# ─── Clock: controllable monotonic time ───
class FakeClock:
    def __init__(self, start_ms: int = 0) -> None:
        self._ms = start_ms

    def now_ms(self) -> int:
        return self._ms

    def advance(self, ms: int) -> None:
        if ms < 0:
            raise ValueError("time does not go backwards")
        self._ms += ms

    def set(self, ms: int) -> None:
        self._ms = ms


# ─── Transport: shared in-memory bus ───
class MemoryBus:
    """Meeting point of the sender and the observer within one process."""

    def __init__(self) -> None:
        self.raw: List[str] = []
        self._lock = threading.Lock()

    def push(self, msg: str) -> None:
        with self._lock:
            self.raw.append(msg)

    def drain_signals(self) -> List[Signal]:
        with self._lock:
            raw, self.raw = self.raw, []
        out = []
        for m in raw:
            s = parse_text_v1(m)
            if s is not None:
                out.append(s)
        return out


class InMemoryTransport:
    """emit never raises and is thread-safe (FR-11): writes to the bus under a lock."""

    def __init__(self, bus: MemoryBus, *, drop: bool = False) -> None:
        self._bus = bus
        self._drop = drop            # drop=True simulates "no listener"

    def emit(self, msg: str) -> None:
        try:
            if self._drop:
                return
            self._bus.push(msg)
        except Exception:
            return                   # contract: the hot path stays silent on any failure


# ─── Probe: programmable result ───
class InMemoryProbe:
    def __init__(self, result: Any = True) -> None:
        # result: bool OR callable() -> bool
        self._result = result

    def check(self) -> bool:
        r = self._result
        return bool(r() if callable(r) else r)

    def set(self, result: Any) -> None:
        self._result = result


# ─── Start mechanism: fake handle ───
class _Handle:
    def __init__(self, name: str) -> None:
        self.name = name
        self.alive = True


class InMemoryStartMechanism:
    def __init__(self, *, fail_on: Optional[Callable[[Any], bool]] = None) -> None:
        self.created: List[str] = []
        self.torn_down: List[str] = []
        self._fail_on = fail_on

    def create(self, service: Any) -> Any:
        if self._fail_on is not None and self._fail_on(service):
            raise RuntimeError("factory failed for %s" % service.name)
        self.created.append(service.name)
        return _Handle(service.name)

    def teardown(self, handle: Any, timeout_ms: int) -> bool:
        if handle is not None:
            handle.alive = False
            self.torn_down.append(handle.name)
        return True


# ─── Action executor: records the target ───
class InMemoryActionExecutor:
    def __init__(self, label: str = "action") -> None:
        self.label = label
        self.calls: List[str] = []

    def execute(self, target: str) -> None:
        self.calls.append(target)


# ─── Process controller: records restarts ───
class InMemoryController:
    def __init__(self) -> None:
        self.restarts: List[str] = []

    def restart(self, process: str) -> None:
        self.restarts.append(process)


# ─── Gate: programmable ───
class InMemoryGate:
    def __init__(self, allow: bool = True) -> None:
        self._allow = allow

    def allow(self) -> bool:
        return self._allow

    def set(self, allow: bool) -> None:
        self._allow = allow


# ─── Logger: list of events ───
class ListLogger:
    def __init__(self) -> None:
        self.events: List[dict] = []

    def log(self, event: str, **fields: Any) -> None:
        rec = {"event": event}
        rec.update(fields)
        self.events.append(rec)

    def count(self, event: str) -> int:
        return sum(1 for e in self.events if e["event"] == event)

    def has(self, event: str) -> bool:
        return self.count(event) > 0
