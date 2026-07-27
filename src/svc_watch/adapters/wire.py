"""wire — text_v1 datagram format (PROTOCOL.md). Lives in the transport adapters,
NOT in core: core receives an already-parsed Signal and knows nothing of the format."""

from __future__ import annotations

from typing import Optional

from ..contracts import Signal


def format_pulse(service: str) -> str:
    return service


def format_state(service: str, state: str) -> str:
    return "%s %s" % (service, state)


def format_activity(service: str, counter: int, state: str = "active") -> str:
    return "%s %s %d" % (service, state, counter)


def parse_text_v1(msg: str) -> Optional[Signal]:
    """Three forms of one ASCII string → Signal. Malformed datagram → None (unknown)."""
    parts = msg.strip().split(" ")
    if not parts or parts[0] == "":
        return None
    service = parts[0]
    state: Optional[str] = None
    counter: Optional[int] = None
    if len(parts) >= 2:
        state = parts[1]
    if len(parts) >= 3:
        try:
            counter = int(parts[2])
        except ValueError:
            return None
    return Signal(service=service, state=state, counter=counter)
