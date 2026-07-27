"""Контракт Probe ×2: check() -> bool, не бросает; успех=цель принимает, провал=False."""
import socket

from svc_watch.adapters.inmemory import InMemoryProbe
from svc_watch.adapters.probe_tcp import TcpProbe


def test_inmemory_probe_programmable():
    p = InMemoryProbe(True)
    assert p.check() is True
    p.set(False)
    assert p.check() is False
    flip = {"v": True}
    p2 = InMemoryProbe(lambda: flip["v"])
    assert p2.check() is True
    flip["v"] = False
    assert p2.check() is False


def test_tcp_probe_true_on_open_port():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        p = TcpProbe("127.0.0.1", port, timeout_ms=500)
        assert p.check() is True
    finally:
        srv.close()


def test_tcp_probe_false_on_closed_port():
    # порт, который никто не слушает: занимаем и сразу закрываем
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()
    p = TcpProbe("127.0.0.1", port, timeout_ms=300)
    assert p.check() is False       # не бросает, просто False
