#!/bin/bash
# Hold runner.lock for an out-of-band state surgery — WITHOUT the ability to
# strand production.
#
# incident: an ad-hoc guard of exactly this shape
#
#     exec 9<>/opt/bilive/autoslice/runner.lock
#     flock -x -n 9
#     while [ ! -e .../fasttrack-0809.guard/release ]; do sleep 3; done
#
# was left behind when its operator died.  The release sentinel was never
# touched, so the guard kept runner.lock forever; every 10-minute cron tick was
# rejected by `flock -n`, which prints nothing at all.  Seven hours of the
# unattended pipeline vanished in complete silence.
#
# Two independent kill-switches make an orphan impossible here:
#   * TTL      — a wall-clock ceiling; on expiry the lock is released, loudly.
#   * PARENT   — the guard dies with the process that started it.
# Whichever fires first wins.  The guard never kills anything else and never
# breaks somebody else's lock: `flock -n` means a busy lock is an immediate,
# explicit failure.
#
# Usage:  runner_lock_hold_guard.sh <token> <release-sentinel-path>
# Exit:   0 released by sentinel | 3 TTL expired | 4 parent died
#         1 lock busy | 2 usage
set -u

LOCK="${AUTOSLICE_GUARD_LOCK:-/opt/bilive/autoslice/runner.lock}"
TTL_S="${AUTOSLICE_GUARD_TTL_S:-1800}"
POLL_S="${AUTOSLICE_GUARD_POLL_S:-3}"
ALERT="${AUTOSLICE_GUARD_ALERT:-$(dirname "$LOCK")/reports/ALERT_RUNNER_LOCK_GUARD.txt}"
GUARD_PARENT_PID="${AUTOSLICE_GUARD_PARENT_PID:-$PPID}"
# Overridable so the suite can exercise the real control flow on a host
# without util-linux flock(1); production always uses the system binary.
FLOCK_BIN="${AUTOSLICE_GUARD_FLOCK_BIN:-flock}"

if [ "$#" -ne 2 ]; then
    echo "usage: $0 <token> <release-sentinel-path>" >&2
    exit 2
fi
TOKEN=$1
RELEASE=$2

guard_ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
guard_alert() {
    mkdir -p "$(dirname "$ALERT")" 2>/dev/null
    echo "$(guard_ts) $*" >> "$ALERT" 2>/dev/null
    echo "$*" >&2
}

exec 9<>"$LOCK" || {
    echo "GUARD_FAILED: cannot open $LOCK" >&2
    exit 1
}
if ! "$FLOCK_BIN" -x -n 9; then
    echo "GUARD_FAILED: runner.lock already held" >&2
    exit 1
fi

started=$(date +%s)
echo "GUARD_UP pid=$$ token=$TOKEN ttl=${TTL_S}s parent=$GUARD_PARENT_PID release=$RELEASE"

while [ ! -e "$RELEASE" ]; do
    # Parent-death: an orphan guard has nobody left to release it, so the only
    # correct move is to let production have the lock back.
    if ! kill -0 "$GUARD_PARENT_PID" 2>/dev/null; then
        guard_alert "GUARD_PARENT_GONE: token=$TOKEN parent=$GUARD_PARENT_PID died before touching $RELEASE — releasing runner.lock after $(( $(date +%s) - started ))s"
        exit 4
    fi
    if [ "$(( $(date +%s) - started ))" -ge "$TTL_S" ]; then
        guard_alert "GUARD_TTL_EXPIRED: token=$TOKEN held runner.lock for ${TTL_S}s without $RELEASE — releasing so the cron tick can run; re-take the lock if the surgery is still in progress"
        exit 3
    fi
    sleep "$POLL_S"
done

echo "GUARD_RELEASED pid=$$ token=$TOKEN held=$(( $(date +%s) - started ))s"
