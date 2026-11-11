"""SUB-24: speaker metadata changes ASS style, never production prefixes."""

import ast
from dataclasses import replace
import inspect
from pathlib import Path
import re
import textwrap

import pytest

from scripts import apply_speaker_turn_overrides as talk
from src.autoslice import subtitle_rendering as song


ROOT = Path(__file__).resolve().parents[1]
PUBLISHED_PACKAGE = ROOT / (
    "reports/song-fastlane-20261024-publication-evidence/oci3/opt/bilive/autoslice/"
    "private-runs/output-continuation-20261023/production/song_133029_1424/"
    "REVIEW-PACKAGE-FL-8e60c667-001"
)


def _events(text):
    return [row.split(",", 9) for row in text.splitlines() if row.startswith("Dialogue:")]


def _srt(tmp_path, texts):
    path = tmp_path / "input.srt"
    path.write_text("\n\n".join(
        f"{i + 1}\n00:00:{i * 2:02d},000 --> 00:00:{i * 2 + 2:02d},000\n{text}"
        for i, text in enumerate(texts)
    ) + "\n", encoding="utf-8")
    return path


def test_published_song_changes_only_seven_spoken_texts(tmp_path):
    """Same real final SRT and frozen ASS, including every lyric/time/style."""
    srt, = PUBLISHED_PACKAGE.glob("*song_133029_1424.srt")
    original_ass, = PUBLISHED_PACKAGE.glob("*song_133029_1424.final-sapphire72.ass")
    before = {path: path.read_bytes() for path in (srt, original_ass)}
    output = tmp_path / "relabel-free.ass"
    song._write_sapphire_ass_from_srt(srt, output)
    old_lines = original_ass.read_text(encoding="utf-8").splitlines()
    new_lines = output.read_text(encoding="utf-8").splitlines()
    assert len(old_lines) == len(new_lines)
    changed = [(i, old, new) for i, (old, new) in enumerate(zip(old_lines, new_lines), 1)
               if old != new]
    assert [i for i, _, _ in changed] == list(range(56, 63))
    for _, old, new in changed:
        old_fields, new_fields = old.split(",", 9), new.split(",", 9)
        assert old_fields[:9] == new_fields[:9]
        assert new_fields[3] == "LDS"
        assert old_fields[9] == "[李豆沙] " + new_fields[9]
    old_lyrics = [row for row in _events("\n".join(old_lines)) if row[3] == "Default"]
    new_lyrics = [row for row in _events("\n".join(new_lines)) if row[3] == "Default"]
    assert len(new_lyrics) == len(old_lyrics) == 45
    assert new_lyrics == old_lyrics
    assert all(not re.match(r"^\[[^\]]+\]", row[9]) for row in _events("\n".join(new_lines)))
    assert all(path.read_bytes() == data for path, data in before.items())


@pytest.mark.parametrize("label,style", [
    (talk.HOST_SPEAKER, "LDS"),
    (talk.HOST_SPEAKER + " +0.06", "LDS"),
    (talk.GUEST_SPEAKER, "GUEST"),
    ("新来的访客", "GUEST"),
    ("Aurora_27 -0.08", "GUEST"),
])
@pytest.mark.parametrize("separator", [" ", "", "\n"])
def test_song_label_variants_are_metadata_before_layout(tmp_path, label, style, separator):
    # The metadata must not consume line width or be split into later events.
    text = "甲" * 24
    srt = _srt(tmp_path, [f"[{label}]{separator}{text}", "未标记的歌词"])
    before = srt.read_bytes()
    ass = tmp_path / "song.ass"
    song._write_sapphire_ass_from_srt(srt, ass)
    rows = _events(ass.read_text(encoding="utf-8"))
    assert [(row[3], row[9]) for row in rows] == [(style, text), ("Default", "未标记的歌词")]
    assert f"Style: {style}," in ass.read_text(encoding="utf-8")
    assert srt.read_bytes() == before


def test_configured_host_name_is_not_hardcoded(tmp_path, monkeypatch):
    monkeypatch.setattr(talk, "HOST_SPEAKER", "新的主播")
    srt = _srt(tmp_path, ["[新的主播] 这是说话", "这是歌词"])
    ass = tmp_path / "other-host.ass"
    song._write_sapphire_ass_from_srt(srt, ass)
    assert [(row[3], row[9]) for row in _events(ass.read_text())] == [
        ("LDS", "这是说话"), ("Default", "这是歌词"),
    ]


def test_song_speech_and_lyrics_cannot_move_words_across_their_seam(tmp_path):
    srt = _srt(tmp_path, [f"[{talk.HOST_SPEAKER}] 好久不见小", "李，总觉得上次见面后还没有分开过"])
    ass = tmp_path / "seam.ass"
    song._write_sapphire_ass_from_srt(srt, ass)
    assert [(row[3], row[9]) for row in _events(ass.read_text())] == [
        ("LDS", "好久不见小"), ("Default", "李，总觉得上次见面后还没有分开过"),
    ]


def test_talk_keeps_existing_style_placement_and_escaping_without_label_option(tmp_path):
    srt = _srt(tmp_path, [f"[{talk.HOST_SPEAKER} +0.06] 主播说话", f"[{talk.GUEST_SPEAKER} -0.08] 访客{{原文}}"])
    cues = talk.parse_labelled_srt(srt)
    cues[1] = replace(cues[1], layer=1, placement="above")
    ass = tmp_path / "talk.ass"
    talk.write_ass(cues, ass)
    rows = _events(ass.read_text())
    assert rows == [
        ["Dialogue: 0", "0:00:00.00", "0:00:02.00", "LDS", "", "0", "0", "0", "", "主播说话"],
        ["Dialogue: 1", "0:00:02.00", "0:00:04.00", "GUEST", "", "0", "0", "142", "", r"访客\{原文\}"],
    ]
    with pytest.raises(TypeError, match="show_speaker_labels"):
        talk.write_ass(cues, ass, show_speaker_labels=True)


def test_talk_text_never_displays_a_leading_label_but_keeps_body_brackets(tmp_path):
    srt = _srt(tmp_path, [f"[{talk.HOST_SPEAKER}] [普通文本] 保留[正文]原文"])
    ass = tmp_path / "literal.ass"
    talk.write_ass(talk.parse_labelled_srt(srt), ass)
    assert _events(ass.read_text())[0][9] == "保留[正文]原文"


def test_unknown_talk_speaker_is_still_rejected(tmp_path):
    srt = _srt(tmp_path, ["[尚未确认的人] 不能当作主播"])
    with pytest.raises(ValueError, match="not labelled"):
        talk.parse_labelled_srt(srt)


def test_both_renderers_use_shared_definition_without_assembling_prefixes(monkeypatch, tmp_path):
    from src.autoslice import speaker_label_display as policy

    original = policy.speaker_display
    calls = []

    def observed(text, **kwargs):
        calls.append((text, kwargs))
        return original(text, **kwargs)

    monkeypatch.setattr(song, "speaker_display", observed)
    srt = _srt(tmp_path, [f"[{talk.HOST_SPEAKER}] 这句是说话"])
    song._write_sapphire_ass_from_srt(srt, tmp_path / "song.ass")
    talk.write_ass(talk.parse_labelled_srt(srt), tmp_path / "talk.ass")
    assert len(calls) == 2
    assert calls[0][0] == f"[{talk.HOST_SPEAKER}] 这句是说话"
    assert calls[1][0] == "这句是说话"
    for renderer in (song._write_sapphire_ass_from_srt, talk.write_ass):
        tree = ast.parse(textwrap.dedent(inspect.getsource(renderer)))
        assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id == "render_burned_ass" for node in ast.walk(tree))
        assert not any(
            isinstance(node, ast.JoinedStr)
            and any(
                isinstance(left, ast.Constant) and isinstance(left.value, str)
                and left.value.endswith("[") and isinstance(middle, ast.FormattedValue)
                and isinstance(right, ast.Constant) and isinstance(right.value, str)
                and right.value.startswith("]")
                for left, middle, right in zip(node.values, node.values[1:], node.values[2:])
            )
            for node in ast.walk(tree)
        ), "renderers must not assemble their own speaker prefixes"
