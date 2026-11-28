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

import json
import re
import statistics
import xml.etree.ElementTree as ElementTree
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence


@dataclass(frozen=True)
class DanmakuItem:
    offset_ms: int
    text: str
    # sent for the viewer by the platform (info[0][9] of the recorder's raw message: 1 a storm, 2 a lottery 口令), not typed
    machine: bool = False
    # who sent it: the 7th field of `p` (the recorder's uid; a hash in other files), "" when the file has none
    user: str = ""


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
        fields = p_attr.split(",")
        items.append(DanmakuItem(offset_ms=offset_ms, text=text, machine=_sent_by_platform(node.get("raw")),
                                 user=fields[6].strip() if len(fields) > 6 else ""))
    items.sort(key=lambda item: item.offset_ms)
    return items


def _sent_by_platform(raw: str | None) -> bool:
    """Whether the recorder's raw message (`raw`, the live room's DANMU_MSG `info`) marks the danmaku as one the platform
    sent for the viewer: info[0][9] is 0 for a typed one, 1 for a 节奏风暴 and 2 for a 天选时刻 lottery 口令."""
    if not raw:
        return False
    try:
        kind = json.loads(raw)[0][9]
    except (ValueError, IndexError, TypeError, KeyError):
        return False
    return kind not in (0, None)


def load_danmaku_xml(path: Path, *, source_bytes: bytes | None = None) -> list[DanmakuItem]:
    payload = path.read_bytes() if source_bytes is None else source_bytes
    return parse_blrec_danmaku_xml(payload.decode("utf-8", errors="replace"))


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


BURST_HINT_AFTER_MS = 60_000        # 样本往突发结束后多取一分钟：解释梗的弹幕比那一刻晚到
BURST_HINT_SAMPLES = 10
BURST_HINT_REPEATS = 5              # 刷得最多的几句（同一句至少两条）
# 手推线选片的突发区（频道的要求）：
# 每半分钟的条数和**这一场直播自己**比（`session_density`）。不跟本段录像自己比：21:30 那段从头到尾都热，和本段中位数比
# 一个突发区也没有；也不用绝对条数：开播人数不同的场不能用同一个数。门槛是这场基线（全场各段录像里有弹幕的半分钟条数的中位数）的
# SESSION_BURST_FACTOR 倍，但不高于这场最密的第 SESSION_DENSEST_HALF_MINUTES 个半分钟，且至少 SESSION_BURST_MIN_COUNT 条。
# 倍数取 3：11-09 至 11-25 十二场，2 倍平均每场 24 个、2.5 倍 14 个、3 倍 7.4 个、4 倍 2.3 个；已发片的播放在哪一档都没有显著差别
# （48 条里落在突发区的和不落的，几何均值比 0.75–1.21，|t|<0.9），所以 3 倍是按每场要模型逐个交代的个数定的。
# 只按倍数，本来就热闹的场几乎一个也不剩（11-24 基线 10、门槛 30，三个半小时只有 2 个，21:59 那段 28、27、26、25 条的半分钟都不算），
# 而他要的是“这场里最密的时段”；所以每场至少有它最密的 8 个半分钟：十二场里每场至少 6 个突发区、平均 9.8 个，11-24 门槛降到 25、
# 有 6 个。8 也不是播放证明出来的（6、8、10、12 个时已发片的几何均值比 1.20、1.11、1.03、1.11，都不显著），取的是让最热闹的场
# 也有和普通场差不多个数的突发区。
SESSION_BURST_FACTOR = 3.0
SESSION_DENSEST_HALF_MINUTES = 8
SESSION_BURST_MIN_COUNT = 6
# 两路影子（只记下、不给模型、不进排座，20-selection.md 链路 2「影子」）：同一个门槛规则，只换数法——数人（同一位观众半分钟里
# 只算一次，刷屏的少数人顶不起一个突发区），和只数笑的弹幕（LAUGH）。词表和规则 冻结，只在之后的场次上评估；
# 这组笑的词是看过 11-24 21:33 之后定的，不能拿 11-25 以前的场次证明它有用。
LAUGH = re.compile(r"哈{2,}|笑死|绷不住|大笑|233")


def laughs(item: DanmakuItem) -> bool:
    """A danmaku that laughs (`LAUGH`, the [xx_大笑] emote included)."""
    return bool(LAUGH.search(item.text))


# 观众的反应比那一下晚到（解梗、笑的弹幕晚 5–60 秒）：一条候选结束在突发区开始前这么久以内，也算这个突发区落在它里面
BURST_LEAD_MS = 60_000


@dataclass(frozen=True)
class SessionDensity:
    """A session's danmaku density: its baseline (the median half minute that has any counted danmaku) and what a half
    minute of it needs to be a burst."""
    baseline: float
    threshold: float


def reaction_buckets(items: Iterable[DanmakuItem], counted: Callable[[DanmakuItem], bool], *,
                     bucket_ms: int = 30_000, per_user: bool = False) -> Counter[int]:
    """How many counted danmaku fall in each `bucket_ms` of the recording (offset // bucket_ms -> count); `per_user`: how
    many viewers sent one (a viewer who sends ten in the half minute counts once; one with no `user` counts each time)."""
    if not per_user:
        return Counter(item.offset_ms // bucket_ms for item in items if counted(item))
    seen = {(item.offset_ms // bucket_ms, item.user or f"#{index}") for index, item in enumerate(items) if counted(item)}
    return Counter(bucket for bucket, _ in seen)


def session_density(recordings: Iterable[Sequence[DanmakuItem]], counted: Callable[[DanmakuItem], bool], *,
                    bucket_ms: int = 30_000, per_user: bool = False) -> SessionDensity | None:
    """The session's own density over every recording of it: the baseline, and the burst threshold -- `SESSION_BURST_FACTOR`
    times the baseline, at most the session's `SESSION_DENSEST_HALF_MINUTES`-th densest half minute (its densest are
    always given), at least `SESSION_BURST_MIN_COUNT`.  None when no recording has a counted danmaku."""
    counts = [n for items in recordings for n in reaction_buckets(items, counted, bucket_ms=bucket_ms, per_user=per_user).values()]
    if not counts:
        return None
    baseline = float(statistics.median(counts))
    densest = sorted(counts, reverse=True)[min(SESSION_DENSEST_HALF_MINUTES, len(counts)) - 1]
    return SessionDensity(baseline=baseline,
                          threshold=float(max(SESSION_BURST_MIN_COUNT, min(SESSION_BURST_FACTOR * baseline, densest))))


def find_session_bursts(
    items: Sequence[DanmakuItem],
    *,
    threshold: float,
    counted: Callable[[DanmakuItem], bool],
    bucket_ms: int = 30_000,
    per_user: bool = False,
) -> list[DanmakuBurst]:
    """The bursts of one recording against the session's threshold (`session_density`): half minutes with at least
    `threshold` counted danmaku, adjacent ones merged, every one of them, in time order.  `count` is the counted danmaku of
    the window; the sample is left to `burst_hint_lines`."""
    counts = reaction_buckets(items, counted, bucket_ms=bucket_ms, per_user=per_user)
    windows: list[list[int]] = []
    for bucket in sorted(b for b, n in counts.items() if n >= threshold):
        if windows and bucket == windows[-1][-1] + 1:
            windows[-1].append(bucket)
        else:
            windows.append([bucket])
    return [DanmakuBurst(start_ms=w[0] * bucket_ms, end_ms=(w[-1] + 1) * bucket_ms, count=sum(counts[b] for b in w),
                         sample_texts=()) for w in windows]


def _says_something(text: str) -> bool:
    """不是纯表情（[xx]）、不是单个字符或同一个字符刷屏（哈哈哈、？？？、111）。"""
    text = text.strip()
    return len(text) >= 2 and not (text.startswith("[") and text.endswith("]")) and len(set(text)) > 1


def burst_hint_lines(
    items: Sequence[DanmakuItem],
    bursts: Sequence[DanmakuBurst],
    *,
    after_ms: int = BURST_HINT_AFTER_MS,
    samples: int = BURST_HINT_SAMPLES,
    baseline: float | None = None,
) -> list[tuple[str, DanmakuBurst, str]]:
    """选题提示里的突发区：按时间排、编号 B1…，每个给 (编号, 突发区, 一行文字)。

    `find_danmaku_bursts` 的 `sample_texts` 是时段内头几条，两个被同行切走的时刻都栽在这上面：
    20:06 那个突发区的头 5 条是「太对了／看看实力…」，说破梗的「矿工是老公属性」晚 3 秒才到；21:06 那个的头 5 条是
    「矮人（／小矮人…」，「捡破烂的绿色就是ntnt」排在第 20 条。这里的样本改从突发开始到结束后 `after_ms` 里取：
    去掉纯表情和刷屏、去重，留最长的 `samples` 条，按时间排。时间写成和字幕时间轴一样的 分:秒。
    同一段里刷得最多的几句（`BURST_HINT_REPEATS`，表情也算，至少两条）另列出条数：观众在笑、在哭、在刷同一个梗，一眼看得出。
    给了 `baseline`（这场每半分钟的基线）时写出它，模型看得出这一段比平时密几倍。
    """

    def clock(ms: int) -> str:
        return f"{ms // 60000:02d}:{ms // 1000 % 60:02d}"

    lines = []
    for number, burst in enumerate(sorted(bursts, key=lambda b: (b.start_ms, b.end_ms)), start=1):
        seen: dict[str, DanmakuItem] = {}
        said: Counter[str] = Counter()
        for item in items:
            if burst.start_ms <= item.offset_ms < burst.end_ms + after_ms and not item.machine:
                said[item.text.strip()] += 1
                if _says_something(item.text):
                    seen.setdefault(item.text.strip(), item)
        kept = sorted(sorted(seen.values(), key=lambda item: -len(item.text.strip()))[:samples], key=lambda item: item.offset_ms)
        sample = " / ".join(f"{clock(item.offset_ms)} {item.text.strip()}" for item in kept)
        repeats = "、".join(f"{text}×{n}" for text, n in said.most_common(BURST_HINT_REPEATS) if n >= 2)
        head = f"B{number} [{clock(burst.start_ms)}-{clock(burst.end_ms)}] {burst.count} 条"
        if baseline is not None:
            head += f"（这场平时每半分钟 {baseline:g} 条）"
        lines.append((f"B{number}", burst, head + (f"；刷得最多：{repeats}；说了什么：" if repeats else "：") + sample))
    return lines


