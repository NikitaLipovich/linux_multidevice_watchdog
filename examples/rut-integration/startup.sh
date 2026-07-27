#!/bin/sh
# startup.sh — bring up the whole RUT956 service stack in ONE place: the unified Python
# services process AND (optionally) the svc_watchdog observer. Idempotent (safe on every boot).
# Invoked from /etc/rc.local (see custom-commands.txt), so deploying a new router is just:
# upload the payload, reboot — this script does the rest.
#
# WHAT RUNS IS CONFIG-DRIVEN, all from the single config svc_watch.conf:
#   which services run   = processes.unified.services.<name>.enabled (default true; or remove the block);
#   whether observer runs = framework.observer.enabled (default true).
# Full mode/command reference: svc_watch/README.md -> "Operating modes".

RD=/usr/local/home/root/external-storage-contents

# 1. wait for the SD overlay — logs and storage live there
while ! df | grep -q '^/dev/sda1'; do echo "startup: waiting for SD (/dev/sda1)..."; sleep 5; done
mkdir -p /mnt/sda1/crash_logs /usr/local/home/root/data

# 2. decide whether to run the observer — straight from the single config (default on if key absent)
WD=$(jsonfilter -i "$RD/svc_watch.conf" -e '@.framework.observer.enabled' 2>/dev/null)
case "$WD" in false|0|no|off) WD=0;; *) WD=1;; esac

# 3. the single config -> /etc (both halves read it: C observer directly, Python via svc_watch_compat)
cp "$RD/svc_watch.conf" /etc/svc_watch.conf

# 4. the unified services process — always, under procd
cp "$RD/init.d-rut_services" /etc/init.d/rut_services && chmod +x /etc/init.d/rut_services
/etc/init.d/rut_services enable
/etc/init.d/rut_services restart

# 5. the observer — only if enabled
if [ "$WD" = 1 ]; then
    if [ -f "$RD/svc_watch_daemon/svc_watchdog.mipsel" ]; then
        cp "$RD/svc_watch_daemon/svc_watchdog.mipsel" "$RD/svc_watchdog"
        chmod +x "$RD/svc_watchdog"
    fi
    cp "$RD/init.d-svc_watchdog" /etc/init.d/svc_watchdog && chmod +x /etc/init.d/svc_watchdog
    /etc/init.d/svc_watchdog enable
    /etc/init.d/svc_watchdog restart
    echo "startup: rut_services + svc_watchdog up (journals in /mnt/sda1/crash_logs)"
else
    if [ -x /etc/init.d/svc_watchdog ]; then /etc/init.d/svc_watchdog stop 2>/dev/null; /etc/init.d/svc_watchdog disable 2>/dev/null; fi
    echo "startup: rut_services up; observer DISABLED (framework.observer.enabled=false) — services only"
fi
