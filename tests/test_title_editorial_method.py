"""Ordinary title prompts carry the editorial source, not just its name."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.autoslice import title_editorial_method, title_policy

REPO = Path(__file__).resolve().parents[1]
SRT = "1\n00:00:00,000 --> 00:00:02,000\n测试字幕\n"
JUDGE_FILE_TEXT = "# 判官模板\n\n---\n@@RULES@@\n@@SUBTITLE@@\n"
QUALITY_FILE_TEXT = "# 写法判官模板\n\n---\n@@RULES@@\n@@SUBTITLE@@\n@@TITLE@@\n"


def _prompt(**overrides):
    arguments = {
        "selection_hook": "旧钩子不得作事实",
        "transcript_sample": "旧样本不得作事实",
        "important_ip_prompt": "重要作品仅作选题信号",
        "clip_context_prompt": "旧上下文不得作事实",
        "persona_asset": "人设片段不得作事实",
        "style_asset": "旧风格片段不得作事实",
        "final_transcript": SRT,
    }
    arguments.update(overrides)
    return title_policy.build_automatic_talk_title_prompt(**arguments)


def test_ordinary_prompt_actually_carries_editorial_method():
    prompt = _prompt()
    assert "**反常的来源**" in prompt
    method = title_editorial_method.load_title_editorial_method()
    assert method.sections in prompt and method.judge_rules in prompt and method.examples in prompt
    assert method.hooks.startswith("## 三、") and method.hooks in prompt      # how the landings are found, before section 四
    assert prompt.index(method.hooks) < prompt.index(method.sections)
    assert all(source.name in prompt and source.sha256 in prompt for source in method.sources)
    assert SRT in prompt and "重要作品仅作选题信号" in prompt
    assert all(x not in prompt for x in ("旧钩子不得作事实", "旧样本不得作事实", "旧上下文不得作事实",
                                         "人设片段不得作事实", "旧风格片段不得作事实"))
    assert "字幕无speaker标签时quote.speaker留空" in prompt
    assert "不许拼接不同说话人" in prompt and "quote非空时其text必须出现在标题中" in prompt
    assert "示范和编辑说明只供学习写法，不是本片事实" in prompt


def test_new_source_reaches_builder_and_offline_transport(tmp_path, monkeypatch):
    skill = tmp_path / "new-skill"
    (skill / "references").mkdir(parents=True)
    new_rule = "全新编辑源成员：测试新的写法"
    new_example = "全新示范成员：测试示例的写法"
    (skill / "SKILL.md").write_text(
        "## 四、标题\n" + new_rule + "\n## 五、封面\n封面正文\n## 六、画面\n"
        "3. **意思**：事实与修辞\n\n   **判官守则**：守则正文\n", encoding="utf-8"
    )
    (skill / "references/examples.md").write_text("# 示范\n" + new_example + "\n", encoding="utf-8")
    (skill / "references/judge-prompt.md").write_text(JUDGE_FILE_TEXT, encoding="utf-8")
    (skill / "references/quality-judge-prompt.md").write_text(QUALITY_FILE_TEXT, encoding="utf-8")
    outputs = [tmp_path / "projection.json", tmp_path / "projection-again.json"]
    for output in outputs:
        subprocess.run([sys.executable, str(REPO / "scripts/build_title_editorial_method.py"),
                        "--skill-root", str(skill), "--output", str(output)],
                       cwd=REPO, check=True, capture_output=True, text=True)
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    payload = json.loads(outputs[0].read_text(encoding="utf-8"))
    assert new_rule in payload["sections"] and new_example in payload["examples"]
    assert payload["sources"] == [
        {"name": "new-skill/SKILL.md", "path": "SKILL.md",
         "sha256": hashlib.sha256((skill / "SKILL.md").read_bytes()).hexdigest()},
        {"name": "new-skill/references/examples.md", "path": "references/examples.md",
         "sha256": hashlib.sha256((skill / "references/examples.md").read_bytes()).hexdigest()},
    ]
    assert payload["judge_source"] == {
        "name": "new-skill/references/judge-prompt.md", "path": "references/judge-prompt.md",
        "sha256": hashlib.sha256((skill / "references/judge-prompt.md").read_bytes()).hexdigest()}
    assert payload["judge_template"] == "@@RULES@@\n@@SUBTITLE@@"
    assert payload["quality_source"] == {
        "name": "new-skill/references/quality-judge-prompt.md", "path": "references/quality-judge-prompt.md",
        "sha256": hashlib.sha256((skill / "references/quality-judge-prompt.md").read_bytes()).hexdigest()}
    assert payload["quality_template"] == "@@RULES@@\n@@SUBTITLE@@\n@@TITLE@@"
    assert str(tmp_path) not in outputs[0].read_text(encoding="utf-8")
    monkeypatch.setattr(title_editorial_method, "TITLE_EDITORIAL_METHOD", outputs[0])
    assert title_editorial_method.load_title_editorial_method().sources[0].name == "new-skill/SKILL.md"
    mixed = dict(payload, judge_source=dict(payload["judge_source"], name="other/references/judge-prompt.md"))
    outputs[1].write_text(json.dumps(mixed, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(title_editorial_method, "TITLE_EDITORIAL_METHOD", outputs[1])
    with pytest.raises(title_editorial_method.AuthorityError, match="invalid source record"):   # one method, one directory
        title_editorial_method.load_title_editorial_method()
    monkeypatch.setattr(title_editorial_method, "TITLE_EDITORIAL_METHOD", outputs[0])
    sent = []
    sent.append(_prompt())  # Offline fake transport: capture exactly the prompt a caller sends.
    assert new_rule in sent[0] and new_example in sent[0] and SRT in sent[0]


@pytest.mark.parametrize("contents", [None, "not json", "{}",
                                    '{"sections":"x","judge_rules":"x","examples":"x","sources":[]}',
                                    '{"sections":"x","judge_rules":"x","examples":"x","sources":[],'
                                    '"judge_template":"x","judge_source":{}}',
                                    '{"sections":"x","judge_rules":"x","examples":"x","sources":[],'
                                    '"judge_template":"x","judge_source":{},"quality_template":"x",'
                                    '"quality_source":{}}'])
def test_required_projection_missing_or_malformed_fails_closed(tmp_path, monkeypatch, contents):
    path = tmp_path / "projection.json"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(title_editorial_method, "TITLE_EDITORIAL_METHOD", path)
    with pytest.raises(title_editorial_method.AuthorityError, match="title editorial method"):
        _prompt()


def test_projection_rejects_malformed_source_record(tmp_path, monkeypatch):
    payload = json.loads(title_editorial_method.TITLE_EDITORIAL_METHOD.read_text(encoding="utf-8"))
    payload["sources"][0]["sha256"] = "not-a-sha256"
    path = tmp_path / "projection.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(title_editorial_method, "TITLE_EDITORIAL_METHOD", path)
    with pytest.raises(title_editorial_method.AuthorityError, match="invalid source record"):
        _prompt()


def test_legacy_and_missing_subtitles_do_not_require_the_projection(tmp_path, monkeypatch):
    monkeypatch.setattr(title_editorial_method, "TITLE_EDITORIAL_METHOD", tmp_path / "missing.json")
    assert _prompt(skill_required=False)
    with pytest.raises(ValueError, match="TITLE_FINAL_SUBTITLES_REQUIRED"):
        _prompt(final_transcript=None)


def test_builder_rejects_missing_or_incomplete_source(tmp_path):
    skill = tmp_path / "skill"
    skill.mkdir()
    out = tmp_path / "out.json"
    command = [sys.executable, str(REPO / "scripts/build_title_editorial_method.py"),
               "--skill-root", str(skill), "--output", str(out)]
    missing = subprocess.run(command, cwd=REPO, capture_output=True, text=True)
    assert missing.returncode != 0 and "unreadable" in missing.stderr and not out.exists()
    (skill / "references").mkdir()
    (skill / "SKILL.md").write_text("## 四、标题\n正文\n", encoding="utf-8")
    (skill / "references/examples.md").write_text("示例", encoding="utf-8")
    (skill / "references/judge-prompt.md").write_text(JUDGE_FILE_TEXT, encoding="utf-8")
    (skill / "references/quality-judge-prompt.md").write_text(QUALITY_FILE_TEXT, encoding="utf-8")
    incomplete = subprocess.run(command, cwd=REPO, capture_output=True, text=True)
    assert incomplete.returncode != 0 and "section marker" in incomplete.stderr and not out.exists()


def test_builder_rejects_a_judge_template_without_the_rules_slot(tmp_path):
    skill = tmp_path / "skill"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("## 四、标题\n正文\n## 五、封面\n封面\n## 六、画面\n"
                                    "3. **意思**：x\n\n   **判官守则**：y\n", encoding="utf-8")
    (skill / "references/examples.md").write_text("示例", encoding="utf-8")
    (skill / "references/judge-prompt.md").write_text("# 模板\n没有分隔线\n@@RULES@@\n", encoding="utf-8")
    (skill / "references/quality-judge-prompt.md").write_text(QUALITY_FILE_TEXT, encoding="utf-8")
    command = [sys.executable, str(REPO / "scripts/build_title_editorial_method.py"),
               "--skill-root", str(skill), "--output", str(tmp_path / "out.json")]
    run = subprocess.run(command, cwd=REPO, capture_output=True, text=True)
    assert run.returncode != 0 and "judge-prompt.md" in run.stderr and not (tmp_path / "out.json").exists()
    # the writing judge (skill 八.4) is held to the same slot: without @@RULES@@ it would judge without section 四
    (skill / "references/judge-prompt.md").write_text(JUDGE_FILE_TEXT, encoding="utf-8")
    (skill / "references/quality-judge-prompt.md").write_text("# 模板\n\n---\n@@TITLE@@\n", encoding="utf-8")
    run = subprocess.run(command, cwd=REPO, capture_output=True, text=True)
    assert run.returncode != 0 and "quality-judge-prompt.md" in run.stderr and not (tmp_path / "out.json").exists()
    (skill / "references/quality-judge-prompt.md").unlink()
    run = subprocess.run(command, cwd=REPO, capture_output=True, text=True)
    assert run.returncode != 0 and "quality-judge-prompt.md" in run.stderr and not (tmp_path / "out.json").exists()


def test_title_rules_are_section_four_alone():
    method = title_editorial_method.load_title_editorial_method()
    rules = title_editorial_method.title_rules(method)
    assert rules.startswith("## 四、") and "## 五、" not in rules and method.sections.startswith(rules)
    assert "直接毙掉的写法" in rules


def test_cover_text_is_section_five_alone():
    method = title_editorial_method.load_title_editorial_method()
    cover = title_editorial_method.cover_text(method)
    assert cover.startswith("## 五、") and "## 四、" not in cover and method.sections.endswith(cover)


def _skill_source():
    root = (Path(os.environ.get("AGENT_TOOLKIT_ROOT", "~/Project/agent-toolkit")).expanduser()
            / "skills/lidousha-title-cover")
    if not (root / "SKILL.md").is_file():
        pytest.skip(f"the lidousha-title-cover skill source is not on this machine: {root}")
    return root


def test_the_deployed_projection_matches_the_skill_source_it_was_built_from():
    """The projection is what every machine reads; it is only as current as its last build."""
    root = _skill_source()
    method = title_editorial_method.load_title_editorial_method()
    recorded = {s.path: s.sha256 for s in (*method.sources, method.judge_source, method.quality_source)}
    current = {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in recorded}
    assert recorded == current, (
        "技能改了要重新生成投影（skill changed: rebuild and commit the projection): "
        f"python3 scripts/build_title_editorial_method.py --skill-root {root}")


def test_the_pinned_skill_commit_holds_the_text_the_projection_was_built_from():
    """The deploy installs the skill by the pinned commit; a projection built from another commit would disagree
    with the installed skill on the media host."""
    root = _skill_source()
    top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode:
        pytest.skip("the skill here is an installed copy, not the agent-toolkit repo (the test above compares it)")
    pin = json.loads((REPO / "config/title_skill_source.json").read_text(encoding="utf-8"))["agent_toolkit_commit"]
    method = title_editorial_method.load_title_editorial_method()
    recorded = {s.path: s.sha256 for s in (*method.sources, method.judge_source, method.quality_source)}
    pinned = {path: hashlib.sha256(subprocess.check_output(
        ["git", "-C", str(root), "show", f"{pin}:./{path}"])).hexdigest() for path in recorded}
    assert recorded == pinned, "config/title_skill_source.json 与投影不是同一版技能：重新生成投影（会一并改钉子）"


def _git_skill(tmp_path):
    repo = tmp_path / "toolkit"
    skill = repo / "skills/lidousha-title-cover"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("## 四、标题\n写法\n## 五、封面\n封面正文\n## 六、画面\n"
                                    "3. **意思**：事实与修辞\n\n   **判官守则**：守则正文\n", encoding="utf-8")
    (skill / "references/examples.md").write_text("# 示范\n例\n", encoding="utf-8")
    (skill / "references/judge-prompt.md").write_text(JUDGE_FILE_TEXT, encoding="utf-8")
    (skill / "references/quality-judge-prompt.md").write_text(QUALITY_FILE_TEXT, encoding="utf-8")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "skill"], check=True)
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    pin = tmp_path / "pin.json"
    pin.write_text(json.dumps({"_doc": "kept", "agent_toolkit_commit": "0" * 40}) + "\n", encoding="utf-8")
    return skill, pin, head


def _build_with_pin(skill, pin, output):
    return subprocess.run([sys.executable, str(REPO / "scripts/build_title_editorial_method.py"), "--skill-root",
                           str(skill), "--output", str(output), "--pin", str(pin)],
                          cwd=REPO, capture_output=True, text=True)


def test_building_the_projection_moves_the_pin_to_the_skill_commit(tmp_path):
    skill, pin, head = _git_skill(tmp_path)
    assert _build_with_pin(skill, pin, tmp_path / "out.json").returncode == 0
    assert json.loads(pin.read_text(encoding="utf-8")) == {"_doc": "kept", "agent_toolkit_commit": head}


def test_uncommitted_skill_text_cannot_become_the_pinned_projection(tmp_path):
    skill, pin, _ = _git_skill(tmp_path)
    (skill / "SKILL.md").write_text((skill / "SKILL.md").read_text(encoding="utf-8") + "未提交\n", encoding="utf-8")
    done = _build_with_pin(skill, pin, tmp_path / "out.json")
    assert done.returncode != 0 and "commit the skill" in done.stderr
    assert not (tmp_path / "out.json").exists()
    assert json.loads(pin.read_text(encoding="utf-8"))["agent_toolkit_commit"] == "0" * 40


def _quality_answer(whose, banned):
    item = {"ok": True, "why": ""}
    return {"named": item, "mishap": {"ok": True, "words": "x", "why": ""}, "setup": item, "open": item,
            "last_clause": {"text": "x", "whose": whose, "evidence": "3"}, "banned": dict.fromkeys(banned, ""),
            "pronouns": [], "swap": {"holds_with_other_host": False, "why": ""}, "overall": "PASS", "fail_reasons": []}


@pytest.mark.parametrize("channel,landing", [("lidousha", ("host", "to_host")),
                                              ("sumi", ("host", "to_host", "material")),
                                              ("huali", ("host", "to_host", "material")),
                                              ("sumire", ("host", "to_host", "material"))])
def test_each_channels_writing_judge_lets_only_its_own_section_four_endings_close_a_title(monkeypatch, channel, landing):
    """Review the other channels and the public template carried lidousha's writing judge (kmx, endings
    only hers or said to her), though their own section 四 also ends a title on a fan's line, a question or a verdict
    made of the clip's material.  The endings allowed are the ones the channel's template offers."""
    from src.autoslice.manual_lane import title_authority

    if not (REPO / "profiles" / channel).is_dir():
        pytest.skip(f"频道 {channel} 不在这棵树里（公开快照只带默认频道和模板）")
    path = REPO / "assets" / channel / "title_editorial_method.json"
    monkeypatch.setattr(title_editorial_method, "TITLE_EDITORIAL_METHOD", path)
    template = json.loads(path.read_text(encoding="utf-8"))["quality_template"]
    shape = title_authority.quality_shape(template)
    assert shape.landing == landing and "catalog" in shape.banned and "flow_list" in shape.banned
    for whose in ("host", "to_host", "material", "others", "editor"):
        verdict = title_authority.quality_verdict(_quality_answer(whose, shape.banned))
        assert (verdict["overall"] == "PASS") == (whose in landing), (whose, verdict)
    # a judge that leaves one of the banned styles unanswered has not judged it: FAIL, not a pass by omission
    verdict = title_authority.quality_verdict(_quality_answer("host", shape.banned[1:]))
    assert verdict["overall"] == "FAIL" and "没有逐项判" in verdict["fail_reasons"][-1]


def test_the_template_channels_carry_the_public_templates_writing_judge():
    from src.autoslice.manual_lane import title_authority

    # the template's source lives under docs/oss/ here; the public snapshot ships it as assets/_template/
    rel = "assets/_template/title_method/references/quality-judge-prompt.md"
    source = REPO / "docs/oss" / rel
    public = (source if source.is_file() else REPO / rel).read_bytes()
    assert "kmx" not in public.decode() and "李豆沙" not in public.decode()
    assert title_authority.quality_shape(public.decode()).landing == ("host", "to_host", "material")
    for channel in ("sumi", "huali", "sumire"):
        if (REPO / "profiles" / channel).is_dir():
            assert (REPO / "assets" / channel / "title_method/references/quality-judge-prompt.md").read_bytes() == public


def test_the_projection_carries_section_three_where_the_method_has_one(tmp_path):
    """Section 三 is how the landings of a clip are found; the reading, the merge and the title prompt carry it.  A
    method without that section (the template channels') has none, and loads."""
    skill = tmp_path / "skill"
    (skill / "references").mkdir(parents=True)
    body = "## 四、标题\n正文\n## 五、封面\n封面\n## 六、画面\n3. **意思**：x\n\n   **判官守则**：y\n"
    (skill / "references/examples.md").write_text("示例", encoding="utf-8")
    (skill / "references/judge-prompt.md").write_text(JUDGE_FILE_TEXT, encoding="utf-8")
    (skill / "references/quality-judge-prompt.md").write_text(QUALITY_FILE_TEXT, encoding="utf-8")
    (skill / "SKILL.md").write_text("## 三、找钩子\n落点一组\n" + body, encoding="utf-8")
    with_hooks = title_editorial_method.extract_title_editorial_method(skill)
    assert with_hooks.hooks == "## 三、找钩子\n落点一组" and title_editorial_method.projection_data(with_hooks)["hooks"] == with_hooks.hooks
    assert title_editorial_method.format_title_editorial_method(with_hooks).startswith("### skill skill 第三节（落点怎么找）\n## 三、找钩子")
    (skill / "SKILL.md").write_text(body, encoding="utf-8")
    without = title_editorial_method.extract_title_editorial_method(skill)
    assert without.hooks == "" and "第三节" not in title_editorial_method.format_title_editorial_method(without)
