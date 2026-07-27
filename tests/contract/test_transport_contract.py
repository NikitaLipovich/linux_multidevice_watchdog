"""Контракт Transport ×2 реализации: emit НИКОГДА не бросает И потокобезопасен (FR-11).

Реальная доставка unix_datagram проверяется на Linux; на хосте без AF_UNIX SOCK_DGRAM
(Windows-дев) OS-специфичная доставка пропускается — но инвариант never-raise/thread-safe
проверяется на ОБЕИХ реализациях везде (это и есть суть контракта FR-11).
"""
import os
import threading
import uuid

import pytest

from svc_watch.adapters.inmemory import InMemoryTransport, MemoryBus
from svc_watch.adapters import transport_unix_datagram as udg


# ── общий инвариант: emit без слушателя не бросает ──
def test_inmemory_emit_never_raises_without_listener():
    t = InMemoryTransport(MemoryBus(), drop=True)
    t.emit("svc")                     # некуда слать — молча
    t.emit("svc active 1")


def test_unix_emit_never_raises_without_listener():
    t = udg.UnixDatagramTransport("/nonexistent/socket/path.sock")
    t.emit("svc")                     # нет слушателя → молчит, не бросает
    t.emit("svc active 1")


# ── общий инвариант: конкурентные emit из N потоков без исключений ──
@pytest.mark.parametrize("make_transport", [
    lambda: InMemoryTransport(MemoryBus()),
    lambda: udg.UnixDatagramTransport("/nonexistent/socket/path.sock"),
], ids=["inmemory", "unix_datagram"])
def test_emit_thread_safe(make_transport):
    t = make_transport()
    errors = []

    def worker():
        try:
            for i in range(200):
                t.emit("svc active %d" % i)
        except Exception as e:   # контракт: не должно быть НИ ОДНОГО
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []


# ── доставка ──
def test_inmemory_delivery():
    bus = MemoryBus()
    t = InMemoryTransport(bus)
    t.emit("config_server active 1042")
    sigs = bus.drain_signals()
    assert len(sigs) == 1
    assert sigs[0].service == "config_server"
    assert sigs[0].state == "active"
    assert sigs[0].counter == 1042


@pytest.mark.skipif(not udg.HAVE_UNIX_DGRAM,
                    reason="AF_UNIX SOCK_DGRAM недоступен на этом хосте (не Linux)")
def test_unix_delivery_when_supported():
    path = os.path.join(os.environ.get("TMPDIR", "/tmp"),
                        "wd_ct_%s.sock" % uuid.uuid4().hex[:8])
    rx = udg.UnixDatagramReceiver(path)
    try:
        tx = udg.UnixDatagramTransport(path)
        tx.emit("udp_logger")
        tx.emit("config_server active 7")
        sigs = rx.drain()
        names = [s.service for s in sigs]
        assert "udp_logger" in names
        assert any(s.counter == 7 for s in sigs)
    finally:
        rx.close()
