#!/bin/bash
# Deploy the autoslice runner to free (2026-07-09 audit: no more dirty-tree
# deploys, no more "which code is production actually running?").
#
# - REFUSES a dirty working tree (production must be reproducible from a commit)
# - streams committed runtime trees plus project authority/docs into staging
#   (scripts/ src/ assets/ profiles/ .agent/ docs/ cleanup_manifests/
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
                "$REMOTE_REPO" "$STAGE" "$BACKUP" "$OLD_COMMIT" <<'REMOTE_ROLLBACK'
set -euo pipefail
repo=$1
stage=$2
backup=$3
old_commit=$4
test -d "$backup"
test -f "$backup/repo.manifest.old.json"
for component in scripts src assets profiles .agent docs cleanup_manifests AGENTS.md README.md; do
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
restore_file upload_sentinel /opt/bilive/autoslice/upload_fatal_sentinel.sh
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
for component in (
    "scripts",
    "src",
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
    scripts src assets profiles .agent docs cleanup_manifests AGENTS.md README.md \
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
    scripts src assets profiles .agent docs cleanup_manifests AGENTS.md README.md \
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
    "$REMOTE_REPO" "$STAGE" "$BACKUP" <<'REMOTE_SWITCH'
set -euo pipefail
repo=$1
stage=$2
backup=$3
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
        cp -p "$source" "$backup/external/$label.file"
        touch "$backup/external/$label.present"
    else
        touch "$backup/external/$label.absent"
    fi
}
capture_file watchdog /opt/bilive/autoslice/free_mount_watchdog.sh
capture_file upload_sentinel /opt/bilive/autoslice/upload_fatal_sentinel.sh
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
    for component in scripts src assets profiles .agent docs cleanup_manifests AGENTS.md README.md; do
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
    restore_file watchdog /opt/bilive/autoslice/free_mount_watchdog.sh
    restore_file upload_sentinel /opt/bilive/autoslice/upload_fatal_sentinel.sh
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
for component in scripts src assets profiles .agent docs cleanup_manifests AGENTS.md README.md; do
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
    test -f "$source"
    test ! -L "$source"
    mkdir -p "$(dirname "$destination")"
    tmp=$destination.deploy.$$
    cp "$source" "$tmp"
    chmod "$mode" "$tmp"
    test "$(sha256sum "$source" | awk '{print $1}')" = "$(sha256sum "$tmp" | awk '{print $1}')"
    mv -f "$tmp" "$destination"
    tmp=
    test -f "$destination"
    test ! -L "$destination"
    test "$(stat -c '%a' "$destination")" = "$mode"
    test "$(sha256sum "$source" | awk '{print $1}')" = "$(sha256sum "$destination" | awk '{print $1}')"
}
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
watchdog_cron='*/5 * * * * /usr/bin/flock -n /opt/bilive/autoslice/watchdog.lock /opt/bilive/autoslice/free_mount_watchdog.sh >> /opt/bilive/autoslice/logs/watchdog.log 2>&1'
upload_fatal_cron='*/5 * * * * /usr/bin/flock -n /opt/bilive/autoslice/upload-fatal-sentinel.lock /opt/bilive/autoslice/upload_fatal_sentinel.sh >> /opt/bilive/autoslice/logs/upload-fatal-sentinel.log 2>&1'
timely_terms_cron='17 6 * * * /usr/bin/flock -n /opt/bilive/autoslice/timely-terms.lock /bin/bash -lc '\''cd /opt/bilive/autoslice/repo && python3 scripts/crawl_timely_terms.py --cache-dir /opt/bilive/autoslice/cache/timely-term-crawler --write /opt/bilive/autoslice/state/timely_terms.json'\'' >> /opt/bilive/autoslice/logs/timely-terms.log 2>&1'
streamer_registry_cron='7 6 * * 0 /usr/bin/flock -n /opt/bilive/autoslice/streamer-registry.lock /bin/bash -lc '\''cd /opt/bilive/autoslice/repo && python3 scripts/crawl_streamer_registry.py --cache-dir /opt/bilive/autoslice/cache/streamer-registry-crawler --write /opt/bilive/autoslice/state/streamer_registry.json'\'' >> /opt/bilive/autoslice/logs/streamer-registry.log 2>&1'
psplive_roster_cron='12 6 * * 0 /usr/bin/flock -n /opt/bilive/autoslice/psplive-roster.lock /bin/bash -lc '\''cd /opt/bilive/autoslice/repo && python3 scripts/crawl_psplive_roster.py --cache-dir /opt/bilive/autoslice/cache/psplive-roster-crawler --write /opt/bilive/autoslice/state/psplive_roster.json'\'' >> /opt/bilive/autoslice/logs/psplive-roster.log 2>&1'
community_names_cron='27 6 * * * /usr/bin/flock -n /opt/bilive/autoslice/community-names.lock /bin/bash -lc '\''cd /opt/bilive/autoslice/repo && set -a && source /opt/bilive/autoslice/cpa.env && set +a && python3 scripts/crawl_community_names.py --registry /opt/bilive/autoslice/state/streamer_registry.json --cache-dir /opt/bilive/autoslice/cache/community-name-crawler --state /opt/bilive/autoslice/state/community_name_state.json --write /opt/bilive/autoslice/state/community_names.json'\'' >> /opt/bilive/autoslice/logs/community-names.log 2>&1'
topic_entity_cron='37 6 * * * /usr/bin/flock -n /opt/bilive/autoslice/topic-entity.lock /bin/bash -lc '\''cd /opt/bilive/autoslice/repo && python3 scripts/crawl_topic_entity_graph.py --timely-terms /opt/bilive/autoslice/state/timely_terms.json --cache-dir /opt/bilive/autoslice/cache/topic-entity-crawler --write /opt/bilive/autoslice/state/topic_entity_graph.json'\'' >> /opt/bilive/autoslice/logs/topic-entity.log 2>&1'
streamer_dynamics_cron='47 6 * * * /usr/bin/flock -n /opt/bilive/autoslice/streamer-dynamics.lock /bin/bash -lc '\''cd /opt/bilive/autoslice/repo && python3 scripts/crawl_streamer_dynamics.py --cache-dir /opt/bilive/autoslice/cache/streamer-dynamics-crawler --write /opt/bilive/autoslice/state/streamer_dynamics.json'\'' >> /opt/bilive/autoslice/logs/streamer-dynamics.log 2>&1'
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
