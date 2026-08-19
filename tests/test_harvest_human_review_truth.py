from pathlib import Path

import pytest

from scripts.harvest_human_review_truth import (
    HarvestError,
    harvest,
    harvest_cue,
    parse_srt,
    parse_truth_segments,
)


def _cue(index: int, text: str, timing: str = "00:00:01,000 --> 00:00:02,000"):
    cues = parse_srt(f"{index}\n{timing}\n{text}\n")
    return cues[0]


def test_unmarked_cue_keeps_machine_label_and_text() -> None:
    result = harvest_cue(_cue(1, "[李豆沙] 大家好"), _cue(1, "[李豆沙] 大家好"))
    assert result["truth_segments"] == [{"text": "大家好", "label": "李豆沙"}]
    assert not result["text_changed"]
    assert not result["label_changed"]
    assert not result["mixed"]
    assert not result["marked"]


def test_text_correction_without_marker() -> None:
    result = harvest_cue(_cue(2, "[李豆沙] 天不熊欺负人"), _cue(2, "[李豆沙] kmx欺负人"))
    assert result["text_changed"]
    assert not result["label_changed"]
    assert result["truth_text"] == "kmx欺负人"


def test_trailing_marker_flips_whole_cue() -> None:
    result = harvest_cue(_cue(3, "[连线] 晚上好"), _cue(3, "[连线] 晚上好 A"))
    assert result["truth_segments"] == [{"text": "晚上好", "label": "李豆沙"}]
    assert result["label_changed"]
    assert result["marked"]
    assert not result["text_changed"]


def test_multi_marker_mixed_cue_governs_back_to_previous_marker() -> None:
    result = harvest_cue(
        _cue(17, "[连线] 这就是我今小孩说是"),
        _cue(17, "[连线] 这就是我今 A 小孩说是 B"),
    )
    assert result["truth_segments"] == [
        {"text": "这就是我今", "label": "李豆沙"},
        {"text": "小孩说是", "label": "连线"},
    ]
    assert result["mixed"]
    assert result["label_changed"]
    assert not result["text_changed"]
    assert result["truth_text"] == "这就是我今 小孩说是"


def test_unmarked_remainder_after_marker_keeps_machine_label() -> None:
    result = harvest_cue(
        _cue(4, "[连线] 你先说后面我来"),
        _cue(4, "[连线] 你先说 A 后面我来"),
    )
    assert result["truth_segments"] == [
        {"text": "你先说", "label": "李豆沙"},
        {"text": "后面我来", "label": "连线"},
    ]
    assert result["mixed"]


def test_moved_boundary_across_machine_space_is_not_a_text_change() -> None:
    result = harvest_cue(
        _cue(59, "[连线] 你知道 我要偶遇！偶遇"),
        _cue(59, "[连线] 你知道我要 A 偶遇！偶遇 B"),
    )
    assert not result["text_changed"]
    assert result["mixed"]
    assert result["truth_segments"][0] == {"text": "你知道我要", "label": "李豆沙"}


def test_letters_inside_words_are_not_markers() -> None:
    result = harvest_cue(
        _cue(5, "[连线] 我是PSP的最大善人啊"),
        _cue(5, "[连线] 我是PSP的最大善人啊"),
    )
    assert not result["marked"]
    result = harvest_cue(_cue(6, "[连线] OK好的BW"), _cue(6, "[连线] OK好的BW"))
    assert not result["marked"]


def test_trailing_spaces_are_stripped_before_matching() -> None:
    result = harvest_cue(_cue(7, "[连线] 好柔弱啊"), _cue(7, "[连线] 好柔弱啊 A  "))
    assert result["truth_segments"] == [{"text": "好柔弱啊", "label": "李豆沙"}]


def test_uniform_era_file_without_prefix() -> None:
    result = harvest_cue(_cue(8, "谢谢大家"), _cue(8, "谢谢大家"))
    assert result["machine_label"] is None
    assert result["truth_segments"] == [{"text": "谢谢大家", "label": None}]
    marked = harvest_cue(_cue(9, "谢谢大家"), _cue(9, "谢谢大家 B"))
    assert marked["truth_segments"] == [{"text": "谢谢大家", "label": "连线"}]
    assert marked["label_changed"]


def test_marker_with_text_edit_in_same_cue() -> None:
    result = harvest_cue(
        _cue(10, "[连线] 法医当一当二不如当三"),
        _cue(10, "[连线] 法衣 A 当一当二不如当三"),
    )
    assert result["truth_segments"][0] == {"text": "法衣", "label": "李豆沙"}
    assert result["text_changed"]
    assert result["mixed"]


def test_pristine_marker_shaped_token_fails_closed() -> None:
    with pytest.raises(HarvestError, match="marker-shaped"):
        harvest_cue(_cue(11, "[连线] 计划 A 启动"), _cue(11, "[连线] 计划 A 启动"))


def test_label_prefix_edit_fails_closed() -> None:
    with pytest.raises(HarvestError, match="label prefix"):
        harvest_cue(_cue(12, "[连线] 大家好"), _cue(12, "[李豆沙] 大家好"))


def test_timing_drift_fails_closed() -> None:
    with pytest.raises(HarvestError, match="timing drift"):
        harvest_cue(
            _cue(13, "[连线] 大家好"),
            _cue(13, "[连线] 大家好", timing="00:00:01,000 --> 00:00:03,000"),
        )


def test_empty_segment_fails_closed() -> None:
    with pytest.raises(HarvestError, match="empty text"):
        parse_truth_segments(" A 后面", "连线")


def test_cue_count_mismatch_fails_closed(tmp_path: Path) -> None:
    pristine = tmp_path / "p.srt"
    annotated = tmp_path / "a.srt"
    pristine.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n[李豆沙] 你好\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\n[连线] 再见\n",
        encoding="utf-8",
    )
    annotated.write_text("1\n00:00:01,000 --> 00:00:02,000\n[李豆沙] 你好\n", encoding="utf-8")
    with pytest.raises(HarvestError, match="cue count mismatch"):
        harvest(pristine, annotated, "auto_test", "unit")


def test_harvest_artifact_shape(tmp_path: Path) -> None:
    pristine = tmp_path / "p.srt"
    annotated = tmp_path / "a.srt"
    pristine.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n[李豆沙] 你好\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\n[连线] 这就是我今小孩说是\n",
        encoding="utf-8",
    )
    annotated.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n[李豆沙] 你好\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\n[连线] 这就是我今 A 小孩说是 B\n",
        encoding="utf-8",
    )
    artifact = harvest(pristine, annotated, "auto_test", "unit")
    assert artifact["schema"] == "维护者-speaker-truth-diff.v2"
    assert artifact["summary"] == {
        "cues": 2,
        "text_changed": 0,
        "label_changed": 1,
        "mixed": 1,
        "marked": 1,
        "merged": 0,
        "timing_tweaked": 0,
    }
    assert artifact["cues"][1]["truth_segments"][0]["label"] == "李豆沙"


def test_harvest_merge_requires_tolerance(tmp_path: Path) -> None:
    pristine = tmp_path / "p.srt"
    annotated = tmp_path / "a.srt"
    pristine.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n甲说\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\n乙说\n",
        encoding="utf-8",
    )
    annotated.write_text(
        "1\n00:00:01,001 --> 00:00:02,950\n甲说 B 乙改 A\n",
        encoding="utf-8",
    )
    with pytest.raises(HarvestError, match="cue count mismatch"):
        harvest(pristine, annotated, "auto_test", "unit")
    artifact = harvest(
        pristine, annotated, "auto_test", "unit", timing_tolerance_ms=100
    )
    assert artifact["summary"]["merged"] == 1
    row = artifact["cues"][0]
    assert row["merged_from"] == [1, 2]
    assert row["machine_text"] == "甲说 乙说"
    assert row["truth_segments"] == [
        {"text": "甲说", "label": "连线"},
        {"text": "乙改", "label": "李豆沙"},
    ]
    assert row["text_changed"] is True and row["mixed"] is True


def test_harvest_merge_rejects_unmatched_deletion(tmp_path: Path) -> None:
    pristine = tmp_path / "p.srt"
    annotated = tmp_path / "a.srt"
    pristine.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n甲说\n\n"
        "2\n00:00:05,000 --> 00:00:06,000\n乙说\n",
        encoding="utf-8",
    )
    annotated.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n甲说\n",
        encoding="utf-8",
    )
    with pytest.raises(HarvestError):
        harvest(pristine, annotated, "auto_test", "unit", timing_tolerance_ms=100)


def test_marker_followed_by_cjk_punctuation(tmp_path: Path) -> None:
    pristine = tmp_path / "p.srt"
    annotated = tmp_path / "a.srt"
    pristine.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n不是，不是吗\n", encoding="utf-8"
    )
    annotated.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n不是 A，不是吗 B\n", encoding="utf-8"
    )
    artifact = harvest(pristine, annotated, "auto_test", "unit")
    row = artifact["cues"][0]
    assert row["truth_segments"] == [
        {"text": "不是", "label": "李豆沙"},
        {"text": "，不是吗", "label": "连线"},
    ]
    assert row["text_changed"] is False
    assert row["mixed"] is True
