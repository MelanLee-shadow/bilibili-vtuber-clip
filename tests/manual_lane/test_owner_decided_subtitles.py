"""Subtitles the channel owner decided himself (`import-subtitles --decided-by owner`): after step 3 no machine step
changes the words or speakers he edited (here: a push without a baseline, so all of them), and the review's objections to
them are recorded in full (REVIEW.md, PROCESS.md, state.dissent) with the default disposition "keep his writing" and
do not stop the package or the seal (`resolve-dissent` may replace the default later).  An import without the mark keeps today's behaviour (the existing tests).  The per-sentence rule and
the disposition itself are in test_owner_edit_authority."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from fakes import UNDERSTANDING_CALLS, FakeCpa, make_ctx
from test_pipeline_fake_e2e import ScriptedCpa, _fake_fmr, _remote
from test_review_doubt_authority import _answer, _focused, _row, _runner
from test_trim_run import Y, answer, make_x, run
from test_review_once import BOUNDARY, GUEST, HOST, LINKED, SUBS_REWIND, TITLE, TITLE_REWIND, _cues, _nonadj, _package, _push, _review, _srt
from test_wide_listen_level import _providers

from src.autoslice.manual_lane import dissent_register, doubt_policy, review_owner, trim_run
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import stage_package as sp
from src.autoslice.manual_lane import step6_cover
from src.autoslice.manual_lane.runner import Step, run_clip
from src.autoslice.manual_lane.stages import STEPS
from src.autoslice.manual_lane.state import ClipState, StateError
from src.autoslice.manual_lane.step_import import import_redo, owner_decided
from src.autoslice.title_editorial_method import AuthorityError

SECTION = review_owner.SECTION
OWNER = {"source": "owner-push", "from": "/push", "authority": "U130", "files": {}, "at": "t", "decided_by": "owner"}


@pytest.fixture
def no_wide(monkeypatch):
    """The unmarked controls end in a hold; keep them out of the post-review listening (not what these cases test)."""
    monkeypatch.setattr(sp, "_finish_or_wide", lambda ctx, record, process:
                        sp._finish(ctx, cpa=record["cpa"], process=process))


def _mark_owner(ctx):
    ctx.state.data["imported"] = dict(OWNER)
    ctx.state.save()


def _objecting_review(**more):
    """An answer that objects in every way the review can: a text fix, a speaker fix (with content evidence), a hold,
    and a doubt decision that would hold a sentence."""
    fixes = {"subtitles": [{"cue_id": 1, "text": "我是雾深，我想举手", "reason": "这里有停顿"}],
             "speakers": [{"cue_id": 1, "speaker": GUEST, "evidence": "自称", "quote": "我是雾深"}]}
    fixes.update(more)
    decision = {"id": "d001", "confidence": "低", "affects_meaning": True, "reason": "两种读法意思相反"}
    return _review(fixes, holds=[{"cue_id": 2, "reason": "这句听不清"}], doubt_decisions=[decision])


def _objecting_package(tmp_path, monkeypatch, *, owner):
    final = _srt([(0, 2, "我是雾深我想举手"), (2, 4, "我也要用气泡音叫你宝贝吗")])
    speaker = _srt([(0, 2, f"[{HOST}] 我是雾深我想举手"), (2, 4, f"[{HOST}] 我也要用气泡音叫你宝贝吗")])
    doubts = [{"id": "d001", "cue_id": 1, "start_s": 0.0, "end_s": 2.0, "state": "KEEP", "final_text": "我是雾深我想举手",
               "confidence": "高", "affects_meaning": False, "source": "owner-push", "basis": "频道主定稿"}]
    ctx, cpa, calls = _package(tmp_path, monkeypatch, _objecting_review(), final=final, doubts=doubts,
                               speaker=speaker, session=LINKED)
    if owner:
        _mark_owner(ctx)
    return ctx, cpa, calls


def _bytes(ctx):
    return [(ctx.clip_dir / rel).read_bytes() for rel in ("subs/final.srt", "speaker/speaker.srt")]


def test_owner_decided_reads_the_import_mark(tmp_path):
    ctx = make_ctx(tmp_path, remote=None, cpa=None)
    push = _push(tmp_path, ctx.state.data["clip"])
    assert owner_decided(ctx) is False
    si.record_import(ctx.state, ctx.clip_dir, push, source="s", authority="U130")
    assert owner_decided(ctx) is False and "decided_by" not in ctx.state.data["imported"]
    rec = si.record_import(ctx.state, ctx.clip_dir, push, source="s", authority="U130", decided_by="owner")
    assert rec["decided_by"] == "owner" and owner_decided(ctx) is True
    with pytest.raises(StateError):
        si.record_import(ctx.state, ctx.clip_dir, push, source="s", authority="U130", decided_by="agent")


def test_a_bad_decided_by_is_refused_before_any_file_is_copied(tmp_path):
    ctx = make_ctx(tmp_path, remote=None, cpa=None)
    with pytest.raises(StateError):
        si.record_import(ctx.state, ctx.clip_dir, _push(tmp_path, ctx.state.data["clip"]), source="s",
                         authority="U130", decided_by="agent")
    assert not (ctx.clip_dir / "import").exists() and "imported" not in ctx.state.data


def test_the_import_command_takes_decided_by_owner_and_only_that():
    from src.autoslice.manual_lane import cli

    parser = cli.build_parser()
    base = ["import-subtitles", "--task-dir", "T", "--cid", "c", "--source-dir", "D", "--source", "s",
            "--authority", "U130"]
    assert parser.parse_args(base).decided_by is None
    assert parser.parse_args([*base, "--decided-by", "owner"]).decided_by == "owner"
    with pytest.raises(SystemExit):
        parser.parse_args([*base, "--decided-by", "agent"])


def test_an_owner_decided_review_adopts_no_subtitle_or_speaker_change_records_them_and_seals_anyway(tmp_path, monkeypatch):
    ctx, _, calls = _objecting_package(tmp_path, monkeypatch, owner=True)
    before = _bytes(ctx)
    out = sp.step9_review(ctx)
    assert _bytes(ctx) == before                                                    # words and speakers untouched
    assert out.rewind == () and calls == ["attest", "seal"]                         # no re-burn; attested and sealed anyway
    assert out.ok is True and out.reason == ""                                      # the objections do not stop the step
    assert not doubt_policy.holds(json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8")))
    record = ctx.state.data["review"]
    assert record["applied"]["subtitles"] == 0 and record["applied"]["speakers"] == 0 and record["applied"]["holds"] == 0
    assert [(d["kind"], d["cue"]) for d in record["dissent"]] == [("text", 1), ("speaker", 1), ("hold", 2),
                                                                  ("decision", None)]
    assert record["dissent"][0] == {"kind": "text", "cue": 1, "text": "我是雾深我想举手", "proposed": "我是雾深，我想举手",
                                    "value": "我是雾深，我想举手", "reason": "这里有停顿"}
    assert any("复审的异议（频道主已定下的内容，按频道主定稿规则保留）" in line and "4 条" in line for line in out.process)
    assert [i["n"] for i in ctx.state.data["dissent"]] == [1, 2, 3, 4]
    assert all(i["resolution"]["how"] == "keep" and i["resolution"]["by"] == dissent_register.DEFAULT_BY
               and i["resolution"]["default"] is True for i in ctx.state.data["dissent"])
    assert len(dissent_register.defaulted(ctx.state)) == 4
    assert "有 4 条异议已按频道主定稿规则保留" in out.process                           # step 9's own statement, then each in full
    listed = "\n".join(out.process)
    assert all(f"[{i['n']}]" in listed and i["proposed"] in listed and i["reason"] in listed and i["text"] in listed
               for i in ctx.state.data["dissent"])


def test_the_same_answer_on_an_unmarked_clip_still_edits_and_holds(tmp_path, monkeypatch, no_wide):
    ctx, _, calls = _objecting_package(tmp_path, monkeypatch, owner=False)
    before = _bytes(ctx)
    out = sp.step9_review(ctx)
    assert _bytes(ctx) != before and out.rewind == tuple(sp.REWIND_SUBTITLES)       # text and speaker adopted
    held = doubt_policy.holds(json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8")))
    assert {d["id"] for d in held} == {"d001", "rv002"} and "attest" not in calls   # the hold and the decision hold
    assert "dissent" not in ctx.state.data["review"]
    assert SECTION not in (ctx.clip_dir / "pkg/REVIEW.md").read_text(encoding="utf-8")
    assert not any("复审的异议" in line for line in out.process)


def test_review_md_lists_every_objection_or_says_none(tmp_path, monkeypatch):
    ctx, _, _ = _objecting_package(tmp_path, monkeypatch, owner=True)
    assert sp.step9_review(ctx).ok is True
    text = (ctx.clip_dir / "pkg/REVIEW.md").read_text(encoding="utf-8")
    section = text.split(SECTION, 1)[1].strip().splitlines()
    assert len(section) == 4
    assert "第 1 句「我是雾深我想举手」" in section[0] and "我是雾深，我想举手" in section[0]
    assert "第 1 句" in section[1] and f"{HOST} → {GUEST}" in section[1]
    assert "第 2 句" in section[2] and "这句听不清" in section[2]
    assert "d001" in section[3] and "两种读法意思相反" in section[3]
    assert all("处置：保留频道主写法（频道主定稿规则：" in line for line in section)
    quiet = tmp_path / "quiet"
    ctx2, _, calls = _package(quiet, monkeypatch, _review(verdict="PASS"), final=_srt([(0, 2, "甲")]), doubts=[])
    _mark_owner(ctx2)
    out = sp.step9_review(ctx2)
    assert (ctx2.clip_dir / "pkg/REVIEW.md").read_text(encoding="utf-8").rstrip().endswith(SECTION + "\n无")
    assert ctx2.state.data["review"]["dissent"] == [] and calls == ["attest", "seal"]
    assert any("异议" in line and "0 条" in line for line in out.process)


def test_a_fix_equal_to_the_current_text_or_speaker_is_not_an_objection(tmp_path, monkeypatch):
    ctx, _, _ = _package(tmp_path, monkeypatch, _review(), final=_srt([(0, 2, "你好")]), doubts=[])
    _mark_owner(ctx)
    answer = {"fixes": {"subtitles": [{"cue_id": 1, "text": "你好"}]}}
    adopted, dissent = review_owner.split(ctx, answer, [])
    assert dissent == [] and adopted["fixes"] == {}
    assert review_owner.split(make_ctx(tmp_path / "plain", remote=None, cpa=None), answer, []) == (answer, None)


def test_the_one_judgement_covers_the_focused_answer_through_apply_fixes(tmp_path, monkeypatch):
    ctx, _, _ = _objecting_package(tmp_path, monkeypatch, owner=True)
    before = _bytes(ctx)
    scoped, skipped = sp._focused_review(ctx, json.loads(_objecting_review()), ["d001"])
    applied = sp.apply_fixes(ctx, sp._combine_reviews(json.loads(_review()), scoped))
    assert _bytes(ctx) == before and applied["subtitles"] == 0 and applied["speakers"] == 0
    assert [d["kind"] for d in applied["dissent"]] == ["text", "speaker", "decision"]   # new holds are out of scope


def test_title_and_tag_fixes_of_the_same_answer_are_still_adopted(tmp_path, monkeypatch):
    ctx, _, _ = _objecting_package(tmp_path, monkeypatch, owner=True)
    monkeypatch.setattr(sp, "_title_fix", lambda c, title, skipped, calls: (c.clip_dir / "title/title.txt").write_text(
        title + "\n", encoding="utf-8") is not None)
    monkeypatch.setattr(sp, "_tags_fix", lambda c, tags: True)
    applied = sp.apply_fixes(ctx, json.loads(_objecting_review(title="【李豆沙】新标题", tags=["李豆沙"])))
    assert applied["title"] is True and applied["tags"] is True and applied["subtitles"] == 0
    assert (ctx.clip_dir / "title/title.txt").read_text(encoding="utf-8").strip() == "【李豆沙】新标题"


def test_wide_rounds_pass_through_the_same_judgement(tmp_path, monkeypatch):
    _providers(monkeypatch)
    ctx, steps, cpa, counts, fmr = _runner(tmp_path, monkeypatch, _answer([]))      # the pushed doubt d493 is a hold
    _mark_owner(ctx)
    cpa.responses.append(_focused(fixes={"subtitles": [{"cue_id": 1, "text": "纸伞落在南边"}]}))
    before = _bytes(ctx)
    report = run_clip(ctx, steps)
    assert report.stopped_at is None and report.rewinds == ["package", "review"]    # the rebuild is done and the clip seals
    dissent = ctx.state.data["review"]["dissent"]
    assert [(d["kind"], d["proposed"]) for d in dissent] == [("text", "纸伞落在南边")]
    assert "未采用：改字（频道主已定下） 第 1 句" in (ctx.clip_dir / "pkg/REVIEW.md").read_text(encoding="utf-8")
    assert _bytes(ctx) == before and counts == {"burn": 1, "tags": 1, "package": 2}
    assert not doubt_policy.is_hold(_row(ctx)) and "attest" in fmr                    # the decision lifted the hold


class _CompleteCpa(ScriptedCpa):
    """Every answer carries the provider's `complete` mark, so a stopped step 9 can replay its saved review on the next run."""

    def ask(self, **kwargs):
        answer = super().ask(**kwargs)
        answer["complete"] = answer["rc"] == 0
        return answer


def _owner_push_run(tmp_path, monkeypatch, *, owner, review, until=9, capture=None, baseline=None):
    """The whole lane (steps 1-9) on fakes from an imported push; returns (ctx, cpa, report, bytes at the end of step 5).
    `review`: one answer, or a list of them (one per review call).  `baseline`: text of the push's subs/baseline.srt."""
    monkeypatch.setattr(sp, "_fmr", _fake_fmr)
    monkeypatch.setattr(sp, "description_text", lambda t, p=None: "简介")
    stubs = tmp_path / "cover-scripts"
    stubs.mkdir(parents=True)
    for name in ("render_cover.py", "redraw_background.py"):
        (stubs / name).write_text("# stub: the fake host never runs it\n", encoding="utf-8")
    monkeypatch.setattr(step6_cover, "COVER_RENDERER", stubs / "render_cover.py")
    monkeypatch.setattr(step6_cover, "COVER_REDRAW", stubs / "redraw_background.py")
    meaning = json.dumps([{"id": "imp-B2", "affects_meaning": False, "reason": "只差语气"}], ensure_ascii=False)
    cpa = _CompleteCpa({"boundary": BOUNDARY, "title": [TITLE, TITLE], "import": [meaning, meaning], "review": review if isinstance(review, list) else [review]})
    ctx = make_ctx(tmp_path / "run", remote=_remote(), cpa=cpa)
    push = _push(tmp_path, ctx.state.data["clip"])
    if baseline is not None:
        (push / "subs/baseline.srt").write_text(baseline, encoding="utf-8")
    si.record_import(ctx.state, ctx.clip_dir, push, source="owner-push", authority="U130",
                     decided_by="owner" if owner else None)
    seen = capture if capture is not None else {}

    def at_step6(step):
        def run(c):
            seen.setdefault("step5", _bytes(c))
            return step.run(c)
        return Step(step.number, step.key, step.title, run)

    def at_step9(step):
        def run(c):
            seen.setdefault("title", [(c.clip_dir / "title/title.txt").read_bytes()])
            return step.run(c)
        return Step(step.number, step.key, step.title, run)

    steps = [at_step6(s) if s.number == 6 else at_step9(s) if s.number == 9 else s for s in STEPS if s.number <= until]
    report = run_clip(ctx, steps)
    return ctx, cpa, report, seen["step5"]


def test_steps_6_to_9_leave_an_owner_decided_final_srt_byte_for_byte_and_the_clip_seals(tmp_path, monkeypatch):
    answer = _review({"subtitles": [{"cue_id": 1, "text": "我想，举手"}, {"cue_id": 4, "text": "改掉最后一句"}],
                      "speakers": [{"cue_id": 2, "speaker": GUEST, "evidence": "自称", "quote": "我想看李豆沙"}],
                      "title": "【李豆沙】她半分钟后真的破防了"},
                     holds=[{"cue_id": 3, "reason": "听不清"}])
    ctx, cpa, report, at_step5 = _owner_push_run(tmp_path, monkeypatch, owner=True, review=answer)
    assert report.stopped_at is None, (report.stopped_at, report.reason)             # the objections do not stop the rebuild
    assert len(dissent_register.defaulted(ctx.state)) == 4 and "FMR seal" in (ctx.clip_dir / "PROCESS.md").read_text()
    assert _bytes(ctx) == at_step5
    assert hashlib.sha256((ctx.clip_dir / "subs/final.srt").read_bytes()).hexdigest() == hashlib.sha256(
        at_step5[0]).hexdigest()
    assert report.rewinds == ["tags", "package", "review"]                           # the title fix only; no re-burn
    assert (ctx.clip_dir / "title/title.txt").read_text(encoding="utf-8").strip() == "【李豆沙】她半分钟后真的破防了"
    assert [c["text"] for c in _cues(ctx, "subs/final.srt")] == ["我想举手", "我想看李豆沙演霸总", "嗯",
                                                                 "我也要用气泡音叫你宝贝吗"]
    process = (ctx.clip_dir / "PROCESS.md").read_text(encoding="utf-8")
    assert "FMR seal" in process and "未封存" not in ctx.state.step("review")["note"]
    assert len(ctx.state.data["review"]["dissent"]) == 4
    assert _nonadj(cpa).count("review") == 1 and UNDERSTANDING_CALLS


def test_the_same_run_without_the_mark_rewrites_the_subtitles_and_holds_the_seal(tmp_path, monkeypatch, no_wide):
    answer = _review({"subtitles": [{"cue_id": 1, "text": "我想，举手"}]}, holds=[{"cue_id": 3, "reason": "听不清"}])
    ctx, _, report, at_step5 = _owner_push_run(tmp_path, monkeypatch, owner=False, review=answer)
    assert report.stopped_at is None, (report.stopped_at, report.reason)
    assert _bytes(ctx) != at_step5 and "burn" in report.rewinds
    assert "未封存" in ctx.state.step("review")["note"]


def test_a_trim_successor_inherits_the_owner_mark_and_only_then(tmp_path, monkeypatch):
    for owner in (False, True):
        task = tmp_path / str(owner)
        ctx = make_x(task, FakeCpa([answer((5, 6), (12, 12)), answer((5, 6), (12, 12))]))
        if owner:
            # trim_clip returns SKIP_IMPORTED for any original that has an import record (`if state.imported`), so the
            # inheritance in _build is not reachable through it today.  A record that reads as marked but falsy, handed
            # in as trim_clip's context, gets the real _build run, which is the code under test.
            class _Marked(dict):
                def __bool__(self):
                    return False
            ctx.state.data["imported"] = _Marked(OWNER)
            monkeypatch.setattr(trim_run, "_ctx", lambda *_args: ctx)
        assert run(ctx)["status"] == "TRIMMED"
        y = ClipState.load(task / "state" / f"{Y}.json")
        assert y.data["imported"]["verbatim"] is True
        if owner:
            assert y.data["imported"]["decided_by"] == "owner"
        else:
            assert "decided_by" not in y.data["imported"]


def _imported_lane(tmp_path, monkeypatch):
    """A plain (unmarked) import run through step 8, with the push directory still on disk."""
    ctx, cpa, report, _ = _owner_push_run(tmp_path, monkeypatch, owner=False, review=_review(verdict="PASS"), until=8)
    assert report.stopped_at is None, (report.stopped_at, report.reason)
    return ctx, tmp_path / "push"


def test_adding_only_the_mark_forgets_the_review_and_keeps_steps_3_to_8(tmp_path, monkeypatch):
    ctx, push = _imported_lane(tmp_path, monkeypatch)
    state = ctx.state.data
    state["review"], state["rewind"], state["revisions"] = {"x": 1}, {"by": "review"}, 2
    state["steps"]["review"] = {"status": "DONE"}
    state["steps"]["upload"] = {"status": "DONE"}
    ctx.state.save()
    final = ctx.clip_dir / "subs/final.srt"
    stamp = (final.read_bytes(), final.stat().st_mtime_ns)
    kept = {k: json.dumps(state["steps"][k], sort_keys=True) for k in ("subtitles", "speaker", "burn", "title_cover", "tags", "package")}
    burned = dict(ctx.adapters.remote.files)
    assert import_redo(ctx.state, ctx.clip_dir, push, decided_by="owner") == "review_only"
    rec = si.record_import(ctx.state, ctx.clip_dir, push, source="owner-push", authority="U131", decided_by="owner")
    assert owner_decided(ctx) and rec["source"] == "owner-push" and rec["authority"] == "U131"
    assert (final.read_bytes(), final.stat().st_mtime_ns) == stamp
    assert {k: json.dumps(ctx.state.data["steps"][k], sort_keys=True) for k in kept} == kept
    assert all(ctx.state.step(k)["status"] == "DONE" for k in kept) and ctx.adapters.remote.files == burned
    assert "review" not in ctx.state.data["steps"] and "upload" not in ctx.state.data["steps"]
    assert not any(k in ctx.state.data for k in ("review", "rewind", "revisions"))
    # and the lane goes on from the review: no step 3-8 runs again, the owner's subtitles are still byte for byte
    ctx.adapters.cpa.answers["review"] = [_review({"subtitles": [{"cue_id": 1, "text": "改"}]})]
    report = run_clip(ctx, STEPS)       # the review's text fix on a sentence of the owner: an objection, kept; step 9 only
    assert (report.stopped_at, report.passed) == (None, [9]) and len(dissent_register.defaulted(ctx.state)) == 1
    assert final.read_bytes() == stamp[0]


def test_a_changed_source_file_redoes_from_step_3(tmp_path, monkeypatch):
    ctx, push = _imported_lane(tmp_path, monkeypatch)
    text = (push / "subs/final.srt").read_text(encoding="utf-8")
    (push / "subs/final.srt").write_text(text.replace("举手", "举首"), encoding="utf-8")
    assert import_redo(ctx.state, ctx.clip_dir, push, decided_by="owner") == "from_step_3"
    si.record_import(ctx.state, ctx.clip_dir, push, source="owner-push", authority="U130", decided_by="owner")
    assert not any(k in ctx.state.data["steps"] for k in ("subtitles", "speaker", "burn", "tags", "package", "review"))
    assert "举首" in (ctx.clip_dir / "import/final.srt").read_text(encoding="utf-8")


def test_importing_again_with_the_same_mark_redoes_from_step_3(tmp_path, monkeypatch):
    ctx, push = _imported_lane(tmp_path, monkeypatch)
    assert import_redo(ctx.state, ctx.clip_dir, push) == "from_step_3"
    si.record_import(ctx.state, ctx.clip_dir, push, source="again", authority="U130")
    assert not any(k in ctx.state.data["steps"] for k in ("subtitles", "speaker", "burn", "tags", "package"))
    ctx2, push2 = _imported_lane(tmp_path / "o", monkeypatch)
    si.record_import(ctx2.state, ctx2.clip_dir, push2, source="s", authority="U130", decided_by="owner")
    assert import_redo(ctx2.state, ctx2.clip_dir, push2, decided_by="owner") == "from_step_3"


# ----- the title the owner picked: the review's title fix is recorded, not adopted --------------------------------------
NEW_TITLE = "【李豆沙】她半分钟后真的破防了"


def _pick(monkeypatch, verdicts=None):
    """title_verdicts as the review sees it (step 6 keeps the real asset): the owner picked clip `clip`'s title body."""
    class Picked:
        def approved(self, cid):
            return {"title_body": "半分钟后她破防了", "evidence": "频道主选定"} if cid == "clip" else None

    def load():
        if isinstance(verdicts, Exception):
            raise verdicts
        return verdicts or Picked()
    monkeypatch.setattr(review_owner, "title_verdicts", SimpleNamespace(load_verdicts=load))


def _everything(**more):
    return _review({"subtitles": [{"cue_id": 1, "text": "我想，举手"}], "title": NEW_TITLE, "tags": ["李豆沙"], **more})


def test_a_picked_title_is_kept_while_the_other_fixes_of_the_answer_are_adopted(tmp_path, monkeypatch):
    _pick(monkeypatch)
    seen = {}
    ctx, cpa, report, at_step5 = _owner_push_run(tmp_path, monkeypatch, owner=False, review=_everything(), capture=seen)
    assert report.stopped_at is None, (report.stopped_at, report.reason)             # the title objection is kept, not waited on
    assert [d["resolution"]["by"] for d in ctx.state.data["dissent"]] == [dissent_register.DEFAULT_BY]
    assert (ctx.clip_dir / "title/title.txt").read_bytes() == seen["title"][0]       # not the review's title
    assert not any(c.startswith("review-title-judge") for c in _nonadj(cpa))         # never even judged
    assert report.rewinds == SUBS_REWIND                                             # the subtitle fix's, none for the title
    assert _cues(ctx, "subs/final.srt")[0]["text"] == "我想，举手"                    # subtitles are not the owner's here
    assert ctx.state.data["reviewed_tags"]["kept"] == ["李豆沙"]
    assert "FMR seal" in (ctx.clip_dir / "PROCESS.md").read_text(encoding="utf-8")
    assert [(d["kind"], d["proposed"]) for d in ctx.state.data["review"]["dissent"]] == [("title", NEW_TITLE)]
    assert ctx.state.data["review"]["dissent"][0]["text"] == seen["title"][0].decode().strip()
    md = (ctx.clip_dir / "pkg/REVIEW.md").read_text(encoding="utf-8")
    assert SECTION in md and "改标题" in md.split(SECTION, 1)[1] and NEW_TITLE in md.split(SECTION, 1)[1]
    assert "字幕 1 条" in md


def test_without_a_picked_title_the_review_title_is_adopted_as_before(tmp_path, monkeypatch):
    seen = {}
    review = _review({"title": NEW_TITLE})
    ctx, cpa, report, _ = _owner_push_run(tmp_path, monkeypatch, owner=False, review=review, capture=seen)
    assert report.stopped_at is None and report.rewinds == TITLE_REWIND
    assert (ctx.clip_dir / "title/title.txt").read_text(encoding="utf-8").strip() == NEW_TITLE
    assert "dissent" not in ctx.state.data["review"]
    assert SECTION not in (ctx.clip_dir / "pkg/REVIEW.md").read_text(encoding="utf-8")


def test_owner_subtitles_and_a_picked_title_are_both_kept_and_both_recorded(tmp_path, monkeypatch):
    _pick(monkeypatch)
    seen = {}
    ctx, cpa, report, at_step5 = _owner_push_run(tmp_path, monkeypatch, owner=True, review=_everything(), capture=seen)
    assert report.stopped_at is None, (report.stopped_at, report.reason)
    assert len(dissent_register.defaulted(ctx.state)) == 2
    assert report.rewinds == ["package", "review"] and _bytes(ctx) == at_step5      # the tags fix only
    assert (ctx.clip_dir / "title/title.txt").read_bytes() == seen["title"][0]
    assert [d["kind"] for d in ctx.state.data["review"]["dissent"]] == ["title", "text"]
    assert "FMR seal" in (ctx.clip_dir / "PROCESS.md").read_text(encoding="utf-8")
    assert ctx.state.data["reviewed_tags"]["kept"] == ["李豆沙"]


def test_an_unreadable_verdicts_asset_means_no_pick_and_the_reason_is_in_process(tmp_path, monkeypatch):
    _pick(monkeypatch, AuthorityError("title_owner_verdicts.v1.json: gone"))
    ctx, _, report, _ = _owner_push_run(tmp_path, monkeypatch, owner=False, review=_review({"title": NEW_TITLE}))
    assert report.stopped_at is None and report.rewinds == TITLE_REWIND
    assert (ctx.clip_dir / "title/title.txt").read_text(encoding="utf-8").strip() == NEW_TITLE
    process = (ctx.clip_dir / "PROCESS.md").read_text(encoding="utf-8")
    assert "选定标题资产读不到，按没有选定标题处理" in process and "gone" in process


def test_the_same_title_as_now_or_no_title_is_not_an_objection(tmp_path, monkeypatch):
    _pick(monkeypatch)
    ctx, _, _ = _package(tmp_path, monkeypatch, _review(), final=_srt([(0, 2, "你好")]), doubts=[])
    current = (ctx.clip_dir / "title/title.txt").read_text(encoding="utf-8").strip()
    for fixes in ({}, {"title": current}, {"title": None}):
        adopted, dissent = review_owner.split(ctx, {"fixes": fixes}, [])
        assert dissent == [] and "title" not in adopted["fixes"]
