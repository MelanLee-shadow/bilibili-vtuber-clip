"""Media execution, provenance, and rollback transactions for the producer."""

from __future__ import annotations

from pathlib import Path

from .shadow_review import _sha256


def _validated_burned_artifact(record: dict) -> Path:
    """Return the exact burn bound into the record, never a directory glob."""

    preview = record.get("burned_preview")
    if not isinstance(preview, dict) or preview.get("status") != "BURNED":
        raise RuntimeError(f"FINAL_SUBTITLE_BURN_NOT_READY: {preview}")
    value = preview.get("path")
    burned = Path(str(value)) if value else None
    if burned is None or not burned.is_file():
        raise RuntimeError(f"FINAL_SUBTITLE_BURN_MISSING: {value}")
    actual = "sha256:" + _sha256(burned)
    expected_preview = preview.get("burned_sha256")
    expected_record = (record.get("artifact_hashes") or {}).get("burned_video_sha256")
    if expected_preview != actual or expected_record != actual:
        raise RuntimeError(
            f"FINAL_SUBTITLE_BURN_HASH_MISMATCH: preview={expected_preview} "
            f"record={expected_record} actual={actual}"
        )
    return burned


