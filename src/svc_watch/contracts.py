"""svc_watch.contracts — the protocols through which core talks to the world (E2).

core.py does NOT know the OS: it holds only these abstractions. Each protocol has
>=2 implementations (a real one for the OS + inmemory for consumer B) — see adapters/.

Time units are WHOLE milliseconds (matching the config keys *_ms; no float drift
and no division). Clock.now_ms() is monotonic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

try:
    from typing import Protocol, runtime_checkable
except ImportError:                       # 3.7 fallback (the box is 3.9+, but we don't risk it)
    from typing_extensions import Protocol, runtime_checkable  # type: ignore


# ─── Structured signal (transport parses text_v1 → this; core doesn't know the format) ───
@dataclass(frozen=True)
class Signal:
    """A parsed datagram. The text_v1 form lives in the transport adapter, not in core."""
    service: str
    state: Optional[str] = None
    counter: Optional[int] = None


# ─── Clock (time injection, FR-22): core does not call time.* directly ───
@runtime_checkable
class Clock(Protocol):
    def now_ms(self) -> int:
        """Monotonic milliseconds. Real ones come from time.monotonic; fake ones are controllable."""
        ...


# ─── EMISSION transport (service hot path, FR-11: never-raises AND thread-safe) ───
@runtime_checkable
class Transport(Protocol):
    def emit(self, msg: str) -> None:
        """Send a signal string to the observer. CONTRACT:
        - NEVER raises (no listener/socket → silently dropped);
        - thread-safe under concurrent calls (event loop + executor threads)."""
        ...


# ─── Probe (proof of life before a from:probe pulse, FR-18) ───
@runtime_checkable
class Probe(Protocol):
    def check(self) -> bool:
        """True → target is alive (pulse may be sent). Never raises: a connection error = False."""
        ...


# ─── Service start mechanism (the factory receives the MODEL of its service, FR-3) ───
@runtime_checkable
class StartMechanism(Protocol):
    def create(self, service: Any) -> Any:
        """Start the service from its validated model; return a handle (resource).
        The factory does not read the config itself — it gets its model as a parameter."""
        ...

    def teardown(self, handle: Any, timeout_ms: int) -> bool:
        """Stop the service, releasing ALL of it (ports + tasks), BOUNDED by timeout_ms
        (FR-14). True — finished in time, False — aborted on timeout."""
        ...


# ─── SERVICE action executor (the ladder calls execute with the target service) ───
@runtime_checkable
class ActionExecutor(Protocol):
    def execute(self, target: str) -> None:
        """Apply the action to the target service (the adapter substitutes it into its
        template, e.g. request_file: creat(/tmp/svc_crash_<target>))."""
        ...


# ─── PROCESS control (the built-in restart_process verb, FR-38) ───
@runtime_checkable
class ProcessController(Protocol):
    def restart(self, process: str) -> None:
        """Restart the owner process (fork/exec sh <script> restart, FR-34)."""
        ...


# ─── Resource gate (before an action; on the C side, always-allow for consumer B) ───
@runtime_checkable
class ResourceGate(Protocol):
    def allow(self) -> bool:
        """True → there are enough resources, the action may run now."""
        ...


# ─── Observability: every line carries up= (FR-50); formatting is the logger's concern ───
@runtime_checkable
class Logger(Protocol):
    def log(self, event: str, **fields: Any) -> None:
        ...
