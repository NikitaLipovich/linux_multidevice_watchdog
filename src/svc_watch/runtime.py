"""svc_watch.runtime — composition root (E2).

A validated model (config.Config) → assembled objects. Here (and ONLY here)
adapters are matched to `type` via the registry — core knows nothing about types.
Ladder steps are pre-resolved into StepPlan(executor|verb), so core calls
.execute()/.restart() rather than branching on the adapter type (FR-41).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from . import config as cfgmod
from . import core
from .contracts import (ActionExecutor, Clock, Logger, ProcessController,
                        ResourceGate, Transport)


# ─── translating cfg structures → core plans ───
def _rate(rl: cfgmod.RateLimit) -> core.RateLimitPlan:
    return core.RateLimitPlan(max=rl.max, per_ms=rl.per_ms,
                              on_exceeded=rl.on_exceeded, cooldown_ms=rl.cooldown_ms)


def _ladder_steps(cfg: cfgmod.Config, level: cfgmod.WatchLevel,
                  executors: Dict[str, ActionExecutor],
                  restart_limit: core.RateLimitPlan):
    steps_src = (level.inline.steps if level.inline is not None
                 else cfg.ladders[level.ladder].steps)
    out = []
    for st in steps_src:
        if st.is_verb:                       # restart_process
            out.append(core.StepPlan(executor=None, tries=st.tries,
                                     rate_limit=restart_limit, suppress_ms=0))
        else:
            action = cfg.actions[st.ref]
            suppress = action.params.get("startup_ms") or 0
            out.append(core.StepPlan(executor=executors[st.ref], tries=st.tries,
                                     rate_limit=_rate(action.rate_limit),
                                     suppress_ms=int(suppress)))
    return out


def _level_plan(cfg: cfgmod.Config, name: str, level: cfgmod.WatchLevel,
                executors: Dict[str, ActionExecutor],
                restart_limit: core.RateLimitPlan) -> core.LevelPlan:
    threshold = 0
    if name == core.L1:
        threshold = int(level.params.get("dead_after_ms") or 0)
    elif name == core.L2:
        threshold = int(level.params.get("frozen_after_ms") or 0)
    return core.LevelPlan(name=name, mode=level.mode, threshold_ms=threshold,
                          steps=_ladder_steps(cfg, level, executors, restart_limit))


def build_processes(cfg: cfgmod.Config,
                    executors: Dict[str, ActionExecutor]) -> Dict[str, core.ProcessPlan]:
    """Model + ready executors keyed by action name → process plans for HealthMachine."""
    procs: Dict[str, core.ProcessPlan] = {}
    for pname, p in cfg.processes.items():
        restart_limit = _rate(p.launch.restart_rate_limit)
        grace_ms = int(p.launch.grace.ms or p.launch.grace.max_ms or 0)
        services: Dict[str, core.ServicePlan] = {}
        for sname, s in p.services.items():
            levels = {ln: _level_plan(cfg, ln, lv, executors, restart_limit)
                      for ln, lv in s.watch.items()}
            services[sname] = core.ServicePlan(
                name=sname, every_ms=s.pulse.every_ms, levels=levels,
                activity_tick_ms=(s.activity.tick_ms if s.activity else None))
        plevels = {ln: _level_plan(cfg, ln, lv, executors, restart_limit)
                   for ln, lv in p.watch.items()}
        procs[pname] = core.ProcessPlan(
            name=pname, grace_ms=grace_ms, restart_limit=restart_limit,
            services=services, plevels=plevels)
    return procs


def build_health_machine(
    cfg: cfgmod.Config,
    executors: Dict[str, ActionExecutor],
    *,
    clock: Clock,
    controller: ProcessController,
    gate: ResourceGate,
    logger: Logger,
    paused: Optional[Callable[[], bool]] = None,
    process_age_ms: Optional[Callable[[str], Optional[int]]] = None,
    request_stuck: Optional[Callable[[str], Any]] = None,
) -> core.HealthMachine:
    procs = build_processes(cfg, executors)
    pc = cfg.framework.pacing
    if pc.type == "fixed":
        pacing = core.PacingPlan(fixed=True, delay_ms=int(pc.params.get("delay_ms") or 0))
    else:
        pacing = core.PacingPlan(fixed=False, start_ms=int(pc.params.get("start_ms") or 0),
                                 factor=float(pc.params.get("factor") or 2.0),
                                 cap_ms=int(pc.params.get("cap_ms") or 0))
    gate_plan = core.GatePlan(gate=gate, force_after_ms=cfg.framework.gates.force_after_ms)
    return core.HealthMachine(
        procs, clock=clock, controller=controller, gate=gate_plan,
        pacing=pacing, logger=logger, paused=paused,
        process_age_ms=process_age_ms, request_stuck=request_stuck)


# ─── REGISTRY of action builders: adapters self-register on import (FR-41) ───
# A new action type = a new FILE adapter that calls register_action_builder — runtime.py
# is NOT edited for this (FR-40; verified by the E3 extension test).
_ACTION_BUILDERS: Dict[str, Callable[[cfgmod.Action], ActionExecutor]] = {}


def register_action_builder(type_name: str,
                            builder: Callable[[cfgmod.Action], ActionExecutor]) -> None:
    _ACTION_BUILDERS[type_name] = builder


def build_real_action_executors(cfg: cfgmod.Config) -> Dict[str, ActionExecutor]:
    """cfg.actions → ready executors via the registry (no switch in core/runtime)."""
    import svc_watch.adapters.action_request_file  # noqa: F401 (self-registration of the base type)
    out: Dict[str, ActionExecutor] = {}
    for name, action in cfg.actions.items():
        b = _ACTION_BUILDERS.get(action.type)
        if b is None:
            raise ValueError("no registered adapter for action.type=%r "
                             "(is its adapter file imported?)" % action.type)
        out[name] = b(action)
    return out


# ─── PROCESS SIDE (consumer A): transport, probe, async supervision ───
def build_transport(cfg: cfgmod.Config) -> Transport:
    """transport type → emission adapter (production RUT: unix_datagram)."""
    tr = cfg.framework.transport
    if tr.type == "unix_datagram":
        from .adapters.transport_unix_datagram import UnixDatagramTransport
        return UnixDatagramTransport(tr.params["socket"])
    if tr.type == "inmemory":
        from .adapters.inmemory import InMemoryTransport, MemoryBus
        return InMemoryTransport(MemoryBus())
    raise ValueError("no transport for type=%r" % tr.type)


def build_pulse_probe(service: cfgmod.Service, *, host: str = "127.0.0.1"
                      ) -> Optional[Callable[[], Awaitable[bool]]]:
    """For pulse from:probe builds an async probe (tcp). from:loop → None."""
    if service.pulse.source != "probe" or service.pulse.probe is None:
        return None
    from . import emit
    pr = service.pulse.probe
    if pr.type == "tcp":
        port = service.port if pr.params.get("port") == "@port" else pr.params.get("port")
        return emit.tcp_probe(host, int(port), (pr.timeout_ms or 2000) / 1000.0)
    raise ValueError("no probe for type=%r" % pr.type)


class _CrashRequested(Exception):
    pass


async def run_service(sup: core.Supervisor, name: str, factory: Callable[[], Awaitable[Any]],
                      teardown: Callable[[str, Any], Awaitable[None]], *,
                      crash_path: str, stop_event: asyncio.Event,
                      poll_s: float, stop_timeout_s: float) -> None:
    """Restart loop for a single service (consumer A). The min_stable/backoff/
    give-up bookkeeping is kept by core.Supervisor (FR-32/37); create/teardown are
    the owner's async factories. Restart is triggered by a crash file from the
    observer (request_file). Isolation: a crash of ONE service restarts only it."""
    async def _bounded_teardown(resource):
        if resource is None:
            return
        try:
            await asyncio.wait_for(teardown(name, resource), timeout=stop_timeout_s)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    cp = Path(crash_path)
    while not stop_event.is_set():
        if not sup.should_start(name):
            if sup.gave_up(name):
                return                       # gave up → stay silent → observer's L1 escalates
            await asyncio.sleep(poll_s)
            continue
        resource = None
        try:
            resource = await factory()
            sup.note_started(name)
            while not stop_event.is_set():
                if cp.exists():
                    try:
                        cp.unlink()
                    except OSError:
                        pass
                    raise _CrashRequested()
                await asyncio.sleep(poll_s)
            await _bounded_teardown(resource)
            return
        except asyncio.CancelledError:
            await _bounded_teardown(resource)
            raise
        except Exception:
            await _bounded_teardown(resource)
            sup.note_exit(name)              # min_stable/backoff/give-up


def build_supervisor(cfg: cfgmod.Config, process_name: str, start_mech, *,
                     clock: Clock, logger: Logger) -> core.Supervisor:
    p = cfg.processes[process_name]
    sup = p.supervisor
    return core.Supervisor(
        start_mech, clock=clock, logger=logger,
        min_stable_ms=sup.min_stable_ms,
        max_consecutive_start_failures=sup.max_consecutive_start_failures,
        backoff=core.BackoffPlan(start_ms=sup.backoff.start_ms,
                                 factor=sup.backoff.factor, cap_ms=sup.backoff.cap_ms),
        stop_timeout_ms=sup.stop_timeout_ms)
