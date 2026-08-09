"""Transactional relocation of a frozen slice package between hosts.

Only runtime locator fields are projected.  Evidence and provenance remain
byte-for-byte JSON values from the producing host.  The three mutable JSON
documents form a small forward-recoverable transaction: speaker first,
publish second, and the record (which binds both hashes) last.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence

from src.autoslice.package_relocation_contract import (
    ROOT_ROLES as _ROOT_ROLES,
    PUBLISH_PATH_POINTERS as _PUBLISH_PATH_POINTERS,
    RECORD_PATH_POINTERS as _RECORD_PATH_POINTERS,
    SPEAKER_PATH_POINTERS as _SPEAKER_PATH_POINTERS,
    JsonPointer,
    PackageRelocationError,
    get_value as _get,
    is_mutable_pointer as _is_mutable_pointer,
    parse_pointer as _parse_pointer,
    pointer_text as _pointer_text,
    reject_unknown_wsl_paths as _reject_unknown_wsl_paths,
    set_value as _set,
    speaker_record_pointer as _speaker_record_pointer,
    validate_document_root_roles as _validate_document_root_roles,
    validate_path_root_role as _validate_path_root_role,
)


SCHEMA_VERSION = "slice-package-relocation.v2"
_HEX64 = re.compile(r"[0-9a-f]{64}")
_TRANSACTION_ID = re.compile(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}")
_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_DOCUMENT_COMMIT_ORDER = ("speaker", "publish", "record")
_COMMIT_ORDER = (
    "chat_authority",
    "clip_context",
    "pre_relocation_speaker",
    "pre_relocation_publish",
    "pre_relocation_record",
    *_DOCUMENT_COMMIT_ORDER,
)
_PUBLISH_STAGING_LOCAL_KEYS = frozenset({"publish_json_path", "status"})
_PUBLISH_NON_STAGING_KEYS = frozenset(
    {"artifact_hashes", "candidate_id", "schema_version", "video_path"}
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _declared_digest(value: object, *, label: str) -> str:
    digest = str(value or "").removeprefix("sha256:")
    if not _HEX64.fullmatch(digest):
        raise PackageRelocationError(f"{label}: invalid SHA-256")
    return digest


def _normal_absolute_root(value: str | Path, *, label: str) -> str:
    raw = str(value).rstrip("/")
    pure = PurePosixPath(raw)
    if not pure.is_absolute() or ".." in pure.parts or raw in {"", "/"}:
        raise PackageRelocationError(f"{label}: unsafe absolute root")
    return raw


def _verified_destination_package_root(
    package_root: Path, declared_root: str | Path
) -> str:
    physical = _normal_absolute_root(package_root, label="physical package root")
    declared = _normal_absolute_root(
        declared_root, label="destination package root"
    )
    if declared != physical:
        raise PackageRelocationError(
            "destination package root does not match the physical package root"
        )
    return declared


def _require_real_directory_root(path: Path, *, label: str) -> Path:
    absolute = path.absolute()
    try:
        resolved = path.resolve(strict=True)
        mode = os.lstat(path).st_mode
    except OSError as exc:
        raise PackageRelocationError(f"{label}: directory missing") from exc
    if resolved != absolute or stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise PackageRelocationError(f"{label}: symlinked directory chain forbidden")
    return absolute


def _rewrite_path(
    value: object,
    *,
    kind: str,
    pointer: JsonPointer,
    mappings: Sequence[tuple[str, str]],
    source_workspace_root: str,
) -> object:
    matched = _validate_path_root_role(
        value,
        kind=kind,
        pointer=pointer,
        mappings=mappings,
    )
    if matched is None:
        return None
    role, side, suffix = matched
    if side == "destination":
        return value
    destination = mappings[_ROOT_ROLES.index(role)][1]
    return destination + suffix


def _changed_pointers(before: object, after: object, pointer: JsonPointer = ()) -> set[JsonPointer]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        if set(before) != set(after):
            return {pointer}
        changed: set[JsonPointer] = set()
        for key in before:
            changed.update(
                _changed_pointers(before[key], after[key], (*pointer, str(key)))
            )
        return changed
    if isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            return {pointer}
        changed: set[JsonPointer] = set()
        for index, (left, right) in enumerate(zip(before, after)):
            changed.update(
                _changed_pointers(left, right, (*pointer, str(index)))
            )
        return changed
    return set() if before == after else {pointer}


def _allowed_changed_pointer(kind: str, pointer: JsonPointer) -> bool:
    if _is_mutable_pointer(kind, pointer):
        return True
    if kind == "record" and pointer in {
        ("speaker_finalization_manifest_sha256",),
        ("artifact_hashes", "publish_draft_sha256"),
    }:
        return True
    return False


def _transform_speaker(
    source: Mapping[str, Any],
    *,
    mappings: Sequence[tuple[str, str]],
    source_workspace_root: str,
) -> dict[str, Any]:
    result = copy.deepcopy(dict(source))
    for pointer in _SPEAKER_PATH_POINTERS:
        if pointer[0] not in result:
            continue
        _set(
            result,
            pointer,
            _rewrite_path(
                _get(result, pointer),
                kind="speaker",
                pointer=pointer,
                mappings=mappings,
                source_workspace_root=source_workspace_root,
            ),
        )
    changed = _changed_pointers(source, result)
    if any(not _allowed_changed_pointer("speaker", pointer) for pointer in changed):
        raise PackageRelocationError("speaker: immutable evidence changed")
    _reject_unknown_wsl_paths(
        result,
        kind="speaker",
        source_workspace_root=source_workspace_root,
    )
    return result


def _transform_publish(
    source: Mapping[str, Any],
    *,
    mappings: Sequence[tuple[str, str]],
    source_workspace_root: str,
) -> dict[str, Any]:
    result = copy.deepcopy(dict(source))
    for pointer in _PUBLISH_PATH_POINTERS:
        if _get(result, pointer) is None:
            continue
        _set(
            result,
            pointer,
            _rewrite_path(
                _get(result, pointer),
                kind="publish",
                pointer=pointer,
                mappings=mappings,
                source_workspace_root=source_workspace_root,
            ),
        )
    changed = _changed_pointers(source, result)
    if any(not _allowed_changed_pointer("publish", pointer) for pointer in changed):
        raise PackageRelocationError("publish: immutable evidence changed")
    _reject_unknown_wsl_paths(
        result,
        kind="publish",
        source_workspace_root=source_workspace_root,
    )
    return result


def _transform_record(
    source: Mapping[str, Any],
    *,
    speaker: Mapping[str, Any],
    speaker_sha256: str,
    publish: Mapping[str, Any],
    publish_sha256: str,
    chat_target: str,
    context_target: str,
    mappings: Sequence[tuple[str, str]],
    source_workspace_root: str,
) -> dict[str, Any]:
    result = copy.deepcopy(dict(source))
    for pointer in _RECORD_PATH_POINTERS:
        if _get(result, pointer) is None:
            continue
        if pointer == ("chat_authority_audit_path",):
            replacement: object = chat_target
        elif pointer == ("clip_context_path",):
            replacement = context_target
        else:
            replacement = _rewrite_path(
                _get(result, pointer),
                kind="record",
                pointer=pointer,
                mappings=mappings,
                source_workspace_root=source_workspace_root,
            )
        _set(result, pointer, replacement)

    result["speaker_finalization"] = copy.deepcopy(dict(speaker))
    result["speaker_finalization_manifest_sha256"] = "sha256:" + speaker_sha256
    artifact_hashes = result.get("artifact_hashes")
    if not isinstance(artifact_hashes, dict):
        raise PackageRelocationError("record: artifact_hashes is not an object")
    artifact_hashes["publish_draft_sha256"] = "sha256:" + publish_sha256

    publish_staging = result.get("publish_staging")
    if not isinstance(publish_staging, dict):
        raise PackageRelocationError("record: publish_staging is not an object")
    expected_staging_keys = (
        set(publish) - _PUBLISH_NON_STAGING_KEYS
    ) | set(_PUBLISH_STAGING_LOCAL_KEYS)
    if set(publish_staging) != expected_staging_keys:
        raise PackageRelocationError("record/publish: staging field set differs")
    for key in publish_staging:
        if key in _PUBLISH_STAGING_LOCAL_KEYS:
            continue
        if key not in publish:
            raise PackageRelocationError(f"publish: missing mirrored field {key}")
        publish_staging[key] = copy.deepcopy(publish[key])

    changed = _changed_pointers(source, result)
    if any(not _allowed_changed_pointer("record", pointer) for pointer in changed):
        bad = sorted(
            _pointer_text(pointer)
            for pointer in changed
            if not _allowed_changed_pointer("record", pointer)
        )
        raise PackageRelocationError(
            "record: immutable evidence changed: " + ",".join(bad)
        )
    _reject_unknown_wsl_paths(
        result,
        kind="record",
        source_workspace_root=source_workspace_root,
    )
    return result


def _assert_destination_projection(
    *,
    record: Mapping[str, Any],
    speaker: Mapping[str, Any],
    publish: Mapping[str, Any],
    chat_target: Path,
    context_target: Path,
    mappings: Sequence[tuple[str, str]],
    source_workspace_root: str,
    source_record: Mapping[str, Any] | None = None,
    source_speaker: Mapping[str, Any] | None = None,
    source_publish: Mapping[str, Any] | None = None,
) -> None:
    projected_speaker = _transform_speaker(
        source_speaker or speaker,
        mappings=mappings,
        source_workspace_root=source_workspace_root,
    )
    projected_publish = _transform_publish(
        source_publish or publish,
        mappings=mappings,
        source_workspace_root=source_workspace_root,
    )
    projected_record = _transform_record(
        source_record or record,
        speaker=projected_speaker,
        speaker_sha256=_sha256_bytes(_json_bytes(projected_speaker)),
        publish=projected_publish,
        publish_sha256=_sha256_bytes(_json_bytes(projected_publish)),
        chat_target=str(chat_target),
        context_target=str(context_target),
        mappings=mappings,
        source_workspace_root=source_workspace_root,
    )
    if (
        projected_speaker != speaker
        or projected_publish != publish
        or projected_record != record
    ):
        raise PackageRelocationError(
            "journal: COMMITTED graph contains unrelocated locator fields"
        )


def _load_json_document(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    _require_regular_path(path, trusted_roots=(path.parent,), label=label)
    try:
        payload = path.read_bytes()
        value = json.loads(payload)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackageRelocationError(f"{label}: unreadable JSON") from exc
    if not isinstance(value, dict):
        raise PackageRelocationError(f"{label}: JSON root is not an object")
    return value, payload


def _decode_json_document(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PackageRelocationError(f"{label}: unreadable JSON") from exc
    if not isinstance(value, dict):
        raise PackageRelocationError(f"{label}: JSON root is not an object")
    return value


def _path_inside(path: Path, root: Path) -> Path | None:
    try:
        return path.absolute().relative_to(root.absolute())
    except ValueError:
        return None


def _require_regular_path(
    path: Path,
    *,
    trusted_roots: Sequence[Path],
    label: str,
) -> Path:
    if not path.is_absolute():
        path = path.absolute()
    selected: tuple[Path, Path] | None = None
    for root in trusted_roots:
        relative = _path_inside(path, root)
        if relative is not None:
            selected = (root.absolute(), relative)
            break
    if selected is None:
        raise PackageRelocationError(f"{label}: target escapes trusted roots")
    root, relative = selected
    try:
        root_stat = os.lstat(root)
    except OSError as exc:
        raise PackageRelocationError(f"{label}: trusted root missing") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise PackageRelocationError(f"{label}: unsafe trusted root")
    current = root
    for component in relative.parts:
        if component in {"", ".", ".."}:
            raise PackageRelocationError(f"{label}: traversal component")
        current = current / component
        try:
            mode = os.lstat(current).st_mode
        except OSError as exc:
            raise PackageRelocationError(f"{label}: artifact missing") from exc
        if stat.S_ISLNK(mode):
            raise PackageRelocationError(f"{label}: symlink forbidden")
    try:
        final_mode = os.lstat(path).st_mode
    except OSError as exc:
        raise PackageRelocationError(f"{label}: artifact missing") from exc
    if not stat.S_ISREG(final_mode):
        raise PackageRelocationError(f"{label}: not a regular file")
    return path


def _require_safe_virtual_target(
    path: Path,
    *,
    trusted_roots: Sequence[Path],
    label: str,
) -> Path:
    """Validate every existing directory component of a staged target."""

    selected: tuple[Path, Path] | None = None
    for root in trusted_roots:
        relative = _path_inside(path, root)
        if relative is not None:
            selected = (root.absolute(), relative)
            break
    if selected is None:
        raise PackageRelocationError(f"{label}: virtual target escapes roots")
    root, relative = selected
    current = root
    try:
        root_mode = os.lstat(root).st_mode
    except OSError as exc:
        raise PackageRelocationError(f"{label}: trusted root missing") from exc
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        raise PackageRelocationError(f"{label}: unsafe trusted root")
    for component in relative.parts[:-1]:
        if component in {"", ".", ".."}:
            raise PackageRelocationError(f"{label}: traversal component")
        current = current / component
        try:
            mode = os.lstat(current).st_mode
        except OSError as exc:
            raise PackageRelocationError(
                f"{label}: virtual target parent missing"
            ) from exc
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise PackageRelocationError(f"{label}: unsafe virtual target parent")
    return path


def _resolve_runtime_path(
    raw: object,
    *,
    aliases: Mapping[str, Path],
    mappings: Sequence[tuple[str, str]],
    trusted_roots: Sequence[Path],
    virtual_files: Mapping[Path, bytes],
    label: str,
) -> Path:
    if not isinstance(raw, str) or not raw.startswith("/"):
        raise PackageRelocationError(f"{label}: declared path is not absolute")
    if raw in aliases:
        candidate = aliases[raw]
    else:
        projected = raw
        for source, destination in mappings:
            if raw == source or raw.startswith(source + "/"):
                projected = destination + raw[len(source) :]
                break
        candidate = Path(projected)
    if candidate.absolute() in virtual_files and not candidate.exists():
        # A package-local evidence file may be part of the same prepared
        # transaction.  Its bytes are already hash-bound; validate the target
        # parent without pretending that the future inode exists yet.
        return _require_safe_virtual_target(
            candidate,
            trusted_roots=trusted_roots,
            label=label,
        )
    return _require_regular_path(candidate, trusted_roots=trusted_roots, label=label)


def _artifact_payload(
    path: Path,
    *,
    virtual_files: Mapping[Path, bytes],
) -> bytes:
    absolute = path.absolute()
    if absolute in virtual_files:
        return virtual_files[absolute]
    return path.read_bytes()


def _validate_bound_artifact(
    raw_path: object,
    declared_sha256: object,
    *,
    aliases: Mapping[str, Path],
    mappings: Sequence[tuple[str, str]],
    trusted_roots: Sequence[Path],
    virtual_files: Mapping[Path, bytes],
    label: str,
) -> Path:
    expected = _declared_digest(declared_sha256, label=label)
    path = _resolve_runtime_path(
        raw_path,
        aliases=aliases,
        mappings=mappings,
        trusted_roots=trusted_roots,
        virtual_files=virtual_files,
        label=label,
    )
    actual = _sha256_bytes(_artifact_payload(path, virtual_files=virtual_files))
    if actual != expected:
        raise PackageRelocationError(
            f"{label}: SHA-256 mismatch expected={expected} actual={actual}"
        )
    return path


def _validate_speaker_artifacts(
    speaker: Mapping[str, Any],
    **kwargs: Any,
) -> None:
    bindings = (
        ("source_media", "source_media_sha256"),
        ("text_final_srt", "text_final_srt_sha256"),
        ("profile", "profile_sha256"),
        ("speaker_override", "speaker_override_sha256"),
        ("source_session_anchor_manifest", "source_session_anchor_manifest_sha256"),
        ("mixed_overlap_evidence", "mixed_overlap_evidence_sha256"),
        ("output_review_srt", "output_review_srt_sha256"),
        ("output_ass", "output_ass_sha256"),
    )
    for path_key, hash_key in bindings:
        path_value = speaker.get(path_key)
        digest_value = speaker.get(hash_key)
        if path_value is None and digest_value is None:
            continue
        if path_value is None or digest_value is None:
            raise PackageRelocationError(
                f"speaker: incomplete binding {path_key}/{hash_key}"
            )
        _validate_bound_artifact(
            path_value,
            digest_value,
            label=f"speaker.{path_key}",
            **kwargs,
        )


def _validate_cover_artifacts(
    cover: Mapping[str, Any],
    **kwargs: Any,
) -> None:
    bindings = (
        ("final_cover", "final_cover_sha256"),
        ("pre_overlay_path", "pre_overlay_sha256"),
        ("ai_background", "ai_background_sha256"),
        ("reference_image", "reference_sha256"),
    )
    for path_key, hash_key in bindings:
        path_value = cover.get(path_key)
        digest_value = cover.get(hash_key)
        if path_value is None and digest_value is None:
            continue
        if path_value is None or digest_value is None:
            raise PackageRelocationError(
                f"cover: incomplete binding {path_key}/{hash_key}"
            )
        _validate_bound_artifact(
            path_value,
            digest_value,
            label=f"cover.{path_key}",
            **kwargs,
        )
    rendered = cover.get("rendered_text_pixels")
    if not isinstance(rendered, Mapping):
        raise PackageRelocationError("cover: rendered_text_pixels missing")
    for path_key, hash_key in (
        ("mask_path", "mask_sha256"),
        ("pre_overlay_path", "pre_overlay_sha256"),
    ):
        _validate_bound_artifact(
            rendered.get(path_key),
            rendered.get(hash_key),
            label=f"cover.rendered_text_pixels.{path_key}",
            **kwargs,
        )


def _validate_graph(
    *,
    record: Mapping[str, Any],
    speaker: Mapping[str, Any],
    publish: Mapping[str, Any],
    speaker_bytes: bytes,
    publish_bytes: bytes,
    aliases: Mapping[str, Path],
    mappings: Sequence[tuple[str, str]],
    trusted_roots: Sequence[Path],
    virtual_files: Mapping[Path, bytes],
) -> None:
    _validate_document_root_roles(record, kind="record", mappings=mappings)
    _validate_document_root_roles(speaker, kind="speaker", mappings=mappings)
    _validate_document_root_roles(publish, kind="publish", mappings=mappings)
    if record.get("status") != "MATERIALIZED":
        raise PackageRelocationError("record: status is not MATERIALIZED")
    if speaker.get("status") != "READY" or speaker.get("production_ready") is not True:
        raise PackageRelocationError("speaker: READY/production_ready required")
    if record.get("speaker_finalization") != speaker:
        raise PackageRelocationError("record: embedded speaker differs from standalone")
    if _declared_digest(
        record.get("speaker_finalization_manifest_sha256"),
        label="record speaker manifest",
    ) != _sha256_bytes(speaker_bytes):
        raise PackageRelocationError("record: speaker manifest hash is stale")

    artifact_hashes = record.get("artifact_hashes")
    publish_hashes = publish.get("artifact_hashes")
    if not isinstance(artifact_hashes, Mapping) or not isinstance(publish_hashes, Mapping):
        raise PackageRelocationError("record/publish: artifact_hashes missing")
    if {
        key: value
        for key, value in artifact_hashes.items()
        if key != "publish_draft_sha256"
    } != dict(publish_hashes):
        raise PackageRelocationError("record/publish: artifact hash maps differ")
    if _declared_digest(
        artifact_hashes.get("publish_draft_sha256"),
        label="record publish draft",
    ) != _sha256_bytes(publish_bytes):
        raise PackageRelocationError("record: publish draft hash is stale")

    publish_staging = record.get("publish_staging")
    if not isinstance(publish_staging, Mapping):
        raise PackageRelocationError("record: publish_staging missing")
    expected_staging_keys = (
        set(publish) - _PUBLISH_NON_STAGING_KEYS
    ) | set(_PUBLISH_STAGING_LOCAL_KEYS)
    if set(publish_staging) != expected_staging_keys:
        raise PackageRelocationError("record/publish: staging field set differs")
    for key, value in publish_staging.items():
        if key in _PUBLISH_STAGING_LOCAL_KEYS:
            continue
        if key not in publish or value != publish[key]:
            raise PackageRelocationError(
                f"record/publish: mirrored field {key} differs"
            )

    common = dict(
        aliases=aliases,
        mappings=mappings,
        trusted_roots=trusted_roots,
        virtual_files=virtual_files,
    )
    _validate_speaker_artifacts(speaker, **common)
    _validate_bound_artifact(
        record.get("speaker_finalization_manifest_path"),
        record.get("speaker_finalization_manifest_sha256"),
        label="record.speaker_finalization_manifest_path",
        **common,
    )
    _validate_bound_artifact(
        publish_staging.get("publish_json_path"),
        artifact_hashes.get("publish_draft_sha256"),
        label="record.publish_staging.publish_json_path",
        **common,
    )
    cover = publish.get("cover_generation")
    if not isinstance(cover, Mapping):
        raise PackageRelocationError("publish: cover_generation missing")
    _validate_cover_artifacts(cover, **common)
    cover_hash_bindings = {
        "cover_sha256": cover.get("final_cover_sha256"),
        "ai_background_sha256": cover.get("ai_background_sha256"),
        "cover_reference_sha256": cover.get("reference_sha256"),
    }
    for artifact_key, cover_value in cover_hash_bindings.items():
        if artifact_hashes.get(artifact_key) != cover_value:
            raise PackageRelocationError(
                f"record/cover: {artifact_key} binding differs"
            )
    speaker_hash_bindings = {
        "video_sha256": speaker.get("source_media_sha256"),
        "subtitle_sha256": speaker.get("text_final_srt_sha256"),
        "speaker_review_srt_sha256": speaker.get("output_review_srt_sha256"),
        "ass_sha256": speaker.get("output_ass_sha256"),
    }
    for artifact_key, speaker_value in speaker_hash_bindings.items():
        if artifact_hashes.get(artifact_key) != speaker_value:
            raise PackageRelocationError(
                f"record/speaker: {artifact_key} binding differs"
            )

    record_bindings = (
        ("media_path", "video_sha256"),
        ("subtitle_path", "subtitle_sha256"),
        ("speaker_review_srt_path", "speaker_review_srt_sha256"),
        ("subtitle_ass_path", "ass_sha256"),
        ("chat_authority_audit_path", "chat_authority_audit_sha256"),
        ("clip_context_path", "clip_context_file_sha256"),
        ("redelivery_baseline_audit_path", "redelivery_baseline_audit_sha256"),
        ("subtitle_regression_audit_path", "subtitle_regression_audit_sha256"),
        ("talk_filler_audit_path", "talk_filler_audit_sha256"),
        ("text_finalization_manifest_path", "text_finalization_manifest_sha256"),
    )
    for path_key, hash_key in record_bindings:
        path_value = record.get(path_key)
        digest_value = artifact_hashes.get(hash_key)
        if path_value is None and digest_value is None:
            continue
        if path_value is None or digest_value is None:
            raise PackageRelocationError(
                f"record: incomplete binding {path_key}/{hash_key}"
            )
        _validate_bound_artifact(
            path_value,
            digest_value,
            label=f"record.{path_key}",
            **common,
        )
    burned = record.get("burned_preview")
    if not isinstance(burned, Mapping):
        raise PackageRelocationError("record: burned_preview missing")
    _validate_bound_artifact(
        burned.get("path"),
        artifact_hashes.get("burned_video_sha256"),
        label="record.burned_preview.path",
        **common,
    )
    _validate_bound_artifact(
        publish.get("video_path"),
        artifact_hashes.get("video_sha256"),
        label="publish.video_path",
        **common,
    )
    _validate_bound_artifact(
        publish.get("cover_path"),
        artifact_hashes.get("cover_sha256"),
        label="publish.cover_path",
        **common,
    )


def _atomic_temp(root: Path, *, name: str, payload: bytes) -> Path:
    fd, raw = tempfile.mkstemp(dir=root, prefix=f".{name}.", suffix=".relocating")
    path = Path(raw)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise PackageRelocationError("journal: symlink forbidden")
    temporary = _atomic_temp(path.parent, name=path.name, payload=_json_bytes(value))
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _transaction_lock(package_root: Path, candidate_id: str) -> Iterator[None]:
    path = package_root / f".{candidate_id}.relocation.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise PackageRelocationError("lock: cannot open safely") from exc
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _evidence_payload(
    *,
    recorded_path: object,
    expected_sha256: object,
    target: Path,
    evidence_root: Path,
    trusted_roots: Sequence[Path],
    label: str,
) -> tuple[bytes, bool]:
    expected = _declared_digest(expected_sha256, label=label)
    try:
        mode = os.lstat(target).st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None:
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise PackageRelocationError(f"{label}: unsafe package target")
        payload = target.read_bytes()
        if _sha256_bytes(payload) != expected:
            raise PackageRelocationError(f"{label}: package target hash drift")
        return payload, False
    if not isinstance(recorded_path, str):
        raise PackageRelocationError(f"{label}: recorded path missing")
    basename = Path(recorded_path).name
    if basename != target.name:
        raise PackageRelocationError(f"{label}: unexpected evidence basename")
    candidates = (Path(recorded_path), evidence_root / basename)
    source: Path | None = None
    for candidate in candidates:
        try:
            _require_regular_path(
                candidate,
                trusted_roots=trusted_roots,
                label=f"{label} source",
            )
        except PackageRelocationError:
            continue
        source = candidate
        break
    if source is None:
        raise PackageRelocationError(f"{label}: exact source unavailable")
    payload = source.read_bytes()
    if _sha256_bytes(payload) != expected:
        raise PackageRelocationError(f"{label}: source hash drift")
    return payload, True


def _preimage_payload(
    *,
    target: Path,
    expected_sha256: str,
    current_payload: bytes,
    package_root: Path,
    label: str,
) -> tuple[bytes, bool]:
    """Load a durable preimage or stage it only from exact current bytes."""

    try:
        mode = os.lstat(target).st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None:
        _require_regular_path(
            target,
            trusted_roots=(package_root,),
            label=label,
        )
        payload = target.read_bytes()
        if _sha256_bytes(payload) != expected_sha256:
            raise PackageRelocationError(f"{label}: preimage hash drift")
        return payload, False
    if _sha256_bytes(current_payload) != expected_sha256:
        raise PackageRelocationError(
            f"{label}: durable preimage unavailable after document transition"
        )
    return current_payload, True


def _chat_speaker_manifest_digest(payload: bytes) -> str:
    try:
        document = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PackageRelocationError("chat authority: unreadable JSON") from exc
    if not isinstance(document, Mapping):
        raise PackageRelocationError("chat authority: JSON root is not an object")
    return _declared_digest(
        document.get("speaker_manifest_sha256"),
        label="chat authority speaker manifest",
    )


def _journal_timestamp(value: object, *, label: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PackageRelocationError(f"journal: invalid {label}")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise PackageRelocationError(f"journal: invalid {label}") from exc
    if parsed.tzinfo is None:
        raise PackageRelocationError(f"journal: invalid {label}")


def _journal_digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise PackageRelocationError(f"journal: invalid {label}")
    return value


def _validate_journal_contract(
    journal: Mapping[str, Any],
    *,
    candidate_id: str,
    roots: Mapping[str, str],
    document_paths: Mapping[str, Path],
    current_payloads: Mapping[str, bytes],
    evidence: Mapping[str, tuple[Path, bytes, bool]],
    chat_speaker_sha256: str,
) -> None:
    status = journal.get("status")
    if status not in {"PREPARED", "COMMITTED"}:
        raise PackageRelocationError("journal: invalid transaction status")
    expected_keys = {
        "schema_version",
        "transaction_id",
        "candidate_id",
        "status",
        "prepared_at",
        "roots",
        "documents",
        "staged_evidence",
        "speaker_manifest_lineage",
        "commit_order",
    }
    if status == "COMMITTED":
        expected_keys.add("committed_at")
    if set(journal) != expected_keys:
        raise PackageRelocationError("journal: unexpected document shape")
    if journal.get("schema_version") != SCHEMA_VERSION:
        raise PackageRelocationError("journal: schema mismatch")
    if journal.get("candidate_id") != candidate_id:
        raise PackageRelocationError("journal: candidate mismatch")
    if journal.get("roots") != roots:
        raise PackageRelocationError("journal: relocation roots mismatch")
    transaction_id = journal.get("transaction_id")
    if not isinstance(transaction_id, str) or not _TRANSACTION_ID.fullmatch(
        transaction_id
    ):
        raise PackageRelocationError("journal: invalid transaction_id")
    _journal_timestamp(journal.get("prepared_at"), label="prepared_at")
    if status == "COMMITTED":
        _journal_timestamp(journal.get("committed_at"), label="committed_at")

    documents = journal.get("documents")
    if not isinstance(documents, Mapping) or set(documents) != set(
        _DOCUMENT_COMMIT_ORDER
    ):
        raise PackageRelocationError("journal: documents shape mismatch")
    rows: dict[str, Mapping[str, Any]] = {}
    for label in _DOCUMENT_COMMIT_ORDER:
        row = documents.get(label)
        if not isinstance(row, Mapping) or set(row) != {
            "path",
            "before_sha256",
            "after_sha256",
            "changed_pointers",
        }:
            raise PackageRelocationError(f"journal: {label} row shape mismatch")
        if row.get("path") != document_paths[label].name:
            raise PackageRelocationError(f"journal: {label} path mismatch")
        before = _journal_digest(
            row.get("before_sha256"), label=f"{label} before_sha256"
        )
        after = _journal_digest(
            row.get("after_sha256"), label=f"{label} after_sha256"
        )
        pointers = row.get("changed_pointers")
        if (
            not isinstance(pointers, list)
            or any(not isinstance(pointer, str) for pointer in pointers)
            or pointers != sorted(set(pointers))
        ):
            raise PackageRelocationError(
                f"journal: {label} changed_pointers invalid"
            )
        for raw_pointer in pointers:
            pointer = _parse_pointer(
                raw_pointer, label=f"journal {label} changed_pointers"
            )
            if not _allowed_changed_pointer(label, pointer):
                raise PackageRelocationError(
                    f"journal: {label} changed pointer not allowed"
                )
        if before == after or not pointers:
            raise PackageRelocationError(
                f"journal: {label} no-op transaction is forbidden"
            )
        current = _sha256_bytes(current_payloads[label])
        allowed_current = {after} if status == "COMMITTED" else {before, after}
        if current not in allowed_current:
            raise PackageRelocationError(
                f"journal: {status.lower()} {label} hash drift"
            )
        rows[label] = row

    staged = journal.get("staged_evidence")
    if not isinstance(staged, Mapping) or set(staged) != set(evidence):
        raise PackageRelocationError("journal: staged_evidence shape mismatch")
    for label, (target, payload, _needs_stage) in evidence.items():
        row = staged.get(label)
        if not isinstance(row, Mapping) or set(row) != {"path", "sha256"}:
            raise PackageRelocationError(
                f"journal: staged evidence {label} shape mismatch"
            )
        if row.get("path") != target.name:
            raise PackageRelocationError(
                f"journal: staged evidence {label} path mismatch"
            )
        if _journal_digest(row.get("sha256"), label=f"{label} sha256") != (
            _sha256_bytes(payload)
        ):
            raise PackageRelocationError(
                f"journal: staged evidence {label} hash drift"
            )
    for document_label in _DOCUMENT_COMMIT_ORDER:
        evidence_label = f"pre_relocation_{document_label}"
        staged_row = staged.get(evidence_label)
        if not isinstance(staged_row, Mapping):
            raise PackageRelocationError(
                f"journal: staged preimage {document_label} missing"
            )
        if staged_row.get("sha256") != rows[document_label].get(
            "before_sha256"
        ):
            raise PackageRelocationError(
                f"journal: {document_label} preimage/before hash mismatch"
            )

    lineage = journal.get("speaker_manifest_lineage")
    if not isinstance(lineage, Mapping) or set(lineage) != {
        "authority",
        "chat_speaker_manifest_sha256",
        "before_sha256",
        "after_sha256",
    }:
        raise PackageRelocationError("journal: speaker lineage shape mismatch")
    if lineage.get("authority") != "preserved_chat_pre_relocation_speaker_manifest":
        raise PackageRelocationError("journal: speaker lineage authority mismatch")
    speaker_row = rows["speaker"]
    if (
        _journal_digest(
            lineage.get("chat_speaker_manifest_sha256"),
            label="chat speaker manifest lineage",
        )
        != chat_speaker_sha256
        or lineage.get("before_sha256") != speaker_row.get("before_sha256")
        or lineage.get("after_sha256") != speaker_row.get("after_sha256")
        or chat_speaker_sha256 != speaker_row.get("before_sha256")
    ):
        raise PackageRelocationError("journal: speaker lineage hash mismatch")
    if journal.get("commit_order") != list(_COMMIT_ORDER):
        raise PackageRelocationError("journal: commit_order mismatch")


def _validate_journal_projection_rows(
    journal: Mapping[str, Any],
    *,
    before_docs: Mapping[str, Mapping[str, Any]],
    before_payloads: Mapping[str, bytes],
    after_docs: Mapping[str, Mapping[str, Any]],
    after_payloads: Mapping[str, bytes],
) -> None:
    rows = journal.get("documents")
    if not isinstance(rows, Mapping):
        raise PackageRelocationError("journal: documents missing")
    for label in _DOCUMENT_COMMIT_ORDER:
        row = rows.get(label)
        if not isinstance(row, Mapping):
            raise PackageRelocationError(f"journal: {label} row missing")
        expected_pointers = sorted(
            _pointer_text(pointer)
            for pointer in _changed_pointers(
                before_docs[label], after_docs[label]
            )
        )
        if row.get("changed_pointers") != expected_pointers:
            raise PackageRelocationError(
                f"journal: {label} changed_pointers do not match projection"
            )
        if row.get("before_sha256") != _sha256_bytes(before_payloads[label]):
            raise PackageRelocationError(
                f"journal: {label} before hash differs from preimage"
            )
        if row.get("after_sha256") != _sha256_bytes(after_payloads[label]):
            raise PackageRelocationError(
                f"journal: {label} after hash differs from projection"
            )


def _commit_relocation_graph(
    *,
    package_root: Path,
    document_paths: Mapping[str, Path],
    evidence: Mapping[str, tuple[Path, bytes, bool]],
    after_payloads: Mapping[str, bytes],
    journal_path: Path,
    journal: Mapping[str, Any],
    fresh_transaction: bool,
    after_aliases: Mapping[str, Path],
    mappings: Sequence[tuple[str, str]],
    trusted_roots: Sequence[Path],
) -> dict[str, Any]:
    if tuple(evidence) != _COMMIT_ORDER[: -len(_DOCUMENT_COMMIT_ORDER)]:
        raise PackageRelocationError("commit: staged evidence order mismatch")
    temporary_paths: dict[str, Path] = {}
    try:
        for label, (target, payload, needs_stage) in evidence.items():
            if needs_stage and not target.exists():
                temporary_paths[label] = _atomic_temp(
                    package_root,
                    name=target.name,
                    payload=payload,
                )
        for label, payload in after_payloads.items():
            target = document_paths[label]
            temporary_paths[label] = _atomic_temp(
                package_root,
                name=target.name,
                payload=payload,
            )

        # The staged bytes, rather than merely the in-memory objects, are the
        # graph that will be committed.  Recheck every temp before the durable
        # PREPARED transition.
        for label, payload in after_payloads.items():
            if temporary_paths[label].read_bytes() != payload:
                raise PackageRelocationError(
                    f"staging: {label} readback differs"
                )
        for label, (_target, payload, _needs_stage) in evidence.items():
            temporary = temporary_paths.get(label)
            if temporary is not None and temporary.read_bytes() != payload:
                raise PackageRelocationError(
                    f"staging: {label} readback differs"
                )
        if fresh_transaction:
            _atomic_json(journal_path, journal)

        for label, (target, _payload, _needs_stage) in evidence.items():
            temporary = temporary_paths.get(label)
            if temporary is None:
                continue
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise PackageRelocationError(
                    f"{label}: target appeared during commit"
                ) from exc
            temporary.unlink()
            _fsync_directory(package_root)
        for label in _DOCUMENT_COMMIT_ORDER:
            os.replace(temporary_paths[label], document_paths[label])
            _fsync_directory(package_root)
    finally:
        for temporary in temporary_paths.values():
            temporary.unlink(missing_ok=True)

    final_record, final_record_payload = _load_json_document(
        document_paths["record"], label="record"
    )
    final_speaker, final_speaker_payload = _load_json_document(
        document_paths["speaker"], label="speaker"
    )
    final_publish, final_publish_payload = _load_json_document(
        document_paths["publish"], label="publish"
    )
    final_payloads = {
        "record": final_record_payload,
        "speaker": final_speaker_payload,
        "publish": final_publish_payload,
    }
    for label, payload in final_payloads.items():
        expected = ((journal.get("documents") or {}).get(label) or {}).get(
            "after_sha256"
        )
        if _sha256_bytes(payload) != expected:
            raise PackageRelocationError(f"commit: {label} readback hash drift")
    _validate_graph(
        record=final_record,
        speaker=final_speaker,
        publish=final_publish,
        speaker_bytes=final_speaker_payload,
        publish_bytes=final_publish_payload,
        aliases=after_aliases,
        mappings=mappings,
        trusted_roots=trusted_roots,
        virtual_files={},
    )
    committed = copy.deepcopy(dict(journal))
    committed["status"] = "COMMITTED"
    committed["committed_at"] = _now()
    _atomic_json(journal_path, committed)
    return committed


@dataclass(frozen=True)
class _RelocationEnvironment:
    package_root: Path
    evidence_root: Path
    workspace: str
    mappings: tuple[tuple[str, str], ...]
    destination_roots: tuple[str, ...]
    trusted_roots: tuple[Path, ...]
    roots: dict[str, str]


def _build_relocation_environment(
    package_root: Path,
    *,
    source_package_root: str | Path,
    destination_package_root: str | Path,
    source_repo_root: str | Path,
    destination_repo_root: str | Path,
    evidence_root: Path | None,
    source_workspace_root: str | Path,
) -> _RelocationEnvironment:
    package = _require_real_directory_root(package_root, label="package_root")
    source_package = _normal_absolute_root(
        source_package_root, label="source package root"
    )
    destination_package = _verified_destination_package_root(
        package, destination_package_root
    )
    source_repo = _normal_absolute_root(source_repo_root, label="source repo root")
    destination_repo = _normal_absolute_root(
        destination_repo_root, label="destination repo root"
    )
    workspace = _normal_absolute_root(
        source_workspace_root, label="source workspace root"
    )
    evidence = _require_real_directory_root(
        evidence_root or package.parent, label="evidence_root"
    )
    if evidence != package.parent:
        raise PackageRelocationError(
            "destination candidate root does not match the physical package parent"
        )
    destination_repo_path = _require_real_directory_root(
        Path(destination_repo), label="destination_repo_root"
    )
    source_candidate = _normal_absolute_root(
        PurePosixPath(source_package).parent, label="source candidate root"
    )
    destination_candidate = _normal_absolute_root(
        evidence, label="destination candidate root"
    )
    if (
        source_package == destination_package
        or source_candidate == destination_candidate
        or source_repo == destination_repo
    ):
        raise PackageRelocationError("relocation roots must not form a no-op")
    mappings = (
        (source_package, destination_package),
        (source_candidate, destination_candidate),
        (source_repo, destination_repo),
    )
    return _RelocationEnvironment(
        package_root=package,
        evidence_root=evidence,
        workspace=workspace,
        mappings=mappings,
        destination_roots=(
            destination_package,
            destination_candidate,
            destination_repo,
        ),
        trusted_roots=(package, evidence, destination_repo_path),
        roots={
            "source_package_root": source_package,
            "destination_package_root": destination_package,
            "source_candidate_root": source_candidate,
            "destination_candidate_root": destination_candidate,
            "source_repo_root": source_repo,
            "destination_repo_root": destination_repo,
            "source_workspace_root": workspace,
            "evidence_root": str(evidence),
        },
    )


def _load_relocation_evidence(
    *,
    package_root: Path,
    candidate_id: str,
    record: Mapping[str, Any],
    current_payloads: Mapping[str, bytes],
    journal: Mapping[str, Any] | None,
    evidence_root: Path,
    trusted_roots: Sequence[Path],
) -> tuple[
    dict[str, tuple[Path, bytes, bool]],
    dict[str, Path],
    dict[Path, bytes],
    str,
]:
    chat_target = package_root / f"{candidate_id}.chat-authority.json"
    context_target = package_root / f"{candidate_id}.clip-context.json"
    artifact_hashes = record.get("artifact_hashes")
    if not isinstance(artifact_hashes, Mapping):
        raise PackageRelocationError("record: artifact_hashes missing")
    chat_payload, chat_needs_stage = _evidence_payload(
        recorded_path=record.get("chat_authority_audit_path"),
        expected_sha256=artifact_hashes.get("chat_authority_audit_sha256"),
        target=chat_target,
        evidence_root=evidence_root,
        trusted_roots=trusted_roots,
        label="chat authority",
    )
    context_payload, context_needs_stage = _evidence_payload(
        recorded_path=record.get("clip_context_path"),
        expected_sha256=artifact_hashes.get("clip_context_file_sha256"),
        target=context_target,
        evidence_root=evidence_root,
        trusted_roots=trusted_roots,
        label="clip context",
    )
    chat_speaker_sha256 = _chat_speaker_manifest_digest(chat_payload)
    before_digests: dict[str, str] = {}
    if journal is None:
        before_digests = {
            label: _sha256_bytes(current_payloads[label])
            for label in _DOCUMENT_COMMIT_ORDER
        }
    else:
        staged = journal.get("staged_evidence")
        if not isinstance(staged, Mapping):
            raise PackageRelocationError("journal: staged_evidence missing")
        for label in _DOCUMENT_COMMIT_ORDER:
            row = staged.get(f"pre_relocation_{label}")
            if not isinstance(row, Mapping):
                raise PackageRelocationError(
                    f"journal: pre-relocation {label} row missing"
                )
            before_digests[label] = _journal_digest(
                row.get("sha256"), label=f"pre-relocation {label} sha256"
            )
    if before_digests["speaker"] != chat_speaker_sha256:
        raise PackageRelocationError(
            "chat authority: speaker manifest does not bind pre-relocation bytes"
        )

    preimages: dict[str, tuple[Path, bytes, bool]] = {}
    for label in _DOCUMENT_COMMIT_ORDER:
        target = package_root / (
            f".{candidate_id}.pre-relocation-{label}.json"
        )
        evidence_label = f"pre_relocation_{label}"
        if (
            journal is not None
            and journal.get("status") == "COMMITTED"
            and not target.exists()
        ):
            raise PackageRelocationError(
                f"journal: COMMITTED evidence missing: {evidence_label}"
            )
        payload, needs_stage = _preimage_payload(
            target=target,
            expected_sha256=before_digests[label],
            current_payload=current_payloads[label],
            package_root=package_root,
            label=f"pre-relocation {label}",
        )
        preimages[evidence_label] = (
            target,
            payload,
            needs_stage,
        )
    return (
        {
            "chat_authority": (chat_target, chat_payload, chat_needs_stage),
            "clip_context": (context_target, context_payload, context_needs_stage),
            **preimages,
        },
        {
            str(record.get("chat_authority_audit_path")): chat_target,
            str(record.get("clip_context_path")): context_target,
        },
        {
            chat_target.absolute(): chat_payload,
            context_target.absolute(): context_payload,
        },
        chat_speaker_sha256,
    )


def _committed_replay_receipt(
    *,
    journal: Mapping[str, Any] | None,
    evidence: Mapping[str, tuple[Path, bytes, bool]],
    current_docs: Mapping[str, Mapping[str, Any]],
    current_payloads: Mapping[str, bytes],
    preimage_docs: Mapping[str, Mapping[str, Any]],
    preimage_payloads: Mapping[str, bytes],
    chat_target: Path,
    context_target: Path,
    environment: _RelocationEnvironment,
    aliases: Mapping[str, Path],
) -> dict[str, Any] | None:
    if journal is None or journal.get("status") != "COMMITTED":
        return None
    missing = sorted(
        label
        for label, (_target, _payload, needs_stage) in evidence.items()
        if needs_stage
    )
    if missing:
        raise PackageRelocationError(
            "journal: COMMITTED evidence missing: " + ",".join(missing)
        )
    rows = journal.get("documents")
    if not isinstance(rows, Mapping):
        raise PackageRelocationError("journal: documents missing")
    for label, payload in current_payloads.items():
        row = rows.get(label)
        if not isinstance(row, Mapping) or row.get(
            "after_sha256"
        ) != _sha256_bytes(payload):
            raise PackageRelocationError(
                f"journal: committed {label} hash drift"
            )
    for kind in _DOCUMENT_COMMIT_ORDER:
        _validate_document_root_roles(
            current_docs[kind], kind=kind, mappings=environment.mappings
        )
    _assert_destination_projection(
        record=current_docs["record"],
        speaker=current_docs["speaker"],
        publish=current_docs["publish"],
        chat_target=chat_target,
        context_target=context_target,
        mappings=environment.mappings,
        source_workspace_root=environment.workspace,
        source_record=preimage_docs["record"],
        source_speaker=preimage_docs["speaker"],
        source_publish=preimage_docs["publish"],
    )
    _validate_journal_projection_rows(
        journal,
        before_docs=preimage_docs,
        before_payloads=preimage_payloads,
        after_docs=current_docs,
        after_payloads=current_payloads,
    )
    _validate_graph(
        record=current_docs["record"],
        speaker=current_docs["speaker"],
        publish=current_docs["publish"],
        speaker_bytes=current_payloads["speaker"],
        publish_bytes=current_payloads["publish"],
        aliases=aliases,
        mappings=environment.mappings,
        trusted_roots=environment.trusted_roots,
        virtual_files={},
    )
    return copy.deepcopy(dict(journal))


def relocate_slice_package(
    package_root: Path,
    *,
    candidate_id: str,
    source_package_root: str | Path,
    destination_package_root: str | Path,
    source_repo_root: str | Path,
    destination_repo_root: str | Path,
    evidence_root: Path | None = None,
    source_workspace_root: str | Path = "/home/ivan/Project",
) -> dict[str, Any]:
    """Relocate one materialized package, or recover its prepared transaction."""
    if not _SAFE_COMPONENT.fullmatch(candidate_id) or ".." in candidate_id:
        raise PackageRelocationError("candidate_id: unsafe component")
    environment = _build_relocation_environment(
        package_root,
        source_package_root=source_package_root,
        destination_package_root=destination_package_root,
        source_repo_root=source_repo_root,
        destination_repo_root=destination_repo_root,
        evidence_root=evidence_root,
        source_workspace_root=source_workspace_root,
    )
    package_root = environment.package_root
    evidence_root = environment.evidence_root
    workspace = environment.workspace
    mappings = environment.mappings
    trusted_roots = environment.trusted_roots
    roots = environment.roots
    record_path = package_root / f"{candidate_id}.record.json"
    speaker_path = package_root / f"{candidate_id}.recut.speaker-final.json"
    publish_path = package_root / f"{candidate_id}.recut.publish.json"
    journal_path = package_root / f".{candidate_id}.package-relocation.json"
    with _transaction_lock(package_root, candidate_id):
        record, record_payload = _load_json_document(record_path, label="record")
        speaker, speaker_payload = _load_json_document(speaker_path, label="speaker")
        publish, publish_payload = _load_json_document(publish_path, label="publish")
        if publish.get("candidate_id") != candidate_id:
            raise PackageRelocationError("publish: candidate_id mismatch")
        journal: dict[str, Any] | None = None
        if journal_path.exists() or journal_path.is_symlink():
            loaded, _payload = _load_json_document(journal_path, label="journal")
            journal = loaded
        current_payloads = {
            "record": record_payload,
            "speaker": speaker_payload,
            "publish": publish_payload,
        }
        evidence, aliases, virtual_evidence, chat_speaker_sha256 = (
            _load_relocation_evidence(
                package_root=package_root,
                candidate_id=candidate_id,
                record=record,
                current_payloads=current_payloads,
                journal=journal,
                evidence_root=evidence_root,
                trusted_roots=trusted_roots,
            )
        )
        chat_target = evidence["chat_authority"][0]
        context_target = evidence["clip_context"][0]
        preimage_payloads = {
            label: evidence[f"pre_relocation_{label}"][1]
            for label in _DOCUMENT_COMMIT_ORDER
        }
        preimage_docs = {
            label: _decode_json_document(
                payload, label=f"pre-relocation {label}"
            )
            for label, payload in preimage_payloads.items()
        }
        current_docs = {"record": record, "speaker": speaker, "publish": publish}
        document_paths = {
            "record": record_path,
            "speaker": speaker_path,
            "publish": publish_path,
        }
        if journal is not None:
            _validate_journal_contract(
                journal,
                candidate_id=candidate_id,
                roots=roots,
                document_paths=document_paths,
                current_payloads=current_payloads,
                evidence=evidence,
                chat_speaker_sha256=chat_speaker_sha256,
            )
        elif chat_speaker_sha256 != _sha256_bytes(speaker_payload):
            raise PackageRelocationError(
                "chat authority: speaker manifest does not bind pre-relocation bytes"
            )
        preimage_record = preimage_docs["record"]
        preimage_aliases = {
            **aliases,
            str(preimage_record.get("chat_authority_audit_path")): chat_target,
            str(preimage_record.get("clip_context_path")): context_target,
        }
        preimage_virtual_files = {
            **virtual_evidence,
            record_path.absolute(): preimage_payloads["record"],
            speaker_path.absolute(): preimage_payloads["speaker"],
            publish_path.absolute(): preimage_payloads["publish"],
        }
        _validate_graph(
            record=preimage_record,
            speaker=preimage_docs["speaker"],
            publish=preimage_docs["publish"],
            speaker_bytes=preimage_payloads["speaker"],
            publish_bytes=preimage_payloads["publish"],
            aliases=preimage_aliases,
            mappings=mappings,
            trusted_roots=trusted_roots,
            virtual_files=preimage_virtual_files,
        )

        committed = _committed_replay_receipt(
            journal=journal,
            evidence=evidence,
            current_docs=current_docs,
            current_payloads=current_payloads,
            preimage_docs=preimage_docs,
            preimage_payloads=preimage_payloads,
            chat_target=chat_target,
            context_target=context_target,
            environment=environment,
            aliases=aliases,
        )
        if committed is not None:
            return committed

        fresh_transaction = journal is None
        if fresh_transaction:
            _reject_unknown_wsl_paths(record, kind="record", source_workspace_root=workspace)
            _reject_unknown_wsl_paths(speaker, kind="speaker", source_workspace_root=workspace)
            _reject_unknown_wsl_paths(publish, kind="publish", source_workspace_root=workspace)
            _validate_graph(
                record=record,
                speaker=speaker,
                publish=publish,
                speaker_bytes=speaker_payload,
                publish_bytes=publish_payload,
                aliases=aliases,
                mappings=mappings,
                trusted_roots=trusted_roots,
                virtual_files=virtual_evidence,
            )
        else:
            document_rows = journal.get("documents")
            if not isinstance(document_rows, Mapping):
                raise PackageRelocationError("journal: documents missing")
            for label, payload in current_payloads.items():
                row = document_rows.get(label)
                digest = _sha256_bytes(payload)
                if not isinstance(row, Mapping) or digest not in {
                    row.get("before_sha256"),
                    row.get("after_sha256"),
                }:
                    raise PackageRelocationError(
                        f"journal: prepared {label} is neither before nor after"
                    )

        new_speaker = _transform_speaker(
            preimage_docs["speaker"],
            mappings=mappings,
            source_workspace_root=workspace,
        )
        new_speaker_payload = _json_bytes(new_speaker)
        new_publish = _transform_publish(
            preimage_docs["publish"],
            mappings=mappings,
            source_workspace_root=workspace,
        )
        new_publish_payload = _json_bytes(new_publish)
        new_record = _transform_record(
            preimage_docs["record"],
            speaker=new_speaker,
            speaker_sha256=_sha256_bytes(new_speaker_payload),
            publish=new_publish,
            publish_sha256=_sha256_bytes(new_publish_payload),
            chat_target=str(chat_target),
            context_target=str(context_target),
            mappings=mappings,
            source_workspace_root=workspace,
        )
        new_record_payload = _json_bytes(new_record)
        after_payloads = {
            "record": new_record_payload,
            "speaker": new_speaker_payload,
            "publish": new_publish_payload,
        }
        after_docs = {
            "record": new_record,
            "speaker": new_speaker,
            "publish": new_publish,
        }

        virtual_after = {
            **virtual_evidence,
            record_path.absolute(): new_record_payload,
            speaker_path.absolute(): new_speaker_payload,
            publish_path.absolute(): new_publish_payload,
        }
        after_aliases = {
            str(chat_target): chat_target,
            str(context_target): context_target,
        }
        _validate_graph(
            record=new_record,
            speaker=new_speaker,
            publish=new_publish,
            speaker_bytes=new_speaker_payload,
            publish_bytes=new_publish_payload,
            aliases=after_aliases,
            mappings=mappings,
            trusted_roots=trusted_roots,
            virtual_files=virtual_after,
        )

        if journal is None:
            documents: dict[str, Any] = {}
            for label in ("speaker", "publish", "record"):
                documents[label] = {
                    "path": {
                        "speaker": speaker_path.name,
                        "publish": publish_path.name,
                        "record": record_path.name,
                    }[label],
                    "before_sha256": _sha256_bytes(current_payloads[label]),
                    "after_sha256": _sha256_bytes(after_payloads[label]),
                    "changed_pointers": sorted(
                        _pointer_text(pointer)
                        for pointer in _changed_pointers(
                            current_docs[label], after_docs[label]
                        )
                    ),
                }
            journal = {
                "schema_version": SCHEMA_VERSION,
                "transaction_id": uuid.uuid4().hex,
                "candidate_id": candidate_id,
                "status": "PREPARED",
                "prepared_at": _now(),
                "roots": roots,
                "documents": documents,
                "staged_evidence": {
                    label: {
                        "path": target.name,
                        "sha256": _sha256_bytes(payload),
                    }
                    for label, (target, payload, _needs_stage) in evidence.items()
                },
                "speaker_manifest_lineage": {
                    "authority": "preserved_chat_pre_relocation_speaker_manifest",
                    "chat_speaker_manifest_sha256": chat_speaker_sha256,
                    "before_sha256": documents["speaker"]["before_sha256"],
                    "after_sha256": documents["speaker"]["after_sha256"],
                },
                "commit_order": list(_COMMIT_ORDER),
            }
            _validate_journal_contract(
                journal,
                candidate_id=candidate_id,
                roots=roots,
                document_paths=document_paths,
                current_payloads=current_payloads,
                evidence=evidence,
                chat_speaker_sha256=chat_speaker_sha256,
            )
        else:
            for label, payload in after_payloads.items():
                expected = (
                    ((journal.get("documents") or {}).get(label) or {}).get(
                        "after_sha256"
                    )
                )
                if _sha256_bytes(payload) != expected:
                    raise PackageRelocationError(
                        f"journal: recovered {label} output differs from prepared graph"
                    )

        _validate_journal_projection_rows(
            journal,
            before_docs=preimage_docs,
            before_payloads=preimage_payloads,
            after_docs=after_docs,
            after_payloads=after_payloads,
        )

        return _commit_relocation_graph(
            package_root=package_root,
            document_paths=document_paths,
            evidence=evidence,
            after_payloads=after_payloads,
            journal_path=journal_path,
            journal=journal,
            fresh_transaction=fresh_transaction,
            after_aliases=after_aliases,
            mappings=mappings,
            trusted_roots=trusted_roots,
        )
