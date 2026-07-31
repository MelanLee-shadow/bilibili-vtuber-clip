#!/usr/bin/env python3
"""Build one portable Song review package from a verified delivery commit.

The verified-song delivery manifest is a manifest-last, no-upload commit marker.
This builder does not infer missing artifacts from filenames and does not repair
the delivery in place.  It replays the current state candidate and the complete
song proof, verifies every delivery and source hash, then copies the committed
bytes into an independent package root.  The resulting review manifest remains
``upload_allowed=false``; a separate authorized-upload manifest is still
required before any publication side effect.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.cover_route_evidence import (  # noqa: E402
    validate_cover_route_decision,
    validate_rendered_text_pixel_evidence,
)
from src.autoslice.channel_profile import load_channel_profile  # noqa: E402
from src.autoslice.song_completion import song_completion_evidence  # noqa: E402
from src.autoslice.title_policy import publish_title_policy_violations  # noqa: E402


CHANNEL_PROFILE = load_channel_profile(ROOT)
DELIVERY_SCHEMA = "verified-song-delivery.v1"
REVIEW_SCHEMA = "lidousha-song-review-manifest.v1"
REQUIRED_ROLES: dict[str, str] = {
    "video": ".mp4",
    "subtitle": ".srt",
    "lyrics_alignment_report": ".lyrics-alignment-report.json",
    "host_vocal_proof": ".host-vocal-proof.json",
    "recut_manifest": ".recut.manifest.json",
    "active_record": ".record.json",
    "publish": ".publish.json",
    "cover": ".cover.png",
    "cover_title_mask": ".cover.title-mask.png",
    "cover_pre_overlay": ".cover.pre-overlay.png",
    "cover_route_background": ".cover.route-background.png",
}
REVIEWABLE_BATCH_STATUSES = {
    "review_ready",
    "review_ready_with_failures",
    "review_ready_retry_wait",
}
SHA256_RE = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


class SongReviewManifestError(RuntimeError):
    """The state/delivery pair cannot prove a portable Song review package."""


def _verify_song_completion(record: dict[str, Any]) -> dict[str, Any]:
    """Replay the proof with the same profile authority as the runner wrapper."""

    return song_completion_evidence(
        record,
        host_vocal_profile=CHANNEL_PROFILE.asset_file("voiceprint_profile"),
    )


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SongReviewManifestError(f"{label} is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SongReviewManifestError(f"{label} must be a JSON object: {path}")
    return value


def _normalized_sha256(value: object, *, label: str) -> str:
    match = SHA256_RE.fullmatch(str(value or ""))
    if match is None:
        raise SongReviewManifestError(f"{label} has an invalid sha256")
    return match.group(1)


def _sha256_regular_file(path: Path, *, label: str) -> str:
    if path.is_symlink():
        raise SongReviewManifestError(f"{label} may not be a symlink: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SongReviewManifestError(
            f"{label} is not an openable regular file: {path}: {exc}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SongReviewManifestError(f"{label} is not a regular file: {path}")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _matches(path: Path, expected: object, *, label: str) -> str:
    expected_hex = _normalized_sha256(expected, label=label)
    actual = _sha256_regular_file(path, label=label)
    if actual != expected_hex:
        raise SongReviewManifestError(
            f"{label} hash drift: expected={expected_hex} actual={actual}"
        )
    return actual


def _canonical_file(path: Path, *, label: str) -> Path:
    if path.is_symlink():
        raise SongReviewManifestError(f"{label} may not be a symlink: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SongReviewManifestError(f"{label} is missing: {path}: {exc}") from exc
    if not resolved.is_file():
        raise SongReviewManifestError(f"{label} is not a regular file: {resolved}")
    return resolved


def _candidate_row(state: Mapping[str, Any], candidate_id: str) -> dict[str, Any]:
    batch_status = str(state.get("status") or "")
    if batch_status not in REVIEWABLE_BATCH_STATUSES:
        raise SongReviewManifestError(
            f"state batch is not reviewable: status={batch_status!r}"
        )
    rows = state.get("songs")
    if not isinstance(rows, list):
        raise SongReviewManifestError("state.songs is missing")
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and str(row.get("candidate_id") or "") == candidate_id
    ]
    if len(matches) != 1:
        raise SongReviewManifestError(
            f"state must contain exactly one Song candidate {candidate_id!r}"
        )
    row = matches[0]
    if row.get("status") != "review_ready" or row.get("rc") != 0:
        raise SongReviewManifestError(
            "Song candidate is not individually review-ready: "
            f"status={row.get('status')!r} rc={row.get('rc')!r}"
        )
    if row.get("delivery_upload_enabled") is not False:
        raise SongReviewManifestError("Song state candidate does not bind no-upload")
    return row


def _delivery_basename(manifest_path: Path) -> str:
    suffix = ".delivery.manifest.json"
    if not manifest_path.name.endswith(suffix):
        raise SongReviewManifestError(
            "delivery manifest name must end in .delivery.manifest.json"
        )
    basename = manifest_path.name[: -len(suffix)]
    if not basename or basename.startswith(".") or "/" in basename:
        raise SongReviewManifestError("delivery basename is unsafe")
    return basename


def _validate_delivery_artifacts(
    *,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    state_row: Mapping[str, Any],
) -> dict[str, tuple[Path, str]]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise SongReviewManifestError("delivery manifest artifacts are missing")
    missing = set(REQUIRED_ROLES) - set(artifacts)
    if missing:
        raise SongReviewManifestError(
            f"delivery manifest lacks required artifacts: {sorted(missing)}"
        )
    absent = manifest.get("absent_artifacts")
    if not isinstance(absent, dict):
        raise SongReviewManifestError("delivery absent_artifacts must be an object")
    if set(absent) & set(REQUIRED_ROLES):
        raise SongReviewManifestError(
            "delivery declares a required review artifact absent"
        )

    parent = manifest_path.parent.resolve(strict=True)
    basename = _delivery_basename(manifest_path)
    validated: dict[str, tuple[Path, str]] = {}
    for role, entry in artifacts.items():
        if not isinstance(role, str) or not role or not isinstance(entry, dict):
            raise SongReviewManifestError("delivery artifact entry is invalid")
        path_value = entry.get("path")
        source_value = entry.get("source_path")
        if not isinstance(path_value, str) or not isinstance(source_value, str):
            raise SongReviewManifestError(
                f"delivery artifact {role} lacks path/source_path"
            )
        artifact_path = _canonical_file(Path(path_value), label=f"{role} artifact")
        source_path = _canonical_file(Path(source_value), label=f"{role} source")
        if artifact_path.parent != parent:
            raise SongReviewManifestError(
                f"delivery artifact {role} escapes the manifest directory"
            )
        if role in REQUIRED_ROLES:
            expected_name = basename + REQUIRED_ROLES[role]
            if artifact_path.name != expected_name:
                raise SongReviewManifestError(
                    f"delivery artifact {role} has a non-canonical name: "
                    f"{artifact_path.name!r} != {expected_name!r}"
                )
        artifact_sha = _matches(
            artifact_path, entry.get("sha256"), label=f"{role} artifact"
        )
        source_sha = _matches(
            source_path, entry.get("source_sha256"), label=f"{role} source"
        )
        if artifact_sha != source_sha:
            raise SongReviewManifestError(
                f"delivery artifact/source hash drift for {role}"
            )
        validated[role] = (artifact_path, artifact_sha)

    # The public manifest is the durable commit marker and must be at least as
    # new as every committed artifact.  Hash verification above proves the
    # stronger byte closure; this timestamp check catches a stale pre-commit
    # manifest accidentally selected after a later artifact rewrite.
    newest_artifact_mtime = max(
        path.stat().st_mtime_ns for path, _sha in validated.values()
    )
    if manifest_path.stat().st_mtime_ns < newest_artifact_mtime:
        raise SongReviewManifestError(
            "delivery manifest is older than a committed artifact; "
            "manifest-last authority is unproven"
        )

    sidecars = state_row.get("delivered_sidecars")
    sidecar_hashes = state_row.get("delivered_sidecar_hashes")
    if not isinstance(sidecars, dict) or not isinstance(sidecar_hashes, dict):
        raise SongReviewManifestError(
            "state candidate lacks delivered sidecar path/hash bindings"
        )
    for role in set(REQUIRED_ROLES) - {"video"}:
        path, sha = validated[role]
        state_path = sidecars.get(role)
        if not isinstance(state_path, str):
            raise SongReviewManifestError(
                f"state candidate lacks delivered sidecar path for {role}"
            )
        try:
            state_resolved = Path(state_path).resolve(strict=True)
        except OSError as exc:
            raise SongReviewManifestError(
                f"state sidecar path is missing for {role}: {exc}"
            ) from exc
        if state_resolved != path or _normalized_sha256(
            sidecar_hashes.get(role), label=f"state {role}"
        ) != sha:
            raise SongReviewManifestError(
                f"state/delivery sidecar binding drift for {role}"
            )
    video, video_sha = validated["video"]
    delivered = state_row.get("delivered")
    if (
        not isinstance(delivered, str)
        or Path(delivered).resolve(strict=True) != video
        or _normalized_sha256(
            state_row.get("delivered_sha256") or state_row.get("video_sha256"),
            label="state delivered video",
        )
        != video_sha
    ):
        raise SongReviewManifestError("state/delivery video binding drift")
    return validated


def _fresh_song_completion(
    *,
    state_row: Mapping[str, Any],
    completion_verifier: Callable[[dict[str, Any]], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    summary_value = state_row.get("selector_summary_path")
    if not isinstance(summary_value, str) or not summary_value:
        raise SongReviewManifestError("state candidate lacks selector_summary_path")
    summary_path = _canonical_file(
        Path(summary_value), label="selector summary"
    )
    _matches(
        summary_path,
        state_row.get("selector_summary_sha256"),
        label="selector summary",
    )
    summary = _load_json(summary_path, label="selector summary")
    selector_id = str(state_row.get("selector_record_candidate_id") or "")
    records = summary.get("records")
    if not isinstance(records, list):
        raise SongReviewManifestError("selector summary records are missing")
    selected = [
        row
        for row in records
        if isinstance(row, dict)
        and str(row.get("candidate_id") or "") == selector_id
    ]
    if len(selected) != 1:
        raise SongReviewManifestError(
            "selector summary does not contain exactly one bound Song record"
        )
    fresh = completion_verifier(selected[0])
    if (
        not isinstance(fresh, dict)
        or fresh.get("ready") is not True
        or fresh.get("reason_codes") != []
        or fresh.get("song_boundary_status") != "FULL_SONG_READY"
        or fresh.get("lyrics_alignment_status") != "READY"
        or fresh.get("host_vocal_status") != "READY"
        or fresh.get("live_performance_status") != "READY"
        or fresh.get("live_performance_mode") != "LIVE_STREAMER_SINGING"
        or fresh.get("joint_singing_decision") != "VERIFIED_LIDOUSHA_SINGING"
        or fresh.get("subtitle_source") != "external_lrc_global_shift"
    ):
        raise SongReviewManifestError(
            "fresh Song completion proof is not delivery-ready: "
            f"{json.dumps(fresh, ensure_ascii=False, sort_keys=True)}"
        )
    frozen = state_row.get("song_completion_evidence")
    if not isinstance(frozen, dict) or fresh != frozen:
        raise SongReviewManifestError(
            "fresh Song completion proof drifted from the state candidate"
        )
    return selected[0], fresh


def _completion_artifact_bindings(
    *,
    completion: Mapping[str, Any],
    artifacts: Mapping[str, tuple[Path, str]],
) -> None:
    expected = {
        "video": (
            completion.get("burned_preview_path"),
            completion.get("burned_preview_sha256"),
        ),
        "lyrics_alignment_report": (
            completion.get("alignment_report_path"),
            completion.get("alignment_report_sha256"),
        ),
        "host_vocal_proof": (
            completion.get("host_vocal_proof_path"),
            completion.get("host_vocal_proof_sha256"),
        ),
        "recut_manifest": (
            completion.get("recut_manifest_path"),
            completion.get("recut_manifest_sha256"),
        ),
    }
    delivery_manifest = _load_json(
        artifacts["recut_manifest"][0], label="delivered recut manifest"
    )
    proof_binding = delivery_manifest.get("verified_output_binding")
    proof_artifacts = (
        proof_binding.get("artifacts")
        if isinstance(proof_binding, dict)
        else None
    )
    proofs = (
        proof_binding.get("proofs") if isinstance(proof_binding, dict) else None
    )
    if (
        delivery_manifest.get("schema_version") != "materialized-recut.v2"
        or delivery_manifest.get("status") != "MATERIALIZED"
        or delivery_manifest.get("subtitle_source")
        != "external_lrc_global_shift"
        or delivery_manifest.get("reason_codes") != []
        or not isinstance(proof_binding, dict)
        or proof_binding.get("schema_version")
        != "verified-song-output-binding.v1"
        or not isinstance(proof_artifacts, dict)
        or not isinstance(proofs, dict)
    ):
        raise SongReviewManifestError(
            "delivered recut manifest lacks the verified Song output binding"
        )
    for role, (source_value, sha_value) in expected.items():
        artifact_path, artifact_sha = artifacts[role]
        if not isinstance(source_value, str):
            raise SongReviewManifestError(
                f"completion proof lacks source path for {role}"
            )
        try:
            source_path = Path(source_value).resolve(strict=True)
        except OSError as exc:
            raise SongReviewManifestError(
                f"completion proof source is missing for {role}: {exc}"
            ) from exc
        delivery_doc = _load_json(
            artifacts["delivery_manifest"][0],
            label="portable delivery manifest",
        )
        source_entry = delivery_doc["artifacts"][role]
        if (
            Path(str(source_entry.get("source_path") or "")).resolve(strict=True)
            != source_path
            or _normalized_sha256(
                sha_value, label=f"completion {role}"
            )
            != artifact_sha
        ):
            raise SongReviewManifestError(
                f"completion/delivery artifact binding drift for {role}"
            )
    if (
        _normalized_sha256(
            proof_artifacts.get("burned_media_sha256"),
            label="verified burned video",
        )
        != artifacts["video"][1]
        or _normalized_sha256(
            proof_artifacts.get("subtitle_sha256"),
            label="verified Song subtitle",
        )
        != artifacts["subtitle"][1]
        or _normalized_sha256(
            proofs.get("lyrics_alignment_report_sha256"),
            label="verified lyrics alignment report",
        )
        != artifacts["lyrics_alignment_report"][1]
        or _normalized_sha256(
            proofs.get("host_vocal_proof_sha256"),
            label="verified host-vocal proof",
        )
        != artifacts["host_vocal_proof"][1]
    ):
        raise SongReviewManifestError(
            "verified Song output binding does not match delivered bytes"
        )


def _validate_publish_cover_surfaces(
    *,
    candidate_id: str,
    state_row: Mapping[str, Any],
    artifacts: Mapping[str, tuple[Path, str]],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    record = _load_json(artifacts["active_record"][0], label="active Song record")
    publish = _load_json(artifacts["publish"][0], label="Song publish draft")
    staging = record.get("publish_staging")
    generation = (
        staging.get("cover_generation") if isinstance(staging, dict) else None
    )
    publish_generation = publish.get("cover_generation")
    title = str(state_row.get("title") or "").strip()
    source_candidate_id = str(record.get("source_candidate_id") or "")
    artifact_hashes = record.get("artifact_hashes")
    publish_hashes = publish.get("artifact_hashes")
    if (
        record.get("delivery_candidate_id") != candidate_id
        or not source_candidate_id
        or publish.get("schema_version") != "shadow-publish-draft.v1"
        or publish.get("candidate_id") != source_candidate_id
        or not isinstance(staging, dict)
        or staging.get("status") != "STAGED"
        or staging.get("title") != title
        or publish.get("title") != title
        or staging.get("upload_enabled") is not False
        or publish.get("upload_enabled") is not False
        or publish_title_policy_violations(title, lane="song")
        or not isinstance(artifact_hashes, dict)
        or not isinstance(publish_hashes, dict)
        or not isinstance(generation, dict)
        or publish_generation != generation
        or not validate_cover_route_decision(
            generation, allow_legacy_v1=False
        )
        or not validate_rendered_text_pixel_evidence(generation)
    ):
        raise SongReviewManifestError(
            "Song title/publish/final-cover authority is missing or stale"
        )

    expected_hashes = {
        "burned_video_sha256": artifacts["video"][1],
        "subtitle_sha256": artifacts["subtitle"][1],
        "cover_sha256": artifacts["cover"][1],
    }
    for key, expected in expected_hashes.items():
        if _normalized_sha256(
            artifact_hashes.get(key), label=f"record {key}"
        ) != expected:
            raise SongReviewManifestError(f"record artifact hash drift: {key}")
        if _normalized_sha256(
            publish_hashes.get(key), label=f"publish {key}"
        ) != expected:
            raise SongReviewManifestError(f"publish artifact hash drift: {key}")
    if (
        _normalized_sha256(
            generation.get("final_cover_sha256"), label="final cover"
        )
        != artifacts["cover"][1]
        or _normalized_sha256(
            generation.get("pre_overlay_sha256"), label="cover pre-overlay"
        )
        != artifacts["cover_pre_overlay"][1]
        or _normalized_sha256(
            generation.get("ai_background_sha256"), label="cover background"
        )
        != artifacts["cover_route_background"][1]
    ):
        raise SongReviewManifestError(
            "final cover/replay attachment binding drift"
        )
    pixels = generation.get("rendered_text_pixels")
    if (
        not isinstance(pixels, dict)
        or _normalized_sha256(
            pixels.get("mask_sha256"), label="cover title mask"
        )
        != artifacts["cover_title_mask"][1]
        or _normalized_sha256(
            pixels.get("pre_overlay_sha256"),
            label="cover pixel pre-overlay",
        )
        != artifacts["cover_pre_overlay"][1]
        or _normalized_sha256(
            pixels.get("final_cover_sha256"),
            label="cover pixel final cover",
        )
        != artifacts["cover"][1]
    ):
        raise SongReviewManifestError(
            "rendered cover pixel evidence/replay attachment drift"
        )
    return title, record, generation


def _copy_verified(source: Path, destination: Path, expected_sha: str) -> None:
    if destination.exists() or destination.is_symlink():
        raise SongReviewManifestError(
            f"portable package target already exists: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary: Path | None = Path(name)
    try:
        with source.open("rb") as source_handle, os.fdopen(
            descriptor, "wb"
        ) as target_handle:
            descriptor = -1
            shutil.copyfileobj(source_handle, target_handle, length=1 << 20)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        actual = _sha256_regular_file(temporary, label=destination.name)
        if actual != expected_sha:
            raise SongReviewManifestError(
                f"portable copy hash drift for {destination.name}"
            )
        os.replace(temporary, destination)
        temporary = None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    body = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary: Path | None = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)


def build(
    package_root: Path,
    *,
    delivery_manifest_path: Path,
    state_path: Path,
    candidate_id: str,
    deployed_commit_file: Path,
    completion_verifier: Callable[
        [dict[str, Any]], dict[str, Any]
    ] = _verify_song_completion,
) -> dict[str, Any]:
    if package_root.exists():
        raise SongReviewManifestError(
            f"portable package root already exists: {package_root}"
        )
    delivery_manifest_path = _canonical_file(
        delivery_manifest_path, label="verified Song delivery manifest"
    )
    state_path = _canonical_file(state_path, label="autoslice state")
    deployed_commit_file = _canonical_file(
        deployed_commit_file, label="deployed commit"
    )
    deployed_commit = deployed_commit_file.read_text(encoding="utf-8").split()[0]
    if COMMIT_RE.fullmatch(deployed_commit) is None:
        raise SongReviewManifestError("deployed commit file is invalid")

    state = _load_json(state_path, label="autoslice state")
    state_row = _candidate_row(state, candidate_id)
    delivery_manifest = _load_json(
        delivery_manifest_path, label="verified Song delivery manifest"
    )
    delivery_sha = _sha256_regular_file(
        delivery_manifest_path, label="verified Song delivery manifest"
    )
    if (
        delivery_manifest.get("schema_version") != DELIVERY_SCHEMA
        or delivery_manifest.get("status") != "DELIVERED_NO_UPLOAD"
        or delivery_manifest.get("candidate_id") != candidate_id
        or delivery_manifest.get("upload_enabled") is not False
        or _normalized_sha256(
            state_row.get("delivery_manifest_sha256"),
            label="state delivery manifest",
        )
        != delivery_sha
    ):
        raise SongReviewManifestError(
            "state/delivery manifest identity or no-upload authority drift"
        )
    state_manifest_value = state_row.get("delivery_manifest_path")
    if not isinstance(state_manifest_value, str) or _canonical_file(
        Path(state_manifest_value), label="state delivery manifest"
    ) != delivery_manifest_path:
        raise SongReviewManifestError(
            "state/delivery manifest path binding drift"
        )

    artifacts = _validate_delivery_artifacts(
        manifest_path=delivery_manifest_path,
        manifest=delivery_manifest,
        state_row=state_row,
    )
    # Make the delivery manifest available to cross-surface validation without
    # relying on its outer local variable or a basename search.
    artifacts = dict(artifacts)
    artifacts["delivery_manifest"] = (
        delivery_manifest_path,
        delivery_sha,
    )
    _selector_record, completion = _fresh_song_completion(
        state_row=state_row,
        completion_verifier=completion_verifier,
    )
    _completion_artifact_bindings(
        completion=completion,
        artifacts=artifacts,
    )
    title, _record, generation = _validate_publish_cover_surfaces(
        candidate_id=candidate_id,
        state_row=state_row,
        artifacts=artifacts,
    )

    package_root.mkdir(parents=True)
    basename = _delivery_basename(delivery_manifest_path)
    portable_paths: dict[str, Path] = {}
    for role, suffix in REQUIRED_ROLES.items():
        source, sha = artifacts[role]
        destination = package_root / f"{basename}{suffix}"
        _copy_verified(source, destination, sha)
        portable_paths[role] = destination
    portable_delivery_manifest = (
        package_root / f"{basename}.delivery.manifest.json"
    )
    _copy_verified(
        delivery_manifest_path,
        portable_delivery_manifest,
        delivery_sha,
    )

    package_date = delivery_manifest_path.parent.name
    if DATE_RE.fullmatch(package_date) is None:
        package_date = state_path.stem
    if DATE_RE.fullmatch(package_date) is None:
        raise SongReviewManifestError("cannot derive a canonical package date")

    item_sha = {
        role: _sha256_regular_file(path, label=f"portable {role}")
        for role, path in portable_paths.items()
    }
    item = {
        "id": candidate_id,
        "candidate_id": candidate_id,
        "stem": basename,
        "kind": "song",
        "classification": "Song",
        "title": title,
        "video": portable_paths["video"].name,
        "subtitle_srt": portable_paths["subtitle"].name,
        "publish_json": portable_paths["publish"].name,
        "record": portable_paths["active_record"].name,
        "evidence_json": portable_paths["active_record"].name,
        "cover": portable_paths["cover"].name,
        "cover_pre_overlay": portable_paths["cover_pre_overlay"].name,
        "cover_title_mask": portable_paths["cover_title_mask"].name,
        "cover_route_background": portable_paths[
            "cover_route_background"
        ].name,
        "lyrics_alignment_report": portable_paths[
            "lyrics_alignment_report"
        ].name,
        "host_vocal_proof": portable_paths["host_vocal_proof"].name,
        "recut_manifest": portable_paths["recut_manifest"].name,
        "delivery_manifest": portable_delivery_manifest.name,
        "sha256": item_sha,
    }
    attestation = {
        "candidate_id": candidate_id,
        "reference_sha256": generation.get("reference_sha256"),
        "final_cover_sha256": generation.get("final_cover_sha256"),
        "method": generation.get("method"),
        "route_decision": generation.get("route_decision"),
        "reference_authority": generation.get("reference_authority"),
    }
    manifest = {
        "schema_version": REVIEW_SCHEMA,
        "generated_by": "build_lidousha_song_review_manifest.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "date": package_date,
        "status": "finished_review_package_no_upload_pending_human_review",
        "batch_status": state.get("status"),
        "candidate_id": candidate_id,
        "classification": "Song",
        "deployed_commit": deployed_commit,
        "run_mode": "PRODUCTION_REVIEW",
        "story_contract_required": False,
        "upload_allowed": False,
        "subtitle_visual_contract": {
            "max_visual_lines": 2,
            "max_chars_per_line": 28,
        },
        "delivery_authority": {
            "schema_version": DELIVERY_SCHEMA,
            "manifest": portable_delivery_manifest.name,
            "manifest_sha256": f"sha256:{delivery_sha}",
            "state_sha256": "sha256:"
            + _sha256_regular_file(state_path, label="autoslice state"),
            "selector_summary_sha256": state_row.get(
                "selector_summary_sha256"
            ),
            "song_completion_evidence": completion,
            "upload_enabled": False,
        },
        "cover_route_attestations": [attestation],
        "items": [item],
    }
    _atomic_json(package_root / "review_manifest.json", manifest)
    return manifest


def run_canonical_auditor(package_root: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        str(ROOT / "scripts/audit_lidousha_review_package.py"),
        str(package_root),
        "--json",
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=900,
    )
    try:
        result = json.loads(completed.stdout)
    except ValueError as exc:
        raise SongReviewManifestError(
            "canonical package auditor did not return JSON: "
            f"rc={completed.returncode} stderr={completed.stderr[-1000:]!r}"
        ) from exc
    if (
        completed.returncode != 0
        or not isinstance(result, dict)
        or result.get("schema_version")
        != "lidousha-review-package-audit.v2"
        or result.get("passed") is not True
    ):
        raise SongReviewManifestError(
            "canonical package auditor rejected the Song package: "
            f"rc={completed.returncode} "
            f"issues={json.dumps(result.get('issues'), ensure_ascii=False)}"
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package_root", type=Path)
    parser.add_argument("--delivery-manifest", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--deployed-commit-file", required=True, type=Path)
    parser.add_argument(
        "--audit-out",
        type=Path,
        default=None,
        help="default: <package_root>/package_audit.json",
    )
    args = parser.parse_args()
    try:
        build(
            args.package_root,
            delivery_manifest_path=args.delivery_manifest,
            state_path=args.state,
            candidate_id=args.candidate,
            deployed_commit_file=args.deployed_commit_file,
        )
        audit = run_canonical_auditor(args.package_root)
        audit_out = args.audit_out or args.package_root / "package_audit.json"
        if audit_out.exists() or audit_out.is_symlink():
            raise SongReviewManifestError(
                f"audit output already exists: {audit_out}"
            )
        audit_out.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(audit_out, audit)
    except SongReviewManifestError as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "PACKAGE_AUDIT_PASS_NO_UPLOAD",
                "package_root": str(args.package_root.resolve()),
                "review_manifest": str(
                    (args.package_root / "review_manifest.json").resolve()
                ),
                "package_audit": str(audit_out.resolve()),
                "upload_allowed": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
