"""One title, one voice (src/autoslice/manual_lane/title_person.py; step 6 self-check `person`).

Owner 「你前面一句话是以李豆沙的口吻说的，后面一句话又是第三人称说的，不喜欢第三人称」.  The two titles
that went online with her name in the narration and 我 in the same sentence are the counterexamples."""

from __future__ import annotations

import pytest
from fakes import srt

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import title_person

WENT_ONLINE = (
    "【李豆沙】安晚、礼墨Sumi、露医生都没标志，李豆沙续费Oopz后，我那个高贵的会员标志显得特别SB！",
    "【李豆沙】李豆沙聊香蕉牛奶是不是侄女，什么？我之前问过她呀，她说她不是啊！",
)
ONE_VOICE = (
    "【李豆沙】香蕉牛奶不是侄女？什么？我之前问过她呀，她说她不是啊！",                  # her words all the way
    "【李豆沙】摄像头里认出kmx后，李豆沙会一直监视大家的，李豆沙会一直监视kmx的！",      # narration all the way
    "【李豆沙】早说只要安静十秒就能拿提督啊，原来直播这么好做",
    "【李豆沙】都，都怪你kmx，你看看现在别人对我都什么印象，老李头私下烟酒槟榔都来的（误",
    "【李豆沙】南町nightin的填表示例从190粉毛念到卖火柴，终于有机会让小李念出来了！",
    "【李豆沙】写“解”能骗步骤分，装“姐”也能骗到一分？",
    "【李豆沙】礼墨自我介绍到一半，被熊猫头打断",                                        # 自我 is not her speaking
)


def _cand(title):
    return {"title": title, "on_stage": ["李豆沙"], "punchline_person": "她",
            "swap_check": {"holds_with_other_host": False, "reason": "换主播不成立"}}


@pytest.mark.parametrize("title", WENT_ONLINE)
def test_her_name_in_the_narration_and_a_first_person_word_fail_the_self_check(title):
    assert title_person.mixed_person(title) == ("李豆沙", "我")
    checks = si.title_checks(_cand(title), ("李豆沙",), srt(3))
    assert not checks["ok"] and "person" in checks["failed"]
    assert any("人称不一致" in reason for reason in checks["reasons"])


@pytest.mark.parametrize("title", ONE_VOICE)
def test_a_title_in_one_voice_passes(title):
    assert title_person.mixed_person(title) is None
    assert "person" not in si.title_checks(_cand(title), ("李豆沙",), srt(3))["failed"]


def test_words_in_quotation_marks_are_a_quotation_and_do_not_count():
    assert title_person.mixed_person("【李豆沙】被问到会员标志，李豆沙说“我那个标志特别SB”") is None
    assert title_person.mixed_person("【李豆沙】kmx说“小李别跑”，我偏要跑") is None
    assert title_person.mixed_person("【李豆沙】kmx说“别跑”，小李：我偏要跑") == ("小李", "我")


def test_the_prefix_is_not_the_narration():
    assert title_person.mixed_person("【李豆沙×礼墨Sumi】我真的是第一次公主抱别人") is None


# ----- whose 我 (U467) ---------------------------------------------------------------------------------------------
# Owner 「有的地方你用一个我，别人观众不知道这个到底是说的是我是这联动里的谁」.  The lines are those of the
# 583860 and 625460 trio clips as he labelled them (a guest line carries the guest label only, as speaker.srt has it).
TRIO = ("李豆沙", "礼墨Sumi", "安晚awa")
GUESTS = TRIO[1:]


def _labelled(*lines):
    return "\n\n".join(f"{i}\n00:00:{i:02d},000 --> 00:00:{i + 1:02d},000\n[{spk}] {text}"
                       for i, (spk, text) in enumerate(lines, 1)) + "\n"


CLIP_583860 = _labelled(("连线", "我是我真的我真的要被优化掉了"), ("连线", "我们可能会被一起优化掉，有感觉吧"),
                        ("李豆沙", "我们，我们感觉要没了"), ("李豆沙", "最后一条命要没了"))
CLIP_625460 = _labelled(("连线", "我勾不到，我这个，快点帮我勾一下。"), ("李豆沙", "你要勾哪？"),
                        ("连线", "哦，勾到了。我要笑。"))


@pytest.mark.parametrize("title,srt_text,said", [
    ("【李豆沙×安晚awa×礼墨Sumi】抓钱游戏只剩最后一条命，我真的要被优化掉了！我们感觉要没了！", CLIP_583860, "我真的要被优化掉了"),
    ("【李豆沙×安晚awa×礼墨Sumi】队友嫌游戏石头没什么好抢的，却喊帮忙勾，“你要勾哪？哦，勾到了。我要笑。”", CLIP_625460, "我要笑"),
    ("【李豆沙×安晚awa×礼墨Sumi】礼墨：我们感觉要没了！", CLIP_583860, "我们感觉要没了"),     # the host's words put on a guest
])
def test_a_first_person_clause_read_as_someone_who_did_not_say_it_fails(title, srt_text, said):
    found = title_person.misattributed(title, srt_text, host="李豆沙", guests=GUESTS)
    assert [c for c, _ in found] == [said]
    checks = si.title_checks(_cand(title) | {"on_stage": list(TRIO)}, TRIO, srt_text)
    assert "first_person_speaker" in checks["failed"]
    assert any("「我」分不清是谁" in r for r in checks["reasons"])


@pytest.mark.parametrize("title,srt_text", [
    ("【李豆沙×安晚awa×礼墨Sumi】抓钱游戏只剩最后一条命，安晚：我真的要被优化掉了！", CLIP_583860),
    ("【李豆沙×安晚awa×礼墨Sumi】抓钱游戏只剩最后一条命，我们感觉要没了！", CLIP_583860),         # her own words
    ("【李豆沙×安晚awa×礼墨Sumi】礼墨喊“快点帮我勾一下”，勾到了又说“我要笑”", CLIP_625460),
    ("【李豆沙×安晚awa×礼墨Sumi】我才不会被优化掉", CLIP_583860),                               # nobody said it: narration
])
def test_a_first_person_clause_named_for_who_said_it_passes(title, srt_text):
    assert title_person.misattributed(title, srt_text, host="李豆沙", guests=GUESTS) == []


@pytest.mark.parametrize("title", ONE_VOICE)
def test_a_solo_clip_has_no_one_else_to_read_a_first_person_word_as(title):
    solo = _labelled(("李豆沙", "我之前问过她呀"), ("李豆沙", "我偏要跑"))
    assert title_person.misattributed(title, solo, host="李豆沙", guests=()) == []


def test_a_revision_does_not_keep_a_published_title_its_subtitles_show_wrong(monkeypatch):
    """Step 6 of a revision re-reads the title online against the revision's subtitles and speakers (625460 kept a
    title quoting two people as one after his labels were imported)."""
    from src.autoslice.manual_lane import step6_title

    monkeypatch.setattr(step6_title, "title_inputs", lambda ctx, names: ([], "", CLIP_625460))
    online = "【李豆沙×安晚awa×礼墨Sumi】队友嫌游戏石头没什么好抢的，却喊帮忙勾，“你要勾哪？哦，勾到了。我要笑。”"
    breaks = step6_title.online_title_breaks(object(), TRIO, online)
    assert any("引号核对未过" in b for b in breaks) and any("「我」分不清是谁" in b for b in breaks)
    kept = "【李豆沙×安晚awa×礼墨Sumi】礼墨喊“快点帮我勾一下”，勾到了又说“我要笑”"
    assert step6_title.online_title_breaks(object(), TRIO, kept) == []
