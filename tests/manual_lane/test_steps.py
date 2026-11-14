"""Step bodies with fake remote and fake CPA: boundary (CPA's cut is adopted), subtitles (U108: every doubt goes to
CPA with all candidates; only an UNRESOLVED that affects meaning stops; 维护者's labels bypass), titles, package,
review.  No ssh, no ffmpeg, no network."""

from __future__ import annotations

import json

from fakes import UNDERSTANDING_CALLS, FakeCpa, FakeRemote, IdCpa, fake_cover, make_ctx, srt

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import stage_package as sp
from src.autoslice.manual_lane.adapters import RemoteResult

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
        {"verdict": "ACCEPT", "start_cue": 6, "end_cue": 7, "reason": "x"})]))
    assert si.step1_boundary(ctx).reason == "BOUNDARY_LENGTH_UNREASONABLE"


def test_boundary_cpa_failure_and_garbage_are_tool_failures(tmp_path):
    ctx = make_ctx(tmp_path / "a", remote=FakeRemote(), cpa=FakeCpa([None]))
    assert si.step1_boundary(ctx).reason == "CPA_CALL_FAILED"
    ctx = make_ctx(tmp_path / "b", remote=FakeRemote(), cpa=FakeCpa(["没有 JSON"]))
    assert si.step1_boundary(ctx).reason == "CPA_RESPONSE_UNPARSEABLE"


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
def test_title_all_candidates_breaking_reviewers_title_rule_are_regenerated_then_the_best_is_taken(tmp_path):
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
    # two regenerations; nothing silently dropped: the best candidate is taken and the unpassed item is recorded
    # regen-2's prompt is identical to regen-1's (same rejected reasons): it is answered from the CPA cache (COST-07)
    assert ctx.adapters.cpa.calls == [*UNDERSTANDING_CALLS, "title", "title-regen-1", "cover-brief", "cover-emote"]
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
