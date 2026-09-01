"""Generic reviewed-baseline replay state projection.

This module keeps the ordinary candidate-rejected transition separate from the
C7b failed-row adoption exception.  The replay module is resolved lazily so its
existing monkeypatch seams and import graph remain unchanged.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


def project_generic_replay_state_after(
    plan: Any, *, runtime_root: Any, state_path: Any,
    finalization: Any, projection: Any, package: Any = None,
    sealed_after_image: Any = None,
) -> Any:
    """Build the ordinary candidate-rejected Talk state after-image."""
    from src.autoslice import reviewed_baseline_replay as replay
    from src.autoslice.runner_state_writeback import state_bytes

    Error = replay.ReviewedBaselineReplayError
    state, before = replay._state_document(state_path, runtime_root=runtime_root)
    picks = state.get("picks")
    if not isinstance(picks, list):
        raise Error("REPLAY_STATE_PICK_INVALID")
    matching = [
        row for row in picks
        if isinstance(row, dict)
        and str(row.get("cid") or row.get("candidate_id") or "") == plan.candidate_id
    ]
    if len(matching) != 1:
        raise Error("REPLAY_STATE_PICK_PREIMAGE_DRIFT")
    if matching[0].get("status") == "failed":
        if package is None and sealed_after_image is None:
            raise Error("REPLAY_FAILED_ROW_PREPARED_PACKAGE_REQUIRED")
        raise Error("REPLAY_STATE_PICK_PREIMAGE_DRIFT")
    if matching[0].get("status") != "candidate_rejected":
        raise Error("REPLAY_STATE_PICK_PREIMAGE_DRIFT")
    delivered = replay._delivery_artifacts_from_prepared(
        plan, finalization=finalization, projection=projection,
    )
    record = replay._load_json(projection.record, label="PROJECTED_RECORD")
    publish = replay._load_json(projection.publish, label="PROJECTED_PUBLISH")
    video = delivered["video"]
    subtitle = delivered["subtitle"]
    cover = delivered["cover"]
    hashes = record.get("artifact_hashes")
    if not isinstance(hashes, Mapping) or hashes.get("burned_video_sha256") != video["sha256"]:
        raise Error("REPLAY_PROJECTED_BURNED_VIDEO_HASH_INVALID")
    publish_hashes = publish.get("artifact_hashes")
    if (
        not isinstance(publish_hashes, Mapping)
        or publish_hashes.get("cover_sha256") != cover["sha256"]
        or publish.get("cover_status") != "AI_COVER_READY"
    ):
        raise Error("REPLAY_PROJECTED_COVER_HASH_STATUS_INVALID")
    for value in (
        record.get("media_path"), record.get("subtitle_path"),
        publish.get("video_path"), publish.get("cover_path"),
    ):
        if not isinstance(value, str) or not Path(value).is_relative_to(replay._replay_package_root(plan)):
            raise Error("REPLAY_PROJECTED_LOCATOR_INVALID")
    story = record.get("story_contract")
    boundary = record.get("boundary_audit")
    timing = record.get("subtitle_timing_qa")
    speaker = replay._load_json(projection.speaker_manifest, label="PROJECTED_SPEAKER")
    if not isinstance(story, Mapping) or story.get("candidate_id") != plan.candidate_id:
        raise Error("REPLAY_PROJECTED_STORY_CANDIDATE_INVALID")
    row = matching[0]
    for key in (
        "failure_kind", "failure_stage", "failure_message", "failure_recoverable",
        "failure_recovery_fingerprint", "failure_fingerprint", "failure_provider_class",
        "failure_provider_status_codes", "rejected_status", "rejection_reason",
        "error", "reason_codes", "subtitle_sha256",
    ):
        row.pop(key, None)
    summary = {
        "candidate_id": plan.candidate_id,
        "final_end_ms": plan.local_end_ms,
        "duration_ms": record.get("duration_ms"),
        "closure_sentence": boundary.get("closure_sentence") if isinstance(boundary, Mapping) else None,
        "boundary_verdict": boundary.get("verdict") if isinstance(boundary, Mapping) else None,
        "red_flags": list(boundary.get("red_flags") or []) if isinstance(boundary, Mapping) else [],
        "boundary_repairs": list(boundary.get("boundary_repairs") or []) if isinstance(boundary, Mapping) else [],
        "timing_qa": timing.get("counts") if isinstance(timing, Mapping) else None,
        "cover_status": "AI_COVER_READY",
        "title": publish.get("title"),
        "delivery": video["target"],
        "subtitle": subtitle["target"],
        "speaker_subtitle": delivered.get("speaker_srt", {}).get("target"),
        "speaker_ass": delivered.get("speaker_ass", {}).get("target"),
        "speaker_status": speaker.get("status"),
        "speaker_guess": record.get("speaker_guess"),
        "subtitle_regression_status": "NOT_CONFIGURED",
        "redelivery_baseline_status": "APPLIED",
        "talk_filler_audit": record.get("talk_filler_audit_path"),
    }
    row.update({
        "status": "review_ready", "rc": 0, "summary": summary,
        "delivered": video["target"], "delivered_subtitle": subtitle["target"],
        "video_sha256": video["sha256"], "cover_status": "AI_COVER_READY",
        "cover_path": str(publish["cover_path"]), "cover_sha256": cover["sha256"],
        "cover_generation": publish.get("cover_generation"),
        "red_flags": list(summary["red_flags"]),
        "boundary_repairs": list(summary["boundary_repairs"]),
    })
    return replay.ReplayStateProjection(
        before=before, after=state_bytes(state), delivered=delivered,
    )
