"""Step 6 works out what the clip is about before it writes the title (title_understanding), and gives the formal
spelling of the names the subtitles mention (title_names).  Every case runs the title half of step 6 on fakes: no model,
no network, and no skill directory (the title text comes from the deployed projection)."""

from __future__ import annotations

import json

import pytest
from fakes import LANDINGS, MERGE, READING, UNDERSTANDING_CALLS, FakeCpa, FakeRemote, make_ctx, srt

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import channel_facts, title_names, title_understanding
from src.autoslice.manual_lane.runner import StepOutcome

SESSION = {"recording_date": "2026-10-24", "speaker_mode": "uniform_host", "enablement_evidence": None,
           "participants": [["李豆沙"], ["雾深Girimi"]], "own_uploader_mids": [1]}
SOLO = dict(SESSION, participants=[["李豆沙"]])


def _cand(body, on_stage=("李豆沙",)):
    return {"title_body": body, "on_stage": list(on_stage), "landing": 1}


LANDINGS_LINE = ("1. [李豆沙] “我才不买呢，顶多买一点”（第 3 句）——她先嘴硬，接着自己拆台〔选片点出〕；"
                 "2. [李豆沙] “那你知道我在直播还给我打电话”（第 5 句）——她反过来问对方")


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
    bad = _cand("她半分钟后真的破防了", ("路人甲",))               # round 0 fails a check (not a participant): round 1 is asked
    ctx, cpa, picked = _pick(tmp_path, [_answer(bad), _answer(_cand("她半分钟后真的破防了"))], hook=hook)
    assert not isinstance(picked, StepOutcome), picked
    assert cpa.calls[:4] == [*UNDERSTANDING_CALLS, "title"]
    read_1, read_2 = cpa.prompts[0], cpa.prompts[1]
    assert read_1 == read_2 and "1 0:00.00" in read_1 and "身份：B站虚拟主播" in read_1
    assert "## 封面表情/角色映射" not in read_1                      # the persona's cover section is not basic fact
    merge = cpa.prompts[cpa.calls.index("title-merge")]
    assert (hook or "（无）") in merge and "李豆沙讲了她的主线" in merge          # the readings are in the merge prompt
    found = _record(ctx)["understanding"]
    assert found["readings"] == [{"main_line": "李豆沙讲了她的主线", "landings": LANDINGS, "punchline": LANDINGS_LINE,
                                  "host_role": "主角"}] * 2
    assert found["merged"] == dict(json.loads(MERGE), punchline=LANDINGS_LINE) and found["hook"] == hook
    assert found["selection"] == {"hook": hook, "reason": "", "peaks": []}
    for prompt in (read_1, merge):                                # both see what the selection wrote
        assert ("- 一句话：" + hook if hook else "选片时写下的“为什么选这段”：（无）") in prompt
    assert any(f"本片主线：李豆沙讲了她的主线（两份阅读一致；选片理由{hook_word}）" == line for line in picked[2])
    assert len(cpa.title_prompts) == 2
    for prompt in cpa.title_prompts:                              # the block closes the method text in each round
        block = prompt.split("## 本片主线和落点（已由两次独立阅读核对；标题必须建在它上面）", 1)[1].split("## 本流水线的输入", 1)[0]
        assert block.index("李豆沙讲了她的主线") < block.index("  1. [李豆沙] “我才不买呢，顶多买一点”（第 3 句）——她先嘴硬，接着自己拆台〔选片点出〕\n")
        assert "  2. [李豆沙] “那你知道我在直播还给我打电话”（第 5 句）——她反过来问对方\n" in block
        assert "每处落点至少写 2 条" in block
        assert "- 指代：它＝她的小号\n" in block and "- 和主线无关的岔话题（不要拿来当标题主体）：顺带聊了晚饭\n" in block
        assert "- 人名、作品名的写法（括号里是不要写成的错字）：李豆沙（不要写成：李豆莎）；雾深Girimi\n" in block
        assert "前缀里已经有全名的联动对象，正文里不再写全名" in block
        assert ("- 一句话：" + hook if hook else "选片时写下的“为什么选这段”：（无）") in block     # the writer sees it too
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
    assert _record(ctx)["understanding"]["merged"] == dict(json.loads(MERGE), punchline=LANDINGS_LINE)
    rejected = [c for c in picked[3] if c["call_id"] == "title-merge"][0]["rejected_answers"]
    assert [r["reason"] for r in rejected] == ["TITLE_UNDERSTANDING_SHAPE (who_is_who must be a list of objects with "
                                               "term and means)"]


@pytest.mark.parametrize("field,value", [("main_line", ""), ("landings", []), ("main_line", 3),
                                         ("landings", [{"quote": "", "who": "", "cues": [], "why": ""}]),
                                         ("landings", [dict(LANDINGS[0], cues=["3"])]),
                                         ("who_is_who", [{"term": "它"}]), ("side_topics", [1])])
def test_a_merge_that_never_meets_the_contract_stops_the_step_as_unreadable(tmp_path, field, value):
    bad = json.dumps(dict(json.loads(MERGE), **{field: value}))
    _, cpa, picked = _pick(tmp_path, [_answer(_cand("她半分钟后真的破防了"))], merges=[bad])
    assert isinstance(picked, StepOutcome) and picked.reason == "CPA_RESPONSE_UNPARSEABLE"
    assert "TITLE_UNDERSTANDING_SHAPE" in picked.note
    assert "title" not in cpa.calls                                # the title is never asked without a main line
    assert len([c for c in cpa.calls if c.startswith("title-merge")]) == 10     # CPA_TEXT_TRIES


def test_more_landings_than_the_cap_keep_the_first_ones_and_the_merge_names_the_rest(tmp_path):
    """11-23 1493180: the selection had marked six beats; ten merges listing all six were rejected and the clip
    stopped.  The landings come most important first, so the first LANDINGS_MAX are kept and the rest are named."""
    six = [dict(LANDINGS[0], quote=f"第{n}下", cues=[n]) for n in range(1, 7)]
    many = json.dumps(dict(json.loads(MERGE), landings=six), ensure_ascii=False)
    reading = json.dumps(dict(json.loads(READING), landings=six), ensure_ascii=False)
    cands = [dict(_cand(body), landing=n) for n in range(1, 5) for body in ("她半分钟后真的破防了", "她半分钟后破防了")]
    ctx, cpa, picked = _pick(tmp_path, [_answer(*cands)], merges=[many], readings=reading)
    assert not isinstance(picked, StepOutcome), picked
    assert [c for c in cpa.calls if c.startswith("title-merge")] == ["title-merge"]     # not asked again
    found = _record(ctx)["understanding"]
    assert [x["quote"] for x in found["merged"]["landings"]] == ["第1下", "第2下", "第3下", "第4下"]
    assert all(len(r["landings"]) == title_understanding.LANDINGS_MAX for r in found["readings"])
    assert [c for c in found["merged"]["conflicts"] if "第5下" in c or "第6下" in c] == found["merged"]["conflicts"][-2:]
    assert "第5下" not in cpa.title_prompts[0]


def test_a_reading_that_is_not_an_object_with_both_fields_is_asked_again_then_unreadable(tmp_path):
    _, cpa, picked = _pick(tmp_path, [], readings=json.dumps({"main_line": "只有主线"}))
    assert isinstance(picked, StepOutcome) and picked.reason == "CPA_RESPONSE_UNPARSEABLE"
    assert "landings must be one or more objects" in picked.note and "title-merge" not in cpa.calls
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



def test_both_readings_and_the_merge_carry_the_selection_tiers_and_pick_the_highest_one(tmp_path):
    """Owner the title should stay on the channel's core attraction (a clear 撒娇, her stance on 百合,
    a relationship pull), even a short one late in the clip; the tiers are the selection metric's own section."""
    ctx, cpa, picked = _pick(tmp_path, [_answer(_cand("她半分钟后真的破防了"))], hook="她嘴硬那一段")
    assert not isinstance(picked, StepOutcome), picked
    tiers = channel_facts.core_attractions()
    merge = cpa.prompts[cpa.calls.index("title-merge")]
    for prompt in (cpa.prompts[0], merge):
        assert tiers in prompt and "标题落点" in prompt and "哪怕它只占一小段" in prompt
    assert "它必须属于主线" not in cpa.prompts[0]
    for prompt in (cpa.prompts[0], merge):          # a group of landings; the selection's moments first within a tier
        assert "同一层里，选片点出的在前" in prompt and '"landings": [{"quote"' in prompt
    assert "不放这里，放 landings" in cpa.prompts[0] and "不归到 side_topics" in merge
    assert "同一层看点里以它为准" in cpa.prompts[cpa.calls.index("title")]


def test_a_metric_without_the_tier_section_stops_the_step_naming_it(tmp_path, monkeypatch):
    bare = tmp_path / "metric.md"
    bare.write_text("# metric\n\n## 七维量化表\n- 只有打分\n", encoding="utf-8")
    monkeypatch.setattr(channel_facts, "METRIC", bare)
    _, cpa, picked = _pick(tmp_path, [])
    assert isinstance(picked, StepOutcome) and picked.reason == "TITLE_AUTHORITY_MISSING"
    assert "metric.md" in picked.note and not cpa.calls

# ----- how she figures in the main line: a clip about others stops before any title --------------------------------
def _roles(read_1, read_2, merged, why="她只说了 4、16 两句，主线靠礼墨Sumi撑着"):
    readings = {run: json.dumps(dict(json.loads(READING), host_role=role), ensure_ascii=False)
                for run, role in ((1, read_1), (2, read_2))}
    merge = json.dumps(dict(json.loads(MERGE), host_role=merged, host_role_why=why), ensure_ascii=False)
    return {"readings": readings, "merges": [merge]}


@pytest.mark.parametrize("read_1,read_2", [("旁观", "旁观"), ("旁观", "参与"), ("参与", "旁观")])
def test_a_clip_whose_main_line_is_other_peoples_business_stops_before_any_title(tmp_path, read_1, read_2):
    """11-23 625460: 礼墨Sumi grabbing stones and melting down, 李豆沙 three lines; three title rounds all failed the
    swap test and the step stopped TITLE_NOT_ACCEPTED.  selection is about the host herself."""
    from src.autoslice.manual_lane import auto_run

    ctx, cpa, picked = _pick(tmp_path, [_answer(_cand("她半分钟后真的破防了"))], **_roles(read_1, read_2, "旁观"))
    assert isinstance(picked, StepOutcome) and picked.reason == "NOT_ABOUT_HOST", picked
    assert cpa.calls == UNDERSTANDING_CALLS                       # no title round, no judge, no context lookup
    assert not (ctx.clip_dir / "title/title.txt").exists() and not (ctx.clip_dir / "title/accepted.json").exists()
    assert picked.note == picked.process[0]
    # no speaker.srt: a clip with no speakers at all, every line hers
    assert picked.note.startswith(f"本片主线不围绕李豆沙：她在主线里是「旁观」（两份阅读：{read_1}、{read_2}；合并：旁观；"
                                  "她的句子 3/3）")
    assert "她只说了 4、16 两句，主线靠礼墨Sumi撑着" in picked.note and "这条不发" in picked.note
    # every call answered: auto_run does not read the stop as waiting for the model, so it is listed as stuck
    ctx.state.data["steps"]["title_cover"] = {"status": "STOPPED", "reason": picked.reason, "cpa": picked.cpa}
    assert not auto_run.provider_failed(ctx.state, "title_cover")


@pytest.mark.parametrize("read_1,read_2,merged", [
    ("主角", "主角", "主角"),      # a reaction clip: her reactions to the video are the main line
    ("参与", "参与", "参与"),      # a collab exchange she is one side of
    ("主角", "参与", "参与"),
    ("旁观", "参与", "参与"),      # the merge settled against the one reading that said 旁观
    ("参与", "参与", "旁观"),      # a merge overruling two readings that agree she is in the main line does not stop
])
def test_a_clip_she_leads_or_takes_part_in_goes_on_to_the_title(tmp_path, read_1, read_2, merged):
    ctx, cpa, picked = _pick(tmp_path, [_answer(_cand("她半分钟后真的破防了"))], **_roles(read_1, read_2, merged))
    assert not isinstance(picked, StepOutcome), picked
    assert "title" in cpa.calls and (ctx.clip_dir / "title/title.txt").is_file()
    found = _record(ctx)["understanding"]
    assert [r["host_role"] for r in found["readings"]] == [read_1, read_2] and found["merged"]["host_role"] == merged


def test_a_revision_of_a_published_clip_is_not_stopped_by_the_reading(tmp_path):
    """The clip is already out: its subtitle revision keeps going (the online title stays)."""
    before = tmp_path / "clips" / "published"
    (before / "upload").mkdir(parents=True)
    (before / "upload/online.json").write_text('{"bvid": "BV1", "title": "【李豆沙】线上的标题"}', encoding="utf-8")
    cpa = FakeCpa([_answer(_cand("她半分钟后真的破防了"))] * 2, **_roles("旁观", "旁观", "旁观"))
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa, session=SESSION)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(srt(3), encoding="utf-8")
    ctx.state.data["revised_from"] = {"cid": "published", "reason": "字幕", "at": "t"}
    picked = si.pick_title(ctx)
    assert not isinstance(picked, StepOutcome), picked
    assert picked[0] == "【李豆沙】线上的标题"


def _speakers(ctx, *labels):
    from src.autoslice.manual_lane import subtitle_track

    cues = subtitle_track.parse_blocks(srt(len(labels)))
    subtitle_track.write_track(ctx.clip_dir, [dict(c, speaker=s) for c, s in zip(cues, labels, strict=True)],
                               speakers=True)


def _pick_labelled(tmp_path, labels, *, clip=None, **cpa_options):
    cpa = FakeCpa([_answer(_cand("她半分钟后真的破防了"))], **cpa_options)
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa, session=SESSION)
    ctx.state.data["clip"].update(clip or {})
    _speakers(ctx, *labels)
    return ctx, cpa, si.pick_title(ctx)


def test_the_stop_says_how_many_of_the_lines_step_4_gave_her(tmp_path):
    _, _, picked = _pick_labelled(tmp_path, ["连线", "李豆沙", "连线"], **_roles("旁观", "旁观", "旁观"))
    assert isinstance(picked, StepOutcome) and picked.reason == "NOT_ABOUT_HOST", picked
    assert "合并：旁观；她的句子 1/3）" in picked.note


def test_no_line_labelled_hers_is_step_4s_problem_not_a_verdict_on_the_clip(tmp_path):
    """In her own stream not one line hers: the reading only followed the labels, so the stop sends the operator to
    step 4 (NO_HOST_LINES) instead of calling the clip not about her."""
    _, cpa, picked = _pick_labelled(tmp_path, ["连线", "连线", "连线"], **_roles("旁观", "参与", "旁观"))
    assert isinstance(picked, StepOutcome) and picked.reason == "NO_HOST_LINES", picked
    assert "0/3 句" in picked.note and "先查第 4 步的说话人" in picked.note and picked.note == picked.process[0]
    assert cpa.calls == UNDERSTANDING_CALLS


def _selection(tmp_path, cid, **entry):
    path = tmp_path / "discover" / f"{cid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"input_pool": [dict(cid=cid, **entry)]}, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_a_clip_seated_for_the_owners_mark_in_the_chat_goes_on_to_the_title(tmp_path):
    """He marked that stretch in the chat to have it cut (owner_marks): what it is about is his call."""
    sel = _selection(tmp_path, "clip", owner_mark={"uid": 1, "text": "标记", "at_ms": 12_000})
    ctx, cpa, picked = _pick_labelled(tmp_path, ["连线", "李豆沙", "连线"], clip={"selection_file": sel},
                                      **_roles("旁观", "旁观", "旁观"))
    assert not isinstance(picked, StepOutcome), picked
    assert "title" in cpa.calls and (ctx.clip_dir / "title/title.txt").is_file()


def test_a_selected_clip_without_the_owners_mark_is_still_stopped(tmp_path):
    sel = _selection(tmp_path, "clip", hook="别人的事")
    _, _, picked = _pick_labelled(tmp_path, ["连线", "李豆沙", "连线"], clip={"selection_file": sel},
                                  **_roles("旁观", "旁观", "旁观"))
    assert isinstance(picked, StepOutcome) and picked.reason == "NOT_ABOUT_HOST", picked


def test_a_title_the_owner_chose_or_a_draw_he_named_is_not_stopped(tmp_path):
    from src.autoslice.manual_lane import step6_title, subtitle_track

    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=FakeCpa([]), session=SESSION)
    _speakers(ctx, "连线", "连线", "李豆沙")
    found = {"readings": [{"host_role": "旁观"}, {"host_role": "旁观"}],
             "merged": {"host_role": "旁观", "host_role_why": "", "main_line": "别人的事"}}
    track = subtitle_track.read_track(ctx.clip_dir)
    assert step6_title._host_stop(ctx, found, track, owner_chose=True, cpa_calls=[]) is None
    stop = step6_title._host_stop(ctx, found, track, owner_chose=False, cpa_calls=[])
    assert stop is not None and stop.reason == "NOT_ABOUT_HOST" and "她的句子 1/3" in stop.note


def test_host_lines_counts_her_label_and_reads_an_unlabelled_clip_as_all_hers():
    assert title_understanding.host_lines([None, None]) == (2, 2)
    assert title_understanding.host_lines(["李豆沙", "连线", None]) == (1, 3)
    assert title_understanding.host_lines(["连线", "连线"]) == (0, 2)
    assert title_understanding.host_lines([]) == (0, 0)


def test_the_owners_mark_is_read_from_the_selection_file_the_clip_was_made_from(tmp_path):
    from src.autoslice.manual_lane import clip_init
    from src.autoslice.manual_lane.state import StateError

    marked = _selection(tmp_path, "semantictalk_1_2", owner_mark={"uid": 1})
    assert clip_init.owner_marked({"cid": "semantictalk_1_2_11231200", "selection_file": marked})   # lane cid suffix
    assert not clip_init.owner_marked({"cid": "x"})                                                   # made by hand
    assert not clip_init.owner_marked({"cid": "y", "selection_file": _selection(tmp_path, "y", hook="h")})
    with pytest.raises(StateError, match="not found"):
        clip_init.owner_marked({"cid": "z", "selection_file": marked})


@pytest.mark.parametrize("role", [None, "", "路人", "旁观者"])
def test_a_reading_without_a_known_host_role_is_asked_again_then_unreadable(tmp_path, role):
    reading = dict(json.loads(READING))
    if role is None:
        del reading["host_role"]
    else:
        reading["host_role"] = role
    _, cpa, picked = _pick(tmp_path, [], readings=json.dumps(reading, ensure_ascii=False))
    assert isinstance(picked, StepOutcome) and picked.reason == "CPA_RESPONSE_UNPARSEABLE"
    assert "host_role must be one of 主角/参与/旁观" in picked.note and "title-merge" not in cpa.calls


def test_both_prompts_ask_where_she_stands_and_say_a_reaction_is_hers(tmp_path):
    _, cpa, _ = _pick(tmp_path, [_answer(_cand("她半分钟后真的破防了"))])
    for prompt in (cpa.prompts[0], cpa.prompts[cpa.calls.index("title-merge")]):
        assert '"host_role": "主角/参与/旁观 三选一"' in prompt and "她的反应就是主线" in prompt
        assert "光接话、回应别人的求助不算参与" in prompt and "只读她的句子，看得出这段在讲什么吗" in prompt
        assert "真分不清参与还是旁观时选" in prompt and "李豆沙在" in prompt and "{host}" not in prompt


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
    assert first["checks"]["score"] == len(si.TITLE_RULES) - 1
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



# ----- what the selection wrote reaches the reading, the merge and the title prompt ------------------------------------
def test_the_selections_reason_and_peaks_reach_all_three_prompts_and_the_record(tmp_path):
    """标题没落在最关键的那一下，就是流水线的问题.  11-24 1254550: the selection wrote
    「必须保留她先答应每天来、随即改口」 and only its 80-character hook reached one of the step's calls."""
    peaks = [{"quote": "哦，不对，你不能每天都来", "who": "", "at_ms": 1_281_000, "why": "刚答应就改口"}]
    cpa = FakeCpa([_answer(_cand("她半分钟后真的破防了"))])
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa, session=SESSION)
    ctx.state.data["clip"].update(hook="坚持轮流配对", selection_reason="必须保留她先答应每天来、随即改口", peaks=peaks)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(srt(3), encoding="utf-8")
    picked = si.pick_title(ctx)
    assert not isinstance(picked, StepOutcome), picked
    for call in ("title-read-1", "title-merge", "title"):
        prompt = cpa.prompts[cpa.calls.index(call)]
        assert "- 一句话：坚持轮流配对\n- 看点说明：必须保留她先答应每天来、随即改口\n" in prompt, call
        assert "  1. “哦，不对，你不能每天都来”——刚答应就改口" in prompt and "事实以字幕为准" in prompt
    assert _record(ctx)["understanding"]["selection"] == {"hook": "坚持轮流配对", "reason": "必须保留她先答应每天来、随即改口",
                                                          "peaks": peaks}


def test_a_state_made_before_the_reason_was_stored_reads_it_from_its_selection_file(tmp_path):
    from src.autoslice.manual_lane import clip_init

    entry = {"start_ms": 0, "end_ms": 1000, "source": {"path": "/r/a.flv"}, "hook": "一句话", "peaks": [{"quote": "q", "who": "", "at_ms": 5, "why": "w"}],
             "selection_scorecard": {"start_cue": 1, "end_cue": 2, "candidate_quality": {"reason": "最细的那段"}}}
    sel = _selection(tmp_path, "semantictalk_1_2", **entry)
    clip = clip_init.clip_from_entry(clip_init.selection_entry(tmp_path / "discover/semantictalk_1_2.json", "semantictalk_1_2"),
                                     cid="semantictalk_1_2", recording_date="2026-11-24", bcut_srt=tmp_path / "b.srt",
                                     bcut_source_zero_ms=0, remote_root="/w", source_dir=None,
                                     selection_file=tmp_path / "discover/semantictalk_1_2.json")
    assert clip["selection_reason"] == "最细的那段" and clip["peaks"] == entry["peaks"]
    assert clip_init.selection_notes(clip) == {"hook": "一句话", "reason": "最细的那段", "peaks": entry["peaks"]}
    old = {"cid": "semantictalk_1_2", "hook": "一句话", "selection_file": sel}             # no selection_reason key
    assert clip_init.selection_notes(old) == {"hook": "一句话", "reason": "最细的那段", "peaks": []}
    # a revision (revise_run copies the clip, `selection_file` too) and a revision of a trimmed clip: the file names the
    # clip they were made from (11-24: semantictalk_583860_806500_11232100_r4 on the host)
    for successor in ("semantictalk_1_2_11231200_r4", "semantictalk_1_2_trim_r2"):
        assert clip_init.selection_notes(dict(old, cid=successor))["reason"] == "最细的那段"
    from src.autoslice.manual_lane.state import StateError
    with pytest.raises(StateError, match="gone"):              # as owner_marked: a file that is gone is an error
        clip_init.selection_notes(dict(old, selection_file=str(tmp_path / "discover/gone.json")))
    with pytest.raises(StateError, match="not found"):
        clip_init.selection_notes(dict(old, cid="semantictalk_9_9_r2"))
    assert clip_init.selection_notes({"cid": "x", "hook": "h"}) == {"hook": "h", "reason": "", "peaks": []}   # made by hand
    bare = clip_init.clip_from_entry({"cid": "c", "start_ms": 0, "end_ms": 1, "segment_path": "/r/a.flv", "cue_ids": [1, 2]},
                                     cid="c", recording_date="d", bcut_srt=tmp_path / "b.srt", bcut_source_zero_ms=0,
                                     remote_root="/w", source_dir=None, selection_file=tmp_path / "discover/semantictalk_1_2.json")
    assert bare["selection_reason"] == "" and "peaks" not in bare


def test_the_owners_draw_replaces_the_landings(tmp_path):
    assert title_understanding.owner_landing("LLBZ 会被误解成李李不直") == [
        {"quote": "", "who": "", "cues": [], "why": "LLBZ 会被误解成李李不直", "owner": True}]
    assert title_understanding.landings_line(title_understanding.owner_landing("他说的看点")) == "1. 他说的看点〔频道主说的看点〕"
