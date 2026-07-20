"""Cue 文本再分配的断点卫生（从 chat_repair 拆出的无依赖小模块）。

_best_text_split 只按相似度找断点；这里保证断点不切进词中间、不产生
闭标点开头的 cue。总文本不变、cue 数不变、时间轴不动。
"""

from __future__ import annotations


def _shift_boundary_punct(parts: list[str]) -> list[str]:
    """_best_text_split 只按相似度找断点，会切出「，我会打」这种闭标点开头的
    cue；把行首闭/终结标点移回上一段（不动总文本）。"""
    closing = "，,、。；;：:！!？?…”』」》）)"
    out = list(parts)
    for index in range(1, len(out)):
        moved = ""
        while out[index] and out[index][0] in closing:
            moved += out[index][0]
            out[index] = out[index][1:]
        if moved and index >= 1:
            out[index - 1] += moved
    return _snap_split_to_punct(out)


_SNAP_PUNCT = "，,、。；;：:！!？?…"
_SNAP_MAX_CHARS = 2


def _snap_split_to_punct(parts: list[str]) -> list[str]:
    """断点吸附标点（2026-07-19 大叫案）：相似度 DP 会把断点切进词中间
    （「…反应不是很大，大」|「叫“李姐是侄女”」把「大叫」拆开）。上一段
    末尾不是标点、但 ≤2 字内有标点时，把标点后的尾字挪到下一段开头——
    完整的词该在完整的 cue 里。总文本不变、cue 数不变。"""

    out = list(parts)
    for index in range(1, len(out)):
        prev = out[index - 1]
        if not prev or prev[-1] in _SNAP_PUNCT:
            continue
        for distance in range(1, _SNAP_MAX_CHARS + 1):
            if len(prev) <= distance + 1:
                break
            if prev[-distance - 1] in _SNAP_PUNCT:
                out[index] = prev[-distance:] + out[index]
                out[index - 1] = prev[: -distance]
                break
    return out
