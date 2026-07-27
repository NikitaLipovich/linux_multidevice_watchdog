"""svc_watch_compat — read the single v2 config (svc_watch.conf) and present the v1.2-shaped
dict that the Python side (wd_runtime, wd_beat, flash.py) still expects.

This is the bridge that lets ONE authored config (svc_watch.conf) drive both halves: the C
observer reads it directly; the Python side reads it through build_v12_view(). We change only
the config SOURCE — wd_runtime/wd_beat logic is untouched.

Fail-closed: build_v12_view() validates svc_watch.conf via svc_watch.config.load() (which raises
on any problem) and refuses ambiguous inputs (e.g. non-uniform per-service pulse intervals that a
single global beat_interval_ms cannot represent).

Field map + ground-truth values: artifacts/watchdog-v2/config-unify/MAPPING.md.
"""

from svc_watch import config as _cfgmod


def build_v12_view(path_or_cfg):
    """svc_watch.conf (path) or a parsed svc_watch.config.Config -> v1.2-shaped dict."""
    cfg = path_or_cfg if isinstance(path_or_cfg, _cfgmod.Config) else _cfgmod.load(path_or_cfg)
    return _map(cfg)


def _map(cfg):
    if "unified" not in cfg.processes:
        raise _cfgmod.WdConfigError(
            [_cfgmod.Problem("processes.unified",
                             "v1.2 view expects a process named 'unified'")])
    proc = cfg.processes["unified"]
    # config-native per-service toggle: enabled=false services are excluded from the v1.2 view, so
    # they are absent from the registry and run_services never starts them (the C observer skips them
    # independently). Everything below then operates on enabled services only.
    svcs = {n: s for n, s in proc.services.items() if s.enabled}
    if not svcs:
        raise _cfgmod.WdConfigError(
            [_cfgmod.Problem("processes.unified.services",
                             "no enabled services — at least one service must have enabled != false")])

    # B6: one global beat_interval_ms — refuse if per-service every_ms diverge (the v1.2 view
    # cannot represent per-service intervals, and the old code assumes one global value).
    every = sorted({s.pulse.every_ms for s in svcs.values()})
    if len(every) != 1:
        raise _cfgmod.WdConfigError(
            [_cfgmod.Problem("signals.pulse.every_ms",
                             "per-service every_ms differ %s — a single global beat_interval_ms "
                             "cannot represent this; make them uniform or migrate the Python side "
                             "off the v1.2 view" % every)])
    beat_interval_ms = every[0]

    # B6: one global probe timeout (only probe-based services carry it)
    probe_tos = sorted({s.pulse.probe.timeout_ms for s in svcs.values()
                        if s.pulse.probe is not None and s.pulse.probe.timeout_ms is not None})
    probe_timeout_ms = probe_tos[0] if probe_tos else 2000

    # B4/B6: L2 activity tick comes from the config_server service specifically
    cs = svcs.get("config_server")
    activity_tick_ms = (cs.activity.tick_ms if cs is not None and cs.activity is not None else 2000)

    # B3: the request-file template uses {service}; wd_runtime/wd_beat call .format(name=...)
    recreate = cfg.actions["recreate"]
    crash_template = str(recreate.params["file"]).replace("{service}", "{name}")

    sup = proc.supervisor

    services = []
    for name, s in svcs.items():
        l1 = s.watch.get("L1_pulse_lost")
        l2 = s.watch.get("L2_activity_frozen")
        services.append({
            "name": name,
            "entry": s.start.params.get("entry"),
            "wait_pulse_timeout_ms": (l1.params.get("dead_after_ms") if l1 else 0),
            # not read by the Python side today, kept so the dict is a complete v1.2 view:
            "action": ("restart" if (l1 and l1.mode == "act") else "log"),
            "progress_stall_ms": (l2.params.get("frozen_after_ms") if l2 else 0),
        })

    return {
        "socket": cfg.framework.transport.params["socket"],
        "beat_interval_ms": beat_interval_ms,
        "process": {
            "crash_file_template": crash_template,
            "pidfile": proc.launch.params.get("pidfile"),
        },
        "python": {
            "teardown_timeout_ms": sup.stop_timeout_ms,
            "probe_timeout_ms": probe_timeout_ms,
            # B2: http_shutdown has no v2 home; server.py already defaults 5.0s. Emit the same
            # value so the v1.2 view stays complete and config_server keeps its timeout.
            "http_shutdown_timeout_ms": 5000,
            "crash_poll_ms": sup.poll_ms,
            "activity_tick_ms": activity_tick_ms,
            "config_server_port": (cs.port if cs is not None else None),
            "supervise_backoff": {
                "initial_ms": sup.backoff.start_ms,
                "factor": sup.backoff.factor,
                "max_ms": sup.backoff.cap_ms,
            },
            "log": {
                "file": sup.log.file,
                "max_bytes": sup.log.rotate_kb * 1024,   # B5: rotate_kb (KB) -> max_bytes (bytes)
                "keep": sup.log.keep,
            },
        },
        "services": services,
    }
