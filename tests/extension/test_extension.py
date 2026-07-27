"""Э3-ворот: новый тип добавляется ТОЛЬКО новым файлом-адаптером + строкой конфига;
core.py и runtime.py при этом не изменяются (FR-40).

Проверяем реально:
  1. до импорта адаптера конфиг с типом raise_alert НЕ валиден (тип не зашит в config.py);
  2. после импорта (адаптер само-регистрируется) — валиден, собирается и РАБОТАЕТ в core;
  3. в исходниках core.py/runtime.py нет ни следа нового типа (расширение аддитивно).
"""
import os

import pytest

from svc_watch import config as cfgmod
from svc_watch.config import WdConfigError

# tests/extension/test_extension.py → tests → repo root; svc_watch source lives under src/svc_watch
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LIB = os.path.join(_ROOT, "src", "svc_watch")


def _cfg_with_alert():
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
            "alert": {"type": "raise_alert",
                      "rate_limit": {"max": 3, "per_ms": 100000,
                                     "on_exceeded": "cooldown", "cooldown_ms": 50000}},
        },
        "ladders": {"just_alert": [{"do": "@alert"}]},
        "processes": {
            "unified": {
                "launch": {"type": "inmemory", "grace": {"type": "fixed", "ms": 3000},
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
                "services": {
                    "solo": {"start": {"type": "inmemory"},
                             "signals": {"pulse": {"from": "loop", "every_ms": 1000}},
                             "watch": {"L1_pulse_lost": {"mode": "act",
                                                         "dead_after_ms": 3000,
                                                         "ladder": "@just_alert"}}},
                },
            }
        },
    }


def test_unknown_type_rejected_before_extension():
    # config.py НЕ знает про raise_alert, пока адаптер не импортирован
    if "raise_alert" in cfgmod.ACTION_TYPES:
        pytest.skip("тип уже зарегистрирован (адаптер импортирован ранее в сессии)")
    with pytest.raises(WdConfigError) as ei:
        cfgmod.parse(_cfg_with_alert())
    assert any("type" in p.path for p in ei.value.problems)


def test_extension_works_after_importing_adapter_file():
    # импорт НОВОГО файла-адаптера → само-регистрация типа и сборщика
    from svc_watch.adapters import action_raise_alert  # noqa: F401
    from svc_watch import runtime
    from svc_watch.adapters.inmemory import (FakeClock, InMemoryController,
                                             InMemoryGate, ListLogger)
    from svc_watch.contracts import Signal

    cfg = cfgmod.parse(_cfg_with_alert())               # теперь валиден
    execs = runtime.build_real_action_executors(cfg)    # собрался по реестру
    assert type(execs["alert"]).__name__ == "RaiseAlertExecutor"

    clock = FakeClock(0)
    hm = runtime.build_health_machine(cfg, execs, clock=clock,
                                      controller=InMemoryController(),
                                      gate=InMemoryGate(True), logger=ListLogger())
    clock.set(1000); hm.on_signal(Signal("solo"))
    clock.set(5000); hm.tick()                          # молчит → сработал НОВЫЙ action
    assert execs["alert"].alerts == ["solo"]


def test_core_and_runtime_untouched_by_extension():
    # расширение аддитивно: имени нового типа НЕТ в исходниках core/runtime
    with open(os.path.join(_LIB, "core.py"), encoding="utf-8") as f:
        core_src = f.read()
    with open(os.path.join(_LIB, "runtime.py"), encoding="utf-8") as f:
        rt_src = f.read()
    assert "raise_alert" not in core_src
    assert "raise_alert" not in rt_src
    # core вообще не знает адаптерных типов
    for t in ("request_file", "unix_datagram", "tcp", "python", "init_script"):
        assert t not in core_src
