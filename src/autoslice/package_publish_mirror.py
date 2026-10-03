"""Pure record/publish mirror validation for external package imports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


PUBLISH_STAGING_REQUIRED_LOCAL_KEYS = frozenset({"publish_json_path", "status"})
PUBLISH_STAGING_OPTIONAL_LOCAL_KEYS = frozenset({"video_path", "subtitle_path"})
PUBLISH_STAGING_LOCAL_KEYS = (
    PUBLISH_STAGING_REQUIRED_LOCAL_KEYS | PUBLISH_STAGING_OPTIONAL_LOCAL_KEYS
)
PUBLISH_NON_STAGING_KEYS = frozenset(
    {"artifact_hashes", "candidate_id", "schema_version", "video_path"}
)


def publish_staging_field_sets(
    publish: Mapping[str, Any],
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Return mirrored, required, and allowed staging fields.

    ``publish_json_path`` and ``status`` are required record-local fields.
    ``video_path`` and ``subtitle_path`` are optional record-local locators used
    by package-specific recovery projections.  They may be present without
    becoming standalone publish semantics, but no unknown extra field is
    accepted.  If a normally local name is explicitly present in the publish
    document, it remains a mirrored field and its value must match.
    """

    mirrored = frozenset(set(publish) - PUBLISH_NON_STAGING_KEYS)
    required = mirrored | PUBLISH_STAGING_REQUIRED_LOCAL_KEYS
    allowed = mirrored | PUBLISH_STAGING_LOCAL_KEYS
    return mirrored, required, allowed


class PublishStagingMirrorError(ValueError):
    """The producer's record and standalone publish draft disagree."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def validate_publish_staging_mirror(
    record: Mapping[str, Any], publish: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the staging mirror only when its exact fields and values match."""

    publish_staging = record.get("publish_staging")
    if not isinstance(publish_staging, dict):
        raise PublishStagingMirrorError(
            "PACKAGE_DOCUMENT_INVALID", "record.publish_staging is not an object"
        )
    mirrored, required, allowed = publish_staging_field_sets(publish)
    actual = set(publish_staging)
    missing = sorted(required - actual)
    extra = sorted(actual - allowed)
    if missing or extra:
        raise PublishStagingMirrorError(
            "PUBLISH_STAGING_FIELD_SET_DRIFT",
            "record.publish_staging and publish.json declare different field "
            f"sets; missing={missing!r} extra={extra!r}",
        )
    for key in sorted(mirrored):
        if publish_staging[key] != publish[key]:
            raise PublishStagingMirrorError(
                "PUBLISH_STAGING_VALUE_DRIFT",
                "record.publish_staging and publish.json disagree on mirrored "
                f"field {key!r}",
            )
    return publish_staging
