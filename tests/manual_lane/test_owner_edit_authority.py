"""The channel owner's authority covers only the sentences he edited (subs/baseline.srt tells which); the review's
objections to those are recorded in full and kept by default (the owner-final rule): they never stop the package or the
seal, and `resolve-dissent` may replace the default afterwards.  Fake sentences only; no network, no model."""

from __future__ import annotations

import json

import pytest
from fakes import FakeRemote, make_ctx
from test_owner_decided_subtitles import OWNER, _bytes, _mark_owner, _owner_push_run
from test_review_once import GUEST, HOST, _cues, _package, _push, _review, _srt

from src.autoslice.manual_lane import cli, dissent_register, owner_edits, package_guards, review_owner
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import stage_package as sp
from src.autoslice.manual_lane.dissent_resolve import resolve_dissent
from src.autoslice.manual_lane.runner import RunReport, run_clip
from src.autoslice.manual_lane.state import ClipState, StateError
from src.autoslice.manual_lane.stages import STEPS
from src.autoslice.manual_lane.subtitle_track import TrackCue


def _cue(start, end, text, speaker=None) -> TrackCue:
    return {"start_s": start, "end_s": end, "text": text, "speaker": speaker}


BASE = [_cue(0, 2, "甲乙丙丁", HOST), _cue(2, 4, "戊己庚辛", GUEST), _cue(4, 6, "壬癸子丑", HOST)]


# ----- the alignment rule (pure) ----------------------------------------------------------------------------------------
def test_alignment_same_words_and_speaker_is_unedited_whitespace_ignored():
    final = [_cue(0, 2, "甲乙 丙丁", HOST), _cue(2, 4, "戊己\n庚辛", GUEST), _cue(4, 6, "壬癸子丑", HOST)]
    assert owner_edits.edited_cues(final, BASE) == []


def test_alignment_changed_words_is_edited():
    assert owner_edits.edited_cues([BASE[0], _cue(2, 4, "戊己庚壬", GUEST), BASE[2]], BASE) == [2]


def test_alignment_changed_speaker_is_edited_but_an_unlabelled_side_is_not_a_change():
    assert owner_edits.edited_cues([BASE[0], _cue(2, 4, "戊己庚辛", HOST), BASE[2]], BASE) == [2]
    assert owner_edits.edited_cues([_cue(0, 2, "甲乙丙丁"), BASE[1], BASE[2]], BASE) == []


def test_alignment_a_split_and_a_join_are_edited():
    split = [_cue(0, 1, "甲乙", HOST), _cue(1, 2, "丙丁", HOST), BASE[1], BASE[2]]
    assert owner_edits.edited_cues(split, BASE) == [1, 2]
    joined = [_cue(0, 4, "甲乙丙丁戊己庚辛", HOST), BASE[2]]
    assert owner_edits.edited_cues(joined, BASE) == [1]


def test_alignment_a_moved_time_is_edited_a_rounding_difference_is_not():
    assert owner_edits.edited_cues([BASE[0], _cue(2.5, 4.5, "戊己庚辛", GUEST), BASE[2]], BASE) == [2]
    assert owner_edits.edited_cues([BASE[0], _cue(2.02, 3.98, "戊己庚辛", GUEST), BASE[2]], BASE) == []


def test_alignment_a_cue_the_baseline_has_no_sentence_for_is_edited():
    assert owner_edits.edited_cues([*BASE, _cue(7, 8, "新增的句子", HOST)], BASE) == [4]
    assert owner_edits.edited_cues(BASE[:2], BASE) == []     # a sentence he deleted is not a decided cue any more


def test_track_numbers_follow_the_import_cues_even_when_normalisation_split_one():
    track = [_cue(0, 2, "甲乙丙丁"), _cue(2, 3, "戊己"), _cue(3, 4, "庚辛"), _cue(4, 6, "壬癸子丑"), _cue(9, 10, "无处可对")]
    assert owner_edits.track_numbers(track, BASE, [2]) == {2, 3, 5}     # his sentence 2 (split in two), and one no push cue covers


def test_a_push_without_a_baseline_has_no_edit_list(tmp_path):
    final = tmp_path / "final.srt"
    final.write_text(_srt([(0, 2, "甲")]), encoding="utf-8")
    assert owner_edits.pushed_edits(final, None, tmp_path / "baseline.srt") is None


# ----- the import records which sentences he edited ---------------------------------------------------------------------
def _baseline_text(changed=None):
    rows = [(0.0, 1.14, "雾深Girimi", "我想举手"), (1.14, 3.24, "李豆沙", "我想看李豆沙演霸总"),
            (2.80, 3.20, "雾深Girimi", "嗯"), (4.22, 6.62, "李豆沙", "我也要用气泡音叫你宝贝吗")]
    return _srt([(a, b, f"[{s}] {(changed or {}).get(i, t)}") for i, (a, b, s, t) in enumerate(rows, 1)])


def test_import_with_a_baseline_records_the_edited_cues_and_lists_the_file(tmp_path):
    ctx = make_ctx(tmp_path, remote=None, cpa=None)
    push = _push(tmp_path, ctx.state.data["clip"])
    (push / "subs/baseline.srt").write_text(_baseline_text({2: "我想看李豆沙演霸道总裁"}), encoding="utf-8")
    rec = si.record_import(ctx.state, ctx.clip_dir, push, source="s", authority="U130", decided_by="owner")
    assert rec["owner_edited"] == [2] and "baseline.srt" in rec["files"]
    assert (ctx.clip_dir / "import/baseline.srt").is_file()


def test_import_without_a_baseline_says_all_and_an_unmarked_import_says_nothing(tmp_path):
    ctx = make_ctx(tmp_path, remote=None, cpa=None)
    push = _push(tmp_path, ctx.state.data["clip"])
    rec = si.record_import(ctx.state, ctx.clip_dir, push, source="s", authority="U130", decided_by="owner")
    assert rec["owner_edited"] is None and "baseline.srt" not in rec["files"]
    (push / "subs/baseline.srt").write_text(_baseline_text(), encoding="utf-8")
    assert "owner_edited" not in si.record_import(ctx.state, ctx.clip_dir, push, source="s", authority="U130")


# ----- the review's proposals, sentence by sentence ------------------------------------------------------------------------
FINAL = _srt([(0, 2, "甲乙丙丁"), (2, 4, "戊己庚辛"), (4, 6, "壬癸子丑")])


def _owner_package(tmp_path, monkeypatch, answer, *, edited):
    """A clip at step 9 whose three sentences are the owner's; `edited`: the import cues he edited (None: no baseline)."""
    ctx, cpa, calls = _package(tmp_path, monkeypatch, answer, final=FINAL, doubts=[])
    (ctx.clip_dir / "import").mkdir()
    (ctx.clip_dir / "import/final.srt").write_text(FINAL, encoding="utf-8")
    ctx.state.data["imported"] = {**OWNER, "owner_edited": edited}
    ctx.state.save()
    return ctx, calls


def _proposals():
    return _review({"subtitles": [{"cue_id": 1, "text": "甲乙丙戊", "reason": "听感不符"},
                                  {"cue_id": 2, "text": "戊己庚壬", "reason": "上下文不通"}]},
                   holds=[{"cue_id": 3, "reason": "听不清"}, {"cue_id": 2, "reason": "这句定不下"}])


def test_with_a_baseline_the_review_changes_an_untouched_sentence_and_objects_to_an_edited_one(tmp_path, monkeypatch):
    ctx, calls = _owner_package(tmp_path, monkeypatch, _proposals(), edited=[2])
    out = sp.step9_review(ctx)
    assert [c["text"] for c in _cues(ctx, "subs/final.srt")] == ["甲乙丙戊", "戊己庚辛", "壬癸子丑"]   # 1 adopted, 2 kept
    assert out.ok and out.rewind == tuple(sp.REWIND_SUBTITLES)
    assert "复审修改了频道主未改动的第 1 句：甲乙丙丁 → 甲乙丙戊" in out.process
    assert [(d["kind"], d["cue"], d["reason"]) for d in ctx.state.data["dissent"]] == [
        ("text", 2, "上下文不通"), ("hold", 2, "这句定不下")]
    held = json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8"))
    assert [d["final_text"] for d in held] == ["壬癸子丑"]                    # the hold on the untouched sentence 3 is adopted
    assert "adopted" not in ctx.state.data["review"]["applied"] and calls == []


def test_without_a_baseline_every_proposal_is_an_objection_and_nothing_changes(tmp_path, monkeypatch):
    ctx, calls = _owner_package(tmp_path, monkeypatch, _proposals(), edited=None)
    before = _bytes_of(ctx)
    out = sp.step9_review(ctx)
    assert _bytes_of(ctx) == before and out.ok is True and out.reason == ""            # kept, and the step goes on
    assert [(d["kind"], d["cue"]) for d in ctx.state.data["dissent"]] == [("text", 1), ("text", 2), ("hold", 3), ("hold", 2)]
    assert not any("复审修改了频道主未改动" in line for line in out.process)


def _bytes_of(ctx):
    return (ctx.clip_dir / "subs/final.srt").read_bytes()


def test_a_decision_on_a_doubt_of_an_untouched_sentence_is_adopted(tmp_path, monkeypatch):
    doubt = {"id": "d1", "cue_id": 3, "start_s": 4.0, "end_s": 6.0, "state": "KEEP", "final_text": "壬癸子丑",
             "confidence": "高", "affects_meaning": False, "source": "owner-push", "basis": "x"}
    ctx, _ = _owner_package(tmp_path, monkeypatch, _review(), edited=[2])
    (ctx.clip_dir / "subs/doubts.json").write_text(json.dumps([doubt, {**doubt, "id": "d2", "start_s": 2.0, "end_s": 4.0}]),
                                                   encoding="utf-8")
    decisions = [{"id": i, "confidence": "低", "affects_meaning": True, "reason": "两读"} for i in ("d1", "d2")]
    answer, dissent = review_owner.split(ctx, {"doubt_decisions": decisions}, [])
    assert [d["id"] for d in answer["doubt_decisions"]] == ["d1"]
    assert [(d["kind"], d["text"]) for d in dissent] == [("decision", "d2")]


# ----- an objection is kept by default; a person may replace that -------------------------------------------------------------------------------------
def _with_objections(tmp_path):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=None)
    (ctx.clip_dir / "burn").mkdir(parents=True)
    (ctx.clip_dir / "burn/intro_result.json").write_text('{"intro_offset_ms": 0}', encoding="utf-8")      # what the audio check of step 8 reads
    (ctx.clip_dir / "subs").mkdir()
    (ctx.clip_dir / "subs/final.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\n句\n", encoding="utf-8")
    dissent_register.add(ctx.state, [
        {"kind": "text", "cue": 2, "text": "戊己庚辛", "proposed": "戊己庚壬", "value": "戊己庚壬", "reason": "上下文不通"},
        {"kind": "hold", "cue": 3, "text": "壬癸子丑", "proposed": "听不清", "value": "听不清", "reason": "听不清"}])
    ctx.state.save()
    return ctx


def test_step_8_does_not_stop_for_objections_which_stand_on_the_owner_final_rule_each_in_full(tmp_path):
    ctx = _with_objections(tmp_path)
    assert package_guards.check(ctx, tmp_path / "pkg")[0] is None
    first, second = ctx.state.data["dissent"]
    assert first["resolution"]["how"] == "keep" and first["resolution"]["by"] == "频道主定稿规则"
    assert first["resolution"]["default"] is True and first["resolution"]["at"] and first["resolution"]["reason"]
    lines = dissent_register.process_lines(ctx.state)
    assert lines[0] == "有 2 条异议已按频道主定稿规则保留" and len(lines) == 3
    assert "[1] 改字 第 2 句「戊己庚辛」：复审意见 戊己庚壬；复审理由 上下文不通；处置：保留频道主写法（频道主定稿规则：" in lines[1]
    assert "[2] 保留项 第 3 句「壬癸子丑」" in lines[2] and second["n"] == 2
    again = [{"kind": "text", "cue": 2, "text": "戊己庚辛", "proposed": "戊己庚壬", "value": "戊己庚壬", "reason": "上下文不通"}]
    dissent_register.add(ctx.state, again)                                              # the same answer again adds nothing
    assert len(ctx.state.data["dissent"]) == 2


def test_a_person_replaces_the_default_once_and_a_second_person_disposition_or_an_unknown_item_is_refused(tmp_path):
    ctx = _with_objections(tmp_path)
    out = resolve_dissent(tmp_path, "clip", item=1, how="keep", by="测试", reason="频道主写法")     # over the default
    assert out["on_default"] == [2]
    saved = ClipState.load(ctx.state.path).data["dissent"][0]["resolution"]
    assert (saved["how"], saved["by"], saved["reason"]) == ("keep", "测试", "频道主写法") and saved["at"]
    assert "default" not in saved
    process = (tmp_path / "clips/clip/PROCESS.md").read_text(encoding="utf-8")
    assert "## 异议处置" in process and "[1] 改字" in process and "保留频道主写法（测试：频道主写法" in process
    for item, how, by, reason in ((1, "keep", "x", "y"), (9, "keep", "x", "y"), (2, "keep", " ", "y"), (2, "keep", "x", "")):
        with pytest.raises(StateError):
            resolve_dissent(tmp_path, "clip", item=item, how=how, by=by, reason=reason)
    with pytest.raises(StateError, match="只能 --keep"):
        resolve_dissent(tmp_path, "clip", item=2, how="apply", by="x", reason="y")      # a hold cannot be taken over


def test_the_command_and_the_run_output(tmp_path, capsys):
    ctx = _with_objections(tmp_path)
    args = ["resolve-dissent", "--task-dir", str(tmp_path), "--cid", "clip", "--item", "1", "--by", "测试", "--reason", "依据"]
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(args)                                           # neither --keep nor --apply
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([*args, "--keep", "--apply"])
    assert cli.main([*args, "--keep"]) == 0 and json.loads(capsys.readouterr().out)["on_default"] == [2]
    assert cli.main([*args, "--keep"]) == 2                                            # a person has already disposed of it
    capsys.readouterr()
    ctx.state = ClipState.load(ctx.state.path)
    cli._finish_run("clip", ctx, RunReport())                                          # a run that did not stop prints them too
    printed = capsys.readouterr().out
    assert "有 1 条异议已按频道主定稿规则保留" in printed and "[1] 改字 第 2 句" in printed and "[2] 保留项 第 3 句" in printed
    assert "测试：依据" in printed and "频道主定稿规则：" in printed


# ----- the whole lane: record, seal, replace the default afterwards ---------------------------------------------------------------------------------
def _objecting_run(tmp_path, monkeypatch, review, *, baseline=None):
    """Steps 1-9 from an owner push; returns (ctx, report of the first run)."""
    ctx, _, report, _ = _owner_push_run(tmp_path, monkeypatch, owner=True, review=review, baseline=baseline)
    return ctx, report


def _task(tmp_path):
    return tmp_path / "run"


def _reload(ctx):
    ctx.state = ClipState.load(ctx.state.path)


def test_the_clip_seals_with_the_objections_kept_by_default_and_they_outlive_the_seal(tmp_path, monkeypatch):
    answer = _review({"subtitles": [{"cue_id": 2, "text": "我想看李豆沙演霸总裁", "reason": "语义不通"}]})
    ctx, report = _objecting_run(tmp_path, monkeypatch, answer)
    assert report.stopped_at is None and ctx.state.step("review")["status"] == "DONE"
    process = (ctx.clip_dir / "PROCESS.md").read_text(encoding="utf-8")
    assert "有 1 条异议已按频道主定稿规则保留" in process
    assert "[1] 改字 第 2 句「我想看李豆沙演霸总」：复审意见 我想看李豆沙演霸总裁；复审理由 语义不通；处置：保留频道主写法（频道主定稿规则：" in process
    assert "FMR seal" in process
    md = (ctx.clip_dir / "pkg/REVIEW.md").read_text(encoding="utf-8")
    assert md.rstrip().splitlines()[-1].startswith("- [1] 改字 第 2 句") and "保留频道主写法（频道主定稿规则：" in md
    kept = ctx.state.data["dissent"]                                  # the review record was rebuilt; the register was not
    assert len(kept) == 1 and kept[0]["resolution"]["default"] is True and kept[0]["reason"] == "语义不通"
    resolve_dissent(_task(tmp_path), "clip", item=1, how="keep", by="测试", reason="频道主定的写法")   # afterwards, by name
    _reload(ctx)
    named = ctx.state.data["dissent"][0]["resolution"]
    assert (named["by"], named["reason"]) == ("测试", "频道主定的写法") and "default" not in named
    assert ctx.state.step("review")["status"] == "DONE"                # nothing is redone by a keep


def test_apply_takes_the_review_s_writing_and_redoes_the_steps_after_the_import(tmp_path, monkeypatch):
    first = _review({"subtitles": [{"cue_id": 1, "text": "我想要举手"},
                                   {"cue_id": 2, "text": "我想看李豆沙演霸总裁", "reason": "语义不通"}]})
    ctx, report = _objecting_run(tmp_path, monkeypatch, [first, _review(verdict="PASS")],
                                 baseline=_baseline_text({2: "我想看李豆沙演霸道总裁"}))
    assert ctx.state.data["imported"]["owner_edited"] == [2]
    assert report.stopped_at is None                                                  # the rebuild goes on under the default
    assert [c["text"] for c in _cues(ctx, "subs/final.srt")][:2] == ["我想要举手", "我想看李豆沙演霸总"]
    out = resolve_dissent(_task(tmp_path), "clip", item=1, how="apply", by="测试", reason="复审依据成立")   # over the default
    assert out["redo"] == "from_step_3" and out["on_default"] == []
    _reload(ctx)
    assert not any(k in ctx.state.data["steps"] for k in ("subtitles", "speaker", "burn", "title_cover", "tags", "package", "review"))
    assert ctx.state.data["dissent"][0]["resolution"]["how"] == "apply"
    assert "我想看李豆沙演霸总裁" in (ctx.clip_dir / "import/speaker.srt").read_text(encoding="utf-8")
    again = run_clip(ctx, [s for s in STEPS if s.number <= 9])
    assert again.stopped_at is None and {3, 4, 5, 6, 7, 8, 9} <= set(again.passed)      # redone from step 3
    assert [c["text"] for c in _cues(ctx, "subs/final.srt")][1] == "我想看李豆沙演霸总裁"
    assert [c["text"] for c in _cues(ctx, "speaker/speaker.srt")][1] == "我想看李豆沙演霸总裁"
    assert "FMR seal" in (ctx.clip_dir / "PROCESS.md").read_text(encoding="utf-8")


def test_apply_of_a_speaker_objection_changes_the_label_in_the_import(tmp_path, monkeypatch):
    answer = _review({"speakers": [{"cue_id": 4, "speaker": GUEST, "evidence": "自称", "quote": "我也要"}]})
    ctx, report = _objecting_run(tmp_path, monkeypatch, [answer, _review(verdict="PASS")])
    assert report.stopped_at is None and ctx.state.data["dissent"][0]["kind"] == "speaker"
    resolve_dissent(_task(tmp_path), "clip", item=1, how="apply", by="测试", reason="自称")
    imported = (ctx.clip_dir / "import/speaker.srt").read_text(encoding="utf-8")
    assert f"[{GUEST}] 我也要用气泡音叫你宝贝吗" in imported


def test_a_stale_cue_or_a_split_cue_cannot_be_applied(tmp_path, monkeypatch):
    ctx, _ = _objecting_run(tmp_path, monkeypatch, _review({"subtitles": [{"cue_id": 2, "text": "我想看李豆沙演霸总裁"}]}))
    ctx.state.data["dissent"][0]["text"] = "已不是这句了"
    ctx.state.save()
    with pytest.raises(StateError, match="已不是异议记录的文字"):
        resolve_dissent(_task(tmp_path), "clip", item=1, how="apply", by="测试", reason="y")
    assert ClipState.load(ctx.state.path).data["dissent"][0]["resolution"]["default"] is True      # refused: still the default
