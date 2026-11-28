"""Step bodies with fake remote and fake CPA: boundary (CPA's cut is adopted), subtitles (U108: every doubt goes to
CPA with all candidates; only an UNRESOLVED that affects meaning stops; 维护者's labels bypass), titles, package,
review.  No ssh, no ffmpeg, no network."""

from __future__ import annotations

import json

from fakes import UNDERSTANDING_CALLS, FakeCpa, FakeRemote, IdCpa, fake_cover, make_ctx, srt

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import step_cut
from src.autoslice.manual_lane import stage_package as sp
from src.autoslice.manual_lane.adapters import RemoteResult
from src.autoslice.manual_lane.srt_text import parse_srt

RD = "/r/clip"
# jianying hears cue 1 as 句子乙 (not the current 句子1): with a 3.8 reading 句子乙 that is two families (U122)
_JY_CUE1 = srt(3, step=2.0).replace("句子1", "句子乙")


# ----- step 1: boundary -------------------------------------------------------------------------
def test_boundary_adopts_the_cpa_cut_even_when_it_differs_from_the_proposal(tmp_path):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=FakeCpa([json.dumps(
        {"verdict": "REJECT", "start_cue": 8, "end_cue": 40, "reason": "开头要交代前情"}, ensure_ascii=False)]))
    out = si.step1_boundary(ctx)
    assert out.ok, out.note
    clip = ctx.state.data["clip"]
    assert (clip["start_cue"], clip["end_cue"]) == (8, 40)
    assert clip["start_ms"] == 14000 and clip["end_ms"] == 80000   # 66 s: above the 60 s talk minimum
    decision = json.loads((ctx.clip_dir / "boundary" / "decision.json").read_text(encoding="utf-8"))
    assert decision["proposal"] == [6, 40] and decision["adopted"]["end_cue"] == 40
    assert any("采用切点 8–40" in line for line in out.process)


def test_boundary_has_no_counter_rounds_only_one_cpa_call(tmp_path):
    cpa = FakeCpa([json.dumps({"verdict": "REJECT", "start_cue": 7, "end_cue": 40, "reason": "x"})])
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    assert si.step1_boundary(ctx).ok
    assert cpa.calls == ["boundary"]


def test_boundary_cut_outside_the_segment_stops(tmp_path):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=FakeCpa([json.dumps(
        {"verdict": "ACCEPT", "start_cue": 6, "end_cue": 999, "reason": "x"})]))
    out = si.step1_boundary(ctx)
    assert not out.ok and out.reason == "BOUNDARY_OUTSIDE_SEGMENT"


def test_boundary_length_outside_range_stops(tmp_path):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=FakeCpa([json.dumps(
        {"verdict": "ACCEPT", "start_cue": 6, "end_cue": 7, "reason": "x"}), NO_EXTENSION]))
    assert si.step1_boundary(ctx).reason == "BOUNDARY_LENGTH_UNREASONABLE"


def _seated(ctx, seat_reason, *, peaks=None):
    """The clip as discover seats it: its selection entry (seat_reason) and the selection's key moments."""
    sel = ctx.clip_dir.parent.parent / "entries" / "clip.json"
    sel.parent.mkdir(parents=True, exist_ok=True)
    sel.write_text(json.dumps({"input_pool": [{"cid": "clip", "seat_reason": seat_reason, "must_cut": ["sajiao"]}]}),
                   encoding="utf-8")
    ctx.state.data["clip"]["selection_file"] = str(sel)
    if peaks:
        ctx.state.data["clip"]["peaks"] = peaks
    ctx.state.save()


def test_boundary_keeps_the_selections_key_moments_in_the_clip(tmp_path):
    # 11-25 826020: the cut ended before 「可以可以，当然可以啦」, the moment the selection chose, and fell under 60 s
    cpa = FakeCpa([json.dumps({"verdict": "REJECT", "start_cue": 6, "end_cue": 30, "reason": "选歌换题"})])
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    _seated(ctx, "MUST_CUT", peaks=[{"quote": "句子38", "who": "", "at_ms": 74_000, "why": "落点"}])
    out = si.step1_boundary(ctx)
    assert out.ok, out.note
    assert (ctx.state.data["clip"]["start_cue"], ctx.state.data["clip"]["end_cue"]) == (6, 38)
    assert "38 句子38" in cpa.prompts[0]                  # the reviewer is told which lines must stay
    decision = json.loads((ctx.clip_dir / "boundary" / "decision.json").read_text(encoding="utf-8"))
    assert decision["peaks_kept"] == [38]


def _side(key, cue, link):
    return {key: cue, "link": link, "why": "同一件事"} if cue else {key: None, "link": "", "why": ""}


def _extension(before=None, after=None):
    return json.dumps({"before": _side("start_cue", *(before or (None, ""))),
                       "after": _side("end_cue", *(after or (None, "")))}, ensure_ascii=False)


NO_EXTENSION = _extension()


def test_a_story_long_enough_is_asked_one_question(tmp_path):
    cpa = FakeCpa([json.dumps({"verdict": "ACCEPT", "start_cue": 6, "end_cue": 40, "reason": "x"})])
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    assert si.step1_boundary(ctx).ok and cpa.calls == ["boundary"]
    assert "60" not in cpa.prompts[0].split("字幕上下文")[0]          # the first judgement never hears of the minimum


def test_a_short_story_widens_only_by_sides_whose_link_is_in_the_added_lines(tmp_path):
    # story 50-60 (22 s) of srt(120); the context reaches 60 s beyond it; before: 20 with a line of 20-49 -> taken;
    # after: 75 quoting a line of the story itself (not of 61-75) -> not taken
    cpa = FakeCpa([json.dumps({"verdict": "ACCEPT", "start_cue": 50, "end_cue": 60, "reason": "x"}),
                   _extension(before=(20, "句子 23！"), after=(75, "句子55"))])
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa, bcut_text=srt(120))
    ctx.state.data["clip"].update(start_cue=50, end_cue=60, proposal={"start_cue": 50, "end_cue": 60})
    ctx.state.save()
    out = si.step1_boundary(ctx)
    assert out.ok, out.note
    assert cpa.calls == ["boundary", "boundary-extend"]
    context = cpa.prompts[1].split("字幕上下文（序号 时间 文字；故事前后各约 60 秒）：")[1]
    assert "\n20 " in context and "\n19 " not in context      # cue 20 starts 60 s before cue 50 (2 s a cue)
    assert "\n91 " in context and "\n92 " not in context
    assert (ctx.state.data["clip"]["start_cue"], ctx.state.data["clip"]["end_cue"]) == (20, 60)
    decision = json.loads((ctx.clip_dir / "boundary" / "decision.json").read_text(encoding="utf-8"))
    assert decision["story"] == {"start_cue": 50, "end_cue": 60}
    assert decision["extend"]["notes"][1].startswith("after：要并到 cue 75")


def test_a_short_must_cut_story_is_queued_with_its_cut_kept_and_the_clip_unchanged(tmp_path):
    # 11-23 1528380: a must-cut story of 42 s, nothing of the same matter on either side -> the compilation queue
    cpa = FakeCpa([json.dumps({"verdict": "REJECT", "start_cue": 10, "end_cue": 25, "reason": "x"}), NO_EXTENSION])
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    _seated(ctx, "MUST_CUT")
    before = dict(ctx.state.data["clip"])
    out = si.step1_boundary(ctx)
    assert not out.ok and out.reason == "BOUNDARY_SHORT_QUEUED"
    assert cpa.calls == ["boundary", "boundary-extend"] and len(out.cpa) == 2
    clip = ctx.state.data["clip"]
    assert {k: clip[k] for k in ("start_cue", "end_cue", "start_ms", "end_ms")} == \
        {k: before[k] for k in ("start_cue", "end_cue", "start_ms", "end_ms")}       # not adopted as a clip
    decision = json.loads((ctx.clip_dir / "boundary" / "decision.json").read_text(encoding="utf-8"))
    assert decision["adopted"] == {"start_cue": 10, "end_cue": 25, "start_ms": 18_000, "end_ms": 50_000}   # the story
    assert decision["short"] == {"duration_s": 32.0, "queue": "BOUNDARY_SHORT_QUEUED"}
    assert decision["story"] == {"start_cue": 10, "end_cue": 25}


def test_the_same_short_story_of_an_ordinary_candidate_still_stops(tmp_path):
    cpa = FakeCpa([json.dumps({"verdict": "REJECT", "start_cue": 10, "end_cue": 25, "reason": "x"}), NO_EXTENSION])
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    _seated(ctx, "QUOTA")
    assert si.step1_boundary(ctx).reason == "BOUNDARY_LENGTH_UNREASONABLE"


def test_a_side_beyond_the_shown_lines_or_with_a_filler_link_is_not_taken():
    # story 50-60 of srt(120), 2 s a cue: the second question shows cues 20-91.  A side reaching cue 5 (never shown) or
    # quoting a filler found anywhere (「哈哈哈」, one distinct character) is not taken, however real the quote is
    cues = parse_srt(srt(120).replace("句子70\n", "哈哈哈\n"))
    answer = json.loads(_extension(before=(5, "句子5"), after=(75, "哈哈哈")))
    start, end, notes = step_cut._extension(cues, 50, 60, answer)
    assert (start, end) == (50, 60)
    assert "超出了给它看的上下文" in notes[0] and "不到 4 个不同的字" in notes[1]
    answer = json.loads(_extension(before=(20, "句子20"), after=(91, "句子91")))      # the edges of what was shown
    assert step_cut._extension(cues, 50, 60, answer)[:2] == (20, 91)


def test_boundary_cpa_failure_and_garbage_are_tool_failures(tmp_path):
    ctx = make_ctx(tmp_path / "a", remote=FakeRemote(), cpa=FakeCpa([None]))
    assert si.step1_boundary(ctx).reason == "CPA_CALL_FAILED"
    ctx = make_ctx(tmp_path / "b", remote=FakeRemote(), cpa=FakeCpa(["没有 JSON"]))
    assert si.step1_boundary(ctx).reason == "CPA_RESPONSE_UNPARSEABLE"


def test_boundary_accept_unchanged_keeps_single_call_behavior(tmp_path):
    cpa = FakeCpa([json.dumps({"verdict": "ACCEPT", "start_cue": 6, "end_cue": 40, "reason": "完整故事"}, ensure_ascii=False)])
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    out = si.step1_boundary(ctx)
    assert out.ok, out.note
    assert cpa.calls == ["boundary"]
    clip = ctx.state.data["clip"]
    assert (clip["start_cue"], clip["end_cue"]) == (6, 40)
    assert (clip["start_ms"], clip["end_ms"]) == (10_000, 80_000)
    decision = json.loads((ctx.clip_dir / "boundary" / "decision.json").read_text(encoding="utf-8"))
    assert decision["adopted"] == {"start_cue": 6, "end_cue": 40, "start_ms": 10_000, "end_ms": 80_000}
    assert "attempts" not in decision


def test_boundary_reject_with_valid_correction_adopts_correction_in_one_call(tmp_path):
    cpa = FakeCpa([json.dumps(
        {"verdict": "REJECT", "start_cue": 4, "end_cue": 44, "reason": "前移补前情，后移到完整句尾"},
        ensure_ascii=False,
    )])
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa, bcut_text=srt(60))
    out = si.step1_boundary(ctx)
    assert out.ok, out.note
    assert cpa.calls == ["boundary"]
    clip = ctx.state.data["clip"]
    assert (clip["start_cue"], clip["end_cue"]) == (4, 44)


def test_boundary_reject_without_correction_retries_with_feedback_and_adopts_retry_cut(tmp_path):
    # Subcase A: REJECT with unchanged proposal cues (witness pattern) -> retry with widened context + feedback
    cpa_a = FakeCpa([
        json.dumps({"verdict": "REJECT", "start_cue": 50, "end_cue": 85, "reason": "开头缺少前情，结尾截在半句话上"},
                   ensure_ascii=False),
        json.dumps({"verdict": "ACCEPT", "start_cue": 30, "end_cue": 88, "reason": "已补前情并延到完整句尾"},
                   ensure_ascii=False),
    ])
    ctx_a = make_ctx(tmp_path / "unchanged", remote=FakeRemote(), cpa=cpa_a, bcut_text=srt(120))
    ctx_a.state.data["clip"].update(start_cue=50, end_cue=85, proposal={"start_cue": 50, "end_cue": 85})
    ctx_a.state.save()
    out_a = si.step1_boundary(ctx_a)
    assert out_a.ok, out_a.note
    assert cpa_a.calls == ["boundary", "boundary-retry-1"]
    clip_a = ctx_a.state.data["clip"]
    assert (clip_a["start_cue"], clip_a["end_cue"]) == (30, 88)
    assert "开头缺少前情，结尾截在半句话上" in cpa_a.prompts[1]
    assert "\n30 " not in cpa_a.prompts[0] and "\n30 " in cpa_a.prompts[1]
    assert "前情" in cpa_a.prompts[1] and "完整" in cpa_a.prompts[1]
    decision_a = json.loads((ctx_a.clip_dir / "boundary" / "decision.json").read_text(encoding="utf-8"))
    assert decision_a["adopted"]["start_cue"] == 30 and decision_a["adopted"]["end_cue"] == 88
    assert len(decision_a["attempts"]) == 2

    # Subcase B: REJECT with null cues -> retry -> REJECT with valid correction
    cpa_b = FakeCpa([
        json.dumps({"verdict": "REJECT", "start_cue": None, "end_cue": None, "reason": "结尾半句"}, ensure_ascii=False),
        json.dumps({"verdict": "REJECT", "start_cue": 4, "end_cue": 38, "reason": "回退到上一完整句"}, ensure_ascii=False),
    ])
    ctx_b = make_ctx(tmp_path / "null", remote=FakeRemote(), cpa=cpa_b, bcut_text=srt(60))
    out_b = si.step1_boundary(ctx_b)
    assert out_b.ok, out_b.note
    assert cpa_b.calls == ["boundary", "boundary-retry-1"]
    assert (ctx_b.state.data["clip"]["start_cue"], ctx_b.state.data["clip"]["end_cue"]) == (4, 38)


def test_boundary_reject_with_invalid_correction_retries_and_uses_later_valid_cut(tmp_path):
    # Subcase A: out-of-segment correction
    cpa_a = FakeCpa([
        json.dumps({"verdict": "REJECT", "start_cue": 6, "end_cue": 999, "reason": "结尾半句"}, ensure_ascii=False),
        json.dumps({"verdict": "ACCEPT", "start_cue": 4, "end_cue": 38, "reason": "回退到完整句"}, ensure_ascii=False),
    ])
    ctx_a = make_ctx(tmp_path / "oob", remote=FakeRemote(), cpa=cpa_a, bcut_text=srt(60))
    out_a = si.step1_boundary(ctx_a)
    assert out_a.ok, out_a.note
    assert cpa_a.calls == ["boundary", "boundary-retry-1"]
    assert (ctx_a.state.data["clip"]["start_cue"], ctx_a.state.data["clip"]["end_cue"]) == (4, 38)
    assert "6–999" in cpa_a.prompts[1] and "结尾半句" in cpa_a.prompts[1]

    # Subcase B: cue outside the shown round-0 window
    cpa_b = FakeCpa([
        json.dumps({"verdict": "REJECT", "start_cue": 5, "end_cue": 85, "reason": "需要更长前情"}, ensure_ascii=False),
        json.dumps({"verdict": "ACCEPT", "start_cue": 25, "end_cue": 85, "reason": "窗口内已补前情"}, ensure_ascii=False),
    ])
    ctx_b = make_ctx(tmp_path / "outside-window", remote=FakeRemote(), cpa=cpa_b, bcut_text=srt(120))
    ctx_b.state.data["clip"].update(start_cue=50, end_cue=85, proposal={"start_cue": 50, "end_cue": 85})
    ctx_b.state.save()
    out_b = si.step1_boundary(ctx_b)
    assert out_b.ok, out_b.note
    assert cpa_b.calls == ["boundary", "boundary-retry-1"]
    assert (ctx_b.state.data["clip"]["start_cue"], ctx_b.state.data["clip"]["end_cue"]) == (25, 85)

    # Subcase C: peaks widening would revert to the rejected proposal
    cpa_c = FakeCpa([
        json.dumps({"verdict": "REJECT", "start_cue": 8, "end_cue": 35, "reason": "开头缺前情"}, ensure_ascii=False),
        json.dumps({"verdict": "ACCEPT", "start_cue": 4, "end_cue": 40, "reason": "向前补前情"}, ensure_ascii=False),
    ])
    ctx_c = make_ctx(tmp_path / "peaks-revert", remote=FakeRemote(), cpa=cpa_c, bcut_text=srt(60))
    _seated(ctx_c, "QUOTA", peaks=[
        {"quote": "句子6", "who": "", "at_ms": 10_000, "why": "起点"},
        {"quote": "句子40", "who": "", "at_ms": 78_000, "why": "落点"},
    ])
    out_c = si.step1_boundary(ctx_c)
    assert out_c.ok, out_c.note
    assert cpa_c.calls == ["boundary", "boundary-retry-1"]
    assert (ctx_c.state.data["clip"]["start_cue"], ctx_c.state.data["clip"]["end_cue"]) == (4, 40)


def test_boundary_reject_budget_exhausted_stops_with_boundary_not_accepted(tmp_path):
    # Subcase A: REJECT with unchanged proposal cues twice -> stops with BOUNDARY_NOT_ACCEPTED
    cpa_a = FakeCpa([
        json.dumps({"verdict": "REJECT", "start_cue": 6, "end_cue": 40, "reason": "开头缺前情，结尾半句"}, ensure_ascii=False),
        json.dumps({"verdict": "REJECT", "start_cue": 6, "end_cue": 40, "reason": "开头缺前情，结尾半句"}, ensure_ascii=False),
    ])
    ctx_a = make_ctx(tmp_path / "unchanged", remote=FakeRemote(), cpa=cpa_a, bcut_text=srt(60))
    out_a = si.step1_boundary(ctx_a)
    assert not out_a.ok and out_a.reason == "BOUNDARY_NOT_ACCEPTED"
    assert cpa_a.calls == ["boundary", "boundary-retry-1"] and len(out_a.cpa) == 2
    assert not (ctx_a.clip_dir / "boundary" / "decision.json").exists()
    clip_a = ctx_a.state.data["clip"]
    assert (clip_a["start_cue"], clip_a["end_cue"], clip_a["start_ms"], clip_a["end_ms"]) == (6, 40, 10_000, 80_000)

    # Subcase B: reversed cues twice -> BOUNDARY_NOT_ACCEPTED
    cpa_b = FakeCpa([
        json.dumps({"verdict": "REJECT", "start_cue": 35, "end_cue": 10, "reason": "倒序"}, ensure_ascii=False),
        json.dumps({"verdict": "REJECT", "start_cue": 35, "end_cue": 10, "reason": "倒序"}, ensure_ascii=False),
    ])
    ctx_b = make_ctx(tmp_path / "reversed", remote=FakeRemote(), cpa=cpa_b, bcut_text=srt(60))
    out_b = si.step1_boundary(ctx_b)
    assert not out_b.ok and out_b.reason == "BOUNDARY_NOT_ACCEPTED"
    assert not (ctx_b.clip_dir / "boundary" / "decision.json").exists()

    # Subcase C: REJECT then retry ACCEPT on the exact rejected proposal -> BOUNDARY_NOT_ACCEPTED
    cpa_c = FakeCpa([
        json.dumps({"verdict": "REJECT", "start_cue": 6, "end_cue": 40, "reason": "半句"}, ensure_ascii=False),
        json.dumps({"verdict": "ACCEPT", "start_cue": 6, "end_cue": 40, "reason": "算了"}, ensure_ascii=False),
    ])
    ctx_c = make_ctx(tmp_path / "retry-accept-rejected", remote=FakeRemote(), cpa=cpa_c, bcut_text=srt(60))
    out_c = si.step1_boundary(ctx_c)
    assert not out_c.ok and out_c.reason == "BOUNDARY_NOT_ACCEPTED"
    assert not (ctx_c.clip_dir / "boundary" / "decision.json").exists()


def test_boundary_unparsable_verdict_non_object_and_extend_bad_json_fail_closed(tmp_path):
    maybe = json.dumps({"verdict": "MAYBE", "start_cue": 6, "end_cue": 40, "reason": "不确定"}, ensure_ascii=False)
    ctx_unknown = make_ctx(tmp_path / "unknown", remote=FakeRemote(), cpa=FakeCpa([maybe]))
    out_unknown = si.step1_boundary(ctx_unknown)
    assert not out_unknown.ok and out_unknown.reason == "CPA_RESPONSE_UNPARSEABLE"
    assert not (ctx_unknown.clip_dir / "boundary" / "decision.json").exists()
    assert not list((tmp_path / "unknown" / "cache" / "cpa").glob("*.json"))
    assert len(list((ctx_unknown.clip_dir / "cpa").glob("boundary.rejected-*.completion.txt"))) == 1

    missing = json.dumps({"start_cue": 6, "end_cue": 40, "reason": "漏了裁决"}, ensure_ascii=False)
    ctx_missing = make_ctx(tmp_path / "missing", remote=FakeRemote(), cpa=FakeCpa([missing]))
    assert si.step1_boundary(ctx_missing).reason == "CPA_RESPONSE_UNPARSEABLE"

    ctx_arr = make_ctx(tmp_path / "arr", remote=FakeRemote(), cpa=FakeCpa(["[6, 40]"]))
    assert si.step1_boundary(ctx_arr).reason == "CPA_RESPONSE_UNPARSEABLE"

    ctx_bool = make_ctx(tmp_path / "bool", remote=FakeRemote(), cpa=FakeCpa([
        json.dumps({"verdict": "ACCEPT", "start_cue": True, "end_cue": 40, "reason": "x"}, ensure_ascii=False),
    ]))
    assert si.step1_boundary(ctx_bool).reason == "BOUNDARY_NO_CUT_PAIR"

    for name, bad_extend in (("extend-arr", "[]"), ("extend-null", "null")):
        ctx_ext = make_ctx(tmp_path / name, remote=FakeRemote(), cpa=FakeCpa([
            json.dumps({"verdict": "ACCEPT", "start_cue": 6, "end_cue": 7, "reason": "太短"}, ensure_ascii=False),
            bad_extend,
        ]))
        out_ext = si.step1_boundary(ctx_ext)
        assert not out_ext.ok and out_ext.reason == "CPA_RESPONSE_UNPARSEABLE"


def test_boundary_extend_cannot_revert_to_rejected_proposal(tmp_path):
    # Finding 1 (ordinary): REJECT 6-40 -> correction 6-20 (30 s) -> boundary-extend wants 6-40 -> widening refused.
    cpa_ord = FakeCpa([
        json.dumps({"verdict": "REJECT", "start_cue": 6, "end_cue": 20, "reason": "结尾半句，先截在20"}, ensure_ascii=False),
        _extension(after=(40, "句子40")),
    ])
    ctx_ord = make_ctx(tmp_path / "ord", remote=FakeRemote(), cpa=cpa_ord)
    out_ord = si.step1_boundary(ctx_ord)
    assert not out_ord.ok and out_ord.reason == "BOUNDARY_LENGTH_UNREASONABLE"
    assert not (ctx_ord.clip_dir / "boundary" / "decision.json").exists()
    assert any("被" in line and "6–40" in line for line in out_ord.process)

    # Finding 1 (must-cut): refused extension leaves 6-20 standing -> normal BOUNDARY_SHORT_QUEUED handling follows.
    cpa_mc = FakeCpa([
        json.dumps({"verdict": "REJECT", "start_cue": 6, "end_cue": 20, "reason": "结尾半句，先截在20"}, ensure_ascii=False),
        _extension(after=(40, "句子40")),
    ])
    ctx_mc = make_ctx(tmp_path / "mc", remote=FakeRemote(), cpa=cpa_mc)
    _seated(ctx_mc, "MUST_CUT")
    out_mc = si.step1_boundary(ctx_mc)
    assert not out_mc.ok and out_mc.reason == "BOUNDARY_SHORT_QUEUED"
    decision = json.loads((ctx_mc.clip_dir / "boundary" / "decision.json").read_text(encoding="utf-8"))
    assert decision["proposal"] == [6, 40]
    assert decision["adopted"]["start_cue"] == 6 and decision["adopted"]["end_cue"] == 20
    assert decision["story"] == {"start_cue": 6, "end_cue": 20}
    assert decision["short"]["duration_s"] == 30.0


def test_boundary_refused_unparseable_advances_request_identity_with_real_cpa_journal(tmp_path, monkeypatch):
    import re
    import shlex
    from pathlib import Path
    from test_cpa_completeness import HEAD, _load, _message, _Resp, _sse

    from src.autoslice.manual_lane import adapters
    from src.autoslice.manual_lane.adapters import SshCpaJudge

    cpa_text = _load(adapters.CPA_TEXT_SCRIPT, "cpa_text_boundary_identity")
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.invalid/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-only")
    monkeypatch.setattr(cpa_text.time, "sleep", lambda _s: None)

    def _stream(text):
        return _sse(HEAD + [
            {"type": "response.output_text.delta", "delta": text},
            {"type": "response.output_text.done", "text": text},
            {"type": "response.output_item.done", "item": _message(text, "completed")},
            {"type": "response.completed", "response": {
                "status": "completed", "output": [_message(text, "completed")],
                "usage": {"input_tokens": 11, "output_tokens": 5}}},
        ])

    class _JournalRemote(FakeRemote):
        def __init__(self, responses, jdir):
            super().__init__()
            self.responses = list(responses)
            self.journal_dir = jdir
            self.journal_dir.mkdir(parents=True, exist_ok=True)
            self.http_requests = []
            self.logical_ids = []
            monkeypatch.setattr(cpa_text.urllib.request, "urlopen", self._urlopen)

        def _urlopen(self, request, timeout):
            self.http_requests.append(json.loads(request.data))
            return _Resp(_stream(self.responses.pop(0)))

        def run_script(self, *, name, body, timeout):
            if name.startswith("cpa-read-"):
                cid = name.removeprefix("cpa-read-")
                out = self.files.get(f"/r/clip/cpa/{cid}.out.json")
                return RemoteResult(0, out.decode("utf-8"), "") if out is not None else RemoteResult(1, "", "")
            if name.startswith("cpa-"):
                cid = name.removeprefix("cpa-")
                wire = body.split("<<'MANUAL_LANE_PROMPT_EOF'\n", 1)[1].split("\nMANUAL_LANE_PROMPT_EOF", 1)[0] + "\n"
                m_flags = re.search(
                    r"--model\s+\S+\s+--effort\s+\S+\s+--tries\s+\d+(?:\s+--max-tokens\s+\d+)?\s+--stream", body
                )
                m_lid = re.search(r"--logical-id\s+([0-9a-f]+)", body)
                assert m_flags is not None and m_lid is not None
                logical_id = m_lid.group(1)
                self.logical_ids.append((cid, logical_id))
                prompt_p = self.journal_dir / f"{cid}.{len(self.logical_ids)}.prompt"
                out_p = self.journal_dir / f"{cid}.{len(self.logical_ids)}.out.json"
                prompt_p.write_bytes(wire.encode("utf-8"))
                argv = [str(prompt_p), str(out_p), *shlex.split(m_flags.group()),
                        "--journal-dir", str(self.journal_dir), "--logical-id", logical_id]
                rc = cpa_text.main(argv)
                self.files[f"/r/clip/cpa/{cid}.out.json"] = out_p.read_bytes()
                return RemoteResult(rc, "", "")
            return super().run_script(name=name, body=body, timeout=timeout)

    # Case A: invalid verdict ("MAYBE") refused in 1 request on run 1 -> next run must get a new --logical-id and not replay terminal
    bad_maybe = json.dumps({"verdict": "MAYBE", "start_cue": 6, "end_cue": 40, "reason": "不确定"}, ensure_ascii=False)
    good_accept = json.dumps({"verdict": "ACCEPT", "start_cue": 6, "end_cue": 40, "reason": "完整"}, ensure_ascii=False)
    remote_a = _JournalRemote([bad_maybe, good_accept], journal_dir / "a")
    ctx_a = make_ctx(tmp_path / "case-a", remote=remote_a, cpa=SshCpaJudge(remote_a))
    out_a1 = si.step1_boundary(ctx_a)
    assert not out_a1.ok and out_a1.reason == "CPA_RESPONSE_UNPARSEABLE"
    assert len(remote_a.http_requests) == 1
    assert len(list((ctx_a.clip_dir / "cpa").glob("boundary.rejected-*.completion.txt"))) == 1
    out_a2 = si.step1_boundary(ctx_a)
    assert out_a2.ok, (out_a2.reason, out_a2.note)
    assert [cid for cid, _lid in remote_a.logical_ids] == ["boundary", "boundary--reply-2"]
    assert len({lid for _cid, lid in remote_a.logical_ids}) == 2

    # Case B: non-JSON prose ("没有 JSON") refused on run 1 -> next run must advance call_id + logical_id and recover
    remote_b = _JournalRemote(["没有 JSON", good_accept], journal_dir / "b")
    ctx_b = make_ctx(tmp_path / "case-b", remote=remote_b, cpa=SshCpaJudge(remote_b))
    out_b1 = si.step1_boundary(ctx_b)
    assert not out_b1.ok and out_b1.reason == "CPA_RESPONSE_UNPARSEABLE"
    assert len(list(Path(ctx_b.clip_dir / "cpa").glob("boundary.rejected-*.completion.txt"))) == 1
    out_b2 = si.step1_boundary(ctx_b)
    assert out_b2.ok, (out_b2.reason, out_b2.note)
    assert [cid for cid, _lid in remote_b.logical_ids] == ["boundary", "boundary--reply-2"]
    assert len({lid for _cid, lid in remote_b.logical_ids}) == 2


def test_boundary_retry_window_strictly_widens_sparse_cues_on_both_sides(tmp_path):
    # Finding 6: 120 cues at 10 s/cue, proposal 50-60. Round 0 shows 38..72; retry must strictly widen both sides.
    cpa = FakeCpa([
        json.dumps({"verdict": "REJECT", "start_cue": 50, "end_cue": 60, "reason": "两头都要扩展"}, ensure_ascii=False),
        json.dumps({"verdict": "ACCEPT", "start_cue": 30, "end_cue": 78, "reason": "扩展到新窗口内"}, ensure_ascii=False),
    ])
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa, bcut_text=srt(120, step=10.0))
    ctx.state.data["clip"].update(start_cue=50, end_cue=60, proposal={"start_cue": 50, "end_cue": 60})
    ctx.state.save()
    out = si.step1_boundary(ctx)
    assert out.ok, (out.reason, out.note)
    assert cpa.calls == ["boundary", "boundary-retry-1"]
    assert "\n30 " not in cpa.prompts[0] and "\n30 " in cpa.prompts[1]
    assert "\n78 " not in cpa.prompts[0] and "\n78 " in cpa.prompts[1]
    assert (ctx.state.data["clip"]["start_cue"], ctx.state.data["clip"]["end_cue"]) == (30, 78)


def test_boundary_reject_skips_retry_when_no_more_cues_exist_on_either_side(tmp_path):
    # Finding 6: srt(40) with proposal 6-40 shows cues 1..40 in round 0; no wider cues exist, so skip retry and stop.
    cpa = FakeCpa([
        json.dumps({"verdict": "REJECT", "start_cue": 6, "end_cue": 40, "reason": "整段都是半句"}, ensure_ascii=False),
        json.dumps({"verdict": "ACCEPT", "start_cue": 4, "end_cue": 38, "reason": "不该再问到"}, ensure_ascii=False),
    ])
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    out = si.step1_boundary(ctx)
    assert not out.ok and out.reason == "BOUNDARY_NOT_ACCEPTED"
    assert cpa.calls == ["boundary"]


def test_boundary_cue_int_accepts_integral_floats_and_handles_oversized_digits_without_raising(tmp_path):
    # Finding 7: integral floats accepted in ACCEPT and extend; bool/non-integral rejected.
    assert step_cut._cue_int(6.0) == 6
    assert step_cut._cue_int(6.5) is None
    assert step_cut._cue_int(float("nan")) is None
    assert step_cut._cue_int(float("inf")) is None
    assert step_cut._cue_int(True) is None
    assert step_cut._cue_int("9" * 4301) is None

    cpa_acc = FakeCpa([json.dumps({"verdict": "ACCEPT", "start_cue": 6.0, "end_cue": 40.0, "reason": "整数浮点"}, ensure_ascii=False)])
    ctx_acc = make_ctx(tmp_path / "float-acc", remote=FakeRemote(), cpa=cpa_acc)
    out_acc = si.step1_boundary(ctx_acc)
    assert out_acc.ok, (out_acc.reason, out_acc.note)
    assert (ctx_acc.state.data["clip"]["start_cue"], ctx_acc.state.data["clip"]["end_cue"]) == (6, 40)

    cpa_ext = FakeCpa([
        json.dumps({"verdict": "ACCEPT", "start_cue": 6, "end_cue": 20, "reason": "偏短"}, ensure_ascii=False),
        json.dumps({"before": None, "after": {"end_cue": 40.0, "link": "句子30", "why": "同一件事"}}, ensure_ascii=False),
    ])
    ctx_ext = make_ctx(tmp_path / "float-ext", remote=FakeRemote(), cpa=cpa_ext)
    out_ext = si.step1_boundary(ctx_ext)
    assert out_ext.ok, (out_ext.reason, out_ext.note)
    assert (ctx_ext.state.data["clip"]["start_cue"], ctx_ext.state.data["clip"]["end_cue"]) == (6, 40)

    # Finding 10: 4301-digit string cue does not raise ValueError; keeps CPA record and proceeds to retry/recover.
    cpa_big = FakeCpa([
        json.dumps({"verdict": "REJECT", "start_cue": "9" * 4301, "end_cue": 40, "reason": "超长数字"}, ensure_ascii=False),
        json.dumps({"verdict": "ACCEPT", "start_cue": 4, "end_cue": 44, "reason": "正常起止"}, ensure_ascii=False),
    ])
    ctx_big = make_ctx(tmp_path / "big-digits", remote=FakeRemote(), cpa=cpa_big, bcut_text=srt(60))
    out_big = si.step1_boundary(ctx_big)
    assert out_big.ok, (out_big.reason, out_big.note)
    assert cpa_big.calls == ["boundary", "boundary-retry-1"] and len(out_big.cpa) == 2


def test_boundary_extend_malformed_side_is_ignored_with_note_and_short_must_cut_still_queues(tmp_path):
    # Finding 8: non-object side in boundary-extend is ignored with a note in PROCESS, and short must-cut still queues.
    cpa = FakeCpa([
        json.dumps({"verdict": "ACCEPT", "start_cue": 6, "end_cue": 7, "reason": "完整短片"}, ensure_ascii=False),
        json.dumps({"before": [], "after": None}, ensure_ascii=False),
    ])
    ctx = make_ctx(tmp_path / "malformed-side", remote=FakeRemote(), cpa=cpa)
    ctx.state.data["clip"].update(start_cue=6, end_cue=7, proposal={"start_cue": 6, "end_cue": 7})
    ctx.state.save()
    _seated(ctx, "MUST_CUT")
    out = si.step1_boundary(ctx)
    assert not out.ok and out.reason == "BOUNDARY_SHORT_QUEUED"
    decision = json.loads((ctx.clip_dir / "boundary" / "decision.json").read_text(encoding="utf-8"))
    assert decision["adopted"]["start_cue"] == 6 and decision["adopted"]["end_cue"] == 7
    assert any("非对象" in n or "格式" in n for n in decision["extend"]["notes"])
    assert any("非对象" in line or "格式" in line for line in out.process)

    # Object root without before/after keys also tolerates without failing
    cpa_empty = FakeCpa([
        json.dumps({"verdict": "ACCEPT", "start_cue": 6, "end_cue": 7, "reason": "完整短片"}, ensure_ascii=False),
        '{"other": 1}',
    ])
    ctx_empty = make_ctx(tmp_path / "empty-obj", remote=FakeRemote(), cpa=cpa_empty)
    _seated(ctx_empty, "MUST_CUT")
    assert si.step1_boundary(ctx_empty).reason == "BOUNDARY_SHORT_QUEUED"


def test_boundary_reject_non_positive_duration_correction_is_rejected(tmp_path):
    # Finding 9: cue 6 starts at 10.0 s while cue 20 ends at 5.0 s -> -5.0 s duration must be rejected as invalid.
    lines = []
    for i in range(1, 61):
        if i == 6:
            s, e = 10.0, 12.0
        elif i == 20:
            s, e = 4.0, 5.0
        elif i == 21:
            s, e = 10.0, 10.0
        else:
            s, e = (i - 1) * 2.0, i * 2.0
        lines.append(f"{i}\n{si.fmt_ts(s)} --> {si.fmt_ts(e)}\n句子{i}")
    bad_srt = "\n\n".join(lines) + "\n"

    cpa_neg = FakeCpa([
        json.dumps({"verdict": "REJECT", "start_cue": 6, "end_cue": 20, "reason": "改到20"}, ensure_ascii=False),
        json.dumps({"verdict": "REJECT", "start_cue": 6, "end_cue": 20, "reason": "还是20"}, ensure_ascii=False),
    ])
    ctx_neg = make_ctx(tmp_path / "neg", remote=FakeRemote(), cpa=cpa_neg, bcut_text=bad_srt)
    _seated(ctx_neg, "MUST_CUT")
    out_neg = si.step1_boundary(ctx_neg)
    assert not out_neg.ok and out_neg.reason == "BOUNDARY_NOT_ACCEPTED"
    assert not (ctx_neg.clip_dir / "boundary" / "decision.json").exists()
    assert "非正" in cpa_neg.prompts[1]

    cpa_zero = FakeCpa([
        json.dumps({"verdict": "REJECT", "start_cue": 6, "end_cue": 21, "reason": "0秒"}, ensure_ascii=False),
        json.dumps({"verdict": "REJECT", "start_cue": 6, "end_cue": 21, "reason": "还是0秒"}, ensure_ascii=False),
    ])
    ctx_zero = make_ctx(tmp_path / "zero", remote=FakeRemote(), cpa=cpa_zero, bcut_text=bad_srt)
    _seated(ctx_zero, "MUST_CUT")
    out_zero = si.step1_boundary(ctx_zero)
    assert not out_zero.ok and out_zero.reason == "BOUNDARY_NOT_ACCEPTED"
    assert not (ctx_zero.clip_dir / "boundary" / "decision.json").exists()


def test_boundary_invalid_verdict_four_rounds_one_request_per_round_becomes_stuck_never_waiting_provider(tmp_path, monkeypatch):
    import re
    import shlex
    from test_cpa_completeness import HEAD, _load, _message, _Resp, _sse

    from src.autoslice.manual_lane import adapters, auto_run
    from src.autoslice.manual_lane.adapters import Adapters, SshCpaJudge
    from src.autoslice.manual_lane.runner import Ctx, run_clip
    from src.autoslice.manual_lane.stages import STEPS
    from src.autoslice.manual_lane.state import ClipState

    cpa_text = _load(adapters.CPA_TEXT_SCRIPT, "cpa_text_round_stuck")
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.invalid/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-only")
    monkeypatch.setattr(cpa_text.time, "sleep", lambda _s: None)
    monkeypatch.setattr(auto_run, "deployed_version", lambda: "")

    bad_maybe = json.dumps({"verdict": "MAYBE", "start_cue": 6, "end_cue": 40, "reason": "不确定"}, ensure_ascii=False)

    def _stream(text):
        return _sse(HEAD + [
            {"type": "response.output_text.delta", "delta": text},
            {"type": "response.output_text.done", "text": text},
            {"type": "response.output_item.done", "item": _message(text, "completed")},
            {"type": "response.completed", "response": {
                "status": "completed", "output": [_message(text, "completed")],
                "usage": {"input_tokens": 11, "output_tokens": 5}}},
        ])

    class _JournalRemote(FakeRemote):
        def __init__(self):
            super().__init__()
            self.http_requests = []
            self.logical_ids = []
            monkeypatch.setattr(cpa_text.urllib.request, "urlopen", self._urlopen)

        def _urlopen(self, request, timeout):
            self.http_requests.append(json.loads(request.data))
            return _Resp(_stream(bad_maybe))

        def run_script(self, *, name, body, timeout):
            if name.startswith("cpa-read-"):
                cid = name.removeprefix("cpa-read-")
                out = self.files.get(f"/r/clip/cpa/{cid}.out.json")
                return RemoteResult(0, out.decode("utf-8"), "") if out is not None else RemoteResult(1, "", "")
            if name.startswith("cpa-"):
                cid = name.removeprefix("cpa-")
                wire = body.split("<<'MANUAL_LANE_PROMPT_EOF'\n", 1)[1].split("\nMANUAL_LANE_PROMPT_EOF", 1)[0] + "\n"
                m_flags = re.search(
                    r"--model\s+\S+\s+--effort\s+\S+\s+--tries\s+\d+(?:\s+--max-tokens\s+\d+)?\s+--stream", body
                )
                m_lid = re.search(r"--logical-id\s+([0-9a-f]+)", body)
                assert m_flags is not None and m_lid is not None
                logical_id = m_lid.group(1)
                self.logical_ids.append((cid, logical_id))
                prompt_p = journal_dir / f"{cid}.{len(self.logical_ids)}.prompt"
                out_p = journal_dir / f"{cid}.{len(self.logical_ids)}.out.json"
                prompt_p.write_bytes(wire.encode("utf-8"))
                argv = [str(prompt_p), str(out_p), *shlex.split(m_flags.group()),
                        "--journal-dir", str(journal_dir), "--logical-id", logical_id]
                rc = cpa_text.main(argv)
                self.files[f"/r/clip/cpa/{cid}.out.json"] = out_p.read_bytes()
                return RemoteResult(rc, "", "")
            return super().run_script(name=name, body=body, timeout=timeout)

    remote = _JournalRemote()
    seed_ctx = make_ctx(tmp_path, remote=remote, cpa=SshCpaJudge(remote))
    round_cids = []

    def _run_clips(cids):
        round_cids.append(list(cids))
        for cid in cids:
            st = ClipState.load(tmp_path / "state" / f"{cid}.json")
            clip_ctx = Ctx(state=st, session=st.data["session"], clip_dir=tmp_path / "clips" / cid,
                           remote_dir=seed_ctx.remote_dir, adapters=Adapters(remote=remote, cpa=SshCpaJudge(remote)))
            run_clip(clip_ctx, [STEPS[0]])
        return 0

    rounds = [auto_run.auto_round(tmp_path, discover_days=lambda: {"days": []}, run_clips=_run_clips, upload=False)
              for _ in range(4)]
    assert round_cids == [["clip"], ["clip"], ["clip"]]
    assert [r["waiting_provider"] for r in rounds] == [[], [], [], []]
    assert rounds[-1]["stuck"] == [{"cid": "clip", "step": "boundary", "reason": "CPA_RESPONSE_UNPARSEABLE",
                                    "times": 3, "version": ""}]
    assert len(remote.http_requests) == 3
    assert [cid for cid, _lid in remote.logical_ids] == ["boundary", "boundary--reply-2", "boundary--reply-3"]
    assert len({lid for _cid, lid in remote.logical_ids}) == 3


def test_boundary_fallback_model_non_object_root_is_cpa_response_unparseable(tmp_path):
    class _QuotaFallbackCpa:
        def __init__(self, accept_first=False):
            self.calls = []
            self.accept_first = accept_first

        def ask(self, *, call_id, prompt_text, model, effort, remote_dir, **_):
            self.calls.append((call_id, model))
            if self.accept_first and call_id == "boundary":
                ans = json.dumps({"verdict": "ACCEPT", "start_cue": 6, "end_cue": 7, "reason": "短故事"},
                                 ensure_ascii=False)
                return {"call_id": call_id, "model": model, "effort": effort, "stream": True, "rc": 0,
                        "complete": True, "elapsed_s": 0.1, "attempts": 1, "response_text": ans, "stderr_tail": ""}
            if model == "gpt-6.2-sol":
                return {"call_id": call_id, "model": model, "effort": effort, "stream": True, "rc": 1,
                        "complete": False, "reason": "CPA_QUOTA_REFUSED: HTTP 403", "elapsed_s": 0.1,
                        "attempts": 1, "response_text": "", "stderr_tail": "CPA_QUOTA_REFUSED: HTTP 403"}
            return {"call_id": call_id, "model": model, "effort": effort, "stream": True, "rc": 0,
                    "complete": True, "elapsed_s": 0.1, "attempts": 1, "response_text": '[{"x": 1}]',
                    "stderr_tail": ""}

    cpa1 = _QuotaFallbackCpa(accept_first=False)
    ctx1 = make_ctx(tmp_path / "b1", remote=FakeRemote(), cpa=cpa1)
    out1 = si.step1_boundary(ctx1)
    assert not out1.ok and out1.reason == "CPA_RESPONSE_UNPARSEABLE"
    assert cpa1.calls == [("boundary", "gpt-6.2-sol"), ("boundary--fallback", "gemini-4-neon")]
    assert not list((tmp_path / "b1" / "cache" / "cpa").glob("*.json"))
    assert len(list((ctx1.clip_dir / "cpa").glob("boundary--fallback.rejected-*.completion.txt"))) == 1

    cpa2 = _QuotaFallbackCpa(accept_first=True)
    ctx2 = make_ctx(tmp_path / "ext", remote=FakeRemote(), cpa=cpa2)
    out2 = si.step1_boundary(ctx2)
    assert not out2.ok and out2.reason == "CPA_RESPONSE_UNPARSEABLE"
    assert cpa2.calls == [("boundary", "gpt-6.2-sol"), ("boundary-extend", "gpt-6.2-sol"),
                          ("boundary-extend--fallback", "gemini-4-neon")]
    assert len(list((ctx2.clip_dir / "cpa").glob("boundary-extend--fallback.rejected-*.completion.txt"))) == 1


def test_boundary_reject_non_positive_correction_rejected_before_peaks_widening(tmp_path):
    lines = []
    for i in range(1, 61):
        if i == 6:
            s, e = 10.0, 12.0
        elif i == 20:
            s, e = 4.0, 5.0
        else:
            s, e = (i - 1) * 2.0, i * 2.0
        lines.append(f"{i}\n{si.fmt_ts(s)} --> {si.fmt_ts(e)}\n句子{i}")
    bad_srt = "\n\n".join(lines) + "\n"

    cpa = FakeCpa([
        json.dumps({"verdict": "REJECT", "start_cue": 6, "end_cue": 20, "reason": "改到20"}, ensure_ascii=False),
        json.dumps({"verdict": "REJECT", "start_cue": 6, "end_cue": 20, "reason": "还是20"}, ensure_ascii=False),
    ])
    ctx = make_ctx(tmp_path / "neg-peaks", remote=FakeRemote(), cpa=cpa, bcut_text=bad_srt)
    _seated(ctx, "MUST_CUT", peaks=[{"quote": "句子38", "who": "", "at_ms": 74_000, "why": "落点"}])
    out = si.step1_boundary(ctx)
    assert not out.ok and out.reason == "BOUNDARY_NOT_ACCEPTED"
    assert cpa.calls == ["boundary", "boundary-retry-1"]
    assert "非正" in cpa.prompts[1]
    assert not (ctx.clip_dir / "boundary" / "decision.json").exists()


def test_json_from_text_and_boundary_handle_oversized_bare_integer_as_unparseable(tmp_path):
    import pytest

    from src.autoslice.manual_lane import srt_text, step_io
    from src.autoslice.manual_lane.runner import run_clip
    from src.autoslice.manual_lane.stages import STEPS

    raw = '{"verdict": "REJECT", "start_cue": ' + ("9" * 4301) + ', "end_cue": 40, "reason": "超长裸整数"}'
    with pytest.raises(srt_text.JSONMalformed):
        srt_text._json_from_text(raw)
    assert step_io._answer_problem(raw, None) == ""

    ctx = make_ctx(tmp_path / "bare-int", remote=FakeRemote(), cpa=FakeCpa([raw]))
    report = run_clip(ctx, [STEPS[0]])
    assert report.stopped_at == 1 and report.reason == "CPA_RESPONSE_UNPARSEABLE"
    step_rec = ctx.state.data["steps"]["boundary"]
    assert len(step_rec["cpa"]) == 1
    assert len(list((ctx.clip_dir / "cpa").glob("boundary.rejected-*.completion.txt"))) == 1
    assert not list((tmp_path / "bare-int" / "cache" / "cpa").glob("*.json"))


# ----- step 3: subtitles ------------------------------------------------------------------------
def _listen_records(*rows):
    """rows: (win, slug, text) for a record at the window start (cue 1 sits at 0 s)."""
    names = {m["slug"]: m["name"] for m in si.LISTEN_MODELS}   # records carry the model name: the scope key
    return [{"win": win, "model": names[slug], "slug": slug, "status": "OK",
             "lines": [{"start_s": 0.0, "text": text}]} for win, slug, text in rows]


def _subs_remote(records, *, listen_fail=False, jianying_text=None):
    def script(name, body, remote):
        if name.startswith("s3-listen"):
            if listen_fail:
                return RemoteResult(1, "", "boom")
            remote.files[f"{RD}/listen/out/all.json"] = json.dumps(records, ensure_ascii=False).encode()
            return RemoteResult(0, "LISTEN_DONE\n", "")
        if name == "s3-asr":
            return RemoteResult(0, "ASR_OK\n", "")
        return RemoteResult(0, "", "")
    remote = FakeRemote(on_script=script)
    text = srt(3, step=2.0).encode()
    remote.files[f"{RD}/asr/bcut.srt"] = text
    remote.files[f"{RD}/asr/jianying.srt"] = jianying_text.encode() if jianying_text else text
    return remote


def _subs_ctx(tmp_path, records, rows=(), *, omit=(), listen_fail=False, reviewer_labels=None, jianying_text=None):
    cpa = IdCpa(rows, omit)
    ctx = make_ctx(tmp_path, remote=_subs_remote(records, listen_fail=listen_fail, jianying_text=jianying_text),
                   cpa=cpa, bcut_text=srt(3, step=2.0), reviewer_labels=reviewer_labels)
    return ctx, cpa


def _final_text(ctx, cue_index=1):
    cues = si.parse_srt((ctx.clip_dir / "subs" / "final.srt").read_text(encoding="utf-8"))
    return next(c["text"] for c in cues if c["id"] == cue_index)


def _doubt(ctx, doubt_id):
    return next(d for d in json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8"))
                if d["id"] == doubt_id)


def test_every_cue_is_adjudicated_in_two_passes_even_when_no_source_differs(tmp_path):
    ctx, cpa = _subs_ctx(tmp_path, [], [])
    assert si.step3_subtitles(ctx).ok
    assert sorted(cpa.calls) == ["adjudicate-p1-1", "adjudicate-p2-1"]
    assert _doubt(ctx, "d002")["state"] == "KEEP" and _doubt(ctx, "d002")["maintained"] is False


def test_unmaintained_doubt_goes_to_cpa_and_cpa_picks_a_witness_reading(tmp_path):
    # Only one family (Gemini 3.8) reads the cue differently: not maintained, but CPA still chooses the word (U108).
    records = _listen_records(("C000", "38m", "句子甲"))
    rows = [{"id": "d001", "state": "ADOPT", "final_text": "句子甲", "affects_meaning": True,
             "reason_key": "同音", "basis": "后文重述", "confidence": "高"}]
    ctx, cpa = _subs_ctx(tmp_path, records, rows)
    out = si.step3_subtitles(ctx)
    assert out.ok, out.note
    assert _final_text(ctx) == "句子甲"
    assert "adjudicate-p1-1" in cpa.calls
    doubt = _doubt(ctx, "d001")
    assert doubt["maintained"] is False and doubt["state"] == "ADOPT" and doubt["source"] == "CPA"
    assert any("维持（两家相同非现稿读法）0 条" in line for line in out.process)


def test_maintained_doubt_is_judged_by_cpa_too(tmp_path):
    # jianying hears 句子乙 and Gemini 3.8 agrees: two families, maintained; CPA still makes the choice
    records = _listen_records(("C000", "38m", "句子乙"))
    rows = [{"id": "d001", "state": "ADOPT", "final_text": "句子乙", "affects_meaning": True, "reason_key": "同音"}]
    ctx, _ = _subs_ctx(tmp_path, records, rows, jianying_text=_JY_CUE1)
    assert si.step3_subtitles(ctx).ok
    assert _final_text(ctx) == "句子乙" and _doubt(ctx, "d001")["maintained"] is True


def test_cpa_may_keep_the_current_text_for_a_maintained_doubt_with_a_stated_conflict(tmp_path):
    records = _listen_records(("C000", "38m", "句子乙"))
    rows = [{"id": "d001", "state": "KEEP", "final_text": "随便", "affects_meaning": False, "reason_key": "同音",
             "against_maintained": "后文明确说的是句子1，与句子乙冲突"}]
    ctx, _ = _subs_ctx(tmp_path, records, rows, jianying_text=_JY_CUE1)
    assert si.step3_subtitles(ctx).ok
    assert _final_text(ctx) == "句子1"


def test_cpa_choice_stands_after_the_fact_even_against_a_two_family_reading(tmp_path):
    # TASK-5 item 3: no post-hoc override.  CPA's KEEP stands even though two families read 句子乙.
    records = _listen_records(("C000", "38m", "句子乙"))
    rows = [{"id": "d001", "state": "KEEP", "final_text": "随便", "affects_meaning": False, "reason_key": "同音"}]
    ctx, _ = _subs_ctx(tmp_path, records, rows, jianying_text=_JY_CUE1)
    assert si.step3_subtitles(ctx).ok
    assert _final_text(ctx) == "句子1" and _doubt(ctx, "d001")["maintained"] is True


def test_cpa_unresolved_without_a_text_stops_the_clip(tmp_path):
    records = _listen_records(("C000", "38m", "句子乙"))
    rows = [{"id": "d001", "state": "UNRESOLVED", "final_text": "", "affects_meaning": True, "reason_key": "数字"}]
    ctx, _ = _subs_ctx(tmp_path, records, rows)
    out = si.step3_subtitles(ctx)
    assert not out.ok and out.reason == "DOUBT_UNRESOLVED"


def test_cpa_unresolved_with_a_best_guess_becomes_a_low_confidence_choice(tmp_path):
    # TASK-5 item 3: CPA must still pick the most likely writing; it is applied and marked 低.
    records = _listen_records(("C000", "38m", "句子乙"))
    rows = [{"id": "d001", "state": "UNRESOLVED", "final_text": "句子丙", "affects_meaning": True, "reason_key": "数字",
             "confidence": "高"}]
    ctx, _ = _subs_ctx(tmp_path, records, rows)
    assert si.step3_subtitles(ctx).ok
    doubt = _doubt(ctx, "d001")
    assert doubt["state"] == "FIT_CONTEXT" and doubt["confidence"] == "低"
    assert _final_text(ctx) == "句子丙"


def test_cpa_row_missing_for_a_doubt_is_a_tool_failure(tmp_path):
    records = _listen_records(("C000", "38m", "句子甲"))
    ctx, _ = _subs_ctx(tmp_path, records, [], omit=["d001"])
    out = si.step3_subtitles(ctx)
    assert not out.ok and out.reason == "CPA_ROW_MISSING"


def test_listen_tool_failure_stops_as_tool_failure(tmp_path):
    ctx, _ = _subs_ctx(tmp_path, [], None, listen_fail=True)
    out = si.step3_subtitles(ctx)
    assert not out.ok and out.reason == "LISTEN_TOOL_FAILED"


def test_reviewer_listening_is_final_and_never_reaches_cpa(tmp_path):

    records = _listen_records(("C000", "38m", "句子甲"))
    # make_ctx's clip starts at 10 s of the source; cue 1 is 0-2 s of the cut, so 10-12 s of the source
    label = {"cid": "clip", "doubt_id": "d001", "abs_start_ms": 10000, "abs_end_ms": 12000, "current": "句子1",
             "text": "句子一", "choice": "custom", "note": ""}
    ctx, cpa = _subs_ctx(tmp_path, records, [], reviewer_labels={"export_sha256": "f" * 64,
                                                             "labels": [label]})
    out = si.step3_subtitles(ctx)
    assert out.ok, out.note
    assert not any('"d001"' in q for q in cpa.prompts)
    assert _final_text(ctx) == "句子一"


def test_stale_reviewer_anchor_stops_before_any_cpa_call(tmp_path):
    label = {"cid": "clip", "doubt_id": "d001", "abs_start_ms": 10000, "abs_end_ms": 12000, "current": "别的",
             "text": "句子一", "choice": "custom", "note": ""}
    ctx, cpa = _subs_ctx(tmp_path, [], None, reviewer_labels={"export_sha256": "f" * 64, "labels": [label]})
    out = si.step3_subtitles(ctx)
    assert out.reason == "REVIEWER_LABEL_STALE" and cpa.calls == []


def test_asr_products_on_disk_are_reused_without_a_remote_asr_call(tmp_path):
    records = _listen_records(("C000", "38m", "句子甲"))
    ctx, _ = _subs_ctx(tmp_path, records, [])
    text = srt(3, step=2.0)
    (ctx.clip_dir / "asr").mkdir(parents=True)
    (ctx.clip_dir / "asr" / "bcut.srt").write_text(text, encoding="utf-8")
    (ctx.clip_dir / "asr" / "jianying.srt").write_text(text, encoding="utf-8")
    assert si.step3_subtitles(ctx).ok
    assert not any(name == "s3-asr" for name, _ in ctx.adapters.remote.scripts)


# ----- step 6: title -----------------------------------------------------------------------------
def test_title_all_candidates_breaking_reviewers_title_rule_are_regenerated_then_the_step_stops(tmp_path):
    cand = [{"title": "【李豆沙】这段直接把人笑哭", "on_stage": ["李豆沙"], "punchline_person": "她笑哭了",
             "swap_check": {"holds_with_other_host": False}},
            {"title": "【李豆沙】当场破防了", "on_stage": ["李豆沙"], "punchline_person": "她破防了",
             "swap_check": {"holds_with_other_host": False}}]
    answer = json.dumps({"candidates": cand, "recommended": 0, "cover_lines": ["甲", "乙"]}, ensure_ascii=False)
    def script(name, body, remote):
        if name.startswith("s6-cover-job"):
            fake_cover(remote, "/r/clip")
        return RemoteResult(0, "", "")
    ctx = make_ctx(tmp_path, remote=FakeRemote(on_script=script), cpa=FakeCpa([answer, answer, answer]))
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs" / "final.srt").write_text(srt(3), encoding="utf-8")
    out = si.step6_title_cover(ctx)
    # two regenerations, then the step stops: no title is chosen by score (skill 八.4; until 11-24 the best was taken)
    # regen-2's prompt is identical to regen-1's (same rejected reasons): it is answered from the CPA cache (COST-07)
    assert ctx.adapters.cpa.calls == [*UNDERSTANDING_CALLS, "title", "title-regen-1"]
    assert out.reason == "TITLE_NOT_ACCEPTED" and not (ctx.clip_dir / "title" / "title.txt").exists()
    cands_doc = json.loads((ctx.clip_dir / "title" / "candidates.json").read_text(encoding="utf-8"))
    assert cands_doc["forced"] is True and "禁词" in "\n".join(out.process)



def test_title_skips_a_banned_recommendation_and_takes_the_next_one(tmp_path):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=FakeCpa([json.dumps({
        "candidates": [{"title": "【李豆沙】这段直接把人笑哭"}, {"title": "【李豆沙】半分钟后她破防了", "on_stage": ["李豆沙"], "punchline_person": "她破防了", "swap_check": {"holds_with_other_host": False, "reason": "测试"}}],
        "recommended": 0, "cover_lines": ["甲"]}, ensure_ascii=False)]))
    (ctx.clip_dir / "subs").mkdir(parents=True)
    (ctx.clip_dir / "subs" / "final.srt").write_text(srt(3), encoding="utf-8")

    def script(name, body, remote):
        fake_cover(remote, "/r/clip")
        return RemoteResult(0, "", "")
    ctx.adapters.remote.on_script = script
    out = si.step6_title_cover(ctx)
    assert out.ok, out.note
    assert (ctx.clip_dir / "title" / "title.txt").read_text(encoding="utf-8").strip() == "【李豆沙】半分钟后她破防了"


# ----- steps 8-9: package and review ---------------------------------------------------------------
def test_package_stops_when_an_input_product_is_missing(tmp_path):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=FakeCpa([]))
    out = sp.step8_package(ctx)
    assert not out.ok and out.reason == "PACKAGE_INPUT_MISSING"


def test_a_filler_answer_with_no_reading_goes_to_cpa_and_can_keep_its_current_text(tmp_path):
    # A spoken answer (嗯) follows normal full-mode adjudication; a legal KEEP can retain the original text.
    records = _listen_records(("C000", "38m", "句子甲"))
    filler = srt(3, step=2.0).replace("句子2", "嗯").encode()
    remote = _subs_remote(records)
    remote.files[f"{RD}/asr/bcut.srt"] = filler
    remote.files[f"{RD}/asr/jianying.srt"] = filler
    answer = {"id": "d002", "state": "KEEP", "final_text": "嗯", "confidence": "高", "affects_meaning": False}
    ctx = make_ctx(tmp_path, remote=remote, cpa=IdCpa([answer]), bcut_text=filler.decode("utf-8"))
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    doubts = json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8"))
    kept = next(d for d in doubts if d["cue_id"] == 2)
    assert kept["source"] == "CPA" and kept["state"] == "KEEP" and kept["final_text"] == "嗯"
    assert any('"d002"' in p for p in ctx.adapters.cpa.prompts)
    assert "嗯" in (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")
