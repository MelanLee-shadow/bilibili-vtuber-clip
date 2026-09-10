"""Rendering-only term boundary regression; no model, network or SRT rewrite."""

import pytest
from src.autoslice import subtitle_rendering as layout

C14_FIRST = (250, 1730, "好久不见小")
C14_SECOND = (1730, 3750, "李，总觉得上次见面后还没有分开过")


def test_real_c14_moves_only_word_tail_keeps_both_short_events():
    rows = layout._layout_cue_sequence_for_display([C14_FIRST, C14_SECOND])
    assert rows == [
        (0, 250, 1730, "好久不见小李，"),
        (1, 1730, 3750, "总觉得上次见面后还没有分开过"),
    ]


@pytest.mark.parametrize("gap", [121, 500, -10])
def test_never_join_across_silence_or_overlap(gap):
    second = (1730 + gap, 3750 + gap, C14_SECOND[2])
    rows = layout._layout_cue_sequence_for_display([C14_FIRST, second])
    assert len(rows) == 2


def test_never_join_two_speakers_or_placement_layers():
    rows = layout._layout_cue_sequence_for_display(
        [C14_FIRST, C14_SECOND], continuity_keys=[("host", 0), ("guest", 0)]
    )
    assert len(rows) == 2


def test_not_all_contiguous_phrases_are_merged():
    rows = layout._layout_cue_sequence_for_display(
        [(0, 1400, "谢谢大家"), (1400, 2800, "欢迎回来")]
    )
    assert len(rows) == 2


def test_latin_registered_name_stays_one_word_across_cues():
    assert layout._layout_cue_sequence_for_display(
        [(0, 1300, "谢谢k"), (1300, 2600, "mx的礼物")]
    ) == [(0, 0, 1300, "谢谢"), (1, 1300, 2600, "kmx的礼物")]


def test_sequence_keeps_input_rows_and_all_words_unchanged():
    original = [C14_FIRST, C14_SECOND, (4000, 5500, "下面一句不变")]
    before = list(original)
    rows = layout._layout_cue_sequence_for_display(original)
    assert original == before
    assert "".join(r[3].replace(r"\N", "") for r in rows) == "".join(r[2] for r in before)
    assert rows[-1] == (2, 4000, 5500, "下面一句不变")


def test_no_soft_wrap_inside_long_registered_name():
    text = "这是很长的前缀文字用来测试" + ("嗯" * 10) + "李豆沙和kmx一起玩游戏"
    rows = layout._layout_cue_for_display(0, 9000, text)
    for word in ["李豆沙", "kmx"]:
        assert any(word in r[2] for r in rows)
    assert "小" + r"\N" + "李" not in layout._wrap_ass_text("嗯" * 23 + "小李来了")


def test_whole_short_clause_stays_one_line():
    text = "上次见到小李还是眨眼之前"
    assert layout._layout_cue_for_display(0, 2000, text) == [(0, 2000, text)]


def test_uniform_writer_and_independent_auditor_use_same_display_sequence(tmp_path):
    from src.autoslice.review_package_ass_audit import _expected_events, _SpeakerCue, HOST_SPEAKER

    raw = "1\n00:00:00,250 --> 00:00:01,730\n好久不见小\n\n2\n00:00:01,730 --> 00:00:03,750\n李，总觉得上次见面后还没有分开过\n"
    srt = tmp_path / "source.srt"
    srt.write_text(raw)
    ass = tmp_path / "test.ass"
    layout._write_sapphire_ass_from_srt(srt, ass)
    assert srt.read_text() == raw
    events = [s for s in ass.read_text().splitlines() if s.startswith("Dialogue:")]
    assert len(events) == 2 and "小李" in events[0] and r"\N" not in events[0]
    expected = _expected_events(
        [_SpeakerCue(a, b, HOST_SPEAKER, t) for a, b, t in [C14_FIRST, C14_SECOND]],
        host_style="Default",
    )
    assert len(expected) == 2 and expected[0].text == events[0].split(",", 9)[9]


def test_long_span_is_not_merged_even_when_name_straddles_boundary():
    rows = layout._layout_cue_sequence_for_display([(0, 3500, "好久不见小"), (3500, 6500, "李")])
    assert len(rows) == 2


def test_oversize_clause_is_not_merged_just_for_one_term():
    rows = layout._layout_cue_sequence_for_display(
        [(0, 1400, "之前说了很多事情现在来欢迎小"), (1400, 3500, "李后来讲了别的话题接着继续聊天")]
    )
    assert len(rows) >= 2
    assert rows[0][0] == 0 and rows[-1][0] == 1


def test_indivisible_oversize_ascii_token_does_not_get_cut_silently():
    with pytest.raises(ValueError, match="indivisible"):
        layout._layout_cue_for_display(0, 3000, "abcdefghijklmnopqrstuvwxyzz")


def test_english_spaces_and_registered_capitalisation_remain():
    text = "Hello KMX welcome back"
    assert layout._layout_cue_for_display(0, 3000, text) == [(0, 3000, text)]


def test_speaker_writer_keeps_host_identity_and_only_moves_between_same_speaker(tmp_path):
    from scripts.apply_speaker_turn_overrides import (
        parse_labelled_srt,
        write_ass,
        HOST_SPEAKER,
        GUEST_SPEAKER,
    )

    p = tmp_path / "speaker.srt"
    p.write_text(
        "1\n00:00:00,250 --> 00:00:01,730\n["
        + HOST_SPEAKER
        + "] 好久不见小\n\n2\n00:00:01,730 --> 00:00:03,750\n["
        + HOST_SPEAKER
        + "] 李，总觉得上次见面后还没有分开过\n"
    )
    a = tmp_path / "speaker.ass"
    write_ass(parse_labelled_srt(p), a)
    rows = [r for r in a.read_text().splitlines() if r.startswith("Dialogue:")]
    assert len(rows) == 2 and ",LDS," in rows[0] and "小李" in rows[0]
    p.write_text(
        p.read_text().replace("[" + HOST_SPEAKER + "] 李，", "[" + GUEST_SPEAKER + "] 李，")
    )
    write_ass(parse_labelled_srt(p), a)
    rows = [r for r in a.read_text().splitlines() if r.startswith("Dialogue:")]
    assert len(rows) == 2 and ",GUEST," in rows[1]


def test_new_repair_never_advances_whole_next_clause():
    result = layout._layout_cue_sequence_for_display([C14_FIRST, C14_SECOND])
    assert len(result) == 2
    assert "总觉得" not in result[0][3]
    assert [(r[1], r[2]) for r in result] == [(250, 1730), (1730, 3750)]


def test_no_automatic_repair_when_word_requires_more_than_two_characters(monkeypatch):
    monkeypatch.setattr(layout, "_layout_protected_terms", lambda: frozenset({"甲乙丙丁戊己庚辛"}))
    source = [(0, 1000, "欢迎甲乙丙丁"), (1000, 2100, "戊己庚辛回来")]
    result = layout._layout_cue_sequence_for_display(source)
    assert [r[3] for r in result] == [r[2] for r in source]


def test_boundary_shift_does_not_erase_only_word_cue():
    source = [(0, 1400, "我看见小"), (1400, 2100, "李")]
    result = layout._layout_cue_sequence_for_display(source)
    assert len(result) == 2 and all(r[3].strip() for r in result)
    assert "".join(r[3] for r in result) == "我看见小李"


def test_static_names_reach_asr_term_repair_without_topic_snapshot(monkeypatch):
    from scripts import produce_slice_package as producer
    from src.autoslice.jingting_chunker import SrtCue
    from src.autoslice.term_boundary import unify_terms_across_cues

    monkeypatch.setattr(producer, "approved_timely_terms", lambda: [])
    monkeypatch.setattr(producer, "_topic_graph_disabled", lambda: True)
    terms = producer._load_term_boundary_surfaces({})
    assert "小李" in terms and "李豆沙" in terms
    src = [
        SrtCue(index=str(i + 1), start_ms=a, end_ms=b, text=t)
        for i, (a, b, t) in enumerate([C14_FIRST, C14_SECOND])
    ]
    result, moves = unify_terms_across_cues(src, terms)
    assert moves and any("小李" in r.text for r in result)
    assert [(r.start_ms, r.end_ms) for r in result] == [(250, 1730), (1730, 3750)]
    assert "".join(r.text for r in result) == "".join(r.text for r in src)


@pytest.mark.parametrize("gap", [121, 500, -10])
def test_early_term_repair_respects_timing_gap(gap):
    from src.autoslice.term_boundary import unify_terms_across_cues
    from src.autoslice.jingting_chunker import SrtCue

    a = SrtCue(index="1", start_ms=0, end_ms=1000, text="谢谢小")
    b = SrtCue(index="2", start_ms=1000 + gap, end_ms=2000 + gap, text="李的礼物")
    out, moves = unify_terms_across_cues([a, b], ["小李"])
    assert not moves and out == [a, b]


def test_cpa_actual_prompt_contains_pair_semantics_and_preserves_timeline(monkeypatch):
    import json
    from src.autoslice import full_session_transcription as transcriber
    from scripts import gemini_slice_jingting
    from src.autoslice.jingting_chunker import parse_srt_cues

    monkeypatch.setattr(gemini_slice_jingting, "glossary", lambda: "")
    seen = []

    def llm(prompt):
        seen.append(prompt)
        return json.dumps(
            {
                "cues": [
                    {"n": 1, "text": "好久不见小李，"},
                    {"n": 2, "text": "总觉得上次见面后还没有分开过"},
                ]
            },
            ensure_ascii=False,
        )

    raw = "1\n00:00:00,250 --> 00:00:01,730\n好久不见小\n\n2\n00:00:01,730 --> 00:00:03,750\n李，总觉得上次见面后还没有分开过\n"
    out = transcriber._cpa_correct_draft_cues(raw, danmaku_lines=[], cpa_llm_call=llm)
    assert len(seen) == 1 and "不限词表专名" in seen[0] and "不要为了完整句把两条合并" in seen[0]
    cues = parse_srt_cues(out)
    assert len(cues) == 2 and cues[0].text == "好久不见小李，"
    assert [(c.start_ms, c.end_ms) for c in cues] == [(250, 1730), (1730, 3750)]


def test_raw_asr_boundary_origin_is_kept_not_relabelled_semantic(tmp_path):
    import json
    from pathlib import Path
    from src.autoslice.subtitle_draft_preparation import record_asr_boundary_origin

    raw = {
        "provider": "bcut",
        "elapsed_s": 1.0,
        "utterances": [
            {
                "start_time": 250,
                "end_time": 1730,
                "transcript": "好久不见小",
                "words": [{"label": "小", "start_time": 1500, "end_time": 1730}],
            },
            {
                "start_time": 1730,
                "end_time": 3750,
                "transcript": "李，总觉得上次见面后还没有分开过",
                "words": [{"label": "李", "start_time": 1730, "end_time": 1850}],
            },
        ],
    }
    srt, origin = record_asr_boundary_origin(raw, tmp_path / "raw.mp4")
    assert "好久不见小\n\n2\n00:00:01,730" in srt
    saved = json.loads(Path(origin["raw_result_path"]).read_text())
    assert saved == raw and origin["raw_utterance_count"] == 2 and origin["word_timings_present"]
    assert record_asr_boundary_origin(raw, tmp_path / "raw.mp4") == (srt, origin)
