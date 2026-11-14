"""TASK-25: step 6 and step 9 write and review titles and cover text from the authorities themselves (the
lidousha-title-cover skill, assets/lidousha/title_style.md and 维护者's hand-set title corpus), not from rules of the
lane's own; every prompt names the authorities' sha256, the skill's judge (references/judge-prompt.md) accepts a title
before it is used, and quotes are checked against the final subtitles with their speakers.

Every case goes through the ordinary path (step 6 or step 9 on fakes) and imports only names the code before TASK-25
already had, so the same checks run against that code (T/title25 old-tree run) and fail there."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fakes import JUDGE_PASS, FakeCpa, FakeRemote, fake_cover, judge_fail, make_ctx, srt

from src.autoslice.manual_lane import layout as layout_mod
from src.autoslice.manual_lane import title_authority
from src.autoslice.manual_lane.runner import StepOutcome
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import stage_package as sp
from src.autoslice.manual_lane.adapters import RemoteResult
from src.autoslice import title_editorial_method, title_policy

RD = "/r/clip"
REPO = layout_mod.REPO_ROOT
STYLE = REPO / "assets/lidousha/title_style.md"
CORPUS = REPO / "assets/lidousha/manual_title_overrides.v1.json"
SESSION = {"recording_date": "2026-10-24", "speaker_mode": "uniform_host", "enablement_evidence": None,
           "participants": [["李豆沙", "小李"], ["雾深Girimi", "雾深"]], "own_uploader_mids": [1]}
GOOD = "【李豆沙】她半分钟后真的破防了"
CONTRARY = ("写出转折", "最好引她的原话")   # the lane's own rules that contradicted the skill (TASK-25 brief)


def _skill():
    """The editable skill source, only to compare the deployed projection with (the title step itself never reads it)."""
    root = (Path(os.environ.get("AGENT_TOOLKIT_ROOT", "~/Project/agent-toolkit")).expanduser()
            / "skills/lidousha-title-cover")
    if not (root / "SKILL.md").is_file():
        pytest.skip(f"the lidousha-title-cover skill is not installed at {root}")
    return root


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _section(path, start, end):
    """The lines of `path` from the heading that begins with `start` to the one that begins with `end` (read here,
    independent of the code under test)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    i = next(n for n, ln in enumerate(lines) if ln.startswith(start))
    j = next(n for n, ln in enumerate(lines) if n > i and ln.startswith(end))
    return [ln for ln in lines[i + 1:j] if ln.strip()]


@pytest.fixture
def fresh_layout():
    layout_mod.layout.cache_clear()
    yield
    layout_mod.layout.cache_clear()


def _cand(title, *, person="她越长大越讨厌看那种有规矩的"):
    return {"title": title, "on_stage": ["李豆沙"], "punchline_person": person,
            "swap_check": {"holds_with_other_host": False, "reason": "换主播不成立"}}


def _answer(*titles):
    return json.dumps({"candidates": [_cand(t) for t in titles], "recommended": 0, "cover_lines": ["破防了", "半分钟"]},
                      ensure_ascii=False)


def _ctx(tmp_path, answers, *, judge=(), speaker=None):
    def script(name, body, remote):
        if name.startswith("s6-cover-job"):
            fake_cover(remote, RD)
        return RemoteResult(0, "", "")
    cpa = FakeCpa(answers, judge=judge)
    ctx = make_ctx(tmp_path, remote=FakeRemote(on_script=script), cpa=cpa, session=SESSION)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(srt(3), encoding="utf-8")
    if speaker is not None:
        (ctx.clip_dir / "speaker").mkdir(parents=True, exist_ok=True)
        (ctx.clip_dir / "speaker/speaker.srt").write_text(speaker, encoding="utf-8")
    return ctx, cpa


def _step6(tmp_path, answers, *, judge=(), speaker=None):
    ctx, cpa = _ctx(tmp_path, answers, judge=judge, speaker=speaker)
    return ctx, cpa, si.step6_title_cover(ctx)


def _pick(tmp_path, answers, *, judge=(), speaker=None):
    """The title half of step 6 only (candidates, checks, judge): it needs no cover renderer on the machine."""
    ctx, cpa = _ctx(tmp_path, answers, judge=judge, speaker=speaker)
    return ctx, cpa, si.pick_title(ctx)


# ----- the prompt carries the authorities, with their hashes -------------------------------------------------
def test_the_title_prompt_carries_the_skill_the_style_note_and_the_corpus_with_their_sha256(tmp_path, fresh_layout):
    skill = _skill()
    ctx, cpa, _ = _pick(tmp_path, [_answer(GOOD)])
    prompt = cpa.title_prompts[0]
    for path in (skill / "SKILL.md", skill / "references/examples.md", STYLE, CORPUS):
        assert _sha(path) in prompt, path
    # the skill's judge is its own template, sent as written; the record names the files it was built from
    judge = cpa.prompts[cpa.calls.index("title-judge-r0-1")]
    template = (skill / "references/judge-prompt.md").read_text(encoding="utf-8").split("\n---\n", 1)[1]
    assert template.strip().splitlines()[0] in judge and "@@" not in judge
    chosen = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))["chosen"]
    assert {a["sha256"] for a in chosen["judge"]["authorities"]} == {_sha(skill / "SKILL.md"),
                                                                     _sha(skill / "references/judge-prompt.md")}
    for line in _section(skill / "SKILL.md", "## 四、", "## 五、") + _section(skill / "SKILL.md", "## 五、", "## 六、"):
        assert line in prompt                                      # the skill's title and cover-text sections, verbatim
    assert all(line in prompt for line in _section(STYLE, "## 标题", "## "))
    bodies = [str(r.get("title_body") or r.get("title") or "") for r in json.loads(CORPUS.read_text("utf-8"))["overrides"]]
    assert [b for b in bodies if b] and all(b in prompt for b in bodies if b)


def _projection_of(skill, output):
    subprocess.run([sys.executable, str(REPO / "scripts/build_title_editorial_method.py"),
                    "--skill-root", str(skill), "--output", str(output)],
                   cwd=REPO, check=True, capture_output=True, text=True)
    return output


def _skill_copy(original, destination, names=("SKILL.md", "references/examples.md", "references/judge-prompt.md")):
    for name in names:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original / name, target)
    return destination


def test_editing_the_skill_changes_what_the_prompt_carries_and_its_hash_once_the_projection_is_rebuilt(
        tmp_path, monkeypatch, fresh_layout):
    original = _skill()
    copy = _skill_copy(original, tmp_path / "skill")
    text = (copy / "SKILL.md").read_text(encoding="utf-8")
    marker = "TASK-25 守卫：这一行只在测试的 skill 副本里"
    i = text.index("## 五、")
    (copy / "SKILL.md").write_text(text[:i] + marker + "\n\n" + text[i:], encoding="utf-8")
    monkeypatch.setattr(title_editorial_method, "TITLE_EDITORIAL_METHOD", _projection_of(copy, tmp_path / "method.json"))
    _, cpa, _ = _pick(tmp_path / "clip", [_answer(GOOD)])
    assert marker in cpa.title_prompts[0] and _sha(copy / "SKILL.md") in cpa.title_prompts[0]
    assert _sha(original / "SKILL.md") not in cpa.title_prompts[0]


def test_fresh_skill_source_reaches_manual_and_ordinary_offline_requests(tmp_path, monkeypatch, fresh_layout):
    original = _skill()
    skill = _skill_copy(original, tmp_path / "skill")
    skill_file = skill / "SKILL.md"
    text = skill_file.read_text(encoding="utf-8")
    marker = "TASK-69 新编辑成员：从同一来源进入两个请求"
    text = text.replace("## 五、", marker + "\n\n## 五、", 1)
    skill_file.write_text(text, encoding="utf-8")
    example = "TASK-69 新示例成员：只作为写法参照"
    with (skill / "references/examples.md").open("a", encoding="utf-8") as out:
        out.write("\n" + example + "\n")

    monkeypatch.setattr(title_editorial_method, "TITLE_EDITORIAL_METHOD", _projection_of(skill, tmp_path / "method.json"))
    _, cpa, _ = _pick(tmp_path / "clip", [_answer(GOOD)])
    ordinary = title_policy.build_automatic_talk_title_prompt(
        selection_hook="", transcript_sample="", important_ip_prompt="", clip_context_prompt="",
        persona_asset="", style_asset="", final_transcript=srt(3),
    )
    sent = [ordinary]  # Offline fake transport.
    for prompt in (cpa.title_prompts[0], sent[0]):
        assert marker in prompt and example in prompt
        assert _sha(skill_file) in prompt and _sha(skill / "references/examples.md") in prompt


def test_the_title_step_reads_the_skill_text_from_the_projection_not_the_skill_directory(tmp_path, monkeypatch,
                                                                                           fresh_layout):
    """On a machine without the skill directory (the remote host) every skill text of the title step still comes, and
    is the same, as the projection's: title_method, cover_text_rules, review_rules, judge_prompt."""
    monkeypatch.setenv("MANUAL_LANE_COVER_SKILL_SCRIPTS", str(tmp_path / "no-such-skill" / "scripts"))
    layout_mod.layout.cache_clear()
    assert not (tmp_path / "no-such-skill").exists()
    method = title_editorial_method.load_title_editorial_method()
    authority = title_authority
    assert authority.title_method().text.count(title_editorial_method.format_title_editorial_method(method)) == 1
    rules, rules_source = authority.cover_text_rules()
    assert rules == title_editorial_method.cover_text(method) and rules.startswith("## 五、")
    assert method.sections.endswith(rules) and rules_source.sha256 == method.sources[0].sha256
    review, review_sources = authority.review_rules()
    assert method.sections in review and method.judge_rules in review and method.sources[0].sha256 in review
    assert [s.sha256 for s in review_sources] == [method.sources[0].sha256]
    judge, judge_sources = authority.judge_prompt("1 0:00.00 [李豆沙] 句子", "【李豆沙】标题", ["封面一", "封面二"])
    assert method.judge_rules in judge and "@@" not in judge and "标题：【李豆沙】标题" in judge
    assert judge.startswith(method.judge_template.split("@@")[0])
    assert [s.sha256 for s in judge_sources] == [method.sources[0].sha256, method.judge_source.sha256]
    ctx, cpa, _ = _pick(tmp_path / "clip", [_answer(GOOD)])
    chosen = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))
    assert {a["sha256"] for a in chosen["authorities"]} >= {s.sha256 for s in method.sources}
    assert method.sections in cpa.title_prompts[0]


@pytest.mark.parametrize("broken", ["missing", "malformed"])
def test_a_missing_or_broken_projection_stops_the_title_step_before_any_provider_call(tmp_path, monkeypatch, broken):
    path = tmp_path / "method.json"
    if broken == "malformed":
        path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(title_editorial_method, "TITLE_EDITORIAL_METHOD", path)
    ctx, cpa, out = _step6(tmp_path / "clip", [_answer(GOOD)])
    assert not out.ok and out.reason == "TITLE_AUTHORITY_MISSING" and not cpa.prompts


def test_the_lanes_contrary_rules_are_not_in_the_prompt(tmp_path, fresh_layout):
    _skill()
    _, cpa, _ = _pick(tmp_path, [_answer(GOOD)])
    for rule in CONTRARY:
        assert rule not in cpa.title_prompts[0]


def test_the_prompt_shows_who_says_each_line(tmp_path, fresh_layout):
    _skill()
    speaker = "".join(f"{i}\n{si.fmt_ts((i - 1) * 2.0)} --> {si.fmt_ts(i * 2.0)}\n[{who}] 句子{i}\n\n"
                      for i, who in ((1, "李豆沙"), (2, "连线"), (3, "李豆沙")))
    _, cpa, _ = _pick(tmp_path, [_answer(GOOD)], speaker=speaker)
    assert "[雾深Girimi] 句子2" in cpa.title_prompts[0] and "[李豆沙] 句子1" in cpa.title_prompts[0]


# ----- the skill's judge accepts the title; its reasons go back to the editor ----------------------------------
def test_a_title_the_judge_fails_is_rewritten_with_the_judges_reasons(tmp_path, fresh_layout):
    _skill()
    reason = "引号里的话字幕里没有：编造了原话"
    ctx, cpa, picked = _pick(tmp_path, [_answer(GOOD), _answer("【李豆沙】她半分钟后破防了还嘴硬")],
                             judge=[judge_fail(reason), JUDGE_PASS])
    assert not isinstance(picked, StepOutcome), picked
    assert "title-regen-1" in cpa.calls and reason in cpa.prompts[cpa.calls.index("title-regen-1")]
    assert (ctx.clip_dir / "title/title.txt").read_text(encoding="utf-8").strip() == "【李豆沙】她半分钟后破防了还嘴硬"


def test_a_quote_the_subtitles_do_not_have_is_not_taken(tmp_path, fresh_layout):
    _skill()
    made_up = "【李豆沙】她半分钟后喊出“我从来没说过这句话”"
    ctx, _, _ = _pick(tmp_path, [_answer(made_up, GOOD)])
    assert (ctx.clip_dir / "title/title.txt").read_text(encoding="utf-8").strip() == GOOD


# ----- step 9: the review reads the same rules and its title passes the same judge -----------------------------
def _review(title=None):
    return json.dumps({"verdict": "FAIL" if title else "PASS", "findings": [], "holds": [], "rechoose": [],
                       "fixes": {"subtitles": [], "title": title, "tags": None}, "review_md": "复审"},
                      ensure_ascii=False)


def _package(tmp_path, monkeypatch, answer, *, judge=()):
    cpa = FakeCpa([answer], judge=judge)
    ctx = make_ctx(tmp_path, remote=FakeRemote(on_script=lambda n, b, r: RemoteResult(0, "", "")), cpa=cpa,
                   session=SESSION)
    c = ctx.clip_dir
    for rel, body in (("subs/final.srt", srt(3)), ("subs/doubts.json", "[]"), ("title/title.txt", "【李豆沙】标题\n"),
                      ("tags/tags.json", json.dumps({"final_tags": ["李豆沙"]})), ("pkg/final.srt", srt(3)),
                      ("pkg/title.txt", "【李豆沙】标题\n"), ("pkg/description.txt", "简介\n"),
                      ("pkg/tags.json", json.dumps({"final_tags": ["李豆沙"]})), ("asr/bcut.srt", srt(3))):
        (c / rel).parent.mkdir(parents=True, exist_ok=True)
        (c / rel).write_text(body, encoding="utf-8")
    sp.package_dir(ctx).mkdir(parents=True, exist_ok=True)
    (sp.package_dir(ctx) / "findings.md").write_text("F", encoding="utf-8")
    monkeypatch.setattr(sp, "_fmr", lambda *a: subprocess.CompletedProcess(a, 0, "", ""))
    return ctx, cpa


def test_the_review_prompt_carries_the_skills_title_rules_and_hash(tmp_path, monkeypatch, fresh_layout):
    skill = _skill()
    ctx, cpa = _package(tmp_path, monkeypatch, _review())
    sp.step9_review(ctx)
    assert _sha(skill / "SKILL.md") in cpa.prompts[0]
    assert all(line in cpa.prompts[0] for line in _section(skill / "SKILL.md", "## 四、", "## 五、"))


def test_a_review_title_the_judge_fails_is_not_adopted(tmp_path, monkeypatch, fresh_layout):
    _skill()
    ctx, cpa = _package(tmp_path, monkeypatch, _review(GOOD), judge=[judge_fail("笑点不在李豆沙身上")])
    out = sp.step9_review(ctx)
    assert (ctx.clip_dir / "title/title.txt").read_text(encoding="utf-8").strip() == "【李豆沙】标题"
    assert "title" not in out.rewind and any("笑点不在李豆沙身上" in line for line in out.process)


@pytest.mark.parametrize("module", ["step6_title", "title_authority", "stage_package"])
def test_the_title_modules_import_on_their_own(module):
    # step6_title once imported subtitle_track at the top, which imports stage_impl, which re-exports step6_title
    import sys
    proc = subprocess.run([sys.executable, "-c", f"import src.autoslice.manual_lane.{module}"], cwd=REPO,
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr[-400:]
