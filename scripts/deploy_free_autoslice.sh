#!/bin/bash
# Deploy the autoslice runner to free (2026-07-09 audit: no more dirty-tree
# deploys, no more "which code is production actually running?").
#
# - REFUSES a dirty working tree (production must be reproducible from a commit)
# - streams committed scripts/ src/ assets/ into staging (ignored files excluded)
# - verifies the complete staged file list and SHA-256 manifest
# - pauses new runs, waits for runner.lock, then swaps the three trees with rollback
# - stamps DEPLOYED_COMMIT only after every runtime/external-file check succeeds
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
REMOTE_BASE=/opt/bilive/autoslice
REMOTE_REPO=$REMOTE_BASE/repo
STAGE=$REMOTE_BASE/repo.deploy-$COMMIT
BACKUP=$REMOTE_BASE/repo.rollback-$COMMIT
DISABLED=$REMOTE_BASE/DISABLED
HAD_DISABLED=$(ssh "$HOST" "test -e '$DISABLED' && echo 1 || echo 0")
SWITCHED=0
COMMITTED=0

cleanup() {
    rc=$?
    set +e
    if [ "$COMMITTED" -ne 1 ]; then
        if [ "$SWITCHED" -eq 1 ]; then
            ssh "$HOST" /usr/bin/flock -w 7200 "$REMOTE_BASE/runner.lock" bash -s -- \
                "$REMOTE_REPO" "$STAGE" "$BACKUP" <<'REMOTE_ROLLBACK'
set -eu
repo=$1
stage=$2
backup=$3
for component in scripts src assets; do
    if [ -e "$backup/$component" ]; then
        rm -rf "$stage/$component"
        if [ -e "$repo/$component" ]; then
            mkdir -p "$stage"
            mv "$repo/$component" "$stage/$component"
        fi
        mv "$backup/$component" "$repo/$component"
    fi
done
rm -rf "$stage" "$backup"
REMOTE_ROLLBACK
        else
            ssh "$HOST" "rm -rf '$STAGE' '$BACKUP'" >/dev/null 2>&1 || true
        fi
    fi
    if [ "$HAD_DISABLED" -eq 0 ]; then
        ssh "$HOST" "rm -f '$DISABLED'" >/dev/null 2>&1 || true
    fi
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Stop new ticks and wait for any current producer to leave the shared lock.
ssh "$HOST" "touch '$DISABLED'; /usr/bin/flock -w 7200 '$REMOTE_BASE/runner.lock' true"
ssh "$HOST" "rm -rf '$STAGE' '$BACKUP'; mkdir -p '$STAGE'"

# `git archive` is the deployment source of truth: only HEAD-tracked bytes can
# enter staging. assets/ is intentionally replaced as a repo-owned tree; private
# enrollment WAVs and the CAM++ model live outside repo/.
git archive --format=tar HEAD scripts src assets | ssh "$HOST" "tar -xf - -C '$STAGE'"

LOCAL_MANIFEST=$(git ls-files -z scripts src assets | python3 -c '
import hashlib, pathlib, sys
paths = sorted(p.decode() for p in sys.stdin.buffer.read().split(b"\0") if p)
for value in paths:
    path = pathlib.Path(value)
    print(hashlib.sha256(path.read_bytes()).hexdigest(), value)
')
REMOTE_MANIFEST=$(ssh "$HOST" "cd '$STAGE' && python3 -" <<'REMOTE_MANIFEST_PY'
import hashlib
from pathlib import Path

paths = sorted(
    path
    for root in (Path("scripts"), Path("src"), Path("assets"))
    for path in root.rglob("*")
    if path.is_file()
)
for path in paths:
    print(hashlib.sha256(path.read_bytes()).hexdigest(), path.as_posix())
REMOTE_MANIFEST_PY
)
if [ "$LOCAL_MANIFEST" != "$REMOTE_MANIFEST" ]; then
    echo "DEPLOY VERIFY FAILED: remote staged file/hash manifest differs from committed tree" >&2
    exit 3
fi

# Performer-identity gate: private enrollment audio stays on the runtime host;
# only its versioned hashes/threshold policy live in git.  Migrate the already
# audited enrollment files once, then verify every subsequent deploy in place.
ssh "$HOST" bash -s -- "$STAGE" <<'REMOTE_VALIDATE'
set -eu
cd "$1"
python3 scripts/install_lidousha_voiceprints.py \
  --profile assets/lidousha/voiceprint_profile.v1.json \
  --source-dir /opt/bilive/autoslice/preview/diar_v2 \
  --target-dir /opt/bilive/autoslice/voiceprints/lidousha \
  --model-source-dir /root/.cache/modelscope/models/damo--speech_campplus_sv_zh-cn_16k-common/snapshots/master \
  --model-target-dir /opt/bilive/autoslice/models/campp
/opt/bilive/autoslice/venv-diar/bin/python -c \
  "import modelscope, soundfile; import src.autoslice.speaker_finalizer"
python3 -c \
  'import json; from pathlib import Path; from src.autoslice.host_vocal_proof import _sha256_directory; p=json.loads(Path("assets/lidousha/voiceprint_profile.v1.json").read_text()); actual=_sha256_directory(Path("/opt/bilive/autoslice/models/campp")); assert actual == p["model"]["tree_sha256"], (actual,p["model"]["tree_sha256"]); print("host-vocal runtime verified", actual)'
REMOTE_VALIDATE

# Swap only versioned deploy trees. Other runtime-owned content under repo/
# (notably historical delivery media) stays in place. The remote trap rolls
# back a partial component swap before releasing runner.lock.
ssh "$HOST" /usr/bin/flock -w 7200 "$REMOTE_BASE/runner.lock" bash -s -- \
    "$REMOTE_REPO" "$STAGE" "$BACKUP" <<'REMOTE_SWITCH'
set -eu
repo=$1
stage=$2
backup=$3
mkdir -p "$backup"
rollback() {
    for component in scripts src assets; do
        if [ -e "$backup/$component" ]; then
            rm -rf "$stage/$component"
            if [ -e "$repo/$component" ]; then
                mkdir -p "$stage"
                mv "$repo/$component" "$stage/$component"
            fi
            mv "$backup/$component" "$repo/$component"
        fi
    done
}
trap rollback ERR INT TERM
for component in scripts src assets; do
    mv "$repo/$component" "$backup/$component"
    mv "$stage/$component" "$repo/$component"
done
rmdir "$stage"
trap - ERR INT TERM
REMOTE_SWITCH
SWITCHED=1

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
ssh "$HOST" "printf '%s  deployed %s\n' '$COMMIT' '$(date -u +%Y-%m-%dT%H:%M:%SZ)' > '$REMOTE_REPO/DEPLOYED_COMMIT'"
COMMITTED=1
ssh "$HOST" "rm -rf '$BACKUP'" || echo "WARN: deployed successfully but rollback-tree cleanup failed" >&2
echo "deployed $COMMIT to $HOST (runner md5 verified)"
