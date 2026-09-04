"""Hash-bound screen-space protection for final cover identity landmarks."""

from __future__ import annotations

from collections.abc import Mapping


SCHEMA_VERSION = "lidousha-cover-identity-landmark-title-exclusion.v1"
LANDMARK = "lidousha_panda_ears"


def _boxes_intersect(left: object, right: object) -> bool:
    return bool(
        isinstance(left, (list, tuple)) and isinstance(right, (list, tuple))
        and len(left) == len(right) == 4
        and left[0] < right[2] and right[0] < left[2]
        and left[1] < right[3] and right[1] < left[3]
    )


def resolve_title_exclusion(
    value: Mapping[str, object] | None, *, background_sha256: str,
) -> dict[str, object] | None:
    """Validate a concrete exclusion before any title pixels are produced."""

    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version", "landmark", "background_sha256", "protected_bbox", "title_zone",
    }:
        raise ValueError("COVER_IDENTITY_LANDMARK_EXCLUSION_INVALID")
    protected, title_zone = value.get("protected_bbox"), value.get("title_zone")
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("landmark") != LANDMARK
        or value.get("background_sha256") != background_sha256
        or any(
            not isinstance(box, list) or len(box) != 4
            or any(isinstance(part, bool) or not isinstance(part, int) for part in box)
            or not (0 <= box[0] < box[2] <= 1920 and 0 <= box[1] < box[3] <= 1080)
            for box in (protected, title_zone)
        )
        or _boxes_intersect(protected, title_zone)
    ):
        raise ValueError("COVER_IDENTITY_LANDMARK_EXCLUSION_INVALID")
    return {
        "schema_version": value["schema_version"], "landmark": value["landmark"],
        "background_sha256": value["background_sha256"],
        "protected_bbox": list(protected), "title_zone": list(title_zone),
    }


def exclusion_evidence(
    exclusion: Mapping[str, object] | None, *, text_pixel_bbox: object,
) -> dict[str, object] | None:
    """Prove final title pixels do not cover the sealed landmark."""

    if exclusion is None:
        return None
    title_zone = exclusion["title_zone"]
    if (
        not isinstance(text_pixel_bbox, list) or len(text_pixel_bbox) != 4
        or text_pixel_bbox[0] < title_zone[0]
        or text_pixel_bbox[1] < title_zone[1]
        or text_pixel_bbox[2] > title_zone[2]
        or text_pixel_bbox[3] > title_zone[3]
        or _boxes_intersect(text_pixel_bbox, exclusion["protected_bbox"])
    ):
        raise ValueError("COVER_IDENTITY_LANDMARK_OCCLUDED")
    return {**exclusion, "status": "PASS", "text_pixel_bbox": list(text_pixel_bbox)}
