"""Verified song-delivery machinery: atomic hash-bound package commit + staging.

Extracted from scripts/free_session_autoslice.py (2026-07-15 屎山治理第四刀).
The atomic-delivery / staging / active-record-commit functions plus their
tightly-coupled SongDeliveryError + _sha256_regular_file move here as one unit.
Runner globals (BASE, REPO_ROOT, safe_name, the song_completion_evidence
wrapper) resolve at CALL TIME through ``_runner`` so monkeypatching them on the
runner steers this code; the import cycle is safe (reference stored at import
time, dereferenced only inside function bodies). The runner re-imports every
name, so its call sites — and cover_repair's ``_runner.SongDeliveryError`` /
``_runner._sha256_regular_file`` — keep resolving unchanged.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

import scripts.free_session_autoslice as _runner
from src.autoslice.verified_io import (
    _matches_sha256,
    _normalized_sha256,
    _read_json_object,
    _document_video_hash,
)


def record_is_song(entry: dict) -> bool:
    """The window IS a song when the in-window recall classified it as one
    (semanticsong_* record id) OR the LRC lane pinned/aligned it.  Keep that
    upstream anchor through later selector retries; final delivery still needs
    independent positive boundary and lyric evidence and otherwise fails closed."""
    job = entry.get("source_context_job") or {}
    return (
        str(entry.get("candidate_id") or "").startswith("semanticsong")
        or job.get("content_type_hint") == "song"
        or job.get("song_candidate") is True
        or job.get("requires_full_source_song_boundary_redo") is True
        or bool(job.get("song_boundary"))
        or bool(job.get("lyrics_alignment"))
    )


def song_delivery_artifacts(record: dict) -> dict:
    """Best-known materialized artifacts for a song record, with sha256 hashes
    whenever the pipeline recorded them (hash hygiene stays; SEMANTIC gating
    does not — the delivery decision is song_delivery_ok).  A missing/blocked
    cover never blocks the video: covers are generated after the release gate
    now, and repair_covers backfills delivered clips."""
    recut = record.get("materialized_recut")
    if not isinstance(recut, dict):
        return {}
    out: dict = {}
    burned = recut.get("burned_preview")
    if isinstance(burned, dict) and burned.get("status") == "BURNED" and burned.get("path"):
        out["video_path"] = str(burned["path"])
        if burned.get("burned_sha256"):
            out["video_sha256"] = str(burned["burned_sha256"])
    recut_hashes = recut.get("artifact_hashes")
    if isinstance(recut_hashes, dict) and recut_hashes.get("burned_video_sha256"):
        out["video_sha256"] = str(recut_hashes["burned_video_sha256"])
    if recut.get("subtitle_path"):
        out["subtitle_path"] = str(recut["subtitle_path"])
        if isinstance(recut_hashes, dict) and recut_hashes.get("subtitle_sha256"):
            out["subtitle_sha256"] = str(recut_hashes["subtitle_sha256"])
    if recut.get("manifest_path"):
        out["recut_manifest_path"] = str(recut["manifest_path"])
        if recut.get("manifest_sha256"):
            out["recut_manifest_sha256"] = str(recut["manifest_sha256"])
    gate = recut.get("cover_release_gate")
    if isinstance(gate, dict):
        out["cover_release_gate_satisfied"] = gate.get("satisfied")
        out["release_gate_path"] = str(gate.get("path") or "")
        gate_hashes = gate.get("artifact_hashes")
        if isinstance(gate_hashes, dict) and gate_hashes.get("burned_video_sha256"):
            out["video_sha256"] = str(gate_hashes["burned_video_sha256"])
    staging = recut.get("publish_staging")
    if isinstance(staging, dict) and staging.get("status") == "STAGED":
        if staging.get("title") or record.get("title"):
            out["title"] = str(staging.get("title") or record.get("title") or "")
        if staging.get("cover_path"):
            out["cover_path"] = str(staging["cover_path"])
            recut_hashes = recut.get("artifact_hashes")
            if isinstance(recut_hashes, dict) and recut_hashes.get("cover_sha256"):
                out["cover_sha256"] = str(recut_hashes["cover_sha256"])
    return out


def _write_song_active_record(
    summary_record: dict,
    *,
    delivery_candidate_id: str,
    title: str,
    video_sha256: str,
    summary_authority_root: Path,
) -> tuple[Path, str]:
    """Persist the invocation-owned materialized song record next to its
    publish draft so later cover repair has the same exact active-document
    authority as talk delivery.  The delivery copy is included in the verified
    song manifest; this source copy remains in the immutable selector attempt."""

    if summary_authority_root.is_symlink():
        raise SongDeliveryError("song summary authority root may not be a symlink")
    try:
        authority_root = summary_authority_root.resolve(strict=True)
    except OSError as exc:
        raise SongDeliveryError(f"song summary authority root is missing: {exc}") from exc
    if not authority_root.is_dir():
        raise SongDeliveryError("song summary authority root is not a directory")

    materialized = summary_record.get("materialized_recut")
    if not isinstance(materialized, dict):
        raise SongDeliveryError("song summary has no materialized_recut record")
    record = copy.deepcopy(materialized)
    staging = record.get("publish_staging")
    if not isinstance(staging, dict):
        raise SongDeliveryError("song materialized record has no publish_staging")
    source_candidate_id = str(summary_record.get("candidate_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", source_candidate_id):
        raise SongDeliveryError("song active record has an unsafe source candidate id")

    # A verified song can legitimately reach the runner while the generic
    # publish/cover release gate is still closed.  In particular,
    # SONG_FULL_BOUNDARY_READY is a positive song proof, but the generic gate
    # deliberately requires *zero* reason codes.  Do not weaken that gate or
    # pretend a cover exists.  Instead materialize the minimum no-upload
    # publish authority required by verified delivery and later cover repair.
    # This branch is intentionally narrow: any semantic/content blocker still
    # fails closed and cannot manufacture a publish draft.
    publish_value = staging.get("publish_json_path")
    if not isinstance(publish_value, str) or not publish_value:
        gate = record.get("cover_release_gate")
        gate_reasons = (
            [str(code) for code in gate.get("reason_codes", [])]
            if isinstance(gate, dict) and isinstance(gate.get("reason_codes"), list)
            else []
        )
        deferred_decision = str(staging.get("decision_action") or "")
        if (
            staging.get("status") != "SKIPPED_RELEASE_GATE"
            or deferred_decision not in {"AUTO_UPLOAD", "AUTO_RECUT"}
            or staging.get("upload_enabled") is not False
            or not isinstance(gate, dict)
            or gate.get("schema_version") != "slice-cover-release-gate.v1"
            or gate.get("candidate_id") != source_candidate_id
            or gate.get("decision_action") != deferred_decision
            or gate.get("satisfied") is not False
            or gate_reasons != ["SONG_FULL_BOUNDARY_READY"]
            or summary_record.get("decision_action") != deferred_decision
            or [str(code) for code in summary_record.get("reason_codes", [])]
            != ["SONG_FULL_BOUNDARY_READY"]
        ):
            raise SongDeliveryError(
                "song active publish draft missing outside the verified deferred-cover case"
            )
        media_value = record.get("media_path")
        manifest_value = record.get("manifest_path")
        burned = record.get("burned_preview")
        burned_value = burned.get("path") if isinstance(burned, dict) else None
        if not all(isinstance(value, str) and value for value in (media_value, manifest_value, burned_value)):
            raise SongDeliveryError("song deferred publish authority is missing materialized paths")
        try:
            media_path = Path(str(media_value)).resolve(strict=True)
            manifest_path = Path(str(manifest_value)).resolve(strict=True)
            burned_path = Path(str(burned_value)).resolve(strict=True)
        except OSError as exc:
            raise SongDeliveryError(f"song deferred publish materialized path is missing: {exc}") from exc
        artifact_root = manifest_path.parent
        if (
            media_path.parent != artifact_root
            or burned_path.parent != artifact_root
            or not artifact_root.is_relative_to(authority_root)
            or not manifest_path.name.endswith(".recut.manifest.json")
            or not media_path.name.endswith(".recut.mp4")
            or not burned_path.name.endswith(".mp4")
            or not _matches_sha256(burned_path, video_sha256)
        ):
            raise SongDeliveryError("song deferred publish artifact-root/video binding mismatch")
        publish_path = media_path.with_suffix(".publish.json")
        if publish_path.parent != artifact_root or publish_path.is_symlink():
            raise SongDeliveryError("song deferred publish path escapes the materialized artifact root")

        cover_text = title.removeprefix("【李豆沙】豆沙歌，").strip() or title
        cover_generation = {
            "workflow": "verified-song-delivery-deferred-cover.v1",
            "status": "BLOCKED",
            "detail": (
                "cover generation deferred until the hash-bound no-upload song "
                "delivery authority exists"
            ),
            "attempted_models": [],
        }
        artifact_hashes = copy.deepcopy(record.get("artifact_hashes"))
        if not isinstance(artifact_hashes, dict):
            raise SongDeliveryError("song deferred publish artifact_hashes is not an object")
        publish = {
            "schema_version": "shadow-publish-draft.v1",
            "candidate_id": source_candidate_id,
            "upload_enabled": False,
            "title": title,
            "title_source": "runner_verified_song_fallback",
            "title_policy_violations": [],
            "video_path": str(media_path),
            "cover_text": cover_text,
            "cover_path": None,
            "cover_status": "BLOCKED_AI_COVER_REQUIRED",
            "cover_generation": cover_generation,
            "reason_codes": ["CPA_AI_COVER_REQUIRED"],
            "artifact_hashes": artifact_hashes,
            "delivery_authority": {
                "schema_version": "verified-song-deferred-cover-authority.v1",
                "release_gate_path": str(gate.get("path") or staging.get("release_gate_path") or ""),
                "release_gate_satisfied": False,
                "release_gate_reason_codes": gate_reasons,
                "upload_enabled": False,
            },
        }
        if publish_path.exists():
            existing_publish = _read_json_object(
                publish_path, label="existing deferred song publish draft"
            )
            if existing_publish != publish:
                raise SongDeliveryError("conflicting deferred song publish draft already exists")
        else:
            _runner._atomic_write_json_file(publish_path, publish)
        record["publish_staging"] = {
            "status": "STAGED",
            "title": title,
            "title_source": "runner_verified_song_fallback",
            "title_policy_violations": [],
            "cover_status": "BLOCKED_AI_COVER_REQUIRED",
            "cover_path": None,
            "cover_text": cover_text,
            "cover_generation": cover_generation,
            "reason_codes": ["CPA_AI_COVER_REQUIRED"],
            "publish_json_path": str(publish_path),
            "release_gate_path": str(gate.get("path") or staging.get("release_gate_path") or ""),
            "upload_enabled": False,
        }
        staging = record["publish_staging"]
        publish_value = str(publish_path)

    if (
        staging.get("title") != title
        or staging.get("upload_enabled") is not False
        or not isinstance(publish_value, str)
        or _document_video_hash(record) != video_sha256
    ):
        raise SongDeliveryError("song active record title/video/upload binding mismatch")
    publish_path = Path(publish_value)
    if publish_path.is_symlink():
        raise SongDeliveryError("song active publish draft may not be a symlink")
    try:
        publish_path = publish_path.resolve(strict=True)
    except OSError as exc:
        raise SongDeliveryError(f"song active publish draft is missing: {exc}") from exc
    if not publish_path.is_relative_to(authority_root):
        raise SongDeliveryError("song active publish draft escapes the bound summary attempt")
    publish = _read_json_object(publish_path, label="song active publish draft")
    if (
        publish.get("schema_version") != "shadow-publish-draft.v1"
        or publish.get("candidate_id") != source_candidate_id
        or publish.get("title") != title
        or publish.get("upload_enabled") is not False
        or _document_video_hash(publish) != video_sha256
    ):
        raise SongDeliveryError("song active publish candidate/title/video/upload binding mismatch")
    if publish_path.name.endswith(".recut.publish.json"):
        record_path = publish_path.with_name(
            publish_path.name[: -len(".recut.publish.json")] + ".record.json"
        )
    elif publish_path.name.endswith(".publish.json"):
        record_path = publish_path.with_name(
            publish_path.name[: -len(".publish.json")] + ".record.json"
        )
    else:
        raise SongDeliveryError("song active publish draft has an unexpected filename")
    if record_path.is_symlink() or not record_path.parent.resolve(strict=True).is_relative_to(
        authority_root
    ):
        raise SongDeliveryError("song active record escapes the bound summary attempt")
    record["delivery_candidate_id"] = delivery_candidate_id
    record["source_candidate_id"] = source_candidate_id
    _runner._atomic_write_json_file(record_path, record)
    return record_path, "sha256:" + _sha256_regular_file(record_path)


def _commit_verified_song_package(
    *,
    date: str,
    delivery_candidate_id: str,
    summary_record: dict,
    title: str,
    selector_rc: int,
    summary_authority_root: Path,
) -> dict:
    """Commit one already-proven song without rerunning ASR/LRC/AGY.

    The same function is used by the fresh selector path and by bounded
    crash/packaging recovery.  It re-verifies every positive song proof and
    every artifact hash before exposing the manifest-last public package.
    Covers remain optional and upload is always disabled.
    """

    if selector_rc != 0:
        raise SongDeliveryError("song selector did not exit successfully")
    if not isinstance(title, str) or not title.strip():
        raise SongDeliveryError("verified song package has no title")
    title = title.strip()
    if summary_authority_root.is_symlink():
        raise SongDeliveryError("song summary authority root may not be a symlink")
    try:
        authority_root = summary_authority_root.resolve(strict=True)
        candidate_root = (_runner.BASE / "out" / date / delivery_candidate_id).resolve(strict=True)
    except OSError as exc:
        raise SongDeliveryError(f"song summary/candidate authority root is missing: {exc}") from exc
    if not authority_root.is_relative_to(candidate_root):
        raise SongDeliveryError("song summary authority root escapes the outer candidate")
    reasons = [str(code) for code in summary_record.get("reason_codes", [])]
    completion = _runner.song_completion_evidence(summary_record)
    if not _runner.song_delivery_ok(
        selector_rc,
        record_is_song(summary_record),
        reasons,
        completion,
    ):
        raise SongDeliveryError("song proof chain is not currently delivery-ready")

    artifacts = song_delivery_artifacts(summary_record)
    video_value = artifacts.get("video_path")
    video_sha256 = artifacts.get("video_sha256")
    if not isinstance(video_value, str) or not isinstance(video_sha256, str):
        raise SongDeliveryError("verified song has no hash-bound burned video")
    burned = Path(video_value)
    if not _matches_sha256(burned, video_sha256):
        raise SongDeliveryError("verified song burned video hash mismatch")

    required_sources = (
        (
            "subtitle",
            artifacts.get("subtitle_path"),
            artifacts.get("subtitle_sha256"),
            ".srt",
        ),
        (
            "lyrics_alignment_report",
            completion.get("alignment_report_path"),
            completion.get("alignment_report_sha256"),
            ".lyrics-alignment-report.json",
        ),
        (
            "host_vocal_proof",
            completion.get("host_vocal_proof_path"),
            completion.get("host_vocal_proof_sha256"),
            ".host-vocal-proof.json",
        ),
        (
            "recut_manifest",
            artifacts.get("recut_manifest_path"),
            artifacts.get("recut_manifest_sha256"),
            ".recut.manifest.json",
        ),
    )
    for role, source_value, sha_value, _suffix in required_sources:
        if not isinstance(source_value, str) or not isinstance(sha_value, str):
            raise SongDeliveryError(f"missing hash-bound delivery sidecar: {role}")
        if not _matches_sha256(Path(source_value), sha_value):
            raise SongDeliveryError(f"hash-bound delivery sidecar drifted: {role}")

    name = _song_delivery_basename(title, delivery_candidate_id)
    delivery = _runner.REPO_ROOT / "lidousha" / date
    delivery.mkdir(parents=True, exist_ok=True)
    specs: dict[str, tuple[Path, Path, str]] = {
        "video": (burned, delivery / f"{name}.mp4", video_sha256),
    }
    for role, source_value, sha_value, suffix in required_sources:
        specs[role] = (Path(str(source_value)), delivery / f"{name}{suffix}", str(sha_value))

    active_record_path, active_record_sha256 = _write_song_active_record(
        summary_record,
        delivery_candidate_id=delivery_candidate_id,
        title=title,
        video_sha256=video_sha256,
        summary_authority_root=authority_root,
    )
    specs["active_record"] = (
        active_record_path,
        delivery / f"{name}.record.json",
        active_record_sha256,
    )

    cover_ok = False
    cover: Path | None = None
    cover_value = artifacts.get("cover_path")
    cover_sha256 = artifacts.get("cover_sha256")
    if isinstance(cover_value, str) and isinstance(cover_sha256, str):
        cover = Path(cover_value)
        cover_ok = _matches_sha256(cover, cover_sha256)
        if cover_ok:
            specs["cover"] = (cover, delivery / f"{name}.cover.png", cover_sha256)

    receipt = _atomic_verified_song_delivery(
        candidate_id=delivery_candidate_id,
        manifest_path=delivery / f"{name}.delivery.manifest.json",
        artifact_specs=specs,
        absent_artifacts=(
            {} if cover_ok else {"cover": delivery / f"{name}.cover.png"}
        ),
    )
    delivered_artifacts = receipt["artifacts"]
    sidecar_roles = [role for role in delivered_artifacts if role != "video"]
    result = {
        "delivered": delivered_artifacts["video"]["path"],
        "delivered_sha256": delivered_artifacts["video"]["sha256"],
        "video_sha256": delivered_artifacts["video"]["sha256"],
        "delivered_sidecars": {
            role: delivered_artifacts[role]["path"] for role in sidecar_roles
        },
        "delivered_sidecar_hashes": {
            role: delivered_artifacts[role]["sha256"] for role in sidecar_roles
        },
        "delivery_manifest_path": receipt["manifest_path"],
        "delivery_manifest_sha256": receipt["manifest_sha256"],
        "delivery_upload_enabled": receipt["upload_enabled"],
        "cover_status": "AI_COVER_READY" if "cover" in delivered_artifacts else "BLOCKED_AI_COVER_REQUIRED",
    }
    if "cover" in delivered_artifacts:
        materialized = summary_record.get("materialized_recut")
        staging = materialized.get("publish_staging") if isinstance(materialized, dict) else None
        result["cover_path"] = delivered_artifacts["cover"]["path"]
        result["cover_sha256"] = delivered_artifacts["cover"]["sha256"]
        if isinstance(staging, dict):
            result["cover_generation"] = staging.get("cover_generation")
    if receipt["cleanup_warnings"]:
        result["delivery_cleanup_warnings"] = receipt["cleanup_warnings"]
    return result


VERIFIED_SONG_DELIVERY_SCHEMA_VERSION = "verified-song-delivery.v1"


class SongDeliveryError(RuntimeError):
    """A verified song package could not be committed to the delivery root."""


def _song_delivery_basename(title_or_hook: object, candidate_id: object) -> str:
    """Readable basename with an injective, runner-owned candidate suffix.

    The readable title prefix is deliberately short, so it cannot own
    uniqueness.  Song candidates are generated from the safe ASCII id grammar;
    retain that complete id in the public basename so concurrent songs with the
    same title can never replace one another's verified package.
    """

    candidate = str(candidate_id or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", candidate):
        raise SongDeliveryError(f"unsafe song delivery candidate id: {candidate!r}")
    readable = _runner.safe_name(f"歌切_{str(title_or_hook or '')}", "歌切")
    return f"{readable}__{candidate}"


def _sha256_regular_file(path: Path) -> str:
    """Hash one regular file without following a final-component symlink."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SongDeliveryError(f"cannot open verified delivery artifact {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SongDeliveryError(f"verified delivery artifact is not a regular file: {path}")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _hidden_delivery_path(destination: Path, label: str) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.{label}-",
        dir=destination.parent,
    )
    os.close(descriptor)
    path = Path(name)
    path.unlink()
    return path


def _stage_verified_copy(source: Path, destination: Path, expected_sha256: str) -> tuple[Path, str]:
    """Copy to a same-directory hidden temp and fsync it before returning."""
    normalized = _normalized_sha256(expected_sha256)
    if normalized is None:
        raise SongDeliveryError(f"invalid expected sha256 for {destination.name}")
    if source.is_symlink():
        raise SongDeliveryError(f"refusing symlink delivery source: {source}")

    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.delivery-",
        suffix=".tmp",
        dir=destination.parent,
    )
    staged = Path(name)
    try:
        source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        source_descriptor = os.open(source, source_flags)
        try:
            source_metadata = os.fstat(source_descriptor)
            if not stat.S_ISREG(source_metadata.st_mode):
                raise SongDeliveryError(f"delivery source is not a regular file: {source}")
            with os.fdopen(source_descriptor, "rb") as source_file, os.fdopen(descriptor, "wb") as target_file:
                source_descriptor = -1
                descriptor = -1
                shutil.copyfileobj(source_file, target_file, length=1024 * 1024)
                os.fchmod(target_file.fileno(), stat.S_IMODE(source_metadata.st_mode))
                target_file.flush()
                os.fsync(target_file.fileno())
        finally:
            if source_descriptor >= 0:
                os.close(source_descriptor)
        copied_sha256 = _sha256_regular_file(staged)
        if copied_sha256 != normalized:
            raise SongDeliveryError(
                f"copied hash mismatch for {destination.name}: expected {normalized}, got {copied_sha256}"
            )
        return staged, copied_sha256
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        staged.unlink(missing_ok=True)
        raise


def _stage_verified_bytes(payload: bytes, destination: Path) -> tuple[Path, str]:
    expected = hashlib.sha256(payload).hexdigest()
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.delivery-",
        suffix=".tmp",
        dir=destination.parent,
    )
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as target:
            descriptor = -1
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        actual = _sha256_regular_file(staged)
        if actual != expected:
            raise SongDeliveryError(
                f"staged manifest hash mismatch for {destination.name}: expected {expected}, got {actual}"
            )
        return staged, actual
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        staged.unlink(missing_ok=True)
        raise


def _atomic_verified_song_delivery(
    *,
    candidate_id: str,
    manifest_path: Path,
    artifact_specs: dict[str, tuple[Path, Path, str]],
    absent_artifacts: dict[str, Path] | None = None,
) -> dict:
    """Atomically expose a hash-bound song package, committing its manifest last.

    All artifact bytes are copied and verified in hidden, same-directory files
    before any public delivery name is touched.  Existing files are held under
    hidden backup names during the short commit window so a caught replace or
    post-copy verification failure can restore the previous complete package.
    The manifest is installed last and is the durable commit marker; it is
    explicitly no-upload and its own hash is returned for state binding.
    """
    if not candidate_id or "video" not in artifact_specs:
        raise SongDeliveryError("verified song delivery requires candidate_id and video")
    absent_artifacts = absent_artifacts or {}
    parent = manifest_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    parent_real = parent.resolve(strict=True)
    if manifest_path.parent.resolve(strict=True) != parent_real:
        raise SongDeliveryError("delivery manifest escaped its parent")

    staged: dict[str, tuple[Path, Path, str]] = {}
    manifest_artifacts: dict[str, dict[str, str]] = {}
    destinations: set[Path] = set()
    try:
        for role, spec in artifact_specs.items():
            if not isinstance(role, str) or not role or not isinstance(spec, tuple) or len(spec) != 3:
                raise SongDeliveryError("invalid verified delivery artifact specification")
            source, destination, expected_value = spec
            if not isinstance(source, Path) or not isinstance(destination, Path):
                raise SongDeliveryError(f"invalid paths for verified delivery artifact {role}")
            if destination.parent.resolve(strict=True) != parent_real or destination.name.startswith("."):
                raise SongDeliveryError(f"delivery destination escaped or is hidden: {destination}")
            if destination in destinations or destination == manifest_path:
                raise SongDeliveryError(f"duplicate delivery destination: {destination}")
            destinations.add(destination)
            normalized = _normalized_sha256(expected_value)
            if normalized is None:
                raise SongDeliveryError(f"invalid expected sha256 for {role}")
            source_real = source.resolve(strict=True)
            temp_path, copied_sha = _stage_verified_copy(source, destination, normalized)
            staged[role] = (temp_path, destination, copied_sha)
            manifest_artifacts[role] = {
                "path": str(destination.absolute()),
                "sha256": f"sha256:{copied_sha}",
                "source_path": str(source_real),
                "source_sha256": f"sha256:{normalized}",
            }

        manifest_absent: dict[str, dict[str, str]] = {}
        for role, destination in absent_artifacts.items():
            if (
                not isinstance(role, str)
                or not role
                or role in artifact_specs
                or not isinstance(destination, Path)
                or destination.parent.resolve(strict=True) != parent_real
                or destination.name.startswith(".")
                or destination in destinations
                or destination == manifest_path
            ):
                raise SongDeliveryError(f"invalid absent delivery artifact specification: {role}")
            destinations.add(destination)
            manifest_absent[role] = {"path": str(destination.absolute()), "status": "ABSENT"}

        manifest_payload = {
            "schema_version": VERIFIED_SONG_DELIVERY_SCHEMA_VERSION,
            "status": "DELIVERED_NO_UPLOAD",
            "candidate_id": candidate_id,
            "upload_enabled": False,
            "artifacts": manifest_artifacts,
            "absent_artifacts": manifest_absent,
        }
        manifest_bytes = (
            json.dumps(manifest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        manifest_temp, manifest_sha = _stage_verified_bytes(manifest_bytes, manifest_path)
        staged["__manifest__"] = (manifest_temp, manifest_path, manifest_sha)
    except BaseException:
        for temp_path, _destination, _digest in staged.values():
            temp_path.unlink(missing_ok=True)
        raise

    # Sidecars first, then the video, with the manifest last as the commit marker.
    install_order = sorted(role for role in artifact_specs if role != "video") + ["video", "__manifest__"]
    backup_order = [staged[role][1] for role in install_order] + list(absent_artifacts.values())
    backups: dict[Path, Path] = {}
    installed: set[Path] = set()
    try:
        for destination in backup_order:
            if os.path.lexists(destination):
                if destination.is_dir() and not destination.is_symlink():
                    raise SongDeliveryError(f"delivery destination is a directory: {destination}")
                backup = _hidden_delivery_path(destination, "previous")
                backups[destination] = backup
                os.replace(destination, backup)
        if backups:
            _fsync_directory(parent)

        for role in install_order:
            temp_path, destination, expected = staged[role]
            if role == "__manifest__":
                # Seal only after every public artifact still matches the
                # selector/materialization hash at the commit boundary.
                for artifact_role in artifact_specs:
                    _artifact_temp, artifact_destination, artifact_expected = staged[artifact_role]
                    actual = _sha256_regular_file(artifact_destination)
                    if actual != artifact_expected:
                        raise SongDeliveryError(
                            f"pre-manifest delivery hash mismatch for {artifact_destination.name}: "
                            f"expected {artifact_expected}, got {actual}"
                        )
            installed.add(destination)
            os.replace(temp_path, destination)
            _fsync_directory(parent)
            actual = _sha256_regular_file(destination)
            if actual != expected:
                raise SongDeliveryError(
                    f"final delivery hash mismatch for {destination.name}: expected {expected}, got {actual}"
                )
    except BaseException as exc:
        rollback_errors: list[str] = []
        for destination in reversed(backup_order):
            if destination in installed:
                try:
                    destination.unlink(missing_ok=True)
                except OSError as rollback_exc:
                    rollback_errors.append(f"remove {destination}: {rollback_exc}")
            backup = backups.get(destination)
            if backup is not None and os.path.lexists(backup):
                try:
                    os.replace(backup, destination)
                except OSError as rollback_exc:
                    rollback_errors.append(f"restore {destination}: {rollback_exc}")
        for temp_path, _destination, _digest in staged.values():
            try:
                temp_path.unlink(missing_ok=True)
            except OSError as rollback_exc:
                rollback_errors.append(f"remove temp {temp_path}: {rollback_exc}")
        try:
            _fsync_directory(parent)
        except OSError as rollback_exc:
            rollback_errors.append(f"fsync {parent}: {rollback_exc}")
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        detail = f"; rollback errors: {'; '.join(rollback_errors)}" if rollback_errors else ""
        raise SongDeliveryError(f"atomic verified delivery failed: {exc}{detail}") from exc

    cleanup_warnings: list[str] = []
    for backup in backups.values():
        try:
            backup.unlink(missing_ok=True)
        except OSError as exc:
            cleanup_warnings.append(f"remove superseded backup {backup}: {exc}")
    if backups:
        try:
            _fsync_directory(parent)
        except OSError as exc:
            cleanup_warnings.append(f"fsync superseded backup cleanup: {exc}")
    return {
        "manifest_path": str(manifest_path.resolve(strict=True)),
        "manifest_sha256": f"sha256:{manifest_sha}",
        "artifacts": manifest_artifacts,
        "upload_enabled": False,
        "cleanup_warnings": cleanup_warnings,
    }
