"""Step 6 puts each round's checked candidates in the channel owner's order of taste (title_pick) before the fact judge
sees any of them: the ranking, not the model's own recommendation, decides which candidate is judged first and which one
is kept; a candidate ending on the editor's own remark goes to the back; a first-ranked candidate predicted 差 is
rewritten, not judged; a ranking that cannot be had never fails the step.  Every case runs on fakes."""

from __future__ import annotations

import json

import pytest
from fakes import FakeCpa, FakeRemote, make_ctx, pick_answer, srt

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import title_pick, title_verdicts
from src.autoslice.manual_lane.runner import StepOutcome

BODIES = ["她半分钟后真的破防了", "这一段她越讲越心虚了", "她把自己的台词全部拆台了"]


def _cand(body, *, check_ok=True):
    return {"title_body": body, "on_stage": ["李豆沙"], "punchline_person": "她越长大越讨厌看那种有规矩的",
            "swap_check": {"holds_with_other_host": not check_ok, "reason": "换主播不成立"}}


def _answer(*cands, recommended=0):
    return json.dumps({"candidates": list(cands), "recommended": recommended, "cover_lines": ["破防了", "半分钟"]},
                      ensure_ascii=False)


def _run(tmp_path, answers, **options):
    cpa = FakeCpa(answers, **options)
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(srt(3), encoding="utf-8")
    return ctx, cpa, si.pick_title(ctx)


def _record(ctx):
    return json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))


def _judged_titles(cpa):
    """The title each judge call was asked about (the first run of each)."""
    return [p for c, p in zip(cpa.calls, cpa.prompts) if c.startswith("title-judge-") and c.endswith("-1")]


# ----- the ranking decides ------------------------------------------------------------------------------------------
def test_the_ranking_decides_which_candidate_is_judged_first_and_which_is_kept(tmp_path):
    ctx, cpa, picked = _run(tmp_path, [_answer(*map(_cand, BODIES), recommended=0)],
                            pick=[pick_answer([2, 0, 1], {2: "很不错", 0: "一般", 1: "差"})])
    assert not isinstance(picked, StepOutcome), picked
    assert picked[0] == f"【李豆沙】{BODIES[2]}"                       # the model recommended candidate 0
    judged = _judged_titles(cpa)
    assert len(judged) == 1 and BODIES[2] in judged[0] and BODIES[0] not in judged[0]
    assert cpa.calls.count("title-pick-r0") == 1
    record = _record(ctx)
    assert record["recommended"] == [0] and record["chosen"]["title_body"] == BODIES[2]
    assert [c["pick"]["rank"] for c in record["candidates"]] == [2, 3, 1]
    assert record["candidates"][2]["pick"] == {"rank": 1, "grade": "很不错", "why": "x", "editor_tail": False}
    assert record["pick_calls"] == [{"call_id": "title-pick-r0", "fell_back": False, "reason": "",
                                     "first": f"【李豆沙】{BODIES[2]}", "first_grade": "很不错", "filtered": 0}]
    assert "按频道主口味排序：第一 【李豆沙】" + BODIES[2] + "（预测 很不错）；筛掉自拟评语收尾 0 条（第 1 轮）" in picked[2]


def test_the_ranking_prompt_has_the_corpus_without_this_clip_the_main_line_and_the_numbered_candidates(tmp_path):
    clips = title_verdicts.load_verdicts().clips
    if not clips:
        pytest.skip("public export: title_owner_verdicts.v1.json carries no clips")
    clip = next(c for c in clips if "approved_title_body" not in c)     # a clip whose title he has not picked is ranked
    cpa = FakeCpa([_answer(*map(_cand, BODIES))])
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    ctx.state.data["clip"]["cid"] = clip["cid"]
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(srt(3), encoding="utf-8")
    assert not isinstance(si.pick_title(ctx), StepOutcome)
    prompt = cpa.prompts[cpa.calls.index("title-pick-r0")]
    assert "{" not in prompt.split("只输出一个 JSON 对象，不要任何其他文字：\n", 1)[0]        # every placeholder filled
    assert prompt.count("### 片 ") == 19 and clip["owner_comment"] not in prompt
    assert "这段讲的是：李豆沙讲了她的主线\n笑点：她先嘴硬，接着自己拆台（序号 3）\n" in prompt
    assert "候选（序号从 0 起）：\n" + "\n".join(f"{i} 【李豆沙】{b}" for i, b in enumerate(BODIES)) + "\n" in prompt


def test_the_ranking_is_asked_at_the_judge_tier():
    from src.autoslice.manual_lane.step_io import _effort_of

    assert _effort_of("title-pick")[0] == "high"


def test_an_order_with_repeats_missing_and_out_of_range_entries_is_mended(tmp_path):
    assert title_pick.normalized_order([2, 2, 9, "1", True, -1], 3) == [2, 0, 1]
    ctx, cpa, picked = _run(tmp_path, [_answer(*map(_cand, BODIES))], pick=[pick_answer([1, 1, 7])])
    assert not isinstance(picked, StepOutcome) and picked[0] == f"【李豆沙】{BODIES[1]}"
    assert [c["pick"]["rank"] for c in _record(ctx)["candidates"]] == [2, 1, 3]       # 1 first, then 0 and 2 as they were


def test_a_candidate_that_failed_its_checks_is_not_ranked_and_does_not_shift_the_numbers(tmp_path):
    cands = [_cand(BODIES[0], check_ok=False), _cand(BODIES[1]), _cand(BODIES[2])]
    ctx, cpa, picked = _run(tmp_path, [_answer(*cands, recommended=0)], pick=[pick_answer([1, 0])])
    assert not isinstance(picked, StepOutcome) and picked[0] == f"【李豆沙】{BODIES[2]}"
    prompt = cpa.prompts[cpa.calls.index("title-pick-r0")]
    assert f"0 【李豆沙】{BODIES[1]}\n1 【李豆沙】{BODIES[2]}\n" in prompt and BODIES[0] not in prompt
    assert [("pick" in c) for c in _record(ctx)["candidates"]] == [False, True, True]


# ----- nothing to rank ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("cands", [[_cand(BODIES[0])], [_cand(BODIES[0], check_ok=False), _cand(BODIES[1])]])
def test_a_single_passing_candidate_is_not_ranked(tmp_path, cands):
    ctx, cpa, picked = _run(tmp_path, [_answer(*cands)])
    assert not isinstance(picked, StepOutcome), picked
    assert not any(c.startswith("title-pick") for c in cpa.calls)
    record = _record(ctx)
    assert record["pick_calls"] == [None] and all("pick" not in c for c in record["candidates"])
    assert not any("按频道主口味排序" in line or "按口味排序未做成" in line for line in picked[2])


# ----- the editor's own tail ----------------------------------------------------------------------------------------
SUBTITLES = "我才不要听什么熊猫头说的话\n李姐你这个人真的很难搞\n结果她当场就破防了"


@pytest.mark.parametrize("body,expected", [
    ("她破防了，熊猫头太好笑了", True),                # nickname + the editor's own comment
    ("她破防了，李姐你太离谱了吧", True),
    ("她破防了，小李服了", True),
    ("她破防了，李姐你这个人真的很难搞", False),        # nickname + the clip's own words
    ("她破防了，我才不要听什么熊猫头说的话", False),
    ("熊猫头直接傻眼了，她破防了", False),             # the nickname is not in the last clause
    ("她破防了，直接傻眼了", False),                   # no nickname
    ("她破防了？小李！", True),                        # the last non-empty clause is 小李, and none of it was said
])
def test_the_editor_tail_filter(body, expected):
    assert title_pick.editor_tail(body, SUBTITLES) is expected, body


def test_a_clause_is_the_clips_when_at_least_half_its_characters_lie_in_pairs_the_subtitles_have():
    assert title_pick.editor_tail("好，小李服了", "他说小李真行") is False       # 小李 + 服了: 小, 李 covered = 2 of 4
    assert title_pick.editor_tail("好，小李服了吧", "他说小李真行") is True        # 2 of 5
    assert title_pick.editor_tail("好，小李", "") is True                          # nothing said, nothing covered


def test_the_filter_uses_the_last_non_empty_clause_and_only_letters_digits_and_han():
    assert title_pick.last_clause("她破防了，直接傻眼了？！") == "直接傻眼了"
    assert title_pick.editor_tail("她破防了，熊猫头！！！", "") is True
    assert title_pick.editor_tail("她破防了，熊猫头！！！", "什么熊猫头啊") is False        # it was said


def test_a_candidate_ending_on_the_editors_remark_goes_to_the_back_even_if_ranked_first(tmp_path):
    bodies = ["她半分钟后真的破防了，熊猫头傻眼了", BODIES[1], BODIES[2]]
    ctx, cpa, picked = _run(tmp_path, [_answer(*map(_cand, bodies), recommended=0)],
                            pick=[pick_answer([0, 2, 1], {0: "很不错", 2: "还可以", 1: "一般"})])
    assert not isinstance(picked, StepOutcome), picked
    assert picked[0] == f"【李豆沙】{bodies[2]}"
    record = _record(ctx)
    assert [(c["pick"]["rank"], c["pick"]["editor_tail"]) for c in record["candidates"]] == [(3, True), (2, False),
                                                                                           (1, False)]
    assert record["pick_calls"][0]["filtered"] == 1
    assert "按频道主口味排序：第一 【李豆沙】" + bodies[2] + "（预测 还可以）；筛掉自拟评语收尾 1 条（第 1 轮）" in picked[2]


def test_when_every_candidate_would_be_filtered_none_is_filtered(tmp_path):
    bodies = ["她破防了，熊猫头傻眼了", "她破防了，李姐你太离谱了吧", "她破防了，小李服了"]
    ctx, _cpa, picked = _run(tmp_path, [_answer(*map(_cand, bodies))], pick=[pick_answer([2, 0, 1])])
    assert not isinstance(picked, StepOutcome) and picked[0] == f"【李豆沙】{bodies[2]}"
    record = _record(ctx)
    assert all(c["pick"]["editor_tail"] is False for c in record["candidates"])
    assert record["pick_calls"][0]["filtered"] == 0


# ----- a first-ranked candidate predicted 差 ---------------------------------------------------------------------
@pytest.mark.parametrize("grade", ["差", "尤其差", "我估计他会觉得尤其差"])
def test_a_first_ranked_candidate_predicted_bad_is_rewritten_not_judged(tmp_path, grade):
    round_1 = ["她是真的不想再装了", "这一次她终于绷不住了", "她把话筒都快捏碎了"]
    ctx, cpa, picked = _run(
        tmp_path, [_answer(*map(_cand, BODIES)), _answer(*map(_cand, round_1))],
        pick=[pick_answer([1, 0, 2], {1: grade, 0: "差", 2: "一般"}, whys={1: "像他骂过的那种自拟评语"}),
              pick_answer([2, 0, 1], {2: "还可以"})])
    assert not isinstance(picked, StepOutcome), picked
    assert [c for c in cpa.calls if c.startswith(("title", "title-judge"))].count("title-judge-r0-1") == 0
    regen = cpa.title_prompts[1]
    assert f"候选 2（【李豆沙】{BODIES[1]}）：通过\n按频道主的口味预测：{grade} 像他骂过的那种自拟评语\n" in regen
    assert f"候选 1（【李豆沙】{BODIES[0]}）：通过\n按频道主的口味预测：差 x\n" in regen
    assert f"候选 3（【李豆沙】{BODIES[2]}）：通过\n按频道主的口味预测：一般 x" in regen
    assert picked[0] == f"【李豆沙】{round_1[2]}"
    assert len(_judged_titles(cpa)) == 1 and round_1[2] in _judged_titles(cpa)[0]
    assert [c["round"] for c in _record(ctx)["candidates"]] == [0, 0, 0, 1, 1, 1]


def test_rewrites_used_up_keep_the_best_ranked_of_the_candidates_that_can_be_used(tmp_path):
    rounds = [[f"第{r}轮的{b}" for b in BODIES] for r in range(3)]
    ctx, cpa, picked = _run(tmp_path, [_answer(*map(_cand, bodies)) for bodies in rounds],
                            pick=[pick_answer([2, 0, 1], {2: "差"}) for _ in range(3)])
    assert not isinstance(picked, StepOutcome), picked
    assert not any(c.startswith("title-judge") for c in cpa.calls)
    assert picked[0] == f"【李豆沙】{rounds[0][2]}"                    # the first round's best ranked
    assert _record(ctx)["forced"] is True
    assert any("候选全部未通过" in line and "未送判官" in line for line in picked[2])


# ----- a ranking that cannot be had ---------------------------------------------------------------------------------
@pytest.mark.parametrize("answers,reason", [
    ([None], "CPA_HTTP_ERROR"),
    (["not json at all"], "TITLE_PICK_UNPARSEABLE"),
    ([json.dumps({"order": [0, 1, 2]})] * 10, "TITLE_PICK_SHAPE"),
    ([json.dumps({"order": "0,1,2", "predicted": []})] * 10, "TITLE_PICK_SHAPE"),
    ([json.dumps({"order": [0, 1, 2], "predicted": [{"index": 0, "grade": "x", "why": "y"}] * 3})] * 10,
     "TITLE_PICK_SHAPE"),
    ([json.dumps({"order": [0, 1, 2], "predicted": [{"index": i, "grade": "", "why": ""} for i in range(3)]})] * 10,
     "TITLE_PICK_SHAPE"),
])
def test_a_ranking_that_fails_or_never_fits_falls_back_to_the_models_recommendation_without_failing_the_step(
        tmp_path, answers, reason):
    ctx, cpa, picked = _run(tmp_path, [_answer(*map(_cand, BODIES), recommended=1)], pick=answers)
    assert not isinstance(picked, StepOutcome), picked
    assert picked[0] == f"【李豆沙】{BODIES[1]}"                          # the model's own recommendation
    call = _record(ctx)["pick_calls"][0]
    assert call["fell_back"] is True and reason in call["reason"] and call["call_id"] == "title-pick-r0"
    assert any(line.startswith("按口味排序未做成（") and reason in line and "本轮按模型自己的推荐" in line
               for line in picked[2])
    assert all("pick" not in c for c in _record(ctx)["candidates"])
    assert len(_judged_titles(cpa)) == 1 and BODIES[1] in _judged_titles(cpa)[0]


def test_a_bad_answer_is_asked_again_and_the_good_one_after_it_is_used(tmp_path):
    ctx, cpa, picked = _run(tmp_path, [_answer(*map(_cand, BODIES))],
                            pick=[json.dumps({"order": [0]}), pick_answer([2, 1, 0])])
    assert not isinstance(picked, StepOutcome) and picked[0] == f"【李豆沙】{BODIES[2]}"
    assert [c for c in cpa.calls if c.startswith("title-pick")] == ["title-pick-r0", "title-pick-r0--reply-2"]
    assert _record(ctx)["pick_calls"][0]["fell_back"] is False


def test_a_default_fake_ranking_keeps_the_original_order_and_predicts_ok_for_all(tmp_path):
    ctx, cpa, picked = _run(tmp_path, [_answer(*map(_cand, BODIES), recommended=2)])
    assert not isinstance(picked, StepOutcome)
    assert [c["pick"] for c in _record(ctx)["candidates"]] == [
        {"rank": r, "grade": "还可以", "why": "x", "editor_tail": False} for r in (1, 2, 3)]
    assert picked[0] == f"【李豆沙】{BODIES[0]}"                          # the ranking, not recommended=2, decides


def test_the_ranking_call_is_logged_among_the_cpa_calls_with_its_tier(tmp_path):
    _ctx, _cpa, picked = _run(tmp_path, [_answer(*map(_cand, BODIES))])
    rows = [c for c in picked[3] if c["call_id"].startswith("title-pick")]
    assert [(c["call_id"], c["effort"]) for c in rows] == [("title-pick-r0", "high")]
