"""The two judges of a title (step6_title.title_judge, skill 八.3 and 八.4) after the writing judge's two
runs that differ are settled by a third, and the fact judge's verdict on the cover lines is apart from the title's — a
cover text it refuses is rewritten and the title kept.  Fakes only: no model, no network."""

from __future__ import annotations

import json

from fakes import (JUDGE_PASS, QUALITY_PASS, FakeCpa, FakeRemote, accept_title, cover_fail, judge_fail, make_ctx,
                   quality_fail, srt)

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import step6_title, title_acceptance
from src.autoslice.manual_lane.runner import StepOutcome


def _answer(body="她半分钟后真的破防了"):
    return json.dumps({"candidates": [{"title_body": body, "on_stage": ["李豆沙"], "landing": 1}], "recommended": 0,
                       "cover_lines": ["破防了", "半分钟"]}, ensure_ascii=False)


class _PerRun(FakeCpa):
    """The writing judge's runs answer by their number (`-q1`, `-q2`, `-q3`), not one verdict for the call."""

    def __init__(self, responses, runs, **options):
        super().__init__(responses, **options)
        self.runs = runs

    def _quality_answer(self, call_id):
        return self.runs[call_id.rsplit("-q", 1)[1]]


def _pick(tmp_path, cpa):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(srt(3), encoding="utf-8")
    return ctx, si.pick_title(ctx)


def _receipt(ctx):
    return json.loads((ctx.clip_dir / "title/accepted.json").read_text(encoding="utf-8"))


# ----- the writing judge: two runs, a third when they differ -----------------------------------------------------------
def test_two_writing_runs_that_differ_are_settled_by_a_third(tmp_path):
    """11-24 1254550: the candidate on the selection's own moment (「哦，不对」「我也要跟礼墨单独播」) passed the facts
    twice and the writing judge once of twice; both-must-pass threw it out and a flatter line became the title."""
    cpa = _PerRun([_answer()], {"1": quality_fail("流水账"), "2": QUALITY_PASS, "3": QUALITY_PASS})
    ctx, picked = _pick(tmp_path, cpa)
    assert not isinstance(picked, StepOutcome), picked
    assert [c for c in cpa.calls if "title-judge" in c] == ["title-judge-r0-1", "title-judge-r0-2", "title-judge-r0-q1",
                                                            "title-judge-r0-q2", "title-judge-r0-q3"]
    judge = _receipt(ctx)["judge"]
    assert judge["overall"] == "PASS" and judge["fail_reasons"] == []
    assert [r["overall"] for r in judge["quality"]] == ["FAIL", "PASS", "PASS"]
    assert title_acceptance.problem(ctx.clip_dir) == ""


def test_a_third_run_that_fails_keeps_the_title_out(tmp_path):
    cpa = _PerRun([_answer()] * 3, {"1": QUALITY_PASS, "2": quality_fail("流水账"), "3": quality_fail("流水账")})
    _ctx, picked = _pick(tmp_path, cpa)
    assert isinstance(picked, StepOutcome) and picked.reason == "TITLE_NOT_ACCEPTED" and "流水账" in picked.note


def test_two_writing_runs_that_agree_ask_no_third(tmp_path):
    cpa = FakeCpa([_answer()])
    _ctx, picked = _pick(tmp_path, cpa)
    assert not isinstance(picked, StepOutcome) and not any(c.endswith("-q3") for c in cpa.calls)
    failing = FakeCpa([_answer()] * 3, quality=[quality_fail()] * 3)
    _ctx, picked = _pick(tmp_path / "b", failing)
    assert isinstance(picked, StepOutcome) and not any(c.endswith("-q3") for c in failing.calls)


def test_the_receipt_counts_the_majority_of_the_writing_runs():
    ok, bad = {"overall": "PASS"}, {"overall": "FAIL"}
    assert title_acceptance.writing_passed([ok, ok]) and title_acceptance.writing_passed([bad, ok, ok])
    assert not title_acceptance.writing_passed([ok, bad]) and not title_acceptance.writing_passed([ok, bad, bad])
    assert not title_acceptance.writing_passed([]) and not title_acceptance.writing_passed(["PASS", "PASS"])


# ----- the cover text is judged apart from the title --------------------------------------------------------------------
def test_a_cover_text_the_fact_judge_refuses_is_rewritten_and_the_title_kept(tmp_path):
    """11-24 298620: the candidate ranked first sat on the selection's own moment; the fact judge faulted the cover line
    「说好唱淡蓝色」 and the title went with it."""
    fixed = json.dumps({"cover_lines": ["她嘴硬了", "顶多买点"]}, ensure_ascii=False)
    cpa = FakeCpa([_answer(), fixed], judge=[cover_fail("封面第二行不是她说的"), JUDGE_PASS])
    ctx, picked = _pick(tmp_path, cpa)
    assert not isinstance(picked, StepOutcome), picked
    title, cover_lines, process = picked[0], picked[1], picked[2]
    assert title == "【李豆沙】她半分钟后真的破防了" and cover_lines == ["她嘴硬了", "顶多买点"]
    assert [c for c in cpa.calls if c.startswith("title") and "judge" not in c and "read" not in c and "merge" not in c] == ["title"]
    fix = cpa.prompts[cpa.calls.index("cover-text-fix-1")]
    assert "【李豆沙】她半分钟后真的破防了" in fix and "- 封面第二行不是她说的" in fix and "当前封面文字：破防了 / 半分钟" in fix
    assert "## 五、" in fix and "1 0:00.00" in fix                      # the skill's cover-text section and the subtitles
    assert "title-judge-cover1-1" in cpa.calls and not any(c.startswith("title-judge-cover1-q") for c in cpa.calls)
    judged = cpa.prompts[cpa.calls.index("title-judge-cover1-1")]
    assert "第一行「她嘴硬了」，第二行「顶多买点」" in judged
    assert any(line.startswith("封面文字重写（第 1 次，标题不变）：她嘴硬了 / 顶多买点；原因：封面第二行不是她说的") for line in process)
    receipt = _receipt(ctx)["judge"]
    assert receipt["overall"] == "PASS" and receipt["cover"] == {"overall": "PASS", "fail_reasons": []}
    assert json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))["cover_lines"] == ["她嘴硬了", "顶多买点"]
    assert title_acceptance.problem(ctx.clip_dir) == ""


def test_a_cover_text_that_stays_refused_stops_the_step_and_names_the_title(tmp_path):
    first, second = (json.dumps({"cover_lines": lines}, ensure_ascii=False) for lines in (["她嘴硬了", "顶多买点"], ["又嘴硬了", "买一点"]))
    cpa = FakeCpa([_answer(), first, second], judge=[cover_fail("封面字 a"), cover_fail("封面字 b"), cover_fail("封面字 c")])
    ctx, picked = _pick(tmp_path, cpa)
    assert isinstance(picked, StepOutcome) and picked.reason == "COVER_TEXT_NOT_ACCEPTED", picked
    assert "【李豆沙】她半分钟后真的破防了" in picked.note and "重写 2 次仍不过：封面字 c" in picked.note
    assert [c for c in cpa.calls if c.startswith("cover-text-fix")] == ["cover-text-fix-1", "cover-text-fix-2"]
    assert not (ctx.clip_dir / "title/title.txt").exists()            # nothing goes out with a cover text refused


def test_a_title_the_fact_judge_refuses_is_still_refused(tmp_path):
    cpa = FakeCpa([_answer()] * 3, judge=[judge_fail("说话人写错")] * 3)
    _ctx, picked = _pick(tmp_path, cpa)
    assert isinstance(picked, StepOutcome) and picked.reason == "TITLE_NOT_ACCEPTED" and "说话人写错" in picked.note
    assert not any(c.startswith("cover-text-fix") for c in cpa.calls)


def test_a_caller_that_cannot_rewrite_the_cover_needs_both_verdicts(tmp_path):
    """The review's rewrite and the re-judging before a package go out with the cover as it is."""
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=FakeCpa([], judge=[cover_fail("合读有歧义")]))
    judged = step6_title.title_judge(ctx, "title-judge-x", "1 0:00.00 一句", "【李豆沙】她半分钟后真的破防了", ["甲", "乙"], [])
    assert judged["overall"] == "PASS" and judged["cover"] == {"overall": "FAIL", "fail_reasons": ["合读有歧义"]}
    assert step6_title.judged_whole(judged) == ["封面字：合读有歧义"]
    accept_title(ctx.clip_dir, "【李豆沙】她半分钟后真的破防了")
    receipt = _receipt(ctx)
    receipt["judge"]["cover"] = judged["cover"]
    (ctx.clip_dir / "title/title.txt").write_text("【李豆沙】她半分钟后真的破防了\n", encoding="utf-8")
    (ctx.clip_dir / "title/accepted.json").write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
    assert "封面字没过事实判官" in title_acceptance.problem(ctx.clip_dir)


def test_a_fact_answer_without_both_verdicts_fails_both():
    from src.autoslice.manual_lane import title_authority

    old_shape = title_authority.judge_verdict({"overall": "PASS", "fail_reasons": []})
    assert old_shape["overall"] == "FAIL" and old_shape["cover"]["overall"] == "FAIL"
    both = title_authority.judge_verdict(json.loads(cover_fail("x")))
    assert both == {"overall": "PASS", "fail_reasons": [], "cover": {"overall": "FAIL", "fail_reasons": ["x"]}}


def test_a_cover_text_written_for_another_landing_than_the_titles_is_rewritten(tmp_path):
    """A round is written on several landings and carries one cover text, the recommended candidate's.  When the
    ranking takes a title on another landing, the cover text is rewritten for it."""
    from fakes import pick_answer

    def answer(recommended):
        return json.dumps({"candidates": [{"title_body": "她半分钟后真的破防了", "on_stage": ["李豆沙"], "landing": 1},
                                          {"title_body": "这一段她越讲越心虚了", "on_stage": ["李豆沙"], "landing": 2}],
                           "recommended": recommended, "cover_lines": ["越讲越心虚", "她心虚了"]}, ensure_ascii=False)
    fixed = json.dumps({"cover_lines": ["破防了", "半分钟"]}, ensure_ascii=False)
    cpa = FakeCpa([answer(1), fixed], pick=[pick_answer([0, 1])])
    ctx, picked = _pick(tmp_path, cpa)
    assert not isinstance(picked, StepOutcome), picked
    assert picked[0] == "【李豆沙】她半分钟后真的破防了" and picked[1] == ["破防了", "半分钟"]
    assert "封面字是按第 2 处落点写的，采用的标题落在第 1 处" in cpa.prompts[cpa.calls.index("cover-text-fix-1")]
    same = FakeCpa([answer(0)], pick=[pick_answer([0, 1])])           # the recommended one is taken: nothing is rewritten
    _ctx, picked = _pick(tmp_path / "same", same)
    assert picked[1] == ["越讲越心虚", "她心虚了"] and not any(c.startswith("cover-text-fix") for c in same.calls)


# ----- the landings of a round (landing_coverage): the selection's moment is written and judged first ---------------------
# fakes.LANDINGS: landing 1 is the one the selection pointed at (`picked`), landing 2 the readers' own.
def _on(*landings, recommended=0):
    bodies = ["她半分钟后真的破防了", "这一段她越讲越心虚了", "她嘴硬说顶多买一点", "她反问那你知道我在直播"]
    return json.dumps({"candidates": [{"title_body": bodies[i], "on_stage": ["李豆沙"], "landing": n}
                                      for i, n in enumerate(landings)],
                       "recommended": recommended, "cover_lines": ["破防了", "半分钟"]}, ensure_ascii=False)


def test_a_candidate_on_the_landing_the_selection_pointed_at_is_judged_before_the_taste_order(tmp_path):
    """11-24 1254550: the moment the selection pointed at was written but ranked below other landings and never judged."""
    from fakes import pick_answer

    cpa = FakeCpa([_on(2, 2, 1, recommended=2)], pick=[pick_answer([0, 1, 2])])     # by taste the landing-2 ones come first
    ctx, picked = _pick(tmp_path, cpa)
    assert not isinstance(picked, StepOutcome), picked
    assert picked[0] == "【李豆沙】她嘴硬说顶多买一点"                    # the only one on landing 1, judged first
    saved = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))
    assert saved["coverage"] == [{"counts": [1, 2], "stray": [], "picked": [1], "short": [1], "picked_missing": []}]
    assert "落点覆盖（第 1 轮）：1〔选片点出〕×1、2×2；送判官的落点 [1]；第 1 处落点只写了 1 条（每处至少 2 条）" in picked[2]


def test_a_round_that_wrote_nothing_on_the_pointed_landing_is_rewritten_unjudged(tmp_path):
    from fakes import pick_answer

    cpa = FakeCpa([_on(2, 2), _on(1, 1, 2, 2)], pick=[pick_answer([0, 1]), pick_answer([0, 1, 2, 3])])
    ctx, picked = _pick(tmp_path, cpa)
    assert not isinstance(picked, StepOutcome), picked
    assert not any(c.startswith("title-judge-r0") for c in cpa.calls)     # round 0 went to no judge
    regen = cpa.prompts[cpa.calls.index("title-regen-1")]
    assert "标〔选片点出〕的第 1 处落点一条候选也没写" in regen
    assert picked[0] == "【李豆沙】她半分钟后真的破防了"
    assert "送判官的落点 无" in next(line for line in picked[2] if line.startswith("落点覆盖（第 1 轮）"))


def test_landing_coverage_counts_each_landing_and_names_the_stray_ones():
    from fakes import LANDINGS

    cands = [{"landing": 2}, {"landing": 7}, {"landing": True}, {}]
    assert step6_title.landing_coverage(cands, LANDINGS) == {
        "counts": [0, 1], "stray": [1, 2, 3], "picked": [1], "short": [1, 2], "picked_missing": [1]}
    # a writer that named no landing at all: noted, the round is not rewritten for it (as _other_landing takes it)
    assert step6_title.landing_coverage([{}, {}], LANDINGS)["picked_missing"] == []
    assert step6_title.on_picked_landing_first([0, 1, 2], [{"landing": 2}, {"landing": 1}, {"landing": 1}],
                                               step6_title.landing_coverage([], LANDINGS)) == [1, 2, 0]


# ----- a revision keeping the published title (online_title_breaks) ---------------------------------------------------
# The lines of the 625460 trio clip as the owner labelled them (a guest line carries the guest label only, as
# speaker.srt has it).
TRIO = ("李豆沙", "礼墨Sumi", "安晚awa")
CLIP_625460 = "\n\n".join(f"{i}\n00:00:{i:02d},000 --> 00:00:{i + 1:02d},000\n[{spk}] {text}" for i, (spk, text) in enumerate(
    (("连线", "我勾不到，我这个，快点帮我勾一下。"), ("李豆沙", "你要勾哪？"), ("连线", "哦，勾到了。我要笑。")), 1)) + "\n"


def test_a_revision_does_not_keep_a_published_title_its_subtitles_show_wrong(monkeypatch):
    """Step 6 of a revision re-reads the title online against the revision's subtitles and speakers (625460 kept a
    title quoting two people as one after his labels were imported): a quotation that is not one person's words breaks
    it before any judge is asked."""
    monkeypatch.setattr(step6_title, "title_inputs", lambda ctx, names: ([], "", CLIP_625460))
    asked = []

    def judge(ctx, call_id, lines, title, cover, cpa_calls, *, facts=True):
        asked.append((call_id, title, facts))
        return {"overall": "PASS", "fail_reasons": [], "runs": [], "quality": [{"overall": "PASS", "fail_reasons": []}],
                "cover": {"overall": "PASS", "fail_reasons": []}, "authorities": []}

    monkeypatch.setattr(step6_title, "title_judge", judge)
    online = "【李豆沙×安晚awa×礼墨Sumi】队友嫌游戏石头没什么好抢的，却喊帮忙勾，“你要勾哪？哦，勾到了。我要笑。”"
    breaks, verdict = step6_title.online_title_breaks(object(), TRIO, online, [])
    assert any("引号核对未过" in b for b in breaks)
    assert verdict is None and asked == []                  # wrong on the subtitles already: no judge is asked
    guessed = "【李豆沙×安晚awa×礼墨Sumi】礼墨喊“快点帮我勾一下”，勾到了又说“我要笑”"
    breaks, verdict = step6_title.online_title_breaks(object(), TRIO, guessed, [])   # one name on a 连线 line: a guess
    assert any("TITLE_QUOTE_SPEAKER_UNVERIFIED" in b for b in breaks) and asked == []
    kept = "【李豆沙×安晚awa×礼墨Sumi】队友喊“快点帮我勾一下”，勾到了又说“我要笑”"
    breaks, verdict = step6_title.online_title_breaks(object(), TRIO, kept, [])
    assert breaks == [] and verdict["overall"] == "PASS"
    assert asked == [("title-judge-online", kept, False)]   # the writing judge alone (skill 八.4)


def test_a_revision_does_not_keep_a_published_title_the_writing_judge_fails(monkeypatch):
    """Owner (U471): 897000's revision kept 「挖矿找不到暂停决定退出，…玩爽了」 — it held on the subtitles,
    so the old recheck (subtitle rules only) kept it; section 四 of the skill rejects it (a 流水账, no mishap)."""
    monkeypatch.setattr(step6_title, "title_inputs", lambda ctx, names: ([], "", CLIP_625460))
    why = "写法（第四节）：流水账：挖矿找不到暂停决定退出"
    monkeypatch.setattr(step6_title, "title_judge", lambda *a, **k: {
        "overall": "FAIL", "fail_reasons": [why], "runs": [], "quality": [{"overall": "FAIL", "fail_reasons": [why]}],
        "cover": {"overall": "PASS", "fail_reasons": []}, "authorities": []})
    kept = "【李豆沙×安晚awa×礼墨Sumi】队友喊“快点帮我勾一下”，勾到了又说“我要笑”"
    breaks, verdict = step6_title.online_title_breaks(object(), TRIO, kept, [])
    assert breaks == ["线上标题不合 skill 第四节：" + why] and verdict["overall"] == "FAIL"


# ----- one title, one voice (review_findings) ---------------------------------------------------------------
def _person_switch_line():
    from src.autoslice.title_editorial_method import load_title_editorial_method

    template = load_title_editorial_method().quality_template
    return template, next(line for line in template.splitlines() if "person_switch：" in line)


def test_her_name_in_the_narration_and_a_first_person_word_fail_the_writing_judge():
    """「李豆沙续费Oopz后，我那个高贵的会员标志…」 went online — the narration names her and the same title says
    我, two people in one title (维护者 11-20).  Since 11-24 no mechanical self-check decides it (60-title): the channel's
    writing judge is asked it as a banned style (`person_switch`, its template says what that is), a title it names
    there fails, and an answer that leaves it unjudged fails too."""
    from src.autoslice.manual_lane import title_authority

    template, line = _person_switch_line()
    assert "person_switch" in title_authority.quality_shape(template).banned
    assert "旁白叫了她的名字" in line and "引号外又出现“我”" in line
    answer = json.loads(QUALITY_PASS)
    answer["banned"]["person_switch"] = "旁白叫“李豆沙”，引号外又有“我”"
    verdict = title_authority.quality_verdict(answer)
    assert verdict == {"overall": "FAIL", "fail_reasons": ["半路换人称：旁白叫“李豆沙”，引号外又有“我”"]}
    del answer["banned"]["person_switch"]
    assert title_authority.quality_verdict(answer)["overall"] == "FAIL"


def test_her_own_words_naming_herself_are_no_switch_of_person():
    """The control: her own words that name her (「不可以在跟李豆沙说话的时候夸礼小虎好可爱！」, all hers) are no switch,
    and a writing judge that finds no switch lets the title pass."""
    from src.autoslice.manual_lane import title_authority

    _, line = _person_switch_line()
    assert "她自己的话里叫自己名字" in line and "不算" in line
    assert title_authority.quality_verdict(json.loads(QUALITY_PASS)) == {"overall": "PASS", "fail_reasons": []}


# ----- the cover lines under every title that goes out (_settled_cover_text) -------------------------------------------
def _judge(cover_overall="PASS", *reasons):
    return {"overall": "PASS", "fail_reasons": [], "runs": [], "quality": [{"overall": "PASS", "fail_reasons": []}],
            "cover": {"overall": cover_overall, "fail_reasons": list(reasons)}, "authorities": []}


def _settle(tmp_path, monkeypatch, chosen, *, kept_online=False, imported=False):
    from src.autoslice.manual_lane import cover_import

    asked = []

    def accepted(ctx, names, title, lines, verdict, cpa_calls, process, other_landing=()):
        asked.append((verdict, list(other_landing)))
        return ["改过的", "封面字"], {"overall": "PASS", "fail_reasons": []}, ["gemini-4-neon"]

    monkeypatch.setattr(step6_title, "_accepted_cover_text", accepted)
    monkeypatch.setattr(cover_import, "imported_cover", lambda ctx: object() if imported else None)
    cands = tmp_path / "candidates.json"
    cands.write_text("{}", encoding="utf-8")
    rnd = {"call_id": "title", "parsed": {"recommended": 0}, "candidates": [chosen], "order": [0], "pick_call": None}
    out = step6_title._settled_cover_text(object(), ("李豆沙",), "【李豆沙】标题", ["连线说", "她"], chosen, rnd, cands, [], [],
                                          kept_online=kept_online)
    return out, asked


def test_the_cover_lines_under_a_kept_online_title_or_the_owners_unjudged_title_are_still_settled(tmp_path, monkeypatch):
    """Before, a revision keeping its published title and the owner's own title skipped the cover text: a label written
    as a name (「连线：……」) went onto the cover unchecked, and so did lines the fact judge had refused."""
    (lines, judge), asked = _settle(tmp_path, monkeypatch, {"landing": 1, "judge": _judge("FAIL", "封面字编了")},
                                    kept_online=True)
    assert lines == ["改过的", "封面字"] and asked == [({"overall": "FAIL", "fail_reasons": ["封面字编了"]}, [])]
    assert judge["cover"] == {"overall": "PASS", "fail_reasons": []}
    assert judge["fallback_models"] == ["gemini-4-neon"]
    (lines, judge), asked = _settle(tmp_path, monkeypatch, {"landing": 1})       # the owner's title, never judged
    assert lines == ["改过的", "封面字"] and judge == {}
    assert asked == [({"overall": "PASS", "fail_reasons": []}, [])]               # the checks on the lines alone decide


def test_a_kept_online_title_receipt_follows_the_final_cover_and_preserves_objections(tmp_path, monkeypatch):
    from src.autoslice.manual_lane import cover_import

    monkeypatch.setattr(cover_import, "imported_cover", lambda ctx: None)
    final_cover = {"overall": "PASS", "fail_reasons": [], "runs": "final cover"}
    monkeypatch.setattr(step6_title, "_accepted_cover_text",
                        lambda *args: (["改过的", "封面字"], final_cover, ["gemini-4-neon"]))
    for by, objections in (("online", []), ("online_kept", ["线上标题写法有异议"])):
        ctx = make_ctx(tmp_path / by, remote=FakeRemote(), cpa=FakeCpa([]))
        cands = ctx.clip_dir / "candidates.json"
        cands.parent.mkdir(parents=True, exist_ok=True)
        cands.write_text("{}", encoding="utf-8")
        online_judge = _judge("FAIL", "旧封面")
        chosen = {"landing": 1, "judge": _judge("FAIL", "候选封面")}
        rnd = {"parsed": {"recommended": 0}, "candidates": [chosen]}
        lines = step6_title._title_goes_out(ctx, ["李豆沙"], "【李豆沙】线上标题", ["旧", "封面"], chosen, rnd,
                                            cands, [], [], forced=False, online_judge=online_judge, kept=True,
                                            objections=objections)
        receipt = _receipt(ctx)
        assert lines == ["改过的", "封面字"] and receipt["by"] == by
        assert receipt["judge"]["cover"] == final_cover and receipt["judge"]["fallback_models"] == ["gemini-4-neon"]
        assert receipt.get("objections", []) == objections


def test_under_an_imported_cover_the_receipt_carries_no_verdict_on_lines_no_cover_shows(tmp_path, monkeypatch):
    """An imported cover brings its own text.  The round's cover verdict (FAIL here) went into the receipt and step 8
    refused it -- 「封面字没过事实判官：从第 6 步重做」 -- and step 6 redone met the same import."""
    (lines, judge), asked = _settle(tmp_path, monkeypatch, {"landing": 1, "judge": _judge("FAIL", "封面字编了")},
                                    imported=True)
    assert asked == [] and lines == ["连线说", "她"] and "cover" not in judge
    clip = tmp_path / "clip"
    (clip / "title").mkdir(parents=True)
    (clip / "title/title.txt").write_text("【李豆沙】标题\n", encoding="utf-8")
    title_acceptance.write(clip, "【李豆沙】标题", "step6", judge)
    assert title_acceptance.problem(clip) == ""
    assert step6_title.judged_whole(step6_title.with_cover_of(_judge("FAIL", "x"), None)) == []
    assert step6_title.judged_whole(step6_title.with_cover_of(_judge("FAIL", "x"), ["a"])) == ["封面字：x"]
