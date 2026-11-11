"""Manual step 5 and finished-media A3 replay the same physical line limit."""

from pathlib import Path

from src.autoslice import subtitle_rendering
from src.autoslice.manual_lane import normalize, subtitle_shape, subtitle_track
from src.autoslice.review_package_ass_audit import audit_review_package_ass
from scripts.audit_review_package import DEFAULT_MAX_VISUAL_LINE_CHARS


def _audit(tmp_path: Path, srt: Path, ass: Path):
    return audit_review_package_ass(
        root=tmp_path, item={"ass_path": ass.name, "speaker_srt": srt.name},
        portable_required=False, max_visual_lines=2, max_visual_line_chars=DEFAULT_MAX_VISUAL_LINE_CHARS,
    )


def test_renderer_and_finished_media_replay_share_manual_width(tmp_path):
    srt, ass, generic_ass = tmp_path / "speaker.srt", tmp_path / "manual.ass", tmp_path / "generic.ass"
    srt.write_text("1\n00:00:00,000 --> 00:00:04,000\n[李豆沙] " + "甲" * 20 + "\n", encoding="utf-8")
    subtitle_rendering._write_sapphire_ass_from_srt(
        srt, ass, max_chars_per_line=DEFAULT_MAX_VISUAL_LINE_CHARS)
    subtitle_rendering._write_sapphire_ass_from_srt(srt, generic_ass)
    assert _audit(tmp_path, srt, ass).issues == ()
    assert ass.read_bytes() != generic_ass.read_bytes()
    assert any(issue.code == "SUBTITLE_ASS_LINE_TOO_LONG" for issue in _audit(tmp_path, srt, generic_ass).issues)


def test_shaped_srt_keeps_words_and_whitespace_within_renderer_width(tmp_path):
    text = "这次请 Amy 和朋友一起去看《晚风中的花园》好不好"
    shaped = normalize.split_cue(text, 0.0, 4.0, normalize._audit())
    assert all(len(line) <= DEFAULT_MAX_VISUAL_LINE_CHARS
               for cue in shaped for line in cue["text"].splitlines())
    assert any("《晚风中的花园》" in line for cue in shaped for line in cue["text"].splitlines())
    srt, ass = tmp_path / "speaker.srt", tmp_path / "manual.ass"
    srt.write_text(subtitle_track.format_blocks([dict(cue, speaker="李豆沙") for cue in shaped], labels=True),
                   encoding="utf-8")
    subtitle_rendering._write_sapphire_ass_from_srt(
        srt, ass, max_chars_per_line=DEFAULT_MAX_VISUAL_LINE_CHARS)
    assert _audit(tmp_path, srt, ass).issues == ()


def test_renderer_uses_word_and_title_boundaries_for_unshaped_input():
    text = "把这两个大学校园的事情告诉大家之后再去寻找《晚风，花园》好不好"
    segments = subtitle_rendering._split_text_segments(text, max_chars=DEFAULT_MAX_VISUAL_LINE_CHARS)
    assert "".join(segments) == text
    assert all(len(segment) <= DEFAULT_MAX_VISUAL_LINE_CHARS for segment in segments)
    boundaries = set()
    offset = 0
    for segment in segments[:-1]:
        offset += len(segment)
        boundaries.add(offset)
    assert boundaries <= subtitle_shape.legal_breaks(text)
    assert any("《晚风，花园》" in segment for segment in segments)
