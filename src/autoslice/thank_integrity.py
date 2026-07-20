"""答谢完整性（2026-07-19 十麻乃/快乐猫猫「谢谢消失」案 + 0:23 未答谢礼物案）。

从 chat_repair 拆出的独立域模块（模块行数预算纪律）：
- restore_thank_prefixes：SC 字幕卡行丢答谢动词按 draft 见证还原（T1 见证语义）
- unthanked_donor_disclosure：窗口内未被答谢的送礼人/SC 发送者只披露不改写
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.chat_evidence import normalize_chat_text
from src.autoslice.chat_repair import _sender_thank_anchor, _spoken_sender_alias

_SC_CAPTION_HEAD = re.compile(
    r"^(?P<name>[^，。！？!?：:\s]{1,24}?)的?(?:SC|sc|钢镚|醒目留言)[：:，, ]"
)
_THANK_HEAD_WORDS = ("谢谢", "感谢", "多谢", "谢")


def restore_thank_prefixes(
    final_srt: str, draft_witness_srt: str
) -> tuple[str, dict[str, object]]:
    """SC 字幕卡式行（「<发送者>的SC：…」）丢了她真实说出的「谢谢」时，
    用同时轴 BCUT draft 见证还原前缀（T1 见证语义：draft=source_surface）。

    2026-07-19 案：fresh 重转写把「谢谢快乐猫猫…的SC」渲染成字幕卡
    「快乐猫猫和忧郁小狗的SC：李姐」，答谢动词是口播内容不许丢。draft
    没听到谢-头时保持不动（fail-closed）。"""

    audit: dict[str, object] = {
        "schema_version": "thank-prefix-restore-audit.v1",
        "status": "NO_CHANGE",
        "restorations": [],
    }
    cues = parse_srt_cues(final_srt)
    if not cues:
        return final_srt, audit
    drafts = [cue for cue in parse_srt_cues(draft_witness_srt) if cue.text.strip()]
    texts = [cue.text for cue in cues]
    changed = False
    for index, cue in enumerate(cues):
        text = cue.text
        if not text or any(text.startswith(word) for word in _THANK_HEAD_WORDS):
            continue
        if _SC_CAPTION_HEAD.match(text) is None:
            continue
        witness = None
        for draft in drafts:
            overlap = min(draft.end_ms, cue.end_ms) - max(draft.start_ms, cue.start_ms)
            if overlap < 200:
                continue
            norm = normalize_chat_text(draft.text)
            if norm.startswith(("谢谢", "感谢", "多谢")):
                witness = draft.text
                break
        if witness is None:
            continue
        texts[index] = "谢谢" + text
        changed = True
        audit["restorations"].append(
            {
                "cue_index": index + 1,
                "before": text,
                "after": texts[index],
                "draft_witness": witness[:80],
            }
        )
    if not changed:
        return final_srt, audit
    audit["status"] = "APPLIED"
    return _render_cues_with_texts(cues, texts), audit


def _render_cues_with_texts(cues: Sequence[Any], texts: Sequence[str]) -> str:
    def format_ms(value: int) -> str:
        hours, remainder = divmod(value, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, millis = divmod(remainder, 1_000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

    blocks = [
        f"{index}\n{format_ms(cue.start_ms)} --> {format_ms(cue.end_ms)}\n{text}"
        for index, (cue, text) in enumerate(zip(cues, texts), start=1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def unthanked_donor_disclosure(
    final_srt: str, evidence: Sequence[Any]
) -> dict[str, object]:
    """窗口内送礼/发SC的人在成品字幕里找不到答谢锚点时列入披露。

    只披露不改写（2026-07-19 0:23 案：礼物发送者被平台打码成「快***」，
    答谢句被 ASR 打成乱码「你没错，怎么办」——审片员和音频仲裁拿这份
    名单当「刚刚没念过的送礼人」候选提示）。"""

    thank_cues = [
        cue.text
        for cue in parse_srt_cues(final_srt)
        if cue.text.strip() and ("谢" in cue.text)
    ]
    unthanked: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for item in evidence:
        kind = getattr(item, "kind", "")
        if kind not in ("superchat", "gift"):
            continue
        sender = str(getattr(item, "sender", "") or "")
        alias = _spoken_sender_alias(sender.replace("*", ""))
        matched = False
        for text in thank_cues:
            if _sender_thank_anchor(text, sender):
                matched = True
                break
            if alias and len(alias) >= 2 and alias in normalize_chat_text(text):
                matched = True
                break
        if matched:
            continue
        key = (kind, sender)
        if key in seen:
            continue
        seen.add(key)
        unthanked.append(
            {
                "kind": kind,
                "sender": sender,
                "text": str(getattr(item, "text", ""))[:60],
                "offset_ms": int(getattr(item, "offset_ms", 0) or 0),
            }
        )
    return {
        "schema_version": "unthanked-donor-disclosure.v1",
        "status": "DISCLOSED" if unthanked else "ALL_THANKED",
        "unthanked": unthanked,
    }
