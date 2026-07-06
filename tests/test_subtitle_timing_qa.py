"""Timing QA rules, shaped by the real 7/2 clip defects: a 24s stuck-segment
"好漂亮" tail, integer-second flash cues, and BGM-masked screams that VAD
scores as silence but are REAL content (must never be dropped)."""

from src.autoslice.review_evidence import SourceCue
from src.autoslice.subtitle_timing_qa import (
    SpeechSpan,
    TimingQaPolicy,
    sanitize_cue_timing,
)


def _cue(cue_id: str, start_ms: int, end_ms: int, text: str) -> SourceCue:
    return SourceCue(
        cue_id=cue_id,
        source_start_ms=start_ms,
        source_end_ms=end_ms,
        text=text,
        language="zh",
        kind="speech",
        confidence=1.0,
    )


WINDOW = dict(window_start_ms=0, window_end_ms=90_000)


def test_stuck_segment_duplicate_long_no_vad_is_dropped():
    cues = [
        _cue("a", 18_000, 22_000, "好漂亮"),
        _cue("b", 24_000, 26_000, "好像阿朵"),
        _cue("c", 60_000, 84_000, "好漂亮"),  # the real 24s hallucinated tail
    ]
    spans = [SpeechSpan(18_200, 21_500), SpeechSpan(24_100, 25_800)]
    result, report = sanitize_cue_timing(cues, spans, **WINDOW)
    assert [cue.cue_id for cue in result] == ["a", "b"]
    assert report["counts"]["dropped"] == 1
    action = next(a for a in report["actions"] if a["action"] == "drop_stuck_segment")
    assert action["cue_id"] == "c"
    assert "duplicate_of_recent_cue" in action["reasons"]


def test_long_cue_with_vad_support_is_never_dropped():
    cues = [
        _cue("a", 18_000, 22_000, "好漂亮"),
        _cue("c", 60_000, 84_000, "好漂亮"),
    ]
    spans = [SpeechSpan(60_500, 80_000)]  # sustained speech under the cue
    result, _ = sanitize_cue_timing(cues, spans, **WINDOW)
    assert [cue.cue_id for cue in result] == ["a", "c"]


def test_non_duplicate_long_low_density_is_retimed_not_dropped():
    # 8s "好像阿朵" whose only speech island sits at the tail (the real cue 8).
    cues = [_cue("a", 28_000, 36_000, "好像阿朵")]
    spans = [SpeechSpan(34_900, 36_400)]
    result, report = sanitize_cue_timing(cues, spans, **WINDOW)
    assert len(result) == 1
    assert result[0].source_start_ms == 34_750  # island start - lead pad
    assert result[0].source_end_ms == 36_000  # clipped at original cue end
    assert report["counts"]["retimed"] == 1
    assert "long_low_density_retimed_to_speech" in report["actions"][0]["reasons"]


def test_long_low_density_without_islands_is_clamped_to_soft_max():
    cues = [_cue("a", 10_000, 21_000, "嗯")]
    result, report = sanitize_cue_timing(cues, [], **WINDOW)
    assert result[0].source_start_ms == 10_000
    assert result[0].source_end_ms == 16_000
    assert "long_low_density_clamped" in report["actions"][0]["reasons"]


def test_screams_with_zero_vad_are_kept():
    # BGM-masked screams score ~0 on silero but are real slice content.
    cues = [
        _cue("a", 47_000, 51_000, "啊"),
        _cue("b", 53_000, 54_000, "啊"),
        _cue("c", 54_000, 55_000, "好可怕"),
    ]
    result, report = sanitize_cue_timing(cues, [], **WINDOW)
    assert [cue.cue_id for cue in result] == ["a", "b", "c"]
    assert report["counts"]["dropped"] == 0


def test_flash_cue_extended_to_min_readable_but_not_into_next_cue():
    cues = [
        _cue("a", 53_000, 53_400, "啊"),
        _cue("b", 54_000, 56_000, "好可怕"),
    ]
    result, report = sanitize_cue_timing(cues, [], **WINDOW)
    assert result[0].source_end_ms == 54_000  # extended to 1s, bounded by next start
    assert any("flash_extended" in action["reasons"] for action in report["actions"])
    # A flash with room extends to the full minimum readable duration.
    solo, _ = sanitize_cue_timing([_cue("x", 10_000, 10_400, "好")], [], **WINDOW)
    assert solo[0].source_end_ms == 11_000


def test_strong_vad_support_snaps_overhanging_edges():
    cues = [_cue("a", 10_000, 18_000, "这真的不是融了阿朵吗这也太像了吧")]
    spans = [SpeechSpan(10_100, 14_500)]
    result, report = sanitize_cue_timing(cues, spans, **WINDOW)
    assert result[0].source_start_ms == 10_000  # start within slack, untouched
    assert result[0].source_end_ms == 14_750  # island end + tail pad
    assert "end_snapped_to_speech" in report["actions"][0]["reasons"]


def test_cue_opening_on_silence_snaps_start_to_first_island():
    # Ivan's defect: the cue hangs on screen through silence and the words only
    # come in the final seconds — the start must snap to the speech island even
    # when overall VAD coverage of the cue is low.
    cues = [_cue("a", 10_000, 16_000, "这是什么")]
    spans = [SpeechSpan(14_500, 15_800)]
    result, report = sanitize_cue_timing(cues, spans, **WINDOW)
    assert result[0].source_start_ms == 14_350  # island start - lead pad
    assert result[0].source_end_ms == 16_000
    assert "start_snapped_to_speech" in report["actions"][0]["reasons"]


def test_good_cues_pass_through_unchanged():
    cues = [
        _cue("a", 0, 4_000, "吸铁石会梦到树上虎鲸吗？这是什么？"),
        _cue("b", 5_000, 8_000, "这真的不是融了阿朵吗"),
    ]
    spans = [SpeechSpan(200, 4_400), SpeechSpan(5_100, 7_900)]
    result, report = sanitize_cue_timing(cues, spans, **WINDOW)
    assert [(c.source_start_ms, c.source_end_ms) for c in result] == [(0, 4_000), (5_000, 8_000)]
    assert report["actions"] == []


def test_cues_outside_window_untouched_and_window_end_bounds_extension():
    cues = [
        _cue("out", 100_000, 130_000, "窗口外超长条"),
        _cue("edge", 89_500, 89_900, "好"),
    ]
    result, _ = sanitize_cue_timing(cues, [], **WINDOW)
    by_id = {cue.cue_id: cue for cue in result}
    assert by_id["out"].source_end_ms == 130_000
    assert by_id["edge"].source_end_ms == 90_000  # min-readable extension clipped at window end


def test_policy_is_recorded_in_report():
    _, report = sanitize_cue_timing([], [], **WINDOW, policy=TimingQaPolicy(soft_max_ms=5_000))
    assert report["policy"]["soft_max_ms"] == 5_000
    assert report["vad_evidence_contract"] == "positive_only_low_recall_under_bgm"
