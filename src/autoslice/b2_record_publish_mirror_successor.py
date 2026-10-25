"""Create a private B2 successor that repairs record/publish hash mirroring.

This is deliberately narrower than package finalization.  It copies one
already-built private review package create-only, archives package-instance
receipts from the source root, projects only runtime locators through the
existing B2 projector, makes the standalone publish artifact hash map equal to
the current record map, then binds the resulting publish bytes from the record.

No package audit, mechanical release gate, provider, image generation, media
render, upload, production-state transition, or public mutation occurs here.
The destination remains a private, unaudited successor until the ordinary
pipeline later consumes it.
"""
from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
from typing import Any, Callable, Mapping

from src.autoslice.package_import_capacity import copy_capacity
from src.autoslice.package_publish_mirror import validate_publish_staging_mirror
from src.autoslice.publication_readiness import _review_package_binding
from src.autoslice.review_evidence_store import review_from_bytes
from src.autoslice.title_cover_qc_locator_successor import (
    create_title_cover_qc_locator_successor,
)


SCHEMA_VERSION = "b2-record-publish-mirror-successor.v1"
CANDIDATE_ID = "auto_120029_1409_1578"
_HISTORY_NAMESPACE = PurePosixPath("history/e570-record-publish-mirror-source")
_CURRENT_GATE_NAMES = frozenset(
    {
        "package_audit.json",
        "title-cover-joint-qc.json",
        f"{CANDIDATE_ID}.title-cover-joint-qc.json",
        f"{CANDIDATE_ID}.mechanical-delivery-review.json",
    }
)


class B2RecordPublishMirrorSuccessorError(ValueError):
    """The source package cannot be projected without semantic mutation."""


def _error(code: str, detail: str) -> B2RecordPublishMirrorSuccessorError:
    return B2RecordPublishMirrorSuccessorError(f"{code}: {detail}")


def _sha_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha_file(path: Path) -> str:
    digest = _evidence_file_sha256(path)
    return digest


def _json_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _error("SUCCESSOR_JSON_INVALID", str(exc)) from exc


def _real_private_directory(path: Path, *, label: str) -> Path:
    absolute = path.absolute()
    try:
        resolved = path.resolve(strict=True)
        metadata = os.lstat(path)
    except OSError as exc:
        raise _error("SUCCESSOR_PATH_INVALID", f"{label} missing") from exc
    if (
        resolved != absolute
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise _error(
            "SUCCESSOR_PATH_INVALID", f"{label} must be owner-private 0700"
        )
    return absolute


def _regular_file(path: Path, *, root: Path, label: str) -> Path:
    absolute = path.absolute()
    try:
        absolute.relative_to(root)
        metadata = os.lstat(absolute)
        resolved = absolute.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error(
            "SUCCESSOR_PATH_INVALID", f"{label} unavailable or escapes package"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise _error("SUCCESSOR_PATH_INVALID", f"{label} is not a regular file")
    return resolved


def _read_json(
    path: Path, *, root: Path, label: str, review: bool = False
) -> dict[str, Any]:
    target = _regular_file(path, root=root, label=label)
    try:
        raw = target.read_bytes()
        value = (
            review_from_bytes(raw, directory=target.parent)
            if review
            else json.loads(raw.decode("utf-8"))
        )
    except (OSError, TypeError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise _error("SUCCESSOR_JSON_INVALID", f"{label}: {exc}") from exc
    if not isinstance(value, dict):
        raise _error("SUCCESSOR_JSON_INVALID", f"{label} root is not an object")
    return value


def _inventory(root: Path) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode):
            raise _error("SUCCESSOR_SOURCE_UNSAFE", f"symlink found: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise _error("SUCCESSOR_SOURCE_UNSAFE", f"non-regular file: {relative}")
        rows[relative] = {
            "sha256": _sha_file(path),
            "bytes": metadata.st_size,
            "mode": stat.S_IMODE(metadata.st_mode),
        }
    if not rows:
        raise _error("SUCCESSOR_SOURCE_EMPTY", str(root))
    return rows


def _load_source(source: Path) -> dict[str, Any]:
    manifest_path = _regular_file(
        source / "review_manifest.json", root=source, label="review manifest"
    )
    manifest = _read_json(manifest_path, root=source, label="review manifest")
    exact_ids = manifest.get("exact_candidate_ids")
    items = manifest.get("items")
    if exact_ids != [CANDIDATE_ID] or not isinstance(items, list) or len(items) != 1:
        raise _error("SUCCESSOR_MANIFEST_INVALID", "exact B2 candidate required")
    item = items[0]
    if not isinstance(item, Mapping) or item.get("candidate_id") != CANDIDATE_ID:
        raise _error("SUCCESSOR_MANIFEST_INVALID", "B2 item missing")
    manifest_bound, roles = _review_package_binding(source, CANDIDATE_ID)
    if not manifest_bound or not isinstance(roles, Mapping):
        raise _error("SUCCESSOR_MANIFEST_INVALID", "current artifact roles invalid")
    record = _read_json(
        Path(roles["record"]), root=source, label="record", review=True
    )
    publish = _read_json(
        Path(roles["publish"]), root=source, label="publish", review=True
    )
    chat_name = item.get("chat_authority")
    clip_name = item.get("clip_context")
    if not isinstance(chat_name, str) or not isinstance(clip_name, str):
        raise _error("SUCCESSOR_MANIFEST_INVALID", "chat/context locators missing")
    chat_path = _regular_file(source / chat_name, root=source, label="chat authority")
    clip_path = _regular_file(source / clip_name, root=source, label="clip context")
    chat = _read_json(chat_path, root=source, label="chat authority", review=True)
    if record.get("candidate_id") != CANDIDATE_ID or publish.get("candidate_id") != CANDIDATE_ID:
        raise _error("SUCCESSOR_DOCUMENT_INVALID", "candidate identity drift")
    validate_publish_staging_mirror(record, publish)
    record_hashes = record.get("artifact_hashes")
    publish_hashes = publish.get("artifact_hashes")
    if not isinstance(record_hashes, Mapping) or not isinstance(publish_hashes, Mapping):
        raise _error("SUCCESSOR_DOCUMENT_INVALID", "artifact hash maps missing")
    expected_publish_hashes = {
        key: value
        for key, value in record_hashes.items()
        if key != "publish_draft_sha256"
    }
    record_only = set(expected_publish_hashes) - set(publish_hashes)
    publish_only = set(publish_hashes) - set(expected_publish_hashes)
    if record_only != {"delivery_subtitle_sha256"} or publish_only != {
        "publish_draft_sha256"
    }:
        raise _error(
            "SUCCESSOR_HASH_KEY_DRIFT",
            "unexpected record/publish artifact hash key drift: "
            f"record_only={sorted(record_only)} publish_only={sorted(publish_only)}",
        )
    source_qc = source / f"{CANDIDATE_ID}.title-cover-joint-qc.json"
    _regular_file(source_qc, root=source, label="canonical title/cover QC")
    return {
        "manifest": manifest,
        "item": dict(item),
        "roles": dict(roles),
        "record": record,
        "publish": publish,
        "chat": chat,
        "chat_path": chat_path,
        "clip_path": clip_path,
        "source_qc": source_qc,
        "inventory": _inventory(source),
        "expected_publish_hashes": expected_publish_hashes,
    }


def _history_target(relative: str) -> PurePosixPath | None:
    path = PurePosixPath(relative)
    if (
        path.parts[0] == "verification"
        or relative in _CURRENT_GATE_NAMES
        or (
            len(path.parts) == 1
            and (
                relative.endswith(".record.json")
                or relative.endswith(".publish.json")
                or relative.endswith(".chat-authority.json")
            )
        )
    ):
        return _HISTORY_NAMESPACE / path
    return None


def _is_projected_current_document(relative: str) -> bool:
    path = PurePosixPath(relative)
    return bool(
        len(path.parts) == 1
        and (
            relative.endswith(".record.json")
            or relative.endswith(".publish.json")
            or relative.endswith(".chat-authority.json")
        )
    )


def _copy_file(source: Path, target: Path, *, mode: int) -> None:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise _error("SUCCESSOR_TARGET_EXISTS", str(target))
    with source.open("rb") as reader, target.open("xb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    os.chmod(target, mode)


def _copy_source(source: Path, output: Path, inventory: Mapping[str, Mapping[str, object]]) -> None:
    output.mkdir(mode=0o700)
    for relative, metadata in inventory.items():
        history = _history_target(relative)
        if relative in {"review_manifest.json"}:
            targets = [output / relative, output / _HISTORY_NAMESPACE / relative]
        elif history is not None:
            targets = [output / history]
            if _is_projected_current_document(relative):
                targets.insert(0, output / relative)
        else:
            targets = [output / relative]
        for target in targets:
            _copy_file(
                source / relative,
                target,
                mode=int(metadata["mode"]),
            )
            if (
                _sha_file(target) != metadata["sha256"]
                or target.stat().st_size != metadata["bytes"]
            ):
                raise _error("SUCCESSOR_COPY_DRIFT", relative)
    verification = output / "verification"
    verification.mkdir(mode=0o700, exist_ok=True)
    os.chmod(verification, 0o700)


def _audio_paths(output: Path, record: Mapping[str, Any]) -> dict[str, Path]:
    correspondence = record.get("subtitle_audio_correspondence")
    if not isinstance(correspondence, Mapping):
        raise _error("SUCCESSOR_AUDIO_BINDING_INVALID", "correspondence missing")
    result: dict[str, Path] = {}
    for key, alias in (
        ("witness_srt_path", "audio_witness"),
        ("provenance_path", "audio_provenance"),
        ("correspondence_path", "audio_correspondence"),
        ("raw_result_path", "audio_raw"),
    ):
        value = correspondence.get(key)
        if not isinstance(value, str) or Path(value).name != Path(value).name:
            raise _error("SUCCESSOR_AUDIO_BINDING_INVALID", key)
        target = output / Path(value).name
        if not target.is_file() or target.is_symlink():
            raise _error("SUCCESSOR_AUDIO_BINDING_INVALID", str(target))
        result[alias] = target
    return result


def _paths(output: Path, item: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Path]:
    required = {
        "record": item.get("record"),
        "publish": item.get("publish_json"),
        "chat": item.get("chat_authority"),
        "clip": item.get("clip_context"),
        "plain_srt": item.get("subtitle_srt"),
        "final_video": item.get("video"),
        "ass": item.get("ass_path"),
        "cover": item.get("cover"),
        "ai_background": item.get("cover_pre_overlay"),
        "title_mask": item.get("cover_title_mask"),
        "builtin_provenance": item.get("cover_builtin_provenance"),
    }
    host = item.get("host_only_v4_binding")
    if not isinstance(host, Mapping):
        raise _error("SUCCESSOR_MANIFEST_INVALID", "host-only binding missing")
    required["reference"] = host.get("reference_path")
    paths: dict[str, Path] = {}
    for key, value in required.items():
        if not isinstance(value, str) or not value:
            raise _error("SUCCESSOR_MANIFEST_INVALID", f"item.{key} missing")
        target = output / value
        if key not in {"record", "publish", "chat"} and (
            not target.is_file() or target.is_symlink()
        ):
            raise _error("SUCCESSOR_ARTIFACT_MISSING", str(target))
        paths[key] = target
    paths["raw_video"] = paths["final_video"]
    paths.update(_audio_paths(output, record))
    return paths


def _default_projector(
    *,
    source: Path,
    output: Path,
    record: dict[str, Any],
    publish: dict[str, Any],
    chat: dict[str, Any],
    authority: dict[str, object],
    paths: Mapping[str, Path],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    from scripts.finalize_b2_caption_same_bv_recovery_package import (
        _project_documents,
    )

    return _project_documents(
        source=source,
        output=output,
        record=record,
        publish=publish,
        chat=chat,
        authority=authority,
        paths=paths,
    )


def _atomic_projected_write(path: Path, raw: bytes) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise _error("SUCCESSOR_PROJECTED_PREIMAGE_MISSING", str(path)) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise _error("SUCCESSOR_PROJECTED_PREIMAGE_INVALID", str(path))
    temporary = path.with_name(f".{path.name}.projected.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise _error("SUCCESSOR_TARGET_EXISTS", str(temporary))
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


def _write_documents(
    *,
    paths: Mapping[str, Path],
    record: dict[str, Any],
    publish: dict[str, Any],
    chat: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    chat_raw = _json_bytes(chat)
    _atomic_projected_write(paths["chat"], chat_raw)
    chat_sha = "sha256:" + _sha_bytes(chat_raw)
    publish_hashes = publish.get("artifact_hashes")
    record_hashes = record.get("artifact_hashes")
    if not isinstance(publish_hashes, dict) or not isinstance(record_hashes, dict):
        raise _error("SUCCESSOR_DOCUMENT_INVALID", "artifact hash maps missing")
    publish_hashes["chat_authority_audit_sha256"] = chat_sha
    record_hashes["chat_authority_audit_sha256"] = chat_sha
    publish_raw = _json_bytes(publish)
    _atomic_projected_write(paths["publish"], publish_raw)
    record_hashes["publish_draft_sha256"] = "sha256:" + _sha_bytes(publish_raw)
    record_raw = _json_bytes(record)
    _atomic_projected_write(paths["record"], record_raw)
    return (
        json.loads(record_raw),
        json.loads(publish_raw),
        json.loads(chat_raw),
    )


def _verify_projection(
    *,
    source_docs: Mapping[str, Any],
    output: Path,
    paths: Mapping[str, Path],
    record: Mapping[str, Any],
    publish: Mapping[str, Any],
    chat: Mapping[str, Any],
) -> dict[str, object]:
    validate_publish_staging_mirror(record, publish)
    record_hashes = record.get("artifact_hashes")
    publish_hashes = publish.get("artifact_hashes")
    if not isinstance(record_hashes, Mapping) or not isinstance(publish_hashes, Mapping):
        raise _error("SUCCESSOR_DOCUMENT_INVALID", "projected hashes missing")
    without_publish = {
        key: value
        for key, value in record_hashes.items()
        if key != "publish_draft_sha256"
    }
    if without_publish != dict(publish_hashes):
        raise _error("SUCCESSOR_MIRROR_FAILED", "artifact hash maps still differ")
    if record_hashes.get("publish_draft_sha256") != (
        "sha256:" + _sha_file(paths["publish"])
    ):
        raise _error("SUCCESSOR_MIRROR_FAILED", "publish draft binding is stale")
    source_record = source_docs["record"]
    for field in (
        "story_contract",
        "boundary_audit",
        "exact_final_review",
        "recovery_publication_authority",
    ):
        if record.get(field) != source_record.get(field):
            raise _error("SUCCESSOR_SEMANTIC_MUTATION", field)
    # The production B2 projector separately freezes the inner subtitle/audio
    # semantic receipt and chat final-review authority while rebinding their
    # package locators. Requiring the locator-bearing outer objects to remain
    # byte-equal here would reject that legitimate projection.
    if paths["final_video"].read_bytes() != Path(source_docs["roles"]["video"]).read_bytes():
        raise _error("SUCCESSOR_MEDIA_MUTATION", "final video")
    return {
        "record_sha256": _sha_file(paths["record"]),
        "publish_sha256": _sha_file(paths["publish"]),
        "chat_sha256": _sha_file(paths["chat"]),
        "artifact_hash_maps_equal": True,
        "semantic_receipts_unchanged": True,
    }


def _attach_final_media_blocker(
    *, source: Path, output: Path
) -> dict[str, object]:
    """Rebind the existing exact clock and persist an UNASSESSED blocker.

    The source package already carries the E557 exact clock.  Reuse those
    measured values without ffprobe, decode, audio extraction or provider
    dispatch; the new manifest/job/state remain destination-bound.
    """

    from src.autoslice import final_human_review as review
    from src.autoslice.final_media_review_bootstrap import (
        bootstrap_final_media_review_job,
    )
    from src.autoslice.final_media_review_inputs import (
        assess_review_inputs,
        consume_review_job,
    )

    candidate = CANDIDATE_ID
    source_verification = source / "verification"
    source_job_path = (
        source_verification / f"{candidate}.final-media-review-job.json"
    )
    source_clock_path = (
        source_verification / f"{candidate}.final-media-review-clock.json"
    )
    source_job = _read_json(
        source_job_path, root=source, label="source final-media job"
    )
    source_clock = _read_json(
        source_clock_path, root=source, label="source exact-media clock"
    )
    media_clock = source_job.get("media_clock")
    source_bindings = source_clock.get("source_bindings")
    source_video = (
        source_bindings.get("source_video")
        if isinstance(source_bindings, Mapping)
        else None
    )
    format_row = (
        source_video.get("ffprobe_format")
        if isinstance(source_video, Mapping)
        else None
    )
    bounds = (
        source_video.get("decoded_frame_bounds")
        if isinstance(source_video, Mapping)
        else None
    )
    if not (
        isinstance(media_clock, Mapping)
        and isinstance(source_video, Mapping)
        and isinstance(format_row, Mapping)
        and isinstance(bounds, Mapping)
    ):
        raise _error(
            "SUCCESSOR_FINAL_MEDIA_CLOCK_INVALID", "source clock fields missing"
        )
    manifest = _read_json(
        output / "review_manifest.json",
        root=output,
        label="destination review manifest",
    )
    order, closures = review._manifest_items(manifest, package_root=output)
    contract_sha, contracts = review._review_contracts()
    if order != [candidate] or candidate not in contracts:
        raise _error(
            "SUCCESSOR_FINAL_MEDIA_CONTRACT_INVALID", "B2 contract missing"
        )
    video = output / str(closures[candidate]["artifacts"]["video"])
    if not (
        _sha_file(video) == str(media_clock.get("source_video_sha256"))
        and video.stat().st_size == media_clock.get("source_video_bytes")
    ):
        raise _error(
            "SUCCESSOR_FINAL_MEDIA_CLOCK_INVALID", "video identity differs"
        )
    measured = {
        "duration_us": int(media_clock["duration_us"]),
        "first_video_pts_us": int(media_clock["first_video_pts_us"]),
        "last_video_pts_us": int(media_clock["last_video_pts_us"]),
        "video_frame_count": int(media_clock["video_frame_count"]),
        "format_duration_seconds": str(format_row["duration"]),
        "stream_start_seconds": "REUSED_EXACT_CLOCK_EVIDENCE",
        "stream_duration_seconds": "REUSED_EXACT_CLOCK_EVIDENCE",
    }
    calls = {"probe": 0}

    def probe(_video: Path) -> dict[str, object]:
        calls["probe"] += 1
        return dict(measured)

    bootstrap = bootstrap_final_media_review_job(
        package_root=output,
        candidate_id=candidate,
        closure=closures[candidate],
        review_contract_sha256=contract_sha,
        review_points=contracts[candidate],
        probe=probe,
    )
    job_path = Path(str(bootstrap["job_path"]))
    job = _read_json(job_path, root=output, label="destination final-media job")
    assessment = assess_review_inputs(job, allowed_root=output)
    state_path = (
        output / "verification" / f"{candidate}.final-media-review-state.json"
    )
    consumed = consume_review_job(job_path, state_path, allowed_root=output)
    state = consumed["state"]
    if not (
        calls["probe"] in {0, 1}
        and assessment.get("content_review_status") == "UNASSESSED"
        and state.get("status") == "BLOCKED_INPUT"
        and consumed.get("provider_called") is False
    ):
        raise _error(
            "SUCCESSOR_FINAL_MEDIA_STATE_INVALID",
            "provider-free blocker did not close as expected",
        )
    return {
        "bootstrap_status": bootstrap["status"],
        "job_path": job_path.relative_to(output).as_posix(),
        "job_sha256": _sha_file(job_path),
        "state_path": state_path.relative_to(output).as_posix(),
        "state_sha256": _sha_file(state_path),
        "state_status": state["status"],
        "content_review_status": assessment["content_review_status"],
        "reason_codes": list(state["reason_codes"]),
        "probe_calls": calls["probe"],
        "ffprobe_calls": 0,
        "decode_calls": 0,
        "audio_extraction_calls": 0,
        "provider_calls": 0,
    }


def plan_successor(source_package: Path, output: Path) -> dict[str, object]:
    source = _real_private_directory(source_package, label="source package")
    parent = _real_private_directory(output.parent, label="output parent")
    target = output.absolute()
    if target.parent != parent or target.exists() or target.is_symlink():
        raise _error("SUCCESSOR_CREATE_ONLY_REQUIRED", str(target))
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise _error("SUCCESSOR_PATH_INVALID", "source/destination overlap")
    loaded = _load_source(source)
    total = sum(int(row["bytes"]) for row in loaded["inventory"].values())
    capacity = copy_capacity(
        [
            {
                "destination": str(target),
                "status": "WOULD_COPY",
                "bytes": total + 64 * 1024 * 1024,
                "role": "B2_RECORD_PUBLISH_MIRROR_SUCCESSOR",
            }
        ]
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "WOULD_CREATE_PRIVATE_SUCCESSOR",
        "source_package": str(source),
        "output": str(target),
        "candidate_id": CANDIDATE_ID,
        "source_inventory_files": len(loaded["inventory"]),
        "source_inventory_bytes": total,
        "capacity": capacity,
        "provider_calls": 0,
        "package_audit_calls": 0,
        "mechanical_review_calls": 0,
        "upload_calls": 0,
    }


def prepare_successor(
    source_package: Path,
    output: Path,
    *,
    projector: Callable[..., tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = _default_projector,
    qc_creator: Callable[..., dict] = create_title_cover_qc_locator_successor,
    final_media_binder: Callable[..., dict[str, object]] = _attach_final_media_blocker,
) -> dict[str, object]:
    plan = plan_successor(source_package, output)
    source = Path(str(plan["source_package"]))
    target = Path(str(plan["output"]))
    loaded = _load_source(source)
    _copy_source(source, target, loaded["inventory"])
    paths = _paths(target, loaded["item"], loaded["record"])
    authority = loaded["item"].get("recovery_publication_authority")
    if not isinstance(authority, dict):
        raise _error("SUCCESSOR_MANIFEST_INVALID", "publication authority missing")
    try:
        record, publish, chat = projector(
            source=source,
            output=target,
            record=deepcopy(loaded["record"]),
            publish=deepcopy(loaded["publish"]),
            chat=deepcopy(loaded["chat"]),
            authority=deepcopy(authority),
            paths=paths,
        )
    except Exception as exc:
        failure = {
            "schema_version": SCHEMA_VERSION,
            "status": "PRIVATE_SUCCESSOR_PREPARATION_FAILED",
            "candidate_id": CANDIDATE_ID,
            "source_package": str(source),
            "output": str(target),
            "error_type": type(exc).__name__,
            "detail": str(exc)[:1000],
            "provider_calls": 0,
            "package_audit_calls": 0,
            "mechanical_review_calls": 0,
            "upload_calls": 0,
            "production_adopted": False,
        }
        failure_path = target / "verification/b2-record-publish-mirror-successor.failed.json"
        failure_path.write_bytes(_json_bytes(failure))
        os.chmod(failure_path, 0o600)
        raise _error("SUCCESSOR_PROJECTOR_FAILED", str(exc)) from exc
    record_staging = record.get("publish_staging")
    if not isinstance(record_staging, dict):
        raise _error("SUCCESSOR_DOCUMENT_INVALID", "publish_staging missing")
    record_staging["video_path"] = str(paths["final_video"])
    record_staging["subtitle_path"] = str(paths["plain_srt"])
    record_hashes = record.get("artifact_hashes")
    if not isinstance(record_hashes, dict):
        raise _error("SUCCESSOR_DOCUMENT_INVALID", "record hashes missing")
    publish["artifact_hashes"] = {
        key: value for key, value in record_hashes.items() if key != "publish_draft_sha256"
    }
    record, publish, chat = _write_documents(
        paths=paths, record=record, publish=publish, chat=chat
    )
    projection = _verify_projection(
        source_docs=loaded,
        output=target,
        paths=paths,
        record=record,
        publish=publish,
        chat=chat,
    )
    qc_output = target / f"{CANDIDATE_ID}.title-cover-joint-qc.json"
    qc = qc_creator(
        source_package_root=source,
        source_receipt_path=loaded["source_qc"],
        destination_package_root=target,
        output_path=qc_output,
        title=str(loaded["item"]["title"]),
    )
    if qc.get("pass") is not True or qc.get("status") != "PASS":
        raise _error("SUCCESSOR_QC_INVALID", "projected title/cover QC did not pass")
    source_after = _inventory(source)
    if source_after != loaded["inventory"]:
        raise _error("SUCCESSOR_SOURCE_MUTATED", str(source))
    final_media = final_media_binder(source=source, output=target)
    if not (
        isinstance(final_media, Mapping)
        and final_media.get("content_review_status") == "UNASSESSED"
        and final_media.get("provider_calls") == 0
    ):
        raise _error(
            "SUCCESSOR_FINAL_MEDIA_STATE_INVALID",
            "final-media binder overclaimed review or provider activity",
        )
    receipt: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "status": "PRIVATE_SUCCESSOR_PREPARED_AUDIT_AND_MECHANICAL_PENDING",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "candidate_id": CANDIDATE_ID,
        "source_package": str(source),
        "source_manifest_sha256": _sha_file(source / "review_manifest.json"),
        "source_record_sha256": _sha_file(Path(loaded["roles"]["record"])),
        "source_publish_sha256": _sha_file(Path(loaded["roles"]["publish"])),
        "output": str(target),
        "record": {
            "path": paths["record"].name,
            "sha256": projection["record_sha256"],
            "bytes": paths["record"].stat().st_size,
        },
        "publish": {
            "path": paths["publish"].name,
            "sha256": projection["publish_sha256"],
            "bytes": paths["publish"].stat().st_size,
        },
        "chat_authority": {
            "path": paths["chat"].name,
            "sha256": projection["chat_sha256"],
            "bytes": paths["chat"].stat().st_size,
        },
        "title_cover_qc": {
            "path": qc_output.name,
            "sha256": _sha_file(qc_output),
            "bytes": qc_output.stat().st_size,
            "pass": True,
            "provider_calls": 0,
        },
        "artifact_hash_maps_equal": True,
        "semantic_receipts_unchanged": True,
        "source_files_unchanged": True,
        "source_instance_gates_archived_under": str(_HISTORY_NAMESPACE),
        "final_media": dict(final_media),
        "content_review_status": final_media["content_review_status"],
        "package_audit_status": "NOT_RUN",
        "mechanical_review_status": "NOT_RUN",
        "provider_calls": 0,
        "image_generation_calls": 0,
        "media_renders": 0,
        "upload_calls": 0,
        "production_adopted": False,
        "upload_allowed": False,
    }
    receipt_path = target / "verification/b2-record-publish-mirror-successor.json"
    receipt_path.write_bytes(_json_bytes(receipt))
    os.chmod(receipt_path, 0o600)
    receipt["receipt_path"] = str(receipt_path)
    receipt["receipt_sha256"] = _sha_file(receipt_path)
    return receipt


__all__ = [
    "B2RecordPublishMirrorSuccessorError",
    "CANDIDATE_ID",
    "SCHEMA_VERSION",
    "plan_successor",
    "prepare_successor",
]
