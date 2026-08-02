import json

import pytest

from src.autoslice import title_policy
from src.autoslice.surface_canon import CHANNEL_PROFILE


class _ProfileWithPolicy:
    def __init__(self, path):
        self.path = path

    def asset_file(self, key):
        assert key == "title_policy"
        return self.path


def test_default_title_policy_is_loaded_from_the_selected_profile_asset():
    payload = json.loads(
        title_policy.CHANNEL_PROFILE.asset_file("title_policy").read_text(
            encoding="utf-8"
        )
    )

    assert tuple(payload["banned_hype_words"]) == title_policy._TITLE_BANNED_HYPE_WORDS
    assert tuple(payload["banned_filler_words"]) == title_policy._TITLE_BANNED_FILLER_WORDS
    assert payload["min_length"] == title_policy._TITLE_MIN_LEN
    assert payload["max_length"] == title_policy._TITLE_MAX_LEN
    assert payload["max_attempts"] == title_policy._TITLE_MAX_ATTEMPTS


def test_title_policy_asset_rejects_unknown_fields(tmp_path, monkeypatch):
    path = tmp_path / "title_policy.json"
    payload = dict(title_policy._POLICY)
    payload["surprise"] = True
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(title_policy, "CHANNEL_PROFILE", _ProfileWithPolicy(path))

    with pytest.raises(title_policy.TitlePolicyError, match=r"unknown=\['surprise'\]"):
        title_policy._load_title_policy()


# Ivan 2026-07-14/19 歌切标题铁律：自动标题只要带歌切前缀就折叠成
# 「前缀《歌名》」，「｜副标题」/hook 尾巴/衬词一律清除。
def test_canonicalize_song_catalog_title_strips_hook_suffix():
    assert title_policy.canonicalize_song_catalog_title(
        f"{CHANNEL_PROFILE.song_title_prefix}《暖暖》｜自弹自唱温柔哄睡"
    ) == f"{CHANNEL_PROFILE.song_title_prefix}《暖暖》"
    assert title_policy.canonicalize_song_catalog_title(
        f"{CHANNEL_PROFILE.song_title_prefix}直播间唱《芽吹くとき》"
    ) == f"{CHANNEL_PROFILE.song_title_prefix}《芽吹くとき》"
    assert title_policy.canonicalize_song_catalog_title(
        f"{CHANNEL_PROFILE.song_title_prefix}吵闹路人甲的《嘉宾》"
    ) == f"{CHANNEL_PROFILE.song_title_prefix}《嘉宾》"


def test_canonicalize_song_catalog_title_passes_talk_titles_through():
    talk = f"{CHANNEL_PROFILE.talk_title_prefix}被说开组会来晚了，主播反怼：因为我们还没开始唱"
    assert title_policy.canonicalize_song_catalog_title(talk) == talk
    quoted_talk = f"{CHANNEL_PROFILE.talk_title_prefix}《虫儿飞》翻车成《冲而飞》？主播唱到满屏幻听笑点"
    assert title_policy.canonicalize_song_catalog_title(quoted_talk) == quoted_talk


def test_canonicalize_song_catalog_title_no_song_name_untouched():
    weird = f"{CHANNEL_PROFILE.song_title_prefix}没有书名号的标题"
    assert title_policy.canonicalize_song_catalog_title(weird) == weird


@pytest.mark.parametrize(
    "title",
    [
        f"{CHANNEL_PROFILE.talk_title_prefix}搭档把大椅子让给{CHANNEL_PROFILE.short_name}（误",
        f"{CHANNEL_PROFILE.talk_title_prefix}搭档说《这也太像了》》",
        f"{CHANNEL_PROFILE.talk_title_prefix}搭档说“最喜欢’",
        f"{CHANNEL_PROFILE.talk_title_prefix}搭档说[最喜欢）",
    ],
)
def test_title_policy_rejects_unbalanced_or_mismatched_marks(title):
    assert "unbalanced_title_marks" in title_policy._title_policy_violations(
        title
    )


def test_title_policy_accepts_nested_balanced_marks():
    title = f"{CHANNEL_PROFILE.talk_title_prefix}搭档问“你最喜欢《哪一个》？（认真）”"
    assert title_policy._title_policy_violations(title) == []


def test_automatic_title_filler_canonicalizer_only_removes_profile_exact_words():
    assert title_policy.canonicalize_automatic_title_fillers(
        f"{CHANNEL_PROFILE.talk_title_prefix}{CHANNEL_PROFILE.short_name}当场拒绝花钱，直接让观众自己开"
    ) == f"{CHANNEL_PROFILE.talk_title_prefix}{CHANNEL_PROFILE.short_name}拒绝花钱，让观众自己开"
    assert title_policy.canonicalize_automatic_title_fillers(
        f"{CHANNEL_PROFILE.talk_title_prefix}{CHANNEL_PROFILE.short_name}秒拒绝花钱，场面太顶"
    ) == f"{CHANNEL_PROFILE.talk_title_prefix}{CHANNEL_PROFILE.short_name}秒拒绝花钱，场面太顶"


@pytest.mark.parametrize(
    ("candidate_id", "expected"),
    [
        (
            "auto_193450_3573_3665",
            "最包容异性恋的直播间，看到男角色只能说出一句不熟",
        ),
        (
            "auto_193450_672_945r3",
            "被坏女人南町问到最最最最喜欢的原因，后来才发现自己才是被收集的那个",
        ),
        (
            "auto_145940_1533_1619r3",
            "嘲笑爱音上不起“月之森贵族学校”，道歉说忘了她是英国留子，又猜她回国可能因为不会做饭",
        ),
        (
            "auto_152944_964_1091",
            "长沙人李豆沙亲自打假“长沙大香肠”，话还没说完，弹幕又提议把技能叫“李姐拉拉”",
        ),
    ],
)
def test_ivan_titles_are_exact_and_survive_recut_suffix(
    candidate_id, expected
):
    assert title_policy.manual_title_override(candidate_id) == expected


def test_publish_title_policy_applies_one_envelope_to_manual_and_auto_titles():
    body = "最包容异性恋的直播间，看到男角色只能说出一句不熟"
    canonical = title_policy.canonicalize_publish_title(body, lane="talk")

    assert canonical == CHANNEL_PROFILE.talk_title_prefix + body
    assert title_policy.publish_title_policy_violations(
        canonical, lane="talk"
    ) == []
    assert "talk_title_prefix_missing" in title_policy.publish_title_policy_violations(
        body, lane="talk"
    )


def test_publish_title_policy_requires_exact_song_catalog_form():
    canonical = f"{CHANNEL_PROFILE.song_title_prefix}《暖暖》"

    assert title_policy.publish_title_policy_violations(
        canonical, lane="song"
    ) == []
    assert "song_catalog_title_not_exact" in (
        title_policy.publish_title_policy_violations(
            canonical + "｜温柔哄睡", lane="song"
        )
    )
    assert title_policy.canonicalize_publish_title(
        "温柔唱《暖暖》｜哄睡", lane="song"
    ) == canonical
