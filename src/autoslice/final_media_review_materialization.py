"""Deterministic successor inputs for autonomous final-media review.

The final-media input consumer can diagnose incomplete audio windows without
calling a provider.  This module consumes that diagnosis and, inside a normal
package root, builds one create-only successor input set:

* the exact package video remains the visual source of truth;
* old partial/misaligned WAV windows are replaced by one full-media PCM WAV;
* existing frame/contact-sheet/cover diagnostics are retained as diagnostics;
* the successor manifest, integrity receipt and job are hash-bound to the
  bootstrap job and parent manifest;
* a valid parent exact-clock receipt is reused by an explicit manifest lineage,
  never relabelled as a content review;
* repeated runs validate and reuse the same deterministic target directory.

No provider, image generation, subtitle mutation, package audit, publication or
human playback claim occurs here.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Callable, Mapping
import wave

from src.autoslice.final_media_review_inputs import (
    JOB_SCHEMA_VERSION,
    FinalMediaReviewInputError,
    assess_review_inputs,
    canonical_sha256,
)


MATERIALIZATION_SCHEMA_VERSION = "final-media-review-materialization-receipt.v2"
SUCCESSOR_MANIFEST_SCHEMA_VERSION = "final-media-review-assets-successor.v2"
MATERIALIZER_AUTHORITY = "AUTONOMOUS_FINAL_MEDIA_MATERIALIZER"
MATERIALIZER_CONTRACT_ID = "exact-sample-trim-v2"
CONTRACT_REFRESH_SCHEMA_VERSION = "final-media-review-contract-refresh-receipt.v1"
CONTRACT_REFRESH_AUTHORITY = "AUTONOMOUS_FINAL_MEDIA_CONTRACT_REFRESH"
CONTRACT_REFRESH_CONTRACT_ID = "current-committed-review-contract-v1"
_AUDIO_SAMPLE_RATE = 16_000
_AUDIO_KINDS = frozenset({"diagnostic_wav", "exact_full_audio_wav"})
_MATERIALIZABLE_ACTIONS = frozenset(
    {
        "MATERIALIZE_EXACT_AUDIO_FOR_UNCOVERED_INTERVALS",
        "REBUILD_OR_REDECLARE_WAV_WINDOWS_FROM_EXACT_CLOCK",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_JSON_BYTES = 8_000_000


class FinalMediaReviewMaterializationError(ValueError):
    """A typed fail-closed local materialization error."""

    def __init__(self, reason_code: str, detail: str):
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


def _error(reason_code: str, detail: str) -> FinalMediaReviewMaterializationError:
    return FinalMediaReviewMaterializationError(reason_code, detail)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(value: object, *, label: str, root: Path) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise _error("FINAL_MEDIA_REVIEW_MATERIALIZATION_INPUT_INVALID", f"{label} missing")
    path = Path(value).expanduser()
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_MATERIALIZATION_PATH_INVALID",
            f"{label} is unavailable or escapes package root: {path}",
        ) from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or not resolved.is_file():
        raise _error(
            "FINAL_MEDIA_REVIEW_MATERIALIZATION_PATH_INVALID",
            f"{label} must be a regular non-symlink file: {path}",
        )
    return resolved


def _read_json(path: Path, *, label: str, root: Path) -> dict[str, object]:
    target = _regular_file(path, label=label, root=root)
    try:
        info = target.stat()
        if not 0 < info.st_size <= _MAX_JSON_BYTES:
            raise ValueError("size outside accepted range")
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_MATERIALIZATION_INPUT_INVALID",
            f"{label} is not a bounded JSON object: {target}: {exc}",
        ) from exc
    if not isinstance(value, dict):
        raise _error(
            "FINAL_MEDIA_REVIEW_MATERIALIZATION_INPUT_INVALID",
            f"{label} root must be an object",
        )
    return value


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    encoded = (
        json.dumps(
            dict(value), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
        )
        + "\n"
    ).encode("utf-8")
    with path.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def _binding(
    path: Path, *, locator: Path | None = None
) -> dict[str, object]:
    return {
        "path": str(locator or path),
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }


def _verify_binding(
    binding: object,
    *,
    label: str,
    root: Path,
) -> Path:
    if not isinstance(binding, Mapping):
        raise _error(
            "FINAL_MEDIA_REVIEW_MATERIALIZATION_RECEIPT_INVALID",
            f"{label} binding missing",
        )
    path = _regular_file(binding.get("path"), label=label, root=root)
    expected_sha = binding.get("sha256")
    expected_bytes = binding.get("bytes")
    if (
        not isinstance(expected_sha, str)
        or not _SHA256.fullmatch(expected_sha)
        or isinstance(expected_bytes, bool)
        or not isinstance(expected_bytes, int)
        or expected_bytes < 0
        or _sha256(path) != expected_sha
        or path.stat().st_size != expected_bytes
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_MATERIALIZATION_RECEIPT_INVALID",
            f"{label} binding drifted",
        )
    return path


def _safe_candidate_output_parent(
    root: Path,
    candidate_id: str,
    *,
    namespace: str,
) -> Path:
    verification = root / "verification"
    if verification.is_symlink() or not verification.is_dir():
        raise _error(
            "FINAL_MEDIA_REVIEW_MATERIALIZATION_PATH_INVALID",
            "package verification directory is missing or unsafe",
        )
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", candidate_id)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", namespace)
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_MATERIALIZATION_INPUT_INVALID",
            "candidate or output namespace is invalid",
        )
    parent = verification / namespace
    candidate = parent / candidate_id
    for directory in (parent, candidate):
        if directory.exists() or directory.is_symlink():
            if directory.is_symlink() or not directory.is_dir():
                raise _error(
                    "FINAL_MEDIA_REVIEW_MATERIALIZATION_PATH_INVALID",
                    f"output parent is unsafe: {directory}",
                )
        else:
            directory.mkdir(mode=0o700)
        directory.chmod(0o700)
        if directory.resolve(strict=True).parent not in {
            verification.resolve(strict=True),
            parent.resolve(strict=True),
        }:
            raise _error(
                "FINAL_MEDIA_REVIEW_MATERIALIZATION_PATH_INVALID",
                f"output parent escaped package: {directory}",
            )
    return candidate


def _safe_output_parent(root: Path, candidate_id: str) -> Path:
    return _safe_candidate_output_parent(
        root,
        candidate_id,
        namespace="final-media-review-inputs",
    )


def _normalize_review_contract_sha256(value: object) -> str:
    raw = str(value or "")
    if raw.startswith("sha256:"):
        raw = raw.removeprefix("sha256:")
    if _SHA256.fullmatch(raw) is None:
        raise _error(
            "FINAL_MEDIA_REVIEW_CONTRACT_REFRESH_INVALID",
            "review contract sha256 is invalid",
        )
    return raw


def _normalize_review_points(
    value: object,
    *,
    duration_us: int,
) -> list[dict[str, object]]:
    if not isinstance(value, list) or not value:
        raise _error(
            "FINAL_MEDIA_REVIEW_CONTRACT_REFRESH_INVALID",
            "review points must be a non-empty array",
        )
    duration_ms = (duration_us + 999) // 1000
    normalized: list[dict[str, object]] = []
    point_ids: set[str] = set()
    expected_fields = {
        "point_id",
        "final_video_start_ms",
        "final_video_end_ms",
        "expectation",
    }
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping) or set(raw) != expected_fields:
            raise _error(
                "FINAL_MEDIA_REVIEW_CONTRACT_REFRESH_INVALID",
                f"review point {index} shape is invalid",
            )
        point_id = raw.get("point_id")
        start_ms = raw.get("final_video_start_ms")
        end_ms = raw.get("final_video_end_ms")
        expectation = raw.get("expectation")
        if (
            not isinstance(point_id, str)
            or not 1 <= len(point_id) <= 128
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", point_id) is None
            or point_id in point_ids
            or isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or not 0 <= start_ms < end_ms <= duration_ms
            or not isinstance(expectation, str)
            or not expectation.strip()
        ):
            raise _error(
                "FINAL_MEDIA_REVIEW_CONTRACT_REFRESH_INVALID",
                f"review point {index} value is invalid",
            )
        point_ids.add(point_id)
        normalized.append(
            {
                "point_id": point_id,
                "final_video_start_ms": start_ms,
                "final_video_end_ms": end_ms,
                "expectation": expectation,
            }
        )
    return normalized


def _contract_refresh_binding_sha256(
    job: Mapping[str, object],
    *,
    review_contract_sha256: str,
    review_points: list[dict[str, object]],
) -> str:
    return canonical_sha256(
        {
            "source_job_binding_sha256": canonical_sha256(job),
            "contract_refresh_contract_id": CONTRACT_REFRESH_CONTRACT_ID,
            "review_contract_sha256": review_contract_sha256,
            "subtitle_review_points": review_points,
        }
    )


def _validate_existing_contract_refresh(
    *,
    target: Path,
    source_job_path: Path,
    source_job: Mapping[str, object],
    root: Path,
    review_contract_sha256: str,
    review_points: list[dict[str, object]],
) -> dict[str, object]:
    receipt_path = target / "contract-refresh-receipt.json"
    receipt = _read_json(
        receipt_path,
        label="contract refresh receipt",
        root=root,
    )
    supplied_hash = receipt.get("receipt_sha256")
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256", None)
    binding = _contract_refresh_binding_sha256(
        source_job,
        review_contract_sha256=review_contract_sha256,
        review_points=review_points,
    )
    if not (
        receipt.get("schema_version") == CONTRACT_REFRESH_SCHEMA_VERSION
        and receipt.get("status") == "REFRESHED_REVIEW_CONTRACT"
        and receipt.get("authority") == CONTRACT_REFRESH_AUTHORITY
        and receipt.get("candidate_id") == source_job.get("candidate_id")
        and receipt.get("contract_refresh_contract_id")
        == CONTRACT_REFRESH_CONTRACT_ID
        and receipt.get("contract_refresh_binding_sha256") == binding
        and receipt.get("source_job_binding_sha256")
        == canonical_sha256(source_job)
        and receipt.get("review_contract_sha256")
        == review_contract_sha256
        and receipt.get("review_points_sha256")
        == canonical_sha256(review_points)
        and supplied_hash == canonical_sha256(unsigned)
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_CONTRACT_REFRESH_RECEIPT_INVALID",
            "existing contract refresh receipt identity differs",
        )
    source_binding = _verify_binding(
        receipt.get("source_job"),
        label="contract refresh source job",
        root=root,
    )
    if source_binding != source_job_path:
        raise _error(
            "FINAL_MEDIA_REVIEW_CONTRACT_REFRESH_RECEIPT_INVALID",
            "contract refresh source job locator differs",
        )
    successor_path = _verify_binding(
        receipt.get("successor_job"),
        label="contract refresh successor job",
        root=root,
    )
    successor = _read_json(
        successor_path,
        label="contract refresh successor job",
        root=root,
    )
    plan = successor.get("review_plan")
    lineage = successor.get("review_contract_refresh_lineage")
    if not (
        successor.get("schema_version") == JOB_SCHEMA_VERSION
        and successor.get("candidate_id") == source_job.get("candidate_id")
        and isinstance(plan, Mapping)
        and _normalize_review_contract_sha256(
            plan.get("review_contract_sha256")
        )
        == review_contract_sha256
        and plan.get("subtitle_review_points") == review_points
        and isinstance(lineage, Mapping)
        and lineage.get("authority") == CONTRACT_REFRESH_AUTHORITY
        and lineage.get("contract_refresh_contract_id")
        == CONTRACT_REFRESH_CONTRACT_ID
        and lineage.get("contract_refresh_binding_sha256") == binding
        and lineage.get("source_job_binding_sha256")
        == canonical_sha256(source_job)
        and lineage.get("receipt_path") == str(receipt_path)
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_CONTRACT_REFRESH_RECEIPT_INVALID",
            "existing contract refresh successor differs",
        )
    assessment = assess_review_inputs(successor, allowed_root=root)
    return {
        "status": "REFRESHED_REVIEW_CONTRACT",
        "cache_reused": True,
        "active_job_path": str(successor_path),
        "receipt_path": str(receipt_path),
        "review_contract_sha256": review_contract_sha256,
        "review_points_sha256": canonical_sha256(review_points),
        "assessment": assessment,
        "provider_calls": 0,
        "media_copies": 0,
        "media_reencodes": 0,
        "source_video_mutations": 0,
        "subtitle_mutations": 0,
    }


def refresh_review_job_contract(
    source_job_path: str | Path,
    *,
    allowed_root: str | Path,
    review_contract_sha256: object,
    review_points: object,
) -> dict[str, object]:
    """Create or reuse one JSON-only successor with the current review contract.

    The source job, its media bindings and any materialized WAV remain untouched.
    A changed contract is therefore a same-candidate successor binding, not a
    relabelled old state or a reason to repeat media extraction.
    """

    root_raw = Path(allowed_root).expanduser()
    if root_raw.is_symlink():
        raise _error(
            "FINAL_MEDIA_REVIEW_MATERIALIZATION_PATH_INVALID",
            "package root must not be a symlink",
        )
    root = root_raw.resolve(strict=True)
    if not root.is_dir():
        raise _error(
            "FINAL_MEDIA_REVIEW_MATERIALIZATION_PATH_INVALID",
            "package root is not a directory",
        )
    job_path = _regular_file(
        source_job_path,
        label="contract refresh source job",
        root=root,
    )
    job = _read_json(
        job_path,
        label="contract refresh source job",
        root=root,
    )
    assessment = assess_review_inputs(job, allowed_root=root)
    candidate_id = str(job.get("candidate_id") or "")
    duration_us = int(assessment["exact_media_clock"]["duration_us"])
    target_contract_sha = _normalize_review_contract_sha256(
        review_contract_sha256
    )
    target_points = _normalize_review_points(
        review_points,
        duration_us=duration_us,
    )
    plan = job.get("review_plan")
    if not isinstance(plan, Mapping):
        raise _error(
            "FINAL_MEDIA_REVIEW_CONTRACT_REFRESH_JOB_INVALID",
            "configured candidate job has no review_plan",
        )
    current_contract_sha = _normalize_review_contract_sha256(
        plan.get("review_contract_sha256")
    )
    if (
        current_contract_sha == target_contract_sha
        and plan.get("subtitle_review_points") == target_points
    ):
        return {
            "status": "REVIEW_CONTRACT_CURRENT",
            "cache_reused": True,
            "active_job_path": str(job_path),
            "receipt_path": None,
            "review_contract_sha256": target_contract_sha,
            "review_points_sha256": canonical_sha256(target_points),
            "assessment": assessment,
            "provider_calls": 0,
            "media_copies": 0,
            "media_reencodes": 0,
            "source_video_mutations": 0,
            "subtitle_mutations": 0,
        }

    binding = _contract_refresh_binding_sha256(
        job,
        review_contract_sha256=target_contract_sha,
        review_points=target_points,
    )
    parent = _safe_candidate_output_parent(
        root,
        candidate_id,
        namespace="final-media-review-contracts",
    )
    target = parent / binding[:24]
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise _error(
                "FINAL_MEDIA_REVIEW_MATERIALIZATION_PATH_INVALID",
                "deterministic contract refresh target is unsafe",
            )
        return _validate_existing_contract_refresh(
            target=target,
            source_job_path=job_path,
            source_job=job,
            root=root,
            review_contract_sha256=target_contract_sha,
            review_points=target_points,
        )

    created = False
    stage: Path | None = Path(
        tempfile.mkdtemp(prefix=".contract-refresh-", dir=parent)
    )
    stage.chmod(0o700)
    try:
        assert stage is not None
        final_job = target / "review-job.successor.json"
        final_receipt = target / "contract-refresh-receipt.json"
        successor_plan = {
            **dict(plan),
            "review_contract_sha256": target_contract_sha,
            "subtitle_review_points": target_points,
            "content_review_status": "UNASSESSED",
        }
        successor_job = {
            **dict(job),
            "schema_version": JOB_SCHEMA_VERSION,
            "review_plan": successor_plan,
            "review_contract_refresh_lineage": {
                "authority": CONTRACT_REFRESH_AUTHORITY,
                "contract_refresh_contract_id": CONTRACT_REFRESH_CONTRACT_ID,
                "contract_refresh_binding_sha256": binding,
                "source_job": _binding(job_path),
                "source_job_binding_sha256": canonical_sha256(job),
                "review_contract_sha256": target_contract_sha,
                "review_points_sha256": canonical_sha256(target_points),
                "receipt_path": str(final_receipt),
            },
        }
        stage_job = stage / final_job.name
        _write_json(stage_job, successor_job)
        receipt: dict[str, object] = {
            "schema_version": CONTRACT_REFRESH_SCHEMA_VERSION,
            "status": "REFRESHED_REVIEW_CONTRACT",
            "authority": CONTRACT_REFRESH_AUTHORITY,
            "candidate_id": candidate_id,
            "contract_refresh_contract_id": CONTRACT_REFRESH_CONTRACT_ID,
            "contract_refresh_binding_sha256": binding,
            "source_job": _binding(job_path),
            "source_job_binding_sha256": canonical_sha256(job),
            "review_contract_sha256": target_contract_sha,
            "review_points_sha256": canonical_sha256(target_points),
            "successor_job": _binding(stage_job, locator=final_job),
            "content_review_status": "UNASSESSED",
            "provider_calls": 0,
            "media_copies": 0,
            "media_reencodes": 0,
            "source_video_mutations": 0,
            "subtitle_mutations": 0,
            "upload_calls": 0,
        }
        receipt["receipt_sha256"] = canonical_sha256(receipt)
        _write_json(stage / final_receipt.name, receipt)
        directory_fd = os.open(stage, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.rename(stage, target)
        stage = None
        created = True
    except FileExistsError:
        pass
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage)

    result = _validate_existing_contract_refresh(
        target=target,
        source_job_path=job_path,
        source_job=job,
        root=root,
        review_contract_sha256=target_contract_sha,
        review_points=target_points,
    )
    result["cache_reused"] = not created
    return result


def _target_audio_frames(duration_us: int, sample_rate: int) -> int:
    """Round one positive media clock to its nearest output PCM sample."""

    if duration_us <= 0 or sample_rate <= 0:
        raise _error(
            "FINAL_MEDIA_REVIEW_AUDIO_MATERIALIZATION_INVALID",
            "audio duration and sample rate must be positive",
        )
    return (duration_us * sample_rate + 500_000) // 1_000_000


def _materializer_binding_sha256(job: Mapping[str, object]) -> str:
    """Bind deterministic cache identity to both the job and implementation contract."""

    return canonical_sha256(
        {
            "bootstrap_job_binding_sha256": canonical_sha256(job),
            "materializer_contract_id": MATERIALIZER_CONTRACT_ID,
        }
    )


def _wav_duration_us(path: Path) -> tuple[int, int, int]:
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
            channels = handle.getnchannels()
    except (OSError, EOFError, wave.Error) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_AUDIO_MATERIALIZATION_INVALID",
            f"materialized WAV cannot be decoded: {path}: {exc}",
        ) from exc
    if frames <= 0 or rate <= 0 or channels <= 0:
        raise _error(
            "FINAL_MEDIA_REVIEW_AUDIO_MATERIALIZATION_INVALID",
            "materialized WAV has no usable samples",
        )
    duration_us = round(frames * 1_000_000 / rate)
    return duration_us, rate, frames


def _default_extract_full_audio(
    source_video: Path,
    output_wav: Path,
    *,
    duration_us: int,
) -> None:
    target_frames = _target_audio_frames(duration_us, _AUDIO_SAMPLE_RATE)
    audio_filter = (
        f"aresample={_AUDIO_SAMPLE_RATE},"
        f"atrim=end_sample={target_frames},asetpts=PTS-STARTPTS"
    )
    completed = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source_video),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-af",
            audio_filter,
            "-ac",
            "1",
            "-ar",
            str(_AUDIO_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(output_wav),
        ],
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=3600,
    )
    if completed.returncode != 0 or not output_wav.is_file():
        detail = " ".join(completed.stderr.split())[-800:]
        raise _error(
            "FINAL_MEDIA_REVIEW_AUDIO_MATERIALIZATION_FAILED",
            f"ffmpeg rc={completed.returncode}: {detail}",
        )
    _duration_us, sample_rate, frames = _wav_duration_us(output_wav)
    if sample_rate != _AUDIO_SAMPLE_RATE or frames != target_frames:
        raise _error(
            "FINAL_MEDIA_REVIEW_AUDIO_MATERIALIZATION_FAILED",
            "ffmpeg output differs from the exact sample-count contract: "
            f"expected={target_frames}@{_AUDIO_SAMPLE_RATE} "
            f"actual={frames}@{sample_rate}",
        )


def _retained_artifacts(manifest: Mapping[str, object]) -> list[dict[str, object]]:
    raw = manifest.get("artifacts")
    if not isinstance(raw, list):
        raise _error(
            "FINAL_MEDIA_REVIEW_MATERIALIZATION_INPUT_INVALID",
            "parent manifest has no artifacts array",
        )
    retained: list[dict[str, object]] = []
    for index, row in enumerate(raw):
        if not isinstance(row, Mapping):
            raise _error(
                "FINAL_MEDIA_REVIEW_MATERIALIZATION_INPUT_INVALID",
                f"parent artifact {index} is not an object",
            )
        if str(row.get("kind") or "") not in _AUDIO_KINDS:
            retained.append(dict(row))
    return retained


def _successor_documents(
    *,
    job: Mapping[str, object],
    assessment: Mapping[str, object],
    parent_manifest: Mapping[str, object],
    parent_manifest_path: Path,
    full_audio_path: Path,
    successor_manifest_path: Path,
    integrity_path: Path,
    successor_job_path: Path,
    receipt_path: Path,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    candidate_id = str(job["candidate_id"])
    final_root = receipt_path.parent
    final_audio_path = final_root / full_audio_path.name
    final_manifest_path = final_root / successor_manifest_path.name
    final_integrity_path = final_root / integrity_path.name
    clock = assessment["exact_media_clock"]
    parent_manifest_sha = _sha256(parent_manifest_path)
    audio_duration_us, sample_rate, frames = _wav_duration_us(full_audio_path)
    duration_us = int(clock["duration_us"])
    retained = _retained_artifacts(parent_manifest)
    full_audio = {
        **_binding(full_audio_path, locator=final_audio_path),
        "kind": "exact_full_audio_wav",
        "window": "full-final-media",
        "range_seconds": [0.0, duration_us / 1_000_000],
        "actual_duration_us": audio_duration_us,
        "sample_rate": sample_rate,
        "sample_frames": frames,
        "source_video_sha256": clock["source_video_sha256"],
    }
    successor_manifest = {
        "schema": SUCCESSOR_MANIFEST_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "parent_manifest": _binding(parent_manifest_path),
        "parent_manifest_sha256": parent_manifest_sha,
        "source_video": dict(parent_manifest["source_video"]),
        "raw_visual_input": dict(parent_manifest["source_video"]),
        "artifacts": [*retained, full_audio],
        "materialization": {
            "authority": MATERIALIZER_AUTHORITY,
            "materializer_contract_id": MATERIALIZER_CONTRACT_ID,
            "content_review_status": "UNASSESSED",
            "provider_calls": 0,
            "image_generation_calls": 0,
            "subtitle_mutations": 0,
        },
    }
    _write_json(successor_manifest_path, successor_manifest)

    parent_clock = assessment["integrity"].get("exact_media_clock_evidence")
    integrity_receipt: dict[str, object] = {
        "authority": MATERIALIZER_AUTHORITY,
        "source_manifest_sha256": _sha256(successor_manifest_path),
        "materialized_from_manifest_sha256": parent_manifest_sha,
        "verified_assets": len(successor_manifest["artifacts"]),
        "source_video_sha256": clock["source_video_sha256"],
        "duration_seconds": duration_us / 1_000_000,
        "disposition": "ACCEPTED_INPUT_INTEGRITY_AND_TIMING_ONLY",
        "content_review_passed": False,
        "quality_release": False,
        "upload": False,
    }
    if isinstance(parent_clock, Mapping):
        integrity_receipt.update(
            validation_file=parent_clock.get("path"),
            validation_sha256=parent_clock.get("sha256"),
        )
    _write_json(integrity_path, integrity_receipt)

    successor_job = {
        **dict(job),
        "schema_version": JOB_SCHEMA_VERSION,
        "asset_manifest": _binding(
            successor_manifest_path, locator=final_manifest_path
        ),
        "integrity_receipt": _binding(
            integrity_path, locator=final_integrity_path
        ),
        "materialization_lineage": {
            "authority": MATERIALIZER_AUTHORITY,
            "materializer_contract_id": MATERIALIZER_CONTRACT_ID,
            "materializer_binding_sha256": _materializer_binding_sha256(job),
            "bootstrap_job_sha256": canonical_sha256(job),
            "parent_manifest_sha256": parent_manifest_sha,
            "receipt_path": str(receipt_path),
        },
    }
    _write_json(successor_job_path, successor_job)
    return successor_manifest, integrity_receipt, successor_job


def _receipt_document(
    *,
    bootstrap_job_path: Path,
    bootstrap_job: Mapping[str, object],
    bootstrap_assessment: Mapping[str, object],
    successor_manifest_path: Path,
    integrity_path: Path,
    successor_job_path: Path,
    full_audio_path: Path,
) -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema_version": MATERIALIZATION_SCHEMA_VERSION,
        "status": "MATERIALIZED_SUCCESSOR_INPUTS",
        "authority": MATERIALIZER_AUTHORITY,
        "candidate_id": bootstrap_job["candidate_id"],
        "materializer_contract_id": MATERIALIZER_CONTRACT_ID,
        "materializer_binding_sha256": _materializer_binding_sha256(bootstrap_job),
        "bootstrap_job": _binding(bootstrap_job_path),
        "bootstrap_job_binding_sha256": canonical_sha256(bootstrap_job),
        "bootstrap_assessment_sha256": canonical_sha256(bootstrap_assessment),
        "materialization_actions_consumed": sorted(
            set(bootstrap_assessment.get("materialization_actions") or [])
            & _MATERIALIZABLE_ACTIONS
        ),
        "full_audio": _binding(full_audio_path),
        "successor_manifest": _binding(successor_manifest_path),
        "successor_integrity_receipt": _binding(integrity_path),
        "successor_job": _binding(successor_job_path),
        "content_review_status": "UNASSESSED",
        "provider_calls": 0,
        "image_generation_calls": 0,
        "media_reencodes": 0,
        "source_video_mutations": 0,
        "subtitle_mutations": 0,
        "upload_calls": 0,
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    return receipt


def _validate_existing_target(
    *,
    target: Path,
    bootstrap_job_path: Path,
    bootstrap_job: Mapping[str, object],
    root: Path,
) -> dict[str, object]:
    receipt_path = target / "materialization-receipt.json"
    receipt = _read_json(receipt_path, label="materialization receipt", root=root)
    supplied_hash = receipt.get("receipt_sha256")
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256", None)
    if not (
        receipt.get("schema_version") == MATERIALIZATION_SCHEMA_VERSION
        and receipt.get("status") == "MATERIALIZED_SUCCESSOR_INPUTS"
        and receipt.get("authority") == MATERIALIZER_AUTHORITY
        and receipt.get("candidate_id") == bootstrap_job["candidate_id"]
        and receipt.get("materializer_contract_id") == MATERIALIZER_CONTRACT_ID
        and receipt.get("materializer_binding_sha256")
        == _materializer_binding_sha256(bootstrap_job)
        and receipt.get("bootstrap_job_binding_sha256")
        == canonical_sha256(bootstrap_job)
        and supplied_hash == canonical_sha256(unsigned)
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_MATERIALIZATION_RECEIPT_INVALID",
            "existing target receipt identity differs",
        )
    bootstrap_binding = receipt.get("bootstrap_job")
    bootstrap_path = _verify_binding(
        bootstrap_binding, label="bootstrap job", root=root
    )
    if bootstrap_path != bootstrap_job_path:
        raise _error(
            "FINAL_MEDIA_REVIEW_MATERIALIZATION_RECEIPT_INVALID",
            "bootstrap job locator differs",
        )
    full_audio = _verify_binding(receipt.get("full_audio"), label="full audio", root=root)
    manifest = _verify_binding(
        receipt.get("successor_manifest"), label="successor manifest", root=root
    )
    integrity = _verify_binding(
        receipt.get("successor_integrity_receipt"),
        label="successor integrity receipt",
        root=root,
    )
    job_path = _verify_binding(
        receipt.get("successor_job"), label="successor job", root=root
    )
    assessment = assess_review_inputs(
        _read_json(job_path, label="successor job", root=root), allowed_root=root
    )
    return {
        "status": "MATERIALIZED_SUCCESSOR_INPUTS",
        "cache_reused": True,
        "active_job_path": str(job_path),
        "receipt_path": str(receipt_path),
        "successor_manifest_path": str(manifest),
        "successor_integrity_path": str(integrity),
        "full_audio_path": str(full_audio),
        "assessment": assessment,
    }


def materialize_review_successor(
    bootstrap_job_path: str | Path,
    *,
    allowed_root: str | Path,
    extract_full_audio: Callable[..., None] = _default_extract_full_audio,
) -> dict[str, object]:
    """Create or reuse one deterministic audio-complete successor review job."""

    root_raw = Path(allowed_root).expanduser()
    if root_raw.is_symlink():
        raise _error(
            "FINAL_MEDIA_REVIEW_MATERIALIZATION_PATH_INVALID",
            "package root must not be a symlink",
        )
    root = root_raw.resolve(strict=True)
    if not root.is_dir():
        raise _error(
            "FINAL_MEDIA_REVIEW_MATERIALIZATION_PATH_INVALID",
            "package root is not a directory",
        )
    job_path = _regular_file(bootstrap_job_path, label="bootstrap job", root=root)
    job = _read_json(job_path, label="bootstrap job", root=root)
    assessment = assess_review_inputs(job, allowed_root=root)
    actions = set(assessment.get("materialization_actions") or [])
    if not actions.intersection(_MATERIALIZABLE_ACTIONS):
        return {
            "status": "NO_MATERIALIZATION_REQUIRED",
            "cache_reused": True,
            "active_job_path": str(job_path),
            "receipt_path": None,
            "assessment": assessment,
        }
    parent_manifest_path = _regular_file(
        assessment["asset_manifest"]["path"], label="parent manifest", root=root
    )
    parent_manifest = _read_json(
        parent_manifest_path, label="parent manifest", root=root
    )
    source_video = _regular_file(
        assessment["exact_media_clock"]["source_video_path"],
        label="source video",
        root=root,
    )
    candidate_id = str(job["candidate_id"])
    binding = _materializer_binding_sha256(job)
    parent = _safe_output_parent(root, candidate_id)
    target = parent / binding[:24]
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise _error(
                "FINAL_MEDIA_REVIEW_MATERIALIZATION_PATH_INVALID",
                "deterministic materialization target is unsafe",
            )
        return _validate_existing_target(
            target=target,
            bootstrap_job_path=job_path,
            bootstrap_job=job,
            root=root,
        )

    created = False
    stage: Path | None = Path(
        tempfile.mkdtemp(prefix=".materialize-", dir=parent)
    )
    stage.chmod(0o700)
    try:
        assert stage is not None
        final_audio = target / "full-final-media.wav"
        final_manifest = target / "review-assets.successor.json"
        final_integrity = target / "integrity-receipt.json"
        final_job = target / "review-job.successor.json"
        final_receipt = target / "materialization-receipt.json"
        stage_audio = stage / final_audio.name
        extract_full_audio(
            source_video,
            stage_audio,
            duration_us=int(assessment["exact_media_clock"]["duration_us"]),
        )
        if stage_audio.is_symlink() or not stage_audio.is_file():
            raise _error(
                "FINAL_MEDIA_REVIEW_AUDIO_MATERIALIZATION_FAILED",
                "audio extractor produced no regular WAV",
            )
        stage_audio.chmod(0o600)
        _wav_duration_us(stage_audio)
        # Documents intentionally carry final locators; the whole private stage
        # is installed atomically after every byte is complete.
        _successor_documents(
            job=job,
            assessment=assessment,
            parent_manifest=parent_manifest,
            parent_manifest_path=parent_manifest_path,
            full_audio_path=stage_audio,
            successor_manifest_path=stage / final_manifest.name,
            integrity_path=stage / final_integrity.name,
            successor_job_path=stage / final_job.name,
            receipt_path=final_receipt,
        )
        receipt = _receipt_document(
            bootstrap_job_path=job_path,
            bootstrap_job=job,
            bootstrap_assessment=assessment,
            successor_manifest_path=stage / final_manifest.name,
            integrity_path=stage / final_integrity.name,
            successor_job_path=stage / final_job.name,
            full_audio_path=stage_audio,
        )
        # Rebind stage-local receipt artifacts to their final package locators.
        for key, final_path in (
            ("full_audio", final_audio),
            ("successor_manifest", final_manifest),
            ("successor_integrity_receipt", final_integrity),
            ("successor_job", final_job),
        ):
            receipt[key] = {**receipt[key], "path": str(final_path)}
        receipt["receipt_sha256"] = canonical_sha256(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
        _write_json(stage / final_receipt.name, receipt)
        directory_fd = os.open(stage, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.rename(stage, target)
        stage = None
        created = True
    except FileExistsError:
        pass
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage)

    result = _validate_existing_target(
        target=target,
        bootstrap_job_path=job_path,
        bootstrap_job=job,
        root=root,
    )
    result["cache_reused"] = not created
    return result


def resolve_or_materialize_review_job(
    bootstrap_job_path: str | Path,
    *,
    allowed_root: str | Path,
    extract_full_audio: Callable[..., None] = _default_extract_full_audio,
) -> dict[str, object]:
    """Resolve the active job, constructing a deterministic successor if useful."""

    try:
        return materialize_review_successor(
            bootstrap_job_path,
            allowed_root=allowed_root,
            extract_full_audio=extract_full_audio,
        )
    except FinalMediaReviewInputError as exc:
        raise _error(exc.reason_code, exc.detail) from exc


__all__ = [
    "CONTRACT_REFRESH_AUTHORITY",
    "CONTRACT_REFRESH_CONTRACT_ID",
    "CONTRACT_REFRESH_SCHEMA_VERSION",
    "FinalMediaReviewMaterializationError",
    "MATERIALIZATION_SCHEMA_VERSION",
    "MATERIALIZER_AUTHORITY",
    "MATERIALIZER_CONTRACT_ID",
    "SUCCESSOR_MANIFEST_SCHEMA_VERSION",
    "materialize_review_successor",
    "refresh_review_job_contract",
    "resolve_or_materialize_review_job",
]
