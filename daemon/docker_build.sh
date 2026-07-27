#!/bin/bash
# Inside ubuntu:22.04 (see verify.sh). Runs the whole E4 gate:
#  1) host build (gcc+libjson-c-dev) + sanity   2) host unit tests (test_host.sh)
#  3) OpenWrt SDK 21.02 ramips/mt76x8 toolchain  4) json-c 0.15 cross from source
#  5) cross build svc_watchdog.mipsel + size<150KB  6) qemu-mipsel smoke (v2 config)
set -e
cd /work

echo "=== [1/6] host deps + build ==="
export DEBIAN_FRONTEND=noninteractive
apt-get -qq update >/dev/null
apt-get -qq install -y gcc make cmake libjson-c-dev python3 wget xz-utils qemu-user file >/dev/null
make -s clean host
./svc_watchdog /nonexistent.conf 2>&1 | grep -q "cannot parse" && echo "binary sanity ok"

echo "=== [2/6] host unit tests ==="
bash test_host.sh

SDK_URL=https://downloads.openwrt.org/releases/21.02.0/targets/ramips/mt76x8/openwrt-sdk-21.02.0-ramips-mt76x8_gcc-8.4.0_musl.Linux-x86_64.tar.xz
SDK_DIR=/opt/sdk
echo "=== [3/6] OpenWrt SDK toolchain ==="
if [ ! -d $SDK_DIR/staging_dir ]; then
    mkdir -p $SDK_DIR
    wget -q $SDK_URL -O /tmp/sdk.tar.xz
    tar -xf /tmp/sdk.tar.xz -C $SDK_DIR --strip-components=1
fi
TC=$SDK_DIR/staging_dir/toolchain-mipsel_24kc_gcc-8.4.0_musl
export STAGING_DIR=$SDK_DIR/staging_dir
CROSS_CC=$TC/bin/mipsel-openwrt-linux-musl-gcc
CROSS_STRIP=$TC/bin/mipsel-openwrt-linux-musl-strip
$CROSS_CC --version | head -1

echo "=== [4/6] json-c 0.15 cross from source ==="
JC=/opt/jsonc2
if [ ! -f $JC/lib/libjson-c.so ]; then
    wget -q https://s3.amazonaws.com/json-c_releases/releases/json-c-0.15.tar.gz -O /tmp/jsonc-src.tgz \
      || wget -q https://github.com/json-c/json-c/archive/refs/tags/json-c-0.15-20200726.tar.gz -O /tmp/jsonc-src.tgz
    rm -rf /tmp/jcsrc && mkdir -p /tmp/jcsrc && tar -xzf /tmp/jsonc-src.tgz -C /tmp/jcsrc --strip-components=1
    mkdir -p /tmp/jcbuild && cd /tmp/jcbuild
    cmake /tmp/jcsrc -DCMAKE_SYSTEM_NAME=Linux -DCMAKE_SYSTEM_PROCESSOR=mipsel \
        -DCMAKE_C_COMPILER=$CROSS_CC -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_TESTING=OFF -DBUILD_STATIC_LIBS=OFF -DDISABLE_WERROR=ON \
        -DCMAKE_INSTALL_PREFIX=$JC >/dev/null
    make -s -j4 >/dev/null 2>&1
    make -s install >/dev/null
    cd /work
fi
ls $JC/lib/libjson-c.so* >/dev/null || { echo "FAIL: json-c cross build"; exit 1; }

echo "=== [5/6] cross build ==="
make -s cross CROSS_CC=$CROSS_CC CROSS_STRIP=$CROSS_STRIP JSONC_INC=$JC/include JSONC_LIB=$JC/lib
file svc_watchdog.mipsel
SIZE=$(stat -c%s svc_watchdog.mipsel)
echo "mipsel binary: $SIZE bytes"
[ $SIZE -lt 153600 ] || { echo "FAIL: binary >= 150KB"; exit 1; }

echo "=== [6/6] qemu smoke (pulse->silence->action->P2->restart) ==="
SYSROOT=/tmp/qemu_root
rm -rf $SYSROOT && mkdir -p $SYSROOT/lib $SYSROOT/usr/lib
cp $TC/lib/libc.so $SYSROOT/lib/
cp $TC/lib/libgcc_s.so.1 $SYSROOT/lib/
ln -sf libc.so $SYSROOT/lib/ld-musl-mipsel-sf.so.1
cp -a $JC/lib/libjson-c.so.5* $SYSROOT/usr/lib/
Q=/tmp/qsmoke; rm -rf $Q && mkdir -p $Q
cat > $Q/wd.conf <<EOF
{
  "schema": 2,
  "framework": {
    "transport": { "type": "unix_datagram", "unix_datagram": { "socket": "$Q/wd.sock", "format": "text_v1" } },
    "observer": { "mode": "act", "tick_ms": 100, "pause_file": "$Q/pause", "oom_adj": 0,
      "log": { "file": "$Q/wd.log", "rotate_kb": 100, "keep": 2, "fsync": false },
      "quiet": { "paused_ms": 60000, "unknown_ms": 60000 } },
    "gates": { "min_free_mb": 8, "max_load1": 500.0, "recheck_ms": 100, "force_after_ms": 2000, "alarm_mb": 5 },
    "pacing": { "type": "fixed", "delay_ms": 100 }
  },
  "actions": {
    "recreate": { "type": "request_file", "file": "$Q/crash_{service}", "eat_within_ms": 400, "startup_ms": 300,
      "rate_limit": { "max": 6, "per_ms": 100000, "on_exceeded": "cooldown", "cooldown_ms": 60000 } }
  },
  "ladders": {
    "soft": [ { "do": "@recreate" } ],
    "po": [ { "do": "restart_process" } ]
  },
  "processes": {
    "unified": {
      "launch": { "type": "init_script", "script": "$Q/fake_initd", "pidfile": "$Q/proc.pid",
        "grace": { "type": "fixed", "ms": 300 },
        "restart_rate_limit": { "max": 5, "per_ms": 600000, "on_exceeded": "cooldown", "cooldown_ms": 300000 } },
      "supervisor": { "poll_ms": 100, "stop_timeout_ms": 2000, "min_stable_ms": 500,
        "max_consecutive_start_failures": 3, "backoff": { "start_ms": 100, "factor": 2, "cap_ms": 1000 },
        "log": { "file": "$Q/u.log", "rotate_kb": 100, "keep": 2, "fallbacks": [] } },
      "watch": { "P1_all_pulses_lost": { "mode": "act", "ladder": "@po" },
                 "P2_request_stuck": { "mode": "act", "ladder": "@po" } },
      "services": {
        "alpha": { "start": { "type": "python", "entry": "m:f" },
          "signals": { "pulse": { "from": "loop", "every_ms": 100 } },
          "watch": { "L1_pulse_lost": { "mode": "act", "dead_after_ms": 400, "ladder": "@soft" } } },
        "beta": { "start": { "type": "python", "entry": "m:f" },
          "signals": { "pulse": { "from": "loop", "every_ms": 100 } },
          "watch": { "L1_pulse_lost": { "mode": "act", "dead_after_ms": 400, "ladder": "@soft" } } }
      }
    }
  }
}
EOF
echo 1 > $Q/proc.pid
printf '#!/bin/sh\necho called >> %s/initd_calls\n' $Q > $Q/fake_initd && chmod +x $Q/fake_initd
qemu-mipsel -L $SYSROOT ./svc_watchdog.mipsel $Q/wd.conf & QPID=$!
sleep 0.5
python3 -c "import socket;s=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM);s.sendto(b'alpha','$Q/wd.sock');print('beat sent ok')"
python3 - "$Q/wd.sock" <<'PY' & BPID=$!
import socket, sys, time
s=socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM); t0=time.time()
while time.time()-t0 < 5.0:
    try:
        s.sendto(b'beta', sys.argv[1])
        if time.time()-t0 < 1.0: s.sendto(b'alpha', sys.argv[1])
    except OSError: pass
    time.sleep(0.1)
PY
sleep 5.5
kill $QPID $BPID 2>/dev/null || true
echo "--- qemu wd.log ---"; cat $Q/wd.log
grep -q "wd_start"                       $Q/wd.log || { echo QEMU-FAIL wd_start; exit 1; }
grep -q "no_pulse name=alpha"            $Q/wd.log || { echo QEMU-FAIL no_pulse; exit 1; }
grep -q "action target=alpha"           $Q/wd.log || { echo QEMU-FAIL action; exit 1; }
grep -q "request_stuck"                  $Q/wd.log || { echo QEMU-FAIL P2; exit 1; }
grep -q "restart_process process=unified" $Q/wd.log || { echo QEMU-FAIL restart; exit 1; }
grep -q called $Q/initd_calls            || { echo QEMU-FAIL initd; exit 1; }
echo "QEMU SMOKE: OK"
echo "G2-CONTAINER: ALL OK"
