"""Explicit time-domain contract for reviewed subtitle replay.

A reviewed SRT can be local either to the whole bound source interval or to the
already-cropped delivery.  Treating one as the other applies the delivery crop
twice and silently deletes/retimes cues.  Operator full-ownership v3 assets must
therefore declare the domain instead of relying on an ambiguous ``start_ms``.
"""

from __future__ import annotations

from collections.abc import Mapping


TIME_DOMAIN_KEY = "time_domain"
PIECE_LOCAL = "PIECE_LOCAL"
DELIVERY_LOCAL = "DELIVERY_LOCAL"
SUPPORTED_TIME_DOMAINS = frozenset({PIECE_LOCAL, DELIVERY_LOCAL})
OPERATOR_TEXT_PIN_V3 = "operator-reviewed-text-full-ownership-pin.v3"


class RedeliveryTimeDomainError(ValueError):
    """A reviewed baseline has no trustworthy source-to-delivery domain."""


def operator_v3_time_domain(config: Mapping[str, object]) -> str | None:
    """Return the mandatory explicit domain for operator-v3 baselines.

    Older baseline schemas retain their historical behavior.  The v3 operator
    lane is release-authoritative and may bypass fresh text generation, so an
    absent or unknown domain must fail closed.
    """

    ownership = config.get("operator_text_full_ownership")
    if not isinstance(ownership, Mapping) or ownership.get("schema_version") != OPERATOR_TEXT_PIN_V3:
        return None
    value = config.get(TIME_DOMAIN_KEY)
    if value not in SUPPORTED_TIME_DOMAINS:
        raise RedeliveryTimeDomainError(
            "REDELIVERY_BASELINE_TIME_DOMAIN_MISSING_OR_INVALID"
        )
    return str(value)


def require_delivery_local_interval(
    config: Mapping[str, object], *, absolute_start_ms: int, absolute_end_ms: int
) -> None:
    """Bind a DELIVERY_LOCAL baseline to the exact media interval once."""

    domain = operator_v3_time_domain(config)
    if domain != DELIVERY_LOCAL:
        return
    if (
        config.get("absolute_source_start_ms") != absolute_start_ms
        or config.get("absolute_source_end_ms") != absolute_end_ms
    ):
        raise RedeliveryTimeDomainError(
            "REDELIVERY_BASELINE_DELIVERY_INTERVAL_MISMATCH"
        )


def replay_diagnostic_duration_ms(
    config: Mapping[str, object],
    *,
    padded_start_ms: int,
    padded_end_ms: int,
    final_start_ms: int,
    final_end_ms: int,
) -> int:
    """Validate a replay plan's domain and return its local SRT duration."""

    domain = operator_v3_time_domain(config)
    if domain == DELIVERY_LOCAL:
        require_delivery_local_interval(
            config,
            absolute_start_ms=padded_start_ms + final_start_ms,
            absolute_end_ms=padded_start_ms + final_end_ms,
        )
        return final_end_ms - final_start_ms
    if domain == PIECE_LOCAL:
        if (
            config.get("absolute_source_start_ms") != padded_start_ms
            or config.get("absolute_source_end_ms") != padded_end_ms
        ):
            raise RedeliveryTimeDomainError(
                "REDELIVERY_BASELINE_PIECE_INTERVAL_MISMATCH"
            )
    return padded_end_ms - padded_start_ms


def require_baseline_receipt_parity(
    config: Mapping[str, object], receipt: Mapping[str, object]
) -> None:
    """Bind a v3 manifest to its canonical materializer receipt."""

    domain = operator_v3_time_domain(config)
    if domain is None:
        return
    source = receipt.get("source_recording")
    source_srt = receipt.get("source_srt")
    reviewed = receipt.get("reviewed_srt")
    manifest_lanes = config.get("operator_truth_lanes")
    receipt_lanes = receipt.get("truth_lanes")
    if not all(
        isinstance(value, Mapping)
        for value in (source, source_srt, reviewed, manifest_lanes, receipt_lanes)
    ):
        raise RedeliveryTimeDomainError("REDELIVERY_BASELINE_RECEIPT_PARITY_MISMATCH")
    expected_start = config.get("absolute_source_start_ms")
    expected_end = config.get("absolute_source_end_ms")
    expected_sha = config.get("sha256")
    if (
        receipt.get("candidate_id") != config.get("candidate_id")
        or source.get("basename") != config.get("source_recording_basename")
        or source.get("sha256") != config.get("source_sha256")
        or source.get("time_domain") != domain
        or source.get("absolute_start_ms") != expected_start
        or source.get("absolute_end_ms") != expected_end
        or receipt.get("baseline_sha256") != expected_sha
        or reviewed.get("sha256") != expected_sha
    ):
        raise RedeliveryTimeDomainError("REDELIVERY_BASELINE_RECEIPT_PARITY_MISMATCH")
    for lane_name in (
        "release_truth",
        "pipeline_diagnostic",
        "decision_ledger",
        "diff_receipt",
    ):
        manifest_lane = manifest_lanes.get(lane_name)
        receipt_lane = receipt_lanes.get(lane_name)
        if (
            not isinstance(manifest_lane, Mapping)
            or not isinstance(receipt_lane, Mapping)
            or manifest_lane.get("sha256") != receipt_lane.get("sha256")
        ):
            raise RedeliveryTimeDomainError(
                "REDELIVERY_BASELINE_RECEIPT_PARITY_MISMATCH"
            )
    pipeline_lane = manifest_lanes.get("pipeline_diagnostic")
    if (
        not isinstance(pipeline_lane, Mapping)
        or source_srt.get("sha256") != pipeline_lane.get("sha256")
    ):
        raise RedeliveryTimeDomainError("REDELIVERY_BASELINE_RECEIPT_PARITY_MISMATCH")
    rows = receipt.get("changed_cues")
    if (
        not isinstance(rows, list)
        or receipt.get("changed_cue_count") != len(rows)
        or isinstance(expected_start, bool)
        or not isinstance(expected_start, int)
    ):
        raise RedeliveryTimeDomainError("REDELIVERY_BASELINE_RECEIPT_PARITY_MISMATCH")
    for row in rows:
        if not isinstance(row, Mapping):
            raise RedeliveryTimeDomainError(
                "REDELIVERY_BASELINE_RECEIPT_PARITY_MISMATCH"
            )
        start_ms = row.get("start_ms")
        end_ms = row.get("end_ms")
        if (
            isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or row.get("absolute_source_start_ms") != expected_start + start_ms
            or row.get("absolute_source_end_ms") != expected_start + end_ms
        ):
            raise RedeliveryTimeDomainError(
                "REDELIVERY_BASELINE_RECEIPT_PARITY_MISMATCH"
            )
