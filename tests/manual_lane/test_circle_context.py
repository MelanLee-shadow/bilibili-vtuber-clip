"""Step 6 looks up the circle references the main line needs and the subtitles and persona.md do not explain
(circle_context).  Every case runs the title half of step 6 on fakes: no model, no network."""

from __future__ import annotations

import json

import pytest
from fakes import MERGE, FakeCpa, FakeRemote, FakeSearch, context_answer, make_ctx, srt

from src.autoslice.manual_lane import circle_context as cc
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import title_understanding
from src.autoslice.manual_lane.runner import StepOutcome

TERM = "某某联盟"
URL = "https://wiki.example.org/union"
OTHER_URL = "https://news.example.net/post/1"
NOTE = "某某联盟是几位主播在近期组成的企划，花礼发起。"
ROWS = [{"title": "联盟简介", "url": URL, "snippet": "几位主播组成的企划"},
        {"title": "相关报道", "url": OTHER_URL, "snippet": "x" * 400}]
ITEM = {"term": TERM, "cues": [2], "why": "笑点靠它才看得懂"}


def _cand(body, on_stage=("李豆沙",)):
    return {"title_body": body, "on_stage": list(on_stage), "punchline_person": "她越长大越讨厌看那种有规矩的",
            "swap_check": {"holds_with_other_host": False, "reason": "换主播不成立"}}


def _answer(*cands):
    return json.dumps({"candidates": list(cands), "recommended": 0, "cover_lines": ["破防了", "半分钟"]},
                      ensure_ascii=False)


def _merge(*items):
    return json.dumps(dict(json.loads(MERGE), unexplained=list(items)), ensure_ascii=False)


def _found(sources=(URL,)):
    return lambda prompt: context_answer(prompt, {TERM: (NOTE, sources)})


def _run(tmp_path, *, search=None, merge=None, context=None, lookup=True):
    """Step 6's title half with a merge that names `merge` items (the default merge names none)."""
    cpa = FakeCpa([_answer(_cand("她半分钟后真的破防了")), _answer(_cand("她半分钟后真的破防了"))],
                  merges=[merge] if merge is not None else None, context=context)
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    if lookup:
        ctx.adapters.lookup = search if search is not None else FakeSearch()
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(srt(3), encoding="utf-8")
    return ctx, cpa, si.pick_title(ctx)


def _context(ctx):
    return json.loads((ctx.clip_dir / "title/context.json").read_text(encoding="utf-8"))


def _candidates(ctx):
    return json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))


# ----- found: the background reaches the title rounds, the ranking and PROCESS -----------------------------------
def test_a_found_reference_is_recorded_and_its_background_reaches_every_title_prompt(tmp_path):
    search = FakeSearch(configured=True, default=ROWS)
    two = [_cand("她半分钟后真的破防了"), _cand("她先嘴硬后拆台", ("李豆沙",))]
    cpa = FakeCpa([_answer(*two)], merges=[_merge(ITEM)], context=_found())
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    ctx.adapters.lookup = search
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(srt(3), encoding="utf-8")
    picked = si.pick_title(ctx)
    assert not isinstance(picked, StepOutcome), picked

    assert search.calls == [(f"{TERM} 李豆沙", 8), (f"{TERM} 虚拟主播", 8)]
    record = _context(ctx)
    assert set(record) == {"terms", "beyond_limit", "call_id", "graph"} and record["call_id"] == "title-context"
    [term] = record["terms"]
    assert term["status"] == "FOUND" and term["note"] == NOTE and term["sources"] == [URL] and term["reason"] == ""
    assert (term["term"], term["cues"], term["why"]) == (TERM, [2], "笑点靠它才看得懂")
    assert [q["query"] for q in term["queries"]] == [f"{TERM} 李豆沙", f"{TERM} 虚拟主播"]
    assert [h["url"] for h in term["queries"][0]["results"]] == [URL, OTHER_URL]
    assert len(term["queries"][0]["results"][1]["snippet"]) == 300 and term["queries"][0]["error"] == ""

    asked = cpa.prompts[cpa.calls.index("title-context")]               # the model sees the sentences around the cue
    assert "句子1" in asked and "句子3" in asked and URL in asked
    assert "title-context" in [c["call_id"] for c in picked[3]]
    assert _candidates(ctx)["context"] == [{"term": TERM, "status": "FOUND"}]

    assert cpa.title_prompts
    for prompt in cpa.title_prompts:
        assert cc.BLOCK_HEAD in prompt and f"- {TERM}：{NOTE}（出处：wiki.example.org）" in prompt
        assert prompt.index("## 本片主线") < prompt.index(cc.BLOCK_HEAD)
    pick = cpa.prompts[cpa.calls.index("title-pick-r0")]
    assert cc.BLOCK_HEAD in pick and NOTE in pick
    assert f"圈内背景：{TERM} → 已查到（出处 1 个）" in picked[2]


def test_the_model_is_asked_in_the_high_tier_for_the_registered_purpose(tmp_path):
    ctx, cpa, picked = _run(tmp_path, search=FakeSearch(configured=True, default=ROWS), merge=_merge(ITEM),
                            context=_found())
    event = next(c for c in picked[3] if c["call_id"] == "title-context")
    assert event["effort"] == "high"


# ----- the sources must be among the results ---------------------------------------------------------------------
def test_a_source_that_is_not_among_the_results_is_dropped_and_the_reference_is_not_found(tmp_path):
    ctx, cpa, picked = _run(tmp_path, search=FakeSearch(configured=True, default=ROWS), merge=_merge(ITEM),
                            context=_found(("https://invented.example.com/x",)))
    assert not isinstance(picked, StepOutcome), picked
    [term] = _context(ctx)["terms"]
    assert term["status"] == "NOT_FOUND" and term["sources"] == [] and term["note"] == ""
    for prompt in cpa.title_prompts:
        assert cc.BLOCK_HEAD not in prompt and NOTE not in prompt
    assert any(line.startswith(f"圈内背景：{TERM} → 未查清（") for line in picked[2])


def test_only_the_sources_among_the_results_are_kept(tmp_path):
    ctx, _, _ = _run(tmp_path, search=FakeSearch(configured=True, default=ROWS), merge=_merge(ITEM),
                     context=_found((URL, "https://invented.example.com/x", URL)))
    [term] = _context(ctx)["terms"]
    assert term["status"] == "FOUND" and term["sources"] == [URL]


def test_a_model_that_finds_nothing_explains_why_not(tmp_path):
    ctx, cpa, picked = _run(tmp_path, search=FakeSearch(configured=True, default=ROWS), merge=_merge(ITEM))
    [term] = _context(ctx)["terms"]
    assert term["status"] == "NOT_FOUND" and term["reason"] == "结果没有说到它"
    assert f"圈内背景：{TERM} → 未查清（结果没有说到它）" in picked[2]


def test_a_search_that_returns_nothing_is_not_found_and_asks_no_model(tmp_path):
    ctx, cpa, picked = _run(tmp_path, search=FakeSearch(configured=True), merge=_merge(ITEM))
    assert _context(ctx)["terms"][0]["status"] == "NOT_FOUND" and "title-context" not in cpa.calls
    assert f"圈内背景：{TERM} → 未查清（检索没有返回结果）" in picked[2]


# ----- unconfigured, failed ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("lookup", [True, False], ids=["fake-unconfigured", "adapter-not-set"])
def test_an_unconfigured_search_is_recorded_and_the_step_goes_on(tmp_path, lookup):
    search = FakeSearch()
    ctx, cpa, picked = _run(tmp_path, search=search, merge=_merge(ITEM), lookup=lookup)
    assert not isinstance(picked, StepOutcome), picked
    assert _context(ctx)["terms"][0]["status"] == "UNCONFIGURED"
    assert "title-context" not in cpa.calls and search.calls == []
    assert f"圈内背景：{TERM} → 未检索（未配置检索入口）" in picked[2]
    assert all(cc.BLOCK_HEAD not in prompt for prompt in cpa.title_prompts)


def test_a_search_that_raises_is_recorded_on_the_term_and_the_step_goes_on(tmp_path):
    search = FakeSearch(configured=True, raises=OSError("network down"))
    ctx, cpa, picked = _run(tmp_path, search=search, merge=_merge(ITEM))
    assert not isinstance(picked, StepOutcome), picked
    [term] = _context(ctx)["terms"]
    assert term["status"] == "FAILED" and "OSError: network down" in term["reason"]
    assert [q["error"] for q in term["queries"]] == ["OSError: network down"] * 2
    assert "title-context" not in cpa.calls
    assert any(line.startswith(f"圈内背景：{TERM} → 检索失败（OSError: network down") for line in picked[2])


def test_one_failed_term_does_not_stop_the_others(tmp_path):
    class Flaky(FakeSearch):
        def web(self, query, *, limit=5):
            if query.startswith("坏"):
                raise OSError("boom")
            return super().web(query, limit=limit)

    bad = {"term": "坏词", "cues": [1], "why": "x"}
    ctx, _, picked = _run(tmp_path, search=Flaky(configured=True, default=ROWS), merge=_merge(bad, ITEM),
                          context=_found())
    assert [t["status"] for t in _context(ctx)["terms"]] == ["FAILED", "FOUND"]
    assert not isinstance(picked, StepOutcome), picked


def test_a_failed_model_call_is_recorded_and_the_step_goes_on(tmp_path):
    ctx, cpa, picked = _run(tmp_path, search=FakeSearch(configured=True, default=ROWS), merge=_merge(ITEM),
                            context=lambda prompt: None)
    assert not isinstance(picked, StepOutcome), picked
    [term] = _context(ctx)["terms"]
    assert term["status"] == "FAILED" and "CPA_HTTP_ERROR" in term["reason"]
    assert any(line.startswith(f"圈内背景：{TERM} → 检索失败（") for line in picked[2])
    assert all(cc.BLOCK_HEAD not in prompt for prompt in cpa.title_prompts)


def test_an_answer_outside_the_contract_is_asked_again_then_failed(tmp_path):
    ctx, cpa, picked = _run(tmp_path, search=FakeSearch(configured=True, default=ROWS), merge=_merge(ITEM),
                            context=json.dumps([{"term": "别的词", "found": False}]))
    assert not isinstance(picked, StepOutcome), picked
    assert _context(ctx)["terms"][0]["status"] == "FAILED"
    assert len([c for c in cpa.calls if c.startswith("title-context")]) == 10        # CPA_TEXT_TRIES


# ----- nothing to look up, and the limit --------------------------------------------------------------------------
@pytest.mark.parametrize("merge", [None, _merge()], ids=["field-missing", "empty-list"])
def test_no_unexplained_reference_means_no_search_and_no_model_call(tmp_path, merge):
    search = FakeSearch(configured=True, default=ROWS)
    ctx, cpa, picked = _run(tmp_path, search=search, merge=merge)
    assert not isinstance(picked, StepOutcome), picked
    assert search.calls == [] and "title-context" not in cpa.calls
    record = _context(ctx)
    graph = record.pop("graph")       # the graph's own record: test_graph_context.py
    assert record == {"terms": [], "beyond_limit": [], "call_id": ""}
    assert graph["status"] == "repo_only" and graph["skipped_search"] == []      # the fake host has no runtime graph
    assert [line for line in picked[2] if line.startswith("圈内背景")] == ["圈内背景：主线里没有需要另查的指称"]
    assert _candidates(ctx)["context"] == []


def test_only_the_first_three_references_are_processed(tmp_path):
    items = [{"term": f"词{n}", "cues": [1], "why": "x"} for n in range(5)]
    search = FakeSearch(configured=True, default=ROWS)
    ctx, cpa, picked = _run(tmp_path, search=search, merge=_merge(*items))
    record = _context(ctx)
    assert [t["term"] for t in record["terms"]] == ["词0", "词1", "词2"] and record["beyond_limit"] == ["词3", "词4"]
    assert len(search.calls) == 6 and len([c for c in cpa.calls if c.startswith("title-context")]) == 1
    lines = [line for line in picked[2] if line.startswith("圈内背景")]
    assert len(lines) == 4 and "另有 2 个指称超过上限 3" in lines[-1]


# ----- the merge's field ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["x", [{"term": "", "cues": [], "why": ""}], [{"term": "a", "cues": ["1"], "why": ""}],
                                 [{"term": "a", "cues": [True], "why": ""}], [{"term": "a", "cues": []}], ["a"]])
def test_a_malformed_unexplained_field_breaks_the_merge_contract(bad):
    assert title_understanding.merge_problem(dict(json.loads(MERGE), unexplained=bad))


def test_a_missing_or_well_formed_unexplained_field_is_accepted():
    merged = json.loads(MERGE)
    assert title_understanding.merge_problem(merged) == ""
    assert title_understanding.merge_problem(dict(merged, unexplained=[ITEM])) == ""
