"""svc_watch.core — the watcher's health machine (Stage 2).

A stable supervision MECHANISM; policy lives outside (the runtime builds the plan
structures from a validated config). core holds ONLY the abstractions from contracts:
Clock (time by injection, FR-22), ActionExecutor, ProcessController, ResourceGate, Logger.

Boundaries (checked by grep in the Stage 2 DoD, FR-42):
  - NO names of concrete services/paths/ports;
  - NO direct time.* — only Clock.now_ms();
  - NO branching on adapter type — a ladder step arrives already resolved
    (an executor object or a verb marker), core calls .execute()/.restart().

The level keys (L1_pulse_lost/L2_activity_frozen/P1_all_pulses_lost/P2_request_stuck)
are CORE semantics (a closed protocol set), not adapter types; branching on them
is legitimate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .contracts import (ActionExecutor, Clock, Logger, ProcessController,
                        ResourceGate, Signal, StartMechanism)

# Level names — core protocol constants (not adapter types).
L1 = "L1_pulse_lost"
L2 = "L2_activity_frozen"
P1 = "P1_all_pulses_lost"
P2 = "P2_request_stuck"


# ════════════════════════════════════════════════════════════════════════════
#  Plans (immutable policy; built by runtime, core only reads)
# ════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class RateLimitPlan:
    max: int
    per_ms: int
    on_exceeded: str            # cooldown | stop
    cooldown_ms: Optional[int]


@dataclass(frozen=True)
class StepPlan:
    """One ladder step, ALREADY resolved by the runtime.

    executor is not None → a service action (execute(target=service));
    executor is None      → the built-in verb restart_process (owner process).
    """
    executor: Optional[ActionExecutor]
    tries: Optional[int]                    # None → last step (retry until recovery)
    rate_limit: Optional[RateLimitPlan]     # on an action step (per-target); on a verb — restart_rate_limit
    suppress_ms: int = 0                    # startup window after the action (don't judge the service)

    @property
    def is_restart(self) -> bool:
        return self.executor is None


@dataclass(frozen=True)
class LevelPlan:
    name: str                   # L1/L2/P1/P2 (core semantics)
    mode: str                   # act | log
    threshold_ms: int           # dead_after_ms | frozen_after_ms (0 for composition-based P-levels)
    steps: List[StepPlan]


@dataclass(frozen=True)
class ServicePlan:
    name: str
    every_ms: int
    levels: Dict[str, LevelPlan]            # L1 required; L2 optional
    activity_tick_ms: Optional[int] = None


@dataclass(frozen=True)
class ProcessPlan:
    name: str
    grace_ms: int
    restart_limit: RateLimitPlan
    services: Dict[str, ServicePlan]
    plevels: Dict[str, LevelPlan] = field(default_factory=dict)   # P1/P2


@dataclass(frozen=True)
class GatePlan:
    gate: ResourceGate
    force_after_ms: int


@dataclass(frozen=True)
class PacingPlan:
    """Pauses between action retries. fixed → exactly delay_ms; backoff → the geometric
    start_ms·factor^(n-1) capped at cap_ms (parity with C pacing_delay)."""
    fixed: bool
    delay_ms: int = 0
    start_ms: int = 0
    factor: float = 2.0
    cap_ms: int = 0

    def delay(self, attempt: int) -> int:
        if self.fixed:
            return self.delay_ms
        d = float(self.start_ms)
        for _ in range(1, max(1, attempt)):
            d *= self.factor
            if self.cap_ms and d >= self.cap_ms:
                break
        return int(min(d, self.cap_ms)) if self.cap_ms else int(d)


# ════════════════════════════════════════════════════════════════════════════
#  Mutable state (per ladder target)
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class _LadderState:
    step: int = 0
    tries_used: int = 0
    last_action_ms: Optional[int] = None    # for pacing
    firing_since_ms: Optional[int] = None    # when the level first "hurts" (for the force gate)
    fires: List[int] = field(default_factory=list)   # execution timestamps (rate window, per target)
    cooldown_until_ms: Optional[int] = None


@dataclass
class _Episode:
    active: bool = False
    counter: Optional[int] = None
    last_progress_ms: int = 0
    last_state: Optional[str] = None


@dataclass
class _ServiceState:
    last_seen_ms: Optional[int] = None
    suppress_until_ms: int = 0               # after recreate/restart — don't judge
    episode: _Episode = field(default_factory=_Episode)
    ladders: Dict[str, _LadderState] = field(default_factory=dict)   # by level name


# ════════════════════════════════════════════════════════════════════════════
#  Health machine
# ════════════════════════════════════════════════════════════════════════════
class HealthMachine:
    """Watches one or more processes. Fed with signals (on_signal) and ticked
    (tick). The judgment order comes from the DESIGN "Judgment order" section."""

    def __init__(
        self,
        processes: Dict[str, ProcessPlan],
        *,
        clock: Clock,
        controller: ProcessController,
        gate: GatePlan,
        pacing: PacingPlan,
        logger: Logger,
        paused: Optional[Callable[[], bool]] = None,
        process_age_ms: Optional[Callable[[str], Optional[int]]] = None,
        request_stuck: Optional[Callable[[str], List[str]]] = None,
    ) -> None:
        self._procs = processes
        self._clock = clock
        self._ctl = controller
        self._gate = gate
        self._pacing = pacing
        self._log = logger
        self._paused = paused or (lambda: False)
        self._age = process_age_ms or (lambda _p: None)
        # request_stuck(process) -> list of services with a stuck request file (P2). B: []
        self._stuck = request_stuck or (lambda _p: [])
        # state
        self._svc: Dict[str, _ServiceState] = {}
        for p in processes.values():
            for sname in p.services:
                self._svc[sname] = _ServiceState()
        # INITIAL process grace (like C main: grace_until=start+grace_ms). It also serves
        # as the ANCHOR for the silence count of a service that has NEVER pulsed (parity with C, Stage 6 review).
        now0 = clock.now_ms()
        self._proc_grace_until: Dict[str, int] = {
            pname: now0 + p.grace_ms for pname, p in processes.items()}
        self._pause_logged_ms: Optional[int] = None
        self._ladder_store: Dict[str, _LadderState] = {}

    # ── signal intake ──
    def on_signal(self, sig: Signal) -> None:
        st = self._svc.get(sig.service)
        now = self._clock.now_ms()
        if st is None:
            self._log.log("unknown_signal", service=sig.service)
            return
        st.last_seen_ms = now                         # a pulse (any form) updates last_seen
        ep = st.episode
        # a state change closes the L2 episode
        if sig.state is not None and sig.state != ep.last_state:
            if ep.last_state == "active" and sig.state != "active":
                ep.active = False
            ep.last_state = sig.state
        if sig.state == "active" and sig.counter is not None:
            if not ep.active:
                ep.active = True
                ep.counter = sig.counter
                ep.last_progress_ms = now
            elif sig.counter != ep.counter:
                ep.counter = sig.counter
                ep.last_progress_ms = now
        if sig.state == "idle":
            ep.active = False

    # ── judgment tick ──
    def tick(self) -> None:
        now = self._clock.now_ms()
        if self._paused():
            if self._pause_logged_ms is None or now - self._pause_logged_ms >= 60000:
                self._log.log("paused")
                self._pause_logged_ms = now
            return
        self._pause_logged_ms = None
        for pname, proc in self._procs.items():
            self._tick_process(now, pname, proc)

    def _tick_process(self, now: int, pname: str, proc: ProcessPlan) -> None:
        # fresh gate: process younger than grace (just reborn) → blindness + reset (AT THE START)
        age = self._age(pname)
        if age is not None and age < proc.grace_ms:
            self._proc_grace_until[pname] = now + (proc.grace_ms - age)
            self._reset_process(proc)
            return
        # grace window (initial from __init__ or after a restart): don't judge, but KEEP THE ANCHOR
        grace_until = self._proc_grace_until.get(pname, now)
        if now < grace_until:
            return

        # P-levels (composition-based only; P1 requires ≥2 services)
        if self._judge_p_levels(now, pname, proc):
            return                              # a process action ran → its grace

        # L-levels per service
        for sname, svc in proc.services.items():
            self._judge_service(now, pname, proc, sname, svc)

    # ── P-levels ──
    def _judge_p_levels(self, now: int, pname: str, proc: ProcessPlan) -> bool:
        # P1: all services silent longer than their dead_after (core rule: ≥2 services)
        p1 = proc.plevels.get(P1)
        if p1 is not None and len(proc.services) >= 2:
            all_silent = True
            for sname, svc in proc.services.items():
                st = self._svc[sname]
                dead = svc.levels[L1].threshold_ms if L1 in svc.levels else None
                if st.last_seen_ms is None:
                    continue
                if dead is None or now - st.last_seen_ms <= dead:
                    all_silent = False
                    break
            seen_any = any(self._svc[s].last_seen_ms is not None for s in proc.services)
            if all_silent and seen_any:
                if self._run_level(now, pname, proc, "@process", p1):
                    return True
        # P2: request file is stuck (supervisor dead) — the fact source is injected
        p2 = proc.plevels.get(P2)
        if p2 is not None and self._stuck(pname):
            if self._run_level(now, pname, proc, "@process", p2):
                return True
        return False

    # ── L-levels of one service ──
    def _judge_service(self, now: int, pname: str, proc: ProcessPlan,
                       sname: str, svc: ServicePlan) -> None:
        st = self._svc[sname]
        if now < st.suppress_until_ms:
            return
        # ref: last_seen, or the process grace anchor for a service that has NEVER pulsed
        # (otherwise a service hung at startup would stay silent forever — a blind spot, Stage 6 review).
        ref = st.last_seen_ms if st.last_seen_ms is not None \
            else self._proc_grace_until.get(pname, now)

        # L1: pulse silence
        l1 = svc.levels.get(L1)
        if l1 is not None:
            if now - ref > l1.threshold_ms:
                self._run_level(now, pname, proc, sname, l1)
                return
            else:
                self._recover(now, sname, L1)

        # L2: counter frozen while active
        l2 = svc.levels.get(L2)
        if l2 is not None:
            ep = st.episode
            frozen = ep.active and (now - ep.last_progress_ms > l2.threshold_ms)
            if frozen:
                self._run_level(now, pname, proc, sname, l2)
                return
            else:
                self._recover(now, sname, L2)

    # ── executing the level ladder ──
    def _run_level(self, now: int, pname: str, proc: ProcessPlan,
                   target: str, level: LevelPlan) -> bool:
        key = "%s/%s" % (target, level.name)
        ls = self._ladder_state(now, key)
        if level.mode == "log":
            if ls.firing_since_ms is None:
                ls.firing_since_ms = now
                self._log.log("level_log_only", target=target, level=level.name)
            return False
        if ls.firing_since_ms is None:
            ls.firing_since_ms = now
        if ls.step >= len(level.steps):
            return False
        step = level.steps[ls.step]

        # cooldown of this step-target
        if ls.cooldown_until_ms is not None and now < ls.cooldown_until_ms:
            return False

        # pacing: pause between retries (fixed or geometric backoff by attempt)
        if ls.last_action_ms is not None and \
                now - ls.last_action_ms < self._pacing.delay(ls.tries_used + 1):
            return False

        # resource gate (with force after force_after_ms)
        if not self._gate.gate.allow():
            since = ls.firing_since_ms or now
            if now - since < self._gate.force_after_ms:
                self._log.log("gate_wait", target=target, level=level.name)
                return False
            self._log.log("gate_force", target=target, level=level.name)

        # rate limit (per target/step)
        rl = step.rate_limit
        if rl is not None:
            window_start = now - rl.per_ms
            ls.fires = [t for t in ls.fires if t >= window_start]
            if len(ls.fires) >= rl.max:
                if rl.on_exceeded == "cooldown":
                    ls.cooldown_until_ms = now + (rl.cooldown_ms or 0)
                    self._log.log("rate_limit_cooldown", target=target, level=level.name,
                                  until_ms=ls.cooldown_until_ms)
                else:  # stop (validator forbade it on a process restart)
                    self._log.log("rate_limit_stop", target=target, level=level.name)
                    ls.cooldown_until_ms = None
                    ls.step = len(level.steps)      # give up
                return False

        # EXECUTION
        if step.is_restart:
            self._ctl.restart(proc.name)
            self._proc_grace_until[proc.name] = now + proc.grace_ms
            self._reset_process(proc)
            self._log.log("restart_process", process=proc.name, level=level.name)
        else:
            step.executor.execute(target)           # target = service name
            self._svc[target].suppress_until_ms = now + step.suppress_ms
            self._log.log("action", target=target, level=level.name, step=ls.step)
        ls.last_action_ms = now
        ls.fires.append(now)
        ls.tries_used += 1

        # advancing through rungs: tries exhausted → next rung
        if step.tries is not None and ls.tries_used >= step.tries:
            ls.step += 1
            ls.tries_used = 0
            ls.last_action_ms = None
        return True

    # ── recovery: reset the level ladder to rung 0 (FR-36) ──
    def _recover(self, now: int, target: str, level_name: str) -> None:
        key = "%s/%s" % (target, level_name)
        ls = self._ladder_store.get(key)
        if ls is None:
            return
        if ls.step != 0 or ls.tries_used != 0 or ls.firing_since_ms is not None:
            self._log.log("recovered", target=target, level=level_name)
        self._ladder_store[key] = _LadderState()

    # ── helpers ──
    def _ladder_state(self, now: int, key: str) -> _LadderState:
        ls = self._ladder_store.get(key)
        if ls is None:
            ls = _LadderState()
            self._ladder_store[key] = ls
        return ls

    def _reset_process(self, proc: ProcessPlan) -> None:
        for sname in proc.services:
            self._svc[sname] = _ServiceState()
        for key in list(self._ladder_store.keys()):
            tgt = key.split("/", 1)[0]
            if tgt in proc.services or tgt == "@process":
                del self._ladder_store[key]


# ════════════════════════════════════════════════════════════════════════════
#  In-process supervisor (py side): min_stable + giving up (FR-32, FR-37)
# ════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class BackoffPlan:
    start_ms: int
    factor: float
    cap_ms: int


@dataclass
class _SupState:
    started_ms: Optional[int] = None
    consecutive_failures: int = 0
    next_allowed_ms: int = 0
    gave_up: bool = False
    handle: object = None


class Supervisor:
    """Recreates the services of its own process. "Came up" = lived min_stable_ms
    (FR-32); dying earlier = failure → backoff; after max_consecutive_start_failures
    in a row it STOPS recreating (FR-37) — the watcher's L1 will catch the silence.
    Event-driven: the runtime calls note_started/note_exit; the OS never enters core."""

    def __init__(self, start_mech: StartMechanism, *, clock: Clock, logger: Logger,
                 min_stable_ms: int, max_consecutive_start_failures: int,
                 backoff: BackoffPlan, stop_timeout_ms: int) -> None:
        self._sm = start_mech
        self._clock = clock
        self._log = logger
        self._min_stable = min_stable_ms
        self._max_fail = max_consecutive_start_failures
        self._backoff = backoff
        self._stop_timeout = stop_timeout_ms
        self._st: Dict[str, _SupState] = {}

    def _state(self, name: str) -> _SupState:
        s = self._st.get(name)
        if s is None:
            s = _SupState()
            self._st[name] = s
        return s

    def should_start(self, name: str) -> bool:
        """Bookkeeping (no I/O): may we attempt a start right now.
        False → gave up or in a backoff pause. For the async consumer A, which
        does the create itself via its own factory (see runtime.run_service)."""
        s = self._state(name)
        if s.gave_up:
            return False
        return self._clock.now_ms() >= s.next_allowed_ms

    def note_started(self, name: str) -> None:
        """Mark that the service has just been started (for the min_stable count)."""
        self._state(name).started_ms = self._clock.now_ms()
        self._log.log("service_started", service=name)

    def start(self, service) -> bool:
        """Synchronous start via StartMechanism (consumer B/tests).
        False → gave up or in a backoff pause."""
        name = service.name
        if not self.should_start(name):
            return False
        self._state(name).handle = self._sm.create(service)
        self.note_started(name)
        return True

    def recreate(self, service) -> bool:
        """Recreate (on request file): bounded teardown + start."""
        name = service.name
        s = self._state(name)
        if s.handle is not None:
            self._sm.teardown(s.handle, self._stop_timeout)
            s.handle = None
        return self.start(service)

    def note_exit(self, name: str) -> None:
        """The runtime noticed the service died. Decide: a start failure or a stable one crashing."""
        s = self._state(name)
        now = self._clock.now_ms()
        lived = now - s.started_ms if s.started_ms is not None else 0
        s.handle = None
        if lived < self._min_stable:
            s.consecutive_failures += 1
            if s.consecutive_failures >= self._max_fail:
                s.gave_up = True
                self._log.log("supervisor_give_up", service=name,
                              failures=s.consecutive_failures)
                return
            delay = min(self._backoff.cap_ms,
                        int(self._backoff.start_ms *
                            (self._backoff.factor ** (s.consecutive_failures - 1))))
            s.next_allowed_ms = now + delay
            self._log.log("service_start_failed", service=name,
                          failures=s.consecutive_failures, backoff_ms=delay)
        else:
            s.consecutive_failures = 0        # was stable — counter reset
            self._log.log("service_exited_stable", service=name, lived_ms=lived)

    def gave_up(self, name: str) -> bool:
        return self._state(name).gave_up
