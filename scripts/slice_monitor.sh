#!/bin/zsh
# Wrapper for the 李豆沙 auto-slice monitor, launched by the LaunchAgent
# com.维护者.slice-monitor every 5 minutes.
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PROJ="/Users/op/Project/vtuber-slice"
cd "$PROJ" || exit 1
LOG="$PROJ/reports/slice_monitor/cron.log"
# keep the log from growing unbounded (tail last 2000 lines)
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 4000 ]; then
  tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
echo "----- $(date '+%Y-%m-%d %H:%M:%S') -----" >> "$LOG"
/opt/homebrew/bin/python3 "$PROJ/scripts/slice_monitor.py" >> "$LOG" 2>&1
