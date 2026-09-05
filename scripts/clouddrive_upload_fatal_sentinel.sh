#!/bin/bash
# CloudDrive upload-fatal sentinel (runs ON free).
#
# loss lesson: the recorder writes straight into the CloudFS FUSE
# mount, so a recording "exists" the moment it enters clouddrive2's local
# write-back cache.  When every upload attempt then fails FATALLY (etag/md5
# mismatch, dead part URLs), the bytes exist nowhere durable: the FUSE view
# keeps showing the file until the cache drops it (for example on a container
# restart), at which point the recording is gone and every already-selected
# slice candidate on it dies with SOURCE_MEDIA_MISSING.
#
# This sentinel narrows that loss window to one cron interval: it scans recent
# clouddrive2 logs for fatal upload errors, immediately copies each
# still-readable victim out of the cache-backed FUSE view into a local rescue
# directory, and appends a NEEDS HUMAN alert.  It never deletes and never
# overwrites an existing rescue copy.
set -u

MOUNT="${UPLOAD_SENTINEL_MOUNT:-/path/to/cloud-drive}"
BASE="${UPLOAD_SENTINEL_BASE:-/opt/bilive/autoslice}"
RESCUE_ROOT="${UPLOAD_SENTINEL_RESCUE_ROOT:-/opt/bilive/upload-fatal-rescue}"
ALERT="${UPLOAD_SENTINEL_ALERT:-$BASE/reports/ALERT_UPLOAD_FATAL.txt}"
SEEN="${UPLOAD_SENTINEL_SEEN:-$BASE/upload-fatal-sentinel.seen}"
SINCE="${UPLOAD_SENTINEL_SINCE:-30m}"
CONTAINER="${UPLOAD_SENTINEL_CONTAINER:-clouddrive2}"
LOG_DIR="${UPLOAD_SENTINEL_LOG_DIR:-/path/to/clouddrive2/config/log}"
# Refuse a rescue copy that would leave the system disk below this floor: the
# runner, recorder staging and deploys share it .
MIN_FREE_KB="${UPLOAD_SENTINEL_MIN_FREE_KB:-8388608}"

DOCKER_BIN="${UPLOAD_SENTINEL_DOCKER_BIN:-docker}"
STAT_BIN="${UPLOAD_SENTINEL_STAT_BIN:-stat}"
DF_BIN="${UPLOAD_SENTINEL_DF_BIN:-df}"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
say() { echo "[$(ts)] $*"; }
alert() {
    mkdir -p "$(dirname "$ALERT")"
    echo "$(ts) $*" >> "$ALERT"
    say "$*"
}

case "$RESCUE_ROOT" in
    "$MOUNT"|"$MOUNT"/*)
        alert "sentinel misconfigured: rescue root is inside the FUSE mount — NEEDS HUMAN"
        exit 1
        ;;
esac

current_log_date="$(date +%Y-%m-%d)"
previous_log_date="$(date -d '1 day ago' +%Y-%m-%d 2>/dev/null || true)"

# Docker keeps the recent window; CloudDrive2's files are bounded to exactly
# today's and yesterday's daily logs.  Missing files/directories are normal.
failures=$(
    {
        "$DOCKER_BIN" logs "$CONTAINER" --since "$SINCE" 2>&1
        for log_date in "$current_log_date" "$previous_log_date"; do
            [ -n "$log_date" ] || continue
            log_file="$LOG_DIR/$log_date.log"
            [ -f "$log_file" ] && cat "$log_file"
        done
    } |
        sed -E $'s/\x1b\\[[0-9;]*m//g' |
        grep -E 'upload error for /' |
        grep -E 'UploadError\(Fatal|PermissionDenied\(' |
        sed -E 's/.*upload error for (.+): (UploadError|PermissionDenied).*/\1/' |
        sort -u
) || true
[ -n "$failures" ] || exit 0

mkdir -p "$(dirname "$SEEN")"
touch "$SEEN"

while IFS= read -r cloud_path; do
    [ -n "$cloud_path" ] || continue
    case "$cloud_path" in
        /*) ;;
        *) continue ;;
    esac
    case "$cloud_path" in
        */../*|*/..|../*|..) continue ;;
    esac
    if grep -Fxq "$cloud_path" "$SEEN"; then
        continue
    fi
    echo "$cloud_path" >> "$SEEN"
    alert "upload FATAL for $cloud_path — cloud bytes are NOT durable — NEEDS HUMAN"
    fuse_path="$MOUNT$cloud_path"
    if [ ! -f "$fuse_path" ]; then
        alert "rescue impossible: $cloud_path already vanished from the FUSE view"
        continue
    fi
    rescue_path="$RESCUE_ROOT$cloud_path"
    if [ -e "$rescue_path" ]; then
        alert "rescue copy already exists: $rescue_path (kept, not overwritten)"
        continue
    fi
    size_kb=$((($("$STAT_BIN" -c '%s' "$fuse_path") + 1023) / 1024))
    mkdir -p "$RESCUE_ROOT"
    free_kb=$("$DF_BIN" --output=avail -k "$RESCUE_ROOT" | tail -1 | tr -d ' ')
    if [ $((free_kb - size_kb)) -lt "$MIN_FREE_KB" ]; then
        alert "rescue skipped for $cloud_path: ${size_kb}KB copy would breach the ${MIN_FREE_KB}KB free-disk floor — NEEDS HUMAN"
        continue
    fi
    mkdir -p "$(dirname "$rescue_path")"
    tmp_path="$rescue_path.rescue.$$"
    if cp -p -- "$fuse_path" "$tmp_path" &&
        [ "$("$STAT_BIN" -c '%s' "$tmp_path")" = "$("$STAT_BIN" -c '%s' "$fuse_path")" ]; then
        mv -- "$tmp_path" "$rescue_path"
        alert "rescued $cloud_path -> $rescue_path ($("$STAT_BIN" -c '%s' "$rescue_path") bytes)"
    else
        rm -f -- "$tmp_path"
        alert "rescue FAILED for $cloud_path: copy out of the cache view did not complete — NEEDS HUMAN"
    fi
done <<< "$failures"

exit 0
