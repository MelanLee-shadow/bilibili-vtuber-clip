from pathlib import Path

from src.autoslice.recut_materialization import _write_source_range_srt
from src.autoslice.subtitle_timing_qa import SourceCue


def _cue(cue_id: str, start_ms: int, end_ms: int, text: str) -> SourceCue:
    return SourceCue(
        cue_id=cue_id,
        source_start_ms=start_ms,
        source_end_ms=end_ms,
        text=text,
    )


def test_source_range_drops_only_unreadable_leading_boundary_fragment(
    tmp_path: Path,
):
    output = tmp_path / "clip.srt"

    _write_source_range_srt(
        [
            _cue("previous", 900, 1_250, "上一话题的尾巴"),
            _cue("opening", 1_250, 2_500, "本片开场"),
        ],
        1_000,
        2_500,
        output,
    )

    rendered = output.read_text(encoding="utf-8")
    assert "上一话题的尾巴" not in rendered
    assert "本片开场" in rendered


def test_source_range_keeps_substantial_clipped_opening(tmp_path: Path):
    output = tmp_path / "clip.srt"

    _write_source_range_srt(
        [_cue("opening", 500, 1_800, "仍有足够可读时长")],
        1_000,
        1_800,
        output,
    )

    assert "仍有足够可读时长" in output.read_text(encoding="utf-8")
