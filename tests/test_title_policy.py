import json

import pytest

from src.autoslice import title_policy


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
        "【李豆沙】豆沙歌，《暖暖》｜自弹自唱温柔哄睡"
    ) == "【李豆沙】豆沙歌，《暖暖》"
    assert title_policy.canonicalize_song_catalog_title(
        "【李豆沙】豆沙歌，直播间唱《芽吹くとき》"
    ) == "【李豆沙】豆沙歌，《芽吹くとき》"
    assert title_policy.canonicalize_song_catalog_title(
        "【李豆沙】豆沙歌，吵闹熊猫头的《嘉宾》"
    ) == "【李豆沙】豆沙歌，《嘉宾》"


def test_canonicalize_song_catalog_title_passes_talk_titles_through():
    talk = "【李豆沙】被说开组会来晚了，主播反怼：因为我们还没开始唱"
    assert title_policy.canonicalize_song_catalog_title(talk) == talk
    quoted_talk = "【李豆沙】《虫儿飞》翻车成《冲而飞》？主播唱到满屏幻听笑点"
    assert title_policy.canonicalize_song_catalog_title(quoted_talk) == quoted_talk


def test_canonicalize_song_catalog_title_no_song_name_untouched():
    weird = "【李豆沙】豆沙歌，没有书名号的标题"
    assert title_policy.canonicalize_song_catalog_title(weird) == weird
