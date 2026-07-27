"""Э1-ворот: по кейсу на каждое нарушение правил 1–17.

Каждый тест мутирует валидную базу в ОДНУ сторону и требует:
  - WdConfigError (fail-closed),
  - среди problems есть один с ожидаемым путём ключа и/или текстом.
Так проверяется и сам отказ, и что ошибка ВНЯТНАЯ (указывает на место).
"""
import pytest

from conftest import base
from svc_watch import config
from svc_watch.config import WdConfigError


# ─── помощники ───
def expect_fail(raw, *, path=None, msg=None):
    """Требует WdConfigError; возвращает список problems для доп-проверок."""
    with pytest.raises(WdConfigError) as ei:
        config.parse(raw)
    probs = ei.value.problems
    if path is not None:
        assert any(path in p.path for p in probs), \
            "нет problem с путём %r; получено: %s" % (path, [p.path for p in probs])
    if msg is not None:
        assert any(msg in p.message for p in probs), \
            "нет problem с текстом %r; получено: %s" % (msg, [p.message for p in probs])
    return probs


def _udp(raw):
    return raw["processes"]["unified"]["services"]["udp_logger"]


def _cfgsrv(raw):
    return raw["processes"]["unified"]["services"]["config_server"]


# ═══ базовая линия: валидный конфиг проходит ═══
def test_base_is_valid():
    cfg = config.parse(base())
    assert cfg.schema == 2
    assert "unified" in cfg.processes
    assert cfg.warnings == []


# ═══ правило 1: пропуск обязательного ключа ═══
def test_rule1_missing_key():
    raw = base()
    del raw["processes"]["unified"]["services"]["udp_logger"]["start"]
    expect_fail(raw, path="udp_logger.start", msg="required key")


def test_rule1_all_missing_reported_together():
    """Fail-closed: несколько пропусков — в ОДНОМ списке, не по одному."""
    raw = base()
    del raw["framework"]["gates"]["min_free_mb"]
    del raw["framework"]["observer"]["tick_ms"]
    probs = expect_fail(raw)
    paths = [p.path for p in probs]
    assert any("gates.min_free_mb" in p for p in paths)
    assert any("observer.tick_ms" in p for p in paths)


# ═══ правило 2: неизвестный type ═══
def test_rule2_unknown_transport_type():
    raw = base()
    raw["framework"]["transport"]["type"] = "carrier_pigeon"
    expect_fail(raw, path="transport.type", msg="not in")


def test_rule2_unknown_probe_type():
    raw = base()
    _cfgsrv(raw)["signals"]["pulse"]["probe"]["type"] = "smoke_signal"
    expect_fail(raw, path="probe.type")


# ═══ правило 3: @-ссылки и глаголы ═══
def test_rule3_dangling_ladder_ref():
    raw = base()
    _udp(raw)["watch"]["L1_pulse_lost"]["ladder"] = "@nonexistent"
    expect_fail(raw, path="L1_pulse_lost.ladder", msg="not found")


def test_rule3_dangling_action_ref():
    raw = base()
    raw["ladders"]["soft_only"] = [{"do": "@ghost_action"}]
    expect_fail(raw, path="ladders.soft_only", msg="not found")


def test_rule3_unknown_verb():
    raw = base()
    raw["ladders"]["process_only"] = [{"do": "reboot_the_universe"}]
    expect_fail(raw, path="ladders.process_only", msg="verb")


# ═══ правило 4: форма пульса ═══
def test_rule4_loop_with_probe():
    raw = base()
    _udp(raw)["signals"]["pulse"]["probe"] = {"type": "tcp", "port": 9, "timeout_ms": 100}
    expect_fail(raw, path="udp_logger.signals.pulse.probe", msg="from:loop")


def test_rule4_probe_without_probe_block():
    raw = base()
    del _cfgsrv(raw)["signals"]["pulse"]["probe"]
    expect_fail(raw, path="config_server.signals.pulse.probe", msg="from:probe requires")


# ═══ правило 5: парность сигнал ↔ уровень ═══
def test_rule5_activity_without_l2():
    raw = base()
    del _cfgsrv(raw)["watch"]["L2_activity_frozen"]
    expect_fail(raw, path="config_server.watch", msg="signals.activity")


def test_rule5_l2_without_activity():
    raw = base()
    del _cfgsrv(raw)["signals"]["activity"]
    expect_fail(raw, path="config_server.signals", msg="L2_activity_frozen")


def test_rule5_pulse_without_l1():
    raw = base()
    del _udp(raw)["watch"]["L1_pulse_lost"]
    expect_fail(raw, path="udp_logger.watch", msg="L1_pulse_lost")


# ═══ правило 6: числовые связки ═══
def test_rule6_dead_lt_3x_every():
    raw = base()
    _udp(raw)["watch"]["L1_pulse_lost"]["dead_after_ms"] = 5000   # every=5000 → нужно ≥15000
    expect_fail(raw, path="udp_logger.watch.L1_pulse_lost.dead_after_ms", msg="3×every")


def test_rule6_frozen_lt_20x_tick():
    raw = base()
    _cfgsrv(raw)["watch"]["L2_activity_frozen"]["frozen_after_ms"] = 10000  # tick=2000 → нужно ≥40000
    expect_fail(raw, path="L2_activity_frozen.frozen_after_ms", msg="20×tick")


def test_rule6_probe_timeout_ge_every():
    raw = base()
    _cfgsrv(raw)["signals"]["pulse"]["probe"]["timeout_ms"] = 5000  # every=5000
    expect_fail(raw, path="probe.timeout_ms", msg="< every_ms")


def test_rule6_min_free_le_alarm():
    raw = base()
    raw["framework"]["gates"]["min_free_mb"] = 5    # alarm_mb=5
    expect_fail(raw, path="gates.min_free_mb", msg="> alarm_mb")


def test_rule6_cooldown_le_ratio():
    raw = base()
    raw["actions"]["recreate"]["rate_limit"]["cooldown_ms"] = 1000  # per/max=100000
    expect_fail(raw, path="recreate.rate_limit.cooldown_ms", msg="per_ms/max")


def test_rule6_inert_rate_limit_unreachable():
    """per_ms/max < startup+min(dead) → лимит инертен."""
    raw = base()
    # startup 60000 + min dead 15000 = 75000; сделаем per/max = 10000
    raw["actions"]["recreate"]["rate_limit"]["per_ms"] = 60000
    raw["actions"]["recreate"]["rate_limit"]["max"] = 6            # 10000 < 75000
    expect_fail(raw, path="recreate.rate_limit", msg="inert")


def test_rule6_poll_gt_eat_over_5():
    raw = base()
    raw["processes"]["unified"]["supervisor"]["poll_ms"] = 5000   # eat=10000 → ≤2000
    expect_fail(raw, path="supervisor.poll_ms", msg="poll_ms")


# ═══ правило 7: глобальная уникальность имён сервисов ═══
def test_rule7_duplicate_service_name_across_processes():
    raw = base()
    # добавить второй процесс со своим сервисом того же имени
    vision = {
        "launch": {
            "type": "init_script", "script": "/etc/init.d/vision",
            "pidfile": "/tmp/vision.pid",
            "grace": {"type": "fixed", "ms": 90000},
            "restart_rate_limit": {"max": 5, "per_ms": 600000,
                                   "on_exceeded": "cooldown", "cooldown_ms": 300000},
        },
        "supervisor": raw["processes"]["unified"]["supervisor"],
        "watch": {"P2_request_stuck": {"mode": "act", "ladder": "@process_only"}},
        "services": {
            "udp_logger": {          # ← тот же name, что в unified
                "start": {"type": "python", "entry": "run_services:start_udp_logger"},
                "signals": {"pulse": {"from": "loop", "every_ms": 5000}},
                "watch": {"L1_pulse_lost": {"mode": "act", "dead_after_ms": 15000,
                                            "ladder": "@soft_only"}},
            }
        },
    }
    raw["processes"]["vision"] = vision
    expect_fail(raw, msg="globally unique")


# ═══ правило 8: неизвестный ключ (опечатка) ═══
def test_rule8_typo_key():
    raw = base()
    raw["ladders"]["escalate_std"][0]["trys"] = 3     # опечатка tries
    expect_fail(raw, path="trys", msg="unknown key")


def test_rule8_typo_top_level():
    raw = base()
    raw["framwork"] = {}                              # опечатка framework
    expect_fail(raw, path="framwork", msg="unknown key")


# ═══ правило 9: вырожденный 0 запрещён ═══
def test_rule9_zero_quiet():
    raw = base()
    raw["framework"]["observer"]["quiet"]["unknown_ms"] = 0
    expect_fail(raw, path="quiet.unknown_ms", msg="≥ 1")


# ═══ правило 10: уникальность шаблонов file ═══
def test_rule10_duplicate_file_template():
    raw = base()
    raw["actions"]["recreate2"] = {
        "type": "request_file",
        "file": "/tmp/svc_crash_{service}",           # ← совпадает с recreate
        "eat_within_ms": 10000, "startup_ms": 60000,
        "rate_limit": {"max": 6, "per_ms": 600000,
                       "on_exceeded": "cooldown", "cooldown_ms": 300000},
    }
    expect_fail(raw, path="file", msg="matches")


# ═══ правило 11: одно-сервисный процесс с P1 → WARNING (не ошибка) ═══
def test_rule11_single_service_p1_is_warning_not_error():
    raw = base()
    svcs = raw["processes"]["unified"]["services"]
    for name in ("data_uploader", "config_server", "ws_bridge"):
        del svcs[name]
    cfg = config.parse(raw)      # НЕ падает
    assert any("P1_all_pulses_lost" in w.path for w in cfg.warnings), \
        "ожидалось предупреждение об инертном P1"


# ═══ правило 12: on_exceeded и запрет stop на процесс-рестарте ═══
def test_rule12_stop_forbidden_on_process_restart():
    raw = base()
    raw["processes"]["unified"]["launch"]["restart_rate_limit"]["on_exceeded"] = "stop"
    expect_fail(raw, path="restart_rate_limit.on_exceeded", msg="stop is FORBIDDEN")


def test_rule12_bad_on_exceeded():
    raw = base()
    raw["actions"]["recreate"]["rate_limit"]["on_exceeded"] = "explode"
    expect_fail(raw, path="on_exceeded", msg="not in")


def test_rule12_action_missing_rate_limit():
    raw = base()
    del raw["actions"]["recreate"]["rate_limit"]
    expect_fail(raw, path="recreate.rate_limit", msg="required key")


# ═══ правило 13: grace ═══
def test_rule13_until_ready_without_ready_signal():
    raw = base()
    raw["processes"]["unified"]["launch"]["grace"] = {"type": "until_ready", "max_ms": 90000}
    expect_fail(raw, path="grace.type", msg="ready signal")


def test_rule13_fixed_missing_ms():
    raw = base()
    raw["processes"]["unified"]["launch"]["grace"] = {"type": "fixed"}
    expect_fail(raw, path="grace.ms", msg="required key")


# ═══ правило 14: типизированный pacing (мёртвый блок) ═══
def test_rule14_pacing_foreign_param():
    raw = base()
    raw["framework"]["pacing"] = {"type": "fixed", "delay_ms": 5000, "start_ms": 2000}
    expect_fail(raw, path="pacing.start_ms", msg="unknown key")


def test_rule14_pacing_bad_type():
    raw = base()
    raw["framework"]["pacing"] = {"type": "instant"}
    expect_fail(raw, path="pacing.type", msg="not in")


# ═══ правило 15: лестница tries ═══
def test_rule15_tries_zero():
    raw = base()
    raw["ladders"]["escalate_std"][0]["tries"] = 0
    expect_fail(raw, path="escalate_std", msg="≥ 1")


def test_rule15_nonlast_step_without_tries():
    raw = base()
    # два шага, первый без tries → следующий недостижим
    raw["ladders"]["escalate_std"] = [{"do": "@recreate"}, {"do": "restart_process"}]
    expect_fail(raw, path="escalate_std", msg="unreachable")


# ═══ правило 16: закрытое множество watch-уровней ═══
def test_rule16_unknown_service_level():
    raw = base()
    _udp(raw)["watch"]["L9_vibes_off"] = {"mode": "act", "ladder": "@soft_only"}
    expect_fail(raw, path="L9_vibes_off", msg="unknown watch level")


def test_rule16_process_level_on_service():
    raw = base()
    _udp(raw)["watch"]["P1_all_pulses_lost"] = {"mode": "act", "ladder": "@process_only"}
    expect_fail(raw, path="P1_all_pulses_lost", msg="unknown watch level")


# ═══ правило 17: ёмкости коллекций ═══
def test_rule17_too_many_ladder_steps():
    raw = base()
    steps = [{"do": "@recreate", "tries": 1} for _ in range(config.MAX_LADDER_STEPS)]
    steps.append({"do": "restart_process"})          # +1 сверх ёмкости
    raw["ladders"]["escalate_std"] = steps
    expect_fail(raw, path="escalate_std", msg="MAX_LADDER_STEPS")


def test_rule17_too_many_processes():
    raw = base()
    proto = raw["processes"]["unified"]
    for i in range(config.MAX_PROCESSES):            # +MAX сверх уже имеющегося
        raw["processes"]["extra_%d" % i] = proto
    expect_fail(raw, path="processes", msg="MAX_PROCESSES")


# ═══ schema ═══
def test_schema_unknown():
    raw = base()
    raw["schema"] = 99
    expect_fail(raw, path="schema", msg="schema 2")


# ═══ inline-лестницы упразднены (согласовано с C: только @имя) ═══
def test_inline_ladder_rejected():
    raw = base()
    _udp(raw)["watch"]["L1_pulse_lost"]["ladder"] = [{"do": "@recreate"}]
    expect_fail(raw, path="L1_pulse_lost.ladder", msg="inline")


# ═══ P-уровень: лестница только из глаголов (нет сервисной цели) ═══
def test_process_level_service_action_rejected():
    raw = base()
    raw["processes"]["unified"]["watch"]["P1_all_pulses_lost"]["ladder"] = "@soft_only"
    expect_fail(raw, path="P1_all_pulses_lost.ladder", msg="restart_process")


# ═══ длины строк (ёмкости C) ═══
def test_service_name_too_long():
    raw = base()
    svcs = raw["processes"]["unified"]["services"]
    svcs["x" * 60] = svcs.pop("udp_logger")
    expect_fail(raw, msg="C capacity")
