# svc_watch — Porting & installing on the router

Two phases: **port** (cross-build the C observer for the router's mipsel-musl CPU) and
**install** (deploy the pair — daemon + config — and wire it under procd). The Python library
is pure stdlib and needs no build.

Bench facts: RUT956 at `192.168.1.1`, `$RD = /usr/local/home/root/external-storage-contents`,
SD card mounted (`/dev/sda1` → `/overlay`, and `/mnt/sda1` for logs), `/etc` is on the SD
overlay (so `/etc/svc_watch.conf` persists across reboots).

---

## 1. Port — cross-build the daemon (off the router, in Docker)

The daemon links `libjson-c` (resident on RutOS) and targets `mipsel_24kc` musl soft-float
(OpenWrt SDK 21.02, ramips/mt76x8). One command does host build + host tests + cross build +
size check + a qemu smoke, all in an `ubuntu:22.04` container:

```sh
bash external-storage-contents/svc_watch/daemon/verify.sh
```
Expected tail: `mipsel binary: ~31500 bytes (<150KB)` followed by `G2 OK`. It produces
`external-storage-contents/svc_watch/daemon/svc_watchdog.mipsel`. The OpenWrt SDK and a
cross-built json-c are cached in the `wdsdk_v2` Docker volume, so only the first run is slow.

Requirements: Docker running; network for the first SDK/json-c download. The stand is **not**
touched by the build.

---

## 2. Install — two paths

### 2a. Observer-only swap (recommended, proven live)

`text_v1`, the socket path, the service names, and the timings are **identical** across
versions. So if a `text_v1`-compatible Python side is already emitting (e.g. the existing
`run_services.py` + `wd_beat`), you migrate **only the C observer** and leave the Python side —
including the flash path — untouched.

The exact, verified runbook (backups, deploy, negative test, init.d switch, restart, smoke,
rollback) is **`examples/unified_rut/DEPLOY_E5.md`** — follow it step by step. In short:

```sh
RK=/tmp/rut_key; H=root@192.168.1.1; RD=/usr/local/home/root/external-storage-contents
# backups → deploy pair → switch init.d PROG/CONF → restart → verify
scp -i $RK external-storage-contents/svc_watch/daemon/svc_watchdog.mipsel $H:$RD/svc_watchdog.v2
scp -i $RK external-storage-contents/svc_watch/conf/svc_watch.conf        $H:/etc/svc_watch.conf
ssh -i $RK $H 'sed -i "s#^PROG=.*#PROG='$RD'/svc_watchdog.v2#; s#^CONF=.*#CONF=/etc/svc_watch.conf#" \
   /etc/init.d/svc_watchdog && /etc/init.d/svc_watchdog restart'
```
Then verify: `wd_start processes=… mode=act` in `wd.log`, health 200, no false `no_pulse` after
grace. Proven on the bench end-to-end (mute cycle + a live Bundle-flash R4, `summary=ok`, zero
false restarts).

### 2b. Full install (fresh box, or migrating the Python side too)

1. **Log dir:** `mkdir -p /mnt/sda1/crash_logs` on the router.
2. **Config:** deploy `svc_watch/conf/svc_watch.conf` → `/etc/svc_watch.conf`. Adjust paths for
   your box if they differ (socket, log files, init.d script). It is validated on both sides at
   startup — a bad file refuses to start (fail-closed).
3. **Daemon:** deploy `svc_watchdog.mipsel` → `$RD/svc_watchdog` (or `.v2`); install its procd
   init script (`START=98 STOP=11`, `respawn 3600 5 0`) with `command $PROG /etc/svc_watch.conf`.
4. **Python library:** deploy the `svc_watch/` package under `$RD` so `PYTHONPATH=$RD` makes
   `import svc_watch` resolve. The supervised process(es) embed it (see `docs/EMBEDDING.md` and
   `examples/unified_rut/main.py`), each under its own procd init script (see
   `examples/new_process_template/init.d-newproc.template`).
5. **Enable & start:** `enable` + `start` the daemon and each process init script.

Only these deploy to the router: the `svc_watch/*.py` package, the built daemon binary, and
`/etc/svc_watch.conf`. `tests/`, `docs/`, `daemon/` source and `examples/` stay in the repo.

---

## 3. Rollback

Observer swap: restore the init script backup and restart — it points back at the previous
binary + config:
```sh
ssh -i /tmp/rut_key root@192.168.1.1 \
  'cp -a /etc/init.d/svc_watchdog.bak_v1.2 /etc/init.d/svc_watchdog && /etc/init.d/svc_watchdog restart'
```
Full details and the earlier-version backup paths are in `examples/unified_rut/DEPLOY_E5.md`.

---

## 4. Operating

- **Pause the observer:** `touch /tmp/wd_pause` (remove to resume).
- **Force-restart a service by hand:** `touch /tmp/svc_crash_<service>` (the supervisor eats it).
- **Observe-only mode:** set `framework.observer.mode` to `"log"` — the observer judges and logs
  but issues no actions. Per-level `mode:"log"` does the same for a single level.
- **Where to look:** the observer journal and the supervisor journal on the SD card — see
  `docs/LOGGING.md`.
