#!/bin/zsh
# Wrapper for the 李豆沙 auto-slice monitor, launched by the LaunchAgent
# A deployment-specific scheduler may invoke this wrapper.
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PROJ="${AUTOSLICE_PROJECT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$PROJ" || exit 1
LOG="${AUTOSLICE_MONITOR_LOG:-$PROJ/reports/slice_monitor/cron.log}"
mkdir -p "$(dirname "$LOG")" || exit 1
# keep the log from growing unbounded (tail last 2000 lines)
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 4000 ]; then
  tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
echo "----- $(date '+%Y-%m-%d %H:%M:%S') -----" >> "$LOG"
"${AUTOSLICE_PYTHON:-python3}" "$PROJ/scripts/slice_monitor.py" >> "$LOG" 2>&1
