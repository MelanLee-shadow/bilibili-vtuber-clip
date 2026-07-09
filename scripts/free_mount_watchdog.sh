#!/bin/bash
# CloudDrive FUSE mount watchdog (runs ON free via cron */5).
#
# 2026-07-09 incident: the clouddrive process died mid-read and restarted, but
# the HOST mountpoint stayed a dead "Transport endpoint is not connected" stub
# (mount propagation only happens at mount time).  bilive_record bind-mounts
# that path into /app/Videos, so the RECORDER's write path was broken for hours
# while every monitor read the outage as "no new recordings" (green).
#
# This watchdog probes the mount with a timeout (a hung FUSE blocks plain
# stat() forever), and on failure runs the validated repair sequence:
#   lazy-unmount stale stub → restart clouddrive2 → wait for host-side health
#   → restart bilive_record (its bind is rprivate; it must re-bind) → verify.
# Every action is appended to an ALERT report file that the Mac launchd pull
# picks up (Ivan's rule: alerts travel via report files).
#
# Deploy: cp to /opt/bilive/autoslice/free_mount_watchdog.sh; cron:
#   */5 * * * * /usr/bin/flock -n /opt/bilive/autoslice/watchdog.lock /opt/bilive/autoslice/free_mount_watchdog.sh >> /opt/bilive/autoslice/logs/watchdog.log 2>&1
set -u

MOUNT="/root/clouddrive2/CloudNAS/CloudDrive"
PROBE_DIR="$MOUNT/123云盘/live-streaming"
BASE="/opt/bilive/autoslice"
ALERT="$BASE/reports/ALERT_MOUNT_WATCHDOG.txt"
COOLDOWN_STAMP="$BASE/watchdog.last_repair"
COOLDOWN_S=1800

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
say() { echo "[$(ts)] $*"; }
alert() { mkdir -p "$(dirname "$ALERT")"; echo "$(ts) $*" >> "$ALERT"; say "$*"; }

probe() { timeout 25 ls "$PROBE_DIR" > /dev/null 2>&1; }

if probe; then
    exit 0
fi

alert "mount probe FAILED ($PROBE_DIR unreadable/hung)"

if [ -f "$COOLDOWN_STAMP" ]; then
    last=$(cat "$COOLDOWN_STAMP" 2>/dev/null || echo 0)
    now=$(date +%s)
    if [ $((now - last)) -lt "$COOLDOWN_S" ]; then
        alert "repair skipped: last repair $((now - last))s ago (< ${COOLDOWN_S}s cooldown) — still broken, needs human"
        exit 1
    fi
fi
date +%s > "$COOLDOWN_STAMP"

alert "repair start: lazy-unmount stale endpoint + restart clouddrive2"
fusermount -uz "$MOUNT" 2>/dev/null
umount -l "$MOUNT" 2>/dev/null
docker restart clouddrive2 > /dev/null 2>&1

healthy=0
for _ in $(seq 1 24); do
    if probe; then healthy=1; break; fi
    sleep 5
done

if [ "$healthy" -ne 1 ]; then
    alert "repair FAILED: mount still unreadable after clouddrive2 restart + 120s — NEEDS HUMAN"
    exit 1
fi
alert "host mount recovered; restarting bilive_record to re-bind /app/Videos"

docker restart bilive_record > /dev/null 2>&1
sleep 8
if timeout 25 docker exec bilive_record ls /app/Videos > /dev/null 2>&1; then
    alert "repair COMPLETE: recorder write path verified inside container"
    exit 0
fi
alert "repair PARTIAL: host mount ok but /app/Videos still unreadable in bilive_record — NEEDS HUMAN"
exit 1
