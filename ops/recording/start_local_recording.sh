#!/usr/bin/env bash
# OCI3 boot gate: local disk only. No CloudDrive or provider operations.
set -Eeuo pipefail
source /opt/bilive/autoslice/recording-health.env
spool=$AUTOSLICE_RECORDING_BIND_ROOT
room=$AUTOSLICE_RECORDING_SPOOL_ROOM
[[ ! -L "$spool" && ! -L "$room" ]] || exit 1
install -d -m 0755 "$spool"
install -d -m 0700 "$room"
fstype=$(findmnt -T "$room" -n -o FSTYPE)
case "$fstype" in ext4|xfs|btrfs) ;; *) echo 'recording spool is not local disk' >&2; exit 1 ;; esac
exec docker compose -f /opt/bilive/compose.yml up -d --no-build --pull never bilive bililive-adapter bililive-recorder
