"""Explicit two-line SRT display authority must survive the ASS projection."""

from pathlib import Path

import pytest

from src.autoslice import subtitle_rendering
from src.autoslice.review_package_ass_audit import (
    HOST_SPEAKER,
    _SpeakerCue,
    _ass_events,
    _expected_events,
)


def test_layout_preserves_two_physical_lines_for_entire_cue() -> None:
    rows = subtitle_rendering._layout_cue_sequence_for_display(
        [(1000, 2500, "内层歌词\n外层插话")]
    )
    assert rows == [(0, 1000, 2500, r"内层歌词\N外层插话")]


def test_uniform_writer_and_auditor_preserve_same_lane_break(tmp_path: Path) -> None:
    srt = tmp_path / "parallel.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,500\n内层歌词\n外层插话\n",
        encoding="utf-8",
    )
    ass = tmp_path / "parallel.ass"
    subtitle_rendering._write_sapphire_ass_from_srt(srt, ass)
    actual, issues = _ass_events(ass)
    assert issues == []
    expected = _expected_events(
        [_SpeakerCue(1000, 2500, HOST_SPEAKER, "内层歌词\n外层插话")],
        host_style="Default",
    )
    assert actual == expected
    assert actual[0].text == r"内层歌词\N外层插话"


def test_explicit_lines_do_not_join_across_neighbor_cues() -> None:
    rows = subtitle_rendering._layout_cue_sequence_for_display(
        [
            (0, 1000, "内层小\n外层说明"),
            (1000, 2000, "李继续说"),
        ]
    )
    assert rows[0] == (0, 0, 1000, r"内层小\N外层说明")
    assert rows[1] == (1, 1000, 2000, "李继续说")


@pytest.mark.parametrize(
    "text,match",
    [
        ("第一行\n第二行\n第三行", "more than two"),
        ("短行\n" + "长" * 25, "safe display width"),
    ],
)
def test_invalid_explicit_line_contract_fails_closed(text: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        subtitle_rendering._layout_cue_sequence_for_display([(0, 1000, text)])
