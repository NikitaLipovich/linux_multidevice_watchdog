"""K1 gate — the v1.2 view built from svc_watch.conf must reproduce the ground-truth values that
the Python side used to read from svc_watchdog.conf (functional, not import-only)."""
import copy
import json
import logging
import os

import pytest

from conftest import _LIB          # external-storage-contents (on sys.path)
import svc_watch_compat            # the mapper (external-storage-contents/svc_watch_compat.py)
from svc_watch import config as cfgmod

# Ground truth = the values the retired svc_watchdog.conf carried (hardcoded so this test stays
# self-contained after that file is removed). The mapper must reproduce these from svc_watch.conf.
GT = {
    "socket": "/var/run/svc_wd.sock",
    "beat_interval_ms": 5000,
    "process": {"crash_file_template": "/tmp/svc_crash_{name}"},
    "python": {
        "teardown_timeout_ms": 20000, "probe_timeout_ms": 2000, "http_shutdown_timeout_ms": 5000,
        "crash_poll_ms": 1000, "activity_tick_ms": 2000, "config_server_port": 8000,
        "supervise_backoff": {"initial_ms": 1000, "factor": 2, "max_ms": 10000},
        "log": {"file": "/mnt/sda1/crash_logs/unified.log", "max_bytes": 1048576, "keep": 7},
    },
    "services": [
        {"name": "udp_logger", "entry": "run_services:start_udp_logger", "wait_pulse_timeout_ms": 15000},
        {"name": "data_uploader", "entry": "run_services:start_data_uploader", "wait_pulse_timeout_ms": 15000},
        {"name": "config_server", "entry": "run_services:start_config_server", "wait_pulse_timeout_ms": 30000},
        {"name": "ws_bridge", "entry": "run_services:start_ws_bridge", "wait_pulse_timeout_ms": 15000},
    ],
}
SVC_WATCH = os.path.join(_LIB, "svc_watch.conf")


def test_v12_view_matches_ground_truth():
    v = svc_watch_compat.build_v12_view(SVC_WATCH)
    assert v["socket"] == GT["socket"]
    assert v["beat_interval_ms"] == GT["beat_interval_ms"]
    assert v["process"]["crash_file_template"] == GT["process"]["crash_file_template"]
    p, gp = v["python"], GT["python"]
    for k in ("teardown_timeout_ms", "probe_timeout_ms", "http_shutdown_timeout_ms",
              "crash_poll_ms", "activity_tick_ms", "config_server_port"):
        assert p[k] == gp[k], (k, p[k], gp[k])
    assert p["supervise_backoff"] == gp["supervise_backoff"]
    assert p["log"]["file"] == gp["log"]["file"]
    assert p["log"]["keep"] == gp["log"]["keep"]
    # services: name / entry / wait_pulse_timeout_ms
    gs = {s["name"]: s for s in GT["services"]}
    vs = {s["name"]: s for s in v["services"]}
    assert set(vs) == set(gs)
    for n in gs:
        assert vs[n]["entry"] == gs[n]["entry"], n
        assert vs[n]["wait_pulse_timeout_ms"] == gs[n]["wait_pulse_timeout_ms"], n


def test_b5_log_max_bytes_is_rotate_kb_times_1024():
    v = svc_watch_compat.build_v12_view(SVC_WATCH)
    assert v["python"]["log"]["max_bytes"] == 1048576          # not the raw 1024
    assert v["python"]["log"]["max_bytes"] == GT["python"]["log"]["max_bytes"]


def test_b3_crash_template_formats_with_name():
    v = svc_watch_compat.build_v12_view(SVC_WATCH)
    assert v["process"]["crash_file_template"].format(name="udp_logger") == "/tmp/svc_crash_udp_logger"


def test_b4_activity_tick_from_config_server():
    v = svc_watch_compat.build_v12_view(SVC_WATCH)
    assert v["python"]["activity_tick_ms"] == 2000            # config_server.signals.activity.tick_ms


def test_b6_nonuniform_every_ms_rejected():
    raw = json.load(open(SVC_WATCH, encoding="utf-8"))
    raw["processes"]["unified"]["services"]["udp_logger"]["signals"]["pulse"]["every_ms"] = 3000
    cfg = cfgmod.parse(raw)                                   # valid v2 (dead_after 15000 >= 3*3000)
    with pytest.raises(cfgmod.WdConfigError):
        svc_watch_compat.build_v12_view(cfg)


def test_wd_runtime_builds_from_view_and_config_server_gets_shutdown():
    import wd_runtime
    v = svc_watch_compat.build_v12_view(SVC_WATCH)
    rt = wd_runtime.WdRuntime(v, logging.getLogger("k1"))
    assert rt.config_server_port == 8000
    assert rt.http_shutdown_s == 5.0                          # B2: config_server still gets a timeout
    assert rt.crash_template.format(name="x") == "/tmp/svc_crash_x"   # B3
    assert [n for n, _ in rt.service_registry()] == \
        ["udp_logger", "data_uploader", "config_server", "ws_bridge"]


def test_wd_beat_auto_detects_v2_and_maps():
    import wd_beat
    wd_beat.CONFIG_PATH = SVC_WATCH
    wd_beat._cfg = None
    c = wd_beat.config()
    assert c["socket"] == "/var/run/svc_wd.sock"
    assert wd_beat.crash_file_template().format(name="x") == "/tmp/svc_crash_x"
    assert c["python"]["activity_tick_ms"] == 2000           # B4: flash.py reads this via wd_beat.config()
