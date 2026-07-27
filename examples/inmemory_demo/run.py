"""inmemory_demo — потребитель B (и C) БЕЗ ОС на FakeClock.

Гоняет ВСЮ библиотеку супервизии без единого файла/сокета/процесса: доказывает, что
core не прибит к unix/procd. Сценарий:
  1. L1: сервис замолчал → лестница → inmemory-действие → пульс вернулся → выздоровление.
  2. L2: счётчик активности замер при active → действие; прогресс → выздоровление.
  3. Потребитель C (второй процесс vision из 2 сервисов): оба молчат → P1 → рестарт
     ИМЕННО vision (адресно), unified остаётся цел.
Финал — печатает OK. Время симулировано, реальных пауз нет (< 5 c wall-clock).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# inmemory_demo -> examples -> repo root; the svc_watch package lives under src/
_LIB = os.path.join(_HERE, "..", "..", "src")
sys.path.insert(0, os.path.abspath(_LIB))

from svc_watch import config as cfgmod                       # noqa: E402
from svc_watch import runtime                                # noqa: E402
from svc_watch.adapters.inmemory import (FakeClock, InMemoryActionExecutor,   # noqa: E402
                                         InMemoryController, InMemoryGate, ListLogger)
from svc_watch.contracts import Signal                        # noqa: E402


def _svc(watch, *, activity=None):
    s = {"start": {"type": "inmemory"},
         "signals": {"pulse": {"from": "loop", "every_ms": 1000}},
         "watch": watch}
    if activity is not None:
        s["signals"]["activity"] = {"tick_ms": activity}
    return s


def _proc(services, watch=None):
    return {
        "launch": {"type": "inmemory", "grace": {"type": "fixed", "ms": 3000},
                   "restart_rate_limit": {"max": 5, "per_ms": 600000,
                                          "on_exceeded": "cooldown", "cooldown_ms": 300000}},
        "supervisor": {"poll_ms": 1000, "stop_timeout_ms": 20000, "min_stable_ms": 5000,
                       "max_consecutive_start_failures": 3,
                       "backoff": {"start_ms": 1000, "factor": 2, "cap_ms": 10000},
                       "log": {"file": "/mnt/sda1/u.log", "rotate_kb": 1024,
                               "keep": 7, "fallbacks": []}},
        "watch": watch or {},
        "services": services,
    }


def _config():
    L1 = lambda ladder, dead=3000: {"L1_pulse_lost": {"mode": "act",   # noqa: E731
                                                      "dead_after_ms": dead, "ladder": ladder}}
    return {
        "schema": 2,
        "framework": {
            "transport": {"type": "inmemory"},
            "observer": {"mode": "act", "tick_ms": 1000, "pause_file": "/tmp/p",
                         "oom_adj": -1000,
                         "log": {"file": "/mnt/sda1/wd.log", "rotate_kb": 1024,
                                 "keep": 7, "fsync": True},
                         "quiet": {"paused_ms": 60000, "unknown_ms": 60000}},
            "gates": {"min_free_mb": 8, "max_load1": 3.0, "recheck_ms": 5000,
                      "force_after_ms": 300000, "alarm_mb": 5},
            "pacing": {"type": "fixed", "delay_ms": 1000},
        },
        "actions": {
            "recreate": {"type": "request_file", "file": "/tmp/svc_crash_{service}",
                         "eat_within_ms": 10000, "startup_ms": 2000,
                         "rate_limit": {"max": 3, "per_ms": 100000,
                                        "on_exceeded": "cooldown", "cooldown_ms": 50000}},
        },
        "ladders": {
            "soft": [{"do": "@recreate"}],
            "po": [{"do": "restart_process"}],
        },
        "processes": {
            "unified": _proc({
                "udp": _svc(L1("@soft")),
                "cfg": {**_svc({"L1_pulse_lost": {"mode": "act", "dead_after_ms": 3000,
                                                  "ladder": "@soft"},
                                "L2_activity_frozen": {"mode": "act", "frozen_after_ms": 2000,
                                                       "ladder": "@soft"}}, activity=100)},
            }),
            "vision": _proc(
                {"cam1": _svc(L1("@soft")), "cam2": _svc(L1("@soft"))},
                watch={"P1_all_pulses_lost": {"mode": "act", "ladder": "@po"}}),
        },
    }


def main() -> int:
    cfg = cfgmod.parse(_config())
    execs = {name: InMemoryActionExecutor(name) for name in cfg.actions}
    clock = FakeClock(0)
    ctl = InMemoryController()
    log = ListLogger()
    hm = runtime.build_health_machine(cfg, execs, clock=clock, controller=ctl,
                                      gate=InMemoryGate(True), logger=log)
    alive = {"udp", "cfg", "cam1", "cam2"}

    def keepalive(names):
        for n in names:
            hm.on_signal(Signal(n))

    def step(t, tick=True, feed=None):
        clock.set(t)
        keepalive(feed if feed is not None else alive)
        if tick:
            hm.tick()

    # ── фаза 1: L1 udp замолчал → recreate → выздоровление ──
    step(1000)
    step(2000, feed=alive - {"udp"})     # udp молчит с 1000
    clock.set(5000); keepalive(alive - {"udp"}); hm.tick()
    assert execs["recreate"].calls == ["udp"], execs["recreate"].calls
    print("phase1: L1 silence -> recreate(udp) OK")
    clock.set(8000); keepalive(alive); hm.tick()     # udp пульсирует снова
    assert log.has("recovered")
    print("phase1: pulse back -> recovered OK")

    # ── фаза 2: L2 счётчик cfg замер при active → действие ──
    clock.set(9000)
    for n in alive:
        hm.on_signal(Signal(n))
    hm.on_signal(Signal("cfg", "active", 1))         # открыть эпизод
    clock.set(10000)
    for n in alive:
        hm.on_signal(Signal(n))
    hm.on_signal(Signal("cfg", "active", 1))         # тот же счётчик (замер)
    calls_before = list(execs["recreate"].calls)
    clock.set(12500); keepalive(alive); hm.on_signal(Signal("cfg", "active", 1)); hm.tick()
    assert execs["recreate"].calls == calls_before + ["cfg"], execs["recreate"].calls
    print("phase2: L2 frozen counter -> action(cfg) OK")

    # ── фаза 3: потребитель C — vision молчит целиком → рестарт vision, unified цел ──
    base_restarts = list(ctl.restarts)
    for t in (14000, 15000):
        clock.set(t); keepalive({"udp", "cfg"}); hm.tick()    # только unified жив
    clock.set(19000); keepalive({"udp", "cfg"}); hm.tick()    # cam1/cam2 молчат > dead
    assert ctl.restarts == base_restarts + ["vision"], ctl.restarts
    assert "unified" not in ctl.restarts, ctl.restarts
    print("phase3: vision group silent -> restart(vision), unified intact OK")

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
