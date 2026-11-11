"""The channel owner's 删 marks (`subs/cut.srt` of a push): they come in with the import (A), the video is cut where they
were, with no model asked (B), and the cut clip is the same manuscript as the original (C: see test_lane_publication_cid).
Everything synthetic; no ssh, ffmpeg, CPA or network."""

from __future__ import annotations

import json

import pytest
from fakes import FakeCpa, push_dir
from test_trim_run import FINAL, X, Y, _cli, _no_trim, _run_cli, _stub_run, _text, final_of, make_x, run

from src.autoslice.manual_lane import gates, subtitle_track, trim, trim_run
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.state import ClipState
from src.autoslice.manual_lane.step_import import IMPORT_FILES, import_redo

MARKED = (5, 6, 12)           # cue numbers of the 18-cue clip of test_trim_run that the owner marked 删
OWNER_DOUBTS = ("d003", "d008", "d014", "d016")


def make_marked(tmp_path, cpa=None, marked=MARKED, **kw):
    """Clip X as an owner-decided import: its final.srt / speaker.srt hold the 18 cues of test_trim_run without the marked
    ones (their cue_numbers), import/cut.srt holds the marked ones, the state records the import with cut.srt."""
    from test_trim_run import DOUBTS

    ctx = make_x(tmp_path, cpa or FakeCpa([]), doubts=[d for d in DOUBTS if d["id"] in OWNER_DOUBTS], **kw)
    kept = [FINAL[k - 1] for k in range(1, 19) if k not in marked]
    for rel, labels in (("subs/final.srt", False), ("speaker/speaker.srt", True)):
        (ctx.clip_dir / rel).write_text(subtitle_track.format_blocks(kept, labels=labels), encoding="utf-8")
    cut = ctx.clip_dir / "import/cut.srt"
    cut.parent.mkdir(parents=True, exist_ok=True)
    cut.write_text(subtitle_track.format_blocks([FINAL[k - 1] for k in marked], labels=False), encoding="utf-8")
    ctx.state.data["imported"] = {"source": "owner-push", "from": "/push", "authority": "U130", "at": "t",
                                  "files": {"cut.srt": str(cut)}, "decided_by": "owner"}
    ctx.state.save()
    return ctx


# ----- A: the marks come in with the import -----------------------------------------------------------------------------
def _push_with_cut(tmp_path, clip, rows, cut_rows):
    src = push_dir(tmp_path, clip, rows)
    if cut_rows is not None:
        (src / "subs/cut.srt").write_text(subtitle_track.format_blocks(cut_rows, labels=False), encoding="utf-8")
    return src


ROWS = [(0.0, 2.0, "李豆沙", "第一句"), (2.0, 4.0, "李豆沙", "第二句"), (9.0, 11.0, "李豆沙", "第三句")]
CUT_ROWS = [{"start_s": 5.0, "end_s": 8.0, "text": "谢谢小花的灯牌"}]


def _ctx(tmp_path):
    from fakes import make_ctx

    return make_ctx(tmp_path, remote=None, cpa=None)


def test_import_copies_cut_srt_and_records_it_and_without_it_nothing_else_changes(tmp_path):
    assert IMPORT_FILES["cut.srt"] == "subs/cut.srt"
    plain, marked = _ctx(tmp_path / "plain"), _ctx(tmp_path / "marked")
    rec_plain = si.record_import(plain.state, plain.clip_dir, _push_with_cut(tmp_path / "plain", plain.state.data["clip"], ROWS, None),
                                 source="s", authority="U130")
    src = _push_with_cut(tmp_path / "marked", marked.state.data["clip"], ROWS, CUT_ROWS)
    rec = si.record_import(marked.state, marked.clip_dir, src, source="s", authority="U130")
    assert "cut.srt" not in rec_plain["files"] and not (plain.clip_dir / "import/cut.srt").exists()
    assert set(rec["files"]) == set(rec_plain["files"]) | {"cut.srt"}
    assert (marked.clip_dir / "import/cut.srt").read_bytes() == (src / "subs/cut.srt").read_bytes()
    assert rec["files"]["cut.srt"] == str(marked.clip_dir / "import/cut.srt")
    for name in rec_plain["files"]:                                  # the other files are copied as they were
        assert (plain.clip_dir / "import" / name).read_bytes() == (marked.clip_dir / "import" / name).read_bytes()
    assert [c["text"] for c in trim_run.owner_marks(marked)] == ["谢谢小花的灯牌"] and trim_run.owner_marks(plain) == []


def test_importing_the_same_files_with_cut_srt_added_redoes_from_step_3_not_review_only(tmp_path):
    ctx = _ctx(tmp_path)
    clip = ctx.state.data["clip"]
    si.record_import(ctx.state, ctx.clip_dir, _push_with_cut(tmp_path, clip, ROWS, None), source="s", authority="U130")
    ctx.state.record("subtitles", ok=True, reason="", products={}, cpa=[], note="")
    ctx.state.record("burn", ok=True, reason="", products={}, cpa=[], note="")
    src = _push_with_cut(tmp_path, clip, ROWS, CUT_ROWS)
    # only the owner mark is added next to the cut.srt: still a different set of files, so not the review-only shortcut
    assert import_redo(ctx.state, ctx.clip_dir, src, decided_by="owner") == "from_step_3"
    si.record_import(ctx.state, ctx.clip_dir, src, source="s", authority="U130", decided_by="owner")
    assert ctx.state.data["steps"].get("subtitles") is None and ctx.state.data["steps"].get("burn") is None
    # the same set again, only the mark differs: the existing shortcut, unchanged
    assert import_redo(ctx.state, ctx.clip_dir, src, decided_by=None) == "review_only"


def test_step_3_of_an_import_does_not_write_the_marked_sentences_into_final_srt(tmp_path):
    ctx = _ctx(tmp_path)
    src = _push_with_cut(tmp_path, ctx.state.data["clip"], ROWS, CUT_ROWS)
    si.record_import(ctx.state, ctx.clip_dir, src, source="s", authority="U130", decided_by="owner")
    outcome = si._step3_import(ctx, ctx.state.data["imported"])
    assert outcome.ok, outcome
    texts = [c["text"] for c in subtitle_track.parse_blocks((ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8"))]
    assert texts == ["第一句", "第二句", "第三句"] and "谢谢小花的灯牌" not in "".join(texts)


# ----- B: the video is cut where the owner marked, no model asked -------------------------------------------------------
def test_marked_sentences_are_cut_with_no_model_call_and_the_rest_moves_forward(tmp_path):
    ctx = make_marked(tmp_path)
    result = run(ctx)
    assert result["status"] == "TRIMMED", result
    assert ctx.adapters.cpa.calls == [] and result["source"] == trim_run.OWNER_MARKS
    assert result["successor"] == Y and result["passed"] == [[5, 6], [12, 12]] and result["vetoed"] == []
    assert [c["n"] for run_ in result["removed"] for c in run_["cues"]] == [5, 6, 12]         # the record names the marked sentences
    assert result["removed"][0]["cues"][0]["text"] == "谢谢小花的灯牌"
    on_disk = json.loads((ctx.clip_dir / "trim/decision.json").read_text(encoding="utf-8"))
    assert on_disk == result and "answers" not in on_disk and "calls" not in on_disk
    assert not (ctx.clip_dir / "trim/prompt-1.txt").exists()
    state_x = ClipState.load(tmp_path / "state/clip.json").data["clip"]
    assert state_x["status"] == "SUPERSEDED" and state_x["superseded_by"] == Y and "频道主标为删" in state_x["superseded"]["reason"]
    # Y: no marked sentence, every other sentence as it was, earlier by what was cut before it
    kept = [k for k in range(1, 19) if k not in MARKED]
    final = final_of(tmp_path, Y)
    assert [c["text"] for c in final] == [_text(k) for k in kept] and "第九句\n第二行" in [c["text"] for c in final]
    (_, e1), (s2, e2), (s3, _) = result["windows"]
    for c, k in zip(final, kept, strict=True):
        hole = (s2 - e1 if k > 6 else 0.0) + (s3 - e2 if k > 12 else 0.0)
        assert c["start_s"] == pytest.approx(FINAL[k - 1]["start_s"] - hole, abs=0.002)
    assert s2 - e1 > 5 and s3 - e2 > 0                                                  # the holes really moved something
    assert [c["speaker"] for c in final_of(tmp_path, Y, "speaker/speaker.srt")] == [FINAL[k - 1]["speaker"] for k in kept]
    yd = json.loads((tmp_path / "clips/clip_trim/subs/doubts.json").read_text(encoding="utf-8"))
    assert [d["id"] for d in yd] == [f"trim-{d}" for d in OWNER_DOUBTS]
    # the successor: the owner's mark is inherited, cut.srt is not (it is cut already)
    y = ClipState.load(tmp_path / "state/clip_trim.json").data
    assert y["imported"]["decided_by"] == "owner" and "cut.srt" not in y["imported"]["files"]
    assert not (tmp_path / "clips/clip_trim/import/cut.srt").exists()


def test_an_imported_clip_without_marks_is_still_skipped(tmp_path):
    ctx = make_x(tmp_path, FakeCpa([]))
    ctx.state.data["imported"] = {"source": "hand", "files": {"final.srt": "x"}}
    ctx.state.save()
    assert run(ctx)["status"] == "SKIP_IMPORTED"
    cut = ctx.clip_dir / "import/cut.srt"                                # an empty cut.srt is no mark either
    cut.parent.mkdir(parents=True)
    cut.write_text("", encoding="utf-8")
    ctx.state.data["imported"]["files"]["cut.srt"] = str(cut)
    ctx.state.save()
    assert run(ctx)["status"] == "SKIP_IMPORTED" and ctx.adapters.cpa.calls == []


def test_a_cut_srt_left_in_the_directory_by_an_older_import_is_no_mark(tmp_path):
    ctx = make_marked(tmp_path)
    ctx.state.data["imported"]["files"] = {}
    ctx.state.save()
    assert (ctx.clip_dir / "import/cut.srt").is_file() and run(ctx)["status"] == "SKIP_IMPORTED"


def test_marks_that_are_not_thanks_are_cut_too_the_model_only_guard_does_not_apply(tmp_path):
    ctx = make_marked(tmp_path, marked=(7,))                  # "正题第7句": no thanks word
    assert run(ctx)["status"] == "TRIMMED"
    assert [c["text"] for c in final_of(tmp_path, Y)][5:7] == [_text(6), _text(8)]


def test_the_marks_hold_no_model_even_when_a_non_owner_clip_would(tmp_path):
    ctx = make_marked(tmp_path, FakeCpa([json.dumps({"remove": []})] * 2))
    assert run(ctx)["status"] == "TRIMMED" and ctx.adapters.cpa.calls == []


@pytest.mark.parametrize("marked, span", [((1,), "cues 1..1 "), ((18,), "cues 18..18 "), ((1, 5), "cues 1..1 ")])
def test_a_mark_that_cannot_be_cut_mechanically_fails_and_names_the_span_and_cuts_nothing(tmp_path, marked, span):
    ctx = make_marked(tmp_path, marked=marked)
    result = run(ctx)
    assert result["status"] == "FAILED" and result["reason"].startswith("OWNER_CUT_NOT_POSSIBLE")
    assert span in result["reason"] and "EDGE" in result["reason"] and ctx.adapters.cpa.calls == []
    assert [v["veto"] for v in result["vetoed"]] == ["EDGE"]
    assert not (tmp_path / "state/clip_trim.json").exists()
    assert ClipState.load(tmp_path / "state/clip.json").data["clip"].get("status") is None


def test_marks_that_leave_too_little_of_the_clip_fail_with_the_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(gates, "CLIP_MIN_SECONDS", 85.0)
    ctx = make_marked(tmp_path)
    result = run(ctx)
    assert result["status"] == "FAILED" and "TOO_SHORT" in result["reason"] and "cues 5..6" in result["reason"]
    assert not (tmp_path / "state/clip_trim.json").exists()


def test_the_pure_table_puts_the_marks_among_the_cues_by_time():
    cues = [{"id": 1, "start_s": 0.0, "end_s": 1.0, "text": "a"}, {"id": 2, "start_s": 6.0, "end_s": 7.0, "text": "d"}]
    marks = [{"id": 1, "start_s": 2.0, "end_s": 3.0, "text": "b"}, {"id": 2, "start_s": 3.0, "end_s": 4.0, "text": "c"}]
    table, spans = trim.with_marks(cues, marks)
    assert [c["text"] for c in table] == ["a", "b", "c", "d"] and spans == [(2, 3)]
    assert trim.guard(spans, table, thanks_words=None, clip_len_s=8.0, min_keep_s=1.0)[0] == [(2, 3)]


# ----- B: `run` without --trim ------------------------------------------------------------------------------------------
def test_run_cuts_the_owner_marks_without_trim_and_goes_on_with_the_new_clip(tmp_path, monkeypatch, capsys):
    ctx = make_marked(tmp_path)
    _cli(monkeypatch, ctx)
    calls = _stub_run(monkeypatch)
    rc, lines, out = _run_cli(capsys, tmp_path, "--until", "7")
    assert rc == 0 and calls == [(X, 4), (Y, 7)] and ctx.adapters.cpa.calls == []
    assert lines[0] == f"片中剪除：原片 {X} 已被 {Y} 取代，剪去 3 句"
    assert out["cid"] == Y and out["trim"] == {"status": "TRIMMED", "replaces": X, "removed_cues": 3}


def test_run_without_marks_has_the_calls_it_always_had(tmp_path, monkeypatch, capsys):
    for name, imported in (("plain", None), ("imported", {"source": "hand", "files": {"final.srt": "x"}})):
        ctx = make_x(tmp_path / name, FakeCpa([]))
        if imported:
            ctx.state.data["imported"] = imported
            ctx.state.save()
        _cli(monkeypatch, ctx)
        _no_trim(monkeypatch)
        calls = _stub_run(monkeypatch)
        rc, lines, out = _run_cli(capsys, tmp_path / name)
        assert rc == 0 and calls == [(X, None)] and len(lines) == 1 and "trim" not in out


def test_run_stops_when_the_owner_marks_cannot_be_cut_and_does_not_go_on_with_the_marked_video(tmp_path, monkeypatch, capsys):
    ctx = make_marked(tmp_path, marked=(1,))
    _cli(monkeypatch, ctx)
    calls = _stub_run(monkeypatch)
    rc, lines, out = _run_cli(capsys, tmp_path)
    assert rc == 1 and calls == [(X, 4)] and out["stopped_at"] == 4 and out["trim"]["status"] == "FAILED"
    assert lines[0].startswith("频道主标记的剪除未完成：") and "OWNER_CUT_NOT_POSSIBLE" in lines[0]
    assert "片中剪除未完成：FAILED: OWNER_CUT_NOT_POSSIBLE" in (ctx.clip_dir / "PROCESS.md").read_text(encoding="utf-8")
