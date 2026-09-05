"""C12-only package-owner consumer for sealed source-truth supersession.

This module intentionally recognizes one candidate and one terminal schema.
It is not a general source-truth success path: the generic package auditor
imports only the dispatcher and preserves its ordinary owner contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path

from src.autoslice.channel_profile import load_channel_profile


C12_CANDIDATE_ID = "auto_130012_435_574"
C12_SOURCE_TRUTH_SUPERSESSION_SCHEMA = (
    "source-subtitle-truth-reapplication-supersession.v1"
)
_C12_SOURCE_TRUTH_SUPERSESSION_STATUS = (
    "SUPERSEDED_BY_OPERATOR_TEXT_FULL_OWNERSHIP"
)
_SOURCE_TRUTH_AUDIT_SCHEMA = "source-subtitle-truth-audit.v1"
_OPERATOR_SUPERSESSION_SCHEMA = (
    "reviewed-baseline-replay-c12-operator-text-supersession.v1"
)
_DELIVERY_RECEIPT_SCHEMA = "reviewed-baseline-full-release-delivery-projection.v2"
_SOURCE_TRUTH_FIELDS = frozenset({
    "schema_version",
    "status",
    "candidate_id",
    "reapplication_attempted",
    "source_truth_reapplication_allowed",
    "pre_redelivery_audit_sha256",
    "preexisting_truth_row_count",
    "superseded_truth_ids",
    "operator_text_full_ownership",
    "final_delivery_srt_sha256",
    "applied",
    "satisfied",
    "failures",
    "baseline_sha256",
})
_OPERATOR_SUPERSESSION_FIELDS = frozenset({
    "schema_version",
    "candidate_id",
    "record_sha256",
    "delivery_projection_receipt_sha256",
    "baseline_sha256",
    "pipeline_diagnostic_sha256",
    "decision_ledger_sha256",
    "diagnostic_diff_sha256",
    "source_cue_count",
    "release_cue_count",
    "operator_exact_text_cue_count",
    "operator_unchanged_freeze_cue_count",
    "operator_drop_cue_count",
    "final_delivery_srt_sha256",
    "source_truth_reapplication_allowed",
})
_DELIVERY_RECEIPT_FIELDS = frozenset({
    "schema_version",
    "receipt_sha256",
    "full_release_cue_count",
    "final_delivery_cue_count",
})
_TRUTH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256 = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
_PREFIXED_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_BOUND_ARTIFACT_BYTES = 4 * 1024 * 1024
_MAX_SUPERSEDED_TRUTH_ROWS = 1024


def _strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _strict_nonnegative_int(value: object) -> bool:
    return _strict_int(value) and int(value) >= 0


def _same_sha256(*values: object) -> bool:
    return bool(
        values
        and all(isinstance(value, str) and _SHA256.fullmatch(value) for value in values)
        and len({str(value).removeprefix("sha256:") for value in values}) == 1
    )


def _canonical_sha256(value: object) -> str | None:
    try:
        payload = (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _regular_bytes(path: Path) -> bytes | None:
    """Read one small package artifact without a symlink or late file swap."""

    try:
        before = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size < 0
            or before.st_size > _MAX_BOUND_ARTIFACT_BYTES
        ):
            return None
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
        finally:
            os.close(descriptor)
        after = os.lstat(path)
    except OSError:
        return None
    if (
        not stat.S_ISREG(after.st_mode)
        or (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
    ):
        return None
    return b"".join(chunks)


def _contained_record_artifact(record_path: Path, value: object) -> Path | None:
    """Resolve a record locator only inside the packaged record directory."""

    if not isinstance(value, str) or not value:
        return None
    raw = Path(value)
    relative = Path(raw.name) if raw.is_absolute() else raw
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    try:
        root = record_path.parent.resolve(strict=True)
        candidate = record_path.parent
        for part in relative.parts:
            candidate = candidate / part
            if candidate.is_symlink():
                return None
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved


def _record_bound_successor(
    *,
    record: Mapping[str, object],
    record_path: Path | None,
) -> Mapping[str, object] | None:
    """Return C12's old-record bridge from actual record and manifest bytes."""

    if record_path is None:
        return None
    record_raw = _regular_bytes(record_path)
    if record_raw is None:
        return None
    try:
        record_document = json.loads(record_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(record_document, Mapping) or dict(record_document) != dict(record):
        return None
    raw_manifest_path = record_document.get("speaker_finalization_manifest_path")
    declared_manifest_sha = record_document.get("speaker_finalization_manifest_sha256")
    embedded_manifest = record_document.get("speaker_finalization")
    if (
        not isinstance(raw_manifest_path, str)
        or not raw_manifest_path
        or not isinstance(declared_manifest_sha, str)
        or _PREFIXED_SHA256.fullmatch(declared_manifest_sha) is None
        or not isinstance(embedded_manifest, Mapping)
    ):
        return None
    manifest_path = _contained_record_artifact(record_path, raw_manifest_path)
    manifest_raw = _regular_bytes(manifest_path) if manifest_path is not None else None
    if manifest_raw is None or not _same_sha256(
        declared_manifest_sha,
        "sha256:" + hashlib.sha256(manifest_raw).hexdigest(),
    ):
        return None
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, Mapping) or dict(manifest) != dict(embedded_manifest):
        return None
    # Make the record/path binding stable across the separate manifest read.
    if _regular_bytes(record_path) != record_raw:
        return None
    successor = manifest.get("reviewed_baseline_text_only_successor")
    return successor if isinstance(successor, Mapping) else None


def _operator_marker_matches_repository(marker: Mapping[str, object]) -> bool:
    """Re-open C12's v3 59/56/3 graph and all hash-bound truth lanes."""

    if set(marker) != _OPERATOR_SUPERSESSION_FIELDS:
        return False
    if (
        marker.get("schema_version") != _OPERATOR_SUPERSESSION_SCHEMA
        or marker.get("candidate_id") != C12_CANDIDATE_ID
        or marker.get("source_cue_count") != 59
        or marker.get("release_cue_count") != 59
        or marker.get("operator_exact_text_cue_count") != 3
        or marker.get("operator_unchanged_freeze_cue_count") != 56
        or marker.get("operator_drop_cue_count") != 0
        or marker.get("source_truth_reapplication_allowed") is not False
        or not all(
            isinstance(marker.get(key), str)
            and _PREFIXED_SHA256.fullmatch(str(marker[key]))
            for key in (
                "record_sha256",
                "delivery_projection_receipt_sha256",
                "baseline_sha256",
                "pipeline_diagnostic_sha256",
                "decision_ledger_sha256",
                "diagnostic_diff_sha256",
                "final_delivery_srt_sha256",
            )
        )
    ):
        return False
    try:
        from src.autoslice.reviewed_subtitle_baseline_registry import (
            ReviewedSubtitleBaselineRegistryError,
            load_candidate_reviewed_subtitle_baseline,
        )

        repo_root = Path(__file__).resolve().parents[2]
        profile = load_channel_profile(repo_root)
        registered = load_candidate_reviewed_subtitle_baseline(
            profile.asset_directory("reviewed_subtitle_baselines"),
            C12_CANDIDATE_ID,
            repo_root=repo_root,
        )
    except (OSError, ReviewedSubtitleBaselineRegistryError, TypeError, ValueError):
        return False
    if registered is None:
        return False
    config = registered.config
    ownership = config.get("operator_text_full_ownership")
    lanes = config.get("operator_truth_lanes")
    if not isinstance(ownership, Mapping) or not isinstance(lanes, Mapping):
        return False
    pipeline = lanes.get("pipeline_diagnostic")
    ledger = lanes.get("decision_ledger")
    diff = lanes.get("diff_receipt")
    if not all(isinstance(value, Mapping) for value in (pipeline, ledger, diff)):
        return False
    return bool(
        config.get("schema_version") == "subtitle-redelivery-baseline.v2"
        and config.get("exact_interval_replay") is True
        and config.get("absolute_source_start_ms") == 434_920
        and config.get("absolute_source_end_ms") == 603_840
        and ownership.get("schema_version") == "operator-reviewed-text-full-ownership-pin.v3"
        and ownership.get("source_cue_count") == 59
        and ownership.get("release_cue_count") == 59
        and ownership.get("operator_exact_text_cue_count") == 3
        and ownership.get("operator_unchanged_freeze_cue_count") == 56
        and ownership.get("operator_drop_cue_count") == 0
        and _same_sha256(marker.get("baseline_sha256"), config.get("sha256"))
        and _same_sha256(
            marker.get("pipeline_diagnostic_sha256"),
            ownership.get("pipeline_srt_sha256"),
            pipeline.get("sha256"),
        )
        and _same_sha256(
            marker.get("decision_ledger_sha256"),
            ownership.get("decision_ledger_sha256"),
            ledger.get("sha256"),
        )
        and _same_sha256(
            marker.get("diagnostic_diff_sha256"),
            ownership.get("diagnostic_diff_sha256"),
            diff.get("sha256"),
        )
    )


def c12_source_truth_supersession_valid(
    truth_audit: Mapping[str, object],
    *,
    chat_authority: Mapping[str, object] | None,
    record: Mapping[str, object] | None,
    record_path: Path | None,
    subtitle_path: Path | None,
) -> bool:
    """Validate the sole C12 schema that retires legacy source-text owners."""

    if (
        set(truth_audit) != _SOURCE_TRUTH_FIELDS
        or truth_audit.get("schema_version") != C12_SOURCE_TRUTH_SUPERSESSION_SCHEMA
        or truth_audit.get("status") != _C12_SOURCE_TRUTH_SUPERSESSION_STATUS
        or truth_audit.get("candidate_id") != C12_CANDIDATE_ID
        or truth_audit.get("reapplication_attempted") is not False
        or truth_audit.get("source_truth_reapplication_allowed") is not False
        or truth_audit.get("applied") != []
        or truth_audit.get("satisfied") != []
        or truth_audit.get("failures") != []
        or not isinstance(truth_audit.get("pre_redelivery_audit_sha256"), str)
        or _PREFIXED_SHA256.fullmatch(str(truth_audit["pre_redelivery_audit_sha256"]))
        is None
        or not isinstance(truth_audit.get("baseline_sha256"), str)
        or _PREFIXED_SHA256.fullmatch(str(truth_audit["baseline_sha256"])) is None
        or not isinstance(truth_audit.get("final_delivery_srt_sha256"), str)
        or _PREFIXED_SHA256.fullmatch(str(truth_audit["final_delivery_srt_sha256"]))
        is None
    ):
        return False
    count = truth_audit.get("preexisting_truth_row_count")
    identifiers = truth_audit.get("superseded_truth_ids")
    marker = truth_audit.get("operator_text_full_ownership")
    if (
        not _strict_int(count)
        or int(count) <= 0
        or int(count) > _MAX_SUPERSEDED_TRUTH_ROWS
        or not isinstance(identifiers, list)
        or not identifiers
        or len(identifiers) > 128
        or not all(
            isinstance(value, str) and _TRUTH_ID.fullmatch(value) for value in identifiers
        )
        or identifiers != sorted(set(identifiers))
        or int(count) < len(identifiers)
        or not isinstance(marker, Mapping)
        or not _operator_marker_matches_repository(marker)
        or not _same_sha256(
            truth_audit.get("baseline_sha256"), marker.get("baseline_sha256")
        )
        or not _same_sha256(
            truth_audit.get("final_delivery_srt_sha256"),
            marker.get("final_delivery_srt_sha256"),
        )
    ):
        return False
    if not isinstance(chat_authority, Mapping) or not isinstance(record, Mapping):
        return False
    pre_truth_audit = chat_authority.get("source_subtitle_truth_pre_redelivery_audit")
    if (
        chat_authority.get("source_subtitle_truth_post_redelivery_audit") != truth_audit
        or not isinstance(pre_truth_audit, Mapping)
        or pre_truth_audit.get("schema_version") != _SOURCE_TRUTH_AUDIT_SCHEMA
        or _canonical_sha256(pre_truth_audit)
        != truth_audit.get("pre_redelivery_audit_sha256")
    ):
        return False
    pre_rows: list[Mapping[str, object]] = []
    for key in ("applied", "satisfied", "failures"):
        rows = pre_truth_audit.get(key)
        if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
            return False
        pre_rows.extend(rows)
    pre_ids = sorted(
        {
            str(row.get("truth_id"))
            for row in pre_rows
            if isinstance(row.get("truth_id"), str)
        }
    )
    if (
        len(pre_rows) != count
        or not pre_ids
        or pre_ids != identifiers
        or any(not isinstance(row.get("truth_id"), str) for row in pre_rows)
        or not all(_TRUTH_ID.fullmatch(value) for value in pre_ids)
    ):
        return False
    baseline_audit = chat_authority.get("redelivery_subtitle_baseline_audit")
    if not isinstance(baseline_audit, Mapping):
        return False
    delivery_receipt = baseline_audit.get("full_release_delivery_projection")
    if (
        baseline_audit.get("status") not in {"APPLIED", "ALREADY_SATISFIED"}
        or baseline_audit.get("application_strategy")
        != "exact_reviewed_interval_replay"
        or baseline_audit.get("exact_replay_then_final_crop") is not True
        or baseline_audit.get("source_truth_reapplication") != truth_audit
        or baseline_audit.get("operator_text_full_ownership_supersession") != marker
        or not _same_sha256(
            baseline_audit.get("baseline_sha256"),
            baseline_audit.get("expected_baseline_sha256"),
            marker.get("baseline_sha256"),
        )
        or not _same_sha256(
            baseline_audit.get("post_source_truth_output_sha256"),
            marker.get("final_delivery_srt_sha256"),
        )
        or not isinstance(delivery_receipt, Mapping)
        or set(delivery_receipt) != _DELIVERY_RECEIPT_FIELDS
        or delivery_receipt.get("schema_version") != _DELIVERY_RECEIPT_SCHEMA
        or delivery_receipt.get("full_release_cue_count") != 59
        or not _strict_int(delivery_receipt.get("final_delivery_cue_count"))
        or not 0 < int(delivery_receipt["final_delivery_cue_count"]) <= 59
        or not _same_sha256(
            delivery_receipt.get("receipt_sha256"),
            marker.get("delivery_projection_receipt_sha256"),
        )
    ):
        return False
    successor = _record_bound_successor(record=record, record_path=record_path)
    if (
        not isinstance(successor, Mapping)
        or successor.get("schema_version")
        != "reviewed-baseline-text-only-speaker-successor.v1"
        or successor.get("candidate_id") != C12_CANDIDATE_ID
        or successor.get("input_grid_mode") != "FINAL_DELIVERY_PROJECTION"
        or successor.get("speaker_labels_inherited") is not True
        or successor.get("speaker_label_mutation_authorized") is not False
        or not _same_sha256(
            successor.get("old_record_sha256"), marker.get("record_sha256")
        )
        or not _same_sha256(
            successor.get("delivery_projection_receipt_sha256"),
            marker.get("delivery_projection_receipt_sha256"),
        )
        or not _same_sha256(
            successor.get("reviewed_baseline_sha256"), marker.get("baseline_sha256")
        )
        or not _same_sha256(
            successor.get("old_diagnostic_sha256"),
            marker.get("pipeline_diagnostic_sha256"),
        )
        or not _same_sha256(
            successor.get("operator_ledger_sha256"), marker.get("decision_ledger_sha256")
        )
        or not _same_sha256(
            successor.get("operator_truth_diff_sha256"), marker.get("diagnostic_diff_sha256")
        )
        or not _same_sha256(
            successor.get("new_plain_srt_sha256"), marker.get("final_delivery_srt_sha256")
        )
    ):
        return False
    subtitle_bytes = _regular_bytes(subtitle_path) if subtitle_path is not None else None
    artifact_hashes = record.get("artifact_hashes")
    if (
        subtitle_bytes is None
        or not isinstance(artifact_hashes, Mapping)
        or not _same_sha256(
            "sha256:" + hashlib.sha256(subtitle_bytes).hexdigest(),
            artifact_hashes.get("subtitle_sha256"),
            chat_authority.get("final_text_srt_sha256"),
            chat_authority.get("final_output_srt_sha256"),
            marker.get("final_delivery_srt_sha256"),
        )
    ):
        return False
    story_contract = record.get("story_contract")
    return isinstance(story_contract, Mapping) and (
        story_contract.get("candidate_id") == C12_CANDIDATE_ID
    )


def c12_zero_current_source_owner_receipt_valid(truth_owner: object) -> bool:
    """Require the normal final-owner receipt to prove zero C12 text owners."""

    if not isinstance(truth_owner, Mapping):
        return False
    interval = truth_owner.get("final_delivery_interval")
    return bool(
        truth_owner.get("status") == "PASS"
        and isinstance(interval, Mapping)
        and interval.get("timeline") == "padded_source_local_ms"
        and interval.get("interval_semantics") == "half_open"
        and _strict_nonnegative_int(interval.get("start_ms"))
        and _strict_int(interval.get("end_ms"))
        and int(interval["end_ms"]) > int(interval["start_ms"])
        and all(
            _strict_nonnegative_int(truth_owner.get(key))
            and int(truth_owner[key]) == 0
            for key in (
                "required_truth_row_count",
                "context_only_truth_row_count",
                "optional_truth_row_count",
                "straddling_truth_row_count",
                "required_window_count",
            )
        )
        and all(
            truth_owner.get(key) == []
            for key in (
                "required_truth_ids",
                "context_only_truth_ids",
                "context_only_truth_evidence",
                "failures",
            )
        )
    )
