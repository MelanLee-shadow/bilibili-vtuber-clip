"""Hash-verified frozen boundary receipts for redelivery replay.

The publication record is an optional frozen decision authority for an exact
redelivery lane, and an ordinary cache when the current request is identical.
Both semantic layers must already be PASS and every stable
endpoint/input/publication binding must still describe this run.  Any
malformed or stale surface returns ``None`` so the caller performs the
ordinary live review.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from src.autoslice.jingting_chunker import parse_srt_cues


REFERENCE_SCHEMA_VERSION = "talk-boundary-frozen-receipt-ref.v1"
REPLAY_AUDIT_SCHEMA_VERSION = "talk-boundary-frozen-receipt-replay.v1"
_REVIEW_SCHEMA_VERSION = "talk-boundary-semantic-review.v1"
_ENDPOINT_SCHEMA_VERSION = "talk-boundary-final-endpoint-binding.v1"
_SHA256_RX = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class FrozenBoundaryReview:
    review: dict[str, object]
    record_path: str
    record_sha256: str
    json_path: str
    exact_interval_projection: bool = False
    selection_hook_sha256: str | None = None


@dataclass(frozen=True)
class FrozenBoundaryReceipt:
    source_full_window: FrozenBoundaryReview
    final_delivery: FrozenBoundaryReview
    final_subtitle_sha256: str


def _canonical_sha256(value: Mapping[str, object]) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _normalized_sha256(value: object) -> str | None:
    text = str(value or "").strip()
    if re.fullmatch(r"[0-9a-f]{64}", text):
        text = "sha256:" + text
    return text if _SHA256_RX.fullmatch(text) else None


def _file_sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _text_sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _baseline_binding(config: Mapping[str, object]) -> dict[str, object] | None:
    schema_version = config.get("schema_version")
    if schema_version not in {
        "subtitle-redelivery-baseline.v1",
        "subtitle-redelivery-baseline.v2",
    }:
        return None
    baseline_sha256 = _normalized_sha256(config.get("sha256"))
    if baseline_sha256 is None:
        return None
    binding: dict[str, object] = {
        "schema_version": schema_version,
        "sha256": baseline_sha256,
    }
    if schema_version == "subtitle-redelivery-baseline.v2":
        exact_replay = config.get("exact_interval_replay", False)
        source_sha256 = _normalized_sha256(config.get("source_sha256"))
        source_basename = str(
            config.get("source_recording_basename") or ""
        ).strip()
        source_start = config.get("absolute_source_start_ms")
        source_end = config.get("absolute_source_end_ms")
        if (
            not isinstance(exact_replay, bool)
            or source_sha256 is None
            or not source_basename
            or Path(source_basename).name != source_basename
            or isinstance(source_start, bool)
            or not isinstance(source_start, int)
            or isinstance(source_end, bool)
            or not isinstance(source_end, int)
            or source_start < 0
            or source_end <= source_start
        ):
            return None
        binding.update(
            exact_interval_replay=exact_replay,
            source_recording_basename=source_basename,
            source_sha256=source_sha256,
            absolute_source_start_ms=source_start,
            absolute_source_end_ms=source_end,
        )
    return binding


def redelivery_baseline_boundary_binding(
    config: Mapping[str, object],
) -> dict[str, object] | None:
    """Public deterministic projection used by the WSL spec writer."""

    return _baseline_binding(config)


def _endpoint_binding(review: Mapping[str, object]) -> dict[str, object] | None:
    endpoint = review.get("final_endpoint_binding")
    scope = review.get("boundary_search_scope")
    if (
        not isinstance(endpoint, Mapping)
        or endpoint.get("schema_version") != _ENDPOINT_SCHEMA_VERSION
        or endpoint.get("status") != "PASS"
        or endpoint.get("semantic_request_sha256")
        != review.get("request_sha256")
        or endpoint.get("semantic_cue_grid_sha256")
        != review.get("cue_grid_sha256")
        or endpoint.get("final_cue_grid_sha256")
        != review.get("cue_grid_sha256")
        or not isinstance(scope, Mapping)
        or _normalized_sha256(scope.get("scope_sha256")) is None
    ):
        return None
    integer_fields = (
        "recommended_end_cue_index",
        "recommended_end_ms",
        "final_closure_cue_index",
        "final_snapped_end_ms",
        "final_start_ms",
        "final_end_ms",
    )
    if any(
        isinstance(endpoint.get(field), bool)
        or not isinstance(endpoint.get(field), int)
        for field in integer_fields
    ):
        return None
    if (
        endpoint.get("recommended_end_cue_index")
        != review.get("recommended_end_cue_index")
        or endpoint.get("recommended_end_ms")
        != review.get("recommended_end_ms")
        or endpoint.get("recommended_end_cue_index")
        != endpoint.get("final_closure_cue_index")
        or endpoint.get("reason_codes") != []
        or _normalized_sha256(endpoint.get("closure_text_sha256")) is None
    ):
        return None
    return {
        "request_sha256": review.get("request_sha256"),
        "cue_grid_sha256": review.get("cue_grid_sha256"),
        "boundary_search_scope_sha256": scope.get("scope_sha256"),
        "final_start_ms": endpoint.get("final_start_ms"),
        "final_end_ms": endpoint.get("final_end_ms"),
    }


def _valid_review(
    review: object, *, candidate_id: str, review_scope: str
) -> dict[str, object] | None:
    if not isinstance(review, Mapping):
        return None
    copied = dict(review)
    if (
        copied.get("schema_version") != _REVIEW_SCHEMA_VERSION
        or copied.get("status") != "PASS"
        or copied.get("review_scope") != review_scope
        or copied.get("candidate_id") != candidate_id
        or _normalized_sha256(copied.get("request_sha256")) is None
        or _normalized_sha256(copied.get("cue_grid_sha256")) is None
        or _endpoint_binding(copied) is None
    ):
        return None
    for field in (
        "syntax_complete",
        "story_closed",
        "next_topic_separated",
        "content_anchor_covered",
        "next_topic_witness_valid",
        "same_topic_continues_after_target",
        "needs_more_context",
    ):
        if not isinstance(copied.get(field), bool):
            return None
    if (
        copied.get("syntax_complete") is not True
        or copied.get("story_closed") is not True
        or copied.get("next_topic_separated") is not True
        or copied.get("content_anchor_covered") is not True
        or copied.get("next_topic_witness_valid") is not True
        or isinstance(copied.get("recommended_end_cue_index"), bool)
        or not isinstance(copied.get("recommended_end_cue_index"), int)
        or isinstance(copied.get("recommended_end_ms"), bool)
        or not isinstance(copied.get("recommended_end_ms"), int)
        or not isinstance(copied.get("evidence_cue_indexes"), list)
        or not copied.get("evidence_cue_indexes")
        or any(
            isinstance(index, bool) or not isinstance(index, int)
            for index in copied["evidence_cue_indexes"]
        )
        or not isinstance(copied.get("reason_codes"), list)
        or not all(
            isinstance(reason, str) for reason in copied["reason_codes"]
        )
        or not isinstance(copied.get("summary"), str)
    ):
        return None
    return copied


def _source_witness_matches(
    final_review: Mapping[str, object], source_review: Mapping[str, object]
) -> bool:
    witness = final_review.get("source_separation_witness")
    endpoint = source_review.get("final_endpoint_binding")
    return bool(
        isinstance(witness, Mapping)
        and isinstance(endpoint, Mapping)
        and witness.get("status") == "PASS"
        and witness.get("reason_codes") == []
        and witness.get("source_review_sha256")
        == _canonical_sha256(source_review)
        and witness.get("source_request_sha256")
        == source_review.get("request_sha256")
        and witness.get("source_cue_grid_sha256")
        == source_review.get("cue_grid_sha256")
        and witness.get("source_recommended_end_ms")
        == source_review.get("recommended_end_ms")
        and witness.get("source_final_start_ms")
        == endpoint.get("final_start_ms")
        and witness.get("source_final_end_ms")
        == endpoint.get("final_end_ms")
    )


def _baseline_cue_evidence(
    baseline: Mapping[str, object],
) -> tuple[str, str, int, str] | None:
    raw_path = str(baseline.get("path") or "").strip()
    expected_sha256 = _normalized_sha256(baseline.get("sha256"))
    if not raw_path or expected_sha256 is None:
        return None
    path = Path(raw_path)
    try:
        if path.is_symlink() or not path.is_file():
            return None
        payload = path.read_bytes()
        text = payload.decode("utf-8", errors="strict")
    except (OSError, UnicodeError):
        return None
    if _file_sha256(payload) != expected_sha256:
        return None
    cues = [cue for cue in parse_srt_cues(text) if cue.text.strip()]
    if not cues:
        return None
    rows = [
        {
            "cue_index": index,
            "start_ms": int(cue.start_ms),
            "end_ms": int(cue.end_ms),
            "text": str(cue.text).strip(),
        }
        for index, cue in enumerate(cues, start=1)
    ]
    grid_sha256 = _canonical_sha256(
        {
            "schema_version": "talk-boundary-cue-grid.v1",
            "cues": rows,
        }
    )
    transcript_sha256 = _text_sha256("\n".join(row["text"] for row in rows))
    last = rows[-1]
    return (
        grid_sha256,
        transcript_sha256,
        int(last["end_ms"]),
        _text_sha256(str(last["text"])),
    )


def _exact_interval_projection_matches(
    *,
    spec: Mapping[str, object],
    baseline: Mapping[str, object],
    document: Mapping[str, object],
    source: Mapping[str, object],
    final: Mapping[str, object],
    final_subtitle_sha256: str,
) -> bool:
    """Bind a frozen verdict to the exact replay authority, not fresh ASR."""

    binding = _baseline_binding(baseline)
    pieces = spec.get("pieces")
    story = document.get("story_contract")
    source_endpoint = source.get("final_endpoint_binding")
    final_endpoint = final.get("final_endpoint_binding")
    source_scope = source.get("boundary_search_scope")
    baseline_audit = document.get("redelivery_baseline")
    cue_evidence = _baseline_cue_evidence(baseline)
    if not (
        isinstance(binding, Mapping)
        and binding.get("schema_version") == "subtitle-redelivery-baseline.v2"
        and binding.get("exact_interval_replay") is True
        and isinstance(pieces, list)
        and len(pieces) == 1
        and isinstance(pieces[0], Mapping)
        and isinstance(story, Mapping)
        and isinstance(source_endpoint, Mapping)
        and isinstance(final_endpoint, Mapping)
        and isinstance(source_scope, Mapping)
        and isinstance(baseline_audit, Mapping)
        and cue_evidence is not None
    ):
        return False
    piece = pieces[0]
    source_sha256 = binding["source_sha256"]
    source_basename = binding["source_recording_basename"]
    absolute_start = binding["absolute_source_start_ms"]
    absolute_end = binding["absolute_source_end_ms"]
    interval = baseline_audit.get("current_source_interval")
    hook = str(spec.get("selection_hook") or "")
    source_fact = story.get("source_fact_review")
    allowed_hooks = {str(story.get("selection_hook") or "")}
    if isinstance(source_fact, Mapping):
        allowed_hooks.add(str(source_fact.get("original_selection_hook") or ""))
    grid_sha256, transcript_sha256, closure_end_ms, closure_text_sha256 = (
        cue_evidence
    )
    try:
        piece_start = int(piece["start_ms"])
        frozen_source_start = piece_start + int(source_endpoint["final_start_ms"])
        frozen_source_end = piece_start + int(source_endpoint["final_end_ms"])
        frozen_closure_end = piece_start + int(source["recommended_end_ms"])
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        _normalized_sha256(piece.get("source_media_sha256")) == source_sha256
        and Path(str(piece.get("remote_media") or "")).name == source_basename
        and source_scope.get("last_piece_start_ms") == piece_start
        and frozen_source_start == absolute_start
        and frozen_source_end == absolute_end
        and frozen_closure_end == absolute_start + closure_end_ms
        and source_endpoint.get("closure_text_sha256") == closure_text_sha256
        and final_endpoint.get("closure_text_sha256") == closure_text_sha256
        and final_endpoint.get("final_start_ms") == 0
        and final_endpoint.get("final_end_ms") == absolute_end - absolute_start
        and final.get("recommended_end_ms") == closure_end_ms
        and final.get("cue_grid_sha256") == grid_sha256
        and final_subtitle_sha256 == binding.get("sha256")
        and story.get("candidate_id") == source.get("candidate_id")
        and story.get("source_media_sha256s") == [source_sha256]
        and story.get("transcript_sha256") == transcript_sha256
        and document.get("selection_scorecard") == spec.get("selection_scorecard")
        and hook in allowed_hooks
        and isinstance(interval, Mapping)
        and interval.get("absolute_source_start_ms") == absolute_start
        and interval.get("absolute_source_end_ms") == absolute_end
        and baseline_audit.get("application_strategy")
        == "exact_reviewed_interval_replay"
    )


def _receipt_reference(spec: Mapping[str, object]) -> tuple[
    Mapping[str, object], Mapping[str, object]
] | None:
    baseline = spec.get("subtitle_redelivery_baseline")
    if not isinstance(baseline, Mapping):
        return None
    nested = baseline.get("frozen_boundary_receipt")
    top_level = spec.get("frozen_boundary_receipt")
    if nested is not None and top_level is not None and nested != top_level:
        return None
    reference = nested if nested is not None else top_level
    if not isinstance(reference, Mapping):
        return None
    return baseline, reference


def load_frozen_boundary_receipt(
    spec: Mapping[str, object], *, candidate_id: str
) -> FrozenBoundaryReceipt | None:
    """Load a pristine record only for an explicitly bound redelivery spec."""

    located = _receipt_reference(spec)
    if located is None:
        return None
    baseline, reference = located
    baseline_binding = _baseline_binding(baseline)
    if (
        reference.get("schema_version") != REFERENCE_SCHEMA_VERSION
        or reference.get("candidate_id") != candidate_id
        or baseline_binding is None
        or reference.get("redelivery_baseline") != baseline_binding
    ):
        return None
    expected_record_sha256 = _normalized_sha256(reference.get("sha256"))
    raw_path = str(reference.get("path") or "").strip()
    if expected_record_sha256 is None or not raw_path:
        return None
    record_path = Path(raw_path)
    try:
        if record_path.is_symlink() or not record_path.is_file():
            return None
        payload = record_path.read_bytes()
    except OSError:
        return None
    if _file_sha256(payload) != expected_record_sha256:
        return None
    try:
        document = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, Mapping):
        return None
    boundary_audit = document.get("boundary_audit")
    if not isinstance(boundary_audit, Mapping):
        return None
    source = _valid_review(
        boundary_audit.get("boundary_semantic_review"),
        candidate_id=candidate_id,
        review_scope="source_full_window",
    )
    final = _valid_review(
        boundary_audit.get("final_delivery_boundary_semantic_review"),
        candidate_id=candidate_id,
        review_scope="final_delivery",
    )
    if source is None or final is None or not _source_witness_matches(final, source):
        return None
    source_binding = _endpoint_binding(source)
    final_binding = _endpoint_binding(final)
    artifact_hashes = document.get("artifact_hashes")
    final_subtitle_sha256 = (
        _normalized_sha256(artifact_hashes.get("subtitle_sha256"))
        if isinstance(artifact_hashes, Mapping)
        else None
    )
    expected_source = reference.get("source_full_window")
    expected_final = reference.get("final_delivery")
    if (
        source_binding is None
        or final_binding is None
        or final_subtitle_sha256 is None
        or expected_source != source_binding
        or expected_final
        != {**final_binding, "subtitle_sha256": final_subtitle_sha256}
    ):
        return None
    resolved_path = str(record_path.resolve())
    exact_interval_projection = _exact_interval_projection_matches(
        spec=spec,
        baseline=baseline,
        document=document,
        source=source,
        final=final,
        final_subtitle_sha256=final_subtitle_sha256,
    )
    if (
        baseline_binding.get("schema_version")
        == "subtitle-redelivery-baseline.v2"
        and baseline_binding.get("exact_interval_replay") is True
        and not exact_interval_projection
    ):
        return None
    selection_hook_sha256 = _text_sha256(
        str(spec.get("selection_hook") or "")
    )
    return FrozenBoundaryReceipt(
        source_full_window=FrozenBoundaryReview(
            source,
            resolved_path,
            expected_record_sha256,
            "boundary_audit.boundary_semantic_review",
            exact_interval_projection,
            selection_hook_sha256,
        ),
        final_delivery=FrozenBoundaryReview(
            final,
            resolved_path,
            expected_record_sha256,
            "boundary_audit.final_delivery_boundary_semantic_review",
            exact_interval_projection,
            selection_hook_sha256,
        ),
        final_subtitle_sha256=final_subtitle_sha256,
    )


def matching_final_delivery_review(
    receipt: FrozenBoundaryReceipt | None, final_srt_text: str
) -> FrozenBoundaryReview | None:
    if receipt is None:
        return None
    current_sha256 = _file_sha256(final_srt_text.encode("utf-8"))
    return (
        receipt.final_delivery
        if current_sha256 == receipt.final_subtitle_sha256
        else None
    )


def _effective_end_ms(
    row: Mapping[str, object], relaxations: object
) -> int | None:
    cue_index = row.get("cue_index")
    end_ms = row.get("end_ms")
    if (
        isinstance(cue_index, bool)
        or not isinstance(cue_index, int)
        or isinstance(end_ms, bool)
        or not isinstance(end_ms, int)
        or not isinstance(relaxations, list)
    ):
        return None
    matching = [
        item
        for item in relaxations
        if isinstance(item, Mapping) and item.get("cue_index") == cue_index
    ]
    if len(matching) > 1:
        return None
    if not matching:
        return end_ms
    relaxation = matching[0]
    kind = relaxation.get("kind")
    if kind == "pin_crossing_closure_cue":
        pin_ms = relaxation.get("pin_ms")
        return (
            pin_ms
            if isinstance(pin_ms, int) and not isinstance(pin_ms, bool)
            else None
        )
    if kind == "silent_gap_closure_cue":
        return end_ms
    return None


def _projected_payload_overrides(
    review: Mapping[str, object],
    *,
    request: Mapping[str, object],
    cue_grid_sha256: str,
    expected_scope: str,
    selection_hook_sha256: str | None,
) -> tuple[dict[str, object], dict[str, object]] | None:
    """Uniquely rebind an exact-interval verdict to current cue ordinals."""

    rows = request.get("cues")
    recommendation_indexes = request.get("recommendation_cue_indexes")
    evidence_indexes = review.get("evidence_cue_indexes")
    recommended_index = review.get("recommended_end_cue_index")
    endpoint = review.get("final_endpoint_binding")
    if not (
        review.get("status") == "PASS"
        and review.get("review_scope") == expected_scope
        and review.get("candidate_id") == request.get("candidate_id")
        and review.get("target_ms") == request.get("target_ms")
        and review.get("max_forward_ms") == request.get("max_forward_ms")
        and review.get("boundary_search_scope")
        == request.get("boundary_search_scope")
        and review.get("selector_story_witness")
        == request.get("selector_story_witness")
        and isinstance(rows, list)
        and isinstance(recommendation_indexes, list)
        and isinstance(evidence_indexes, list)
        and isinstance(recommended_index, int)
        and not isinstance(recommended_index, bool)
        and isinstance(endpoint, Mapping)
        and _text_sha256(str(request.get("selection_hook") or ""))
        == selection_hook_sha256
    ):
        return None
    by_index = {
        row.get("cue_index"): row
        for row in rows
        if isinstance(row, Mapping)
    }
    if any(
        isinstance(index, bool) or not isinstance(index, int)
        for index in recommendation_indexes
    ):
        return None
    current_relaxations = request.get("recommendation_relaxations")
    candidates = [
        row
        for index in recommendation_indexes
        if isinstance((row := by_index.get(index)), Mapping)
        and _text_sha256(str(row.get("text") or ""))
        == endpoint.get("closure_text_sha256")
        and _effective_end_ms(row, current_relaxations)
        == review.get("recommended_end_ms")
    ]
    if len(candidates) != 1:
        return None
    recommended = candidates[0]
    current_recommended_index = recommended.get("cue_index")
    if isinstance(current_recommended_index, bool) or not isinstance(
        current_recommended_index, int
    ):
        return None
    ordered_indexes = [
        row.get("cue_index") for row in rows if isinstance(row, Mapping)
    ]
    try:
        recommended_position = ordered_indexes.index(current_recommended_index)
    except ValueError:
        return None
    terminal_witness = request.get("terminal_source_separation_witness")
    if expected_scope == "source_full_window":
        post_closure = ordered_indexes[recommended_position + 1 :]
        if not post_closure or not isinstance(post_closure[0], int):
            return None
        current_evidence = [current_recommended_index, post_closure[0]]
    else:
        frozen_witness = review.get("source_separation_witness")
        if not isinstance(terminal_witness, Mapping) or not isinstance(
            frozen_witness, Mapping
        ):
            return None
        stable_witness_fields = (
            "status",
            "reason_codes",
            "source_recommended_end_ms",
            "source_final_start_ms",
            "source_final_end_ms",
        )
        if not (
            cue_grid_sha256 == review.get("cue_grid_sha256")
            and recommended_position == len(ordered_indexes) - 1
            and all(index in by_index for index in evidence_indexes)
            and all(
                terminal_witness.get(field) == frozen_witness.get(field)
                for field in stable_witness_fields
            )
        ):
            return None
        current_evidence = list(evidence_indexes)
    return (
        {
            "recommended_end_cue_index": current_recommended_index,
            "evidence_cue_indexes": current_evidence,
        },
        {
            "frozen_recommended_end_cue_index": recommended_index,
            "current_recommended_end_cue_index": current_recommended_index,
            "frozen_evidence_cue_indexes": list(evidence_indexes),
            "current_evidence_cue_indexes": current_evidence,
            "closure_text_sha256": endpoint.get("closure_text_sha256"),
        },
    )


def frozen_review_payload(
    frozen: FrozenBoundaryReview | None,
    *,
    request: Mapping[str, object],
    cue_grid_sha256: str,
) -> tuple[dict[str, object], dict[str, object]] | None:
    """Return a frozen verdict for an identical or exact-replay input."""

    if frozen is None:
        return None
    review = frozen.review
    current_request_sha256 = _canonical_sha256(request)
    expected_scope = (
        "final_delivery"
        if request.get("terminal_source_separation_witness") is not None
        else "source_full_window"
    )
    strict_match = bool(
        review.get("status") == "PASS"
        and review.get("review_scope") == expected_scope
        and review.get("candidate_id") == request.get("candidate_id")
        and review.get("request_sha256") == current_request_sha256
        and review.get("cue_grid_sha256") == cue_grid_sha256
        and review.get("target_ms") == request.get("target_ms")
        and review.get("boundary_search_scope")
        == request.get("boundary_search_scope")
    )
    projection = (
        _projected_payload_overrides(
            review,
            request=request,
            cue_grid_sha256=cue_grid_sha256,
            expected_scope=expected_scope,
            selection_hook_sha256=frozen.selection_hook_sha256,
        )
        if frozen.exact_interval_projection and not strict_match
        else None
    )
    if not strict_match and projection is None:
        return None
    payload = {
        field: review.get(field)
        for field in (
            "syntax_complete",
            "story_closed",
            "next_topic_separated",
            "content_anchor_covered",
            "recommended_end_cue_index",
            "evidence_cue_indexes",
            "same_topic_continues_after_target",
            "needs_more_context",
            "reason_codes",
            "summary",
        )
    }
    if projection is not None:
        payload.update(projection[0])
    replay_audit = {
        "schema_version": REPLAY_AUDIT_SCHEMA_VERSION,
        "status": "CARRIED",
        "decision_authority": "FROZEN_HASH_BOUND_BOUNDARY_RECEIPT",
        "replay_mode": (
            "EXACT_REQUEST_REPLAY"
            if strict_match
            else "EXACT_INTERVAL_FROZEN_VERDICT_PROJECTION"
        ),
        "review_scope": expected_scope,
        "frozen_verdict": review.get("status"),
        "record_path": frozen.record_path,
        "record_sha256": frozen.record_sha256,
        "record_json_path": frozen.json_path,
        "frozen_request_sha256": review.get("request_sha256"),
        "frozen_cue_grid_sha256": review.get("cue_grid_sha256"),
        "frozen_review_sha256": _canonical_sha256(review),
        "current_request_sha256": current_request_sha256,
        "current_cue_grid_sha256": cue_grid_sha256,
        "llm_call_skipped": True,
        "reason_code": "FROZEN_BOUNDARY_RECEIPT_REPLAYED",
    }
    if projection is not None:
        replay_audit.update(projection[1])
    return payload, replay_audit
