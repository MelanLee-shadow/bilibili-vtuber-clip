"""Danmaku evidence: blrec raw danmaku as recall signal and subtitle hints.

Most of Li Dousha's talk is reactive — she reads danmaku aloud or riffs on
what's on screen — so the recorded danmaku timeline (blrec
``save_raw_danmaku=true`` → ``<date>/sources/*.xml``) is first-class evidence
three ways:

1. recall: danmaku bursts mark where the audience reacted hardest (the 7/2
   backtest: the shipped clip sat in a burst, and the missed 反沙绕口令 meme
   was the single biggest burst of the session);
2. jingting hints: the burst text is exactly what the streamer is reading
   aloud, so it disambiguates names/memes/homophones during refinement;
3. CPA viewer-context: real danmaku text shows whether a window is
   danmaku-triggered and whether the trigger is inside the clip.

Offsets are milliseconds relative to the recording segment start
(``record_start_time`` in the XML metadata), which matches the source video
timeline used by the full-session selector.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

DANMAKU_EVIDENCE_SCHEMA_VERSION = "danmaku-evidence.v1"

# 选题 prompt 能带多少条弹幕爆发提示（`talk_lane.danmaku_hints()` 用）。
#
# ## 旧值 6 是一条实证成因，不是保守估计
#
# 8/7 那场 660000-690000 的爆发（x22）**检测到了**，但按 count 排在第 7，正好被
# `danmaku_hints()` 里的 `bursts[:6]` 丢掉，选题模型连提示都没看到，于是
# `auto_223750_578_654` 被切在包袱之前（`docs/reviews/2026-08-10-ivan-blind-review-
# tier1-ground-truth.md`）。同一场同一个文件的直方图已经作为回归基线冻结在
# `tests/test_boundary_payoff_extension.py::REAL_DANMAKU_BUCKETS`。
#
# ## 20 的依据：free 上 136 个真实弹幕 XML 全量重放
#
# 2026-06-24 ~ 08-10 全部录制日（`live-streaming/22966160/*/*.xml`，136 个文件、
# 367 条爆发）按本文件的检测算术原样重放，**bucket_ms / min_count / baseline_factor
# 一字未动**，只统计 `find_danmaku_bursts` 到底吐出多少条：
#
#   - blrec 30 分钟分段（n=135，这才是 talk lane 真正的喂入单位）：
#     中位数 2、P90 7、P95 8、**最大 12**；
#   - 另有 1 个 155 分钟整场 XML（B 站 VOD 导入形态）：**19 条**，全样本最大值。
#
# 取 20 = 覆盖观测到的 136 个文件的**全部**爆发，一条不丢。对照：
# 旧的 6 会在 14.7% 的文件上丢掉共 50 条爆发；8 仍会在 4 个文件上丢掉共 18 条。
#
# 代价实测（真实弹幕文本渲染出的整块提示字符数）：中位 86、P95 320、全样本最大 946。
# 同一段字幕 transcript 在 prompt 里约 23000 字符（8/7 22-37-50 实测 788 条 cue），
# 所以提示块占比只从 1.1% 抬到最多 1.4% —— 撑不爆 prompt，也谈不上稀释注意力。
#
# ## 为什么必须保留上限，以及必须**显式传进来**
#
# 30 分钟分段结构上最多 60 个桶、即最多 30 个窗口，但长整场 XML 没有这个天花板，
# 所以名额帽不能取消；超额时按 count 降序保留最强的，丢最弱的。
#
# 更要命的是 `find_danmaku_bursts` 自己的默认 `max_bursts=8` 是**第二道看不见的截断**：
# 只放宽调用方的切片，名额仍会被它按 8 卡死。调用方因此必须把同一个常量显式传成
# `max_bursts=`——这正是本次事故「两道帽子、小的那道藏在被调方」的同型陷阱。
# 本模块自己的默认值保持 8 不动：`run_full_session_selector_cpa_shadow.py` 依赖它。
DANMAKU_HINT_MAX_BURSTS = 20


@dataclass(frozen=True)
class DanmakuItem:
    offset_ms: int
    text: str


@dataclass(frozen=True)
class DanmakuBurst:
    start_ms: int
    end_ms: int
    count: int
    sample_texts: tuple[str, ...]


def parse_blrec_danmaku_xml(xml_text: str) -> list[DanmakuItem]:
    """Parse blrec/bilibili danmaku XML (``<d p="offset_s,...">text</d>``)."""

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise ValueError(f"danmaku XML unparseable: {exc}") from exc
    items: list[DanmakuItem] = []
    for node in root.iter("d"):
        p_attr = node.get("p") or ""
        text = (node.text or "").strip()
        if not text:
            continue
        offset_raw = p_attr.split(",", 1)[0]
        try:
            offset_ms = int(float(offset_raw) * 1000)
        except ValueError:
            continue
        items.append(DanmakuItem(offset_ms=offset_ms, text=text))
    items.sort(key=lambda item: item.offset_ms)
    return items


def load_danmaku_xml(path: Path) -> list[DanmakuItem]:
    return parse_blrec_danmaku_xml(path.read_text(encoding="utf-8", errors="replace"))


def find_danmaku_bursts(
    items: Sequence[DanmakuItem],
    *,
    bucket_ms: int = 30_000,
    min_count: int = 6,
    baseline_factor: float = 2.0,
    max_bursts: int = 8,
    samples_per_burst: int = 5,
) -> list[DanmakuBurst]:
    """Buckets where danmaku density spikes over the session baseline.

    Burst = bucket count >= max(min_count, baseline_factor * median non-empty
    bucket).  Adjacent burst buckets merge into one window.  Returned ordered
    by count descending, capped at ``max_bursts``.
    """

    if not items:
        return []
    counts: dict[int, int] = {}
    for item in items:
        counts[item.offset_ms // bucket_ms] = counts.get(item.offset_ms // bucket_ms, 0) + 1
    non_empty = sorted(counts.values())
    baseline = non_empty[len(non_empty) // 2]
    threshold = max(min_count, baseline * baseline_factor)

    burst_buckets = sorted(bucket for bucket, count in counts.items() if count >= threshold)
    windows: list[list[int]] = []
    for bucket in burst_buckets:
        if windows and bucket == windows[-1][-1] + 1:
            windows[-1].append(bucket)
        else:
            windows.append([bucket])

    bursts: list[DanmakuBurst] = []
    for window in windows:
        start_ms = window[0] * bucket_ms
        end_ms = (window[-1] + 1) * bucket_ms
        in_window = [item for item in items if start_ms <= item.offset_ms < end_ms]
        bursts.append(
            DanmakuBurst(
                start_ms=start_ms,
                end_ms=end_ms,
                count=len(in_window),
                sample_texts=tuple(item.text for item in in_window[:samples_per_burst]),
            )
        )
    bursts.sort(key=lambda burst: burst.count, reverse=True)
    return bursts[:max_bursts]


def danmaku_in_window(
    items: Sequence[DanmakuItem],
    start_ms: int,
    end_ms: int,
    *,
    max_items: int = 40,
) -> list[DanmakuItem]:
    window = [item for item in items if start_ms <= item.offset_ms < end_ms]
    return window[:max_items]


def format_danmaku_lines(items: Sequence[DanmakuItem], *, base_ms: int = 0) -> list[str]:
    """``mm:ss 文本`` lines with offsets rebased to ``base_ms`` (e.g. chunk or
    clip start) for prompt embedding."""

    lines = []
    for item in items:
        rel = max(0, item.offset_ms - base_ms)
        seconds = rel // 1000
        lines.append(f"{seconds // 60:02d}:{seconds % 60:02d} {item.text}")
    return lines
