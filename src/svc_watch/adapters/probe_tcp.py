"""probe_tcp — real probe: TCP-connect to a port. Success → the target accepts connections.
Does not raise: any connection error = False (no pulse is sent, L1 catches the silence)."""

from __future__ import annotations

import socket


class TcpProbe:
    def __init__(self, host: str, port: int, timeout_ms: int) -> None:
        self._host = host
        self._port = port
        self._timeout_s = max(0.001, timeout_ms / 1000.0)

    def check(self) -> bool:
        try:
            with socket.create_connection((self._host, self._port), self._timeout_s):
                return True
        except Exception:
            return False
