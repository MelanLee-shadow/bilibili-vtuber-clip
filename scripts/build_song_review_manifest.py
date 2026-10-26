#!/usr/bin/env python3
"""Build a portable Song package from a verified delivery or same-BV replacement.

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
import copy
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, TypeAlias


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.cover_route_evidence import (
    validate_cover_route_decision,
    validate_rendered_text_pixel_evidence,
)
from src.autoslice.host_only_v4_package_binding import (
    BINDING_ITEM_KEY,
    SONG_BINDING_ITEM_KEY,
    SONG_BINDING_SCHEMA_VERSION,
    HostOnlyV4PackageBindingError,
    materialize_package_binding,
)
from src.autoslice.channel_profile import load_channel_profile
from src.autoslice import operator_processing_scope as operator_scope
from src.autoslice import semantic_evidence_scorecard_refresh as semantic_refresh
from src.autoslice.song_delivery import (
    SongDeliveryError,
    _selector_summary_record,
    _validated_song_upload_tags,
)
from src.autoslice.song_completion import song_completion_evidence
from src.autoslice.producer_delivery_transaction import (
    ProducerDeliveryTransactionError,
    load_prepared_delivery,
    preflight_prepared_delivery,
)
from src.autoslice.prepared_review_recovery import (
    historical_prepared_review,
)
from src.autoslice.qixi_transaction_core import exclusive_runner_commit
from src.autoslice.song_materialized_successor import completion as materialized_song_completion
from src.autoslice.title_policy import publish_title_policy_violations
from scripts.suggest_upload_tags import generate_upload_tags


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
TALK_REFRESH_BLOCKED_BATCH_STATUSES = {
    "semantic_chat_scorecard_refresh_blocked",
    "paused_semantic_chat_scorecard_refresh",
}
SHA256_RE = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


# The replacement constructor returns serialized JSON, not arbitrary objects.
JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


class SongReviewManifestError(RuntimeError):
    """The supplied authority cannot prove a portable Song review package."""


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


def _talk_refresh_block_is_isolated_from_song(
    state: Mapping[str, Any], candidate_id: str, *, recording_date: str | None
) -> bool:
    """Accept only a typed Talk-only refresh block for no-upload Song review.

    The normal batch status whitelist deliberately remains closed.  This is
    solely the recovery case where a historical, operator-scoped Talk refresh
    is blocked after an independently completed Song delivery.  It does not
    reclassify the batch, clear the Talk block, or authorize publication.
    """

    if (
        recording_date is None
        or DATE_RE.fullmatch(recording_date) is None
        or state.get("status") not in TALK_REFRESH_BLOCKED_BATCH_STATUSES
        or state.get("upload_allowed") is not False
    ):
        return False
    receipt = state.get(semantic_refresh.REFRESH_STATE_KEY)
    if not isinstance(receipt, Mapping):
        return False
    admission = operator_scope.operator_scope_admission(state, date=recording_date)
    frozen_scope = operator_scope.operator_talk_scope(state, date=recording_date)
    if (
        not admission.admitted
        or not isinstance(admission.disclosure, Mapping)
        or not admission.grant_id
        or frozen_scope != admission.candidate_ids
        or candidate_id in admission.candidate_ids
    ):
        return False
    expected_keys = {
        "schema_version",
        "recording_date",
        "scope_grant_id",
        "scope_grant_sha256",
        "status",
        "refreshed_candidate_ids",
        "current_candidate_ids",
        "blocked_candidate_ids",
        "deferred_candidate_ids",
        "duplicate_candidate_ids",
        "missing_never_recalled_candidates_outside_scope",
        "receipt_sha256",
    }
    if set(receipt) != expected_keys:
        return False
    receipt_body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    expected_statuses = (
        {"BLOCKED", "BLOCKED_DUPLICATE_CANDIDATE"}
        if state.get("status") == "semantic_chat_scorecard_refresh_blocked"
        else {"RETRY_WAIT"}
    )
    if (
        receipt.get("schema_version") != semantic_refresh.REFRESH_RUN_SCHEMA
        or receipt.get("recording_date") != recording_date
        or receipt.get("scope_grant_id") != admission.grant_id
        or receipt.get("scope_grant_sha256")
        != semantic_refresh._canonical_sha256(state.get(operator_scope.STATE_KEY))
        or receipt.get("status") not in expected_statuses
        or receipt.get("missing_never_recalled_candidates_outside_scope") is not True
        or receipt.get("receipt_sha256") != semantic_refresh._canonical_sha256(receipt_body)
    ):
        return False
    receipt_ids: list[str] = []
    for key in (
        "refreshed_candidate_ids",
        "current_candidate_ids",
        "blocked_candidate_ids",
        "deferred_candidate_ids",
        "duplicate_candidate_ids",
    ):
        values = receipt.get(key)
        if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
            return False
        receipt_ids.extend(values)
    scope_ids = set(admission.candidate_ids)
    if len(receipt_ids) != len(set(receipt_ids)) or set(receipt_ids) != scope_ids:
        return False
    talk_rows = [
        row
        for key in ("pending_talk", "talk_backlog")
        for row in (state.get(key) if isinstance(state.get(key), list) else [])
        if isinstance(row, Mapping)
        and str(row.get("candidate_id") or row.get("cid") or "") in scope_ids
    ]
    song_ids = {
        str(row.get("candidate_id") or row.get("cid") or "")
        for key in operator_scope.SONG_STATE_COLLECTIONS
        for row in (state.get(key) if isinstance(state.get(key), list) else [])
        if isinstance(row, Mapping)
    }
    return bool(
        len(talk_rows) == len(scope_ids)
        and {
            str(row.get("candidate_id") or row.get("cid") or "")
            for row in talk_rows
        }
        == scope_ids
        and all(row.get("lane") in {"semantic_recall", "semantic_recall_sharded"} for row in talk_rows)
        and not (scope_ids & song_ids)
    )


def _candidate_row(
    state: Mapping[str, Any], candidate_id: str, *, recording_date: str | None = None
) -> dict[str, Any]:
    batch_status = str(state.get("status") or "")
    if batch_status not in REVIEWABLE_BATCH_STATUSES and not _talk_refresh_block_is_isolated_from_song(
        state, candidate_id, recording_date=recording_date
    ):
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
    state_row: Mapping[str, Any] | None = None,
    title: str | None = None,
    artifacts: Mapping[str, tuple[Path, str]],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    record = _load_json(artifacts["active_record"][0], label="active Song record")
    publish = _load_json(artifacts["publish"][0], label="Song publish draft")
    staging = record.get("publish_staging")
    generation = (
        staging.get("cover_generation") if isinstance(staging, dict) else None
    )
    publish_generation = publish.get("cover_generation")
    if title is None:
        if state_row is None:
            raise SongReviewManifestError(
                "Song publish/cover validation has no title authority"
            )
        title = str(state_row.get("title") or "").strip()
    else:
        title = str(title).strip()
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


def _validate_ready_song_completion(
    record: dict[str, Any],
    *,
    completion_verifier: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Replay the exact materialized Song proof for a prepared continuation."""

    try:
        completion = completion_verifier(record)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise SongReviewManifestError(
            f"prepared Song completion proof cannot be replayed: {exc}"
        ) from exc
    if (
        not isinstance(completion, dict)
        or completion.get("ready") is not True
        or completion.get("reason_codes") != []
        or completion.get("song_boundary_status") != "FULL_SONG_READY"
        or completion.get("lyrics_alignment_status") != "READY"
        or completion.get("host_vocal_status") != "READY"
        or completion.get("live_performance_status") != "READY"
        or completion.get("live_performance_mode") != "LIVE_STREAMER_SINGING"
        or completion.get("joint_singing_decision") != "VERIFIED_LIDOUSHA_SINGING"
        or completion.get("subtitle_source") != "external_lrc_global_shift"
    ):
        raise SongReviewManifestError(
            "prepared Song completion proof is not delivery-ready: "
            f"{json.dumps(completion, ensure_ascii=False, sort_keys=True)}"
        )
    return completion


def _historical_review_artifacts(handle, lease):
    """Compatibility spelling for the shared review-only check."""

    return historical_prepared_review(handle, lease).checked


def _prepared_handle_bindings(
    *,
    runtime_root: Path,
    prepared_manifest_path: Path,
    prepared_manifest_sha256: object,
) -> tuple[object, Any, dict[str, Any], str, str]:
    """Load and preflight a private prepared handle under the runner lease."""

    prepared_path = _canonical_file(
        prepared_manifest_path, label="prepared Song manifest"
    )
    expected_handle_sha = _normalized_sha256(
        prepared_manifest_sha256, label="prepared Song manifest"
    )
    try:
        handle = load_prepared_delivery(
            runtime_root=runtime_root, manifest_path=prepared_path
        )
        with exclusive_runner_commit(runtime_root) as lease:
            from src.autoslice.producer_delivery_transaction import _read_document, deployment_authority_binding
            document = _read_document(handle)
            if document["deployed_authority"] == deployment_authority_binding(runtime_root):
                checked = preflight_prepared_delivery(handle=handle, lease=lease).checked
            else:
                checked = _historical_review_artifacts(handle, lease)
    except (ProducerDeliveryTransactionError, OSError, ValueError) as exc:
        raise SongReviewManifestError(
            f"prepared Song transaction preflight failed: {exc}"
        ) from exc
    if handle.lane != "song" or expected_handle_sha != handle.prepared_sha256:
        raise SongReviewManifestError(
            "prepared Song manifest lane or prepared_sha256 binding drifts"
        )
    return (
        handle,
        checked,
        document,
        handle.prepared_sha256,
        _sha256_regular_file(prepared_path, label="prepared Song manifest file"),
    )


def _prepared_artifact_bindings(
    *,
    checked: tuple,
    document: Mapping[str, Any],
    candidate_id: str,
) -> tuple[str, dict[str, tuple[Path, str]], dict[str, dict[str, Any]], Path]:
    """Map staged transaction roles to the same portable role contract."""

    if (
        document.get("schema_version") != "producer-prepared-delivery.v1"
        or document.get("lane") != "song"
        or document.get("candidate_id") != candidate_id
        or document.get("upload_enabled") is not False
    ):
        raise SongReviewManifestError(
            "prepared Song manifest candidate/lane/upload authority drifts"
        )
    rows = document.get("artifacts")
    if not isinstance(rows, list) or not rows:
        raise SongReviewManifestError("prepared Song manifest has no artifacts")
    if not isinstance(checked, tuple) or len(checked) != len(rows):
        raise SongReviewManifestError("prepared Song preflight inventory drifts")

    staged: dict[str, tuple[Path, str]] = {}
    entries: dict[str, dict[str, Any]] = {}
    for entry, staged_path, target_path, expected in checked:
        role = str(entry.get("role") or "") if isinstance(entry, dict) else ""
        if not role or role in entries:
            raise SongReviewManifestError("prepared Song artifact role inventory drifts")
        staged_file = _canonical_file(
            Path(str(staged_path)), label=f"prepared {role} staged artifact"
        )
        actual = _sha256_regular_file(
            staged_file, label=f"prepared {role} staged artifact"
        )
        if actual != str(expected):
            raise SongReviewManifestError(
                f"prepared {role} staged artifact hash drift"
            )
        staged[role] = (staged_file, actual)
        entries[role] = entry

    required = set(REQUIRED_ROLES) | {"delivery_manifest"}
    missing = required - set(staged)
    if missing:
        raise SongReviewManifestError(
            f"prepared Song manifest lacks required roles: {sorted(missing)}"
        )
    video_target = Path(str(entries["video"].get("target_path") or "")).absolute()
    if not video_target.name.endswith(REQUIRED_ROLES["video"]):
        raise SongReviewManifestError("prepared Song video target has an unsafe name")
    basename = video_target.name[: -len(REQUIRED_ROLES["video"])]
    if not basename or basename.startswith(".") or "/" in basename:
        raise SongReviewManifestError("prepared Song delivery basename is unsafe")
    parent = video_target.parent
    for role, suffix in REQUIRED_ROLES.items():
        target = Path(str(entries[role].get("target_path") or "")).absolute()
        if target.parent != parent or target.name != basename + suffix:
            raise SongReviewManifestError(
                f"prepared {role} target does not match the delivery role contract"
            )
    manifest_target = Path(
        str(entries["delivery_manifest"].get("target_path") or "")
    ).absolute()
    if (
        manifest_target.parent != parent
        or manifest_target.name != basename + ".delivery.manifest.json"
    ):
        raise SongReviewManifestError(
            "prepared delivery manifest target does not match the Song contract"
        )
    return basename, staged, entries, manifest_target


def _validate_prepared_delivery_manifest(
    *,
    delivery_manifest_path: Path,
    delivery_manifest: Mapping[str, Any],
    staged: Mapping[str, tuple[Path, str]],
    entries: Mapping[str, Mapping[str, Any]],
    candidate_id: str,
) -> None:
    """Validate the intended no-upload manifest against staged role bytes."""

    if (
        delivery_manifest.get("schema_version") != DELIVERY_SCHEMA
        or delivery_manifest.get("status") != "DELIVERED_NO_UPLOAD"
        or delivery_manifest.get("candidate_id") != candidate_id
        or delivery_manifest.get("upload_enabled") is not False
    ):
        raise SongReviewManifestError(
            "prepared Song delivery manifest is not a no-upload intended manifest"
        )
    manifest_artifacts = delivery_manifest.get("artifacts")
    absent = delivery_manifest.get("absent_artifacts")
    if not isinstance(manifest_artifacts, dict) or not isinstance(absent, dict):
        raise SongReviewManifestError(
            "prepared Song delivery manifest artifact inventory is invalid"
        )
    if set(REQUIRED_ROLES) - set(manifest_artifacts) or set(absent) & set(REQUIRED_ROLES):
        raise SongReviewManifestError(
            "prepared Song delivery manifest role inventory is incomplete"
        )
    for role in REQUIRED_ROLES:
        manifest_entry = manifest_artifacts.get(role)
        prepared_entry = entries[role]
        if not isinstance(manifest_entry, dict):
            raise SongReviewManifestError(
                f"prepared delivery manifest {role} entry is invalid"
            )
        target = str(prepared_entry.get("target_path") or "")
        source = str(prepared_entry.get("source_path") or "")
        if (
            manifest_entry.get("path") != target
            or manifest_entry.get("source_path") != source
            or _normalized_sha256(
                manifest_entry.get("sha256"), label=f"prepared delivery {role}"
            )
            != staged[role][1]
            or _normalized_sha256(
                manifest_entry.get("source_sha256"),
                label=f"prepared delivery source {role}",
            )
            != _normalized_sha256(
                prepared_entry.get("source_sha256"),
                label=f"prepared source {role}",
            )
        ):
            raise SongReviewManifestError(
                f"prepared delivery manifest {role} binding drifts"
            )
    if delivery_manifest_path.is_symlink():
        raise SongReviewManifestError("prepared delivery manifest may not be a symlink")


def _emit_portable_song_package(
    package_root: Path,
    *,
    candidate_id: str,
    basename: str,
    title: str,
    artifacts: Mapping[str, tuple[Path, str]],
    delivery_manifest_path: Path,
    delivery_sha: str,
    package_date: str,
    deployed_commit: str,
    completion: Mapping[str, Any],
    generation: Mapping[str, Any],
    batch_status: object,
    delivery_authority: Mapping[str, Any],
    prepared_authority: Mapping[str, Any] | None = None,
    extra_files: tuple[tuple[str, str, Path, str], ...] = (),
) -> dict[str, Any]:
    """Copy validated Song bytes and emit one shared review manifest.

    State-backed delivery and private prepared continuation deliberately use
    different validators and authority inputs, but their portable package
    byte closure is the same.  Keeping this emitter shared prevents a new
    continuation from silently drifting from the existing review contract.
    ``extra_files`` carries prepared-only authority files without making them
    delivery roles or canonical state.
    """

    if package_root.exists():
        raise SongReviewManifestError(
            f"portable package root already exists: {package_root}"
        )
    required = set(REQUIRED_ROLES)
    if required - set(artifacts):
        raise SongReviewManifestError(
            f"portable Song emitter lacks required roles: {sorted(required - set(artifacts))}"
        )
    from src.autoslice.package_cover_network import source_cover_network, validate_package_cover_network

    record = _load_json(artifacts["active_record"][0], label="active Song record")
    publish = _load_json(artifacts["publish"][0], label="Song publish draft")
    network = source_cover_network(record, publish,
        subtitle_path=artifacts["subtitle"][0], cover_path=artifacts["cover"][0])
    package_root.mkdir(parents=True)
    portable_paths: dict[str, Path] = {}
    for role, suffix in REQUIRED_ROLES.items():
        source, sha = artifacts[role]
        destination = package_root / f"{basename}{suffix}"
        _copy_verified(source, destination, sha)
        portable_paths[role] = destination
    portable_delivery_manifest = package_root / f"{basename}.delivery.manifest.json"
    _copy_verified(delivery_manifest_path, portable_delivery_manifest, delivery_sha)

    item_sha = {
        role: _sha256_regular_file(path, label=f"portable {role}")
        for role, path in portable_paths.items()
    }
    item: dict[str, Any] = {
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
        "cover_route_background": portable_paths["cover_route_background"].name,
        "lyrics_alignment_report": portable_paths[
            "lyrics_alignment_report"
        ].name,
        "host_vocal_proof": portable_paths["host_vocal_proof"].name,
        "recut_manifest": portable_paths["recut_manifest"].name,
        "delivery_manifest": portable_delivery_manifest.name,
        "sha256": item_sha,
    }
    for role, (source, expected_sha) in network.items():
        destination = package_root / f"{basename}.cover-{role}.json"
        _copy_verified(source, destination, _normalized_sha256(expected_sha, label=f"cover {role}"))
        item["cover_" + role] = destination.name
        item_sha["cover_" + role] = _sha256_regular_file(destination, label=f"portable cover {role}")
    validate_package_cover_network(package_root, item, record, publish,
        subtitle_path=portable_paths["subtitle"], cover_path=portable_paths["cover"])
    for item_key, suffix, source, expected_sha in extra_files:
        destination = package_root / f"{basename}{suffix}"
        _copy_verified(source, destination, expected_sha)
        item[item_key] = destination.name

    # The final SRT is lyric authority, not the display event list. Preserve
    # the exact ASS used by the verified burn so portable QC can inspect the
    # same word-safe subcues without rewriting canonical lyrics.
    recut = _load_json(artifacts["recut_manifest"][0], label="recut manifest")
    output = recut.get("verified_output_binding")
    burn = output.get("burn_transform") if isinstance(output, dict) else None
    if isinstance(burn, dict) and burn.get("subtitle_style") == "lidousha-final-sapphire72":
        bound = output.get("artifacts") if isinstance(output, dict) else None
        if not isinstance(bound, dict):
            raise SongReviewManifestError("recut proof lacks burn artifacts")
        ass_source = _canonical_file(
            Path(str(bound.get("ass_path") or "")), label="burn ASS"
        )
        ass_sha = _normalized_sha256(bound.get("ass_sha256"), label="burn ASS")
        ass_destination = package_root / f"{basename}.final-sapphire72.ass"
        _copy_verified(ass_source, ass_destination, ass_sha)
        item["ass_path"] = ass_destination.name
        item["ass_sha256"] = "sha256:" + ass_sha

    try:
        host_only_binding = materialize_package_binding(
            root=package_root,
            item=item,
            generation=generation,
            allow_song=True,
        )
    except HostOnlyV4PackageBindingError as exc:
        raise SongReviewManifestError(
            f"HOST_ONLY v4 package binding failed: {exc}"
        ) from exc
    if host_only_binding is not None:
        key = (
            SONG_BINDING_ITEM_KEY
            if host_only_binding.get("schema_version") == SONG_BINDING_SCHEMA_VERSION
            else BINDING_ITEM_KEY
        )
        item[key] = host_only_binding

    attestation = {
        "candidate_id": candidate_id,
        "reference_sha256": generation.get("reference_sha256"),
        "final_cover_sha256": generation.get("final_cover_sha256"),
        "method": generation.get("method"),
        "route_decision": generation.get("route_decision"),
        "reference_authority": generation.get("reference_authority"),
    }
    manifest: dict[str, Any] = {
        "schema_version": REVIEW_SCHEMA,
        "generated_by": "build_song_review_manifest.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "date": package_date,
        "status": "finished_review_package_no_upload_pending_human_review",
        "batch_status": batch_status,
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
        "delivery_authority": copy.deepcopy(dict(delivery_authority)),
        "cover_route_attestations": [attestation],
        "items": [item],
    }
    if prepared_authority is not None:
        manifest["prepared_authority"] = copy.deepcopy(dict(prepared_authority))
    _atomic_json(package_root / "review_manifest.json", manifest)
    return manifest


def build_prepared(
    package_root: Path,
    *,
    prepared_manifest_path: Path,
    prepared_manifest_sha256: object,
    selector_summary_path: Path,
    selector_summary_sha256: object,
    candidate_id: str,
    recording_date: str,
    runtime_root: Path,
    completion_verifier: Callable[
        [dict[str, Any]], dict[str, Any]
    ] = materialized_song_completion,
) -> dict[str, Any]:
    """Build a portable Song package from a preflighted private handle.

    This path intentionally has no autoslice state input.  The selector row,
    prepared transaction and deployed authority seal are the three authorities
    that it retains in the review package; no canonical delivery projection is
    created by this builder.
    """

    if package_root.exists():
        raise SongReviewManifestError(
            f"portable package root already exists: {package_root}"
        )
    if DATE_RE.fullmatch(recording_date) is None:
        raise SongReviewManifestError("prepared Song recording date is invalid")
    try:
        runtime_root = runtime_root.resolve(strict=True)
    except OSError as exc:
        raise SongReviewManifestError(
            f"prepared Song runtime root is missing: {runtime_root}"
        ) from exc
    if not runtime_root.is_dir():
        raise SongReviewManifestError("prepared Song runtime root is not a directory")

    prepared_manifest_source = _canonical_file(
        prepared_manifest_path, label="prepared Song manifest"
    )

    (
        handle,
        checked,
        prepared_document,
        prepared_sha,
        prepared_file_sha,
    ) = _prepared_handle_bindings(
        runtime_root=runtime_root,
        prepared_manifest_path=prepared_manifest_path,
        prepared_manifest_sha256=prepared_manifest_sha256,
    )
    if handle.candidate_id != candidate_id:
        raise SongReviewManifestError(
            "prepared Song handle candidate does not match delivery candidate"
        )
    (
        basename,
        staged,
        prepared_entries,
        _manifest_target,
    ) = _prepared_artifact_bindings(
        checked=checked,
        document=prepared_document,
        candidate_id=candidate_id,
    )
    delivery_manifest_path, delivery_sha = staged["delivery_manifest"]
    delivery_manifest = _load_json(
        delivery_manifest_path, label="prepared Song delivery manifest"
    )
    _validate_prepared_delivery_manifest(
        delivery_manifest_path=delivery_manifest_path,
        delivery_manifest=delivery_manifest,
        staged=staged,
        entries=prepared_entries,
        candidate_id=candidate_id,
    )

    # The sealed active record binds the outer delivery to the selector row.
    # A full-source seeded retry can give those two authorities different IDs.
    active_record = _load_json(staged["active_record"][0], label="active Song record")
    if active_record.get("delivery_candidate_id") != candidate_id:
        raise SongReviewManifestError(
            "active Song record does not match delivery candidate"
        )
    source_candidate_id = active_record.get("source_candidate_id")
    if not isinstance(source_candidate_id, str) or not source_candidate_id:
        raise SongReviewManifestError(
            "active Song record lacks source_candidate_id"
        )
    try:
        selector_record, title, summary_path = _selector_summary_record(
            summary_path=selector_summary_path,
            summary_sha256=str(selector_summary_sha256),
            candidate_id=source_candidate_id,
            date=recording_date,
            base=runtime_root,
        )
    except SongDeliveryError as exc:
        raise SongReviewManifestError(str(exc)) from exc
    selector_sha = _sha256_regular_file(
        summary_path, label="selector summary"
    )
    if selector_sha != _normalized_sha256(
        selector_summary_sha256, label="selector summary"
    ):
        raise SongReviewManifestError("selector summary binding drifted")

    completion = _validate_ready_song_completion(
        selector_record, completion_verifier=completion_verifier
    )
    artifacts: dict[str, tuple[Path, str]] = {
        role: staged[role] for role in REQUIRED_ROLES
    }
    artifacts["delivery_manifest"] = staged["delivery_manifest"]
    _completion_artifact_bindings(
        completion=completion,
        artifacts=artifacts,
    )
    title, _record, generation = _validate_publish_cover_surfaces(
        candidate_id=candidate_id,
        title=title,
        artifacts=artifacts,
    )

    authority = prepared_document.get("deployed_authority")
    original_deployed_commit = authority.get("commit") if isinstance(authority, dict) else None
    from src.autoslice.producer_delivery_transaction import deployment_authority_binding
    deployed_commit = deployment_authority_binding(runtime_root)["commit"]
    if not isinstance(deployed_commit, str) or COMMIT_RE.fullmatch(deployed_commit) is None:
        raise SongReviewManifestError(
            "prepared Song handle lacks a valid deployed commit authority"
        )

    portable_prepared_manifest = package_root / f"{basename}.prepared.json"
    portable_selector_summary = package_root / f"{basename}.selector-summary.json"
    authority_manifest = {
        "schema_version": DELIVERY_SCHEMA,
        "authority_kind": "prepared_no_target",
        "materialized": False,
        "manifest": f"{basename}.delivery.manifest.json",
        "manifest_sha256": f"sha256:{delivery_sha}",
        "prepared_manifest": portable_prepared_manifest.name,
        "prepared_manifest_sha256": f"sha256:{prepared_sha}",
        "prepared_manifest_file_sha256": f"sha256:{prepared_file_sha}",
        "selector_summary": portable_selector_summary.name,
        "selector_summary_path": str(summary_path),
        "selector_summary_sha256": f"sha256:{selector_sha}",
        "date": recording_date,
        "song_completion_evidence": completion,
        "upload_enabled": False,
    }
    prepared_authority = {
        "status": "PREPARED_NO_TARGET",
        "manifest": portable_prepared_manifest.name,
        "manifest_sha256": f"sha256:{prepared_sha}",
        "manifest_file_sha256": f"sha256:{prepared_file_sha}",
        "selector_summary": portable_selector_summary.name,
        "selector_summary_sha256": f"sha256:{selector_sha}",
        "target_materialized": False,
        "original_deployed_commit": original_deployed_commit,
        "consumer_deployed_commit": deployed_commit,
        "upload_enabled": False,
    }
    return _emit_portable_song_package(
        package_root,
        candidate_id=candidate_id,
        basename=basename,
        title=title,
        artifacts=artifacts,
        delivery_manifest_path=delivery_manifest_path,
        delivery_sha=delivery_sha,
        package_date=recording_date,
        deployed_commit=deployed_commit,
        completion=completion,
        generation=generation,
        batch_status="prepared_no_target",
        delivery_authority=authority_manifest,
        prepared_authority=prepared_authority,
        extra_files=(
            ("prepared_manifest", ".prepared.json", prepared_manifest_source, prepared_file_sha),
            ("selector_summary", ".selector-summary.json", summary_path, selector_sha),
        ),
    )


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
    _atomic_bytes(path, _json_bytes(payload))


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _atomic_bytes(path: Path, body: bytes) -> None:
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
        directory_flags = os.O_RDONLY | getattr(
            os, "O_DIRECTORY", 0
        )
        directory_descriptor = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        temporary = None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)


@contextmanager
def _runner_lock_for_state(state_path: Path):
    lock_path = state_path.parent.parent / "runner.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(
                handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
            )
        except OSError as exc:
            raise SongReviewManifestError(
                f"runner.lock is busy: {lock_path}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_json_closure(
    targets: list[tuple[Path, Mapping[str, Any]]],
    *,
    writer: Callable[[Path, bytes], None] = _atomic_bytes,
) -> None:
    """Commit a small ordered JSON closure, rolling back caught failures.

    Source and delivery records are installed before the delivery manifest;
    state is last.  This preserves the manifest-last contract at both the
    forward and rollback boundary.  Every target must already be a verified
    regular file, so this repair cannot manufacture a parallel authority.
    """

    originals: dict[Path, bytes] = {}
    intended: list[tuple[Path, bytes]] = []
    seen: set[Path] = set()
    for path, payload in targets:
        canonical = _canonical_file(path, label="upload-tags closure target")
        if canonical in seen:
            raise SongReviewManifestError(
                f"duplicate upload-tags closure target: {canonical}"
            )
        seen.add(canonical)
        originals[canonical] = canonical.read_bytes()
        intended.append((canonical, _json_bytes(payload)))
    try:
        for path, body in intended:
            writer(path, body)
        for path, body in intended:
            if path.read_bytes() != body:
                raise OSError(
                    f"upload-tags closure verification failed: {path}"
                )
    except BaseException as exc:
        rollback_failures: list[str] = []
        for path, _body in intended:
            try:
                writer(path, originals[path])
            except BaseException as rollback_exc:
                rollback_failures.append(
                    f"{path}: {type(rollback_exc).__name__}: {rollback_exc}"
                )
        if rollback_failures:
            raise SongReviewManifestError(
                "upload-tags closure failed and rollback was incomplete: "
                + "; ".join(rollback_failures)
            ) from exc
        raise SongReviewManifestError(
            f"upload-tags closure commit failed and was rolled back: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _refresh_upload_tags_closure(
    *,
    title: str,
    state_path: Path,
    recording_date: str,
    state: dict[str, Any],
    state_row: dict[str, Any],
    delivery_manifest_path: Path,
    delivery_manifest: dict[str, Any],
    artifacts: Mapping[str, tuple[Path, str]],
    tag_generator: Callable[..., dict],
    closure_writer: Callable[[Path, bytes], None] = _atomic_bytes,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Refresh source/delivery/state tag authority from frozen title + SRT."""

    subtitle_path = artifacts["subtitle"][0]
    manifest_artifacts = delivery_manifest.get("artifacts")
    active_entry = (
        manifest_artifacts.get("active_record")
        if isinstance(manifest_artifacts, dict)
        else None
    )
    if not isinstance(active_entry, dict):
        raise SongReviewManifestError(
            "delivery manifest lacks active_record tag authority"
        )
    delivery_record_path = _canonical_file(
        Path(str(active_entry.get("path") or "")),
        label="delivery active record",
    )
    source_record_path = _canonical_file(
        Path(str(active_entry.get("source_path") or "")),
        label="source active record",
    )
    source_record = _load_json(source_record_path, label="source active record")
    delivery_record = _load_json(
        delivery_record_path, label="delivery active record"
    )
    if source_record != delivery_record:
        raise SongReviewManifestError(
            "source/delivery active record bytes are not semantically identical"
        )
    current_tags = source_record.get("upload_tags")
    if current_tags is not None:
        try:
            _validated_song_upload_tags(current_tags)
        except SongDeliveryError as exc:
            raise SongReviewManifestError(
                "source record already has invalid upload_tags"
            ) from exc
        return (
            state,
            delivery_manifest,
            _sha256_regular_file(
                delivery_manifest_path,
                label="verified Song delivery manifest",
            ),
        )
    try:
        upload_tags = _validated_song_upload_tags(
            tag_generator(title, subtitle_path, timeout=180.0)
        )
    except SongDeliveryError as exc:
        raise SongReviewManifestError(str(exc)) from exc

    updated_record = copy.deepcopy(source_record)
    updated_record["upload_tags"] = upload_tags
    record_bytes = _json_bytes(updated_record)
    record_sha = "sha256:" + hashlib.sha256(record_bytes).hexdigest()

    updated_manifest = copy.deepcopy(delivery_manifest)
    updated_active = updated_manifest["artifacts"]["active_record"]
    updated_active["sha256"] = record_sha
    updated_active["source_sha256"] = record_sha
    manifest_bytes = _json_bytes(updated_manifest)
    manifest_sha = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()

    updated_state = copy.deepcopy(state)
    updated_state_row = _candidate_row(
        updated_state,
        str(state_row.get("candidate_id") or ""),
        recording_date=recording_date,
    )
    state_sidecar_hashes = updated_state_row.get("delivered_sidecar_hashes")
    if not isinstance(state_sidecar_hashes, dict):
        raise SongReviewManifestError(
            "state candidate lacks delivered sidecar hashes"
        )
    state_sidecar_hashes["active_record"] = record_sha
    updated_state_row["delivery_manifest_sha256"] = manifest_sha

    _write_json_closure(
        [
            (source_record_path, updated_record),
            (delivery_record_path, updated_record),
            (delivery_manifest_path, updated_manifest),
            (state_path, updated_state),
        ],
        writer=closure_writer,
    )
    return updated_state, updated_manifest, manifest_sha.removeprefix(
        "sha256:"
    )


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
    refresh_upload_tags: bool = False,
    tag_generator: Callable[..., dict] = generate_upload_tags,
    closure_writer: Callable[[Path, bytes], None] = _atomic_bytes,
) -> dict[str, Any]:
    if package_root.exists():
        raise SongReviewManifestError(
            f"portable package root already exists: {package_root}"
        )
    delivery_manifest_path = _canonical_file(
        delivery_manifest_path, label="verified Song delivery manifest"
    )
    state_path = _canonical_file(state_path, label="autoslice state")
    recording_date = state_path.stem
    if DATE_RE.fullmatch(recording_date) is None:
        raise SongReviewManifestError("autoslice state path has an invalid recording date")
    deployed_commit_file = _canonical_file(
        deployed_commit_file, label="deployed commit"
    )
    deployed_commit = deployed_commit_file.read_text(encoding="utf-8").split()[0]
    if COMMIT_RE.fullmatch(deployed_commit) is None:
        raise SongReviewManifestError("deployed commit file is invalid")

    state = _load_json(state_path, label="autoslice state")
    state_row = _candidate_row(state, candidate_id, recording_date=recording_date)
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
    if refresh_upload_tags:
        state, delivery_manifest, delivery_sha = (
            _refresh_upload_tags_closure(
                title=title,
                state_path=state_path,
                recording_date=recording_date,
                state=state,
                state_row=state_row,
                delivery_manifest_path=delivery_manifest_path,
                delivery_manifest=delivery_manifest,
                artifacts=artifacts,
                tag_generator=tag_generator,
                closure_writer=closure_writer,
            )
        )
        state_row = _candidate_row(state, candidate_id, recording_date=recording_date)
        artifacts = _validate_delivery_artifacts(
            manifest_path=delivery_manifest_path,
            manifest=delivery_manifest,
            state_row=state_row,
        )
        artifacts = dict(artifacts)
        artifacts["delivery_manifest"] = (
            delivery_manifest_path,
            delivery_sha,
        )
        title, _record, generation = _validate_publish_cover_surfaces(
            candidate_id=candidate_id,
            state_row=state_row,
            artifacts=artifacts,
        )

    basename = _delivery_basename(delivery_manifest_path)
    package_date = delivery_manifest_path.parent.name
    if DATE_RE.fullmatch(package_date) is None:
        package_date = state_path.stem
    if DATE_RE.fullmatch(package_date) is None:
        raise SongReviewManifestError("cannot derive a canonical package date")

    delivery_authority = {
        "schema_version": DELIVERY_SCHEMA,
        "manifest": f"{basename}.delivery.manifest.json",
        "manifest_sha256": f"sha256:{delivery_sha}",
        "state_sha256": "sha256:"
        + _sha256_regular_file(state_path, label="autoslice state"),
        "selector_summary_sha256": state_row.get("selector_summary_sha256"),
        "song_completion_evidence": completion,
        "upload_enabled": False,
    }
    return _emit_portable_song_package(
        package_root,
        candidate_id=candidate_id,
        basename=basename,
        title=title,
        artifacts=artifacts,
        delivery_manifest_path=delivery_manifest_path,
        delivery_sha=delivery_sha,
        package_date=package_date,
        deployed_commit=deployed_commit,
        completion=completion,
        generation=generation,
        batch_status=state.get("status"),
        delivery_authority=delivery_authority,
    )


def _replacement_package_file(root: Path, value: object, *, label: str) -> Path:
    relative = Path(str(value or ""))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise SongReviewManifestError(f"{label} must be package-relative")
    path = root / relative
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise SongReviewManifestError(f"{label} may not traverse a symlink")
    return _canonical_file(path, label=label)


def _audio_packet_sha256(path: Path) -> str:
    """Hash compressed packets, exactly as the frozen burn validator does."""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0",
         "-c", "copy", "-f", "hash", "-hash", "sha256", "-"],
        capture_output=True, text=True, timeout=900, check=False,
    )
    if result.returncode or not re.fullmatch(r"SHA256=[0-9a-f]{64}", result.stdout.strip()):
        raise SongReviewManifestError(f"audio packet hash cannot be computed: {path}")
    return result.stdout.strip()


def _same_bv_replacement_plan(
    package_root: Path, *, original_package_root: Path,
    replacement_video: Path, replacement_ass: Path,
    ass_comparison_path: Path, burn_validation_path: Path,
    first_publication_authority_path: Path,
    origin_video: Path | None = None, origin_ass: Path | None = None,
    generated_at: str | None = None,
) -> tuple[dict[str, JsonValue], dict[str, bytes], dict[str, tuple[Path, str]], dict[Path, str]]:
    """Derive the sole allowed projection without materializing a package.

    Consumers replay the constructor's exact byte/evidence checks. Historical
    input locators remain provenance when the checked bytes live in a package.
    """
    from src.autoslice.new_bv_repair_authority import (
        validate_new_bv_repair_authority, replay_first_publication_manifest,
    )
    from src.autoslice.subtitle_rendering import _write_sapphire_ass_from_srt

    package_root = Path(os.path.abspath(package_root))
    original_package_root = Path(os.path.abspath(original_package_root))
    if (package_root.is_relative_to(original_package_root)
            or original_package_root.is_relative_to(package_root)
            or any(p.is_symlink() for p in (original_package_root, *original_package_root.parents,
                                            *package_root.parents))):
        raise SongReviewManifestError("replacement root must be independent and symlink-free")
    input_evidence = {
        "ass-comparison.json": ass_comparison_path,
        "burn-validation.json": burn_validation_path,
        "first-publication-authority.json": first_publication_authority_path,
    }
    input_evidence_hashes = {
        name: _sha256_regular_file(path, label=name) for name, path in input_evidence.items()
    }
    review_path = _replacement_package_file(original_package_root, "review_manifest.json", label="original review")
    manifest = _load_json(review_path, label="original review")
    items = manifest.get("items")
    if (manifest.get("schema_version") != REVIEW_SCHEMA or not isinstance(items, list)
            or len(items) != 1 or not isinstance(items[0], dict)
            or str(items[0].get("classification", "")).lower() != "song"):
        raise SongReviewManifestError("original package must contain exactly one Song")
    item = items[0]
    candidate_id = str(item.get("candidate_id") or "")
    title = str(item.get("title") or "")
    try:
        authority = validate_new_bv_repair_authority(
            _load_json(first_publication_authority_path, label="first-publication authority"),
            candidate_id=candidate_id, expected_final_title=title,
        )
        published = replay_first_publication_manifest(authority)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise SongReviewManifestError(f"first-publication authority invalid: {exc}") from exc
    attestation = published.get("package_attestation") or {}
    if (Path(str(attestation.get("package_root") or "")).resolve() != original_package_root
            or authority["recording_date"] != manifest.get("date")
            or authority["observed_public_title"] != title):
        raise SongReviewManifestError("first-publication authority original package/date/title mismatch")

    paths = {role: _replacement_package_file(original_package_root, item[key], label=f"original {role}")
             for role, key in (("video", "video"), ("cover", "cover"), ("subtitle", "subtitle_srt"),
                               ("record", "record"), ("ass", "ass_path"), ("recut", "recut_manifest"),
                               ("publish", "publish_json"))}
    for role, entry in (("video", published.get("video")), ("cover", published.get("cover")),
                        ("subtitle", attestation.get("subtitle")), ("record", attestation.get("record"))):
        if not isinstance(entry, dict) or Path(str(entry.get("path") or "")).resolve() != paths[role]:
            raise SongReviewManifestError(f"first-publication original {role} locator mismatch")
        _matches(paths[role], entry.get("sha256"), label=f"first-publication original {role}")
    original_review_binding = attestation.get("review_manifest")
    if not isinstance(original_review_binding, dict):
        raise SongReviewManifestError("first-publication original review binding missing")
    _matches(review_path, original_review_binding.get("sha256"), label="first-publication original review")
    role_keys = {"subtitle": "subtitle_srt", "active_record": "record", "publish": "publish_json"}
    for role in REQUIRED_ROLES:
        source = _replacement_package_file(original_package_root, item.get(role_keys.get(role, role)),
                                           label=f"original {role}")
        _matches(source, (item.get("sha256") or {}).get(role), label=f"original {role}")
    for role, key in (("video", "video"), ("cover", "cover"), ("subtitle", "subtitle"),
                      ("record", "active_record"), ("publish", "publish"), ("recut", "recut_manifest")):
        _matches(paths[role], (item.get("sha256") or {}).get(key), label=f"original {role}")
    _matches(paths["ass"], item.get("ass_sha256"), label="original ASS")
    replacement_video = _canonical_file(replacement_video, label="replacement MP4")
    replacement_ass = _canonical_file(replacement_ass, label="replacement ASS")
    video_sha = _sha256_regular_file(replacement_video, label="replacement MP4")
    ass_sha = _sha256_regular_file(replacement_ass, label="replacement ASS")
    origin_video = origin_video or replacement_video
    origin_ass = origin_ass or replacement_ass
    comparison = _load_json(ass_comparison_path, label="ASS comparison")
    burn = _load_json(burn_validation_path, label="burn validation")
    for key, path in (("srt", paths["subtitle"]), ("original_ass", paths["ass"]), ("new_ass", replacement_ass)):
        expected_locator = origin_ass if key == "new_ass" else path
        if Path(str(comparison.get(key + "_path") or "")).resolve() != expected_locator:
            raise SongReviewManifestError(f"ASS comparison {key} locator mismatch")
        _matches(path, comparison.get(key + "_sha256"), label=f"ASS comparison {key}")
    old_lines = paths["ass"].read_text(encoding="utf-8").splitlines(keepends=True)
    new_lines = replacement_ass.read_text(encoding="utf-8").splitlines(keepends=True)
    changes = [i for i, (a, b) in enumerate(zip(old_lines, new_lines), 1) if a != b]
    lyrics = lambda lines: [line for line in lines if line.startswith("Dialogue:") and line.split(",", 9)[3] == "Default"]
    if (comparison.get("status") != "PASS" or len(old_lines) != len(new_lines)
            or changes != comparison.get("changed_lines")
            or lyrics(old_lines) != lyrics(new_lines)
            or len(lyrics(new_lines)) != comparison.get("lyric_events_identical")):
        raise SongReviewManifestError("ASS differences do not equal the comparison evidence")
    for i in changes:
        left, right = old_lines[i-1].rstrip("\n").split(",", 9), new_lines[i-1].rstrip("\n").split(",", 9)
        if (len(left) != 10 or len(right) != 10 or left[:9] != right[:9]
                or right[3] != comparison.get("speech_style") or right[3] != "LDS"
                or left[9] != "[李豆沙] " + right[9]):
            raise SongReviewManifestError("ASS comparison permits only speech display-prefix removal")
    if len(changes) != comparison.get("speech_cues"):
        raise SongReviewManifestError("ASS speech cue count mismatch")
    with tempfile.TemporaryDirectory(prefix="song-replacement-ass-") as temporary:
        expected = Path(temporary) / "expected.ass"
        _write_sapphire_ass_from_srt(paths["subtitle"], expected)
        if expected.read_bytes() != replacement_ass.read_bytes():
            raise SongReviewManifestError("replacement ASS differs from current canonical SRT projection")
    recut = _load_json(paths["recut"], label="original recut")
    output = recut.get("verified_output_binding") or {}
    bound = output.get("artifacts") or {}
    if (output.get("schema_version") != "verified-song-output-binding.v1"
            or (output.get("burn_transform") or {}).get("subtitle_style") != "lidousha-final-sapphire72"):
        raise SongReviewManifestError("original verified Song burn binding missing")
    _matches(paths["video"], bound.get("burned_media_sha256"), label="original bound video")
    _matches(paths["ass"], bound.get("ass_sha256"), label="original bound ASS")
    _matches(paths["subtitle"], bound.get("subtitle_sha256"), label="original bound SRT")
    if (burn.get("status") != "PASS" or burn.get("full_decode_rc") != 0
            or burn.get("full_decode_stderr") != "" or burn.get("branding_intro") is not None
            or burn.get("interval") != output.get("interval")
            or Path(str(burn.get("target") or "")).resolve() != origin_video
            or burn.get("target_bytes") != replacement_video.stat().st_size):
        raise SongReviewManifestError("burn validation status/target/interval mismatch")
    for path, expected, label in ((replacement_video, burn.get("target_sha256"), "burn target"),
                                   (replacement_ass, burn.get("ass_sha256"), "burn ASS"),
                                   (paths["video"], burn.get("original_published_sha256"), "burn original")):
        _matches(path, expected, label=label)
    audio = burn.get("audio_packet_hashes")
    if not isinstance(audio, dict):
        raise SongReviewManifestError("burn audio packet evidence missing")
    old_audio, new_audio = _audio_packet_sha256(paths["video"]), _audio_packet_sha256(replacement_video)
    if (old_audio != new_audio or audio.get(str(paths["video"])) != old_audio
            or audio.get(str(origin_video)) != new_audio):
        raise SongReviewManifestError("replacement audio packet hash differs from original/evidence")

    # Snapshot before creating anything. Only review-referenced files are copied;
    # upload receipts, old audits and publication side effects are not cloned.
    inventory: dict[str, str] = {}
    def collect(value: object) -> None:
        if isinstance(value, dict):
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
        elif isinstance(value, str) and value and not Path(value).is_absolute():
            relative = Path(value)
            if ".." in relative.parts:
                return
            try:
                is_file = (original_package_root / relative).is_file()
            except OSError as exc:
                # Observation prose is also present in this manifest. It need
                # not be a legal filename; required artifact roles were already
                # checked explicitly above. Preserve prose without stat'ing it
                # into a fatal path-length error.
                if exc.errno != errno.ENAMETOOLONG:
                    raise
                return
            if is_file:
                path = _replacement_package_file(original_package_root, value, label="original reference")
                inventory[value] = _sha256_regular_file(path, label="original reference")
    collect(manifest)
    inventory["review_manifest.json"] = _sha256_regular_file(review_path, label="original review")
    evidence_inputs = {"original-review.json": review_path, "original-record.json": paths["record"],
                       "original-recut.json": paths["recut"], "original-ass.ass": paths["ass"],
                       **input_evidence}
    for name, path in input_evidence.items():
        _matches(path, input_evidence_hashes[name], label=name)
    frozen_evidence = {
        name: (_canonical_file(path, label=name), input_evidence_hashes[name]
               if name in input_evidence_hashes else _sha256_regular_file(path, label=name))
        for name, path in evidence_inputs.items()
    }
    manifest = copy.deepcopy(manifest)
    item = manifest["items"][0]
    record = _load_json(paths["record"], label="original record")
    publish = _load_json(paths["publish"], label="original publish")
    if record.get("burned_preview", {}).get("status") != "BURNED":
        raise SongReviewManifestError("original BURNED receipt missing")
    if (record.get("upload_tags", {}).get("final_tags") is not None
            and record["upload_tags"]["final_tags"] != published.get("tags")):
        raise SongReviewManifestError("first-publication tags differ from original record")
    # Update only byte/locator projections. No ready/PASS/content decision is set.
    record["artifact_hashes"]["burned_video_sha256"] = "sha256:" + video_sha
    record["artifact_hashes"]["ass_sha256"] = "sha256:" + ass_sha
    record["burned_preview"].update(path=item["video"], ass_path=item["ass_path"], burned_sha256="sha256:" + video_sha)
    for document in (record, record["publish_staging"], publish):
        document["recovery_publication_authority"] = copy.deepcopy(authority)
    if isinstance(publish.get("artifact_hashes"), dict):
        publish["artifact_hashes"]["burned_video_sha256"] = "sha256:" + video_sha
        if "ass_sha256" in publish["artifact_hashes"]:
            publish["artifact_hashes"]["ass_sha256"] = "sha256:" + ass_sha
    bound.update(burned_media_sha256="sha256:" + video_sha, ass_sha256="sha256:" + ass_sha,
                 ass_path=item["ass_path"])
    if "burned_media_path" in bound:
        bound["burned_media_path"] = item["video"]
    evidence = {name: {"path": "replacement-evidence/" + name, "sha256": "sha256:" + sha}
                for name, (_, sha) in frozen_evidence.items()}
    provenance = {"schema_version": "song-same-bv-replacement-package.v1", "candidate_id": candidate_id,
                  "bvid": authority["bvid"], "aid": authority["aid"], "cid": authority["cid"],
                  "original_package_root": str(original_package_root), "evidence": evidence,
                  "replacement_video": {"path": item["video"], "sha256": "sha256:" + video_sha},
                  "replacement_ass": {"path": item["ass_path"], "sha256": "sha256:" + ass_sha},
                  "audio_packet_sha256": old_audio, "upload_enabled": False}
    provenance["first_publication_metadata"] = {
        key: copy.deepcopy(published.get(key))
        for key in ("title", "description", "tags", "season", "publish_policy")
    }
    # Historical producer proofs stay in the immutable source snapshot. Current
    # artifact projections consistently describe the validated replacement.
    for document in (recut, record):
        if isinstance(document.get("artifact_hashes"), dict):
            document["artifact_hashes"].update(
                burned_video_sha256="sha256:" + video_sha, ass_sha256="sha256:" + ass_sha)
        if isinstance(document.get("burned_preview"), dict):
            preview = document["burned_preview"]
            preview.update(path=item["video"], ass_path=item["ass_path"], burned_sha256="sha256:" + video_sha)
            if "command" in preview:
                preview["source_command"] = preview.pop("command")
            preview["replacement_validation"] = evidence["burn-validation.json"]
    source_burn = output["burn_transform"]
    if "command" in source_burn:
        source_burn["source_command"] = source_burn.pop("command")
    source_burn["replacement_validation"] = evidence["burn-validation.json"]
    recut["verified_output_binding"] = copy.deepcopy(output)
    record["verified_output_binding"] = copy.deepcopy(output)
    recut["same_bv_replacement"] = copy.deepcopy(provenance)
    record["same_bv_replacement"] = copy.deepcopy(provenance)
    item["same_bv_replacement"] = provenance
    item["recovery_publication_authority"] = copy.deepcopy(authority)
    item["ass_sha256"] = "sha256:" + ass_sha
    manifest["recovery_publication_authorities_by_candidate"] = {candidate_id: authority}
    manifest["upload_allowed"] = False
    for key in ("delivery_authority", "prepared_authority"):
        if key in manifest:
            manifest["source_" + key] = manifest.pop(key)
    manifest["generated_by"] = "build_song_review_manifest.same_bv_replacement.v1"
    manifest["generated_at"] = generated_at or datetime.now(timezone.utc).isoformat()
    documents = {
        item["recut_manifest"]: _json_bytes(recut),
        item["publish_json"]: _json_bytes(publish),
    }
    if "manifest_path" in record:
        record["manifest_path"] = item["recut_manifest"]
    if "manifest_sha256" in record:
        record["manifest_sha256"] = "sha256:" + hashlib.sha256(documents[item["recut_manifest"]]).hexdigest()
    record["artifact_hashes"]["publish_draft_sha256"] = "sha256:" + hashlib.sha256(documents[item["publish_json"]]).hexdigest()
    documents[item["record"]] = _json_bytes(record)
    for role, key in (("video", "video"), ("active_record", "record"),
                      ("publish", "publish_json"), ("recut_manifest", "recut_manifest")):
        item["sha256"][role] = (video_sha if role == "video" else
                               hashlib.sha256(documents[item[key]]).hexdigest())
    documents["review_manifest.json"] = _json_bytes(manifest)
    replaced = {"review_manifest.json", item["video"], item["ass_path"],
                item["record"], item["publish_json"], item["recut_manifest"]}
    copies = {relative: (original_package_root / relative, sha)
              for relative, sha in inventory.items() if relative not in replaced}
    copies.update({evidence[name]["path"]: (source, sha)
                   for name, (source, sha) in frozen_evidence.items()})
    copies[item["video"]] = (replacement_video, video_sha)
    copies[item["ass_path"]] = (replacement_ass, ass_sha)
    originals = {original_package_root / relative: sha for relative, sha in inventory.items()}
    return manifest, documents, copies, originals


def build_same_bv_replacement(
    package_root: Path, *, original_package_root: Path,
    replacement_video: Path, replacement_ass: Path,
    ass_comparison_path: Path, burn_validation_path: Path,
    first_publication_authority_path: Path,
) -> dict[str, JsonValue]:
    """Create a private replacement using the same projection consumers replay."""
    package_root = Path(os.path.abspath(package_root))
    if package_root.exists() or package_root.is_symlink():
        raise SongReviewManifestError(f"portable package root already exists: {package_root}")
    manifest, documents, copies, originals = _same_bv_replacement_plan(
        package_root, original_package_root=original_package_root,
        replacement_video=replacement_video, replacement_ass=replacement_ass,
        ass_comparison_path=ass_comparison_path, burn_validation_path=burn_validation_path,
        first_publication_authority_path=first_publication_authority_path,
    )
    package_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        for relative, (source, sha) in copies.items():
            target = package_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_verified(source, target, sha)
        for relative, body in documents.items():
            _atomic_bytes(package_root / relative, body)
        # Recheck every source after materialization, including copied evidence.
        for relative, (source, sha) in copies.items():
            _matches(source, sha, label=f"replacement source unchanged: {relative}")
        for source, sha in originals.items():
            _matches(source, sha, label="original package unchanged")
    except BaseException:
        shutil.rmtree(package_root)
        raise
    return manifest


def validate_same_bv_replacement_package(package_root: Path) -> dict[str, JsonValue]:
    """Read-only replay of the constructor, retaining historical Song proofs.

    No new completion is issued: the source envelope must independently pass
    the ordinary verified-Song consumer, and this plan permits only the proven
    audio-identical display projection and its derived byte bindings.
    """
    root = Path(os.path.abspath(package_root))
    manifest = _load_json(_replacement_package_file(root, "review_manifest.json", label="replacement review"),
                          label="replacement review")
    items = manifest.get("items")
    if (manifest.get("generated_by") != "build_song_review_manifest.same_bv_replacement.v1"
            or not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict)):
        raise SongReviewManifestError("replacement envelope invalid")
    item = items[0]
    provenance = item.get("same_bv_replacement")
    if not isinstance(provenance, dict) or provenance.get("schema_version") != "song-same-bv-replacement-package.v1":
        raise SongReviewManifestError("replacement provenance missing")
    evidence = provenance.get("evidence")
    if not isinstance(evidence, dict):
        raise SongReviewManifestError("replacement evidence missing")
    def evidence_file(name: str) -> Path:
        binding = evidence.get(name)
        if not isinstance(binding, dict):
            raise SongReviewManifestError(f"replacement evidence missing: {name}")
        path = _replacement_package_file(root, binding.get("path"), label=name)
        _matches(path, binding.get("sha256"), label=name)
        return path
    comparison_path = evidence_file("ass-comparison.json")
    burn_path = evidence_file("burn-validation.json")
    comparison = _load_json(comparison_path, label="ASS comparison")
    burn = _load_json(burn_path, label="burn validation")
    original = Path(str(provenance.get("original_package_root") or ""))
    origin_video = Path(str(burn.get("target") or ""))
    origin_ass = Path(str(comparison.get("new_ass_path") or ""))
    if not all(path.is_absolute() for path in (original, origin_video, origin_ass)):
        raise SongReviewManifestError("replacement origin locator invalid")
    expected, documents, copies, originals = _same_bv_replacement_plan(
        root, original_package_root=original,
        replacement_video=_replacement_package_file(root, item.get("video"), label="replacement MP4"),
        replacement_ass=_replacement_package_file(root, item.get("ass_path"), label="replacement ASS"),
        ass_comparison_path=comparison_path, burn_validation_path=burn_path,
        first_publication_authority_path=evidence_file("first-publication-authority.json"),
        origin_video=origin_video, origin_ass=origin_ass, generated_at=manifest.get("generated_at"),
    )
    if expected != manifest:
        raise SongReviewManifestError("replacement manifest projection drift")
    for relative, body in documents.items():
        # The manifest is compared structurally; all derived role bytes are exact.
        if relative != "review_manifest.json" and _replacement_package_file(root, relative, label=relative).read_bytes() != body:
            raise SongReviewManifestError(f"replacement document projection drift: {relative}")
    for relative, (_source, sha) in copies.items():
        _matches(_replacement_package_file(root, relative, label=relative), sha, label=relative)
    for source, sha in originals.items():
        _matches(source, sha, label="original package unchanged")
    return manifest


def run_canonical_auditor(package_root: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        str(ROOT / "scripts/audit_review_package.py"),
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
    parser.add_argument("--original-published-package", type=Path)
    parser.add_argument("--replacement-video", type=Path)
    parser.add_argument("--replacement-ass", type=Path)
    parser.add_argument("--ass-comparison", type=Path)
    parser.add_argument("--burn-validation", type=Path)
    parser.add_argument("--first-publication-authority", type=Path)
    parser.add_argument("--delivery-manifest", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--candidate", "--candidate-id", dest="candidate")
    parser.add_argument("--deployed-commit-file", type=Path)
    parser.add_argument(
        "--prepared-manifest",
        type=Path,
        help="private producer-prepared Song manifest for a no-target package",
    )
    parser.add_argument(
        "--prepared-manifest-sha256",
        help="sha256 digest from prepared_delivery.prepared_sha256",
    )
    parser.add_argument(
        "--selector-summary",
        type=Path,
        help="hash-bound completed Song selector summary",
    )
    parser.add_argument(
        "--selector-summary-sha256",
        help="sha256 digest of the selector summary",
    )
    parser.add_argument(
        "--date",
        help="recording date bound to the selector summary (prepared mode)",
    )
    parser.add_argument(
        "--base",
        type=Path,
        default=Path(os.environ.get("AUTOSLICE_BASE", "/opt/bilive/autoslice")),
        help="runtime BASE for prepared mode; defaults to AUTOSLICE_BASE",
    )
    parser.add_argument(
        "--refresh-upload-tags",
        action="store_true",
        help=(
            "generate current tags from the frozen title/SRT and update the "
            "source+delivery+state hash closure before creating the package"
        ),
    )
    parser.add_argument(
        "--audit-out",
        type=Path,
        default=None,
        help="default: <package_root>/package_audit.json",
    )
    args = parser.parse_args()
    try:
        prepared_values = (
            args.prepared_manifest,
            args.prepared_manifest_sha256,
            args.selector_summary,
            args.selector_summary_sha256,
            args.date,
        )
        state_values = (
            args.delivery_manifest,
            args.state,
            args.deployed_commit_file,
        )
        using_prepared = any(value is not None for value in prepared_values)
        replacement_values = (
            args.original_published_package, args.replacement_video, args.replacement_ass,
            args.ass_comparison, args.burn_validation, args.first_publication_authority,
        )
        if any(value is not None for value in replacement_values):
            if (any(value is None for value in replacement_values) or using_prepared
                    or any(value is not None for value in state_values)
                    or args.candidate is not None or args.refresh_upload_tags
                    or args.base != Path(os.environ.get("AUTOSLICE_BASE", "/opt/bilive/autoslice"))):
                parser.error("same-BV replacement requires all six replacement arguments and no state/prepared arguments")
            build_same_bv_replacement(
                args.package_root, original_package_root=args.original_published_package,
                replacement_video=args.replacement_video, replacement_ass=args.replacement_ass,
                ass_comparison_path=args.ass_comparison, burn_validation_path=args.burn_validation,
                first_publication_authority_path=args.first_publication_authority,
            )
        elif using_prepared:
            if any(value is None for value in prepared_values) or args.candidate is None:
                parser.error(
                    "prepared mode requires --prepared-manifest, "
                    "--prepared-manifest-sha256, --selector-summary, "
                    "--selector-summary-sha256, --date, and --candidate"
                )
            if args.refresh_upload_tags:
                parser.error("prepared mode cannot refresh upload tags")
            if any(value is not None for value in state_values):
                parser.error(
                    "prepared mode cannot be combined with state/delivery arguments"
                )
            build_prepared(
                args.package_root,
                prepared_manifest_path=args.prepared_manifest,
                prepared_manifest_sha256=args.prepared_manifest_sha256,
                selector_summary_path=args.selector_summary,
                selector_summary_sha256=args.selector_summary_sha256,
                candidate_id=args.candidate,
                recording_date=args.date,
                runtime_root=args.base,
            )
        else:
            if any(value is None for value in state_values) or args.candidate is None:
                parser.error(
                    "state mode requires --delivery-manifest, --state, "
                    "--candidate, and --deployed-commit-file"
                )
            if args.date is not None or args.base != Path(
                os.environ.get("AUTOSLICE_BASE", "/opt/bilive/autoslice")
            ):
                parser.error("--date and --base are only valid in prepared mode")
            lock = (
                _runner_lock_for_state(args.state)
                if args.refresh_upload_tags
                else nullcontext()
            )
            with lock:
                build(
                    args.package_root,
                    delivery_manifest_path=args.delivery_manifest,
                    state_path=args.state,
                    candidate_id=args.candidate,
                    deployed_commit_file=args.deployed_commit_file,
                    refresh_upload_tags=args.refresh_upload_tags,
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
