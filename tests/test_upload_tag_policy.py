import hashlib
import json

import pytest

from src.autoslice.upload_tag_policy import (
    UploadTagPolicyError,
    load_selected_upload_tag_policy,
    load_upload_tag_policy,
)


def test_default_upload_tag_policy_preserves_the_pre_asset_contract():
    policy = load_selected_upload_tag_policy()

    assert policy.base_tags == ("李豆沙", "虚拟主播", "虚拟UP主", "直播切片")
    assert policy.max_tags_default == 12
    assert policy.max_tag_chars == 20
    assert len(policy.term_rules) == 39
    assert hashlib.sha256(policy.content_prompt_template.encode()).hexdigest() == (
        "717e3d2e1c946853d48118d590f491af38a9dad0b4ee300ca39172df4dabc844"
    )
    zhinv = next(rule for rule in policy.term_rules if rule.name == "zhinv")
    assert zhinv.patterns == ("侄女", "直女")
    assert zhinv.tags == ("侄女", "百合", "女同")


def test_upload_tag_policy_rejects_invalid_regex(tmp_path):
    source = load_selected_upload_tag_policy().source_path
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["term_rules"][0]["patterns"] = ["["]
    path = tmp_path / "upload_tag_policy.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(UploadTagPolicyError, match="invalid regex"):
        load_upload_tag_policy(path)


def test_upload_tag_policy_allows_a_new_channel_to_start_without_term_rules(tmp_path):
    source = load_selected_upload_tag_policy().source_path
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["base_tags"] = ["新主播", "直播切片"]
    payload["term_rules"] = []
    payload["known_proper_surfaces_extra"] = []
    payload["theme_allowed"] = []
    payload["banned_content_tags"] = []
    path = tmp_path / "upload_tag_policy.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    policy = load_upload_tag_policy(path)

    assert policy.base_tags == ("新主播", "直播切片")
    assert policy.term_rules == ()
