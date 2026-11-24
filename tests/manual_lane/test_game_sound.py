"""Step 3: the game's or the software's own words are not subtitled (game_sound, owner U469)."""

from __future__ import annotations

import json

from fakes import FakeCpa, FakeRemote, make_ctx

from src.autoslice.manual_lane import game_sound as gs
from src.autoslice.manual_lane import package_guards, step3_evidence, step3_subtitles
from src.autoslice.manual_lane import subtitle_track as st
from src.autoslice.manual_lane.runner import StepOutcome
from src.autoslice.manual_lane.srt_text import parse_srt


def cue(start, end, text, speaker=None):
    return {"start_s": start, "end_s": end, "text": text, "speaker": speaker}


# 11-23 316670 redone from step 3 with R21: the countdown the agent wrote and the one the jianying orphan rule added
CLIP_316670 = [cue(44.0, 46.9, "矿工也是很难当"), cue(47.3, 48.22, "10"), cue(49.68, 51.0, "矿工也是很难当"),
               cue(51.16, 53.4, "哎这居然没中"), cue(54.68, 55.76, "three two"), cue(59.2, 61.0, "冲冲冲")]


def rows(*parts):
    return [{"source": s, "start_s": t, "text": x} for s, t, x in parts]


def records(win, first, second, status="OK"):
    return [{"win": win, "slug": "38m", "model": "m38", "status": status, "lines": first},
            {"win": win, "slug": "argon", "model": "carbon", "status": status, "lines": second}]


def test_candidates_are_the_lines_with_a_clause_of_their_own_in_latin_letters_or_digits():
    lines = ["10", "three two", "six，哇，哇！我们是来看烟花的吗？", "OK，懂了喵。", "ten, nine, eight, seven, six,",
             "five, four, three, two, one！上不来！", "F键，F，F。对，试试。", "two，啊啊啊啊",
             # letters or digits inside her own words are no clause of their own
             "awawa是猪！ awawa是猪！awawa是猪！", "怎么还有30秒？", "kmx说什么我就做什么啦", "矿工也是很难当"]
    found = gs.candidates([cue(i, i + 1, t) for i, t in enumerate(lines)])
    assert [lines[i] for i in found] == lines[:8]
    assert [gs.parts(lines[6])[i] for i in gs.foreign_clauses(lines[6])] == ["F", "F"]


def test_a_countdown_both_levels_place_in_the_game_is_removed_and_the_package_step_does_not_stop_for_it(tmp_path):
    clip = tmp_path / "clip"
    (clip / "subs").mkdir(parents=True)
    (clip / "asr").mkdir()
    (clip / "subs/final.srt").write_text(st.format_blocks(CLIP_316670, labels=True), encoding="utf-8")
    # the dictation heard the countdown too: without the record step 8 would stop on SPEECH_WITHOUT_SUBTITLES
    (clip / "asr/bcut.srt").write_text("1\n00:00:54,700 --> 00:00:55,700\nthree two one\n", encoding="utf-8")
    cues = st.parse_blocks((clip / "subs/final.srt").read_text(encoding="utf-8"))
    wins = gs.windows(cues, gs.candidates(cues))
    assert [(w["win"], w["text"], w["audio"]) for w in wins] == [("GS002", "10", [47.1, 48.95]),
                                                                ("GS005", "three two", [54.04, 56.76])]
    # one level spells the number as the line does, the other as it was said
    got = [*records("GS002", rows(("game", 1.0, "10")), rows(("game", 0.9, "ten"))),
           *records("GS005", rows(("game", 0.7, "three two")), rows(("game", 0.6, "three"), ("game", 1.1, "two")))]
    verdicts = gs.decide(cues, wins, got, ["38m", "argon"])
    kept, left_out = gs.apply(cues, verdicts)
    assert [c["text"] for c in kept] == ["矿工也是很难当", "矿工也是很难当", "哎这居然没中", "冲冲冲"]
    assert [(r["start_s"], r["end_s"], r["text"]) for r in left_out] == [(47.3, 48.22, "10"), (54.68, 55.76, "three two")]
    (clip / "subs/final.srt").write_text(st.format_blocks(kept, labels=True), encoding="utf-8")
    package_guards.record_left_out(clip, left_out, gs.BY)
    final = st.parse_blocks((clip / "subs/final.srt").read_text(encoding="utf-8"))
    found = package_guards.gaps_with_speech(final, parse_srt((clip / "asr/bcut.srt").read_text()))
    assert found and package_guards.undecided(clip, found) == []


def test_her_words_after_a_countdown_stay_and_start_where_they_were_heard():
    cues = [cue(50.0, 52.0, "好"), cue(53.97, 57.2, "six，哇，哇！我们是来看烟花的吗？"),
            cue(80.0, 86.0, "ten, nine, eight, seven, six,"), cue(86.946, 92.0, "five, four, three, two, one！上不来！")]
    wins = gs.windows(cues, gs.candidates(cues))
    got = [*records("GS002", rows(("game", 1.0, "six"), ("person", 1.8, "哇，哇！我们是来看烟花的吗？")),
                    rows(("game", 1.0, "six"), ("person", 2.0, "哇，哇！我们是来看烟花的吗？"))),
           *records("GS003", rows(("game", 1.0, "ten, nine, eight, seven, six,")), rows(("game", 1.0, "ten nine eight seven six"))),
           *records("GS004", rows(("game", 1.0, "five, four, three, two, one！"), ("person", 4.0, "上不来！")),
                    rows(("game", 1.0, "five four three two one"), ("person", 4.1, "上不来")))]
    kept, left_out = gs.apply(cues, gs.decide(cues, wins, got, ["38m", "argon"]))
    assert [(round(c["start_s"], 3), c["end_s"], c["text"]) for c in kept] == [
        (50.0, 52.0, "好"), (round(wins[0]["audio"][0] + 1.8, 3), 57.2, "哇，哇！我们是来看烟花的吗？"),
        (round(wins[2]["audio"][0] + 4.0, 3), 92.0, "上不来！")]
    assert [r["text"] for r in left_out] == ["six", "ten，nine，eight，seven，six", "five，four，three，two，one"]


def test_a_line_one_level_hears_as_a_person_or_only_one_level_answers_stays():
    cues = [cue(76.48, 78.0, "OK，懂了喵。"), cue(30.11, 33.0, "F键，F，F。对，试试。"), cue(46.33, 48.0, "two，啊啊啊啊")]
    wins = gs.windows(cues, gs.candidates(cues))
    got = [*records("GS001", rows(("game", 0.5, "OK"), ("person", 0.9, "懂了喵")), rows(("person", 0.5, "OK，懂了喵。"))),
           *records("GS002", rows(("person", 0.2, "F键，F，F。对，试试。")), rows(("unsure", 0.2, "F键，F，F。"), ("person", 1.5, "对，试试"))),
           records("GS003", rows(("game", 0.5, "two，啊啊啊啊")), [])[0]]
    verdicts = gs.decide(cues, wins, got, ["38m", "argon"])
    assert [v["removed"] for v in verdicts] == [[], [], []]
    kept, left_out = gs.apply(cues, verdicts)
    assert kept == cues and left_out == []
    assert "只有一遍复听回答、未删：第 3 句「two，啊啊啊啊」" in gs.process_line(verdicts)


def test_her_chinese_words_are_never_removed_even_when_both_levels_call_the_whole_line_the_game():
    cues = [cue(46.33, 48.0, "two，啊啊啊啊")]
    wins = gs.windows(cues, [0])
    got = records("GS001", rows(("game", 0.5, "two，啊啊啊啊")), rows(("game", 0.4, "two 啊啊啊啊")))
    kept, _ = gs.apply(cues, gs.decide(cues, wins, got, ["38m", "argon"]))
    assert [c["text"] for c in kept] == ["啊啊啊啊"]


def _clip_ctx(tmp_path, cues):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=FakeCpa([]))
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(st.format_blocks(cues, labels=True), encoding="utf-8")
    return ctx


def _step3_hears(monkeypatch, listen):
    monkeypatch.setattr(step3_subtitles, "_step3_decided", lambda c: StepOutcome(True))
    monkeypatch.setattr(step3_subtitles, "_chat_product", lambda c, p: None)
    monkeypatch.setattr(step3_evidence, "upload_listen_tools", lambda c, p: None)
    monkeypatch.setattr(step3_evidence, "_listen_pass", listen)
    monkeypatch.setattr(step3_subtitles.writing_canon, "settle_step3", lambda c, o, verbatim: o)


def _hearing(second_level="OK"):
    """A listening tool that places every candidate line in the game; the second level answers with `second_level`."""
    asked = []

    def listen(ctx_, label, windows, models, *, prompt, prepared, **_kw):
        asked.append((label, [(w["win"], w["text"], w["audio"]) for w in windows], [m["slug"] for m in models], prompt,
                      prepared))
        return [{"win": w["win"], "slug": m["slug"], "model": m["name"], "lines": rows(("game", 1.0, w["text"])),
                 "status": "OK" if m["slug"] == "38m" else second_level} for w in windows for m in models]
    return asked, listen


def test_step3_takes_the_countdown_out_of_the_lane_s_own_subtitles(tmp_path, monkeypatch):
    # the guard on the ordinary path: step 3 as a whole, its decided subtitles holding 316670's countdown
    ctx = _clip_ctx(tmp_path, CLIP_316670)
    asked, listen = _hearing()
    _step3_hears(monkeypatch, listen)
    outcome = step3_subtitles.step3_subtitles(ctx)
    assert outcome.ok
    assert [(a[0], [t for _w, t, _a in a[1]], a[2], a[3], a[4]) for a in asked] == [
        ("game-sound", ["10", "three two"], ["38m", "argon"], "listen_game_sound.md", True),
        ("game-sound-again", ["10", "three two"], ["38m"], "listen_game_sound.md", True)]
    # the second hearing asks the same audio under its own window names (the tool would hand back the first answer)
    assert [(w, a) for w, _t, a in asked[1][1]] == [("GR" + w[2:], a) for w, _t, a in asked[0][1]]
    final = (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")
    assert "three two" not in final and "\n10\n" not in final and "哎这居然没中" in final
    assert [v["removed"] for v in json.loads((ctx.clip_dir / gs.REPORT).read_text(encoding="utf-8"))] == [[0], [0]]
    assert any("删去 2 句" in line for line in outcome.process)


def test_with_the_second_level_off_the_first_level_heard_twice_takes_the_countdown_out(tmp_path, monkeypatch):
    # 11-24: lane_rules listening.models argon names no served model since 11-16 (no Carbon), so the second level is
    # UNAVAILABLE on every window; asked the two levels only, 850930's 「ten, nine, ...」 stayed although 3.8 heard the game
    ctx = _clip_ctx(tmp_path, CLIP_316670)
    asked, listen = _hearing(second_level="UNAVAILABLE")
    _step3_hears(monkeypatch, listen)
    outcome = step3_subtitles.step3_subtitles(ctx)
    assert outcome.ok and len(asked) == 2
    final = (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")
    assert "three two" not in final and "\n10\n" not in final
    verdicts = json.loads((ctx.clip_dir / gs.REPORT).read_text(encoding="utf-8"))
    assert [sorted(v["levels"]) for v in verdicts] == [["38m", "38m-again"]] * 2
    assert [v["removed"] for v in verdicts] == [[0], [0]]


def test_step3_stops_when_no_level_answered_for_a_candidate(tmp_path, monkeypatch):
    ctx = _clip_ctx(tmp_path, CLIP_316670)
    _step3_hears(monkeypatch, lambda *a, **k: [{"win": "GS002", "slug": "38m", "status": "QUOTA", "lines": []}])
    outcome = step3_subtitles.step3_subtitles(ctx)
    assert not outcome.ok and outcome.reason == gs.UNHEARD and "「three two」" in outcome.note


def test_one_hearing_alone_removes_nothing(tmp_path, monkeypatch):
    # the first hearing places the countdown in the game, its second hearing gives no answer: nothing is removed
    ctx = _clip_ctx(tmp_path, CLIP_316670)

    def listen(ctx_, label, windows, models, **_kw):
        status = "OK" if label == "game-sound" else "EMPTY"
        return [{"win": w["win"], "slug": "38m", "model": "m38", "status": status,
                 "lines": rows(("game", 1.0, w["text"]))} for w in windows]
    _step3_hears(monkeypatch, listen)
    outcome = step3_subtitles.step3_subtitles(ctx)
    assert outcome.ok and "three two" in (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")
    assert any("只有一遍复听回答、未删" in line for line in outcome.process)


def test_a_line_a_hearing_came_back_empty_for_is_heard_a_third_time(tmp_path, monkeypatch):
    # 11-23 1493180: 3.8's first hearing of 「Three, two.」 came back with no part at all and its second placed it in
    # the game; one answer removes nothing, so the countdown stayed.  A hearing with no part has not answered: that line,
    # and only that line, is heard once more
    ctx = _clip_ctx(tmp_path, CLIP_316670)
    asked = []

    def listen(ctx_, label, windows, models, **_kw):
        asked.append((label, [w["win"] for w in windows]))

        def lines(w, slug):
            if label == "game-sound" and (slug != "38m" or w["text"] == "three two"):
                return []       # the second level is off; 3.8 heard nothing in the countdown's window
            return rows(("game", 1.0, w["text"]))
        return [{"win": w["win"], "slug": m["slug"], "model": m["name"], "status": "OK", "lines": lines(w, m["slug"])}
                for w in windows for m in models]
    _step3_hears(monkeypatch, listen)
    outcome = step3_subtitles.step3_subtitles(ctx)
    assert outcome.ok
    assert asked == [("game-sound", ["GS002", "GS005"]), ("game-sound-again", ["GR002", "GR005"]),
                     ("game-sound-third", ["GT005"])]
    final = (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")
    assert "three two" not in final and "\n10\n" not in final
    verdicts = json.loads((ctx.clip_dir / gs.REPORT).read_text(encoding="utf-8"))
    assert [sorted(v["levels"]) for v in verdicts] == [["38m", "38m-again"], ["38m-again", "38m-third"]]
    assert [v["removed"] for v in verdicts] == [[0], [0]]


def test_a_clip_without_such_a_line_is_not_heard_and_an_imported_push_is_left_alone(tmp_path, monkeypatch):
    ctx = _clip_ctx(tmp_path, [cue(1.0, 2.0, "矿工也是很难当"), cue(3.0, 4.0, "怎么还有30秒？")])
    monkeypatch.setattr(step3_evidence, "_listen_pass", lambda *a, **k: (_ for _ in ()).throw(AssertionError("heard")))
    process = []
    assert gs.settle(ctx, process) is None and process == ["- 游戏/软件声音检查：没有只写外文或数字的分句，未复听"]
    pushed = _clip_ctx(tmp_path / "push", CLIP_316670)
    monkeypatch.setattr(step3_subtitles, "imported", lambda c: {"decided_by": "owner"})
    monkeypatch.setattr(step3_subtitles, "_step3_decided", lambda c: StepOutcome(True))
    monkeypatch.setattr(step3_subtitles, "_chat_product", lambda c, p: None)
    monkeypatch.setattr(step3_subtitles.writing_canon, "settle_step3", lambda c, o, verbatim: o)
    assert step3_subtitles.step3_subtitles(pushed).ok
    assert "three two" in (pushed.clip_dir / "subs/final.srt").read_text(encoding="utf-8")


def test_the_listening_tool_hands_a_window_s_line_to_the_prompt_and_leaves_other_prompts_as_they_were():
    from assets.manual_lane.remote import listen_windows

    tpl = "引子 {lead} 尾巴 {tail} 字幕「{text}」"
    win = {"win": "GS001", "audio": [9.0, 12.0], "core": [10.0, 11.0], "tempo": 1.0}
    assert listen_windows.prompt_for({**win, "text": "three two"}, tpl) == "引子 1.0 尾巴 1.0 字幕「three two」"
    assert listen_windows.prompt_for(win, tpl) == "引子 1.0 尾巴 1.0 字幕「{text}」"
