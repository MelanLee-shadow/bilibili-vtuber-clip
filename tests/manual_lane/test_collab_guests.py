"""A collab day whose session names nobody but the host: the guests come from the whole session's chat, are kept only
on chat lines that name them, are shared by every clip of the day, and reach the title as named participants."""

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
    got = collab_guests.name_guests(ctx, lambda call_id, prompt: asked.append(prompt) or ANSWER)
    assert got == ['安晚awa', '礼墨Sumi']                  # 露医生: not a candidate, her quote is in no chat line
    assert [g[0] for g in ctx.session['participants']] == ['李豆沙', '安晚awa', '礼墨Sumi']
    assert '安晚awa（弹幕里 8 条提到）' in asked[0] and 'kmx' not in asked[0]
    second, _ = _collab(tmp_path, 'b')                     # the same day: read from the task directory, not asked again
    assert collab_guests.name_guests(second, lambda *a: asked.append(a) or None) == got and len(asked) == 1


def test_a_quote_that_is_not_a_chat_line_naming_her_keeps_nobody(tmp_path):
    ctx, _ = _collab(tmp_path, 'c')
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
    got = []
    threads = [threading.Thread(target=lambda c=c: got.append(collab_guests.name_guests(c, ask))) for c in ctxs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1 and got == [['安晚awa', '礼墨Sumi']] * 3


# U468 (owner): 「这个联动完全可以直接从新闻Crawler把这个动态一抓就知道了」 — the 11-23 dynamics as posted
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
    assert ctx.session['guest_evidence']['source'] == "session_chat+dynamics"
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
    assert record["dynamics"]["unread"] == ["礼墨Sumi: CrawlError: risk control"] and record["guests"] == ["安晚awa", "礼墨Sumi"]


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
