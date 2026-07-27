# svc_watch_daemon — the C observer: source + how to rebuild it

The observer is a single-file C program (`svc_watchdog.c`) that links libjson-c (resident on
RutOS) and targets `mipsel_24kc` musl soft-float — the RUT956 CPU. **The prebuilt binary
`svc_watchdog.mipsel` is checked in and ships with the payload, so a normal deployment never
builds anything.** You only rebuild if you change `svc_watchdog.c`.

## Rebuild (one command)

```sh
bash build.sh
```

`build.sh` does everything in an `ubuntu:22.04` container (via `docker_build.sh`) — nothing on
your machine or the router is touched:

1. compile natively and run the **host tests** (`test_host.sh`),
2. cross-compile for the router's mipsel CPU (OpenWrt SDK 21.02 + a cross-built json-c),
3. run a **qemu smoke test** (boot the mipsel binary under qemu, feed it heartbeats, confirm it
   escalates correctly),
4. check the binary is under 150 KB.

Expected tail: `mipsel binary: ~31500 bytes (<150KB)` then `BUILD OK`. Requirements: Docker
running; network on the first run (the SDK + json-c are then cached in the `wdsdk_v2` Docker
volume, so later runs are fast).

## Do the tests actually check anything?

Yes — they are the daemon's real test suite, not filler:

- **`test_host.sh`** feeds the daemon ~13 broken configs and asserts it **rejects** each with
  the right error (unknown key, unknown type, `tries:0`, `stop` on a process restart,
  `dead_after_ms` too small, a dangling `@`-ref, a duplicate service name, …), then runs three
  **behavioural** scenarios against the running binary: a silent service → recreate → restart,
  two processes where only the silent one is restarted, and a rate-limit that trips into
  cooldown. These caught real bugs while the daemon was written.
- **the qemu smoke** (inside `docker_build.sh`) proves the *cross-compiled* binary actually runs
  on the target architecture and reacts to live heartbeats — a native build passing is not
  enough on its own.

If you never touch `svc_watchdog.c`, you never run any of this; the shipped binary is what runs.

## Files

| File | Role |
|---|---|
| `svc_watchdog.c` | the observer: config parser + strict validator + the judgment loop |
| `Makefile` | `make host` (native binary for the tests) · `make cross` (the mipsel binary) |
| `docker_build.sh` | runs inside the container: fetches the SDK + json-c, builds, tests, qemu |
| `test_host.sh` | the host test suite (validator negatives + the three behavioural scenarios) |
| `build.sh` | the one command above — launches the container and checks the size |
| `svc_watchdog.mipsel` | the prebuilt binary that ships to the router |

## Deploy

`../startup.sh` (the single bring-up script, run on boot from `/etc/rc.local`) copies
`svc_watchdog.mipsel` into place and starts it under procd. After a rebuild: re-upload the
payload (`python upload.py uploadcode`) and reboot the router (or run `startup.sh` by hand).
Config and logic reference: `../svc_watch/README.md`.
