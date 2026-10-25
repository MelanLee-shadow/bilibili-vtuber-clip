"""One tag policy feeds prompts, generation and upload gates; no live API calls."""
from __future__ import annotations

import ast
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

import pytest

from scripts import suggest_upload_tags as st
from src.autoslice import authorized_upload_metadata as metadata
from src.autoslice import upload_tag_policy as policy_module


ROOT = Path(__file__).resolve().parents[1]
CONSUMERS = (
    "scripts/suggest_upload_tags.py",
    "src/autoslice/fastlane_c2_release_bridge.py",
    "src/autoslice/song_delivery.py",
    "src/autoslice/upload_tag_policy.py",
)


def _payload():
    return json.loads(policy_module.load_selected_upload_tag_policy().source_path.read_text())


def _load(tmp_path, payload):
    path = tmp_path / "upload_tag_policy.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return policy_module.load_upload_tag_policy(path)


def _fresh_generator(monkeypatch, policy):
    # Persisted policy enters at the ordinary selection seam. Re-importing the
    # generator exercises its own policy wiring, including function defaults;
    # no per-consumer copying of policy numbers is done by this test.
    monkeypatch.setattr(policy_module, "load_selected_upload_tag_policy", lambda: policy)
    name = "scripts._wp9_tag_policy_probe"
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts/suggest_upload_tags.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_default_prompt_bytes_and_numbers_round_trip():
    payload = _payload()
    policy = policy_module.load_selected_upload_tag_policy()
    assert "{max_dynamic_tags}" in payload["content_prompt_template"]
    assert "{max_dynamic_tag_chars}" in payload["content_prompt_template"]
    assert int(re.search(r"最多(\d+)个", st.CONTENT_PROMPT)[1]) == payload["max_dynamic_tags"]
    assert int(re.search(r"不超过(\d+)个字符", st.CONTENT_PROMPT)[1]) == payload["max_dynamic_tag_chars"]
    assert policy.max_dynamic_tag_chars == payload["max_dynamic_tag_chars"]
    # Freeze the actual baseline bytes; the older control's stale hash is left
    # unchanged and compared separately as an existing baseline failure.
    assert hashlib.sha256(st.CONTENT_PROMPT.encode()).hexdigest() == (
        "27f1e4184e48f86955501f60ef93d765ebfa8fdbb9d923cf21abcaf75f113038"
    )


@pytest.mark.parametrize("total,dynamic,chars,content_chars", [(5, 3, 8, 5), (7, 5, 13, 7)])
def test_new_policy_reaches_prompt_generator_and_upload(tmp_path, monkeypatch, total, dynamic, chars, content_chars):
    payload = _payload()
    payload.update(base_tags=["新主播", "直播切片"], max_tags_default=total,
                   max_dynamic_tags=dynamic, max_tag_chars=chars,
                   max_dynamic_tag_chars=content_chars)
    too_long = "甲" * (content_chars + 1)
    payload["term_rules"].append({
        "name": "wp9_future_proper_name", "patterns": [re.escape(too_long)],
        "tags": [too_long], "min_hits": 1, "note": "已核实专名",
    })
    policy = _load(tmp_path, payload)
    generator = _fresh_generator(monkeypatch, policy)
    srt = tmp_path / "clip.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n正文话题\n", encoding="utf-8")
    exact = "乙" * content_chars
    candidates = [too_long, exact, *[f"话题{i}" for i in range(dynamic + 1)]]

    def call(prompt):
        assert f"最多{dynamic}个" in prompt
        assert f"不超过{content_chars}个字符" in prompt
        assert "新主播" in prompt and "正文话题" in prompt
        return json.dumps({"tags": [{"tag": tag, "why": "当前正文"} for tag in candidates]})

    result = generator.generate_upload_tags("测试主题", srt, llm_call=call)
    assert result["status"] == "OK"
    assert too_long not in result["final_tags"] and exact in result["final_tags"]
    assert f"弃(格式/长度): {too_long!r}" in result["warnings"]
    assert len(result["content_tags"]) == dynamic
    assert len(result["final_tags"]) == total
    assert metadata.validate_tags(result["final_tags"]) == []
    assert metadata.validate_tags([*result["final_tags"], "超额"])
    assert metadata.validate_tags(["丙" * (chars + 1)])
    # Proper names retain the full-tag budget, rather than the LLM-only budget.
    proper = generator.generate_upload_tags(too_long, srt, use_llm=False)
    assert too_long in proper["final_tags"]
    assert metadata.validate_tags(proper["final_tags"]) == []
    report = generator.render_markdown([])
    assert f"基础位({len(policy.base_tags)} 个)" in report
    assert f"封顶 {total} 个" in report and f"内容位最多 {dynamic} 个" in report


def test_upload_enforces_channel_and_platform_domains(monkeypatch):
    policy = policy_module.load_selected_upload_tag_policy()
    assert metadata.validate_tags([f"t{i}" for i in range(policy.max_tags_default)]) == []
    assert metadata.validate_tags([f"t{i}" for i in range(policy.max_tags_default + 1)])
    assert metadata.validate_tags(["甲" * policy.max_tag_chars]) == []
    assert metadata.validate_tags(["甲" * (policy.max_tag_chars + 1)])
    # Even a bypassed/malformed policy cannot raise the upload platform ceiling.
    monkeypatch.setattr(policy_module, "load_selected_upload_tag_policy", lambda: replace(
        policy, max_tags_default=metadata.PLATFORM_MAX_TAGS + 1,
        max_tag_chars=metadata.PLATFORM_MAX_TAG_CHARS + 1,
    ))
    assert metadata.validate_tags([f"t{i}" for i in range(metadata.PLATFORM_MAX_TAGS + 1)])
    assert metadata.validate_tags(["甲" * (metadata.PLATFORM_MAX_TAG_CHARS + 1)])


@pytest.mark.parametrize("field", ["max_tags_default", "max_tag_chars", "max_dynamic_tag_chars"])
def test_policy_cannot_exceed_its_upper_bound(tmp_path, field):
    payload = _payload()
    if field == "max_tags_default":
        payload[field] = metadata.PLATFORM_MAX_TAGS + 1
        payload["max_dynamic_tags"] = payload[field] - len(payload["base_tags"])
    elif field == "max_tag_chars":
        payload[field] = metadata.PLATFORM_MAX_TAG_CHARS + 1
    else:
        payload[field] = payload["max_tag_chars"] + 1
    with pytest.raises(policy_module.UploadTagPolicyError, match="exceeds"):
        _load(tmp_path, payload)


@pytest.mark.parametrize("bad", [True, 0, -1, "10"])
def test_content_character_budget_is_a_positive_integer(tmp_path, bad):
    payload = _payload()
    payload["max_dynamic_tag_chars"] = bad
    with pytest.raises(policy_module.UploadTagPolicyError, match="positive integer"):
        _load(tmp_path, payload)


def test_selected_policy_is_below_platform_bounds():
    policy = policy_module.load_selected_upload_tag_policy()
    assert policy.max_tags_default <= metadata.PLATFORM_MAX_TAGS
    assert policy.max_tag_chars <= metadata.PLATFORM_MAX_TAG_CHARS
    assert policy.max_dynamic_tag_chars <= policy.max_tag_chars


def test_c2_uses_channel_limits_and_song_keeps_platform_limits(tmp_path, monkeypatch):
    from src.autoslice import fastlane_c2_release_bridge as bridge, song_delivery

    payload = _payload()
    payload.update(max_tags_default=5, max_tag_chars=9, max_dynamic_tag_chars=5)
    policy = _load(tmp_path, payload)
    monkeypatch.setattr(bridge, "load_selected_upload_tag_policy", lambda: policy)
    assert bridge._valid_tags({"status": "OK", "final_tags": ["甲" * policy.max_tag_chars]})
    for tags in ([f"t{i}" for i in range(policy.max_tags_default + 1)], ["甲" * (policy.max_tag_chars + 1)]):
        with pytest.raises(bridge.C2ReleaseBridgeError):
            bridge._valid_tags({"status": "OK", "final_tags": tags})
    # This typed package guard was platform-only and remains so; the ordinary
    # authorized uploader independently applies the channel budget afterwards.
    tags = [f"t{i}" for i in range(metadata.PLATFORM_MAX_TAGS)]
    block = {"engine": "test", "status": "OK", "final_tags": tags, "final_tag_line": ",".join(tags)}
    assert song_delivery._validated_song_upload_tags(block) == block
    for tags in ([*tags, "超额"], ["甲" * (metadata.PLATFORM_MAX_TAG_CHARS + 1)], ["重复", "重复"], ["逗,号"], [" 空格"]):
        with pytest.raises(song_delivery.SongDeliveryError):
            song_delivery._validated_song_upload_tags({**block, "final_tags": tags, "final_tag_line": ",".join(tags)})


@pytest.mark.parametrize("tags", [[""], ["重复", "重复"], ["Case", "case"], ["逗,号"], ["换\n行"], ["制\t表"], [None], "非列表"])
def test_existing_upload_rejections_remain(tags):
    assert metadata.validate_tags(tags)


def _tag_numeric_literals(source):
    """Flag contract literals, not unrelated evidence/sample-count budgets."""
    findings = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {part.id.lower() for target in targets for part in ast.walk(target) if isinstance(part, ast.Name)}
            if any("tag" in name and ("max" in name or "limit" in name or "cap" in name) for name in names):
                if node.value is not None and any(isinstance(part, ast.Constant) and type(part.value) is int for part in ast.walk(node.value)):
                    findings.append(node.lineno)
        if isinstance(node, ast.Compare):
            names = {part.id for part in ast.walk(node) if isinstance(part, ast.Name)}
            if names & {"tag", "tags", "clean", "cleaned", "accepted", "final"}:
                if any(isinstance(part, ast.Constant) and type(part.value) is int for part in ast.walk(node)):
                    findings.append(node.lineno)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any(term in node.value for term in ("标签", "基础位", "内容位")) and re.search(r"\d+\s*(?:个|字符|位)", node.value):
                findings.append(node.lineno)
    return findings


def test_no_tag_budget_literals_in_consumers():
    for name in CONSUMERS:
        assert _tag_numeric_literals((ROOT / name).read_text()) == [], name


def test_spelling_guard_detects_unseen_budgets_but_allows_other_numbers():
    for source in ("MAX_TAGS = 7", "if len(tags) > 9: pass", "if len(accepted) >= 11: pass", 'prompt = "最多7个标签"'):
        assert _tag_numeric_literals(source)
    assert not _tag_numeric_literals("evidence = rows[:4]\nif len(surface) >= 2: pass")


def _tag_fields(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "tags":
                yield child
            yield from _tag_fields(child)
    elif isinstance(value, list):
        for child in value:
            yield from _tag_fields(child)
