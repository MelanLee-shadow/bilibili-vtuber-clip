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
