"""近失念读的原始音频强制二选一（从 chat_proposals 拆出的域模块）。

candidate_entities 恒为两个纯文本候选（弹幕原文 vs 声学跨度），交
entity_audio_verifier 黑帧强制选边；任何结局（确认/否决/UNCERTAIN）
都写入 read_aloud_arbitrations 审计——静默出局是 2026-07-20 脑海案
里最贵的病。"""

from __future__ import annotations

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
    if isinstance(read_aloud, Mapping):
        if read_aloud.get("authority_kind") == "audio_forced_choice":
            return "raw-audio-forced-choice.v1"
        if read_aloud.get("reason_code") == "READ_ALOUD_CONFIRMED_BY_CONTEXT":
            return "structured-chat-context-plus-near-complete-transcript.v1"
        return "verified-read-aloud-plus-near-complete-transcript.v1"
    return "audio-derived-transcript-proxy.v1"


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

    authority_norm = normalize_chat_text(item.text)
    near_norm = normalize_chat_text(near_span)
    coverage = float(proposal.get("coverage") or 0.0)
    precision = float(proposal.get("precision") or 0.0)
    common_chars = int(proposal.get("common_chars") or 0)
    extent = len(near_norm) / max(1, len(authority_norm))
    required_common = min(6, len(authority_norm))
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
    transcript_span_supported = bool(
        authority_norm
        and coverage >= _WHOLE_LINE_MIN_COVERAGE
        and precision >= _WHOLE_LINE_MIN_PRECISION
        and extent >= _WHOLE_LINE_MIN_EXTENT
        and common_chars >= required_common
    )
    metrics = {
        "coverage": round(coverage, 4),
        "precision": round(precision, 4),
        "extent": round(extent, 4),
        "common_chars": common_chars,
        "required_common_chars": required_common,
        "min_coverage": _WHOLE_LINE_MIN_COVERAGE,
        "min_precision": _WHOLE_LINE_MIN_PRECISION,
        "min_extent": _WHOLE_LINE_MIN_EXTENT,
        "transcript_span_supported": transcript_span_supported,
        "full_span_audio_verdict": full_span_audio_verdict,
        "proof_basis": (
            "hash_bound_full_span_audio_verdict"
            if full_span_audio_verdict
            else (
                "near_complete_transcript_span"
                if transcript_span_supported
                else "partial_evidence"
            )
        ),
    }
    supported = transcript_span_supported or full_span_audio_verdict
    return supported, metrics


def _arbitrate_read_aloud_near_match(
    item: ChatEvidence,
    proposal: dict[str, Any],
    *,
    cues: Sequence[Any],
    texts: Sequence[str],
    entity_verifier: EntityVerifier,
    discovery: _ChatProposalDiscoveryLike,
) -> None:
    """Run one raw-audio forced choice for a bounded near-match."""

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
    }
    if verdict is not None and verdict.get("canonical_entity") == item.text:
        whole_line_supported, whole_line_metrics = _whole_line_exact_copy_supported(
            item,
            proposal,
            near_span,
            verdict,
        )
        arbitration_row["whole_line_exact_copy_gate"] = {
            **whole_line_metrics,
            "status": "PASS" if whole_line_supported else "BLOCKED_PARTIAL_EVIDENCE",
        }
        if whole_line_supported:
            arbitration_row["outcome"] = "authority_confirmed_by_audio"
            proposal["mode"] = "exact_span"
            proposal["read_aloud_verdict"] = verdict
            proposal["support_scores"] = []
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
        arbitration_row["outcome"] = "acoustic_span_confirmed_by_audio"
        discovery.superseded_chat_proposals.append(
            {
                "evidence_id": item.evidence_id,
                "kind": item.kind,
                "exact_text": item.text,
                "cue_indexes": request["cue_indexes"],
                "matched_start_ms": request["matched_start_ms"],
                "matched_end_ms": request["matched_end_ms"],
                "reason_code": "EXACT_CHAT_REJECTED_BY_READ_ALOUD_AUDIO",
            }
        )
    else:
        arbitration_row["outcome"] = "uncertain_no_change"
    discovery.read_aloud_arbitrations.append(arbitration_row)
