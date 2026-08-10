#!/bin/bash
# CloudDrive FUSE mount and recorder-consumer bootstrap watchdog (runs ON free).
#
# Two distinct incidents are covered:
#   1. A dead CloudDrive FUSE endpoint can remain bound into recorder containers.
#   2. At host boot, Docker can start consumers before CloudDrive mounts. Docker
#      then creates the nested bind source on the system disk, which makes the
#      CloudDrive mountpoint non-empty and prevents CloudDrive from mounting.
#
# The consumer compose therefore uses restart=on-failure (no daemon-reboot
# autostart). This script is the only boot/start gate:
#   verify an exact CloudFS FUSE mount -> start/recreate consumers -> verify that
#   every container sees a FUSE filesystem.
#
# On a broken mount it stops consumers before touching the mount. Any files
# found underneath an *unmounted* mountpoint are moved to a recoverable
# quarantine; they are never deleted or hidden below a subsequent FUSE mount.
set -u

MOUNT="${AUTOSLICE_WATCHDOG_MOUNT:-/root/clouddrive2/CloudNAS/CloudDrive}"
PROBE_DIR="${AUTOSLICE_WATCHDOG_PROBE_DIR:-$MOUNT/123云盘/live-streaming}"
BASE="${AUTOSLICE_WATCHDOG_BASE:-/opt/bilive/autoslice}"
ALERT="${AUTOSLICE_WATCHDOG_ALERT:-$BASE/reports/ALERT_MOUNT_WATCHDOG.txt}"
COOLDOWN_STAMP="${AUTOSLICE_WATCHDOG_COOLDOWN_STAMP:-$BASE/watchdog.last_repair}"
COOLDOWN_S="${AUTOSLICE_WATCHDOG_COOLDOWN_S:-1800}"
MOUNT_RETRIES="${AUTOSLICE_WATCHDOG_MOUNT_RETRIES:-24}"
RECORDER_RETRIES="${AUTOSLICE_WATCHDOG_RECORDER_RETRIES:-4}"
RETRY_SLEEP_S="${AUTOSLICE_WATCHDOG_RETRY_SLEEP_S:-5}"
RECORDER_SETTLE_S="${AUTOSLICE_WATCHDOG_RECORDER_SETTLE_S:-8}"
EXPECTED_SOURCE="${AUTOSLICE_WATCHDOG_EXPECTED_SOURCE:-CloudFS}"
COMPOSE_FILE="${AUTOSLICE_WATCHDOG_COMPOSE_FILE:-/opt/bilive/compose.yml}"
QUARANTINE_ROOT="${AUTOSLICE_WATCHDOG_QUARANTINE_ROOT:-/opt/bilive/mount-fallback-quarantine}"

HEARTBEAT="${AUTOSLICE_WATCHDOG_HEARTBEAT:-$BASE/reports/heartbeat.txt}"
RUNNER_LOCK="${AUTOSLICE_WATCHDOG_RUNNER_LOCK:-$BASE/runner.lock}"
DISABLED_FLAG="${AUTOSLICE_WATCHDOG_DISABLED:-$BASE/DISABLED}"
STALL_ALERT="${AUTOSLICE_WATCHDOG_STALL_ALERT:-$BASE/reports/ALERT_RUNNER_STALLED.txt}"
STALL_AFTER_S="${AUTOSLICE_WATCHDOG_STALL_AFTER_S:-1800}"
SELF_HOLDER_RX="${AUTOSLICE_WATCHDOG_SELF_HOLDER_RX:-free_session_autoslice\.py}"
PROC_LOCKS="${AUTOSLICE_WATCHDOG_PROC_LOCKS:-/proc/locks}"
PROC_ROOT="${AUTOSLICE_WATCHDOG_PROC_ROOT:-/proc}"

DOCKER_BIN="${AUTOSLICE_WATCHDOG_DOCKER_BIN:-docker}"
STAT_BIN="${AUTOSLICE_WATCHDOG_STAT_BIN:-stat}"
FINDMNT_BIN="${AUTOSLICE_WATCHDOG_FINDMNT_BIN:-findmnt}"
FIND_BIN="${AUTOSLICE_WATCHDOG_FIND_BIN:-find}"
FUSERMOUNT_BIN="${AUTOSLICE_WATCHDOG_FUSERMOUNT_BIN:-fusermount}"
INSTALL_BIN="${AUTOSLICE_WATCHDOG_INSTALL_BIN:-install}"
MV_BIN="${AUTOSLICE_WATCHDOG_MV_BIN:-mv}"
SLEEP_BIN="${AUTOSLICE_WATCHDOG_SLEEP_BIN:-sleep}"
TIMEOUT_BIN="${AUTOSLICE_WATCHDOG_TIMEOUT_BIN:-timeout}"
UMOUNT_BIN="${AUTOSLICE_WATCHDOG_UMOUNT_BIN:-umount}"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
say() { echo "[$(ts)] $*"; }
alert() {
    mkdir -p "$(dirname "$ALERT")"
    echo "$(ts) $*" >> "$ALERT"
    say "$*"
}

stall_alert() {
    mkdir -p "$(dirname "$STALL_ALERT")"
    echo "$(ts) $*" >> "$STALL_ALERT"
    say "$*"
}

runner_lock_holders() {
    # /proc/locks identifies the locked file by MAJ:MIN:INODE (the kernel
    # prints "%02x:%02x:%lu"), so derive the same key from the lock file.
    # 2026-08-09: runner.lock was held for seven hours by an orphaned process
    # and every blocked cron tick exited silently — nobody could name the
    # holder afterwards because nothing ever recorded it.
    [ -e "$RUNNER_LOCK" ] || return 0
    [ -r "$PROC_LOCKS" ] || return 0
    dev_hex=$("$STAT_BIN" -c '%D' "$RUNNER_LOCK" 2>/dev/null) || return 0
    inode=$("$STAT_BIN" -c '%i' "$RUNNER_LOCK" 2>/dev/null) || return 0
    [ -n "$dev_hex" ] && [ -n "$inode" ] || return 0
    while [ "${#dev_hex}" -lt 4 ]; do dev_hex="0$dev_hex"; done
    maj=${dev_hex%??}
    min=${dev_hex#"$maj"}
    key="$maj:$min:$inode"
    pids=$(
        awk -v key="$key" \
            '{for (i = 2; i <= NF; i++) if ($i == key) print $(i - 1)}' \
            "$PROC_LOCKS" 2>/dev/null
    )
    for pid in $pids; do
        cmd=$(tr '\0' ' ' < "$PROC_ROOT/$pid/cmdline" 2>/dev/null)
        [ -n "$cmd" ] || cmd="(cmdline unreadable)"
        echo "pid=$pid cmd=$cmd"
    done
}

check_runner_not_starved() {
    # A cron tick rejected by `flock -n` writes NOTHING: no log line, no
    # heartbeat, no alert.  On 2026-08-09 that silence hid a seven-hour outage.
    # The heartbeat's own age is the only signal that survives starvation.
    [ -e "$DISABLED_FLAG" ] && return 0   # paused on purpose (deploy/operator)
    [ -f "$HEARTBEAT" ] || return 0       # fresh base: nothing to compare yet
    last=$("$STAT_BIN" -c '%Y' "$HEARTBEAT" 2>/dev/null) || return 0
    [ -n "$last" ] || return 0
    age=$(( $(date +%s) - last ))
    [ "$age" -gt "$STALL_AFTER_S" ] || return 0
    holders=$(runner_lock_holders)
    # The heartbeat is only written at tick END, and a marathon batch legitimately
    # runs for hours (2026-08-09: one healthy tick spanned 09:40→11:42).  A tick
    # holding its own lock is working, not starved — the incident shape is a
    # FOREIGN holder, so only that raises the alarm.  Crying wolf on every long
    # batch would retire this alert within a week.
    if echo "$holders" | grep -Eq "$SELF_HOLDER_RX"; then
        say "runner.lock held by the runner's own tick for ${age}s — working, not starved"
        return 0
    fi
    if [ -z "$holders" ]; then
        holders="(none in $PROC_LOCKS — cron itself may be dead)"
    else
        holders=$(echo "$holders" | tr '\n' ';')
    fi
    stall_alert "runner STALLED: heartbeat ${age}s old (> ${STALL_AFTER_S}s) with no DISABLED flag; runner.lock holder(s): $holders — NEEDS HUMAN"
}

mount_value() {
    field=$1
    "$TIMEOUT_BIN" 10 "$FINDMNT_BIN" -n -T "$MOUNT" -o "$field" \
        2>/dev/null | tr -d '\r\n'
}

mount_is_real() {
    target=$(mount_value TARGET) || return 1
    fstype=$(mount_value FSTYPE) || return 1
    source=$(mount_value SOURCE) || return 1
    [ "$target" = "$MOUNT" ] || return 1
    case "$fstype" in
        fuse|fuse.*) ;;
        *) return 1 ;;
    esac
    [ "$source" = "$EXPECTED_SOURCE" ]
}

probe() {
    mount_is_real &&
        "$TIMEOUT_BIN" 25 ls "$PROBE_DIR" > /dev/null 2>&1
}

consumer_mount_ok() {
    container=$1
    path=$2
    running=$(
        "$TIMEOUT_BIN" 10 "$DOCKER_BIN" inspect \
            -f '{{.State.Running}}' "$container" 2>/dev/null
    ) || return 1
    [ "$running" = "true" ] || return 1
    container_fstype=$(
        "$TIMEOUT_BIN" 25 "$DOCKER_BIN" exec "$container" \
            stat -f -c '%T' "$path" 2>/dev/null | tr -d '\r\n'
    ) || return 1
    case "$container_fstype" in
        fuse*) return 0 ;;
        *) return 1 ;;
    esac
}

all_consumers_ok() {
    consumer_mount_ok bililive_adapter /adapter/Videos &&
        consumer_mount_ok bililive_recorder /rec/Videos &&
        consumer_mount_ok bilive_record /app/Videos
}

stop_consumers() {
    for container in bililive_recorder bililive_adapter bilive_record; do
        "$DOCKER_BIN" stop "$container" > /dev/null 2>&1 || true
    done
}

verify_consumers() {
    for attempt in $(seq 1 "$RECORDER_RETRIES"); do
        if all_consumers_ok; then
            alert "consumer gate COMPLETE: adapter, recorder and tooling all see CloudFS"
            return 0
        fi
        say "consumer mount verification retry $attempt/$RECORDER_RETRIES"
        "$SLEEP_BIN" "$RETRY_SLEEP_S"
    done
    alert "consumer gate FAILED: one or more consumers do not see a FUSE recording path — NEEDS HUMAN"
    return 1
}

start_consumers() {
    if ! "$DOCKER_BIN" compose -f "$COMPOSE_FILE" up -d --force-recreate; then
        alert "consumer start FAILED: docker compose up returned non-zero — NEEDS HUMAN"
        return 1
    fi
    "$SLEEP_BIN" "$RECORDER_SETTLE_S"
    verify_consumers
}

quarantine_unmounted_contents() {
    # Never move files out of a mounted filesystem. -M checks the exact
    # mountpoint, unlike -T which otherwise falls back to the root filesystem.
    if "$TIMEOUT_BIN" 10 "$FINDMNT_BIN" -n -M "$MOUNT" > /dev/null 2>&1; then
        alert "repair FAILED: $MOUNT is still a mountpoint after unmount — NEEDS HUMAN"
        return 1
    fi
    case "$QUARANTINE_ROOT" in
        "$MOUNT"|"$MOUNT"/*)
            alert "repair FAILED: quarantine root is inside the mountpoint — NEEDS HUMAN"
            return 1
            ;;
    esac
    first_entry=$(
        "$TIMEOUT_BIN" 10 "$FIND_BIN" "$MOUNT" \
            -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null
    ) || {
        alert "repair FAILED: cannot inspect the unmounted mountpoint — NEEDS HUMAN"
        return 1
    }
    [ -n "$first_entry" ] || return 0

    quarantine="$QUARANTINE_ROOT/$(date -u +%Y%m%dT%H%M%SZ)-$$"
    "$INSTALL_BIN" -d -m 700 "$quarantine" || return 1
    while IFS= read -r -d '' entry; do
        if ! "$MV_BIN" -- "$entry" "$quarantine/"; then
            alert "repair FAILED: could not preserve fallback entry $entry — NEEDS HUMAN"
            return 1
        fi
    done < <("$FIND_BIN" "$MOUNT" -mindepth 1 -maxdepth 1 -print0)
    alert "preserved system-disk fallback entries in $quarantine"
}

case "${1:-}" in
    "")
        ;;
    --probe-only)
        probe
        exit $?
        ;;
    *)
        echo "usage: $0 [--probe-only]" >&2
        exit 2
        ;;
esac

# Independent of mount health: a healthy mount with a starved runner is still
# a dead pipeline, and this is the only cron line that keeps running when
# runner.lock is held.
check_runner_not_starved

if probe; then
    if all_consumers_ok; then
        exit 0
    fi
    alert "real CloudFS mount is healthy but recorder consumers are absent or stale; bootstrapping"
    start_consumers
    exit $?
fi

alert "real CloudFS mount probe FAILED ($PROBE_DIR)"
# This ordering is critical: no consumer may recreate nested bind-source
# directories while CloudDrive is trying to mount.
stop_consumers

if [ -f "$COOLDOWN_STAMP" ]; then
    last=$(cat "$COOLDOWN_STAMP" 2>/dev/null || echo 0)
    now=$(date +%s)
    if [ $((now - last)) -lt "$COOLDOWN_S" ]; then
        alert "repair skipped: last repair $((now - last))s ago (< ${COOLDOWN_S}s cooldown); consumers remain stopped — NEEDS HUMAN"
        exit 1
    fi
fi
date +%s > "$COOLDOWN_STAMP"

alert "repair start: consumers stopped; unmounting stale endpoint"
"$FUSERMOUNT_BIN" -uz "$MOUNT" > /dev/null 2>&1 || true
"$UMOUNT_BIN" -l "$MOUNT" > /dev/null 2>&1 || true
if ! quarantine_unmounted_contents; then
    exit 1
fi

if ! "$DOCKER_BIN" restart clouddrive2 > /dev/null 2>&1; then
    alert "repair FAILED: docker restart clouddrive2 returned non-zero — NEEDS HUMAN"
    exit 1
fi

healthy=0
for _ in $(seq 1 "$MOUNT_RETRIES"); do
    if probe; then
        healthy=1
        break
    fi
    "$SLEEP_BIN" "$RETRY_SLEEP_S"
done
if [ "$healthy" -ne 1 ]; then
    alert "repair FAILED: exact CloudFS FUSE mount did not recover — NEEDS HUMAN"
    exit 1
fi

alert "real CloudFS mount recovered; recreating recorder consumers"
start_consumers
