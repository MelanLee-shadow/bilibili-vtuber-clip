#!/bin/bash
# oci3_lane_spool_copy.sh SPOOL_ROOM_DIR LANE_ROOM_DIR [OWNER]
# The media host records the stream itself (the spool).  The mover clears a segment once the cloud drive has it, which
# can be minutes after the stream ends, long before the clip lane has read the day.  This copies the finished stream
# (video and chat files) into the lane's own directory of recordings, so the lane reads the host's own recording and
# nothing has to be fetched from the other recorder.  Run every couple of minutes (the .timer).
#   - a day directory is claimed (`.source` = spool) as soon as the host is recording it; one marked otherwise (fetched
#     from the other recorder) is left alone: the two recorders cut a stream at different moments, and a day is read
#     from one of them;
#   - a segment is copied as soon as it is closed (its video and chat not written in the last QUIET_MIN minutes); the
#     one being recorded waits.  Since the mover clears each closed segment once 115 and 123 have it, during
#     the stream, so a day copied only after the stream would have lost its first segments (维护者:
#     「原始录播文件已经不见了，还得重新下」).  The mover also keeps a segment until this copy of it is whole
#     (recording_spool_mover.py --lane-root).  The lane does not read a day before its newest file has settled;
#   - a file is copied only when the disk can take it and keep RESERVE_KB free; otherwise it is logged and tried again;
#   - while anything of the day in the spool is not yet here (being recorded, or deferred), `.copy_pending` is touched on
#     every run: the lane lists it with the day's files, so the day does not settle on a partial copy.
set -euo pipefail
SPOOL="${1:?spool room directory}"; LANE="${2:?lane room directory}"; OWNER="${3:-ubuntu:ubuntu}"
QUIET_MIN="${QUIET_MIN:-3}"; RESERVE_KB="${RESERVE_KB:-8388608}"
[ -d "$SPOOL" ] || exit 0
for day_dir in $(find "$SPOOL" -mindepth 1 -maxdepth 1 -type d -name "20??-??-??" | sort | tail -n 2); do
  day=$(basename "$day_dir"); target="$LANE/$day"
  if [ -f "$target/.source" ] && [ "$(cat "$target/.source")" != spool ]; then continue; fi
  [ -n "$(find "$day_dir" -maxdepth 1 -type f -name "*.flv" -print -quit)" ] || continue
  mkdir -p "$target"
  [ -f "$target/.source" ] || echo spool > "$target/.source"
  chown "$OWNER" "$target" "$target/.source"
  files=""; need_kb=0
  missing() { [ ! -f "$target/$1" ] || [ "$(stat -c %s "$target/$1")" != "$2" ]; }
  while IFS=$'\t' read -r name size; do
    [ -n "$name" ] || continue
    if missing "$name" "$size"; then
      files="$files$name"$'\n'; need_kb=$((need_kb + size / 1024 + 1))
    fi
  done < <(cd "$day_dir" && find . -maxdepth 1 -type f \( -name "*.flv" -o -name "*.xml" \) -mmin "+$QUIET_MIN" -printf "%f\t%s\n")
  if [ -n "$files" ]; then
  avail_kb=$(df -k --output=avail "$LANE" | tail -1)
  if [ $((need_kb + RESERVE_KB)) -gt "$avail_kb" ]; then
    echo "LANE_SPOOL_COPY_DEFERRED day=$day need_kb=$need_kb avail_kb=$avail_kb reserve_kb=$RESERVE_KB"
  else
    printf "%s" "$files" | rsync -a --files-from=- --chown="$OWNER" --chmod=Du+rwx,Fu+rw "$day_dir/" "$target/"
    echo "LANE_SPOOL_COPY_OK day=$day files=$(printf "%s" "$files" | wc -l) kb=$need_kb"
  fi
  fi
  pending=0
  while IFS=$'\t' read -r name size; do
    [ -n "$name" ] && missing "$name" "$size" && pending=1
  done < <(cd "$day_dir" && find . -maxdepth 1 -type f \( -name "*.flv" -o -name "*.xml" \) -printf "%f\t%s\n")
  if [ "$pending" = 1 ]; then touch "$target/.copy_pending"; chown "$OWNER" "$target/.copy_pending"; else rm -f "$target/.copy_pending"; fi
done
