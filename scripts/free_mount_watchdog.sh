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

MOUNT="${AUTOSLICE_WATCHDOG_MOUNT:-/root/clouddrive2/CloudNAS/CloudDrive}"
PROBE_DIR="${AUTOSLICE_WATCHDOG_PROBE_DIR:-$MOUNT/123云盘/live-streaming}"
BASE="${AUTOSLICE_WATCHDOG_BASE:-/opt/bilive/autoslice}"
ALERT="$BASE/reports/ALERT_MOUNT_WATCHDOG.txt"
COOLDOWN_STAMP="$BASE/watchdog.last_repair"
COOLDOWN_S="${AUTOSLICE_WATCHDOG_COOLDOWN_S:-1800}"
MOUNT_RETRIES="${AUTOSLICE_WATCHDOG_MOUNT_RETRIES:-24}"
RECORDER_RETRIES="${AUTOSLICE_WATCHDOG_RECORDER_RETRIES:-4}"
RETRY_SLEEP_S="${AUTOSLICE_WATCHDOG_RETRY_SLEEP_S:-5}"
RECORDER_SETTLE_S="${AUTOSLICE_WATCHDOG_RECORDER_SETTLE_S:-8}"
DOCKER_BIN="${AUTOSLICE_WATCHDOG_DOCKER_BIN:-docker}"
TIMEOUT_BIN="${AUTOSLICE_WATCHDOG_TIMEOUT_BIN:-timeout}"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
say() { echo "[$(ts)] $*"; }
alert() { mkdir -p "$(dirname "$ALERT")"; echo "$(ts) $*" >> "$ALERT"; say "$*"; }

probe() { "$TIMEOUT_BIN" 25 ls "$PROBE_DIR" > /dev/null 2>&1; }

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
if ! "$DOCKER_BIN" restart clouddrive2 > /dev/null 2>&1; then
    alert "repair FAILED: docker restart clouddrive2 returned non-zero — NEEDS HUMAN"
    exit 1
fi

healthy=0
for _ in $(seq 1 "$MOUNT_RETRIES"); do
    if probe; then healthy=1; break; fi
    sleep "$RETRY_SLEEP_S"
done

if [ "$healthy" -ne 1 ]; then
    alert "repair FAILED: mount still unreadable after clouddrive2 restart + 120s — NEEDS HUMAN"
    exit 1
fi
alert "host mount recovered; restarting bilive_record to re-bind /app/Videos"

if ! "$DOCKER_BIN" restart bilive_record > /dev/null 2>&1; then
    # Docker can stop the old container successfully and still return non-zero
    # when the immediate start loses a mount race.  July 22 left the recorder
    # Exited(137) here while the host mount was already healthy.  A bounded
    # explicit start closes that partial-repair hole.
    alert "bilive_record restart returned non-zero; entering bounded start fallback"
fi
sleep "$RECORDER_SETTLE_S"
for attempt in $(seq 1 "$RECORDER_RETRIES"); do
    if "$TIMEOUT_BIN" 25 "$DOCKER_BIN" exec bilive_record ls /app/Videos > /dev/null 2>&1; then
        alert "repair COMPLETE: recorder running and write path verified inside container"
        exit 0
    fi
    "$DOCKER_BIN" start bilive_record > /dev/null 2>&1 || true
    sleep "$RETRY_SLEEP_S"
    say "bilive_record verification retry $attempt/$RECORDER_RETRIES"
done
alert "repair PARTIAL: host mount ok but recorder could not be started with readable /app/Videos — NEEDS HUMAN"
exit 1
