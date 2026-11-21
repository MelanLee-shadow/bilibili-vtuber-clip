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
