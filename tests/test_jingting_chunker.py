import pytest

from scripts.gemini_slice_jingting import validate_same_timing
from src.autoslice.jingting_chunker import (
    merge_refined_chunks,
    parse_srt_cues,
    plan_jingting_chunks,
)


def _srt(cues: list[tuple[int, int, int, str]]) -> str:
    blocks = []
    for index, start_ms, end_ms, text in cues:
        blocks.append(f"{index}\n{_time(start_ms)} --> {_time(end_ms)}\n{text}")
    return "\n\n".join(blocks) + "\n"


def _time(ms: int) -> str:
    seconds, millis = divmod(ms, 1000)
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}:{sec:02d},{millis:03d}"


def _long_session(cue_seconds: int = 8, gap_seconds: int = 2, count: int = 180) -> str:
    cues = []
    cursor = 0
    for index in range(1, count + 1):
        start = cursor
        end = start + cue_seconds * 1000
        cues.append((index, start, end, f"第{index}句"))
        cursor = end + gap_seconds * 1000
    return _srt(cues)


def test_short_context_stays_single_chunk():
    srt_text = _srt([(1, 0, 4000, "你好"), (2, 5000, 9000, "再见")])
    chunks = plan_jingting_chunks(srt_text)
    assert len(chunks) == 1
    assert chunks[0].media_start_ms == 0
    assert [cue.index for cue in chunks[0].cues] == ["1", "2"]


def test_long_session_splits_near_target_at_cue_gaps():
    srt_text = _long_session()
    chunks = plan_jingting_chunks(srt_text, target_chunk_ms=300_000)
    assert len(chunks) > 1
    for chunk in chunks:
        span_ms = chunk.cues[-1].end_ms - chunk.cues[0].start_ms
        assert span_ms <= 390_000
    # Every cue lands in exactly one chunk, in order.
    all_indices = [cue.index for chunk in chunks for cue in chunk.cues]
    assert all_indices == [str(i) for i in range(1, 181)]
    # Media windows tile the timeline without overlap.
    for previous, current in zip(chunks, chunks[1:]):
        assert previous.media_end_ms == current.media_start_ms
        assert previous.media_end_ms <= current.cues[0].start_ms


def test_chunk_srt_is_rebased_to_media_window():
    srt_text = _long_session()
    chunks = plan_jingting_chunks(srt_text)
    second = chunks[1]
    rebased = parse_srt_cues(second.chunk_srt_text())
    assert rebased[0].index == second.cues[0].index
    assert rebased[0].start_ms == second.cues[0].start_ms - second.media_start_ms
    assert rebased[0].start_ms >= 0


def test_merge_transplants_text_and_keeps_draft_timing():
    srt_text = _long_session()
    chunks = plan_jingting_chunks(srt_text)
    refined_pairs = []
    for chunk in chunks:
        refined = chunk.chunk_srt_text().replace("句", "句(精修)")
        refined_pairs.append((chunk, refined))
    merged = merge_refined_chunks(srt_text, refined_pairs)
    validate_same_timing(srt_text, merged)
    merged_cues = parse_srt_cues(merged)
    assert all("(精修)" in cue.text for cue in merged_cues)
    original_cues = parse_srt_cues(srt_text)
    assert [(c.index, c.start_ms, c.end_ms) for c in merged_cues] == [
        (c.index, c.start_ms, c.end_ms) for c in original_cues
    ]


def test_merge_rejects_missing_or_reordered_cues():
    srt_text = _long_session()
    chunks = plan_jingting_chunks(srt_text)
    truncated = "\n\n".join(chunks[0].chunk_srt_text().split("\n\n")[:-2]) + "\n"
    with pytest.raises(ValueError, match="mismatch"):
        merge_refined_chunks(srt_text, [(chunks[0], truncated)] + [(c, c.chunk_srt_text()) for c in chunks[1:]])


def test_merge_rejects_uncovered_draft_cues():
    srt_text = _long_session()
    chunks = plan_jingting_chunks(srt_text)
    with pytest.raises(ValueError, match="did not cover"):
        merge_refined_chunks(srt_text, [(chunks[0], chunks[0].chunk_srt_text())])


def test_empty_draft_produces_no_chunks():
    assert plan_jingting_chunks("") == []
