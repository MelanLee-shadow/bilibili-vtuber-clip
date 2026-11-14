"""Deterministic generation fixes: subtitle shape (FMR A2/A3 limits, read from the lane's audit module), the repo's
surface canon (A8), and silence snapping of cut points (A7)."""

from __future__ import annotations

from itertools import pairwise

import pytest

from src.autoslice.manual_lane import normalize as nz
from src.autoslice.manual_lane import subtitle_track
from src.autoslice.subtitle_validation import validate_srt_text


def _nonspace(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


def test_a_long_cue_is_split_so_every_piece_fits_the_lane_limits():
    audit = nz._audit()
    text = "一二三四五六七八九十甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥"
    pieces = nz.split_cue(text, 10.0, 40.0, audit)
    assert len(pieces) >= 2
    for piece in pieces:
        lines = piece["text"].split("\n")
        assert len(lines) <= audit.DEFAULT_MAX_VISUAL_LINES
        assert all(_nonspace(line) <= audit.DEFAULT_MAX_VISUAL_LINE_CHARS for line in lines)
        assert piece["end_s"] - piece["start_s"] <= audit.LONG_STATIC_CUE_SECONDS + 1e-6
    assert abs(pieces[0]["start_s"] - 10.0) < 1e-9 and abs(pieces[-1]["end_s"] - 40.0) < 1e-9
    for a, b in pairwise(pieces):
        assert abs(a["end_s"] - b["start_s"]) < 1e-9
    assert "".join(p["text"].replace("\n", "") for p in pieces) == text


def test_a_short_cue_stays_one_cue_and_one_line():
    audit = nz._audit()
    pieces = nz.split_cue("短句", 0.0, 2.0, audit)
    assert pieces == [{"start_s": 0.0, "end_s": 2.0, "text": "短句"}]


@pytest.mark.parametrize(("text", "protected"), [
    ("今天参加活动的时候我们带上手工贺卡和大家见面", ["参加", "活动", "我们", "手工", "贺卡"]),
    ("她说接下来会在12月24日和大家一起庆祝这个节日", ["12月24日", "庆祝", "节日"]),
    ("我今天想和大家一起重新观看SuperNova2026的完整演出", ["SuperNova2026", "完整", "演出"]),
    ("昨天的活动我们碰到小彬Nova说了很多开心的事情", ["小彬Nova", "事情"]),
    ("就非常非常正好看到由菜Yuna给我发消息", ["由菜Yuna"]),
    ("你们想不想听她说一说《夜空中的银色列车》的故事", ["《夜空中的银色列车》", "故事"]),
    ("她刚才看完「风吹过的地方」之后和朋友说了很多话", ["「风吹过的地方」", "朋友"]),
])
def test_unseen_sentences_wrap_at_word_and_protected_boundaries(text, protected):
    shaped = nz.split_cue(text, 0.0, 4.0, nz._audit())
    lines = [line for cue in shaped for line in cue["text"].splitlines()]
    assert "".join(lines) == text
    assert all(_nonspace(line) <= nz._audit().DEFAULT_MAX_VISUAL_LINE_CHARS for line in lines)
    for word in protected:
        assert any(word in line for line in lines), (word, lines)


def test_indivisible_title_that_cannot_fit_is_rejected():
    with pytest.raises(ValueError, match="cannot split"):
        nz.split_cue("《" + "甲" * 40 + "》", 0.0, 3.0, nz._audit())


@pytest.mark.parametrize(("text", "duration"), [("嗯", 30.0), ("对对", 30.0), ("好的好的", 60.0),
                                                ("好呀好", 120.0)])
def test_short_text_over_long_span_preserves_the_baseline_cue(text, duration):
    assert nz.split_cue(text, 0.0, duration, nz._audit()) == [
        {"start_s": 0.0, "end_s": duration, "text": text}]


def test_long_text_over_extreme_duration_can_still_use_a_valid_split():
    text = "甲" * 20 + "……" + "乙" * 60
    cues = nz.shape_cues([{"start_s": 0.0, "end_s": 600.0, "text": text}])
    assert "".join(c["text"].replace("\n", "") for c in cues) == text
    assert cues[0]["start_s"] == 0.0 and cues[-1]["end_s"] == 600.0
    assert all(nz.text_len(line) <= nz._audit().DEFAULT_MAX_VISUAL_LINE_CHARS
               for c in cues for line in c["text"].splitlines())


@pytest.mark.parametrize("text", ["甲" * 14 + "……" + "乙" * 5, "甲" * 14 + "？！" + "乙" * 5,
                                  "甲" * 17 + "……" + "乙" * 17])
def test_splitting_preserves_punctuation_and_produces_release_valid_lines(text, tmp_path):
    # The ordinary writer and the review-fix writer share shape_cues and both subtitle tracks.
    cues = [{"start_s": 0.0, "end_s": 4.0, "text": text, "speaker": "李豆沙"}]
    shaped = nz.shape_cues(cues)
    subtitle_track.write_track(tmp_path, shaped, speakers=True)
    final = (tmp_path / "subs/final.srt").read_text()
    speaker = (tmp_path / "speaker/speaker.srt").read_text()
    errors = validate_srt_text(final)["errors"]
    assert not any(e["code"] == "SRT_TEXT_LEADING_PUNCTUATION" for e in errors)
    assert "".join(c["text"].replace("\n", "") for c in shaped) == text
    assert all(nz.text_len(line) <= nz._audit().DEFAULT_MAX_VISUAL_LINE_CHARS
               for c in shaped for line in c["text"].splitlines())
    assert [c["text"] for c in subtitle_track.parse_blocks(final)] == [
        c["text"] for c in subtitle_track.parse_blocks(speaker)]


def test_preexisting_illegal_initial_punctuation_is_not_silently_deleted():
    text = "，" + "甲" * 20
    shaped = nz.shape_cues([{"start_s": 0.0, "end_s": 4.0, "text": text}])
    result = subtitle_track.format_blocks(shaped, labels=False)
    assert any(e["code"] == "SRT_TEXT_LEADING_PUNCTUATION" for e in validate_srt_text(result)["errors"])
    assert "".join(c["text"].replace("\n", "") for c in shaped) == text


def test_no_piece_is_a_lone_character():
    audit = nz._audit()
    text = "甲" * 37 + "乙"
    for piece in nz.split_cue(text, 0.0, 30.0, audit):
        assert _nonspace(piece["text"]) >= 2


def test_canon_uses_the_repo_surface_rules_and_reports_each_replacement():
    text, hits = nz.canon_text("这是 superchat 的例子")
    assert text == "这是 SC 的例子"
    assert any(h.get("surface") == "superchat" and h.get("canonical") == "SC" for h in hits)


def test_canon_leaves_unrelated_text_alone():
    assert nz.canon_text("普通句子") == ("普通句子", [])


def test_normalize_cues_canons_then_splits():
    audit = nz._audit()
    cues = [{"id": 1, "start_s": 0.0, "end_s": 2.0, "text": "superchat"},
            {"id": 2, "start_s": 2.0, "end_s": 30.0, "text": "甲" * 50}]
    out, log = nz.normalize_cues(cues)
    assert out[0]["text"] == "SC"
    assert len(out) > 2 and log["extra_cues_from_split"] == len(out) - 2
    assert all(_nonspace(line) <= audit.DEFAULT_MAX_VISUAL_LINE_CHARS
               for piece in out for line in piece["text"].split("\n"))
    assert log["canon_replacements"][0]["cue_id"] == 1


def test_silence_intervals_are_read_on_the_absolute_clock():
    log = ("[silencedetect @ x] silence_start: 0.5\n"
           "[silencedetect @ x] silence_end: 1.5 | silence_duration: 1.0\n"
           "[silencedetect @ x] silence_start: 3.0\n")
    assert nz.silence_intervals(log, 10.0) == [(10.5, 11.5), (13.0, float("inf"))]


def test_start_point_moves_into_a_silence_that_lasts_past_it():
    intervals = [(9.8, 10.9)]
    assert nz.snap_point(10.0, intervals, role="start") == 10.0          # already inside, 0.9 s of silence left
    # silence 9.8-10.3 leaves 0.1 s from 10.2 on: the closest qualifying moment to 10.4 is 10.2
    assert abs(nz.snap_point(10.4, [(9.8, 10.3)], role="start") - 10.2) < 1e-9


def test_end_point_moves_to_a_moment_with_enough_silence_before_it():
    assert nz.snap_point(10.0, [(9.7, 10.2)], role="end") == 10.0       # 0.3 s of silence before 10.0
    moved = nz.snap_point(10.0, [(9.95, 10.4)], role="end")             # only 0.05 s before 10.0: must move
    assert moved is not None and moved - 9.95 >= nz.SILENCE_MIN_S - 1e-9 and abs(moved - 10.0) <= nz.SNAP_RADIUS_S


def test_no_silence_within_the_radius_means_no_move():
    assert nz.snap_point(10.0, [(12.0, 13.0)], role="start") is None
    assert nz.snap_point(10.0, [], role="end") is None


def test_an_unterminated_silence_counts_for_the_end_point():
    assert nz.snap_point(10.0, [(9.0, float("inf"))], role="end") == 10.0


def test_uniform_host_speaker_srt_passes_the_lanes_own_speaker_parser(tmp_path):
    # FMR A3 rejects a speaker SRT whose lines lack the canonical label.  The label is added on the first line only,
    # timing and text are kept, and the lane's parser reads every cue with no issue.
    from src.autoslice.manual_lane import stage_impl as si
    from src.autoslice.review_package_ass_audit import _speaker_cues

    final = tmp_path / "final.srt"
    final.write_text("1\n00:00:00,000 --> 00:00:02,000\n我来给你讲一个\n\n"
                     "2\n00:00:02,000 --> 00:00:05,000\n第二句很长的台词\n第二行\n", encoding="utf-8")
    labelled = tmp_path / "speaker.srt"
    labelled.write_text(si._label_host_lines(final), encoding="utf-8")
    cues, issues = _speaker_cues(labelled)
    assert issues == [] and len(cues) == 2
    assert [c.speaker for c in cues] == ["李豆沙", "李豆沙"]
    assert cues[1].text == "第二句很长的台词 第二行" or cues[1].text == "第二句很长的台词\n第二行"
    assert "[李豆沙] 第二句很长的台词" in labelled.read_text(encoding="utf-8")
