"""K3 gate — offline integration: the whole Python side configures from svc_watch.conf alone,
builds the registry, constructs crash paths (first supervise) without KeyError, and emits the
right heartbeat/activity datagrams."""
import logging
import os
from pathlib import Path

from conftest import _LIB

SVC_WATCH = os.path.join(_LIB, "svc_watch.conf")


def test_load_config_from_svc_watch_builds_registry_and_crash_paths():
    import wd_runtime
    wd_runtime.CONFIG_PATH = SVC_WATCH            # default source is now svc_watch.conf
    cfg = wd_runtime.load_config()                # v2 -> v1.2 view; passes _REQUIRED_KEYS
    rt = wd_runtime.WdRuntime(cfg, logging.getLogger("k3"))
    assert [n for n, _ in rt.service_registry()] == \
        ["udp_logger", "data_uploader", "config_server", "ws_bridge"]
    # first supervise builds a crash path per service without KeyError('service')
    for name, _ in rt.service_registry():
        assert Path(rt.crash_template.format(name=name)) == Path("/tmp/svc_crash_%s" % name)
    assert rt.config_server_port == 8000
    assert rt.http_shutdown_s == 5.0


def test_heartbeat_and_activity_datagram_forms():
    import wd_beat
    sent = []

    class FakeSock:
        def sendto(self, data, addr):
            sent.append((data, addr))
        def close(self):
            pass

    wd_beat.CONFIG_PATH = SVC_WATCH
    wd_beat._cfg = None
    wd_beat._sock = FakeSock()

    wd_beat.beat("udp_logger")                    # base pulse (from:loop)
    assert sent[-1][0] == b"udp_logger"
    wd_beat.beat("config_server", "active", 42)   # L2 activity tick
    assert sent[-1][0] == b"config_server active 42"
    wd_beat.beat("config_server", "idle")         # closes the L2 episode
    assert sent[-1][0] == b"config_server idle"
    # all went to the socket declared in the single config
    assert sent[-1][1] == "/var/run/svc_wd.sock"
