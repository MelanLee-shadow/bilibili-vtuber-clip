"""Media lineage checks for manual cover binding."""

from __future__ import annotations

from collections.abc import Mapping

from src.autoslice.verified_io import _document_video_hash


def validate_manual_cover_media_lineage(
    *,
    publish_document: Mapping[str, object],
    record_document: Mapping[str, object] | None,
    media_sha256: str,
) -> None:
    """Bind either direct delivery media or a source-to-burned media chain."""

    if record_document is not None and (
        _document_video_hash(record_document) != media_sha256
    ):
        raise ValueError("package delivery record video hash mismatch")
    if _document_video_hash(publish_document) == media_sha256:
        return
    publish_hashes = publish_document.get("artifact_hashes") or {}
    record_hashes = (record_document or {}).get("artifact_hashes") or {}
    if not (
        isinstance(publish_hashes, Mapping)
        and isinstance(record_hashes, Mapping)
        and not publish_hashes.get("burned_video_sha256")
        and record_hashes.get("burned_video_sha256") == media_sha256
        and publish_hashes.get("video_sha256")
        and publish_hashes["video_sha256"] == record_hashes.get("video_sha256")
    ):
        raise ValueError(
            "package publish draft video hash does not match delivery media"
        )
