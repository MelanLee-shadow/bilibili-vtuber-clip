import json

import pytest

from src.autoslice.source_subtitle_truth import apply_source_subtitle_truth


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
