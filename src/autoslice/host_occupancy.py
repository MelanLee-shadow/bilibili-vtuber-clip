"""候选级 Target Host Occupancy Estimator + 两段式争席次序。

ChatGPT Pro 的方案（内部证据留存·chatgpt-pro-
ordering-and-rubric.txt`，维护者 已批准）解决的是这个次序矛盾：`prioritize()` 在
说话人路由**之前**跑，召回侧纯文本零说话人标注，于是 `lidousha_centrality`
在物理上判不了主角——维护者 盲审四条 tier-1 的真值证明它与真值**反相关**
（内部盲评真值文档留存）。

    召回打分（不打 centrality）
      → 用其余六维排序，取前 N 争席
      → 对这 N 条各跑轻量主播占比检测
      → 得三态 + 主播占比档
      → 定 centrality → 重排 → produce

## 「单人」是检测结果，不是跳过检测的输入假设

维护者 对 Pro 这条纠正的逐字裁定是「可以」。所以本模块**对进入争席的
所有候选都跑检测**，没有"整场单人就豁免"的入口：名义单人场可能有 NPC / 连麦 /
视频素材 / TTS / 临时嘉宾，而多人场里某条候选也可能只有她独白。

## 三态，不是二态

``SOLO_VERIFIED`` / ``MULTI_VERIFIED`` / ``UNKNOWN``。``acoustic_diversity_low``
**不能**推出"一定是主播一个人"（Pro §2）：BGM、游戏角色语音、变声、压缩失真都
会制造多样性，而音色相近的多人也可能看起来同质。检测不出来 → ``UNKNOWN`` →
停泊转人工（维护者「说话人存疑都要直接给人工审阅」），**不许猜成 SOLO 放行**。

## 与既有接口的关系

- 声纹前向复用 ``campp_embed_once``（embed-once 内容寻址缓存）。重叠候选先求
  区间**并集**、窗口落在**全局固定栅格**上，于是两条重叠候选共用的窗口是逐字节
  相同的文件，缓存天然命中，不重复推理。
- 归属状态映射到 ``selection_metric_v2`` 既有的 ``attribution_status`` 词汇，
  不造第三套命名。**唯一的扩展**见 `map_attribution_status` 的 docstring：
  「已分离且主播不是主体」在旧词汇里没有名字，而把它塞进
  ``VERIFIED_HOST_DOMINANT`` 是**事实错误**（维护者 说的正是"主要发言人不是李豆沙"），
  塞进 ``UNVERIFIED`` 又会把一条**归属已确定**的候选送去人工——两条都不能做。
- ``UNVERIFIED`` → ``speaker_manual_review`` 停泊，不是扣分不是猜。

## ⚠️ 标定状态

双阈值 ``HOST_SIMILARITY_MIN`` / ``OTHER_SIMILARITY_MAX`` 是本模块**新**常量，
标 ``provisional``。标定依据是仓内既有同域数值，不是新拍的数（见常量处逐条注释）。
Pro 建议的分位数法 ``t_H = Q_0.995(s | non-host)`` / ``t_O = Q_0.005(s | host)``
需要域内 challenge set，本仓今天没有 —— 没有就如实标 provisional，不假装标定过。

本模块不改 ``DIMENSION_WEIGHTS``、不改 ``selection_scorecard.py`` 的既有算术、
不动 ``assets/lidousha/selection_score_calibration.v1.json``。centrality 仍留在
v1 七维卡里（``selection_scorecard_is_valid`` 硬要求维度集合逐字相等），本模块
只是在**争席排序**时不看它——它怎么从 v1 评分卡退出是下一步，要 维护者 单独裁。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import wave
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.selection_metric_v2 import (
    ATTRIBUTION_UNVERIFIED,
    ATTRIBUTION_VERIFIED_HOST_DOMINANT,
    ATTRIBUTION_VERIFIED_HOST_MINOR,
    ATTRIBUTION_VERIFIED_SOLO,
)
from src.autoslice.selection_scorecard import (
    DIMENSION_WEIGHTS,
    selection_scorecard_is_valid,
)
from src.autoslice.speaker_common import DEFAULT_POLICY
from src.autoslice.speaker_manual_review import park_for_manual_review

REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(REPO_ROOT)
PROFILE_ID = CHANNEL_PROFILE.profile_id

SCHEMA_VERSION = f"{PROFILE_ID}-host-occupancy.v2"
ESTIMATOR_VERSION = "target-host-occupancy-campp-window.v2"
# 阈值版本独立于代码版本：改任何一个阈值都必须改这个字符串，出证里带着它。
THRESHOLD_VERSION = "host-occupancy-thresholds.provisional.v1"

# --- 三态（Pro §4）。二态是被明确否决的形态。 ---
SOLO_VERIFIED = "SOLO_VERIFIED"
MULTI_VERIFIED = "MULTI_VERIFIED"
UNKNOWN = "UNKNOWN"
OCCUPANCY_STATES = frozenset({SOLO_VERIFIED, MULTI_VERIFIED, UNKNOWN})

# --- 窗口标签（Pro §2 MVP 链路）。NON_SPEECH 不进任何占比分母。 ---
LABEL_HOST = "HOST"
LABEL_OTHER = "OTHER"
LABEL_UNKNOWN = "UNKNOWN"
LABEL_NON_SPEECH = "NON_SPEECH"

# --- 争席次序（维护者 逐字：「N 可以选 10 个，不够了再补上」） ---
CONTENTION_SET_SIZE = 10
# 「不够了再补上」的补位必须有界，否则一场里所有候选都会被拉进音频检测。
# 上限＝N 的两倍：一轮全灭也只再补一轮，不做无限补。
CONTENTION_DETECTION_CAP = 2 * CONTENTION_SET_SIZE

# centrality 这一维的权重就是 Pro 的 25 分未观测项；其余六维构成 B_i。
ATTRIBUTION_AXIS = "lidousha_centrality"
BASE_AXES: tuple[str, ...] = tuple(
    axis for axis in DIMENSION_WEIGHTS if axis != ATTRIBUTION_AXIS
)
CENTRALITY_WEIGHT = float(DIMENSION_WEIGHTS[ATTRIBUTION_AXIS])

# --- 音频前处理（Pro §2「推荐实现：候选级 Target Host Occupancy Estimator」） ---
SAMPLE_RATE_HZ = 16_000
# Pro：「每个候选前后各扩 5–10 秒」。取中值 8s。
CANDIDATE_PAD_MS = 8_000
# Pro：「1.5–2.0 秒语音窗 / hop 0.5–1.0 秒」。取窗上界 + hop 上界：窗越长
# CAM++ 越稳，hop 越大前向次数越少，而 hop=1.0s 仍给 2.0s 窗 50% 重叠。
WINDOW_MS = 2_000
HOP_MS = 1_000
# 栅格锚在**段起点 0**，不锚候选起点：两条重叠候选于是共用逐字节相同的窗口
# 文件，embed-once 缓存按内容寻址天然去重（Pro「重叠候选区间合并」）。
GRID_ANCHOR_MS = 0

# --- 轻量 VAD（Pro：链路第一步「轻量 VAD」） ---
VAD_FRAME_MS = 10
# 绝对地板：PCM16 满量程 32768，-50 dBFS ≈ 104。低于它当静音，不管相对能量。
VAD_ABSOLUTE_RMS_FLOOR = 104.0
# 相对判据：跨度自身能量分布的中位数的一半。直播底噪/BGM 与人声的差距远大于
# 2 倍，取一半中位数只滤掉明显静音段，**不做人声/音乐分离**（本模块不声称能）。
VAD_RELATIVE_MEDIAN_FRACTION = 0.5
# 一个窗必须有这么多比例的帧过 VAD 才算语音窗。
VAD_MIN_SPEECH_FRAME_RATIO = 0.5

# --- 双阈值（provisional）。标定依据全部来自仓内既有同域数值，不是新拍的。 ---
#
# t_H = 0.50 —— ``host_vocal_proof.MIN_SESSION_ENROLL_MEDIAN``。那是仓内唯一
#   一个"窗口 vs enrollment"域的既有判据（歌切主播人声出证用它认定同场主播
#   语音锚点），与本模块的打分对象完全同域，所以直接沿用而不是另拍一个。
#   它高于模型自带的 ``yesOrno_thr = 0.31``，满足 Pro「HOST 阈值应比 OTHER
#   更严格」——错误的 HOST 标签会直接制造虚假的高 centrality。
# t_O = 0.31 —— 钉住的 CAM++ 模型自带 ``yesOrno_thr``（同一个数也是
#   ``host_vocal_proof.MIN_CHECKPOINT_MEDIAN``）。低于模型自己的"是"阈值才敢
#   说"不是她"，避免轻易把主播判成别人。
# 中间带 [0.31, 0.50) 全部 abstain —— Pro「两个分布重叠的部分全部 abstain」。
#
# ⚠️ 没有采用 ``speaker_common.DEFAULT_POLICY`` 的 0.68/0.42：那两个是**同场
# seed** 相似度（cue vs 同场主播锚点），同场同麦同增益，分数系统性高于
# "cue vs 跨场 enrollment"。照搬会把 abstention 拉爆。两个数仍在下面回显进
# 出证里，作为"本模块知道它们存在且刻意没用"的留痕。
HOST_SIMILARITY_MIN = 0.50
OTHER_SIMILARITY_MAX = 0.31
_SESSION_SEED_POLICY_NOT_USED = {
    "host_session_seed_min": DEFAULT_POLICY["host_session_seed_min"],
    "guest_seed_max": DEFAULT_POLICY["guest_seed_max"],
    "reason": "SAME_SESSION_SEED_DOMAIN_NOT_ENROLLMENT_DOMAIN",
}

# --- 时间平滑 / 最短持续时间约束（Pro 链路最后一步） ---
# 单窗孤岛（两侧邻居都是别的标签）降级为 UNKNOWN，方向永远朝 abstain。
SMOOTHING_MIN_RUN_WINDOWS = 2

# --- 归属可用性门（Pro §2「初始可使用这些工作阈值」逐条） ---
MIN_CLASSIFIED_COVERAGE = 0.85
MAX_UNKNOWN_SHARE = 0.15
MIN_CLASSIFIED_SPEECH_MS = 8_000

# --- SOLO 严格声学审计（Pro §4C 逐条） ---
SOLO_MIN_CLASSIFIED_COVERAGE = 0.95
SOLO_MIN_HOST_RATIO = 0.98
SOLO_MAX_UNKNOWN_SHARE = 0.05
SOLO_MAX_OTHER_RUN_MS = 2_000
# 认定"确有别人在说"的最短连续 OTHER 段。与 SOLO 那条同一个数：>=2s 连续
# 高置信非主播语音既毙掉 SOLO，也正是 MULTI 的正证据。
MIN_OTHER_RUN_MS = 2_000

# --- 交给 LLM 的是档位不是裸浮点（Pro §2「输出给 LLM 的不是裸浮点数」） ---
BAND_TRACE = "trace"
BAND_LOW = "low"
BAND_MIXED = "mixed"
BAND_DOMINANT = "dominant"
BAND_UNAVAILABLE = "unavailable"
BAND_EDGES: tuple[tuple[float, str], ...] = (
    (0.10, BAND_TRACE),
    (0.30, BAND_LOW),
    (0.60, BAND_MIXED),
)
# 主播占比达到这一档才算"主播为主体"。与 dominant 档同一条线，不另设阈值。
HOST_DOMINANT_MIN_SHARE = 0.60


class HostOccupancyError(RuntimeError):
    """输入或运行时违反了本模块的 fail-closed 不变量。"""


_TUNABLES: dict[str, object] = {
    "estimator_version": ESTIMATOR_VERSION,
    "threshold_version": THRESHOLD_VERSION,
    "candidate_pad_ms": CANDIDATE_PAD_MS,
    "window_ms": WINDOW_MS,
    "hop_ms": HOP_MS,
    "grid_anchor_ms": GRID_ANCHOR_MS,
    "sample_rate_hz": SAMPLE_RATE_HZ,
    "vad_frame_ms": VAD_FRAME_MS,
    "vad_absolute_rms_floor": VAD_ABSOLUTE_RMS_FLOOR,
    "vad_relative_median_fraction": VAD_RELATIVE_MEDIAN_FRACTION,
    "vad_min_speech_frame_ratio": VAD_MIN_SPEECH_FRAME_RATIO,
    "host_similarity_min": HOST_SIMILARITY_MIN,
    "other_similarity_max": OTHER_SIMILARITY_MAX,
    "smoothing_min_run_windows": SMOOTHING_MIN_RUN_WINDOWS,
    "min_classified_coverage": MIN_CLASSIFIED_COVERAGE,
    "max_unknown_share": MAX_UNKNOWN_SHARE,
    "min_classified_speech_ms": MIN_CLASSIFIED_SPEECH_MS,
    "solo_min_classified_coverage": SOLO_MIN_CLASSIFIED_COVERAGE,
    "solo_min_host_ratio": SOLO_MIN_HOST_RATIO,
    "solo_max_unknown_share": SOLO_MAX_UNKNOWN_SHARE,
    "solo_max_other_run_ms": SOLO_MAX_OTHER_RUN_MS,
    "min_other_run_ms": MIN_OTHER_RUN_MS,
    "host_dominant_min_share": HOST_DOMINANT_MIN_SHARE,
    "contention_set_size": CONTENTION_SET_SIZE,
    "contention_detection_cap": CONTENTION_DETECTION_CAP,
}
CONFIG_HASH = (
    "sha256:"
    + hashlib.sha256(
        json.dumps(_TUNABLES, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
)


# --------------------------------------------------------------------------
# 区间：候选 → 扩窗 → 并集 → 全局栅格窗口
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateSpan:
    """一条候选在**同一段源录像**内的毫秒区间。"""

    candidate_id: str
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        for value in (self.start_ms, self.end_ms):
            if isinstance(value, bool) or not isinstance(value, int):
                raise HostOccupancyError("candidate span bounds must be integers")
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise HostOccupancyError(
                f"candidate {self.candidate_id} span is not a positive interval"
            )


@dataclass(frozen=True)
class PaddedSpan:
    candidate_id: str
    start_ms: int
    end_ms: int
    requested_start_ms: int
    requested_end_ms: int

    @property
    def head_pad_truncated_ms(self) -> int:
        return max(0, self.start_ms - self.requested_start_ms)

    @property
    def tail_pad_truncated_ms(self) -> int:
        return max(0, self.requested_end_ms - self.end_ms)


def pad_candidate(span: CandidateSpan, *, source_duration_ms: int) -> PaddedSpan:
    """前后各扩 ``CANDIDATE_PAD_MS``，并**钳在段边界内**。

    不越界读、也不跨段拼接：截掉的扩窗量原样披露（``*_pad_truncated_ms``），
    让下游看得见"这条的尾部上下文其实没拿到"，而不是静默当拿到了。
    """

    if isinstance(source_duration_ms, bool) or not isinstance(source_duration_ms, int):
        raise HostOccupancyError("source_duration_ms must be an integer")
    if source_duration_ms <= 0:
        raise HostOccupancyError("source_duration_ms must be positive")
    requested_start = span.start_ms - CANDIDATE_PAD_MS
    requested_end = span.end_ms + CANDIDATE_PAD_MS
    start = max(0, requested_start)
    end = min(source_duration_ms, requested_end)
    if end <= start:
        raise HostOccupancyError(
            f"candidate {span.candidate_id} padded span falls outside the source"
        )
    return PaddedSpan(
        candidate_id=span.candidate_id,
        start_ms=start,
        end_ms=end,
        requested_start_ms=requested_start,
        requested_end_ms=requested_end,
    )


def union_spans(spans: Sequence[PaddedSpan]) -> list[tuple[int, int]]:
    """重叠/相接的扩窗区间合并成并集，避免重复抽取与重复推理（Pro §2 输入）。"""

    ordered = sorted((span.start_ms, span.end_ms) for span in spans)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def grid_windows(start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    """落在 ``[start_ms, end_ms)`` 内、锚在全局栅格上的窗口列表。

    第一个窗的起点是 ``>= start_ms`` 的最小栅格点，最后一个窗必须整窗落在区间
    内——半个窗不补零凑数（补零会给 CAM++ 喂一段不存在的静音）。
    """

    if end_ms - start_ms < WINDOW_MS:
        return []
    first_index = max(0, -(-(start_ms - GRID_ANCHOR_MS) // HOP_MS))
    windows: list[tuple[int, int]] = []
    index = first_index
    while True:
        window_start = GRID_ANCHOR_MS + index * HOP_MS
        window_end = window_start + WINDOW_MS
        if window_end > end_ms:
            break
        if window_start >= start_ms:
            windows.append((window_start, window_end))
        index += 1
    return windows


# --------------------------------------------------------------------------
# 轻量 VAD
# --------------------------------------------------------------------------


def frame_rms(samples: Sequence[int], *, frame_length: int) -> list[float]:
    """逐帧 RMS。有 numpy 走 numpy，没有就纯 Python——两条路数值一致。"""

    if frame_length <= 0:
        raise HostOccupancyError("frame_length must be positive")
    frame_count = len(samples) // frame_length
    if frame_count == 0:
        return []
    try:  # pragma: no cover - numpy 只在生产 ML runtime 上存在
        import numpy  # type: ignore[import-not-found]
    except ImportError:
        values: list[float] = []
        for index in range(frame_count):
            offset = index * frame_length
            total = 0
            for position in range(offset, offset + frame_length):
                value = samples[position]
                total += value * value
            values.append(math.sqrt(total / frame_length))
        return values
    block = numpy.asarray(
        samples[: frame_count * frame_length], dtype=numpy.float64
    ).reshape(frame_count, frame_length)
    return [float(value) for value in numpy.sqrt((block * block).mean(axis=1))]


def speech_frame_flags(frame_values: Sequence[float]) -> list[bool]:
    """绝对地板 ∧ 相对中位数判据。**不声称能分人声与音乐**。"""

    if not frame_values:
        return []
    positive = sorted(value for value in frame_values if value > 0.0)
    median = positive[len(positive) // 2] if positive else 0.0
    threshold = max(VAD_ABSOLUTE_RMS_FLOOR, median * VAD_RELATIVE_MEDIAN_FRACTION)
    return [value >= threshold for value in frame_values]


def window_is_speech(flags: Sequence[bool]) -> bool:
    if not flags:
        return False
    return sum(1 for flag in flags if flag) / len(flags) >= VAD_MIN_SPEECH_FRAME_RATIO


# --------------------------------------------------------------------------
# 逐窗分类 + 时间平滑
# --------------------------------------------------------------------------


@dataclass
class WindowObservation:
    start_ms: int
    end_ms: int
    label: str
    score: float | None = None
    best_prototype: str = ""
    prototype_scores: dict[str, float] = field(default_factory=dict)
    audio_sha256: str = ""


def classify_score(score: float) -> str:
    """双阈值三分类。重叠带全部 abstain，绝不二分。"""

    if not math.isfinite(score):
        raise HostOccupancyError("CAM++ similarity must be finite")
    if score >= HOST_SIMILARITY_MIN:
        return LABEL_HOST
    if score <= OTHER_SIMILARITY_MAX:
        return LABEL_OTHER
    return LABEL_UNKNOWN


def smooth_labels(observations: Sequence[WindowObservation]) -> list[WindowObservation]:
    """最短持续时间约束：太短的 HOST/OTHER 连段降级为 UNKNOWN。

    ``NON_SPEECH``、不同标签和真实时间缺口都会打断连段；静音两侧的孤立
    ``HOST`` 不能被拼成一段。
    降级方向**只朝 abstain**：短的 OTHER 不会被"平滑"成 HOST，反之亦然。
    """

    smoothed = [
        WindowObservation(
            start_ms=observation.start_ms,
            end_ms=observation.end_ms,
            label=observation.label,
            score=observation.score,
            best_prototype=observation.best_prototype,
            prototype_scores=dict(observation.prototype_scores),
            audio_sha256=observation.audio_sha256,
        )
        for observation in observations
    ]
    run_start = 0
    while run_start < len(smoothed):
        label = smoothed[run_start].label
        run_end = run_start
        while run_end + 1 < len(smoothed):
            current = smoothed[run_end]
            following = smoothed[run_end + 1]
            if (
                following.label != label
                or following.start_ms > current.end_ms
            ):
                break
            run_end += 1
        length = run_end - run_start + 1
        if label in {LABEL_HOST, LABEL_OTHER} and length < SMOOTHING_MIN_RUN_WINDOWS:
            for position in range(run_start, run_end + 1):
                smoothed[position].label = LABEL_UNKNOWN
        run_start = run_end + 1
    return smoothed


def observation_cells(
    observations: Sequence[WindowObservation],
    *,
    clip_start_ms: int | None = None,
    clip_end_ms: int | None = None,
) -> list[tuple[int, int, str]]:
    """Project overlapping analysis windows onto disjoint attribution cells.

    A 2 s window sampled every 1 s is an observation centred on a time point,
    not two independent seconds of speech.  Giving every window its full span
    lets different labels own the same millisecond and can make
    ``HOST + OTHER + UNKNOWN`` exceed the candidate duration.  Midpoints
    between neighbouring window centres form deterministic Voronoi cells:
    interior windows own one hop, edge windows own the uncovered half-window,
    and real gaps are never filled.
    """

    if (clip_start_ms is None) != (clip_end_ms is None):
        raise HostOccupancyError("observation cell clipping requires both bounds")
    if (
        clip_start_ms is not None
        and clip_end_ms is not None
        and clip_end_ms <= clip_start_ms
    ):
        raise HostOccupancyError("observation cell clipping must be positive")

    ordered = sorted(
        observations,
        key=lambda item: (
            (item.start_ms + item.end_ms) / 2,
            item.start_ms,
            item.end_ms,
            item.label,
        ),
    )
    cells: list[tuple[int, int, str]] = []
    for index, observation in enumerate(ordered):
        if observation.end_ms <= observation.start_ms:
            raise HostOccupancyError("window observation must be a positive interval")
        center2 = observation.start_ms + observation.end_ms
        left = observation.start_ms
        right = observation.end_ms
        if index:
            previous = ordered[index - 1]
            previous_center2 = previous.start_ms + previous.end_ms
            if previous.end_ms >= observation.start_ms:
                boundary = (previous_center2 + center2) // 4
                left = max(left, boundary)
        if index + 1 < len(ordered):
            following = ordered[index + 1]
            following_center2 = following.start_ms + following.end_ms
            if observation.end_ms >= following.start_ms:
                boundary = (center2 + following_center2) // 4
                right = min(right, boundary)
        if clip_start_ms is not None and clip_end_ms is not None:
            left = max(left, clip_start_ms)
            right = min(right, clip_end_ms)
        if right > left:
            cells.append((left, right, observation.label))
    return cells


def _covered_ms(
    observations: Sequence[WindowObservation],
    label: str,
    *,
    clip_start_ms: int | None = None,
    clip_end_ms: int | None = None,
) -> int:
    """Milliseconds owned by ``label`` after cross-label overlap removal."""

    return sum(
        end - start
        for start, end, value in observation_cells(
            observations,
            clip_start_ms=clip_start_ms,
            clip_end_ms=clip_end_ms,
        )
        if value == label
    )


def longest_run_ms(
    observations: Sequence[WindowObservation],
    label: str,
    *,
    clip_start_ms: int | None = None,
    clip_end_ms: int | None = None,
) -> int:
    """Longest contiguous attribution-cell run for ``label``."""

    best = 0
    run_start: int | None = None
    run_end: int | None = None
    for start, end, value in observation_cells(
        observations,
        clip_start_ms=clip_start_ms,
        clip_end_ms=clip_end_ms,
    ):
        if value == label and run_end is not None and start <= run_end:
            run_end = max(run_end, end)
            continue
        if run_start is not None and run_end is not None:
            best = max(best, run_end - run_start)
        if value == label:
            run_start, run_end = start, end
        else:
            run_start = run_end = None
    if run_start is not None and run_end is not None:
        best = max(best, run_end - run_start)
    return best


def occupancy_band(host_share: float | None) -> str:
    if host_share is None:
        return BAND_UNAVAILABLE
    for edge, name in BAND_EDGES:
        if host_share < edge:
            return name
    return BAND_DOMINANT


# --------------------------------------------------------------------------
# 聚合 → 三态
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MultiSpeakerEvidence:
    """非声学的强证据/强反证（Pro §4「信号权重」）。

    ``named_guest`` 是"标题/弹幕/画面出现**明确**嘉宾名字"——模糊人名、游戏角色
    名或观众称呼**不算**，那些只能降为 UNKNOWN，不能武断判多人。
    ``solo_source`` 是结构化节目配置 / ingestion roster / 明确 allowlist；
    「标题没有嘉宾名」**不是** solo 正向来源，所以本仓今天默认它是 False。
    """

    named_guest: bool = False
    guest_roster_present: bool = False
    solo_source: bool = False
    source_note: str = ""


def aggregate_occupancy(
    observations: Sequence[WindowObservation],
    *,
    evidence: MultiSpeakerEvidence | None = None,
    clip_start_ms: int | None = None,
    clip_end_ms: int | None = None,
) -> dict[str, object]:
    """把逐窗标签聚成一条候选的三态与主播占比。

    判定次序（Pro §4 可执行判据）：

    1. 归属可用性门不过 → ``UNKNOWN``（**先于**任何 solo/multi 判断）。
    2. 明确嘉宾名 / 已知嘉宾 roster → ``MULTI_VERIFIED``。
    3. 严格声学审计过 ∧ 无强多人反证 → ``SOLO_VERIFIED``。
    4. 有 >= ``MIN_OTHER_RUN_MS`` 的连续高置信非主播语音 → ``MULTI_VERIFIED``。
    5. 其余 → ``UNKNOWN``（"看着挺干净但没干净到严格门"**不是** solo）。
    """

    facts = evidence or MultiSpeakerEvidence()
    host_ms = _covered_ms(
        observations,
        LABEL_HOST,
        clip_start_ms=clip_start_ms,
        clip_end_ms=clip_end_ms,
    )
    other_ms = _covered_ms(
        observations,
        LABEL_OTHER,
        clip_start_ms=clip_start_ms,
        clip_end_ms=clip_end_ms,
    )
    unknown_ms = _covered_ms(
        observations,
        LABEL_UNKNOWN,
        clip_start_ms=clip_start_ms,
        clip_end_ms=clip_end_ms,
    )
    speech_ms = host_ms + other_ms + unknown_ms
    classified_ms = host_ms + other_ms
    classified_coverage = classified_ms / speech_ms if speech_ms else 0.0
    unknown_share = unknown_ms / speech_ms if speech_ms else 1.0
    host_share = host_ms / classified_ms if classified_ms else None
    longest_other_ms = longest_run_ms(
        observations,
        LABEL_OTHER,
        clip_start_ms=clip_start_ms,
        clip_end_ms=clip_end_ms,
    )

    metrics: dict[str, object] = {
        "host_speech_ms": host_ms,
        "other_speech_ms": other_ms,
        "unknown_speech_ms": unknown_ms,
        "speech_ms": speech_ms,
        "classified_speech_ms": classified_ms,
        "classified_coverage": round(classified_coverage, 4),
        "unknown_share": round(unknown_share, 4),
        "host_speech_share": None if host_share is None else round(host_share, 4),
        "host_speech_share_band": occupancy_band(host_share),
        "longest_other_run_ms": longest_other_ms,
        "speech_window_count": sum(
            1 for item in observations if item.label != LABEL_NON_SPEECH
        ),
        "non_speech_window_count": sum(
            1 for item in observations if item.label == LABEL_NON_SPEECH
        ),
    }

    gates = {
        "classified_coverage": classified_coverage >= MIN_CLASSIFIED_COVERAGE,
        "unknown_share": unknown_share <= MAX_UNKNOWN_SHARE,
        "classified_speech_ms": classified_ms >= MIN_CLASSIFIED_SPEECH_MS,
    }
    reasons: list[str] = []
    if not all(gates.values()):
        for name, passed in sorted(gates.items()):
            if not passed:
                reasons.append(f"ATTRIBUTION_GATE_FAILED_{name.upper()}")
        return {**metrics, "state": UNKNOWN, "gates": gates, "reason_codes": reasons}

    if facts.named_guest or facts.guest_roster_present:
        return {
            **metrics,
            "state": MULTI_VERIFIED,
            "gates": gates,
            "reason_codes": ["NAMED_GUEST_EVIDENCE"],
        }

    solo_audit = {
        "trusted_solo_source": facts.solo_source,
        "classified_coverage": classified_coverage >= SOLO_MIN_CLASSIFIED_COVERAGE,
        "host_ratio": host_share is not None and host_share >= SOLO_MIN_HOST_RATIO,
        "unknown_share": unknown_share <= SOLO_MAX_UNKNOWN_SHARE,
        "no_sustained_other": longest_other_ms < SOLO_MAX_OTHER_RUN_MS,
    }
    if all(solo_audit.values()):
        return {
            **metrics,
            "state": SOLO_VERIFIED,
            "gates": gates,
            "solo_audit": solo_audit,
            "reason_codes": ["TRUSTED_SOLO_SOURCE_AND_ACOUSTIC_AUDIT_PASSED"],
        }
    if longest_other_ms >= MIN_OTHER_RUN_MS:
        return {
            **metrics,
            "state": MULTI_VERIFIED,
            "gates": gates,
            "solo_audit": solo_audit,
            "reason_codes": ["SUSTAINED_NON_HOST_SPEECH"],
        }
    for name, passed in sorted(solo_audit.items()):
        if not passed:
            reasons.append(f"SOLO_AUDIT_FAILED_{name.upper()}")
    reasons.append("NO_SUSTAINED_NON_HOST_SPEECH_EITHER")
    return {
        **metrics,
        "state": UNKNOWN,
        "gates": gates,
        "solo_audit": solo_audit,
        "reason_codes": reasons,
    }


def map_attribution_status(occupancy: Mapping[str, object]) -> str:
    """本模块三态 → ``selection_metric_v2`` 的 ``attribution_status`` 词汇。

    ``SOLO_VERIFIED`` → ``VERIFIED_SOLO``；``UNKNOWN`` → ``UNVERIFIED``（停泊）。

    ``MULTI_VERIFIED`` **不是**一对一：它只说"归属已确定"，没说谁是主体。
    按主播占比分岔——

    - ``>= HOST_DOMINANT_MIN_SHARE`` → ``VERIFIED_HOST_DOMINANT``
    - 否则 → ``VERIFIED_HOST_MINOR``

    为什么必须分岔：把"主要发言人不是李豆沙"的候选叫 ``VERIFIED_HOST_DOMINANT``
    是**事实错误**（那正是 维护者 盲审对两条候选的原话）；而把它叫 ``UNVERIFIED``
    会把一条**归属已经确定**的候选送去人工，既淹没人工队列（Pro §5：能靠其他
    独立条件安全淘汰的候选不必送审），也违反 维护者「最好是能够自然给出低分，
    而不是强制压低」——自然的低分来自 centrality 看见 ``[其他]`` 标签，不来自
    把它伪装成存疑。停泊只留给 ``UNKNOWN``。
    """

    state = occupancy.get("state")
    if state == SOLO_VERIFIED:
        return ATTRIBUTION_VERIFIED_SOLO
    if state == UNKNOWN:
        return ATTRIBUTION_UNVERIFIED
    if state != MULTI_VERIFIED:
        raise HostOccupancyError(f"unknown occupancy state: {state!r}")
    share = occupancy.get("host_speech_share")
    if not isinstance(share, (int, float)) or isinstance(share, bool):
        # 归属门已经过了却没有占比＝内部不一致，fail-closed 回停泊。
        return ATTRIBUTION_UNVERIFIED
    if float(share) >= HOST_DOMINANT_MIN_SHARE:
        return ATTRIBUTION_VERIFIED_HOST_DOMINANT
    return ATTRIBUTION_VERIFIED_HOST_MINOR


CUE_LABEL_HOST = f"[{CHANNEL_PROFILE.host_speaker_label}]"
CUE_LABEL_OTHER = "[其他]"
CUE_LABEL_UNCERTAIN = "[存疑]"
# 一条 cue 要被判给某个说话人，该说话人的窗口必须覆盖这条 cue 的多数时长。
# A known label must own nearly the whole ASR cue.  A simple majority would let
# a cue that straddles a speaker change (for example 55% HOST / 45% OTHER) look
# attributable even though the actual words cannot be assigned safely.
CUE_LABEL_MIN_OVERLAP_SHARE = 0.85


def label_cues(
    cues: Sequence[Mapping[str, object]], observations: Sequence[WindowObservation]
) -> list[dict[str, object]]:
    """把逐窗标签按时间交叠贴到现有 ASR 行上（Pro §2 末「输出给 LLM」）。

    这是 centrality rubric 唯一被允许的身份来源：rubric 明文「不得根据措辞、语气、
    人物自称或上下文自行把未标注话语认作李豆沙」「『我』『我们』绝不能用于推断
    说话人身份」。所以这里**只按时间**贴标签，一个字的文本都不看。

    覆盖不足 / 主导标签不明确 → ``[存疑]``，不是"就近取一个"。
    """

    results: list[dict[str, object]] = []
    for cue in cues:
        start = int(cue["start_ms"])  # type: ignore[call-overload]
        end = int(cue["end_ms"])  # type: ignore[call-overload]
        duration = max(0, end - start)
        overlaps: dict[str, int] = {
            LABEL_HOST: 0,
            LABEL_OTHER: 0,
            LABEL_UNKNOWN: 0,
            LABEL_NON_SPEECH: 0,
        }
        for cell_start, cell_end, cell_label in observation_cells(observations):
            if cell_label not in overlaps:
                continue
            covered = min(end, cell_end) - max(start, cell_start)
            if covered > 0:
                overlaps[cell_label] += covered
        known_overlaps = {
            LABEL_HOST: overlaps[LABEL_HOST],
            LABEL_OTHER: overlaps[LABEL_OTHER],
        }
        winner, covered_ms = max(
            known_overlaps.items(), key=lambda item: (item[1], item[0])
        )
        runner_up = min(known_overlaps.values())
        observed_ms = min(duration, sum(overlaps.values()))
        uncovered_ms = max(0, duration - observed_ms)
        share = covered_ms / duration if duration else 0.0
        uncertain_ms = (
            overlaps[LABEL_UNKNOWN] + overlaps[LABEL_NON_SPEECH] + uncovered_ms
        )
        uncertain_share = uncertain_ms / duration if duration else 1.0
        # Nearly-complete known ownership is required.  UNKNOWN, a speaker
        # switch, VAD disagreement, or uncovered audio all move toward abstain.
        if (
            share < CUE_LABEL_MIN_OVERLAP_SHARE
            or covered_ms <= runner_up
            or uncertain_share > (1.0 - CUE_LABEL_MIN_OVERLAP_SHARE)
        ):
            label = CUE_LABEL_UNCERTAIN
        else:
            label = CUE_LABEL_HOST if winner == LABEL_HOST else CUE_LABEL_OTHER
        results.append(
            {
                "cue_id": cue.get("cue_id"),
                "start_ms": start,
                "end_ms": end,
                "speaker_label": label,
                "overlap_share": round(min(1.0, share), 4),
                "uncertain_overlap_share": round(min(1.0, uncertain_share), 4),
                "label_overlap_ms": {
                    **overlaps,
                    "UNCOVERED": uncovered_ms,
                },
                "label_overlap_share": {
                    value: round(milliseconds / duration, 4) if duration else 0.0
                    for value, milliseconds in {
                        **overlaps,
                        "UNCOVERED": uncovered_ms,
                    }.items()
                },
            }
        )
    return results


def key_moments_are_attributed(
    labeled_cues: Sequence[Mapping[str, object]], key_cue_ids: Sequence[object]
) -> bool:
    """rubric 规则 4：关键推进/落点的说话人是 ``[存疑]`` → 不得输出 0–4 分。"""

    wanted = {value for value in key_cue_ids}
    if not wanted:
        return False
    seen = {
        cue["cue_id"]: cue["speaker_label"]
        for cue in labeled_cues
        if cue.get("cue_id") in wanted
    }
    if set(seen) != wanted:
        return False
    return all(label != CUE_LABEL_UNCERTAIN for label in seen.values())


def requires_manual_review(occupancy: Mapping[str, object]) -> bool:
    return map_attribution_status(occupancy) == ATTRIBUTION_UNVERIFIED


def park_unverified_candidate(record: dict, occupancy: Mapping[str, object]) -> dict | None:
    """``UNVERIFIED`` → 复用既有停泊机制，不新造平行状态。

    只在归属存疑时盖章；已确定归属（含主播不是主体）的候选一律不进人工队列。
    """

    if not requires_manual_review(occupancy):
        return None
    record["status"] = "speaker_review_required"
    record["failure_kind"] = "speaker_evidence"
    return park_for_manual_review(
        record,
        reason="host_occupancy_attribution_unverified",
        migrated_from={
            "schema_version": SCHEMA_VERSION,
            "estimator_version": ESTIMATOR_VERSION,
            "threshold_version": THRESHOLD_VERSION,
            "authority": "REVIEWER_2026-08-10_SPEAKER_UNCERTAIN_GOES_TO_HUMAN_REVIEW",
            "reason_codes": list(occupancy.get("reason_codes") or []),
        },
    )


# --------------------------------------------------------------------------
# 两段式争席次序
# --------------------------------------------------------------------------


def base_score(scorecard: Mapping[str, object]) -> float:
    """Pro 的 B_i：**除 centrality 外**六维的加权分（满分 75）。

    与 v1 同一套算术（``w_d * l_d / 4``），只是不含那一项。不改
    ``DIMENSION_WEIGHTS``、不重新归一化——重新归一化会造出一个与 v1 不可比的
    新标度，而 Pro 的剪枝判据 ``B_i + 25`` 要求 B 与 v1 同标度。
    """

    dimensions = scorecard.get("dimensions")
    if not isinstance(dimensions, Mapping):
        raise HostOccupancyError("scorecard has no dimensions")
    return sum(
        DIMENSION_WEIGHTS[axis] * float(dimensions[axis]) / 4 for axis in BASE_AXES
    )


def score_bounds(item: Mapping[str, object]) -> tuple[float, float] | None:
    """``[B_i, B_i + 25]``：centrality 未观测时这条候选的分数区间。"""

    scorecard = item.get("selection_scorecard")
    if not selection_scorecard_is_valid(scorecard):
        return None
    assert isinstance(scorecard, Mapping)
    base = base_score(scorecard)
    return base, base + CENTRALITY_WEIGHT


def contention_rank_key(item: Mapping[str, object]) -> tuple[float, float, float, str]:
    """争席排序键：硬 Tier → 分数**上界** → 置信度 → cid。

    与 ``selection_rank_key`` 同形（Tier 在最前，同一套 Tier 语义），但第二位
    换成 ``B_i + 25`` 而不是含 centrality 的 ``effective_score``——Pro §1
    「优先检查上界最大的候选」。无效卡与既有惯例一致落 Tier 3。
    """

    confidence_raw = item.get("confidence")
    confidence = (
        float(confidence_raw)
        if isinstance(confidence_raw, (int, float))
        and not isinstance(confidence_raw, bool)
        and math.isfinite(float(confidence_raw))
        else 0.0
    )
    candidate_id = str(item.get("cid") or item.get("candidate_id") or "")
    bounds = score_bounds(item)
    scorecard = item.get("selection_scorecard")
    if bounds is None or not isinstance(scorecard, Mapping):
        return (3.0, -(max(0.0, min(1.0, confidence)) * 100.0), -confidence, candidate_id)
    return (float(scorecard["tier"]), -bounds[1], -confidence, candidate_id)


@dataclass(frozen=True)
class ContentionSet:
    """一轮争席的准入结果。"""

    admitted: tuple[str, ...]
    deferred: tuple[str, ...]
    pruned: tuple[str, ...]
    detections_spent: int
    backfilled: tuple[str, ...] = ()
    capped: bool = False


def build_contention_set(
    items: Sequence[Mapping[str, object]],
    *,
    publish_threshold: float | None = None,
    already_detected: Sequence[str] = (),
    size: int = CONTENTION_SET_SIZE,
) -> ContentionSet:
    """取上界最高的 ``size`` 条进音频检测（维护者：N 可以选 10 个）。

    ``publish_threshold`` 给了就先做安全剪枝：``B_i + 25 < τ`` 的候选即使
    centrality 满分也过不了发布线，直接淘汰、**不跑音频**（Pro §1 branch-and-
    bound）。没给就不剪——不替 维护者 发明发布线。

    ``already_detected`` 是本场已经花过检测预算的候选：补位时算进上限，
    保证「不够了再补上」不会退化成整场全检。
    """

    if size <= 0:
        raise HostOccupancyError("contention set size must be positive")
    pruned: list[str] = []
    eligible: list[Mapping[str, object]] = []
    for item in items:
        candidate_id = str(item.get("cid") or item.get("candidate_id") or "")
        bounds = score_bounds(item)
        if (
            publish_threshold is not None
            and bounds is not None
            and bounds[1] < float(publish_threshold)
        ):
            pruned.append(candidate_id)
            continue
        eligible.append(item)
    ordered = sorted(eligible, key=contention_rank_key)
    spent = len({str(value) for value in already_detected})
    budget = max(0, CONTENTION_DETECTION_CAP - spent)
    allowed = min(size, budget)
    admitted = [
        str(item.get("cid") or item.get("candidate_id") or "") for item in ordered[:allowed]
    ]
    deferred = [
        str(item.get("cid") or item.get("candidate_id") or "") for item in ordered[allowed:]
    ]
    return ContentionSet(
        admitted=tuple(admitted),
        deferred=tuple(deferred),
        pruned=tuple(pruned),
        detections_spent=spent + len(admitted),
        capped=allowed < min(size, len(ordered)),
    )


def backfill_contention_set(
    previous: ContentionSet,
    items: Sequence[Mapping[str, object]],
    *,
    vacated: Sequence[str],
    publish_threshold: float | None = None,
) -> ContentionSet:
    """维护者「不够了再补上」：空出多少席就补多少，且**有界**。

    补位只从 ``previous.deferred`` 里按同一排序取，绝不回收已剪枝的候选
    （它们的上界本来就过不了线），也绝不越过 ``CONTENTION_DETECTION_CAP``。
    """

    vacated_ids = {str(value) for value in vacated}
    if not vacated_ids <= set(previous.admitted):
        raise HostOccupancyError("vacated candidates must come from the admitted set")
    by_id = {
        str(item.get("cid") or item.get("candidate_id") or ""): item for item in items
    }
    pool = [by_id[cid] for cid in previous.deferred if cid in by_id]
    if publish_threshold is not None:
        pool = [
            item
            for item in pool
            if (bounds := score_bounds(item)) is None
            or bounds[1] >= float(publish_threshold)
        ]
    ordered = sorted(pool, key=contention_rank_key)
    budget = max(0, CONTENTION_DETECTION_CAP - previous.detections_spent)
    take = min(len(vacated_ids), len(ordered), budget)
    added = [
        str(item.get("cid") or item.get("candidate_id") or "") for item in ordered[:take]
    ]
    remaining = [
        str(item.get("cid") or item.get("candidate_id") or "") for item in ordered[take:]
    ]
    survivors = tuple(cid for cid in previous.admitted if cid not in vacated_ids)
    return ContentionSet(
        admitted=survivors + tuple(added),
        deferred=tuple(remaining),
        pruned=previous.pruned,
        detections_spent=previous.detections_spent + len(added),
        backfilled=tuple(added),
        capped=take < min(len(vacated_ids), len(ordered)),
    )


# --------------------------------------------------------------------------
# 运行时：抽取 → 切窗 → 打分 → 出证
# --------------------------------------------------------------------------


def extract_span_wav(
    source_media: Path, *, start_ms: int, end_ms: int, output_path: Path
) -> Path:
    """一次 ffmpeg 抽一整个并集跨度（**不是**每个窗一次）。

    FUSE 挂载上的随机 seek 是成本主项；一个跨度一次拉取、之后在本地按样本切窗，
    把 O(窗数) 次远端 seek 压成 O(并集跨度数) 次。
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", f"{start_ms / 1000:.3f}",
        "-i", str(source_media),
        "-t", f"{(end_ms - start_ms) / 1000:.3f}",
        "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE_HZ), "-c:a", "pcm_s16le",
        str(output_path),
    ]
    try:
        subprocess.run(
            command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
        )
    except FileNotFoundError as exc:
        raise HostOccupancyError("ffmpeg is required to extract candidate audio") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()[-800:]
        raise HostOccupancyError(f"ffmpeg span extraction failed: {detail}") from exc
    return output_path


def read_wav_samples(path: Path) -> array:
    with wave.open(str(path), "rb") as handle:
        if (
            handle.getnchannels() != 1
            or handle.getframerate() != SAMPLE_RATE_HZ
            or handle.getsampwidth() != 2
        ):
            raise HostOccupancyError(f"span wav must be mono PCM16 16kHz: {path}")
        raw = handle.readframes(handle.getnframes())
    samples = array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":  # pragma: no cover - little-endian hosts only
        samples.byteswap()
    return samples


def write_window_wav(samples: Sequence[int], path: Path) -> Path:
    payload = array("h", samples)
    if sys.byteorder != "little":  # pragma: no cover - little-endian hosts only
        payload.byteswap()
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE_HZ)
        handle.writeframes(payload.tobytes())
    return path


def _slice(samples: Sequence[int], *, span_start_ms: int, start_ms: int, end_ms: int):
    begin = (start_ms - span_start_ms) * SAMPLE_RATE_HZ // 1000
    finish = (end_ms - span_start_ms) * SAMPLE_RATE_HZ // 1000
    return samples[begin:finish]


def observe_span(
    *,
    span_start_ms: int,
    span_end_ms: int,
    samples: Sequence[int],
    prototypes: Mapping[str, Path],
    similarity: Callable[[Path, Path], float],
    window_dir: Path,
) -> list[WindowObservation]:
    """一个并集跨度的逐窗观测：VAD → 写窗 wav → 与每个 prototype 比分 → 三分类。

    ``similarity`` 由调用方注入（生产是 ``campp_embed_once`` 的 embed-once
    打分器），本函数不加载任何 ML runtime。
    """

    if not prototypes:
        raise HostOccupancyError("host enrollment prototype set is empty")
    frame_length = VAD_FRAME_MS * SAMPLE_RATE_HZ // 1000
    energies = frame_rms(samples, frame_length=frame_length)
    flags = speech_frame_flags(energies)
    observations: list[WindowObservation] = []
    for start_ms, end_ms in grid_windows(span_start_ms, span_end_ms):
        first_frame = (start_ms - span_start_ms) // VAD_FRAME_MS
        last_frame = (end_ms - span_start_ms) // VAD_FRAME_MS
        if not window_is_speech(flags[first_frame:last_frame]):
            observations.append(
                WindowObservation(start_ms=start_ms, end_ms=end_ms, label=LABEL_NON_SPEECH)
            )
            continue
        window_samples = _slice(
            samples, span_start_ms=span_start_ms, start_ms=start_ms, end_ms=end_ms
        )
        window_path = write_window_wav(
            window_samples, window_dir / f"w_{start_ms:09d}_{end_ms:09d}.wav"
        )
        scores = {
            name: float(similarity(window_path, path))
            for name, path in sorted(prototypes.items())
        }
        best_prototype, best_score = max(scores.items(), key=lambda item: (item[1], item[0]))
        observations.append(
            WindowObservation(
                start_ms=start_ms,
                end_ms=end_ms,
                label=classify_score(best_score),
                score=round(best_score, 5),
                best_prototype=best_prototype,
                prototype_scores={name: round(value, 5) for name, value in scores.items()},
                audio_sha256="sha256:" + hashlib.sha256(window_path.read_bytes()).hexdigest(),
            )
        )
    return smooth_labels(observations)


def candidate_observations(
    padded: PaddedSpan,
    span_observations: Mapping[tuple[int, int], Sequence[WindowObservation]],
    *,
    candidate_start_ms: int,
    candidate_end_ms: int,
) -> tuple[list[WindowObservation], list[WindowObservation]]:
    """从并集跨度的观测里切回这条候选的窗口。

    返回 ``(候选窗, 扩窗窗)``：占比按**候选自身区间**算（窗口中心落在其中），
    扩窗那份只作披露——8 秒前摇里别人说话不该把一条干净的独白判成多人。
    """

    for (span_start, span_end), observations in span_observations.items():
        if span_start <= padded.start_ms and padded.end_ms <= span_end:
            padded_windows = [
                observation
                for observation in observations
                if observation.start_ms >= padded.start_ms
                and observation.end_ms <= padded.end_ms
            ]
            inner = [
                observation
                for observation in padded_windows
                if candidate_start_ms
                <= (observation.start_ms + observation.end_ms) // 2
                < candidate_end_ms
            ]
            return inner, padded_windows
    raise HostOccupancyError(
        f"candidate {padded.candidate_id} has no covering union span"
    )


def estimate_candidate_occupancy(
    span: CandidateSpan,
    *,
    padded: PaddedSpan,
    span_observations: Mapping[tuple[int, int], Sequence[WindowObservation]],
    evidence: MultiSpeakerEvidence | None = None,
    enrollment: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """一条候选的完整出证：三态 + 占比 + 版本绑定 + 逐窗证据。"""

    inner, padded_windows = candidate_observations(
        padded,
        span_observations,
        candidate_start_ms=span.start_ms,
        candidate_end_ms=span.end_ms,
    )
    occupancy = aggregate_occupancy(
        inner,
        evidence=evidence,
        clip_start_ms=span.start_ms,
        clip_end_ms=span.end_ms,
    )
    context = aggregate_occupancy(
        padded_windows,
        evidence=evidence,
        clip_start_ms=padded.start_ms,
        clip_end_ms=padded.end_ms,
    )
    status = map_attribution_status(occupancy)
    return {
        "schema_version": SCHEMA_VERSION,
        "estimator_version": ESTIMATOR_VERSION,
        "threshold_version": THRESHOLD_VERSION,
        "config_hash": CONFIG_HASH,
        "provisional_calibration": True,
        "candidate_id": span.candidate_id,
        "candidate_start_ms": span.start_ms,
        "candidate_end_ms": span.end_ms,
        "padded_start_ms": padded.start_ms,
        "padded_end_ms": padded.end_ms,
        "head_pad_truncated_ms": padded.head_pad_truncated_ms,
        "tail_pad_truncated_ms": padded.tail_pad_truncated_ms,
        "enrollment": dict(enrollment or {}),
        "session_seed_policy_not_used": dict(_SESSION_SEED_POLICY_NOT_USED),
        "occupancy": occupancy,
        "context_occupancy": context,
        "attribution_status": status,
        "requires_speaker_manual_review": status == ATTRIBUTION_UNVERIFIED,
        "windows": [
            {
                "start_ms": observation.start_ms,
                "end_ms": observation.end_ms,
                "label": observation.label,
                "score": observation.score,
                "best_prototype": observation.best_prototype,
                "prototype_scores": dict(observation.prototype_scores),
                "audio_sha256": observation.audio_sha256,
            }
            for observation in inner
        ],
    }


def build_campp_similarity(model_dir: Path, *, cache_dir: Path) -> Callable[[Path, Path], float]:
    """生产打分器：钉住的 CAM++ + ``campp_embed_once`` 的内容寻址缓存。

    只在真正要跑推理的进程里 import ModelScope；普通验证进程 import 本模块
    不会拖进 ML runtime。
    """

    from src.autoslice.campp_embed_once import _build_embedding_similarity
    from src.autoslice.host_vocal_proof import _load_campplus_pipeline, _sha256_directory

    verifier = _load_campplus_pipeline(model_dir)
    return _build_embedding_similarity(
        verifier=verifier, model_hash=_sha256_directory(model_dir), work_dir=cache_dir
    )


def enrollment_prototypes(
    profile_path: Path, reference_dir: Path
) -> tuple[dict[str, Path], dict[str, object]]:
    """读出 enrollment prototype 集合并按 sha256 校验（**不是** centroid）。

    Pro 建议 6–12 个片段 / 60–120 秒 / 保留 3–5 个 prototype。本仓实际只有
    profile 里声明的那几条——本函数如实报告数量与时长，不补齐、不假装满足。
    """

    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    references = payload.get("references")
    if not isinstance(references, list) or not references:
        raise HostOccupancyError("voiceprint profile declares no references")
    prototypes: dict[str, Path] = {}
    manifest: list[dict[str, object]] = []
    for reference in references:
        path = reference_dir / str(reference["filename"])
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != str(reference["sha256"]).lower().removeprefix("sha256:"):
            raise HostOccupancyError(f"enrollment reference drifted: {reference['id']}")
        with wave.open(str(path), "rb") as handle:
            duration_ms = round(handle.getnframes() * 1000 / handle.getframerate())
        prototypes[str(reference["id"])] = path
        manifest.append(
            {
                "id": str(reference["id"]),
                "filename": str(reference["filename"]),
                "sha256": digest,
                "duration_ms": duration_ms,
            }
        )
    return prototypes, {
        "profile_id": payload.get("profile_id"),
        "schema_version": payload.get("schema_version"),
        "prototype_count": len(manifest),
        "total_duration_ms": sum(int(item["duration_ms"]) for item in manifest),
        "prototypes": manifest,
        "pro_recommended_prototype_range": [3, 5],
        "pro_recommended_clip_range": [6, 12],
        "pro_recommended_total_seconds": [60, 120],
    }


def run_estimator(
    *,
    source_media: Path,
    source_duration_ms: int,
    spans: Sequence[CandidateSpan],
    prototypes: Mapping[str, Path],
    similarity: Callable[[Path, Path], float],
    work_dir: Path,
    evidence: MultiSpeakerEvidence | None = None,
    enrollment: Mapping[str, object] | None = None,
    extract: Callable[..., Path] | None = None,
) -> list[dict[str, object]]:
    """端到端：扩窗 → 并集 → 逐跨度抽取 → 逐窗打分 → 逐候选出证。"""

    extractor = extract if extract is not None else extract_span_wav
    padded = [pad_candidate(span, source_duration_ms=source_duration_ms) for span in spans]
    observations: dict[tuple[int, int], list[WindowObservation]] = {}
    for index, (start_ms, end_ms) in enumerate(union_spans(padded)):
        span_wav = extractor(
            source_media,
            start_ms=start_ms,
            end_ms=end_ms,
            output_path=work_dir / f"span_{index:03d}.wav",
        )
        observations[(start_ms, end_ms)] = observe_span(
            span_start_ms=start_ms,
            span_end_ms=end_ms,
            samples=read_wav_samples(span_wav),
            prototypes=prototypes,
            similarity=similarity,
            window_dir=work_dir / "windows",
        )
    return [
        estimate_candidate_occupancy(
            span,
            padded=padded[index],
            span_observations=observations,
            evidence=evidence,
            enrollment=enrollment,
        )
        for index, span in enumerate(spans)
    ]


def _parse_candidate(value: str) -> CandidateSpan:
    parts = value.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("candidate must be CID:START_MS:END_MS")
    return CandidateSpan(parts[0], int(parts[1]), int(parts[2]))


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - CLI
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-media", type=Path, required=True)
    parser.add_argument("--source-duration-ms", type=int, required=True)
    parser.add_argument("--candidate", type=_parse_candidate, action="append", required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--reference-dir", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    prototypes, enrollment = enrollment_prototypes(args.profile, args.reference_dir)
    similarity = build_campp_similarity(
        args.model_dir, cache_dir=args.work_dir / "embedding-cache"
    )
    reports = run_estimator(
        source_media=args.source_media,
        source_duration_ms=args.source_duration_ms,
        spans=args.candidate,
        prototypes=prototypes,
        similarity=similarity,
        work_dir=args.work_dir,
        enrollment=enrollment,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(reports, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for report in reports:
        occupancy = report["occupancy"]
        assert isinstance(occupancy, dict)
        print(
            f"{report['candidate_id']}\t{occupancy['state']}\t"
            f"host_share={occupancy['host_speech_share']}\t"
            f"band={occupancy['host_speech_share_band']}\t"
            f"attribution={report['attribution_status']}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
