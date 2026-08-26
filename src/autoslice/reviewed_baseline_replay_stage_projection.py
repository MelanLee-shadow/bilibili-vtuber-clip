"""Private receipt sealing helpers for reviewed-baseline replay stages."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping

from src.autoslice.redelivery_full_window_replay import (
    FullWindowReplayError,
    replay_full_window_text_and_crop,
)
from src.autoslice.redelivery_subtitle_baseline import (
    apply_redelivery_subtitle_baseline,
)
from src.autoslice.redelivery_time_domain import (
    DELIVERY_LOCAL,
    RedeliveryTimeDomainError,
    operator_v3_time_domain,
    require_delivery_local_interval,
)
from src.autoslice.fastlane_c3_private_stage import (
    C3PrivateStageAuthorityError,
    resolve_c3_private_stage_geometry,
)


_PRIVATE_OPERATOR_STAGE_CANDIDATES = frozenset({
    ("auto_130040_201_255", "2026-08-14"),
    ("auto_130012_435_574", "2026-08-15"),
})


def baseline_application_interval(
    *,
    config: Mapping[str, object],
    padded_start_ms: int,
    padded_end_ms: int,
    final_start_ms: int,
    final_end_ms: int,
    error: Callable[[str], Exception],
) -> tuple[int, int, int, int]:
    """Resolve the only source interval a sealed v2 baseline may consume.

    Most reviewed baselines describe the whole padded source.  A small number
    are explicitly attested from the final recut start through a longer
    source-owned review window.  Accept only those two geometries: accepting
    any arbitrary subinterval would silently turn a source-bound replay into
    an unreviewed crop.
    """

    if (
        config.get("schema_version") != "subtitle-redelivery-baseline.v2"
        or config.get("exact_interval_replay") is not True
    ):
        return padded_start_ms, padded_end_ms, final_start_ms, final_end_ms
    start = config.get("absolute_source_start_ms")
    end = config.get("absolute_source_end_ms")
    if (
        isinstance(start, bool) or not isinstance(start, int)
        or isinstance(end, bool) or not isinstance(end, int)
        or start >= end
    ):
        raise error("REPLAY_BASELINE_ATTESTED_INTERVAL_INVALID")
    padded = (padded_start_ms, padded_end_ms)
    final = (padded_start_ms + final_start_ms, padded_start_ms + final_end_ms)
    attested = (start, end)
    if attested == padded:
        return padded_start_ms, padded_end_ms, final_start_ms, final_end_ms
    # The reviewed SRT's local zero is the record's exact source start.  Its
    # attested tail may deliberately retain source context beyond the current
    # delivery endpoint, but it must remain inside the padded provenance.
    if start == final[0] and final[1] <= end <= padded_end_ms:
        return start, end, 0, final[1] - final[0]
    raise error("REPLAY_BASELINE_ATTESTED_INTERVAL_DRIFT")


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
    c3_geometry: object | None = None,
    private_stage_authority_gate: bool = False,
) -> tuple[bytes, Mapping[str, object], dict[str, object] | None]:
    """Replay a baseline once in its declared domain and seal any real crop."""

    baseline = getattr(plan, "baseline")
    config = getattr(baseline, "config")
    ownership = config.get("operator_text_full_ownership")
    pinned_operator = ownership.get("operator_authority") if isinstance(ownership, Mapping) else None
    if (
        private_stage_authority_gate
        and isinstance(pinned_operator, Mapping)
        and (getattr(plan, "candidate_id", None), getattr(plan, "date", None))
        not in _PRIVATE_OPERATOR_STAGE_CANDIDATES
    ):
        raise error("REPLAY_BASELINE_APPLICATION_FAILED")
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
    baseline_start, baseline_end, crop_start, crop_end = baseline_application_interval(
        config=config,
        padded_start_ms=padded_start,
        padded_end_ms=padded_end,
        final_start_ms=getattr(plan, "local_start_ms"),
        final_end_ms=getattr(plan, "local_end_ms"),
        error=error,
    )
    try:
        fresh_srt_to_source_cues(text, window_start_ms=0, duration_ms=baseline_end - baseline_start)
    except ValueError as exc:
        raise error("REPLAY_PIPELINE_DIAGNOSTIC_GEOMETRY_INVALID") from exc
    if c3_geometry is None:
        try:
            c3_geometry = resolve_c3_private_stage_geometry(plan)
        except C3PrivateStageAuthorityError as exc:
            raise error(str(exc)) from exc
    if c3_geometry is not None:
        binding = getattr(c3_geometry, "binding")
        source_local_start = getattr(c3_geometry, "source_local_start_ms")
        source_local_end = getattr(c3_geometry, "source_local_end_ms")
        record_local_start = getattr(c3_geometry, "record_local_start_ms")
        record_local_end = getattr(c3_geometry, "record_local_end_ms")
        if (
            source_local_start != 0
            or source_local_end != getattr(binding, "absolute_source_end_ms") - getattr(binding, "absolute_source_start_ms")
            or record_local_end - record_local_start != source_local_end
        ):
            raise error("C3_STAGE_FINAL_INTERVAL_DRIFT")
        try:
            reviewed, audit = apply_redelivery_subtitle_baseline(
                text, config=config, spec_parent=parent,
                current_source_start_ms=getattr(binding, "absolute_source_start_ms"),
                current_source_end_ms=getattr(binding, "absolute_source_end_ms"),
                current_source_recording_basename=getattr(binding, "source_recording_basename"),
                current_source_sha256=getattr(binding, "source_sha256"),
                candidate_id=getattr(plan, "candidate_id", None),
                recording_date=getattr(plan, "date", None),
            )
        except (TypeError, ValueError) as exc:
            raise error("C3_STAGE_FINAL_INTERVAL_APPLICATION_INVALID") from exc
        if audit.get("status") not in {"APPLIED", "ALREADY_SATISFIED"}:
            raise error("REPLAY_BASELINE_APPLICATION_FAILED")
        try:
            fresh_srt_to_source_cues(
                reviewed, window_start_ms=source_local_start,
                duration_ms=source_local_end - source_local_start,
            )
        except ValueError as exc:
            raise error("C3_STAGE_FINAL_INTERVAL_GEOMETRY_INVALID") from exc
        return reviewed.encode("utf-8"), audit, None
    try:
        time_domain = operator_v3_time_domain(config)
    except RedeliveryTimeDomainError as exc:
        raise error(str(exc)) from exc
    if time_domain == DELIVERY_LOCAL:
        local_start = getattr(plan, "local_start_ms")
        local_end = getattr(plan, "local_end_ms")
        if (
            boundary.get("final_start_ms") != local_start
            or boundary.get("final_end_ms") != local_end
        ):
            raise error("REPLAY_RECORD_DELIVERY_INTERVAL_MISMATCH")
        absolute_start = padded_start + local_start
        absolute_end = padded_start + local_end
        try:
            require_delivery_local_interval(
                config,
                absolute_start_ms=absolute_start,
                absolute_end_ms=absolute_end,
            )
        except RedeliveryTimeDomainError as exc:
            raise error(str(exc)) from exc
        try:
            fresh_srt_to_source_cues(
                text,
                window_start_ms=0,
                duration_ms=local_end - local_start,
            )
            reviewed, audit = apply_redelivery_subtitle_baseline(
                text,
                config=config,
                spec_parent=parent,
                current_source_start_ms=absolute_start,
                current_source_end_ms=absolute_end,
                current_source_recording_basename=str(
                    config["source_recording_basename"]
                ),
                current_source_sha256=str(config["source_sha256"]),
                candidate_id=getattr(plan, "candidate_id", None),
                recording_date=getattr(plan, "date", None),
            )
            fresh_srt_to_source_cues(
                reviewed,
                window_start_ms=0,
                duration_ms=local_end - local_start,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise error("REPLAY_DELIVERY_LOCAL_BASELINE_INVALID") from exc
        if audit.get("status") not in {"APPLIED", "ALREADY_SATISFIED"}:
            raise error("REPLAY_BASELINE_APPLICATION_FAILED")
        return reviewed.encode("utf-8"), audit, None
    receipt = stage / "full-release-delivery-projection.json"
    crop = stage / ".reviewed-window.srt"
    try:
        cropped, audit = replay_full_window_text_and_crop(
            text=text, config=config, spec_parent=getattr(baseline, "manifest_path").parent,
            padded_start_ms=baseline_start, padded_end_ms=baseline_end,
            final_start_ms=crop_start, final_end_ms=crop_end,
            write_source_range_srt=write_source_range_srt, crop_path=crop,
            read_crop=lambda path: read_small_bytes(regular_binding(path, label="REVIEWED_WINDOW"), label="REVIEWED_WINDOW"),
            projection_receipt_path=receipt, projection_candidate_id=getattr(plan, "candidate_id"),
            projection_record_sha256=getattr(record_binding, "sha256"), projection_record_boundary=boundary,
            projection_staged_media_sha256=getattr(rebuilt, "sha256"),
            projection_lane_bytes=lane_bytes,
            c5_start_clamp_proposal_path=c5_start_clamp_proposal_path,
            c5_start_clamp_acceptance_path=c5_start_clamp_acceptance_path,
            recording_date=recording_date,
            delivery_projection_padded_start_ms=padded_start,
            delivery_projection_padded_end_ms=padded_end,
            delivery_projection_final_start_ms=getattr(plan, "local_start_ms"),
            delivery_projection_final_end_ms=getattr(plan, "local_end_ms"),
            delivery_projection_baseline_start_ms=(baseline_start if baseline_start != padded_start else None),
            delivery_projection_baseline_end_ms=(baseline_end if baseline_start != padded_start else None),
            delivery_projection_baseline_crop_start_ms=(crop_start if baseline_start != padded_start else None),
            delivery_projection_baseline_crop_end_ms=(crop_end if baseline_start != padded_start else None),
        )
    except FullWindowReplayError as exc:
        raise error(str(exc)) from exc
    descriptor = None
    if receipt.exists():
        binding = regular_binding(receipt, label="DELIVERY_PROJECTION_RECEIPT")
        descriptor = {"path": str(getattr(binding, "path")), "sha256": getattr(binding, "sha256"), "bytes": getattr(binding, "size")}
    return cropped, audit, descriptor
