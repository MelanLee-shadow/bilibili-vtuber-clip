#!/bin/bash
# Deploy the autoslice runner to free (2026-07-09 audit: no more dirty-tree
# deploys, no more "which code is production actually running?").
#
# - REFUSES a dirty working tree (production must be reproducible from a commit)
# - streams committed runtime trees plus project authority/docs into staging
#   (scripts/ src/ ops/ assets/ profiles/ .agent/ docs/ cleanup_manifests/
#   AGENTS.md README.md; ignored files excluded)
# - verifies the complete staged file list and SHA-256 manifest
# - owns a remote deploy guard from initial observation through final cleanup
# - pauses new runs, waits for runner.lock, then swaps every managed component with rollback
# - verifies rollback against a full path/type/mode/SHA-256 manifest
# - seals DEPLOYED_COMMIT plus a commit-bound authority-asset manifest only
#   after every runtime/external-file and complete-tree check succeeds
# - installs the mount watchdog + its cron line (idempotent)
# - installs the guarded do_upload.sh (refuses bare invocation)
# - md5-verifies the runner after push (sync lesson: never swallow errors)
set -euo pipefail
HOST="${1:-free}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ "${1:-}" = "--recover-deploy-guard" ]; then
    RECOVERY_OWNER=${2:-}
    RECOVERY_HOST=${3:-free}
    if [ "$#" -lt 2 ] || [ "$#" -gt 3 ] || ! [[ "$RECOVERY_OWNER" =~ ^[0-9a-f]{40}-[0-9]{8}T[0-9]{6}Z-[0-9]+$ ]]; then
        echo "usage: $0 --recover-deploy-guard <exact-owner> [host]" >&2
        exit 2
    fi
    RECOVERY_STAGE_PROBE=$(ssh "$RECOVERY_HOST" bash -s -- /opt/bilive/autoslice "$RECOVERY_OWNER" <<'REMOTE_PREBACKUP_STAGE_PROBE'
set -euo pipefail
base=$1
owner=$2
commit=${owner%%-*}
guard=$base/deploy.guard
backup=$base/repo.rollback-$commit
stage=$base/repo.deploy-$commit
test -d "$guard" && test ! -L "$guard"
test -f "$guard/owner" && test ! -L "$guard/owner"
test "$(cat "$guard/owner")" = "$owner"
test "$(find "$guard" -mindepth 1 -maxdepth 1 -exec printf . \; | wc -c)" -eq 1
if [ -e "$backup" ] || [ -L "$backup" ]; then
    printf '%s\n' backup-present
    exit 0
fi
test -d "$stage"
test ! -L "$stage"
python3 - "$stage" <<'PY_PREBACKUP_STAGE_INVENTORY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
entries = {"": {"type": "dir", "mode": stat.S_IMODE(root.stat().st_mode)}}
for path in sorted(root.rglob("*")):
    relative = path.relative_to(root).as_posix()
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if path.is_symlink():
        raise SystemExit(f"unsafe staging symlink: {relative}")
    if path.is_dir():
        entries[relative] = {"type": "dir", "mode": mode}
    elif path.is_file():
        entries[relative] = {
            "type": "file",
            "mode": mode,
            "size": info.st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    else:
        raise SystemExit(f"unsafe staging entry: {relative}")
canonical = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
print(json.dumps({"schema_version": "deploy-prebackup-stage-inventory.v1", "entries": entries,
                  "tree_sha256": hashlib.sha256(canonical).hexdigest()},
                 ensure_ascii=False, sort_keys=True, separators=(",", ":")))
PY_PREBACKUP_STAGE_INVENTORY
REMOTE_PREBACKUP_STAGE_PROBE
)
    if [ "$RECOVERY_STAGE_PROBE" != backup-present ]; then
        RECOVERY_STAGE_ANALYSIS=$(python3 - "$RECOVERY_OWNER" "$RECOVERY_STAGE_PROBE" <<'PY_LOCAL_PREBACKUP_STAGE'
import hashlib
import io
import json
import subprocess
import sys
import tarfile

owner, raw = sys.argv[1:]
commit = owner.split("-", 1)[0]
inventory = json.loads(raw)
assert inventory.get("schema_version") == "deploy-prebackup-stage-inventory.v1"
entries = inventory.get("entries")
assert isinstance(entries, dict)
assert entries.get("") == {"type": "dir", "mode": 0o755}
actual_files = {path: entry for path, entry in entries.items() if path and entry.get("type") == "file"}
assert all(entry.get("type") in {"file", "dir"} for entry in entries.values())
archive = subprocess.check_output([
    "git", "archive", "--format=tar", commit,
    "scripts", "src", "ops", "assets", "profiles", ".agent", "docs", "cleanup_manifests",
    "AGENTS.md", "README.md",
])
expected_members = []
with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
    for member in bundle:
        if member.isdir():
            expected_members.append(("dir", member.name.rstrip("/"), member.mode & ~0o022, None))
        else:
            assert member.isfile(), f"unexpected archive member: {member.name}"
            expected_members.append(
                ("file", member.name, member.mode & ~0o022, bundle.extractfile(member).read())
            )
seen = []
partial = None
stopped = False
for kind, name, mode, content in expected_members:
    if stopped:
        break
    if kind == "dir":
        entry = entries.get(name)
        assert entry is not None and entry.get("type") == "dir", name
        continue
    entry = actual_files.get(name)
    if entry is None:
        stopped = True
        break
    actual_size = entry.get("size")
    assert isinstance(actual_size, int) and 0 < actual_size <= len(content)
    if actual_size == len(content):
        assert entry.get("mode") == mode, (name, entry.get("mode"), mode)
        assert entry.get("sha256") == hashlib.sha256(content).hexdigest(), name
        seen.append(name)
        continue
    assert partial is None
    # GNU tar creates an interrupted member under its temporary 0600 mode;
    # completed members have already had --no-same-permissions plus umask 022.
    assert entry.get("mode") == 0o600, (name, entry.get("mode"), 0o600)
    assert entry.get("sha256") == hashlib.sha256(content[:actual_size]).hexdigest(), name
    partial = {"path": name, "size": actual_size, "sha256": entry["sha256"]}
    seen.append(name)
    stopped = True
assert set(actual_files) == set(seen)
expected_files = [(name, mode, content) for kind, name, mode, content in expected_members if kind == "file"]
assert seen == [item[0] for item in expected_files[:len(seen)]]
allowed_dirs = {""}
for name in seen:
    parts = name.split("/")[:-1]
    for index in range(1, len(parts) + 1):
        allowed_dirs.add("/".join(parts[:index]))
actual_dirs = {path for path, entry in entries.items() if entry.get("type") == "dir"}
assert actual_dirs == allowed_dirs
active_ancestors = set()
if partial is not None:
    parts = partial["path"].split("/")[:-1]
    for index in range(1, len(parts) + 1):
        active_ancestors.add("/".join(parts[:index]))
for path in actual_dirs:
    if not path:
        continue
    expected_mode = 0o700 if path in active_ancestors else 0o755
    assert entries[path].get("mode") == expected_mode, (path, entries[path].get("mode"), expected_mode)
print(json.dumps({"tree_sha256": inventory["tree_sha256"], "partial": partial},
                 ensure_ascii=False, sort_keys=True, separators=(",", ":")))
PY_LOCAL_PREBACKUP_STAGE
)
        RECOVERY_STAGE_TREE_SHA=$(python3 - "$RECOVERY_STAGE_ANALYSIS" <<'PY_TREE_SHA'
import json
import sys
payload = json.loads(sys.argv[1])
value = payload.get("tree_sha256")
assert isinstance(value, str) and len(value) == 64
print(value)
PY_TREE_SHA
)
        ssh "$RECOVERY_HOST" bash -s -- /opt/bilive/autoslice "$RECOVERY_OWNER" "$RECOVERY_STAGE_TREE_SHA" <<'REMOTE_PREBACKUP_GUARD_RECOVERY'
set -euo pipefail
base=$1
owner=$2
expected_tree_sha=$3
commit=${owner%%-*}
repo=$base/repo
backup=$base/repo.rollback-$commit
stage=$base/repo.deploy-$commit
guard=$base/deploy.guard
test -d "$guard" && test ! -L "$guard"
test -f "$guard/owner" && test ! -L "$guard/owner"
test "$(cat "$guard/owner")" = "$owner"
test "$(find "$guard" -mindepth 1 -maxdepth 1 -exec printf . \; | wc -c)" -eq 1
test ! -e "$backup"
test ! -L "$backup"
test -d "$stage" && test ! -L "$stage"
python3 - "$repo" "$stage" "$expected_tree_sha" <<'PY_PREBACKUP_RECOVER'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

repo = Path(sys.argv[1])
stage = Path(sys.argv[2])
expected_tree = sys.argv[3]
entries = {"": {"type": "dir", "mode": stat.S_IMODE(stage.stat().st_mode)}}
for path in sorted(stage.rglob("*")):
    relative = path.relative_to(stage).as_posix()
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if path.is_symlink():
        raise SystemExit(f"unsafe staging symlink: {relative}")
    if path.is_dir():
        entries[relative] = {"type": "dir", "mode": mode}
    elif path.is_file():
        entries[relative] = {"type": "file", "mode": mode, "size": info.st_size,
                             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    else:
        raise SystemExit(f"unsafe staging entry: {relative}")
actual_tree = hashlib.sha256(json.dumps(entries, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")).encode()).hexdigest()
assert actual_tree == expected_tree
commit = (repo / "DEPLOYED_COMMIT").read_text(encoding="utf-8").split(maxsplit=1)[0]
assert __import__("re").fullmatch(r"[0-9a-f]{40}", commit)
sys.path.insert(0, str(repo))
from src.autoslice.repository_asset_authority import build_deployed_authority_manifest
manifest = json.loads((repo / "DEPLOYED_AUTHORITY_MANIFEST.json").read_text(encoding="utf-8"))
registry = repo / "assets/lidousha/publication_registry.v1.json"
authority_dir = repo / "assets/lidousha"
assert registry.is_file() and not registry.is_symlink()
assert authority_dir.is_dir() and not authority_dir.is_symlink()
paths = [registry]
for candidate in authority_dir.rglob("*"):
    assert not candidate.is_symlink()
    if candidate.is_file() and candidate.suffix in {".json", ".srt"}:
        paths.append(candidate)
expected_manifest = build_deployed_authority_manifest(
    repo_root=repo, deployed_commit=commit, relative_paths=[path.relative_to(repo) for path in paths]
)
assert manifest == expected_manifest
for source, destination in (
    (repo / "scripts/free_mount_watchdog.sh", Path("/opt/bilive/autoslice/free_mount_watchdog.sh")),
    (repo / "scripts/clouddrive_upload_fatal_sentinel.sh", Path("/opt/bilive/autoslice/upload_fatal_sentinel.sh")),
    (repo / "scripts/free_do_upload.sh", Path("/opt/bilive/app/tmp_manual_upload/do_upload.sh")),
    (repo / "ops/recording/bililive_recorder_adapter.py", Path("/opt/bilive/recording/bililive_recorder_adapter.py")),
):
    assert source.is_file() and not source.is_symlink()
    assert destination.is_file() and not destination.is_symlink()
    assert hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(destination.read_bytes()).digest()
PY_PREBACKUP_RECOVER
rm -rf -- "$stage"
test "$(cat "$guard/owner")" = "$owner"
rm -f "$guard/owner"
rmdir "$guard"
REMOTE_PREBACKUP_GUARD_RECOVERY
        exit $?
    fi
    ssh "$RECOVERY_HOST" bash -s -- /opt/bilive/autoslice "$RECOVERY_OWNER" <<'REMOTE_GUARD_RECOVERY'
set -euo pipefail
base=$1
owner=$2
commit=${owner%%-*}
repo=$base/repo
backup=$base/repo.rollback-$commit
stage=$base/repo.deploy-$commit
guard=$base/deploy.guard
test -d "$guard"
test ! -L "$guard"
test -f "$guard/owner"
test ! -L "$guard/owner"
test "$(cat "$guard/owner")" = "$owner"
test "$(find "$guard" -mindepth 1 -maxdepth 1 -exec printf . \; | wc -c)" -eq 1
test -d "$backup"
test ! -L "$backup"
test -f "$backup/DEPLOYED_COMMIT.old"
test -f "$backup/repo.manifest.old.json"
test "$(awk 'NR==1 {print $1}' "$repo/DEPLOYED_COMMIT")" = "$(awk 'NR==1 {print $1}' "$backup/DEPLOYED_COMMIT.old")"
python3 - "$repo" "$backup/repo.manifest.old.json" <<'PY_GUARD_RECOVERY_MANIFEST'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
expected = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
actual = {}
for component in (
    "scripts", "src", "ops", "assets", "profiles", ".agent", "docs", "cleanup_manifests",
    "AGENTS.md", "README.md",
):
    base = root / component
    if not base.exists():
        continue
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
                "type": "file", "mode": mode, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()
            }
        else:
            actual[relative] = {"type": "other", "mode": mode}
assert actual == expected
PY_GUARD_RECOVERY_MANIFEST
verify_external() {
    label=$1
    destination=$2
    if [ -f "$backup/external/$label.present" ]; then
        test -f "$backup/external/$label.file"
        test ! -L "$backup/external/$label.file"
        test -f "$destination"
        test ! -L "$destination"
        cmp -s "$backup/external/$label.file" "$destination"
    elif [ -f "$backup/external/$label.absent" ]; then
        test ! -e "$destination"
    else
        return 1
    fi
}
verify_external watchdog /opt/bilive/autoslice/free_mount_watchdog.sh
verify_external upload_sentinel /opt/bilive/autoslice/upload_fatal_sentinel.sh
verify_external uploader /opt/bilive/app/tmp_manual_upload/do_upload.sh
verify_external recorder_adapter /opt/bilive/recording/bililive_recorder_adapter.py
marker=$backup/external/connection_stub_bootstrap.marker
state_preimage=$backup/external/connection_stub_bootstrap_preimage/adapter-state.json
status_preimage=$backup/external/connection_stub_bootstrap_preimage/status.json
receipt=/opt/bilive/recording/connection-stub-bootstrap-receipts/$commit.json
validate_bootstrap_snapshot() {
    require_marker=$1
    for path in "$state_preimage" "$status_preimage" "$receipt" \
        /opt/bilive/recording/adapter-state.json /opt/bilive/recording/status.json; do
        test -f "$path"
        test ! -L "$path"
    done
    if [ "$require_marker" = 1 ]; then
        test -f "$marker"
        test ! -L "$marker"
    else
        test ! -e "$marker"
        test -f "$backup/external/recorder_adapter.restart-required"
        test ! -L "$backup/external/recorder_adapter.restart-required"
        test -d "$stage"
        test ! -L "$stage"
    fi
    python3 - "$marker" "$state_preimage" "$status_preimage" \
        /opt/bilive/recording/adapter-state.json /opt/bilive/recording/status.json \
        "$receipt" "$commit" "$require_marker" <<'PY_GUARD_RECOVERY_BOOTSTRAP'
import hashlib
import json
import re
import sys

marker_path, state_path, status_path, live_state_path, live_status_path, receipt_path, expected_id, require_marker = sys.argv[1:]
marker = json.load(open(marker_path, encoding="utf-8")) if require_marker == "1" else None
receipt = json.load(open(receipt_path, encoding="utf-8"))
state_raw = open(state_path, "rb").read()
status_raw = open(status_path, "rb").read()
state = json.loads(state_raw)
status = json.loads(status_raw)
live_state = json.load(open(live_state_path, encoding="utf-8"))
live_status = json.load(open(live_status_path, encoding="utf-8"))

def state_material(value):
    assert isinstance(value, dict)
    material = {key: item for key, item in value.items() if key != "last_room_status_epoch"}
    cookie_health = material.get("cookie_health")
    assert isinstance(cookie_health, dict)
    material["cookie_health"] = {
        key: item for key, item in cookie_health.items()
        if key not in {"checked_at", "checked_at_epoch"}
    }
    return material

def status_projection(value):
    assert isinstance(value, dict)
    projected = json.loads(json.dumps(value))
    projected.pop("generated_at", None)
    projected.pop("generated_at_epoch", None)
    cookie_status = projected.get("bilibili_cookie")
    assert isinstance(cookie_status, dict)
    projected["bilibili_cookie"] = {
        key: item for key, item in cookie_status.items()
        if key not in {"checked_at", "checked_at_epoch"}
    }
    errors = projected.get("finalize_errors")
    if isinstance(errors, list):
        for entry in errors:
            if isinstance(entry, dict) and isinstance(entry.get("error"), str):
                entry["error"] = re.sub(r"0x[0-9a-fA-F]+", "0x<address>", entry["error"])
    return projected

def legacy_status_projection(value):
    return {
        key: value.get(key)
        for key in (
            "service_reachable", "streaming", "recording", "finalizing", "error", "finalize_errors"
        )
    }

state_material_sha256 = lambda value: hashlib.sha256(
    json.dumps(state_material(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
status_projection_sha256 = lambda value: hashlib.sha256(
    json.dumps(status_projection(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
receipt_payload = dict(receipt)
receipt_integrity = receipt_payload.pop("canonical_integrity", None)
paths = receipt.get("source_relative_paths")
expected_paths = [
    "2026-08-20/22966160_20260820-21-00-20.flv",
    "2026-08-20/22966160_20260820-21-56-14.flv",
]
expected_sources = {"/adapter/Videos/22966160/" + path for path in expected_paths}
assert receipt.get("schema_version") == "recording-connection-stub-bootstrap.v1"
assert receipt.get("receipt_id") == expected_id
assert receipt.get("adapter_state_sha256") == hashlib.sha256(state_raw).hexdigest()
if "adapter_state_material_sha256" in receipt:
    assert status_projection_sha256(status) == receipt.get("adapter_status_preimage_sha256")
else:
    # The retained 3f0f622 pre-marker incident predates durable receipt
    # projections.  Its legacy hash still binds the full exact error rows;
    # live/preimage comparison below supplies the stricter durable check.
    assert hashlib.sha256(
        json.dumps(legacy_status_projection(status), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest() == receipt.get("adapter_status_preimage_sha256")
assert state_material(live_state) == state_material(state)
assert status_projection(live_status) == status_projection(status)
assert isinstance(receipt_integrity, dict) and receipt_integrity.get("algorithm") == "sha256"
assert receipt_integrity.get("canonical_json_sha256") == hashlib.sha256(
    json.dumps(receipt_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
assert paths == expected_paths
assert status.get("error") == "2 closed recording(s) failed finalization"
errors = status.get("finalize_errors")
assert isinstance(errors, list) and len(errors) == len(expected_paths)
assert {entry.get("source") for entry in errors if isinstance(entry, dict)} == expected_sources
if marker is not None:
    marker_payload = dict(marker)
    marker_integrity = marker_payload.pop("canonical_integrity", None)
    assert marker.get("schema_version") == "recording-connection-stub-bootstrap-rollback-marker.v1"
    assert marker.get("receipt_id") == expected_id
    assert marker.get("receipt_path") == receipt_path
    assert marker.get("receipt_sha256") == hashlib.sha256(open(receipt_path, "rb").read()).hexdigest()
    assert marker.get("state_sha256") == hashlib.sha256(state_raw).hexdigest()
    assert marker.get("status_sha256") == hashlib.sha256(status_raw).hexdigest()
    assert marker.get("status_projection_sha256") == status_projection_sha256(status)
    assert isinstance(marker_integrity, dict) and marker_integrity.get("algorithm") == "sha256"
    assert marker_integrity.get("canonical_json_sha256") == hashlib.sha256(
        json.dumps(marker_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
PY_GUARD_RECOVERY_BOOTSTRAP
}
if [ -e "$marker" ]; then
    validate_bootstrap_snapshot 1
elif [ -f "$backup/external/recorder_adapter.restart-required" ]; then
    # This is the one known incomplete transaction: candidate bytes were
    # installed, but activation did not create a marker and therefore no new
    # daemon could have persisted receipt rows.  Do not turn this into a
    # generic stale-guard remover.
    validate_bootstrap_snapshot 0
else
    echo "REFUSE: guard is not an exact bootstrap rollback incident" >&2
    exit 1
fi
if [ -e "$stage" ]; then
    test -d "$stage"
    test ! -L "$stage"
fi
rm -rf -- "$stage" "$backup"
test "$(cat "$guard/owner")" = "$owner"
rm -f "$guard/owner"
rmdir "$guard"
REMOTE_GUARD_RECOVERY
    exit $?
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "REFUSE: working tree is dirty — commit first, production deploys are commit-only." >&2
    git status --short >&2
    exit 2
fi

# 2026-07-31 audit: no more red-suite deploys.
# 7/30-7/31 那轮 65 个 commit 全程只跑定向测试，架构护栏从 7/15 起红了两周没人
# 发现，最后带着 5 条失败部署进了生产。门放在这里——dirty-tree 拒绝之后、任何
# 远端动作之前，纯本地、纯只读。
# 全量、无 marker、无排除：子集豁免豁免掉的不是存量欠账，是未来所有新违规。
# 没有 bypass 开关是故意的——这个仓库里"紧急"应该去找 Ivan，不是去找环境变量。
VENV_PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
    # 既有部署惯例是从干净的临时 worktree 部署（主检出常年带未跟踪的证据树，
    # 过不了上面的 dirty 检查）。worktree 里没有 .venv——回落到主检出的 venv，
    # 测试仍然跑 worktree（= 被部署的冻结 commit）的代码。
    MAIN_CHECKOUT="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
    VENV_PYTHON="$MAIN_CHECKOUT/.venv/bin/python"
fi
if [ ! -x "$VENV_PYTHON" ]; then
    echo "REFUSE: no test venv found ($ROOT/.venv or main checkout) — deploys require it." >&2
    exit 2
fi
echo "== pre-deploy full test suite =="
if ! "$VENV_PYTHON" -m pytest -q; then
    echo "REFUSE: test suite is red — production deploys require a green suite." >&2
    exit 2
fi
# 让欠账数字每次部署都从眼前过一遍，账本才不会变成垃圾场。
"$VENV_PYTHON" - <<'LEDGER_REPORT' || true
import sys
sys.path.insert(0, "tests")
import test_runtime_architecture as arch
print(
    f"== architecture debt: {len(arch.FUNCTION_DEBT_LEDGER)} functions / "
    f"{len(arch.MODULE_DEBT_LEDGER)} modules over budget "
    "(2026-07-31 baseline 20 / 12) =="
)
LEDGER_REPORT

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
LOCAL_ARCHIVE_DIR=

cleanup() {
    rc=$?
    set +e
    rollback_ok=1
    if [ "$COMMITTED" -ne 1 ]; then
        if [ "$SWITCHED" -eq 1 ]; then
            if ! ssh "$HOST" /usr/bin/flock -w 7200 "$REMOTE_BASE/runner.lock" bash -s -- \
                "$REMOTE_REPO" "$STAGE" "$BACKUP" "$OLD_COMMIT" "$COMMIT" <<'REMOTE_ROLLBACK'
set -euo pipefail
repo=$1
stage=$2
backup=$3
old_commit=$4
new_commit=$5
test -d "$backup"
test -f "$backup/repo.manifest.old.json"
for component in scripts src ops assets profiles .agent docs cleanup_manifests AGENTS.md README.md; do
    if [ -e "$backup/$component" ]; then
        rm -rf "$stage/$component"
        if [ -e "$repo/$component" ]; then
            mkdir -p "$stage"
            mv "$repo/$component" "$stage/$component"
        fi
        mv "$backup/$component" "$repo/$component"
    elif [ -e "$backup/$component.absent" ]; then
        rm -rf "$repo/$component"  # tree added by the deploy: rollback removes it
    fi
done
test -f "$backup/DEPLOYED_COMMIT.old"

restore_repository_file() {
    label=$1
    destination=$2
    if [ -f "$backup/repository/$label.present" ]; then
        tmp=$destination.rollback.$$
        cp -p "$backup/repository/$label.file" "$tmp"
        cmp -s "$backup/repository/$label.file" "$tmp"
        mv -f "$tmp" "$destination"
        cmp -s "$backup/repository/$label.file" "$destination"
    elif [ -f "$backup/repository/$label.absent" ]; then
        rm -f "$destination"
        test ! -e "$destination"
    else
        echo "missing repository rollback marker: $label" >&2
        return 1
    fi
}
restore_repository_file deployed_authority_manifest "$repo/DEPLOYED_AUTHORITY_MANIFEST.json"
cp "$backup/DEPLOYED_COMMIT.old" "$repo/DEPLOYED_COMMIT"
cmp -s "$backup/DEPLOYED_COMMIT.old" "$repo/DEPLOYED_COMMIT"

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
external_mutation_started=0
if [ -e "$backup/external/external-mutation-started" ]; then
    test -f "$backup/external/external-mutation-started"
    test ! -L "$backup/external/external-mutation-started"
    test "$(stat -c '%a' "$backup/external/external-mutation-started")" = 600
    test "$(cat "$backup/external/external-mutation-started")" = external-mutation-started.v1
    external_mutation_started=1
fi
if [ "$external_mutation_started" -eq 1 ]; then
    restore_file watchdog /opt/bilive/autoslice/free_mount_watchdog.sh
    restore_file upload_sentinel /opt/bilive/autoslice/upload_fatal_sentinel.sh
    restore_file uploader /opt/bilive/app/tmp_manual_upload/do_upload.sh
fi
connection_stub_bootstrap_restored=0
restore_connection_stub_bootstrap_preimage() {
    marker=$backup/external/connection_stub_bootstrap.marker
    state_preimage=$backup/external/connection_stub_bootstrap_preimage/adapter-state.json
    status_preimage=$backup/external/connection_stub_bootstrap_preimage/status.json
    receipt=/opt/bilive/recording/connection-stub-bootstrap-receipts/$new_commit.json
    if [ ! -e "$marker" ]; then
        return 0
    fi
    for path in "$marker" "$state_preimage" "$status_preimage" "$receipt"; do
        test -f "$path"
        test ! -L "$path"
    done
    python3 - "$marker" "$state_preimage" "$status_preimage" "$receipt" "$new_commit" <<'PY_BOOTSTRAP_ROLLBACK_MARKER'
import hashlib
import json
import re
import sys

marker_path, state_path, status_path, receipt_path, expected_id = sys.argv[1:]
marker = json.load(open(marker_path, encoding="utf-8"))
receipt = json.load(open(receipt_path, encoding="utf-8"))
state_raw = open(state_path, "rb").read()
status_raw = open(status_path, "rb").read()
status = json.loads(status_raw)
def status_projection(value):
    projected = json.loads(json.dumps(value))
    projected.pop("generated_at", None)
    projected.pop("generated_at_epoch", None)
    cookie_status = projected.get("bilibili_cookie")
    assert isinstance(cookie_status, dict)
    projected["bilibili_cookie"] = {
        key: item for key, item in cookie_status.items()
        if key not in {"checked_at", "checked_at_epoch"}
    }
    errors = projected.get("finalize_errors")
    if isinstance(errors, list):
        for entry in errors:
            if isinstance(entry, dict) and isinstance(entry.get("error"), str):
                entry["error"] = re.sub(r"0x[0-9a-fA-F]+", "0x<address>", entry["error"])
    return projected
projection = status_projection(status)
marker_payload = dict(marker)
marker_integrity = marker_payload.pop("canonical_integrity", None)
receipt_payload = dict(receipt)
receipt_integrity = receipt_payload.pop("canonical_integrity", None)
paths = receipt.get("source_relative_paths")
expected_paths = [
    "2026-08-20/22966160_20260820-21-00-20.flv",
    "2026-08-20/22966160_20260820-21-56-14.flv",
]
expected_sources = {"/adapter/Videos/22966160/" + path for path in expected_paths}
assert marker.get("schema_version") == "recording-connection-stub-bootstrap-rollback-marker.v1"
assert marker.get("receipt_id") == expected_id == receipt.get("receipt_id")
assert marker.get("receipt_path") == receipt_path
assert marker.get("receipt_sha256") == hashlib.sha256(open(receipt_path, "rb").read()).hexdigest()
assert marker.get("state_sha256") == hashlib.sha256(state_raw).hexdigest() == receipt.get("adapter_state_sha256")
assert marker.get("status_sha256") == hashlib.sha256(status_raw).hexdigest()
assert marker.get("status_projection_sha256") == hashlib.sha256(
    json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
).hexdigest() == receipt.get("adapter_status_preimage_sha256")
assert isinstance(marker_integrity, dict) and marker_integrity.get("algorithm") == "sha256"
assert marker_integrity.get("canonical_json_sha256") == hashlib.sha256(
    json.dumps(marker_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
assert isinstance(receipt_integrity, dict) and receipt_integrity.get("algorithm") == "sha256"
assert receipt_integrity.get("canonical_json_sha256") == hashlib.sha256(
    json.dumps(receipt_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
assert paths == expected_paths
assert status.get("error") == "2 closed recording(s) failed finalization"
errors = status.get("finalize_errors")
assert isinstance(errors, list) and len(errors) == len(expected_paths)
assert {entry.get("source") for entry in errors if isinstance(entry, dict)} == expected_sources
PY_BOOTSTRAP_ROLLBACK_MARKER
    for pair in \
        "$state_preimage:/opt/bilive/recording/adapter-state.json" \
        "$status_preimage:/opt/bilive/recording/status.json"; do
        source=${pair%%:*}
        destination=${pair#*:}
        tmp=$destination.rollback.$$
        cp -p "$source" "$tmp"
        cmp -s "$source" "$tmp"
        mv -f "$tmp" "$destination"
        cmp -s "$source" "$destination"
    done
    connection_stub_bootstrap_restored=1
}
if [ "$external_mutation_started" -eq 1 ]; then
    restore_connection_stub_bootstrap_preimage
fi
restore_adapter_atomic() {
    destination=/opt/bilive/recording/bililive_recorder_adapter.py
    tmp=$destination.rollback.$$
    if [ -f "$backup/external/recorder_adapter.present" ]; then
        mode=$(stat -c '%a' "$backup/external/recorder_adapter.file")
        install -m "$mode" "$backup/external/recorder_adapter.file" "$tmp"
        cmp -s "$backup/external/recorder_adapter.file" "$tmp"
        mv -f "$tmp" "$destination"
        cmp -s "$backup/external/recorder_adapter.file" "$destination"
    elif [ -f "$backup/external/recorder_adapter.absent" ]; then
        rm -f "$destination"
        test ! -e "$destination"
    else
        echo "missing recorder adapter rollback marker" >&2
        return 1
    fi
}
adapter_status_clean_idle() {
    python3 - /opt/bilive/recording/status.json <<'PY_ROLLBACK_CLEAN_ADAPTER_IDLE'
import json
import sys
import time

payload = json.load(open(sys.argv[1], encoding="utf-8"))
age = time.time() - float(payload["generated_at_epoch"])
assert 0 <= age <= 90
assert payload.get("service_reachable") is True
assert payload.get("streaming") is False
assert payload.get("recording") is False
assert payload.get("finalizing") is False
assert payload.get("error") is None
PY_ROLLBACK_CLEAN_ADAPTER_IDLE
}
adapter_status_supported_repair_idle() {
    python3 - /opt/bilive/recording/status.json <<'PY_ROLLBACK_SUPPORTED_ADAPTER_REPAIR_IDLE'
import json
import sys
import time

payload = json.load(open(sys.argv[1], encoding="utf-8"))
age = time.time() - float(payload["generated_at_epoch"])
error = payload.get("error")
assert 0 <= age <= 90
assert payload.get("service_reachable") is False
assert payload.get("streaming") is False
assert payload.get("recording") is False
assert payload.get("finalizing") is False
assert isinstance(error, str) and (
    error.startswith("source disposition drift:")
    or error
    in {
        "source disposition identity rebind hash retry is pending",
        "source disposition identity rebind hash retry exhausted",
    }
)
PY_ROLLBACK_SUPPORTED_ADAPTER_REPAIR_IDLE
}
adapter_identity_rebind_hash_child_absent() {
    container_processes=$(docker top bililive_adapter -eo pid,args) || return 1
    test -n "$container_processes" || return 1
    ! printf '%s\n' "$container_processes" \
        | grep -F -- '--identity-rebind-hash-child' >/dev/null
}
adapter_restart_environment_safe() {
    expected_adapter_sha=$1
    test "$(findmnt -T /root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming -n -o TARGET)" = "/root/clouddrive2/CloudNAS/CloudDrive" || return 1
    case "$(findmnt -T /root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming -n -o FSTYPE)" in fuse*) ;; *) return 1 ;; esac
    test "$(findmnt -T /root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming -n -o SOURCE)" = "CloudFS" || return 1
    timeout 15 find /root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming -mindepth 1 -maxdepth 1 -print -quit >/dev/null || return 1
    test "$(docker inspect -f '{{.State.Status}}' bililive_recorder)" = running || return 1
    test "$(docker inspect -f '{{.State.Status}}' bililive_adapter)" = running || return 1
    test "$(docker inspect -f '{{index .Config.Cmd 0}}|{{index .Config.Cmd 1}}' bililive_adapter)" = 'python3|/state/bililive_recorder_adapter.py' || return 1
    test "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/state"}}{{.Source}}|{{.Type}}|{{.RW}}{{end}}{{end}}' bililive_adapter)" = '/opt/bilive/recording|bind|true' || return 1
    case "$(docker exec bililive_recorder stat -f -c %T /rec/Videos)" in fuse*) ;; *) return 1 ;; esac
    case "$(docker exec bililive_adapter stat -f -c %T /adapter/Videos)" in fuse*) ;; *) return 1 ;; esac
    docker exec bililive_adapter timeout 15 find /adapter/Videos -mindepth 1 -maxdepth 1 -print -quit >/dev/null || return 1
    test "$(sha256sum /opt/bilive/recording/bililive_recorder_adapter.py | awk '{print $1}')" = "$expected_adapter_sha" || return 1
    test "$(docker exec bililive_adapter sha256sum /state/bililive_recorder_adapter.py | awk '{print $1}')" = "$expected_adapter_sha" || return 1
    docker exec -i bililive_adapter python3 - <<'PY_ROLLBACK_LIVE_IDLE' || return 1
from pathlib import Path
import sys

sys.path.insert(0, "/state")
import bililive_recorder_adapter as adapter

environment = adapter.load_env_file(Path("/run/secrets/brec_http_env"))
room = adapter.query_room_status(
    "http://bililive-recorder:2356/graphql",
    22966160,
    username=environment.get("BREC_HTTP_BASIC_USER", ""),
    password=environment.get("BREC_HTTP_BASIC_PASS", ""),
    timeout_seconds=5,
)
assert room.get("streaming") is False
assert room.get("recording") is False
PY_ROLLBACK_LIVE_IDLE
}
adapter_restart_safe() {
    adapter_status_clean_idle || return 1
    adapter_restart_environment_safe "$1"
}
adapter_repair_restart_safe() {
    adapter_status_supported_repair_idle || return 1
    adapter_restart_environment_safe "$1"
}
wait_adapter_runtime() {
    restarted_after=$1
    expected_sha=$2
    require_clean=$3
    bootstrap_receipt=${4:-}
    bootstrap_marker=${5:-}
    for _attempt in $(seq 1 120); do
        if [ "$(docker inspect -f '{{.State.Status}}' bililive_adapter 2>/dev/null || true)" = running ] && \
           [ "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' bililive_adapter 2>/dev/null || true)" = healthy ] && \
           [ "$(docker inspect -f '{{index .Config.Cmd 0}}|{{index .Config.Cmd 1}}' bililive_adapter 2>/dev/null || true)" = 'python3|/state/bililive_recorder_adapter.py' ] && \
           [ "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/state"}}{{.Source}}|{{.Type}}|{{.RW}}{{end}}{{end}}' bililive_adapter 2>/dev/null || true)" = '/opt/bilive/recording|bind|true' ] && \
           [ "$(sha256sum /opt/bilive/recording/bililive_recorder_adapter.py 2>/dev/null | awk '{print $1}')" = "$expected_sha" ] && \
           [ "$(docker exec bililive_adapter sha256sum /state/bililive_recorder_adapter.py 2>/dev/null | awk '{print $1}')" = "$expected_sha" ] && \
           python3 - /opt/bilive/recording/status.json "$restarted_after" "$require_clean" "$bootstrap_receipt" "$bootstrap_marker" "$backup/external/connection_stub_bootstrap_preimage/adapter-state.json" "$backup/external/connection_stub_bootstrap_preimage/status.json" <<'PY_ROLLBACK_FRESH'
import json
import sys
import time

payload = json.load(open(sys.argv[1], encoding="utf-8"))
generated = float(payload["generated_at_epoch"])
error = payload.get("error")
require_clean = sys.argv[3] == "1"
receipt_path = marker_path = state_preimage = status_preimage = None
if len(sys.argv) == 8:
    receipt_path, marker_path, state_preimage, status_preimage = map(__import__("pathlib").Path, sys.argv[4:])
assert generated >= float(sys.argv[2])
assert 0 <= time.time() - generated <= 90
assert payload.get("streaming") is False
assert payload.get("recording") is False
assert payload.get("finalizing") is False
if require_clean:
    assert payload.get("service_reachable") is True
    assert error is None
else:
    clean = payload.get("service_reachable") is True and error is None
    supported_preimage = (
        payload.get("service_reachable") is False
        and isinstance(error, str)
        and (
            error.startswith("source disposition drift:")
            or error
            in {
                "source disposition identity rebind hash retry is pending",
                "source disposition identity rebind hash retry exhausted",
            }
        )
    )
    bootstrap_preimage = False
    if (
        receipt_path is not None
        and marker_path.is_file()
        and receipt_path.is_file()
        and state_preimage.is_file()
        and status_preimage.is_file()
        and not marker_path.is_symlink()
        and not receipt_path.is_symlink()
        and not state_preimage.is_symlink()
        and not status_preimage.is_symlink()
    ):
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        original = json.loads(status_preimage.read_text(encoding="utf-8"))
        paths = receipt.get("source_relative_paths")
        expected = {"/adapter/Videos/22966160/" + path for path in paths or []}
        original_projection = {
            key: original.get(key)
            for key in (
                "service_reachable",
                "streaming",
                "recording",
                "finalizing",
                "error",
                "finalize_errors",
            )
        }
        receipt_payload = dict(receipt)
        integrity = receipt_payload.pop("canonical_integrity", None)
        marker_payload = dict(marker)
        marker_integrity = marker_payload.pop("canonical_integrity", None)
        bootstrap_preimage = (
            receipt.get("schema_version") == "recording-connection-stub-bootstrap.v1"
            and receipt.get("receipt_id") == receipt_path.stem
            and marker.get("schema_version") == "recording-connection-stub-bootstrap-rollback-marker.v1"
            and marker.get("receipt_id") == receipt_path.stem
            and marker.get("receipt_path") == str(receipt_path)
            and marker.get("receipt_sha256") == __import__("hashlib").sha256(receipt_path.read_bytes()).hexdigest()
            and isinstance(integrity, dict)
            and integrity.get("algorithm") == "sha256"
            and integrity.get("canonical_json_sha256")
            == __import__("hashlib").sha256(
                json.dumps(receipt_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            and isinstance(marker_integrity, dict)
            and marker_integrity.get("algorithm") == "sha256"
            and marker_integrity.get("canonical_json_sha256")
            == __import__("hashlib").sha256(
                json.dumps(marker_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            and receipt.get("adapter_state_sha256") == __import__("hashlib").sha256(state_preimage.read_bytes()).hexdigest()
            and marker.get("state_sha256") == __import__("hashlib").sha256(state_preimage.read_bytes()).hexdigest()
            and marker.get("status_sha256") == __import__("hashlib").sha256(status_preimage.read_bytes()).hexdigest()
            and receipt.get("adapter_status_preimage_sha256")
            == __import__("hashlib").sha256(
                json.dumps(original_projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            and marker.get("status_projection_sha256")
            == __import__("hashlib").sha256(
                json.dumps(original_projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            and isinstance(paths, list) and len(paths) == 2
            and payload.get("service_reachable") is True
            and error == original.get("error") == f"{len(paths)} closed recording(s) failed finalization"
            and payload.get("finalize_errors") == original.get("finalize_errors")
            and {entry.get("source") for entry in payload.get("finalize_errors", []) if isinstance(entry, dict)} == expected
        )
    assert clean or supported_preimage or bootstrap_preimage
PY_ROLLBACK_FRESH
        then
            docker exec bililive_adapter timeout 15 find /adapter/Videos -mindepth 1 -maxdepth 1 -print -quit >/dev/null
            return 0
        fi
        sleep 5
    done
    return 1
}
if [ "$external_mutation_started" -eq 1 ]; then
    restore_adapter_atomic
fi
connection_stub_bootstrap_pre_marker_safe() {
    marker=$backup/external/connection_stub_bootstrap.marker
    state_preimage=$backup/external/connection_stub_bootstrap_preimage/adapter-state.json
    status_preimage=$backup/external/connection_stub_bootstrap_preimage/status.json
    receipt=/opt/bilive/recording/connection-stub-bootstrap-receipts/$new_commit.json
    test ! -e "$marker" || return 1
    test -f "$backup/external/recorder_adapter.restart-required" || return 1
    for path in "$state_preimage" "$status_preimage" "$receipt" \
        /opt/bilive/recording/adapter-state.json /opt/bilive/recording/status.json; do
        test -f "$path" && test ! -L "$path" || return 1
    done
    python3 - "$state_preimage" "$status_preimage" \
        /opt/bilive/recording/adapter-state.json /opt/bilive/recording/status.json \
        "$receipt" "$new_commit" <<'PY_ROLLBACK_PREMARKER'
import hashlib
import json
import re
import sys

state_path, status_path, live_state_path, live_status_path, receipt_path, receipt_id = sys.argv[1:]
state_raw = open(state_path, "rb").read()
state = json.loads(state_raw)
status = json.load(open(status_path, encoding="utf-8"))
live_state = json.load(open(live_state_path, encoding="utf-8"))
live_status = json.load(open(live_status_path, encoding="utf-8"))
receipt = json.load(open(receipt_path, encoding="utf-8"))

def state_material(value):
    assert isinstance(value, dict)
    material = {key: item for key, item in value.items() if key != "last_room_status_epoch"}
    cookie_health = material.get("cookie_health")
    assert isinstance(cookie_health, dict)
    material["cookie_health"] = {
        key: item for key, item in cookie_health.items()
        if key not in {"checked_at", "checked_at_epoch"}
    }
    return material

def status_projection(value):
    assert isinstance(value, dict)
    projected = json.loads(json.dumps(value))
    projected.pop("generated_at", None)
    projected.pop("generated_at_epoch", None)
    cookie_status = projected.get("bilibili_cookie")
    assert isinstance(cookie_status, dict)
    projected["bilibili_cookie"] = {
        key: item for key, item in cookie_status.items()
        if key not in {"checked_at", "checked_at_epoch"}
    }
    errors = projected.get("finalize_errors")
    if isinstance(errors, list):
        for entry in errors:
            if isinstance(entry, dict) and isinstance(entry.get("error"), str):
                entry["error"] = re.sub(r"0x[0-9a-fA-F]+", "0x<address>", entry["error"])
    return projected

def legacy_status_projection(value):
    return {
        key: value.get(key)
        for key in (
            "service_reachable", "streaming", "recording", "finalizing", "error", "finalize_errors"
        )
    }

receipt_payload = dict(receipt)
integrity = receipt_payload.pop("canonical_integrity", None)
paths = receipt.get("source_relative_paths")
expected_paths = [
    "2026-08-20/22966160_20260820-21-00-20.flv",
    "2026-08-20/22966160_20260820-21-56-14.flv",
]
expected_sources = {"/adapter/Videos/22966160/" + path for path in expected_paths}
assert receipt.get("schema_version") == "recording-connection-stub-bootstrap.v1"
assert receipt.get("receipt_id") == receipt_id
assert isinstance(integrity, dict) and integrity.get("algorithm") == "sha256"
assert integrity.get("canonical_json_sha256") == hashlib.sha256(
    json.dumps(receipt_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
assert receipt.get("adapter_state_sha256") == hashlib.sha256(state_raw).hexdigest()
if "adapter_state_material_sha256" in receipt:
    assert receipt.get("adapter_status_preimage_sha256") == hashlib.sha256(
        json.dumps(status_projection(status), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
else:
    assert receipt.get("adapter_status_preimage_sha256") == hashlib.sha256(
        json.dumps(legacy_status_projection(status), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
assert state_material(live_state) == state_material(state)
assert status_projection(live_status) == status_projection(status)
assert paths == expected_paths
assert status.get("error") == "2 closed recording(s) failed finalization"
errors = status.get("finalize_errors")
assert isinstance(errors, list) and len(errors) == len(expected_paths)
assert {entry.get("source") for entry in errors if isinstance(entry, dict)} == expected_sources
PY_ROLLBACK_PREMARKER
}
if [ "$external_mutation_started" -eq 1 ] && [ -f "$backup/external/crontab.present" ]; then
    crontab "$backup/external/crontab.file"
    crontab -l | cmp -s - "$backup/external/crontab.file"
elif [ "$external_mutation_started" -eq 1 ] && [ -f "$backup/external/crontab.absent" ]; then
    crontab -r 2>/dev/null || true
    ! crontab -l >/dev/null 2>&1
elif [ "$external_mutation_started" -eq 1 ]; then
    echo "missing crontab rollback marker" >&2
    exit 1
fi
if [ "$external_mutation_started" -eq 1 ] && [ -f "$backup/external/recorder_adapter.restart-required" ]; then
    test -f "$backup/external/recorder_adapter.file"
    cmp -s "$backup/external/recorder_adapter.file" /opt/bilive/recording/bililive_recorder_adapter.py
    old_adapter_sha=$(sha256sum "$backup/external/recorder_adapter.file" | awk '{print $1}')
    rollback_restart_required=1
    if connection_stub_bootstrap_pre_marker_safe; then
        # Marker creation is immediately before `docker restart`; its absence
        # plus the bound receipt proves the old process never loaded candidate
        # bytes.  Preserve its heartbeat instead of restarting it.
        adapter_restart_environment_safe "$old_adapter_sha"
        rollback_restart_required=0
    elif adapter_restart_safe "$old_adapter_sha"; then
        :
    elif adapter_repair_restart_safe "$old_adapter_sha"; then
        :
    elif [ "$connection_stub_bootstrap_restored" -eq 1 ]; then
        adapter_restart_environment_safe "$old_adapter_sha"
    else
        exit 1
    fi
    adapter_identity_rebind_hash_child_absent
    if [ "$rollback_restart_required" -eq 1 ]; then
        restart_epoch=$(python3 -c 'import time; print(time.time())')
        docker restart bililive_adapter >/dev/null
        wait_adapter_runtime "$restart_epoch" "$old_adapter_sha" 0 "/opt/bilive/recording/connection-stub-bootstrap-receipts/$new_commit.json" "$backup/external/connection_stub_bootstrap.marker"
    fi
    cmp -s "$backup/external/recorder_adapter.file" /opt/bilive/recording/bililive_recorder_adapter.py
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
for component in (
    "scripts",
    "src",
    "ops",
    "assets",
    "profiles",
    ".agent",
    "docs",
    "cleanup_manifests",
    "AGENTS.md",
    "README.md",
):
    base = root / component
    if not base.exists():
        continue  # a tree added in this commit is absent from the prior deployment
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
    [ -z "$LOCAL_ARCHIVE_DIR" ] || rm -rf "$LOCAL_ARCHIVE_DIR"
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
if [ "$HAD_DISABLED" -eq 1 ]; then
    # 既有 DISABLED 会被本次部署尊重并保留（收尾不清除）。它可能是操作员
    # 有意的杀开关，也可能是上一次被超时/信号打断的部署留下的尸留（2026-07-26
    # 案：外层 5min 超时 SIGKILL 级中断 → trap 未跑完 → 主 lane 静默停摆）。
    echo "WARNING: $DISABLED already exists on $HOST and will be preserved." >&2
    echo "WARNING: if no operator set it intentionally, it is likely residue of an interrupted deploy — verify and remove it manually." >&2
fi
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

# Freeze the exact committed bytes locally. Every later comparison and remote
# archive uses COMMIT, never a mutable worktree or a HEAD that could advance.
LOCAL_ARCHIVE_DIR=$(mktemp -d)
git archive --format=tar "$COMMIT" \
    scripts src ops assets profiles .agent docs cleanup_manifests AGENTS.md README.md \
    | (umask 022; tar -xf - -C "$LOCAL_ARCHIVE_DIR")
LOCAL_MANIFEST=$(python3 - "$LOCAL_ARCHIVE_DIR" <<'LOCAL_MANIFEST_PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = {}
for component in (
    "scripts",
    "src",
    "ops",
    "assets",
    "profiles",
    ".agent",
    "docs",
    "cleanup_manifests",
    "AGENTS.md",
    "README.md",
):
    base = root / component
    if not base.exists():
        continue  # a tree added in this commit is absent from the prior deployment
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
print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
LOCAL_MANIFEST_PY
)

# Mark ownership before the remote command can create DISABLED.  If this
# process is interrupted while the remote flock is still waiting, cleanup must
# know that the empty stop file belongs to this deployment and remove it.
DISABLED_TOUCHED=1
ssh "$HOST" "touch '$DISABLED'; /usr/bin/flock -w 7200 '$REMOTE_BASE/runner.lock' true"
ssh "$HOST" "test ! -e '$STAGE' && test ! -e '$BACKUP' && mkdir '$STAGE'"
STAGE_CREATED=1

# `git archive` is the deployment source of truth: only COMMIT-tracked bytes can
# enter staging. assets/ is intentionally replaced as a repo-owned tree; private
# enrollment WAVs and the CAM++ model live outside repo/.
git archive --format=tar "$COMMIT" \
    scripts src ops assets profiles .agent docs cleanup_manifests AGENTS.md README.md \
    | ssh "$HOST" "umask 022; tar --no-same-permissions -xf - -C '$STAGE'"

REMOTE_MANIFEST=$(ssh "$HOST" python3 - "$STAGE" <<'REMOTE_MANIFEST_PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = {}
for component in (
    "scripts",
    "src",
    "ops",
    "assets",
    "profiles",
    ".agent",
    "docs",
    "cleanup_manifests",
    "AGENTS.md",
    "README.md",
):
    base = root / component
    if not base.exists():
        continue  # a tree added in this commit is absent from the prior deployment
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
print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
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
  "import modelscope, soundfile; import src.autoslice.speaker_finalizer; import src.autoslice.speaker_session_router"
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -c \
  "import scripts.free_session_autoslice; import scripts.produce_slice_package; import src.autoslice.chat_authority"
PYTHONDONTWRITEBYTECODE=1 /opt/bilive/autoslice/venv-diar/bin/python -c \
  'from src.autoslice.speaker_finalizer import _speaker_context_env; e=_speaker_context_env(); assert e.get("CPA_BASE_URL") and e.get("CPA_API_KEY"); print("speaker context env verified")'
PYTHONDONTWRITEBYTECODE=1 /opt/bilive/autoslice/venv-diar/bin/python - <<'PY'
import json
from pathlib import Path

from scripts.apply_speaker_turn_overrides import (
    sha256_file,
    validate_bound_speaker_override_document,
)
from scripts.apply_subtitle_text_overrides import validate_bound_override_document
from scripts.batch_speaker_review import resolve_staged_repo_asset, validate_plan
from src.autoslice.chat_authority import load_referent_groups
from src.autoslice.host_vocal_proof import _sha256_directory, _validate_profile
from src.autoslice.speaker_finalizer import (
    _policy,
    _validate_source_session_anchor_document,
    _validate_source_session_provenance,
)
from src.autoslice.topic_entity_graph import validate_topic_entity_graph

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
profile_sha256 = hashlib.sha256(
    Path("assets/lidousha/voiceprint_profile.v1.json").read_bytes()
).hexdigest()
reference_hashes = {str(item["id"]): str(item["sha256"]) for item in references}
for anchor_path in sorted(Path("assets/lidousha/speaker_session_anchors").glob("*.json")):
    document = json.loads(anchor_path.read_text())
    allowed = document.get("allowed_targets") or []
    for target in allowed:
        target_path = Path(target["media_path"])
        validated = _validate_source_session_anchor_document(
            document,
            target_media_sha256=str(target["media_sha256"]),
            target_media_path=target_path,
            profile_sha256=profile_sha256,
            model_tree_sha256=actual_model,
            reference_hashes=reference_hashes,
            host_seed_min=float(_policy(profile)["host_session_seed_min"]),
        )
        _validate_source_session_provenance(
            validated,
            target_media_path=target_path,
            target_media_sha256=str(target["media_sha256"]),
        )
    donor = document.get("donor")
    if donor is not None:
        assert sha256_file(Path(donor["media_path"])) == donor["media_sha256"]
        assert sha256_file(Path(donor["text_srt_path"])) == donor["text_srt_sha256"]
referents = json.loads(Path("assets/lidousha/entity_confusables.json").read_text())
assert referents.get("schema_version") == "lidousha-referent-groups.v2"
assert isinstance(referents.get("groups"), list)
assert load_referent_groups(Path("assets/lidousha/entity_confusables.json"))
timely = json.loads(Path("assets/lidousha/timely_terms.json").read_text())
assert timely.get("schema_version") == "lidousha-timely-terms.v1"
assert timely.get("status") in {"fresh", "stale"}
assert isinstance(timely.get("terms"), list)
topic_graph = validate_topic_entity_graph(
    json.loads(Path("assets/lidousha/topic_entity_graph.json").read_text())
)
assert topic_graph["topics"] and topic_graph["works"] and topic_graph["entities"]
batch_plan_path = Path("assets/lidousha/speaker_batch_plans/2026-07-09.json")
batch_plan = validate_plan(json.loads(batch_plan_path.read_text()))
staged_root = Path.cwd()
for entry in batch_plan["entries"]:
    for path_field, hash_field in (
        ("subtitle_text_override_path", "subtitle_text_override_sha256"),
        ("speaker_override_path", "speaker_override_sha256"),
        ("source_session_anchor_path", "source_session_anchor_sha256"),
    ):
        value = entry.get(path_field)
        if not value:
            continue
        staged = resolve_staged_repo_asset(value, staged_root=staged_root)
        assert sha256_file(staged) == entry[hash_field], (
            entry["candidate_id"], path_field, staged, entry[hash_field]
        )
        if path_field == "subtitle_text_override_path":
            validate_bound_override_document(
                Path(entry["text_srt_path"]),
                staged,
                candidate_id=entry["candidate_id"],
                expected_source_srt_sha256=entry["text_source_srt_sha256"],
                expected_final_srt_sha256=entry["text_final_srt_sha256"],
            )
        elif path_field == "speaker_override_path":
            validate_bound_speaker_override_document(
                staged,
                candidate_id=entry["candidate_id"],
                expected_source_media_sha256=entry["source_media_sha256"],
                expected_text_final_srt_sha256=entry["text_final_srt_sha256"],
            )
from src.autoslice.branding_intro import load_branding_intro_policy, resolve_intro_media

intro_policy = load_branding_intro_policy(Path("assets/lidousha/intro/branding_intro.v1.json"))
if intro_policy is None:
    print("branding intro disabled/absent")
else:
    intro_media = resolve_intro_media(intro_policy, Path.cwd())
    print("branding intro verified", intro_media)
print("speaker runtime assets verified", actual_model, len(references))
PY
REMOTE_VALIDATE

# Swap only versioned deploy trees. Other runtime-owned content under repo/
# (notably historical delivery media) stays in place. The remote trap rolls
# back a partial component swap before releasing runner.lock.
SWITCHED=1
ssh "$HOST" /usr/bin/flock -w 7200 "$REMOTE_BASE/runner.lock" bash -s -- \
    "$REMOTE_REPO" "$STAGE" "$BACKUP" "$COMMIT" <<'REMOTE_SWITCH'
set -euo pipefail
repo=$1
stage=$2
backup=$3
commit=$4
umask 077
mkdir -p "$backup"
cp "$repo/DEPLOYED_COMMIT" "$backup/DEPLOYED_COMMIT.old"
mkdir -p "$backup/repository"
capture_repository_file() {
    label=$1
    source=$2
    if [ -e "$source" ]; then
        test -f "$source"
        test ! -L "$source"
        cp -p "$source" "$backup/repository/$label.file"
        cmp -s "$source" "$backup/repository/$label.file"
        touch "$backup/repository/$label.present"
    else
        touch "$backup/repository/$label.absent"
    fi
}
capture_repository_file \
    deployed_authority_manifest \
    "$repo/DEPLOYED_AUTHORITY_MANIFEST.json"
python3 - "$repo" "$backup/repo.manifest.old.json" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = {}
for component in (
    "scripts",
    "src",
    "ops",
    "assets",
    "profiles",
    ".agent",
    "docs",
    "cleanup_manifests",
    "AGENTS.md",
    "README.md",
):
    base = root / component
    if not base.exists():
        continue  # a tree added in this commit is absent from the prior deployment
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
        test -f "$source"
        test ! -L "$source"
        cp -p "$source" "$backup/external/$label.file"
        cmp -s "$source" "$backup/external/$label.file"
        touch "$backup/external/$label.present"
    else
        touch "$backup/external/$label.absent"
    fi
}
capture_file watchdog /opt/bilive/autoslice/free_mount_watchdog.sh
capture_file upload_sentinel /opt/bilive/autoslice/upload_fatal_sentinel.sh
capture_file uploader /opt/bilive/app/tmp_manual_upload/do_upload.sh
capture_file recorder_adapter /opt/bilive/recording/bililive_recorder_adapter.py
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
restore_connection_stub_bootstrap_preimage() {
    marker=$backup/external/connection_stub_bootstrap.marker
    state_preimage=$backup/external/connection_stub_bootstrap_preimage/adapter-state.json
    status_preimage=$backup/external/connection_stub_bootstrap_preimage/status.json
    receipt=/opt/bilive/recording/connection-stub-bootstrap-receipts/$commit.json
    if [ ! -e "$marker" ]; then
        return 0
    fi
    for path in "$marker" "$state_preimage" "$status_preimage" "$receipt"; do
        test -f "$path"
        test ! -L "$path"
    done
    python3 - "$marker" "$state_preimage" "$status_preimage" "$receipt" "$commit" <<'PY_BOOTSTRAP_ROLLBACK_MARKER'
import hashlib
import json
import sys

marker_path, state_path, status_path, receipt_path, expected_id = sys.argv[1:]
marker = json.load(open(marker_path, encoding="utf-8"))
receipt = json.load(open(receipt_path, encoding="utf-8"))
state_raw = open(state_path, "rb").read()
status_raw = open(status_path, "rb").read()
status = json.loads(status_raw)
def status_projection(value):
    projected = json.loads(json.dumps(value))
    projected.pop("generated_at", None)
    projected.pop("generated_at_epoch", None)
    cookie_status = projected.get("bilibili_cookie")
    assert isinstance(cookie_status, dict)
    projected["bilibili_cookie"] = {
        key: item for key, item in cookie_status.items()
        if key not in {"checked_at", "checked_at_epoch"}
    }
    errors = projected.get("finalize_errors")
    if isinstance(errors, list):
        import re
        for entry in errors:
            if isinstance(entry, dict) and isinstance(entry.get("error"), str):
                entry["error"] = re.sub(r"0x[0-9a-fA-F]+", "0x<address>", entry["error"])
    return projected
projection = status_projection(status)
marker_payload = dict(marker)
marker_integrity = marker_payload.pop("canonical_integrity", None)
receipt_payload = dict(receipt)
receipt_integrity = receipt_payload.pop("canonical_integrity", None)
paths = receipt.get("source_relative_paths")
expected_paths = [
    "2026-08-20/22966160_20260820-21-00-20.flv",
    "2026-08-20/22966160_20260820-21-56-14.flv",
]
expected_sources = {"/adapter/Videos/22966160/" + path for path in expected_paths}
assert marker.get("schema_version") == "recording-connection-stub-bootstrap-rollback-marker.v1"
assert marker.get("receipt_id") == expected_id == receipt.get("receipt_id")
assert marker.get("receipt_path") == receipt_path
assert marker.get("receipt_sha256") == hashlib.sha256(open(receipt_path, "rb").read()).hexdigest()
assert marker.get("state_sha256") == hashlib.sha256(state_raw).hexdigest() == receipt.get("adapter_state_sha256")
assert marker.get("status_sha256") == hashlib.sha256(status_raw).hexdigest()
assert marker.get("status_projection_sha256") == hashlib.sha256(
    json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
).hexdigest() == receipt.get("adapter_status_preimage_sha256")
assert isinstance(marker_integrity, dict) and marker_integrity.get("algorithm") == "sha256"
assert marker_integrity.get("canonical_json_sha256") == hashlib.sha256(
    json.dumps(marker_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
assert isinstance(receipt_integrity, dict) and receipt_integrity.get("algorithm") == "sha256"
assert receipt_integrity.get("canonical_json_sha256") == hashlib.sha256(
    json.dumps(receipt_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
assert paths == expected_paths
assert status.get("error") == "2 closed recording(s) failed finalization"
errors = status.get("finalize_errors")
assert isinstance(errors, list) and len(errors) == len(expected_paths)
assert {entry.get("source") for entry in errors if isinstance(entry, dict)} == expected_sources
PY_BOOTSTRAP_ROLLBACK_MARKER
    for pair in \
        "$state_preimage:/opt/bilive/recording/adapter-state.json" \
        "$status_preimage:/opt/bilive/recording/status.json"; do
        source=${pair%%:*}
        destination=${pair#*:}
        tmp=$destination.rollback.$$
        cp -p "$source" "$tmp"
        cmp -s "$source" "$tmp"
        mv -f "$tmp" "$destination"
        cmp -s "$source" "$destination"
    done
}
restore_repository_file() {
    label=$1
    destination=$2
    if [ -f "$backup/repository/$label.present" ]; then
        tmp=$destination.rollback.$$
        cp -p "$backup/repository/$label.file" "$tmp"
        cmp -s "$backup/repository/$label.file" "$tmp"
        mv -f "$tmp" "$destination"
        cmp -s "$backup/repository/$label.file" "$destination"
    elif [ -f "$backup/repository/$label.absent" ]; then
        rm -f "$destination"
        test ! -e "$destination"
    else
        return 1
    fi
}
rollback() {
    for component in scripts src ops assets profiles .agent docs cleanup_manifests AGENTS.md README.md; do
        if [ -e "$backup/$component" ]; then
            rm -rf "$stage/$component"
            if [ -e "$repo/$component" ]; then
                mkdir -p "$stage"
                mv "$repo/$component" "$stage/$component"
            fi
            mv "$backup/$component" "$repo/$component"
        elif [ -e "$backup/$component.absent" ]; then
            rm -rf "$repo/$component"  # tree added by the deploy: rollback removes it
        fi
    done
    restore_repository_file \
        deployed_authority_manifest \
        "$repo/DEPLOYED_AUTHORITY_MANIFEST.json"
cp "$backup/DEPLOYED_COMMIT.old" "$repo/DEPLOYED_COMMIT"
cmp -s "$backup/DEPLOYED_COMMIT.old" "$repo/DEPLOYED_COMMIT"
    # This transaction only switches the repository tree.  External targets,
    # cron, and recorder state are first mutable in REMOTE_EXTERNAL_INSTALL;
    # they must therefore remain untouched when this earlier switch fails.
}
trap 'rc=$?; trap - ERR; rollback; exit "$rc"' ERR
trap 'trap - ERR HUP INT TERM; rollback; exit 130' INT
trap 'trap - ERR HUP INT TERM; rollback; exit 143' TERM
trap 'trap - ERR HUP INT TERM; rollback; exit 129' HUP
for component in scripts src ops assets profiles .agent docs cleanup_manifests AGENTS.md README.md; do
    if [ -e "$repo/$component" ]; then
        mv "$repo/$component" "$backup/$component"
    else
        touch "$backup/$component.absent"  # added tree: rollback removes it
    fi
    mv "$stage/$component" "$repo/$component"
done
rmdir "$stage"
trap - ERR INT TERM HUP
REMOTE_SWITCH

# Install external entrypoints only from the already-switched committed tree.
# Temp + rename avoids exposing a truncated executable to cron/manual callers.
ssh "$HOST" /usr/bin/flock -w 7200 "$REMOTE_BASE/runner.lock" bash -s -- \
    "$BACKUP" "$COMMIT" <<'REMOTE_EXTERNAL_INSTALL'
set -euo pipefail
backup=$1
commit=$2
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
    test -f "$source"
    test ! -L "$source"
    mkdir -p "$(dirname "$destination")"
    tmp=$destination.deploy.$$
    install -m "$mode" "$source" "$tmp"
    test "$(sha256sum "$source" | awk '{print $1}')" = "$(sha256sum "$tmp" | awk '{print $1}')"
    mv -f "$tmp" "$destination"
    tmp=
    test -f "$destination"
    test ! -L "$destination"
    test "$(stat -c '%a' "$destination")" = "$mode"
    test "$(sha256sum "$source" | awk '{print $1}')" = "$(sha256sum "$destination" | awk '{print $1}')"
}
adapter_status_clean_idle() {
    python3 - /opt/bilive/recording/status.json <<'PY_CLEAN_ADAPTER_IDLE'
import json
import sys
import time

payload = json.load(open(sys.argv[1], encoding="utf-8"))
age = time.time() - float(payload["generated_at_epoch"])
assert 0 <= age <= 90
assert payload.get("service_reachable") is True
assert payload.get("streaming") is False
assert payload.get("recording") is False
assert payload.get("finalizing") is False
assert payload.get("error") is None
PY_CLEAN_ADAPTER_IDLE
}
adapter_status_zero_touch_fresh() {
    test -f /opt/bilive/recording/status.json || return 1
    test ! -L /opt/bilive/recording/status.json || return 1
    python3 - /opt/bilive/recording/status.json <<'PY_ZERO_TOUCH_ADAPTER_STATUS'
import json
import sys
import time

payload = json.load(open(sys.argv[1], encoding="utf-8"))
age = time.time() - float(payload["generated_at_epoch"])
assert 0 <= age <= 90
assert type(payload.get("service_reachable")) is bool
assert "error" in payload and (payload["error"] is None or type(payload["error"]) is str)
for field in ("streaming", "recording", "finalizing"):
    assert type(payload.get(field)) is bool
PY_ZERO_TOUCH_ADAPTER_STATUS
}
adapter_status_supported_repair_idle() {
    python3 - /opt/bilive/recording/status.json <<'PY_SUPPORTED_ADAPTER_REPAIR_IDLE'
import json
import sys
import time

payload = json.load(open(sys.argv[1], encoding="utf-8"))
age = time.time() - float(payload["generated_at_epoch"])
error = payload.get("error")
assert 0 <= age <= 90
assert payload.get("service_reachable") is False
assert payload.get("streaming") is False
assert payload.get("recording") is False
assert payload.get("finalizing") is False
assert isinstance(error, str) and (
    error.startswith("source disposition drift:")
    or error
    in {
        "source disposition identity rebind hash retry is pending",
        "source disposition identity rebind hash retry exhausted",
    }
)
PY_SUPPORTED_ADAPTER_REPAIR_IDLE
}
adapter_identity_rebind_hash_child_absent() {
    container_processes=$(docker top bililive_adapter -eo pid,args) || return 1
    test -n "$container_processes" || return 1
    ! printf '%s\n' "$container_processes" \
        | grep -F -- '--identity-rebind-hash-child' >/dev/null
}
adapter_environment_healthy() {
    expected_adapter_sha=$1
    test "$(findmnt -T /root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming -n -o TARGET)" = "/root/clouddrive2/CloudNAS/CloudDrive" || return 1
    case "$(findmnt -T /root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming -n -o FSTYPE)" in fuse*) ;; *) return 1 ;; esac
    test "$(findmnt -T /root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming -n -o SOURCE)" = "CloudFS" || return 1
    timeout 15 find /root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming -mindepth 1 -maxdepth 1 -print -quit >/dev/null || return 1
    test "$(docker inspect -f '{{.State.Status}}' bililive_recorder)" = running || return 1
    test "$(docker inspect -f '{{.State.Status}}' bililive_adapter)" = running || return 1
    test "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' bililive_adapter)" = healthy || return 1
    test "$(docker inspect -f '{{index .Config.Cmd 0}}|{{index .Config.Cmd 1}}' bililive_adapter)" = 'python3|/state/bililive_recorder_adapter.py' || return 1
    test "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/state"}}{{.Source}}|{{.Type}}|{{.RW}}{{end}}{{end}}' bililive_adapter)" = '/opt/bilive/recording|bind|true' || return 1
    case "$(docker exec bililive_recorder stat -f -c %T /rec/Videos)" in fuse*) ;; *) return 1 ;; esac
    case "$(docker exec bililive_adapter stat -f -c %T /adapter/Videos)" in fuse*) ;; *) return 1 ;; esac
    docker exec bililive_adapter timeout 15 find /adapter/Videos -mindepth 1 -maxdepth 1 -print -quit >/dev/null || return 1
    test "$(sha256sum /opt/bilive/recording/bililive_recorder_adapter.py | awk '{print $1}')" = "$expected_adapter_sha" || return 1
    test "$(docker exec bililive_adapter sha256sum /state/bililive_recorder_adapter.py | awk '{print $1}')" = "$expected_adapter_sha" || return 1
}
adapter_restart_environment_safe() {
    adapter_environment_healthy "$1" || return 1
    docker exec -i bililive_adapter python3 - <<'PY_LIVE_IDLE' || return 1
from pathlib import Path
import sys

sys.path.insert(0, "/state")
import bililive_recorder_adapter as adapter

environment = adapter.load_env_file(Path("/run/secrets/brec_http_env"))
room = adapter.query_room_status(
    "http://bililive-recorder:2356/graphql",
    22966160,
    username=environment.get("BREC_HTTP_BASIC_USER", ""),
    password=environment.get("BREC_HTTP_BASIC_PASS", ""),
    timeout_seconds=5,
)
assert room.get("streaming") is False
assert room.get("recording") is False
PY_LIVE_IDLE
}
adapter_restart_safe() {
    adapter_status_clean_idle || return 1
    adapter_restart_environment_safe "$1"
}
adapter_repair_restart_safe() {
    adapter_status_supported_repair_idle || return 1
    adapter_restart_environment_safe "$1"
}
capture_connection_stub_bootstrap_preimage() {
    stage=/opt/bilive/recording/.connection-stub-bootstrap-preimage-$commit
    preimage=$backup/external/connection_stub_bootstrap_preimage
    marker=$backup/external/connection_stub_bootstrap.marker
    test ! -e "$stage"
    test ! -e "$preimage"
    test ! -e "$marker"
    mkdir -m 700 "$stage" "$preimage"
    for label in adapter-state.json status.json; do
        source=/opt/bilive/recording/$label
        if ! test -f "$source" || ! test ! -L "$source" || \
            ! cp -p "$source" "$stage/$label" || ! cmp -s "$source" "$stage/$label" || \
            ! cp -p "$stage/$label" "$preimage/$label" || ! cmp -s "$stage/$label" "$preimage/$label" || \
            ! chmod 600 "$preimage/$label"; then
            discard_connection_stub_bootstrap_staging
            return 1
        fi
    done
}
discard_connection_stub_bootstrap_staging() {
    stage=/opt/bilive/recording/.connection-stub-bootstrap-preimage-$commit
    if [ ! -e "$stage" ]; then
        return 0
    fi
    test -d "$stage"
    test ! -L "$stage"
    for label in adapter-state.json status.json; do
        if [ -e "$stage/$label" ]; then
            test -f "$stage/$label"
            test ! -L "$stage/$label"
            rm -f "$stage/$label"
        fi
    done
    rmdir "$stage"
}
activate_connection_stub_bootstrap_marker() {
    marker=$backup/external/connection_stub_bootstrap.marker
    preimage=$backup/external/connection_stub_bootstrap_preimage
    receipt=/opt/bilive/recording/connection-stub-bootstrap-receipts/$commit.json
    test ! -e "$marker"
    for path in \
        "$preimage/adapter-state.json" \
        "$preimage/status.json" \
        /opt/bilive/recording/adapter-state.json \
        /opt/bilive/recording/status.json \
        "$receipt"; do
        test -f "$path"
        test ! -L "$path"
    done
    python3 - "$marker" "$preimage/adapter-state.json" "$preimage/status.json" \
        /opt/bilive/recording/adapter-state.json /opt/bilive/recording/status.json \
        "$receipt" "$commit" <<'PY_BOOTSTRAP_ACTIVATE_MARKER'
import hashlib
import json
import os
import sys

marker_path, state_path, status_path, live_state_path, live_status_path, receipt_path, receipt_id = sys.argv[1:]
state_raw = open(state_path, "rb").read()
status_raw = open(status_path, "rb").read()
state = json.loads(state_raw)
status = json.loads(status_raw)
live_state = json.load(open(live_state_path, encoding="utf-8"))
live_status = json.load(open(live_status_path, encoding="utf-8"))
receipt_raw = open(receipt_path, "rb").read()
receipt = json.loads(receipt_raw)

def state_material(payload):
    assert isinstance(payload, dict)
    material = {key: value for key, value in payload.items() if key != "last_room_status_epoch"}
    cookie_health = material.get("cookie_health")
    assert isinstance(cookie_health, dict)
    material["cookie_health"] = {
        key: value for key, value in cookie_health.items()
        if key not in {"checked_at", "checked_at_epoch"}
    }
    return material

def status_projection(payload):
    assert isinstance(payload, dict)
    projected = json.loads(json.dumps(payload))
    projected.pop("generated_at", None)
    projected.pop("generated_at_epoch", None)
    cookie_status = projected.get("bilibili_cookie")
    assert isinstance(cookie_status, dict)
    projected["bilibili_cookie"] = {
        key: value for key, value in cookie_status.items()
        if key not in {"checked_at", "checked_at_epoch"}
    }
    errors = projected.get("finalize_errors")
    if isinstance(errors, list):
        import re
        for entry in errors:
            if isinstance(entry, dict) and isinstance(entry.get("error"), str):
                entry["error"] = re.sub(r"0x[0-9a-fA-F]+", "0x<address>", entry["error"])
    return projected

state_material_sha256 = lambda payload: hashlib.sha256(
    json.dumps(state_material(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
status_projection_sha256 = lambda payload: hashlib.sha256(
    json.dumps(status_projection(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
expected_paths = [
    "2026-08-20/22966160_20260820-21-00-20.flv",
    "2026-08-20/22966160_20260820-21-56-14.flv",
]
expected_sources = {"/adapter/Videos/22966160/" + path for path in expected_paths}
assert receipt.get("schema_version") == "recording-connection-stub-bootstrap.v1"
assert receipt.get("receipt_id") == receipt_id
assert receipt.get("adapter_state_sha256") == hashlib.sha256(state_raw).hexdigest()
assert receipt.get("adapter_state_material_sha256") == state_material_sha256(state)
assert receipt.get("adapter_status_preimage_sha256") == status_projection_sha256(status)
assert state_material_sha256(live_state) == receipt.get("adapter_state_material_sha256")
assert status_projection_sha256(live_status) == receipt.get("adapter_status_preimage_sha256")
receipt_payload = dict(receipt)
receipt_integrity = receipt_payload.pop("canonical_integrity", None)
assert isinstance(receipt_integrity, dict) and receipt_integrity.get("algorithm") == "sha256"
assert receipt_integrity.get("canonical_json_sha256") == hashlib.sha256(
    json.dumps(receipt_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
assert receipt.get("source_relative_paths") == expected_paths
assert status.get("error") == "2 closed recording(s) failed finalization"
errors = status.get("finalize_errors")
assert isinstance(errors, list) and len(errors) == len(expected_paths)
assert {entry.get("source") for entry in errors if isinstance(entry, dict)} == expected_sources
marker = {
    "schema_version": "recording-connection-stub-bootstrap-rollback-marker.v1",
    "receipt_id": receipt_id,
    "receipt_path": receipt_path,
    "receipt_sha256": hashlib.sha256(receipt_raw).hexdigest(),
    "state_sha256": hashlib.sha256(state_raw).hexdigest(),
    "status_sha256": hashlib.sha256(status_raw).hexdigest(),
    "state_material_sha256": state_material_sha256(state),
    "status_projection_sha256": status_projection_sha256(status),
}
marker["canonical_integrity"] = {
    "algorithm": "sha256",
    "canonical_json_sha256": hashlib.sha256(
        json.dumps(marker, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest(),
}
encoded = (json.dumps(marker, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
fd = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, "wb") as handle:
    handle.write(encoded)
    handle.flush()
    os.fsync(handle.fileno())
PY_BOOTSTRAP_ACTIVATE_MARKER
}
adapter_connection_stub_bootstrap_safe() {
    old_sha=$1
    new_sha=$2
    receipt=/opt/bilive/recording/connection-stub-bootstrap-receipts/$commit.json
    adapter_restart_environment_safe "$old_sha" || return 1
    capture_connection_stub_bootstrap_preimage || return 1
    if ! docker exec -i bililive_adapter timeout 360 python3 - \
        --prepare-connection-stub-bootstrap \
        --record-root /adapter/Videos/22966160 \
        --state-path /state/.connection-stub-bootstrap-preimage-$commit/adapter-state.json \
        --bootstrap-receipt-root /state/connection-stub-bootstrap-receipts \
        --bootstrap-receipt-id "$commit" \
        --bootstrap-candidate-adapter-sha256 "$new_sha" \
        --bootstrap-installed-adapter-path /state/bililive_recorder_adapter.py \
        --bootstrap-status-path /state/.connection-stub-bootstrap-preimage-$commit/status.json \
        --bootstrap-source-relative 2026-08-20/22966160_20260820-21-00-20.flv \
        --bootstrap-source-relative 2026-08-20/22966160_20260820-21-56-14.flv \
        < "$new_adapter_source" >/dev/null
    then
        discard_connection_stub_bootstrap_staging
        return 1
    fi
    if ! python3 - "$backup/external/connection_stub_bootstrap_preimage/adapter-state.json" "$backup/external/connection_stub_bootstrap_preimage/status.json" "$receipt" "$new_sha" "$old_sha" <<'PY_BOOTSTRAP_PREIMAGE'
import hashlib
import json
import re
import sys
state_raw = open(sys.argv[1], "rb").read()
state = json.loads(state_raw)
payload = json.load(open(sys.argv[2], encoding="utf-8"))
receipt = json.load(open(sys.argv[3], encoding="utf-8"))
new_sha, old_sha = sys.argv[4:]
paths = receipt.get("source_relative_paths")
rows = receipt.get("rows")
receipt_payload = dict(receipt)
integrity = receipt_payload.pop("canonical_integrity", None)
assert receipt.get("schema_version") == "recording-connection-stub-bootstrap.v1"
assert receipt.get("candidate_adapter_sha256") == new_sha
assert receipt.get("installed_adapter_sha256") == old_sha
assert receipt.get("adapter_state_sha256") == hashlib.sha256(state_raw).hexdigest()

def state_material(value):
    assert isinstance(value, dict)
    material = {key: item for key, item in value.items() if key != "last_room_status_epoch"}
    cookie_health = material.get("cookie_health")
    assert isinstance(cookie_health, dict)
    material["cookie_health"] = {
        key: item for key, item in cookie_health.items()
        if key not in {"checked_at", "checked_at_epoch"}
    }
    return material

def status_projection(value):
    assert isinstance(value, dict)
    projected = json.loads(json.dumps(value))
    projected.pop("generated_at", None)
    projected.pop("generated_at_epoch", None)
    cookie_status = projected.get("bilibili_cookie")
    assert isinstance(cookie_status, dict)
    projected["bilibili_cookie"] = {
        key: item for key, item in cookie_status.items()
        if key not in {"checked_at", "checked_at_epoch"}
    }
    errors = projected.get("finalize_errors")
    if isinstance(errors, list):
        for entry in errors:
            if isinstance(entry, dict) and isinstance(entry.get("error"), str):
                entry["error"] = re.sub(r"0x[0-9a-fA-F]+", "0x<address>", entry["error"])
    return projected

assert receipt.get("adapter_state_material_sha256") == hashlib.sha256(
    json.dumps(state_material(state), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
assert isinstance(integrity, dict) and integrity.get("algorithm") == "sha256"
assert integrity.get("canonical_json_sha256") == hashlib.sha256(
    json.dumps(receipt_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
assert isinstance(paths, list) and len(paths) == 2 and paths == sorted(paths)
assert isinstance(rows, dict) and sorted(rows) == paths
assert all(row.get("source_relative_path") == path for path, row in rows.items())
assert payload.get("service_reachable") is True
assert payload.get("streaming") is False
assert payload.get("recording") is False
assert payload.get("finalizing") is False
errors = payload.get("finalize_errors")
assert isinstance(errors, list) and len(errors) == len(paths)
expected = {"/adapter/Videos/22966160/" + path for path in paths}
assert {entry.get("source") for entry in errors if isinstance(entry, dict)} == expected
assert payload.get("error") == f"{len(paths)} closed recording(s) failed finalization"
assert receipt.get("adapter_status_preimage_sha256") == hashlib.sha256(
    json.dumps(status_projection(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
PY_BOOTSTRAP_PREIMAGE
    then
        discard_connection_stub_bootstrap_staging
        return 1
    fi
    discard_connection_stub_bootstrap_staging
}
adapter_connection_stub_bootstrap_postcondition() {
    receipt=/opt/bilive/recording/connection-stub-bootstrap-receipts/$commit.json
    python3 - "$receipt" /opt/bilive/recording/adapter-state.json <<'PY_BOOTSTRAP_POSTCONDITION'
import hashlib
import json
import sys

receipt = json.load(open(sys.argv[1], encoding="utf-8"))
state = json.load(open(sys.argv[2], encoding="utf-8"))
receipt_payload = dict(receipt)
integrity = receipt_payload.pop("canonical_integrity", None)
assert receipt.get("schema_version") == "recording-connection-stub-bootstrap.v1"
assert receipt.get("receipt_id") == __import__("pathlib").Path(sys.argv[1]).stem
assert isinstance(integrity, dict) and integrity.get("algorithm") == "sha256"
assert integrity.get("canonical_json_sha256") == hashlib.sha256(
    json.dumps(receipt_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
paths = receipt.get("source_relative_paths")
rows = receipt.get("rows")
dispositions = state.get("source_dispositions")
assert isinstance(paths, list) and len(paths) == 2
assert isinstance(rows, dict) and sorted(rows) == paths
assert isinstance(dispositions, dict)
assert all(dispositions.get(path) == rows[path] for path in paths)
PY_BOOTSTRAP_POSTCONDITION
}
wait_adapter_runtime() {
    restarted_after=$1
    expected_sha=$2
    for _attempt in $(seq 1 120); do
        if [ "$(docker inspect -f '{{.State.Status}}' bililive_adapter 2>/dev/null || true)" = running ] && \
           [ "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' bililive_adapter 2>/dev/null || true)" = healthy ] && \
           [ "$(docker inspect -f '{{index .Config.Cmd 0}}|{{index .Config.Cmd 1}}' bililive_adapter 2>/dev/null || true)" = 'python3|/state/bililive_recorder_adapter.py' ] && \
           [ "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/state"}}{{.Source}}|{{.Type}}|{{.RW}}{{end}}{{end}}' bililive_adapter 2>/dev/null || true)" = '/opt/bilive/recording|bind|true' ] && \
           [ "$(sha256sum /opt/bilive/recording/bililive_recorder_adapter.py 2>/dev/null | awk '{print $1}')" = "$expected_sha" ] && \
           [ "$(docker exec bililive_adapter sha256sum /state/bililive_recorder_adapter.py 2>/dev/null | awk '{print $1}')" = "$expected_sha" ] && \
           python3 - /opt/bilive/recording/status.json "$restarted_after" <<'PY_FRESH'
import json
import sys
import time

payload = json.load(open(sys.argv[1], encoding="utf-8"))
generated = float(payload["generated_at_epoch"])
assert generated >= float(sys.argv[2])
assert 0 <= time.time() - generated <= 90
assert payload.get("service_reachable") is True
assert payload.get("streaming") is False
assert payload.get("recording") is False
assert payload.get("finalizing") is False
assert payload.get("error") is None
PY_FRESH
        then
            docker exec bililive_adapter timeout 15 find /adapter/Videos -mindepth 1 -maxdepth 1 -print -quit >/dev/null
            return 0
        fi
        sleep 5
    done
    return 1
}
new_adapter_source=/opt/bilive/autoslice/repo/ops/recording/bililive_recorder_adapter.py
host_adapter_path=/opt/bilive/recording/bililive_recorder_adapter.py
watchdog_cron='*/5 * * * * /usr/bin/flock -n /opt/bilive/autoslice/watchdog.lock /opt/bilive/autoslice/free_mount_watchdog.sh >> /opt/bilive/autoslice/logs/watchdog.log 2>&1'
upload_fatal_cron='*/5 * * * * /usr/bin/flock -n /opt/bilive/autoslice/upload-fatal-sentinel.lock /opt/bilive/autoslice/upload_fatal_sentinel.sh >> /opt/bilive/autoslice/logs/upload-fatal-sentinel.log 2>&1'
timely_terms_cron='17 6 * * * /usr/bin/flock -n /opt/bilive/autoslice/timely-terms.lock /bin/bash -lc '\''cd /opt/bilive/autoslice/repo && python3 scripts/crawl_timely_terms.py --cache-dir /opt/bilive/autoslice/cache/timely-term-crawler --write /opt/bilive/autoslice/state/timely_terms.json'\'' >> /opt/bilive/autoslice/logs/timely-terms.log 2>&1'
streamer_registry_cron='7 6 * * 0 /usr/bin/flock -n /opt/bilive/autoslice/streamer-registry.lock /bin/bash -lc '\''cd /opt/bilive/autoslice/repo && python3 scripts/crawl_streamer_registry.py --cache-dir /opt/bilive/autoslice/cache/streamer-registry-crawler --write /opt/bilive/autoslice/state/streamer_registry.json'\'' >> /opt/bilive/autoslice/logs/streamer-registry.log 2>&1'
psplive_roster_cron='12 6 * * 0 /usr/bin/flock -n /opt/bilive/autoslice/psplive-roster.lock /bin/bash -lc '\''cd /opt/bilive/autoslice/repo && python3 scripts/crawl_psplive_roster.py --cache-dir /opt/bilive/autoslice/cache/psplive-roster-crawler --write /opt/bilive/autoslice/state/psplive_roster.json'\'' >> /opt/bilive/autoslice/logs/psplive-roster.log 2>&1'
community_names_cron='27 6 * * * /usr/bin/flock -n /opt/bilive/autoslice/community-names.lock /bin/bash -lc '\''cd /opt/bilive/autoslice/repo && set -a && source /opt/bilive/autoslice/cpa.env && set +a && python3 scripts/crawl_community_names.py --registry /opt/bilive/autoslice/state/streamer_registry.json --cache-dir /opt/bilive/autoslice/cache/community-name-crawler --state /opt/bilive/autoslice/state/community_name_state.json --write /opt/bilive/autoslice/state/community_names.json'\'' >> /opt/bilive/autoslice/logs/community-names.log 2>&1'
topic_entity_cron='37 6 * * * /usr/bin/flock -n /opt/bilive/autoslice/topic-entity.lock /bin/bash -lc '\''cd /opt/bilive/autoslice/repo && python3 scripts/crawl_topic_entity_graph.py --timely-terms /opt/bilive/autoslice/state/timely_terms.json --cache-dir /opt/bilive/autoslice/cache/topic-entity-crawler --write /opt/bilive/autoslice/state/topic_entity_graph.json'\'' >> /opt/bilive/autoslice/logs/topic-entity.log 2>&1'
streamer_dynamics_cron='47 6 * * * /usr/bin/flock -n /opt/bilive/autoslice/streamer-dynamics.lock /bin/bash -lc '\''cd /opt/bilive/autoslice/repo && python3 scripts/crawl_streamer_dynamics.py --cache-dir /opt/bilive/autoslice/cache/streamer-dynamics-crawler --write /opt/bilive/autoslice/state/streamer_dynamics.json'\'' >> /opt/bilive/autoslice/logs/streamer-dynamics.log 2>&1'
managed_crontab_exact() {
    existing_crontab=$(crontab -l) || return 1
    for entry in \
        "free_mount_watchdog.sh:$watchdog_cron" \
        "upload_fatal_sentinel.sh:$upload_fatal_cron" \
        "scripts/crawl_timely_terms.py:$timely_terms_cron" \
        "scripts/crawl_streamer_registry.py:$streamer_registry_cron" \
        "scripts/crawl_psplive_roster.py:$psplive_roster_cron" \
        "scripts/crawl_community_names.py:$community_names_cron" \
        "scripts/crawl_topic_entity_graph.py:$topic_entity_cron" \
        "scripts/crawl_streamer_dynamics.py:$streamer_dynamics_cron"; do
        family=${entry%%:*}
        canonical=${entry#*:}
        test "$(printf '%s\n' "$existing_crontab" | grep -Fc -- "$family")" -eq 1 || return 1
        printf '%s\n' "$existing_crontab" | grep -Fxq -- "$canonical" || return 1
    done
}
external_payload_exact() {
    label=$1
    source=$2
    destination=$3
    source_mode=$4
    destination_mode=$5
    test ! -e "$backup/external/$label.absent" || return 1
    test ! -L "$backup/external/$label.absent" || return 1
    for path in "$source" "$destination" "$backup/external/$label.present" "$backup/external/$label.file"; do
        test -f "$path" || return 1
        test ! -L "$path" || return 1
    done
    test "$(stat -c '%a' "$source")" = "$source_mode" || return 1
    test "$(stat -c '%a' "$destination")" = "$destination_mode" || return 1
    test "$(stat -c '%a' "$backup/external/$label.file")" = "$destination_mode" || return 1
    cmp -s "$source" "$destination" || return 1
    cmp -s "$backup/external/$label.file" "$destination" || return 1
}
external_payload_unchanged_safe() {
    expected_adapter_sha=$1
    external_payload_exact watchdog \
        /opt/bilive/autoslice/repo/scripts/free_mount_watchdog.sh \
        /opt/bilive/autoslice/free_mount_watchdog.sh 755 755 || return 1
    external_payload_exact upload_sentinel \
        /opt/bilive/autoslice/repo/scripts/clouddrive_upload_fatal_sentinel.sh \
        /opt/bilive/autoslice/upload_fatal_sentinel.sh 755 755 || return 1
    external_payload_exact uploader \
        /opt/bilive/autoslice/repo/scripts/free_do_upload.sh \
        /opt/bilive/app/tmp_manual_upload/do_upload.sh 755 700 || return 1
    external_payload_exact recorder_adapter \
        "$new_adapter_source" "$host_adapter_path" 644 755 || return 1
    adapter_status_zero_touch_fresh || return 1
    adapter_environment_healthy "$expected_adapter_sha" || return 1
    managed_crontab_exact
}
test -f "$backup/external/recorder_adapter.present"
test -f "$backup/external/recorder_adapter.file"
test -f "$new_adapter_source"
test ! -L "$new_adapter_source"
test -f "$host_adapter_path"
test ! -L "$host_adapter_path"
cmp -s "$backup/external/recorder_adapter.file" "$host_adapter_path"
adapter_content_changed=0
if ! cmp -s "$new_adapter_source" "$host_adapter_path"; then
    adapter_content_changed=1
fi
old_adapter_sha=$(sha256sum "$host_adapter_path" | awk '{print $1}')
new_adapter_sha=$(sha256sum "$new_adapter_source" | awk '{print $1}')
connection_stub_bootstrap=0
test "$old_adapter_sha" = "$(sha256sum "$backup/external/recorder_adapter.file" | awk '{print $1}')"
external_payload_unchanged=0
if external_payload_unchanged_safe "$old_adapter_sha"; then
    # All four external targets already equal their committed payloads and the
    # captured preimage.  Do not touch files, cron, or the live adapter.
    external_payload_unchanged=1
elif [ "$adapter_content_changed" -eq 0 ]; then
    adapter_restart_safe "$old_adapter_sha"
elif adapter_restart_safe "$old_adapter_sha"; then
    :
elif adapter_connection_stub_bootstrap_safe "$old_adapter_sha" "$new_adapter_sha"; then
    connection_stub_bootstrap=1
else
    adapter_repair_restart_safe "$old_adapter_sha"
fi
if [ "$external_payload_unchanged" -eq 0 ]; then
    printf '%s\n' external-mutation-started.v1 > "$backup/external/external-mutation-started"
    chmod 600 "$backup/external/external-mutation-started"
    install_atomic \
        /opt/bilive/autoslice/repo/scripts/free_mount_watchdog.sh \
        /opt/bilive/autoslice/free_mount_watchdog.sh \
        755
    install_atomic \
        /opt/bilive/autoslice/repo/scripts/clouddrive_upload_fatal_sentinel.sh \
        /opt/bilive/autoslice/upload_fatal_sentinel.sh \
        755
    install_atomic \
        /opt/bilive/autoslice/repo/scripts/free_do_upload.sh \
        /opt/bilive/app/tmp_manual_upload/do_upload.sh \
        700
    adapter_identity_rebind_hash_child_absent
    install_atomic \
        "$new_adapter_source" \
        "$host_adapter_path" \
        755
    test "$new_adapter_sha" = "$(sha256sum "$host_adapter_path" | awk '{print $1}')"
    test "$new_adapter_sha" = "$(docker exec bililive_adapter sha256sum /state/bililive_recorder_adapter.py | awk '{print $1}')"
fi
if [ "$external_payload_unchanged" -eq 0 ] && [ "$adapter_content_changed" -eq 1 ]; then
    # Close the install-to-restart race with a second direct idle query. This
    # also imports the new bytes in a disposable process before the daemon is
    # restarted; failure here rolls the file back while the old daemon remains.
    adapter_restart_environment_safe "$new_adapter_sha"
    touch "$backup/external/recorder_adapter.restart-required"
    adapter_identity_rebind_hash_child_absent
    if [ "$connection_stub_bootstrap" -eq 1 ]; then
        activate_connection_stub_bootstrap_marker
    fi
    restart_epoch=$(python3 -c 'import time; print(time.time())')
    docker restart bililive_adapter >/dev/null
    wait_adapter_runtime "$restart_epoch" "$new_adapter_sha"
    if [ "$connection_stub_bootstrap" -eq 1 ]; then
        adapter_connection_stub_bootstrap_postcondition
    fi
elif [ "$external_payload_unchanged" -eq 0 ]; then
    test "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' bililive_adapter)" = healthy
fi
if [ "$external_payload_unchanged" -eq 0 ]; then
existing_crontab=$(crontab -l 2>/dev/null || true)
{
    printf '%s\n' "$existing_crontab" \
        | grep -Fv '/opt/bilive/autoslice/free_mount_watchdog.sh' \
        | grep -Fv '/opt/bilive/autoslice/upload_fatal_sentinel.sh' \
        | grep -Fv 'scripts/crawl_timely_terms.py' \
        | grep -Fv 'scripts/crawl_streamer_registry.py' \
        | grep -Fv 'scripts/crawl_psplive_roster.py' \
        | grep -Fv 'scripts/crawl_community_names.py' \
        | grep -Fv 'scripts/crawl_topic_entity_graph.py' \
        | grep -Fv 'scripts/crawl_streamer_dynamics.py' || true
    printf '%s\n' "$watchdog_cron"
    printf '%s\n' "$upload_fatal_cron"
    printf '%s\n' "$streamer_registry_cron"
    printf '%s\n' "$psplive_roster_cron"
    printf '%s\n' "$timely_terms_cron"
    printf '%s\n' "$community_names_cron"
    printf '%s\n' "$topic_entity_cron"
    printf '%s\n' "$streamer_dynamics_cron"
} | crontab -
crontab -l | grep -Fxq "$watchdog_cron"
test "$(crontab -l | grep -Fxc "$watchdog_cron")" -eq 1
crontab -l | grep -Fxq "$upload_fatal_cron"
test "$(crontab -l | grep -Fxc "$upload_fatal_cron")" -eq 1
crontab -l | grep -Fxq "$timely_terms_cron"
test "$(crontab -l | grep -Fxc "$timely_terms_cron")" -eq 1
crontab -l | grep -Fxq "$streamer_registry_cron"
test "$(crontab -l | grep -Fxc "$streamer_registry_cron")" -eq 1
crontab -l | grep -Fxq "$psplive_roster_cron"
test "$(crontab -l | grep -Fxc "$psplive_roster_cron")" -eq 1
crontab -l | grep -Fxq "$community_names_cron"
test "$(crontab -l | grep -Fxc "$community_names_cron")" -eq 1
crontab -l | grep -Fxq "$topic_entity_cron"
test "$(crontab -l | grep -Fxc "$topic_entity_cron")" -eq 1
crontab -l | grep -Fxq "$streamer_dynamics_cron"
test "$(crontab -l | grep -Fxc "$streamer_dynamics_cron")" -eq 1
fi
if [ "$external_payload_unchanged" -eq 1 ]; then
    # Close the read-only fast-route interval: success is valid only while the
    # same payload, cron, adapter and status closure remains true.
    external_payload_unchanged_safe "$old_adapter_sha"
fi
REMOTE_EXTERNAL_INSTALL

# verify: the deployed runner is byte-identical to the committed one
LOCAL_MD5=$(md5 -q "$LOCAL_ARCHIVE_DIR/scripts/free_session_autoslice.py" 2>/dev/null || md5sum "$LOCAL_ARCHIVE_DIR/scripts/free_session_autoslice.py" | cut -d' ' -f1)
REMOTE_MD5=$(ssh "$HOST" "md5sum /opt/bilive/autoslice/repo/scripts/free_session_autoslice.py" | cut -d' ' -f1)
if [ "$LOCAL_MD5" != "$REMOTE_MD5" ]; then
    echo "DEPLOY VERIFY FAILED: runner md5 mismatch (local $LOCAL_MD5 remote $REMOTE_MD5)" >&2
    exit 3
fi
REMOTE_DEPLOYED_MANIFEST=$(ssh "$HOST" python3 - "$REMOTE_REPO" <<'REMOTE_DEPLOYED_MANIFEST_PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = {}
for component in (
    "scripts",
    "src",
    "ops",
    "assets",
    "profiles",
    ".agent",
    "docs",
    "cleanup_manifests",
    "AGENTS.md",
    "README.md",
):
    base = root / component
    if not base.exists():
        continue  # a tree added in this commit is absent from the prior deployment
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
print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
REMOTE_DEPLOYED_MANIFEST_PY
)
if [ "$LOCAL_MANIFEST" != "$REMOTE_DEPLOYED_MANIFEST" ]; then
    echo "DEPLOY VERIFY FAILED: switched production tree differs from committed tree" >&2
    exit 3
fi

# The complete deployed tree has now matched the frozen git archive. Seal the
# small set of security-sensitive authority assets from those exact deployed
# bytes. The manifest is installed before DEPLOYED_COMMIT, so a crash between
# the two atomic renames fails closed (commit mismatch); both files are restored
# from the rollback tree on every local or remote failure path.
ssh "$HOST" /usr/bin/flock -w 7200 "$REMOTE_BASE/runner.lock" bash -s -- \
    "$REMOTE_REPO" "$BACKUP" "$COMMIT" <<'REMOTE_SEAL_DEPLOYMENT_IDENTITY'
set -euo pipefail
repo=$1
backup=$2
commit=$3
manifest_path=$repo/DEPLOYED_AUTHORITY_MANIFEST.json
commit_path=$repo/DEPLOYED_COMMIT
umask 022

manifest_tmp=$(mktemp "$repo/.DEPLOYED_AUTHORITY_MANIFEST.json.deploy.XXXXXX")
commit_tmp=$(mktemp "$repo/.DEPLOYED_COMMIT.deploy.XXXXXX")
chmod 644 "$manifest_tmp" "$commit_tmp"
cleanup_tmp() {
    [ -z "${manifest_tmp:-}" ] || rm -f "$manifest_tmp"
    [ -z "${commit_tmp:-}" ] || rm -f "$commit_tmp"
}
restore_repository_file() {
    label=$1
    destination=$2
    if [ -f "$backup/repository/$label.present" ]; then
        tmp=$destination.rollback.$$
        cp -p "$backup/repository/$label.file" "$tmp"
        cmp -s "$backup/repository/$label.file" "$tmp"
        mv -f "$tmp" "$destination"
        cmp -s "$backup/repository/$label.file" "$destination"
    elif [ -f "$backup/repository/$label.absent" ]; then
        rm -f "$destination"
        test ! -e "$destination"
    else
        echo "missing repository rollback marker: $label" >&2
        return 1
    fi
}
restore_identity() {
    cleanup_tmp
    restore_repository_file deployed_authority_manifest "$manifest_path"
    cp "$backup/DEPLOYED_COMMIT.old" "$commit_path"
    cmp -s "$backup/DEPLOYED_COMMIT.old" "$commit_path"
}
trap 'rc=$?; trap - ERR; restore_identity; exit "$rc"' ERR
trap 'trap - ERR HUP INT TERM; restore_identity; exit 129' HUP
trap 'trap - ERR HUP INT TERM; restore_identity; exit 130' INT
trap 'trap - ERR HUP INT TERM; restore_identity; exit 143' TERM

PYTHONDONTWRITEBYTECODE=1 python3 - \
    "$repo" "$commit" "$manifest_tmp" <<'REMOTE_AUTHORITY_MANIFEST_PY'
import json
import os
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
commit = sys.argv[2]
output = Path(sys.argv[3])
if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
    raise SystemExit("invalid deployed commit for authority manifest")
sys.path.insert(0, str(root))

from src.autoslice.repository_asset_authority import (  # noqa: E402
    build_deployed_authority_manifest,
)

registry = root / "assets/lidousha/publication_registry.v1.json"
authority_dir = root / "assets/lidousha"
if registry.is_symlink() or not registry.is_file():
    raise SystemExit("publication registry authority is missing or unsafe")
authority_paths = []
if authority_dir.exists():
    if authority_dir.is_symlink() or not authority_dir.is_dir():
        raise SystemExit("authority asset directory is unsafe")
    for candidate in authority_dir.rglob("*"):
        if candidate.is_symlink():
            raise SystemExit(f"authority asset tree contains symlink: {candidate}")
        if candidate.is_file() and candidate.suffix in {".json", ".srt"}:
            authority_paths.append(candidate)

manifest = build_deployed_authority_manifest(
    repo_root=root,
    deployed_commit=commit,
    relative_paths=[
        path.relative_to(root)
        for path in (registry, *sorted(authority_paths))
    ],
)
serialized = (
    json.dumps(
        manifest,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    + "\n"
).encode("utf-8")
with output.open("wb") as handle:
    handle.write(serialized)
    handle.flush()
    os.fsync(handle.fileno())
REMOTE_AUTHORITY_MANIFEST_PY
printf '%s  deployed %s\n' "$commit" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$commit_tmp"
python3 - "$commit_tmp" <<'REMOTE_FSYNC_COMMIT_STAMP_PY'
import os
import sys

with open(sys.argv[1], "rb") as handle:
    os.fsync(handle.fileno())
REMOTE_FSYNC_COMMIT_STAMP_PY

# Install manifest first: until the commit stamp moves, readers reject the new
# manifest rather than accepting an authority document under the old identity.
mv -f "$manifest_tmp" "$manifest_path"
manifest_tmp=
mv -f "$commit_tmp" "$commit_path"
commit_tmp=

PYTHONDONTWRITEBYTECODE=1 python3 - \
    "$repo" "$commit" <<'REMOTE_VERIFY_DEPLOYMENT_IDENTITY_PY'
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
expected_commit = sys.argv[2]
sys.path.insert(0, str(root))

from src.autoslice.repository_asset_authority import (  # noqa: E402
    DEPLOYED_AUTHORITY_MANIFEST_SCHEMA,
    build_deployed_authority_manifest,
    require_repository_asset_authority,
)

commit_path = root / "DEPLOYED_COMMIT"
manifest_path = root / "DEPLOYED_AUTHORITY_MANIFEST.json"
actual_commit = commit_path.read_text(encoding="utf-8").split(maxsplit=1)[0]
if actual_commit != expected_commit:
    raise SystemExit("deployed commit readback mismatch")
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
registry = root / "assets/lidousha/publication_registry.v1.json"
authority_dir = root / "assets/lidousha"
if registry.is_symlink() or not registry.is_file():
    raise SystemExit("publication registry authority is missing or unsafe")
authority_paths = []
if authority_dir.exists():
    if authority_dir.is_symlink() or not authority_dir.is_dir():
        raise SystemExit("authority asset directory is unsafe")
    for candidate in authority_dir.rglob("*"):
        if candidate.is_symlink():
            raise SystemExit(f"authority asset tree contains symlink: {candidate}")
        if candidate.is_file() and candidate.suffix in {".json", ".srt"}:
            authority_paths.append(candidate)
relative_paths = [
    path.relative_to(root)
    for path in (registry, *sorted(authority_paths))
]
expected_manifest = build_deployed_authority_manifest(
    repo_root=root,
    deployed_commit=actual_commit,
    relative_paths=relative_paths,
)
if manifest != expected_manifest:
    raise SystemExit("deployed authority manifest readback mismatch")
if manifest.get("schema_version") != DEPLOYED_AUTHORITY_MANIFEST_SCHEMA:
    raise SystemExit("deployed authority manifest schema mismatch")
for relative in relative_paths:
    path = root / relative
    authority = require_repository_asset_authority(
        repo_root=root,
        relative_path=relative,
        observed_bytes=path.read_bytes(),
    )
    if authority.commit != actual_commit or authority.mode != "DEPLOYED_MANIFEST":
        raise SystemExit(f"deployed authority loader rejected identity: {relative}")
directory_fd = os.open(root, os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
REMOTE_VERIFY_DEPLOYMENT_IDENTITY_PY

trap - ERR HUP INT TERM
cleanup_tmp
REMOTE_SEAL_DEPLOYMENT_IDENTITY
COMMITTED=1
ssh "$HOST" "rm -rf '$BACKUP'" || echo "WARN: deployed successfully but rollback-tree cleanup failed" >&2
echo "deployed $COMMIT to $HOST (runner md5 verified)"
