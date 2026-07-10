#!/bin/bash
# Deploy the autoslice runner to free (2026-07-09 audit: no more dirty-tree
# deploys, no more "which code is production actually running?").
#
# - REFUSES a dirty working tree (production must be reproducible from a commit)
# - streams committed scripts/ src/ assets/ into staging (ignored files excluded)
# - verifies the complete staged file list and SHA-256 manifest
# - owns a remote deploy guard from initial observation through final cleanup
# - pauses new runs, waits for runner.lock, then swaps the three trees with rollback
# - verifies rollback against a full path/type/mode/SHA-256 manifest
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
DEPLOY_GUARD=$REMOTE_BASE/deploy.guard
DEPLOY_OWNER=$COMMIT-$(date -u +%Y%m%dT%H%M%SZ)-$$
HAD_DISABLED=1
DISABLED_TOUCHED=0
OLD_COMMIT=
SWITCHED=0
COMMITTED=0
STAGE_CREATED=0
GUARD_ACQUIRED=0

cleanup() {
    rc=$?
    set +e
    rollback_ok=1
    if [ "$COMMITTED" -ne 1 ]; then
        if [ "$SWITCHED" -eq 1 ]; then
            if ! ssh "$HOST" /usr/bin/flock -w 7200 "$REMOTE_BASE/runner.lock" bash -s -- \
                "$REMOTE_REPO" "$STAGE" "$BACKUP" "$OLD_COMMIT" <<'REMOTE_ROLLBACK'
set -euo pipefail
repo=$1
stage=$2
backup=$3
old_commit=$4
test -d "$backup"
test -f "$backup/repo.manifest.old.json"
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
test -f "$backup/DEPLOYED_COMMIT.old"
cp "$backup/DEPLOYED_COMMIT.old" "$repo/DEPLOYED_COMMIT"

restore_file() {
    label=$1
    destination=$2
    if [ -f "$backup/external/$label.present" ]; then
        mkdir -p "$(dirname "$destination")"
        cp -p "$backup/external/$label.file" "$destination"
        cmp -s "$backup/external/$label.file" "$destination"
    elif [ -f "$backup/external/$label.absent" ]; then
        rm -f "$destination"
        test ! -e "$destination"
    else
        echo "missing external rollback marker: $label" >&2
        return 1
    fi
}
restore_file watchdog /opt/bilive/autoslice/free_mount_watchdog.sh
restore_file uploader /opt/bilive/app/tmp_manual_upload/do_upload.sh
if [ -f "$backup/external/crontab.present" ]; then
    crontab "$backup/external/crontab.file"
    crontab -l | cmp -s - "$backup/external/crontab.file"
elif [ -f "$backup/external/crontab.absent" ]; then
    crontab -r 2>/dev/null || true
    ! crontab -l >/dev/null 2>&1
else
    echo "missing crontab rollback marker" >&2
    exit 1
fi

test "$(awk 'NR==1 {print $1}' "$repo/DEPLOYED_COMMIT")" = "$old_commit"
python3 - "$repo" "$backup/repo.manifest.old.json" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
expected = json.loads(Path(sys.argv[2]).read_text())
actual = {}
for component in ("scripts", "src", "assets"):
    base = root / component
    for path in (base, *base.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if path.is_symlink():
            actual[relative] = {"type": "symlink", "target": os.readlink(path), "mode": mode}
        elif path.is_dir():
            actual[relative] = {"type": "dir", "mode": mode}
        elif path.is_file():
            actual[relative] = {
                "type": "file",
                "mode": mode,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        else:
            actual[relative] = {"type": "other", "mode": mode}
if actual != expected:
    missing = sorted(set(expected) - set(actual))[:20]
    extra = sorted(set(actual) - set(expected))[:20]
    changed = sorted(key for key in set(actual) & set(expected) if actual[key] != expected[key])[:20]
    raise SystemExit(f"rollback manifest mismatch missing={missing} extra={extra} changed={changed}")
PY
rm -rf "$stage" "$backup"
REMOTE_ROLLBACK
            then
                rollback_ok=0
            fi
        elif [ "$STAGE_CREATED" -eq 1 ]; then
            # Only remove staging that this invocation definitely created.
            # A pre-existing rollback tree is recovery evidence and must never
            # be erased by a retry that has not yet switched production.
            ssh "$HOST" "rm -rf '$STAGE'" >/dev/null 2>&1 || true
        fi
    fi
    if [ "$DISABLED_TOUCHED" -eq 1 ] && [ "$HAD_DISABLED" -eq 0 ]; then
        if [ "$COMMITTED" -eq 1 ] || [ "$rollback_ok" -eq 1 ]; then
            if ! ssh "$HOST" "rm -f '$DISABLED'; test ! -e '$DISABLED'" >/dev/null 2>&1; then
                echo "CRITICAL: deployment state is safe but $DISABLED could not be removed" >&2
                [ "$rc" -ne 0 ] || rc=5
            fi
        else
            echo "CRITICAL: rollback could not be verified; leaving $DISABLED in place" >&2
        fi
    fi
    if [ "$rollback_ok" -ne 1 ]; then
        rc=4
    fi
    if [ "$GUARD_ACQUIRED" -eq 1 ] && [ "$rollback_ok" -eq 1 ]; then
        if ! ssh "$HOST" bash -s -- "$DEPLOY_GUARD" "$DEPLOY_OWNER" <<'REMOTE_UNLOCK'
set -euo pipefail
guard=$1
owner=$2
test "$(cat "$guard/owner")" = "$owner"
rm -rf "$guard"
REMOTE_UNLOCK
        then
            echo "CRITICAL: could not release owned deploy guard $DEPLOY_GUARD" >&2
            [ "$rc" -ne 0 ] || rc=8
        fi
    elif [ "$GUARD_ACQUIRED" -eq 1 ]; then
        echo "CRITICAL: rollback is unverified; retaining deploy guard owned by $DEPLOY_OWNER" >&2
    fi
    exit "$rc"
}

# One deployment owns all remote state from the first observation through final
# cleanup. A stale guard is deliberate recovery evidence and requires an
# operator to inspect it rather than being stolen by a later commit.
if ! ssh "$HOST" bash -s -- "$DEPLOY_GUARD" "$DEPLOY_OWNER" <<'REMOTE_LOCK'
set -euo pipefail
guard=$1
owner=$2
if ! mkdir "$guard" 2>/dev/null; then
    echo "REFUSE: deploy guard already exists (owner: $(cat "$guard/owner" 2>/dev/null || echo unknown))" >&2
    exit 1
fi
umask 077
printf '%s\n' "$owner" > "$guard/owner"
REMOTE_LOCK
then
    exit 7
fi
GUARD_ACQUIRED=1
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

HAD_DISABLED=$(ssh "$HOST" "test -e '$DISABLED' && echo 1 || echo 0")
OLD_COMMIT=$(ssh "$HOST" "awk 'NR==1 {print \$1}' '$REMOTE_REPO/DEPLOYED_COMMIT'")
if ! [[ "$OLD_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
    echo "REFUSE: deployed commit stamp is missing or malformed: $OLD_COMMIT" >&2
    exit 9
fi

# Stop new ticks and wait for any current producer to leave the shared lock.
RESIDUAL=$(ssh "$HOST" "find '$REMOTE_BASE' -mindepth 1 -maxdepth 1 \( -name 'repo.deploy-*' -o -name 'repo.rollback-*' \) -print")
if [ -n "$RESIDUAL" ]; then
    echo "REFUSE: residual deploy/rollback state exists; recover or clear it explicitly:" >&2
    echo "$RESIDUAL" >&2
    exit 6
fi
ssh "$HOST" "touch '$DISABLED'; /usr/bin/flock -w 7200 '$REMOTE_BASE/runner.lock' true"
DISABLED_TOUCHED=1
ssh "$HOST" "test ! -e '$STAGE' && test ! -e '$BACKUP' && mkdir '$STAGE'"
STAGE_CREATED=1

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

# Performer-identity gate: private enrollment audio/model stay outside repo.
# Deployment is read-only toward those external assets: a new profile that does
# not match the already-installed runtime fails and requires a separate,
# explicitly transactional asset migration before code deployment.
ssh "$HOST" bash -s -- "$STAGE" <<'REMOTE_VALIDATE'
set -euo pipefail
cd "$1"
PYTHONDONTWRITEBYTECODE=1 /opt/bilive/autoslice/venv-diar/bin/python -c \
  "import modelscope, soundfile; import src.autoslice.speaker_finalizer"
PYTHONDONTWRITEBYTECODE=1 /opt/bilive/autoslice/venv-diar/bin/python -c \
  'from src.autoslice.speaker_finalizer import _speaker_context_env; e=_speaker_context_env(); assert e.get("CPA_BASE_URL") and e.get("CPA_API_KEY"); print("speaker context env verified")'
PYTHONDONTWRITEBYTECODE=1 /opt/bilive/autoslice/venv-diar/bin/python - <<'PY'
import json
from pathlib import Path

from src.autoslice.host_vocal_proof import _sha256_directory, _validate_profile

profile = json.loads(Path("assets/lidousha/voiceprint_profile.v1.json").read_text())
model, references = _validate_profile(profile)
model_dir = Path("/opt/bilive/autoslice/models/campp")
actual_model = _sha256_directory(model_dir)
assert actual_model == model["tree_sha256"], (actual_model, model["tree_sha256"])
reference_dir = Path("/opt/bilive/autoslice/voiceprints/lidousha")
for expected in references:
    path = reference_dir / expected["filename"]
    import hashlib
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    assert actual == expected["sha256"], (expected["id"], actual, expected["sha256"])
print("speaker runtime assets verified", actual_model, len(references))
PY
REMOTE_VALIDATE

# Swap only versioned deploy trees. Other runtime-owned content under repo/
# (notably historical delivery media) stays in place. The remote trap rolls
# back a partial component swap before releasing runner.lock.
SWITCHED=1
ssh "$HOST" /usr/bin/flock -w 7200 "$REMOTE_BASE/runner.lock" bash -s -- \
    "$REMOTE_REPO" "$STAGE" "$BACKUP" <<'REMOTE_SWITCH'
set -euo pipefail
repo=$1
stage=$2
backup=$3
umask 077
mkdir -p "$backup"
cp "$repo/DEPLOYED_COMMIT" "$backup/DEPLOYED_COMMIT.old"
python3 - "$repo" "$backup/repo.manifest.old.json" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = {}
for component in ("scripts", "src", "assets"):
    base = root / component
    for path in (base, *base.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if path.is_symlink():
            manifest[relative] = {"type": "symlink", "target": os.readlink(path), "mode": mode}
        elif path.is_dir():
            manifest[relative] = {"type": "dir", "mode": mode}
        elif path.is_file():
            manifest[relative] = {
                "type": "file",
                "mode": mode,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        else:
            manifest[relative] = {"type": "other", "mode": mode}
Path(sys.argv[2]).write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
PY

mkdir -p "$backup/external"
capture_file() {
    label=$1
    source=$2
    if [ -e "$source" ]; then
        cp -p "$source" "$backup/external/$label.file"
        touch "$backup/external/$label.present"
    else
        touch "$backup/external/$label.absent"
    fi
}
capture_file watchdog /opt/bilive/autoslice/free_mount_watchdog.sh
capture_file uploader /opt/bilive/app/tmp_manual_upload/do_upload.sh
if crontab -l > "$backup/external/crontab.file" 2>/dev/null; then
    touch "$backup/external/crontab.present"
else
    touch "$backup/external/crontab.absent"
fi

restore_file() {
    label=$1
    destination=$2
    if [ -f "$backup/external/$label.present" ]; then
        mkdir -p "$(dirname "$destination")"
        cp -p "$backup/external/$label.file" "$destination"
        cmp -s "$backup/external/$label.file" "$destination"
    elif [ -f "$backup/external/$label.absent" ]; then
        rm -f "$destination"
        test ! -e "$destination"
    else
        return 1
    fi
}
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
    cp "$backup/DEPLOYED_COMMIT.old" "$repo/DEPLOYED_COMMIT"
    restore_file watchdog /opt/bilive/autoslice/free_mount_watchdog.sh
    restore_file uploader /opt/bilive/app/tmp_manual_upload/do_upload.sh
    if [ -f "$backup/external/crontab.present" ]; then
        crontab "$backup/external/crontab.file"
    elif [ -f "$backup/external/crontab.absent" ]; then
        crontab -r 2>/dev/null || true
    else
        return 1
    fi
}
trap 'rc=$?; trap - ERR; rollback; exit "$rc"' ERR
trap 'trap - ERR HUP INT TERM; rollback; exit 130' INT
trap 'trap - ERR HUP INT TERM; rollback; exit 143' TERM
trap 'trap - ERR HUP INT TERM; rollback; exit 129' HUP
for component in scripts src assets; do
    mv "$repo/$component" "$backup/$component"
    mv "$stage/$component" "$repo/$component"
done
rmdir "$stage"
trap - ERR INT TERM HUP
REMOTE_SWITCH

# Install external entrypoints only from the already-switched committed tree.
# Temp + rename avoids exposing a truncated executable to cron/manual callers.
ssh "$HOST" bash -s <<'REMOTE_EXTERNAL_INSTALL'
set -euo pipefail
tmp=
cleanup_tmp() { [ -z "$tmp" ] || rm -f "$tmp"; }
trap cleanup_tmp EXIT
trap 'cleanup_tmp; exit 129' HUP
trap 'cleanup_tmp; exit 130' INT
trap 'cleanup_tmp; exit 143' TERM
install_atomic() {
    source=$1
    destination=$2
    mode=$3
    mkdir -p "$(dirname "$destination")"
    tmp=$destination.deploy.$$
    cp "$source" "$tmp"
    chmod "$mode" "$tmp"
    test "$(sha256sum "$source" | awk '{print $1}')" = "$(sha256sum "$tmp" | awk '{print $1}')"
    mv -f "$tmp" "$destination"
    tmp=
    test "$(sha256sum "$source" | awk '{print $1}')" = "$(sha256sum "$destination" | awk '{print $1}')"
}
install_atomic \
    /opt/bilive/autoslice/repo/scripts/free_mount_watchdog.sh \
    /opt/bilive/autoslice/free_mount_watchdog.sh \
    755
install_atomic \
    /opt/bilive/autoslice/repo/scripts/free_do_upload.sh \
    /opt/bilive/app/tmp_manual_upload/do_upload.sh \
    700
if ! crontab -l 2>/dev/null | grep -q free_mount_watchdog; then
    (crontab -l 2>/dev/null; echo "*/5 * * * * /usr/bin/flock -n /opt/bilive/autoslice/watchdog.lock /opt/bilive/autoslice/free_mount_watchdog.sh >> /opt/bilive/autoslice/logs/watchdog.log 2>&1") | crontab -
    echo "watchdog cron installed"
fi
REMOTE_EXTERNAL_INSTALL

# verify: the deployed runner is byte-identical to the committed one
LOCAL_MD5=$(md5 -q scripts/free_session_autoslice.py 2>/dev/null || md5sum scripts/free_session_autoslice.py | cut -d' ' -f1)
REMOTE_MD5=$(ssh "$HOST" "md5sum /opt/bilive/autoslice/repo/scripts/free_session_autoslice.py" | cut -d' ' -f1)
if [ "$LOCAL_MD5" != "$REMOTE_MD5" ]; then
    echo "DEPLOY VERIFY FAILED: runner md5 mismatch (local $LOCAL_MD5 remote $REMOTE_MD5)" >&2
    exit 3
fi
REMOTE_DEPLOYED_MANIFEST=$(ssh "$HOST" "cd '$REMOTE_REPO' && python3 -" <<'REMOTE_DEPLOYED_MANIFEST_PY'
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
REMOTE_DEPLOYED_MANIFEST_PY
)
if [ "$LOCAL_MANIFEST" != "$REMOTE_DEPLOYED_MANIFEST" ]; then
    echo "DEPLOY VERIFY FAILED: switched production tree differs from committed tree" >&2
    exit 3
fi
ssh "$HOST" "printf '%s  deployed %s\n' '$COMMIT' '$(date -u +%Y-%m-%dT%H:%M:%SZ)' > '$REMOTE_REPO/DEPLOYED_COMMIT'"
COMMITTED=1
ssh "$HOST" "rm -rf '$BACKUP'" || echo "WARN: deployed successfully but rollback-tree cleanup failed" >&2
echo "deployed $COMMIT to $HOST (runner md5 verified)"
