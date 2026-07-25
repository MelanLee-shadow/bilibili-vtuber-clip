"""语境关联召回（2026-07-25 叹十七手案，Ivan 推理的机制化）。

误听面清单永远追不上 ASR 打散专名的新变体（kmx 组 7/14-7/19 已手工追了
19 个 surface）。本模块把「关联词 + 专名已在本片确认 → 句首杂段送音频
强裁」编码为通用召回：清单只是加速器，不再是召回上限。UNCERTAIN 一律
保留原文不阻塞（额外召回拿不准 = 维持现状）；音频确证 canonical 后由
chat_repair 改写（合成杂段绝非合法别名）。"""

from __future__ import annotations

import re
from typing import Any, Sequence

from src.autoslice.chat_evidence import ReferentGroup

_ASSOCIATION_HEAD_STOP_WORDS = ("里面", "这里", "那里", "就是", "里", "的", "是")
_ASSOCIATION_SPAN_RANGE = (2, 6)


def _context_association_occurrences(
    texts: Sequence[str],
    cue_offset: int,
    group: ReferentGroup,
) -> list[dict[str, Any]]:
    """语境关联召回（2026-07-25 叹十七手案，Ivan 推理的机制化）。

    误听面清单追不上 ASR 打散专名的新变体；但「kmx里面的坏熊太多了」被听成
    「叹十七手里面的坏熊太多了」时，语境证据是完整的：cue 里有含语义核字的
    关联词（坏熊↔kimo熊），且专名 canonical 已在本片其他 cue 字面确认。此时
    句首到首个功能词前的 2-6 字杂段成为该组的仲裁候选（音频强裁二选一），
    绝不盲替；UNCERTAIN 由调用方保留原文不阻塞。"""

    if not group.association_core_chars:
        return []
    text = str(texts[cue_offset])
    lowered = text.lower()
    all_surfaces = {
        surface.lower() for entity in group.entities for surface in entity.surfaces
    }
    # 门1：cue 含关联词——以核字为中心的 2 字汉字词，且整词不是组内已知面。
    association_hit = None
    for core in group.association_core_chars:
        for match in re.finditer(re.escape(core), text):
            i = match.start()
            for window in (text[i - 1 : i + 1], text[i : i + 2]):
                if (
                    len(window) == 2
                    and all("一" <= ch <= "鿿" for ch in window)
                    and window.lower() not in all_surfaces
                ):
                    association_hit = i
                    break
            if association_hit is not None:
                break
        if association_hit is not None:
            break
    if association_hit is None:
        return []
    # 门2：组内某 canonical 已在本片其他 cue 字面出现（clip 内确认）。
    confirmed = any(
        entity.canonical.lower() in str(other).lower()
        for index, other in enumerate(texts)
        if index != cue_offset
        for entity in group.entities
    )
    if not confirmed:
        return []
    # 句首杂段：跳过前导语气词/标点，截到首个功能词或关联词前。
    head = 0
    while head < len(text) and text[head] in "哇哦嗯啊唉呀，。？！?!,. ：:":
        head += 1
    cut = association_hit
    for stop in _ASSOCIATION_HEAD_STOP_WORDS:
        pos = lowered.find(stop, head)
        if head < pos < cut:
            cut = pos
    span = text[head:cut]
    lo, hi = _ASSOCIATION_SPAN_RANGE
    if not (lo <= len(span) <= hi):
        return []
    if span.lower() in all_surfaces or any(
        core in span for core in group.association_core_chars
    ):
        return []
    canonical = group.entities[0].canonical
    return [
        {
            "canonical": canonical,
            "surface": span,
            "start": head,
            "end": cut,
            "recall_basis": "context_association",
        }
    ]
