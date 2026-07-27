#!/bin/bash
# Runs INSIDE the ubuntu:22.04 container (see verify_g2.sh). Does everything G2:
#  1) host build (gcc + libjson-c-dev) + host unit tests (test_host.sh)
#  2) OpenWrt SDK 21.02.0 ramips/mt76x8 toolchain (mipsel_24kc musl SOFT-FLOAT)
#  3) json-c 0.15 headers (source) + prebuilt libjson-c.so.5 (openwrt feed ipk)
#  4) cross build svc_watchdog.mipsel + size check
#  5) qemu-mipsel smoke: пульс→тишина→crash-файл→эскалация по логу
set -e
cd /work

echo "=== [1/5] host deps + build ==="
export DEBIAN_FRONTEND=noninteractive
apt-get -qq update >/dev/null
apt-get -qq install -y gcc make cmake libjson-c-dev python3 wget xz-utils qemu-user file >/dev/null
make -s clean host
./svc_watchdog /nonexistent.conf 2>&1 | grep -q "cannot parse" && echo "binary sanity ok"

echo "=== [2/5] host unit tests ==="
bash test_host.sh

SDK_URL=https://downloads.openwrt.org/releases/21.02.0/targets/ramips/mt76x8/openwrt-sdk-21.02.0-ramips-mt76x8_gcc-8.4.0_musl.Linux-x86_64.tar.xz
SDK_DIR=/opt/sdk
echo "=== [3/5] OpenWrt SDK toolchain ==="
if [ ! -d $SDK_DIR/staging_dir ]; then
    mkdir -p $SDK_DIR
    wget -q $SDK_URL -O /tmp/sdk.tar.xz
    tar -xf /tmp/sdk.tar.xz -C $SDK_DIR --strip-components=1
fi
TC=$(echo $SDK_DIR/staging_dir/toolchain-mipsel_24kc_gcc-8.4.0_musl)
export STAGING_DIR=$SDK_DIR/staging_dir   # toolchain wrapper requires it
CROSS_CC=$TC/bin/mipsel-openwrt-linux-musl-gcc
CROSS_STRIP=$TC/bin/mipsel-openwrt-linux-musl-strip
$CROSS_CC --version | head -1

echo "=== json-c 0.15: CROSS-сборка из исходников (feed'овские .so — sstrip'нуты, ld их не ест) ==="
# Линкуемся со СВОЕЙ libjson-c.so (SONAME libjson-c.so.5); на устройстве в
# рантайме резолвится РОДНАЯ резидентная /usr/lib/libjson-c.so.5 — та же 0.15.
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

echo "=== [4/5] cross build ==="
echo "--- diag: $JC/lib ---"; ls -la $JC/lib; file $JC/lib/* || true
$TC/bin/mipsel-openwrt-linux-musl-readelf -h $JC/lib/libjson-c.so.5* 2>&1 | grep -E "Class|Machine|Flags" || true
make -s cross CROSS_CC=$CROSS_CC CROSS_STRIP=$CROSS_STRIP JSONC_INC=$JC/include JSONC_LIB=$JC/lib
file svc_watchdog.mipsel
SIZE=$(stat -c%s svc_watchdog.mipsel)
echo "mipsel binary: $SIZE bytes"
[ $SIZE -lt 153600 ] || { echo "FAIL: binary >= 150KB"; exit 1; }

echo "=== [5/5] qemu smoke (пульс→тишина→crash→эскалация) ==="
SYSROOT=/tmp/qemu_root
rm -rf $SYSROOT && mkdir -p $SYSROOT/lib $SYSROOT/usr/lib
cp $TC/lib/libc.so $SYSROOT/lib/
cp $TC/lib/libgcc_s.so.1 $SYSROOT/lib/     # soft-float double-эмуляция (на RUT есть /lib/libgcc_s.so.1)
ln -sf libc.so $SYSROOT/lib/ld-musl-mipsel-sf.so.1
cp -a $JC/lib/libjson-c.so.5* $SYSROOT/usr/lib/
Q=/tmp/qsmoke; rm -rf $Q && mkdir -p $Q
# v1.2 fail-closed: конфиг ПОЛНЫЙ — каждый читаемый ключ обязателен
cat > $Q/wd.conf <<EOF
{
  "socket": "$Q/wd.sock", "beat_interval_ms": 100, "tick_ms": 100,
  "pause_file": "$Q/pause",
  "log": {"file": "$Q/wd.log", "max_bytes": 100000, "keep": 2, "fsync": false},
  "self_oom_adj": 0, "paused_log_every_ms": 60000, "unknown_name_log_every_ms": 60000,
  "resources": {"rut_floor_mb": 5, "min_free_mb": 8, "max_load1": 50.0, "recheck_ms": 200, "max_wait_ms": 2000},
  "restart_delay": {"mode": "fixed", "interval_ms": 300, "initial_ms": 2000, "factor": 2, "max_ms": 8000},
  "process": {"name": "unified", "initd": "$Q/fake_initd", "pidfile": "$Q/proc.pid",
              "crash_file_template": "$Q/crash_{name}",
              "soft_fail_escalation": {"enabled": false, "after_attempts": 3},
              "consume_timeout_ms": 500, "service_relaunch_ms": 600, "start_grace_ms": 500, "action": "restart"},
  "services": [ {"name": "alpha", "wait_pulse_timeout_ms": 400, "action": "restart", "escalate_to_process": true, "progress_stall_ms": 0},
                {"name": "beta",  "wait_pulse_timeout_ms": 400, "action": "log",     "escalate_to_process": true, "progress_stall_ms": 0} ]
}
EOF
echo 1 > $Q/proc.pid   # PID 1 = старый процесс: fresh-гейт не мешает qemu-сценарию
printf '#!/bin/bash\necho called >> %s/initd_calls\n' $Q > $Q/fake_initd && chmod +x $Q/fake_initd
qemu-mipsel -L $SYSROOT ./svc_watchdog.mipsel $Q/wd.conf &
QPID=$!
sleep 0.5
ls -la $Q/ | grep -E "sock|total" || true
python3 -c "import socket;s=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM);s.sendto(b'alpha','$Q/wd.sock');print('beat sent ok')"
# 1с оба пульсируют, дальше beta продолжает (loop жив), alpha молчит → per-service ветка
python3 - "$Q/wd.sock" <<'EOF' &
import socket, sys, time
s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
t0 = time.time()
while time.time() - t0 < 5.0:
    try:
        s.sendto(b'beta', sys.argv[1])
        if time.time() - t0 < 1.0:
            s.sendto(b'alpha', sys.argv[1])
    except OSError:
        pass
    time.sleep(0.1)
EOF
BPID=$!
sleep 5.5
kill $QPID $BPID 2>/dev/null || true
echo "--- qemu wd.log ---"; cat $Q/wd.log
grep -q "wd_start"                        $Q/wd.log || { echo QEMU-FAIL wd_start; exit 1; }
grep -q "no_pulse name=alpha"             $Q/wd.log || { echo QEMU-FAIL no_pulse; exit 1; }
grep -q "service_restart name=alpha"      $Q/wd.log || { echo QEMU-FAIL service_restart; exit 1; }
grep -q "escalation name=alpha"           $Q/wd.log || { echo QEMU-FAIL escalation; exit 1; }
grep -q "process_restart reason=escalation" $Q/wd.log || { echo QEMU-FAIL process_restart; exit 1; }
grep -q called $Q/initd_calls             || { echo QEMU-FAIL initd; exit 1; }
echo "QEMU SMOKE: OK"

echo "G2-CONTAINER: ALL OK"
