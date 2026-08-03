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
