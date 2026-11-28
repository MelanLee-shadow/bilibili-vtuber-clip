"""A collab day whose session names nobody but the host: the guests come from the whole session's chat, are kept only
on chat lines that name them, are shared by every clip of the day, and reach the title as named participants.  Beside
that one judgement, what the clips themselves show (the classifier's guest classes, the host naming someone a guest
answers, other uploaders' titles) adds a person two of these routes name, and the day's list grows as clips arrive."""

from __future__ import annotations

import json

from fakes import fake_title_skill
from test_title_history import context

from src.autoslice.manual_lane import collab_guests, step6_title, subtitle_track, title_history
from src.autoslice.manual_lane.runner import StepOutcome

CHAT = (["假面安晚睡的香", "awa好可爱", "安晚awa笑死", "awa在说话", "安晚你别走", "awa又抢石头", "安晚安晚", "awa awa"]
        + ["礼墨好好笑", "礼墨在骂人", "礼墨sumi来了", "礼墨的矿车", "礼墨你行不行", "礼墨又掉坑", "礼墨礼墨", "礼墨卡住了"]
        + ["kmx在吗"] * 20 + ["今天玩什么"] * 30)
ANSWER = {"in_call": [{"name": "安晚awa", "quotes": ["假面安晚睡的香"]}, {"name": "礼墨Sumi", "quotes": ["礼墨在骂人"]},
                      {"name": "露医生", "quotes": ["露医生来了"]}], "not_in_call": []}


def _collab(tmp_path, cid, *, chat=CHAT):
    ctx, cpa = context(tmp_path, cid, '队友提议炸弹取暖')
    ctx.session['participants'] = [['李豆沙', '小李']]        # 11-23: the room title named nobody
    cues = subtitle_track.read_track(ctx.clip_dir)
    cues[1]['speaker'] = step6_title.GUEST_LABEL
    subtitle_track.write_track(ctx.clip_dir, cues, speakers=True)
    (ctx.clip_dir / 'danmaku').mkdir()
    (ctx.clip_dir / 'danmaku/session.json').write_text(json.dumps(
        {'key': 'k', 'rows': [{'t_s': float(i), 'text': t} for i, t in enumerate(chat)]}, ensure_ascii=False))
    return ctx, cpa


def test_guests_the_session_chat_names_in_the_call_are_added_to_the_session(tmp_path):
    asked = []
    ctx, _ = _collab(tmp_path, 'a')
    _voice(ctx, 安晚awa=6, 礼墨Sumi=8)                  # judge + this clip's voice confirms both
    got = collab_guests.name_guests(ctx, lambda call_id, prompt: asked.append(prompt) or ANSWER)
    assert got == ['安晚awa', '礼墨Sumi']                  # 露医生: not a candidate, her quote is in no chat line
    assert [g[0] for g in ctx.session['participants']] == ['李豆沙', '安晚awa', '礼墨Sumi']
    assert '安晚awa（弹幕里 8 条提到）' in asked[0] and 'kmx' not in asked[0]
    second, _ = _collab(tmp_path, 'b')                     # the same day: read from the task directory, not asked again
    assert collab_guests.name_guests(second, lambda *a: asked.append(a) or None) == got and len(asked) == 1


def test_a_quote_that_is_not_a_chat_line_naming_her_keeps_nobody(tmp_path):
    ctx, _ = _collab(tmp_path, 'c')
    _voice(ctx, 安晚awa=6)                                  # voice is present; only the bad quote disqualifies her
    made_up = {"in_call": [{"name": "安晚awa", "quotes": ["安晚在和她连线"]}]}
    assert collab_guests.name_guests(ctx, lambda *a: made_up) == []
    assert [g[0] for g in ctx.session['participants']] == ['李豆沙']


def test_a_session_that_names_its_guests_or_a_clip_with_no_other_voice_asks_nothing(tmp_path):
    ctx, _ = _collab(tmp_path, 'd')
    ctx.session['participants'] = [['李豆沙'], ['雾深Girimi']]
    assert collab_guests.name_guests(ctx, lambda *a: (_ for _ in ()).throw(AssertionError("asked"))) == []
    solo, _ = context(tmp_path, 'e', '她嘴硬了')
    solo.session['participants'] = [['李豆沙']]
    assert collab_guests.name_guests(solo, lambda *a: (_ for _ in ()).throw(AssertionError("asked"))) == []


def test_the_title_of_a_collab_clip_is_written_with_the_guests_the_chat_named(tmp_path, monkeypatch):
    # 11-23: every title came out as 「游戏队友…」 and the pick named "named" (点名) as the research rule it broke
    fake_title_skill(tmp_path, monkeypatch)
    monkeypatch.setattr(title_history, 'load_publication_registry', lambda: {'entries': []})
    ctx, cpa = _collab(tmp_path, 'f')
    _voice(ctx, 安晚awa=6, 礼墨Sumi=8)
    collab_guests.name_guests(ctx, lambda *a: ANSWER)
    assert not isinstance(step6_title.pick_title(ctx), StepOutcome)
    prompt = cpa.title_prompts[0]
    assert '本场连线的安晚awa、礼墨Sumi中认不出是哪位的一句' in prompt and '[未具名嘉宾]' not in prompt


def test_answer_shapes_that_are_not_a_list_of_guests_keep_nobody():
    named = {"安晚awa": ["假面安晚睡的香"]}
    for bad in (None, [], {"in_call": "安晚awa"}, {"in_call": [{"name": "安晚awa"}]}, {"in_call": [{"quotes": ["x"]}]}):
        assert collab_guests.kept(bad, named) == []


def test_a_failed_guest_call_fails_the_step_instead_of_titling_the_collab_unnamed(tmp_path):
    # 11-23: nine clips asked at once, the gateway refused the copies in flight, and each went on with 「队友」
    import pytest
    ctx, _ = _collab(tmp_path, 'g')
    _voice(ctx, 安晚awa=6, 礼墨Sumi=8)
    with pytest.raises(collab_guests.GuestsUnknown):
        collab_guests.name_guests(ctx, lambda *a: None)
    assert not (tmp_path / collab_guests.CACHE.format(date=ctx.session['recording_date'])).exists()
    asked = []
    assert collab_guests.name_guests(ctx, lambda *a: asked.append(1) or ANSWER) == ['安晚awa', '礼墨Sumi'] and asked == [1]


def test_clips_of_one_day_asking_together_send_one_call(tmp_path):
    import threading
    import time
    calls = []

    def ask(*a):
        calls.append(1)
        time.sleep(0.2)
        return ANSWER
    ctxs = [_collab(tmp_path, f'p{i}')[0] for i in range(3)]
    for c in ctxs:
        _voice(c, 安晚awa=6, 礼墨Sumi=8)
    got = []
    threads = [threading.Thread(target=lambda c=c: got.append(collab_guests.name_guests(c, ask))) for c in ctxs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1 and got == [['安晚awa', '礼墨Sumi']] * 3


# the channel's request — the 11-23 dynamics as posted
DYNAMICS = {
    step6_title.HOST: [{"published_at": "2026-11-22T13:22:00+00:00", "text": "大家——不好意思刚干完活回去会太晚惹,今晚就早点休息好不好我们明天晚上八点的女大矿工见噢...!"},
                       {"published_at": "2026-11-17T15:13:00+00:00", "text": "晚安小熊——附赠一张照片"}],
    "安晚awa": [{"published_at": "2026-11-23T15:33:00+00:00", "text": "晚安矿工是对的但我要晕倒了!(什么我没发出来这个动态)"},
              {"published_at": "2026-11-18T15:32:00+00:00", "text": "晚安为什么...下播...如此困难..."}],
    "礼墨Sumi": [{"published_at": "2026-11-23T08:50:00+00:00", "text": "请宿主做好准备！"},
               {"published_at": "2026-11-13T16:19:00+00:00", "text": "晚安哦晚安哦！"}],
    "雾深Girimi": [{"published_at": "2026-11-23T14:00:00+00:00", "text": "晚安大家今天也辛苦了"},
                 {"published_at": "2026-11-10T14:00:00+00:00", "text": "晚安大家"}],
}
PEERS = {"安晚awa": 1, "礼墨Sumi": 2, "雾深Girimi": 3}


def _day_of_dynamics(tmp_path, cid, monkeypatch, *, chat, dynamics=DYNAMICS, unread=()):
    monkeypatch.setattr(collab_guests, "accounts", lambda: dict(PEERS))
    monkeypatch.setattr(collab_guests, "read_dynamics", lambda uids: ({n: v for n, v in dynamics.items()
                                                                       if n == step6_title.HOST or n in uids}, list(unread)))
    ctx, _ = _collab(tmp_path, cid, chat=chat)
    ctx.session['recording_date'] = '2026-11-23'
    return ctx


def test_the_event_the_host_announced_and_a_peer_posted_about_links_her_without_the_chat():
    assert collab_guests.linked(DYNAMICS, '2026-11-23') == {"安晚awa": ["矿工"]}    # 晚安: both always write it
    named = {**DYNAMICS, "礼墨Sumi": [{"published_at": "2026-11-23T08:50:00+00:00", "text": "今晚和李豆沙一起挖矿"}]}
    assert "李豆沙" in collab_guests.linked(named, "2026-11-23")["礼墨Sumi"]       # or she names the host
    assert collab_guests.linked(DYNAMICS, '2026-12-05') == {}                         # another day: nothing around it


RECORDER_HEADER = ('\ufeff<?xml version="1.0" encoding="utf-8"?>\n<i>\n  <BililiveRecorder version="2.18.0" />\n'
                   '  <BililiveRecorderRecordInfo roomid="22966160" shortid="0" name="李豆沙_Channel" '
                   'title="？！女大矿工？！" areanameparent="虚拟主播" start_time="2026-11-23T20:00:46+08:00" />\n</i>\n')


def test_the_live_room_title_is_the_hosts_word_about_the_day(tmp_path, monkeypatch):
    # 11-23: the room title 「？！女大矿工？！」 named the event 安晚awa posted about, with no dynamic of the host's needed
    silent = {**DYNAMICS, step6_title.HOST: [d for d in DYNAMICS[step6_title.HOST] if "矿工" not in d["text"]]}
    assert collab_guests.linked(silent, "2026-11-23") == {}
    assert collab_guests.linked(silent, "2026-11-23", "？！女大矿工？！") == {"安晚awa": ["矿工"]}
    asked = []
    ctx = _day_of_dynamics(tmp_path, 'rt1', monkeypatch, chat=["awa在吗"] + ["今天玩什么"] * 30, dynamics=silent)
    assert collab_guests.room_title(ctx.clip_dir) == ""                       # no chat file: nothing read
    (ctx.clip_dir / "danmaku/recording.xml").write_text(RECORDER_HEADER, encoding="utf-8")
    assert collab_guests.room_title(ctx.clip_dir) == "？！女大矿工？！"
    answer = {"in_call": [{"name": "安晚awa", "quotes": ["矿工是对的但我要晕倒了"]}], "not_in_call": []}
    assert collab_guests.name_guests(ctx, lambda _id, prompt: asked.append(prompt) or answer) == ["安晚awa"]
    assert "这场的直播间标题：？！女大矿工？！" in asked[0] and "两人以前都没写过的词：矿工" in asked[0]
    record = json.loads((tmp_path / collab_guests.CACHE.format(date='2026-11-23')).read_text(encoding="utf-8"))
    assert record["dynamics"]["room_title"] == "？！女大矿工？！" and record["guests"] == ["安晚awa"]


def test_a_guest_the_chat_hardly_names_is_asked_about_from_her_dynamics_and_kept_on_them(tmp_path, monkeypatch):
    asked = []
    ctx = _day_of_dynamics(tmp_path, 'dy1', monkeypatch, chat=["awa在吗"] + ["今天玩什么"] * 30)
    answer = {"in_call": [{"name": "安晚awa", "quotes": ["矿工是对的但我要晕倒了"]}], "not_in_call": []}
    assert collab_guests.name_guests(ctx, lambda _id, prompt: asked.append(prompt) or answer) == ["安晚awa"]
    prompt = asked[0]
    assert "明天晚上八点的女大矿工见" in prompt and "### 安晚awa（弹幕里 1 条提到）" in prompt
    assert "两人以前都没写过的词：矿工" in prompt and "### 雾深Girimi（弹幕里 0 条提到）" in prompt
    assert ctx.session['guest_evidence']['by'] == {"安晚awa": ["chat", "linked"]}    # dynamics tie confirms judge
    record = json.loads((tmp_path / collab_guests.CACHE.format(date='2026-11-23')).read_text(encoding="utf-8"))
    assert record["dynamics"]["linked"] == {"安晚awa": ["矿工"]} and record["guests"] == ["安晚awa"]


def test_a_quote_from_a_dynamic_must_stand_in_her_own_post_and_say_something(tmp_path, monkeypatch):
    # too short, the host's post not hers, not in her post, her daily post with nothing tying it to the host
    for i, (name, quotes) in enumerate((("安晚awa", ["晚安"]), ("安晚awa", ["明天晚上八点的女大矿工见"]),
                                        ("安晚awa", ["矿工是对的但我不晕"]), ("雾深Girimi", ["晚安大家今天也辛苦了"]))):
        ctx = _day_of_dynamics(tmp_path / str(i), f'dq{i}', monkeypatch, chat=["今天玩什么"] * 30)
        answer = {"in_call": [{"name": name, "quotes": quotes}]}
        assert collab_guests.name_guests(ctx, lambda *a, ans=answer: ans) == [], quotes


def test_chat_candidates_get_their_dynamics_beside_and_an_unread_feed_is_recorded(tmp_path, monkeypatch):
    asked = []
    ctx = _day_of_dynamics(tmp_path, 'dy2', monkeypatch, chat=CHAT, unread=["礼墨Sumi: CrawlError: risk control"])
    collab_guests.name_guests(ctx, lambda _id, prompt: asked.append(prompt) or ANSWER)
    assert "请宿主做好准备！" in asked[0] and "### 礼墨Sumi（弹幕里 8 条提到）" in asked[0]
    record = json.loads((tmp_path / collab_guests.CACHE.format(date='2026-11-23')).read_text(encoding="utf-8"))
    # 安晚awa is confirmed by her dynamics tie (矿工); 礼墨Sumi has chat talk only so she stays a candidate
    assert record["dynamics"]["unread"] == ["礼墨Sumi: CrawlError: risk control"]
    assert record["guests"] == ["安晚awa"] and record["chat_guests"] == ["安晚awa", "礼墨Sumi"]


def test_the_feed_reader_takes_the_time_from_the_dynamic_id_and_leaves_picture_posts_out(monkeypatch):
    import pytest

    from src.autoslice.manual_lane import dynamics_feed

    rows = [{"opus_id": "1273928639090524164", "content": "晚安\n矿工是对的但我要晕倒了!"}, {"opus_id": "1273824645026414617", "content": ""},
            {"opus_id": "x", "content": "坏行"}]
    monkeypatch.setattr(dynamics_feed, "_get", lambda url, referer="": {"code": 0, "data": {"items": rows}})
    assert dynamics_feed.recent(1) == [{"dynamic_id": "1273928639090524164", "published_at": "2026-11-23T15:33:45+00:00",
                                        "text": "晚安矿工是对的但我要晕倒了!"}]
    monkeypatch.setattr(dynamics_feed, "_get", lambda url, referer="": {"code": 0, "data": {"items": []}})
    with pytest.raises(dynamics_feed.FeedError):          # an empty first page is Bilibili refusing, not "no posts"
        dynamics_feed.recent(1)


# ----- 安晚awa sat with 礼墨Sumi and spoke through her stream; the judge answered 礼墨Sumi alone -------------

H, G = step6_title.HOST, step6_title.GUEST_LABEL
JUDGE_LIMO = {"in_call": [{"name": "礼墨Sumi", "quotes": ["礼墨在骂人"]}], "not_in_call": ["安晚awa"]}
PEER_TITLE = "玩的有点大的女大组三人【礼墨&安晚&李豆沙】"


def _voice(ctx, **chunks):
    """The clip's chunk run: `chunks` confident chunks per guest class, five of the host's, two the classifier is unsure of."""
    rows = [(H, 0.9)] * 5 + [(name, 0.9) for name, n in chunks.items() for _ in range(n)] + [("安晚awa", 0.6)] * 2
    units = [{"id": f"u{i}", "s": i * 1200, "e": (i + 1) * 1200} for i in range(len(rows))]
    preds = [{"id": f"u{i}", "ok": True, "probs": {c: (p if c == top else round((1 - p) / 2, 3)) for c in (H, "礼墨Sumi", "安晚awa")}}
             for i, (top, p) in enumerate(rows)]
    (ctx.clip_dir / "speaker").mkdir(exist_ok=True)
    (ctx.clip_dir / collab_guests.VOICE_UNITS).write_text(json.dumps({"units": units}), encoding="utf-8")
    (ctx.clip_dir / collab_guests.VOICE_PREDS).write_text(json.dumps({"predictions": preds}, ensure_ascii=False), encoding="utf-8")


def _peers(ctx, *videos):
    (ctx.clip_dir / "dedup").mkdir(exist_ok=True)
    (ctx.clip_dir / collab_guests.PEER_VIDEOS).write_text(json.dumps({"videos": [
        {"bvid": f"BV{i}", "title": title, "same_session_confirmed": confirmed} for i, (title, confirmed) in enumerate(videos)]},
        ensure_ascii=False), encoding="utf-8")


def _lines(ctx, *rows):
    subtitle_track.write_track(ctx.clip_dir, [{"start_s": float(i), "end_s": i + 0.9, "text": text, "speaker": who}
                                              for i, (who, text) in enumerate(rows)], speakers=True)


def test_a_word_said_twice_does_not_name_the_person_at_its_seam():
    # 11-24: four of the eleven chat lines shown to the judge as naming 安晚awa were 「晚安晚安」
    known = {"安晚awa": ["安晚", "awa", "安晚awa"]}
    chat = ["晚安晚安", "大家晚安晚安啦", "晚安晚安晚安", "怎么是awa先和lmsm线下联动播呢", "晚安安晚", "安晚安晚", "今晚安晚来吗"]
    assert collab_guests.mentions(chat, known)["安晚awa"] == chat[3:]
    assert collab_guests.candidates(["晚安晚安"] * 9, known) == {}


def test_what_one_clip_shows_of_who_is_in_the_call(tmp_path):
    known = {"安晚awa": ["安晚", "awawa"], "礼墨Sumi": ["礼墨"], "南町Nightin": ["南町"]}
    ctx, _ = _collab(tmp_path, 'ev')
    _voice(ctx, 安晚awa=6, 礼墨Sumi=3)
    assert collab_guests.voice_heard(ctx.clip_dir, 0.8) == {"礼墨Sumi": 3, "安晚awa": 6}      # the two unsure chunks: nobody's
    _lines(ctx, (H, "安晚也要来"), (G, "那你要告诉我你什么时候来啊"), (H, "南町昨天说她不来"), (H, "对吧"), (H, "嗯"),
           (H, "awawa请说"), (H, "快说"), (G, "有吗"), (G, "礼墨你说"), (H, "好"))
    # 南町: the host talks on and no guest answers within two lines; 礼墨: a guest said the name, not the host
    assert collab_guests.answered(subtitle_track.read_track(ctx.clip_dir), known, 2) == {"安晚awa": 2}
    _peers(ctx, (PEER_TITLE, True), ("【礼墨×李豆沙×安晚】今晚联动怎么有南町nightin啊", True),
           ("【晚礼沙】不可以在和李豆沙说话的时候和礼小虎玩", False), ("【李豆沙】南町为什么不找我吃饭", True),
           ("【李豆沙&南町】没写日期的另一场", False))
    assert collab_guests.peer_titles(ctx.clip_dir, known) == {
        "BV0": {"title": PEER_TITLE, "names": ["安晚awa", "礼墨Sumi"]},
        "BV1": {"title": "【礼墨×李豆沙×安晚】今晚联动怎么有南町nightin啊", "names": ["安晚awa", "礼墨Sumi"]}}


def test_a_watched_videos_voices_and_lines_are_nobody_in_the_call(tmp_path):
    # the host watching 礼墨Sumi's own video hears her voice and says her name while "a guest" speaks: not a call
    ctx, _ = _collab(tmp_path, 'wv')
    _voice(ctx, 礼墨Sumi=9)
    _lines(ctx, (H, "礼墨这里好好笑"), (G, "大家好我是礼墨"))
    _peers(ctx, (PEER_TITLE, True))
    known = {"礼墨Sumi": ["礼墨"], "安晚awa": ["安晚"]}
    assert collab_guests.clip_evidence(ctx, known)["voice"]["礼墨Sumi"] == 9
    (ctx.clip_dir / "speaker/decision.json").write_text(json.dumps({"mode": "other_voices_video"}), encoding="utf-8")
    assert collab_guests.clip_evidence(ctx, known) == {
        "voice": {"礼墨Sumi": 0, "安晚awa": 0}, "subtitles": {}, "paths": [],
        "peers": {"BV0": {"title": PEER_TITLE, "names": ["安晚awa", "礼墨Sumi"]}}}


def _ev(voice=None, answered=None, peers=None, paths=()):
    return {"voice": {"礼墨Sumi": 0, "安晚awa": 0, **(voice or {})}, "subtitles": answered or {},
            "peers": {b: {"title": "t", "names": n} for b, n in (peers or {}).items()}, "paths": list(paths)}


def test_a_person_two_routes_name_joins_the_days_guests_and_one_route_names_nobody():
    heard = _ev(voice={"安晚awa": 9, "礼墨Sumi": 12})
    # the classifier's classes alone: a watched video, the host singing (11-16: 35 chunks of 礼墨Sumi's class in one clip)
    assert collab_guests.decide({"a": heard, "b": heard}, [], [])[1] == []
    # the host named her and a guest answered, alone: someone talked about is answered too
    talked = _ev(answered={"南町Nightin": 4})
    assert collab_guests.decide({"a": talked, "b": talked}, [], [])[1] == []
    # chat talk alone names nobody; one clip that hears 安晚awa is not two clips for an unnamed guest, while one clip's
    # strong voice confirms the judge-named 礼墨Sumi; a second clip makes voice a route for 安晚awa beside peers
    unconfirmed = {"a": _ev(voice={"安晚awa": 9}, peers={"BV1": ["安晚awa"]})}
    assert collab_guests.decide(unconfirmed, ["礼墨Sumi"], ["礼墨Sumi"])[1] == []
    one = {"a": _ev(voice={"安晚awa": 9, "礼墨Sumi": 12}, peers={"BV1": ["安晚awa"]})}
    assert collab_guests.decide(one, ["礼墨Sumi"], ["礼墨Sumi"])[1] == ["礼墨Sumi"]
    routes, guests, shared = collab_guests.decide({**one, "b": _ev(voice={"安晚awa": 4})}, ["礼墨Sumi"], ["礼墨Sumi"])
    assert guests == ["礼墨Sumi", "安晚awa"] and shared == []
    assert routes["安晚awa"]["routes"] == ["voice", "peers"] and not routes["安晚awa"]["chat"] and routes["礼墨Sumi"]["chat"]
    # three chunks in a clip is what a clip of the host alone shows (11-15: 3): not heard
    assert collab_guests.decide({"a": _ev(voice={"安晚awa": 3}, peers={"BV1": ["安晚awa"]}), "b": _ev(voice={"安晚awa": 3})}, [], [])[1] == []
    # unconfirmed guests come off on recompute; a confirmed guest stays
    assert collab_guests.decide({}, [], ["礼墨Sumi", "安晚awa"])[1] == []
    assert collab_guests.decide({"a": _ev(voice={"礼墨Sumi": 8})}, ["礼墨Sumi"], ["礼墨Sumi", "安晚awa"])[1] == ["礼墨Sumi"]


def test_the_classifiers_classes_do_not_count_on_a_day_with_a_guest_it_does_not_know():
    # its classes are a closed set: with 雾深Girimi confirmed in the call (dynamics tie) her voice is counted as one of them
    clips = {c: _ev(voice={"安晚awa": 9}, answered={"安晚awa": 2}) for c in "ab"}
    assert collab_guests.decide(clips, [], [])[1] == ["安晚awa"]
    assert collab_guests.decide(clips, ["雾深Girimi"], [], links={"雾深Girimi": ["联动"]})[1] == ["雾深Girimi"]


def test_two_guests_on_one_sound_path_in_two_clips_share_it():
    # 11-24, 礼墨Sumi's path: 20 and 22 confident chunks; 11-23, each her own path: 61 and 5 (7.6 %), 5 and 2
    together = _ev(voice={"礼墨Sumi": 20, "安晚awa": 22}, paths=[{"礼墨Sumi": 20, "安晚awa": 22}])
    apart = _ev(voice={"礼墨Sumi": 61, "安晚awa": 5}, paths=[{"礼墨Sumi": 61, "安晚awa": 5}, {"礼墨Sumi": 2, "安晚awa": 5}])
    guests = ["礼墨Sumi", "安晚awa"]
    assert collab_guests.decide({"a": together, "b": _ev(voice={"礼墨Sumi": 11, "安晚awa": 4}, paths=[{"礼墨Sumi": 11, "安晚awa": 3}])}, guests, guests)[2] == [guests]
    assert collab_guests.decide({"a": together, "b": apart}, guests, guests)[2] == []          # one clip: not yet
    assert collab_guests.decide({"a": apart, "b": apart}, guests, guests)[2] == []
    assert collab_guests.decide({"a": together, "b": together}, ["礼墨Sumi"], ["礼墨Sumi"])[2] == []   # she is not on the list


def _arrival(ctx, runs, named):
    (ctx.clip_dir / "speaker/arrival.json").write_text(json.dumps({
        "segments": [{"start_ms": a, "end_ms": b, "speaker": g} for a, b, g in runs],
        "named": {g: {"speaker": who} for g, who in named.items()}, "persons": {}, "owner": "礼墨Sumi", "bvid": "BV",
        "centres_ms": [0.0, 500.0]}, ensure_ascii=False), encoding="utf-8")


def test_the_guest_classes_on_each_sound_path_of_the_arrival_evidence(tmp_path):
    ctx, _ = _collab(tmp_path, 'pv')
    _voice(ctx, 礼墨Sumi=4, 安晚awa=3)            # chunks: 0-4 the host's, 5-8 礼墨Sumi's, 9-11 安晚awa's, 12-13 unsure
    assert collab_guests.path_voices(ctx.clip_dir, 0.8) == []                 # no arrival evidence: no paths
    _arrival(ctx, [(0, 6000, "g1"), (6000, 14400, "g0")], {"g0": G, "g1": H})   # the guests' path holds both of them
    assert collab_guests.path_voices(ctx.clip_dir, 0.8) == [{"礼墨Sumi": 4, "安晚awa": 3}]
    _arrival(ctx, [(0, 6000, "g2"), (6000, 10800, "g0"), (10800, 14400, "g1")], {"g0": G, "g1": G, "g2": H})
    assert collab_guests.path_voices(ctx.clip_dir, 0.8) == [{"礼墨Sumi": 4, "安晚awa": 0}, {"礼墨Sumi": 0, "安晚awa": 3}]


def test_the_days_list_grows_with_the_clips_and_a_clip_named_before_is_behind(tmp_path):
    first, _ = _collab(tmp_path, 'a')
    _voice(first, 礼墨Sumi=8)
    assert collab_guests.name_guests(first, lambda *a: JUDGE_LIMO) == ["礼墨Sumi"]          # as on 11-24
    second, _ = _collab(tmp_path, 'b')
    _voice(second, 安晚awa=6, 礼墨Sumi=9)
    _peers(second, (PEER_TITLE, True))
    never = lambda *a: (_ for _ in ()).throw(AssertionError("the judge is asked once a day"))
    assert collab_guests.name_guests(second, never) == ["礼墨Sumi"]         # her voice in one clip and a title: not yet two routes
    third, _ = _collab(tmp_path, 'c')
    _voice(third, 安晚awa=5)
    assert collab_guests.name_guests(third, never) == ["礼墨Sumi", "安晚awa"]
    assert [g[0] for g in third.session['participants']] == ['李豆沙', '礼墨Sumi', '安晚awa']
    assert third.session['guest_evidence']['by'] == {"礼墨Sumi": ["chat", "voice", "peers"], "安晚awa": ["voice", "peers"]}
    record = json.loads((tmp_path / collab_guests.CACHE.format(date=first.session['recording_date'])).read_text(encoding="utf-8"))
    assert record["chat_guests"] == ["礼墨Sumi"] and record["guests"] == ["礼墨Sumi", "安晚awa"]
    assert [(a["clip"], a["guests"], a["by"], a["judge_said_not"]) for a in record["added"]] == [
        ("c", ["安晚awa"], {"安晚awa": ["voice", "peers"]}, ["安晚awa"])]        # the judge had said she was not: kept, not dropped
    # the two clips named from the shorter list
    assert collab_guests.behind(first) == ["安晚awa"] == collab_guests.behind(second) and collab_guests.behind(third) == []
    assert collab_guests.name_guests(second, never) == ["安晚awa"]           # at its next step it takes her
    assert collab_guests.catch_up(first) == ["安晚awa"] and collab_guests.behind(first) == []
    assert "安晚awa（声纹分类＋同场他人切片标题）" in collab_guests.process_line(first, ["安晚awa"])


def test_a_replaced_clip_no_longer_counts_and_a_clip_that_went_online_without_her_is_listed(tmp_path):
    first, _ = _collab(tmp_path, 'a')
    _voice(first, 礼墨Sumi=8)
    collab_guests.name_guests(first, lambda *a: JUDGE_LIMO)
    first.state.record("upload", ok=True, reason="", products={}, cpa=[], note="online")
    old, _ = _collab(tmp_path, 'b')
    _voice(old, 安晚awa=6)
    _peers(old, (PEER_TITLE, True))
    collab_guests.name_guests(old, lambda *a: None)
    old.state.data["clip"]["status"] = "SUPERSEDED"                    # revised: b_r2 is the same minutes again
    old.state.save()
    again, _ = _collab(tmp_path, 'b_r2')
    _voice(again, 安晚awa=6)
    assert collab_guests.name_guests(again, lambda *a: None) == ["礼墨Sumi"]      # one clip heard her, not two
    other, _ = _collab(tmp_path, 'c')
    _voice(other, 安晚awa=4)
    _peers(other, (PEER_TITLE, True))
    assert collab_guests.name_guests(other, lambda *a: None) == ["礼墨Sumi", "安晚awa"]
    record = json.loads((tmp_path / collab_guests.CACHE.format(date=first.session['recording_date'])).read_text(encoding="utf-8"))
    assert record["added"][0]["published_without"] == ["a"]


def test_a_session_named_from_the_days_record_asks_again_and_one_written_by_hand_does_not(tmp_path):
    ctx, _ = _collab(tmp_path, 'n')
    _voice(ctx, 礼墨Sumi=8)
    assert collab_guests.needed(ctx) and not collab_guests.needed(ctx, heard=False) and collab_guests.needed(ctx, heard=True)
    collab_guests.name_guests(ctx, lambda *a: JUDGE_LIMO)
    assert collab_guests.needed(ctx)                                      # named from the record: it may hold more by now
    ctx.session.pop('guest_evidence')
    assert not collab_guests.needed(ctx) and collab_guests.behind(ctx) == []     # participants.json


def test_a_day_record_kept_before_the_routes_is_read_as_the_judges(tmp_path):
    # legacy {"guests": [...]} with no confirming evidence shrinks to [] while keeping chat_guests;
    # once a clip adds confirming voice, behind/catch_up confirm both without re-asking CPA
    ctx, _ = _collab(tmp_path, 'o')
    cache = tmp_path / collab_guests.CACHE.format(date=ctx.session['recording_date'])
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"date": "d", "guests": ["安晚awa", "礼墨Sumi"]}, ensure_ascii=False), encoding="utf-8")
    assert collab_guests.behind(ctx) == []                           # it never asked the record
    ctx.session["guest_evidence"] = {"guests": []}
    assert collab_guests.behind(ctx) == []                           # no confirming route yet: recomputed to []
    assert json.loads(cache.read_text(encoding="utf-8"))["chat_guests"] == ["安晚awa", "礼墨Sumi"]
    _voice(ctx, 安晚awa=6, 礼墨Sumi=8)
    assert collab_guests.behind(ctx) == ["安晚awa", "礼墨Sumi"]
    assert collab_guests.catch_up(ctx) == ["安晚awa", "礼墨Sumi"] and ctx.session["guest_evidence"]["by"]["礼墨Sumi"] == ["chat", "voice"]
    fresh, _ = _collab(tmp_path, 'q')
    assert collab_guests.name_guests(fresh, lambda *a: (_ for _ in ()).throw(AssertionError("asked"))) == ["安晚awa", "礼墨Sumi"]
    assert json.loads(cache.read_text(encoding="utf-8"))["chat_guests"] == ["安晚awa", "礼墨Sumi"]


def test_step_6_goes_back_to_the_speakers_when_the_session_gains_a_guest(tmp_path):
    # step 4 told the host from "the others" as two people; the day's record now names two confirmed guests: three people
    ctx, _ = _collab(tmp_path, 'r')
    cache = tmp_path / collab_guests.CACHE.format(date=ctx.session['recording_date'])
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"date": "d", "chat_guests": ["礼墨Sumi", "安晚awa"],
                                 "guests": ["礼墨Sumi", "安晚awa"],
                                 "dynamics": {"linked": {"礼墨Sumi": ["矿工"], "安晚awa": ["矿工"]}}}, ensure_ascii=False),
                     encoding="utf-8")
    (ctx.clip_dir / "speaker/decision.json").write_text(json.dumps({"mode": "guests_heard_v8"}), encoding="utf-8")
    out = step6_title.step6_title_cover(ctx)
    assert out.ok and out.rewind == ("speaker", "burn", "title_cover") and "礼墨Sumi、安晚awa" in out.process[0]
    assert [g[0] for g in ctx.session['participants']] == ['李豆沙', '礼墨Sumi', '安晚awa']
    for mode, decided in (("linked_v8_clusters", True), ("import", False), ("other_voices_video", False)):
        (ctx.clip_dir / "speaker/decision.json").write_text(json.dumps({"mode": mode}), encoding="utf-8")
        assert step6_title._voice_decided(ctx) is decided


def test_a_day_made_before_she_was_recognised_catches_up_when_its_clips_are_run_on(tmp_path):
    # 11-24 as it stands on the media host: every clip done, each session 【礼墨Sumi】, the record the judge's (with tie) alone
    clips = []
    for cid, voice in (("a", 6), ("b", 5), ("c", 0)):
        ctx, _ = _collab(tmp_path, cid)
        _voice(ctx, 礼墨Sumi=8)
        collab_guests.name_guests(ctx, lambda *a: JUDGE_LIMO)
        clips.append(ctx)
        cache = tmp_path / collab_guests.CACHE.format(date=ctx.session['recording_date'])
        cache.write_text(json.dumps({"date": "d", "chat_guests": ["礼墨Sumi"], "guests": ["礼墨Sumi"],
                                     "dynamics": {"linked": {"礼墨Sumi": ["矿工"]}}, "answer": JUDGE_LIMO},
                                    ensure_ascii=False), encoding="utf-8")
        _voice(ctx, 安晚awa=voice, 礼墨Sumi=8)          # the products step 3 and step 0 left in the clip directory
        _peers(ctx, (PEER_TITLE, True))
    first, second, third = clips
    assert collab_guests.behind(first) == []                         # one clip heard her: not yet
    assert collab_guests.behind(second) == ["安晚awa"] == collab_guests.behind(third) == collab_guests.behind(first)
    (first.clip_dir / "subs/final.srt").unlink()                     # redone from the subtitles: nothing to add, still behind
    (first.clip_dir / "speaker/speaker.srt").unlink()
    assert collab_guests.behind(first) == ["安晚awa"]


def test_a_guest_whose_evidence_is_gone_comes_off_the_list_and_stale_sessions_catch_up(tmp_path):
    """Unconfirmed guests whose evidence is gone come off the list cleanly on recompute (no KeyError), and a clip
    whose session still holds them is reported behind and rewritten by catch_up."""
    routes, guests, _ = collab_guests.decide({"c": _ev()}, [], ["雾深Girimi"])
    assert guests == [] and routes == {}
    ctx, _ = _collab(tmp_path, 'c')
    ctx.session["participants"] = [["李豆沙"], ["礼墨Sumi"], ["雾深Girimi"]]
    ctx.session["guest_evidence"] = {"source": "collab_guests", "record": "r",
                                     "guests": ["礼墨Sumi", "雾深Girimi"], "by": {"礼墨Sumi": ["chat"], "雾深Girimi": []}}
    ctx.state.save()
    cache = tmp_path / collab_guests.CACHE.format(date=ctx.session['recording_date'])
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"date": "d", "chat_guests": ["礼墨Sumi"], "guests": ["礼墨Sumi", "雾深Girimi"],
                                 "dynamics": {"linked": {"礼墨Sumi": ["矿工"]}}, "clips": {}},
                                ensure_ascii=False), encoding="utf-8")
    never = lambda *a: (_ for _ in ()).throw(AssertionError("the judge is asked once a day"))
    assert collab_guests.lacking(ctx) == ["雾深Girimi"]
    assert collab_guests.name_guests(ctx, never) == ["雾深Girimi"]
    assert [g[0] for g in ctx.session["participants"]] == ["李豆沙", "礼墨Sumi"]
    assert ctx.session["guest_evidence"]["by"] == {"礼墨Sumi": ["chat", "linked"]}
    assert "移出未证实候选 雾深Girimi" in collab_guests.process_line(ctx, ["雾深Girimi"])
    assert collab_guests.catch_up(ctx) == [] and collab_guests.lacking(ctx) == []


def test_chat_talk_alone_keeps_candidate_in_record_without_adding_guest(tmp_path, monkeypatch):
    # solo stream shape: chat judge names 安晚awa and 礼墨Sumi from chat talk, no dynamics tie,
    # 2 clips have only 1 and 0 voice chunks, no subtitles, no peers -> guests stays [], candidates stay in record
    monkeypatch.setattr(collab_guests, "accounts", lambda: {})
    monkeypatch.setattr(collab_guests, "read_dynamics", lambda _uids: ({}, []))
    first, _ = _collab(tmp_path, "s1")
    _voice(first, 安晚awa=1, 礼墨Sumi=1)
    assert collab_guests.name_guests(first, lambda *a: ANSWER, heard=True) == []
    assert [g[0] for g in first.session["participants"]] == ["李豆沙"]
    assert first.session["guest_evidence"]["guests"] == []
    assert first.session["guest_evidence"]["by"] == {}
    second, _ = _collab(tmp_path, "s2")
    _voice(second)
    never = lambda *a: (_ for _ in ()).throw(AssertionError("asked"))
    assert collab_guests.name_guests(second, never) == []
    assert [g[0] for g in second.session["participants"]] == ["李豆沙"]
    record = json.loads((tmp_path / collab_guests.CACHE.format(date=first.session["recording_date"])).read_text(encoding="utf-8"))
    assert record["guests"] == []
    assert record["chat_guests"] == ["安晚awa", "礼墨Sumi"]
    assert record["routes"]["安晚awa"]["chat"] is True and record["routes"]["安晚awa"]["routes"] == []
    assert record["routes"]["礼墨Sumi"]["chat"] is True and record["routes"]["礼墨Sumi"]["routes"] == []


def test_judge_named_one_and_routes_named_other_both_confirmed(tmp_path):
    # shape: judge names 礼墨Sumi only; clips bring voice + peers + subtitles for both -> both confirmed;
    # dynamics.linked alone without judge does NOT count toward routes_needed for an unnamed person
    clips = {
        "c1": _ev(voice={"礼墨Sumi": 23, "安晚awa": 7}, peers={"BV0": ["安晚awa", "礼墨Sumi"]}),
        "c2": _ev(voice={"礼墨Sumi": 28, "安晚awa": 19}, answered={"安晚awa": 6, "礼墨Sumi": 5}, peers={"BV0": ["安晚awa", "礼墨Sumi"]}),
    }
    routes, guests, _ = collab_guests.decide(clips, ["礼墨Sumi"], [], links={"奈奈莉娅": ["矿工"]})
    assert guests == ["礼墨Sumi", "安晚awa"]
    assert routes["礼墨Sumi"]["chat"] is True and routes["安晚awa"]["chat"] is False
    assert "奈奈莉娅" not in guests
    # also via name_guests end-to-end
    c1, _ = _collab(tmp_path, "c1")
    _voice(c1, 礼墨Sumi=23, 安晚awa=7)
    _peers(c1, (PEER_TITLE, True))
    assert collab_guests.name_guests(c1, lambda *a: JUDGE_LIMO) == ["礼墨Sumi"]
    c2, _ = _collab(tmp_path, "c2")
    _voice(c2, 礼墨Sumi=28, 安晚awa=19)
    _peers(c2, (PEER_TITLE, True))
    never = lambda *a: (_ for _ in ()).throw(AssertionError("asked"))
    assert collab_guests.name_guests(c2, never) == ["礼墨Sumi", "安晚awa"]


def test_first_clip_of_real_collab_day_with_strong_voice_confirms_judge_guest(tmp_path):
    # Clip #1 at step 4 (heard=True), no peers/subtitles/dynamics tie yet: >= voice_min_chunks (4) confirms the judge's guest
    c1, _ = _collab(tmp_path, "first_strong")
    _voice(c1, 安晚awa=7)
    assert collab_guests.name_guests(c1, lambda *a: {"in_call": [{"name": "安晚awa", "quotes": ["假面安晚睡的香"]}]}, heard=True) == ["安晚awa"]
    assert [g[0] for g in c1.session["participants"]] == ["李豆沙", "安晚awa"]
    assert c1.session["guest_evidence"]["by"] == {"安晚awa": ["chat", "voice"]}
    # Contrast: 3 chunks (< voice_min_chunks = 4) is NOT confirmed on Clip #1
    c_weak, _ = _collab(tmp_path / "weak", "first_weak")
    _voice(c_weak, 安晚awa=3)
    assert collab_guests.name_guests(c_weak, lambda *a: {"in_call": [{"name": "安晚awa", "quotes": ["假面安晚睡的香"]}]}, heard=True) == []
    assert [g[0] for g in c_weak.session["participants"]] == ["李豆沙"]


def test_stored_record_from_before_rule_shrinks_on_read_and_clip_is_behind(tmp_path):
    # Pre-rule 11-25 stored record on disk + clip session already holding stale unconfirmed guests
    ctx, _ = _collab(tmp_path, "stale")
    _voice(ctx, 安晚awa=1, 礼墨Sumi=1)
    ctx.session["participants"] = [["李豆沙"], ["安晚awa"], ["礼墨Sumi"]]
    ctx.session["guest_evidence"] = {"source": "collab_guests", "record": "collab-guests-2026-01-01.json",
                                     "guests": ["安晚awa", "礼墨Sumi"], "by": {"安晚awa": ["chat"], "礼墨Sumi": ["chat"]}}
    ctx.state.save()
    cache = tmp_path / collab_guests.CACHE.format(date=ctx.session["recording_date"])
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({
        "date": ctx.session["recording_date"],
        "chat_guests": ["安晚awa", "礼墨Sumi"],
        "guests": ["安晚awa", "礼墨Sumi"],
        "dynamics": {"linked": {}},
        "clips": {"stale": {"voice": {"礼墨Sumi": 1, "安晚awa": 1}, "subtitles": {}, "peers": {}, "paths": []}},
    }, ensure_ascii=False), encoding="utf-8")
    assert collab_guests.lacking(ctx) == ["安晚awa", "礼墨Sumi"]
    assert collab_guests.behind(ctx) == ["安晚awa", "礼墨Sumi"]
    recomputed = json.loads(cache.read_text(encoding="utf-8"))
    assert recomputed["guests"] == [] and recomputed["chat_guests"] == ["安晚awa", "礼墨Sumi"]
    assert collab_guests.catch_up(ctx) == ["安晚awa", "礼墨Sumi"]
    assert [g[0] for g in ctx.session["participants"]] == ["李豆沙"]
    assert ctx.session["guest_evidence"]["guests"] == [] and ctx.session["guest_evidence"]["by"] == {}
    line = collab_guests.process_line(ctx, ["安晚awa", "礼墨Sumi"])
    assert "移出未证实候选 安晚awa、礼墨Sumi" in line
    assert collab_guests.lacking(ctx) == [] and collab_guests.behind(ctx) == []
    # Hand-written participants.json (no guest_evidence) is untouched even when day record shrinks
    hand, _ = _collab(tmp_path, "hand")
    hand.session["participants"] = [["李豆沙"], ["安晚awa"]]
    hand.state.save()
    assert collab_guests.lacking(hand) == [] and collab_guests.behind(hand) == []


def test_step4_step6_rewind_terminates_once_guest_is_confirmed(tmp_path, monkeypatch):
    # Reviewer item 1 reproduction: subtitle evidence flips with 2-person (0.5) vs 3-person (0.8) host threshold;
    # once B is confirmed in `confirmed`, step 4/6 rewind happens at most once and terminates cleanly.
    from src.autoslice.manual_lane import step4_speaker

    A, B = "礼墨Sumi", "安晚awa"
    ctx, _ = _collab(tmp_path, "b")
    ctx.session["participants"] = [[H], [A]]
    ctx.session["guest_evidence"] = {
        "source": "collab_guests", "record": "day", "guests": [A], "by": {A: ["chat", "linked"]},
    }
    ctx.state.save()
    cache = tmp_path / collab_guests.CACHE.format(date=ctx.session["recording_date"])
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({
        "chat_guests": [A, B], "guests": [A], "dynamics": {"linked": {A: ["矿工"]}},
        "clips": {"a": {"voice": {A: 0, B: 0}, "subtitles": {B: 2}, "peers": {}, "paths": []}},
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(collab_guests, "streamers", lambda _ctx: {B: ["安晚"], A: ["礼墨"]})
    monkeypatch.setattr(step4_speaker, "_witness_clusters", lambda *a: step4_speaker._Clusters())
    monkeypatch.setattr(step4_speaker, "_review_prompt", lambda *a: "synthetic")
    monkeypatch.setattr(step4_speaker, "_ask_cpa", lambda *a, **kw: {
        "rc": 0, "response_text": json.dumps([{"n": 1, "speaker": H, "evidence": "声音", "reason": "按语气"}], ensure_ascii=False),
    })
    monkeypatch.setattr(step6_title, "_identify_guests", lambda _ctx: ({}, []))
    monkeypatch.setattr(step6_title, "pick_title", lambda _ctx: StepOutcome(True, note="reached title"))
    _lines(ctx, (H, "安晚请说"), (G, "好的"))
    units = [{"id": "u0", "s": 0, "e": 900, "lw": 900}, {"id": "u1", "s": 1000, "e": 1900, "lw": 1900}]
    preds = {
        "u0": {"id": "u0", "ok": True, "probs": {H: 0.6, A: 0.2, B: 0.2}},
        "u1": {"id": "u1", "ok": True, "probs": {H: 0.05, A: 0.05, B: 0.9}},
    }
    (ctx.clip_dir / "speaker").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / collab_guests.VOICE_PREDS).write_text(
        json.dumps({"predictions": list(preds.values())}, ensure_ascii=False), encoding="utf-8")

    # Pass 1 (2-person threshold 0.5 -> line 1 is HOST -> B reaches 3 subtitle hits -> B confirmed -> rewind once)
    assert step4_speaker._speaker_from_preds(ctx, units, preds, unit_kind="fake").ok
    first = step6_title.step6_title_cover(ctx)
    assert first.rewind == ("speaker", "burn", "title_cover")
    assert [g[0] for g in ctx.session["participants"]] == [H, A, B]
    record = json.loads(cache.read_text(encoding="utf-8"))
    assert record["confirmed"][A]["by"] == ["chat", "linked"]
    assert record["confirmed"][B]["by"] == ["chat", "subtitles"] and record["confirmed"][B]["clip"] == "b"

    # Pass 2 (3-person threshold 0.8 -> line 1 becomes GUEST -> live subtitle count drops, but B stays confirmed -> no rewind)
    assert step4_speaker._speaker_from_preds(ctx, units, preds, unit_kind="fake").ok
    second = step6_title.step6_title_cover(ctx)
    assert second.ok and second.rewind == () and second.note == "reached title"
    assert [g[0] for g in ctx.session["participants"]] == [H, A, B]
    after = json.loads(cache.read_text(encoding="utf-8"))
    assert after["guests"] == [A, B] and set(after["confirmed"]) == {A, B}


def test_legacy_records_migrate_confirmed_guests_permanently_and_drop_unconfirmed(tmp_path):
    # Legacy records without `confirmed` migrate on first read: confirmed guests are recorded permanently in `confirmed`
    # (11-23 and 11-24 keep both guests even if all clips are later superseded); unconfirmed 11-25 candidates come off.
    # 11-23 shape: both in chat_guests, confirmed by voice + peers
    t23, _ = _collab(tmp_path / "d23", "c23")
    t23.session["participants"] = [[H], ["安晚awa"], ["礼墨Sumi"]]
    t23.session["guest_evidence"] = {"source": "collab_guests", "record": "r", "guests": ["安晚awa", "礼墨Sumi"], "by": {}}
    t23.state.save()
    c23 = (tmp_path / "d23") / collab_guests.CACHE.format(date=t23.session["recording_date"])
    c23.parent.mkdir(parents=True)
    c23.write_text(json.dumps({
        "date": "2026-11-23", "chat_guests": ["安晚awa", "礼墨Sumi"], "guests": ["安晚awa", "礼墨Sumi"],
        "clips": {"x23": {"voice": {"礼墨Sumi": 15, "安晚awa": 21}, "subtitles": {},
                          "peers": {"BV1": {"title": "t", "names": ["安晚awa", "礼墨Sumi"]}}, "paths": []}},
    }, ensure_ascii=False), encoding="utf-8")
    assert collab_guests.lacking(t23) == []
    r23 = json.loads(c23.read_text(encoding="utf-8"))
    assert set(r23["confirmed"]) == {"安晚awa", "礼墨Sumi"} and r23["guests"] == ["安晚awa", "礼墨Sumi"]
    # Even if x23 is later superseded (clips become empty), confirmed guests stay permanently
    r23["clips"] = {}
    c23.write_text(json.dumps(r23, ensure_ascii=False), encoding="utf-8")
    assert collab_guests.lacking(t23) == [] and collab_guests.behind(t23) == []
    assert json.loads(c23.read_text(encoding="utf-8"))["guests"] == ["安晚awa", "礼墨Sumi"]

    # 11-24 shape: judge named 礼墨Sumi, 2 clips' voice + peers named 安晚awa + shared_path
    t24, _ = _collab(tmp_path / "d24", "c24")
    t24.session["participants"] = [[H], ["礼墨Sumi"], ["安晚awa"]]
    t24.session["guest_evidence"] = {"source": "collab_guests", "record": "r", "guests": ["礼墨Sumi", "安晚awa"], "by": {}}
    t24.state.save()
    c24 = (tmp_path / "d24") / collab_guests.CACHE.format(date=t24.session["recording_date"])
    c24.parent.mkdir(parents=True)
    c24.write_text(json.dumps({
        "date": "2026-11-24", "chat_guests": ["礼墨Sumi"], "guests": ["礼墨Sumi", "安晚awa"],
        "clips": {
            "x1": {"voice": {"礼墨Sumi": 23, "安晚awa": 7}, "subtitles": {},
                   "peers": {"BV2": {"title": "t", "names": ["安晚awa", "礼墨Sumi"]}}, "paths": [{"礼墨Sumi": 22, "安晚awa": 7}]},
            "x2": {"voice": {"礼墨Sumi": 28, "安晚awa": 19}, "subtitles": {},
                   "peers": {"BV2": {"title": "t", "names": ["安晚awa", "礼墨Sumi"]}}, "paths": [{"礼墨Sumi": 27, "安晚awa": 19}]},
        },
    }, ensure_ascii=False), encoding="utf-8")
    assert collab_guests.lacking(t24) == []
    r24 = json.loads(c24.read_text(encoding="utf-8"))
    assert set(r24["confirmed"]) == {"礼墨Sumi", "安晚awa"} and r24["shared_path"] == [["礼墨Sumi", "安晚awa"]]
    r24["clips"] = {}
    c24.write_text(json.dumps(r24, ensure_ascii=False), encoding="utf-8")
    assert collab_guests.lacking(t24) == []
    assert json.loads(c24.read_text(encoding="utf-8"))["guests"] == ["礼墨Sumi", "安晚awa"]

    # 11-25 shape: judge named both on chat talk alone -> confirmed={}, guests=[], candidates stay in chat_guests
    t25, _ = _collab(tmp_path / "d25", "c25")
    t25.session["participants"] = [[H], ["安晚awa"], ["礼墨Sumi"]]
    t25.session["guest_evidence"] = {"source": "collab_guests", "record": "r", "guests": ["安晚awa", "礼墨Sumi"], "by": {}}
    t25.state.save()
    c25 = (tmp_path / "d25") / collab_guests.CACHE.format(date=t25.session["recording_date"])
    c25.parent.mkdir(parents=True)
    c25.write_text(json.dumps({
        "date": "2026-11-25", "chat_guests": ["安晚awa", "礼墨Sumi"], "guests": ["安晚awa", "礼墨Sumi"],
        "dynamics": {"linked": {}},
        "clips": {"x25": {"voice": {"礼墨Sumi": 1, "安晚awa": 1}, "subtitles": {}, "peers": {}, "paths": []}},
    }, ensure_ascii=False), encoding="utf-8")
    assert collab_guests.lacking(t25) == ["安晚awa", "礼墨Sumi"]
    r25 = json.loads(c25.read_text(encoding="utf-8"))
    assert r25["confirmed"] == {} and r25["guests"] == [] and r25["chat_guests"] == ["安晚awa", "礼墨Sumi"]


def test_apply_keeps_alias_first_host_group_and_unnamed_placeholder(tmp_path):
    # G6: host group identified by membership of HOST (not index-0 spelling) keeps both its alias-first group
    # and the unnamed guest placeholder intact when guests are added or unconfirmed guests removed.
    ctx, _ = _collab(tmp_path, "alias_host")
    ctx.session["participants"] = [["小李", "李豆沙"], ["未具名嘉宾"], ["安晚awa"]]
    ctx.session["guest_evidence"] = {"source": "collab_guests", "record": "day", "guests": ["安晚awa"], "by": {"安晚awa": ["chat"]}}
    ctx.state.save()
    assert collab_guests._session_guests(ctx.session) == ["安晚awa"]
    assert collab_guests._apply(ctx, {"guests": [], "routes": {}, "confirmed": {}, "shared_path": []}, "day") == ["安晚awa"]
    assert ctx.session["participants"] == [["小李", "李豆沙"], ["未具名嘉宾"]]
    added = collab_guests._apply(ctx, {
        "guests": ["礼墨Sumi"],
        "routes": {"礼墨Sumi": {"chat": True, "routes": ["linked"]}},
        "confirmed": {"礼墨Sumi": {"by": ["chat", "linked"], "at": "t", "clip": "c"}},
        "shared_path": [],
    }, "day")
    assert added == ["礼墨Sumi"]
    assert ctx.session["participants"] == [["小李", "李豆沙"], ["未具名嘉宾"], ["礼墨Sumi"]]


def test_a_legacy_record_is_migrated_before_this_clips_evidence_replaces_what_it_held(tmp_path, monkeypatch):
    """Review the entry that updates a clip's evidence (name_guests / behind) replaced the clip's stored
    evidence first and migrated second, so a guest the stored record confirmed (subtitles: 2 in clip a, 1 in clip b)
    lost her confirmation when clip b, made again under today's list, no longer held its one answer; read through
    `lacking` first, the same record kept her.  The order of the first read must not decide who was in the call."""
    monkeypatch.setattr(collab_guests, "streamers", lambda ctx: {"安晚awa": ["安晚"], "礼墨Sumi": ["礼墨"]})
    stored = {"chat_guests": ["礼墨Sumi", "安晚awa"], "guests": ["礼墨Sumi", "安晚awa"],
              "dynamics": {"linked": {"礼墨Sumi": ["矿工"]}},
              "clips": {"a": {"voice": {}, "subtitles": {"安晚awa": 2}, "peers": {}, "paths": []},
                        "b": {"voice": {}, "subtitles": {"安晚awa": 1}, "peers": {}, "paths": []}}}
    kept = {}
    for order in ("lacking_first", "update_first"):
        ctx, _ = _collab(tmp_path / order, "b")
        ctx.session["participants"] = [[H], ["礼墨Sumi"], ["安晚awa"]]
        ctx.session["guest_evidence"] = {"source": "collab_guests", "record": "r", "guests": ["礼墨Sumi", "安晚awa"], "by": {}}
        ctx.state.save()
        cache = (tmp_path / order) / collab_guests.CACHE.format(date=ctx.session["recording_date"])
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
        _lines(ctx, (H, "今天玩什么"), (H, "我先开一局"))          # clip b as it is now: nobody is named and answered
        if order == "lacking_first":
            assert collab_guests.lacking(ctx) == []
        assert collab_guests.behind(ctx) == []
        record = json.loads(cache.read_text(encoding="utf-8"))
        kept[order] = (sorted(record["confirmed"]), record["guests"])
    assert kept["lacking_first"] == kept["update_first"] == (["安晚awa", "礼墨Sumi"], ["礼墨Sumi", "安晚awa"])
