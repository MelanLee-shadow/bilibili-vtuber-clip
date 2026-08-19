"""Audience-payoff boundary extension for the semantic recall lane.

## 为什么有这个模块（维护者 盲审 8/7 tier-1 的真值）

维护者 不看分数直接看原片，判定 `auto_223750_578_654`（评分器排最低的 69.5）是四条里
**唯一该发的**，并且「这个应该再往后面切一点，补全剧情」。按他确认的重切结果，正确边界是
`578030 → 734200`，而流水线切出来的是 `578030 → 654170` —— **短了整整 80 秒，包袱还没抖响
就收尾了**。他描述的第二层反转（作为食鸟鸭想吃尸体、没吃到就被人发现）和「弹幕出现很多说
她是坏女人」的观众高光，整个落在 654170 之后。

### 根因不是评分，是召回阶段根本没有收尾判据

`semantic_candidate_selector.select_semantic_session_candidates()` 对 talk 候选**逐字采用
LLM 给的 `end_cue`**：它直接构造一个 `BoundaryResolution(reason_codes=("SEMANTIC_RECALL",))`，
`resolved_end_ms = window[-1].source_end_ms`。带 payoff / open-loop 判据的
`boundary_resolver.resolve_talk_boundary()` 只在 keyword/fallback lane 和 `live_source_review`
被调用，**这条 lane 一次都不走**。所以「是哪条判据让它停在 654170」的答案是：一条都没有——
654170 就是 LLM 选的 cue #272 的结束时刻，后面没有任何确定性复核。

弹幕当时也只是**提示**：`talk_lane.danmaku_hints()` 当时把 `find_danmaku_bursts()` 的结果
取前 6 条塞进 prompt。8/7 那场 660000–690000 的爆发（x22）被检测到了，但按 count 排在第 7，
**正好被 `[:6]` 截掉**，选题模型连提示都没看到。确定性层面弹幕对 talk 边界的参与度是零
（`danmaku_count_in()` 只服务歌切）。
（那道提示名额已于 按 136 个真实弹幕 XML 的实测放宽到
`danmaku_evidence.DANMAKU_HINT_MAX_BURSTS` = 20，依据见该常量注释；本模块的判据不受影响。）

## 判据（维护者 确认的收敛判据，按可靠性排序）

1. **必须落在人声边界**——不许断在句子中间。本模块的落点恒为某条 cue 的 `source_end_ms`。
2. **必须到故事完结**——包袱抖响并落地。确定性近似 = 把切点之后那段**无人认领的连续语音
   串整段采纳**，不在中间随意停。
3. **下一话题起点是天然停止位**——代理 = 下一条被选中候选的窗口起点。

**弹幕密度被降级为辅助信号**：它只回答「这里有高光、别急着收」，回答不了「事说完了没有」。
所以本模块里弹幕只做**后延许可**（要不要跨过切点后的那段静默继续听下去），落点完全由
上面三条确定性判据决定。

## 许可与停止

许可（三条全部满足才跨过切点）：

- L1 爆发发生在**切点之后**（`burst.start_ms >= end`，且 `burst.end_ms > end`）——是切点
  之后新炸出来的观众反应，不是切点之前就在持续的存量反应；
- L2 语音重新开始的那一刻，观众正处在这次爆发里（`burst.start_ms <= resume.start < burst.end_ms`）；
- L3 被跨过的静默不超过 `PAYOFF_MAX_CROSSED_SILENCE_MS`——她要是真安静了这么久，片子就是完了。

停止（任一命中即停，永远停在 cue 结束）：

- 下一条被选中候选的起点（判据 3，同时保证不会新造重叠）；
- `>= MIN_DEAD_PAUSE_MS` 的真停顿（复用 `talk_filler` 里本仓已有的 dead-pause 定义，
  不另造一个「连续语音」阈值）；
- 时长门 `max_talk_window_ms`（**钳制不丢弃**：延长到超时长就少延，绝不让候选被门毙掉）；
- 采纳硬帽 `PAYOFF_MAX_ADOPTION_MS` 与字幕覆盖终点。

收敛性：每一步都要求一次 `start_ms >= 当前终点` 的爆发，终点严格单调递增，爆发集合有限，
因此每个爆发最多被消费一次；外加 `PAYOFF_MAX_EXTENSION_STEPS` 步数帽。没有弹幕、没有爆发、
或许可不成立时**整个模块是 no-op**（＝今天的行为）——它是增量许可，不是新的门，不改变任何
既有 fail-closed 语义。

**已知边界（披露，不是缺陷）**：判据 3 的代理是「下一条**被选中**候选的起点」，质量取决于
召回覆盖；邻居不存在时退化为 dead-pause 与硬帽收敛，可能越过故事真终点。爆发按 30 秒桶
对齐检测，「切点之后的新反应 vs 存量反应」因此是桶粒度的判断，偏保守（漏延而不是乱延）。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Mapping, Sequence

from src.autoslice.danmaku_evidence import (
    DanmakuBurst,
    DanmakuItem,
    find_danmaku_bursts,
    load_danmaku_xml,
)
from src.autoslice.full_session_candidate_selector import (
    FullSessionCandidate,
    _join_text,
)
from src.autoslice.review_evidence import SourceCue
# 本仓已有的 dead pause 定义（「至少约 3 秒、没有承载画面反应或话题节奏」）。跨模块复用
# 而不是新造一个「连续语音」阈值：判断「这是不是一次真停顿」在两处是同一个问题。
from src.autoslice.talk_filler import MIN_DEAD_PAUSE_MS

AUDIENCE_PAYOFF_EXTENSION_SCHEMA = "audience-payoff-extension.v1"
AUDIENCE_PAYOFF_EXTENDED_REASON = "AUDIENCE_PAYOFF_EXTENDED"

# 允许跨过的静默上限。8/7 真值案实测被跨过的静默是 6510ms；这个数是安全天花板
# （只会让后延变短），不是内容门。
PAYOFF_MAX_CROSSED_SILENCE_MS = 15_000
# 单条候选一次修复能吃进的最大增量。8/7 真值案实测 +80060ms。
PAYOFF_MAX_ADOPTION_MS = 120_000
# 链式后延的步数帽（收敛性由「每步消费一个新爆发」保证，步数帽是第二道保险）。
PAYOFF_MAX_EXTENSION_STEPS = 4
# 爆发扫描面必须是全量：`danmaku_hints` 当时的 top-6 截断正是本案漏掉 660000-690000
# 那次爆发的直接原因。这里只放宽**返回条数**，`find_danmaku_bursts` 的检测算术
# （bucket/min_count/baseline_factor）一字不动。本常量是**确定性扫描**预算（不进 prompt，
# 所以可以给 64）；进 prompt 的名额是另一回事，见 `DANMAKU_HINT_MAX_BURSTS`。
PAYOFF_BURST_SCAN_MAX = 64


def load_payoff_danmaku_items(xml_path: Path | str | None) -> tuple[DanmakuItem, ...]:
    """Best-effort danmaku load; any failure degrades to "no signal" (no-op)."""

    if not xml_path:
        return ()
    try:
        return tuple(load_danmaku_xml(Path(xml_path)))
    except Exception:  # noqa: BLE001 — 弹幕是可选增量信号，缺了就退回今天的行为
        return ()


def _payoff_bursts(items: Sequence[DanmakuItem]) -> tuple[DanmakuBurst, ...]:
    if not items:
        return ()
    bursts = find_danmaku_bursts(items, max_bursts=PAYOFF_BURST_SCAN_MAX)
    return tuple(sorted(bursts, key=lambda burst: (burst.start_ms, burst.end_ms)))


def _licensing_burst(
    bursts: Sequence[DanmakuBurst], *, end_ms: int, resume_start_ms: int
) -> DanmakuBurst | None:
    """The burst that licenses crossing the silence after ``end_ms`` (L1 + L2)."""

    for burst in bursts:
        if burst.start_ms < end_ms or burst.end_ms <= end_ms:
            continue  # L1：必须是切点之后才发生的新反应
        if burst.start_ms <= resume_start_ms < burst.end_ms:
            return burst  # L2：语音重启时观众正在这次爆发里
    return None


def _next_candidate_start_ms(
    candidate: FullSessionCandidate,
    selected: Sequence[FullSessionCandidate],
    *,
    end_ms: int,
) -> int | None:
    """维护者 判据 3 的代理：下一条被选中候选的窗口起点。"""

    starts = [
        other.boundary.resolved_start_ms
        for other in selected
        if other is not candidate and other.boundary.resolved_start_ms >= end_ms
    ]
    return min(starts) if starts else None


def _adopt_run_end_ms(
    cues: Sequence[SourceCue], *, end_ms: int, ceiling_ms: int
) -> int:
    """Walk the contiguous speech run after ``end_ms``; return its last cue end.

    Stops before any cue that opens after a real dead pause or that would cross
    ``ceiling_ms``.  The return value is always some cue's ``source_end_ms``
    (维护者 判据 1：落点恒在人声边界), or ``end_ms`` when nothing is adoptable.
    """

    adopted_end_ms = end_ms
    previous_end_ms: int | None = None
    for cue in cues:
        if cue.source_start_ms < end_ms:
            continue
        if previous_end_ms is not None and cue.source_start_ms - previous_end_ms >= MIN_DEAD_PAUSE_MS:
            break
        if cue.source_end_ms > ceiling_ms:
            break
        adopted_end_ms = cue.source_end_ms
        previous_end_ms = cue.source_end_ms
    return adopted_end_ms


def _extend_one(
    candidate: FullSessionCandidate,
    *,
    ordered_cues: Sequence[SourceCue],
    bursts: Sequence[DanmakuBurst],
    selected: Sequence[FullSessionCandidate],
    max_talk_window_ms: int,
    merge_gap_total_ms: int,
    coverage_end_ms: int,
) -> tuple[int, list[dict[str, object]], dict[str, object]]:
    """Resolve one candidate's extended end plus its step receipts."""

    start_ms = candidate.boundary.resolved_start_ms
    original_end_ms = candidate.boundary.resolved_end_ms
    end_ms = original_end_ms
    neighbour_start_ms = _next_candidate_start_ms(candidate, selected, end_ms=end_ms)
    # 时长门用**有效时长**（扣掉跳切缝隙）计算，与召回层的时长门口径一致。
    duration_ceiling_ms = start_ms + max_talk_window_ms + merge_gap_total_ms
    ceiling_ms = min(
        neighbour_start_ms if neighbour_start_ms is not None else coverage_end_ms,
        duration_ceiling_ms,
        original_end_ms + PAYOFF_MAX_ADOPTION_MS,
        coverage_end_ms,
    )
    limits = {
        "next_candidate_start_ms": neighbour_start_ms,
        "duration_ceiling_ms": duration_ceiling_ms,
        "adoption_ceiling_ms": original_end_ms + PAYOFF_MAX_ADOPTION_MS,
        "coverage_end_ms": coverage_end_ms,
        "ceiling_ms": ceiling_ms,
    }
    steps: list[dict[str, object]] = []
    for _ in range(PAYOFF_MAX_EXTENSION_STEPS):
        resume = next(
            (cue for cue in ordered_cues if cue.source_start_ms >= end_ms), None
        )
        if resume is None:
            break
        crossed_silence_ms = resume.source_start_ms - end_ms
        if crossed_silence_ms > PAYOFF_MAX_CROSSED_SILENCE_MS:
            break  # L3
        burst = _licensing_burst(
            bursts, end_ms=end_ms, resume_start_ms=resume.source_start_ms
        )
        if burst is None:
            break
        adopted_end_ms = _adopt_run_end_ms(
            ordered_cues, end_ms=end_ms, ceiling_ms=ceiling_ms
        )
        if adopted_end_ms <= end_ms:
            break
        steps.append(
            {
                "from_end_ms": end_ms,
                "to_end_ms": adopted_end_ms,
                "crossed_silence_ms": crossed_silence_ms,
                "burst_start_ms": burst.start_ms,
                "burst_end_ms": burst.end_ms,
                "burst_count": burst.count,
            }
        )
        end_ms = adopted_end_ms
    return end_ms, steps, limits


def extend_candidates_for_audience_payoff(
    candidates: Sequence[FullSessionCandidate],
    *,
    cues: Sequence[SourceCue],
    danmaku_items: Sequence[DanmakuItem] | None = None,
    max_talk_window_ms: int,
    merge_gaps: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
) -> tuple[list[FullSessionCandidate], list[dict[str, object]]]:
    """Extend talk windows that stopped before the audience payoff landed.

    Returns ``(candidates, receipts)``.  Invariants: the candidate count and
    order never change, every resolved end is monotonically non-decreasing, and
    every extended end sits exactly on a cue end.  With no danmaku evidence the
    input list is returned untouched.
    """

    result = list(candidates)
    bursts = _payoff_bursts(danmaku_items or ())
    if not bursts or not cues:
        return result, []
    ordered_cues = sorted(
        cues, key=lambda cue: (cue.source_start_ms, cue.source_end_ms, cue.cue_id)
    )
    coverage_end_ms = max(cue.source_end_ms for cue in ordered_cues)
    receipts: list[dict[str, object]] = []
    for index, candidate in enumerate(result):
        if candidate.content_type_hint != "talk":
            continue
        candidate_id = candidate.anchor.candidate_id
        gaps = (merge_gaps or {}).get(candidate_id) or ()
        merge_gap_total_ms = sum(
            max(0, int(gap["end_ms"]) - int(gap["start_ms"]))
            for gap in gaps
            if isinstance(gap, Mapping) and "start_ms" in gap and "end_ms" in gap
        )
        extended_end_ms, steps, limits = _extend_one(
            candidate,
            ordered_cues=ordered_cues,
            bursts=bursts,
            selected=result,
            max_talk_window_ms=max_talk_window_ms,
            merge_gap_total_ms=merge_gap_total_ms,
            coverage_end_ms=coverage_end_ms,
        )
        if not steps or extended_end_ms <= candidate.boundary.resolved_end_ms:
            continue
        original_end_ms = candidate.boundary.resolved_end_ms
        window = tuple(
            cue
            for cue in ordered_cues
            if cue.source_end_ms > candidate.boundary.resolved_start_ms
            and cue.source_start_ms < extended_end_ms
        )
        boundary = replace(
            candidate.boundary,
            resolved_end_ms=extended_end_ms,
            next_end_ms=extended_end_ms,
            reason_codes=tuple(
                dict.fromkeys(
                    (*candidate.boundary.reason_codes, AUDIENCE_PAYOFF_EXTENDED_REASON)
                )
            ),
        )
        # anchor 一律不动：它是召回提示（LLM 提的窗口），也是 hooks/scorecards/
        # filler_proposals/merge_gaps 各字典的键，改了下游全部对不上。
        result[index] = replace(
            candidate,
            boundary=boundary,
            cues=window,
            text_preview=_join_text(window)[:160],
        )
        receipts.append(
            {
                "schema_version": AUDIENCE_PAYOFF_EXTENSION_SCHEMA,
                "candidate_id": candidate_id,
                "original_end_ms": original_end_ms,
                "extended_end_ms": extended_end_ms,
                "extension_ms": extended_end_ms - original_end_ms,
                "steps": steps,
                "limits": limits,
                # 评分卡描述的是**延长前**的窗口；本车道不重评分（那是 B3 的事）。
                "scorecard_describes_original_window": True,
            }
        )
    return result, receipts
