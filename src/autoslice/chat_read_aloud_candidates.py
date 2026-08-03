"""Read-aloud candidate discovery and independent transcript receipts.

These routines rank evidence spans but do not mutate subtitle text or resolve
entities.  Keeping that boundary explicit lets ``chat_proposals`` focus on
arbitration and application while preserving its compatibility exports.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from typing import Any, Mapping, Sequence

from src.autoslice.chat_evidence import (
    READ_ALOUD_MIN_DELAY_MS,
    ChatEvidence,
    normalize_chat_text,
)
from src.autoslice.chat_repair import (
    _authority_tail_continues_in_next_cue,
    _excess_is_mid_read_interjection,
    _mask_compatible_sender,
    _match_metrics,
    _matched_read_prefix,
    _partial_question_patch,
    _sender_thank_anchor,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.read_aloud_arbitration import typed_whole_line_support_receipt


_LATIN_LETTER_RX = re.compile(r"[A-Za-z]")
_CJK_CHAR_RX = re.compile(r"[一-鿿]")


def _latin_phonetic_ratio(authority_text: str, candidate: str) -> float:
    """去声调拼音 vs 拉丁乱转写的模糊比。

    中文直播流里突现的拉丁 ASR 转写常是「念弹幕被英文化」——字符相似度
    结构性失效。只在候选 cue 拉丁占优时才计算；返回值只用于把候选送进
    声学仲裁（弹幕文本进闭集由 witness+judge 定夺），绝不直接改字。
    """

    letters = _LATIN_LETTER_RX.findall(candidate)
    compact = re.sub(r"\s", "", candidate)
    if len(letters) < 6 or not compact or len(letters) < 0.6 * len(compact):
        return 0.0
    hanzi = "".join(_CJK_CHAR_RX.findall(authority_text))
    if not hanzi:
        return 0.0
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return 0.0
    pin = "".join(lazy_pinyin(hanzi))
    lat = "".join(letter.lower() for letter in letters)
    if not pin:
        return 0.0
    return difflib.SequenceMatcher(a=pin, b=lat, autojunk=False).ratio()


def find_best_read_aloud_candidate(
    item: ChatEvidence,
    *,
    evidence: Sequence[ChatEvidence],
    cues: Sequence[Any],
    texts: Sequence[str],
    max_cues: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Find the safest exact span and the best audio-arbitration fallback."""

    authority_norm = normalize_chat_text(item.text)
    best: dict[str, Any] | None = None
    best_near: dict[str, Any] | None = None
    sender_anchor_indexes: set[int] = (
        {index for index, text in enumerate(texts) if _sender_thank_anchor(text, item.sender)}
        if item.kind == "superchat" and item.sender
        else set()
    )
    # SC 线程延续：观众先用 SC 提问，随后
    # 同一人用普通弹幕接龙（弹幕名被打码，只能掩码兼容+时间窗联结）。
    # 这类弹幕是高先验念读对象——她会直接念出来。
    thread_anchor = bool(
        item.kind == "danmaku"
        and item.sender
        and any(
            other.kind == "superchat"
            and _mask_compatible_sender(other.sender, item.sender)
            and 0 <= item.offset_ms - other.offset_ms <= 600_000
            for other in evidence
        )
    )
    for start in range(len(cues)):
        if item.kind == "danmaku":
            if item.offset_ms >= 0:
                delay = cues[start].start_ms - item.offset_ms
                if delay < READ_ALOUD_MIN_DELAY_MS or delay > 90_000:
                    continue
            elif cues[start].start_ms > 90_000:
                continue
        elif (
            item.offset_ms >= 0 and cues[start].start_ms < item.offset_ms + READ_ALOUD_MIN_DELAY_MS
        ):
            continue
        for count in range(1, min(max_cues, len(cues) - start) + 1):
            candidate_parts = texts[start : start + count]
            candidate = "".join(candidate_parts)
            cue_boundaries: set[int] = set()
            cursor = 0
            for part in candidate_parts[:-1]:
                cursor += len(part)
                cue_boundaries.add(cursor)
            score, ratio, coverage, precision, common = _match_metrics(
                item.text,
                candidate,
            )
            preserved_suffix = _matched_read_prefix(
                item.text,
                candidate,
                cue_boundaries=cue_boundaries,
            )
            if preserved_suffix is not None:
                score, ratio, coverage, precision, common = _match_metrics(
                    item.text,
                    preserved_suffix[0],
                )
            matched_candidate = preserved_suffix[0] if preserved_suffix is not None else candidate
            extent = len(normalize_chat_text(matched_candidate)) / max(
                1,
                len(authority_norm),
            )
            full = (
                len(authority_norm) >= 4
                and score >= 0.68
                and coverage >= 0.60
                and precision >= 0.52
                and extent >= 0.82
                and common >= min(6, len(authority_norm))
            )
            # SC 线程弹幕按念读处理（维护者：这是她念的弹幕，不需要听出来）。
            # 谐音梗让字符相似度结构性失效；线程、时间窗和长度构成直接支持。
            thread_delay_ms = cues[start].start_ms - item.offset_ms if item.offset_ms >= 0 else None
            thread_full = (
                thread_anchor
                and count <= 2
                and len(authority_norm) >= 4
                and thread_delay_ms is not None
                and READ_ALOUD_MIN_DELAY_MS <= thread_delay_ms <= 45_000
                and 0.7 <= extent <= 1.4
                and common >= 2
            )
            if thread_full and not full:
                full = True
            if (
                full
                and len(normalize_chat_text(candidate)) > len(authority_norm) + 1
                and preserved_suffix is None
                and not _excess_is_mid_read_interjection(item.text, candidate)
            ):
                full = False
            partial = None
            if count == 1 and item.kind == "danmaku":
                near = item.offset_ms < 0 or cues[start].start_ms - item.offset_ms <= 20_000
                partial = _partial_question_patch(item.text, candidate) if near else None
            if not full and partial is None:
                # 文本 near-miss 交给原始音频二选一，不放宽 exact-span 本身。
                sender_anchored = bool(
                    item.kind == "superchat"
                    and sender_anchor_indexes
                    and any(0 <= start - index <= 4 for index in sender_anchor_indexes)
                )
                latin_garble_suspect = bool(
                    item.kind == "danmaku"
                    and count <= 2
                    and len(authority_norm) >= 4
                    and _latin_phonetic_ratio(item.text, candidate) >= 0.35
                )
                if latin_garble_suspect or (
                    item.kind == "danmaku"
                    and count <= 2
                    and len(authority_norm) >= 6
                    and score >= 0.55
                    and coverage >= 0.50
                    and common >= 4
                ) or (
                    sender_anchored
                    and count <= 4
                    and len(authority_norm) >= 4
                    and score >= 0.40
                    and coverage >= 0.35
                    and common >= 3
                ):
                    near_candidate = {
                        "evidence": item,
                        "start": start,
                        "count": count,
                        "score": score,
                        "ratio": ratio,
                        "coverage": coverage,
                        "precision": precision,
                        "common_chars": common,
                        "mode": "read_aloud_arbitration",
                        "replacement": None,
                        "preserved_suffix": None,
                        "sender_anchored": sender_anchored,
                        "latin_garble_suspect": latin_garble_suspect,
                    }
                    if best_near is None or (count, -score) < (
                        best_near["count"],
                        -best_near["score"],
                    ):
                        best_near = near_candidate
                continue
            if (
                full
                and start + count < len(cues)
                and _authority_tail_continues_in_next_cue(
                    item.text,
                    candidate,
                    texts[start + count],
                )
            ):
                continue
            proposal = {
                "evidence": item,
                "start": start,
                "count": count,
                "score": score,
                "ratio": ratio,
                "coverage": coverage,
                "precision": precision,
                "common_chars": common,
                "mode": "exact_span" if full else "question_particle_patch",
                "replacement": partial,
                "preserved_suffix": preserved_suffix,
                "thread_anchored": thread_full,
            }
            # The first viable cue span is safest: adding a later cue can
            # consume the beginning of an acoustic reply.
            if best is None or (count, -score) < (best["count"], -best["score"]):
                best = proposal
    return best, best_near


def read_aloud_support_receipts(
    item: ChatEvidence,
    *,
    support_srt_texts: Sequence[str],
    max_cues: int,
) -> list[dict[str, Any]]:
    """Keep one typed whole-line receipt for every independent transcript."""

    support_receipts: list[dict[str, Any]] = []
    for support_index, support_text in enumerate(support_srt_texts):
        support_cues = [cue for cue in parse_srt_cues(support_text) if cue.text.strip()]
        best_support: dict[str, Any] | None = None
        for support_start in range(len(support_cues)):
            if item.kind == "danmaku" and item.offset_ms >= 0:
                delay = support_cues[support_start].start_ms - item.offset_ms
                if delay < READ_ALOUD_MIN_DELAY_MS or delay > 90_000:
                    continue
            for support_count in range(
                1,
                min(max_cues, len(support_cues) - support_start) + 1,
            ):
                support_parts = [
                    cue.text for cue in support_cues[support_start : support_start + support_count]
                ]
                support_candidate = "".join(support_parts)
                cue_boundaries: set[int] = set()
                cursor = 0
                for part in support_parts[:-1]:
                    cursor += len(part)
                    cue_boundaries.add(cursor)
                preserved_suffix = _matched_read_prefix(
                    item.text,
                    support_candidate,
                    cue_boundaries=cue_boundaries,
                )
                variants = [(support_candidate, "")]
                if preserved_suffix is not None:
                    variants.append((preserved_suffix[0], preserved_suffix[1]))
                variant_receipts: list[tuple[dict[str, Any], str]] = []
                for witnessed_candidate, reply_suffix in variants:
                    (
                        support_score,
                        _ratio,
                        support_coverage,
                        support_precision,
                        support_common,
                    ) = _match_metrics(item.text, witnessed_candidate)
                    variant_receipts.append(
                        (
                            typed_whole_line_support_receipt(
                                item.text,
                                witnessed_candidate,
                                score=support_score,
                                coverage=support_coverage,
                                precision=support_precision,
                                common_chars=support_common,
                                support_kind="independent_transcript",
                            ),
                            reply_suffix,
                        )
                    )
                receipt, selected_reply_suffix = max(
                    variant_receipts,
                    key=lambda row: (
                        bool(row[0]["owner_eligible"]),
                        float(row[0]["score"]),
                        float(row[0]["coverage"]),
                        float(row[0]["precision"]),
                    ),
                )
                receipt.update(
                    {
                        "support_index": support_index,
                        "support_sha256": hashlib.sha256(support_text.encode("utf-8")).hexdigest(),
                        "cue_indexes": [
                            index + 1
                            for index in range(
                                support_start,
                                support_start + support_count,
                            )
                        ],
                        "matched_start_ms": support_cues[support_start].start_ms,
                        "matched_end_ms": support_cues[support_start + support_count - 1].end_ms,
                        "preserved_reply_suffix": selected_reply_suffix,
                    }
                )
                receipt["question_particle_patch_supported"] = bool(
                    support_count == 1
                    and item.kind == "danmaku"
                    and _partial_question_patch(
                        item.text,
                        support_candidate,
                    )
                    is not None
                )
                support_coverage_floor = 0.65 if item.kind == "superchat" else 0.72
                receipt["source_truth_anchor_eligible"] = bool(
                    float(receipt["score"]) >= 0.62
                    and float(receipt["coverage"]) >= support_coverage_floor
                    and float(receipt["extent"]) >= 0.82
                    and int(receipt["common"]) >= min(6, len(normalize_chat_text(item.text)))
                )
                rank = (
                    bool(receipt["owner_eligible"]),
                    bool(receipt["question_particle_patch_supported"]),
                    float(receipt["score"]),
                    float(receipt["coverage"]),
                    float(receipt["precision"]),
                    -support_count,
                )
                if best_support is None or rank > best_support["_rank"]:
                    best_support = {**receipt, "_rank": rank}
        if best_support is None:
            best_support = typed_whole_line_support_receipt(
                item.text,
                "",
                score=0.0,
                coverage=0.0,
                precision=0.0,
                common_chars=0,
                support_kind="independent_transcript",
            )
            best_support.update(
                {
                    "support_index": support_index,
                    "support_sha256": hashlib.sha256(support_text.encode("utf-8")).hexdigest(),
                    "cue_indexes": [],
                    "matched_start_ms": None,
                    "matched_end_ms": None,
                    "preserved_reply_suffix": "",
                    "question_particle_patch_supported": False,
                    "source_truth_anchor_eligible": False,
                }
            )
        best_support.pop("_rank", None)
        support_receipts.append(best_support)
    return support_receipts


def read_aloud_support_scores(
    item: ChatEvidence,
    *,
    authority_norm: str,
    support_srt_texts: Sequence[str],
    max_cues: int,
) -> list[float]:
    """Compatibility view for callers that still consume scalar scores."""

    del authority_norm  # Receipt construction normalizes the authority itself.
    return [
        float(receipt["score"])
        for receipt in read_aloud_support_receipts(
            item,
            support_srt_texts=support_srt_texts,
            max_cues=max_cues,
        )
        if receipt.get("owner_eligible") is True
        or receipt.get("question_particle_patch_supported") is True
    ]


def partial_whole_line_rejection(
    item: ChatEvidence,
    proposal: Mapping[str, Any],
    *,
    cues: Sequence[Any],
    matched_audio_text: str,
) -> dict[str, Any]:
    """Describe why partial evidence cannot own a whole subtitle line."""

    return {
        "evidence_id": item.evidence_id,
        "kind": item.kind,
        "exact_text": item.text,
        "cue_indexes": [
            index + 1
            for index in range(
                int(proposal["start"]),
                int(proposal["start"]) + int(proposal["count"]),
            )
        ],
        "matched_start_ms": cues[int(proposal["start"])].start_ms,
        "matched_end_ms": cues[int(proposal["start"]) + int(proposal["count"]) - 1].end_ms,
        "matched_audio_text": matched_audio_text,
        "reason_code": ("PARTIAL_CHAT_EVIDENCE_CANNOT_AUTHORIZE_WHOLE_LINE_COPY"),
        "whole_line_exact_copy_gate": proposal.get("whole_line_exact_copy_gate"),
        "allowed_followup": "entity_or_source_truth_slot_only",
    }
