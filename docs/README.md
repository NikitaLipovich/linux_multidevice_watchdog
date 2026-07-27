# svc_watch — a config-driven supervision framework

`svc_watch` keeps a fleet of services alive on a resource-tiny router. One file,
`svc_watch.conf`, is the **single source of truth** for every behaviour: which processes and
services exist, how liveness is proven, what counts as "dead", and exactly how to escalate.

Two sides read that same file:

- a small **C daemon** (`daemon/svc_watchdog.c`) — the **observer**: it judges health
  (levels L1/L2/P1/P2) and issues actions (recreate a service, restart a process);
- the **Python library** (`svc_watch`) — the **emission + supervision** side that services
  embed: it sends heartbeats and runs the in-process supervisor.

The decision core is **OS-free and clock-injected**, so the identical logic runs headless
in-memory (for tests) and on the real router.

---

## Quickstart (30 seconds, no router, no OS deps)

```sh
# from the repo root
python external-storage-contents/svc_watch/examples/inmemory_demo/run.py
```
It drives the whole framework on simulated time — pulses → silence → ladder decision →
in-memory action → recovery, an L2 freeze, and a second process whose group goes silent and is
restarted addressably. It prints `OK`.

Run the test suite:
```sh
python -m pytest external-storage-contents/svc_watch/tests -q      # 70 passed, 1 skipped
```

Validate a config without running anything:
```sh
python -c "import sys; sys.path.insert(0,'external-storage-contents'); \
from svc_watch import config; config.load('external-storage-contents/svc_watch/conf/svc_watch.conf'); print('OK')"
```

---

## Documentation

| Doc | What it covers |
|---|---|
| [CONFIG_GUIDE.md](CONFIG_GUIDE.md) | Every config section, the 17 validator rules, signal↔level pairing, ladders, and the fixed judgment/escalation **sequences**. Start here to write or edit a config. |
| [EMBEDDING.md](EMBEDDING.md) | How to embed the framework in a Python process: emit pulses/activity, run the supervisor, wire a process, write service factories — and how config links map to code. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, the emission↔observer split, judgment order, escalation, and the live-bench topology — **6 diagrams**. |
| [PROTOCOL.md](PROTOCOL.md) | The `text_v1` wire format, states/episodes, and the test hooks. |
| [INSTALL_ROUTER.md](INSTALL_ROUTER.md) | Port (cross-build the daemon) and install on the router (deploy the pair), plus the proven observer-only swap and rollback. |
| [LOGGING.md](LOGGING.md) | By what rule and where logs are written (observer vs supervisor journals, SD vs tmpfs, rotation, the `up=` stamp). |
| [CHEATSHEET.md](CHEATSHEET.md) | One-page ops reference: levels, ladders, operator/dev commands, troubleshooting, glossary. |
| [design/](design/) | The deep rationale: variability axes, framework rules (FR-*), the v1→v2 trace, and the config draft. |

Templates:
- [../examples/new_process_template/](../examples/new_process_template/) — copy-paste recipe to
  add a **new process** and tie it to the daemon (config block + composition root + init script).
- [../examples/unified_rut/](../examples/unified_rut/) — the real composition root (consumer *A*)
  and the live deploy runbook.
- [../examples/inmemory_demo/](../examples/inmemory_demo/) — the no-OS demo (consumer *B*).

---

## Repository layout

```
svc_watch/
├── config.py contracts.py core.py runtime.py emit.py __init__.py   # the library
├── adapters/            # one file per type (real + inmemory); registry, no core switch
├── daemon/              # C observer: svc_watchdog.c + Makefile + Docker build + verify.sh
├── conf/svc_watch.conf  # canonical config (deploy → /etc/svc_watch.conf)
├── tests/               # invalid_config · contract (×2 impls) · extension
├── examples/            # inmemory_demo · unified_rut · new_process_template
└── docs/                # this documentation (+ docs/design rationale)
```

On the router only the **library package**, the **built daemon binary**, and
**`/etc/svc_watch.conf`** are deployed; `tests/`, `docs/`, `daemon/` (source) and `examples/`
are development-only.

---

## Status

Proven on the live bench: the v2 observer runs in `act` mode, correctly judges the real
service stream (mute-cycle: silence → recreate → recovered), and handled a full live
**Bundle-flash** of arm-unit R4 — `summary=ok`, the `config_server` L2 episode ran
`active → idle` for ~5.5 min with **zero false restarts**.
