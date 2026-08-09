"""Hash-bound source-only boundary authority for exact redelivery.

This lane carries only a previously reviewed source-window verdict.  It never
contains or exposes a final-delivery verdict: the post-correction subtitle must
still pass the ordinary fresh final semantic reviewer.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from src.autoslice.boundary_semantic_review import boundary_search_scope_is_valid
from src.autoslice.frozen_boundary_receipt import (
    FrozenBoundaryReview,
    FrozenBoundaryReceipt,
    _baseline_binding,
    _baseline_cue_evidence,
    _canonical_sha256,
    _endpoint_binding,
    _file_sha256,
    _normalized_sha256,
    _text_sha256,
    _valid_review,
    load_frozen_boundary_receipt,
)


REFERENCE_SCHEMA_VERSION = "talk-boundary-frozen-source-receipt-ref.v1"
SOURCE_PROJECTION_REPLAY_MODE = (
    "EXACT_INTERVAL_FROZEN_SOURCE_VERDICT_PROJECTION"
)
SOURCE_EXACT_REPLAY_MODE = "EXACT_REQUEST_FROZEN_SOURCE_VERDICT_REPLAY"
SOURCE_DECISION_AUTHORITY = "FROZEN_HASH_BOUND_SOURCE_BOUNDARY_RECEIPT"
SOURCE_REPLAY_REASON_CODE = "FROZEN_SOURCE_BOUNDARY_RECEIPT_REPLAYED"


@dataclass(frozen=True)
class FrozenSourceBoundaryReceipt:
    """A validated source-window verdict with no final-delivery authority."""

    source_full_window: FrozenBoundaryReview


def normalized_generation_inputs(
    spec: Mapping[str, object],
) -> dict[str, object] | None:
    """Project only immutable selection/boundary inputs from a producer spec."""

    baseline = spec.get("subtitle_redelivery_baseline")
    pieces = spec.get("pieces")
    binding = _baseline_binding(baseline) if isinstance(baseline, Mapping) else None
    if (
        binding is None
        or binding.get("schema_version") != "subtitle-redelivery-baseline.v2"
        or binding.get("exact_interval_replay") is not True
        or not isinstance(pieces, list)
        or len(pieces) != 1
        or not isinstance(pieces[0], Mapping)
    ):
        return None
    piece = pieces[0]
    piece_source_sha256 = _normalized_sha256(
        piece.get("source_media_sha256")
    )
    baseline_source_sha256 = _normalized_sha256(binding.get("source_sha256"))
    # Frozen attempt snapshots created before source-media hashes were copied
    # onto each piece remain usable only because the v2 baseline already binds
    # that exact source.  A present-but-different piece hash is never repaired.
    source_sha256 = piece_source_sha256 or baseline_source_sha256
    integer_fields = (
        "lead_pad_ms",
        "semantic_start_ms",
        "semantic_end_ms",
        "boundary_repair_extend_cap_ms",
        "semantic_tail_trim_cap_ms",
    )
    piece_integer_fields = ("start_ms", "end_ms")
    given_end_ms = spec.get("given_end_ms")
    given_end_mode = spec.get("given_end_mode")
    given_end_authority = spec.get("given_end_authority")
    if (
        source_sha256 is None
        or (
            piece.get("source_media_sha256") is not None
            and piece_source_sha256 != baseline_source_sha256
        )
        or any(
            isinstance(spec.get(field), bool)
            or not isinstance(spec.get(field), int)
            for field in integer_fields
        )
        or any(
            isinstance(piece.get(field), bool)
            or not isinstance(piece.get(field), int)
            for field in piece_integer_fields
        )
        or int(piece["start_ms"]) < 0
        or int(piece["end_ms"]) <= int(piece["start_ms"])
        or (
            given_end_ms is not None
            and (
                isinstance(given_end_ms, bool)
                or not isinstance(given_end_ms, int)
            )
        )
        or (given_end_mode is not None and not isinstance(given_end_mode, str))
        or (
            given_end_authority is not None
            and not isinstance(given_end_authority, str)
        )
    ):
        return None
    selection_hook = str(spec.get("selection_hook") or "").strip()
    scorecard = spec.get("selection_scorecard")
    remote_basename = Path(str(piece.get("remote_media") or "")).name
    if not selection_hook or not isinstance(scorecard, Mapping) or not remote_basename:
        return None
    return {
        "candidate_id": str(spec.get("candidate_id") or ""),
        "selection_hook": selection_hook,
        "selection_scorecard": dict(scorecard),
        "given_end_ms": given_end_ms,
        "given_end_mode": given_end_mode,
        "given_end_authority": given_end_authority,
        **{field: int(spec[field]) for field in integer_fields},
        "pieces": [
            {
                "remote_media_basename": remote_basename,
                "source_media_sha256": source_sha256,
                "start_ms": int(piece["start_ms"]),
                "end_ms": int(piece["end_ms"]),
                "piece_role": str(piece.get("piece_role") or "primary"),
            }
        ],
        "redelivery_baseline": binding,
    }


def _load_json_file(
    reference: Mapping[str, object], *, path_label: str
) -> tuple[Path, Mapping[str, object], str] | None:
    raw_path = str(reference.get("path") or "").strip()
    expected_sha256 = _normalized_sha256(reference.get("sha256"))
    if not raw_path or expected_sha256 is None:
        return None
    path = Path(raw_path)
    try:
        if path.is_symlink() or not path.is_file():
            return None
        payload = path.read_bytes()
        if _file_sha256(payload) != expected_sha256:
            return None
        document = json.loads(payload.decode("utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, Mapping):
        return None
    return path.resolve(), document, expected_sha256


def _source_geometry_matches(
    *,
    spec: Mapping[str, object],
    baseline: Mapping[str, object],
    review: Mapping[str, object],
) -> bool:
    binding = _baseline_binding(baseline)
    cue_evidence = _baseline_cue_evidence(baseline)
    pieces = spec.get("pieces")
    endpoint = review.get("final_endpoint_binding")
    scope = review.get("boundary_search_scope")
    if not (
        isinstance(binding, Mapping)
        and binding.get("schema_version") == "subtitle-redelivery-baseline.v2"
        and binding.get("exact_interval_replay") is True
        and cue_evidence is not None
        and isinstance(pieces, list)
        and len(pieces) == 1
        and isinstance(pieces[0], Mapping)
        and isinstance(endpoint, Mapping)
        and isinstance(scope, Mapping)
        and boundary_search_scope_is_valid(scope)
    ):
        return False
    piece = pieces[0]
    _grid_sha256, _transcript_sha256, closure_end_ms, closure_text_sha256 = (
        cue_evidence
    )
    try:
        piece_start = int(piece["start_ms"])
        source_start = piece_start + int(endpoint["final_start_ms"])
        source_end = piece_start + int(endpoint["final_end_ms"])
        closure_end = piece_start + int(review["recommended_end_ms"])
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        _normalized_sha256(piece.get("source_media_sha256"))
        == binding.get("source_sha256")
        and Path(str(piece.get("remote_media") or "")).name
        == binding.get("source_recording_basename")
        and source_start == binding.get("absolute_source_start_ms")
        and source_end == binding.get("absolute_source_end_ms")
        and closure_end
        == int(binding["absolute_source_start_ms"]) + closure_end_ms
        and endpoint.get("closure_text_sha256") == closure_text_sha256
        and scope.get("last_piece_start_ms") == piece_start
    )


def load_frozen_source_boundary_receipt(
    spec: Mapping[str, object], *, candidate_id: str
) -> FrozenSourceBoundaryReceipt | None:
    """Load a source-only verdict; malformed authority falls back to fresh review."""

    baseline = spec.get("subtitle_redelivery_baseline")
    if not isinstance(baseline, Mapping):
        return None
    reference = baseline.get("frozen_source_boundary_receipt")
    if reference is None:
        return None
    if (
        not isinstance(reference, Mapping)
        or baseline.get("frozen_boundary_receipt") is not None
        or spec.get("frozen_boundary_receipt") is not None
        or spec.get("frozen_source_boundary_receipt") is not None
    ):
        return None
    expected_keys = {
        "schema_version",
        "candidate_id",
        "path",
        "sha256",
        "json_path",
        "source_review_sha256",
        "redelivery_baseline",
        "source_full_window",
        "generation_spec",
        "final_delivery_policy",
    }
    if (
        set(reference) != expected_keys
        or reference.get("schema_version") != REFERENCE_SCHEMA_VERSION
        or reference.get("candidate_id") != candidate_id
        or reference.get("json_path") != "boundary_semantic_review"
        or reference.get("final_delivery_policy") != "FRESH_REQUIRED"
    ):
        return None
    binding = _baseline_binding(baseline)
    if binding is None or reference.get("redelivery_baseline") != binding:
        return None
    loaded = _load_json_file(reference, path_label="source boundary audit")
    generation_reference = reference.get("generation_spec")
    if loaded is None or not isinstance(generation_reference, Mapping):
        return None
    audit_path, audit, audit_sha256 = loaded
    generation_loaded = _load_json_file(
        generation_reference, path_label="source generation spec"
    )
    if generation_loaded is None:
        return None
    _generation_path, generation_spec, _generation_sha256 = generation_loaded
    expected_generation_keys = {"path", "sha256", "normalized_inputs"}
    current_inputs = normalized_generation_inputs(spec)
    frozen_inputs = normalized_generation_inputs(generation_spec)
    if (
        set(generation_reference) != expected_generation_keys
        or current_inputs is None
        or frozen_inputs is None
        or generation_reference.get("normalized_inputs") != frozen_inputs
        or frozen_inputs != current_inputs
        or current_inputs.get("candidate_id") != candidate_id
    ):
        return None
    review = _valid_review(
        audit.get("boundary_semantic_review"),
        candidate_id=candidate_id,
        review_scope="source_full_window",
    )
    review_sha256 = (
        _canonical_sha256(review) if isinstance(review, Mapping) else None
    )
    if (
        review is None
        or reference.get("source_review_sha256") != review_sha256
        or reference.get("source_full_window") != _endpoint_binding(review)
        or not _source_geometry_matches(
            spec=spec, baseline=baseline, review=review
        )
    ):
        return None
    return FrozenSourceBoundaryReceipt(
        source_full_window=FrozenBoundaryReview(
            review=dict(review),
            record_path=str(audit_path),
            record_sha256=audit_sha256,
            json_path="boundary_semantic_review",
            exact_interval_projection=True,
            selection_hook_sha256=_text_sha256(
                str(spec.get("selection_hook") or "")
            ),
            projection_replay_mode=SOURCE_PROJECTION_REPLAY_MODE,
            exact_request_replay_mode=SOURCE_EXACT_REPLAY_MODE,
            decision_authority=SOURCE_DECISION_AUTHORITY,
            replay_reason_code=SOURCE_REPLAY_REASON_CODE,
            authority_kind="SOURCE_ONLY",
            final_delivery_policy="FRESH_REQUIRED",
        )
    )


def load_boundary_review_authorities(
    spec: Mapping[str, object],
    *,
    candidate_id: str,
    current_owner_contract: object = None,
) -> tuple[FrozenBoundaryReceipt | None, FrozenBoundaryReview | None]:
    """Resolve type-separated source and final authorities at one choke point."""

    baseline = spec.get("subtitle_redelivery_baseline")
    baseline_source = (
        baseline.get("frozen_source_boundary_receipt")
        if isinstance(baseline, Mapping)
        else None
    )
    baseline_full = (
        baseline.get("frozen_boundary_receipt")
        if isinstance(baseline, Mapping)
        else None
    )
    has_source_authority = bool(
        baseline_source is not None
        or spec.get("frozen_source_boundary_receipt") is not None
    )
    has_full_authority = bool(
        baseline_full is not None
        or spec.get("frozen_boundary_receipt") is not None
    )
    if has_source_authority and has_full_authority:
        return None, None

    full = load_frozen_boundary_receipt(
        spec,
        candidate_id=candidate_id,
        current_owner_contract=current_owner_contract,
    )
    source_only = (
        None
        if full is not None
        else load_frozen_source_boundary_receipt(spec, candidate_id=candidate_id)
    )
    source_review = (
        full.source_full_window
        if full is not None
        else source_only.source_full_window if source_only is not None else None
    )
    return full, source_review
