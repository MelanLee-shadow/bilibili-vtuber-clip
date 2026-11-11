#!/bin/bash
# Wrapper for the 李豆沙 auto-slice monitor, launched by the LaunchAgent
# A deployment-specific scheduler may invoke this wrapper.
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# The checkout is wherever this script lives; state and log directories follow it.
PROJ="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$PROJ" || exit 1
export AUTOSLICE_MONITOR_REPORT_DIR="${AUTOSLICE_MONITOR_REPORT_DIR:-$PROJ/reports/slice_monitor}"
mkdir -p "$AUTOSLICE_MONITOR_REPORT_DIR" || exit 1
LOG="$AUTOSLICE_MONITOR_REPORT_DIR/cron.log"
# keep the log from growing unbounded (tail last 2000 lines)
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 4000 ]; then
  tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
echo "----- $(date '+%Y-%m-%d %H:%M:%S') -----" >> "$LOG"
"${AUTOSLICE_PYTHON:-python3}" "$PROJ/scripts/slice_monitor.py" >> "$LOG" 2>&1
