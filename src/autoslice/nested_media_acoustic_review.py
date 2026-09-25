"""Exact-batch convergence for nested-media acoustic attribution.

The visual attribution layer can align embedded captions to final-SRT cues, but
one visual observation is not speaker authority.  This module freezes a
hash-bound observation manifest and exact review windows before any acoustic
work, then consumes a partial or complete receipt set without authorizing a
subtitle mutation.  A downstream private-only projector may act only after a
complete batch and its own independent authority checks.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from src.autoslice.nested_media_caption_attribution import (
    ACOUSTIC_SCHEMA,
    OBSERVATION_SCHEMA,
    AttributionInputError,
    build_caption_source_attribution,
)
from src.autoslice.nested_media_observation_manifest import (
    ObservationManifestError,
    load_complete_observation_manifest,
)


PLAN_SCHEMA = "nested-media-acoustic-review-plan.v1"
BATCH_SCHEMA = "nested-media-acoustic-review-batch.v1"
PLAN_STATUS = "PENDING_ACOUSTIC_REVIEW"
COMPLETE_STATUS = "COMPLETE_CONSERVATIVE_ATTRIBUTION"


class NestedMediaAcousticReviewError(ValueError):
    """The exact acoustic-review plan or one of its receipts is invalid."""


def _canonical_sha256(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _require_mapping(value: object, *, code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise NestedMediaAcousticReviewError(code)
    return value


def _require_sequence(value: object, *, code: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise NestedMediaAcousticReviewError(code)
    return value


def _normalized_observation(
    row: Mapping[str, object], *, candidate_id: str
) -> dict[str, object]:
    return {
        "schema_version": OBSERVATION_SCHEMA,
        "observation_id": row["observation_id"],
        "candidate_id": candidate_id,
        "frame_ms": row["frame_ms"],
        "caption_present": row["caption_present"],
        "visible_text": row["visible_text"],
        "role": row["role"],
        "pipeline_burned_subtitle": False,
        "frame_path": row["frame_path"],
        "frame_sha256": row["frame_sha256"],
    }


def _review_interval(row: Mapping[str, object]) -> list[int]:
    cue_interval = row.get("cue_interval_ms")
    frame_ms = row.get("frame_ms")
    if not (
        isinstance(cue_interval, list)
        and len(cue_interval) == 2
        and all(type(value) is int for value in cue_interval)
        and type(frame_ms) is int
        and 0 <= cue_interval[0] < cue_interval[1]
        and frame_ms >= 0
    ):
        raise NestedMediaAcousticReviewError("OBSERVATION_CUE_ALIGNMENT_REQUIRED")
    return [min(cue_interval[0], frame_ms), max(cue_interval[1], frame_ms + 1)]


def _policy_values(policy: Mapping[str, object]) -> tuple[int, int, float, str]:
    required = {
        "lead_lag_ms",
        "max_span_cues",
        "fuzzy_threshold",
        "observation_list_key",
        "required_receipt_schema",
        "receipt_completeness",
        "receipt_interval",
        "drop_before_complete",
    }
    if set(policy) != required:
        raise NestedMediaAcousticReviewError("REVIEW_PLAN_POLICY_INVALID")
    lead_lag_ms = policy.get("lead_lag_ms")
    max_span_cues = policy.get("max_span_cues")
    fuzzy_threshold = policy.get("fuzzy_threshold")
    list_key = policy.get("observation_list_key")
    if not (
        type(lead_lag_ms) is int
        and lead_lag_ms >= 0
        and type(max_span_cues) is int
        and max_span_cues > 0
        and type(fuzzy_threshold) in (int, float)
        and 0 <= float(fuzzy_threshold) <= 1
        and isinstance(list_key, str)
        and list_key
        and policy.get("required_receipt_schema") == ACOUSTIC_SCHEMA
        and policy.get("receipt_completeness") == "EXACT_OBSERVATION_SET"
        and policy.get("receipt_interval") == "EXACT_PLANNED_INTERVAL"
        and policy.get("drop_before_complete") is False
    ):
        raise NestedMediaAcousticReviewError("REVIEW_PLAN_POLICY_INVALID")
    return lead_lag_ms, max_span_cues, float(fuzzy_threshold), list_key


def _plan_body(
    *,
    candidate_id: str,
    source_media_path: str | Path,
    source_media_sha256: str,
    final_srt_path: str | Path,
    final_srt_sha256: str,
    observation_manifest_path: str | Path,
    observation_manifest_sha256: str,
    observation_list_key: str,
    lead_lag_ms: int,
    max_span_cues: int,
    fuzzy_threshold: float,
) -> dict[str, object]:
    try:
        manifest, observations = load_complete_observation_manifest(
            observation_manifest_path,
            expected_sha256=observation_manifest_sha256,
            candidate_id=candidate_id,
            list_key=observation_list_key,
        )
    except ObservationManifestError as exc:
        raise NestedMediaAcousticReviewError(str(exc)) from exc
    try:
        visual = build_caption_source_attribution(
            candidate_id=candidate_id,
            source_media_path=source_media_path,
            source_media_sha256=source_media_sha256,
            final_srt_path=final_srt_path,
            final_srt_sha256=final_srt_sha256,
            observations=observations,
            acoustic_receipt_bindings=(),
            lead_lag_ms=lead_lag_ms,
            max_span_cues=max_span_cues,
            fuzzy_threshold=fuzzy_threshold,
        )
    except AttributionInputError as exc:
        raise NestedMediaAcousticReviewError(str(exc)) from exc
    rows = visual.get("rows")
    if not isinstance(rows, list) or not rows:
        raise NestedMediaAcousticReviewError("REVIEW_PLAN_REQUIRES_OBSERVATIONS")
    normalized: list[dict[str, object]] = []
    items: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in rows:
        row = _require_mapping(raw, code="ATTRIBUTION_ROW_INVALID")
        observation_id = row.get("observation_id")
        cue_indexes = row.get("cue_indexes")
        if not (
            isinstance(observation_id, str)
            and observation_id
            and observation_id not in seen
            and isinstance(cue_indexes, list)
        ):
            raise NestedMediaAcousticReviewError("ATTRIBUTION_ROW_INVALID")
        if not cue_indexes:
            raise NestedMediaAcousticReviewError("OBSERVATION_CUE_ALIGNMENT_REQUIRED")
        seen.add(observation_id)
        normalized.append(_normalized_observation(row, candidate_id=candidate_id))
        items.append(
            {
                "observation_id": observation_id,
                "frame_ms": row["frame_ms"],
                "frame_sha256": row["frame_sha256"],
                "cue_indexes": list(cue_indexes),
                "cue_interval_ms": list(row["cue_interval_ms"]),
                "review_interval_ms": _review_interval(row),
                "caption_present": row["caption_present"],
                "visual_match": row["visual_match"],
                "visual_similarity": row["similarity"],
            }
        )
    return {
        "schema_version": PLAN_SCHEMA,
        "status": PLAN_STATUS,
        "candidate_id": candidate_id,
        "source_media": {
            "path": visual["source_media_path"],
            "sha256": visual["source_media_sha256"],
        },
        "final_srt": {
            "path": visual["final_srt_path"],
            "sha256": visual["final_srt_sha256"],
        },
        "observation_manifest": manifest,
        "observations": normalized,
        "items": items,
        "visual_input_binding_sha256": visual["input_binding_sha256"],
        "policy": {
            "lead_lag_ms": lead_lag_ms,
            "max_span_cues": max_span_cues,
            "fuzzy_threshold": fuzzy_threshold,
            "observation_list_key": observation_list_key,
            "required_receipt_schema": ACOUSTIC_SCHEMA,
            "receipt_completeness": "EXACT_OBSERVATION_SET",
            "receipt_interval": "EXACT_PLANNED_INTERVAL",
            "drop_before_complete": False,
        },
        "mutation_authorized": False,
        "publication_authority": False,
        "provider_calls": 0,
    }


def build_acoustic_review_plan(
    *,
    candidate_id: str,
    source_media_path: str | Path,
    source_media_sha256: str,
    final_srt_path: str | Path,
    final_srt_sha256: str,
    observation_manifest_path: str | Path,
    observation_manifest_sha256: str,
    observation_list_key: str = "observations",
    lead_lag_ms: int = 1_800,
    max_span_cues: int = 3,
    fuzzy_threshold: float = 0.72,
) -> dict[str, object]:
    """Freeze one exact manifest-backed observation set without provider calls."""

    body = _plan_body(
        candidate_id=candidate_id,
        source_media_path=source_media_path,
        source_media_sha256=source_media_sha256,
        final_srt_path=final_srt_path,
        final_srt_sha256=final_srt_sha256,
        observation_manifest_path=observation_manifest_path,
        observation_manifest_sha256=observation_manifest_sha256,
        observation_list_key=observation_list_key,
        lead_lag_ms=lead_lag_ms,
        max_span_cues=max_span_cues,
        fuzzy_threshold=fuzzy_threshold,
    )
    return {**body, "plan_sha256": _canonical_sha256(body)}


def validate_acoustic_review_plan(plan: object) -> dict[str, object]:
    """Recompute every manifest/source/frame/SRT binding in a review plan."""

    value = _require_mapping(plan, code="REVIEW_PLAN_INVALID")
    body = dict(value)
    declared = body.pop("plan_sha256", None)
    if not isinstance(declared, str) or declared != _canonical_sha256(body):
        raise NestedMediaAcousticReviewError("REVIEW_PLAN_DIGEST_MISMATCH")
    if not (
        body.get("schema_version") == PLAN_SCHEMA
        and body.get("status") == PLAN_STATUS
        and body.get("mutation_authorized") is False
        and body.get("publication_authority") is False
        and body.get("provider_calls") == 0
    ):
        raise NestedMediaAcousticReviewError("REVIEW_PLAN_INVALID")
    source = _require_mapping(body.get("source_media"), code="REVIEW_PLAN_INVALID")
    final_srt = _require_mapping(body.get("final_srt"), code="REVIEW_PLAN_INVALID")
    manifest = _require_mapping(
        body.get("observation_manifest"), code="REVIEW_PLAN_INVALID"
    )
    policy = _require_mapping(body.get("policy"), code="REVIEW_PLAN_INVALID")
    lead_lag_ms, max_span_cues, fuzzy_threshold, list_key = _policy_values(policy)
    expected = build_acoustic_review_plan(
        candidate_id=str(body.get("candidate_id") or ""),
        source_media_path=str(source.get("path") or ""),
        source_media_sha256=str(source.get("sha256") or ""),
        final_srt_path=str(final_srt.get("path") or ""),
        final_srt_sha256=str(final_srt.get("sha256") or ""),
        observation_manifest_path=str(manifest.get("path") or ""),
        observation_manifest_sha256=str(manifest.get("sha256") or ""),
        observation_list_key=list_key,
        lead_lag_ms=lead_lag_ms,
        max_span_cues=max_span_cues,
        fuzzy_threshold=fuzzy_threshold,
    )
    if dict(value) != expected:
        raise NestedMediaAcousticReviewError("REVIEW_PLAN_RECOMPUTE_MISMATCH")
    return expected


def consume_acoustic_review_batch(
    *,
    plan: Mapping[str, object],
    acoustic_receipt_bindings: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Consume partial/complete receipts; never expose partial drop evidence."""

    frozen = validate_acoustic_review_plan(plan)
    source = _require_mapping(frozen["source_media"], code="REVIEW_PLAN_INVALID")
    final_srt = _require_mapping(frozen["final_srt"], code="REVIEW_PLAN_INVALID")
    policy = _require_mapping(frozen["policy"], code="REVIEW_PLAN_INVALID")
    lead_lag_ms, max_span_cues, fuzzy_threshold, _list_key = _policy_values(policy)
    observations = _require_sequence(
        frozen["observations"], code="REVIEW_PLAN_INVALID"
    )
    try:
        attribution = build_caption_source_attribution(
            candidate_id=str(frozen["candidate_id"]),
            source_media_path=str(source["path"]),
            source_media_sha256=str(source["sha256"]),
            final_srt_path=str(final_srt["path"]),
            final_srt_sha256=str(final_srt["sha256"]),
            observations=[dict(item) for item in observations if isinstance(item, Mapping)],
            acoustic_receipt_bindings=acoustic_receipt_bindings,
            lead_lag_ms=lead_lag_ms,
            max_span_cues=max_span_cues,
            fuzzy_threshold=fuzzy_threshold,
        )
    except AttributionInputError as exc:
        raise NestedMediaAcousticReviewError(str(exc)) from exc
    planned_items = {
        str(item["observation_id"]): item
        for item in _require_sequence(frozen["items"], code="REVIEW_PLAN_INVALID")
        if isinstance(item, Mapping)
    }
    received: list[str] = []
    missing: list[str] = []
    for raw in _require_sequence(attribution.get("rows"), code="ATTRIBUTION_INVALID"):
        row = _require_mapping(raw, code="ATTRIBUTION_INVALID")
        observation_id = str(row.get("observation_id") or "")
        planned = planned_items.get(observation_id)
        if planned is None:
            raise NestedMediaAcousticReviewError("ATTRIBUTION_OBSERVATION_NOT_PLANNED")
        receipt = row.get("acoustic_receipt")
        if receipt is None:
            missing.append(observation_id)
            continue
        receipt_map = _require_mapping(receipt, code="ACOUSTIC_RECEIPT_INVALID")
        if receipt_map.get("review_plan_sha256") != frozen["plan_sha256"]:
            raise NestedMediaAcousticReviewError("ACOUSTIC_REVIEW_PLAN_BINDING_MISMATCH")
        if receipt_map.get("interval_ms") != planned.get("review_interval_ms"):
            raise NestedMediaAcousticReviewError("ACOUSTIC_REVIEW_INTERVAL_MISMATCH")
        received.append(observation_id)
    complete = not missing and len(received) == len(planned_items)
    status = COMPLETE_STATUS if complete else PLAN_STATUS
    evidence_supported = (
        list(attribution.get("safe_to_drop_cue_indexes") or []) if complete else []
    )
    body: dict[str, object] = {
        "schema_version": BATCH_SCHEMA,
        "status": status,
        "complete": complete,
        "candidate_id": frozen["candidate_id"],
        "review_plan_sha256": frozen["plan_sha256"],
        "received_observation_ids": received,
        "missing_observation_ids": missing,
        "required_observation_count": len(planned_items),
        "received_observation_count": len(received),
        "diagnostic_attribution": attribution,
        "evidence_supported_source_only_cue_indexes": evidence_supported,
        "mutation_authorized": False,
        "publication_authority": False,
        "partial_drop_evidence_exposed": False,
    }
    return {**body, "batch_sha256": _canonical_sha256(body)}


def write_create_only(path: str | Path, value: Mapping[str, object]) -> Path:
    """Write one immutable JSON result without following path symlinks."""

    target = Path(path).expanduser().absolute()
    if any(part.is_symlink() for part in (target, *target.parents)):
        raise NestedMediaAcousticReviewError("CREATE_ONLY_OUTPUT_UNSAFE")
    target.parent.mkdir(parents=True, exist_ok=True)
    target = target.parent.resolve(strict=True) / target.name
    payload = (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError as exc:
        raise NestedMediaAcousticReviewError("CREATE_ONLY_OUTPUT_EXISTS") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return target.resolve(strict=True)
