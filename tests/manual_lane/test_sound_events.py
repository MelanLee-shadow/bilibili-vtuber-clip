"""Step 3's sound events (sound_events): candidates from the recognitions, the loudness where they wrote nothing and the
chat, one hearing each, who from step 4 and the game-sound check."""

from __future__ import annotations

import json

from fakes import FakeCpa, FakeRemote, make_ctx

from src.autoslice.manual_lane import channel, sound_events, step3_evidence, step3_subtitles
from src.autoslice.manual_lane import subtitle_track as st
from src.autoslice.manual_lane.adapters import RemoteResult
from src.autoslice.manual_lane.srt_text import fmt_ts
from src.autoslice.manual_lane.runner import StepOutcome

HOP = 0.05


def cue(start, end, text, speaker=None):
    return {"start_s": start, "end_s": end, "text": text, "speaker": speaker}


def srt(rows):
    return "\n".join(f"{i}\n{fmt_ts(a)} --> {fmt_ts(b)}\n{t}\n" for i, (a, b, t) in enumerate(rows, 1))


def _levels(duration, loud=()):
    db = [-30.0] * int(duration / HOP)
    for a, b, level in loud:
        db[int(a / HOP):int(b / HOP)] = [level] * (int(b / HOP) - int(a / HOP))
    return {"hop_s": HOP, "frame_s": 0.04, "rate": 16000, "db": db, "f0": [300.0] * len(db)}


def test_laughter_and_cries_are_candidates_fillers_and_words_are_not():
    # lines the recognitions wrote on 11-14 to 11-25 clips
    yes = [("哈哈哈", 1.2), ("呵呵", 1.2), ("嘿嘿嘿嘿哈哈哈哈哈", 3.3), ("给我出啊啊啊啊啊", 5.1), ("这会使命中啊啊啊啊", 3.9),
           ("哇啊啊，你怎么了", 2.2), ("two 啊啊啊啊", 4.2)]
    no = [("嗯", 0.8), ("嗯嗯", 0.8), ("呃呃", 1.0), ("哎呀", 0.8), ("哈哈", 0.4), ("啊", 0.8), ("然后李豆沙也被吓飞了", 1.9),
          ("哈哈，还真是禁止三人以上啊", 6.0)]
    assert [t for t, d in yes if sound_events.nonverbal(t, d)] == [t for t, _ in yes]
    assert [t for t, d in no if sound_events.nonverbal(t, d)] == []
    assert [t for t, d in yes if sound_events.cries(t, d)] == ["给我出啊啊啊啊啊", "这会使命中啊啊啊啊", "哇啊啊，你怎么了", "two 啊啊啊啊"]


def test_candidates_merge_what_several_recognitions_wrote_and_a_chat_burst_alone_is_heard_where_it_was_loudest():
    lines = [("bcut", 111.2, 115.3, "哈哈哈哈啊我啊，我是猪啊"), ("jianying", 111.4, 112.9, "哈哈哈"),
             ("bcut", 20.0, 21.0, "对了"), ("bcut", 33.5, 36.0, "我跟你说对了")]
    chat = [{"t_s": t, "text": x} for t, x in ((112.2, "好吵！"), (112.7, "[李豆沙_大笑]"), (114.0, "笑死"), (114.8, "好吵"),
                                               (40.0, "笑死"), (41.0, "哈哈哈哈"), (43.5, "[礼墨sumi_哈哈哈]"),
                                               (60.0, "好可爱"))]
    levels = sound_events.Levels(_levels(180.0, [(34.0, 35.5, -12.0)]))
    found = sound_events.candidates(lines, chat, 180.0, levels)
    assert [(c["start_s"], c["end_s"], c["found_by"], c["families"], c["cry"], len(c["danmaku"])) for c in found] == [
        (34.0, 35.5, ["弹幕"], [], False, 3),           # the burst at 40 s: the loudest stretch of the 8 s before it
        (111.2, 115.3, ["识别稿"], ["bcut", "jianying"], False, 4)]   # the burst at 112 s is the laughter's own reaction
    assert found[0]["loud_db"] == 18.0 and sound_events.ranked(found)[0]["start_s"] == 111.2


def test_a_loud_jump_nobody_wrote_a_word_for_is_a_candidate_and_one_inside_a_line_is_not():
    # plan B3: both dictations wrote nothing and the sound jumps over the two seconds around it
    levels = sound_events.Levels(_levels(60.0, [(10.0, 10.6, -14.0), (30.0, 30.6, -14.0), (50.0, 50.6, -26.0)]))
    assert [round(t) for t, _ in levels.jumps()] == [10, 30]       # 50 s is only 4 dB up
    lines = [("bcut", 29.0, 31.0, "你在干嘛"), ("mai", 9.0, 11.0, "啊")]   # a witness's 「啊」 is no dictation
    (c,) = sound_events.candidates(lines, [], 60.0, levels)
    assert (c["found_by"], c["cry"], round(c["start_s"]), round(c["end_s"])) == (["响度"], False, 9, 11)
    assert sound_events.points(c) >= 2 and c["evidence"][0].startswith("响度 10.")
    # no dictation at all: nothing says the jump was blank
    assert sound_events.candidates([("mai", 9.0, 11.0, "啊")], [], 60.0, levels) == []


def test_a_cry_is_heard_before_the_clip_s_many_quiet_laughs():
    # 1493180 (11-23): twelve candidates; 「啊啊啊啊啊」 at 67 s had no chat reaction and came 11th by reactions alone
    lines = [("bcut", 10.0 * i, 10.0 * i + 1.5, "哈哈哈") for i in range(1, 10)] + [("bcut", 67.2, 70.4, "啊啊啊啊啊")]
    chat = [{"t_s": 10.0 * i + 1, "text": "哈哈哈"} for i in range(1, 10)]
    order = sound_events.ranked(sound_events.candidates(lines, chat, 120.0, None))
    assert order[0]["start_s"] == 67.2 and order[0]["cry"]


def _ctx(tmp_path, monkeypatch, *, bcut, chat=(), levels_rc=0, said=None, status="OK"):
    def on_script(name, body, remote):
        if name == "s3-sound-levels":
            return RemoteResult(levels_rc, json.dumps(_levels(70.0, [(30.2, 30.8, -12.0)])) + "\n", "")
        return RemoteResult(0, "", "")
    ctx = make_ctx(tmp_path, remote=FakeRemote(on_script=on_script), cpa=FakeCpa([]))
    for d in ("subs", "asr", "danmaku"):
        (ctx.clip_dir / d).mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(
        st.format_blocks([cue(25.0, 29.5, "然后我就看到了"), cue(31.5, 33.0, "吓死我了")], labels=True), encoding="utf-8")
    (ctx.clip_dir / "asr/bcut.srt").write_text(srt(bcut), encoding="utf-8")
    (ctx.clip_dir / "danmaku/lines.json").write_text(json.dumps([{"t_s": t, "text": x} for t, x in chat]), encoding="utf-8")
    asked = []

    def listen(ctx_, label, windows, models, *, prompt, prepared, **_kw):
        asked.append((label, [(w["win"], w["core"], w["audio"]) for w in windows], [m["slug"] for m in models], prompt))
        return [{"win": w["win"], "slug": m["slug"], "model": m["name"], "status": status,
                 "lines": said if said is not None else [{"kind": "尖叫", "sound": "一声拖长的高音尖叫", "source": "person",
                                                          "strength": "强", "start_s": 1.0, "end_s": 1.8}]}
                for w in windows for m in models]
    monkeypatch.setattr(step3_subtitles, "_step3_decided", lambda c: StepOutcome(True))
    monkeypatch.setattr(step3_subtitles, "_chat_product", lambda c, p: None)
    monkeypatch.setattr(step3_evidence, "upload_listen_tools", lambda c, p: None)
    monkeypatch.setattr(step3_evidence, "_listen_once", listen)

    def waits(*_a, **_kw):
        raise AssertionError("a side note does not wait out a rate limit")
    monkeypatch.setattr(step3_evidence, "_listen_pass", waits)
    monkeypatch.setattr(step3_subtitles.writing_canon, "settle_step3", lambda c, o, verbatim: o)
    return ctx, asked


def test_step3_records_a_heard_scream_with_its_peak_frame_and_leaves_the_subtitles_alone(tmp_path, monkeypatch):
    chat = ((31.0, "吓死"), (32.0, "耳朵！"), (33.5, "[李豆沙_被吓到]"))
    ctx, asked = _ctx(tmp_path, monkeypatch, bcut=[(25.0, 29.5, "然后我就看到了"), (29.6, 30.9, "啊啊啊啊")], chat=chat)
    before = (ctx.clip_dir / "subs/final.srt").read_bytes()
    outcome = step3_subtitles.step3_subtitles(ctx)
    assert outcome.ok and (ctx.clip_dir / "subs/final.srt").read_bytes() == before
    # one hearing, by the first level only, of the candidate with a second of audio either side
    assert asked == [("sound-event-se", [("SE001", [29.6, 30.9], [28.6, 31.9])], ["38m"], "listen_sound_event.md")]
    report = json.loads((ctx.clip_dir / sound_events.REPORT).read_text(encoding="utf-8"))
    (e,) = report["events"]
    assert (e["start_s"], e["end_s"], e["peak_s"], e["kind"], e["source"], e["strength"], e["who"], e["found_by"]) == (
        29.6, 30.4, 30.35, "尖叫", "person", "强", None, ["识别稿"])
    assert e["loud_db"] == 18.0 and len(e["danmaku"]) == 3
    assert any(line.startswith("- 声音事件：候选 1 处，复听 1 处，记下 1 个") for line in outcome.process)


def test_a_hearing_of_no_sound_records_nothing_and_a_failed_one_is_named_without_stopping(tmp_path, monkeypatch):
    ctx, _ = _ctx(tmp_path, monkeypatch, bcut=[(29.6, 30.9, "哈哈哈")], said=[{"kind": "无", "sound": ""}])
    assert step3_subtitles.step3_subtitles(ctx).ok
    report = json.loads((ctx.clip_dir / sound_events.REPORT).read_text(encoding="utf-8"))
    assert (report["candidates"], report["heard"], report["events"], report["not_heard"]) == (1, 1, [], [])
    ctx, _ = _ctx(tmp_path / "b", monkeypatch, bcut=[(29.6, 30.9, "哈哈哈")], said=[], levels_rc=1)
    outcome = step3_subtitles.step3_subtitles(ctx)
    report = json.loads((ctx.clip_dir / sound_events.REPORT).read_text(encoding="utf-8"))
    assert outcome.ok and report["events"] == [] and [u["why"] for u in report["not_heard"]] == ["复听没有回答"]
    assert report["levels"].startswith("响度没量到")
    # a rate limit is not waited for: the candidate is left unheard and the step goes on
    ctx, _ = _ctx(tmp_path / "c", monkeypatch, bcut=[(29.6, 30.9, "哈哈哈")], said=[], status="QUOTA")
    assert step3_subtitles.step3_subtitles(ctx).ok
    report = json.loads((ctx.clip_dir / sound_events.REPORT).read_text(encoding="utf-8"))
    assert [u["why"] for u in report["not_heard"]] == ["复听被限流，不等"]


def test_a_clip_without_laughter_or_a_chat_burst_is_not_heard(tmp_path, monkeypatch):
    ctx, asked = _ctx(tmp_path, monkeypatch, bcut=[(25.0, 29.5, "然后我就看到了"), (29.6, 30.4, "嗯")])
    outcome = step3_subtitles.step3_subtitles(ctx)
    assert outcome.ok and asked == []
    assert json.loads((ctx.clip_dir / sound_events.REPORT).read_text(encoding="utf-8"))["events"] == []


def _report(clip_dir, *events):
    rows = [{"start_s": a, "end_s": b, "peak_s": (a + b) / 2, "kind": k, "sound": "声音", "source": src, "strength": s,
             "loud_db": None, "danmaku": [], "evidence": [], "found_by": list(by), "who": None, "who_basis": ""}
            for a, b, k, src, s, *by in events]
    for r in rows:
        r["found_by"] = r["found_by"] or ["识别稿"]
    (clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (clip_dir / sound_events.REPORT).write_text(json.dumps({"events": rows}), encoding="utf-8")


def test_who_is_step_4_s_evidence_and_never_a_guess(tmp_path):
    clip = tmp_path / "c"
    _report(clip, (10.0, 11.0, "笑声", "person", "中"), (20.0, 21.0, "尖叫", "person", "强"),
            (30.0, 31.0, "怪声", "content", "中"), (40.0, 41.0, "笑声", "person", "弱"))
    (clip / "speaker").mkdir()
    (clip / "speaker/arrival.json").write_text(json.dumps({
        "segments": [{"start_ms": 9_800, "end_ms": 11_200, "speaker": "g1"}, {"start_ms": 20_000, "end_ms": 20_500, "speaker": "g0"},
                     {"start_ms": 20_500, "end_ms": 21_000, "speaker": "g1"}],
        "persons": {"g0": "礼墨Sumi", "g1": channel.HOST}}), encoding="utf-8")
    sound_events.attribute(clip)
    got = [(e["who"], e["who_basis"][:5]) for e in sound_events.read(clip)]
    assert got == [(channel.HOST, "到达时间："), ("不确定", "到达时间："), ("画面内容", "复听：游戏"), ("不确定", "到达时间：")]
    # two paths that step 4 named as the same person count together
    (clip / "speaker/arrival.json").write_text(json.dumps({
        "segments": [{"start_ms": 20_000, "end_ms": 20_500, "speaker": "g1"}, {"start_ms": 20_500, "end_ms": 21_000, "speaker": "g2"}],
        "persons": {"g0": "礼墨Sumi", "g1": channel.HOST, "g2": channel.HOST}}), encoding="utf-8")
    sound_events.attribute(clip)
    assert sound_events.read(clip)[1]["who"] == channel.HOST
    # no arrival evidence: the host only when step 4 heard her voice alone
    (clip / "speaker/arrival.json").unlink()
    (clip / "subs/voices.json").write_text(json.dumps({"others": False}), encoding="utf-8")
    (clip / "speaker/speaker.srt").write_text(st.format_blocks([cue(1, 2, "好", channel.HOST_LABEL)], labels=True), encoding="utf-8")
    sound_events.attribute(clip)
    assert [e["who"] for e in sound_events.read(clip)] == [channel.HOST, channel.HOST, "画面内容", channel.HOST]
    (clip / "subs/voices.json").write_text(json.dumps({"others": True}), encoding="utf-8")
    sound_events.attribute(clip)
    assert [e["who"] for e in sound_events.read(clip)] == ["不确定", "不确定", "画面内容", "不确定"]


def _alone(clip):
    (clip / "speaker").mkdir(exist_ok=True)
    (clip / "subs/voices.json").write_text(json.dumps({"others": False}), encoding="utf-8")
    (clip / "speaker/speaker.srt").write_text(st.format_blocks([cue(1, 2, "好", channel.HOST_LABEL)], labels=True), encoding="utf-8")


def test_a_sound_that_may_be_the_game_s_or_a_video_s_is_nobody_s(tmp_path):
    # a solo game clip: the voiceprint hears nobody else, and that does not make every sound hers
    clip = tmp_path / "c"
    _report(clip, (10.0, 11.0, "尖叫", "unsure", "强", "弹幕"), (20.0, 21.0, "尖叫", "person", "强"),
            (40.0, 41.0, "笑声", "person", "中"))
    _alone(clip)
    (clip / "subs/game_sound.json").write_text(json.dumps([
        {"cue": 3, "text": "Three, two.", "line_s": [21.5, 22.6], "levels": {"38m": [], "38m-again": [0, 2]}, "removed": []},
        {"cue": 5, "text": "ok", "line_s": [40.5, 41.0], "levels": {"38m": [], "38m-again": []}, "removed": []}]),
        encoding="utf-8")
    sound_events.attribute(clip)
    got = [(e["who"], e["who_basis"][:3]) for e in sound_events.read(clip)]
    assert got == [("不确定", "复听听"), ("不确定", "第3步"), (channel.HOST, "复听判")]
    # step 4 took the clip for a watched video: a laugh may be the video's people
    (clip / "speaker/decision.json").write_text(json.dumps({"mode": "other_voices_video"}), encoding="utf-8")
    sound_events.attribute(clip)
    assert [e["who"] for e in sound_events.read(clip)] == ["不确定", "不确定", "不确定"]


def test_step4_fills_in_who_once_it_is_done():
    from src.autoslice.manual_lane.stages import STEPS
    step4 = next(s for s in STEPS if s.number == 4)
    assert step4.run.__name__ == "step4_speaker"


def test_the_title_reads_the_people_s_events_by_subtitle_number_and_the_cover_their_peaks_strongest_first(tmp_path):
    clip = tmp_path / "c"
    _report(clip, (5.0, 6.0, "叹气", "person", "强", "弹幕"), (10.0, 11.0, "笑声", "person", "中"),
            (20.0, 21.0, "尖叫", "person", "强"), (30.0, 31.0, "怪声", "content", "强"))
    (clip / "subs/final.srt").write_text(st.format_blocks([cue(9.0, 12.0, "你说什么"), cue(15.0, 18.0, "我不信")], labels=True),
                                         encoding="utf-8")
    assert sound_events.title_lines(clip) == ["第 1 句前（5.0–6.0 秒）不确定：叹气（强；只凭弹幕反应找的位置，可能不是这一声）——声音",
                                              "第 1 句时（10.0–11.0 秒）不确定：笑声（中）——声音",
                                              "第 2 句后（20.0–21.0 秒）不确定：尖叫（强）——声音"]
    # a chat-only event's place is a guess: its frame comes after the ones a recognition or the loudness found
    assert sound_events.cover_times(clip) == [20.5, 10.5, 5.5]
    assert sound_events.read(tmp_path / "none") is None
