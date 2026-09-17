"""Keep the release validator's cue blocks when converting final text to ASS.

Fixtures contain synthetic text, not new transcriptions. The unchanged package
ASS auditor is used to detect merged payloads or clocks, not mocked to pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.autoslice import recut_materialization as media
from src.autoslice import subtitle_rendering as render
from src.autoslice.subtitle_validation import validate_srt_file
from tests.test_final_ass_text_preservation import _audit

CUES = [
    (500, 1500, "第一句原文"),
    (2000, 3000, "第二句原文"),
]


def _write(path: Path, *, separator: str, newline: str = "\n", envelope: bool = False) -> bytes:
    value = (
        "1\n00:00:00,500 --> 00:00:01,500\n第一句原文"
        + separator
        + "2\n00:00:02,000 --> 00:00:03,000\n第二句原文\n"
    )
    if envelope:
        value = "\n \n\t\n" + value + "\n\t\n  \n"
    data = ("\ufeff" + value.replace("\n", newline)).encode("utf-8")
    path.write_bytes(data)
    return data


def _rows(path: Path):
    return [
        line.split(",", 9) for line in path.read_text().splitlines() if line.startswith("Dialogue:")
    ]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"], ids=["LF", "CRLF", "CR"])
@pytest.mark.parametrize(
    "separator",
    ["\n\n", "\n \n", "\n\t\n", "\n \t \n", "\n \n\t\n \n"],
    ids=["empty", "spaces", "tab", "mixed", "repeated"],
)
def test_native_burn_preserves_each_accepted_block(tmp_path, monkeypatch, separator, newline):
    monkeypatch.delenv("VTUBER_SLICE_TERM_LEXICON", raising=False)
    srt = tmp_path / "final.srt"
    original = _write(srt, separator=separator, newline=newline)
    verdict = validate_srt_file(srt)
    assert verdict["status"] == "PASS" and verdict["cue_count"] == verdict["block_count"] == 2

    parsed = render._parse_srt(srt, normalize_terms=False)
    assert [(c.source_start_ms, c.source_end_ms, c.text) for c in parsed] == CUES
    result = media._burn_preview_subtitles(
        {
            "status": "MATERIALIZED",
            "media_path": str(tmp_path / "synthetic.mp4"),
            "subtitle_path": str(srt),
        },
        run_ffmpeg=False,
    )
    assert result["burned_preview"]["status"] == "DRY_RUN"
    ass = Path(result["burned_preview"]["ass_path"])
    rows = _rows(ass)
    assert [(r[1], r[2], r[9]) for r in rows] == [
        ("0:00:00.50", "0:00:01.50", CUES[0][2]),
        ("0:00:02.00", "0:00:03.00", CUES[1][2]),
    ]
    assert not _audit(srt, ass).issues
    assert srt.read_bytes() == original


def test_blank_envelope_is_not_a_cue_or_a_shifted_identity(tmp_path):
    srt = tmp_path / "final.srt"
    original = _write(srt, separator="\n\t\n", envelope=True)
    assert validate_srt_file(srt)["status"] == "PASS"
    cues = render._parse_srt(srt, normalize_terms=False, source_offset_ms=7100)
    assert [c.cue_id for c in cues] == ["u_000001", "u_000002"]
    assert [(c.source_start_ms, c.source_end_ms) for c in cues] == [(7600, 8600), (9100, 10100)]
    assert [c.text for c in cues] == [c[2] for c in CUES] and srt.read_bytes() == original


def test_no_blank_line_means_no_new_cue_even_for_digits_or_arrow_text(tmp_path):
    srt = tmp_path / "quoted.srt"
    quoted = "这行文字里的编号\n2\n00:00:02,000 --> 00:00:03,000\n仍然只是文字"
    srt.write_text("1\n00:00:00,500 --> 00:00:05,500\n" + quoted + "\n")
    assert validate_srt_file(srt)["status"] == "PASS"
    cues = render._parse_srt(srt, normalize_terms=False)
    assert len(cues) == 1 and cues[0].text == quoted


def test_draft_normalization_and_final_spelling_remain_separate(tmp_path, monkeypatch):
    monkeypatch.delenv("VTUBER_SLICE_TERM_LEXICON", raising=False)
    srt = tmp_path / "final.srt"
    original = _write(srt, separator="\n \t\n")
    lex = tmp_path / "term_lexicon.json"
    lex.write_text(
        json.dumps(
            {"sources": [], "overrides": [{"canonical": "草稿替代词", "aliases": ["第一句原文"]}]},
            ensure_ascii=False,
        )
    )
    draft = render._parse_srt(srt)
    assert [c.text for c in draft] == ["草稿替代词", "第二句原文"]
    final = render._parse_srt(srt, normalize_terms=False)
    assert [c.text for c in final] == [c[2] for c in CUES]
    ass = tmp_path / "final.ass"
    render._write_sapphire_ass_from_srt(srt, ass)
    assert [r[9] for r in _rows(ass)] == [c[2] for c in CUES]
    assert srt.read_bytes() == original and not _audit(srt, ass).issues


def test_unnumbered_legacy_source_reading_does_not_gain_release_approval(tmp_path):
    srt = tmp_path / "draft.srt"
    srt.write_text(
        "00:00:00,500 --> 00:00:01,500\n第一句原文\n \n00:00:02,000 --> 00:00:03,000\n第二句原文\n"
    )
    assert validate_srt_file(srt)["status"] == "FAIL"
    cues = render._parse_srt(srt, normalize_terms=False)
    assert [(c.source_start_ms, c.source_end_ms, c.text) for c in cues] == CUES


@pytest.mark.parametrize(
    "old,new",
    [
        ("\n2\n", "\n7\n"),
        ("00:00:02,000", "00:61:02,000"),
        ("00:00:02,000", "00:00:01,000"),
        ("00:00:03,000", "00:00:02,100"),
    ],
)
def test_existing_release_errors_are_not_relaxed(tmp_path, old, new):
    srt = tmp_path / "invalid.srt"
    raw = _write(srt, separator="\n\t\n").decode("utf-8-sig")
    assert old in raw
    srt.write_text(raw.replace(old, new, 1))
    result = validate_srt_file(srt)
    assert result["status"] == "FAIL" and result["errors"]


def test_independent_auditor_rejects_old_clock_collapse_even_with_fresh_hashes(tmp_path):
    srt = tmp_path / "final.srt"
    _write(srt, separator="\n\n")
    ass = tmp_path / "wrong.ass"
    render._write_sapphire_ass_from_srt(srt, ass)
    text = ass.read_text()
    first, second = [line for line in text.splitlines() if line.startswith("Dialogue:")]
    combined = first + " 2 00:00:02,000 --> 00:00:03,000 第二句原文"
    ass.write_text(text.replace(first, combined).replace(second + "\n", ""))
    issues = _audit(srt, ass).issues
    assert issues  # The helper rebinds hashes; text/event parity still rejects it.
