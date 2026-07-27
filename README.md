# svc-watch

A config-driven **service supervisor + watchdog**. One declarative config (`svc_watch.conf`)
is the single source of truth for which processes/services exist, how each proves it is alive,
what counts as "dead", and exactly how to escalate — nothing is hard-coded (fail-closed).

It has **two sides that read the same config**:

- a **Python library** (`src/svc_watch/`) that services embed to *emit* heartbeats and run an
  in-process *supervisor*;
- a small **C observer** (`daemon/`) that *judges* health over a unix datagram socket and
  *issues actions* (recreate a service, or restart its owning process).

> Status: extracted from the `rut_scripts` project (RUT956 router bench) into this standalone
> repository. See `docs/` for the full design, protocol, and config guide, and
> `examples/rut-integration/` for how it was originally embedded.

## Layout
```
src/svc_watch/     Python library (contracts, core, config, runtime, emit, adapters)
daemon/            C observer (svc_watchdog.c + build) — see daemon/BUILD.md
docs/              architecture, protocol, config guide, embedding; docs/design/ = spec; docs/history/ = v1
tests/             pytest suite (contract / invalid_config / extension / unify)
examples/          inmemory_demo (no OS), add-a-service, add-a-process, configs; rut-integration/ (reference)
```

## Quickstart (no hardware)
```sh
python -m pip install -e .[test]      # or: pip install pytest
python -m pytest tests -q             # contract + validator suite
python examples/inmemory_demo/run.py  # runs the whole supervisor on a FakeClock, prints OK
```

Validate a config without starting anything:
```sh
python -c "import sys;sys.path.insert(0,'src');from svc_watch import config;config.load('examples/configs/svc_watch.single-service.conf');print('OK')"
```

## Docs
- `docs/README.md` — documentation index
- `docs/ARCHITECTURE.md`, `docs/PROTOCOL.md`, `docs/CONFIG_GUIDE.md`, `docs/EMBEDDING.md`, `docs/LOGGING.md`, `docs/CHEATSHEET.md`
- `docs/design/` — the design spec (variability axes, framework rules, v1→v2 trace)
- `daemon/BUILD.md` — build the C observer
