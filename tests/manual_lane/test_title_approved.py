"""When the channel owner has picked a clip's title body himself (`approved_title_body` and `approved_evidence` on the
clip's row of title_owner_verdicts), step 6 uses that body: the prompt says so, the candidate that has it goes first with
no ranking call, a round without it is rewritten, rounds used up without it fail, and the pick is never replaced by
another title even when it fails the checks or the judge.  Every case runs on fakes with a temporary asset."""

from __future__ import annotations

import json

import pytest
from fakes import JUDGE_PASS, FakeCpa, FakeRemote, judge_fail, make_ctx, srt

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import title_verdicts
from src.autoslice.manual_lane.runner import StepOutcome
from src.autoslice.title_editorial_method import AuthorityError

APPROVED = "她半分钟后真的破防了"
OTHERS = ["这一段她越讲越心虚了", "她把自己的台词全部拆台了"]
EVIDENCE = "就这个标题，别改"
OWN_BODY = "自己这片评过的候选标题甲"
OWN_COMMENT = "这片的原话乙丙"


def _cand(body, *, check_ok=True):
    return {"title_body": body, "on_stage": ["李豆沙"], "punchline_person": "她越长大越讨厌看那种有规矩的",
            "swap_check": {"holds_with_other_host": not check_ok, "reason": "换主播不成立"}}


def _answer(*cands, recommended=0):
    return json.dumps({"candidates": list(cands), "recommended": recommended, "cover_lines": ["破防了", "半分钟"]},
                      ensure_ascii=False)


def _asset(tmp_path, monkeypatch, *, approved=True, **extra):
    """The real asset plus a row for the clip under test (cid `clip`)."""
    top = json.loads(title_verdicts.VERDICTS.read_text(encoding="utf-8"))
    row = {"no": 99, "cid": "clip", "main_line": "本片主线", "punchline": "本片笑点", "owner_comment": OWN_COMMENT,
           "candidates": [{"letter": "A", "title_body": OWN_BODY, "grade": 1, "label": "还可以"},
                          {"letter": "B", "title_body": "自己这片评过的候选标题乙", "grade": -1, "label": "差"}]}
    if approved:
        row.update(approved_title_body=APPROVED, approved_evidence=EVIDENCE)
    top["clips"].append({**row, **extra})
    path = tmp_path / "verdicts.json"
    path.write_text(json.dumps(top, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(title_verdicts, "VERDICTS", path)


def _run(tmp_path, answers, **options):
    cpa = FakeCpa(answers, **options)
    ctx = make_ctx(tmp_path / "w", remote=FakeRemote(), cpa=cpa)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(srt(3), encoding="utf-8")
    return ctx, cpa, si.pick_title(ctx)


def _record(ctx):
    return json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))


SAYING = f"频道主已为本片选定标题正文：{APPROVED}。把它逐字作为第一条候选的 title_body，封面字按它写；其余候选照常写。"


# ----- the model writes it ------------------------------------------------------------------------------------------
def test_the_picked_body_goes_first_unranked_and_is_judged_and_kept(tmp_path, monkeypatch):
    _asset(tmp_path, monkeypatch)
    ctx, cpa, picked = _run(tmp_path, [_answer(_cand(OTHERS[0]), _cand(APPROVED), _cand(OTHERS[1]), recommended=0)])
    assert not isinstance(picked, StepOutcome), picked
    assert picked[0] == f"【李豆沙】{APPROVED}"
    assert not any(c.startswith("title-pick") for c in cpa.calls)
    judged = [p for c, p in zip(cpa.calls, cpa.prompts) if c == "title-judge-r0-1"]
    assert len(judged) == 1 and APPROVED in judged[0] and OTHERS[0] not in judged[0]
    record = _record(ctx)
    assert [c.get("owner_approved") for c in record["candidates"]] == [None, True, None]
    assert all("pick" not in c for c in record["candidates"]) and record["pick_calls"] == [None]
    assert record["owner_approved_title"] == {"title_body": APPROVED, "evidence": EVIDENCE}
    assert f"频道主选定标题正文：{APPROVED}（原话：{EVIDENCE}）" in picked[2]
    assert not any("未过检查" in line or "候选全部未通过" in line for line in picked[2])


def test_the_prompt_says_the_body_after_the_verdicts_and_keeps_this_clips_own_verdicts_out(tmp_path, monkeypatch):
    _asset(tmp_path, monkeypatch)
    _ctx, cpa, _picked = _run(tmp_path, [_answer(_cand(APPROVED), _cand(OTHERS[0]), _cand(OTHERS[1]))])
    prompt = cpa.title_prompts[0]
    heading = "## 频道主逐条评过的候选"
    assert prompt.count(SAYING) == 1 and prompt.index(heading) < prompt.index(SAYING) < prompt.index("## 本流水线的输入")
    for own in (OWN_BODY, "自己这片评过的候选标题乙", OWN_COMMENT, EVIDENCE):
        assert own not in prompt, own                      # the clip's own verdicts stay out of the taste texts


def test_the_body_is_compared_after_the_formal_name_case_fix(tmp_path, monkeypatch):
    approved = "雾深Girimi说她半分钟后真的破防了"
    _asset(tmp_path, monkeypatch, approved_title_body=approved)
    cpa = FakeCpa([_answer(_cand(OTHERS[0]), _cand("雾深girimi说她半分钟后真的破防了"), _cand(OTHERS[1]))])
    ctx = make_ctx(tmp_path / "w", remote=FakeRemote(), cpa=cpa)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n雾深Girimi你好\n\n2\n00:00:02,000 --> 00:00:04,000\n句子二\n", encoding="utf-8")
    picked = si.pick_title(ctx)
    assert not isinstance(picked, StepOutcome), picked
    assert [c.get("owner_approved") for c in _record(ctx)["candidates"]] == [None, True, None]


# ----- the model does not write it ----------------------------------------------------------------------------------
def test_a_round_without_the_body_is_rewritten_with_the_demand_and_a_later_round_can_have_it(tmp_path, monkeypatch):
    _asset(tmp_path, monkeypatch)
    ctx, cpa, picked = _run(tmp_path, [_answer(*map(_cand, OTHERS)), _answer(_cand(OTHERS[0]), _cand(APPROVED))])
    assert not isinstance(picked, StepOutcome), picked
    assert picked[0] == f"【李豆沙】{APPROVED}"
    assert not any(c.startswith("title-judge") and c.startswith("title-judge-r0") for c in cpa.calls)   # round 0: no judge
    assert not any(c.startswith("title-pick") for c in cpa.calls)
    assert f"第一条候选的 title_body 必须逐字是：{APPROVED}" in cpa.title_prompts[1]
    assert f"第一条候选的 title_body 必须逐字是：{APPROVED}" not in cpa.title_prompts[0]
    assert [c["round"] for c in _record(ctx)["candidates"] if c.get("owner_approved")] == [1]


def test_rounds_used_up_without_the_body_fail_and_never_fall_back_to_another_title(tmp_path, monkeypatch):
    _asset(tmp_path, monkeypatch)
    _ctx, cpa, picked = _run(tmp_path, [_answer(*(_cand(f"第{r}轮{b}") for b in OTHERS)) for r in range(3)])
    assert isinstance(picked, StepOutcome) and picked.ok is False and picked.reason == "TITLE_APPROVED_NOT_REPRODUCED"
    assert APPROVED in picked.note
    assert [c for c in cpa.calls if c.startswith("title")] == [
        "title-read-1", "title-read-2", "title-merge", "title", "title-regen-1", "title-regen-2"]
    assert all(f"第一条候选的 title_body 必须逐字是：{APPROVED}" in p for p in cpa.title_prompts[1:])


# ----- the picked one fails the checks or the judge ---------------------------------------------------------------
def test_a_picked_title_that_fails_the_checks_is_still_used_and_the_process_says_why(tmp_path, monkeypatch):
    _asset(tmp_path, monkeypatch)
    answers = [_answer(_cand(APPROVED, check_ok=False), _cand(f"第{r}轮{OTHERS[0]}"), _cand(OTHERS[1]))
               for r in range(3)]
    ctx, cpa, picked = _run(tmp_path, answers)
    assert not isinstance(picked, StepOutcome), picked
    assert picked[0] == f"【李豆沙】{APPROVED}"
    assert not any(c.startswith("title-judge") for c in cpa.calls)       # the other candidates were never tried
    assert _record(ctx)["forced"] is True
    line = next(line for line in picked[2] if line.startswith("频道主选定的标题未过检查："))
    assert "换主播自检未通过" in line and "未送判官" in line and "行政亲审前请处理" in line
    assert not any(line.startswith("候选全部未通过") for line in picked[2])


def test_a_picked_title_that_fails_the_judge_is_still_used_and_the_process_says_why(tmp_path, monkeypatch):
    _asset(tmp_path, monkeypatch)
    answers = [_answer(_cand(APPROVED), _cand(f"第{r}轮{OTHERS[0]}"), _cand(OTHERS[1])) for r in range(3)]
    _ctx, cpa, picked = _run(tmp_path, answers, judge=[judge_fail("事实不符")] * 3)
    assert not isinstance(picked, StepOutcome), picked
    assert picked[0] == f"【李豆沙】{APPROVED}"
    judged = [p for c, p in zip(cpa.calls, cpa.prompts) if c.startswith("title-judge") and c.endswith("-1")]
    assert judged and all(APPROVED in p and OTHERS[1] not in p for p in judged)    # only the picked title was judged
    assert any(line.startswith("频道主选定的标题未过检查：") and "事实不符" in line for line in picked[2])


def test_another_candidate_that_passes_everything_does_not_replace_the_picked_one(tmp_path, monkeypatch):
    _asset(tmp_path, monkeypatch)
    answers = [_answer(_cand(f"第{r}轮{OTHERS[0]}"), _cand(APPROVED, check_ok=False)) for r in range(3)]
    _ctx, cpa, picked = _run(tmp_path, answers, judge=[JUDGE_PASS] * 3)
    assert not isinstance(picked, StepOutcome) and picked[0] == f"【李豆沙】{APPROVED}"
    assert not any(c.startswith("title-judge") for c in cpa.calls)


# ----- nothing picked: as before -----------------------------------------------------------------------------------
def test_without_a_picked_body_nothing_changes(tmp_path, monkeypatch):
    _asset(tmp_path, monkeypatch, approved=False)
    ctx, cpa, picked = _run(tmp_path, [_answer(*map(_cand, [APPROVED, *OTHERS]))])
    assert not isinstance(picked, StepOutcome), picked
    assert "频道主已为本片选定标题正文" not in cpa.title_prompts[0]
    assert cpa.calls.count("title-pick-r0") == 1
    record = _record(ctx)
    assert record["owner_approved_title"] is None and all("owner_approved" not in c for c in record["candidates"])
    assert not any("频道主选定" in line for line in picked[2])


def test_a_body_picked_in_the_real_asset_is_one_he_accepted_with_his_words():
    # a picked body is never a candidate he graded below 还可以, and it always carries his words
    for clip in title_verdicts.load_verdicts().clips:
        if "approved_title_body" not in clip:
            continue
        assert clip["approved_evidence"].strip()
        same = [c for c in clip["candidates"] if c["title_body"].casefold() == clip["approved_title_body"].casefold()]
        assert all(c["grade"] is not None and c["grade"] >= 1 for c in same), clip["cid"]


# ----- the asset ---------------------------------------------------------------------------------------------------
def test_only_the_clip_and_its_trimmed_successor_have_a_picked_body_and_it_stays_out_of_the_taste_texts(tmp_path, monkeypatch):
    _asset(tmp_path, monkeypatch)
    verdicts = title_verdicts.load_verdicts()
    assert verdicts.approved("clip") == {"title_body": APPROVED, "evidence": EVIDENCE}
    assert verdicts.approved("clip_trim") == verdicts.approved("clip") and verdicts.approved("other") is None
    for text in (verdicts.liked_and_disliked("clip"), verdicts.corpus("clip")):
        assert OWN_BODY not in text and OWN_COMMENT not in text and EVIDENCE not in text
    assert OWN_BODY in verdicts.liked_and_disliked("elsewhere")


@pytest.mark.parametrize("extra", [{"approved_evidence": ""}, {"approved_title_body": " "},
                                   {"approved_title_body": 3}, {"approved_evidence": None}])
def test_a_picked_body_needs_both_fields_as_text(tmp_path, monkeypatch, extra):
    _asset(tmp_path, monkeypatch, **extra)
    with pytest.raises(AuthorityError, match="approved|approved_title_body|approved_evidence"):
        title_verdicts.load_verdicts()


def test_a_picked_body_without_his_words_is_refused(tmp_path, monkeypatch):
    _asset(tmp_path, monkeypatch, approved=False, approved_title_body=APPROVED)
    with pytest.raises(AuthorityError, match="approved_evidence"):
        title_verdicts.load_verdicts()
