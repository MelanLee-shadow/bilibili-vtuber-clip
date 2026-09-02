"""C9-only source-action private replay stage.

Kept outside the generic reviewed-baseline replay module so C9 cannot increase
or alter the generic replay entry point.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def stage_c9_source_action_private_replay(
    *, repo_root: Path, date: str, candidate_id: str, stage_parent: Path,
) -> dict[str, Any]:
    from src.autoslice.fastlane_c9_private_replay import (
        FastlaneC9PrivateReplayError,
        materialize_c9_private_replay,
        validate_c9_root_acceptance_envelope,
    )
    from src.autoslice.reviewed_baseline_replay import (
        ReviewedBaselineReplayError,
        _canonical,
        _mkdir_private,
        _safe_directory,
        _sha,
        _write_private,
        regular_binding,
    )

    if date != "2026-08-15" or candidate_id != "auto_143025_1112_1285":
        raise ReviewedBaselineReplayError("REPLAY_C9_SOURCE_ACTION_IDENTITY_INVALID")
    repo_root = _safe_directory(repo_root)
    stage_parent = _safe_directory(stage_parent)
    try:
        projection = validate_c9_root_acceptance_envelope(repo_root)
    except FastlaneC9PrivateReplayError as exc:
        raise ReviewedBaselineReplayError("REPLAY_C9_SOURCE_ACTION_ACCEPTANCE_DRIFT") from exc
    seed = _sha(_canonical({
        "date": date,
        "candidate_id": candidate_id,
        "receipt": projection["receipt_sha256"],
        "source_srt": projection["source_srt_sha256"],
        "reviewed_srt": projection["reviewed_srt_sha256"],
        "acceptance_envelope": projection["acceptance_envelope_sha256"],
    })).removeprefix("sha256:")
    stage = _mkdir_private(stage_parent / f"{date}-{candidate_id}-c9-{seed[:16]}")
    try:
        materialize_c9_private_replay(root=repo_root, output_dir=stage / candidate_id)
    except FastlaneC9PrivateReplayError as exc:
        raise ReviewedBaselineReplayError("REPLAY_C9_SOURCE_ACTION_MATERIALIZATION_FAILED") from exc
    package = stage / candidate_id
    document: dict[str, Any] = {
        "schema_version": "reviewed-baseline-replay-c9-source-action-stage.v1",
        "date": date,
        "candidate_id": candidate_id,
        "receipt_sha256": projection["receipt_sha256"],
        "source_srt_sha256": projection["source_srt_sha256"],
        "reviewed_srt_sha256": projection["reviewed_srt_sha256"],
        "acceptance_envelope_sha256": projection["acceptance_envelope_sha256"],
        "accepted": True,
        "accepted_for_private_replay": True,
        "private_packet_sha256": regular_binding(package / "private-replay.json", label="C9_PRIVATE_PACKET").sha256,
        "upload_allowed": False,
        "canonical_delivery_allowed": False,
        "state_write_allowed": False,
        "provider_allowed": False,
    }
    document["stage_sha256"] = _sha(_canonical(document))
    _write_private(stage / "stage.json", _canonical(document))
    return {
        "stage": str(stage),
        "stage_sha256": document["stage_sha256"],
        "private_packet_sha256": document["private_packet_sha256"],
        "accepted": True,
        "accepted_for_private_replay": True,
        "upload_allowed": False,
    }
