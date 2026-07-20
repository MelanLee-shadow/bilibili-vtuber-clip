"""近失念读的原始音频强制二选一（从 chat_proposals 拆出的域模块）。

candidate_entities 恒为两个纯文本候选（弹幕原文 vs 声学跨度），交
entity_audio_verifier 黑帧强制选边；任何结局（确认/否决/UNCERTAIN）
都写入 read_aloud_arbitrations 审计——静默出局是 2026-07-20 脑海案
里最贵的病。"""

from __future__ import annotations

from typing import Any, Sequence

from src.autoslice.chat_evidence import (
    ChatEvidence,
    EntityVerifier,
    _request_sha256,
    _validated_read_aloud_verdict,
)


def _arbitrate_read_aloud_near_match(
    item: ChatEvidence,
    proposal: dict[str, Any],
    *,
    cues: Sequence[Any],
    texts: Sequence[str],
    entity_verifier: EntityVerifier,
    discovery: _ChatProposalDiscovery,
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
        arbitration_row["outcome"] = "authority_confirmed_by_audio"
        proposal["mode"] = "exact_span"
        proposal["read_aloud_verdict"] = verdict
        proposal["support_scores"] = []
        discovery.proposals.append(proposal)
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
