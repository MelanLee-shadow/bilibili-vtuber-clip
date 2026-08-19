"""Pure record/publish mirror validation for external package imports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


PUBLISH_STAGING_LOCAL_KEYS = frozenset({"publish_json_path", "status"})
PUBLISH_NON_STAGING_KEYS = frozenset(
    {"artifact_hashes", "candidate_id", "schema_version", "video_path"}
)


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
    expected_keys = (
        set(publish) - PUBLISH_NON_STAGING_KEYS
    ) | set(PUBLISH_STAGING_LOCAL_KEYS)
    if set(publish_staging) != expected_keys:
        missing = sorted(expected_keys - set(publish_staging))
        extra = sorted(set(publish_staging) - expected_keys)
        raise PublishStagingMirrorError(
            "PUBLISH_STAGING_FIELD_SET_DRIFT",
            "record.publish_staging and publish.json declare different field "
            f"sets; missing={missing!r} extra={extra!r}",
        )
    for key in sorted(publish_staging):
        if key in PUBLISH_STAGING_LOCAL_KEYS:
            continue
        if publish_staging[key] != publish[key]:
            raise PublishStagingMirrorError(
                "PUBLISH_STAGING_VALUE_DRIFT",
                "record.publish_staging and publish.json disagree on mirrored "
                f"field {key!r}",
            )
    return publish_staging
