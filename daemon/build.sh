#!/bin/bash
# build.sh — rebuild AND self-test the observer binary. Run this only if you change
# svc_watchdog.c (the router ships the prebuilt svc_watchdog.mipsel, so normal deployment never
# needs it). Everything runs in an ubuntu:22.04 container (docker_build.sh) — nothing on your
# machine or the router is touched. It does: native build + host tests (test_host.sh) +
# cross-compile for the router's mipsel CPU + a qemu smoke test, then checks the size (<150KB).
set -u
SRC="$(cd "$(dirname "$0")" && pwd)"
DSRC=$(echo "$SRC" | sed 's|^C:|/c|; s|\\|/|g')

# the wdsdk_v2 volume caches the OpenWrt SDK and json-c between runs (first run downloads them)
MSYS_NO_PATHCONV=1 docker run --rm -v "$DSRC:/work" -v wdsdk_v2:/opt -w /work ubuntu:22.04 \
    bash docker_build.sh || { echo "BUILD FAILED"; exit 1; }

[ -f "$SRC/svc_watchdog.mipsel" ] || { echo "BUILD FAILED: mipsel binary missing"; exit 1; }
SIZE=$(stat -c%s "$SRC/svc_watchdog.mipsel" 2>/dev/null || stat --format=%s "$SRC/svc_watchdog.mipsel")
[ "$SIZE" -lt 153600 ] || { echo "BUILD FAILED: binary $SIZE >= 150KB"; exit 1; }
echo "mipsel binary: $SIZE bytes (<150KB)"
echo "BUILD OK — svc_watchdog.mipsel is ready to ship"
exit 0
