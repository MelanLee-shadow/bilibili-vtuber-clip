"""A same-session compilation through the unchanged step bodies: step 1 does not review its boundary, step 0 and the
package name every real recording interval, and a clip without compilation.json is exactly what it was.

The manifest is written here as a literal file (compilation.json), so these tests read the contract the steps consume,
not the code that makes it.  No ssh, no ffmpeg, no CPA."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes import FakeCpa, FakeRemote, fake_title_skill, make_ctx, srt
from test_fmr_calls import Recorder, _patch, _prepared
from test_review_once import _package, _review, _srt

from src.autoslice.finished_media_review import checks
from src.autoslice.manual_lane import channel, compilation
from src.autoslice.manual_lane import stage_package as sp
from src.autoslice.manual_lane import step6_cover, step6_title
from src.autoslice.manual_lane.adapters import RemoteResult
from src.autoslice.manual_lane.step_cut import step0_dedupe, step1_boundary

REC_A, REC_B = "rec-a.flv", "rec-b.flv"


def _manifest(duration_ms: int = 70000) -> dict:
    return {
        "schema": "manual-lane.compilation.v1", "cid": "clip", "theme": "t", "hook": "h", "reason": "r",
        "virtual_source": "compilation-clip.flv", "virtual_path": "/r/sources/compilation-clip.flv",
        "duration_ms": duration_ms, "recordings": [
            {"segment": 1, "name": REC_A, "path": "/r/a", "bcut_srt": "/synthetic/a.srt", "bcut_source_zero_ms": -100000},
            {"segment": 2, "name": REC_B, "path": "/r/b", "bcut_srt": "/synthetic/b.srt", "bcut_source_zero_ms": -500000},
        ],
        "pieces": [
            {"index": 0, "segment": 1, "recording": REC_A, "start_cue": 3, "end_cue": 9, "why": "",
             "source_start_ms": 100000, "source_end_ms": 130000, "measured_ms": 30000,
             "compiled_start_ms": 0, "compiled_end_ms": 30000},
            {"index": 1, "segment": 2, "recording": REC_B, "start_cue": 40, "end_cue": 70, "why": "",
             "source_start_ms": 500000, "source_end_ms": 540000, "measured_ms": 40020,
             "compiled_start_ms": 30000, "compiled_end_ms": 70020},
        ],
    }


def _compilation_ctx(tmp_path: Path, *, seconds: int = 70, cpa=None, remote=None):
    ctx = make_ctx(tmp_path, remote=remote or FakeRemote(), cpa=cpa or FakeCpa([]), bcut_text=srt(seconds // 2))
    clip = ctx.state.data["clip"]
    clip.update({"start_ms": 0, "end_ms": seconds * 1000, "start_cue": 1, "end_cue": seconds // 2})
    ctx.clip_dir.mkdir(parents=True, exist_ok=True)
    manifest = _manifest(seconds * 1000)
    (ctx.clip_dir / "compilation.json").write_text(json.dumps(manifest), encoding="utf-8")
    return ctx


# ----- step 1 -----------------------------------------------------------------------------------------------------
def test_step1_adopts_the_joined_clip_without_asking_cpa_or_checking_a_story_length(tmp_path):
    cpa = FakeCpa([])                     # a scripted answer would be popped by any call; there is none
    ctx = _compilation_ctx(tmp_path, seconds=20, cpa=cpa)      # 20 s: below the 60 s a single story must have
    out = step1_boundary(ctx)
    assert out.ok, out.note
    assert cpa.calls == []
    clip = ctx.state.data["clip"]
    assert (clip["start_ms"], clip["end_ms"], clip["start_cue"], clip["end_cue"]) == (0, 20000, 1, 10)
    decision = json.loads((ctx.clip_dir / "boundary/decision.json").read_text(encoding="utf-8"))
    assert decision["adopted"] == {"start_cue": 1, "end_cue": 10, "start_ms": 0, "end_ms": 20000}
    assert decision["cpa"] is None and "不跑" in decision["skipped"]
    assert any("不跑" in line for line in out.process)


# ----- step 0 -----------------------------------------------------------------------------------------------------
def _dedupe(ctx):
    ctx.adapters.remote.files["/r/clip/dedup/result.json"] = json.dumps({"decision": "UNKNOWN"}).encode()
    assert step0_dedupe(ctx).ok
    return json.loads((ctx.clip_dir / "dedup/package.json").read_text(encoding="utf-8"))


def test_dedupe_names_every_real_interval_not_the_joined_file(tmp_path):
    ctx = _compilation_ctx(tmp_path, remote=FakeRemote(on_script=lambda n, b, r: RemoteResult(0, "", "")))
    package = _dedupe(ctx)
    assert [(i["recording"], i["start_seconds"], i["end_seconds"]) for i in package["source_intervals"]] == [
        (REC_A, 100.0, 130.0), (REC_B, 500.0, 540.0)]
    assert "compilation-clip.flv" not in json.dumps(package) and package["duration_seconds"] == 70.0


def test_dedupe_maps_a_snapped_cut_back_to_the_recordings_it_holds(tmp_path):
    ctx = _compilation_ctx(tmp_path, remote=FakeRemote(on_script=lambda n, b, r: RemoteResult(0, "", "")))
    ctx.state.data["clip"].update({"start_ms": 500, "end_ms": 69500})     # step 2 moved both ends into silence
    package = _dedupe(ctx)
    assert [(i["recording"], i["start_seconds"], i["end_seconds"]) for i in package["source_intervals"]] == [
        (REC_A, 100.5, 130.0), (REC_B, 500.0, 539.5)]


def test_dedupe_of_an_ordinary_clip_is_unchanged(tmp_path):
    ctx = make_ctx(tmp_path, remote=FakeRemote(on_script=lambda n, b, r: RemoteResult(0, "", "")), cpa=FakeCpa([]))
    assert not (ctx.clip_dir / "compilation.json").exists()
    package = _dedupe(ctx)
    assert [(i["recording"], i["start_seconds"], i["end_seconds"]) for i in package["source_intervals"]] == [
        ("src.flv", 10.0, 80.0)]
    assert set(package["source_intervals"][0]) == {"recording", "room_id", "start_seconds", "end_seconds"}


# ----- the package ------------------------------------------------------------------------------------------------
def _member(ctx, duration_ms: int) -> dict:
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/doubts.json").write_text("[]", encoding="utf-8")
    (ctx.clip_dir / "subs/final.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n句\n", encoding="utf-8")
    return sp._member(ctx, files={}, cover_lines=[], dedup={}, cut_local=Path("/synthetic/cut.mp4"),
                      duration_ms=duration_ms, selection_hook="h")


def test_member_states_each_real_source_interval(tmp_path):
    ctx = _compilation_ctx(tmp_path)
    pieces = _member(ctx, 70020)["source"]["pieces"]
    assert pieces == [
        {"path": "/synthetic/cut.mp4", "start_ms": 0, "end_ms": 30000, "recording": REC_A,
         "recording_start_ms": 100000, "recording_end_ms": 130000,
         "recording_context_srt": {"path": "/synthetic/a.srt", "source_zero_srt_ms": 100000}},
        {"path": "/synthetic/cut.mp4", "start_ms": 30000, "end_ms": 70020, "recording": REC_B,
         "recording_start_ms": 500000, "recording_end_ms": 540000,
         "recording_context_srt": {"path": "/synthetic/b.srt", "source_zero_srt_ms": 500000}},
    ]


def test_member_of_a_snapped_compilation_cut_starts_at_zero_and_covers_what_remains(tmp_path):
    ctx = _compilation_ctx(tmp_path)
    ctx.state.data["clip"].update({"start_ms": 5000, "end_ms": 60000})
    pieces = _member(ctx, 55000)["source"]["pieces"]
    assert [(p["start_ms"], p["end_ms"], p["recording"], p["recording_start_ms"], p["recording_end_ms"])
            for p in pieces] == [(0, 25000, REC_A, 105000, 130000), (25000, 55000, REC_B, 500000, 530000)]


def test_member_of_an_ordinary_clip_keeps_its_single_cut_piece(tmp_path):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=FakeCpa([]))
    assert _member(ctx, 70000)["source"]["pieces"] == [{"path": "/synthetic/cut.mp4", "start_ms": 0, "end_ms": 70000}]


def test_step8_writes_the_compilation_member_file(tmp_path, monkeypatch):
    ctx, _ = _prepared(tmp_path)
    (ctx.clip_dir / "compilation.json").write_text(json.dumps(_manifest(60000)), encoding="utf-8")
    ctx.state.data["clip"].update({"start_ms": 0, "end_ms": 60000})
    (ctx.clip_dir / "cut/probe.json").write_text(json.dumps({"format": {"duration": "60.0"}}), encoding="utf-8")
    _patch(monkeypatch, Recorder())
    assert sp.step8_package(ctx).ok
    member = json.loads((ctx.clip_dir / "pkg/clip.member.json").read_text(encoding="utf-8"))
    assert [p["recording"] for p in member["source"]["pieces"]] == [REC_A, REC_B]
    assert member["source"]["pieces"][-1]["end_ms"] == 60000


@pytest.mark.parametrize("is_compilation", [False, True])
def test_step6_passes_compilation_facts_to_both_prompts_and_preserves_ordinary_bytes(
    tmp_path, monkeypatch, is_compilation,
):
    cpa = FakeCpa([None])  # stop after capturing the ordinary title call; cover brief has its fake response
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(srt(3), encoding="utf-8")
    manifest = _manifest()
    manifest.update(theme="主题甲", hook="全片钩子乙")
    manifest["pieces"][0]["why"] = "选段理由丙"
    manifest["pieces"][1]["why"] = "选段理由丁"
    if is_compilation:
        (ctx.clip_dir / "compilation.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(step6_title.title_authority, "title_method", lambda **_: SimpleNamespace(text="METHOD"))
    names = ["李豆沙"]
    step6_title._title_rounds(ctx, names, [])
    step6_cover._cover_brief(ctx, [], "测试标题", ["测试封面"], names)
    prompts = dict(zip(cpa.calls, cpa.prompts))
    title_prompt, cover_prompt = prompts["title"], prompts["cover-brief"]
    if is_compilation:
        for prompt in (title_prompt, cover_prompt):
            for fact in ("主题甲", "全片钩子乙", "选段理由丙", "选段理由丁", "不能只讲其中一段"):
                assert fact in prompt
    else:
        prompt_dir = Path(step6_title.__file__).resolve().parents[3] / "assets/manual_lane/prompts"
        old_title = (prompt_dir / "title.md").read_text().replace("{compilation_context}", "")
        old_cover = (prompt_dir / "cover_brief.md").read_text().replace("{compilation_context}", "")
        assert hashlib.sha256(old_title.encode()).hexdigest() == "55281ca296d78a7982f35f75a3da236b4f947480bc1388aeaac4ff6561fc2500"
        assert hashlib.sha256(old_cover.encode()).hexdigest() == "d8c28bd7f7d6aa6b2de38b08790b7f7d814d86cbd6e74c48e8db38663539bfd4"
        old_title, old_cover = channel.fill(old_title), channel.fill(old_cover)     # the channel's names come from the profile
        track, lines, _ = step6_title.title_inputs(ctx, names)
        expected_title = old_title
        for key, value in (("context", lines), ("participants", "、".join(names)), ("feedback", ""),
                           ("speaker_note", step6_title._speaker_note(track, names)), ("method", "METHOD"),
                           ("punch_max", "7"), ("first_max", "16")):      # the cover text limits (lane_rules cover)
            expected_title = expected_title.replace("{" + key + "}", value)
        expected_cover = old_cover
        for key, value in (("title", "测试标题"), ("lines", "测试封面"), ("participants", "、".join(names)),
                           ("context", ""), ("rows_max", "4"), ("punch_row_max", "4"), ("context_row_max", "8")):   # lane_rules cover
            expected_cover = expected_cover.replace("{" + key + "}", value)
        # TASK-96 adds batch naming context after METHOD; the ordinary facts/interface remain byte-identical.
        start = title_prompt.index("\n\n## 最近标题的称呼")
        end = title_prompt.index("\n\n## 本流水线的输入", start)
        assert (title_prompt[:start] + title_prompt[end:]).encode() == expected_title.encode()
        assert cover_prompt.encode() == expected_cover.encode()


def test_both_review_prompts_share_compilation_context_and_ordinary_bytes(tmp_path, monkeypatch):
    fake_title_skill(tmp_path, monkeypatch)
    ctx, _, _ = _package(tmp_path, monkeypatch, _review(verdict="PASS"), final=_srt([(0, 2, "测试")]), doubts=[])
    base_main, base_held = sp.review_prompt(ctx), sp.review_held_prompt(ctx, ["d1"])
    prompt_dir = Path(sp.__file__).resolve().parents[3] / "assets/manual_lane/prompts"
    for name, expected in (("review.md", "86542cf6d9cddd414c24642c910ed5f067a5b4ad2e36e7f0e403ad90622f4098"),
                           ("review_held.md", "3297be8de6e8651240b4b7e08cc7df859bfc969da5b83731cb32950dd82e0213")):
        original = (prompt_dir / name).read_text().replace("{compilation_context}", "")
        assert hashlib.sha256(original.encode()).hexdigest() == expected
    manifest = _manifest()
    manifest.update(theme="主题甲{materials}", hook="合集钩子乙{title_rules}")
    manifest["pieces"][0]["why"] = "第一段理由丙{session}"
    manifest["pieces"][1]["why"] = "第二段理由丁"
    (ctx.clip_dir / "compilation.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    context = step6_cover._compilation_context(ctx)
    for original, result in ((base_main, sp.review_prompt(ctx)), (base_held, sp.review_held_prompt(ctx, ["d1"]))):
        assert result.replace(context, "", 1).encode() == original.encode()
        assert all(fact in result for fact in ("主题甲{materials}", "合集钩子乙{title_rules}",
                                               "第一段理由丙{session}", "第二段理由丁"))
        assert result.index(context) < result.index("材料：")


def test_compilation_a7_reads_each_recording_at_its_own_clock(tmp_path, monkeypatch):
    ctx = _compilation_ctx(tmp_path)
    manifest_path = ctx.clip_dir / "compilation.json"
    manifest = json.loads(manifest_path.read_text())
    a, b = tmp_path / "a.srt", tmp_path / "b.srt"
    a.write_text("1\n00:03:18,000 --> 00:03:19,000\n甲之前\n\n2\n00:03:20,000 --> 00:03:21,000\n甲片内\n", encoding="utf-8")
    b.write_text("1\n00:17:00,000 --> 00:17:01,000\n乙片内\n\n2\n00:17:21,000 --> 00:17:22,000\n乙之后\n", encoding="utf-8")
    manifest["recordings"][0]["bcut_srt"] = str(a)
    manifest["recordings"][1]["bcut_srt"] = str(b)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    compilation.snapshot_recording_bcuts(ctx.clip_dir, [compilation.RecordingInput(REC_A, a, -100000),
                                                          compilation.RecordingInput(REC_B, b, -500000)])
    a.unlink()
    b.unlink()  # simulate moving the task to a host without the controller's paths
    member = _member(ctx, 70020)
    context = checks.Context(tmp_path / "package", "clip", member)
    before, after = checks._context_cues(context)
    assert before[-1]["text"] == "甲之前" and before[-1]["source_start_ms"] == 98000
    assert after[0]["text"] == "乙之后" and after[0]["source_start_ms"] == 541000
    monkeypatch.setattr(checks, "_silence_around", lambda *_: {"inside_silence": False})
    result = checks.a7_source_interval(context)
    assert result["status"] == "FLAG"
    assert "FMR_A7_CONTEXT_MISSING" not in result["reason_codes"]
    assert "CUT_START_NOT_IN_SILENCE" in result["reason_codes"]


def test_ordinary_a7_uses_original_cut_clock(tmp_path):
    srt_file = tmp_path / "source.srt"
    srt_file.write_text("1\n00:00:09,000 --> 00:00:09,500\n之前\n\n2\n00:00:12,000 --> 00:00:13,000\n之后\n", encoding="utf-8")
    member = {"source": {"pieces": [{"path": "/cut", "start_ms": 1000, "end_ms": 2000}],
                         "context_srt": {"path": str(srt_file), "source_zero_srt_ms": 9000}}}
    before, after = checks._context_cues(checks.Context(tmp_path / "package", "clip", member))
    assert [c["text"] for c in before] == ["之前"]
    assert [c["text"] for c in after] == ["之后"]


# ----- a trim clip (compile_pieces, kind "trim") ------------------------------------------------------------------------
def _trim_manifest(duration_ms: int = 70020) -> dict:
    manifest = _manifest(duration_ms)
    manifest.update(kind="trim", origin={"cid": "orig-clip", "removed": [[4, 5]]}, theme="", hook="原片理由")
    for piece in manifest["pieces"]:
        piece["why"] = ""
    return manifest


def _trim_ctx(tmp_path: Path, **kwargs):
    ctx = _compilation_ctx(tmp_path, **kwargs)
    (ctx.clip_dir / "compilation.json").write_text(json.dumps(_trim_manifest(), ensure_ascii=False), encoding="utf-8")
    return ctx


def test_step1_of_a_trim_clip_says_why_it_does_not_review_the_boundary(tmp_path):
    cpa = FakeCpa([])
    ctx = _trim_ctx(tmp_path, seconds=20, cpa=cpa)
    out = step1_boundary(ctx)
    assert out.ok and cpa.calls == []
    decision = json.loads((ctx.clip_dir / "boundary/decision.json").read_text(encoding="utf-8"))
    assert decision["skipped"] == ("片中剪除：本片由 orig-clip 挖掉与主线无关的致谢后拼成，"
                                   "起止沿用原片已定的边界，所以不再跑边界审查")
    assert decision["adopted"] == {"start_cue": 1, "end_cue": 10, "start_ms": 0, "end_ms": 20000}
    assert out.process[0] == decision["skipped"]


def test_dedupe_member_and_wide_windows_of_a_trim_clip_follow_its_pieces(tmp_path):
    from src.autoslice.manual_lane import wide_held

    ctx = _trim_ctx(tmp_path, remote=FakeRemote(on_script=lambda n, b, r: RemoteResult(0, "", "")))
    assert [(i["recording"], i["start_seconds"], i["end_seconds"]) for i in _dedupe(ctx)["source_intervals"]] == [
        (REC_A, 100.0, 130.0), (REC_B, 500.0, 540.0)]
    assert [(p["recording"], p["recording_start_ms"], p["recording_end_ms"]) for p in _member(ctx, 70020)["source"]["pieces"]] == [
        (REC_A, 100000, 130000), (REC_B, 500000, 540000)]
    rows = [(26.0, 29.0, "接缝前"), (31.0, 34.0, "接缝后")]
    (ctx.clip_dir / "subs/final.srt").write_text(_srt(rows), encoding="utf-8")
    wide = wide_held.prepare(ctx, [{"id": "h1", "start_s": 26.0, "end_s": 29.0}, {"id": "h2", "start_s": 31.0, "end_s": 34.0}],
                             {"h1": [1], "h2": [2]})
    audio = {t["id"]: next(w for w in wide["windows"] if w["win"] == t["win"])["audio"] for t in wide["targets"]}
    assert audio["h1"][1] <= 30.0 and audio["h2"][0] >= 30.0       # no window crosses the seam at 30 s


def test_title_cover_and_both_reviews_of_a_trim_clip_equal_those_of_an_ordinary_clip(tmp_path, monkeypatch):
    fake_title_skill(tmp_path, monkeypatch)
    monkeypatch.setattr(step6_title.title_authority, "title_method", lambda **_: SimpleNamespace(text="METHOD"))
    prompts = {}
    for label in ("ordinary", "trim"):
        cpa = FakeCpa([None])
        ctx = make_ctx(tmp_path / label, remote=FakeRemote(), cpa=cpa)
        (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
        (ctx.clip_dir / "subs/final.srt").write_text(srt(3), encoding="utf-8")
        if label == "trim":
            (ctx.clip_dir / "compilation.json").write_text(json.dumps(_trim_manifest(), ensure_ascii=False), encoding="utf-8")
        assert step6_cover._compilation_context(ctx) == ""
        step6_title._title_rounds(ctx, ["李豆沙"], [])
        step6_cover._cover_brief(ctx, [], "测试标题", ["测试封面"], ["李豆沙"])
        by_call = dict(zip(cpa.calls, cpa.prompts))
        rctx, _, _ = _package(tmp_path / f"{label}-pkg", monkeypatch, _review(verdict="PASS"), final=_srt([(0, 2, "测试")]),
                              doubts=[])
        if label == "trim":
            (rctx.clip_dir / "compilation.json").write_text(json.dumps(_trim_manifest(), ensure_ascii=False), encoding="utf-8")
        prompts[label] = (by_call["title"], by_call["cover-brief"], sp.review_prompt(rctx), sp.review_held_prompt(rctx, ["d1"]))
    assert all(a.encode() == b.encode() for a, b in zip(prompts["ordinary"], prompts["trim"], strict=True))
