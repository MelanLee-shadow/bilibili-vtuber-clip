"""Deterministic HOST_ONLY source-region exclusion for screenshot covers.

The ordinary source-composition witness locates Li Dousha, but a margin around
that bbox can still include chat avatars.  This module consumes a separate,
hash-bound exclusion authority and materializes an identity card exclusively
from the approved source pixels.  It never edits, masks, inpaints, or generates
pixels outside the existing deterministic blur/contain compositor.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from PIL import Image

from src.autoslice.cover_source_composition import (
    SCHEMA_VERSION as SOURCE_COMPOSITION_SCHEMA,
    _compose_identity_card_crop,
    source_composition_bbox,
    validate_source_composition_verification,
)

AUTHORITY_SCHEMA = "lidousha-host-only-source-exclusion-authority.v1"
AUTHORITY_SCOPE = "HOST_ONLY_SOURCE_PIXEL_EXCLUSION"
CROP_STRATEGY = "HOST_ONLY_SAFE_REGION_IDENTITY_CARD"
OUTPUT_STATUS = "HASH_BOUND_HOST_ONLY_SAFE_REGION_IDENTITY_CARD"


class HostOnlySourceExclusionError(ValueError):
    """The exclusion authority or requested materialization is unsafe."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _box(value: object, *, label: str) -> tuple[float, float, float, float]:
    if not (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        and len(value) == 4
        and all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(float(item))
            for item in value
        )
    ):
        raise HostOnlySourceExclusionError(f"{label} is not a finite bbox")
    x0, y0, x1, y1 = (float(item) for item in value)
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        raise HostOnlySourceExclusionError(f"{label} is outside normalized bounds")
    return x0, y0, x1, y1


def _contains(outer: Sequence[float], inner: Sequence[float]) -> bool:
    return bool(
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and outer[2] >= inner[2]
        and outer[3] >= inner[3]
    )


def _intersects(left: Sequence[float], right: Sequence[float]) -> bool:
    return bool(
        max(left[0], right[0]) < min(left[2], right[2])
        and max(left[1], right[1]) < min(left[3], right[3])
    )


def _load_authority(path: Path, *, expected_sha256: str) -> dict[str, Any]:
    if not path.is_file() or _sha256(path) != expected_sha256:
        raise HostOnlySourceExclusionError("exclusion authority bytes drifted")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HostOnlySourceExclusionError("exclusion authority is unreadable") from exc
    if not isinstance(value, dict):
        raise HostOnlySourceExclusionError("exclusion authority is not an object")
    return value


def _validate_diagnostic(
    authority: Mapping[str, object], *, reference_sha256: str
) -> None:
    diagnostic = authority.get("diagnostic")
    if not isinstance(diagnostic, Mapping):
        raise HostOnlySourceExclusionError("diagnostic binding is absent")
    image_sha = str(diagnostic.get("image_sha256") or "")
    if image_sha and not image_sha.startswith("sha256:"):
        image_sha = "sha256:" + image_sha
    if not (
        diagnostic.get("provider") == "cpa"
        and diagnostic.get("status") == "OBSERVED"
        and image_sha == reference_sha256
        and isinstance(diagnostic.get("receipt_sha256"), str)
        and str(diagnostic["receipt_sha256"]).startswith("sha256:")
    ):
        raise HostOnlySourceExclusionError("diagnostic binding is invalid")


def validate_host_only_source_exclusion_authority(
    authority: Mapping[str, object],
    *,
    candidate_id: str,
    reference_sha256: str,
    source_size: tuple[int, int],
    source_composition_verification: Mapping[str, object],
    source_composition_receipt_sha256: str,
) -> tuple[float, float, float, float]:
    """Return the approved safe region after validating every causal binding."""

    if not (
        authority.get("schema_version") == AUTHORITY_SCHEMA
        and authority.get("status") == "PASS"
        and authority.get("scope") == AUTHORITY_SCOPE
        and authority.get("candidate_id") == candidate_id
        and authority.get("reference_sha256") == reference_sha256
        and authority.get("source_size") == [source_size[0], source_size[1]]
        and authority.get("source_composition_receipt_sha256")
        == source_composition_receipt_sha256
        and authority.get("safe_region_excludes_all_non_host") is True
        and isinstance(authority.get("accepted_by"), str)
        and bool(str(authority.get("accepted_by") or "").strip())
        and isinstance(authority.get("accepted_at"), str)
        and bool(str(authority.get("accepted_at") or "").strip())
    ):
        raise HostOnlySourceExclusionError("exclusion authority binding is invalid")
    witness_sha = str(source_composition_verification.get("witness_receipt_sha256") or "")
    if authority.get("source_composition_witness_sha256") != witness_sha:
        raise HostOnlySourceExclusionError("source-composition witness binding drifted")
    source_host = source_composition_bbox(source_composition_verification)
    if source_host is None:
        raise HostOnlySourceExclusionError("source-composition host bbox is absent")
    bound_source_host = _box(
        authority.get("source_composition_host_bbox_norm"),
        label="bound source-composition host bbox",
    )
    if list(bound_source_host) != [float(value) for value in source_host]:
        raise HostOnlySourceExclusionError("source-composition host bbox drifted")
    diagnostic_host = _box(
        authority.get("diagnostic_host_bbox_norm"), label="diagnostic host bbox"
    )
    safe = _box(authority.get("safe_source_region_norm"), label="safe source region")
    if not _contains(bound_source_host, diagnostic_host):
        raise HostOnlySourceExclusionError("diagnostic host escaped source authority")
    if not _contains(safe, diagnostic_host):
        raise HostOnlySourceExclusionError("safe source region clips the host")
    rows = authority.get("non_host_entities")
    if not isinstance(rows, list) or not rows:
        raise HostOnlySourceExclusionError("non-host entity evidence is absent")
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping) or not str(row.get("label") or "").strip():
            raise HostOnlySourceExclusionError(f"non-host entity {index} is invalid")
        bbox = _box(row.get("bbox_norm"), label=f"non-host entity {index} bbox")
        if _intersects(safe, bbox):
            raise HostOnlySourceExclusionError(
                f"safe source region intersects non-host entity {index}"
            )
    if safe == (0.0, 0.0, 1.0, 1.0):
        raise HostOnlySourceExclusionError("safe source region cannot be full-frame")
    _validate_diagnostic(authority, reference_sha256=reference_sha256)
    return safe


def materialize_host_only_safe_region_identity_card(
    *,
    reference_path: Path,
    output_path: Path,
    frame_ms: int,
    candidate_id: str,
    source_composition_verification: Mapping[str, object],
    source_composition_receipt_path: Path,
    source_composition_receipt_sha256: str,
    exclusion_authority_path: Path,
    exclusion_authority_sha256: str,
) -> dict[str, object]:
    """Render a 16:9 card using only the approved HOST_ONLY source region."""

    reference_sha256 = _sha256(reference_path)
    if not validate_source_composition_verification(
        source_composition_verification, reference_sha256=reference_sha256
    ):
        raise HostOnlySourceExclusionError("source-composition verification is invalid")
    if (
        not source_composition_receipt_path.is_file()
        or _sha256(source_composition_receipt_path)
        != source_composition_receipt_sha256
    ):
        raise HostOnlySourceExclusionError("source-composition receipt bytes drifted")
    with Image.open(reference_path) as raw_image:
        source = raw_image.convert("RGB")
    authority = _load_authority(
        exclusion_authority_path, expected_sha256=exclusion_authority_sha256
    )
    safe = validate_host_only_source_exclusion_authority(
        authority,
        candidate_id=candidate_id,
        reference_sha256=reference_sha256,
        source_size=source.size,
        source_composition_verification=source_composition_verification,
        source_composition_receipt_sha256=source_composition_receipt_sha256,
    )
    crop_box = (
        max(0, int(math.floor(safe[0] * source.width))),
        max(0, int(math.floor(safe[1] * source.height))),
        min(source.width, int(math.ceil(safe[2] * source.width))),
        min(source.height, int(math.ceil(safe[3] * source.height))),
    )
    if crop_box == (0, 0, source.width, source.height):
        raise HostOnlySourceExclusionError("safe source region rounded to full-frame")
    rendered, foreground_box = _compose_identity_card_crop(source, crop_box)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rendered.save(output_path, format="PNG", optimize=False)
    witness_sha = str(source_composition_verification.get("witness_receipt_sha256") or "")
    diagnostic_host = list(
        _box(authority.get("diagnostic_host_bbox_norm"), label="diagnostic host bbox")
    )
    return {
        "schema": "cover-frame-transfer.v2",
        "status": OUTPUT_STATUS,
        "frame_ms": int(frame_ms),
        "source_path": str(reference_path),
        "source_sha256": reference_sha256,
        "reference_sha256": reference_sha256,
        "crop_applied": True,
        "crop_box": list(crop_box),
        "crop_strategy": CROP_STRATEGY,
        "full_frame_degeneracy_avoided": True,
        "identity_card_foreground_box": foreground_box,
        "identity_card_background": "BLURRED_HOST_ONLY_SAFE_REGION",
        "source_size": [source.width, source.height],
        "output_size": list(rendered.size),
        "zoom": round(source.width / max(1, crop_box[2] - crop_box[0]), 4),
        "camera_window_crop": True,
        "authority_identity_crop": True,
        "authority_bbox_frac": diagnostic_host,
        "motion_bbox_role": "CANDIDATE_ONLY_NOT_AUTHORITY",
        "source_composition_schema_version": SOURCE_COMPOSITION_SCHEMA,
        "source_composition_witness_sha256": witness_sha,
        "source_composition_receipt_path": str(source_composition_receipt_path),
        "source_composition_receipt_sha256": source_composition_receipt_sha256,
        "host_only_source_exclusion_authority": {
            "path": str(exclusion_authority_path),
            "sha256": exclusion_authority_sha256,
            "schema_version": AUTHORITY_SCHEMA,
            "safe_source_region_norm": list(safe),
            "diagnostic_receipt_sha256": authority["diagnostic"]["receipt_sha256"],
        },
        "crop_output_sha256": _sha256(output_path),
    }
