"""Shared, read-only release gate for package-local final-media review state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.autoslice.final_media_review_inputs import (
    FinalMediaReviewInputError,
    JOB_SCHEMA_VERSION,
    consumer_state_release_passes,
    load_consumer_state,
)

GATE_SCHEMA_VERSION = "final-media-review-release-gate.v1"
ATTESTATION_SCHEMA_VERSION = "final-media-review-release-attestation.v1"
_MAX_JSON_BYTES = 8_000_000
_CANDIDATE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_STATUSES = frozenset({"PASS", "NOT_REQUIRED"})


def _normalize_sha256(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized.startswith("sha256:"):
        normalized = normalized[7:]
    if _SHA256_RE.fullmatch(normalized) is None:
        raise ValueError("sha256 is invalid")
    return normalized


def _safe_root(value: str | Path) -> Path:
    raw = Path(value).expanduser().absolute()
    cursor = Path(raw.anchor)
    try:
        for part in raw.parts[1:]:
            cursor /= part
            info = os.lstat(cursor)
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("symlink in package root")
            if cursor != raw and not stat.S_ISDIR(info.st_mode):
                raise ValueError("non-directory package-root ancestor")
    except OSError as exc:
        raise ValueError("package root is unavailable") from exc
    if not stat.S_ISDIR(os.lstat(raw).st_mode):
        raise ValueError("package root is not a directory")
    return raw


def _safe_regular(value: str | Path, *, root: Path) -> Path:
    raw = Path(value).expanduser()
    absolute = raw.absolute() if raw.is_absolute() else (root / raw).absolute()
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise ValueError("file escapes package root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("file path is unsafe")
    cursor = root
    try:
        for part in relative.parts:
            cursor /= part
            info = os.lstat(cursor)
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("symlink in package file path")
            if cursor != absolute and not stat.S_ISDIR(info.st_mode):
                raise ValueError("non-directory package-file ancestor")
    except OSError as exc:
        raise ValueError("package file is unavailable") from exc
    info = os.lstat(absolute)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("package file must be a single-link regular file")
    return absolute


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object(path: Path) -> dict[str, Any]:
    info = path.stat()
    if not 0 < info.st_size <= _MAX_JSON_BYTES:
        raise ValueError("JSON size is invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("JSON root is not an object")
    return value


def _binding(path: Path) -> dict[str, object]:
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }


def _gate(
    *,
    status: str,
    candidate_id: str,
    source_video_sha256: str,
    reason_code: str | None = None,
    bindings: Mapping[str, object] | None = None,
    state_sha256: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": GATE_SCHEMA_VERSION,
        "status": status,
        "candidate_id": candidate_id,
        "source_video_sha256": source_video_sha256,
        "reason_code": reason_code,
        "bindings": dict(bindings or {}),
        "state_sha256": state_sha256,
        "release_passes": status in _RELEASE_STATUSES,
    }


def inspect_final_media_review_release(
    package_root: str | Path,
    candidate_id: str,
    *,
    source_video_sha256: str,
) -> dict[str, object]:
    """Inspect one exact package without invoking a provider or mutating state."""

    source_sha = _normalize_sha256(source_video_sha256)
    if _CANDIDATE_RE.fullmatch(str(candidate_id or "")) is None:
        return _gate(
            status="INVALID",
            candidate_id=str(candidate_id or ""),
            source_video_sha256=source_sha,
            reason_code="FINAL_MEDIA_REVIEW_STATE_INVALID",
        )
    try:
        root = _safe_root(package_root)
    except ValueError:
        return _gate(
            status="INVALID",
            candidate_id=candidate_id,
            source_video_sha256=source_sha,
            reason_code="FINAL_MEDIA_REVIEW_STATE_INVALID",
        )
    verification = root / "verification"
    job_path = verification / f"{candidate_id}.final-media-review-job.json"
    state_path = verification / f"{candidate_id}.final-media-review-state.json"
    has_job = job_path.exists() or job_path.is_symlink()
    has_state = state_path.exists() or state_path.is_symlink()
    if not has_job and not has_state:
        return _gate(
            status="NOT_CONFIGURED",
            candidate_id=candidate_id,
            source_video_sha256=source_sha,
        )
    if not has_job:
        return _gate(
            status="INVALID",
            candidate_id=candidate_id,
            source_video_sha256=source_sha,
            reason_code="FINAL_MEDIA_REVIEW_STATE_INVALID",
        )
    try:
        canonical_job = _safe_regular(job_path, root=root)
    except ValueError:
        return _gate(
            status="INVALID",
            candidate_id=candidate_id,
            source_video_sha256=source_sha,
            reason_code="FINAL_MEDIA_REVIEW_STATE_INVALID",
        )
    bindings: dict[str, object] = {"job": _binding(canonical_job)}
    if not has_state:
        return _gate(
            status="UNRESOLVED",
            candidate_id=candidate_id,
            source_video_sha256=source_sha,
            reason_code="FINAL_MEDIA_REVIEW_UNRESOLVED",
            bindings=bindings,
        )
    try:
        state_file = _safe_regular(state_path, root=root)
        state = load_consumer_state(
            state_file,
            candidate_id=candidate_id,
            source_video_sha256=source_sha,
        )
        active_job = _safe_regular(str(state["job_path"]), root=root)
        active_job.relative_to(verification)
        job = _json_object(active_job)
        requirements = job.get("requirements")
        if (
            job.get("schema_version") != JOB_SCHEMA_VERSION
            or job.get("candidate_id") != candidate_id
            or not isinstance(requirements, Mapping)
            or requirements.get("content_review_required")
            != state.get("content_review_required")
            or _sha256(active_job) != state.get("job_sha256")
        ):
            raise ValueError("job/state binding differs")
    except (
        FinalMediaReviewInputError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
    ):
        return _gate(
            status="INVALID",
            candidate_id=candidate_id,
            source_video_sha256=source_sha,
            reason_code="FINAL_MEDIA_REVIEW_STATE_INVALID",
            bindings=bindings,
        )
    bindings.update(
        {
            "state": _binding(state_file),
            "active_job": _binding(active_job),
        }
    )
    state_sha = str(state.get("state_sha256") or "")
    if state.get("content_review_required") is not True:
        return _gate(
            status="NOT_REQUIRED",
            candidate_id=candidate_id,
            source_video_sha256=source_sha,
            bindings=bindings,
            state_sha256=state_sha,
        )
    if consumer_state_release_passes(state):
        return _gate(
            status="PASS",
            candidate_id=candidate_id,
            source_video_sha256=source_sha,
            bindings=bindings,
            state_sha256=state_sha,
        )
    result = state.get("result")
    if (
        state.get("status") == "COMPLETE"
        and isinstance(result, Mapping)
        and result.get("status") == "BLOCK"
        and result.get("content_review_status") == "BLOCK"
    ):
        return _gate(
            status="BLOCK",
            candidate_id=candidate_id,
            source_video_sha256=source_sha,
            reason_code="FINAL_MEDIA_REVIEW_CONTENT_BLOCKED",
            bindings=bindings,
            state_sha256=state_sha,
        )
    return _gate(
        status="UNRESOLVED",
        candidate_id=candidate_id,
        source_video_sha256=source_sha,
        reason_code="FINAL_MEDIA_REVIEW_UNRESOLVED",
        bindings=bindings,
        state_sha256=state_sha,
    )


def release_attestation(gate: Mapping[str, object]) -> dict[str, object]:
    if gate.get("status") not in _RELEASE_STATUSES:
        raise ValueError("final-media review does not pass release")
    return {
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "candidate_id": gate["candidate_id"],
        "status": gate["status"],
        "source_video_sha256": gate["source_video_sha256"],
        "state_sha256": gate["state_sha256"],
        "bindings": gate["bindings"],
    }


def attach_release_attestation(
    manifest: dict[str, object],
    *,
    package_root: str | Path,
    candidate_id: str,
    source_video_sha256: str,
) -> list[str]:
    attestation = manifest.get("package_attestation")
    if not isinstance(attestation, dict):
        return ["manifest v3 has no package_attestation object"]
    gate = inspect_final_media_review_release(
        package_root,
        candidate_id,
        source_video_sha256=source_video_sha256,
    )
    status = gate["status"]
    if status == "NOT_CONFIGURED":
        return []
    if status not in _RELEASE_STATUSES:
        return [str(gate.get("reason_code") or "FINAL_MEDIA_REVIEW_STATE_INVALID")]
    attestation["final_media_review"] = release_attestation(gate)
    return []


def release_attestation_problems(
    manifest: Mapping[str, object],
    *,
    package_root: str | Path,
    candidate_id: str,
    source_video_sha256: str,
) -> list[str]:
    attestation = manifest.get("package_attestation")
    if not isinstance(attestation, Mapping):
        return ["manifest v3 has no package_attestation object"]
    frozen = attestation.get("final_media_review")
    gate = inspect_final_media_review_release(
        package_root,
        candidate_id,
        source_video_sha256=source_video_sha256,
    )
    status = gate["status"]
    if status == "NOT_CONFIGURED":
        return [] if frozen is None else ["FINAL_MEDIA_REVIEW_ATTESTATION_STALE"]
    if status not in _RELEASE_STATUSES:
        return [str(gate.get("reason_code") or "FINAL_MEDIA_REVIEW_STATE_INVALID")]
    expected = release_attestation(gate)
    if frozen != expected:
        return ["FINAL_MEDIA_REVIEW_ATTESTATION_DRIFT"]
    return []


__all__ = [
    "ATTESTATION_SCHEMA_VERSION",
    "GATE_SCHEMA_VERSION",
    "attach_release_attestation",
    "inspect_final_media_review_release",
    "release_attestation",
    "release_attestation_problems",
]
