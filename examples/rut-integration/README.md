# rut-integration — reference: how svc-watch was embedded in the RUT956 project

> **Not part of the framework.** This folder is a *reference snapshot* of how `svc-watch` was
> wired into its origin project (`rut_scripts`, the RUT956 router bench). Nothing under
> `../../src/` or `../../daemon/` depends on anything here. Keep it as a worked example; do not
> import from it in framework code.

## What's here

| File | What it is |
|---|---|
| `svc_watch.conf` | the **live RUT config** (unified process, 4 bench services). Also serves as the single full valid config the framework test-suite loads as its base (`../../tests/conftest.py`). |
| `startup.sh` | the **full RUT bring-up** run from `/etc/rc.local` on every boot — waits for the SD card, copies the config to `/etc`, drops the prebuilt observer binary, installs procd init scripts, starts services + observer. RUT-specific; illustrative, not the framework installer. |
| `init.d-svc_watchdog` | procd init script for the C observer on the router. |
| `unified_rut_main.py` | the RUT composition root that runs all 4 services in one asyncio process and wires them to the observer (the real embedding of the library). |
| `DEPLOY_E5.md` | deployment notes for the unified RUT process. |

### v1.2 → v2 migration bundle (RUT-specific)
| File | What it is |
|---|---|
| `svc_watch_compat.py` | maps the single v2 `svc_watch.conf` into the **v1.2 view** the retired runtime expected — a migration aid tied to this project's history (depends only on `svc_watch.config`). |
| `wd_runtime.py`, `wd_beat.py` | the RUT project's **retired v1.2 Python runtime** (supervisor + heartbeat sender). Superseded by the `svc_watch` library; kept here so the migration tests run and as historical reference. |
| `tests/` | migration tests proving the v1.2 view reproduces the exact values the old `svc_watchdog.conf` carried. Run separately from the framework suite: `python -m pytest examples/rut-integration/tests -q`. |

## Not copied on purpose

- **`run_services.py`** — the RUT service composition root (the actual service *logic*: udp_logger,
  data_uploader, config_server, ws_bridge). It lives in the parent project
  (`rut_scripts/external-storage-contents/run_services.py`) and is **out of scope** for this framework
  repo. The config here references its factories by name (`run_services:start_udp_logger`, …) purely
  as the embedding example — the framework never imports it.
- Compiled observer binaries (`svc_watchdog.mipsel`) — build from `../../daemon/svc_watchdog.c`.

## The bigger picture
For the framework itself, start at the repo `../../README.md` and `../../docs/`. This folder only
answers "what did a real deployment look like?".
