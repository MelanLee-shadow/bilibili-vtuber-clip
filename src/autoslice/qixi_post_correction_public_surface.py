"""Candidate-sealed post-subtitle public-surface closure for 女友感.

This is intentionally a one-candidate recovery lane.  It stages the existing
source-fact/title/cover machinery under a private artifact root, freezes every
resulting byte in a resumable journal, and only then projects the same result
to the package, delivery mirror, and one state row.  It never changes subtitle,
ASS, media, the correction receipt, or upload permission.
"""

from __future__ import annotations

import copy
import base64
import fcntl
import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from src.autoslice.llm_client import LlmConfig, LlmCall, build_llm_call
from src.autoslice.publish_staging import _stage_publish_draft
from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthorityError,
    require_repository_asset_authority,
)
from src.autoslice.story_contract import audit_story_artifact, build_story_contract
from src.autoslice.source_fact_review import validate_source_fact_review
from src.autoslice.review_package_portable_evidence import rebuild_package_speaker_evidence
from src.autoslice.qixi_post_correction_projection_paths import (
    QixiPostCorrectionPublicSurfaceError,
    adopt_staged_file as _adopt_staged_file,
    assert_no_stage_locator as _assert_no_stage_locator,
    copy_sealed_stage_file as _copy_sealed_stage_file,
    clear_staged_file_owner as _clear_staged_file_owner,
    create_staged_file as _create_staged_file,
    package_cover_paths as _package_cover_paths,
    materialized_public_targets as _materialized_public_targets,
    prepare_stage_documents as _prepare_stage_documents,
    project_state_pick as _project_state_pick,
    public_artifact_root as _public_artifact_root,
    replace_stage_locators as _replace_stage_locators,
    state_diff_is_exact as _state_diff_is_exact,
    sha256_bytes as _file_sha256_from_bytes,
    source_cues as _source_cues,
    validate_manual_title_projection as _validate_manual_title_projection,
    validate_replayed_cover as _validate_replayed_cover,
    validate_public_artifact_namespace_absent,
)

ROOT = Path(__file__).resolve().parents[2]
RELATIVE_AUTHORITY_PATH = Path(
    "assets/lidousha/qixi_post_correction_public_surface/auto_123655_771_844.v1.json"
)
CANDIDATE_ID = "auto_123655_771_844"
RECORDING_DATE = "2026-08-17"
MANUAL_TITLE = "小李有女友感吗？宿敌是否有点亲密了"
PUBLIC_TITLE = f"【李豆沙】{MANUAL_TITLE}"
SCHEMA_VERSION, JOURNAL_SCHEMA_VERSION = "qixi-post-correction-public-surface-authority.v1", "qixi-post-correction-public-surface-journal.v1"
_JOURNAL_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "candidate_id",
        "recording_date",
        "upload_enabled",
        "authority_sha256",
        "entries",
        "metadata",
        "metadata_sha256",
        "journal_sha256",
    }
)
_JOURNAL_ENTRY_KEYS = frozenset(
    {
        "role",
        "target",
        "before_bytes_b64",
        "before_sha256",
        "before_mode",
        "before_device",
        "before_inode",
        "after_bytes_b64",
        "after_sha256",
        "after_mode",
        "backup_name",
        "staged_name",
        "staged_device",
        "staged_inode",
        "phase",
        "installed_device",
        "installed_inode",
    }
)

_ENTRY_PHASES = frozenset({"PREPARED", "BACKED_UP", "INSTALLING", "INSTALLED"})
_AUTHORITY_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_id",
        "recording_date",
        "upload_enabled",
        "manual_title",
        "runtime_root",
        "state_file",
        "artifacts",
        "repository_inputs",
        "source_fact_preimage",
        "sealed_before",
        "authority_sha256",
    }
)
_ARTIFACT_ROLES = frozenset(
    {
        "record",
        "delivery_record",
        "publish",
        "main",
        "srt",
        "ass",
        "burn",
        "correction",
        "cover",
        "chat",
        "clip_context",
        "timing",
    }
)
_REPOSITORY_INPUTS = frozenset(
    {"subtitle_authority", "diagnostic", "manual_title_overrides"}
)
_DESCRIPTOR_FIELDS = frozenset({"path", "sha256", "bytes"})
_STATE_DESCRIPTOR_FIELDS = frozenset({"path", "sha256", "bytes", "mode"})
_SOURCE_FACT_PREIMAGE_FIELDS = frozenset(
    {
        "clip_context_prompt_sha256",
        "clip_context_prompt_bytes",
        "source_fact_receipt_sha256",
        "speaker_evidence",
        "speaker_evidence_sha256",
    }
)
_SEALED_BEFORE_FIELDS = frozenset({"record", "delivery_record", "publish", "state"})


def _canonical_sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _b64(payload: bytes | None) -> str | None:
    return None if payload is None else base64.b64encode(payload).decode("ascii")


def _unb64(value: object, *, label: str) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise QixiPostCorrectionPublicSurfaceError(f"{label} payload is invalid")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except ValueError as exc:
        raise QixiPostCorrectionPublicSurfaceError(f"{label} payload is invalid") from exc


def _require_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        raise QixiPostCorrectionPublicSurfaceError(f"{label} must be a SHA-256 value")
    try:
        int(value[7:], 16)
    except ValueError as exc:
        raise QixiPostCorrectionPublicSurfaceError(f"{label} must be a SHA-256 value") from exc
    return value


def _require_regular(path: Path, *, label: str) -> None:
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        try:
            mode = os.lstat(cursor).st_mode
        except OSError as exc:
            raise QixiPostCorrectionPublicSurfaceError(f"{label} is unavailable: {path}") from exc
        if stat.S_ISLNK(mode):
            raise QixiPostCorrectionPublicSurfaceError(f"{label} contains a symlink: {path}")
    if not path.is_file():
        raise QixiPostCorrectionPublicSurfaceError(f"{label} is not a regular file: {path}")


def _descriptor(value: object, *, label: str, repository: bool = False) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _DESCRIPTOR_FIELDS:
        raise QixiPostCorrectionPublicSurfaceError(f"{label} descriptor fields drift")
    descriptor = dict(value)
    path = descriptor["path"]
    if not isinstance(path, str) or not path:
        raise QixiPostCorrectionPublicSurfaceError(f"{label} descriptor path is invalid")
    if repository:
        if Path(path).is_absolute() or ".." in Path(path).parts:
            raise QixiPostCorrectionPublicSurfaceError(f"{label} repository path is invalid")
    elif not Path(path).is_absolute():
        raise QixiPostCorrectionPublicSurfaceError(f"{label} runtime path is invalid")
    _require_sha(descriptor["sha256"], label=f"{label} sha256")
    if isinstance(descriptor["bytes"], bool) or not isinstance(descriptor["bytes"], int) or descriptor["bytes"] < 0:
        raise QixiPostCorrectionPublicSurfaceError(f"{label} descriptor bytes is invalid")
    return descriptor


def _state_descriptor(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _STATE_DESCRIPTOR_FIELDS:
        raise QixiPostCorrectionPublicSurfaceError("state descriptor fields drift")
    descriptor = dict(value)
    path = descriptor["path"]
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise QixiPostCorrectionPublicSurfaceError("state descriptor path is invalid")
    _require_sha(descriptor["sha256"], label="state descriptor sha256")
    if isinstance(descriptor["bytes"], bool) or not isinstance(descriptor["bytes"], int) or descriptor["bytes"] < 0:
        raise QixiPostCorrectionPublicSurfaceError("state descriptor bytes is invalid")
    if isinstance(descriptor["mode"], bool) or not isinstance(descriptor["mode"], int) or descriptor["mode"] < 0 or descriptor["mode"] > 0o777:
        raise QixiPostCorrectionPublicSurfaceError("state descriptor mode is invalid")
    return descriptor


def _source_fact_preimage(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _SOURCE_FACT_PREIMAGE_FIELDS:
        raise QixiPostCorrectionPublicSurfaceError("source-fact preimage fields drift")
    preimage = dict(value)
    for key in (
        "clip_context_prompt_sha256",
        "source_fact_receipt_sha256",
        "speaker_evidence_sha256",
    ):
        _require_sha(preimage[key], label=f"source-fact preimage {key}")
    if (
        isinstance(preimage["clip_context_prompt_bytes"], bool)
        or not isinstance(preimage["clip_context_prompt_bytes"], int)
        or preimage["clip_context_prompt_bytes"] < 0
        or not isinstance(preimage["speaker_evidence"], Mapping)
        or _canonical_sha256(preimage["speaker_evidence"])
        != preimage["speaker_evidence_sha256"]
    ):
        raise QixiPostCorrectionPublicSurfaceError("source-fact preimage binding drifts")
    return preimage


def _sealed_before(authority: Mapping[str, object]) -> dict[str, dict[str, object]]:
    value = authority.get("sealed_before")
    if not isinstance(value, Mapping) or set(value) != _SEALED_BEFORE_FIELDS:
        raise QixiPostCorrectionPublicSurfaceError("sealed-before descriptor set drifts")
    artifacts = authority["artifacts"]
    assert isinstance(artifacts, Mapping)
    expected_paths = {
        "record": _descriptor(artifacts["record"], label="artifact record")["path"],
        "delivery_record": _descriptor(artifacts["delivery_record"], label="artifact delivery")["path"],
        "publish": _descriptor(artifacts["publish"], label="artifact publish")["path"],
        "state": _state_descriptor(authority["state_file"])["path"],
    }
    normalized: dict[str, dict[str, object]] = {}
    for role in _SEALED_BEFORE_FIELDS:
        descriptor = _state_descriptor(value[role])
        if descriptor["path"] != expected_paths[role]:
            raise QixiPostCorrectionPublicSurfaceError("sealed-before descriptor path drifts")
        baseline = (
            _state_descriptor(authority["state_file"])
            if role == "state"
            else _descriptor(artifacts[role], label=f"artifact {role}")
        )
        if (
            descriptor["sha256"] != baseline["sha256"]
            or descriptor["bytes"] != baseline["bytes"]
        ):
            raise QixiPostCorrectionPublicSurfaceError("sealed-before descriptor content drifts")
        normalized[role] = descriptor
    return normalized


def validate_authority(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _AUTHORITY_FIELDS:
        raise QixiPostCorrectionPublicSurfaceError("post-correction authority fields drift")
    authority = dict(value)
    declared = authority.pop("authority_sha256")
    if authority.get("schema_version") != SCHEMA_VERSION:
        raise QixiPostCorrectionPublicSurfaceError("post-correction authority schema drifts")
    if authority.get("candidate_id") != CANDIDATE_ID or authority.get("recording_date") != RECORDING_DATE:
        raise QixiPostCorrectionPublicSurfaceError("post-correction authority identity drifts")
    if authority.get("upload_enabled") is not False or authority.get("manual_title") != MANUAL_TITLE:
        raise QixiPostCorrectionPublicSurfaceError("post-correction authority public surface drifts")
    root = authority.get("runtime_root")
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise QixiPostCorrectionPublicSurfaceError("post-correction authority runtime root is invalid")
    if _require_sha(declared, label="post-correction authority self hash") != _canonical_sha256(authority):
        raise QixiPostCorrectionPublicSurfaceError("post-correction authority self hash drifts")
    artifacts = authority.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != _ARTIFACT_ROLES:
        raise QixiPostCorrectionPublicSurfaceError("post-correction authority artifact set drifts")
    for role, descriptor in artifacts.items():
        _descriptor(descriptor, label=f"artifact {role}")
    state = _state_descriptor(authority.get("state_file"))
    if Path(str(state["path"])) != Path(root) / "state" / f"{RECORDING_DATE}.json":
        raise QixiPostCorrectionPublicSurfaceError("state descriptor path drifts")
    repository_inputs = authority.get("repository_inputs")
    if not isinstance(repository_inputs, Mapping) or set(repository_inputs) != _REPOSITORY_INPUTS:
        raise QixiPostCorrectionPublicSurfaceError("post-correction authority repository input set drifts")
    for role, descriptor in repository_inputs.items():
        _descriptor(descriptor, label=f"repository input {role}", repository=True)
    _source_fact_preimage(authority.get("source_fact_preimage"))
    _sealed_before(authority)
    authority["authority_sha256"] = declared
    return authority


def load_deployed_authority(repo_root: Path = ROOT) -> dict[str, object]:
    path = repo_root / RELATIVE_AUTHORITY_PATH
    _require_regular(path, label="post-correction authority")
    raw = path.read_bytes()
    try:
        require_repository_asset_authority(
            repo_root=repo_root, relative_path=RELATIVE_AUTHORITY_PATH, observed_bytes=raw
        )
        return validate_authority(json.loads(raw))
    except (RepositoryAssetAuthorityError, ValueError, json.JSONDecodeError) as exc:
        raise QixiPostCorrectionPublicSurfaceError(
            f"post-correction authority is not deployment-sealed: {exc}"
        ) from exc


def _validate_file_descriptor(descriptor: Mapping[str, object], *, label: str) -> Path:
    path = Path(str(descriptor["path"]))
    _require_regular(path, label=label)
    if path.stat().st_size != descriptor["bytes"] or _file_sha256(path) != descriptor["sha256"]:
        raise QixiPostCorrectionPublicSurfaceError(f"{label} bytes drift")
    return path


def _validate_manual_title_asset(repo_root: Path, descriptor: Mapping[str, object]) -> None:
    path = repo_root / str(descriptor["path"])
    _require_regular(path, label="manual title override asset")
    raw = path.read_bytes()
    if len(raw) != descriptor["bytes"] or "sha256:" + hashlib.sha256(raw).hexdigest() != descriptor["sha256"]:
        raise QixiPostCorrectionPublicSurfaceError("manual title override asset bytes drift")
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise QixiPostCorrectionPublicSurfaceError("manual title override asset is invalid") from exc
    matches = [
        row
        for row in document.get("overrides", [])
        if isinstance(row, Mapping) and row.get("candidate_id") == CANDIDATE_ID
    ]
    if len(matches) != 1 or matches[0].get("title") != MANUAL_TITLE:
        raise QixiPostCorrectionPublicSurfaceError("manual title override binding drifts")


def _validate_repository_inputs(authority: Mapping[str, object], *, repo_root: Path) -> None:
    repository_inputs = authority["repository_inputs"]
    assert isinstance(repository_inputs, Mapping)
    for role, value in repository_inputs.items():
        descriptor = _descriptor(value, label=f"repository input {role}", repository=True)
        path = repo_root / str(descriptor["path"])
        _require_regular(path, label=f"repository input {role}")
        raw = path.read_bytes()
        if len(raw) != descriptor["bytes"] or "sha256:" + hashlib.sha256(raw).hexdigest() != descriptor["sha256"]:
            raise QixiPostCorrectionPublicSurfaceError(f"repository input {role} bytes drift")
        try:
            require_repository_asset_authority(
                repo_root=repo_root, relative_path=Path(str(descriptor["path"])), observed_bytes=raw
            )
        except RepositoryAssetAuthorityError as exc:
            raise QixiPostCorrectionPublicSurfaceError(f"repository input {role} is unsealed") from exc
    _validate_manual_title_asset(
        repo_root,
        _descriptor(repository_inputs["manual_title_overrides"], label="manual title overrides", repository=True),
    )


@dataclass(frozen=True, slots=True)
class RuntimeInputs:
    authority: dict[str, object]
    artifact_paths: dict[str, Path]
    record: dict[str, object]
    publish: dict[str, object]
    state_path: Path
    state: dict[str, object]
    state_sha256: str
    state_pick_index: int


@dataclass(frozen=True, slots=True)
class InstalledSnapshot:
    path: Path
    device: int
    inode: int
    sha256: str
    mode: int


def validate_runtime(
    authority: Mapping[str, object], *, repo_root: Path, runtime_root: Path
) -> RuntimeInputs:
    normalized = validate_authority(authority)
    if runtime_root != Path(str(normalized["runtime_root"])):
        raise QixiPostCorrectionPublicSurfaceError("runtime root differs from the sealed authority")
    artifacts = normalized["artifacts"]
    assert isinstance(artifacts, Mapping)
    paths = {
        role: _validate_file_descriptor(_descriptor(value, label=f"artifact {role}"), label=f"artifact {role}")
        for role, value in artifacts.items()
    }
    sealed_before = _sealed_before(normalized)
    for role, descriptor in sealed_before.items():
        path = Path(str(descriptor["path"]))
        _require_regular(path, label=f"sealed-before {role}")
        if stat.S_IMODE(os.lstat(path).st_mode) != descriptor["mode"]:
            raise QixiPostCorrectionPublicSurfaceError(f"sealed-before {role} mode drifts")
    _validate_repository_inputs(normalized, repo_root=repo_root)
    try:
        record = json.loads(paths["record"].read_text(encoding="utf-8"))
        delivery_record = json.loads(paths["delivery_record"].read_text(encoding="utf-8"))
        publish = json.loads(paths["publish"].read_text(encoding="utf-8"))
        correction = json.loads(paths["correction"].read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QixiPostCorrectionPublicSurfaceError("sealed runtime JSON is unreadable") from exc
    if record != delivery_record or not isinstance(record, dict) or not isinstance(publish, dict):
        raise QixiPostCorrectionPublicSurfaceError("package/delivery record mirror drifts")
    validate_public_artifact_namespace_absent(
        paths["record"].parent,
        normalized,
        require_safe_parent=_require_safe_target_parent,
    )
    hashes = record.get("artifact_hashes")
    if not isinstance(hashes, Mapping) or any(
        hashes.get(key) != artifacts[role]["sha256"]
        for key, role in (
            ("video_sha256", "main"),
            ("subtitle_sha256", "srt"),
            ("ass_sha256", "ass"),
            ("burned_video_sha256", "burn"),
        )
    ):
        raise QixiPostCorrectionPublicSurfaceError("record final media hashes drift")
    if record.get("media_path") != str(paths["main"]) or record.get("subtitle_path") != str(paths["srt"]):
        raise QixiPostCorrectionPublicSurfaceError("record media locator drifts")
    correction_after_srt = correction.get("after_srt_sha256")
    if isinstance(correction_after_srt, str) and len(correction_after_srt) == 64:
        correction_after_srt = "sha256:" + correction_after_srt
    if (
        correction.get("candidate_id") != CANDIDATE_ID
        or correction_after_srt != artifacts["srt"]["sha256"]
    ):
        raise QixiPostCorrectionPublicSurfaceError("correction receipt does not bind current subtitle")
    if record.get("human_text_correction_manifest_sha256") != artifacts["correction"]["sha256"]:
        raise QixiPostCorrectionPublicSurfaceError("record correction receipt binding drifts")
    _validate_source_fact_preimage(normalized, record=record, publish=publish, paths=paths)
    state_descriptor = _state_descriptor(normalized["state_file"])
    state_path = Path(str(state_descriptor["path"]))
    _require_regular(state_path, label="candidate state")
    state_stat = os.lstat(state_path)
    if (
        state_path.stat().st_size != state_descriptor["bytes"]
        or _file_sha256(state_path) != state_descriptor["sha256"]
        or stat.S_IMODE(state_stat.st_mode) != state_descriptor["mode"]
    ):
        raise QixiPostCorrectionPublicSurfaceError("candidate state preimage drifts")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QixiPostCorrectionPublicSurfaceError("candidate state is unreadable") from exc
    picks = state.get("picks") if isinstance(state, Mapping) else None
    matches = [
        index for index, row in enumerate(picks or []) if isinstance(row, Mapping) and row.get("candidate_id") == CANDIDATE_ID
    ]
    if len(matches) != 1:
        raise QixiPostCorrectionPublicSurfaceError("candidate state pick is missing or ambiguous")
    return RuntimeInputs(
        normalized,
        paths,
        record,
        publish,
        state_path,
        dict(state),
        _file_sha256(state_path),
        matches[0],
    )


def _validate_source_fact_preimage(
    authority: Mapping[str, object],
    *,
    record: Mapping[str, object],
    publish: Mapping[str, object],
    paths: Mapping[str, Path],
) -> None:
    """Replay the sealed uniform-host source-fact input before provider staging.

    This candidate has no speaker sidecar by design.  The absence is therefore
    a typed input (not an omitted optional file), and must regenerate the same
    canonical ``AbsentAuthorized`` evidence as the sealed receipt.
    """

    preimage = _source_fact_preimage(authority["source_fact_preimage"])
    story = record.get("story_contract")
    if not isinstance(story, Mapping) or record.get("speaker_mode") != "uniform_host":
        raise QixiPostCorrectionPublicSurfaceError("source-fact uniform-host preimage drifts")
    if any(
        record.get(key) is not None
        for key in (
            "speaker_review_srt_path",
            "speaker_finalization_manifest_path",
            "speaker_finalization",
            "speaker_finalization_manifest_sha256",
        )
    ):
        raise QixiPostCorrectionPublicSurfaceError("uniform-host preimage claims speaker sidecars")
    prompt = story.get("clip_context_prompt")
    receipt = story.get("source_fact_review")
    if (
        not isinstance(prompt, str)
        or len(prompt.encode("utf-8")) != preimage["clip_context_prompt_bytes"]
        or _file_sha256_from_bytes(prompt.encode("utf-8"))
        != preimage["clip_context_prompt_sha256"]
        or not isinstance(receipt, Mapping)
        or receipt.get("receipt_sha256") != preimage["source_fact_receipt_sha256"]
    ):
        raise QixiPostCorrectionPublicSurfaceError("source-fact prompt or receipt preimage drifts")
    try:
        speaker_evidence = rebuild_package_speaker_evidence(
            root=paths["record"].parent,
            item={"subtitle_srt": paths["srt"].name},
            record=record,
            subtitle_path=paths["srt"],
        )
    except (OSError, ValueError) as exc:
        raise QixiPostCorrectionPublicSurfaceError("uniform-host speaker evidence cannot replay") from exc
    if speaker_evidence != preimage["speaker_evidence"]:
        raise QixiPostCorrectionPublicSurfaceError("uniform-host speaker evidence preimage drifts")


def _story_contract_rebuilder(
    record: Mapping[str, object], *, srt_path: Path, clip_context_path: Path
) -> Callable[[str], dict[str, object]]:
    prior = record.get("story_contract")
    if not isinstance(prior, Mapping):
        raise QixiPostCorrectionPublicSurfaceError("record StoryContract is missing")
    if record.get("clip_context_path") != str(clip_context_path):
        raise QixiPostCorrectionPublicSurfaceError("record clip context is missing")
    _require_regular(clip_context_path, label="record clip context")
    try:
        clip_context = json.loads(clip_context_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QixiPostCorrectionPublicSurfaceError("record clip context is unreadable") from exc
    transcript = "\n".join(cue.text for cue in _source_cues(srt_path))
    scorecard = record.get("selection_scorecard")
    relation = record.get("session_relation_authority")
    source_hashes = prior.get("source_media_sha256s")
    if not isinstance(source_hashes, list) or not all(isinstance(value, str) for value in source_hashes):
        raise QixiPostCorrectionPublicSurfaceError("StoryContract source media binding is invalid")
    boundary = prior.get("boundary_semantic_review")
    human_boundary = str(prior.get("human_boundary_authority") or "")
    cover_reference = prior.get("cover_reference_authority")

    def rebuild(hook: str) -> dict[str, object]:
        contract = build_story_contract(
            candidate_id=CANDIDATE_ID,
            selection_hook=hook,
            transcript_text=transcript,
            selection_scorecard=scorecard,
            session_relation_authority=relation,
            cover_reference_authority=cover_reference if isinstance(cover_reference, Mapping) else None,
            source_media_sha256s=list(source_hashes),
            clip_context=clip_context,
            recording_date=RECORDING_DATE,
            boundary_semantic_review=boundary if isinstance(boundary, Mapping) else None,
            human_boundary_authority=human_boundary,
        )
        contract["input_audits"] = [
            audit_story_artifact(hook, story_contract=contract, artifact_kind="selection_hook"),
            audit_story_artifact(transcript, story_contract=contract, artifact_kind="subtitle"),
        ]
        return contract

    return rebuild


def _default_source_fact_llm() -> LlmCall:
    return build_llm_call(
        LlmConfig(
            transport="command",
            command_template=(
                "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} "
                "'gpt-5.6-sol gpt-5.5 gpt-5.4' high"
            ),
            timeout_seconds=600.0,
        )
    )


def _write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    opened = os.fstat(fd)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        _unlink_owned_path(
            path,
            device=opened.st_dev,
            inode=opened.st_ino,
            label="create-only transaction artifact",
        )
        raise


def _atomic_replace(path: Path, payload: bytes) -> InstalledSnapshot:
    try:
        target_mode = stat.S_IMODE(os.lstat(path).st_mode)
    except FileNotFoundError:
        target_mode = 0o644
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.qixi-public-surface-", dir=path.parent)
    temporary = Path(temporary_name)
    opened = os.fstat(fd)
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view) :]
        os.fchmod(fd, target_mode)
        os.fsync(fd)
        staged = os.fstat(fd)
        os.close(fd)
        fd = -1
        temporary_info = os.lstat(temporary)
        if (
            not stat.S_ISREG(temporary_info.st_mode)
            or stat.S_ISLNK(temporary_info.st_mode)
            or (temporary_info.st_dev, temporary_info.st_ino) != (staged.st_dev, staged.st_ino)
            or stat.S_IMODE(temporary_info.st_mode) != target_mode
            or _file_sha256(temporary) != "sha256:" + hashlib.sha256(payload).hexdigest()
        ):
            raise QixiPostCorrectionPublicSurfaceError("atomic transaction temporary ownership drifted")
        os.replace(temporary, path)
        installed = os.lstat(path)
        if (
            not stat.S_ISREG(installed.st_mode)
            or stat.S_ISLNK(installed.st_mode)
            or (installed.st_dev, installed.st_ino) != (staged.st_dev, staged.st_ino)
            or _file_sha256(path) != "sha256:" + hashlib.sha256(payload).hexdigest()
        ):
            raise QixiPostCorrectionPublicSurfaceError("transaction installed inode ownership was lost")
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return InstalledSnapshot(
            path=path,
            device=installed.st_dev,
            inode=installed.st_ino,
            sha256=_file_sha256(path),
            mode=stat.S_IMODE(installed.st_mode),
        )
    finally:
        if fd >= 0:
            os.close(fd)
        _unlink_owned_path(
            temporary,
            device=opened.st_dev,
            inode=opened.st_ino,
            label="atomic transaction temporary",
        )


def _unlink_owned_path(path: Path, *, device: int, inode: int, label: str) -> None:
    """Unlink only the inode this transaction created; preserve replacements."""

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise QixiPostCorrectionPublicSurfaceError(f"{label} cannot be inspected") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or (info.st_dev, info.st_ino) != (device, inode)
    ):
        raise QixiPostCorrectionPublicSurfaceError(f"{label} ownership drifted")
    path.unlink()


def _current_regular_sha256(path: Path, *, label: str) -> str | None:
    """Return a regular-file hash, refusing a symlink or another existing type."""

    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise QixiPostCorrectionPublicSurfaceError(f"{label} cannot be inspected: {path}") from exc
    if not stat.S_ISREG(mode):
        raise QixiPostCorrectionPublicSurfaceError(f"{label} is not a regular file: {path}")
    _require_regular(path, label=label)
    return _file_sha256(path)


def _preimage_snapshot(
    path: Path, *, label: str
) -> tuple[bytes | None, str | None, int | None, int | None, int | None]:
    """Read one target only if its identity remains stable across the read."""

    try:
        first = os.lstat(path)
    except FileNotFoundError:
        return None, None, None, None, None
    except OSError as exc:
        raise QixiPostCorrectionPublicSurfaceError(f"{label} cannot be inspected: {path}") from exc
    if not stat.S_ISREG(first.st_mode) or stat.S_ISLNK(first.st_mode):
        raise QixiPostCorrectionPublicSurfaceError(f"{label} is not a regular file: {path}")
    _require_regular(path, label=label)
    try:
        payload = path.read_bytes()
        second = os.lstat(path)
    except OSError as exc:
        raise QixiPostCorrectionPublicSurfaceError(f"{label} cannot be read: {path}") from exc
    if (
        not stat.S_ISREG(second.st_mode)
        or stat.S_ISLNK(second.st_mode)
        or (first.st_dev, first.st_ino, first.st_mode) != (second.st_dev, second.st_ino, second.st_mode)
    ):
        raise QixiPostCorrectionPublicSurfaceError(f"{label} inode drifted during read: {path}")
    return (
        payload,
        "sha256:" + hashlib.sha256(payload).hexdigest(),
        stat.S_IMODE(second.st_mode),
        second.st_dev,
        second.st_ino,
    )


def _snapshot_installed(path: Path, *, sha256: str, mode: int) -> InstalledSnapshot:
    _require_regular(path, label="installed transaction target")
    info = os.lstat(path)
    if stat.S_IMODE(info.st_mode) != mode or _file_sha256(path) != sha256:
        raise QixiPostCorrectionPublicSurfaceError("transaction write verification failed")
    return InstalledSnapshot(path, info.st_dev, info.st_ino, sha256, mode)


def _require_safe_target_parent(path: Path) -> None:
    cursor = Path(path.anchor)
    for part in path.parts[1:-1]:
        cursor /= part
        try:
            mode = os.lstat(cursor).st_mode
        except OSError as exc:
            raise QixiPostCorrectionPublicSurfaceError(f"transaction target parent is unavailable: {path}") from exc
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise QixiPostCorrectionPublicSurfaceError(f"transaction target parent is unsafe: {path}")


@contextmanager
def _exclusive_runner_lock(runtime_root: Path):
    """Own the runner's day-state lock through provider staging and commit."""

    lock = runtime_root / "runner.lock"
    _require_safe_target_parent(lock)
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        opened = os.fstat(descriptor)
        observed = os.lstat(lock)
        if (
            not stat.S_ISREG(observed.st_mode)
            or stat.S_ISLNK(observed.st_mode)
            or (observed.st_dev, observed.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise QixiPostCorrectionPublicSurfaceError("runner.lock identity is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise QixiPostCorrectionPublicSurfaceError("runner.lock is busy") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path != root


def _journal_target_allowed(target: Path, *, authority: Mapping[str, object]) -> bool:
    artifacts = authority["artifacts"]
    assert isinstance(artifacts, Mapping)
    record = Path(str(_descriptor(artifacts["record"], label="artifact record")["path"]))
    delivery = Path(
        str(_descriptor(artifacts["delivery_record"], label="artifact delivery_record")["path"])
    )
    runtime_root = Path(str(authority["runtime_root"]))
    state = runtime_root / "state" / f"{RECORDING_DATE}.json"
    return _within(target, record.parent) or target in {delivery, state}


def _assert_prepare_preimages(inputs: RuntimeInputs, targets: Mapping[Path, bytes]) -> None:
    """Freeze known inputs and forbid replacing an unsealed existing sidecar."""

    expected = {
        path: str(descriptor["sha256"])
        for role, path in inputs.artifact_paths.items()
        for descriptor in [_descriptor(inputs.authority["artifacts"][role], label=f"artifact {role}")]
    }
    expected[inputs.state_path] = inputs.state_sha256
    for target in targets:
        _require_safe_target_parent(target)
        if not _journal_target_allowed(target, authority=inputs.authority):
            raise QixiPostCorrectionPublicSurfaceError(f"after-image target escapes candidate closure: {target}")
        current = _current_regular_sha256(target, label="after-image preimage")
        if target in expected:
            if current != expected[target]:
                raise QixiPostCorrectionPublicSurfaceError(f"sealed preimage drift before transaction: {target}")
        elif current is not None:
            raise QixiPostCorrectionPublicSurfaceError(
                f"after-image would replace an unsealed existing sidecar: {target}"
            )


def _remove_private_stage(stage_root: Path) -> None:
    """Remove only the mkdtemp-owned private tree; never follow a replacement link."""

    try:
        mode = os.lstat(stage_root).st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise QixiPostCorrectionPublicSurfaceError("private stage cleanup cannot inspect its root") from exc
    if not stat.S_ISDIR(mode):
        raise QixiPostCorrectionPublicSurfaceError("private stage root was replaced before cleanup")
    for child in stage_root.rglob("*"):
        if stat.S_ISLNK(os.lstat(child).st_mode):
            raise QixiPostCorrectionPublicSurfaceError("private stage acquired a symlink before cleanup")
    shutil.rmtree(stage_root)


def _build_after_image(
    inputs: RuntimeInputs,
    *,
    stage_root: Path,
    source_fact_llm_call: LlmCall,
    stage_publish: Callable[..., dict[str, object] | None] = _stage_publish_draft,
) -> tuple[dict[Path, bytes], dict[str, object]]:
    package_root = inputs.artifact_paths["record"].parent
    public_artifacts = _public_artifact_root(package_root, inputs.authority)
    stage_artifacts = stage_root / "artifacts"
    stage_artifacts.mkdir(mode=0o700)
    stage_media = stage_root / inputs.artifact_paths["main"].name
    stage_srt = stage_root / inputs.artifact_paths["srt"].name
    stage_ass = stage_root / inputs.artifact_paths["ass"].name
    for source, destination in ((inputs.artifact_paths["main"], stage_media), (inputs.artifact_paths["srt"], stage_srt), (inputs.artifact_paths["ass"], stage_ass)):
        _copy_sealed_stage_file(source, destination)
    stage_record = copy.deepcopy(inputs.record)
    stage_record.update({"media_path": str(stage_media), "subtitle_path": str(stage_srt), "subtitle_ass_path": str(stage_ass)})
    selection_hook = (inputs.record.get("story_contract") or {}).get("selection_hook")
    if not isinstance(selection_hook, str) or not selection_hook.strip():
        raise QixiPostCorrectionPublicSurfaceError("sealed StoryContract selection hook is missing")
    # Source-fact receipts bind the prompt embedded in StoryContract.  Rebuild
    # it before staging, rather than after the receipt was made, so the staged
    # review and final replay observe the same sealed SRT/context contract.
    stage_record["story_contract"] = _story_contract_rebuilder(
        inputs.record,
        srt_path=stage_srt,
        clip_context_path=inputs.artifact_paths["clip_context"],
    )(selection_hook)
    staged = stage_publish(
        stage_record,
        candidate_id=CANDIDATE_ID,
        title=MANUAL_TITLE,
        cues=_source_cues(stage_srt),
        run_ffmpeg=True,
        title_llm_call=None,
        art_direction_llm_call=None,
        source_fact_llm_call=source_fact_llm_call,
        story_contract_rebuilder=_story_contract_rebuilder(
            inputs.record, srt_path=stage_srt, clip_context_path=inputs.artifact_paths["clip_context"]
        ),
        selection_hook=selection_hook,
        private_artifact_root=stage_artifacts,
        private_publish_json_path=stage_artifacts / inputs.artifact_paths["publish"].name,
    )
    if not isinstance(staged, Mapping):
        raise QixiPostCorrectionPublicSurfaceError("canonical publish stage did not return a record")
    stage_publish_path = stage_artifacts / inputs.artifact_paths["publish"].name
    _require_regular(stage_publish_path, label="staged publish")
    try:
        staged_publish = json.loads(stage_publish_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QixiPostCorrectionPublicSurfaceError("staged publish is unreadable") from exc
    if not isinstance(staged_publish, Mapping):
        raise QixiPostCorrectionPublicSurfaceError("staged publish is malformed")
    staging = staged.get("publish_staging")
    if not isinstance(staging, Mapping) or staging.get("title") != PUBLIC_TITLE:
        raise QixiPostCorrectionPublicSurfaceError("canonical stage did not consume the exact manual title")
    if staging.get("upload_enabled") is not False or staged_publish.get("upload_enabled") is not False:
        raise QixiPostCorrectionPublicSurfaceError("canonical stage enabled upload")
    source_fact = staging.get("source_fact_review")
    if not isinstance(source_fact, Mapping) or source_fact.get("status") != "PASS":
        raise QixiPostCorrectionPublicSurfaceError("source-fact review did not pass; no title repair authority is implied")
    if staged_publish.get("video_path") != str(stage_media):
        raise QixiPostCorrectionPublicSurfaceError("staged publish media locator drifts")
    staged_publish_for_projection = copy.deepcopy(dict(staged_publish))
    staged_publish_for_projection["video_path"] = str(inputs.artifact_paths["main"])
    all_stage_files = {
        path.relative_to(stage_artifacts).as_posix()
        for path in stage_artifacts.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    stage_publish_relative = stage_publish_path.relative_to(stage_artifacts).as_posix()
    if stage_publish_relative not in all_stage_files:
        raise QixiPostCorrectionPublicSurfaceError("staged publish escaped the private artifact manifest")
    staged_for_projection, referenced = _prepare_stage_documents(
        staged,
        staged_publish_for_projection,
        stage_artifacts=stage_artifacts,
        stage_publish_path=stage_publish_path,
        public_publish_path=inputs.artifact_paths["publish"],
    )
    if not referenced.issubset(all_stage_files):
        raise QixiPostCorrectionPublicSurfaceError("staged public locator is not materialized")
    rewritten_record = _replace_stage_locators(
        staged_for_projection,
        stage_artifacts=stage_artifacts,
        public_artifacts=public_artifacts,
        materialized=referenced,
    )
    rewritten_publish = _replace_stage_locators(
        staged_publish_for_projection,
        stage_artifacts=stage_artifacts,
        public_artifacts=public_artifacts,
        materialized=referenced,
    )
    assert isinstance(rewritten_record, dict) and isinstance(rewritten_publish, dict)
    rewritten_record["media_path"] = str(inputs.artifact_paths["main"])
    rewritten_record["subtitle_path"] = str(inputs.artifact_paths["srt"])
    rewritten_record["subtitle_ass_path"] = str(inputs.artifact_paths["ass"])
    rewritten_record["human_text_correction_manifest_path"] = str(inputs.artifact_paths["correction"])
    _assert_no_stage_locator(rewritten_record, stage_root)
    _assert_no_stage_locator(rewritten_publish, stage_root)
    if rewritten_record.get("story_contract", {}).get("source_fact_review") != rewritten_record.get("publish_staging", {}).get("source_fact_review") or rewritten_publish.get("source_fact_review") != rewritten_record.get("publish_staging", {}).get("source_fact_review"):
        raise QixiPostCorrectionPublicSurfaceError("source-fact receipt is not identical across required mirrors")
    state = _project_state_pick(
        inputs.state,
        pick_index=inputs.state_pick_index,
        candidate_id=CANDIDATE_ID,
        record=rewritten_record,
        publish=rewritten_publish,
    )
    if not _state_diff_is_exact(
        inputs.state, state, pick_index=inputs.state_pick_index, candidate_id=CANDIDATE_ID
    ):
        raise QixiPostCorrectionPublicSurfaceError("state after-image exceeds the sealed candidate pick")
    targets = _materialized_public_targets(
        stage_artifacts=stage_artifacts,
        namespace=public_artifacts,
        relatives=referenced,
        require_regular=lambda path: _require_regular(path, label="referenced staged artifact"),
    )
    targets[inputs.artifact_paths["record"]] = _json_bytes(rewritten_record)
    targets[inputs.artifact_paths["delivery_record"]] = _json_bytes(rewritten_record)
    targets[inputs.artifact_paths["publish"]] = _json_bytes(rewritten_publish)
    targets[inputs.state_path] = _json_bytes(state)
    for immutable in ("main", "srt", "ass", "burn", "correction"):
        if inputs.artifact_paths[immutable] in targets:
            raise QixiPostCorrectionPublicSurfaceError(f"after-image attempts to rewrite immutable {immutable}")
    metadata = {
        "title": PUBLIC_TITLE,
        "source_fact_receipt_sha256": _canonical_sha256(rewritten_record["publish_staging"]["source_fact_review"]),
        "cover_sha256": rewritten_record["artifact_hashes"].get("cover_sha256"),
        "target_count": len(targets),
    }
    return targets, metadata


def _validate_after_image(
    *, authority: Mapping[str, object], before: Mapping[Path, bytes | None], after: Mapping[Path, bytes]
) -> set[Path]:
    """Replay the only permissible public closure from journal bytes."""

    artifacts = authority["artifacts"]
    assert isinstance(artifacts, Mapping)
    paths = {
        role: Path(str(_descriptor(value, label=f"artifact {role}")["path"]))
        for role, value in artifacts.items()
    }
    state_descriptor = _state_descriptor(authority["state_file"])
    state_path = Path(str(state_descriptor["path"]))
    fixed = {paths["record"], paths["delivery_record"], paths["publish"], state_path}
    if not fixed.issubset(after) or any(path in after for path in (paths["main"], paths["srt"], paths["ass"], paths["burn"], paths["correction"])):
        raise QixiPostCorrectionPublicSurfaceError("journal after-image target roles drift")
    try:
        record = json.loads(after[paths["record"]].decode("utf-8"))
        delivery = json.loads(after[paths["delivery_record"]].decode("utf-8"))
        publish = json.loads(after[paths["publish"]].decode("utf-8"))
        before_record = json.loads((before[paths["record"]] or b"").decode("utf-8"))
        before_state = json.loads((before[state_path] or b"").decode("utf-8"))
        state = json.loads(after[state_path].decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise QixiPostCorrectionPublicSurfaceError("journal after-image JSON is invalid") from exc
    if not all(isinstance(value, dict) for value in (record, delivery, publish, before_record, before_state, state)):
        raise QixiPostCorrectionPublicSurfaceError("journal after-image document type drifts")
    if (
        record != delivery
        or record.get("media_path") != str(paths["main"])
        or record.get("subtitle_path") != str(paths["srt"])
        or record.get("subtitle_ass_path") != str(paths["ass"])
        or record.get("human_text_correction_manifest_path") != str(paths["correction"])
        or record.get("human_text_correction_manifest_sha256") != artifacts["correction"]["sha256"]
    ):
        raise QixiPostCorrectionPublicSurfaceError("journal record/delivery media closure drifts")
    hashes = record.get("artifact_hashes")
    if not isinstance(hashes, Mapping) or any(
        hashes.get(key) != artifacts[role]["sha256"]
        for key, role in (
            ("video_sha256", "main"),
            ("subtitle_sha256", "srt"),
            ("ass_sha256", "ass"),
            ("burned_video_sha256", "burn"),
        )
    ):
        raise QixiPostCorrectionPublicSurfaceError("journal final media binding drifts")
    staging = record.get("publish_staging")
    story = record.get("story_contract")
    if not isinstance(staging, Mapping) or not isinstance(story, Mapping):
        raise QixiPostCorrectionPublicSurfaceError("journal public staging closure is missing")
    source_fact = staging.get("source_fact_review")
    generation = staging.get("cover_generation")
    if (
        staging.get("title") != PUBLIC_TITLE
        or publish.get("title") != PUBLIC_TITLE
        or staging.get("upload_enabled") is not False
        or not isinstance(source_fact, Mapping)
        or source_fact.get("status") != "PASS"
        or story.get("source_fact_review") != source_fact
        or publish.get("source_fact_review") != source_fact
        or publish.get("cover_generation") != generation
        or publish.get("upload_enabled") is not False
    ):
        raise QixiPostCorrectionPublicSurfaceError("journal source-fact/title/cover mirrors drift")
    _validate_manual_title_projection(
        staging, publish, source_fact=source_fact, public_title=PUBLIC_TITLE
    )
    before_hashes = before_record.get("artifact_hashes")
    if not isinstance(before_hashes, Mapping):
        raise QixiPostCorrectionPublicSurfaceError("journal predecessor artifact hashes are missing")
    for key in set(before_hashes) | set(hashes):
        if key != "cover_sha256" and before_hashes.get(key) != hashes.get(key):
            raise QixiPostCorrectionPublicSurfaceError("journal immutable artifact hash drifts")
    # Rebuild the two semantic public surfaces from sealed inputs.  No other
    # record column is an allowed journal mutation.
    for key in set(before_record) | set(record):
        if key not in {"story_contract", "publish_staging", "artifact_hashes"} and before_record.get(key) != record.get(key):
            raise QixiPostCorrectionPublicSurfaceError("journal record closure mutates an unrelated field")
    hook = story.get("selection_hook") or (before_record.get("story_contract") or {}).get("selection_hook")
    try:
        expected_story = _story_contract_rebuilder(
            before_record, srt_path=paths["srt"], clip_context_path=paths["clip_context"]
        )(hook) if isinstance(hook, str) else None
    except Exception as exc:
        raise QixiPostCorrectionPublicSurfaceError("journal StoryContract input cannot replay") from exc
    if isinstance(expected_story, dict):
        expected_story["source_fact_review"] = source_fact
    if not isinstance(hook, str) or story != expected_story:
        raise QixiPostCorrectionPublicSurfaceError("journal StoryContract replay drifts")
    package_root = paths["record"].parent
    try:
        speaker_evidence = rebuild_package_speaker_evidence(
            root=package_root,
            item={"subtitle_srt": paths["srt"].name},
            record=before_record,
            subtitle_path=paths["srt"],
        )
    except (OSError, ValueError) as exc:
        raise QixiPostCorrectionPublicSurfaceError("journal uniform-host speaker evidence replay drifts") from exc
    if not validate_source_fact_review(
        source_fact,
        selection_hook=hook,
        title=PUBLIC_TITLE,
        final_transcript="\n".join(cue.text for cue in _source_cues(paths["srt"])),
        clip_context_prompt=str(story.get("clip_context_prompt") or ""),
        selection_scorecard=story.get("selection_scorecard"),
        candidate_id=CANDIDATE_ID,
        final_reviewed_srt_path=paths["srt"],
        speaker_evidence=speaker_evidence,
        story_contract=story,
    ):
        raise QixiPostCorrectionPublicSurfaceError("journal source-fact receipt replay drifts")
    _validate_replayed_cover(generation, story=story, package_root=paths["record"].parent, after=after)
    cover_path = generation.get("final_cover")
    cover_sha = generation.get("final_cover_sha256")
    if (
        not isinstance(cover_path, str)
        or staging.get("cover_path") != cover_path
        or publish.get("cover_path") != cover_path
        or hashes.get("cover_sha256") != cover_sha
        or Path(cover_path) not in after
    ):
        raise QixiPostCorrectionPublicSurfaceError("journal cover projection drifts")
    allowed_publish_changes = {
        "title",
        "title_source",
        "title_authority_status",
        "recovery_publication_authority",
        "title_authority_error",
        "title_policy_violations",
        "important_content_ips",
        "title_story_audit",
        "entity_projection_audit",
        "cover_entity_projection_audit",
        "source_fact_review",
        "manual_title_repair_authority_consumption",
        "manual_title_keep_authority_consumption",
        "public_text_surface_authority_consumption",
        "video_path",
        "cover_text",
        "cover_generation",
        "cover_path",
        "cover_status",
        "reason_codes",
        "artifact_hashes",
        "upload_enabled",
        "source_fact_scorecard_rescore_provenance",
    }
    before_publish = json.loads((before[paths["publish"]] or b"").decode("utf-8"))
    if not isinstance(before_publish, dict) or any(
        before_publish.get(key) != publish.get(key)
        for key in set(before_publish) | set(publish)
        if key not in allowed_publish_changes
    ):
        raise QixiPostCorrectionPublicSurfaceError("journal publish closure mutates an unrelated field")
    before_publish_hashes = before_publish.get("artifact_hashes")
    publish_hashes = publish.get("artifact_hashes")
    if (before_publish_hashes is None) != (publish_hashes is None):
        raise QixiPostCorrectionPublicSurfaceError("journal publish artifact hash projection drifts")
    if isinstance(before_publish_hashes, Mapping) and isinstance(publish_hashes, Mapping):
        for key in set(before_publish_hashes) | set(publish_hashes):
            if key != "cover_sha256" and before_publish_hashes.get(key) != publish_hashes.get(key):
                raise QixiPostCorrectionPublicSurfaceError("journal publish immutable artifact hash drifts")
        if publish_hashes.get("cover_sha256") != cover_sha:
            raise QixiPostCorrectionPublicSurfaceError("journal publish cover hash projection drifts")
    before_picks = before_state.get("picks")
    if not isinstance(before_picks, list):
        raise QixiPostCorrectionPublicSurfaceError("journal state preimage is invalid")
    indices = [index for index, pick in enumerate(before_picks) if isinstance(pick, Mapping) and pick.get("candidate_id") == CANDIDATE_ID]
    if (
        len(indices) != 1
        or not _state_diff_is_exact(
            before_state, state, pick_index=indices[0], candidate_id=CANDIDATE_ID
        )
        or state
        != _project_state_pick(
            before_state,
            pick_index=indices[0],
            candidate_id=CANDIDATE_ID,
            record=record,
            publish=publish,
        )
    ):
        raise QixiPostCorrectionPublicSurfaceError("journal state closure drifts")
    package_root = paths["record"].parent
    referenced = _package_cover_paths(generation, package_root=package_root)
    referenced.update(_package_cover_paths(staging.get("cover_path"), package_root=package_root))
    referenced.update(_package_cover_paths(publish.get("cover_path"), package_root=package_root))
    extras = set(after) - fixed
    if not extras.issubset(referenced) or any(not _within(path, package_root) for path in extras):
        raise QixiPostCorrectionPublicSurfaceError("journal cover sidecar inventory drifts")
    return fixed | referenced


def _journal_root(inputs: RuntimeInputs) -> Path:
    return inputs.artifact_paths["record"].parent.parent / "qixi_post_correction_public_surface" / inputs.authority["authority_sha256"][7:23]


def _journal_root_from_authority(authority: Mapping[str, object]) -> Path:
    artifacts = authority["artifacts"]
    assert isinstance(artifacts, Mapping)
    record = _descriptor(artifacts["record"], label="artifact record")
    return Path(str(record["path"])).parent.parent / "qixi_post_correction_public_surface" / str(authority["authority_sha256"])[7:23]


def _journal_sha256(journal: Mapping[str, object]) -> str:
    unsigned = dict(journal)
    unsigned.pop("journal_sha256", None)
    return _canonical_sha256(unsigned)


def _private_journal_root(root: Path, *, create: bool) -> None:
    cursor = Path(root.anchor)
    for part in root.parts[1:-1]:
        cursor /= part
        if not os.path.lexists(cursor):
            break
        mode = os.lstat(cursor).st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise QixiPostCorrectionPublicSurfaceError("post-correction journal parent is unsafe")
    if os.path.lexists(root):
        mode = os.lstat(root).st_mode
        if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode) or stat.S_IMODE(mode) != 0o700:
            raise QixiPostCorrectionPublicSurfaceError("post-correction journal root is unsafe")
    elif create:
        root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_mode = os.lstat(root.parent).st_mode
        if stat.S_ISLNK(parent_mode) or not stat.S_ISDIR(parent_mode):
            raise QixiPostCorrectionPublicSurfaceError("post-correction journal parent is unsafe")
        try:
            root.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise QixiPostCorrectionPublicSurfaceError("post-correction journal root raced creation") from exc


def _entry_role(target: Path, *, inputs: RuntimeInputs) -> str:
    fixed = {
        inputs.artifact_paths["record"]: "record",
        inputs.artifact_paths["delivery_record"]: "delivery_record",
        inputs.artifact_paths["publish"]: "publish",
        inputs.state_path: "state",
    }
    return fixed.get(target, "cover_artifact")


def _write_prepared_journal(root: Path, *, inputs: RuntimeInputs, targets: Mapping[Path, bytes], metadata: Mapping[str, object]) -> dict[str, object]:
    _assert_prepare_preimages(inputs, targets)
    sealed = {
        path: str(descriptor["sha256"])
        for role, path in inputs.artifact_paths.items()
        for descriptor in [_descriptor(inputs.authority["artifacts"][role], label=f"artifact {role}")]
    }
    sealed[inputs.state_path] = inputs.state_sha256
    before: dict[Path, bytes | None] = {}
    entries: list[dict[str, object]] = []
    for index, (target, payload) in enumerate(sorted(targets.items(), key=lambda row: str(row[0]))):
        raw, current, mode, device, inode = _preimage_snapshot(target, label="journal preimage")
        if (target in sealed and current != sealed[target]) or (target not in sealed and current is not None):
            raise QixiPostCorrectionPublicSurfaceError("journal preimage drifted before freeze")
        before[target] = raw
        entries.append(
            {
                "role": _entry_role(target, inputs=inputs),
                "target": str(target),
                "before_bytes_b64": _b64(raw),
                "before_sha256": current,
                "before_mode": mode,
                "before_device": device,
                "before_inode": inode,
                "after_bytes_b64": _b64(payload),
                "after_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "after_mode": mode if mode is not None else 0o644,
                # A journal is not a backup.  The b64 preimage is retained for
                # semantic replay, while this name owns a durable, create-only
                # backup materialized before this target may be replaced.
                "backup_name": f"{index:03d}-{_entry_role(target, inputs=inputs)}.before",
                "staged_name": f".{target.name}.qixi-public-surface-{index:03d}.tmp",
                "staged_device": None,
                "staged_inode": None,
                "phase": "PREPARED",
                "installed_device": None,
                "installed_inode": None,
            }
        )
    _validate_after_image(authority=inputs.authority, before=before, after=dict(targets))
    journal: dict[str, object] = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "status": "PREPARED",
        "candidate_id": CANDIDATE_ID,
        "recording_date": RECORDING_DATE,
        "upload_enabled": False,
        "authority_sha256": inputs.authority["authority_sha256"],
        "entries": entries,
        "metadata": dict(metadata),
        "metadata_sha256": _canonical_sha256(metadata),
        "journal_sha256": "",
    }
    journal["journal_sha256"] = _journal_sha256(journal)
    _private_journal_root(root, create=True)
    _write_new(root / "journal.json", _json_bytes(journal))
    return journal


def _write_journal(root: Path, journal: dict[str, object]) -> None:
    """Durably checkpoint a phase transition before touching the next target."""

    journal["journal_sha256"] = _journal_sha256(journal)
    _atomic_replace(root / "journal.json", _json_bytes(journal))


def _backup_root(root: Path, *, create: bool) -> Path:
    backups = root / "backups"
    if os.path.lexists(backups):
        mode = os.lstat(backups).st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode) or stat.S_IMODE(mode) != 0o700:
            raise QixiPostCorrectionPublicSurfaceError("post-correction backup root is unsafe")
    elif create:
        try:
            backups.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise QixiPostCorrectionPublicSurfaceError("post-correction backup root raced creation") from exc
    return backups


def _entry_backup_path(root: Path, entry: Mapping[str, object]) -> Path:
    name = entry.get("backup_name")
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise QixiPostCorrectionPublicSurfaceError("post-correction backup name drifts")
    return _backup_root(root, create=False) / name


def _verify_backup(root: Path, entry: Mapping[str, object]) -> bytes | None:
    before = _unb64(entry["before_bytes_b64"], label="journal before")
    if before is None:
        # An absent sidecar is deliberately represented by no backup file.
        if os.path.lexists(_entry_backup_path(root, entry)):
            raise QixiPostCorrectionPublicSurfaceError("absent target acquired a backup residue")
        return None
    backup = _entry_backup_path(root, entry)
    _require_regular(backup, label="post-correction target backup")
    if backup.read_bytes() != before or _file_sha256(backup) != entry["before_sha256"]:
        raise QixiPostCorrectionPublicSurfaceError("post-correction target backup drifts")
    return before


def _ensure_backup(root: Path, entry: Mapping[str, object]) -> None:
    _verify_entry_preimage(entry, label="target backup")
    before = _unb64(entry["before_bytes_b64"], label="journal before")
    backup = _entry_backup_path(root, entry)
    if before is None:
        if os.path.lexists(backup):
            raise QixiPostCorrectionPublicSurfaceError("absent target acquired a backup residue")
        return
    if os.path.lexists(backup):
        _verify_backup(root, entry)
        return
    _backup_root(root, create=True)
    _write_new(backup, before)
    _verify_backup(root, entry)


def _entry_staged_path(entry: Mapping[str, object]) -> Path:
    target = Path(str(entry["target"]))
    name = entry.get("staged_name")
    if not isinstance(name, str) or Path(name).name != name or not name.startswith("."):
        raise QixiPostCorrectionPublicSurfaceError("post-correction staged name drifts")
    return target.parent / name


def _stage_owned_inode(entry: Mapping[str, object]) -> InstalledSnapshot:
    path = _entry_staged_path(entry)
    device, inode = entry.get("staged_device"), entry.get("staged_inode")
    if isinstance(device, bool) or not isinstance(device, int) or isinstance(inode, bool) or not isinstance(inode, int):
        raise QixiPostCorrectionPublicSurfaceError("post-correction staged inode evidence drifts")
    return InstalledSnapshot(path, device, inode, str(entry["after_sha256"]), int(entry["after_mode"]))


def _verify_staged_inode(entry: Mapping[str, object]) -> None:
    snapshot = _stage_owned_inode(entry)
    _require_regular(snapshot.path, label="staged transaction target")
    info = os.lstat(snapshot.path)
    if (info.st_dev, info.st_ino) != (snapshot.device, snapshot.inode) or stat.S_IMODE(info.st_mode) != snapshot.mode or _file_sha256(snapshot.path) != snapshot.sha256:
        raise QixiPostCorrectionPublicSurfaceError("staged transaction inode ownership drifted")


def _create_staged_inode(entry: Mapping[str, object]) -> InstalledSnapshot:
    path = _entry_staged_path(entry)
    _require_safe_target_parent(path)
    payload = _unb64(entry["after_bytes_b64"], label="journal after")
    assert payload is not None
    mode, sha256 = int(entry["after_mode"]), str(entry["after_sha256"])
    device, inode = _create_staged_file(path, payload=payload, sha256=sha256, mode=mode)
    return InstalledSnapshot(path, device, inode, sha256, mode)


def _verify_staged_snapshot(snapshot: InstalledSnapshot) -> None:
    _require_regular(snapshot.path, label="staged transaction target")
    info = os.lstat(snapshot.path)
    if (info.st_dev, info.st_ino) != (snapshot.device, snapshot.inode) or stat.S_IMODE(info.st_mode) != snapshot.mode or _file_sha256(snapshot.path) != snapshot.sha256:
        raise QixiPostCorrectionPublicSurfaceError("staged transaction write verification failed")


def _installed_snapshot_from_entry(entry: Mapping[str, object]) -> InstalledSnapshot:
    target = Path(str(entry["target"]))
    device, inode = entry.get("installed_device"), entry.get("installed_inode")
    if isinstance(device, bool) or not isinstance(device, int) or isinstance(inode, bool) or not isinstance(inode, int):
        raise QixiPostCorrectionPublicSurfaceError("post-correction installed inode evidence drifts")
    return InstalledSnapshot(target, device, inode, str(entry["after_sha256"]), int(entry["after_mode"]))


def _verify_installed(entry: Mapping[str, object]) -> None:
    snapshot = _installed_snapshot_from_entry(entry)
    _require_regular(snapshot.path, label="installed transaction target")
    info = os.lstat(snapshot.path)
    if (
        (info.st_dev, info.st_ino) != (snapshot.device, snapshot.inode)
        or stat.S_IMODE(info.st_mode) != snapshot.mode
        or _file_sha256(snapshot.path) != snapshot.sha256
    ):
        raise QixiPostCorrectionPublicSurfaceError("transaction installed target ownership drifted")


def _entry_role_from_authority(target: Path, *, authority: Mapping[str, object]) -> str:
    artifacts = authority["artifacts"]
    assert isinstance(artifacts, Mapping)
    fixed = {
        Path(str(_descriptor(artifacts["record"], label="artifact record")["path"])): "record",
        Path(str(_descriptor(artifacts["delivery_record"], label="artifact delivery_record")["path"])): "delivery_record",
        Path(str(_descriptor(artifacts["publish"], label="artifact publish")["path"])): "publish",
        Path(str(_state_descriptor(authority["state_file"])["path"])): "state",
    }
    return fixed.get(target, "cover_artifact")


def _validate_sealed_before_entry(
    entry: Mapping[str, object], *, authority: Mapping[str, object]
) -> None:
    """Bind every mutable preimage to the deployment-sealed authority.

    A self-rehashed journal is only a recovery record, never an authority for
    choosing different predecessor bytes.  Cover outputs are create-only for
    this lane, so their sole valid predecessor is absence.
    """

    role = str(entry["role"])
    before = _unb64(entry["before_bytes_b64"], label="journal before")
    if role == "cover_artifact":
        if (
            before is not None
            or entry["before_sha256"] is not None
            or entry["before_mode"] is not None
            or entry["before_device"] is not None
            or entry["before_inode"] is not None
        ):
            raise QixiPostCorrectionPublicSurfaceError("journal cover predecessor is not create-only")
        return
    descriptor = _sealed_before(authority)[role]
    if (
        before is None
        or entry["before_sha256"] != descriptor["sha256"]
        or entry["before_mode"] != descriptor["mode"]
        or len(before) != descriptor["bytes"]
        or "sha256:" + hashlib.sha256(before).hexdigest() != descriptor["sha256"]
    ):
        raise QixiPostCorrectionPublicSurfaceError("journal sealed predecessor bytes drift")


def _verify_entry_preimage(entry: Mapping[str, object], *, label: str) -> None:
    """Reject same-byte target replacement after the journal froze its inode."""

    target = Path(str(entry["target"]))
    payload, digest, mode, device, inode = _preimage_snapshot(target, label=label)
    expected = _unb64(entry["before_bytes_b64"], label="journal before")
    if expected is None:
        if payload is not None:
            raise QixiPostCorrectionPublicSurfaceError(f"foreign target appeared before {label}")
        return
    if (
        payload != expected
        or digest != entry["before_sha256"]
        or mode != entry["before_mode"]
        or device != entry["before_device"]
        or inode != entry["before_inode"]
    ):
        raise QixiPostCorrectionPublicSurfaceError(f"foreign target inode drift before {label}")


def _checkpoint_entry(root: Path, journal: dict[str, object], index: int, **changes: object) -> None:
    entries = journal["entries"]
    assert isinstance(entries, list) and isinstance(entries[index], Mapping)
    updated = dict(entries[index])
    updated.update(changes)
    entries[index] = updated
    _write_journal(root, journal)


def _validate_backup_inventory(root: Path, journal: Mapping[str, object]) -> None:
    """Allow only this journal's owned, regular backup files.

    A backup may exist while its phase is still PREPARED: that is the crash
    window between create-only backup materialization and its journal
    checkpoint.  It is still safe because its exact bytes are replayed before
    the target can be installed.
    """

    entries = journal["entries"]
    assert isinstance(entries, list)
    expected = {
        str(entry["backup_name"])
        for entry in entries
        if _unb64(entry["before_bytes_b64"], label="journal before") is not None
    }
    backups = root / "backups"
    if not os.path.lexists(backups):
        if journal["status"] != "COMMITTED" and any(
            entry["phase"] in {"BACKED_UP", "INSTALLING", "INSTALLED"}
            and _unb64(entry["before_bytes_b64"], label="journal before") is not None
            for entry in entries
        ):
            raise QixiPostCorrectionPublicSurfaceError("checkpointed transaction backup is missing")
        return
    _backup_root(root, create=False)
    actual = {child.name for child in backups.iterdir()}
    if not actual.issubset(expected):
        raise QixiPostCorrectionPublicSurfaceError("post-correction backup inventory drifts")
    for entry in entries:
        before = _unb64(entry["before_bytes_b64"], label="journal before")
        if before is not None and str(entry["backup_name"]) in actual:
            _verify_backup(root, entry)
        if journal["status"] != "COMMITTED" and entry["phase"] in {"BACKED_UP", "INSTALLING", "INSTALLED"} and before is not None:
            _verify_backup(root, entry)


def _cleanup_committed_backups(root: Path, journal: Mapping[str, object]) -> bool:
    """Remove only validated backup inodes owned by an already committed journal."""

    backups = root / "backups"
    if not os.path.lexists(backups):
        return False
    _validate_backup_inventory(root, journal)
    entries = journal["entries"]
    assert isinstance(entries, list)
    for entry in entries:
        if _unb64(entry["before_bytes_b64"], label="journal before") is None:
            continue
        backup = _entry_backup_path(root, entry)
        # A prior committed cleanup may have removed earlier entries before a
        # crash.  Missing is therefore a valid committed-residue state; an
        # existing inode must still be this entry's exact backup before it is
        # removed.
        if not os.path.lexists(backup):
            continue
        _verify_backup(root, entry)
        info = os.lstat(backup)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise QixiPostCorrectionPublicSurfaceError("post-correction backup became unsafe during cleanup")
        backup.unlink()
    backups.rmdir()
    directory_fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return False


def _cleanup_committed_backups_result(root: Path, journal: Mapping[str, object]) -> str:
    """Best-effort residue cleanup after the durable commit point.

    Once all three public documents and every cover target have been replayed,
    the transaction is irrevocably committed.  A later backup cleanup problem
    is actionable residue, not a reason to report a pre-commit failure or to
    roll back proven installed targets.
    """

    try:
        _cleanup_committed_backups(root, journal)
    except (OSError, QixiPostCorrectionPublicSurfaceError):
        return "APPLIED_WITH_CLEANUP_RESIDUE"
    return "APPLIED"


def _validate_journal(journal: object, *, authority: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(journal, dict) or set(journal) != _JOURNAL_KEYS:
        raise QixiPostCorrectionPublicSurfaceError("post-correction journal schema drifts")
    if (
        journal.get("schema_version") != JOURNAL_SCHEMA_VERSION
        or journal.get("candidate_id") != CANDIDATE_ID
        or journal.get("recording_date") != RECORDING_DATE
        or journal.get("upload_enabled") is not False
        or journal.get("authority_sha256") != authority["authority_sha256"]
        or journal.get("status") not in {"PREPARED", "COMMITTED"}
        or journal.get("journal_sha256") != _journal_sha256(journal)
        or journal.get("metadata_sha256") != _canonical_sha256(journal.get("metadata"))
    ):
        raise QixiPostCorrectionPublicSurfaceError("post-correction journal binding drifts")
    entries = journal.get("entries")
    if not isinstance(entries, list) or not entries:
        raise QixiPostCorrectionPublicSurfaceError("post-correction journal entries are invalid")
    before: dict[Path, bytes | None] = {}
    after: dict[Path, bytes] = {}
    roles: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != _JOURNAL_ENTRY_KEYS:
            raise QixiPostCorrectionPublicSurfaceError("post-correction journal entry schema drifts")
        target = Path(str(entry["target"]))
        _require_safe_target_parent(target)
        role = entry["role"]
        backup_name = entry["backup_name"]
        phase = entry["phase"]
        if (
            not isinstance(role, str)
            or role != _entry_role_from_authority(target, authority=authority)
            or target in after
            or not _journal_target_allowed(target, authority=authority)
            or not isinstance(backup_name, str)
            or not backup_name
            or Path(backup_name).name != backup_name
            or not isinstance(entry["staged_name"], str)
            or Path(str(entry["staged_name"])).name != entry["staged_name"]
            or phase not in _ENTRY_PHASES
        ):
            raise QixiPostCorrectionPublicSurfaceError("post-correction journal entry identity drifts")
        old, new = _unb64(entry["before_bytes_b64"], label="journal before"), _unb64(entry["after_bytes_b64"], label="journal after")
        if new is None or (old is None) != (entry["before_sha256"] is None):
            raise QixiPostCorrectionPublicSurfaceError("post-correction journal entry preimage drifts")
        if (
            (old is not None and entry["before_sha256"] != "sha256:" + hashlib.sha256(old).hexdigest())
            or entry["after_sha256"] != "sha256:" + hashlib.sha256(new).hexdigest()
            or entry["before_mode"] is not None and (isinstance(entry["before_mode"], bool) or not isinstance(entry["before_mode"], int) or not 0 <= entry["before_mode"] <= 0o777)
            or isinstance(entry["after_mode"], bool) or not isinstance(entry["after_mode"], int) or not 0 <= entry["after_mode"] <= 0o777
        ):
            raise QixiPostCorrectionPublicSurfaceError("post-correction journal entry bytes drift")
        before_device, before_inode = entry["before_device"], entry["before_inode"]
        if old is None:
            if before_device is not None or before_inode is not None:
                raise QixiPostCorrectionPublicSurfaceError("post-correction absent preimage inode drifts")
        elif (
            isinstance(before_device, bool)
            or not isinstance(before_device, int)
            or isinstance(before_inode, bool)
            or not isinstance(before_inode, int)
        ):
            raise QixiPostCorrectionPublicSurfaceError("post-correction preimage inode evidence drifts")
        _validate_sealed_before_entry(entry, authority=authority)
        device, inode = entry["installed_device"], entry["installed_inode"]
        staged_device, staged_inode = entry["staged_device"], entry["staged_inode"]
        if phase in {"INSTALLING", "INSTALLED"}:
            _stage_owned_inode(entry)
        elif staged_device is not None or staged_inode is not None:
            raise QixiPostCorrectionPublicSurfaceError("post-correction unstaged inode evidence drifts")
        if phase == "INSTALLED":
            _installed_snapshot_from_entry(entry)
        elif device is not None or inode is not None:
            raise QixiPostCorrectionPublicSurfaceError("post-correction journal uninstalled inode evidence drifts")
        before[target], after[target], roles = old, new, roles | {role}
    fixed_roles = {"record", "delivery_record", "publish", "state"}
    if not fixed_roles.issubset(roles) or not roles.issubset(fixed_roles | {"cover_artifact"}):
        raise QixiPostCorrectionPublicSurfaceError("post-correction journal roles drift")
    allowed = _validate_after_image(authority=authority, before=before, after=after)
    if set(after) != allowed:
        raise QixiPostCorrectionPublicSurfaceError("post-correction journal target inventory drifts")
    return journal


def _load_journal(root: Path, *, authority: Mapping[str, object]) -> dict[str, object] | None:
    if not os.path.lexists(root):
        return None
    _private_journal_root(root, create=False)
    path = root / "journal.json"
    _require_regular(path, label="post-correction journal")
    try:
        journal = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QixiPostCorrectionPublicSurfaceError("post-correction journal is unreadable") from exc
    normalized = _validate_journal(journal, authority=authority)
    expected_inventory = {"journal.json", "final-receipt.json", "backups"}
    actual_inventory = {entry.name for entry in root.iterdir()}
    if not actual_inventory.issubset(expected_inventory):
        raise QixiPostCorrectionPublicSurfaceError("post-correction journal inventory drifts")
    _validate_backup_inventory(root, normalized)
    receipt_path = root / "final-receipt.json"
    if os.path.lexists(receipt_path):
        if normalized["status"] != "COMMITTED":
            raise QixiPostCorrectionPublicSurfaceError("post-correction final receipt precedes commit")
        _require_regular(receipt_path, label="post-correction final receipt")
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise QixiPostCorrectionPublicSurfaceError("post-correction final receipt is unreadable") from exc
        if receipt != _final_receipt(normalized):
            raise QixiPostCorrectionPublicSurfaceError("post-correction final receipt drifts")
    return normalized


def _postcommit_replay(journal: Mapping[str, object], *, authority: Mapping[str, object]) -> None:
    entries = journal["entries"]
    assert isinstance(entries, list)
    before: dict[Path, bytes | None] = {}
    after: dict[Path, bytes] = {}
    for entry in entries:
        assert isinstance(entry, Mapping)
        target = Path(str(entry["target"]))
        expected = _unb64(entry["after_bytes_b64"], label="journal after")
        assert expected is not None
        _require_regular(target, label="postcommit target")
        observed = target.read_bytes()
        if observed != expected:
            raise QixiPostCorrectionPublicSurfaceError("postcommit target bytes drift")
        if entry["phase"] != "INSTALLED":
            raise QixiPostCorrectionPublicSurfaceError("postcommit target lacks installed inode checkpoint")
        _verify_installed(entry)
        before[target], after[target] = _unb64(entry["before_bytes_b64"], label="journal before"), observed
    _validate_after_image(authority=authority, before=before, after=after)


def _final_receipt(journal: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": "qixi-post-correction-public-surface-final-receipt.v1",
        "status": "COMMITTED",
        "candidate_id": CANDIDATE_ID,
        "recording_date": RECORDING_DATE,
        "authority_sha256": journal["authority_sha256"],
        "journal_sha256": journal["journal_sha256"],
        "entries_sha256": _canonical_sha256(journal["entries"]),
    }
    result["receipt_sha256"] = _canonical_sha256(result)
    return result


def _write_final_receipt(root: Path, journal: Mapping[str, object]) -> None:
    receipt = root / "final-receipt.json"
    expected = _json_bytes(_final_receipt(journal))
    if os.path.lexists(receipt):
        _require_regular(receipt, label="post-correction final receipt")
        if receipt.read_bytes() != expected:
            raise QixiPostCorrectionPublicSurfaceError("post-correction final receipt drifts")
        return
    _write_new(receipt, expected)
    _require_regular(receipt, label="post-correction final receipt")
    if receipt.read_bytes() != expected:
        raise QixiPostCorrectionPublicSurfaceError("post-correction final receipt write verification failed")


def _rollback_owned(root: Path, journal: dict[str, object], *, authority: Mapping[str, object]) -> None:
    """Undo only target inodes proved by the durable INSTALLED checkpoints."""

    entries = journal["entries"]
    assert isinstance(entries, list)
    for index in reversed(range(len(entries))):
        entry = entries[index]
        assert isinstance(entry, Mapping)
        if entry["phase"] == "INSTALLING":
            _verify_staged_inode(entry)
            staged = _stage_owned_inode(entry)
            _clear_staged_file_owner(
                staged.path,
                device=staged.device,
                inode=staged.inode,
                sha256=staged.sha256,
                mode=staged.mode,
            )
            _entry_staged_path(entry).unlink()
            _checkpoint_entry(root, journal, index, phase="BACKED_UP", staged_device=None, staged_inode=None)
            entry = journal["entries"][index]
            assert isinstance(entry, Mapping)
        if entry["phase"] != "INSTALLED":
            continue
        _verify_installed(entry)
        target = Path(str(entry["target"]))
        before = _verify_backup(root, entry)
        restored_device = restored_inode = None
        if before is None:
            snapshot = _installed_snapshot_from_entry(entry)
            info = os.lstat(target)
            if (info.st_dev, info.st_ino) != (snapshot.device, snapshot.inode):
                raise QixiPostCorrectionPublicSurfaceError("rollback target inode ownership drifted")
            target.unlink()
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        else:
            restored = _atomic_replace(target, before)
            if _current_regular_sha256(target, label="rolled-back transaction target") != entry["before_sha256"]:
                raise QixiPostCorrectionPublicSurfaceError("rollback target bytes verification failed")
            restored_device, restored_inode = restored.device, restored.inode
        _checkpoint_entry(
            root,
            journal,
            index,
            phase="BACKED_UP",
            staged_device=None,
            staged_inode=None,
            installed_device=None,
            installed_inode=None,
            before_device=restored_device,
            before_inode=restored_inode,
        )
    _validate_journal(journal, authority=authority)


def _commit_journal(root: Path, journal: dict[str, object], *, authority: Mapping[str, object]) -> str:
    journal = _validate_journal(journal, authority=authority)
    if journal["status"] == "COMMITTED":
        _postcommit_replay(journal, authority=authority)
        _write_final_receipt(root, journal)
        return _cleanup_committed_backups_result(root, journal)
    entries = journal["entries"]
    assert isinstance(entries, list)
    try:
        for index, raw_entry in enumerate(entries):
            assert isinstance(raw_entry, Mapping)
            entry = raw_entry
            target = Path(str(entry["target"]))
            _require_safe_target_parent(target)
            payload = _unb64(entry["after_bytes_b64"], label="journal after")
            assert payload is not None
            phase = entry["phase"]
            if phase == "INSTALLED":
                _verify_backup(root, entry)
                _verify_installed(entry)
                continue
            if phase == "PREPARED":
                _verify_entry_preimage(entry, label="target backup")
                _ensure_backup(root, entry)
                _verify_entry_preimage(entry, label="target backup checkpoint")
                _checkpoint_entry(root, journal, index, phase="BACKED_UP")
                entry = journal["entries"][index]
                assert isinstance(entry, Mapping)
            _verify_backup(root, entry)
            if entry["phase"] == "BACKED_UP":
                _verify_entry_preimage(entry, label="transaction staging")
                staged_path = _entry_staged_path(entry)
                if os.path.lexists(staged_path):
                    device, inode = _adopt_staged_file(
                        staged_path,
                        sha256=str(entry["after_sha256"]),
                        mode=int(entry["after_mode"]),
                    )
                    staged = InstalledSnapshot(
                        staged_path,
                        device,
                        inode,
                        str(entry["after_sha256"]),
                        int(entry["after_mode"]),
                    )
                else:
                    staged = _create_staged_inode(entry)
                _checkpoint_entry(root, journal, index, phase="INSTALLING", staged_device=staged.device, staged_inode=staged.inode)
                entry = journal["entries"][index]
                assert isinstance(entry, Mapping)
            if entry["phase"] != "INSTALLING":
                raise QixiPostCorrectionPublicSurfaceError("transaction phase is not resumable")
            staged = _stage_owned_inode(entry)
            _clear_staged_file_owner(
                staged.path,
                device=staged.device,
                inode=staged.inode,
                sha256=staged.sha256,
                mode=staged.mode,
            )
            target_info = os.lstat(target) if os.path.lexists(target) else None
            if target_info is not None and (target_info.st_dev, target_info.st_ino) == (staged.device, staged.inode):
                # Crash after rename: target is the checkpointed staged inode.
                _verify_staged_snapshot(InstalledSnapshot(target, staged.device, staged.inode, staged.sha256, staged.mode))
            else:
                _verify_staged_inode(entry)
                _verify_entry_preimage(entry, label="transaction install")
                os.replace(staged.path, target)
                directory_fd = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            installed = _snapshot_installed(target, sha256=str(entry["after_sha256"]), mode=int(entry["after_mode"]))
            _checkpoint_entry(
                root,
                journal,
                index,
                phase="INSTALLED",
                installed_device=installed.device,
                installed_inode=installed.inode,
            )
            _verify_backup(root, journal["entries"][index])
            _verify_installed(journal["entries"][index])
        _validate_journal(journal, authority=authority)
        _postcommit_replay(journal, authority=authority)
    except Exception:
        # Rollback is deliberately best effort only for journal-proven inodes;
        # a failed rollback remains a fail-closed recovery journal.
        try:
            _rollback_owned(root, journal, authority=authority)
        except BaseException:
            pass
        raise
    journal["status"] = "COMMITTED"
    _write_journal(root, journal)
    _validate_journal(journal, authority=authority)
    _postcommit_replay(journal, authority=authority)
    _write_final_receipt(root, journal)
    _postcommit_replay(journal, authority=authority)
    return _cleanup_committed_backups_result(root, journal)


def finalize(
    *,
    apply: bool,
    repo_root: Path = ROOT,
    runtime_root: Path = Path("/opt/bilive/autoslice"),
    source_fact_llm_call: LlmCall | None = None,
    authority: Mapping[str, object] | None = None,
    _stage_publish: Callable[..., dict[str, object] | None] = _stage_publish_draft,
    _runner_lock_held: bool = False,
) -> dict[str, object]:
    """Plan (zero-write) or apply this one sealed public-surface closure."""

    loaded = dict(authority) if authority is not None else load_deployed_authority(repo_root)
    normalized = validate_authority(loaded)
    if runtime_root != Path(str(normalized["runtime_root"])):
        raise QixiPostCorrectionPublicSurfaceError("runtime root differs from the sealed authority")
    if apply:
        if not _runner_lock_held:
            with _exclusive_runner_lock(runtime_root):
                return finalize(
                    apply=True,
                    repo_root=repo_root,
                    runtime_root=runtime_root,
                    source_fact_llm_call=source_fact_llm_call,
                    authority=normalized,
                    _stage_publish=_stage_publish,
                    _runner_lock_held=True,
                )
        _validate_repository_inputs(normalized, repo_root=repo_root)
        journal_root = _journal_root_from_authority(normalized)
        journal = _load_journal(journal_root, authority=normalized)
        if journal is not None:
            status = _commit_journal(journal_root, journal, authority=normalized)
            return {
                "schema_version": "qixi-post-correction-public-surface-result.v1",
                "status": status,
                "candidate_id": CANDIDATE_ID,
                "recording_date": RECORDING_DATE,
                "title": PUBLIC_TITLE,
                "upload_enabled": False,
                "journal": str(journal_root / "journal.json"),
                "metadata": journal.get("metadata"),
            }
    inputs = validate_runtime(normalized, repo_root=repo_root, runtime_root=runtime_root)
    if not apply:
        return {
            "schema_version": "qixi-post-correction-public-surface-plan.v1",
            "status": "DRY_RUN_PASS",
            "candidate_id": CANDIDATE_ID,
            "recording_date": RECORDING_DATE,
            "title": PUBLIC_TITLE,
            "upload_enabled": False,
        }
    journal_root = _journal_root(inputs)
    journal = _load_journal(journal_root, authority=inputs.authority)
    if journal is None:
        stage_root = Path(
            tempfile.mkdtemp(
                prefix=".qixi-public-surface-stage-",
                dir=inputs.artifact_paths["record"].parent,
            )
        )
        try:
            targets, metadata = _build_after_image(
                inputs,
                stage_root=stage_root,
                source_fact_llm_call=source_fact_llm_call or _default_source_fact_llm(),
                stage_publish=_stage_publish,
            )
            journal = _write_prepared_journal(
                journal_root, inputs=inputs, targets=targets, metadata=metadata
            )
        finally:
            _remove_private_stage(stage_root)
    status = _commit_journal(journal_root, journal, authority=inputs.authority)
    return {
        "schema_version": "qixi-post-correction-public-surface-result.v1",
        "status": status,
        "candidate_id": CANDIDATE_ID,
        "recording_date": RECORDING_DATE,
        "title": PUBLIC_TITLE,
        "upload_enabled": False,
        "journal": str(journal_root / "journal.json"),
        "metadata": journal.get("metadata"),
    }
