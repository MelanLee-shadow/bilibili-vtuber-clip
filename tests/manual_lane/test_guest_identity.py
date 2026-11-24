"""A collab with two named guests: a guest line is told to be one of them only through the witnesses' speaker groups and
a cited line that holds (11-23 443680: 「果然awawa就是这种幕后黑手的人」 → 「我我我不是，不要瞎说。」 is 安晚awa, and so is
the bomb proposal in the same MOSS group)."""

from __future__ import annotations

import json

from test_title_history import context

from src.autoslice.manual_lane import guest_identity, step6_title, subtitle_track

LINES = [("李豆沙", "果然awawa就是这种幕后黑手的人。"), ("连线", "我我我不是，不要瞎说。"),
         ("连线", "我可以拿那个炸弹，稍微暖一暖。"), ("连线", "好好可怕。"), ("李豆沙", "那我直接死了。"),
         ("连线", "这个是什么必杀技吗？")]
MOSS = ["S01", "S04", "S04", "S02", "S01", "S02"]
MAI = ["S00", "S02", "S02", "S01", "S00", "S01"]
ANSWER = {"anchors": [{"line": 2, "name": "安晚awa", "kind": "被点名后接话", "quote": "我我我不是"}]}


def _clip(tmp_path, cid, *, moss=MOSS, mai=MAI, guests=(["安晚awa"], ["礼墨Sumi"])):
    ctx, cpa = context(tmp_path, cid, '提议拿炸弹暖一暖')
    ctx.session['participants'] = [['李豆沙', '小李'], *guests]
    cues = [{"start_s": 2.0 * i, "end_s": 2.0 * i + 1.8, "text": t, "speaker": s} for i, (s, t) in enumerate(LINES)]
    subtitle_track.write_track(ctx.clip_dir, cues, speakers=True)
    (ctx.clip_dir / 'witness').mkdir()
    for engine, groups in (("moss", moss), ("mai", mai)):
        segs = [{"start_ms": 2000 * i, "end_ms": 2000 * i + 1800, "speaker": g} for i, g in enumerate(groups)]
        (ctx.clip_dir / f'witness/{engine}.json').write_text(json.dumps({"status": "OK", "segments": segs}))
    return ctx, cpa


def _identify(ctx, answer=ANSWER, asked=None):
    def ask(call_id, prompt):
        if asked is not None:
            asked.append(prompt)
        return answer
    return guest_identity.identify(ctx, subtitle_track.read_track(ctx.clip_dir), ask)


def test_a_cited_answer_names_her_group_and_the_other_lines_of_it(tmp_path):
    asked = []
    ctx, _ = _clip(tmp_path, 'a')
    assert _identify(ctx, asked=asked) == {2: "安晚awa", 3: "安晚awa"}
    assert '[连线·甲] 我我我不是' in asked[0] and '[连线·乙] 好好可怕' in asked[0]
    _track, lines, srt = step6_title.title_inputs(ctx, ['李豆沙', '安晚awa', '礼墨Sumi'])
    assert '[安晚awa] 我可以拿那个炸弹' in lines and '[连线] 好好可怕' in lines and '[安晚awa]' in srt


def test_citations_that_do_not_hold_name_nobody(tmp_path):
    ctx, _ = _clip(tmp_path, 'b')
    made_up = {"anchors": [{"line": 2, "name": "安晚awa", "kind": "被点名后接话", "quote": "我是安晚"},      # not in the line
                           {"line": 4, "name": "礼墨Sumi", "kind": "被点名后接话", "quote": "好好可怕"},     # nobody named her
                           {"line": 5, "name": "礼墨Sumi", "kind": "自称", "quote": "那我直接死了"},        # the host's line
                           {"line": 6, "name": "露医生", "kind": "自称", "quote": "必杀技"},              # not a guest
                           {"line": 6, "name": "礼墨Sumi", "kind": "口吻像她", "quote": "必杀技"}]}       # not evidence
    assert _identify(ctx, made_up) == {}


def test_a_line_whose_engines_name_different_guests_stays_unnamed(tmp_path):
    # MAI puts line 3 with line 4, whose group a second citation names 礼墨Sumi; MOSS keeps it with 安晚awa
    ctx, _ = _clip(tmp_path, 'c', mai=["S00", "S02", "S01", "S01", "S00", "S01"])
    two = {"anchors": [*ANSWER["anchors"], {"line": 6, "name": "礼墨Sumi", "kind": "自称", "quote": "必杀技"}]}
    got = _identify(ctx, two)
    assert got[2] == "安晚awa" and got[6] == "礼墨Sumi" and got[4] == "礼墨Sumi" and 3 not in got


def test_one_guest_or_a_failed_call_names_nothing(tmp_path):
    ctx, _ = _clip(tmp_path, 'd', guests=(["安晚awa"],))
    assert _identify(ctx, answer=AssertionError) == {}
    ctx, _ = _clip(tmp_path, 'e')
    assert _identify(ctx, answer=None) == {} and guest_identity.saved_lines(ctx) == {}
