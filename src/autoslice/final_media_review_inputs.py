"""Autonomous, hash-bound preflight for final-media perceptual review.

This module closes one deliberately narrow gap between mechanical package
checks and a real audio/visual review:

* exact media identity and clock are explicit inputs;
* diagnostic WAV windows are checked from their RIFF headers (no ASR and no
  claim that anybody listened);
* sparse frames/contact sheets are never promoted to continuous visual
  coverage;
* independently verified asset-integrity receipts may be reused without
  re-decoding or regenerating media;
* a content-bound state file prevents restarts from duplicating a provider
  call, and preserves typed retry/backoff state;
* the canonical CPA runtime/producer transport is prepared only after the raw
  perceptual inputs are actually sufficient.

The assessment itself can only say whether inputs are suitable for a future
perceptual reviewer.  It can never emit a content PASS or impersonate a human
playback receipt.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Callable, Mapping
import wave

from src.autoslice.cpa_runtime import (
    build_cpa_qa_command,
    resolve_cpa_env_path,
)
from src.autoslice.final_media_review_transport_evidence import (
    FinalMediaReviewTransportEvidenceError,
    validate_transport_evidence,
)
from src.autoslice.llm_client import LlmCallError, sanitize_provider_diagnostics
from src.autoslice.producer_final_review_transport import (
    build_final_review_llm_call,
)


JOB_SCHEMA_VERSION = "final-media-review-input-job.v1"
ASSESSMENT_SCHEMA_VERSION = "final-media-review-input-assessment.v1"
STATE_SCHEMA_VERSION = "final-media-review-consumer-state.v1"
RESULT_SCHEMA_VERSION = "final-media-perceptual-review-result.v1"

TERMINAL_STATES = frozenset({"BLOCKED_INPUT", "COMPLETE", "FAILED"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_JSON_BYTES = 8_000_000


class FinalMediaReviewInputError(ValueError):
    """A typed, fail-closed final-media input or state error."""

    def __init__(self, reason_code: str, detail: str):
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


def _error(reason_code: str, detail: str) -> FinalMediaReviewInputError:
    return FinalMediaReviewInputError(reason_code, detail)


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise _error(
            "FINAL_MEDIA_REVIEW_JSON_INVALID",
            "value is not canonical JSON",
        ) from None


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise _error("FINAL_MEDIA_REVIEW_BINDING_INVALID", f"{label} sha256 missing")
    normalized = value.strip().lower()
    if normalized.startswith("sha256:"):
        normalized = normalized[7:]
    if not _SHA256_RE.fullmatch(normalized):
        raise _error("FINAL_MEDIA_REVIEW_BINDING_INVALID", f"{label} sha256 invalid")
    return normalized


def _regular_file(value: object, *, label: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise _error("FINAL_MEDIA_REVIEW_FILE_INVALID", f"{label} path missing")
    path = Path(value).expanduser()
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_FILE_INVALID",
            f"{label} unavailable: {path}: {exc}",
        ) from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or not resolved.is_file():
        raise _error(
            "FINAL_MEDIA_REVIEW_FILE_INVALID",
            f"{label} is not a regular non-symlink file: {path}",
        )
    return resolved


def _require_contained(
    path: Path, *, allowed_root: Path | None, label: str
) -> Path:
    if allowed_root is None:
        return path
    try:
        path.relative_to(allowed_root)
    except ValueError:
        raise _error(
            "FINAL_MEDIA_REVIEW_PATH_ESCAPE",
            f"{label} escapes the allowed package root: {path}",
        ) from None
    return path


def _resolve_allowed_root(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    raw = Path(value).expanduser()
    if raw.is_symlink():
        raise _error(
            "FINAL_MEDIA_REVIEW_PATH_ESCAPE",
            "allowed package root must not be a symlink",
        )
    resolved = raw.resolve(strict=True)
    if not resolved.is_dir():
        raise _error(
            "FINAL_MEDIA_REVIEW_PATH_ESCAPE",
            "allowed package root must be a real directory",
        )
    return resolved


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    target = _regular_file(path, label=label)
    try:
        info = target.stat()
        if not 0 < info.st_size <= _MAX_JSON_BYTES:
            raise ValueError("size outside accepted range")
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_JSON_INVALID",
            f"{label} is not a valid bounded JSON object: {target}: {exc}",
        ) from exc
    if not isinstance(value, dict):
        raise _error(
            "FINAL_MEDIA_REVIEW_JSON_INVALID",
            f"{label} root must be an object",
        )
    return value


def _int(value: object, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _error(
            "FINAL_MEDIA_REVIEW_CLOCK_INVALID",
            f"{label} must be an integer >= {minimum}",
        )
    return value


def _seconds_to_us(value: object, *, label: str) -> int:
    if isinstance(value, bool):
        raise _error("FINAL_MEDIA_REVIEW_CLOCK_INVALID", f"{label} is invalid")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _error("FINAL_MEDIA_REVIEW_CLOCK_INVALID", f"{label} is invalid") from None
    if not decimal.is_finite() or decimal < 0:
        raise _error("FINAL_MEDIA_REVIEW_CLOCK_INVALID", f"{label} is invalid")
    return int(
        (decimal * Decimal(1_000_000)).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )


def _merge_intervals(
    intervals: list[tuple[int, int]], *, duration_us: int
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    clipped = sorted(
        (max(0, start), min(duration_us, end))
        for start, end in intervals
        if end > 0 and start < duration_us and end > start
    )
    merged: list[tuple[int, int]] = []
    for start, end in clipped:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    gaps: list[tuple[int, int]] = []
    cursor = 0
    for start, end in merged:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration_us:
        gaps.append((cursor, duration_us))
    return merged, gaps


def _interval_rows(intervals: list[tuple[int, int]]) -> list[dict[str, int]]:
    return [
        {"start_us": start, "end_us": end, "duration_us": end - start}
        for start, end in intervals
    ]


def _wav_duration(path: Path) -> dict[str, int]:
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
    except (OSError, EOFError, wave.Error) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_AUDIO_ASSET_INVALID",
            f"WAV header cannot be read: {path}: {exc}",
        ) from exc
    if frames <= 0 or rate <= 0 or channels <= 0 or sample_width <= 0:
        raise _error(
            "FINAL_MEDIA_REVIEW_AUDIO_ASSET_INVALID",
            f"WAV header has no usable samples: {path}",
        )
    duration_us = int(
        (Decimal(frames) * Decimal(1_000_000) / Decimal(rate)).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )
    sample_period_us = max(1, math.ceil(1_000_000 / rate))
    return {
        "frames": frames,
        "sample_rate": rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
        "duration_us": duration_us,
        "sample_period_us": sample_period_us,
    }


def _verify_file_binding(path: Path, row: Mapping[str, object], *, label: str) -> None:
    expected_bytes = row.get("bytes")
    if (
        isinstance(expected_bytes, bool)
        or not isinstance(expected_bytes, int)
        or expected_bytes < 0
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_BINDING_INVALID",
            f"{label} byte count invalid",
        )
    if path.stat().st_size != expected_bytes:
        raise _error(
            "FINAL_MEDIA_REVIEW_BINDING_DRIFT",
            f"{label} byte count changed",
        )
    expected_sha = _normalize_sha256(row.get("sha256"), label=label)
    if _sha256(path) != expected_sha:
        raise _error(
            "FINAL_MEDIA_REVIEW_BINDING_DRIFT",
            f"{label} sha256 changed",
        )


def _validated_job(job: Mapping[str, object]) -> dict[str, Any]:
    if not isinstance(job, Mapping) or job.get("schema_version") != JOB_SCHEMA_VERSION:
        raise _error(
            "FINAL_MEDIA_REVIEW_JOB_INVALID",
            f"expected {JOB_SCHEMA_VERSION}",
        )
    candidate_id = job.get("candidate_id")
    if not isinstance(candidate_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*", candidate_id
    ):
        raise _error("FINAL_MEDIA_REVIEW_JOB_INVALID", "candidate_id invalid")
    asset = job.get("asset_manifest")
    clock = job.get("media_clock")
    requirements = job.get("requirements")
    if not all(isinstance(value, Mapping) for value in (asset, clock, requirements)):
        raise _error(
            "FINAL_MEDIA_REVIEW_JOB_INVALID",
            "asset_manifest, media_clock and requirements are required objects",
        )
    duration_us = _int(clock.get("duration_us"), label="duration_us", minimum=1)
    normalized_clock: dict[str, object] = {
        "source_video_sha256": _normalize_sha256(
            clock.get("source_video_sha256"), label="source_video"
        ),
        "source_video_bytes": _int(
            clock.get("source_video_bytes"), label="source_video_bytes", minimum=1
        ),
        "duration_us": duration_us,
    }
    for key in ("first_video_pts_us", "last_video_pts_us", "video_frame_count"):
        raw = clock.get(key)
        if raw is not None:
            normalized_clock[key] = _int(raw, label=key, minimum=0)
    first_pts = normalized_clock.get("first_video_pts_us")
    last_pts = normalized_clock.get("last_video_pts_us")
    if (
        isinstance(first_pts, int)
        and isinstance(last_pts, int)
        and (last_pts < first_pts or last_pts > duration_us + 1_000_000)
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_CLOCK_INVALID",
            "video PTS bounds are inconsistent with duration",
        )
    for flag in (
        "continuous_audio_required",
        "continuous_visual_required",
        "content_review_required",
        "exact_media_clock_evidence_required",
        "provider_accepts_bound_source_video",
        "provider_accepts_bound_audio",
    ):
        if not isinstance(requirements.get(flag), bool):
            raise _error(
                "FINAL_MEDIA_REVIEW_JOB_INVALID",
                f"requirements.{flag} must be boolean",
            )
    return {
        **dict(job),
        "candidate_id": candidate_id,
        "asset_manifest": dict(asset),
        "media_clock": normalized_clock,
        "requirements": dict(requirements),
    }


def _validate_exact_media_clock_evidence(
    *,
    receipt: Mapping[str, object],
    manifest_sha256: str,
    parent_manifest_sha256: str | None,
    clock: Mapping[str, object],
    allowed_root: Path | None,
) -> dict[str, object] | None:
    raw_path = receipt.get("validation_file")
    raw_sha = receipt.get("validation_sha256")
    if raw_path is None and raw_sha is None:
        return None
    if raw_path is None or raw_sha is None:
        raise _error(
            "FINAL_MEDIA_REVIEW_CLOCK_EVIDENCE_INVALID",
            "integrity receipt carries only half of the timing-evidence binding",
        )
    path = _require_contained(
        _regular_file(raw_path, label="exact media clock evidence"),
        allowed_root=allowed_root,
        label="exact media clock evidence",
    )
    expected_sha = _normalize_sha256(
        raw_sha, label="exact media clock evidence"
    )
    if _sha256(path) != expected_sha:
        raise _error(
            "FINAL_MEDIA_REVIEW_CLOCK_EVIDENCE_INVALID",
            "exact media clock evidence hash changed",
        )
    evidence = _read_json(path, label="exact media clock evidence")
    scope = evidence.get("scope")
    source_bindings = evidence.get("source_bindings")
    source = (
        source_bindings.get("source_video")
        if isinstance(source_bindings, Mapping)
        else None
    )
    scope_manifest_sha256 = (
        scope.get("manifest_sha256") if isinstance(scope, Mapping) else None
    )
    materialized_parent_reuse = bool(
        receipt.get("authority") == "AUTONOMOUS_FINAL_MEDIA_MATERIALIZER"
        and isinstance(parent_manifest_sha256, str)
        and receipt.get("materialized_from_manifest_sha256")
        == parent_manifest_sha256
        and scope_manifest_sha256 == parent_manifest_sha256
    )
    if (
        not isinstance(scope, Mapping)
        or not (
            scope_manifest_sha256 == manifest_sha256
            or materialized_parent_reuse
        )
        or scope.get("review_status") != "input_integrity_and_timing_only"
        or scope.get("perceptual_content_review") != "not_performed"
        or not isinstance(source, Mapping)
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_CLOCK_EVIDENCE_INVALID",
            "timing evidence does not bind the manifest as non-perceptual input evidence",
        )
    if (
        _normalize_sha256(
            source.get("actual_sha256"), label="timing evidence source video"
        )
        != clock["source_video_sha256"]
        or source.get("actual_bytes") != clock["source_video_bytes"]
        or source.get("sha256_match") is not True
        or source.get("bytes_match") is not True
        or source.get("regular_nonlinked") is not True
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_CLOCK_EVIDENCE_INVALID",
            "timing evidence source identity differs from the exact media clock",
        )
    ffprobe_format = source.get("ffprobe_format")
    decoded_bounds = source.get("decoded_frame_bounds")
    if not isinstance(ffprobe_format, Mapping):
        raise _error(
            "FINAL_MEDIA_REVIEW_CLOCK_EVIDENCE_INVALID",
            "timing evidence lacks ffprobe format clock",
        )
    format_duration_us = _seconds_to_us(
        ffprobe_format.get("duration"), label="timing evidence format duration"
    )
    if format_duration_us != clock["duration_us"]:
        raise _error(
            "FINAL_MEDIA_REVIEW_CLOCK_EVIDENCE_INVALID",
            "timing evidence format duration differs from the job clock",
        )
    validated: dict[str, object] = {
        "path": str(path),
        "sha256": expected_sha,
        "manifest_sha256": manifest_sha256,
        "clock_evidence_manifest_sha256": scope_manifest_sha256,
        "materialized_parent_reuse": materialized_parent_reuse,
        "source_video_sha256": clock["source_video_sha256"],
        "source_video_bytes": clock["source_video_bytes"],
        "duration_us": format_duration_us,
        "content_review_proved": False,
    }
    if any(
        key in clock
        for key in ("first_video_pts_us", "last_video_pts_us", "video_frame_count")
    ):
        if not isinstance(decoded_bounds, Mapping):
            raise _error(
                "FINAL_MEDIA_REVIEW_CLOCK_EVIDENCE_INVALID",
                "timing evidence lacks decoded frame bounds",
            )
        decoded_count = _int(
            decoded_bounds.get("decoded_frame_count"),
            label="decoded frame count",
            minimum=1,
        )
        first_us = _seconds_to_us(
            decoded_bounds.get("first_best_effort_timestamp_seconds"),
            label="first decoded frame timestamp",
        )
        last_us = _seconds_to_us(
            decoded_bounds.get("last_best_effort_timestamp_seconds"),
            label="last decoded frame timestamp",
        )
        expected = {
            "video_frame_count": decoded_count,
            "first_video_pts_us": first_us,
            "last_video_pts_us": last_us,
        }
        for key, actual in expected.items():
            if key in clock and clock[key] != actual:
                raise _error(
                    "FINAL_MEDIA_REVIEW_CLOCK_EVIDENCE_INVALID",
                    f"timing evidence {key} differs from the job clock",
                )
        validated.update(expected)
    return validated


def _validate_independent_integrity(
    job: Mapping[str, Any],
    manifest_path: Path,
    manifest: Mapping[str, object],
    *,
    allowed_root: Path | None,
) -> tuple[
    bool, dict[str, object] | None, dict[str, object] | None
]:
    raw = job.get("integrity_receipt")
    if raw is None:
        return False, None, None
    if not isinstance(raw, Mapping):
        raise _error(
            "FINAL_MEDIA_REVIEW_INTEGRITY_RECEIPT_INVALID",
            "integrity_receipt must be an object",
        )
    receipt_path = _require_contained(
        _regular_file(raw.get("path"), label="integrity receipt"),
        allowed_root=allowed_root,
        label="integrity receipt",
    )
    expected_receipt_sha = _normalize_sha256(
        raw.get("sha256"), label="integrity receipt"
    )
    if _sha256(receipt_path) != expected_receipt_sha:
        raise _error(
            "FINAL_MEDIA_REVIEW_INTEGRITY_RECEIPT_INVALID",
            "integrity receipt hash changed",
        )
    receipt = _read_json(receipt_path, label="integrity receipt")
    manifest_sha = _sha256(manifest_path)
    artifacts = manifest.get("artifacts")
    source = manifest.get("source_video")
    clock = job["media_clock"]
    if not isinstance(artifacts, list) or not isinstance(source, Mapping):
        raise _error(
            "FINAL_MEDIA_REVIEW_ASSET_MANIFEST_INVALID",
            "asset manifest lacks artifacts/source_video",
        )
    if not (
        receipt.get("source_manifest_sha256") == manifest_sha
        and receipt.get("verified_assets") == len(artifacts)
        and receipt.get("source_video_sha256")
        == clock["source_video_sha256"]
        and receipt.get("disposition")
        == "ACCEPTED_INPUT_INTEGRITY_AND_TIMING_ONLY"
        and receipt.get("content_review_passed") is False
        and receipt.get("quality_release") is False
        and receipt.get("upload") is False
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_INTEGRITY_RECEIPT_INVALID",
            "receipt does not bind the manifest as integrity/timing-only evidence",
        )
    duration = receipt.get("duration_seconds")
    if duration is not None and abs(
        _seconds_to_us(duration, label="integrity duration")
        - int(clock["duration_us"])
    ) > 1:
        raise _error(
            "FINAL_MEDIA_REVIEW_CLOCK_MISMATCH",
            "integrity receipt duration differs from exact media clock",
        )
    raw_parent_manifest_sha256 = manifest.get("parent_manifest_sha256")
    parent_manifest_sha256 = (
        _normalize_sha256(
            raw_parent_manifest_sha256, label="parent asset manifest"
        )
        if raw_parent_manifest_sha256 is not None
        else None
    )
    clock_evidence = _validate_exact_media_clock_evidence(
        receipt=receipt,
        manifest_sha256=manifest_sha,
        parent_manifest_sha256=parent_manifest_sha256,
        clock=clock,
        allowed_root=allowed_root,
    )
    materializer_receipt = (
        receipt.get("authority") == "AUTONOMOUS_FINAL_MEDIA_MATERIALIZER"
    )
    return (
        not materializer_receipt,
        {
            "path": str(receipt_path),
            "sha256": expected_receipt_sha,
            "verified_assets": receipt.get("verified_assets"),
            "disposition": receipt.get("disposition"),
            "authority": receipt.get("authority") or "INDEPENDENT_INPUT_VALIDATOR",
        },
        clock_evidence,
    )


def _validated_transport_capability(
    *,
    job: Mapping[str, object],
    requirements: Mapping[str, object],
    allowed_root: Path | None,
) -> tuple[dict[str, object] | None, bool, bool]:
    declared_audio = bool(requirements["provider_accepts_bound_audio"])
    declared_video = bool(
        requirements["provider_accepts_bound_source_video"]
    )
    if not declared_audio and not declared_video:
        return None, False, False

    from src.autoslice.final_media_review_raw_av import (
        FinalMediaReviewRawAvError,
        validate_package_transport_binding,
    )

    try:
        capability = validate_package_transport_binding(
            job.get("transport_capability"), allowed_root=allowed_root
        )
    except FinalMediaReviewRawAvError as exc:
        raise _error(exc.reason_code, exc.detail) from exc
    accepts = capability.get("accepts")
    if not isinstance(accepts, Mapping):
        raise _error(
            "FINAL_MEDIA_REVIEW_TRANSPORT_CAPABILITY_BINDING_INVALID",
            "transport capability accepts object missing",
        )
    if (
        declared_audio and accepts.get("raw_audio") is not True
    ) or (
        declared_video
        and accepts.get("continuous_source_video") is not True
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_TRANSPORT_CAPABILITY_BINDING_INVALID",
            "declared provider modalities exceed the bound capability",
        )
    return capability, declared_audio, declared_video


def assess_review_inputs(
    job_value: Mapping[str, object],
    *,
    allowed_root: str | Path | None = None,
) -> dict[str, object]:
    """Validate one review-input job without calling any provider.

    A returned READY state means only that the configured perceptual transport
    could receive adequate raw inputs.  ``content_review_status`` remains
    ``UNASSESSED`` by construction.
    """
    job = _validated_job(job_value)
    contained_root = _resolve_allowed_root(allowed_root)
    asset_binding = job["asset_manifest"]
    manifest_path = _require_contained(
        _regular_file(asset_binding.get("path"), label="asset manifest"),
        allowed_root=contained_root,
        label="asset manifest",
    )
    expected_manifest_sha = _normalize_sha256(
        asset_binding.get("sha256"), label="asset manifest"
    )
    actual_manifest_sha = _sha256(manifest_path)
    if actual_manifest_sha != expected_manifest_sha:
        raise _error(
            "FINAL_MEDIA_REVIEW_BINDING_DRIFT",
            "asset manifest sha256 changed",
        )
    manifest = _read_json(manifest_path, label="asset manifest")
    artifacts = manifest.get("artifacts")
    source = manifest.get("source_video")
    if not isinstance(artifacts, list) or not isinstance(source, Mapping):
        raise _error(
            "FINAL_MEDIA_REVIEW_ASSET_MANIFEST_INVALID",
            "manifest requires artifacts[] and source_video",
        )

    clock = job["media_clock"]
    source_path = _require_contained(
        _regular_file(source.get("path"), label="source video"),
        allowed_root=contained_root,
        label="source video",
    )
    source_sha = _normalize_sha256(source.get("sha256"), label="source video")
    source_bytes = source.get("bytes")
    if (
        source_sha != clock["source_video_sha256"]
        or source_bytes != clock["source_video_bytes"]
        or source_path.stat().st_size != clock["source_video_bytes"]
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_SOURCE_VIDEO_BINDING_INVALID",
            "source video identity differs from exact media clock",
        )

    (
        integrity_reused,
        integrity_binding,
        exact_media_clock_evidence,
    ) = _validate_independent_integrity(
        job, manifest_path, manifest, allowed_root=contained_root
    )
    if not integrity_reused or contained_root is not None:
        _verify_file_binding(source_path, source, label="source video")
        for index, raw in enumerate(artifacts):
            if not isinstance(raw, Mapping):
                raise _error(
                    "FINAL_MEDIA_REVIEW_ASSET_MANIFEST_INVALID",
                    f"artifact {index} is not an object",
                )
            path = _require_contained(
                _regular_file(raw.get("path"), label=f"artifact {index}"),
                allowed_root=contained_root,
                label=f"artifact {index}",
            )
            _verify_file_binding(path, raw, label=f"artifact {index}")

    duration_us = int(clock["duration_us"])
    reasons: set[str] = set()
    declared_intervals: list[tuple[int, int]] = []
    actual_audio_intervals: list[tuple[int, int]] = []
    wav_rows: list[dict[str, object]] = []
    frame_rows: list[dict[str, object]] = []
    contact_sheet_count = 0
    exact_cover_count = 0

    for index, raw in enumerate(artifacts):
        if not isinstance(raw, Mapping):
            raise _error(
                "FINAL_MEDIA_REVIEW_ASSET_MANIFEST_INVALID",
                f"artifact {index} is not an object",
            )
        kind = str(raw.get("kind") or "")
        path = _require_contained(
            _regular_file(raw.get("path"), label=f"artifact {index}"),
            allowed_root=contained_root,
            label=f"artifact {index}",
        )
        if kind in {"diagnostic_wav", "exact_full_audio_wav"}:
            raw_range = raw.get("range_seconds")
            if (
                not isinstance(raw_range, list)
                or len(raw_range) != 2
            ):
                raise _error(
                    "FINAL_MEDIA_REVIEW_ASSET_MANIFEST_INVALID",
                    f"diagnostic WAV {index} has no two-value range_seconds",
                )
            start_us = _seconds_to_us(raw_range[0], label="audio range start")
            end_us = _seconds_to_us(raw_range[1], label="audio range end")
            if end_us <= start_us:
                raise _error(
                    "FINAL_MEDIA_REVIEW_ASSET_MANIFEST_INVALID",
                    f"diagnostic WAV {index} range is empty",
                )
            if end_us > duration_us:
                reasons.add("FINAL_MEDIA_REVIEW_WINDOW_AFTER_EOF")
            declared_intervals.append((start_us, end_us))
            observation = _wav_duration(path)
            actual_end_us = start_us + observation["duration_us"]
            actual_audio_intervals.append((start_us, actual_end_us))
            declared_duration_us = end_us - start_us
            delta_us = observation["duration_us"] - declared_duration_us
            tolerance_us = observation["sample_period_us"]
            if abs(delta_us) > tolerance_us:
                reasons.add("FINAL_MEDIA_REVIEW_AUDIO_DURATION_MISMATCH")
            wav_rows.append(
                {
                    "path": str(path),
                    "kind": kind,
                    "sha256": _normalize_sha256(raw.get("sha256"), label=f"artifact {index}"),
                    "window": raw.get("window"),
                    "declared_start_us": start_us,
                    "declared_end_us": end_us,
                    "declared_duration_us": declared_duration_us,
                    "actual_duration_us": observation["duration_us"],
                    "actual_end_us": actual_end_us,
                    "delta_us": delta_us,
                    "duration_tolerance_us": tolerance_us,
                    "sample_rate": observation["sample_rate"],
                    "sample_frames": observation["frames"],
                }
            )
            continue
        if kind == "contact_sheet":
            contact_sheet_count += 1
            continue
        if kind == "exact_cover":
            exact_cover_count += 1
            continue
        if raw.get("timestamp_seconds") is not None:
            timestamp_us = _seconds_to_us(
                raw.get("timestamp_seconds"), label="frame timestamp"
            )
            if timestamp_us > duration_us:
                reasons.add("FINAL_MEDIA_REVIEW_FRAME_AFTER_EOF")
            frame_rows.append(
                {
                    "path": str(path),
                    "sha256": _normalize_sha256(raw.get("sha256"), label=f"artifact {index}"),
                    "window": raw.get("window"),
                    "timestamp_us": timestamp_us,
                }
            )

    declared_coverage, declared_gaps = _merge_intervals(
        declared_intervals, duration_us=duration_us
    )
    actual_coverage, actual_gaps = _merge_intervals(
        actual_audio_intervals, duration_us=duration_us
    )
    requirements = job["requirements"]
    continuous_audio_required = bool(requirements["continuous_audio_required"])
    continuous_visual_required = bool(requirements["continuous_visual_required"])
    exact_media_clock_evidence_required = bool(
        requirements["exact_media_clock_evidence_required"]
    )
    (
        transport_capability,
        provider_accepts_audio,
        provider_accepts_video,
    ) = _validated_transport_capability(
        job=job,
        requirements=requirements,
        allowed_root=contained_root,
    )

    if exact_media_clock_evidence_required and exact_media_clock_evidence is None:
        reasons.add("FINAL_MEDIA_REVIEW_CLOCK_EVIDENCE_MISSING")
    if continuous_audio_required and actual_gaps:
        reasons.add("FINAL_MEDIA_REVIEW_AUDIO_COVERAGE_INCOMPLETE")
    if continuous_visual_required and not provider_accepts_video:
        # Isolated frames and contact sheets remain useful diagnostics, but are
        # not a continuous watch of the exact source video.
        reasons.add("FINAL_MEDIA_REVIEW_VISUAL_INPUT_NOT_CONTINUOUS")
    if continuous_audio_required and not provider_accepts_audio:
        reasons.add("FINAL_MEDIA_REVIEW_AUDIO_TRANSPORT_MISSING")

    source_inputs_sufficient = not reasons.intersection(
        {
            "FINAL_MEDIA_REVIEW_WINDOW_AFTER_EOF",
            "FINAL_MEDIA_REVIEW_FRAME_AFTER_EOF",
            "FINAL_MEDIA_REVIEW_AUDIO_DURATION_MISMATCH",
            "FINAL_MEDIA_REVIEW_AUDIO_COVERAGE_INCOMPLETE",
            "FINAL_MEDIA_REVIEW_CLOCK_EVIDENCE_MISSING",
            "FINAL_MEDIA_REVIEW_VISUAL_INPUT_NOT_CONTINUOUS",
        }
    )
    transport_ready = (
        (not continuous_audio_required or provider_accepts_audio)
        and (not continuous_visual_required or provider_accepts_video)
    )
    perceptual_review_ready = source_inputs_sufficient and transport_ready

    materialization_actions: list[str] = []
    if exact_media_clock_evidence_required and exact_media_clock_evidence is None:
        materialization_actions.append(
            "PRODUCE_HASH_BOUND_EXACT_MEDIA_CLOCK_RECEIPT"
        )
    if continuous_audio_required and actual_gaps:
        materialization_actions.append("MATERIALIZE_EXACT_AUDIO_FOR_UNCOVERED_INTERVALS")
    if continuous_visual_required and not provider_accepts_video:
        materialization_actions.append(
            "BIND_A_TRANSPORT_THAT_CAN_CONSUME_EXACT_SOURCE_VIDEO"
        )
    if continuous_audio_required and not provider_accepts_audio:
        materialization_actions.append(
            "BIND_A_TRANSPORT_THAT_CAN_CONSUME_EXACT_AUDIO"
        )
    if "FINAL_MEDIA_REVIEW_AUDIO_DURATION_MISMATCH" in reasons:
        materialization_actions.append(
            "REBUILD_OR_REDECLARE_WAV_WINDOWS_FROM_EXACT_CLOCK"
        )

    return {
        "schema_version": ASSESSMENT_SCHEMA_VERSION,
        "candidate_id": job["candidate_id"],
        "job_sha256": canonical_sha256(job),
        "asset_manifest": {
            "path": str(manifest_path),
            "sha256": actual_manifest_sha,
            "schema": manifest.get("schema") or manifest.get("schema_version"),
            "artifact_count": len(artifacts),
        },
        "integrity": {
            "status": "PASS",
            "independent_receipt_reused": integrity_reused,
            "receipt": integrity_binding,
            "exact_media_clock_evidence": exact_media_clock_evidence,
            "content_review_proved": False,
        },
        "exact_media_clock": {
            **clock,
            "source_video_path": str(source_path),
        },
        "requirements": {
            "continuous_audio_required": continuous_audio_required,
            "continuous_visual_required": continuous_visual_required,
            "content_review_required": bool(
                requirements["content_review_required"]
            ),
            "exact_media_clock_evidence_required": (
                exact_media_clock_evidence_required
            ),
            "provider_accepts_bound_audio": provider_accepts_audio,
            "provider_accepts_bound_source_video": provider_accepts_video,
        },
        "transport_capability": transport_capability,
        "audio": {
            "diagnostic_wav_count": sum(
                row["kind"] == "diagnostic_wav" for row in wav_rows
            ),
            "exact_full_audio_wav_count": sum(
                row["kind"] == "exact_full_audio_wav" for row in wav_rows
            ),
            "windows": wav_rows,
            "declared_coverage": _interval_rows(declared_coverage),
            "declared_gaps": _interval_rows(declared_gaps),
            "actual_coverage": _interval_rows(actual_coverage),
            "actual_gaps": _interval_rows(actual_gaps),
            "continuous": not actual_gaps,
        },
        "visual": {
            "frame_count": len(frame_rows),
            "frames": frame_rows,
            "contact_sheet_count": contact_sheet_count,
            "exact_cover_count": exact_cover_count,
            "coverage": (
                "BOUND_SOURCE_VIDEO"
                if provider_accepts_video
                else "SPARSE_DIAGNOSTIC_FRAMES"
            ),
            "continuous": provider_accepts_video,
        },
        "status": (
            "READY_FOR_PERCEPTUAL_REVIEW"
            if perceptual_review_ready
            else "BLOCKED_INPUT"
        ),
        "source_inputs_sufficient": source_inputs_sufficient,
        "transport_ready": transport_ready,
        "perceptual_review_ready": perceptual_review_ready,
        "content_review_status": "UNASSESSED",
        "reason_codes": sorted(reasons),
        "materialization_actions": materialization_actions,
    }


def _parse_env_file(path: Path) -> dict[str, str]:
    target = _regular_file(path, label="CPA env")
    values: dict[str, str] = {}
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"cannot read CPA env: {target}: {exc}") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
            value = value[1:-1]
        values[key] = value
    return values


def _runtime_components(
    *,
    runtime_root: Path,
    runtime_ssh_host: str | None,
    environment: Mapping[str, str],
    env_loader: Callable[[Path], Mapping[str, str]],
    cpa_command_builder: Callable[..., str],
    final_review_call_builder: Callable[..., Callable[[str], str]],
) -> tuple[dict[str, object], Callable[[str], str], str]:
    env_path = resolve_cpa_env_path(
        runtime_root / "cpa.env", environment=environment
    )
    command = cpa_command_builder(
        env_path,
        load_env_file=env_loader,
        environment=environment,
    )
    llm_call = final_review_call_builder(
        runtime_root=runtime_root,
        runtime_ssh_host=runtime_ssh_host,
    )
    binding = getattr(llm_call, "provider_runtime_binding", {})
    return (
        {
            "adapter": "producer_final_review_transport.build_final_review_llm_call",
            "semantic_command_builder": "cpa_runtime.build_cpa_qa_command",
            "semantic_command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
            "runtime": sanitize_provider_diagnostics(
                binding if isinstance(binding, Mapping) else {}
            ),
        },
        llm_call,
        command,
    )


def _iso(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _seal_state(value: Mapping[str, object]) -> dict[str, object]:
    unsigned = dict(value)
    unsigned.pop("state_sha256", None)
    return {**unsigned, "state_sha256": canonical_sha256(unsigned)}


def _validate_state(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping) or value.get("schema_version") != STATE_SCHEMA_VERSION:
        raise _error(
            "FINAL_MEDIA_REVIEW_STATE_INVALID",
            f"expected {STATE_SCHEMA_VERSION}",
        )
    supplied = value.get("state_sha256")
    unsigned = dict(value)
    unsigned.pop("state_sha256", None)
    if supplied != canonical_sha256(unsigned):
        raise _error(
            "FINAL_MEDIA_REVIEW_STATE_INVALID",
            "state self-hash differs",
        )
    expected_fields = {
        "schema_version",
        "candidate_id",
        "binding_sha256",
        "job_path",
        "job_sha256",
        "source_video_sha256",
        "asset_manifest_sha256",
        "content_review_required",
        "attempt_count",
        "updated_at",
        "next_attempt_at",
        "assessment",
        "runtime_binding",
        "provider_diagnostics",
        "result",
        "reason_codes",
        "status",
        "state_sha256",
    }
    if set(value) != expected_fields:
        raise _error(
            "FINAL_MEDIA_REVIEW_STATE_INVALID",
            "state fields differ from the canonical consumer envelope",
        )
    candidate_id = value.get("candidate_id")
    if not isinstance(candidate_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*", candidate_id
    ):
        raise _error("FINAL_MEDIA_REVIEW_STATE_INVALID", "candidate_id invalid")
    for key in (
        "binding_sha256",
        "job_sha256",
        "source_video_sha256",
        "asset_manifest_sha256",
    ):
        raw = value.get(key)
        if not isinstance(raw, str) or not _SHA256_RE.fullmatch(raw):
            raise _error(
                "FINAL_MEDIA_REVIEW_STATE_INVALID",
                f"{key} invalid",
            )
    if not isinstance(value.get("job_path"), str) or not value["job_path"]:
        raise _error("FINAL_MEDIA_REVIEW_STATE_INVALID", "job_path invalid")
    if not isinstance(value.get("content_review_required"), bool):
        raise _error(
            "FINAL_MEDIA_REVIEW_STATE_INVALID",
            "content_review_required must be boolean",
        )
    attempt_count = value.get("attempt_count")
    if (
        isinstance(attempt_count, bool)
        or not isinstance(attempt_count, int)
        or attempt_count < 0
    ):
        raise _error("FINAL_MEDIA_REVIEW_STATE_INVALID", "attempt_count invalid")
    if _parse_time(value.get("updated_at")) is None:
        raise _error("FINAL_MEDIA_REVIEW_STATE_INVALID", "updated_at invalid")
    if value.get("next_attempt_at") is not None and _parse_time(
        value.get("next_attempt_at")
    ) is None:
        raise _error("FINAL_MEDIA_REVIEW_STATE_INVALID", "next_attempt_at invalid")
    if not isinstance(value.get("provider_diagnostics"), Mapping):
        raise _error(
            "FINAL_MEDIA_REVIEW_STATE_INVALID",
            "provider_diagnostics must be an object",
        )
    if value.get("runtime_binding") is not None and not isinstance(
        value.get("runtime_binding"), Mapping
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_STATE_INVALID",
            "runtime_binding must be null or an object",
        )
    reasons = value.get("reason_codes")
    if not isinstance(reasons, list) or not all(
        isinstance(reason, str) and reason for reason in reasons
    ):
        raise _error("FINAL_MEDIA_REVIEW_STATE_INVALID", "reason_codes invalid")
    status = value.get("status")
    if status not in {
        "BLOCKED_INPUT",
        "WAITING_CAPABILITY",
        "RETRY_WAIT",
        "DISPATCHING",
        "DISPATCH_AMBIGUOUS",
        "COMPLETE",
        "FAILED",
    }:
        raise _error("FINAL_MEDIA_REVIEW_STATE_INVALID", "state status invalid")
    assessment = value.get("assessment")
    if (
        not isinstance(assessment, Mapping)
        or assessment.get("schema_version") != ASSESSMENT_SCHEMA_VERSION
        or assessment.get("candidate_id") != candidate_id
        or assessment.get("content_review_status") != "UNASSESSED"
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_STATE_INVALID",
            "assessment binding invalid",
        )
    assessment_manifest = assessment.get("asset_manifest")
    assessment_clock = assessment.get("exact_media_clock")
    if (
        not isinstance(assessment_manifest, Mapping)
        or assessment_manifest.get("sha256") != value["asset_manifest_sha256"]
        or not isinstance(assessment_clock, Mapping)
        or assessment_clock.get("source_video_sha256")
        != value["source_video_sha256"]
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_STATE_INVALID",
            "assessment media binding differs from state",
        )
    result = value.get("result")
    if status == "COMPLETE":
        if not isinstance(result, Mapping):
            raise _error(
                "FINAL_MEDIA_REVIEW_STATE_INVALID",
                "COMPLETE state has no result",
            )
        if not (
            result.get("schema_version") == RESULT_SCHEMA_VERSION
            and result.get("candidate_id") == candidate_id
            and _normalize_sha256(
                result.get("source_video_sha256"), label="state result source video"
            )
            == value["source_video_sha256"]
            and _normalize_sha256(
                result.get("asset_manifest_sha256"), label="state result asset manifest"
            )
            == value["asset_manifest_sha256"]
            and result.get("status") in {"PASS", "BLOCK"}
            and result.get("content_review_status") == result.get("status")
            and isinstance(result.get("observations"), list)
        ):
            raise _error(
                "FINAL_MEDIA_REVIEW_STATE_INVALID",
                "terminal result does not bind the exact consumer inputs",
            )
        _validate_transport_evidence(
            result,
            assessment,
            reason_code="FINAL_MEDIA_REVIEW_STATE_INVALID",
        )
    elif result is not None:
        raise _error(
            "FINAL_MEDIA_REVIEW_STATE_INVALID",
            "nonterminal/failed state must not carry a result",
        )
    return dict(value)


def _atomic_json_write(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(
            dict(value), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) > _MAX_JSON_BYTES:
        raise _error("FINAL_MEDIA_REVIEW_STATE_INVALID", "state exceeds size limit")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary: Path | None = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _archive_superseded_state(
    state_path: Path, state: Mapping[str, object]
) -> Path:
    binding = str(state.get("binding_sha256") or "")
    if not _SHA256_RE.fullmatch(binding):
        raise _error(
            "FINAL_MEDIA_REVIEW_STATE_INVALID",
            "cannot archive state without a valid input binding",
        )
    archive = state_path.with_name(
        f"{state_path.stem}.superseded-{binding}.json"
    )
    if archive.exists() or archive.is_symlink():
        archived = _validate_state(
            _read_json(archive, label="superseded final-media review state")
        )
        if archived != dict(state):
            raise _error(
                "FINAL_MEDIA_REVIEW_STATE_ARCHIVE_CONFLICT",
                "existing superseded-state archive has different bytes",
            )
        return archive
    _atomic_json_write(archive, state)
    return archive


@contextmanager
def _state_lock(state_path: Path):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_name(state_path.name + ".lock")
    fd = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _error(
                "FINAL_MEDIA_REVIEW_STATE_INVALID",
                "state lock is not a regular single-link file",
            )
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _retryable_llm_failure(exc: LlmCallError) -> bool:
    diagnostics = exc.provider_diagnostics
    return diagnostics.get("provider_http_status") == 429


def _local_capability_llm_failure(exc: LlmCallError) -> bool:
    return exc.safe_reason in {
        "LLM_RUNTIME_CPA_ENV_UNSAFE",
        "LLM_RUNTIME_CPA_ENV_INVALID",
        "LLM_RUNTIME_CPA_BINDING_REQUIRED",
        "LLM_PROVIDER_CAPACITY_TIMEOUT",
        "LLM_PROVIDER_CAPACITY_UNAVAILABLE",
    }


def _validate_transport_evidence(
    result: Mapping[str, object],
    assessment: Mapping[str, object],
    *,
    reason_code: str,
) -> None:
    try:
        validate_transport_evidence(result, assessment)
    except FinalMediaReviewTransportEvidenceError as exc:
        raise _error(reason_code, exc.detail) from exc


def _validate_result(
    result: Mapping[str, object],
    *,
    job: Mapping[str, Any],
    assessment: Mapping[str, object],
) -> dict[str, object]:
    if not isinstance(result, Mapping) or result.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise _error(
            "FINAL_MEDIA_REVIEW_RESULT_INVALID",
            f"expected {RESULT_SCHEMA_VERSION}",
        )
    status = result.get("status")
    content = result.get("content_review_status")
    if status not in {"PASS", "BLOCK"} or content != status:
        raise _error(
            "FINAL_MEDIA_REVIEW_RESULT_INVALID",
            "status/content_review_status must be the same PASS or BLOCK",
        )
    manifest = assessment.get("asset_manifest")
    clock = assessment.get("exact_media_clock")
    if not isinstance(manifest, Mapping) or not isinstance(clock, Mapping):
        raise _error("FINAL_MEDIA_REVIEW_RESULT_INVALID", "assessment binding missing")
    if not (
        result.get("candidate_id") == job["candidate_id"]
        and _normalize_sha256(
            result.get("source_video_sha256"), label="result source video"
        )
        == clock.get("source_video_sha256")
        and _normalize_sha256(
            result.get("asset_manifest_sha256"), label="result asset manifest"
        )
        == manifest.get("sha256")
        and isinstance(result.get("observations"), list)
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_RESULT_INVALID",
            "result does not bind exact candidate/media/input manifest",
        )
    _validate_transport_evidence(
        result,
        assessment,
        reason_code="FINAL_MEDIA_REVIEW_RESULT_INVALID",
    )
    return dict(result)


def _dispatch_review_job(
    *,
    job: Mapping[str, Any],
    assessment: Mapping[str, object],
    state_file: Path,
    base: Mapping[str, object],
    attempt_count: int,
    runtime_binding: Mapping[str, object],
    llm_call: Callable[[str], str],
    semantic_command: str,
    executor: Callable[..., Mapping[str, object]],
    current_time: datetime,
    superseded_state_path: Path | None,
) -> dict[str, object]:
    """Persist one dispatch attempt and classify its exact outcome."""

    attempt_count += 1
    dispatching_state = _seal_state(
        {
            **base,
            "status": "DISPATCHING",
            "attempt_count": attempt_count,
            "runtime_binding": runtime_binding,
            "reason_codes": [
                "FINAL_MEDIA_REVIEW_PROVIDER_DISPATCH_IN_FLIGHT"
            ],
        }
    )
    # This write is the durable no-duplicate boundary. If the process dies
    # after this point, restart observes DISPATCHING and will not invoke the
    # provider again until a reconciler or a new content binding supersedes
    # the state.
    _atomic_json_write(state_file, dispatching_state)
    try:
        raw_result = executor(job, assessment, llm_call, semantic_command)
    except LlmCallError as exc:
        diagnostics = sanitize_provider_diagnostics(exc.provider_diagnostics)
        if _retryable_llm_failure(exc):
            delay_seconds = min(3600, 60 * (2 ** max(0, attempt_count - 1)))
            state = _seal_state(
                {
                    **base,
                    "status": "RETRY_WAIT",
                    "attempt_count": attempt_count,
                    "next_attempt_at": _iso(
                        current_time + timedelta(seconds=delay_seconds)
                    ),
                    "runtime_binding": runtime_binding,
                    "provider_diagnostics": diagnostics,
                    "reason_codes": [
                        "FINAL_MEDIA_REVIEW_PROVIDER_RATE_LIMITED"
                    ],
                }
            )
            provider_call_status = "RATE_LIMITED_RESPONSE"
        elif _local_capability_llm_failure(exc):
            state = _seal_state(
                {
                    **base,
                    "status": "WAITING_CAPABILITY",
                    "attempt_count": attempt_count,
                    "next_attempt_at": _iso(
                        current_time + timedelta(minutes=15)
                    ),
                    "runtime_binding": runtime_binding,
                    "provider_diagnostics": diagnostics,
                    "reason_codes": [
                        "FINAL_MEDIA_REVIEW_CPA_RUNTIME_UNAVAILABLE"
                    ],
                }
            )
            provider_call_status = "LOCAL_CAPABILITY_FAILURE"
        else:
            state = _seal_state(
                {
                    **base,
                    "status": "DISPATCH_AMBIGUOUS",
                    "attempt_count": attempt_count,
                    "runtime_binding": runtime_binding,
                    "provider_diagnostics": diagnostics,
                    "reason_codes": [
                        "FINAL_MEDIA_REVIEW_PROVIDER_DISPATCH_AMBIGUOUS"
                    ],
                }
            )
            provider_call_status = "AMBIGUOUS"
        _atomic_json_write(state_file, state)
        return {
            "state": state,
            "cache_reused": False,
            "provider_called": (
                True
                if provider_call_status == "RATE_LIMITED_RESPONSE"
                else False
                if provider_call_status == "LOCAL_CAPABILITY_FAILURE"
                else None
            ),
            "provider_call_status": provider_call_status,
            "superseded_state_path": (
                str(superseded_state_path)
                if superseded_state_path is not None
                else None
            ),
        }
    except Exception as exc:  # noqa: BLE001 - dispatch may have occurred
        state = _seal_state(
            {
                **base,
                "status": "DISPATCH_AMBIGUOUS",
                "attempt_count": attempt_count,
                "runtime_binding": runtime_binding,
                "reason_codes": [
                    "FINAL_MEDIA_REVIEW_PROVIDER_DISPATCH_AMBIGUOUS"
                ],
                "provider_diagnostics": {
                    "provider_error_code": type(exc).__name__[:128],
                    "provider_error_message": str(exc)[:512],
                },
            }
        )
        _atomic_json_write(state_file, state)
        return {
            "state": state,
            "cache_reused": False,
            "provider_called": None,
            "provider_call_status": "AMBIGUOUS",
            "superseded_state_path": (
                str(superseded_state_path)
                if superseded_state_path is not None
                else None
            ),
        }

    try:
        result = _validate_result(raw_result, job=job, assessment=assessment)
    except FinalMediaReviewInputError as exc:
        state = _seal_state(
            {
                **base,
                "status": "FAILED",
                "attempt_count": attempt_count,
                "runtime_binding": runtime_binding,
                "reason_codes": ["FINAL_MEDIA_REVIEW_RESULT_INVALID"],
                "provider_diagnostics": {
                    "provider_error_code": exc.reason_code,
                    "provider_error_message": exc.detail[:512],
                },
            }
        )
        _atomic_json_write(state_file, state)
        return {
            "state": state,
            "cache_reused": False,
            "provider_called": True,
            "provider_call_status": "RESPONSE_INVALID",
            "superseded_state_path": (
                str(superseded_state_path)
                if superseded_state_path is not None
                else None
            ),
        }

    state = _seal_state(
        {
            **base,
            "status": "COMPLETE",
            "attempt_count": attempt_count,
            "runtime_binding": runtime_binding,
            "result": result,
            "reason_codes": (
                []
                if result["status"] == "PASS"
                else ["FINAL_MEDIA_REVIEW_CONTENT_BLOCKED"]
            ),
        }
    )
    _atomic_json_write(state_file, state)
    return {
        "state": state,
        "cache_reused": False,
        "provider_called": True,
        "provider_call_status": "RESPONSE_ACCEPTED",
        "superseded_state_path": (
            str(superseded_state_path)
            if superseded_state_path is not None
            else None
        ),
    }


_RAW_AV_EXECUTOR_BINDING_KEYS = (
    "provider",
    "transport",
    "accepts",
    "result_schema_version",
    "model",
    "endpoint_family",
    "capability_id",
    "runtime_capability_sha256",
    "executable_sha256",
    "command_contract_sha256",
    "model_capability_seal_receipt_sha256",
    "model_capability_attestation_sha256",
    "model_capability_contract_sha256",
    "model_capability_sentinel_result_sha256",
    "model_capability_sentinel_result_self_sha256",
    "model_capability_sentinel_runner_sha256",
    "model_capability_expires_at",
)


def _raw_av_executor_components(
    *,
    executor: Callable[..., Mapping[str, object]] | None,
    transport_capability: Mapping[str, object],
    runtime_path: Path,
    environment: Mapping[str, str],
) -> tuple[
    Callable[..., Mapping[str, object]],
    dict[str, object],
    Callable[[str], str],
    str,
]:
    from src.autoslice.final_media_review_raw_av import (
        FinalMediaReviewRawAvError,
        build_runtime_raw_av_executor,
    )

    active = executor
    if active is None:
        try:
            active = build_runtime_raw_av_executor(
                runtime_root=runtime_path,
                package_binding=transport_capability,
                environment=environment,
            )
        except FinalMediaReviewRawAvError as exc:
            raise _error(exc.reason_code, exc.detail) from exc
    raw = getattr(active, "raw_av_runtime_binding", None)
    if not isinstance(raw, Mapping):
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_EXECUTOR_BINDING_MISSING",
            "raw-AV executor has no verifiable runtime binding",
        )
    for key in _RAW_AV_EXECUTOR_BINDING_KEYS:
        if raw.get(key) != transport_capability.get(key):
            raise _error(
                "FINAL_MEDIA_REVIEW_RAW_AV_EXECUTOR_BINDING_MISMATCH",
                f"raw-AV executor binding differs at {key}",
            )
    runtime_binding = dict(raw)
    semantic_command = (
        "raw-av-capability:"
        + str(runtime_binding.get("command_contract_sha256") or "")
    )

    def llm_call(_prompt: str) -> str:
        raise LlmCallError(
            "text CPA must not be used by the raw-AV executor",
            safe_reason="LLM_RUNTIME_CPA_BINDING_REQUIRED",
        )

    return active, runtime_binding, llm_call, semantic_command


def _selected_runtime_path(
    runtime_root: str | Path | None,
    environment: Mapping[str, str],
) -> Path | None:
    selected: str | Path | None = runtime_root
    if selected is None:
        selected = (
            environment.get("AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT")
            or environment.get("AUTOSLICE_BASE")
            or None
        )
    return Path(selected).expanduser().absolute() if selected else None


def consume_review_job(
    job_path: str | Path,
    state_path: str | Path,
    *,
    runtime_root: str | Path | None = None,
    runtime_ssh_host: str | None = None,
    environment: Mapping[str, str] | None = None,
    allowed_root: str | Path | None = None,
    now: datetime | None = None,
    executor: Callable[
        [Mapping[str, object], Mapping[str, object], Callable[[str], str], str],
        Mapping[str, object],
    ]
    | None = None,
    env_loader: Callable[[Path], Mapping[str, str]] = _parse_env_file,
    cpa_command_builder: Callable[..., str] = build_cpa_qa_command,
    final_review_call_builder: Callable[..., Callable[[str], str]] = build_final_review_llm_call,
) -> dict[str, object]:
    """Consume a review job with durable dedupe and typed provider backoff.

    The executor is intentionally injected: current canonical text CPA cannot
    hear WAVs or watch a full MP4.  A production executor must itself prove it
    consumed the exact bound raw inputs before returning RESULT_SCHEMA_VERSION.
    """

    job_file = _regular_file(job_path, label="review job")
    job = _validated_job(_read_json(job_file, label="review job"))
    binding_sha = canonical_sha256(job)
    state_file = Path(state_path).expanduser().absolute()
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    env = dict(os.environ if environment is None else environment)

    with _state_lock(state_file):
        # Revalidate every bound dependency before reusing a terminal state.
        # This remains provider-free; package-root consumers also rehash every
        # current artifact, while independent external replays may reuse their
        # explicit integrity receipt.
        assessment = assess_review_inputs(job, allowed_root=allowed_root)
        existing: dict[str, object] | None = None
        superseded_state_path: Path | None = None
        if state_file.exists():
            existing = _validate_state(_read_json(state_file, label="review state"))
            if existing.get("binding_sha256") != binding_sha:
                if existing.get("candidate_id") != job["candidate_id"]:
                    raise _error(
                        "FINAL_MEDIA_REVIEW_STATE_BINDING_MISMATCH",
                        "state path belongs to a different candidate",
                    )
                # A repaired asset manifest or exact media clock is a new,
                # content-bound review request. Preserve the old durable state
                # before replacing the fixed per-candidate current pointer; no
                # operator deletion or ambiguous provider replay is required.
                superseded_state_path = _archive_superseded_state(
                    state_file, existing
                )
                existing = None
            elif canonical_sha256(existing.get("assessment")) != canonical_sha256(
                assessment
            ):
                raise _error(
                    "FINAL_MEDIA_REVIEW_STATE_ASSESSMENT_DRIFT",
                    "current input assessment differs from the persisted state",
                )
            elif existing.get("status") in {
                "DISPATCHING", "DISPATCH_AMBIGUOUS"
            }:
                return {
                    "state": existing,
                    "cache_reused": True,
                    "provider_called": None,
                    "provider_call_status": "AMBIGUOUS_PREVIOUS_DISPATCH",
                    "superseded_state_path": None,
                }
            elif existing.get("status") in TERMINAL_STATES:
                return {
                    "state": existing,
                    "cache_reused": True,
                    "provider_called": False,
                    "provider_call_status": "NOT_CALLED",
                    "superseded_state_path": None,
                }
            else:
                due = _parse_time(existing.get("next_attempt_at"))
                if due is not None and current_time < due:
                    return {
                        "state": existing,
                        "cache_reused": True,
                        "provider_called": False,
                        "provider_call_status": "NOT_CALLED",
                        "superseded_state_path": None,
                    }

        attempt_count = int(existing.get("attempt_count", 0)) if existing else 0
        base: dict[str, object] = {
            "schema_version": STATE_SCHEMA_VERSION,
            "candidate_id": job["candidate_id"],
            "binding_sha256": binding_sha,
            "job_path": str(job_file),
            "job_sha256": _sha256(job_file),
            "source_video_sha256": assessment["exact_media_clock"]["source_video_sha256"],
            "asset_manifest_sha256": assessment["asset_manifest"]["sha256"],
            "content_review_required": assessment["requirements"]["content_review_required"],
            "attempt_count": attempt_count,
            "updated_at": _iso(current_time),
            "next_attempt_at": None,
            "assessment": assessment,
            "runtime_binding": None,
            "provider_diagnostics": {},
            "result": None,
            "reason_codes": [],
        }

        if assessment["source_inputs_sufficient"] is not True:
            state = _seal_state(
                {
                    **base,
                    "status": "BLOCKED_INPUT",
                    "reason_codes": list(assessment["reason_codes"]),
                }
            )
            _atomic_json_write(state_file, state)
            return {
                "state": state,
                "cache_reused": False,
                "provider_called": False,
                "provider_call_status": "NOT_CALLED",
                "superseded_state_path": (
                    str(superseded_state_path)
                    if superseded_state_path is not None
                    else None
                ),
            }

        runtime_path = _selected_runtime_path(runtime_root, env)
        if runtime_path is None:
            retry_at = current_time + timedelta(minutes=15)
            state = _seal_state(
                {
                    **base,
                    "status": "WAITING_CAPABILITY",
                    "next_attempt_at": _iso(retry_at),
                    "reason_codes": ["FINAL_MEDIA_REVIEW_CPA_RUNTIME_UNBOUND"],
                }
            )
            _atomic_json_write(state_file, state)
            return {
                "state": state,
                "cache_reused": False,
                "provider_called": False,
                "provider_call_status": "NOT_CALLED",
                "superseded_state_path": (
                    str(superseded_state_path)
                    if superseded_state_path is not None
                    else None
                ),
            }

        active_executor = executor
        transport_capability = assessment.get("transport_capability")
        injected_raw_binding = (
            getattr(active_executor, "raw_av_runtime_binding", None)
            if active_executor is not None
            else None
        )
        if isinstance(transport_capability, Mapping) and (
            active_executor is None or isinstance(injected_raw_binding, Mapping)
        ):
            try:
                (
                    active_executor,
                    runtime_binding,
                    llm_call,
                    semantic_command,
                ) = _raw_av_executor_components(
                    executor=active_executor,
                    transport_capability=transport_capability,
                    runtime_path=runtime_path,
                    environment=env,
                )
            except FinalMediaReviewInputError as exc:
                retry_at = current_time + timedelta(minutes=15)
                state = _seal_state(
                    {
                        **base,
                        "status": "WAITING_CAPABILITY",
                        "next_attempt_at": _iso(retry_at),
                        "reason_codes": [exc.reason_code],
                    }
                )
                _atomic_json_write(state_file, state)
                return {
                    "state": state,
                    "cache_reused": False,
                    "provider_called": False,
                    "provider_call_status": "NOT_CALLED",
                    "superseded_state_path": (
                        str(superseded_state_path)
                        if superseded_state_path is not None
                        else None
                    ),
                }
        else:
            try:
                runtime_binding, llm_call, semantic_command = _runtime_components(
                    runtime_root=runtime_path,
                    runtime_ssh_host=runtime_ssh_host,
                    environment=env,
                    env_loader=env_loader,
                    cpa_command_builder=cpa_command_builder,
                    final_review_call_builder=final_review_call_builder,
                )
            except (OSError, RuntimeError, ValueError, LlmCallError) as exc:
                retry_at = current_time + timedelta(minutes=15)
                diagnostics = (
                    sanitize_provider_diagnostics(exc.provider_diagnostics)
                    if isinstance(exc, LlmCallError)
                    else {}
                )
                state = _seal_state(
                    {
                        **base,
                        "status": "WAITING_CAPABILITY",
                        "next_attempt_at": _iso(retry_at),
                        "provider_diagnostics": diagnostics,
                        "reason_codes": [
                            "FINAL_MEDIA_REVIEW_CPA_RUNTIME_UNAVAILABLE"
                        ],
                    }
                )
                _atomic_json_write(state_file, state)
                return {
                    "state": state,
                    "cache_reused": False,
                    "provider_called": False,
                    "provider_call_status": "NOT_CALLED",
                    "superseded_state_path": (
                        str(superseded_state_path)
                        if superseded_state_path is not None
                        else None
                    ),
                }

            if active_executor is None:
                retry_at = current_time + timedelta(minutes=15)
                state = _seal_state(
                    {
                        **base,
                        "status": "WAITING_CAPABILITY",
                        "next_attempt_at": _iso(retry_at),
                        "runtime_binding": runtime_binding,
                        "reason_codes": [
                            "FINAL_MEDIA_REVIEW_RAW_AV_EXECUTOR_MISSING"
                        ],
                    }
                )
                _atomic_json_write(state_file, state)
                return {
                    "state": state,
                    "cache_reused": False,
                    "provider_called": False,
                    "provider_call_status": "NOT_CALLED",
                    "superseded_state_path": (
                        str(superseded_state_path)
                        if superseded_state_path is not None
                        else None
                    ),
                }

        return _dispatch_review_job(
            job=job,
            assessment=assessment,
            state_file=state_file,
            base=base,
            attempt_count=attempt_count,
            runtime_binding=runtime_binding,
            llm_call=llm_call,
            semantic_command=semantic_command,
            executor=active_executor,
            current_time=current_time,
            superseded_state_path=superseded_state_path,
        )


def load_consumer_state(
    state_path: str | Path,
    *,
    candidate_id: str | None = None,
    source_video_sha256: str | None = None,
) -> dict[str, object]:
    state = _validate_state(
        _read_json(Path(state_path), label="final-media review consumer state")
    )
    if candidate_id is not None and state.get("candidate_id") != candidate_id:
        raise _error(
            "FINAL_MEDIA_REVIEW_STATE_BINDING_MISMATCH",
            "candidate_id differs",
        )
    if source_video_sha256 is not None and state.get("source_video_sha256") != _normalize_sha256(
        source_video_sha256, label="expected source video"
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_STATE_BINDING_MISMATCH",
            "source video sha256 differs",
        )
    return state


def consumer_state_release_passes(state: Mapping[str, object]) -> bool:
    try:
        validated = _validate_state(state)
    except FinalMediaReviewInputError:
        return False
    result = validated.get("result")
    return bool(
        validated.get("status") == "COMPLETE"
        and isinstance(result, Mapping)
        and result.get("schema_version") == RESULT_SCHEMA_VERSION
        and result.get("status") == "PASS"
        and result.get("content_review_status") == "PASS"
    )


__all__ = [
    "ASSESSMENT_SCHEMA_VERSION",
    "FinalMediaReviewInputError",
    "JOB_SCHEMA_VERSION",
    "RESULT_SCHEMA_VERSION",
    "STATE_SCHEMA_VERSION",
    "assess_review_inputs",
    "canonical_sha256",
    "consume_review_job",
    "consumer_state_release_passes",
    "load_consumer_state",
]
