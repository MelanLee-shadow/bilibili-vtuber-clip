#!/usr/bin/env bash
# Keep the OCI3 production soak independent from Free while it is recording.
#
# Modes:
#   activate  validate the live OCI3 surface, install one exact cron, then
#             remove DISABLED as the final commit step
#   run       verify the active surface and run one real, live-gated tick
#   verify    verify the exact active steady state without changing it
#   deactivate create/verify DISABLED, then remove only this script's cron
#
# Production values below are intentionally not environment-overridable.  The
# explicit test seam is local-only and uses a real fixture root.
set -Eeuo pipefail
umask 077

readonly EXPECTED_HOST="recording-host"
readonly EXPECTED_ARCH="aarch64"
readonly ROOM="22966160"
readonly START_DATE="2026-08-20"
readonly MOUNT_PATH="/path/to/cloud-drive"
readonly SOURCE_PATH="/path/to/cloud-drive/live-streaming-oci3test/22966160"
readonly BASE_PATH="/opt/bilive/autoslice"
readonly RECORDER_PATH="/opt/bilive/recording"
readonly MAIN_PYTHON_PATH="/opt/bilive/autoslice/venv-main/bin/python"
readonly DIAR_PYTHON_PATH="/opt/bilive/autoslice/venv-diar/bin/python"
readonly AGY_PATH="agy"

die() {
    printf 'OCI3_NO_UPLOAD_SOAK_REFUSE: %s\n' "$*" >&2
    exit 2
}

usage() {
    cat >&2 <<'USAGE'
usage: oci3_no_upload_soak.sh activate|run|verify|deactivate
USAGE
}

MODE=${1:-}
[[ $# == 1 ]] || { usage; exit 2; }
case "$MODE" in
    activate|run|verify|deactivate) ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
esac

TEST_MODE=${OCI3_NO_UPLOAD_SOAK_TEST_MODE:-0}
[[ "$TEST_MODE" == 0 || "$TEST_MODE" == 1 ]] || die 'test mode must be 0 or 1'
TEST_ROOT=${OCI3_NO_UPLOAD_SOAK_TEST_ROOT:-}
if [[ "$TEST_MODE" == 1 ]]; then
    [[ -n "$TEST_ROOT" && -d "$TEST_ROOT" && ! -L "$TEST_ROOT" ]] || \
        die 'test mode requires an existing real OCI3_NO_UPLOAD_SOAK_TEST_ROOT'
    TEST_ROOT=$(cd -- "$TEST_ROOT" && pwd -P)
    BASE="$TEST_ROOT/opt/bilive/autoslice"
    REPO="$BASE/repo"
    MOUNT_ROOT="$TEST_ROOT$MOUNT_PATH"
    SOURCE_ROOT="$TEST_ROOT$SOURCE_PATH"
    RECORDER_ROOT="$TEST_ROOT$RECORDER_PATH"
    MAIN_PYTHON=${OCI3_NO_UPLOAD_SOAK_TEST_MAIN_PYTHON:-$(command -v python3 || true)}
    DIAR_PYTHON=${OCI3_NO_UPLOAD_SOAK_TEST_DIAR_PYTHON:-$MAIN_PYTHON}
    AGY_BIN=${OCI3_NO_UPLOAD_SOAK_TEST_AGY:-$TEST_ROOT$AGY_PATH}
    OBSERVED_HOST=${OCI3_NO_UPLOAD_SOAK_TEST_HOSTNAME:-$EXPECTED_HOST}
    OBSERVED_ARCH=${OCI3_NO_UPLOAD_SOAK_TEST_ARCH:-$EXPECTED_ARCH}
    EXPECTED_UID=${OCI3_NO_UPLOAD_SOAK_TEST_UID:-$(id -u)}
    EXPECTED_GID=${OCI3_NO_UPLOAD_SOAK_TEST_GID:-$(id -g)}
else
    [[ "$(id -u)" == 0 && "$(id -un)" == root ]] || die 'production mode requires root'
    OBSERVED_HOST=$(hostname -s 2>/dev/null || hostname)
    OBSERVED_ARCH=$(uname -m)
    EXPECTED_UID=0
    EXPECTED_GID=0
    BASE="$BASE_PATH"
    REPO="$BASE/repo"
    MOUNT_ROOT="$MOUNT_PATH"
    SOURCE_ROOT="$SOURCE_PATH"
    RECORDER_ROOT="$RECORDER_PATH"
    MAIN_PYTHON="$MAIN_PYTHON_PATH"
    DIAR_PYTHON="$DIAR_PYTHON_PATH"
    AGY_BIN="$AGY_PATH"
fi

[[ "$OBSERVED_HOST" == "$EXPECTED_HOST" ]] || die "hostname must be $EXPECTED_HOST (observed $OBSERVED_HOST)"
[[ "$OBSERVED_ARCH" == "$EXPECTED_ARCH" ]] || die "architecture must be $EXPECTED_ARCH (observed $OBSERVED_ARCH)"
[[ "$EXPECTED_UID" =~ ^[0-9]+$ && "$EXPECTED_GID" =~ ^[0-9]+$ ]] || die 'owner identity is malformed'

readonly DISABLED="$BASE/DISABLED"
readonly CPA_ENV="$BASE/cpa.env"
readonly STATUS_PATH="$RECORDER_ROOT/status.json"
readonly ADAPTER_STATE_PATH="$RECORDER_ROOT/adapter-state.json"
readonly VAD_SCRIPT="$REPO/scripts/silero_vad_spans.py"
readonly VAD_MODEL="$REPO/assets/vad/silero_vad.onnx"
readonly MODEL_DIR="$BASE/models/campp"
readonly VOICEPRINT_DIR="$BASE/voiceprints/lidousha"
readonly LOCK_PATH="$BASE/oci3-no-upload-soak.lock"
readonly CONTROL_LOCK_PATH="$BASE/.oci3-no-upload-soak.control.lock"
readonly LOG_PATH="$BASE/logs/oci3-no-upload-soak.log"
readonly OWNERSHIP_TOKEN="AUTOSLICE_OCI3_NO_UPLOAD_SOAK=1"
readonly DEPLOY_LOCK_PATH="$BASE/.oci3-python-runtime.lock"
readonly ASSET_LOCK_PATH="$BASE/.oci3-runtime-assets-install.lock"
readonly FREE_TICK_LOCK_PATH="$BASE/tick.lock"
readonly FREE_RUNNER_LOCK_PATH="$BASE/runner.lock"
readonly SHADOW_TICK_LOCK_PATH="/opt/bilive/autoslice-shadow/tick.lock"
readonly SHADOW_RUNNER_LOCK_PATH="/opt/bilive/autoslice-shadow/runner.lock"

ENGINE_PYTHON=${OCI3_NO_UPLOAD_SOAK_TEST_ENGINE_PYTHON:-$(command -v python3 2>/dev/null || true)}
[[ -n "$ENGINE_PYTHON" && -x "$ENGINE_PYTHON" ]] || die 'python3 is required for read-only gates'
FLOCK_BIN=${OCI3_NO_UPLOAD_SOAK_TEST_FLOCK:-$(command -v flock 2>/dev/null || true)}
CRONTAB_BIN=${OCI3_NO_UPLOAD_SOAK_TEST_CRONTAB:-$(command -v crontab 2>/dev/null || true)}
FINDMNT_BIN=${OCI3_NO_UPLOAD_SOAK_TEST_FINDMNT:-$(command -v findmnt 2>/dev/null || true)}
TIMEOUT_BIN=${OCI3_NO_UPLOAD_SOAK_TEST_TIMEOUT:-$(command -v timeout 2>/dev/null || true)}
[[ -n "$FLOCK_BIN" && -x "$FLOCK_BIN" ]] || die 'flock is required'
[[ -n "$CRONTAB_BIN" && -x "$CRONTAB_BIN" ]] || die 'crontab is required'
[[ -n "$FINDMNT_BIN" && -x "$FINDMNT_BIN" ]] || die 'findmnt is required'
[[ -n "$TIMEOUT_BIN" && -x "$TIMEOUT_BIN" ]] || die 'timeout is required'

if [[ "$TEST_MODE" == 1 ]]; then
    case "$BASE" in "$TEST_ROOT"|"$TEST_ROOT"/*) ;; *) die 'test base escapes test root' ;; esac
    case "$SOURCE_ROOT" in "$TEST_ROOT"|"$TEST_ROOT"/*) ;; *) die 'test source escapes test root' ;; esac
fi

RUNNER_ENV="env -u AUTO_UPLOAD -u GEMINI_PAID_BACKUP_DEV_EXCEPTION -u AUTOSLICE_CORRECTION_MODE -u AUTOSLICE_IGNORE_LIVE_HOLD ${OWNERSHIP_TOKEN} AUTOSLICE_BASE=${BASE} AUTOSLICE_REC_ROOT=${SOURCE_ROOT} AUTOSLICE_CANONICAL_REC_ROOT=${SOURCE_ROOT} AUTOSLICE_RECORDER_STATUS_PATH=${STATUS_PATH} AUTOSLICE_RECORDER_ADAPTER_STATE_PATH=${ADAPTER_STATE_PATH} AUTOSLICE_CPA_ENV=${CPA_ENV} AUTOSLICE_ROOM=${ROOM} AUTOSLICE_START_DATE=${START_DATE} AUTOSLICE_AUTOMATIC_MAINTENANCE_NOT_BEFORE=${START_DATE} AUTOSLICE_MAX_PARALLEL_PRODUCE=1 GEMINI_PAID_BACKUP_DAILY_CAP=0 AUTOSLICE_AGY_HOST=localhost AGY_BIN=${AGY_BIN} AUTOSLICE_AGY_BIN=${AGY_BIN} AGY_REMOTE_BIN=${AGY_BIN} AUTOSLICE_SPEAKER_MODE=uniform_host AUTOSLICE_SPEAKER_PYTHON=${DIAR_PYTHON} AUTOSLICE_HOST_VOCAL_PYTHON=${DIAR_PYTHON} AUTOSLICE_MAIN_PYTHON=${MAIN_PYTHON} AUTOSLICE_HOST_VOCAL_MODEL_DIR=${MODEL_DIR} AUTOSLICE_HOST_VOCAL_REFERENCE_DIR=${VOICEPRINT_DIR} AUTOSLICE_VAD_SCRIPT=${VAD_SCRIPT} AUTOSLICE_VAD_MODEL=${VAD_MODEL} AUTOSLICE_VAD_PYTHON=${MAIN_PYTHON} AUTOSLICE_SHADOW_ONLY=1 AUTOSLICE_UPLOAD_ENABLED=0 PYTHONPATH=${REPO}"
# The cron entry intentionally invokes the runner directly, matching Free's
# established ten-minute contract. ``run`` is the operator one-shot with a
# full preflight; re-hashing the deployed tree on every tick would add a
# second scheduler/lock protocol without improving the already-verified bind.
RUNNER_COMMAND="cd ${REPO} && ${RUNNER_ENV} ${MAIN_PYTHON} scripts/session_autoslice.py --once"
MANAGED_CRON="*/10 * * * * /usr/bin/flock -n ${LOCK_PATH} /bin/bash -lc '${RUNNER_COMMAND}' >> ${LOG_PATH} 2>&1"

path_regular() {
    local path=$1 mode=$2
    [[ -f "$path" && ! -L "$path" ]] || die "required regular file is missing or symlinked: $path"
    [[ "$(stat -c '%h' -- "$path")" == 1 ]] || die "required file has unexpected hard links: $path"
    [[ "$(stat -c '%u' -- "$path")" == "$EXPECTED_UID" ]] || die "file owner mismatch: $path"
    [[ "$(stat -c '%g' -- "$path")" == "$EXPECTED_GID" ]] || die "file group mismatch: $path"
    [[ "$(stat -c '%a' -- "$path")" == "$mode" ]] || die "file mode mismatch for $path (expected $mode)"
}

path_executable() {
    local path=$1 resolved
    [[ -f "$path" && -x "$path" ]] || die "required executable is missing: $path"
    resolved=$("$ENGINE_PYTHON" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$path") || die "cannot resolve executable: $path"
    [[ -f "$resolved" && ! -L "$resolved" ]] || die "resolved executable is not regular: $path"
    [[ "$(stat -c '%u' -- "$resolved")" == "$EXPECTED_UID" ]] || die "executable owner mismatch: $path"
    [[ "$(stat -c '%a' -- "$resolved")" == 755 ]] || die "executable mode mismatch: $path"
}

path_directory() {
    local path=$1
    [[ -d "$path" && ! -L "$path" ]] || die "required directory is missing or symlinked: $path"
    [[ "$(stat -c '%u' -- "$path")" == "$EXPECTED_UID" ]] || die "directory owner mismatch: $path"
}

verify_identity() {
    [[ "$OBSERVED_HOST" == "$EXPECTED_HOST" && "$OBSERVED_ARCH" == "$EXPECTED_ARCH" ]] || die 'OCI3 identity drifted'
    if [[ "$TEST_MODE" == 0 ]]; then
        [[ "$(id -u)" == 0 && "$(id -un)" == root ]] || die 'effective user is not root'
    fi
}

verify_repo_authority() {
    path_directory "$REPO"
    path_regular "$REPO/DEPLOYED_COMMIT" 644
    path_regular "$REPO/DEPLOYED_MANIFEST.json" 644
    path_regular "$REPO/DEPLOYED_AUTHORITY_MANIFEST.json" 644
    "$ENGINE_PYTHON" - "$REPO" "$EXPECTED_UID" "$EXPECTED_GID" <<'PY_AUTHORITY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath

root = Path(sys.argv[1])
owner = int(sys.argv[2])
group = int(sys.argv[3])

def regular(path: Path) -> os.stat_result:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SystemExit(f"authority path is not a regular file: {path}")
    if info.st_uid != owner or info.st_gid != group:
        raise SystemExit(f"authority owner mismatch: {path}")
    return info

def load(path: Path):
    regular(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"authority JSON is invalid: {path}") from exc

commit = root.joinpath("DEPLOYED_COMMIT").read_text(encoding="utf-8").split(maxsplit=1)[0]
if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
    raise SystemExit("deployed commit identity is invalid")
manifest = load(root / "DEPLOYED_MANIFEST.json")
if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "commit", "entries", "tree_sha256"}:
    raise SystemExit("deployed repository manifest shape is invalid")
if manifest.get("schema_version") != "oci3-shadow-autoslice-repo-manifest.v1" or manifest.get("commit") != commit:
    raise SystemExit("deployed repository manifest identity is invalid")
entries = manifest.get("entries")
if not isinstance(entries, dict) or not entries:
    raise SystemExit("deployed repository manifest has no entries")
canonical = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
if hashlib.sha256(canonical).hexdigest() != manifest.get("tree_sha256"):
    raise SystemExit("deployed repository manifest tree hash drifted")
for relative, expected in entries.items():
    if not isinstance(relative, str):
        raise SystemExit(f"unsafe deployed manifest path: {relative!r}")
    parsed = PurePosixPath(relative)
    if parsed.is_absolute() or ".." in parsed.parts or relative in {"", "."} or parsed.as_posix() != relative:
        raise SystemExit(f"unsafe deployed manifest path: {relative!r}")
    path = root / Path(relative)
    if not isinstance(expected, dict):
        raise SystemExit(f"unsafe deployed manifest entry: {relative}")
    cursor = root
    for part in parsed.parts:
        cursor /= part
        info = os.lstat(cursor)
        if stat.S_ISLNK(info.st_mode):
            raise SystemExit(f"authority path contains symlink: {relative}")
    info = os.lstat(cursor)
    if stat.S_ISDIR(info.st_mode):
        if set(expected) != {"type", "mode"} or expected.get("type") != "dir" or not isinstance(expected.get("mode"), int):
            raise SystemExit(f"deployed directory entry drifted: {relative}")
        if stat.S_IMODE(info.st_mode) & ~0o022 != expected.get("mode"):
            raise SystemExit(f"deployed directory mode drifted: {relative}")
    elif stat.S_ISREG(info.st_mode):
        if set(expected) != {"type", "mode", "sha256"} or expected.get("type") != "file" or not isinstance(expected.get("mode"), int) or not isinstance(expected.get("sha256"), str):
            raise SystemExit(f"deployed file entry drifted: {relative}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected.get("sha256") or stat.S_IMODE(info.st_mode) & ~0o022 != expected.get("mode"):
            raise SystemExit(f"deployed file bytes or mode drifted: {relative}")
    else:
        raise SystemExit(f"deployed manifest entry is special: {relative}")

sys.path.insert(0, str(root))
from src.autoslice.repository_asset_authority import require_repository_asset_authority  # noqa: E402

authority_path = root / "DEPLOYED_AUTHORITY_MANIFEST.json"
authority_manifest = load(authority_path)
if not isinstance(authority_manifest, dict) or authority_manifest.get("deployed_commit") != commit:
    raise SystemExit("deployed authority manifest identity is invalid")
authority_entries = authority_manifest.get("entries")
if not isinstance(authority_entries, dict) or not authority_entries:
    raise SystemExit("deployed authority manifest has no entries")
for relative in authority_entries:
    path = root / Path(relative)
    observed = path.read_bytes()
    bound = require_repository_asset_authority(repo_root=root, relative_path=Path(relative), observed_bytes=observed)
    if bound.mode != "DEPLOYED_MANIFEST" or bound.commit != commit:
        raise SystemExit(f"deployed authority loader rejected: {relative}")
PY_AUTHORITY
}

verify_source_mount() {
    path_directory "$SOURCE_ROOT"
    local mount_line target fstype source
    mount_line=$("$FINDMNT_BIN" -T "$SOURCE_ROOT" -n -o TARGET,FSTYPE,SOURCE) || die 'CloudFS findmnt probe failed'
    read -r target fstype source <<<"$mount_line"
    [[ "$target" == "$MOUNT_ROOT" ]] || die "CloudFS mount target mismatch: $target"
    case "$fstype" in fuse|fuse.*) ;; *) die "recording source is not FUSE: $fstype" ;; esac
    [[ "$source" == CloudFS ]] || die "recording source is not CloudFS: $source"
    "$TIMEOUT_BIN" 15 /bin/ls -d -- "$SOURCE_ROOT" >/dev/null || die 'CloudFS source probe failed'
}

verify_recorder_state() {
    path_regular "$STATUS_PATH" 644
    path_regular "$ADAPTER_STATE_PATH" 600
    "$ENGINE_PYTHON" - "$STATUS_PATH" "$ADAPTER_STATE_PATH" "$ROOM" <<'PY_STATE'
import json
import sys
import time
from pathlib import Path

status_path, adapter_path, room = Path(sys.argv[1]), Path(sys.argv[2]), str(sys.argv[3])

def read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"recorder state is invalid: {path}") from exc

status = read(status_path)
if not isinstance(status, dict) or status.get("schema_version") != "recorder-neutral-status.v1":
    raise SystemExit("recorder status schema mismatch")
if str(status.get("room_id")) != room:
    raise SystemExit("recorder status room mismatch")
if status.get("service_reachable") is not True or status.get("error"):
    raise SystemExit("recorder status is not healthy")
stamp = status.get("generated_at_epoch")
if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
    raise SystemExit("recorder status timestamp is invalid")
age = time.time() - float(stamp)
if age < -300 or age > 180:
    raise SystemExit(f"recorder status is stale ({age:.0f}s)")
live_status = status.get("live_status")
if type(live_status) not in {bool, int} or live_status not in (0, 1, False, True):
    raise SystemExit("recorder live status is unknown")
adapter = read(adapter_path)
if not isinstance(adapter, dict) or adapter.get("schema_version") != "bililive-recorder-adapter-state.v1":
    raise SystemExit("recorder adapter state schema mismatch")
if adapter.get("room_id") is not None and str(adapter.get("room_id")) != room:
    raise SystemExit("recorder adapter state room mismatch")
PY_STATE
}

verify_cpa_and_assets() {
    path_regular "$CPA_ENV" 600
    grep -Eq '^CPA_BASE_URL=[^[:space:]]' "$CPA_ENV" || die 'CPA_BASE_URL is missing'
    grep -Eq '^CPA_API_KEY=.' "$CPA_ENV" || die 'CPA_API_KEY is missing'
    path_regular "$REPO/scripts/session_autoslice.py" 644
    path_regular "$VAD_SCRIPT" 644
    path_regular "$VAD_MODEL" 644
    path_regular "$REPO/assets/lidousha/voiceprint_profile.v1.json" 644
    path_directory "$MODEL_DIR"
    path_directory "$VOICEPRINT_DIR"
    path_executable "$MAIN_PYTHON"
    path_executable "$DIAR_PYTHON"
    path_regular "$AGY_BIN" 755
    "$TIMEOUT_BIN" 10 "$MAIN_PYTHON" --version >/dev/null 2>&1 || die 'venv-main is not runnable'
    "$TIMEOUT_BIN" 10 "$DIAR_PYTHON" --version >/dev/null 2>&1 || die 'venv-diar is not runnable'
    env -i PATH=/usr/bin:/bin "$TIMEOUT_BIN" 10 "$AGY_BIN" --version >/dev/null 2>&1 || die 'native AGY is not runnable'
    if [[ "$TEST_MODE" == 0 ]]; then
        path_regular "$BASE/reports/oci3-python-runtime.json" 600
        path_regular "$BASE/reports/agy-install-v1.1.22.json" 600
        path_regular "$BASE/reports/oci3-runtime-assets-v1.json" 600
    fi
}

verify_forbidden_state() {
    local root marker
    for root in "$BASE" "$REPO"; do
        for marker in AUTO_UPLOAD deploy.guard upload.lock; do
            [[ ! -e "$root/$marker" && ! -L "$root/$marker" ]] || die "$marker must be absent: $root"
        done
    done
}

cron_read() {
    local output=$1 errors rc
    errors=$(mktemp)
    if "$CRONTAB_BIN" -l >"$output" 2>"$errors"; then
        rm -f -- "$errors"
        return 0
    fi
    rc=$?
    if grep -Eqi 'no crontab|no crontab for' "$errors"; then
        : >"$output"
        rm -f -- "$errors"
        return 0
    fi
    cat "$errors" >&2
    rm -f -- "$errors"
    return "$rc"
}

cron_line_count() {
    local file=$1
    awk -v exact="$MANAGED_CRON" '$0 == exact { n++ } END { print n + 0 }' "$file"
}

cron_owned_drift_count() {
    local file=$1
    awk -v token="$OWNERSHIP_TOKEN" -v exact="$MANAGED_CRON" 'index($0, token) && $0 != exact { n++ } END { print n + 0 }' "$file"
}

verify_cron_file() {
    local file=$1 expected_count=$2
    [[ "$(cron_owned_drift_count "$file")" == 0 ]] || die 'OCI3 soak cron ownership token has drifted'
    [[ "$(cron_line_count "$file")" == "$expected_count" ]] || die "OCI3 soak cron count is not $expected_count"
}

cron_install() {
    local file=$1
    "$CRONTAB_BIN" "$file" || die 'crontab install failed'
}

cron_readback_matches() {
    local expected=$1 actual
    actual=$(mktemp)
    cron_read "$actual" || { rm -f -- "$actual"; die 'crontab readback failed'; }
    cmp -s -- "$expected" "$actual" || { rm -f -- "$actual"; die 'crontab readback differs'; }
    rm -f -- "$actual"
}

acquire_control_lock() {
    [[ ! -L "$CONTROL_LOCK_PATH" ]] || die 'control lock is symlinked'
    if [[ ! -e "$CONTROL_LOCK_PATH" ]]; then
        (set -C; : >"$CONTROL_LOCK_PATH") || die 'cannot create control lock'
        chmod 600 "$CONTROL_LOCK_PATH"
    fi
    path_regular "$CONTROL_LOCK_PATH" 600
    exec 9>>"$CONTROL_LOCK_PATH"
    "$FLOCK_BIN" -n 9 || die 'another OCI3 soak control operation is active'
}

acquire_run_lock() {
    local create=${1:-0}
    [[ ! -L "$LOCK_PATH" ]] || die 'soak run lock is symlinked'
    if [[ ! -e "$LOCK_PATH" ]]; then
        [[ "$create" == 1 ]] || die 'soak run lock is missing'
        (set -C; : >"$LOCK_PATH") || die 'cannot create soak run lock'
        chmod 600 "$LOCK_PATH"
        RUN_LOCK_CREATED=1
    fi
    path_regular "$LOCK_PATH" 600
    exec 8>>"$LOCK_PATH"
    "$FLOCK_BIN" -n 8 || die 'soak runner lock is busy'
}

check_run_lock_idle() {
    [[ ! -L "$LOCK_PATH" ]] || die 'soak run lock is symlinked'
    [[ -e "$LOCK_PATH" ]] || die 'soak run lock is missing'
    path_regular "$LOCK_PATH" 600
    (exec 8>>"$LOCK_PATH"; "$FLOCK_BIN" -n 8) || die 'soak runner lock is busy'
}

check_known_locks_idle() {
    local lock
    for lock in "$DEPLOY_LOCK_PATH" "$ASSET_LOCK_PATH" "$FREE_TICK_LOCK_PATH" "$FREE_RUNNER_LOCK_PATH" \
        "$SHADOW_TICK_LOCK_PATH" "$SHADOW_RUNNER_LOCK_PATH"; do
        [[ ! -e "$lock" ]] && continue
        [[ -f "$lock" && ! -L "$lock" ]] || die "known lock is not a regular file: $lock"
        (exec 7>>"$lock"; "$FLOCK_BIN" -n 7) || die "known lock is busy: $lock"
    done
}

validate_disabled() {
    path_regular "$DISABLED" 644
    [[ ! -s "$DISABLED" ]] || die 'DISABLED must be empty'
}

validate_active_files() {
    path_directory "$BASE"
    verify_repo_authority
    verify_source_mount
    verify_recorder_state
    verify_cpa_and_assets
    verify_forbidden_state
}

ORIGINAL_CRON=
CRON_CANDIDATE=
DISABLED_BACKUP=
DISABLED_PRESENT=0
CRON_MUTATED=0
RUN_LOCK_CREATED=0

rollback_activation() {
    local rc=$? rollback_actual
    trap - EXIT INT TERM HUP
    set +e
    if [[ "$CRON_MUTATED" == 1 && -n "${ORIGINAL_CRON:-}" ]]; then
        "$CRONTAB_BIN" "$ORIGINAL_CRON" >/dev/null 2>&1 || true
        rollback_actual=$(mktemp)
        "$CRONTAB_BIN" -l >"$rollback_actual" 2>/dev/null || :
        cmp -s -- "$ORIGINAL_CRON" "$rollback_actual" || true
        rm -f -- "$rollback_actual"
    fi
    if [[ "$DISABLED_PRESENT" == 1 && -n "${DISABLED_BACKUP:-}" ]]; then
        cp -- "$DISABLED_BACKUP" "$DISABLED"
        chmod 644 "$DISABLED"
    elif [[ "$DISABLED_PRESENT" == 0 ]]; then
        rm -f -- "$DISABLED"
    fi
    if [[ "$RUN_LOCK_CREATED" == 1 ]]; then rm -f -- "$LOCK_PATH"; fi
    rm -f -- "$ORIGINAL_CRON" "$CRON_CANDIDATE" "$DISABLED_BACKUP"
    exit "$rc"
}

activate() {
    verify_identity
    [[ -d "$BASE" && ! -L "$BASE" ]] || die 'canonical base is missing or symlinked'
    validate_active_files
    check_known_locks_idle
    acquire_control_lock
    acquire_run_lock 1

    ORIGINAL_CRON=$(mktemp)
    cron_read "$ORIGINAL_CRON" || die 'cannot read current crontab'
    [[ "$(cron_owned_drift_count "$ORIGINAL_CRON")" == 0 ]] || die 'OCI3 soak cron ownership token has drifted'
    [[ "$(cron_line_count "$ORIGINAL_CRON")" == 0 || "$(cron_line_count "$ORIGINAL_CRON")" == 1 ]] || die 'duplicate OCI3 soak cron entries found'
    if [[ "$(cron_line_count "$ORIGINAL_CRON")" == 1 && ! -e "$DISABLED" && ! -L "$DISABLED" ]]; then
        verify_cron_file "$ORIGINAL_CRON" 1
        rm -f -- "$ORIGINAL_CRON"
        printf 'OCI3_NO_UPLOAD_SOAK_ACTIVE: cron=10m start_date=%s parallel=1 paid_backup_cap=0\n' "$START_DATE"
        return 0
    fi
    validate_disabled
    DISABLED_PRESENT=1
    DISABLED_BACKUP=$(mktemp)
    trap rollback_activation EXIT INT TERM HUP
    cp -- "$DISABLED" "$DISABLED_BACKUP"

    if [[ "$(cron_line_count "$ORIGINAL_CRON")" == 0 ]]; then
        CRON_CANDIDATE=$(mktemp)
        cat -- "$ORIGINAL_CRON" >"$CRON_CANDIDATE"
        if [[ -s "$ORIGINAL_CRON" && "$(tail -c 1 -- "$ORIGINAL_CRON" | od -An -t x1 | tr -d '[:space:]')" != 0a ]]; then
            printf '\n' >>"$CRON_CANDIDATE"
        fi
        printf '%s\n' "$MANAGED_CRON" >>"$CRON_CANDIDATE"
        CRON_MUTATED=1
        cron_install "$CRON_CANDIDATE"
        cron_readback_matches "$CRON_CANDIDATE"
    fi
    [[ "${OCI3_NO_UPLOAD_SOAK_TEST_FAIL_AFTER_CRON:-0}" == 1 ]] && die 'test failure after cron readback'
    rm -f -- "$DISABLED"
    [[ ! -e "$DISABLED" && ! -L "$DISABLED" ]] || die 'DISABLED removal readback failed'
    trap - EXIT INT TERM HUP
    rm -f -- "$ORIGINAL_CRON" "$CRON_CANDIDATE" "$DISABLED_BACKUP"
    printf 'OCI3_NO_UPLOAD_SOAK_ACTIVE: cron=10m start_date=%s parallel=1 paid_backup_cap=0\n' "$START_DATE"
}

verify() {
    verify_identity
    path_directory "$BASE"
    validate_active_files
    path_regular "$LOCK_PATH" 600
    check_known_locks_idle
    acquire_control_lock
    check_run_lock_idle
    local current
    current=$(mktemp)
    cron_read "$current" || die 'cannot read current crontab'
    verify_cron_file "$current" 1
    [[ ! -e "$DISABLED" && ! -L "$DISABLED" ]] || die 'DISABLED must be absent in active steady state'
    rm -f -- "$current"
    printf 'OCI3_NO_UPLOAD_SOAK_VERIFIED: cron=exact live_hold=real no_upload=1 start_date=%s\n' "$START_DATE"
}

run_once() {
    verify
    exec "$FLOCK_BIN" -n "$LOCK_PATH" /bin/bash -lc "$RUNNER_COMMAND"
}

deactivate() {
    verify_identity
    [[ -d "$BASE" && ! -L "$BASE" ]] || die 'canonical base is missing or symlinked'
    acquire_control_lock
    if [[ -e "$DISABLED" || -L "$DISABLED" ]]; then
        validate_disabled
    else
        (set -C; : >"$DISABLED") || die 'cannot create DISABLED'
        chmod 644 "$DISABLED"
        validate_disabled
    fi
    if [[ -e "$LOCK_PATH" ]]; then
        acquire_run_lock 0
    fi
    local current candidate count
    current=$(mktemp)
    cron_read "$current" || die 'cannot read current crontab'
    count=$(cron_line_count "$current")
    [[ "$(cron_owned_drift_count "$current")" == 0 ]] || die 'OCI3 soak cron ownership token has drifted'
    [[ "$count" == 0 || "$count" == 1 ]] || die 'duplicate OCI3 soak cron entries found'
    if [[ "$count" == 1 ]]; then
        candidate=$(mktemp)
        grep -Fvx -- "$MANAGED_CRON" "$current" >"$candidate" || [[ $? == 1 ]]
        cron_install "$candidate"
        cron_readback_matches "$candidate"
        rm -f -- "$candidate"
    fi
    rm -f -- "$current"
    validate_disabled
    printf 'OCI3_NO_UPLOAD_SOAK_DISABLED: cron_removed=1 foreign_crons=preserved\n'
}

case "$MODE" in
    activate) activate ;;
    run) run_once ;;
    verify) verify ;;
    deactivate) deactivate ;;
esac
