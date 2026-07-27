"""core.HealthMachine + Supervisor на FakeClock (БЕЗ реального сна, FR-22).

Каждый сценарий строит минимальный конфиг → runtime собирает планы → гоняем машину
на управляемом времени. Действия — inmemory (записывают цель), не трогают ОС.
"""
import copy

from svc_watch import config as cfgmod
from svc_watch import runtime
from svc_watch.adapters.inmemory import (FakeClock, InMemoryActionExecutor,
                                         InMemoryController, InMemoryGate, ListLogger)
from svc_watch.contracts import Signal


# ─── общий конструктор ───
def _base_dict():
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
            "esc": [{"do": "@recreate", "tries": 2}, {"do": "restart_process"}],
            "po": [{"do": "restart_process"}],
        },
        "processes": {
            "unified": {
                "launch": {"type": "inmemory",
                           "grace": {"type": "fixed", "ms": 3000},
                           "restart_rate_limit": {"max": 5, "per_ms": 600000,
                                                  "on_exceeded": "cooldown",
                                                  "cooldown_ms": 300000}},
                "supervisor": {"poll_ms": 1000, "stop_timeout_ms": 20000,
                               "min_stable_ms": 5000,
                               "max_consecutive_start_failures": 3,
                               "backoff": {"start_ms": 1000, "factor": 2, "cap_ms": 10000},
                               "log": {"file": "/mnt/sda1/u.log", "rotate_kb": 1024,
                                       "keep": 7, "fallbacks": []}},
                "watch": {},
                "services": {},
            }
        },
    }


def _one_service(watch, *, activity=None):
    svc = {
        "start": {"type": "inmemory"},
        "signals": {"pulse": {"from": "loop", "every_ms": 1000}},
        "watch": watch,
    }
    if activity is not None:
        svc["signals"]["activity"] = {"tick_ms": activity}
    return svc


def _build(raw):
    cfg = cfgmod.parse(raw)
    execs = {name: InMemoryActionExecutor(name) for name in cfg.actions}
    clock = FakeClock(0)
    ctl = InMemoryController()
    gate = InMemoryGate(True)
    log = ListLogger()
    hm = runtime.build_health_machine(cfg, execs, clock=clock, controller=ctl,
                                      gate=gate, logger=log)
    return cfg, hm, execs, clock, ctl, log


# ═══ L1: тишина → действие → выздоровление ═══
def test_l1_silence_action_then_recovery():
    raw = _base_dict()
    raw["processes"]["unified"]["services"]["solo"] = _one_service(
        {"L1_pulse_lost": {"mode": "act", "dead_after_ms": 3000, "ladder": "@soft"}})
    cfg, hm, execs, clock, ctl, log = _build(raw)

    clock.set(1000); hm.on_signal(Signal("solo"))
    clock.set(2000); hm.tick()                       # жив
    assert execs["recreate"].calls == []
    clock.set(5000); hm.tick()                       # молчит 4000 > 3000 → действие
    assert execs["recreate"].calls == ["solo"]
    assert log.has("action")

    clock.set(8000); hm.on_signal(Signal("solo")); hm.tick()   # пульс вернулся
    assert log.has("recovered")
    assert execs["recreate"].calls == ["solo"]       # больше не дёргали


# ═══ L1: эскалация в restart_process после tries ═══
def test_l1_escalates_to_restart_process():
    raw = _base_dict()
    raw["processes"]["unified"]["services"]["solo"] = _one_service(
        {"L1_pulse_lost": {"mode": "act", "dead_after_ms": 3000, "ladder": "@esc"}})
    cfg, hm, execs, clock, ctl, log = _build(raw)

    clock.set(1000); hm.on_signal(Signal("solo"))
    clock.set(5000); hm.tick()                       # recreate #1 (tries 1/2), suppress→7000
    clock.set(7000); hm.tick()                       # recreate #2 (2/2) → шаг1, suppress→9000
    assert execs["recreate"].calls == ["solo", "solo"]
    clock.set(9000); hm.tick()                       # шаг1 restart_process
    assert ctl.restarts == ["unified"]
    assert log.has("restart_process")


# ═══ rate_limit: >max за окно → cooldown, не долбёж ═══
def test_rate_limit_cooldown():
    raw = _base_dict()
    raw["actions"]["recreate"]["startup_ms"] = 1000
    raw["actions"]["recreate"]["rate_limit"] = {"max": 2, "per_ms": 100000,
                                                "on_exceeded": "cooldown",
                                                "cooldown_ms": 60000}
    raw["processes"]["unified"]["services"]["solo"] = _one_service(
        {"L1_pulse_lost": {"mode": "act", "dead_after_ms": 3000, "ladder": "@soft"}})
    cfg, hm, execs, clock, ctl, log = _build(raw)

    clock.set(1000); hm.on_signal(Signal("solo"))
    clock.set(5000); hm.tick()                       # #1
    clock.set(6000); hm.tick()                       # #2
    assert execs["recreate"].calls == ["solo", "solo"]
    clock.set(7000); hm.tick()                       # #3 заблокирован → cooldown
    assert execs["recreate"].calls == ["solo", "solo"]
    assert log.has("rate_limit_cooldown")


# ═══ L2: счётчик замер при active → действие, затем прогресс → выздоровление ═══
def test_l2_frozen_then_progress_recovers():
    raw = _base_dict()
    raw["processes"]["unified"]["services"]["solo"] = _one_service(
        {"L1_pulse_lost": {"mode": "act", "dead_after_ms": 3000, "ladder": "@soft"},
         "L2_activity_frozen": {"mode": "act", "frozen_after_ms": 2000, "ladder": "@soft"}},
        activity=100)
    cfg, hm, execs, clock, ctl, log = _build(raw)

    clock.set(1000); hm.on_signal(Signal("solo", "active", 1))
    clock.set(2000); hm.on_signal(Signal("solo", "active", 1))   # пульс жив, счётчик тот же
    clock.set(3500); hm.tick()                       # L1 ок (жив 1500), L2 замер 2500>2000 → действие
    assert execs["recreate"].calls == ["solo"]

    # прогресс счётчика → выздоровление L2 (после suppress-окна 2000)
    clock.set(6000); hm.on_signal(Signal("solo", "active", 2)); hm.tick()
    assert log.has("recovered")


# ═══ P1: два сервиса молчат → рестарт процесса (не L1 каждого) ═══
def test_p1_all_silent_restarts_process():
    raw = _base_dict()
    raw["processes"]["unified"]["watch"] = {
        "P1_all_pulses_lost": {"mode": "act", "ladder": "@po"}}
    for n in ("a", "b"):
        raw["processes"]["unified"]["services"][n] = _one_service(
            {"L1_pulse_lost": {"mode": "act", "dead_after_ms": 3000, "ladder": "@soft"}})
    cfg, hm, execs, clock, ctl, log = _build(raw)

    clock.set(1000); hm.on_signal(Signal("a")); hm.on_signal(Signal("b"))
    clock.set(5000); hm.tick()                       # оба молчат 4000>3000 → P1
    assert ctl.restarts == ["unified"]
    assert execs["recreate"].calls == []             # L1-действия НЕ сработали (P1 раньше)


# ═══ Supervisor: min_stable + сдача после N провалов ═══
def test_supervisor_min_stable_and_give_up():
    raw = _base_dict()
    raw["processes"]["unified"]["services"]["solo"] = _one_service(
        {"L1_pulse_lost": {"mode": "act", "dead_after_ms": 3000, "ladder": "@soft"}})
    cfg = cfgmod.parse(raw)
    from svc_watch.adapters.inmemory import InMemoryStartMechanism
    clock = FakeClock(0); log = ListLogger()
    sm = InMemoryStartMechanism()
    sup = runtime.build_supervisor(cfg, "unified", sm, clock=clock, logger=log)
    svc = cfg.processes["unified"].services["solo"]

    # 3 провала подряд (смерть раньше min_stable=5000) → сдача
    for i in range(3):
        clock.set(clock.now_ms() + 20000)     # промотать backoff
        assert sup.start(svc) is True
        clock.set(clock.now_ms() + 1000)      # прожил 1000 < 5000 → провал
        sup.note_exit("solo")
    assert sup.gave_up("solo") is True
    assert log.has("supervisor_give_up")
    assert sup.start(svc) is False            # больше не пересоздаёт


def test_supervisor_stable_resets_failures():
    raw = _base_dict()
    raw["processes"]["unified"]["services"]["solo"] = _one_service(
        {"L1_pulse_lost": {"mode": "act", "dead_after_ms": 3000, "ladder": "@soft"}})
    cfg = cfgmod.parse(raw)
    from svc_watch.adapters.inmemory import InMemoryStartMechanism
    clock = FakeClock(0); log = ListLogger()
    sm = InMemoryStartMechanism()
    sup = runtime.build_supervisor(cfg, "unified", sm, clock=clock, logger=log)
    svc = cfg.processes["unified"].services["solo"]

    assert sup.start(svc) is True
    clock.set(10000)                          # прожил 10000 > min_stable → стабилен
    sup.note_exit("solo")
    assert sup.gave_up("solo") is False
    assert log.has("service_exited_stable")
