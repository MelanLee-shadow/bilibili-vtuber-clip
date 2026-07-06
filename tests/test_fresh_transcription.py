"""Fresh whole-window transcription: finished talk clips get their subtitle
from a fresh transcription of the final media, not from the coarse
integer-second production ASR — with loud validation and a recorded
fallback."""

import pytest

from scripts.run_auto_review_shadow_pipeline import (
    _fresh_srt_to_source_cues,
    _materialize_recut_record,
)
from src.autoslice.auto_review import DecisionAction
from src.autoslice.boundary_resolver import BoundaryResolution
from src.autoslice.review_evidence import SourceCue


def _srt(cues: list[tuple[int, int, str]]) -> str:
    blocks = []
    for index, (start_ms, end_ms, text) in enumerate(cues, start=1):
        blocks.append(f"{index}\n{_time(start_ms)} --> {_time(end_ms)}\n{text}")
    return "\n\n".join(blocks) + "\n"


def _time(ms: int) -> str:
    seconds, millis = divmod(ms, 1000)
    minutes, sec = divmod(seconds, 60)
    return f"00:{minutes:02d}:{sec:02d},{millis:03d}"


GOOD_FRESH = _srt(
    [
        (200, 4_100, "吸铁石会梦到树上虎鲸吗？"),
        (7_900, 8_600, "好"),
        (11_200, 13_900, "这真的不是融了Ado吗"),
        (15_400, 17_000, "一眼AI 好吧"),
    ]
)


def test_fresh_srt_lifted_onto_source_timeline():
    cues = _fresh_srt_to_source_cues(GOOD_FRESH, window_start_ms=604_646, duration_ms=84_000)
    assert cues[0].source_start_ms == 604_846
    assert cues[0].text == "吸铁石会梦到树上虎鲸吗？"
    assert cues[-1].source_end_ms == 604_646 + 17_000
    assert all(cue.cue_id.startswith("fresh_") for cue in cues)


@pytest.mark.parametrize(
    "bad,match",
    [
        (_srt([(0, 2_000, "只有"), (3_000, 4_000, "两条")]), "too few"),
        (_srt([(5_000, 9_000, "一"), (2_000, 4_000, "倒序重叠"), (9_500, 10_000, "三")]), "overlaps"),
    ],
)
def test_fresh_srt_garbage_is_rejected(bad, match):
    with pytest.raises(ValueError, match=match):
        _fresh_srt_to_source_cues(bad, window_start_ms=0, duration_ms=84_000)


def test_tail_overrun_is_clamped_not_rejected():
    # One drifting tail cue must not discard 40+ good cues (the real 反沙 clip
    # failure: cue 45 ended 5.08s past a 90.42s clip and the whole fresh
    # transcription was thrown away).
    srt = _srt(
        [
            (0, 3_000, "第一句"),
            (5_000, 8_000, "第二句"),
            (80_000, 89_000, "尾部正常"),
            (88_000, 95_500, "尾部越界被裁剪"),
            (95_000, 96_000, "起点已超出片长被丢弃"),
        ]
    )
    cues = _fresh_srt_to_source_cues(srt, window_start_ms=0, duration_ms=90_420)
    assert [cue.text for cue in cues] == ["第一句", "第二句", "尾部正常", "尾部越界被裁剪"]
    assert cues[-1].source_end_ms == 90_420


def _materialize(tmp_path, transcriber):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake source")
    boundary = BoundaryResolution(
        candidate_id="talk_test",
        action=DecisionAction.AUTO_RECUT,
        resolved_start_ms=604_646,
        resolved_end_ms=688_646,
        start_boundary_score=0.9,
        end_boundary_score=0.9,
        reason_codes=(),
        next_start_ms=604_646,
        next_end_ms=688_646,
    )
    cues = [
        SourceCue("a", 604_646, 608_646, "粗轴第一句", "zh", "speech", 1.0),
        SourceCue("b", 610_000, 612_000, "粗轴第二句", "zh", "speech", 1.0),
    ]
    return _materialize_recut_record(
        source_video=source,
        candidate_id="talk_test",
        boundary_resolution=boundary,
        output_dir=tmp_path,
        cues=cues,
        run_ffmpeg=False,
        fresh_talk_transcriber=transcriber,
    )


def test_fresh_transcription_replaces_asr_cue_subtitle(tmp_path):
    record = _materialize(tmp_path, lambda media_path: GOOD_FRESH)
    assert record["subtitle_source"] == "fresh_agy_transcription"
    assert record["fresh_transcription"]["status"] == "USED"
    subtitle = (tmp_path / "replacement_recuts" / "talk_test.recut.srt").read_text(encoding="utf-8")
    assert "吸铁石会梦到树上虎鲸吗" in subtitle
    assert "粗轴第一句" not in subtitle


def test_fresh_transcription_failure_falls_back_to_asr_cues(tmp_path):
    def broken(media_path):
        raise RuntimeError("agy down")

    record = _materialize(tmp_path, broken)
    assert record["subtitle_source"] == "asr_cues"
    assert record["fresh_transcription"]["status"] == "FAILED_FALLBACK_ASR_CUES"
    assert "agy down" in record["fresh_transcription"]["error"]
    subtitle = (tmp_path / "replacement_recuts" / "talk_test.recut.srt").read_text(encoding="utf-8")
    assert "粗轴第一句" in subtitle


def test_garbage_fresh_output_falls_back(tmp_path):
    record = _materialize(tmp_path, lambda media_path: "not srt at all")
    assert record["subtitle_source"] == "asr_cues"
    assert record["fresh_transcription"]["status"] == "FAILED_FALLBACK_ASR_CUES"
