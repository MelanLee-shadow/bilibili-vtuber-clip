"""真值目标 cue 选择（从 source_subtitle_truth 抽出的纯函数簇）。

replace 目标选择带边界擦入豁免（2026-07-25 1573 r12 案），保护/覆盖路径
保持保守 80ms 门；drop 用完整包含判定防真值窗吞真实语音。"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from src.autoslice.jingting_chunker import SrtCue
MIN_CUE_OVERLAP_MS = 80
DROP_CUE_BOUNDARY_EPSILON_MS = 120

# 边界擦入上限：与 DELIVERY_TAIL_PAD_MS 同量级的"有界无内容抖动"常数——
# 网格轮间漂移最多几百 ms；超过它的重叠是实质内容跨 cue，不是坐标残余。
_GRAZING_OVERLAP_MAX_MS = 400

def _overlap_ms(cue: SrtCue, start_ms: int, end_ms: int) -> int:
    return max(0, min(cue.end_ms, end_ms) - max(cue.start_ms, start_ms))


def _target_indexes(
    cues: Sequence[SrtCue],
    windows: Sequence[Mapping[str, object]],
    *,
    grazing_exempt: bool = False,
) -> list[int]:
    # 擦入豁免（2026-07-25 1573 r12 案）：真值窗坐标带落值轮网格，fresh
    # 网格句尾早移时窗尾以 ≥80ms 绝对门擦进邻句——邻句整条被纳入 projection
    # 后成为终验 owner 窗，aggregate 永远多一句。重叠**又小又短**（占比
    # <50% 且绝对值 ≤400ms=既定有界抖动量级）才判边界擦入踢出；实质跨 cue
    # 内容（如 1000ms/33% 的 punct-split 案）保留。drop_cue 走
    # _drop_cue_targets 的完整包含判定，不受此影响。
    matches: list[tuple[int, int]] = []
    for index, cue in enumerate(cues):
        best = 0
        qualified = False
        for window in windows:
            w_start, w_end = int(window["start_ms"]), int(window["end_ms"])
            overlap = _overlap_ms(cue, w_start, w_end)
            best = max(best, overlap)
            if overlap < MIN_CUE_OVERLAP_MS:
                continue
            basis = min(
                max(1, cue.end_ms - cue.start_ms), max(1, w_end - w_start)
            )
            grazing = (
                grazing_exempt
                and overlap * 2 < basis
                and overlap <= _GRAZING_OVERLAP_MAX_MS
            )
            if not grazing:
                qualified = True
        if qualified:
            matches.append((index, best))
    matches.sort(key=lambda row: row[0])
    return [index for index, _overlap in matches]


def _drop_cue_targets(
    cues: Sequence[SrtCue], windows: Sequence[Mapping[str, object]]
) -> tuple[list[int], list[dict[str, Any]]]:
    """Resolve deletions without allowing a truth window to eat real speech.

    A cue may be dropped only when one local truth window contains its complete
    timeline.  The small epsilon absorbs encoder/SRT rounding drift; a cue that
    materially straddles either boundary is a conflict and is left untouched.
    """

    targets: list[int] = []
    conflicts: list[dict[str, Any]] = []
    for index, cue in enumerate(cues):
        overlapping = [
            window
            for window in windows
            if _overlap_ms(cue, int(window["start_ms"]), int(window["end_ms"]))
            >= MIN_CUE_OVERLAP_MS
        ]
        if not overlapping:
            continue
        if any(
            cue.start_ms
            >= int(window["start_ms"]) - DROP_CUE_BOUNDARY_EPSILON_MS
            and cue.end_ms
            <= int(window["end_ms"]) + DROP_CUE_BOUNDARY_EPSILON_MS
            for window in overlapping
        ):
            targets.append(index)
            continue
        conflicts.append(
            {
                "cue_index": index + 1,
                "cue_start_ms": cue.start_ms,
                "cue_end_ms": cue.end_ms,
                "text": cue.text,
                "windows": [
                    {
                        "start_ms": int(window["start_ms"]),
                        "end_ms": int(window["end_ms"]),
                    }
                    for window in overlapping
                ],
            }
        )
    return targets, conflicts
