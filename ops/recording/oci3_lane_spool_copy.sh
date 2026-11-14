#!/bin/bash
# oci3_lane_spool_copy.sh SPOOL_ROOM_DIR LANE_ROOM_DIR [OWNER]
# The media host records the stream itself (the spool).  The mover clears a segment once the cloud drive has it, which
# can be minutes after the stream ends, long before the clip lane has read the day.  This copies the finished stream
# (video and chat files) into the lane's own directory of recordings, so the lane reads the host's own recording and
# nothing has to be fetched from the other recorder.  Run every couple of minutes (the .timer).
#   - a day directory is claimed (`.source` = spool) as soon as the host is recording it; one marked otherwise (fetched
#     from the other recorder) is left alone: the two recorders cut a stream at different moments, and a day is read
#     from one of them;
#   - nothing is copied while the stream is being recorded (a video file written in the last QUIET_MIN minutes): the
#     lane does not read a day before it has settled, and the disk holds one copy, not two, during the stream;
#   - a day is copied whole or not at all: when the disk cannot take every file still missing and keep RESERVE_KB free,
#     nothing is copied and the reason is logged, so the lane never sees part of a stream as the whole of it.
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
  [ -z "$(find "$day_dir" -maxdepth 1 -type f -name "*.flv" -mmin "-$QUIET_MIN" -print -quit)" ] || continue
  files=""; need_kb=0
  while IFS=$'\t' read -r name size; do
    [ -n "$name" ] || continue
    if [ ! -f "$target/$name" ] || [ "$(stat -c %s "$target/$name")" != "$size" ]; then
      files="$files$name"$'\n'; need_kb=$((need_kb + size / 1024 + 1))
    fi
  done < <(cd "$day_dir" && find . -maxdepth 1 -type f \( -name "*.flv" -o -name "*.xml" \) -printf "%f\t%s\n")
  [ -n "$files" ] || continue
  avail_kb=$(df -k --output=avail "$LANE" | tail -1)
  if [ $((need_kb + RESERVE_KB)) -gt "$avail_kb" ]; then
    echo "LANE_SPOOL_COPY_DEFERRED day=$day need_kb=$need_kb avail_kb=$avail_kb reserve_kb=$RESERVE_KB"
    continue
  fi
  printf "%s" "$files" | rsync -a --files-from=- --chown="$OWNER" --chmod=Du+rwx,Fu+rw "$day_dir/" "$target/"
  echo "LANE_SPOOL_COPY_OK day=$day files=$(printf "%s" "$files" | wc -l) kb=$need_kb"
done
