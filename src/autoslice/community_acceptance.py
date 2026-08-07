"""Typed acceptance policy for community-name evidence."""

from __future__ import annotations

from typing import Any, Mapping


def mapping_status(
    *,
    prior_status: str | None,
    member: Mapping[str, Any],
    summary: Mapping[str, Any],
    config: Mapping[str, Any],
    conflict: bool,
    relation_kind: str,
) -> tuple[str, list[str]]:
    if conflict:
        return "conflict", ["SURFACE_CONFLICTS_WITH_ANOTHER_REGISTRY_ENTITY"]
    if prior_status == "accepted":
        return "accepted", ["ACCEPTED_MAPPING_PERSISTS"]
    gate = config["acceptance"]
    official_mid = member.get("official_mid")
    official_supported = isinstance(official_mid, int) and any(
        row_mid == official_mid for row_mid in summary.get("uploader_mids", [])
    )
    if official_supported and (
        summary["score"] >= int(gate["official_minimum_score"])
        and summary["video_count"] >= int(gate["official_minimum_videos"])
        and summary["distinct_uploader_mids"] >= int(gate["official_minimum_uploaders"])
    ):
        return "accepted", ["OFFICIAL_SELF_EVIDENCE_PLUS_INDEPENDENT_SUPPORT"]
    metadata_videos = int(summary.get("metadata_video_count", summary["video_count"]))
    balanced = summary["max_videos_from_one_uploader"] * 2 <= metadata_videos
    meme_pass = relation_kind == "meme_of" and (
        summary["score"] >= int(gate["meme_minimum_score"])
        and metadata_videos >= int(gate["meme_minimum_videos"])
        and summary["distinct_uploader_mids"] >= int(gate["meme_minimum_uploaders"])
        and summary["strong_metadata_link_count"]
        >= int(gate["meme_minimum_strong_links"])
        and balanced
    )
    if meme_pass:
        return "accepted", ["EVENT_MEME_INDEPENDENT_QUORUM_MET"]
    community_pass = (
        summary["score"] >= int(gate["minimum_score"])
        and metadata_videos >= int(gate["minimum_videos"])
        and summary["distinct_uploader_mids"] >= int(gate["minimum_uploaders"])
        and summary["metadata_distinct_days"] >= int(gate["minimum_days"])
        and summary["strong_link_count"] >= int(gate["minimum_strong_links"])
        and balanced
    )
    if community_pass:
        return "accepted", ["INDEPENDENT_COMMUNITY_QUORUM_MET"]

    comment_type_allowed = relation_kind != "associated_with"
    meme_comment_pass = relation_kind == "meme_of" and (
        summary["distinct_commenters"]
        >= int(gate["meme_comment_minimum_commenters"])
        and summary["comment_video_count"] >= int(gate["meme_comment_minimum_videos"])
        and (
            summary["official_comment_video_count"] >= 1
            or summary["comment_distinct_video_uploaders"] >= 2
        )
    )
    if meme_comment_pass:
        return "accepted", ["EVENT_MEME_COMMENT_QUORUM_MET"]

    official_comment_pass = comment_type_allowed and (
        summary["distinct_commenters"]
        >= int(gate["official_comment_minimum_commenters"])
        and summary["official_comment_video_count"] >= 1
        and max(summary["comment_video_count"], summary["comment_distinct_days"])
        >= int(gate["official_comment_minimum_videos_or_days"])
    )
    if official_comment_pass:
        return "accepted", ["OFFICIAL_UPLOAD_COMMENT_CONTEXT_PLUS_COMMUNITY_QUORUM"]

    comment_pass = comment_type_allowed and (
        summary["distinct_commenters"] >= int(gate["comment_minimum_commenters"])
        and summary["comment_video_count"] >= int(gate["comment_minimum_videos"])
        and summary["comment_distinct_days"] >= int(gate["comment_minimum_days"])
    )
    if comment_pass:
        return "accepted", ["INDEPENDENT_COMMENTER_QUORUM_MET"]
    return "candidate", ["INSUFFICIENT_INDEPENDENT_EVIDENCE"]
