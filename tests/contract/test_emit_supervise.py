"""Сторона процесса (потребитель A) локально, БЕЗ стенда/флеша: activity-троттлинг,
пульс с пробой, async run_service до сдачи (min_stable/give-up)."""
import asyncio

from svc_watch import config as cfgmod
from svc_watch import emit, runtime
from svc_watch.adapters.inmemory import (FakeClock, InMemoryStartMechanism,
                                         InMemoryTransport, ListLogger, MemoryBus)


class _ClockS:
    """Секундные часы поверх FakeClock (ms) — для ActivityEmitter."""
    def __init__(self, fake): self._f = fake
    def __call__(self): return self._f.now_ms() / 1000.0


def test_activity_emitter_throttles():
    bus = MemoryBus(); tx = InMemoryTransport(bus)
    clk = FakeClock(0)
    ae = emit.ActivityEmitter("cfg", tx, tick_s=2.0, clock=_ClockS(clk))
    ae.tick()                       # первый — эмитит
    ae.tick()                       # сразу — троттл
    clk.advance(2100)               # >2с
    ae.tick()                       # снова эмитит
    ae.idle()                       # закрывает эпизод
    sigs = bus.drain_signals()
    active = [s for s in sigs if s.state == "active"]
    idle = [s for s in sigs if s.state == "idle"]
    assert len(active) == 2, [str(s) for s in sigs]
    assert len(idle) == 1
    assert active[-1].counter == 3   # счётчик рос на каждый tick


def test_pulse_loop_probe_gates_emit():
    async def scenario():
        bus = MemoryBus(); tx = InMemoryTransport(bus)
        gate = {"ok": True}
        async def probe(): return gate["ok"]
        task = asyncio.ensure_future(emit.pulse_loop("svc", tx, 0.01, probe=probe))
        await asyncio.sleep(0.05)
        gate["ok"] = False           # проба падает → пульс НЕ шлём
        await asyncio.sleep(0.05)
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        return bus.drain_signals()
    sigs = asyncio.run(scenario())
    assert len(sigs) >= 1            # были пульсы, пока проба True
    assert all(s.service == "svc" for s in sigs)


def _cfg():
    return cfgmod.parse({
        "schema": 2,
        "framework": {
            "transport": {"type": "inmemory"},
            "observer": {"mode": "act", "tick_ms": 1000, "pause_file": "/tmp/p", "oom_adj": 0,
                         "log": {"file": "/mnt/sda1/wd.log", "rotate_kb": 1024, "keep": 7, "fsync": True},
                         "quiet": {"paused_ms": 60000, "unknown_ms": 60000}},
            "gates": {"min_free_mb": 8, "max_load1": 3.0, "recheck_ms": 5000,
                      "force_after_ms": 300000, "alarm_mb": 5},
            "pacing": {"type": "fixed", "delay_ms": 1000}},
        "actions": {"recreate": {"type": "request_file", "file": "/tmp/svc_crash_{service}",
                                 "eat_within_ms": 10000, "startup_ms": 60000,
                                 "rate_limit": {"max": 6, "per_ms": 600000,
                                                "on_exceeded": "cooldown", "cooldown_ms": 300000}}},
        "ladders": {"soft": [{"do": "@recreate"}]},
        "processes": {"unified": {
            "launch": {"type": "inmemory", "grace": {"type": "fixed", "ms": 3000},
                       "restart_rate_limit": {"max": 5, "per_ms": 600000,
                                              "on_exceeded": "cooldown", "cooldown_ms": 300000}},
            "supervisor": {"poll_ms": 1000, "stop_timeout_ms": 20000, "min_stable_ms": 5000,
                           "max_consecutive_start_failures": 3,
                           "backoff": {"start_ms": 1, "factor": 2, "cap_ms": 5},
                           "log": {"file": "/mnt/sda1/u.log", "rotate_kb": 1024, "keep": 7, "fallbacks": []}},
            "watch": {},
            "services": {"solo": {"start": {"type": "inmemory"},
                                  "signals": {"pulse": {"from": "loop", "every_ms": 1000}},
                                  "watch": {"L1_pulse_lost": {"mode": "act", "dead_after_ms": 15000,
                                                              "ladder": "@soft"}}}}}}})


def test_run_service_gives_up_after_failures():
    async def scenario():
        cfg = _cfg()
        from svc_watch.adapters.clock_monotonic import MonotonicClock
        sup = runtime.build_supervisor(cfg, "unified", InMemoryStartMechanism(),
                                       clock=MonotonicClock(), logger=ListLogger())
        stop = asyncio.Event()
        async def factory(): raise RuntimeError("boom")   # умирает мгновенно < min_stable
        async def teardown(name, res): pass
        await asyncio.wait_for(
            runtime.run_service(sup, "solo", factory, teardown,
                                crash_path="/tmp/nope_solo", stop_event=stop,
                                poll_s=0.005, stop_timeout_s=0.1),
            timeout=3.0)
        return sup
    sup = asyncio.run(scenario())
    assert sup.gave_up("solo") is True     # 3 провала подряд → сдался (FR-37)
