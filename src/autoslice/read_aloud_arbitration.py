"""近失念读的闭集裁决（从 chat_proposals 拆出的域模块）。

candidate_entities 恒为两个纯文本候选（弹幕原文 vs 当前跨度）；AGY
只能接收剥离候选后的拼音听写请求，CPA 才能从闭集选边。任何结局
（确认/否决/UNCERTAIN）都写入 read_aloud_arbitrations 审计——静默
出局是 脑海案里最贵的病。"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any, Mapping, Protocol, Sequence

from src.autoslice.chat_evidence import (
    ChatEvidence,
    EntityVerifier,
    _request_sha256,
    _validated_read_aloud_verdict,
    _valid_sha256,
    normalize_chat_text,
)

_WHOLE_LINE_MIN_COVERAGE = 0.80
_WHOLE_LINE_MIN_EXTENT = 0.82
_WHOLE_LINE_MIN_PRECISION = 0.60
_WHOLE_LINE_MAX_INTERIOR_GAP_CHARS = 2
_WHOLE_LINE_MAX_INTERIOR_GAP_RATIO = 0.20
# 边界借字剥离：authority 边界的孤立小匹配块（≤2 字）若与相邻块隔着
# ≥3 字的 observed 侧插入 run，多半是从转录里的相邻句借来的同形字
# （1863 案：SC 尾字「了」借到下一句「现在几点了」），不算边界见证。
_BOUNDARY_BORROW_MAX_BLOCK_CHARS = 2
_BOUNDARY_BORROW_MIN_OBSERVED_GAP_CHARS = 3


class _ChatProposalDiscoveryLike(Protocol):
    proposals: list[dict[str, Any]]
    superseded_chat_proposals: list[dict[str, Any]]
    read_aloud_arbitrations: list[dict[str, Any]]


def proposal_alignment_basis(proposal: Mapping[str, Any]) -> str:
    """Name the evidence that actually authorizes a chat text mutation."""

    entity = proposal.get("entity_verdict")
    read_aloud = proposal.get("read_aloud_verdict")
    if isinstance(entity, Mapping) and entity.get(
        "authority_kind"
    ) == "audio_forced_choice":
        return "raw-audio-forced-choice.v1"
    if isinstance(entity, Mapping) and entity.get(
        "authority_kind"
    ) == "cpa_witness_adjudication":
        return "candidate-blind-audio-witness-plus-cpa-judge.v1"
    if isinstance(read_aloud, Mapping):
        if read_aloud.get("authority_kind") == "audio_forced_choice":
            return "raw-audio-forced-choice.v1"
        if read_aloud.get(
            "authority_kind"
        ) == "cpa_witness_adjudication":
            return "candidate-blind-audio-witness-plus-cpa-judge.v1"
        if read_aloud.get("reason_code") == "READ_ALOUD_CONFIRMED_BY_CONTEXT":
            return "structured-chat-context-plus-near-complete-transcript.v1"
        return "verified-read-aloud-plus-near-complete-transcript.v1"
    return "audio-derived-transcript-proxy.v1"


def _unsupported_authority_regions(
    authority_norm: str,
    observed_norm: str,
) -> tuple[str, str, list[str], list[str]]:
    """Expose authority text that no aligned transcript block witnessed."""

    blocks = [
        block
        for block in SequenceMatcher(
            None,
            authority_norm,
            observed_norm,
            autojunk=False,
        ).get_matching_blocks()
        if block.size
    ]
    if not blocks:
        return authority_norm, "", [], []
    borrowed: list[str] = []
    while len(blocks) >= 2:
        last, prev = blocks[-1], blocks[-2]
        observed_gap = last.b - (prev.b + prev.size)
        if (
            last.size <= _BOUNDARY_BORROW_MAX_BLOCK_CHARS
            and observed_gap >= _BOUNDARY_BORROW_MIN_OBSERVED_GAP_CHARS
        ):
            borrowed.append(authority_norm[last.a : last.a + last.size])
            blocks.pop()
            continue
        break
    while len(blocks) >= 2:
        first, second = blocks[0], blocks[1]
        observed_gap = second.b - (first.b + first.size)
        if (
            first.size <= _BOUNDARY_BORROW_MAX_BLOCK_CHARS
            and observed_gap >= _BOUNDARY_BORROW_MIN_OBSERVED_GAP_CHARS
        ):
            borrowed.append(authority_norm[first.a : first.a + first.size])
            blocks.pop(0)
            continue
        break
    head = authority_norm[: blocks[0].a]
    interior: list[str] = []
    prior_end = blocks[0].a + blocks[0].size
    for block in blocks[1:]:
        if block.a > prior_end:
            interior.append(authority_norm[prior_end : block.a])
        prior_end = block.a + block.size
    tail = authority_norm[prior_end:]
    return head, tail, interior, borrowed


def typed_whole_line_support_receipt(
    authority_text: str,
    observed_text: str,
    *,
    score: float,
    coverage: float,
    precision: float,
    common_chars: int,
    support_kind: str,
) -> dict[str, Any]:
    """Build one auditable, fail-closed whole-line transcript receipt.

    Scalar similarity alone is not ownership.  Boundary omissions are
    especially dangerous because a missing negation or discourse prefix can
    still leave excellent aggregate coverage.  Independent transcripts may
    own a whole-line copy only when both the numeric and structural checks
    pass; the current/primary transcript is disclosed but is not independent.
    """

    authority_norm = normalize_chat_text(authority_text)
    observed_norm = normalize_chat_text(observed_text)
    extent = len(observed_norm) / max(1, len(authority_norm))
    required_common = min(6, len(authority_norm))
    (
        unsupported_head,
        unsupported_tail,
        unsupported_interior,
        borrowed_boundary_blocks,
    ) = _unsupported_authority_regions(authority_norm, observed_norm)
    unsupported_interior_chars = sum(len(value) for value in unsupported_interior)
    max_interior_chars = max(
        1,
        int(len(authority_norm) * _WHOLE_LINE_MAX_INTERIOR_GAP_RATIO),
    )
    numeric_near_complete = bool(
        authority_norm
        and coverage >= _WHOLE_LINE_MIN_COVERAGE
        and precision >= _WHOLE_LINE_MIN_PRECISION
        and extent >= _WHOLE_LINE_MIN_EXTENT
        and common_chars >= required_common
    )
    boundary_complete = not unsupported_head and not unsupported_tail
    interior_near_complete = bool(
        all(
            len(value) <= _WHOLE_LINE_MAX_INTERIOR_GAP_CHARS
            for value in unsupported_interior
        )
        and unsupported_interior_chars <= max_interior_chars
    )
    near_complete_transcript = bool(
        numeric_near_complete
        and boundary_complete
        and interior_near_complete
    )
    owner_eligible = bool(
        support_kind == "independent_transcript" and near_complete_transcript
    )
    return {
        "schema_version": "chat-whole-line-support.v1",
        "support_kind": support_kind,
        "candidate_present": bool(observed_norm),
        "observed_text": observed_text,
        "score": round(float(score), 4),
        "coverage": round(float(coverage), 4),
        "precision": round(float(precision), 4),
        "extent": round(extent, 4),
        "common": int(common_chars),
        "common_chars": int(common_chars),
        "required_common_chars": required_common,
        "min_coverage": _WHOLE_LINE_MIN_COVERAGE,
        "min_precision": _WHOLE_LINE_MIN_PRECISION,
        "min_extent": _WHOLE_LINE_MIN_EXTENT,
        "unsupported_authority_head": unsupported_head,
        "unsupported_authority_tail": unsupported_tail,
        "unsupported_authority_interior": unsupported_interior,
        "unsupported_authority_interior_chars": unsupported_interior_chars,
        "borrowed_boundary_blocks_stripped": borrowed_boundary_blocks,
        "max_unsupported_interior_run_chars": (
            _WHOLE_LINE_MAX_INTERIOR_GAP_CHARS
        ),
        "max_unsupported_interior_chars": max_interior_chars,
        "numeric_near_complete": numeric_near_complete,
        "boundary_complete": boundary_complete,
        "interior_near_complete": interior_near_complete,
        "near_complete_transcript": near_complete_transcript,
        "owner_eligible": owner_eligible,
    }


def whole_line_exact_copy_gate(
    item: ChatEvidence,
    proposal: Mapping[str, Any],
    observed_span: str,
    *,
    verdict: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Combine typed transcript receipts, audio proof, and thread exception."""

    primary_receipt = typed_whole_line_support_receipt(
        item.text,
        observed_span,
        score=float(proposal.get("score") or 0.0),
        coverage=float(proposal.get("coverage") or 0.0),
        precision=float(proposal.get("precision") or 0.0),
        common_chars=int(proposal.get("common_chars") or 0),
        support_kind="primary_transcript",
    )
    support_receipts = [
        dict(receipt)
        for receipt in proposal.get("support_receipts") or []
        if isinstance(receipt, Mapping)
    ]
    independent_owner_supports = [
        receipt
        for receipt in support_receipts
        if receipt.get("support_kind") == "independent_transcript"
        and receipt.get("owner_eligible") is True
    ]
    verdict = verdict or {}
    authority_norm = normalize_chat_text(item.text)
    full_span_audio_verdict = bool(
        verdict.get("authority_kind") == "audio_forced_choice"
        and normalize_chat_text(str(verdict.get("heard_syllables") or ""))
        == authority_norm
        and all(
            _valid_sha256(verdict.get(key))
            for key in (
                "source_media_sha256",
                "audio_clip_sha256",
                "prompt_sha256",
                "response_sha256",
            )
        )
    )
    full_span_cpa_witness_verdict = bool(
        verdict.get("authority_kind") == "cpa_witness_adjudication"
        and verdict.get("decision_authority") == "CPA_JUDGE"
        and verdict.get("witness_authority") == "EVIDENCE_ONLY"
        and verdict.get("witness_status") == "OBSERVED"
        and verdict.get("witness_target_audible") is True
        and verdict.get("canonical_entity") == item.text
        and all(
            _valid_sha256(verdict.get(key))
            for key in (
                "witness_request_sha256",
                "witness_source_media_sha256",
                "witness_audio_clip_sha256",
                "witness_prompt_sha256",
                "witness_response_sha256",
                "judge_prompt_sha256",
                "judge_completion_sha256",
            )
        )
    )
    thread_anchored = bool(proposal.get("thread_anchored"))
    if full_span_audio_verdict:
        proof_basis = "hash_bound_full_span_audio_verdict"
    elif full_span_cpa_witness_verdict:
        proof_basis = "hash_bound_candidate_blind_witness_plus_cpa_judge"
    elif thread_anchored:
        proof_basis = "strong_thread_anchor"
    elif independent_owner_supports:
        proof_basis = "near_complete_independent_transcript"
    else:
        proof_basis = "partial_evidence"
    owner_eligible = bool(
        full_span_audio_verdict
        or full_span_cpa_witness_verdict
        or independent_owner_supports
        or thread_anchored
    )
    return {
        "schema_version": "chat-whole-line-support-gate.v1",
        "status": "PASS" if owner_eligible else "BLOCKED_PARTIAL_EVIDENCE",
        "owner_eligible": owner_eligible,
        "proof_basis": proof_basis,
        "full_span_audio_verdict": full_span_audio_verdict,
        "full_span_cpa_witness_verdict": (
            full_span_cpa_witness_verdict
        ),
        "thread_anchored": thread_anchored,
        "independent_owner_support_count": len(independent_owner_supports),
        "independent_supports": support_receipts,
        "primary_transcript": primary_receipt,
        # Keep the primary metrics flat for existing audit consumers.
        "score": primary_receipt["score"],
        "coverage": primary_receipt["coverage"],
        "precision": primary_receipt["precision"],
        "extent": primary_receipt["extent"],
        "common": primary_receipt["common"],
        "common_chars": primary_receipt["common_chars"],
        "required_common_chars": primary_receipt["required_common_chars"],
        "min_coverage": primary_receipt["min_coverage"],
        "min_precision": primary_receipt["min_precision"],
        "min_extent": primary_receipt["min_extent"],
        "transcript_span_supported": primary_receipt[
            "near_complete_transcript"
        ],
        "unsupported_authority_head": primary_receipt[
            "unsupported_authority_head"
        ],
        "unsupported_authority_tail": primary_receipt[
            "unsupported_authority_tail"
        ],
        "unsupported_authority_interior": primary_receipt[
            "unsupported_authority_interior"
        ],
    }


def _whole_line_exact_copy_supported(
    item: ChatEvidence,
    proposal: dict[str, Any],
    near_span: str,
    verdict: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Require near-complete observed speech before copying a whole chat line.

    A contextual/read-aloud verdict proves *which chat event* the host reacted
    to.  It does not prove that every unobserved clause in that event was
    spoken.  In particular, a short ASR span with one matching entity slot must
    never be expanded into the rest of a Super Chat.  Partial cases stay
    unchanged so the independent entity/source-truth paths can repair only
    their explicitly witnessed slots.
    """

    gate = whole_line_exact_copy_gate(
        item,
        proposal,
        near_span,
        verdict=verdict,
    )
    return bool(gate["owner_eligible"]), gate


def _arbitrate_read_aloud_near_match(
    item: ChatEvidence,
    proposal: dict[str, Any],
    *,
    cues: Sequence[Any],
    texts: Sequence[str],
    entity_verifier: EntityVerifier,
    discovery: _ChatProposalDiscoveryLike,
) -> None:
    """Run one CPA-owned closed choice for a bounded near-match."""

    near_span = "".join(texts[proposal["start"] : proposal["start"] + proposal["count"]])
    request = {
        "schema_version": "chat-read-aloud-verification-request.v1",
        "evidence_id": item.evidence_id,
        "kind": item.kind,
        "source": item.source,
        "source_sha256": item.source_sha256,
        "source_event_id": item.source_event_id,
        "source_offset_ms": item.offset_ms,
        "exact_text": item.text,
        "cue_indexes": [
            index + 1
            for index in range(proposal["start"], proposal["start"] + proposal["count"])
        ],
        "matched_start_ms": cues[proposal["start"]].start_ms,
        "matched_end_ms": cues[proposal["start"] + proposal["count"] - 1].end_ms,
        "context_start_ms": max(
            0, cues[proposal["start"]].start_ms - 1_500
        ),
        "context_end_ms": (
            cues[proposal["start"] + proposal["count"] - 1].end_ms
            + 1_500
        ),
        "source_media_timeline_offset_ms": 0,
        "matched_audio_text": near_span,
        "candidate_entities": [
            {"canonical": item.text, "surfaces": [], "readings": []},
            {"canonical": near_span, "surfaces": [], "readings": []},
        ],
        "context_before": texts[proposal["start"] - 1] if proposal["start"] > 0 else "",
        "context_after": texts[proposal["start"] + proposal["count"]]
        if proposal["start"] + proposal["count"] < len(texts)
        else "",
        "reason": "danmaku near-miss read-aloud arbitration",
    }
    request["request_sha256"] = _request_sha256(request)
    try:
        raw_verdict = entity_verifier(request)
    except Exception as exc:  # verifier failure is evidence, never permission
        raw_verdict = {
            "schema_version": "chat-entity-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "UNCERTAIN",
            "reason_code": "READ_ALOUD_VERIFIER_ERROR",
            "error": f"{type(exc).__name__}: {exc}",
        }
    verdict = _validated_read_aloud_verdict(raw_verdict, request=request)
    arbitration_row = {
        "evidence_id": item.evidence_id,
        "kind": item.kind,
        "exact_text": item.text,
        "matched_audio_text": near_span,
        "cue_indexes": request["cue_indexes"],
        "matched_start_ms": request["matched_start_ms"],
        "matched_end_ms": request["matched_end_ms"],
        "score": round(proposal["score"], 4),
        "coverage": round(proposal["coverage"], 4),
        "sender_anchored": bool(proposal.get("sender_anchored")),
        "request_sha256": request["request_sha256"],
        "verdict": verdict if verdict is not None else raw_verdict,
        "owner_eligible": False,
    }
    if verdict is not None and verdict.get("canonical_entity") == item.text:
        whole_line_supported, whole_line_metrics = _whole_line_exact_copy_supported(
            item,
            proposal,
            near_span,
            verdict,
        )
        arbitration_row["whole_line_exact_copy_gate"] = whole_line_metrics
        arbitration_row["owner_eligible"] = whole_line_supported
        if whole_line_supported:
            if whole_line_metrics["full_span_audio_verdict"]:
                arbitration_row["outcome"] = (
                    "authority_confirmed_by_audio"
                )
            elif whole_line_metrics[
                "full_span_cpa_witness_verdict"
            ]:
                arbitration_row["outcome"] = (
                    "authority_confirmed_by_cpa_with_blind_audio_witness"
                )
            else:
                arbitration_row["outcome"] = (
                    "authority_confirmed_by_independent_transcript"
                )
            proposal["mode"] = "exact_span"
            proposal["read_aloud_verdict"] = verdict
            proposal["owner_eligible"] = True
            proposal["whole_line_exact_copy_gate"] = whole_line_metrics
            discovery.proposals.append(proposal)
        else:
            arbitration_row["outcome"] = "partial_evidence_no_whole_line_copy"
            discovery.superseded_chat_proposals.append(
                {
                    "evidence_id": item.evidence_id,
                    "kind": item.kind,
                    "exact_text": item.text,
                    "cue_indexes": request["cue_indexes"],
                    "matched_start_ms": request["matched_start_ms"],
                    "matched_end_ms": request["matched_end_ms"],
                    "matched_audio_text": near_span,
                    "reason_code": (
                        "PARTIAL_CHAT_EVIDENCE_CANNOT_AUTHORIZE_WHOLE_LINE_COPY"
                    ),
                    "whole_line_exact_copy_gate": arbitration_row[
                        "whole_line_exact_copy_gate"
                    ],
                    "allowed_followup": "entity_or_source_truth_slot_only",
                }
            )
    elif verdict is not None:
        cpa_witness = (
            verdict.get("authority_kind")
            == "cpa_witness_adjudication"
        )
        arbitration_row["outcome"] = (
            "current_confirmed_by_cpa_with_blind_audio_witness"
            if cpa_witness
            else "acoustic_span_confirmed_by_audio"
        )
        discovery.superseded_chat_proposals.append(
            {
                "evidence_id": item.evidence_id,
                "kind": item.kind,
                "exact_text": item.text,
                "cue_indexes": request["cue_indexes"],
                "matched_start_ms": request["matched_start_ms"],
                "matched_end_ms": request["matched_end_ms"],
                "reason_code": (
                    "EXACT_CHAT_REJECTED_BY_CPA_WITH_BLIND_AUDIO_WITNESS"
                    if cpa_witness
                    else "EXACT_CHAT_REJECTED_BY_READ_ALOUD_AUDIO"
                ),
            }
        )
    else:
        arbitration_row["outcome"] = "uncertain_no_change"
    discovery.read_aloud_arbitrations.append(arbitration_row)
