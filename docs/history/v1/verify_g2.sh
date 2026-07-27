#!/bin/bash
# verify_g2.sh — DoD G2: хост-тесты зелёные, mipsel-бинарь <150КБ, qemu-сценарий пройден.
# Всё выполняется в ubuntu:22.04 контейнере (docker_build.sh); стенд НЕ трогается.
set -u
SRC="$(cd "$(dirname "$0")/src" && pwd)"
# на Windows path для docker -v; MSYS_NO_PATHCONV — чтобы Git Bash не переписывал /work
DSRC=$(echo "$SRC" | sed 's|^C:|/c|; s|\\|/|g')

# том wdsdk кэширует SDK/json-c между прогонами (/opt)
MSYS_NO_PATHCONV=1 docker run --rm -v "$DSRC:/work" -v wdsdk:/opt -w /work ubuntu:22.04 bash docker_build.sh || { echo "G2 FAIL"; exit 1; }

[ -f "$SRC/svc_watchdog.mipsel" ] || { echo "G2 FAIL: нет mipsel-бинаря"; exit 1; }
SIZE=$(stat -c%s "$SRC/svc_watchdog.mipsel" 2>/dev/null || stat --format=%s "$SRC/svc_watchdog.mipsel")
[ "$SIZE" -lt 153600 ] || { echo "G2 FAIL: бинарь $SIZE >= 150КБ"; exit 1; }
echo "mipsel binary: $SIZE bytes (<150KB)"
echo "G2 OK"
exit 0
