import hashlib
import json

import pytest

from src.autoslice.auto_review import DecisionAction
from src.autoslice.llm_client import LlmCallError
from src.autoslice.review_evidence import SourceCue
from src.autoslice.semantic_candidate_selector import (
    build_semantic_recall_prompt,
    select_semantic_session_candidates,
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


def test_default_profile_keeps_pre_profile_semantic_prompt_byte_identical():
    prompt = build_semantic_recall_prompt(
        [SourceCue("c1", 1_000, 3_000, "测试")],
        max_candidates=2,
        danmaku_hints="00:01 burst",
    )

    assert hashlib.sha256(prompt.encode()).hexdigest() == (
        "d15bf3e86469bde0505b8a4b3273820e0cc45933684319bc484b890d24dbcc51"
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
