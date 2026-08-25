"""Private receipt sealing helpers for reviewed-baseline replay stages."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping

from src.autoslice.redelivery_full_window_replay import (
    FullWindowReplayError,
    replay_full_window_text_and_crop,
)


def stage_delivery_projection_receipt(
    stage: Path,
    *,
    load_json: Callable[..., Mapping[str, object]],
    regular_binding: Callable[..., object],
    canonical: Callable[[object], bytes],
    sha: Callable[[bytes], str],
    sha_pattern: object,
    error: Callable[[str], Exception],
) -> tuple[Path | None, str | None]:
    """Return only a stage-sealed private projection receipt, if present."""

    document_path = stage / "stage.json"
    if not document_path.exists():
        return None, None
    document = load_json(regular_binding(document_path, label="STAGE_DOCUMENT"), label="STAGE_DOCUMENT")
    unsigned = dict(document)
    declared = unsigned.pop("stage_sha256", None)
    fullmatch = getattr(sha_pattern, "fullmatch", None)
    if not isinstance(declared, str) or not callable(fullmatch) or fullmatch(declared) is None or declared != sha(canonical(unsigned)):
        raise error("REPLAY_STAGE_DOCUMENT_DRIFT")
    descriptor = document.get("delivery_projection_receipt")
    if descriptor is None:
        return None, None
    if not isinstance(descriptor, Mapping):
        raise error("REPLAY_DELIVERY_PROJECTION_RECEIPT_INVALID")
    expected_path = stage / "full-release-delivery-projection.json"
    raw_path, raw_sha, raw_bytes = descriptor.get("path"), descriptor.get("sha256"), descriptor.get("bytes")
    if (
        raw_path != str(expected_path)
        or not isinstance(raw_sha, str) or fullmatch(raw_sha) is None
        or isinstance(raw_bytes, bool) or not isinstance(raw_bytes, int) or raw_bytes <= 0
    ):
        raise error("REPLAY_DELIVERY_PROJECTION_RECEIPT_INVALID")
    binding = regular_binding(expected_path, label="DELIVERY_PROJECTION_RECEIPT")
    if getattr(binding, "sha256", None) != raw_sha or getattr(binding, "size", None) != raw_bytes:
        raise error("REPLAY_DELIVERY_PROJECTION_RECEIPT_DRIFT")
    return expected_path, raw_sha


def prepare_stage_delivery_projection(
    plan: object,
    stage: Path,
    rebuilt: object,
    *,
    regular_binding: Callable[..., object],
    load_json: Callable[..., Mapping[str, object]],
    read_small_bytes: Callable[..., bytes],
    fresh_srt_to_source_cues: Callable[..., object],
    write_source_range_srt: Callable[..., None],
    error: Callable[[str], Exception],
    c5_start_clamp_proposal_path: Path | None = None,
    c5_start_clamp_acceptance_path: Path | None = None,
    recording_date: str | None = None,
) -> tuple[bytes, Mapping[str, object], dict[str, object] | None]:
    """Replay the full release and, only when needed, seal its 17-cue crop."""

    baseline = getattr(plan, "baseline")
    config = getattr(baseline, "config")
    record_path = getattr(plan, "record_path")
    record_binding = regular_binding(record_path, label="RECORD")
    record = load_json(record_binding, label="RECORD")
    boundary = record.get("boundary_audit")
    if not isinstance(boundary, Mapping):
        raise error("REPLAY_RECORD_TIMING_INVALID")
    parent = getattr(baseline, "manifest_path").parent
    lanes = config.get("operator_truth_lanes")
    if not isinstance(lanes, Mapping):
        raise error("REPLAY_DELIVERY_PROJECTION_LANES_INVALID")
    lane_bytes: dict[str, bytes] = {}
    for key, label in (
        ("pipeline_diagnostic", "PIPELINE_DIAGNOSTIC"),
        ("decision_ledger", "OPERATOR_DECISION_LEDGER"),
        ("diff_receipt", "OPERATOR_TRUTH_DIFF"),
    ):
        descriptor = lanes.get(key)
        raw_path = descriptor.get("path") if isinstance(descriptor, Mapping) else None
        if not isinstance(raw_path, str) or not raw_path:
            raise error("REPLAY_DELIVERY_PROJECTION_LANES_INVALID")
        path = Path(raw_path)
        if not path.is_absolute():
            path = parent / path
        if path.parent.absolute() != parent.absolute():
            raise error("REPLAY_DELIVERY_PROJECTION_LANES_INVALID")
        lane_bytes[key] = read_small_bytes(
            regular_binding(path, label=label), label=label
        )
    try:
        text = lane_bytes["pipeline_diagnostic"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise error("REPLAY_PIPELINE_DIAGNOSTIC_INVALID") from exc
    padded = getattr(plan, "padded_path")
    name = padded.name
    import re
    match = re.fullmatch(r"padded_(\d+)_(\d+)\.mp4", name)
    if match is None:
        raise error("REPLAY_PADDED_SOURCE_NAME_INVALID")
    padded_start, padded_end = (int(value) for value in match.groups())
    try:
        fresh_srt_to_source_cues(text, window_start_ms=0, duration_ms=padded_end - padded_start)
    except ValueError as exc:
        raise error("REPLAY_PIPELINE_DIAGNOSTIC_GEOMETRY_INVALID") from exc
    receipt = stage / "full-release-delivery-projection.json"
    crop = stage / ".reviewed-window.srt"
    try:
        cropped, audit = replay_full_window_text_and_crop(
            text=text, config=config, spec_parent=getattr(baseline, "manifest_path").parent,
            padded_start_ms=padded_start, padded_end_ms=padded_end,
            final_start_ms=getattr(plan, "local_start_ms"), final_end_ms=getattr(plan, "local_end_ms"),
            write_source_range_srt=write_source_range_srt, crop_path=crop,
            read_crop=lambda path: read_small_bytes(regular_binding(path, label="REVIEWED_WINDOW"), label="REVIEWED_WINDOW"),
            projection_receipt_path=receipt, projection_candidate_id=getattr(plan, "candidate_id"),
            projection_record_sha256=getattr(record_binding, "sha256"), projection_record_boundary=boundary,
            projection_staged_media_sha256=getattr(rebuilt, "sha256"),
            projection_lane_bytes=lane_bytes,
            c5_start_clamp_proposal_path=c5_start_clamp_proposal_path,
            c5_start_clamp_acceptance_path=c5_start_clamp_acceptance_path,
            recording_date=recording_date,
        )
    except FullWindowReplayError as exc:
        raise error(str(exc)) from exc
    descriptor = None
    if receipt.exists():
        binding = regular_binding(receipt, label="DELIVERY_PROJECTION_RECEIPT")
        descriptor = {"path": str(getattr(binding, "path")), "sha256": getattr(binding, "sha256"), "bytes": getattr(binding, "size")}
    return cropped, audit, descriptor
