"""Candidate-bound formal successor for B2 nested-media caption correction.

This module preserves the historical CPA/final-review receipts and advances the
exact release bytes through a typed successor.  It never issues provider calls,
renders media, or publishes.  The only authorized subtitle mutations are the
six watched-source cue drops and the one bounded outer-host reaction retained
by :mod:`src.autoslice.b2_caption_successor`.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unicodedata

from src.autoslice.b2_caption_successor import (
    CANDIDATE_ID,
    expected_corrected_cues,
    load_authority,
)
from src.autoslice.boundary_endpoint_binding import bind_final_semantic_endpoint
from src.autoslice.boundary_semantic_review import cue_grid_sha256
from src.autoslice.final_review_contract import (
    FinalReviewContractError,
    validate_final_review_release,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.review_package_boundary_validators import (
    semantic_boundary_review_is_valid,
)
from src.autoslice.review_evidence_store import review_from_bytes

FINAL_REVIEW_SCHEMA_VERSION = "b2-caption-final-review-successor.v1"
SOURCE_FACT_SCHEMA_VERSION = "b2-caption-source-fact-successor.v1"
CHAT_SUCCESSOR_SCHEMA_VERSION = "b2-caption-chat-authority-successor.v1"
SOURCE_FACT_DECISION = "KEEP_VIA_NESTED_SOURCE_ATTRIBUTION_SUCCESSOR"
PARENT_SRT_SHA256 = (
    "sha256:d76ab9b4eaf88a708a77d90e497751fd9eb84526e0a6e4f1c216a4360742b503"
)
CURRENT_SRT_SHA256 = (
    "sha256:f10409d4ebe626a15ca3414dab41b1efcc0faee8bd805ee848d7967046f2047a"
)
PARENT_CHAT_FILE_SHA256 = (
    "sha256:f25b4bfcc39e751bef77598bc4c72ebd0c760503468ea8203e29b9f154987a4a"
)
ATTRIBUTION_FILE_SHA256 = (
    "sha256:68b9da6d95f97bfca226fa3f196b173269b2b59ade8712cb0e6148bd94b91d02"
)
PARENT_SOURCE_FACT_RECEIPT_SHA256 = (
    "sha256:5c2d40c9b557cdfe7befa9cb89cb69d2e20db4056e1c8731a2e2e146978e69a0"
)
PARENT_BOUNDARY_REQUEST_SHA256 = (
    "sha256:35c0e9b190d2ef8c62b529533ecd50ca504cd84c52fd12327f2d722f01a1a0c7"
)
PARENT_BOUNDARY_CUE_GRID_SHA256 = (
    "sha256:74bea73637d097e6d97fa7e9a8425b31790165dd7b4428de0a52a057308dbc3c"
)
DELIVERY_END_MS = 169_630
SNAPPED_END_MS = 169_230


class B2CaptionFormalSuccessorError(ValueError):
    """A frozen B2 formal-successor invariant failed."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _canonical_sha256(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _bytes_sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _text_sha256(value: str) -> str:
    return _bytes_sha256(value.encode("utf-8"))


def _seal(document: Mapping[str, object]) -> dict[str, object]:
    sealed = deepcopy(dict(document))
    sealed.pop("receipt_sha256", None)
    sealed["receipt_sha256"] = _canonical_sha256(sealed)
    return sealed


def _receipt_is_sealed(document: Mapping[str, object]) -> bool:
    body = deepcopy(dict(document))
    declared = body.pop("receipt_sha256", None)
    return isinstance(declared, str) and declared == _canonical_sha256(body)


def _read_bound(path: Path, *, expected_sha256: str, reason_code: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise B2CaptionFormalSuccessorError(reason_code)
    raw = path.read_bytes()
    if _bytes_sha256(raw) != expected_sha256:
        raise B2CaptionFormalSuccessorError(reason_code)
    return raw


def _decode(raw: bytes, *, reason_code: str) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise B2CaptionFormalSuccessorError(reason_code) from exc


def _load_json_bytes(
    raw: bytes,
    *,
    reason_code: str,
    review: bool = False,
    directory: Path | None = None,
) -> dict[str, object]:
    try:
        value = (
            review_from_bytes(raw, directory=directory or Path.cwd())
            if review
            else json.loads(raw.decode("utf-8"))
        )
    except (OSError, TypeError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise B2CaptionFormalSuccessorError(reason_code) from exc
    if not isinstance(value, dict):
        raise B2CaptionFormalSuccessorError(reason_code)
    return value


def _transcript(raw: bytes) -> str:
    return "\n".join(
        cue.text.strip()
        for cue in parse_srt_cues(
            _decode(raw, reason_code="B2_FORMAL_SRT_INVALID")
        )
        if cue.text.strip()
    )


def _normalized(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    return "".join(
        char
        for char in normalized
        if char.isalnum() or "\u4e00" <= char <= "\u9fff"
    )


def _validate_attribution(
    raw: bytes, *, current_srt_sha256: str
) -> dict[str, object]:
    if _bytes_sha256(raw) != ATTRIBUTION_FILE_SHA256:
        raise B2CaptionFormalSuccessorError("B2_ATTRIBUTION_FILE_DRIFT")
    document = _load_json_bytes(raw, reason_code="B2_ATTRIBUTION_INVALID")
    output_srt = document.get("output_srt")
    decisions = document.get("decisions")
    if not (
        document.get("schema_version") == "e352-b2-final-source-attribution.v1"
        and document.get("status")
        == "PASS_CONSERVATIVE_SOURCE_ATTRIBUTION_CORRECTION"
        and document.get("candidate_id") == CANDIDATE_ID
        and isinstance(output_srt, Mapping)
        and "sha256:" + str(output_srt.get("sha256") or "")
        == current_srt_sha256
        and isinstance(decisions, list)
    ):
        raise B2CaptionFormalSuccessorError("B2_ATTRIBUTION_INVALID")
    by_cue = {
        int(row["original_cue"]): row
        for row in decisions
        if isinstance(row, Mapping)
        and isinstance(row.get("original_cue"), int)
    }
    expected = {
        45: ("REMOVE_SOURCE_MEDIA", "我打嗝跟打雷似的", None),
        46: ("REMOVE_SOURCE_MEDIA", "雷公电母来了有感觉吗", None),
        47: ("REMOVE_SOURCE_MEDIA", "露蒂丝打嗝可爱吗", None),
        48: ("REMOVE_SOURCE_MEDIA", "呃可爱吧", None),
        49: (
            "KEEP_OVERLAP_HOST_REACTION_ONLY",
            "是吗，我们俩长得很像吗，哎",
            "哈",
        ),
        50: ("REMOVE_SOURCE_MEDIA", "哎呀，就是因为我们长得太像了", None),
        51: ("REMOVE_SOURCE_MEDIA", "就是因为我们长得太像了", None),
    }
    if set(by_cue) != set(expected):
        raise B2CaptionFormalSuccessorError("B2_ATTRIBUTION_DECISIONS_INVALID")
    for cue_index, (decision, source_text, host_text) in expected.items():
        row = by_cue[cue_index]
        if not (
            row.get("decision") == decision
            and row.get("source_text") == source_text
            and row.get("host_text") == host_text
        ):
            raise B2CaptionFormalSuccessorError(
                "B2_ATTRIBUTION_DECISIONS_INVALID"
            )
    overlap = by_cue[49]
    if not (
        overlap.get("new_start_ms") == 116_680
        and overlap.get("new_end_ms") == 117_460
        and overlap.get("inference")
    ):
        raise B2CaptionFormalSuccessorError("B2_ATTRIBUTION_OVERLAP_INVALID")
    boundaries = document.get("evidence_boundaries")
    if not (
        isinstance(boundaries, Mapping)
        and boundaries.get("uniform_host_is_display_only") is True
        and boundaries.get("no_subtitle_means_host") is False
        and boundaries.get("ocr_alone_did_not_authorize_mutation") is True
        and boundaries.get("host_reaction_is_inference") is True
    ):
        raise B2CaptionFormalSuccessorError("B2_ATTRIBUTION_BOUNDARY_INVALID")
    return document


def _typed_mutations(parent_srt_raw: bytes, current_srt_raw: bytes) -> list[dict[str, object]]:
    parent = parse_srt_cues(
        _decode(parent_srt_raw, reason_code="B2_PARENT_SRT_INVALID")
    )
    current = parse_srt_cues(
        _decode(current_srt_raw, reason_code="B2_CURRENT_SRT_INVALID")
    )
    if len(parent) != 69 or len(current) != 63:
        raise B2CaptionFormalSuccessorError("B2_CUE_COUNT_DRIFT")
    expected = expected_corrected_cues(parent, load_authority())
    # The parser returns SrtCue, while the correction projector returns TimedCue.
    # Compare their contract fields rather than unrelated dataclass identities.
    if [(c.index, c.start_ms, c.end_ms, c.text) for c in current] != [
        (c.index, c.start_ms, c.end_ms, c.text) for c in expected
    ]:
        raise B2CaptionFormalSuccessorError("B2_CURRENT_SRT_PROJECTION_DRIFT")
    result: list[dict[str, object]] = []
    for source_index in (45, 46, 47, 48, 50, 51):
        cue = parent[source_index - 1]
        row: dict[str, object] = {
            "schema_version": "b2-caption-typed-mutation.v1",
            "candidate_id": CANDIDATE_ID,
            "action": "DROP_WATCHED_SOURCE_MEDIA_CUE",
            "source_cue_index": source_index,
            "before": cue.text,
            "before_start_ms": cue.start_ms,
            "before_end_ms": cue.end_ms,
            "after": None,
            "decision_authority": "E352_EXACT_SOURCE_ALIGNMENT_AND_FRAME_CAPTION",
            "attribution_file_sha256": ATTRIBUTION_FILE_SHA256,
            "timing_mutated": True,
            "text_mutated": True,
        }
        row["mutation_sha256"] = _canonical_sha256(row)
        result.append(row)
    before = parent[48]
    after = next(cue for cue in current if cue.start_ms == 116_680)
    overlap: dict[str, object] = {
        "schema_version": "b2-caption-typed-mutation.v1",
        "candidate_id": CANDIDATE_ID,
        "action": "RETAIN_OUTER_HOST_REACTION_FROM_OVERLAP",
        "source_cue_index": 49,
        "before": before.text,
        "before_start_ms": before.start_ms,
        "before_end_ms": before.end_ms,
        "after": after.text,
        "after_start_ms": after.start_ms,
        "after_end_ms": after.end_ms,
        "decision_authority": "E352_OUTER_ONLY_RESIDUAL_BURST_INFERENCE",
        "attribution_file_sha256": ATTRIBUTION_FILE_SHA256,
        "inference_disclosed": True,
        "timing_mutated": True,
        "text_mutated": True,
    }
    overlap["mutation_sha256"] = _canonical_sha256(overlap)
    result.append(overlap)
    return result


def _validate_parent_review(
    review: object, *, parent_srt_sha256: str
) -> dict[str, object]:
    if not isinstance(review, Mapping):
        raise B2CaptionFormalSuccessorError("B2_PARENT_REVIEW_INVALID")
    parent = deepcopy(dict(review))
    # Validate the immutable original review, not a resealed replacement of it.
    if _canonical_sha256(parent) != "sha256:0a3aa1b6616a2a6f6dc73fa1b965b64beef1802fff48bea69d100fae57d6dcc6":
        raise B2CaptionFormalSuccessorError("B2_PARENT_REVIEW_DRIFT")
    try:
        validate_final_review_release(
            parent, expected_srt_sha256=parent_srt_sha256
        )
    except (FinalReviewContractError, TypeError, ValueError) as exc:
        raise B2CaptionFormalSuccessorError("B2_PARENT_REVIEW_INVALID") from exc
    boundary = parent.get("boundary_semantic_review")
    if not (
        isinstance(boundary, Mapping)
        and boundary.get("candidate_id") == CANDIDATE_ID
        and boundary.get("request_sha256") == PARENT_BOUNDARY_REQUEST_SHA256
        and boundary.get("cue_grid_sha256") == PARENT_BOUNDARY_CUE_GRID_SHA256
        and boundary.get("status") == "PASS"
        and boundary.get("review_scope") == "final_delivery"
    ):
        raise B2CaptionFormalSuccessorError("B2_PARENT_BOUNDARY_INVALID")
    return parent


def _rebind_boundary(
    *,
    parent_boundary: Mapping[str, object],
    current_srt_raw: bytes,
    attribution_file_sha256: str,
) -> dict[str, object]:
    cues = parse_srt_cues(
        _decode(current_srt_raw, reason_code="B2_CURRENT_SRT_INVALID")
    )
    if len(cues) != 63:
        raise B2CaptionFormalSuccessorError("B2_CUE_COUNT_DRIFT")
    parent_endpoint = parent_boundary.get("final_endpoint_binding")
    if not isinstance(parent_endpoint, Mapping):
        raise B2CaptionFormalSuccessorError("B2_PARENT_BOUNDARY_INVALID")
    grid = cue_grid_sha256(cues)
    request_lineage = {
        "schema_version": "b2-caption-boundary-successor-request.v1",
        "candidate_id": CANDIDATE_ID,
        "parent_request_sha256": parent_boundary.get("request_sha256"),
        "parent_cue_grid_sha256": parent_boundary.get("cue_grid_sha256"),
        "current_cue_grid_sha256": grid,
        "parent_cue_count": 69,
        "current_cue_count": 63,
        "dropped_source_cues": [45, 46, 47, 48, 50, 51],
        "overlap_source_cue": 49,
        "attribution_file_sha256": attribution_file_sha256,
        "terminal_text_and_timing_unchanged": True,
        "source_separation_witness_reused": True,
    }
    successor = deepcopy(dict(parent_boundary))
    successor["request_sha256"] = _canonical_sha256(request_lineage)
    successor["cue_grid_sha256"] = grid
    successor["target_cue_index"] = 63
    successor["recommended_end_cue_index"] = 63
    successor["evidence_cue_indexes"] = [58, 59, 60, 61, 62, 63]
    successor["summary"] = (
        "唯一可选终点63句法完整，并已收束方脸调侃后的自我克制回应、"
        "覆盖内容锚点；中段仅移除已归属被观看媒体的字幕并保留一处外层反应，"
        "终点文字、时间与绑定的源分离见证不变。"
    )
    successor["b2_caption_boundary_successor"] = _seal(request_lineage)
    successor.pop("final_endpoint_binding", None)
    rebound, reasons = bind_final_semantic_endpoint(
        semantic_review=successor,
        cues=cues,
        closure_cue=cues[-1],
        snapped_end_ms=SNAPPED_END_MS,
        final_start_ms=0,
        final_end_ms=DELIVERY_END_MS,
    )
    if reasons or not semantic_boundary_review_is_valid(
        rebound, expected_scope="final_delivery"
    ):
        raise B2CaptionFormalSuccessorError("B2_BOUNDARY_REBIND_FAILED")
    endpoint = rebound.get("final_endpoint_binding")
    if not (
        isinstance(endpoint, Mapping)
        and rebound.get("recommended_end_ms") == SNAPPED_END_MS
        and endpoint.get("final_snapped_end_ms") == SNAPPED_END_MS
        and endpoint.get("final_end_ms") == DELIVERY_END_MS
        and endpoint.get("final_closure_cue_index") == 63
        and endpoint.get("closure_text_sha256")
        == parent_endpoint.get("closure_text_sha256")
        and rebound.get("source_separation_witness")
        == parent_boundary.get("source_separation_witness")
    ):
        raise B2CaptionFormalSuccessorError("B2_BOUNDARY_SEMANTICS_DRIFT")
    return rebound


def _build_final_review_from_bound_inputs(
    *,
    parent_review: Mapping[str, object],
    parent_srt_raw: bytes,
    current_srt_raw: bytes,
    attribution_raw: bytes,
) -> dict[str, object]:
    if _bytes_sha256(parent_srt_raw) != PARENT_SRT_SHA256:
        raise B2CaptionFormalSuccessorError("B2_PARENT_SRT_DRIFT")
    if _bytes_sha256(current_srt_raw) != CURRENT_SRT_SHA256:
        raise B2CaptionFormalSuccessorError("B2_CURRENT_SRT_DRIFT")
    parent = _validate_parent_review(
        parent_review, parent_srt_sha256=PARENT_SRT_SHA256
    )
    attribution = _validate_attribution(
        attribution_raw, current_srt_sha256=CURRENT_SRT_SHA256
    )
    mutations = _typed_mutations(parent_srt_raw, current_srt_raw)
    parent_boundary = parent.get("boundary_semantic_review")
    assert isinstance(parent_boundary, Mapping)
    boundary = _rebind_boundary(
        parent_boundary=parent_boundary,
        current_srt_raw=current_srt_raw,
        attribution_file_sha256=ATTRIBUTION_FILE_SHA256,
    )
    document: dict[str, object] = {
        "schema_version": FINAL_REVIEW_SCHEMA_VERSION,
        "status": "CLEAN",
        "release_gate": "PASS",
        "candidate_id": CANDIDATE_ID,
        "reviewed_srt_sha256": CURRENT_SRT_SHA256,
        "parent_reviewed_srt_sha256": PARENT_SRT_SHA256,
        "parent_review": parent,
        "parent_review_sha256": _canonical_sha256(parent),
        "parent_srt_raw_b64": base64.b64encode(parent_srt_raw).decode("ascii"),
        "current_srt_raw_b64": base64.b64encode(current_srt_raw).decode("ascii"),
        "attribution_raw_b64": base64.b64encode(attribution_raw).decode("ascii"),
        "attribution_file_sha256": ATTRIBUTION_FILE_SHA256,
        "attribution_status": attribution["status"],
        "typed_mutations": mutations,
        "typed_mutation_count": len(mutations),
        "boundary_semantic_review": boundary,
        "findings": [],
        "resolved_findings": [],
        "validated_finding_count": 0,
        "unresolved_findings_disclosed": [],
        "provider_calls": 0,
        "paid_calls": 0,
        "new_audio_calls": 0,
        "asr_calls": 0,
        "render_calls": 0,
        "image_generation_calls": 0,
        "production_state_mutations": 0,
        "upload_calls": 0,
        "timing_mutated": True,
        "text_mutated": True,
        "parent_exact_final_receipts_preserved": True,
        "authority": "E352_EXACT_WATCHED_SOURCE_ALIGNMENT_AND_OUTER_RESIDUAL_ATTRIBUTION",
    }
    return _seal(document)


def build_final_review_successor(
    *,
    parent_chat_authority: Path,
    parent_srt: Path,
    current_srt: Path,
    attribution: Path,
) -> dict[str, object]:
    parent_chat_raw = _read_bound(
        Path(parent_chat_authority),
        expected_sha256=PARENT_CHAT_FILE_SHA256,
        reason_code="B2_PARENT_CHAT_DRIFT",
    )
    parent_chat = _load_json_bytes(
        parent_chat_raw,
        reason_code="B2_PARENT_CHAT_INVALID",
        review=True,
        directory=Path(parent_chat_authority).parent,
    )
    parent_review = parent_chat.get("final_review_audit")
    if not isinstance(parent_review, Mapping):
        raise B2CaptionFormalSuccessorError("B2_PARENT_REVIEW_INVALID")
    return _build_final_review_from_bound_inputs(
        parent_review=parent_review,
        parent_srt_raw=_read_bound(
            Path(parent_srt),
            expected_sha256=PARENT_SRT_SHA256,
            reason_code="B2_PARENT_SRT_DRIFT",
        ),
        current_srt_raw=_read_bound(
            Path(current_srt),
            expected_sha256=CURRENT_SRT_SHA256,
            reason_code="B2_CURRENT_SRT_DRIFT",
        ),
        attribution_raw=_read_bound(
            Path(attribution),
            expected_sha256=ATTRIBUTION_FILE_SHA256,
            reason_code="B2_ATTRIBUTION_FILE_DRIFT",
        ),
    )


def validate_final_review_successor(
    review: object, *, expected_srt_sha256: str | None = None
) -> dict[str, object]:
    if not isinstance(review, Mapping) or review.get(
        "schema_version"
    ) != FINAL_REVIEW_SCHEMA_VERSION:
        raise B2CaptionFormalSuccessorError("B2_FINAL_REVIEW_SCHEMA_INVALID")
    document = deepcopy(dict(review))
    if not _receipt_is_sealed(document):
        raise B2CaptionFormalSuccessorError("B2_FINAL_REVIEW_RECEIPT_INVALID")
    if expected_srt_sha256 is not None and expected_srt_sha256 != CURRENT_SRT_SHA256:
        raise B2CaptionFormalSuccessorError("B2_FINAL_REVIEW_SRT_BINDING_MISMATCH")
    try:
        parent_raw = base64.b64decode(
            str(document["parent_srt_raw_b64"]), validate=True
        )
        current_raw = base64.b64decode(
            str(document["current_srt_raw_b64"]), validate=True
        )
        attribution_raw = base64.b64decode(
            str(document["attribution_raw_b64"]), validate=True
        )
    except (KeyError, ValueError) as exc:
        raise B2CaptionFormalSuccessorError("B2_FINAL_REVIEW_PAYLOAD_INVALID") from exc
    parent_review = document.get("parent_review")
    if not isinstance(parent_review, Mapping):
        raise B2CaptionFormalSuccessorError("B2_FINAL_REVIEW_PAYLOAD_INVALID")
    expected = _build_final_review_from_bound_inputs(
        parent_review=parent_review,
        parent_srt_raw=parent_raw,
        current_srt_raw=current_raw,
        attribution_raw=attribution_raw,
    )
    if document != expected:
        raise B2CaptionFormalSuccessorError("B2_FINAL_REVIEW_MISMATCH")
    return document


def build_chat_authority_successor(
    *,
    parent_chat_authority: Path,
    final_review_successor: Mapping[str, object],
) -> dict[str, object]:
    parent_raw = _read_bound(
        Path(parent_chat_authority),
        expected_sha256=PARENT_CHAT_FILE_SHA256,
        reason_code="B2_PARENT_CHAT_DRIFT",
    )
    parent = _load_json_bytes(
        parent_raw,
        reason_code="B2_PARENT_CHAT_INVALID",
        review=True,
        directory=Path(parent_chat_authority).parent,
    )
    review = validate_final_review_successor(
        final_review_successor, expected_srt_sha256=CURRENT_SRT_SHA256
    )
    chat = deepcopy(parent)
    ledger = {
        "schema_version": CHAT_SUCCESSOR_SCHEMA_VERSION,
        "status": "PASS",
        "candidate_id": CANDIDATE_ID,
        "parent_chat_file_sha256": PARENT_CHAT_FILE_SHA256,
        "parent_srt_sha256": PARENT_SRT_SHA256,
        "current_srt_sha256": CURRENT_SRT_SHA256,
        "final_review_successor_receipt_sha256": review["receipt_sha256"],
        "attribution_file_sha256": ATTRIBUTION_FILE_SHA256,
        "provider_calls": 0,
        "asr_calls": 0,
        "production_state_mutations": 0,
        "upload_calls": 0,
    }
    chat["b2_caption_attribution_successor"] = _seal(ledger)
    chat["final_review_audit"] = deepcopy(review)
    current_plain = CURRENT_SRT_SHA256.removeprefix("sha256:")
    for field in (
        "final_text_srt_sha256",
        "final_speaker_srt_sha256",
        "final_output_srt_sha256",
    ):
        chat[field] = current_plain
    return chat


def _validate_parent_source_fact(
    *,
    parent_receipt: Mapping[str, object],
    parent_srt_raw: bytes,
    selection_hook: str,
    title: str,
    clip_context_prompt: str,
    selection_scorecard: object,
    speaker_evidence: object,
    story_contract: Mapping[str, object],
) -> None:
    from src.autoslice.source_fact_review import validate_source_fact_review

    if not (
        parent_receipt.get("schema_version") == "lidousha-source-fact-review.v1"
        and parent_receipt.get("status") == "PASS"
        and parent_receipt.get("decision") == "KEEP"
        and parent_receipt.get("receipt_sha256")
        == PARENT_SOURCE_FACT_RECEIPT_SHA256
    ):
        raise B2CaptionFormalSuccessorError("B2_PARENT_SOURCE_FACT_INVALID")
    parent_story = deepcopy(dict(story_contract))
    parent_story["transcript_sha256"] = _text_sha256(
        _transcript(parent_srt_raw)
    )
    parent_story["source_fact_review"] = deepcopy(dict(parent_receipt))
    with tempfile.TemporaryDirectory(prefix="b2-source-fact-") as directory:
        parent_path = Path(directory) / "parent.srt"
        parent_path.write_bytes(parent_srt_raw)
        if not validate_source_fact_review(
            parent_receipt,
            selection_hook=selection_hook,
            title=title,
            final_transcript=_transcript(parent_srt_raw),
            clip_context_prompt=clip_context_prompt,
            selection_scorecard=selection_scorecard,
            candidate_id=CANDIDATE_ID,
            final_reviewed_srt_path=parent_path,
            speaker_evidence=speaker_evidence,
            story_contract=parent_story,
        ):
            raise B2CaptionFormalSuccessorError("B2_PARENT_SOURCE_FACT_INVALID")


def _build_source_fact_from_bound_inputs(
    *,
    parent_receipt: Mapping[str, object],
    final_review_successor: Mapping[str, object],
    parent_srt_raw: bytes,
    current_srt_raw: bytes,
    attribution_raw: bytes,
    selection_hook: str,
    title: str,
    clip_context_prompt: str,
    selection_scorecard: object,
    speaker_evidence: object,
    story_contract: Mapping[str, object],
) -> dict[str, object]:
    if _bytes_sha256(parent_srt_raw) != PARENT_SRT_SHA256:
        raise B2CaptionFormalSuccessorError("B2_SOURCE_FACT_PARENT_SRT_DRIFT")
    if _bytes_sha256(current_srt_raw) != CURRENT_SRT_SHA256:
        raise B2CaptionFormalSuccessorError("B2_SOURCE_FACT_CURRENT_SRT_DRIFT")
    final_review = validate_final_review_successor(
        final_review_successor, expected_srt_sha256=CURRENT_SRT_SHA256
    )
    attribution = _validate_attribution(
        attribution_raw, current_srt_sha256=CURRENT_SRT_SHA256
    )
    _validate_parent_source_fact(
        parent_receipt=parent_receipt,
        parent_srt_raw=parent_srt_raw,
        selection_hook=selection_hook,
        title=title,
        clip_context_prompt=clip_context_prompt,
        selection_scorecard=selection_scorecard,
        speaker_evidence=speaker_evidence,
        story_contract=story_contract,
    )
    if not (
        parent_receipt.get("final_selection_hook") == selection_hook
        and parent_receipt.get("final_title") == title
    ):
        raise B2CaptionFormalSuccessorError("B2_SOURCE_FACT_PUBLIC_SURFACE_DRIFT")
    current_transcript = _transcript(current_srt_raw)
    required_current = (
        "看露蒂丝和瓜咪打嗝",
        "这嗝很可爱啊",
        "人家这嗝多可爱啊",
        "所以我们俩，她、她、她、她、她这样鼓嘴脸好大",
        "我鼓起来脸是个方的，你看",
    )
    current_normalized = _normalized(current_transcript)
    current_checks = [
        {
            "surface": surface,
            "surface_sha256": _text_sha256(surface),
            "present": _normalized(surface) in current_normalized,
        }
        for surface in required_current
    ]
    if not all(row["present"] is True for row in current_checks):
        raise B2CaptionFormalSuccessorError("B2_SOURCE_FACT_CURRENT_EVIDENCE_MISSING")
    decisions = attribution["decisions"]
    assert isinstance(decisions, list)
    source_surfaces = {
        int(row["original_cue"]): str(row.get("source_text") or "")
        for row in decisions
        if isinstance(row, Mapping) and isinstance(row.get("original_cue"), int)
    }
    if not (
        source_surfaces.get(45) == "我打嗝跟打雷似的"
        and source_surfaces.get(46) == "雷公电母来了有感觉吗"
        and "雷公电母" in title
        and "像打雷的声音" in selection_hook
        and "片中" in selection_hook
    ):
        raise B2CaptionFormalSuccessorError("B2_SOURCE_FACT_NESTED_EVIDENCE_MISSING")
    document: dict[str, object] = {
        "schema_version": SOURCE_FACT_SCHEMA_VERSION,
        "status": "PASS",
        "decision": SOURCE_FACT_DECISION,
        "candidate_id": CANDIDATE_ID,
        "original_selection_hook": selection_hook,
        "original_title": title,
        "final_selection_hook": selection_hook,
        "final_title": title,
        "parent_receipt": deepcopy(dict(parent_receipt)),
        "parent_receipt_sha256": PARENT_SOURCE_FACT_RECEIPT_SHA256,
        "final_review_successor": deepcopy(dict(final_review)),
        "final_review_successor_receipt_sha256": final_review["receipt_sha256"],
        "parent_srt_raw_b64": base64.b64encode(parent_srt_raw).decode("ascii"),
        "current_srt_raw_b64": base64.b64encode(current_srt_raw).decode("ascii"),
        "attribution_raw_b64": base64.b64encode(attribution_raw).decode("ascii"),
        "attribution_file_sha256": ATTRIBUTION_FILE_SHA256,
        "parent_transcript_sha256": _text_sha256(_transcript(parent_srt_raw)),
        "current_transcript_sha256": _text_sha256(current_transcript),
        "clip_context_prompt_sha256": _text_sha256(clip_context_prompt),
        "selection_scorecard_sha256": _canonical_sha256(selection_scorecard),
        "speaker_evidence": deepcopy(speaker_evidence),
        "speaker_evidence_sha256": _canonical_sha256(speaker_evidence),
        "current_transcript_evidence_checks": current_checks,
        "nested_source_evidence": [
            {
                "original_cue": 45,
                "surface": source_surfaces[45],
                "supports": "selection_hook_like_thunder",
            },
            {
                "original_cue": 46,
                "surface": source_surfaces[46],
                "supports": "title_雷公电母",
            },
        ],
        "source_attribution_scope": (
            "WATCHED_SOURCE_MEDIA_FACTS_SUPPORT_DERIVED_PUBLIC_TEXT_ONLY;"
            "DOES_NOT_REINSERT_SOURCE_DIALOGUE_INTO_HOST_SUBTITLES"
        ),
        "changed_surfaces": [],
        "provider_calls": 0,
        "paid_calls": 0,
        "new_fact_votes": 0,
        "new_audio_calls": 0,
        "asr_calls": 0,
        "render_calls": 0,
        "image_generation_calls": 0,
        "production_state_mutations": 0,
        "upload_calls": 0,
        "summary": (
            "The public hook/title remain unchanged. Host subtitles no longer claim "
            "the watched-source dialogue; exact nested-source attribution independently "
            "supports the thunder/雷公电母 public-text surfaces."
        ),
    }
    return _seal(document)


def build_source_fact_successor(
    *,
    parent_receipt: Mapping[str, object],
    final_review_successor: Mapping[str, object],
    parent_srt: Path,
    current_srt: Path,
    attribution: Path,
    selection_hook: str,
    title: str,
    clip_context_prompt: str,
    selection_scorecard: object,
    speaker_evidence: object,
    story_contract: Mapping[str, object],
) -> dict[str, object]:
    return _build_source_fact_from_bound_inputs(
        parent_receipt=parent_receipt,
        final_review_successor=final_review_successor,
        parent_srt_raw=_read_bound(
            Path(parent_srt),
            expected_sha256=PARENT_SRT_SHA256,
            reason_code="B2_SOURCE_FACT_PARENT_SRT_DRIFT",
        ),
        current_srt_raw=_read_bound(
            Path(current_srt),
            expected_sha256=CURRENT_SRT_SHA256,
            reason_code="B2_SOURCE_FACT_CURRENT_SRT_DRIFT",
        ),
        attribution_raw=_read_bound(
            Path(attribution),
            expected_sha256=ATTRIBUTION_FILE_SHA256,
            reason_code="B2_ATTRIBUTION_FILE_DRIFT",
        ),
        selection_hook=selection_hook,
        title=title,
        clip_context_prompt=clip_context_prompt,
        selection_scorecard=selection_scorecard,
        speaker_evidence=speaker_evidence,
        story_contract=story_contract,
    )


def validate_source_fact_successor(
    review: object,
    *,
    selection_hook: str,
    title: str,
    final_transcript: str,
    clip_context_prompt: str,
    selection_scorecard: object = None,
    candidate_id: str | None = None,
    final_reviewed_srt_path: Path | None = None,
    speaker_evidence: object = None,
    story_contract: Mapping[str, object] | None = None,
    **_unused: object,
) -> bool:
    if not isinstance(review, Mapping) or review.get(
        "schema_version"
    ) != SOURCE_FACT_SCHEMA_VERSION:
        raise B2CaptionFormalSuccessorError("B2_SOURCE_FACT_SCHEMA_INVALID")
    document = deepcopy(dict(review))
    if not _receipt_is_sealed(document):
        raise B2CaptionFormalSuccessorError("B2_SOURCE_FACT_RECEIPT_INVALID")
    if (
        candidate_id != CANDIDATE_ID
        or final_reviewed_srt_path is None
        or story_contract is None
    ):
        raise B2CaptionFormalSuccessorError("B2_SOURCE_FACT_SCOPE_INVALID")
    current_raw = _read_bound(
        Path(final_reviewed_srt_path),
        expected_sha256=CURRENT_SRT_SHA256,
        reason_code="B2_SOURCE_FACT_CURRENT_SRT_DRIFT",
    )
    if _transcript(current_raw) != final_transcript:
        raise B2CaptionFormalSuccessorError("B2_SOURCE_FACT_TRANSCRIPT_MISMATCH")
    try:
        parent_raw = base64.b64decode(
            str(document["parent_srt_raw_b64"]), validate=True
        )
        embedded_current = base64.b64decode(
            str(document["current_srt_raw_b64"]), validate=True
        )
        attribution_raw = base64.b64decode(
            str(document["attribution_raw_b64"]), validate=True
        )
    except (KeyError, ValueError) as exc:
        raise B2CaptionFormalSuccessorError("B2_SOURCE_FACT_PAYLOAD_INVALID") from exc
    if embedded_current != current_raw:
        raise B2CaptionFormalSuccessorError("B2_SOURCE_FACT_CURRENT_SRT_DRIFT")
    parent_receipt = document.get("parent_receipt")
    final_review = document.get("final_review_successor")
    if not isinstance(parent_receipt, Mapping) or not isinstance(
        final_review, Mapping
    ):
        raise B2CaptionFormalSuccessorError("B2_SOURCE_FACT_PAYLOAD_INVALID")
    expected = _build_source_fact_from_bound_inputs(
        parent_receipt=parent_receipt,
        final_review_successor=final_review,
        parent_srt_raw=parent_raw,
        current_srt_raw=current_raw,
        attribution_raw=attribution_raw,
        selection_hook=selection_hook,
        title=title,
        clip_context_prompt=clip_context_prompt,
        selection_scorecard=selection_scorecard,
        speaker_evidence=speaker_evidence,
        story_contract=story_contract,
    )
    if document != expected:
        raise B2CaptionFormalSuccessorError("B2_SOURCE_FACT_MISMATCH")
    return True


def validate_source_fact_from_scope(scope: Mapping[str, object]) -> bool | None:
    review = scope.get("review")
    if not isinstance(review, Mapping) or review.get(
        "schema_version"
    ) != SOURCE_FACT_SCHEMA_VERSION:
        return None
    try:
        return validate_source_fact_successor(
            review,
            selection_hook=str(scope.get("selection_hook") or ""),
            title=str(scope.get("title") or ""),
            final_transcript=str(scope.get("final_transcript") or ""),
            clip_context_prompt=str(scope.get("clip_context_prompt") or ""),
            selection_scorecard=scope.get("selection_scorecard"),
            candidate_id=(
                str(scope.get("candidate_id"))
                if scope.get("candidate_id") is not None
                else None
            ),
            final_reviewed_srt_path=(
                Path(scope["final_reviewed_srt_path"])
                if scope.get("final_reviewed_srt_path") is not None
                else None
            ),
            speaker_evidence=scope.get("speaker_evidence"),
            story_contract=(
                scope.get("story_contract")
                if isinstance(scope.get("story_contract"), Mapping)
                else None
            ),
        )
    except B2CaptionFormalSuccessorError:
        return False
