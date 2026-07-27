"""svc_watch.config — schema v2, config parser and validator (stage E1).

Fail-closed: parses and validates the config WITHOUT running anything. Returns
a typed model (not a raw dict). ALL problems are collected into ONE list
(each with its key path), and only then is WdConfigError raised.

Implements rules 1–17 from CONFIG_V2_DRAFT.md. Collection capacities (rule 17)
are declared here as the single source of truth; the C daemon (E4) must match.

Public entry points:
    load(path)  -> Config     # reads file, validates, returns model
    loads(text) -> Config      # same, from a string
    parse(dict) -> Config      # same, from an already-parsed JSON

Errors:
    WdConfigError.problems : list[Problem]  # errors (validator failed)
    Config.warnings        : list[Problem]  # non-fatal warnings (rule 11)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ─── Collection capacities (rule 17) — SINGLE SOURCE, C-defines must match ───
MAX_PROCESSES = 8
MAX_SERVICES = 30          # per process; carried over from v1.2 (COMPILE-TIME)
MAX_ACTIONS = 16
MAX_LADDERS = 16
MAX_LADDER_STEPS = 8
MAX_WATCH_LEVELS = 4       # closed set is small anyway; capped for the C parser

# ─── String lengths (bytes) — C buffers NAME 48 / STATE 24 / PATH 256 (room for \0) ───
NAME_MAX = 47
STATE_MAX = 23
PATH_MAX = 256

# ─── type registries (rule 2). Extensible via adapter files (register_type, E3). ───
TRANSPORT_TYPES = {"unix_datagram", "inmemory"}
PROBE_TYPES = {"tcp", "inmemory"}
START_TYPES = {"python", "inmemory"}
ACTION_TYPES = {"request_file", "inmemory"}
LAUNCH_TYPES = {"init_script", "inmemory", "external"}

_TYPE_REGISTRIES = {
    "transport": TRANSPORT_TYPES,
    "probe": PROBE_TYPES,
    "start": START_TYPES,
    "action": ACTION_TYPES,
    "launch": LAUNCH_TYPES,
}


def register_type(kind: str, name: str) -> None:
    """Register a new type via an adapter (E3: new kind = new file + this line,
    WITHOUT editing config.py). kind ∈ transport|probe|start|action|launch."""
    reg = _TYPE_REGISTRIES.get(kind)
    if reg is None:
        raise ValueError("unknown registry kind: %r" % (kind,))
    reg.add(name)


# ─── Typed (non-adapter) variants — fixed schema sets ───
PACING_TYPES = {"fixed", "backoff"}
GRACE_TYPES = {"fixed", "until_ready"}
VERBS = {"restart_process"}                 # do without @ (rule 3)
MODES = {"act", "log"}

SERVICE_LEVELS = {"L1_pulse_lost", "L2_activity_frozen"}     # rule 16
PROCESS_LEVELS = {"P1_all_pulses_lost", "P2_request_stuck"}


# ════════════════════════════════════════════════════════════════════════════
#  Errors
# ════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Problem:
    path: str
    message: str

    def __str__(self) -> str:
        return "%s: %s" % (self.path, self.message)


class WdConfigError(Exception):
    """Config failed validation. Carries ALL found problems at once."""

    def __init__(self, problems: List[Problem]):
        self.problems = list(problems)
        body = "\n".join("  - %s" % p for p in self.problems)
        super().__init__(
            "config is invalid (%d problems):\n%s" % (len(self.problems), body)
        )


# ════════════════════════════════════════════════════════════════════════════
#  Typed model (what the loader returns)
# ════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class RateLimit:
    max: int
    per_ms: int
    on_exceeded: str          # cooldown | stop
    cooldown_ms: Optional[int]


@dataclass(frozen=True)
class Log:
    file: str
    rotate_kb: int
    keep: int
    fsync: Optional[bool] = None
    fallbacks: Optional[List[str]] = None


@dataclass(frozen=True)
class Transport:
    type: str
    params: Dict[str, Any]


@dataclass(frozen=True)
class Observer:
    mode: str
    tick_ms: int
    pause_file: str
    oom_adj: int
    log: Log
    quiet_paused_ms: int
    quiet_unknown_ms: int
    enabled: bool = True        # config-native toggle: startup.sh gates the daemon; default on


@dataclass(frozen=True)
class Gates:
    min_free_mb: int
    max_load1: float
    recheck_ms: int
    force_after_ms: int
    alarm_mb: int


@dataclass(frozen=True)
class Pacing:
    type: str
    params: Dict[str, Any]


@dataclass(frozen=True)
class Framework:
    transport: Transport
    observer: Observer
    gates: Gates
    pacing: Pacing


@dataclass(frozen=True)
class Action:
    name: str
    type: str
    params: Dict[str, Any]     # for request_file: file, eat_within_ms, startup_ms
    rate_limit: RateLimit


@dataclass(frozen=True)
class LadderStep:
    do: str                    # original string
    is_verb: bool              # True → built-in verb; False → @action
    ref: str                   # action name (if !is_verb) or the verb itself
    tries: Optional[int]       # None only for the last step


@dataclass(frozen=True)
class Ladder:
    name: str
    steps: List[LadderStep]


@dataclass(frozen=True)
class Grace:
    type: str
    ms: Optional[int] = None            # fixed
    max_ms: Optional[int] = None        # until_ready


@dataclass(frozen=True)
class Launch:
    type: str
    params: Dict[str, Any]              # script, pidfile (for init_script) etc.
    grace: Grace
    restart_rate_limit: RateLimit


@dataclass(frozen=True)
class Backoff:
    start_ms: int
    factor: float
    cap_ms: int


@dataclass(frozen=True)
class Supervisor:
    poll_ms: int
    stop_timeout_ms: int
    min_stable_ms: int
    max_consecutive_start_failures: int
    backoff: Backoff
    log: Log


@dataclass(frozen=True)
class Probe:
    type: str
    params: Dict[str, Any]              # port (resolved from @port), timeout_ms
    timeout_ms: Optional[int]           # None for non-tcp (inmemory) variants


@dataclass(frozen=True)
class PulseSignal:
    source: str                        # loop | probe
    every_ms: int
    probe: Optional[Probe]


@dataclass(frozen=True)
class ActivitySignal:
    tick_ms: int


@dataclass(frozen=True)
class WatchLevel:
    name: str                          # L1_pulse_lost | L2_activity_frozen | P1_* | P2_*
    mode: str
    ladder: str                        # ladder name from the catalog (@ stripped) or "" for inline
    inline: Optional[Ladder]           # if ladder is given as an array
    params: Dict[str, Any]             # dead_after_ms | frozen_after_ms


@dataclass(frozen=True)
class Start:
    type: str
    params: Dict[str, Any]             # entry (for python) etc.


@dataclass(frozen=True)
class Service:
    name: str
    start: Start
    port: Optional[int]
    pulse: PulseSignal
    activity: Optional[ActivitySignal]
    watch: Dict[str, WatchLevel]
    enabled: bool = True        # config-native toggle: false → not started (Python) nor watched (C); default on


@dataclass(frozen=True)
class Process:
    name: str
    launch: Launch
    supervisor: Supervisor
    watch: Dict[str, WatchLevel]
    services: Dict[str, Service]


@dataclass(frozen=True)
class Config:
    schema: int
    framework: Framework
    actions: Dict[str, Action]
    ladders: Dict[str, Ladder]
    processes: Dict[str, Process]
    warnings: List[Problem] = field(default_factory=list)


# ════════════════════════════════════════════════════════════════════════════
#  Validator (error accumulator)
# ════════════════════════════════════════════════════════════════════════════
_MISSING = object()


class _V:
    """Validation context: accumulates errors/warnings, does not raise mid-flight."""

    def __init__(self) -> None:
        self.errors: List[Problem] = []
        self.warnings: List[Problem] = []

    def err(self, path: str, msg: str) -> None:
        self.errors.append(Problem(path, msg))

    def warn(self, path: str, msg: str) -> None:
        self.warnings.append(Problem(path, msg))

    # — primitives —
    def req(self, d: Any, key: str, path: str, typ: Optional[str] = None) -> Any:
        """Required key. Missing/wrong type → error + _MISSING."""
        if not isinstance(d, dict):
            return _MISSING
        if key not in d:
            self.err("%s.%s" % (path, key), "required key is missing")
            return _MISSING
        v = d[key]
        if typ is not None and not _typeok(v, typ):
            self.err("%s.%s" % (path, key), "expected type %s, got %s"
                     % (typ, _typename(v)))
            return _MISSING
        return v

    def opt(self, d: Any, key: str, path: str, typ: Optional[str] = None) -> Any:
        """Optional key. Missing → _MISSING (no error); wrong type → error."""
        if not isinstance(d, dict) or key not in d:
            return _MISSING
        v = d[key]
        if typ is not None and not _typeok(v, typ):
            self.err("%s.%s" % (path, key), "expected type %s, got %s"
                     % (typ, _typename(v)))
            return _MISSING
        return v

    def enum(self, v: Any, allowed: set, path: str, what: str) -> None:
        if v is _MISSING:
            return
        if v not in allowed:
            self.err(path, "%s: %r not in {%s}"
                     % (what, v, ", ".join(sorted(map(str, allowed)))))

    def unknown(self, d: Any, allowed: set, path: str) -> None:
        """Rule 8: unknown key = rejection (guards against typos)."""
        if not isinstance(d, dict):
            return
        for k in d:
            if k not in allowed:
                self.err("%s.%s" % (path, k),
                         "unknown key (typo? allowed: %s)"
                         % ", ".join(sorted(allowed)))

    def positive(self, v: Any, path: str) -> None:
        """Rule 9: intervals/limits ≥ 1 (degenerate 0 is forbidden)."""
        if v is _MISSING:
            return
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return
        if v < 1:
            self.err(path, "must be ≥ 1 (degenerate 0/negative is forbidden)")

    def strlen(self, v: Any, limit: int, path: str) -> None:
        if v is _MISSING or not isinstance(v, str):
            return
        n = len(v.encode("utf-8"))
        if n > limit:
            self.err(path, "string %d bytes > C capacity %d" % (n, limit))


def _typeok(v: Any, t: str) -> bool:
    if t == "int":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "num":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "str":
        return isinstance(v, str)
    if t == "bool":
        return isinstance(v, bool)
    if t == "dict":
        return isinstance(v, dict)
    if t == "list":
        return isinstance(v, list)
    return True


def _typename(v: Any) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, dict):
        return "object"
    if isinstance(v, list):
        return "list"
    if v is None:
        return "null"
    return type(v).__name__


# ════════════════════════════════════════════════════════════════════════════
#  Section parsing
# ════════════════════════════════════════════════════════════════════════════
def _rate_limit(v: _V, d: Any, path: str, *, allow_stop: bool) -> Optional[RateLimit]:
    """Rule 12: rate_limit is required wherever it is invoked; on_exceeded ∈ {cooldown,stop};
    stop is forbidden when allow_stop=False (launch.restart_rate_limit)."""
    v.unknown(d, {"max", "per_ms", "on_exceeded", "cooldown_ms"}, path)
    mx = v.req(d, "max", path, "int")
    per = v.req(d, "per_ms", path, "int")
    oe = v.req(d, "on_exceeded", path, "str")
    v.positive(mx, "%s.max" % path)
    v.positive(per, "%s.per_ms" % path)
    cd = _MISSING
    allowed = {"cooldown", "stop"}
    v.enum(oe, allowed, "%s.on_exceeded" % path, "on_exceeded")
    if oe == "stop" and not allow_stop:
        v.err("%s.on_exceeded" % path,
              "stop is FORBIDDEN on process restart (rule 12): the only path "
              "to rescue the box without an operator has no right to give up forever")
    if oe == "cooldown":
        cd = v.req(d, "cooldown_ms", path, "int")
        v.positive(cd, "%s.cooldown_ms" % path)
        # rule 6: cooldown_ms > per_ms/max
        if isinstance(cd, int) and isinstance(per, int) and isinstance(mx, int) and mx > 0:
            if cd <= per / mx:
                v.err("%s.cooldown_ms" % path,
                      "cooldown_ms (%d) must be > per_ms/max (%.0f)"
                      % (cd, per / mx))
    elif "cooldown_ms" in (d if isinstance(d, dict) else {}):
        v.err("%s.cooldown_ms" % path, "cooldown_ms only makes sense with on_exceeded:cooldown")
    if _MISSING in (mx, per, oe):
        return None
    return RateLimit(max=mx, per_ms=per, on_exceeded=oe,
                     cooldown_ms=(cd if cd is not _MISSING else None))


def _log(v: _V, d: Any, path: str, *, kind: str) -> Optional[Log]:
    """kind='observer' → fsync; kind='supervisor' → fallbacks."""
    allowed = {"file", "rotate_kb", "keep"}
    if kind == "observer":
        allowed |= {"fsync"}
    else:
        allowed |= {"fallbacks"}
    v.unknown(d, allowed, path)
    f = v.req(d, "file", path, "str")
    rk = v.req(d, "rotate_kb", path, "int")
    kp = v.req(d, "keep", path, "int")
    v.strlen(f, PATH_MAX, "%s.file" % path)
    v.positive(rk, "%s.rotate_kb" % path)
    v.positive(kp, "%s.keep" % path)
    fsync = v.opt(d, "fsync", path, "bool")
    fb = v.opt(d, "fallbacks", path, "list")
    fb_val: Optional[List[str]] = None
    if fb is not _MISSING:
        fb_val = []
        for i, item in enumerate(fb):
            if not isinstance(item, str):
                v.err("%s.fallbacks[%d]" % (path, i), "expected a path string")
            else:
                v.strlen(item, PATH_MAX, "%s.fallbacks[%d]" % (path, i))
                fb_val.append(item)
    if _MISSING in (f, rk, kp):
        return None
    return Log(file=f, rotate_kb=rk, keep=kp,
               fsync=(fsync if fsync is not _MISSING else None),
               fallbacks=fb_val)


def _type_field(v: _V, d: Any, path: str, allowed: set) -> Any:
    """Read and check the required `type` (rule 2). Unknown-key checking is done by
    the CALLER (the set of allowed fields depends on type)."""
    t = v.req(d, "type", path, "str")
    v.enum(t, allowed, "%s.type" % path, "type")
    return t


def _transport(v: _V, d: Any, path: str) -> Optional[Transport]:
    """The only NESTED variant: params live under the type-name key
    (rule 5: a foreign variant block = dead filler = rejection)."""
    if not isinstance(d, dict):
        v.err(path, "expected a transport object")
        return None
    variant_keys = {"unix_datagram": {"socket", "format"}, "inmemory": set()}
    t = _type_field(v, d, path, TRANSPORT_TYPES)
    v.unknown(d, {"type"} | set(variant_keys.keys()), path)
    for vt in variant_keys:
        if vt in d and vt != t:
            v.err("%s.%s" % (path, vt),
                  "variant block %r is redundant when type=%r (dead filler is forbidden)"
                  % (vt, t))
    params: Dict[str, Any] = {}
    if t == "unix_datagram":
        block = d.get("unix_datagram")
        if not isinstance(block, dict):
            v.err("%s.unix_datagram" % path, "required params block for type=unix_datagram is missing")
        else:
            v.unknown(block, {"socket", "format"}, "%s.unix_datagram" % path)
            sock = v.req(block, "socket", "%s.unix_datagram" % path, "str")
            fmt = v.req(block, "format", "%s.unix_datagram" % path, "str")
            v.strlen(sock, PATH_MAX, "%s.unix_datagram.socket" % path)
            v.enum(fmt, {"text_v1"}, "%s.unix_datagram.format" % path, "format")
            params = dict(block)
    if t is _MISSING:
        return None
    return Transport(type=t, params=params)


def _observer(v: _V, d: Any, path: str) -> Optional[Observer]:
    v.unknown(d, {"mode", "tick_ms", "pause_file", "oom_adj", "log", "quiet", "enabled"}, path)
    mode = v.req(d, "mode", path, "str")
    v.enum(mode, MODES, "%s.mode" % path, "mode")
    tick = v.req(d, "tick_ms", path, "int")
    v.positive(tick, "%s.tick_ms" % path)
    pf = v.req(d, "pause_file", path, "str")
    v.strlen(pf, PATH_MAX, "%s.pause_file" % path)
    oom = v.req(d, "oom_adj", path, "int")
    logd = v.req(d, "log", path, "dict")
    log = _log(v, logd, "%s.log" % path, kind="observer") if logd is not _MISSING else None
    quiet = v.req(d, "quiet", path, "dict")
    qp = qu = _MISSING
    if quiet is not _MISSING:
        v.unknown(quiet, {"paused_ms", "unknown_ms"}, "%s.quiet" % path)
        qp = v.req(quiet, "paused_ms", "%s.quiet" % path, "int")
        qu = v.req(quiet, "unknown_ms", "%s.quiet" % path, "int")
        v.positive(qp, "%s.quiet.paused_ms" % path)      # rule 9: 0 forbidden
        v.positive(qu, "%s.quiet.unknown_ms" % path)
    en = v.opt(d, "enabled", path, "bool")
    enabled = True if en is _MISSING else en
    if _MISSING in (mode, tick, pf, oom, qp, qu) or log is None:
        return None
    return Observer(mode=mode, tick_ms=tick, pause_file=pf, oom_adj=oom, log=log,
                    quiet_paused_ms=qp, quiet_unknown_ms=qu, enabled=enabled)


def _gates(v: _V, d: Any, path: str) -> Optional[Gates]:
    v.unknown(d, {"min_free_mb", "max_load1", "recheck_ms", "force_after_ms", "alarm_mb"}, path)
    mf = v.req(d, "min_free_mb", path, "int")
    ml = v.req(d, "max_load1", path, "num")
    rc = v.req(d, "recheck_ms", path, "int")
    fa = v.req(d, "force_after_ms", path, "int")
    al = v.req(d, "alarm_mb", path, "int")
    v.positive(rc, "%s.recheck_ms" % path)
    v.positive(fa, "%s.force_after_ms" % path)
    # rule 6: min_free_mb > alarm_mb
    if isinstance(mf, int) and isinstance(al, int) and mf <= al:
        v.err("%s.min_free_mb" % path,
              "min_free_mb (%d) must be > alarm_mb (%d)" % (mf, al))
    if _MISSING in (mf, ml, rc, fa, al):
        return None
    return Gates(min_free_mb=mf, max_load1=float(ml), recheck_ms=rc,
                 force_after_ms=fa, alarm_mb=al)


def _pacing(v: _V, d: Any, path: str) -> Optional[Pacing]:
    """Rule 14: type ∈ {fixed,backoff}; only the params of its own variant are active
    (flat: params next to type; a foreign params → unknown-key = rejection)."""
    if not isinstance(d, dict):
        v.err(path, "expected a pacing object")
        return None
    t = _type_field(v, d, path, PACING_TYPES)
    allowed = {"type"}
    if t == "fixed":
        allowed |= {"delay_ms"}
    elif t == "backoff":
        allowed |= {"start_ms", "factor", "cap_ms"}
    v.unknown(d, allowed, path)
    params: Dict[str, Any] = {}
    if t == "fixed":
        dl = v.req(d, "delay_ms", path, "int")
        v.positive(dl, "%s.delay_ms" % path)
        params = {"delay_ms": dl if dl is not _MISSING else None}
    elif t == "backoff":
        s = v.req(d, "start_ms", path, "int")
        fa = v.req(d, "factor", path, "num")
        cp = v.req(d, "cap_ms", path, "int")
        v.positive(s, "%s.start_ms" % path)
        v.positive(cp, "%s.cap_ms" % path)
        params = {"start_ms": s if s is not _MISSING else None,
                  "factor": float(fa) if isinstance(fa, (int, float)) and not isinstance(fa, bool) else None,
                  "cap_ms": cp if cp is not _MISSING else None}
    if t is _MISSING:
        return None
    return Pacing(type=t, params=params)


def _grace(v: _V, d: Any, path: str, *, process_has_ready: bool) -> Optional[Grace]:
    """Rule 13: type ∈ {fixed,until_ready}; fixed⇒ms; until_ready⇒max_ms AND ready signal."""
    v.unknown(d, {"type", "ms", "max_ms"}, path)
    t = v.req(d, "type", path, "str")
    v.enum(t, GRACE_TYPES, "%s.type" % path, "grace.type")
    ms = mx = _MISSING
    if t == "fixed":
        ms = v.req(d, "ms", path, "int")
        v.positive(ms, "%s.ms" % path)
        if isinstance(d, dict) and "max_ms" in d:
            v.err("%s.max_ms" % path, "max_ms only makes sense with type:until_ready")
    elif t == "until_ready":
        mx = v.req(d, "max_ms", path, "int")     # rule 6/13: max_ms is required
        v.positive(mx, "%s.max_ms" % path)
        if not process_has_ready:
            v.err("%s.type" % path,
                  "until_ready requires a declared process ready signal, which is absent "
                  "(accept-but-cannot-honor, rule 13); only fixed is legal for now")
        if isinstance(d, dict) and "ms" in d:
            v.err("%s.ms" % path, "ms only makes sense with type:fixed")
    if t is None:
        return None
    return Grace(type=t, ms=(ms if ms is not _MISSING else None),
                 max_ms=(mx if mx is not _MISSING else None))


def _action(v: _V, name: str, d: Any, path: str) -> Optional[Action]:
    if not isinstance(d, dict):
        v.err(path, "expected an action object")
        return None
    t = _type_field(v, d, path, ACTION_TYPES)
    top = {"type", "rate_limit"}
    rl = None
    if t == "request_file":
        top |= {"file", "eat_within_ms", "startup_ms"}
    v.unknown(d, top, path)
    params: Dict[str, Any] = {}
    if t == "request_file":
        f = v.req(d, "file", path, "str")
        ew = v.req(d, "eat_within_ms", path, "int")
        su = v.req(d, "startup_ms", path, "int")
        v.strlen(f, PATH_MAX, "%s.file" % path)
        v.positive(ew, "%s.eat_within_ms" % path)
        v.positive(su, "%s.startup_ms" % path)
        params = {"file": f if f is not _MISSING else None,
                  "eat_within_ms": ew if ew is not _MISSING else None,
                  "startup_ms": su if su is not _MISSING else None}
    rld = v.req(d, "rate_limit", path, "dict")     # rule 12: required
    if rld is not _MISSING:
        rl = _rate_limit(v, rld, "%s.rate_limit" % path, allow_stop=True)
    if t is _MISSING or rl is None:
        return None
    return Action(name=name, type=t, params=params, rate_limit=rl)


def _ladder_steps(v: _V, arr: Any, path: str) -> Optional[List[LadderStep]]:
    """Rule 3 (do → @action|verb) + rule 15 (tries≥1, only the last step without tries)."""
    if not isinstance(arr, list):
        v.err(path, "ladder must be an array of steps")
        return None
    if len(arr) == 0:
        v.err(path, "ladder is empty")
        return None
    if len(arr) > MAX_LADDER_STEPS:
        v.err(path, "steps %d > capacity MAX_LADDER_STEPS=%d" % (len(arr), MAX_LADDER_STEPS))
    steps: List[LadderStep] = []
    ok = True
    last = len(arr) - 1
    for i, step in enumerate(arr):
        sp = "%s[%d]" % (path, i)
        if not isinstance(step, dict):
            v.err(sp, "step must be an object {do, tries?}")
            ok = False
            continue
        v.unknown(step, {"do", "tries"}, sp)
        do = v.req(step, "do", sp, "str")
        is_verb = False
        ref = ""
        if do is not _MISSING:
            if do.startswith("@"):
                ref = do[1:]                       # @action — resolved later
                is_verb = False
            else:
                is_verb = True
                ref = do
                if do not in VERBS:                # rule 3: closed set of verbs
                    v.err(sp + ".do", "%r is neither an @action nor a built-in verb {%s}"
                          % (do, ", ".join(sorted(VERBS))))
                    ok = False
        tries = _MISSING
        if isinstance(step, dict) and "tries" in step:
            tries = v.opt(step, "tries", sp, "int")
            if isinstance(tries, int) and tries < 1:     # rule 15: tries≥1
                v.err(sp + ".tries", "tries must be ≥ 1 (0 = the step never yields its turn)")
                ok = False
        else:
            # rule 15: only the last step may omit tries
            if i != last:
                v.err(sp, "non-last step without tries: the next step is unreachable "
                          "(rule 15)")
                ok = False
        if do is _MISSING:
            ok = False
            continue
        steps.append(LadderStep(
            do=do, is_verb=is_verb, ref=ref,
            tries=(tries if isinstance(tries, int) else None)))
    return steps if ok else None


def _grace_and_launch(v: _V, d: Any, path: str) -> Optional[Launch]:
    if not isinstance(d, dict):
        v.err(path, "expected a launch object")
        return None
    t = _type_field(v, d, path, LAUNCH_TYPES)
    top = {"type", "grace", "restart_rate_limit"}
    if t == "init_script":
        top |= {"script", "pidfile"}
    v.unknown(d, top, path)
    params: Dict[str, Any] = {}
    if t == "init_script":
        sc = v.req(d, "script", path, "str")
        pid = v.req(d, "pidfile", path, "str")
        v.strlen(sc, PATH_MAX, "%s.script" % path)
        v.strlen(pid, PATH_MAX, "%s.pidfile" % path)
        params = {"script": sc if sc is not _MISSING else None,
                  "pidfile": pid if pid is not _MISSING else None}
    gd = v.req(d, "grace", path, "dict")
    grace = _grace(v, gd, "%s.grace" % path, process_has_ready=False) if gd is not _MISSING else None
    rld = v.req(d, "restart_rate_limit", path, "dict")   # rule 12
    rl = _rate_limit(v, rld, "%s.restart_rate_limit" % path, allow_stop=False) if rld is not _MISSING else None
    if t is _MISSING or grace is None or rl is None:
        return None
    return Launch(type=t, params=params, grace=grace, restart_rate_limit=rl)


def _supervisor(v: _V, d: Any, path: str) -> Tuple[Optional[Supervisor], Any]:
    allowed = {"poll_ms", "stop_timeout_ms", "min_stable_ms",
               "max_consecutive_start_failures", "backoff", "log"}
    v.unknown(d, allowed, path)
    poll = v.req(d, "poll_ms", path, "int")
    st = v.req(d, "stop_timeout_ms", path, "int")
    mst = v.req(d, "min_stable_ms", path, "int")
    mcf = v.req(d, "max_consecutive_start_failures", path, "int")
    v.positive(poll, "%s.poll_ms" % path)
    v.positive(st, "%s.stop_timeout_ms" % path)
    v.positive(mst, "%s.min_stable_ms" % path)
    v.positive(mcf, "%s.max_consecutive_start_failures" % path)
    bd = v.req(d, "backoff", path, "dict")
    backoff = None
    if bd is not _MISSING:
        v.unknown(bd, {"start_ms", "factor", "cap_ms"}, "%s.backoff" % path)
        bs = v.req(bd, "start_ms", "%s.backoff" % path, "int")
        bf = v.req(bd, "factor", "%s.backoff" % path, "num")
        bc = v.req(bd, "cap_ms", "%s.backoff" % path, "int")
        v.positive(bs, "%s.backoff.start_ms" % path)
        v.positive(bc, "%s.backoff.cap_ms" % path)
        if _MISSING not in (bs, bf, bc):
            backoff = Backoff(start_ms=bs, factor=float(bf), cap_ms=bc)
    logd = v.req(d, "log", path, "dict")
    log = _log(v, logd, "%s.log" % path, kind="supervisor") if logd is not _MISSING else None
    if _MISSING in (poll, st, mst, mcf) or backoff is None or log is None:
        return None, poll
    return Supervisor(poll_ms=poll, stop_timeout_ms=st, min_stable_ms=mst,
                      max_consecutive_start_failures=mcf, backoff=backoff, log=log), poll


def _watch_level(v: _V, name: str, d: Any, path: str, *, allowed_levels: set,
                 ladders: set) -> Optional[WatchLevel]:
    # rule 16: level key from a closed set
    if name not in allowed_levels:
        v.err(path, "unknown watch level %r; allowed {%s}"
              % (name, ", ".join(sorted(allowed_levels))))
        return None
    # allowed fields depend on the level
    fields = {"mode", "ladder"}
    if name == "L1_pulse_lost":
        fields |= {"dead_after_ms"}
    elif name == "L2_activity_frozen":
        fields |= {"frozen_after_ms"}
    v.unknown(d, fields, path)
    mode = v.req(d, "mode", path, "str")
    v.enum(mode, MODES, "%s.mode" % path, "mode")
    params: Dict[str, Any] = {}
    if name == "L1_pulse_lost":
        da = v.req(d, "dead_after_ms", path, "int")
        v.positive(da, "%s.dead_after_ms" % path)
        params["dead_after_ms"] = da if da is not _MISSING else None
    elif name == "L2_activity_frozen":
        fz = v.req(d, "frozen_after_ms", path, "int")
        v.positive(fz, "%s.frozen_after_ms" % path)
        params["frozen_after_ms"] = fz if fz is not _MISSING else None
    # ladder: only "@name" from the catalog (inline arrays are abolished — agreed with
    # the C daemon, which accepts only @name; so both validators match, review E6)
    ladder_name = ""
    inline = None
    lad = v.req(d, "ladder", path, None)
    if lad is not _MISSING:
        if isinstance(lad, str):
            if not lad.startswith("@"):
                v.err("%s.ladder" % path, "ladder reference must be '@name'")
            else:
                ladder_name = lad[1:]
                if ladder_name not in ladders:      # rule 3: @-reference must resolve
                    v.err("%s.ladder" % path,
                          "ladder @%s not found in the ladders catalog" % ladder_name)
        else:
            v.err("%s.ladder" % path,
                  "ladder must be a string '@name' (inline arrays are not supported)")
    if mode is _MISSING:
        return None
    return WatchLevel(name=name, mode=mode, ladder=ladder_name, inline=inline, params=params)


def _probe(v: _V, d: Any, path: str, *, service_ports: set) -> Optional[Probe]:
    if not isinstance(d, dict):
        v.err(path, "expected a probe object")
        return None
    t = _type_field(v, d, path, PROBE_TYPES)
    top = {"type"}
    if t == "tcp":
        top |= {"port", "timeout_ms"}
    v.unknown(d, top, path)
    params: Dict[str, Any] = {}
    tmo: Any = _MISSING
    if t == "tcp":
        port = v.req(d, "port", path, None)
        tmo = v.req(d, "timeout_ms", path, "int")
        v.positive(tmo, "%s.timeout_ms" % path)
        # port may be "@port" (reference to its own service) or int
        if port is not _MISSING:
            if isinstance(port, str):
                if port != "@port":
                    v.err("%s.port" % path, "port reference: expected '@port' or a number")
                elif "port" not in service_ports:
                    v.err("%s.port" % path,
                          "@port references service.port, which the service does not have")
            elif not (isinstance(port, int) and not isinstance(port, bool)):
                v.err("%s.port" % path, "port must be a number or '@port'")
            params["port"] = port
    if t is _MISSING:
        return None
    return Probe(type=t, params=params,
                 timeout_ms=(tmo if isinstance(tmo, int) else None))


def _service(v: _V, name: str, d: Any, path: str, *, ladders: set) -> Optional[Service]:
    v.unknown(d, {"start", "port", "signals", "watch", "enabled"}, path)
    v.strlen(name, NAME_MAX, "%s (service name)" % path)
    en = v.opt(d, "enabled", path, "bool")
    enabled = True if en is _MISSING else en
    # start
    sd = v.req(d, "start", path, "dict")
    start = None
    if sd is not _MISSING:
        t = _type_field(v, sd, "%s.start" % path, START_TYPES)
        top = {"type"}
        if t == "python":
            top |= {"entry"}
        v.unknown(sd, top, "%s.start" % path)
        params: Dict[str, Any] = {}
        if t == "python":
            entry = v.req(sd, "entry", "%s.start" % path, "str")
            params = {"entry": entry if entry is not _MISSING else None}
        if t is not _MISSING:
            start = Start(type=t, params=params)
    # port
    port = v.opt(d, "port", path, "int")
    service_ports = {"port"} if port is not _MISSING else set()
    port_val = port if port is not _MISSING else None
    # signals
    sig = v.req(d, "signals", path, "dict")
    pulse = None
    activity = None
    if sig is not _MISSING:
        v.unknown(sig, {"pulse", "activity"}, "%s.signals" % path)
        pd = v.req(sig, "pulse", "%s.signals" % path, "dict")
        if pd is not _MISSING:
            pulse = _pulse(v, pd, "%s.signals.pulse" % path, service_ports=service_ports)
        ad = v.opt(sig, "activity", "%s.signals" % path, "dict")
        if ad is not _MISSING:
            v.unknown(ad, {"tick_ms"}, "%s.signals.activity" % path)
            tk = v.req(ad, "tick_ms", "%s.signals.activity" % path, "int")
            v.positive(tk, "%s.signals.activity.tick_ms" % path)
            if tk is not _MISSING:
                activity = ActivitySignal(tick_ms=tk)
    # watch
    wd = v.req(d, "watch", path, "dict")
    watch: Dict[str, WatchLevel] = {}
    if wd is not _MISSING:
        if len(wd) > MAX_WATCH_LEVELS:
            v.err("%s.watch" % path, "levels %d > MAX_WATCH_LEVELS=%d"
                  % (len(wd), MAX_WATCH_LEVELS))
        for lname, ld in wd.items():
            wl = _watch_level(v, lname, ld, "%s.watch.%s" % (path, lname),
                              allowed_levels=SERVICE_LEVELS, ladders=ladders)
            if wl is not None:
                watch[lname] = wl

    # ── rule 5: signal ↔ level pairing ──
    has_pulse = pulse is not None
    has_l1 = "L1_pulse_lost" in watch
    if has_pulse and not has_l1:
        v.err("%s.watch" % path, "signals.pulse is present, but watch.L1_pulse_lost is missing (rule 5)")
    if has_l1 and not has_pulse:
        v.err("%s.signals" % path, "watch.L1_pulse_lost is present, but signals.pulse is missing (rule 5)")
    has_act = activity is not None
    has_l2 = "L2_activity_frozen" in watch
    if has_act and not has_l2:
        v.err("%s.watch" % path,
              "signals.activity is present, but watch.L2_activity_frozen is missing (rule 5)")
    if has_l2 and not has_act:
        v.err("%s.signals" % path,
              "watch.L2_activity_frozen is present, but signals.activity is missing (rule 5)")

    # ── rule 6: numeric relationships within the service ──
    if pulse is not None and has_l1:
        da = watch["L1_pulse_lost"].params.get("dead_after_ms")
        if isinstance(da, int) and isinstance(pulse.every_ms, int):
            if da < 3 * pulse.every_ms:
                v.err("%s.watch.L1_pulse_lost.dead_after_ms" % path,
                      "dead_after_ms (%d) must be ≥ 3×every_ms (%d)"
                      % (da, 3 * pulse.every_ms))
        if pulse.probe is not None and isinstance(pulse.probe.timeout_ms, int) \
                and isinstance(pulse.every_ms, int):
            if pulse.probe.timeout_ms >= pulse.every_ms:
                v.err("%s.signals.pulse.probe.timeout_ms" % path,
                      "probe.timeout_ms (%d) must be < every_ms (%d)"
                      % (pulse.probe.timeout_ms, pulse.every_ms))
    if activity is not None and has_l2:
        fz = watch["L2_activity_frozen"].params.get("frozen_after_ms")
        if isinstance(fz, int) and isinstance(activity.tick_ms, int):
            if fz < 20 * activity.tick_ms:
                v.err("%s.watch.L2_activity_frozen.frozen_after_ms" % path,
                      "frozen_after_ms (%d) must be ≥ 20×tick_ms (%d)"
                      % (fz, 20 * activity.tick_ms))

    if start is None or pulse is None:
        return None
    return Service(name=name, start=start, port=port_val, pulse=pulse,
                   activity=activity, watch=watch, enabled=enabled)


def _pulse(v: _V, d: Any, path: str, *, service_ports: set) -> Optional[PulseSignal]:
    v.unknown(d, {"from", "every_ms", "probe"}, path)
    src = v.req(d, "from", path, "str")
    v.enum(src, {"loop", "probe"}, "%s.from" % path, "from")
    ev = v.req(d, "every_ms", path, "int")
    v.positive(ev, "%s.every_ms" % path)
    has_probe = isinstance(d, dict) and "probe" in d
    probe = None
    # rule 4: pulse form
    if src == "loop" and has_probe:
        v.err("%s.probe" % path,
              "from:loop proves liveness by the loop itself — a probe block is forbidden (rule 4)")
    if src == "probe":
        if not has_probe:
            v.err("%s.probe" % path,
                  "from:probe requires a probe block (the pulse is sent only on a successful probe, rule 4)")
        else:
            pd = d["probe"]
            if not isinstance(pd, dict):
                v.err("%s.probe" % path, "probe must be an object")
            else:
                probe = _probe(v, pd, "%s.probe" % path, service_ports=service_ports)
    if _MISSING in (src, ev):
        return None
    return PulseSignal(source=src, every_ms=ev, probe=probe)


def _process(v: _V, name: str, d: Any, path: str, *, ladders: set) -> Optional[Process]:
    v.unknown(d, {"launch", "supervisor", "watch", "services"}, path)
    v.strlen(name, NAME_MAX, "%s (process name)" % path)
    ld = v.req(d, "launch", path, "dict")
    launch = _grace_and_launch(v, ld, "%s.launch" % path) if ld is not _MISSING else None
    supd = v.req(d, "supervisor", path, "dict")
    supervisor, poll_ms = (None, _MISSING)
    if supd is not _MISSING:
        supervisor, poll_ms = _supervisor(v, supd, "%s.supervisor" % path)
    # process watch (P-levels)
    wd = v.req(d, "watch", path, "dict")
    pwatch: Dict[str, WatchLevel] = {}
    if wd is not _MISSING:
        if len(wd) > MAX_WATCH_LEVELS:
            v.err("%s.watch" % path, "levels %d > MAX_WATCH_LEVELS=%d"
                  % (len(wd), MAX_WATCH_LEVELS))
        for lname, lvd in wd.items():
            wl = _watch_level(v, lname, lvd, "%s.watch.%s" % (path, lname),
                              allowed_levels=PROCESS_LEVELS, ladders=ladders)
            if wl is not None:
                pwatch[lname] = wl
    # services
    svd = v.req(d, "services", path, "dict")
    services: Dict[str, Service] = {}
    if svd is not _MISSING:
        if len(svd) < 1:
            v.err("%s.services" % path, "process must have ≥ 1 service")
        if len(svd) > MAX_SERVICES:
            v.err("%s.services" % path, "services %d > MAX_SERVICES=%d"
                  % (len(svd), MAX_SERVICES))
        for sname, sd in svd.items():
            svc = _service(v, sname, sd, "%s.services.%s" % (path, sname), ladders=ladders)
            if svc is not None:
                services[sname] = svc

    # rule 11 (WARNING): single-service process with active P1 — P1 is inert
    if isinstance(svd, dict) and len(svd) == 1 and "P1_all_pulses_lost" in pwatch:
        if pwatch["P1_all_pulses_lost"].mode == "act":
            v.warn("%s.watch.P1_all_pulses_lost" % path,
                   "process with a SINGLE service: P1 is inert (indistinguishable from service L1, rule 11); "
                   "coverage = L1 ladder + P2")

    # rule 6: poll_ms ≤ min(eat_within_ms)/5 — checked at the Config level
    if launch is None or supervisor is None:
        return None
    return Process(name=name, launch=launch, supervisor=supervisor,
                   watch=pwatch, services=services)


# ════════════════════════════════════════════════════════════════════════════
#  Top level
# ════════════════════════════════════════════════════════════════════════════
def parse(raw: Any) -> Config:
    """Validate an already-parsed JSON object and return the typed model.
    Raises WdConfigError with ALL problems if something is wrong."""
    v = _V()
    if not isinstance(raw, dict):
        raise WdConfigError([Problem("<root>", "config root must be an object")])

    v.unknown(raw, {"schema", "framework", "actions", "ladders", "processes"}, "<root>")
    schema = v.req(raw, "schema", "<root>", "int")
    if schema is not _MISSING and schema != 2:
        v.err("schema", "only schema 2 is supported, got %r" % (schema,))

    # framework
    fw = v.req(raw, "framework", "<root>", "dict")
    framework = None
    if fw is not _MISSING:
        v.unknown(fw, {"transport", "observer", "gates", "pacing"}, "framework")
        tr = _transport(v, v.req(fw, "transport", "framework", "dict"), "framework.transport")
        ob = _observer(v, v.req(fw, "observer", "framework", "dict"), "framework.observer")
        ga = _gates(v, v.req(fw, "gates", "framework", "dict"), "framework.gates")
        pa = _pacing(v, v.req(fw, "pacing", "framework", "dict"), "framework.pacing")
        if None not in (tr, ob, ga, pa):
            framework = Framework(transport=tr, observer=ob, gates=ga, pacing=pa)

    # actions (catalog)
    ad = v.req(raw, "actions", "<root>", "dict")
    actions: Dict[str, Action] = {}
    if ad is not _MISSING:
        if len(ad) > MAX_ACTIONS:
            v.err("actions", "actions %d > MAX_ACTIONS=%d" % (len(ad), MAX_ACTIONS))
        for aname, aval in ad.items():
            a = _action(v, aname, aval, "actions.%s" % aname)
            if a is not None:
                actions[aname] = a

    # ladders (catalog) — names needed before services for @ resolution
    ladders_raw = v.req(raw, "ladders", "<root>", "dict")
    ladder_names: set = set(ladders_raw.keys()) if isinstance(ladders_raw, dict) else set()

    # processes
    pd = v.req(raw, "processes", "<root>", "dict")
    processes: Dict[str, Process] = {}
    if pd is not _MISSING:
        if len(pd) > MAX_PROCESSES:
            v.err("processes", "processes %d > MAX_PROCESSES=%d" % (len(pd), MAX_PROCESSES))
        for pname, pval in pd.items():
            p = _process(v, pname, pval, "processes.%s" % pname, ladders=ladder_names)
            if p is not None:
                processes[pname] = p

    # ladders — parse steps (after action names are known)
    ladders: Dict[str, Ladder] = {}
    if isinstance(ladders_raw, dict):
        if len(ladders_raw) > MAX_LADDERS:
            v.err("ladders", "ladders %d > MAX_LADDERS=%d" % (len(ladders_raw), MAX_LADDERS))
        for lname, larr in ladders_raw.items():
            steps = _ladder_steps(v, larr, "ladders.%s" % lname)
            if steps is not None:
                ladders[lname] = Ladder(name=lname, steps=steps)
                # rule 3: @-actions in steps resolve to the actions catalog
                for i, st in enumerate(steps):
                    if not st.is_verb and st.ref not in actions:
                        v.err("ladders.%s[%d].do" % (lname, i),
                              "@%s not found in the actions catalog" % st.ref)

    # ── cross-section checks ──
    _cross_checks(v, actions, ladders, processes)

    if v.errors:
        raise WdConfigError(v.errors)
    return Config(schema=schema, framework=framework, actions=actions,
                  ladders=ladders, processes=processes, warnings=list(v.warnings))


def _iter_services(processes: Dict[str, Process]):
    for pname, p in processes.items():
        for sname, s in p.services.items():
            yield pname, p, sname, s


def _cross_checks(v: _V, actions: Dict[str, Action], ladders: Dict[str, Ladder],
                  processes: Dict[str, Process]) -> None:
    # P-level (P1/P2) → ladder ONLY of restart_process verbs: the process
    # level has no {service} target, an @action would be unaddressable (parity with C, review E6).
    for pname, p in processes.items():
        for lname, lvl in p.watch.items():
            lad = ladders.get(lvl.ladder)
            if lad is None:
                continue
            for i, st in enumerate(lad.steps):
                if not st.is_verb:
                    v.err("processes.%s.watch.%s.ladder" % (pname, lname),
                          "ladder @%s for a process level contains a service "
                          "action @%s — a P-level has no service target (only restart_process)"
                          % (lvl.ladder, st.ref))
                    break

    # rule 7: service names are GLOBALLY unique across all processes
    seen: Dict[str, str] = {}
    for pname, _p, sname, _s in _iter_services(processes):
        if sname in seen:
            v.err("processes.%s.services.%s" % (pname, sname),
                  "service name %r is already used by process %r; the text_v1 datagram has no "
                  "process qualifier — names must be globally unique (rule 7)"
                  % (sname, seen[sname]))
        else:
            seen[sname] = pname

    # rule 10: file templates of request_file actions are unique
    file_of: Dict[str, str] = {}
    for aname, a in actions.items():
        if a.type == "request_file":
            f = a.params.get("file")
            if isinstance(f, str):
                if f in file_of:
                    v.err("actions.%s.file" % aname,
                          "file template %r matches actions.%s (ambiguous consumption, rule 10)"
                          % (f, file_of[f]))
                else:
                    file_of[f] = aname

    # which services use each action (via their ladders) — for rule 6
    #   action -> min(dead_after_ms of services whose ladder contains this action)
    def steps_of(ladder_ref: str, inline: Optional[Ladder]) -> List[LadderStep]:
        if inline is not None:
            return inline.steps
        lad = ladders.get(ladder_ref)
        return lad.steps if lad else []

    action_min_dead: Dict[str, int] = {}
    for _pn, _p, _sn, s in _iter_services(processes):
        da = None
        if "L1_pulse_lost" in s.watch:
            da = s.watch["L1_pulse_lost"].params.get("dead_after_ms")
        if not isinstance(da, int):
            continue
        used_actions = set()
        for wl in s.watch.values():
            for st in steps_of(wl.ladder, wl.inline):
                if not st.is_verb:
                    used_actions.add(st.ref)
        for aref in used_actions:
            prev = action_min_dead.get(aref)
            action_min_dead[aref] = da if prev is None else min(prev, da)

    # rule 6: rate_limit reachability for request_file:  per_ms/max ≥ startup_ms + min(dead)
    for aname, a in actions.items():
        if a.type != "request_file":
            continue
        rl = a.rate_limit
        su = a.params.get("startup_ms")
        min_dead = action_min_dead.get(aname)
        if isinstance(rl.max, int) and isinstance(rl.per_ms, int) and rl.max > 0 \
                and isinstance(su, int) and isinstance(min_dead, int):
            floor = su + min_dead
            if rl.per_ms / rl.max < floor:
                v.err("actions.%s.rate_limit" % aname,
                      "limit is inert: per_ms/max (%.0f) < startup_ms+min(dead_after_ms) (%d); "
                      "threshold unreachable within a real recreation cycle (rule 6)"
                      % (rl.per_ms / rl.max, floor))

    # rule 6: poll_ms ≤ min(eat_within_ms of all request_file)/5
    eat_vals = [a.params.get("eat_within_ms") for a in actions.values()
                if a.type == "request_file"]
    eat_vals = [e for e in eat_vals if isinstance(e, int)]
    if eat_vals:
        min_eat = min(eat_vals)
        for pname, p in processes.items():
            poll = p.supervisor.poll_ms
            if isinstance(poll, int) and poll > min_eat / 5:
                v.err("processes.%s.supervisor.poll_ms" % pname,
                      "poll_ms (%d) must be ≤ min(eat_within_ms)/5 (%.0f), otherwise "
                      "a file request may linger longer than eat_within_ms (rule 6)"
                      % (poll, min_eat / 5))


def loads(text: str) -> Config:
    try:
        raw = json.loads(text)
    except ValueError as e:
        raise WdConfigError([Problem("<json>", "cannot parse JSON: %s" % e)])
    return parse(raw)


def load(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as f:
        return loads(f.read())
