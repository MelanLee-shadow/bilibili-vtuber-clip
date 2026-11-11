"""Step 6 works out what the clip is about before it writes the title (title_understanding), and gives the formal
spelling of the names the subtitles mention (title_names).  Every case runs the title half of step 6 on fakes: no model,
no network, and no skill directory (the title text comes from the deployed projection)."""

from __future__ import annotations

import json

import pytest
from fakes import MERGE, UNDERSTANDING_CALLS, FakeCpa, FakeRemote, make_ctx, srt

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import title_names, title_understanding
from src.autoslice.manual_lane.runner import StepOutcome

SESSION = {"recording_date": "2026-10-24", "speaker_mode": "uniform_host", "enablement_evidence": None,
           "participants": [["李豆沙"], ["雾深Girimi"]], "own_uploader_mids": [1]}
SOLO = dict(SESSION, participants=[["李豆沙"]])


def _cand(body, on_stage=("李豆沙",)):
    return {"title_body": body, "on_stage": list(on_stage), "punchline_person": "她越长大越讨厌看那种有规矩的",
            "swap_check": {"holds_with_other_host": False, "reason": "换主播不成立"}}


def _answer(*cands):
    return json.dumps({"candidates": list(cands), "recommended": 0, "cover_lines": ["破防了", "半分钟"]},
                      ensure_ascii=False)


def _pick(tmp_path, answers, *, session=SESSION, hook=None, **cpa_options):
    cpa = FakeCpa(answers, **cpa_options)
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa, session=session)
    if hook is not None:
        ctx.state.data["clip"]["hook"] = hook
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(srt(3), encoding="utf-8")
    return ctx, cpa, si.pick_title(ctx)


def _record(ctx):
    return json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))


# ----- the readings and the merge reach candidates.json, PROCESS and every title round --------------------------
@pytest.mark.parametrize("hook,hook_word", [("她嘴硬那一段", "成立"), ("", "无")])
def test_two_readings_and_a_merge_are_recorded_and_carried_by_every_title_round(tmp_path, hook, hook_word):
    bad = _cand("她半分钟后真的破防了", ("李豆沙",))
    bad["swap_check"]["holds_with_other_host"] = True            # round 0 fails its self-check, so round 1 is asked
    ctx, cpa, picked = _pick(tmp_path, [_answer(bad), _answer(_cand("她半分钟后真的破防了"))], hook=hook)
    assert not isinstance(picked, StepOutcome), picked
    assert cpa.calls[:4] == [*UNDERSTANDING_CALLS, "title"]
    read_1, read_2 = cpa.prompts[0], cpa.prompts[1]
    assert read_1 == read_2 and "1 0:00.00" in read_1 and "身份：B站虚拟主播" in read_1
    assert "## 封面表情/角色映射" not in read_1                      # the persona's cover section is not basic fact
    merge = cpa.prompts[cpa.calls.index("title-merge")]
    assert (hook or "（无）") in merge and "李豆沙讲了她的主线" in merge          # the readings are in the merge prompt
    found = _record(ctx)["understanding"]
    assert found["readings"] == [{"main_line": "李豆沙讲了她的主线", "punchline": "她先嘴硬，接着自己拆台（序号 3）"}] * 2
    assert found["merged"] == json.loads(MERGE) and found["hook"] == hook
    assert any(f"本片主线：李豆沙讲了她的主线（两份阅读一致；选片理由{hook_word}）" == line for line in picked[2])
    assert len(cpa.title_prompts) == 2
    for prompt in cpa.title_prompts:                              # the block closes the method text in each round
        block = prompt.split("## 本片主线（已由两次独立阅读核对；标题必须建在它上面）", 1)[1].split("## 本流水线的输入", 1)[0]
        assert block.index("- 主线：李豆沙讲了她的主线") < block.index("- 笑点：她先嘴硬，接着自己拆台（序号 3）")
        assert "- 指代：它＝她的小号\n" in block and "- 岔话题（不要拿来当标题主体）：顺带聊了晚饭\n" in block
        assert "- 人名、作品名用正式写法：李豆沙（不要写成：李豆莎）；雾深Girimi\n" in block
        assert "前缀里已经有的联动对象，正文不再写一遍" in block
        assert prompt.index("## 最近标题的称呼") < prompt.index("## 本片主线")


def test_a_reading_without_agreement_or_hook_verdict_is_reported_as_undecided(tmp_path):
    merge = json.loads(MERGE)
    del merge["agree"], merge["hook_holds"]
    _, _, picked = _pick(tmp_path, [_answer(_cand("她半分钟后真的破防了"))], merges=[json.dumps(merge)], hook="有理由")
    assert "本片主线：李豆沙讲了她的主线（两份阅读未判定；选片理由未判定）" in picked[2]


def test_who_is_who_and_side_topics_are_none_when_empty(tmp_path):
    merge = dict(json.loads(MERGE), who_is_who=[], side_topics=[])
    _, cpa, _ = _pick(tmp_path, [_answer(_cand("她半分钟后真的破防了"))], merges=[json.dumps(merge)])
    assert "- 指代：无\n" in cpa.title_prompts[0] and "不要拿来当标题主体）：无\n" in cpa.title_prompts[0]


# ----- the answers' contract: ask again, then say why ------------------------------------------------------------
def test_an_unusable_merge_is_asked_again_and_the_good_answer_is_used(tmp_path):
    bad = json.dumps(dict(json.loads(MERGE), who_is_who="它指小号"))
    ctx, cpa, picked = _pick(tmp_path, [_answer(_cand("她半分钟后真的破防了"))], merges=[bad, MERGE])
    assert not isinstance(picked, StepOutcome)
    assert [c for c in cpa.calls if c.startswith("title-merge")] == ["title-merge", "title-merge--reply-2"]
    assert _record(ctx)["understanding"]["merged"] == json.loads(MERGE)
    rejected = [c for c in picked[3] if c["call_id"] == "title-merge"][0]["rejected_answers"]
    assert [r["reason"] for r in rejected] == ["TITLE_UNDERSTANDING_SHAPE (who_is_who must be a list of objects with "
                                               "term and means)"]


@pytest.mark.parametrize("field,value", [("main_line", ""), ("punchline", "  "), ("main_line", 3),
                                         ("who_is_who", [{"term": "它"}]), ("side_topics", [1])])
def test_a_merge_that_never_meets_the_contract_stops_the_step_as_unreadable(tmp_path, field, value):
    bad = json.dumps(dict(json.loads(MERGE), **{field: value}))
    _, cpa, picked = _pick(tmp_path, [_answer(_cand("她半分钟后真的破防了"))], merges=[bad])
    assert isinstance(picked, StepOutcome) and picked.reason == "CPA_RESPONSE_UNPARSEABLE"
    assert "TITLE_UNDERSTANDING_SHAPE" in picked.note
    assert "title" not in cpa.calls                                # the title is never asked without a main line
    assert len([c for c in cpa.calls if c.startswith("title-merge")]) == 10     # CPA_TEXT_TRIES


def test_a_reading_that_is_not_an_object_with_both_fields_is_asked_again_then_unreadable(tmp_path):
    _, cpa, picked = _pick(tmp_path, [], readings=json.dumps({"main_line": "只有主线"}))
    assert isinstance(picked, StepOutcome) and picked.reason == "CPA_RESPONSE_UNPARSEABLE"
    assert "punchline missing" in picked.note and "title-merge" not in cpa.calls
    assert len([c for c in cpa.calls if c.startswith("title-read-1")]) == 10


def test_a_failed_understanding_call_is_a_failed_call(tmp_path):
    _, cpa, picked = _pick(tmp_path, [], merges=[None])
    assert isinstance(picked, StepOutcome) and picked.reason == "CPA_CALL_FAILED"
    assert [c["call_id"] for c in picked.cpa] == ["title-read-1", "title-read-2", "title-merge"]


def test_the_two_readings_are_two_cache_entries_of_one_prompt(tmp_path):
    ctx, cpa, _ = _pick(tmp_path, [_answer(_cand("她半分钟后真的破防了"))])
    files = sorted(p.name for p in (ctx.clip_dir.parent.parent / "cache" / "cpa").glob("*.json"))
    assert len(files) == len(set(files)) and len(files) >= 5      # read 1, read 2, merge, title, judge runs
    assert cpa.prompts[0] == cpa.prompts[1] and cpa.calls[:2] == ["title-read-1", "title-read-2"]


def test_a_missing_persona_stops_the_step_naming_it(tmp_path, monkeypatch):
    monkeypatch.setattr(title_understanding, "PERSONA", tmp_path / "no-persona.md")
    _, cpa, picked = _pick(tmp_path, [])
    assert isinstance(picked, StepOutcome) and picked.reason == "TITLE_AUTHORITY_MISSING"
    assert "persona.md" in picked.note and not cpa.calls


# ----- the channel's basic facts ----------------------------------------------------------------------------------
def test_basic_facts_are_the_persona_without_its_cover_section_and_keep_sections_after_it():
    text = ("# 标题\n\n- 身份：主播\n\n## 封面表情/角色映射（AI 封面 art-direction 用）\n\n- 开心：笑\n\n"
            "## 事实台账（自动生成）\n\n- 新接受的事实\n\n## 另一节\n\n- 尾巴\n")
    basics = title_understanding.persona_basics(text)
    assert "- 身份：主播" in basics and "封面表情" not in basics and "开心：笑" not in basics
    assert "## 事实台账（自动生成）\n\n- 新接受的事实" in basics and "## 另一节\n\n- 尾巴" in basics
    assert title_understanding.persona_basics("# 全文\n- 事实\n") == "# 全文\n- 事实"       # no such heading: the whole file
    assert title_understanding.persona_basics("- 前\n## 封面表情/角色映射\n- 只有这一节\n") == "- 前"   # section runs to the end


# ----- formal names ---------------------------------------------------------------------------------------------------
def test_a_title_that_differs_from_a_formal_name_only_in_case_is_corrected_and_recorded(tmp_path):
    ctx, cpa, picked = _pick(tmp_path, [_answer(_cand("雾深girimi一上来就认输", ("李豆沙", "雾深Girimi")))])
    assert not isinstance(picked, StepOutcome), picked
    chosen = _record(ctx)["chosen"]
    assert chosen["title"] == "【李豆沙×雾深Girimi】雾深Girimi一上来就认输"
    assert chosen["name_fixes"] == [{"written": "雾深girimi", "formal": "雾深Girimi"}]
    assert chosen["checks"]["ok"] and "canonical_names" not in chosen["checks"]["failed"]


def test_a_non_formal_spelling_fails_the_self_check_and_the_reason_goes_back_to_the_editor(tmp_path):
    ctx, cpa, picked = _pick(tmp_path, [_answer(_cand("李豆莎半分钟后真的破防了")),
                                        _answer(_cand("李豆沙半分钟后真的破防了"))])
    assert not isinstance(picked, StepOutcome), picked
    record = _record(ctx)
    first = record["candidates"][0]
    assert first["checks"]["failed"] == ["canonical_names"] and not first["checks"]["ok"]
    assert first["checks"]["reasons"] == ["名字写法不是正式写法：李豆莎 应写成 李豆沙"]
    assert first["checks"]["score"] == len(si.TITLE_RULES) - 1 == 10
    assert "李豆莎 应写成 李豆沙" in cpa.title_prompts[1]
    assert record["chosen"]["title"] == "【李豆沙】李豆沙半分钟后真的破防了" and record["rounds"] == ["title", "title-regen-1"]


def test_prefix_repeat_is_recorded_without_changing_the_verdict(tmp_path):
    repeated = _cand("雾深Girimi和她抢着看切片", ("李豆沙", "雾深Girimi"))
    once = _cand("她和人抢着看切片", ("李豆沙", "雾深Girimi"))
    solo = _cand("她半分钟后真的破防了")
    ctx, _, picked = _pick(tmp_path, [_answer(repeated, once, solo)])
    assert not isinstance(picked, StepOutcome), picked
    candidates = _record(ctx)["candidates"]
    assert [c["prefix_repeat"] for c in candidates] == [True, False, False]
    assert candidates[0]["checks"]["ok"] and candidates[0]["checks"]["failed"] == []     # recorded only


def test_canonical_names_come_from_the_table_entries_the_subtitles_mention_and_the_participants():
    names = title_names.canonical_names("今天聊了李豆莎和里豆沙，还有 kimo熊\n", ["李豆沙", "雾深Girimi", "陌生人"])
    text = names.prompt_text()
    assert "李豆沙（不要写成：李豆莎）" in text and "小李（不要写成：里豆沙）" in text
    assert text.endswith("雾深Girimi；陌生人") or "雾深Girimi；陌生人" in text
    assert title_names.canonical_names("什么都没有", []).prompt_text() == "按字幕里的写法"
    # a one-character canonical is never chosen by the subtitles
    assert all(len(e.canonical) > 1 for e in title_names.canonical_names("硬 摁", []).entries)


def test_case_fix_keeps_latin_words_and_part_of_a_formal_name_alone():
    names = title_names.CanonicalNames((title_names.NameEntry("Ado", ()), title_names.NameEntry("南町Nightin", ()),
                                        title_names.NameEntry("露蒂丝", ("露蒂丝酱", "蒂丝"))))
    assert names.fix_case("shadow 在唱 ADO 和 ado") == ("shadow 在唱 Ado 和 Ado", [
        {"written": "ADO", "formal": "Ado"}, {"written": "ado", "formal": "Ado"}])
    assert names.fix_case("南町nightin来了")[0] == "南町Nightin来了"
    assert names.fix_case("南町Nightin来了") == ("南町Nightin来了", [])
    assert [h["written"] for h in names.non_formal("露蒂丝酱来了")] == ["露蒂丝酱"]
    assert names.non_formal("露蒂丝来了") == []             # a part of the formal name inside the formal name is no hit
    assert [h["written"] for h in names.non_formal("蒂丝来了")] == ["蒂丝"]


def test_a_subtitle_code_for_a_person_is_not_a_title_spelling():
    # the table writes what she says as `xxsk`; a title that names 星汐 must not be told to write the code
    names = title_names.canonical_names("今天和星汐一起，xxsk 也来了\n", ["李豆沙"])
    assert "xxsk" not in names.prompt_text()
    assert names.non_formal("星汐来了") == []
