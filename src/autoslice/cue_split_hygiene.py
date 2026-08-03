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
    """断点吸附标点：相似度 DP 会把断点切进词中间
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


def merge_release_grade_cues(
    srt_text: str,
    *,
    min_cue_ms: int = 300,
    contiguous_gap_ms: int = 150,
    max_rounds: int = 3,
):
    """Merge release-validator-rejected slivers into contiguous neighbors.

    1863 案：真实语音的 240ms「哦」与独立单字「行」被发布级
    校验拒（SRT_CUE_TOO_SHORT / SRT_SINGLE_CJK_CHARACTER），生产端却放行——
    包永远到不了发布级。本合并器**只**消费校验器自己的判决块（秦秦/秦
    名回声等豁免自动保留），把被拒 cue 并进贴邻邻居（间隙≤150ms，取更近
    侧；两侧都不贴邻则保持原样并披露）。中文侧无缝拼接，边界无标点时补
    「，」。返回 (srt_text, report_rows)。
    """

    import re as _re

    from src.autoslice.jingting_chunker import parse_srt_cues
    from src.autoslice.subtitle_validation import validate_srt_text

    def _render(cues, texts):
        def _ms(value):
            hours, rem = divmod(int(value), 3_600_000)
            minutes, rem = divmod(rem, 60_000)
            seconds, millis = divmod(rem, 1_000)
            return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

        out = []
        index = 0
        for cue, text in zip(cues, texts):
            if not text.strip():
                continue
            index += 1
            out.append(f"{index}\n{_ms(cue[0])} --> {_ms(cue[1])}\n{text}\n")
        return "\n".join(out)

    def _join(left: str, right: str) -> str:
        if not left:
            return right
        if not right:
            return left
        if _re.search(r"[，。！？!?,、…~—]$", left) or _re.search(
            r"^[，。！？!?,、…~—]", right
        ):
            return left + right
        return left + "，" + right

    rows = []
    current = srt_text
    for _round in range(max_rounds):
        verdict = validate_srt_text(current, min_cue_ms=min_cue_ms)
        bad_blocks = {
            int(err["block"])
            for err in verdict.get("errors") or []
            if err.get("code")
            in ("SRT_CUE_TOO_SHORT", "SRT_SINGLE_CJK_CHARACTER")
        }
        if not bad_blocks:
            break
        parsed = [
            ((cue.start_ms, cue.end_ms), cue.text)
            for cue in parse_srt_cues(current)
            if cue.text.strip()
        ]
        cues = [row[0] for row in parsed]
        texts = [row[1] for row in parsed]
        merged_any = False
        for position in range(len(cues)):
            block_number = position + 1
            if block_number not in bad_blocks or not texts[position].strip():
                continue
            gap_prev = (
                cues[position][0] - cues[position - 1][1]
                if position > 0 and texts[position - 1].strip()
                else None
            )
            gap_next = (
                cues[position + 1][0] - cues[position][1]
                if position + 1 < len(cues) and texts[position + 1].strip()
                else None
            )
            candidates = [
                (gap, side)
                for gap, side in ((gap_prev, "prev"), (gap_next, "next"))
                if gap is not None and 0 <= gap <= contiguous_gap_ms
            ]
            if not candidates:
                rows.append(
                    {
                        "block": block_number,
                        "text": texts[position],
                        "action": "UNMERGEABLE_NOT_CONTIGUOUS",
                    }
                )
                continue
            _gap, side = min(candidates)
            if side == "prev":
                target = position - 1
                texts[target] = _join(texts[target], texts[position])
                cues[target] = (cues[target][0], cues[position][1])
            else:
                target = position + 1
                texts[target] = _join(texts[position], texts[target])
                cues[target] = (cues[position][0], cues[target][1])
            rows.append(
                {
                    "block": block_number,
                    "text": texts[position],
                    "action": f"MERGED_INTO_{side.upper()}",
                }
            )
            texts[position] = ""
            merged_any = True
        if not merged_any:
            break
        current = _render(cues, texts)
    return current, rows
