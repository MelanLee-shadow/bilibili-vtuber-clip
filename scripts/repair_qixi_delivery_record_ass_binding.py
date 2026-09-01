#!/usr/bin/env python3
"""One-shot, sealed repair of Qixi's stale final-ASS record binding.

This candidate-specific tool never creates media.  It replaces exactly two
JSON pointers in two byte-identical records and writes one create-only receipt.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
ASSET_RELATIVE = Path(
    "assets/lidousha/delivery_record_ass_binding_recovery/auto_113022_354_496.v1.json"
)
PACKAGE_ROOT = Path(
    "/opt/bilive/autoslice/out/2026-08-17/auto_113022_354_496/replacement_recuts"
)
SCHEMA = "qixi-delivery-record-ass-binding-recovery.v1"
_SHA = __import__("re").compile(r"sha256:[0-9a-f]{64}\Z")

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.autoslice.repository_asset_authority import (  # noqa: E402
    RepositoryAssetAuthorityError,
    require_repository_asset_authority,
)


class AssBindingRecoveryError(ValueError):
    """The sealed record-only recovery cannot prove its complete closure."""


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    raw: bytes | None
    sha256: str
    size: int
    device: int
    inode: int
    mode: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class ExecutionContext:
    authority_raw: bytes
    authority_seal: tuple[str, str, str, str]
    authority: Mapping[str, Any]
    snapshots: tuple[tuple[str, FileSnapshot], ...]
    primary_after: bytes
    receipt_after: bytes
    receipt_path: Path


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _canonical_sha(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _inode(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _lstat_regular(path: Path, *, label: str) -> os.stat_result:
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise AssBindingRecoveryError(f"{label} is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise AssBindingRecoveryError(f"{label} is not a regular non-symlink file")
    return info


def _assert_directory_chain(path: Path, *, label: str, leaf_may_be_absent: bool = False) -> None:
    if not path.is_absolute():
        raise AssBindingRecoveryError(f"{label} path is not absolute")
    try:
        relative = path.relative_to(PACKAGE_ROOT)
    except ValueError as exc:
        raise AssBindingRecoveryError(f"{label} path escapes the fixed package root") from exc
    if not relative.parts or ".." in relative.parts:
        raise AssBindingRecoveryError(f"{label} path is not canonical")
    parents = [PACKAGE_ROOT]
    cursor = PACKAGE_ROOT
    for part in relative.parts[:-1]:
        cursor = cursor / part
        parents.append(cursor)
    for parent in parents:
        info = os.lstat(parent)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise AssBindingRecoveryError(f"{label} ancestor is not a directory without symlinks")
    if not leaf_may_be_absent:
        _lstat_regular(path, label=label)


def _snapshot(path: Path, *, label: str, retain_raw: bool) -> FileSnapshot:
    _assert_directory_chain(path, label=label)
    before = _lstat_regular(path, label=label)
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or _inode(opened) != _inode(before):
                raise AssBindingRecoveryError(f"{label} changed while opening")
            raw = handle.read()
            after_open = os.fstat(handle.fileno())
    except OSError as exc:
        raise AssBindingRecoveryError(f"{label} cannot be read") from exc
    after = _lstat_regular(path, label=label)
    if _inode(after) != _inode(before) or after.st_size != before.st_size or _inode(after_open) != _inode(before):
        raise AssBindingRecoveryError(f"{label} changed while reading")
    return FileSnapshot(
        path=path,
        raw=raw if retain_raw else None,
        sha256=_digest(raw),
        size=len(raw),
        device=before.st_dev,
        inode=before.st_ino,
        mode=stat.S_IMODE(before.st_mode),
        mtime_ns=before.st_mtime_ns,
        ctime_ns=before.st_ctime_ns,
    )


def _same_snapshot(snapshot: FileSnapshot, *, label: str) -> None:
    fresh = _snapshot(snapshot.path, label=label, retain_raw=snapshot.raw is not None)
    if fresh != snapshot:
        raise AssBindingRecoveryError(f"{label} drifted after execution context was frozen")


def _descriptor(value: object, *, label: str) -> tuple[Path, str, int]:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256", "bytes"}:
        raise AssBindingRecoveryError(f"{label} descriptor is malformed")
    path, digest, size = Path(str(value["path"])), value["sha256"], value["bytes"]
    if (
        not path.is_absolute()
        or not isinstance(digest, str)
        or _SHA.fullmatch(digest) is None
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
    ):
        raise AssBindingRecoveryError(f"{label} descriptor is invalid")
    return path, digest, size


def _require_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise AssBindingRecoveryError(f"{label} is not a SHA-256 digest")
    return value


def _descriptor_snapshot(value: object, *, label: str, retain_raw: bool) -> FileSnapshot:
    path, digest, size = _descriptor(value, label=label)
    snapshot = _snapshot(path, label=label, retain_raw=retain_raw)
    if snapshot.sha256 != digest or snapshot.size != size:
        raise AssBindingRecoveryError(f"{label} bytes drifted")
    return snapshot


def _seal_tuple(seal: object) -> tuple[str, str, str, str]:
    try:
        result = (seal.mode, seal.commit, seal.relative_path, seal.file_sha256)
    except AttributeError as exc:
        raise AssBindingRecoveryError("record-ASS recovery authority seal is malformed") from exc
    if not all(isinstance(value, str) and value for value in result):
        raise AssBindingRecoveryError("record-ASS recovery authority seal is malformed")
    return result


def _load_authority() -> tuple[bytes, dict[str, Any], tuple[str, str, str, str]]:
    path = ROOT / ASSET_RELATIVE
    _lstat_regular(path, label="record-ASS recovery authority")
    raw = path.read_bytes()
    try:
        seal = require_repository_asset_authority(
            repo_root=ROOT, relative_path=ASSET_RELATIVE, observed_bytes=raw
        )
        authority = json.loads(raw)
    except (RepositoryAssetAuthorityError, OSError, json.JSONDecodeError) as exc:
        raise AssBindingRecoveryError("record-ASS recovery authority is not sealed") from exc
    if not isinstance(authority, dict):
        raise AssBindingRecoveryError("record-ASS recovery authority is not an object")
    return raw, authority, _seal_tuple(seal)


def _validate_predecessor(value: object) -> None:
    expected = {
        "relative_path",
        "authority_sha256",
        "successor_correction_manifest_sha256",
        "successor_record_sha256",
        "successor_burned_video_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise AssBindingRecoveryError("predecessor v2 evidence shape drifted")
    for key in expected - {"relative_path"}:
        _require_sha(value[key], label=f"predecessor v2 evidence {key}")
    if not isinstance(value["relative_path"], str):
        raise AssBindingRecoveryError("predecessor v2 evidence path is invalid")
    relative = Path(str(value["relative_path"]))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise AssBindingRecoveryError("predecessor v2 evidence path is unsafe")
    path = ROOT / relative
    _lstat_regular(path, label="predecessor v2 authority")
    raw = path.read_bytes()
    try:
        seal = require_repository_asset_authority(
            repo_root=ROOT, relative_path=relative, observed_bytes=raw
        )
        document = json.loads(raw)
    except (RepositoryAssetAuthorityError, OSError, json.JSONDecodeError) as exc:
        raise AssBindingRecoveryError("predecessor v2 evidence is not sealed") from exc
    if (
        not isinstance(document, Mapping)
        or document.get("schema_version") != "delivery-branding-recovery-authority.v2"
        or seal.file_sha256 != value["authority_sha256"]
        or any(document.get(key) != value[key] for key in (
            "successor_correction_manifest_sha256",
            "successor_record_sha256",
            "successor_burned_video_sha256",
        ))
    ):
        raise AssBindingRecoveryError("predecessor v2 evidence drifted")


def _require_exact_mapping(value: object, keys: set[str], *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise AssBindingRecoveryError(f"{label} shape drifted")
    return value


def _validate(authority: Mapping[str, Any]) -> tuple[dict[str, FileSnapshot], dict[str, Any], Path]:
    top_keys = {
        "schema_version", "candidate_id", "package_root", "receipt_path", "predecessor_v2_evidence",
        "primary_record", "mirror_record", "artifacts", "record_invariants", "correction_invariants",
    }
    if (
        set(authority) != top_keys
        or authority.get("schema_version") != SCHEMA
        or authority.get("candidate_id") != "auto_113022_354_496"
        or authority.get("package_root") != str(PACKAGE_ROOT)
    ):
        raise AssBindingRecoveryError("record-ASS recovery authority shape drifted")
    _validate_predecessor(authority["predecessor_v2_evidence"])
    primary = _descriptor_snapshot(authority["primary_record"], label="primary record", retain_raw=True)
    mirror = _descriptor_snapshot(authority["mirror_record"], label="mirror record", retain_raw=True)
    receipt = Path(str(authority["receipt_path"]))
    _assert_directory_chain(receipt, label="recovery receipt", leaf_may_be_absent=True)
    if _lexists(receipt) or receipt in {primary.path, mirror.path}:
        raise AssBindingRecoveryError("recovery receipt target is not create-only")
    if primary.path == mirror.path:
        raise AssBindingRecoveryError("primary and mirror record targets are not distinct")
    if primary.raw != mirror.raw:
        raise AssBindingRecoveryError("primary and mirror record preimages differ")
    try:
        record = json.loads(primary.raw)
    except json.JSONDecodeError as exc:
        raise AssBindingRecoveryError("record preimage is unreadable") from exc
    if not isinstance(record, dict):
        raise AssBindingRecoveryError("record preimage is not an object")
    artifacts = _require_exact_mapping(
        authority["artifacts"], {"subtitle", "main", "burned", "ass", "correction", "cover", "publish"},
        label="record-ASS artifact set",
    )
    snapshots: dict[str, FileSnapshot] = {"primary_record": primary, "mirror_record": mirror}
    for name, descriptor in artifacts.items():
        snapshots[name] = _descriptor_snapshot(descriptor, label=name, retain_raw=name == "correction")
    if len({primary.path, mirror.path, receipt}) != 3:
        raise AssBindingRecoveryError("record recovery targets are not distinct")
    inv = _require_exact_mapping(authority["record_invariants"], {
        "subtitle_ass_path", "subtitle_style", "speaker_mode", "human_text_correction_manifest_path",
        "human_text_correction_manifest_sha256", "artifact_hashes", "burned_preview_sha256",
        "boundary_audit_sha256", "publish_staging_sha256", "story_contract_sha256", "title", "cover_path",
        "cover_status",
    }, label="record invariants")
    expected_hashes = {
        "ai_background_sha256", "ass_sha256", "burned_video_sha256", "chat_authority_audit_sha256",
        "clip_context_file_sha256", "cover_reference_sha256", "cover_sha256", "publish_draft_sha256",
        "redelivery_baseline_audit_sha256", "subtitle_sha256", "video_sha256",
    }
    _require_exact_mapping(inv["artifact_hashes"], expected_hashes, label="record artifact hash closure")
    for key, value in inv["artifact_hashes"].items():
        _require_sha(value, label=f"record artifact hash closure {key}")
    for key in ("human_text_correction_manifest_sha256", "burned_preview_sha256", "boundary_audit_sha256", "publish_staging_sha256", "story_contract_sha256"):
        _require_sha(inv[key], label=f"record invariant {key}")
    if record.get("artifact_hashes") != inv["artifact_hashes"]:
        raise AssBindingRecoveryError("record artifact closure drifted")
    for key in (
        "subtitle_ass_path", "subtitle_style", "speaker_mode", "human_text_correction_manifest_path",
        "human_text_correction_manifest_sha256",
    ):
        if record.get(key) != inv[key]:
            raise AssBindingRecoveryError(f"record invariant drifted: {key}")
    staging = record.get("publish_staging")
    if not isinstance(staging, Mapping) or any(staging.get(key) != inv[key] for key in ("title", "cover_path", "cover_status")):
        raise AssBindingRecoveryError("record title/cover invariant drifted")
    for key, value in (("burned_preview", record.get("burned_preview")), ("boundary_audit", record.get("boundary_audit")), ("publish_staging", staging), ("story_contract", staging.get("story_contract"))):
        if _canonical_sha(value) != inv[f"{key}_sha256"]:
            raise AssBindingRecoveryError(f"record invariant drifted: {key}")
    artifact_paths = {name: snap.path for name, snap in snapshots.items() if name not in {"primary_record", "mirror_record"}}
    hashes = record["artifact_hashes"]
    if (
        record.get("subtitle_path") != str(artifact_paths["subtitle"])
        or record.get("media_path") != str(artifact_paths["main"])
        or record.get("human_text_correction_manifest_path") != str(artifact_paths["correction"])
        or staging.get("cover_path") != str(artifact_paths["cover"])
        or staging.get("publish_json_path") != str(artifact_paths["publish"])
        or record.get("burned_preview", {}).get("path") != str(artifact_paths["burned"])
        or hashes.get("subtitle_sha256") != snapshots["subtitle"].sha256
        or hashes.get("video_sha256") != snapshots["main"].sha256
        or hashes.get("burned_video_sha256") != snapshots["burned"].sha256
        or hashes.get("cover_sha256") != snapshots["cover"].sha256
        or hashes.get("publish_draft_sha256") != snapshots["publish"].sha256
    ):
        raise AssBindingRecoveryError("record artifact pointers drifted")
    try:
        correction = json.loads(snapshots["correction"].raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AssBindingRecoveryError("correction receipt is unreadable") from exc
    correction_inv = _require_exact_mapping(authority["correction_invariants"], {
        "schema_version", "candidate_id", "after_srt_sha256", "burned_media_sha256", "upload_enabled",
        "branding_intro", "authority_sha256", "authority_repository_seal",
    }, label="correction invariants")
    branding = correction.get("delivery_branding_authority") if isinstance(correction, Mapping) else None
    _require_exact_mapping(correction_inv["branding_intro"], {"status", "intro_id", "intro_media_sha256", "intro_offset_ms"}, label="Z2 branding intro")
    _require_exact_mapping(correction_inv["authority_repository_seal"], {"mode", "deployed_commit", "relative_path", "sha256"}, label="correction repository seal")
    for key in ("after_srt_sha256", "burned_media_sha256", "authority_sha256"):
        _require_sha(correction_inv[key], label=f"correction invariant {key}")
    _require_sha(correction_inv["branding_intro"]["intro_media_sha256"], label="Z2 intro media")
    _require_sha(correction_inv["authority_repository_seal"]["sha256"], label="correction repository seal sha256")
    if (
        not isinstance(branding, Mapping)
        or any(correction.get(key) != correction_inv[key] for key in ("schema_version", "candidate_id", "upload_enabled"))
        or "sha256:" + str(correction.get("after_srt_sha256")) != correction_inv["after_srt_sha256"]
        or "sha256:" + str(correction.get("burned_media_sha256")) != correction_inv["burned_media_sha256"]
        or branding.get("branding_intro") != correction_inv["branding_intro"]
        or branding.get("authority_sha256") != correction_inv["authority_sha256"]
        or branding.get("authority_repository_seal") != correction_inv["authority_repository_seal"]
    ):
        raise AssBindingRecoveryError("correction/Z2 invariant drifted")
    return snapshots, record, receipt


def _after_image(record: Mapping[str, Any], ass: FileSnapshot) -> bytes:
    after = copy.deepcopy(record)
    after["subtitle_ass_path"] = str(ass.path)
    after["artifact_hashes"]["ass_sha256"] = ass.sha256
    before_without, after_without = copy.deepcopy(record), copy.deepcopy(after)
    before_without.pop("subtitle_ass_path")
    after_without.pop("subtitle_ass_path")
    before_without["artifact_hashes"].pop("ass_sha256")
    after_without["artifact_hashes"].pop("ass_sha256")
    if before_without != after_without:
        raise AssBindingRecoveryError("after-image modifies fields outside the sealed pointer set")
    return _json_bytes(after)


def _evidence_descriptor(snapshot: FileSnapshot) -> dict[str, object]:
    return {"path": str(snapshot.path), "sha256": snapshot.sha256, "bytes": snapshot.size}


def _prepare() -> ExecutionContext:
    authority_raw, authority, seal = _load_authority()
    if seal[2] != ASSET_RELATIVE.as_posix():
        raise AssBindingRecoveryError("record-ASS recovery authority seal path drifted")
    snapshots, record, receipt = _validate(authority)
    after = _after_image(record, snapshots["ass"])
    receipt_doc = {
        "schema_version": SCHEMA,
        "mode": "APPLIED",
        "candidate_id": authority["candidate_id"],
        "authority": {
            "relative_path": seal[2], "mode": seal[0], "commit": seal[1], "sha256": seal[3],
        },
        "allowed_json_pointers": ["/artifact_hashes/ass_sha256", "/subtitle_ass_path"],
        "evidence_descriptors": {name: _evidence_descriptor(snapshot) for name, snapshot in sorted(snapshots.items())},
        "primary_preimage": {"sha256": snapshots["primary_record"].sha256, "bytes": snapshots["primary_record"].size},
        "mirror_preimage": {"sha256": snapshots["mirror_record"].sha256, "bytes": snapshots["mirror_record"].size},
        "postimage": {"sha256": _digest(after), "bytes": len(after)},
    }
    return ExecutionContext(
        authority_raw=authority_raw,
        authority_seal=seal,
        authority=authority,
        snapshots=tuple(sorted(snapshots.items())),
        primary_after=after,
        receipt_after=_json_bytes(receipt_doc),
        receipt_path=receipt,
    )


def _revalidate(context: ExecutionContext) -> None:
    if _prepare() != context:
        raise AssBindingRecoveryError("authority, epoch, preimage, or evidence drifted before commit")


def _assert_absent(path: Path, *, label: str) -> None:
    _assert_directory_chain(path, label=label, leaf_may_be_absent=True)
    if not _lexists(path):
        return
    raise AssBindingRecoveryError(f"{label} is no longer create-only")


def _lexists(path: Path) -> bool:
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False


def _owned(path: Path, owner: tuple[int, int], *, directory: bool = False) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    return not stat.S_ISLNK(info.st_mode) and expected_kind(info.st_mode) and _inode(info) == owner


def _installed_matches(path: Path, installed: FileSnapshot) -> bool:
    try:
        _same_snapshot(installed, label="installed transaction target")
    except AssBindingRecoveryError:
        return False
    return path == installed.path


def _restored_matches(snapshot: FileSnapshot) -> bool:
    """A rename changes ctime, so rollback verifies immutable content and inode."""

    try:
        fresh = _snapshot(snapshot.path, label="rollback preimage", retain_raw=True)
    except AssBindingRecoveryError:
        return False
    return (
        fresh.path == snapshot.path
        and fresh.raw == snapshot.raw
        and fresh.sha256 == snapshot.sha256
        and fresh.size == snapshot.size
        and fresh.device == snapshot.device
        and fresh.inode == snapshot.inode
        and fresh.mode == snapshot.mode
    )


def _relocated_matches(path: Path, snapshot: FileSnapshot, *, label: str) -> bool:
    """Verify a rename preserved the sealed preimage without trusting ctime."""

    try:
        fresh = _snapshot(path, label=label, retain_raw=True)
    except AssBindingRecoveryError:
        return False
    return (
        fresh.raw == snapshot.raw
        and fresh.sha256 == snapshot.sha256
        and fresh.size == snapshot.size
        and fresh.device == snapshot.device
        and fresh.inode == snapshot.inode
        and fresh.mode == snapshot.mode
    )


def _write_private(path: Path, payload: bytes) -> tuple[int, int]:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    owner: tuple[int, int] | None = None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            owner = _inode(os.fstat(handle.fileno()))
    except Exception:
        if owner is not None and _owned(path, owner):
            path.unlink()
        raise
    if owner is None:
        raise AssBindingRecoveryError("private transaction file did not retain ownership")
    if not _owned(path, owner):
        raise AssBindingRecoveryError("private transaction file ownership was lost")
    return owner


def _cleanup_private(stage: Path, owner: tuple[int, int]) -> None:
    if not _owned(stage, owner, directory=True):
        raise AssBindingRecoveryError("private transaction ownership was lost; preserved evidence")
    entries = list(stage.iterdir())
    if entries:
        raise AssBindingRecoveryError("private transaction has residual evidence; preserved it")
    stage.rmdir()


def _rollback(entries: list[dict[str, Any]], stage: Path, stage_owner: tuple[int, int]) -> str | None:
    problems: list[str] = []
    for entry in reversed(entries):
        target, original, backup, installed = entry["target"], entry["original"], entry["backup"], entry["installed"]
        if original is None:
            if _lexists(target):
                if installed is not None and _installed_matches(target, installed):
                    target.unlink()
                else:
                    problems.append(f"foreign replacement preserved at {target}")
            continue
        if _lexists(target):
            if installed is None:
                if _restored_matches(original):
                    continue
                problems.append(f"foreign replacement preserved at {target}")
                continue
            if installed is not None and _installed_matches(target, installed):
                target.unlink()
            else:
                problems.append(f"foreign replacement preserved at {target}")
                continue
        if backup is None or not _relocated_matches(backup, original, label="rollback backup"):
            problems.append(f"preimage backup ownership lost for {target}")
            continue
        try:
            _assert_absent(target, label="rollback target")
            os.replace(backup, target)
        except (OSError, AssBindingRecoveryError) as exc:
            problems.append(f"rollback restore failed for {target}: {exc}")
            continue
        if not _restored_matches(original):
            problems.append(f"rollback preimage drifted for {target}")
    for entry in entries:
        staged = entry["staged"]
        if _lexists(staged):
            if _owned(staged, entry["staged_owner"]):
                staged.unlink()
            else:
                problems.append(f"private staged ownership lost at {staged}")
    if problems:
        return "; ".join(problems) + f"; transaction evidence preserved at {stage}"
    try:
        _cleanup_private(stage, stage_owner)
    except (OSError, AssBindingRecoveryError) as exc:
        return str(exc)
    return None


def _postcommit_snapshot(entry: Mapping[str, Any], expected: bytes) -> None:
    target = entry["target"]
    installed = entry["installed"]
    if not isinstance(installed, FileSnapshot):
        raise AssBindingRecoveryError("postcommit transaction target was never installed")
    fresh = _snapshot(target, label="postcommit target", retain_raw=True)
    if fresh != installed or fresh.raw != expected:
        raise AssBindingRecoveryError("postcommit target ownership or bytes drifted")


def _cleanup_after_commit(
    entries: list[dict[str, Any]], stage: Path, stage_owner: tuple[int, int]
) -> str | None:
    """Best-effort cleanup after the durable three-target commit point.

    This deliberately never rolls back the now-visible records or receipt.
    """

    expected = (entries[0]["payload"], entries[1]["payload"], entries[2]["payload"])
    try:
        for entry, payload in zip(entries, expected, strict=True):
            backup = entry["backup"]
            if backup is None:
                continue
            _postcommit_snapshot(entry, payload)
            if not _relocated_matches(backup, entry["original"], label="preimage backup"):
                raise AssBindingRecoveryError("preimage backup ownership was lost before cleanup")
            backup.unlink()
        _cleanup_private(stage, stage_owner)
    except (OSError, AssBindingRecoveryError) as exc:
        return f"{exc}; committed transaction evidence preserved at {stage}"
    return None


def _commit(context: ExecutionContext) -> dict[str, str]:
    _revalidate(context)
    snapshots = dict(context.snapshots)
    outputs = [
        (snapshots["primary_record"], context.primary_after),
        (snapshots["mirror_record"], context.primary_after),
        (None, context.receipt_after),
    ]
    stage = Path(tempfile.mkdtemp(prefix=".qixi-ass-binding-", dir=PACKAGE_ROOT))
    stage_info = os.lstat(stage)
    if stat.S_ISLNK(stage_info.st_mode) or not stat.S_ISDIR(stage_info.st_mode):
        raise AssBindingRecoveryError("private transaction directory is unsafe")
    stage_owner = _inode(stage_info)
    entries: list[dict[str, Any]] = []
    try:
        for index, (original, payload) in enumerate(outputs):
            staged = stage / f"install-{index}"
            staged_owner = _write_private(staged, payload)
            entries.append({
                "target": original.path if original is not None else context.receipt_path,
                "original": original,
                "backup": stage / f"backup-{index}" if original is not None else None,
                "staged": staged,
                "staged_owner": staged_owner,
                "payload": payload,
                "installed": None,
            })
        _revalidate(context)
        for entry in entries:
            target, original, backup = entry["target"], entry["original"], entry["backup"]
            if original is None:
                _assert_absent(target, label="recovery receipt")
            else:
                _same_snapshot(original, label="commit preimage")
                _assert_absent(backup, label="private preimage backup")
                os.replace(target, backup)
                if not _relocated_matches(backup, original, label="preimage backup"):
                    raise AssBindingRecoveryError("preimage changed during backup rename")
                _assert_absent(target, label="cleared record target")
            os.replace(entry["staged"], target)
            installed = _snapshot(target, label="installed transaction target", retain_raw=True)
            if _inode(os.lstat(target)) != entry["staged_owner"] or installed.raw != entry["payload"]:
                raise AssBindingRecoveryError("installed target ownership was lost")
            entry["installed"] = installed
        for entry, expected in zip(
            entries, (context.primary_after, context.primary_after, context.receipt_after), strict=True
        ):
            _postcommit_snapshot(entry, expected)
    except (OSError, AssBindingRecoveryError) as exc:
        outcome = _rollback(entries, stage, stage_owner)
        detail = "restored preimages" if outcome is None else outcome
        raise AssBindingRecoveryError(f"record-ASS recovery commit failed; {detail}") from exc
    residue = _cleanup_after_commit(entries, stage, stage_owner)
    if residue is not None:
        return {"mode": "APPLIED_WITH_CLEANUP_RESIDUE", "cleanup_residue": residue}
    return {"mode": "APPLIED"}


def recover(*, apply: bool) -> dict[str, Any]:
    context = _prepare()
    plan = json.loads(context.receipt_after)
    plan["mode"] = "DRY_RUN"
    plan["receipt_path"] = str(context.receipt_path)
    if apply:
        applied = _commit(context)
        plan.update(applied)
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="atomically install the sealed after-image")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(recover(apply=args.apply), ensure_ascii=False, sort_keys=True))
    except AssBindingRecoveryError as exc:
        print(f"QIXI_RECORD_ASS_BINDING_REFUSED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
