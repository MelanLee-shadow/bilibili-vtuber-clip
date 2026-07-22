import hashlib
import json

import pytest

from src.autoslice.auto_review import DecisionAction
from src.autoslice.llm_client import LlmCallError
from src.autoslice.review_evidence import SourceCue
from src.autoslice.semantic_candidate_selector import (
    build_semantic_recall_prompt,
    plan_semantic_recall_shards,
    select_semantic_session_candidates,
    select_semantic_session_candidates_covered,
)


def _cues(count: int = 60, cue_ms: int = 5_000, gap_ms: int = 1_000) -> list[SourceCue]:
    cues = []
    cursor = 0
    for index in range(1, count + 1):
        cues.append(
            SourceCue(
                cue_id=f"u_{index:06d}",
                source_start_ms=cursor,
                source_end_ms=cursor + cue_ms,
                text=f"第{index}句话",
                language="zh",
                kind="speech",
                confidence=1.0,
            )
        )
        cursor += cue_ms + gap_ms
    return cues


def _completion(candidates: list[dict]) -> str:
    return json.dumps({"candidates": candidates}, ensure_ascii=False)


def test_prompt_is_viewer_perspective_and_lists_all_cues():
    cues = _cues(5)
    prompt = build_semantic_recall_prompt(cues, max_candidates=3)
    assert "观众视角" in prompt
    assert "弹幕" in prompt
    assert "context_trigger_cue" in prompt
    assert "只播放原唱" in prompt
    assert "背景音乐都不是歌切" in prompt
    assert "#1 " in prompt and "#5 " in prompt
    assert "第5句话" in prompt


def test_long_session_recall_is_sharded_with_overlap_and_covers_the_second_hour():
    cues = _cues(count=360, cue_ms=20_000, gap_ms=0)  # exactly two hours
    shards = plan_semantic_recall_shards(cues)

    assert len(shards) == 4
    assert [shard["core_start_ms"] for shard in shards] == [
        0,
        1_800_000,
        3_600_000,
        5_400_000,
    ]
    assert shards[1]["window_start_ms"] == 1_680_000
    assert shards[1]["window_end_ms"] == 3_720_000
    assert shards[-1]["window_end_ms"] == 7_200_000

    calls: list[str] = []

    def llm(prompt: str) -> str:
        calls.append(prompt)
        number = len(calls)
        return _completion(
            [
                {
                    "start_cue": 10,
                    "end_cue": 13,
                    "kind": "talk",
                    "event_key": f"分窗事件{number}",
                    "hook": f"分窗候选{number}",
                    "confidence": 0.95 - number / 100,
                }
            ]
        )

    selected, diagnostics = select_semantic_session_candidates_covered(
        cues,
        llm_call=llm,
        max_candidates=12,
    )

    assert len(calls) == 4
    assert all("整场直播的一个覆盖窗" in prompt for prompt in calls)
    assert len(selected) == 4
    assert selected[-1].anchor.anchor_start_ms >= 5_400_000
    assert diagnostics["mode"] == "sharded"
    assert diagnostics["coverage_end_ms"] == 7_200_000
    assert len(diagnostics["shards"]) == 4


def test_default_profile_semantic_prompt_policy_fingerprint():
    prompt = build_semantic_recall_prompt(
        [SourceCue("c1", 1_000, 3_000, "测试")],
        max_candidates=2,
        danmaku_hints="00:01 burst",
    )

    # 2026-07-19：event_key 增补「同主题隔段再触发也用同一 key」规则后的指纹。
    assert hashlib.sha256(prompt.encode()).hexdigest() == (
        "f4474d7898efc4dfc2aabf9a9fd618a81f3c97cb1d9c25b82eb9971fc013975d"
    )


def test_selects_talk_candidate_with_semantic_boundary():
    cues = _cues()

    def llm(prompt: str) -> str:
        return _completion(
            [{"start_cue": 10, "end_cue": 20, "kind": "talk", "hook": "弹幕接梗", "context_trigger_cue": None, "context_inferable": True, "confidence": 0.9}]
        )

    selected, diagnostics = select_semantic_session_candidates(cues, llm_call=llm, max_candidates=3)
    assert len(selected) == 1
    candidate = selected[0]
    assert candidate.content_type_hint == "talk"
    assert candidate.anchor.candidate_id.startswith("semantictalk_")
    assert candidate.boundary.action == DecisionAction.AUTO_RECUT
    assert candidate.boundary.resolved_start_ms == cues[9].source_start_ms
    assert candidate.boundary.resolved_end_ms == cues[19].source_end_ms
    assert "SEMANTIC_RECALL" in candidate.boundary.reason_codes
    assert diagnostics["hooks"][candidate.anchor.candidate_id] == "弹幕接梗"


def test_semantic_recall_carries_bounded_filler_proposals_to_diagnostics():
    cues = _cues()

    def llm(prompt: str) -> str:
        return _completion(
            [
                {
                    "start_cue": 5,
                    "end_cue": 20,
                    "kind": "talk",
                    "hook": "礼物致谢后继续原话题",
                    "confidence": 0.95,
                    "filler_removals": [
                        {
                            "mode": "remove_cues",
                            "start_cue": "#10",
                            "end_cue": 11.0,
                            "reason": "gift_thanks",
                            "bridge_coherent": True,
                            "bridge": "第9句和第12句在讲同一个话题",
                            "confidence": 0.98,
                        }
                    ],
                }
            ]
        )

    selected, diagnostics = select_semantic_session_candidates(
        cues, llm_call=llm, max_candidates=3
    )

    candidate_id = selected[0].anchor.candidate_id
    proposal = diagnostics["filler_proposals"][candidate_id][0]
    assert proposal["start_cue"] == 10
    assert proposal["end_cue"] == 11
    assert proposal["reason"] == "gift_thanks"
    assert proposal["bridge_coherent"] is True


def test_context_trigger_extends_window_start():
    cues = _cues()

    def llm(prompt: str) -> str:
        return _completion(
            [{"start_cue": 15, "end_cue": 25, "kind": "talk", "hook": "读弹幕后破防", "context_trigger_cue": 12, "context_inferable": False, "confidence": 0.8}]
        )

    selected, _ = select_semantic_session_candidates(cues, llm_call=llm, max_candidates=3)
    assert selected[0].boundary.resolved_start_ms == cues[11].source_start_ms


def test_context_trigger_beyond_backtrack_cap_is_ignored():
    cues = _cues(count=200)

    def llm(prompt: str) -> str:
        return _completion(
            [{"start_cue": 180, "end_cue": 190, "kind": "talk", "hook": "x", "context_trigger_cue": 1, "context_inferable": True, "confidence": 0.7}]
        )

    selected, _ = select_semantic_session_candidates(cues, llm_call=llm, max_candidates=3)
    assert selected[0].boundary.resolved_start_ms == cues[179].source_start_ms


def test_song_candidate_gets_full_source_boundary_redo():
    cues = _cues()

    def llm(prompt: str) -> str:
        return _completion(
            [{"start_cue": 5, "end_cue": 50, "kind": "song", "hook": "唱歌", "context_trigger_cue": None, "context_inferable": True, "confidence": 0.95}]
        )

    selected, _ = select_semantic_session_candidates(cues, llm_call=llm, max_candidates=3)
    candidate = selected[0]
    assert candidate.content_type_hint == "song"
    assert candidate.anchor.candidate_id.startswith("semanticsong_")
    assert "SONG_BOUNDARY_REDO_REQUIRED" in candidate.boundary.reason_codes
    job = candidate.to_source_context_job(source_duration_ms=cues[-1].source_end_ms)
    assert job["timeline"]["context_start_ms"] == 0
    assert job["timeline"]["context_end_ms"] == cues[-1].source_end_ms


def test_distinct_adjacent_semantic_songs_are_not_deduped_by_gap_alone():
    cues = _cues(count=80)

    def llm(prompt: str) -> str:
        return _completion(
            [
                {"start_cue": 5, "end_cue": 30, "kind": "song", "hook": "画面歌名《第一首》", "confidence": 0.95},
                {"start_cue": 31, "end_cue": 55, "kind": "song", "hook": "画面歌名《第二首》", "confidence": 0.94},
            ]
        )

    selected, _ = select_semantic_session_candidates(cues, llm_call=llm, max_candidates=4)

    assert len(selected) == 2
    assert [candidate.content_type_hint for candidate in selected] == ["song", "song"]


def test_out_of_range_and_too_short_candidates_are_skipped():
    cues = _cues()

    def llm(prompt: str) -> str:
        return _completion(
            [
                {"start_cue": 999, "end_cue": 1005, "kind": "talk", "hook": "越界", "confidence": 0.9},
                {"start_cue": 3, "end_cue": 3, "kind": "talk", "hook": "太短", "confidence": 0.9},
                {"start_cue": 30, "end_cue": 40, "kind": "talk", "hook": "正常", "confidence": 0.6},
            ]
        )

    selected, diagnostics = select_semantic_session_candidates(cues, llm_call=llm, max_candidates=3)
    assert [c.anchor.candidate_id.startswith("semantictalk_") for c in selected] == [True]
    assert any(item.get("reason") == "cue_range_invalid" for item in diagnostics["skipped"])
    assert any(item.get("reason") == "talk_duration_out_of_range" for item in diagnostics["skipped"])


def test_overlapping_candidates_are_deduped_by_confidence():
    cues = _cues()

    def llm(prompt: str) -> str:
        return _completion(
            [
                {"start_cue": 10, "end_cue": 20, "kind": "talk", "hook": "高分", "confidence": 0.95},
                {"start_cue": 12, "end_cue": 22, "kind": "talk", "hook": "重叠低分", "confidence": 0.60},
            ]
        )

    selected, diagnostics = select_semantic_session_candidates(cues, llm_call=llm, max_candidates=3)
    assert len(selected) == 1
    assert diagnostics["hooks"][selected[0].anchor.candidate_id] == "高分"
    assert any(item.get("reason") == "overlaps_selected" for item in diagnostics["skipped"])


def test_same_event_key_merges_nonoverlapping_windows_before_top_n_quota():
    cues = _cues(count=70)

    def llm(prompt: str) -> str:
        return _completion(
            [
                {
                    "start_cue": 5,
                    "end_cue": 15,
                    "kind": "talk",
                    "event_key": "妈感姐妹分类",
                    "hook": "弹幕先问妈感姐还是妈感妹",
                    "confidence": 0.92,
                },
                {
                    "start_cue": 20,
                    "end_cue": 40,
                    "kind": "talk",
                    "event_key": "妈感姐妹分类",
                    "hook": "接着分类Kaya并给出绯闻女友包袱",
                    "confidence": 0.88,
                },
                {
                    "start_cue": 50,
                    "end_cue": 60,
                    "kind": "talk",
                    "event_key": "另一个事件",
                    "hook": "另一件事",
                    "confidence": 0.80,
                },
            ]
        )

    selected, diagnostics = select_semantic_session_candidates(
        cues, llm_call=llm, max_candidates=2
    )

    assert len(selected) == 2
    merged = selected[0]
    assert merged.boundary.resolved_start_ms == cues[4].source_start_ms
    assert merged.boundary.resolved_end_ms == cues[39].source_end_ms
    # 2026-07-19 同主题合并跳切：25s 缝隙成为 merge_gap（由 talk_filler 的
    # merge_gap 车道在成品里跳切掉），审计带 merge_outcome 与缝隙毫秒区间。
    assert diagnostics["merged_events"] == [
        {
            "event_key": "妈感姐妹分类",
            "input_ranges": [[5, 15], [20, 40]],
            "merge_gaps_ms": [[89000, 114000]],
            "merge_outcome": "merged",
            "merged_range": [5, 40],
        }
    ]
    merged_cid = merged.anchor.candidate_id
    assert diagnostics["merge_gaps"][merged_cid] == [
        {"start_ms": 89000, "end_ms": 114000, "event_key": "妈感姐妹分类"}
    ]


def test_cue_positions_accept_llm_number_formats():
    cues = _cues()

    def llm(prompt: str) -> str:
        return _completion(
            [{"start_cue": "10", "end_cue": 20.0, "kind": "talk", "hook": "字符串和浮点编号", "context_trigger_cue": "#8", "context_inferable": True, "confidence": 0.9}]
        )

    selected, _ = select_semantic_session_candidates(cues, llm_call=llm, max_candidates=3)
    assert len(selected) == 1
    assert selected[0].boundary.resolved_start_ms == cues[7].source_start_ms
    assert selected[0].boundary.resolved_end_ms == cues[19].source_end_ms


def test_bad_completion_raises_llm_call_error():
    cues = _cues()
    with pytest.raises(LlmCallError):
        select_semantic_session_candidates(cues, llm_call=lambda prompt: "not json at all", max_candidates=3)
    with pytest.raises(LlmCallError):
        select_semantic_session_candidates(cues, llm_call=lambda prompt: json.dumps({"candidates": "nope"}), max_candidates=3)


def test_prompt_injects_slice_selection_metric_asset(tmp_path, monkeypatch):
    """Ivan's curated selection metric (assets/lidousha/slice_selection_metric.md)
    must reach the recall prompt so unattended selection follows his taste."""
    metric = tmp_path / "metric.md"
    metric.write_text("# metric\n观点/立场强度测试标记词", encoding="utf-8")
    monkeypatch.setenv("LIDOUSHA_SLICE_METRIC", str(metric))
    prompt = build_semantic_recall_prompt(_cues(3), max_candidates=2)
    assert "观点/立场强度测试标记词" in prompt
    assert "选题优先级 metric" in prompt


def test_prompt_survives_missing_metric_asset(tmp_path, monkeypatch):
    monkeypatch.setenv("LIDOUSHA_SLICE_METRIC", str(tmp_path / "absent.md"))
    prompt = build_semantic_recall_prompt(_cues(3), max_candidates=2)
    assert "观众视角" in prompt
    assert "选题优先级 metric" not in prompt


def test_repo_metric_asset_reaches_prompt_by_default():
    prompt = build_semantic_recall_prompt(_cues(3), max_candidates=2)
    assert "观点/立场强度" in prompt  # from assets/lidousha/slice_selection_metric.md
    assert "不许因「niche/otaku 向」武断压低" in prompt


def test_same_topic_merge_with_8min_gap_produces_merge_gap_jumpcut():
    """2026-07-18 kmx 称呼两条切片案（源间距 8min25s）：同一 event_key 的
    分离窗口按有效时长过门、缝隙成为 merge_gap，而不是被 5 分钟窗口上限
    整条毙掉或分成两条切片。"""
    cues = _cues(count=200, cue_ms=5_000, gap_ms=1_000)

    def llm(prompt: str) -> str:
        return _completion(
            [
                # 两段各 ~1min，中间隔 ~8.4min（85 个 cue × 6s）。
                {"start_cue": 5, "end_cue": 15, "kind": "talk",
                 "event_key": "kmx称呼串", "hook": "SC称呼串起头", "confidence": 0.9},
                {"start_cue": 100, "end_cue": 112, "kind": "talk",
                 "event_key": "kmx称呼串", "hook": "回访同一个梗", "confidence": 0.85},
            ]
        )

    selected, diagnostics = select_semantic_session_candidates(
        cues, llm_call=llm, max_candidates=2
    )

    assert len(selected) == 1
    merged = selected[0]
    assert merged.boundary.resolved_start_ms == cues[4].source_start_ms
    assert merged.boundary.resolved_end_ms == cues[111].source_end_ms
    event = diagnostics["merged_events"][0]
    assert event["merge_outcome"] == "merged"
    gaps = diagnostics["merge_gaps"][merged.anchor.candidate_id]
    assert len(gaps) == 1
    gap_ms = gaps[0]["end_ms"] - gaps[0]["start_ms"]
    assert 500_000 < gap_ms <= 600_000


def test_same_topic_merge_gap_over_cap_keeps_winner_only():
    """缝隙超过 10 分钟上限时不盲扫中间内容：保置信度最高的一段，审计记录。"""
    cues = _cues(count=300, cue_ms=5_000, gap_ms=1_000)

    def llm(prompt: str) -> str:
        return _completion(
            [
                {"start_cue": 5, "end_cue": 15, "kind": "talk",
                 "event_key": "超远同主题", "hook": "第一段", "confidence": 0.9},
                {"start_cue": 250, "end_cue": 262, "kind": "talk",
                 "event_key": "超远同主题", "hook": "第二段", "confidence": 0.7},
            ]
        )

    selected, diagnostics = select_semantic_session_candidates(
        cues, llm_call=llm, max_candidates=2
    )

    assert len(selected) == 1
    winner = selected[0]
    assert winner.boundary.resolved_start_ms == cues[4].source_start_ms
    assert winner.boundary.resolved_end_ms == cues[14].source_end_ms
    event = diagnostics["merged_events"][0]
    assert event["merge_outcome"] == "kept_winner_gap_too_large"
    assert diagnostics["merge_gaps"] == {}
