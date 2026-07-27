"""Sender-name authority repairs for thanks lines.

平台记录里的发送者名是强权威（Ivan 2026-07-27 复制人名令：「直接复制
人名，根本不用听」）。三条通道：体锚（念了 SC 正文）、舰长守卫锚、
时间锚（只谢不念，1209 唐琳韵案）。歧义绝不落刀，出 verdict。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from src.autoslice.chat_evidence import ChatEvidence, normalize_chat_text
from difflib import SequenceMatcher

from src.autoslice.chat_repair import (
    _THANK_NAME,
    _match_metrics,
    _repair_sc_action_sender,
    _repair_sc_sender,
    _spoken_sender_alias,
)


_GUARD_THANK = re.compile(
    r"(?P<prefix>(?:谢谢|感谢|谢)(?:一下)?(?:刚刚)?)(?P<name>[^，。！？!?\s]{1,32}?)"
    r"的(?P<guard>舰长|提督|总督)",
    re.IGNORECASE,
)
GUARD_THANK_WINDOW_AFTER_MS = 300_000
_GENERIC_GUARD_NAMES = frozenset({"你", "您", "你的", "您的", "大家", "刚刚"})


def _thanks_sender_pinyin_ratio(alias: str, heard: str) -> float:
    """Toneless-pinyin similarity for thanks-name compatibility.

    1209 唐琳韵案（Ivan 2026-07-27 复制人名令）：ASR 把「唐琳韵」听成
    「桃林」，字符共通只有一个「林」（coverage 0.33 过不了字符门），但
    tao-lin vs tang-lin-yun 语音上明显同源。时间锚通道必须能看见读音。
    """

    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return 0.0
    if not alias or not heard:
        return 0.0
    alias_tokens = [str(t).lower() for t in lazy_pinyin(alias)]
    heard_tokens = [str(t).lower() for t in lazy_pinyin(heard)]
    # 字符级拼音串会被字母汤骗（张三丰 vs 唐琳韵 = 0.54）：必须至少共享
    # 一个完整音节，字符率才有资格说话。
    if not set(alias_tokens) & set(heard_tokens):
        return 0.0
    return SequenceMatcher(
        None,
        " ".join(alias_tokens),
        " ".join(heard_tokens),
    ).ratio()


def thanks_name_slot(text: str) -> tuple[str, int, int] | None:
    """Return (heard_name, start, end) of a thanks-line name slot, if any."""

    match = _THANK_NAME.search(text)
    if match is None:
        return None
    return match.group("name"), match.start("name"), match.end("name")


def time_anchored_sender_compatible(sender: str, heard: str) -> bool:
    """Whether a platform sender plausibly IS the heard thanks-name.

    字符门（既有 _sender_thank_anchor 度量）或读音门任一过即兼容；
    两门都不过=她谢的可能根本不是这个事件的人，绝不强改。
    """

    alias = _spoken_sender_alias(sender)
    if not alias or not heard:
        return False
    if alias.lower() == heard.lower():
        return True
    score, _ratio, coverage, _precision, _common = _match_metrics(alias, heard)
    if score >= 0.5 or coverage >= 0.6:
        return True
    return _thanks_sender_pinyin_ratio(alias, heard) >= 0.5


def audit_named_thanks_record_coverage(
    *,
    evidence: Sequence[ChatEvidence],
    cues: Sequence[Any],
    texts: Sequence[str],
    repaired_cue_indexes: set[int],
) -> list[dict[str, Any]]:
    """Disclose every remaining named thanks line's record-channel status.

    自审计面（Ivan 2026-07-27 复盘令：「有记录没用上」不许静默）。三态：
    - RECORD_CHANNEL_ABSENT：窗口内无任何带名事件（录播/备份双缺时才会
      出现——听是唯一通道，诚实说明）；
    - RECORD_PRESENT_NAME_INCOMPATIBLE：有事件但读音/字符两门都不认
      （她谢的可能不是这单，或名字听错得离谱——需要耳裁的真实残余）；
    - 已修的 cue 不出行（修复行自身就是披露）。
    """

    rows: list[dict[str, Any]] = []
    window_events = [
        item
        for item in evidence
        if item.kind in ("superchat", "guard", "gift") and item.sender
    ]
    for index, cue in enumerate(cues):
        if index in repaired_cue_indexes:
            continue
        slot = thanks_name_slot(texts[index])
        if slot is None:
            continue
        heard = slot[0]
        nearby = [
            item
            for item in window_events
            if _TIME_ANCHORED_CAUSAL_FLOOR_MS
            <= cue.start_ms - item.offset_ms
            <= _TIME_ANCHORED_THANKS_WINDOW_MS
        ]
        if not nearby:
            rows.append(
                {
                    "cue_index": index + 1,
                    "heard_name": heard,
                    "status": "RECORD_CHANNEL_ABSENT",
                }
            )
            continue
        compatible = [
            item
            for item in nearby
            if time_anchored_sender_compatible(item.sender, heard)
        ]
        exact = any(
            _spoken_sender_alias(item.sender).lower() == heard.lower()
            for item in nearby
        )
        if exact:
            continue  # 名字已与某事件精确一致
        if not compatible:
            rows.append(
                {
                    "cue_index": index + 1,
                    "heard_name": heard,
                    "status": "RECORD_PRESENT_NAME_INCOMPATIBLE",
                    "nearby_event_senders": sorted(
                        {
                            _spoken_sender_alias(item.sender)
                            for item in nearby
                        }
                    )[:6],
                }
            )
    return rows


def _apply_guard_sender_repairs(
    evidence: Sequence[ChatEvidence], cues: Sequence[Any], texts: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Match GUARD_BUY thanks in a causal 300s window; ambiguity changes nothing."""
    events = [item for item in evidence if item.kind == "guard" and item.sender and item.text]
    repairs: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    used: set[str] = set()
    for index, cue in enumerate(cues):
        match = _GUARD_THANK.search(texts[index])
        if match is None or normalize_chat_text(match["name"]) in _GENERIC_GUARD_NAMES:
            continue
        choices = []
        for item in events:
            delay = cue.start_ms - item.offset_ms
            if item.evidence_id in used or not 2_000 <= delay <= GUARD_THANK_WINDOW_AFTER_MS:
                continue
            if normalize_chat_text(match["guard"]) not in normalize_chat_text(item.text):
                continue
            alias = _spoken_sender_alias(item.sender)
            score, _ratio, coverage, _precision, _common = _match_metrics(alias, match["name"])
            exact = normalize_chat_text(alias) == normalize_chat_text(match["name"])
            strength = 3 if exact else (2 if score >= 0.48 or coverage >= 0.60 else 1)
            choices.append((item, alias, strength, delay))
        if not choices:
            continue
        strong = [row for row in choices if row[2] >= 2]
        pool = strong or choices
        senders = {row[1].lower() for row in pool if row[1]}
        if len(senders) > 1:
            blocked.append(
                {
                    "kind": "guard",
                    "thank_cue_index": index + 1,
                    "matched_start_ms": cue.start_ms,
                    "matched_end_ms": cue.end_ms,
                    "before": texts[index],
                    "heard_sender": match["name"],
                    "guard_name": match["guard"],
                    "reason_code": "GUARD_BUY_SENDER_AMBIGUOUS",
                    "candidate_event_ids": sorted(
                        str(row[0].source_event_id or row[0].evidence_id) for row in pool
                    ),
                    "candidate_spoken_senders": sorted(senders),
                }
            )
            continue
        item, alias, strength, delay = min(pool, key=lambda row: row[3])
        used.add(item.evidence_id)
        if not alias or normalize_chat_text(alias) == normalize_chat_text(match["name"]):
            continue
        before = texts[index]
        after = before[: match.start("name")] + alias + before[match.end("name") :]
        texts[index] = after
        repairs.append(
            {
                "kind": "guard",
                "evidence_id": item.evidence_id,
                "source_event_id": item.source_event_id,
                "sender": item.sender,
                "spoken_sender": alias,
                "heard_sender": match["name"],
                "guard_name": match["guard"],
                "cue_index": index + 1,
                "matched_start_ms": cue.start_ms,
                "matched_end_ms": cue.end_ms,
                "delay_ms": delay,
                "name_match_strength": strength,
                "before": before,
                "after": after,
                "alignment_basis": "guard-buy-plus-thank-action-anchor.v1",
            }
        )
    return repairs, blocked


def _apply_sc_sender_repairs(
    *,
    applied_proposals: Sequence[Mapping[str, Any]],
    evidence: Sequence[ChatEvidence],
    cues: Sequence[Any],
    texts: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Repair only the thank-name slot bound to an already matched SC body."""

    sender_repairs: list[dict[str, Any]] = []
    sender_verdict_required: list[dict[str, Any]] = []
    for proposal in applied_proposals:
        item = proposal["evidence"]
        if item.kind != "superchat" or not item.sender:
            continue
        repair_candidate: tuple[int, str] | None = None
        alignment_basis = "matched-superchat-body-plus-platform-sender.v1"
        for index in range(proposal["start"], proposal["start"] + proposal["count"]):
            repaired = _repair_sc_action_sender(texts[index], item.sender)
            if repaired is not None:
                repair_candidate = (index, repaired)
                alignment_basis = "matched-superchat-body-plus-action-anchor.v1"
                break
        for index in range(proposal["start"] - 1, max(-1, proposal["start"] - 3), -1):
            if repair_candidate is not None:
                break
            if index < 0 or cues[proposal["start"]].start_ms - cues[index].end_ms > 10_000:
                break
            repaired = _repair_sc_sender(texts[index], item.sender)
            if repaired is not None:
                repair_candidate = (index, repaired)
                break
        if repair_candidate is None:
            continue
        index, repaired = repair_candidate
        matched_cue_start = cues[proposal["start"]].start_ms
        same_body_events = [
            other
            for other in evidence
            if other.kind == "superchat"
            and normalize_chat_text(other.text) == normalize_chat_text(item.text)
            and (other.offset_ms < 0 or matched_cue_start >= other.offset_ms - 2_000)
        ]
        event_identities = {
            str(other.source_event_id or other.evidence_id) for other in same_body_events
        }
        spoken_senders = {
            _spoken_sender_alias(other.sender).lower()
            for other in same_body_events
            if _spoken_sender_alias(other.sender)
        }
        if len(event_identities) > 1 and len(spoken_senders) > 1:
            sender_verdict_required.append(
                {
                    "evidence_id": item.evidence_id,
                    "source_event_id": item.source_event_id,
                    "thank_cue_index": index + 1,
                    "cue_indexes": [
                        index + 1
                        for index in range(
                            proposal["start"],
                            proposal["start"] + proposal["count"],
                        )
                    ],
                    "matched_start_ms": cues[proposal["start"]].start_ms,
                    "matched_end_ms": cues[proposal["start"] + proposal["count"] - 1].end_ms,
                    "reason_code": "DUPLICATE_SC_BODY_SENDER_AMBIGUOUS",
                    "candidate_event_ids": sorted(event_identities),
                    "candidate_spoken_senders": sorted(spoken_senders),
                }
            )
            continue
        # The exact matched SC body identifies one concrete platform event;
        # its sender is direct authority for the narrow preceding name slot.
        before_text = texts[index]
        texts[index] = repaired
        sender_repairs.append(
            {
                "evidence_id": item.evidence_id,
                "source_event_id": item.source_event_id,
                "sender": item.sender,
                "spoken_sender": _spoken_sender_alias(item.sender),
                "cue_index": index + 1,
                "matched_start_ms": cues[index].start_ms,
                "matched_end_ms": cues[index].end_ms,
                "before": before_text,
                "after": repaired,
                "alignment_basis": alignment_basis,
            }
        )
    return sender_repairs, sender_verdict_required


_TIME_ANCHORED_THANKS_WINDOW_MS = 90_000
_TIME_ANCHORED_EVENT_LOOKBACK_MS = 60_000
# 因果下界（Ivan 2026-07-27 追问补全，沿用舰长锚/念读锚既有纪律）：
# 事件发生到她看见弹窗并开口至少要 2s——同帧或更早的「感谢」物理不可能
# 是在谢这单，绝不匹配。
_TIME_ANCHORED_CAUSAL_FLOOR_MS = 2_000


def _apply_time_anchored_thanks_sender_repairs(
    *,
    evidence: Sequence[ChatEvidence],
    cues: Sequence[Any],
    texts: list[str],
    repaired_cue_indexes: set[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Copy platform sender names into thanks lines by TIME anchor alone.

    1209 唐琳韵案（Ivan 2026-07-27 复制人名令：「这种东西应该来自弹幕
    记录…直接复制人名，根本不用听」）：她只答谢不念正文时，既有的
    matched-superchat-body 锚失败，具名感谢线全靠硬听。本通道以事件
    时刻为锚：SC/舰长事件后 90s 内的 谢谢X的{钢镚|SC|…} 线，heard 名与
    平台名过字符门或读音门即复制精确名；同窗多个不同 sender 兼容时不
    落刀、出 verdict（绝不猜人）。
    """

    repairs: list[dict[str, Any]] = []
    verdict_required: list[dict[str, Any]] = []
    candidates_by_cue: dict[int, list[ChatEvidence]] = {}
    for item in evidence:
        if item.kind not in ("superchat", "guard") or not item.sender:
            continue
        if item.offset_ms < -_TIME_ANCHORED_EVENT_LOOKBACK_MS:
            continue
        for index, cue in enumerate(cues):
            if index in repaired_cue_indexes:
                continue
            delta = cue.start_ms - item.offset_ms
            if (
                delta < _TIME_ANCHORED_CAUSAL_FLOOR_MS
                or delta > _TIME_ANCHORED_THANKS_WINDOW_MS
            ):
                continue
            slot = thanks_name_slot(texts[index])
            if slot is None:
                continue
            heard = slot[0]
            alias = _spoken_sender_alias(item.sender)
            if alias and alias.lower() == heard.lower():
                continue  # 已经是精确名
            if not time_anchored_sender_compatible(item.sender, heard):
                continue
            candidates_by_cue.setdefault(index, []).append(item)
    for index, items in sorted(candidates_by_cue.items()):
        senders = {
            _spoken_sender_alias(item.sender).lower() for item in items
        }
        if len(senders) > 1:
            verdict_required.append(
                {
                    "thank_cue_index": index + 1,
                    "cue_indexes": [index + 1],
                    "matched_start_ms": cues[index].start_ms,
                    "matched_end_ms": cues[index].end_ms,
                    "reason_code": "TIME_ANCHORED_SENDER_AMBIGUOUS",
                    "candidate_event_ids": sorted(
                        str(item.source_event_id or item.evidence_id)
                        for item in items
                    ),
                    "candidate_spoken_senders": sorted(senders),
                }
            )
            continue
        item = items[0]
        repaired = _repair_sc_sender(texts[index], item.sender)
        if repaired is None:
            continue
        before_text = texts[index]
        texts[index] = repaired
        repaired_cue_indexes.add(index)
        repairs.append(
            {
                "evidence_id": item.evidence_id,
                "source_event_id": item.source_event_id,
                "sender": item.sender,
                "spoken_sender": _spoken_sender_alias(item.sender),
                "cue_index": index + 1,
                "matched_start_ms": cues[index].start_ms,
                "matched_end_ms": cues[index].end_ms,
                "before": before_text,
                "after": repaired,
                "alignment_basis": "time-anchored-platform-sender.v1",
            }
        )
    return repairs, verdict_required


