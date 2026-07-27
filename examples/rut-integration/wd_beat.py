"""Heartbeat sender for the svc_watchdog observer (fail-closed).

Single source of truth is the observer config (/etc/svc_watch.conf, JSON; read through
svc_watch_compat's v1.2 view): this module reads `socket`, `beat_interval_ms` and
`process.crash_file_template` from it so Python services and the C observer can never disagree
on names/paths.

FAIL-CLOSED (v1.2): there are NO built-in fallback values. config() raises on a
missing/broken file and the accessors raise KeyError on a missing key — in the
running process that can't happen (run_services refuses to start on the same
config), and any standalone caller gets a loud error instead of a silent guess.

beat() is the ONE exception: it is a bare sendto() syscall on the hot path of
every service loop and MUST never raise — with no usable config it simply sends
nothing (the process it lives in would not have started anyway).

Test hook (bench matrix): an existing /tmp/wd_test_mute_<name> file mutes the
pulse of that one service, simulating its silent death without touching it.
"""

import json
import os
import socket

CONFIG_PATH = os.environ.get("WD_CONFIG", "/etc/svc_watch.conf")

_cfg = None
_sock = None


def config():
    """The watchdog config dict. Raises RuntimeError when missing/unparsable —
    callers must not run on guesses (fail-closed)."""
    global _cfg
    if _cfg is None:
        try:
            with open(CONFIG_PATH) as f:
                raw = json.load(f)
        except Exception as e:
            raise RuntimeError(f"wd_beat: {CONFIG_PATH} unusable ({e}) — "
                               f"fail-closed, no defaults") from e
        # Single source of truth = svc_watch.conf (v2): map it to the v1.2 view wd_beat expects.
        if isinstance(raw, dict) and raw.get("schema") == 2:
            import svc_watch_compat
            _cfg = svc_watch_compat.build_v12_view(CONFIG_PATH)
        else:
            _cfg = raw
    return _cfg


def socket_path():
    return config()["socket"]


def interval_s():
    return float(config()["beat_interval_ms"]) / 1000.0


def crash_file_template():
    return config()["process"]["crash_file_template"]


def beat(name, state=None, counter=None):
    """Send one heartbeat datagram: b"<name>", b"<name> <state>" or
    b"<name> <state> <counter>". The counter is the L2 liveness tick of a worker
    loop (progress-stall detection) and only travels together with a state word.
    Never raises."""
    global _sock
    try:
        if os.path.exists("/tmp/wd_test_mute_" + name):
            return
        payload = name
        if state:
            payload += " " + state
            if counter is not None:
                payload += " " + str(int(counter))
        payload = payload.encode()
        if _sock is None:
            _sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        _sock.sendto(payload, socket_path())
    except Exception:
        # No config / no listener / no socket: the service must never care.
        try:
            if _sock is not None:
                _sock.close()
        except Exception:
            pass
        _sock = None
