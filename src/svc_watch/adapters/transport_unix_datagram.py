"""transport_unix_datagram — real emission transport (production RUT).

emit: AF_UNIX SOCK_DGRAM sendto — connectionless, best-effort. CONTRACT FR-11:
never raises (no socket/listener → silently dropped) AND thread-safe
(sendto is atomic; the socket is created lazily under a lock). The receiving side (observer)
on the production bench is a C daemon; here we have only the sender + an optional receiver for tests.
"""

from __future__ import annotations

import socket
import threading
from typing import List, Optional

from ..contracts import Signal
from .wire import parse_text_v1

# Flag for AF_UNIX SOCK_DGRAM support (Linux — yes; Windows — no).
HAVE_UNIX_DGRAM = hasattr(socket, "AF_UNIX")


class UnixDatagramTransport:
    def __init__(self, socket_path: str) -> None:
        self._path = socket_path
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()

    def _ensure(self) -> Optional[socket.socket]:
        if self._sock is not None:
            return self._sock
        with self._lock:
            if self._sock is None:
                try:
                    self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                except Exception:
                    self._sock = None
        return self._sock

    def emit(self, msg: str) -> None:
        try:
            s = self._ensure()
            if s is None:
                return
            s.sendto(msg.encode("ascii", "replace"), self._path)
        except Exception:
            return                    # no listener/socket — stay silent, the service is untouched

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                finally:
                    self._sock = None


class UnixDatagramReceiver:
    """Receiver for TESTS / the consumer-A harness (on the production bench the C daemon judges)."""

    def __init__(self, socket_path: str) -> None:
        self._path = socket_path
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._sock.bind(socket_path)
        self._sock.settimeout(1.0)

    def recv_signal(self) -> Optional[Signal]:
        try:
            data, _ = self._sock.recvfrom(128)
        except socket.timeout:
            return None
        return parse_text_v1(data.decode("ascii", "replace"))

    def drain(self) -> List[Signal]:
        out: List[Signal] = []
        self._sock.settimeout(0.05)
        while True:
            try:
                data, _ = self._sock.recvfrom(128)
            except socket.timeout:
                break
            except OSError:
                break
            s = parse_text_v1(data.decode("ascii", "replace"))
            if s is not None:
                out.append(s)
        return out

    def close(self) -> None:
        try:
            self._sock.close()
        finally:
            import os
            try:
                os.unlink(self._path)
            except OSError:
                pass
