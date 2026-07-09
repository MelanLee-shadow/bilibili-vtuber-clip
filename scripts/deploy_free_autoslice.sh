#!/bin/bash
# Deploy the autoslice runner to free (2026-07-09 audit: no more dirty-tree
# deploys, no more "which code is production actually running?").
#
# - REFUSES a dirty working tree (production must be reproducible from a commit)
# - rsyncs scripts/ src/ assets/ → free:/opt/bilive/autoslice/repo/
# - stamps DEPLOYED_COMMIT (hash + time) on the remote
# - installs the mount watchdog + its cron line (idempotent)
# - installs the guarded do_upload.sh (refuses bare invocation)
# - md5-verifies the runner after push (sync lesson: never swallow errors)
set -euo pipefail
HOST="${1:-free}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -n "$(git status --porcelain)" ]; then
    echo "REFUSE: working tree is dirty — commit first, production deploys are commit-only." >&2
    git status --short >&2
    exit 2
fi
COMMIT=$(git rev-parse HEAD)

rsync -a --delete "$ROOT/scripts/" "$HOST:/opt/bilive/autoslice/repo/scripts/"
rsync -a --delete "$ROOT/src/" "$HOST:/opt/bilive/autoslice/repo/src/"
rsync -a "$ROOT/assets/" "$HOST:/opt/bilive/autoslice/repo/assets/"

ssh "$HOST" "echo '$COMMIT  deployed $(date -u +%Y-%m-%dT%H:%M:%SZ)' > /opt/bilive/autoslice/repo/DEPLOYED_COMMIT"

# mount watchdog + cron (idempotent)
ssh "$HOST" '
  cp /opt/bilive/autoslice/repo/scripts/free_mount_watchdog.sh /opt/bilive/autoslice/free_mount_watchdog.sh
  chmod +x /opt/bilive/autoslice/free_mount_watchdog.sh
  if ! crontab -l 2>/dev/null | grep -q free_mount_watchdog; then
    (crontab -l 2>/dev/null; echo "*/5 * * * * /usr/bin/flock -n /opt/bilive/autoslice/watchdog.lock /opt/bilive/autoslice/free_mount_watchdog.sh >> /opt/bilive/autoslice/logs/watchdog.log 2>&1") | crontab -
    echo "watchdog cron installed"
  fi
'

# guarded manual uploader (manifest-bound channel only)
scp -q "$ROOT/scripts/free_do_upload.sh" "$HOST:/opt/bilive/app/tmp_manual_upload/do_upload.sh"
ssh "$HOST" "chmod 700 /opt/bilive/app/tmp_manual_upload/do_upload.sh"

# verify: the deployed runner is byte-identical to the committed one
LOCAL_MD5=$(md5 -q scripts/free_session_autoslice.py 2>/dev/null || md5sum scripts/free_session_autoslice.py | cut -d' ' -f1)
REMOTE_MD5=$(ssh "$HOST" "md5sum /opt/bilive/autoslice/repo/scripts/free_session_autoslice.py" | cut -d' ' -f1)
if [ "$LOCAL_MD5" != "$REMOTE_MD5" ]; then
    echo "DEPLOY VERIFY FAILED: runner md5 mismatch (local $LOCAL_MD5 remote $REMOTE_MD5)" >&2
    exit 3
fi
echo "deployed $COMMIT to $HOST (runner md5 verified)"
