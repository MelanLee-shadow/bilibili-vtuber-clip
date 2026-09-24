"""Existing import lane and publish-mirror checks; no file or state mutation."""
from __future__ import annotations
from typing import Any, Mapping
from .failed_pick_import import PackageImportError
from .surface_canon import CHANNEL_PROFILE
from .package_publish_mirror import (
    PublishStagingMirrorError,
    validate_publish_staging_mirror as _validate_publish_staging_mirror,
)


def package_lane(record: Mapping[str, Any], publish: Mapping[str, Any]) -> str:
    """Mirror ``build_daily_review_manifest._candidate_lane`` exactly.

    A song package whose record omits ``classification`` is still a song — the
    manifest builder decides by the channel's song title prefix.  Disagreeing
    here would let a song reach the talk-only bind and land in ``picks``
    instead of ``songs``.
    """

    if str(record.get("classification") or "").lower() == "song":
        return "song"
    if str(publish.get("title") or "").startswith(
        CHANNEL_PROFILE.song_title_prefix
    ):
        return "song"
    if isinstance(publish.get("lyrics_proof"), Mapping):
        return "song"
    return "talk"


def validate_publish_staging_mirror(
    record: Mapping[str, Any], publish: Mapping[str, Any]
) -> dict[str, Any]:
    """Translate the pure mirror validator into this importer's typed error."""

    try:
        return _validate_publish_staging_mirror(record, publish)
    except PublishStagingMirrorError as exc:
        raise PackageImportError(exc.code, exc.detail) from exc
