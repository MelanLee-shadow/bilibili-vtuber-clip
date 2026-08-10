"""观众 payoff 后延（2026-08-10 `auto_223750_578_654` 真值案）。

真值出处：Ivan 盲审 8/7 四条 tier-1（不看分数直接看原片），判定评分器排最低的
`auto_223750_578_654` 是唯一该发的，并要求「再往后面切一点，补全剧情」。integrator 按此
重切、Ivan 逐字确认「收尾没问题」，正确边界 **578030 → 734200**（原流水线 578030 → 654170）。
本文件的 cue 栅格与弹幕桶直方图逐条抄自 free 上的真实产物
（`cache/2026-08-07/22966160_20260807-22-37-50.bcut.srt` 与同名 blrec 弹幕 XML）。

Ivan 确认的三条收敛判据（按可靠性排序）在这里逐条有测试：
1. 落点必须在人声边界（恒为某条 cue 的结束）；
2. 必须到故事完结（把无人认领的连续语音串整段采纳）；
3. 下一话题起点是天然停止位（代理＝下一条被选中候选的起点）。
弹幕密度只做后延**许可**，不做落点判据。
"""

from __future__ import annotations

import json

import pytest

from src.autoslice.auto_review import DecisionAction
from src.autoslice.boundary_payoff_extension import (
    AUDIENCE_PAYOFF_EXTENDED_REASON,
    PAYOFF_MAX_ADOPTION_MS,
    PAYOFF_MAX_CROSSED_SILENCE_MS,
    extend_candidates_for_audience_payoff,
)
from src.autoslice.boundary_resolver import AnchorCandidate, BoundaryResolution
from src.autoslice.danmaku_evidence import DanmakuItem
from src.autoslice.full_session_candidate_selector import FullSessionCandidate
from src.autoslice.review_evidence import SourceCue
from src.autoslice.semantic_candidate_selector import (
    select_semantic_session_candidates_covered,
)
from src.autoslice.talk_filler import MIN_DEAD_PAUSE_MS

# free 上真实 bcut SRT 的 [578030, 737160] 区间，逐条原文。
REAL_CUE_ROWS: tuple[tuple[int, int, str], ...] = (
    (578030, 580150, "我想活着诶"),
    (580230, 582530, "你好你好"),
    (582740, 584300, "我们又在一起了"),
    (584300, 585480, "莉亚活着"),
    (585480, 586420, "我想活着"),
    (586420, 587140, "就算你是狼"),
    (587140, 588020, "你放过我好吗"),
    (588020, 589080, "我想活着"),
    (589080, 590900, "好的好的，放心吧"),
    (592500, 594100, "那我们一起走吗"),
    (594100, 595200, "真的能放心吗"),
    (595200, 596580, "我们一起走吗"),
    (597460, 599100, "好呀好呀，好跟我走"),
    (599100, 601140, "我做任务，对不起"),
    (602980, 604500, "对不起，我对不起"),
    (604500, 605820, "对不起对不起"),
    (614920, 616940, "你等会要写那个文笔"),
    (616940, 621000, "你好你好"),
    (634900, 636140, "怎么没人了"),
    (639140, 640460, "怎么没人了"),
    (643200, 645320, "我去我去"),
    (646200, 649120, "这人有诶，有有有有"),
    (649930, 654170, "啧，没迟到没迟到没迟到没迟到"),
    (660680, 665280, "沙德利，你还有何话说"),
    (666530, 669820, "沙口令我不是我三"),
    (669820, 671620, "我做了四个任务"),
    (671620, 673840, "北幽香一直在守护我们"),
    (673840, 674900, "听这个地方"),
    (674900, 680080, "SHAW 立在藏能吃尸体。不是我"),
    (680200, 684280, "他脚下有一个安稳阿瓦的尸体"),
    (684560, 687320, "全票打飞山"),
    (687440, 690720, "NO ，不是不是我"),
    (691900, 694420, "额，我这轮是跟 NT"),
    (694420, 696900, "NT 和爵士在一起"),
    (696900, 698780, "然后拿我杀的"),
    (698780, 701420, "没跟我们一起，难听 NT 往右走了"),
    (701450, 706890, "然后我跟这个绝世猛一在书房做了两个任务"),
    (706990, 710750, "然后绝世猛一跟我一直走一起的"),
    (710750, 713510, "我永远拥护这个绝世猛一啊"),
    (713870, 716490, "嗯，我们都是抱着任务名做的"),
    (716490, 717870, "做了两三个任务吧"),
    (717870, 719870, "然后我我们两个说一起走"),
    (719870, 722780, "结果走到，哎，走到哪儿啊"),
    (722940, 724420, "工作室还是哪里"),
    (724420, 726580, "就通马桶那个任务的地方"),
    (726710, 728110, "让我拿马桶塞进"),
    (728110, 729090, "然后那里面一堆"),
    (729090, 730890, "我还可以自爆呢"),
    (730890, 731990, "一扭头就走"),
    (731990, 734230, "我以为都能吃掉"),
    (734400, 737160, "嗯，听听沙多利怎么说了"),
)

# free 上真实弹幕 XML 的 30 秒桶直方图（bucket_index, count），全场 602 条。
# `find_danmaku_bursts` 的检测只看桶计数，所以按直方图重放与用原始 XML 完全等价，
# 判定阈值同样是 max(6, 2 × 非空桶中位数 9) = 18，爆发同样只落在 20 号桶
# （600000-630000, x24）和 22 号桶（660000-690000, x22）。
REAL_DANMAKU_BUCKETS: tuple[tuple[int, int], ...] = (
    (0, 7), (1, 3), (2, 1), (3, 11), (4, 4), (5, 1), (6, 3), (7, 12), (8, 4), (9, 3),
    (10, 22), (11, 17), (12, 14), (13, 18), (14, 3), (15, 19), (16, 18), (17, 7),
    (18, 5), (19, 8), (20, 24), (21, 11), (22, 22), (23, 15), (24, 10), (25, 6),
    (26, 4), (27, 6), (28, 6), (29, 2), (30, 9), (31, 11), (32, 6), (33, 25),
    (34, 12), (35, 11), (36, 6), (37, 13), (38, 9), (39, 12), (41, 4), (42, 4),
    (43, 1), (44, 21), (45, 21), (46, 16), (47, 9), (48, 11), (49, 11), (50, 9),
    (51, 17), (52, 29), (53, 15), (54, 10), (55, 7), (56, 2), (57, 4), (58, 8),
    (59, 3),
)

REAL_START_MS = 578030
REAL_PIPELINE_END_MS = 654170  # 流水线切出来的（包袱之前）
IVAN_CONFIRMED_END_MS = 734230  # 「我以为都能吃掉」说完那一刻；Ivan 复核的重切是 734200
NEXT_CANDIDATE_START_MS = 734400  # `auto_223750_734_822` 的起点＝下一话题


def _cues(rows=REAL_CUE_ROWS) -> tuple[SourceCue, ...]:
    return tuple(
        SourceCue(cue_id=f"c{index}", source_start_ms=start, source_end_ms=end, text=text)
        for index, (start, end, text) in enumerate(rows, start=1)
    )


def _danmaku(buckets=REAL_DANMAKU_BUCKETS) -> tuple[DanmakuItem, ...]:
    items = []
    for bucket, count in buckets:
        for offset in range(count):
            items.append(
                DanmakuItem(offset_ms=bucket * 30_000 + offset * 10, text=f"d{bucket}_{offset}")
            )
    return tuple(items)


def _candidate(
    start_ms: int, end_ms: int, cues: tuple[SourceCue, ...], *, kind: str = "talk"
) -> FullSessionCandidate:
    anchor = AnchorCandidate(
        candidate_id=f"semantic{kind}_{start_ms}_{end_ms}",
        anchor_start_ms=start_ms,
        anchor_end_ms=end_ms,
    )
    window = tuple(
        cue for cue in cues if cue.source_end_ms > start_ms and cue.source_start_ms < end_ms
    )
    return FullSessionCandidate(
        anchor=anchor,
        boundary=BoundaryResolution(
            candidate_id=anchor.candidate_id,
            action=DecisionAction.AUTO_RECUT,
            resolved_start_ms=start_ms,
            resolved_end_ms=end_ms,
            start_boundary_score=0.88,
            end_boundary_score=0.88,
            reason_codes=("SEMANTIC_RECALL",),
            next_start_ms=start_ms,
            next_end_ms=end_ms,
        ),
        cues=window,
        text_preview="".join(cue.text for cue in window)[:160],
        content_type_hint=kind,
    )


def _extend(candidates, cues, danmaku, *, max_talk_window_ms: int = 300_000, merge_gaps=None):
    return extend_candidates_for_audience_payoff(
        candidates,
        cues=cues,
        danmaku_items=danmaku,
        max_talk_window_ms=max_talk_window_ms,
        merge_gaps=merge_gaps,
    )


def _real_pair() -> tuple[tuple[SourceCue, ...], list[FullSessionCandidate]]:
    cues = _cues()
    return cues, [
        _candidate(REAL_START_MS, REAL_PIPELINE_END_MS, cues),
        _candidate(NEXT_CANDIDATE_START_MS, 737160, cues),
    ]


def test_real_20260807_case_extends_to_ivan_confirmed_boundary() -> None:
    """真值回归：流水线的 654170 后延到 Ivan 复核确认的 734230。"""

    cues, candidates = _real_pair()
    extended, receipts = _extend(candidates, cues, _danmaku())

    boundary = extended[0].boundary
    assert boundary.resolved_start_ms == REAL_START_MS  # 起点不动
    assert boundary.resolved_end_ms == IVAN_CONFIRMED_END_MS
    assert boundary.next_end_ms == IVAN_CONFIRMED_END_MS
    assert AUDIENCE_PAYOFF_EXTENDED_REASON in boundary.reason_codes
    assert "SEMANTIC_RECALL" in boundary.reason_codes  # 原回执不被抹掉
    # 判据 2：Ivan 说的第二层反转（想吃尸体没吃到就被发现）与她自己抖的包袱都进来了。
    texts = [cue.text for cue in extended[0].cues]
    assert "SHAW 立在藏能吃尸体。不是我" in texts
    assert texts[-1] == "我以为都能吃掉"
    # 判据 1：落点必须是人声边界。
    assert any(cue.source_end_ms == boundary.resolved_end_ms for cue in cues)

    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["original_end_ms"] == REAL_PIPELINE_END_MS
    assert receipt["extended_end_ms"] == IVAN_CONFIRMED_END_MS
    assert receipt["extension_ms"] == 80_060
    assert receipt["limits"]["next_candidate_start_ms"] == NEXT_CANDIDATE_START_MS
    assert receipt["scorecard_describes_original_window"] is True
    step = receipt["steps"][0]
    assert (step["burst_start_ms"], step["burst_end_ms"], step["burst_count"]) == (
        660_000,
        690_000,
        22,
    )
    assert step["crossed_silence_ms"] == 6_510


def test_next_candidate_start_stops_the_extension() -> None:
    """判据 3：下一话题（＝下一条被选中候选）的起点是硬顶，且不会新造重叠。"""

    cues, candidates = _real_pair()
    extended, _ = _extend(candidates, cues, _danmaku())

    assert extended[0].boundary.resolved_end_ms <= NEXT_CANDIDATE_START_MS
    assert extended[1].boundary.resolved_start_ms == NEXT_CANDIDATE_START_MS
    # 邻居本身一字不动。
    assert extended[1].boundary == candidates[1].boundary


def test_without_the_next_candidate_the_dead_pause_still_converges() -> None:
    """邻居不存在时不会失控：后面第一个真停顿就收（已知边界，有界不是无界）。"""

    cues = _cues()
    extended, receipts = _extend(
        [_candidate(REAL_START_MS, REAL_PIPELINE_END_MS, cues)], cues, _danmaku()
    )

    end_ms = extended[0].boundary.resolved_end_ms
    assert end_ms == 737_160  # 这段素材里 734230 之后只剩一条 cue（gap 170ms）
    assert any(cue.source_end_ms == end_ms for cue in cues)
    assert receipts[0]["limits"]["next_candidate_start_ms"] is None


def test_no_audience_burst_after_the_cut_leaves_the_boundary_untouched() -> None:
    """没有观众爆发就不许延——这正是今天的行为，模块整体 no-op。"""

    cues, candidates = _real_pair()
    quiet = tuple(
        DanmakuItem(offset_ms=index * 30_000, text="q") for index in range(60)
    )  # 每桶 1 条，全场都够不到阈值
    extended, receipts = _extend(candidates, cues, quiet)

    assert receipts == []
    assert [c.boundary for c in extended] == [c.boundary for c in candidates]


def test_missing_danmaku_is_a_no_op() -> None:
    cues, candidates = _real_pair()
    extended, receipts = _extend(candidates, cues, None)
    assert receipts == []
    assert extended[0].boundary.resolved_end_ms == REAL_PIPELINE_END_MS


def test_burst_that_started_before_the_cut_is_not_a_license() -> None:
    """存量反应不算许可：要的是切点之后才炸出来的新反应（L1）。"""

    cues = _cues()
    # 把爆发整体移到切点之前（20/21 号桶），语音重启点 660680 就不再落在任何
    # 「切点之后开始」的爆发里。
    buckets = tuple(
        (bucket, count) for bucket, count in REAL_DANMAKU_BUCKETS if bucket != 22
    ) + ((21, 24),)
    extended, receipts = _extend(
        [_candidate(REAL_START_MS, REAL_PIPELINE_END_MS, cues)], cues, _danmaku(buckets)
    )
    assert receipts == []
    assert extended[0].boundary.resolved_end_ms == REAL_PIPELINE_END_MS


def test_long_silence_blocks_the_license() -> None:
    """L3：她真安静了超过帽值，片子就是完了，弹幕再热也不许跨。"""

    rows = tuple(row for row in REAL_CUE_ROWS if row[1] <= REAL_PIPELINE_END_MS)
    resume_ms = REAL_PIPELINE_END_MS + PAYOFF_MAX_CROSSED_SILENCE_MS + 1
    rows = rows + ((resume_ms, resume_ms + 4_000, "延后开口"),)
    cues = _cues(rows)
    burst_bucket = resume_ms // 30_000
    buckets = REAL_DANMAKU_BUCKETS + ((burst_bucket, 40),)
    extended, receipts = _extend(
        [_candidate(REAL_START_MS, REAL_PIPELINE_END_MS, cues)], cues, _danmaku(buckets)
    )
    assert receipts == []
    assert extended[0].boundary.resolved_end_ms == REAL_PIPELINE_END_MS


def test_dead_pause_inside_the_run_stops_the_adoption() -> None:
    """判据 2 的护栏：采纳只走连续语音串，遇到真停顿就停。"""

    cues = _cues()
    rows = tuple(row for row in REAL_CUE_ROWS if row[1] <= 674_900)
    pause_start = 674_900 + MIN_DEAD_PAUSE_MS
    rows = rows + ((pause_start, pause_start + 3_000, "停顿之后另起一段"),)
    paused_cues = _cues(rows)
    extended, receipts = _extend(
        [_candidate(REAL_START_MS, REAL_PIPELINE_END_MS, paused_cues)],
        paused_cues,
        _danmaku(),
    )
    assert extended[0].boundary.resolved_end_ms == 674_900
    assert receipts[0]["extended_end_ms"] == 674_900
    # 对照：同样的许可、没有那个停顿时能一路走到底。
    assert (
        _extend([_candidate(REAL_START_MS, REAL_PIPELINE_END_MS, cues)], cues, _danmaku())[0][
            0
        ].boundary.resolved_end_ms
        > 674_900
    )


def test_duration_gate_clamps_instead_of_dropping() -> None:
    """时长门是钳制，不是毙掉：候选数量和顺序都不许变。"""

    cues, candidates = _real_pair()
    extended, receipts = _extend(candidates, cues, _danmaku(), max_talk_window_ms=100_000)

    assert len(extended) == len(candidates)
    assert [c.anchor.candidate_id for c in extended] == [
        c.anchor.candidate_id for c in candidates
    ]
    end_ms = extended[0].boundary.resolved_end_ms
    assert REAL_PIPELINE_END_MS < end_ms <= REAL_START_MS + 100_000
    assert any(cue.source_end_ms == end_ms for cue in cues)
    assert receipts[0]["limits"]["duration_ceiling_ms"] == REAL_START_MS + 100_000


def test_merge_gaps_are_excluded_from_the_duration_ceiling() -> None:
    """有效时长口径与召回层的时长门一致（跳切缝隙不占时长）。"""

    cues, candidates = _real_pair()
    gaps = {
        candidates[0].anchor.candidate_id: [{"start_ms": 605_820, "end_ms": 614_920}]
    }
    clamped, _ = _extend(candidates, cues, _danmaku(), max_talk_window_ms=100_000)
    with_gap, receipts = _extend(
        candidates, cues, _danmaku(), max_talk_window_ms=100_000, merge_gaps=gaps
    )
    assert (
        with_gap[0].boundary.resolved_end_ms > clamped[0].boundary.resolved_end_ms
    )
    assert receipts[0]["limits"]["duration_ceiling_ms"] == REAL_START_MS + 100_000 + 9_100


def test_adoption_cap_bounds_a_single_repair() -> None:
    assert PAYOFF_MAX_ADOPTION_MS > 0
    cues, candidates = _real_pair()
    _, receipts = _extend(candidates, cues, _danmaku())
    limits = receipts[0]["limits"]
    assert limits["adoption_ceiling_ms"] == REAL_PIPELINE_END_MS + PAYOFF_MAX_ADOPTION_MS
    assert receipts[0]["extended_end_ms"] <= limits["adoption_ceiling_ms"]


def test_song_candidates_are_never_extended() -> None:
    cues = _cues()
    song = _candidate(REAL_START_MS, REAL_PIPELINE_END_MS, cues, kind="song")
    extended, receipts = _extend([song], cues, _danmaku())
    assert receipts == []
    assert extended[0].boundary.resolved_end_ms == REAL_PIPELINE_END_MS


def test_extension_is_monotone_and_keeps_candidate_identity() -> None:
    cues, candidates = _real_pair()
    extended, _ = _extend(candidates, cues, _danmaku())
    for before, after in zip(candidates, extended):
        assert after.anchor == before.anchor  # 下游字典的键必须稳定
        assert after.boundary.resolved_start_ms == before.boundary.resolved_start_ms
        assert after.boundary.resolved_end_ms >= before.boundary.resolved_end_ms
        assert after.content_type_hint == before.content_type_hint
        assert any(cue.source_end_ms == after.boundary.resolved_end_ms for cue in cues)


def test_extension_is_idempotent() -> None:
    cues, candidates = _real_pair()
    once, _ = _extend(candidates, cues, _danmaku())
    twice, receipts = _extend(once, cues, _danmaku())
    assert receipts == []
    assert [c.boundary for c in twice] == [c.boundary for c in once]


@pytest.mark.parametrize("with_danmaku", [True, False])
def test_semantic_recall_lane_wires_the_extension(with_danmaku: bool, tmp_path) -> None:
    """端到端：历史 LLM 输出 → 真实召回代码路径 → 边界与回执。"""

    cues = _cues()
    positions = {cue.source_start_ms: index for index, cue in enumerate(cues, start=1)}
    ends = {cue.source_end_ms: index for index, cue in enumerate(cues, start=1)}
    payload = {
        "candidates": [
            {
                "start_cue": positions[REAL_START_MS],
                "end_cue": ends[REAL_PIPELINE_END_MS],
                "kind": "talk",
                "event_key": "求饶后吃莉亚",
                "hook": "小李求莉亚放过自己，转头就吃了她",
                "confidence": 0.88,
            },
            {
                "start_cue": positions[NEXT_CANDIDATE_START_MS],
                "end_cue": ends[737_160],
                "kind": "talk",
                "event_key": "听沙多利怎么说",
                "hook": "听听沙多利怎么说",
                "confidence": 0.86,
            },
        ]
    }
    xml_path = None
    if with_danmaku:
        xml_path = tmp_path / "danmaku.xml"
        nodes = "".join(
            f'<d p="{item.offset_ms / 1000:.3f},1,25,16777215,0,0,0,0">{item.text}</d>'
            for item in _danmaku()
        )
        xml_path.write_text(f"<i>{nodes}</i>", encoding="utf-8")

    selected, diagnostics = select_semantic_session_candidates_covered(
        cues,
        llm_call=lambda prompt: json.dumps(payload, ensure_ascii=False),
        max_candidates=18,
        # 第二条只有 2.76 秒，端到端里放开下限才能让「下一话题」这个停止位存在。
        min_talk_window_ms=2_000,
        danmaku_xml=xml_path,
    )

    assert len(selected) == 2
    target = next(c for c in selected if c.boundary.resolved_start_ms == REAL_START_MS)
    expected_end = IVAN_CONFIRMED_END_MS if with_danmaku else REAL_PIPELINE_END_MS
    assert target.boundary.resolved_end_ms == expected_end
    receipts = diagnostics["audience_payoff_extensions"]
    assert bool(receipts) is with_danmaku
    if with_danmaku:
        assert receipts[0]["extended_end_ms"] == IVAN_CONFIRMED_END_MS
