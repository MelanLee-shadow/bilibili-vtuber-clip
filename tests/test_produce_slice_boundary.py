"""Topic-closure boundary rules for finished clips (Ivan 2026-07-04): both
cuts must land on complete-sentence boundaries near the semantic targets, and
run-on cues near the closure trigger a fine micro-pass instead of a bad cut."""

from scripts.produce_slice_package import (
    boundary_audit,
    needs_tail_refinement,
    snap_end_to_sentence,
    snap_start_to_sentence,
)
from src.autoslice.jingting_chunker import SrtCue
from src.autoslice.subtitle_timing_qa import SpeechSpan


def test_snap_end_picks_nearest_sentence_end():
    ends = [70_000, 79_900, 83_500, 96_000]
    assert snap_end_to_sentence(ends, 80_000) == 79_900
    assert snap_end_to_sentence(ends, 82_000) == 83_500


def test_snap_end_fails_closed_when_no_boundary_near_target():
    assert snap_end_to_sentence([10_000, 40_000], 25_000) is None


def test_snap_start_opens_on_a_sentence():
    starts = [2_600, 3_100, 9_000]
    assert snap_start_to_sentence(starts, 3_000) == 3_100
    assert snap_start_to_sentence([9_000], 3_000) is None


def _cue(start_ms, end_ms):
    return SrtCue(index="1", start_ms=start_ms, end_ms=end_ms, text="x")


def test_runon_cue_straddling_target_triggers_refinement():
    # The real houqun failure: a 17s run-on cue welded the closure sentence to
    # the next topic, so the coarse grid could not place the cut.
    cues = [_cue(300_000, 319_000), _cue(319_000, 336_000)]
    assert needs_tail_refinement(cues, snapped_end=319_000, target_ms=324_700) is True


def test_clean_snap_near_target_needs_no_refinement():
    cues = [_cue(70_000, 79_900), _cue(80_200, 84_000)]
    assert needs_tail_refinement(cues, snapped_end=79_900, target_ms=80_000) is False


def test_boundary_audit_requires_both_snapped_boundaries():
    spans = [SpeechSpan(85_000, 95_000)]
    ok = boundary_audit(spans, start_ms=250, cut_ms=90_000, start_snapped=True, end_snapped=True)
    assert ok["verdict"] == "ok_sentence_boundary_cut"
    assert ok["end_cut_inside_speech_island"] is True
    assert ok["end_island_continues_ms"] == 5_000

    bad_start = boundary_audit(spans, start_ms=0, cut_ms=90_000, start_snapped=False, end_snapped=True)
    assert bad_start["verdict"] == "start_not_on_sentence_boundary"
