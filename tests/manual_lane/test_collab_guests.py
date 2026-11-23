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
