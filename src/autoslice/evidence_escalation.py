"""Evidence escalation: upgrade provenance-less proposals from records.

公开规则两道令（424_522 1:24「战斗回合用尽」案 + 同日扩展）：
- 触发器 = 听不清（弱证词）∪ 语境不通（审片员立了 finding 且提案无出处）；
- 查证顺序 = 先弹幕池（结构化记录，零成本，structured_chat_bound 出处）
 → 再看两帧画面（OCR 文本池，verified_ocr 出处）。

两池都按拼音与听写对齐挑选；嵌入式念读（「大家说的都是〈原文〉」）用
逐字拼音滑窗在 cue 内定位念读跨度后拼接提案。升级只替换「提案+出处」，
裁决仍由同一 witness-judge 引擎完成——记录供候选，永不直改。非空但无法
拼音对齐或拼接的屏幕池保留为 EVIDENCE_ONLY，供 CPA 复核但不授予正字权威。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from src.autoslice.screen_read_witness import (
    danmaku_text_pool,
    locate_read_span,
    match_screen_read,
    witness_is_weak,
)

_MIN_SPLICE_SPAN_RATIO = 0.55


def _escalation_finding_upgrade(
    srt_text: str,
    finding: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    matched_text: str,
    provenance: dict[str, Any],
    why: str,
    clip_context: Mapping[str, object] | None,
    source_media_timeline_offset_ms: int,
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    from src.autoslice.final_review_auditor import (
        build_context_adjudication_request,
    )

    current_cue = str(request.get("current_cue") or "")
    if not matched_text or matched_text == current_cue:
        return None
    located = locate_read_span(current_cue, matched_text)
    if located is None:
        return None
    span_start, span_end, span_ratio = located
    if span_ratio < _MIN_SPLICE_SPAN_RATIO:
        return None
    suspect = current_cue[span_start:span_end]
    if not suspect or suspect == matched_text:
        return None
    proposed_full_cue = (
        current_cue[:span_start] + matched_text + current_cue[span_end:]
    )
    upgraded_finding: dict[str, Any] = {
        **finding,
        "suspect": suspect,
        "suggestion": matched_text,
        "proposed_full_cue": proposed_full_cue,
        "repair_class": "source_backed_entity",
        "candidate_provenance": provenance,
        "why": why,
    }
    try:
        upgraded_request = build_context_adjudication_request(
            srt_text,
            upgraded_finding,
            clip_context=clip_context,
            source_media_timeline_offset_ms=source_media_timeline_offset_ms,
        )
    except (TypeError, ValueError):
        return None
    return upgraded_finding, upgraded_request


def _screen_read_evidence_only_upgrade(
    srt_text: str,
    finding: Mapping[str, Any],
    *,
    clip_context: Mapping[str, object] | None,
    source_media_timeline_offset_ms: int,
    probe_result: Mapping[str, Any],
    screen_match: Mapping[str, Any] | None,
    reason_code: str,
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    """Bind an actual screen probe as evidence without granting text authority.

    A frame pool that cannot be aligned to the blind hearing is still useful to
    the CPA prompt, but it is not a verified spelling source.  Keep the
    original CURRENT/PROPOSED finding intact and make the non-authoritative
    status explicit so this path cannot silently become an OCR mutation.
    """

    from src.autoslice.final_review_auditor import (
        build_context_adjudication_request,
    )

    prior_provenance = finding.get("candidate_provenance")
    if isinstance(prior_provenance, Mapping):
        provenance: dict[str, Any] = dict(prior_provenance)
    else:
        provenance = {
            "kind": "screen_read_evidence",
            "mutation_authorized": False,
        }
    provenance["mutation_authorized"] = False

    pool = list(probe_result.get("pool") or [])
    receipts = [
        dict(row) if isinstance(row, Mapping) else row
        for row in list(probe_result.get("receipts") or [])
    ]
    evidence: dict[str, Any] = {
        "schema_version": "screen-read-evidence-only.v1",
        "status": "EVIDENCE_ONLY",
        "witness_authority": "EVIDENCE_ONLY",
        "mutation_authorized": False,
        "reason_code": reason_code,
        "probe_schema_version": probe_result.get("schema_version"),
        "media_path": probe_result.get("media_path"),
        "span_start_ms": probe_result.get("span_start_ms"),
        "span_end_ms": probe_result.get("span_end_ms"),
        "frame_ms": list(probe_result.get("frame_ms") or []),
        "pool": pool,
        "receipts": receipts,
        "match": dict(screen_match) if isinstance(screen_match, Mapping) else None,
    }
    provenance["screen_read_evidence"] = evidence
    upgraded_finding: dict[str, Any] = {
        **finding,
        "candidate_provenance": provenance,
    }
    try:
        upgraded_request = build_context_adjudication_request(
            srt_text,
            upgraded_finding,
            clip_context=clip_context,
            source_media_timeline_offset_ms=source_media_timeline_offset_ms,
        )
    except (TypeError, ValueError):
        return None
    return upgraded_finding, upgraded_request


def evidence_escalation_upgrade(
    srt_text: str,
    finding: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    verdict: Mapping[str, Any],
    screen_probe: Callable[[int, int], Mapping[str, Any]] | None,
    clip_context: Mapping[str, object] | None,
    source_media_timeline_offset_ms: int,
) -> tuple[Mapping[str, Any], Mapping[str, Any], dict[str, Any]] | None:
    """先弹幕池后画面：把无出处提案升级为记录背书的提案。"""

    heard = str(verdict.get("heard_pinyin") or "")
    audit: dict[str, Any] = {
        "schema_version": "evidence-escalation-upgrade.v1",
        "weak_witness": witness_is_weak(verdict),
    }

    chat_pool = danmaku_text_pool(clip_context)
    audit["danmaku_pool_size"] = len(chat_pool)
    chat_match = match_screen_read(heard_pinyin=heard, pool=chat_pool)
    if chat_match is not None:
        audit["source"] = "danmaku_pool"
        audit["match"] = chat_match
        upgraded = _escalation_finding_upgrade(
            srt_text,
            finding,
            request=request,
            matched_text=str(chat_match["text"]),
            provenance={
                "kind": "structured_chat_bound",
                "surface": str(chat_match["text"]),
                "pinyin_ratio": chat_match["pinyin_ratio"],
            },
            why=(
                f"[证据升级·弹幕] 听写拼音相似 {chat_match['pinyin_ratio']} "
                "对齐结构化弹幕原文；structured_chat_bound 出处进入裁决"
            ),
            clip_context=clip_context,
            source_media_timeline_offset_ms=source_media_timeline_offset_ms,
        )
        if upgraded is not None:
            return upgraded[0], upgraded[1], audit

    if screen_probe is None:
        return None
    try:
        span_start = (
            int(request["matched_start_ms"]) + source_media_timeline_offset_ms
        )
        span_end = (
            int(request["matched_end_ms"]) + source_media_timeline_offset_ms
        )
        probe_result = dict(screen_probe(span_start, span_end))
    except Exception:  # noqa: BLE001 — 视觉失败绝不拖垮声学链
        return None
    screen_pool = list(probe_result.get("pool") or [])
    screen_match = match_screen_read(heard_pinyin=heard, pool=screen_pool)
    audit.update(
        {
            "source": "screen_frames",
            "frame_ms": probe_result.get("frame_ms"),
            "screen_pool_size": len(screen_pool),
            "match": screen_match,
        }
    )
    if screen_match is None:
        if screen_pool:
            evidence_only = _screen_read_evidence_only_upgrade(
                srt_text,
                finding,
                clip_context=clip_context,
                source_media_timeline_offset_ms=source_media_timeline_offset_ms,
                probe_result=probe_result,
                screen_match=None,
                reason_code="SCREEN_POOL_NO_PINYIN_MATCH",
            )
            if evidence_only is not None:
                evidence = evidence_only[0]["candidate_provenance"][
                    "screen_read_evidence"
                ]
                audit["evidence_only"] = evidence
                audit["mutation_authorized"] = False
                return evidence_only[0], evidence_only[1], audit
        return None
    upgraded = _escalation_finding_upgrade(
        srt_text,
        finding,
        request=request,
        matched_text=str(screen_match["text"]),
        provenance={
            "kind": "verified_ocr",
            "surface": str(screen_match["text"]),
            "screen_read": {
                key: probe_result.get(key)
                for key in ("media_path", "frame_ms")
            },
            "pinyin_ratio": screen_match["pinyin_ratio"],
        },
        why=(
            f"[证据升级·读屏] 听写拼音相似 {screen_match['pinyin_ratio']} "
            "对齐帧内文本；verified_ocr 出处进入裁决"
        ),
        clip_context=clip_context,
        source_media_timeline_offset_ms=source_media_timeline_offset_ms,
    )
    if upgraded is None:
        if screen_pool:
            evidence_only = _screen_read_evidence_only_upgrade(
                srt_text,
                finding,
                clip_context=clip_context,
                source_media_timeline_offset_ms=source_media_timeline_offset_ms,
                probe_result=probe_result,
                screen_match=screen_match,
                reason_code="SCREEN_MATCH_NOT_SPLICEABLE",
            )
            if evidence_only is not None:
                evidence = evidence_only[0]["candidate_provenance"][
                    "screen_read_evidence"
                ]
                audit["evidence_only"] = evidence
                audit["mutation_authorized"] = False
                return evidence_only[0], evidence_only[1], audit
        return None
    return upgraded[0], upgraded[1], audit


__all__ = ["evidence_escalation_upgrade"]
