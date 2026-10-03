"""Create a normal final-media review job from a concrete package anomaly.

This is a narrow bootstrap, not a generic requirement to review every package.
It activates only when the canonical package closure exposes a stale current /
legacy duration witness and the candidate's committed review contract contains
an explicit full-media review point.  It then:

* probes the exact final video clock provider-free;
* binds video, subtitle, cover and record bytes into a package-local manifest;
* preserves the review contract points and duration discrepancy as provenance;
* writes a timing-only clock receipt, integrity receipt and job create-only;
* leaves audio construction to the ordinary materializer and content judgment
  to a later raw-AV CPA executor.

No model, image generation, upload, subtitle mutation or publication state is
touched.  A job file is the final commit marker and is written last.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Callable, Mapping, Sequence

from src.autoslice.final_media_review_inputs import (
    JOB_SCHEMA_VERSION,
    canonical_sha256,
)


BOOTSTRAP_SCHEMA_VERSION = "final-media-review-bootstrap-receipt.v1"
ASSET_MANIFEST_SCHEMA_VERSION = "final-media-review-bootstrap-assets.v1"
CLOCK_EVIDENCE_SCHEMA_VERSION = "final-media-review-exact-clock-evidence.v1"
BOOTSTRAP_AUTHORITY = "AUTONOMOUS_FINAL_MEDIA_REVIEW_BOOTSTRAP"
_SHA256 = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")
_MAX_JSON_BYTES = 8_000_000


class FinalMediaReviewBootstrapError(ValueError):
    """Typed bootstrap failure before any provider call."""

    def __init__(self, reason_code: str, detail: str):
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


def _error(reason_code: str, detail: str) -> FinalMediaReviewBootstrapError:
    return FinalMediaReviewBootstrapError(reason_code, detail)


def _json_bytes(value: Mapping[str, object]) -> bytes:
    try:
        return (
            json.dumps(
                dict(value),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_JSON_INVALID",
            "bootstrap document is not canonical JSON",
        ) from exc


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_BINDING_INVALID",
            f"{label} sha256 missing",
        )
    match = _SHA256.fullmatch(value.strip().lower())
    if match is None:
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_BINDING_INVALID",
            f"{label} sha256 invalid",
        )
    return match.group(1)


def _package_file(root: Path, relative: object, *, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_PATH_INVALID",
            f"{label} package path missing",
        )
    candidate = root / relative
    try:
        info = candidate.lstat()
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_PATH_INVALID",
            f"{label} package file unavailable: {relative}",
        ) from exc
    if candidate.is_symlink() or not stat.S_ISREG(info.st_mode) or not resolved.is_file():
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_PATH_INVALID",
            f"{label} must be a regular package file: {relative}",
        )
    return resolved


def _binding(path: Path) -> dict[str, object]:
    return {"path": str(path), "sha256": _sha_file(path), "bytes": path.stat().st_size}


def _planned_binding(path: Path, payload: bytes) -> dict[str, object]:
    return {"path": str(path), "sha256": _sha_bytes(payload), "bytes": len(payload)}


def _read_bounded_json(path: Path, *, label: str) -> dict[str, object]:
    if (
        path.is_symlink()
        or not path.is_file()
        or not 0 < path.stat().st_size <= _MAX_JSON_BYTES
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_TARGET_CONFLICT",
            f"existing {label} is unsafe",
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_TARGET_CONFLICT",
            f"existing {label} is malformed",
        ) from exc
    if not isinstance(value, dict):
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_TARGET_CONFLICT",
            f"existing {label} is not an object",
        )
    return value


def _verify_existing_binding(
    binding: object, *, expected_path: Path, label: str
) -> None:
    if not isinstance(binding, Mapping):
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_TARGET_CONFLICT",
            f"existing {label} binding missing",
        )
    expected_sha = _normalize_sha(binding.get("sha256"), label=label)
    expected_bytes = binding.get("bytes")
    if not (
        binding.get("path") == str(expected_path)
        and not expected_path.is_symlink()
        and expected_path.is_file()
        and isinstance(expected_bytes, int)
        and not isinstance(expected_bytes, bool)
        and expected_bytes == expected_path.stat().st_size
        and expected_sha == _sha_file(expected_path)
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_TARGET_CONFLICT",
            f"existing {label} binding drifted",
        )


def _validate_existing_bootstrap(
    *,
    paths: Mapping[str, Path],
    candidate_id: str,
    contract_sha: str,
) -> dict[str, object]:
    receipt = _read_bounded_json(paths["receipt"], label="bootstrap receipt")
    supplied = receipt.get("receipt_sha256")
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256", None)
    if not (
        receipt.get("schema_version") == BOOTSTRAP_SCHEMA_VERSION
        and receipt.get("status") == "BOOTSTRAPPED_FINAL_MEDIA_REVIEW_JOB"
        and receipt.get("authority") == BOOTSTRAP_AUTHORITY
        and receipt.get("candidate_id") == candidate_id
        and _normalize_sha(
            receipt.get("review_contract_sha256"), label="review contract"
        )
        == contract_sha
        and supplied == canonical_sha256(unsigned)
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_TARGET_CONFLICT",
            "existing bootstrap receipt identity differs",
        )
    for key, receipt_key in (
        ("manifest", "manifest"),
        ("clock", "clock_evidence"),
        ("integrity", "integrity_receipt"),
        ("job", "job"),
    ):
        _verify_existing_binding(
            receipt.get(receipt_key),
            expected_path=paths[key],
            label=receipt_key,
        )
    return {
        "status": "REUSED_EXISTING_JOB",
        "job_path": str(paths["job"].resolve()),
        "receipt_path": str(paths["receipt"].resolve()),
        "created": False,
        "trigger_reason_codes": list(receipt.get("trigger_reason_codes") or []),
    }


def _write_create_or_verify(path: Path, payload: bytes) -> bool:
    """Create one owner-only file, or verify an identical restart preimage."""

    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_PATH_INVALID",
            f"bootstrap directory is unsafe: {path.parent}",
        )
    path.parent.chmod(0o700)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise _error(
                "FINAL_MEDIA_REVIEW_BOOTSTRAP_TARGET_CONFLICT",
                f"existing bootstrap target differs: {path}",
            )
        return False
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise _error(
                "FINAL_MEDIA_REVIEW_BOOTSTRAP_TARGET_CONFLICT",
                f"concurrent bootstrap target differs: {path}",
            ) from None
        return False
    except OSError as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_WRITE_FAILED",
            f"cannot persist bootstrap target: {path}: {exc}",
        ) from exc
    return True


def _seconds_to_us(value: object, *, label: str) -> int:
    if isinstance(value, bool):
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_CLOCK_INVALID", f"{label} invalid"
        )
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_CLOCK_INVALID", f"{label} invalid"
        ) from None
    if not decimal.is_finite() or decimal < 0:
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_CLOCK_INVALID", f"{label} invalid"
        )
    return int(
        (decimal * Decimal(1_000_000)).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )


def _run_ffprobe(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_CLOCK_PROBE_FAILED",
            f"ffprobe could not run: {type(exc).__name__}",
        ) from exc
    if completed.returncode != 0:
        detail = " ".join(completed.stderr.split())[-800:]
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_CLOCK_PROBE_FAILED",
            f"ffprobe rc={completed.returncode}: {detail}",
        )
    return completed


def probe_exact_media_clock(video: Path) -> dict[str, object]:
    """Measure format duration and decoded video-frame bounds provider-free."""

    metadata = _run_ffprobe(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=index,codec_type,start_time,duration,nb_frames",
            "-of",
            "json",
            str(video),
        ],
        timeout=120,
    )
    try:
        payload = json.loads(metadata.stdout)
        format_row = payload["format"]
        streams = payload["streams"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_CLOCK_PROBE_FAILED",
            "ffprobe metadata result is malformed",
        ) from exc
    if not isinstance(format_row, Mapping) or not isinstance(streams, list):
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_CLOCK_PROBE_FAILED",
            "ffprobe metadata result lacks format/streams",
        )
    video_streams = [
        row
        for row in streams
        if isinstance(row, Mapping) and row.get("codec_type") == "video"
    ]
    if len(video_streams) != 1:
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_CLOCK_PROBE_FAILED",
            "final media must have exactly one video stream",
        )
    frame_probe = _run_ffprobe(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "csv=p=0",
            str(video),
        ],
        timeout=900,
    )
    timestamps: list[int] = []
    for raw in frame_probe.stdout.splitlines():
        token = raw.strip().split(",", 1)[0]
        if not token or token == "N/A":
            continue
        timestamps.append(_seconds_to_us(token, label="decoded frame timestamp"))
    if not timestamps or timestamps != sorted(timestamps):
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_CLOCK_PROBE_FAILED",
            "decoded video frame clock is empty or non-monotonic",
        )
    duration_us = _seconds_to_us(format_row.get("duration"), label="format duration")
    if duration_us <= 0 or timestamps[-1] > duration_us + 1_000_000:
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_CLOCK_PROBE_FAILED",
            "decoded video clock is inconsistent with format duration",
        )
    declared_size = format_row.get("size")
    if declared_size is not None and int(declared_size) != video.stat().st_size:
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_CLOCK_PROBE_FAILED",
            "ffprobe format size differs from the package video",
        )
    declared_frames = video_streams[0].get("nb_frames")
    if declared_frames not in (None, "N/A") and int(declared_frames) != len(timestamps):
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_CLOCK_PROBE_FAILED",
            "stream frame count differs from decoded frame count",
        )
    return {
        "duration_us": duration_us,
        "first_video_pts_us": timestamps[0],
        "last_video_pts_us": timestamps[-1],
        "video_frame_count": len(timestamps),
        "format_duration_seconds": str(format_row.get("duration")),
        "stream_start_seconds": str(video_streams[0].get("start_time")),
        "stream_duration_seconds": str(video_streams[0].get("duration")),
    }


def _full_media_contract_points(
    points: object, *, final_duration_ms: int
) -> list[dict[str, object]]:
    if not isinstance(points, Sequence) or isinstance(points, (str, bytes)):
        return []
    normalized: list[dict[str, object]] = []
    full_range = False
    for point in points:
        if not isinstance(point, Mapping):
            return []
        start = point.get("final_video_start_ms")
        end = point.get("final_video_end_ms")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 0
            or end <= start
            or not isinstance(point.get("point_id"), str)
            or not isinstance(point.get("expectation"), str)
        ):
            return []
        row = dict(point)
        normalized.append(row)
        if start == 0 and final_duration_ms - 500 <= end <= final_duration_ms + 500:
            full_range = True
    return normalized if full_range else []


def bootstrap_is_required(
    closure: Mapping[str, object], review_points: object
) -> bool:
    final_duration = closure.get("final_duration_ms")
    return bool(
        closure.get("duration_witness_mismatch") is True
        and isinstance(final_duration, int)
        and not isinstance(final_duration, bool)
        and final_duration > 0
        and _full_media_contract_points(
            review_points, final_duration_ms=final_duration
        )
    )


def _clock_evidence(
    *,
    manifest_sha256: str,
    video: Path,
    clock: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": CLOCK_EVIDENCE_SCHEMA_VERSION,
        "scope": {
            "manifest_sha256": manifest_sha256,
            "review_status": "input_integrity_and_timing_only",
            "perceptual_content_review": "not_performed",
        },
        "source_bindings": {
            "source_video": {
                "path": str(video),
                "actual_sha256": _sha_file(video),
                "actual_bytes": video.stat().st_size,
                "sha256_match": True,
                "bytes_match": True,
                "regular_nonlinked": True,
                "ffprobe_format": {
                    "duration": clock["format_duration_seconds"],
                    "size": str(video.stat().st_size),
                },
                "decoded_frame_bounds": {
                    "decoded_frame_count": clock["video_frame_count"],
                    "first_best_effort_timestamp_seconds": (
                        int(clock["first_video_pts_us"]) / 1_000_000
                    ),
                    "last_best_effort_timestamp_seconds": (
                        int(clock["last_video_pts_us"]) / 1_000_000
                    ),
                },
            }
        },
        "content_review_passed": False,
        "quality_release": False,
        "upload": False,
    }


def _read_existing_clock(path: Path, *, video: Path) -> dict[str, object] | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_JSON_BYTES:
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_TARGET_CONFLICT",
            "existing exact-clock evidence is unsafe",
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        source = value["source_bindings"]["source_video"]
        bounds = source["decoded_frame_bounds"]
        duration = source["ffprobe_format"]["duration"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_TARGET_CONFLICT",
            "existing exact-clock evidence is malformed",
        ) from exc
    if not (
        value.get("schema_version") == CLOCK_EVIDENCE_SCHEMA_VERSION
        and _normalize_sha(source.get("actual_sha256"), label="clock source video")
        == _sha_file(video)
        and source.get("actual_bytes") == video.stat().st_size
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_TARGET_CONFLICT",
            "existing exact-clock evidence binds different media",
        )
    return {
        "duration_us": _seconds_to_us(duration, label="existing format duration"),
        "first_video_pts_us": _seconds_to_us(
            bounds.get("first_best_effort_timestamp_seconds"),
            label="existing first decoded frame",
        ),
        "last_video_pts_us": _seconds_to_us(
            bounds.get("last_best_effort_timestamp_seconds"),
            label="existing last decoded frame",
        ),
        "video_frame_count": int(bounds["decoded_frame_count"]),
        "format_duration_seconds": str(duration),
        "stream_start_seconds": "UNKNOWN_REUSED_CLOCK_RECEIPT",
        "stream_duration_seconds": "UNKNOWN_REUSED_CLOCK_RECEIPT",
    }


def bootstrap_final_media_review_job(
    *,
    package_root: Path,
    candidate_id: str,
    closure: Mapping[str, object],
    review_contract_sha256: str,
    review_points: object,
    provider_accepts_bound_source_video: bool = False,
    provider_accepts_bound_audio: bool = False,
    probe: Callable[[Path], Mapping[str, object]] = probe_exact_media_clock,
) -> dict[str, object]:
    """Create or reuse the package-local bootstrap job for one proven anomaly."""

    root = package_root.resolve(strict=True)
    if package_root.is_symlink() or not root.is_dir():
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_PATH_INVALID", "package root is unsafe"
        )
    points = _full_media_contract_points(
        review_points, final_duration_ms=int(closure.get("final_duration_ms") or 0)
    )
    if not bootstrap_is_required(closure, points):
        return {"status": "NOT_REQUIRED", "job_path": None, "created": False}
    contract_sha = _normalize_sha(review_contract_sha256, label="review contract")
    artifacts = closure.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_INPUT_INVALID",
            "manifest closure has no artifact map",
        )
    video = _package_file(root, artifacts.get("video"), label="final video")
    subtitle = _package_file(root, artifacts.get("subtitle"), label="final subtitle")
    cover = _package_file(root, artifacts.get("cover"), label="final cover")
    record = _package_file(root, closure.get("record_path"), label="record")
    verification = root / "verification"
    if verification.is_symlink() or not verification.is_dir():
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_PATH_INVALID",
            "package verification directory is missing or unsafe",
        )
    paths = {
        "manifest": verification / f"{candidate_id}.final-media-review-assets.json",
        "clock": verification / f"{candidate_id}.final-media-review-clock.json",
        "integrity": verification / f"{candidate_id}.final-media-review-integrity.json",
        "receipt": verification / f"{candidate_id}.final-media-review-bootstrap.json",
        "job": verification / f"{candidate_id}.final-media-review-job.json",
    }
    if paths["job"].exists() or paths["job"].is_symlink():
        return _validate_existing_bootstrap(
            paths=paths, candidate_id=candidate_id, contract_sha=contract_sha
        )

    manifest = {
        "schema": ASSET_MANIFEST_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "source_video": _binding(video),
        "raw_visual_input": _binding(video),
        "artifacts": [
            {**_binding(subtitle), "kind": "final_subtitle_srt"},
            {**_binding(cover), "kind": "exact_cover"},
            {**_binding(record), "kind": "final_record_json"},
        ],
        "bootstrap": {
            "authority": BOOTSTRAP_AUTHORITY,
            "trigger_reason_codes": [
                "FINAL_MEDIA_REVIEW_LEGACY_DURATION_WITNESS_MISMATCH"
            ],
            "content_review_status": "UNASSESSED",
            "provider_calls": 0,
        },
    }
    manifest_bytes = _json_bytes(manifest)
    manifest_sha = _sha_bytes(manifest_bytes)
    existing_clock = _read_existing_clock(paths["clock"], video=video)
    clock = dict(existing_clock or probe(video))
    required_clock_fields = {
        "duration_us",
        "first_video_pts_us",
        "last_video_pts_us",
        "video_frame_count",
        "format_duration_seconds",
    }
    if not required_clock_fields <= clock.keys():
        raise _error(
            "FINAL_MEDIA_REVIEW_BOOTSTRAP_CLOCK_INVALID",
            "clock probe omitted required fields",
        )
    clock_doc = _clock_evidence(
        manifest_sha256=manifest_sha, video=video, clock=clock
    )
    clock_bytes = _json_bytes(clock_doc)
    exact_duration_ms = int(clock["duration_us"]) // 1000
    trigger_codes = ["FINAL_MEDIA_REVIEW_LEGACY_DURATION_WITNESS_MISMATCH"]
    current_duration_ms = int(closure["final_duration_ms"])
    if abs(exact_duration_ms - current_duration_ms) > 500:
        trigger_codes.append("FINAL_MEDIA_REVIEW_CURRENT_DURATION_PROBE_MISMATCH")
    integrity = {
        "source_manifest_sha256": manifest_sha,
        "verified_assets": len(manifest["artifacts"]),
        "source_video_sha256": _sha_file(video),
        "duration_seconds": int(clock["duration_us"]) / 1_000_000,
        "validation_file": str(paths["clock"]),
        "validation_sha256": _sha_bytes(clock_bytes),
        "disposition": "ACCEPTED_INPUT_INTEGRITY_AND_TIMING_ONLY",
        "content_review_passed": False,
        "quality_release": False,
        "upload": False,
        "authority": BOOTSTRAP_AUTHORITY,
    }
    integrity_bytes = _json_bytes(integrity)
    job = {
        "schema_version": JOB_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "asset_manifest": _planned_binding(paths["manifest"], manifest_bytes),
        "integrity_receipt": _planned_binding(paths["integrity"], integrity_bytes),
        "media_clock": {
            "source_video_sha256": _sha_file(video),
            "source_video_bytes": video.stat().st_size,
            "duration_us": int(clock["duration_us"]),
            "first_video_pts_us": int(clock["first_video_pts_us"]),
            "last_video_pts_us": int(clock["last_video_pts_us"]),
            "video_frame_count": int(clock["video_frame_count"]),
        },
        "requirements": {
            "continuous_audio_required": True,
            "continuous_visual_required": True,
            "content_review_required": True,
            "exact_media_clock_evidence_required": True,
            "provider_accepts_bound_source_video": bool(
                provider_accepts_bound_source_video
            ),
            "provider_accepts_bound_audio": bool(
                provider_accepts_bound_audio
            ),
        },
        "review_plan": {
            "authority": BOOTSTRAP_AUTHORITY,
            "trigger_reason_codes": trigger_codes,
            "review_contract_sha256": contract_sha,
            "subtitle_review_points": points,
            "duration_binding": {
                key: closure.get(key)
                for key in (
                    "final_duration_ms",
                    "final_duration_source",
                    "current_final_duration_ms",
                    "legacy_branding_intro_duration_ms",
                    "duration_witness_mismatch_ms",
                    "duration_witness_mismatch",
                )
            },
            "artifacts": {
                "video": _binding(video),
                "subtitle": _binding(subtitle),
                "cover": _binding(cover),
                "record": _binding(record),
            },
            "content_review_status": "UNASSESSED",
        },
    }
    job_bytes = _json_bytes(job)
    receipt = {
        "schema_version": BOOTSTRAP_SCHEMA_VERSION,
        "status": "BOOTSTRAPPED_FINAL_MEDIA_REVIEW_JOB",
        "authority": BOOTSTRAP_AUTHORITY,
        "candidate_id": candidate_id,
        "trigger_reason_codes": trigger_codes,
        "review_contract_sha256": contract_sha,
        "manifest": _planned_binding(paths["manifest"], manifest_bytes),
        "clock_evidence": _planned_binding(paths["clock"], clock_bytes),
        "integrity_receipt": _planned_binding(paths["integrity"], integrity_bytes),
        "job": _planned_binding(paths["job"], job_bytes),
        "content_review_status": "UNASSESSED",
        "declared_raw_av_capabilities": {
            "source_video": bool(provider_accepts_bound_source_video),
            "audio": bool(provider_accepts_bound_audio),
        },
        "provider_calls": 0,
        "image_generation_calls": 0,
        "media_reencodes": 0,
        "subtitle_mutations": 0,
        "upload_calls": 0,
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    receipt_bytes = _json_bytes(receipt)

    created = False
    for key, payload in (
        ("manifest", manifest_bytes),
        ("clock", clock_bytes),
        ("integrity", integrity_bytes),
        ("receipt", receipt_bytes),
        # Job last: its existence is the bootstrap transaction commit marker.
        ("job", job_bytes),
    ):
        created = _write_create_or_verify(paths[key], payload) or created
    return {
        "status": "BOOTSTRAPPED_FINAL_MEDIA_REVIEW_JOB",
        "job_path": str(paths["job"].resolve()),
        "receipt_path": str(paths["receipt"].resolve()),
        "created": created,
        "trigger_reason_codes": trigger_codes,
        "clock": clock,
    }


__all__ = [
    "ASSET_MANIFEST_SCHEMA_VERSION",
    "BOOTSTRAP_AUTHORITY",
    "BOOTSTRAP_SCHEMA_VERSION",
    "CLOCK_EVIDENCE_SCHEMA_VERSION",
    "FinalMediaReviewBootstrapError",
    "bootstrap_final_media_review_job",
    "bootstrap_is_required",
    "probe_exact_media_clock",
]
