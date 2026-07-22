import json
from pathlib import Path

import pytest

from src.autoslice.source_subtitle_truth import apply_source_subtitle_truth


REPO_ROOT = Path(__file__).resolve().parents[1]


def _srt(*rows: tuple[int, int, str]) -> str:
    blocks = []
    for index, (start, end, text) in enumerate(rows, start=1):
        blocks.append(
            f"{index}\n00:00:{start:02d},000 --> 00:00:{end:02d},000\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def _srt_ms(*rows: tuple[int, int, str]) -> str:
    def timestamp(value: int) -> str:
        seconds, millis = divmod(value, 1_000)
        return f"00:00:{seconds:02d},{millis:03d}"

    blocks = []
    for index, (start, end, text) in enumerate(rows, start=1):
        blocks.append(
            f"{index}\n{timestamp(start)} --> {timestamp(end)}\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def _ledger(tmp_path, entries):
    path = tmp_path / "truth.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "source-subtitle-truth-ledger.v1",
                "entries": entries,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_source_interval_truth_applies_to_every_overlapping_candidate(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "same-source",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 114_000,
                "action": "replace_cue",
                "text": "kmx在线下叫李豆沙",
                "authority": "Ivan",
                "required": True,
            }
        ],
    )
    for piece_start, cue_start, cue_end in (
        (100_000, 10, 14),
        (105_000, 5, 9),
    ):
        corrected, audit = apply_source_subtitle_truth(
            _srt((cue_start, cue_end, "提莫怂在线下叫李豆莎")),
            spec={
                "pieces": [
                    {
                        "remote_media": "/source/recording.mp4",
                        "start_ms": piece_start,
                        "end_ms": piece_start + 20_000,
                    }
                ]
            },
            durations=[20_000],
            ledger_path=ledger,
        )
        assert "kmx在线下叫李豆沙" in corrected
        assert audit["status"] == "APPLIED"


def test_replace_cue_can_pin_reviewed_spoken_start_after_silent_prefix(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "silent-hallucinated-prefix",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 114_000,
                "action": "replace_cue",
                "text": "乱说的啊",
                "spoken_start_ms": 111_500,
                "authority": "Ivan plus bounded raw-audio onset check",
                "required": True,
            }
        ],
    )
    source = _srt_ms((10_000, 14_000, "我草，乱说的啊"))
    corrected, audit = apply_source_subtitle_truth(
        source,
        spec={
            "pieces": [
                {
                    "remote_media": "/source/recording.mp4",
                    "start_ms": 100_000,
                    "end_ms": 120_000,
                }
            ]
        },
        durations=[20_000],
        ledger_path=ledger,
    )

    assert "00:00:11,500 --> 00:00:14,000" in corrected
    assert "乱说的啊" in corrected
    assert "我草" not in corrected
    assert audit["status"] == "APPLIED"
    assert audit["applied"][0]["timing_pin"] == {
        "source_spoken_start_ms": 111_500,
        "before_start_ms": 10_000,
        "after_start_ms": 11_500,
    }


def test_spoken_start_pin_fails_closed_when_target_is_not_unique(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "ambiguous-onset",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 114_000,
                "action": "replace_cue",
                "text": "审定文本",
                "spoken_start_ms": 111_500,
                "required": True,
            }
        ],
    )
    source = _srt_ms(
        (10_000, 12_000, "第一条"),
        (12_000, 14_000, "第二条"),
    )
    corrected, audit = apply_source_subtitle_truth(
        source,
        spec={
            "pieces": [
                {
                    "remote_media": "/source/recording.mp4",
                    "start_ms": 100_000,
                    "end_ms": 120_000,
                }
            ]
        },
        durations=[20_000],
        ledger_path=ledger,
    )

    assert corrected == source
    assert audit["status"] == "FAILED"
    assert audit["failures"][0]["reason_code"] == (
        "SPOKEN_START_TARGET_NOT_UNIQUE"
    )


def test_source_interval_truth_does_not_leak_to_other_recording(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "bound-source",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 114_000,
                "action": "replace_cue",
                "text": "kmx",
                "required": True,
            }
        ],
    )
    original = _srt((10, 14, "提防"))
    corrected, audit = apply_source_subtitle_truth(
        original,
        spec={
            "pieces": [
                {
                    "remote_media": "/source/other.mp4",
                    "start_ms": 100_000,
                    "end_ms": 120_000,
                }
            ]
        },
        durations=[20_000],
        ledger_path=ledger,
    )
    assert corrected == original
    assert audit["status"] == "NO_RELEVANT_INTERVAL"


def test_drop_cue_requires_full_containment_and_renumbers(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "silent-hallucination",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 114_000,
                "action": "drop_cue",
                "authority": "Ivan",
                "required": True,
            }
        ],
    )
    spec = {
        "pieces": [
            {
                "remote_media": "/source/recording.mp4",
                "start_ms": 100_000,
                "end_ms": 120_000,
            }
        ]
    }
    source = _srt((8, 10, "前一句"), (10, 14, "无声幻听"), (14, 18, "后一句"))

    corrected, audit = apply_source_subtitle_truth(
        source,
        spec=spec,
        durations=[20_000],
        ledger_path=ledger,
    )

    assert "无声幻听" not in corrected
    assert "1\n00:00:08,000" in corrected
    assert "2\n00:00:14,000" in corrected
    assert "3\n" not in corrected
    assert audit["status"] == "APPLIED"
    assert audit["applied"][0]["drop_status"] == "APPLIED"

    second, second_audit = apply_source_subtitle_truth(
        corrected,
        spec=spec,
        durations=[20_000],
        ledger_path=ledger,
    )
    assert second == corrected
    assert second_audit["status"] == "ALREADY_SATISFIED"
    assert second_audit["satisfied"][0]["drop_status"] == "ALREADY_ABSENT"


def test_drop_cue_fails_closed_when_cue_straddles_truth_interval(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "do-not-over-delete",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 114_000,
                "action": "drop_cue",
                "authority": "Ivan",
                "required": True,
            }
        ],
    )
    source = _srt((9, 15, "真实前半句，无声后半句"), (15, 18, "后一句"))

    corrected, audit = apply_source_subtitle_truth(
        source,
        spec={
            "pieces": [
                {
                    "remote_media": "/source/recording.mp4",
                    "start_ms": 100_000,
                    "end_ms": 120_000,
                }
            ]
        },
        durations=[20_000],
        ledger_path=ledger,
    )

    assert corrected == source
    assert audit["status"] == "FAILED"
    assert audit["failures"][0]["reason_code"] == (
        "DROP_CUE_STRADDLES_TRUTH_INTERVAL"
    )
    assert audit["failures"][0]["drop_status"] == "CONFLICT"


def test_piece_duration_jitter_does_not_capture_adjacent_cue(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "encoded-duration-jitter",
                "recording_basename": "recording.mp4",
                "source_start_ms": 21_000,
                "source_end_ms": 22_000,
                "action": "replace_cue",
                "text": "正确文本",
                "required": True,
            }
        ],
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms(
            (11_000, 12_000, "误听文本"),
            (12_000, 13_000, "下一句"),
        ),
        spec={
            "pieces": [
                {
                    "remote_media": "/source/recording.mp4",
                    "start_ms": 0,
                    "end_ms": 10_000,
                },
                {
                    "remote_media": "/source/recording.mp4",
                    "start_ms": 20_000,
                    "end_ms": 30_000,
                },
            ]
        },
        durations=[10_003, 10_000],
        ledger_path=ledger,
    )

    assert "正确文本" in corrected
    assert "下一句" in corrected
    assert audit["status"] == "APPLIED"
    assert audit["applied"][0]["cue_indexes"] == [1]


def test_source_interval_substring_repair_is_local_and_idempotent(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "emoticon-reading",
                "recording_basename": "recording.mp4",
                "source_start_ms": 210_000,
                "source_end_ms": 216_000,
                "action": "replace_substring",
                "replacements": [
                    {"surface": "风光风光", "canonical": "分号分号"},
                    {"surface": "封号封号", "canonical": "分号分号"},
                ],
                "required_text": "分号分号",
                "required": True,
            }
        ],
    )
    spec = {
        "pieces": [
            {
                "remote_media": "/source/recording.mp4",
                "start_ms": 200_000,
                "end_ms": 220_000,
            }
        ]
    }
    corrected, audit = apply_source_subtitle_truth(
        _srt((10, 16, "主播念成风光风光了")),
        spec=spec,
        durations=[20_000],
        ledger_path=ledger,
    )
    assert "分号分号" in corrected
    assert audit["status"] == "APPLIED"

    second, second_audit = apply_source_subtitle_truth(
        corrected,
        spec=spec,
        durations=[20_000],
        ledger_path=ledger,
    )
    assert second == corrected
    assert second_audit["status"] == "ALREADY_SATISFIED"


def test_july18_hecheng_kmx_truth_handles_downstream_third_homophone():
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms((25_730, 27_960, "我这真的有一些题")),
        spec={
            "pieces": [
                {
                    "remote_media": (
                        "/source/22966160_20260718-23-59-36.mp4"
                    ),
                    # 窗口止于 kmx 钉区间末端：2026-07-19 起同一录播 545.5s
                    # 处还有「为什么幻听」钉，本测试只回放 kmx 三重同音案。
                    "start_ms": 517_500,
                    "end_ms": 545_460,
                }
            ]
        },
        durations=[27_960],
        ledger_path=(
            REPO_ROOT / "assets/lidousha/subtitle_truth_ledger.v1.json"
        ),
    )

    assert "我这真的有一些kmx" in corrected
    assert audit["status"] == "APPLIED"
    assert not audit["failures"]


def test_required_included_truth_fails_closed_when_timeline_has_no_cue(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "required",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 114_000,
                "action": "replace_cue",
                "text": "正确文本",
                "required": True,
            }
        ],
    )
    _corrected, audit = apply_source_subtitle_truth(
        _srt((1, 2, "别处")),
        spec={
            "pieces": [
                {
                    "remote_media": "/source/recording.mp4",
                    "start_ms": 100_000,
                    "end_ms": 120_000,
                }
            ]
        },
        durations=[20_000],
        ledger_path=ledger,
    )
    assert audit["status"] == "FAILED"
    assert audit["failures"][0]["reason_code"] == "REPLACE_CUE_TARGET_NOT_UNIQUE"


def test_required_truth_fails_closed_when_candidate_cuts_through_interval(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "partial-phrase",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 114_000,
                "action": "replace_cue",
                "text": "完整原话",
                "required": True,
            }
        ],
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt((0, 2, "半句")),
        spec={
            "pieces": [
                {
                    "remote_media": "/source/recording.mp4",
                    "start_ms": 112_000,
                    "end_ms": 120_000,
                }
            ]
        },
        durations=[8_000],
        ledger_path=ledger,
    )

    assert "完整原话" not in corrected
    assert audit["status"] == "FAILED"
    assert audit["failures"][0]["reason_code"] == (
        "SOURCE_INTERVAL_PARTIALLY_RETAINED"
    )


def test_symlink_ledger_is_rejected(tmp_path):
    real = _ledger(tmp_path, [])
    linked = tmp_path / "linked.json"
    linked.symlink_to(real)
    with pytest.raises(RuntimeError, match="LEDGER_INVALID"):
        apply_source_subtitle_truth(
            "",
            spec={"pieces": []},
            durations=[],
            ledger_path=linked,
        )


def test_replace_cue_split_across_recued_cues_counts_satisfied(tmp_path):
    """2026-07-19 合并跳切实证：fresh 重转写把钉子区间切成两条 cue，
    真值文本跨界拼接已逐字成立 → satisfied（no-op），不再 NOT_UNIQUE 失败。"""
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "split-cue-satisfied",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 118_000,
                "action": "replace_cue",
                "text": "很多人笑出声这件事情kmx要不要想想为什么",
                "authority": "Ivan",
                "required": True,
            }
        ],
    )
    srt = _srt(
        (10, 14, "很多人笑出声这件事情"),
        (14, 18, "kmx要不要想想为什么"),
    )
    corrected, audit = apply_source_subtitle_truth(
        srt,
        spec={
            "pieces": [
                {"remote_media": "/x/recording.mp4", "start_ms": 100_000, "end_ms": 120_000}
            ]
        },
        durations=[20_000],
        ledger_path=ledger,
    )
    assert audit["status"] == "ALREADY_SATISFIED"
    assert audit["failures"] == []
    assert audit["satisfied"][0]["truth_id"] == "split-cue-satisfied"
    assert corrected == srt  # no-op


def test_replace_cue_split_cues_garble_now_redistributes(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "split-cue-wrong",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 118_000,
                "action": "replace_cue",
                "text": "很多人笑出声这件事情kmx要不要想想为什么",
                "authority": "Ivan",
                "required": True,
            }
        ],
    )
    srt = _srt(
        (10, 14, "很多人笑出声这件事情"),
        (14, 18, "乒乓球要不要想想为什么"),
    )
    corrected, audit = apply_source_subtitle_truth(
        srt,
        spec={
            "pieces": [
                {"remote_media": "/x/recording.mp4", "start_ms": 100_000, "end_ms": 120_000}
            ]
        },
        durations=[20_000],
        ledger_path=ledger,
    )
    # 2026-07-20 契约升级：高相似跨 cue 残渣正是多 cue 重分配的正解场景
    # ——钉文落刀、乒乓球残渣清除；邻句保护由辖区收缩测试单独把守。
    assert audit["status"] == "APPLIED"
    assert "乒乓球" not in corrected
    assert "kmx" in corrected


def test_replace_cue_split_cues_punctuation_insensitive_satisfied(tmp_path):
    """2026-07-19 实证第二层：审定文本「…事情，kmx」跨 cue 时逗号由边界
    停顿表达——去标点归一后内容成立即 satisfied，不被一个标点冤枉。"""
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "punct-split",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 114_000,
                "action": "replace_cue",
                "text": "很多人笑出声这件事情，kmx",
                "authority": "Ivan",
                "required": True,
            }
        ],
    )
    srt = _srt(
        (10, 13, "很多人笑出声这件事情"),
        (13, 16, "kmx要不要考虑一下为什么"),
    )
    corrected, audit = apply_source_subtitle_truth(
        srt,
        spec={
            "pieces": [
                {"remote_media": "/x/recording.mp4", "start_ms": 100_000, "end_ms": 120_000}
            ]
        },
        durations=[20_000],
        ledger_path=ledger,
    )
    assert audit["status"] == "ALREADY_SATISFIED"
    assert audit["failures"] == []
    assert corrected == srt


def test_multi_cue_replace_redistributes_pin_text(tmp_path):
    """多 cue 辖区重分配（2026-07-20 kmx r5 案）：fresh 掷出不同切分时,
    整句钉按相似度分布到覆盖 cue,零内容发明、时间轴不动。"""

    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "only-kmx",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 116_000,
                "action": "replace_cue",
                "text": "只有kmx会这样称呼李豆沙",
                "required": True,
            }
        ],
    )
    srt = (
        "1\n00:00:10,000 --> 00:00:13,000\n只有，只有kmx。\n\n"
        "2\n00:00:13,000 --> 00:00:16,000\n你们怎么会这样称呼李豆沙？\n"
    )
    corrected, audit = apply_source_subtitle_truth(
        srt,
        spec={"pieces": [{"remote_media": "/x/recording.mp4", "start_ms": 100_000, "end_ms": 130_000}]},
        durations=[30_000],
        ledger_path=ledger,
    )
    assert audit["status"] == "APPLIED"
    assert not audit["failures"]
    joined = corrected.replace("\n", "")
    assert "只有kmx会这样称呼李豆沙" in joined.replace("，", "").replace("。", "") or (
        "只有kmx" in corrected and "称呼李豆沙" in corrected
    )
    # 两条 cue 都有文本(不许空 cue),且不再含错误残渣「你们怎么会」
    row = audit["applied"][0]
    parts = row["multi_cue_redistribution"]
    assert len(parts) == 2 and all(p.strip() for p in parts)
    assert "你们" not in corrected


def test_unrelated_neighbor_cue_never_overwritten_by_pin(tmp_path):
    """邻句保护（辖区收缩）：钉窗擦到与钉文零共通的邻句时,邻句原文分毫
    不动;收缩后单 cue 落刀正常执行。"""

    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "graze-neighbor",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 114_200,
                "action": "replace_cue",
                "text": "只有kmx会这样称呼李豆沙",
                "required": True,
            }
        ],
    )
    srt = _srt_ms(
        (10_000, 14_000, "只有，只有kmx这样称李豆沙"),
        (14_050, 18_000, "今天晚饭吃番茄炒蛋"),
    )
    corrected, audit = apply_source_subtitle_truth(
        srt,
        spec={"pieces": [{"remote_media": "/x/recording.mp4", "start_ms": 100_000, "end_ms": 130_000}]},
        durations=[30_000],
        ledger_path=ledger,
    )
    assert "今天晚饭吃番茄炒蛋" in corrected
    assert "只有kmx会这样称呼李豆沙" in corrected
    assert audit["status"] == "APPLIED"


def test_committed_ledger_restores_complete_opening_nancho_cue():
    """已审定整句不能依赖误词仍存在；ASR 删掉专名时也必须完整恢复。"""

    ledger = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_truth_ledger.v1.json"
    )
    srt = _srt_ms((0, 2_800, "这不是主播最最最最喜欢"),)
    spec = {
        "pieces": [
            {
                "remote_media": "/recordings/22966160_20260722-19-35-15.mp4",
                "start_ms": 672_920,
                "end_ms": 675_480,
            }
        ]
    }

    corrected, audit = apply_source_subtitle_truth(
        srt,
        spec=spec,
        durations=[2_560],
        ledger_path=ledger,
    )

    assert "这不是主播最最最最喜欢的南町nightin吗，llnnhhb" in corrected
    assert audit["status"] == "APPLIED"
    assert [row["truth_id"] for row in audit["applied"]] == [
        "20260722-nancho-confrontation-opening-mixed-name"
    ]
