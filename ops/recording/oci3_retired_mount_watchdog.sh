#!/bin/bash
# OCI3 compatibility tombstone for stale root cron/maintenance callers.
# Free still uses scripts/mount_watchdog.sh unchanged. Never control
# recording or CD2 from the retired OCI3 mount path.
echo 'OCI3_MOUNT_WATCHDOG_RETIRED: recording uses local spool; use local recording service' >&2
exit 75
