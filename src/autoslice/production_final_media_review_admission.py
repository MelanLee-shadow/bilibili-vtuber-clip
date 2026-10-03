"""Admit a daily production package into the existing final-media review chain.

Ordinary production packages do not require a final-media content review.  The
admission gate is intentionally narrow and provider-free:

* the canonical daily manifest must project a final-media duration binding;
* that binding must replay exactly from the current package record;
* the current and legacy duration witnesses must differ by more than 500 ms;
* the committed candidate-specific review contract must contain a point that
  spans the exact current final-media duration.

Only then does this module call the generic create-only bootstrap implemented by
``final_media_review_bootstrap``.  It creates no mechanical receipt, upload
manifest, publication authority, or human-playback claim.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Callable, Mapping

from src.autoslice import final_human_review as review
from src.autoslice.final_media_duration_binding import (
    FinalMediaDurationBindingError,
    final_burn_duration_binding,
)
from src.autoslice.final_media_review_bootstrap import (
    FinalMediaReviewBootstrapError,
    bootstrap_final_media_review_job,
    bootstrap_is_required,
    probe_exact_media_clock,
)


PRODUCTION_REVIEW_MANIFEST_SCHEMA_VERSION = (
    "lidousha-daily-review-manifest.v1"
)
ADMISSION_AUTHORITY = "COMMITTED_PRODUCTION_FINAL_MEDIA_REVIEW_ADMISSION"
_DURATION_FIELDS = frozenset(
    {
        "final_duration_ms",
        "final_duration_source",
        "current_final_duration_ms",
        "legacy_branding_intro_duration_ms",
        "duration_witness_mismatch_ms",
        "duration_witness_mismatch",
    }
)
_SHA256_RE = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")
_MAX_JSON_BYTES = 8_000_000


class ProductionFinalMediaReviewAdmissionError(ValueError):
    """A typed fail-closed daily-package admission error."""

    def __init__(self, reason_code: str, detail: str):
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


def _error(
    reason_code: str, detail: str
) -> ProductionFinalMediaReviewAdmissionError:
    return ProductionFinalMediaReviewAdmissionError(reason_code, detail)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise _error(
            "FINAL_MEDIA_REVIEW_PRODUCTION_ADMISSION_BINDING_INVALID",
            f"{label} sha256 missing",
        )
    match = _SHA256_RE.fullmatch(value.strip().lower())
    if match is None:
        raise _error(
            "FINAL_MEDIA_REVIEW_PRODUCTION_ADMISSION_BINDING_INVALID",
            f"{label} sha256 invalid",
        )
    return match.group(1)


def _safe_root(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise _error(
            "FINAL_MEDIA_REVIEW_PRODUCTION_ADMISSION_PATH_INVALID",
            "package root must be an absolute non-symlink directory",
        )
    try:
        root = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_PRODUCTION_ADMISSION_PATH_INVALID",
            "package root is unavailable",
        ) from exc
    if not root.is_dir():
        raise _error(
            "FINAL_MEDIA_REVIEW_PRODUCTION_ADMISSION_PATH_INVALID",
            "package root is not a directory",
        )
    return root


def _contained_regular(
    root: Path, relative: object, *, label: str
) -> Path:
    if not isinstance(relative, str) or not relative:
        raise _error(
            "FINAL_MEDIA_REVIEW_PRODUCTION_ADMISSION_PATH_INVALID",
            f"{label} package path missing",
        )
    candidate = root / relative
    try:
        info = candidate.lstat()
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_PRODUCTION_ADMISSION_PATH_INVALID",
            f"{label} is unavailable or escapes package root",
        ) from exc
    if (
        candidate.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or not resolved.is_file()
        or info.st_nlink != 1
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_PRODUCTION_ADMISSION_PATH_INVALID",
            f"{label} must be a single-link regular package file",
        )
    return resolved


def _read_json(path: Path, *, label: str) -> dict[str, object]:
    try:
        info = path.stat()
        if not 0 < info.st_size <= _MAX_JSON_BYTES:
            raise ValueError("size outside accepted range")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_PRODUCTION_ADMISSION_JSON_INVALID",
            f"{label} is not a bounded JSON object",
        ) from exc
    if not isinstance(value, dict):
        raise _error(
            "FINAL_MEDIA_REVIEW_PRODUCTION_ADMISSION_JSON_INVALID",
            f"{label} root must be an object",
        )
    return value


def _ensure_verification_directory(root: Path) -> Path:
    verification = root / "verification"
    if verification.exists() or verification.is_symlink():
        if verification.is_symlink() or not verification.is_dir():
            raise _error(
                "FINAL_MEDIA_REVIEW_PRODUCTION_ADMISSION_PATH_INVALID",
                "verification path is not a safe package directory",
            )
        return verification.resolve(strict=True)
    try:
        verification.mkdir(mode=0o700)
        directory_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_PRODUCTION_ADMISSION_WRITE_FAILED",
            "cannot create package verification directory",
        ) from exc
    if verification.is_symlink() or not verification.is_dir():
        raise _error(
            "FINAL_MEDIA_REVIEW_PRODUCTION_ADMISSION_PATH_INVALID",
            "verification directory creation was unsafe",
        )
    return verification.resolve(strict=True)


def _not_required(
    candidate_id: str, reason_code: str
) -> dict[str, object]:
    return {
        "status": "NOT_REQUIRED",
        "authority": ADMISSION_AUTHORITY,
        "candidate_id": candidate_id,
        "reason_code": reason_code,
        "provider_calls": 0,
        "job_path": None,
        "created": False,
    }


def _duration_binding(
    item: Mapping[str, object],
    *,
    record: Mapping[str, object],
    candidate_id: str,
) -> dict[str, object] | None:
    projected = item.get("final_media_duration_binding")
    if projected is None:
        return None
    if not isinstance(projected, Mapping) or set(projected) != _DURATION_FIELDS:
        raise _error(
            "FINAL_MEDIA_REVIEW_PRODUCTION_DURATION_BINDING_INVALID",
            f"{candidate_id} manifest duration binding is malformed",
        )
    try:
        replayed = final_burn_duration_binding(record.get("burned_preview"))
    except FinalMediaDurationBindingError as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_PRODUCTION_DURATION_BINDING_INVALID",
            f"{candidate_id} record duration binding is invalid: {exc}",
        ) from exc
    if dict(projected) != replayed:
        raise _error(
            "FINAL_MEDIA_REVIEW_PRODUCTION_DURATION_BINDING_MISMATCH",
            f"{candidate_id} manifest duration binding differs from current record",
        )
    return replayed


def inspect_production_final_media_review_admission(
    package_root: str | Path,
    candidate_id: str,
) -> dict[str, object]:
    """Inspect one daily package without probing media or writing sidecars."""

    root = _safe_root(package_root)
    manifest_path = _contained_regular(
        root, "review_manifest.json", label="daily review manifest"
    )
    manifest = _read_json(manifest_path, label="daily review manifest")
    if manifest.get("schema_version") != PRODUCTION_REVIEW_MANIFEST_SCHEMA_VERSION:
        return _not_required(candidate_id, "NON_PRODUCTION_REVIEW_MANIFEST")
    if not (
        manifest.get("status") == "review_ready"
        and manifest.get("run_mode") == "PRODUCTION_REVIEW"
        and manifest.get("upload_allowed") is False
        and manifest.get("candidate_id") == candidate_id
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_PRODUCTION_MANIFEST_IDENTITY_INVALID",
            f"daily review manifest does not identify {candidate_id}",
        )
    raw_items = manifest.get("items")
    if not isinstance(raw_items, list) or len(raw_items) != 1:
        raise _error(
            "FINAL_MEDIA_REVIEW_PRODUCTION_MANIFEST_IDENTITY_INVALID",
            "daily review manifest must contain exactly one item",
        )
    item = raw_items[0]
    if not isinstance(item, Mapping) or item.get("candidate_id") != candidate_id:
        raise _error(
            "FINAL_MEDIA_REVIEW_PRODUCTION_MANIFEST_IDENTITY_INVALID",
            "daily review item identity differs",
        )
    lane = str(item.get("kind") or item.get("classification") or "")
    if lane != "talk":
        return _not_required(candidate_id, "NON_TALK_DAILY_PACKAGE")
    if item.get("final_media_duration_binding") is None:
        return _not_required(candidate_id, "NO_PROJECTED_DURATION_BINDING")

    sha_map = item.get("sha256")
    if not isinstance(sha_map, Mapping):
        raise _error(
            "FINAL_MEDIA_REVIEW_PRODUCTION_ADMISSION_BINDING_INVALID",
            "daily review item has no artifact hash map",
        )
    relative = {
        "video": item.get("video"),
        "subtitle": item.get("subtitle_srt"),
        "cover": item.get("cover"),
        "record": item.get("record"),
    }
    paths = {
        key: _contained_regular(root, value, label=f"final {key}")
        for key, value in relative.items()
    }
    expected_sha = {
        "video": sha_map.get("video"),
        "subtitle": sha_map.get("subtitle_srt"),
        "cover": sha_map.get("cover"),
        "record": sha_map.get("evidence_json"),
    }
    for key, path in paths.items():
        if _normalize_sha(expected_sha[key], label=key) != _sha256(path):
            raise _error(
                "FINAL_MEDIA_REVIEW_PRODUCTION_ADMISSION_BINDING_MISMATCH",
                f"{candidate_id} final {key} hash differs from manifest",
            )
    record = _read_json(paths["record"], label="daily final record")
    duration = _duration_binding(
        item, record=record, candidate_id=candidate_id
    )
    if duration is None or duration.get("duration_witness_mismatch") is not True:
        return _not_required(candidate_id, "NO_DURATION_WITNESS_MISMATCH")

    try:
        contract_sha, contracts = review._review_contracts()  # noqa: SLF001
    except review.FinalHumanReviewError as exc:
        raise _error(exc.reason_code, exc.detail or str(exc)) from exc
    points = contracts.get(candidate_id)
    if not bootstrap_is_required(duration, points):
        return _not_required(candidate_id, "NO_FULL_MEDIA_COMMITTED_REVIEW_POINT")
    closure = {
        "artifacts": {
            "video": str(relative["video"]),
            "subtitle": str(relative["subtitle"]),
            "cover": str(relative["cover"]),
        },
        "record_path": str(relative["record"]),
        **duration,
    }
    return {
        "status": "REQUIRED",
        "authority": ADMISSION_AUTHORITY,
        "candidate_id": candidate_id,
        "reason_code": "COMMITTED_DURATION_MISMATCH_FULL_MEDIA_REVIEW",
        "provider_calls": 0,
        "review_contract_sha256": contract_sha,
        "review_points": points,
        "closure": closure,
    }


def production_final_media_review_is_required(
    package_root: str | Path,
    candidate_id: str,
) -> bool:
    """Return the provider-free scheduling predicate for terminal backfill."""

    return (
        inspect_production_final_media_review_admission(
            package_root, candidate_id
        ).get("status")
        == "REQUIRED"
    )


def bootstrap_production_final_media_review_job(
    package_root: str | Path,
    candidate_id: str,
    *,
    probe: Callable[[Path], Mapping[str, object]] = probe_exact_media_clock,
) -> dict[str, object]:
    """Create/reuse the generic job only after committed daily admission."""

    admission = inspect_production_final_media_review_admission(
        package_root, candidate_id
    )
    if admission.get("status") != "REQUIRED":
        return admission
    root = _safe_root(package_root)
    _ensure_verification_directory(root)
    try:
        result = bootstrap_final_media_review_job(
            package_root=root,
            candidate_id=candidate_id,
            closure=admission["closure"],
            review_contract_sha256=str(
                admission["review_contract_sha256"]
            ),
            review_points=admission["review_points"],
            probe=probe,
        )
    except FinalMediaReviewBootstrapError as exc:
        raise _error(exc.reason_code, exc.detail) from exc
    return {
        **result,
        "admission_authority": ADMISSION_AUTHORITY,
        "admission_reason_code": admission["reason_code"],
        "review_contract_sha256": admission[
            "review_contract_sha256"
        ],
        "provider_calls": 0,
    }


__all__ = [
    "ADMISSION_AUTHORITY",
    "PRODUCTION_REVIEW_MANIFEST_SCHEMA_VERSION",
    "ProductionFinalMediaReviewAdmissionError",
    "bootstrap_production_final_media_review_job",
    "inspect_production_final_media_review_admission",
    "production_final_media_review_is_required",
]
