"""The `trim` command end to end on fakes: two CPA answers, the guard, the new clip built from kept intervals, X's
decided subtitles moved onto it, X superseded last.  Every cue, name and thanks line is synthetic.  No ssh, ffmpeg, CPA
or network: the remote is the fake of fakes.py, CPA a scripted fake."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fakes import FakeCpa, FakeRemote, fake_cover, fake_title_skill, make_ctx, srt
from test_pipeline_fake_e2e import ScriptedCpa
from test_review_once import _review

from src.autoslice.manual_lane import cli, doubt_policy, gates, revision_authority, speaker_v8, subtitle_track, title_history, trim, trim_run
from src.autoslice.manual_lane import stage_package as sp
from src.autoslice.manual_lane import step6_cover
from src.autoslice.manual_lane.adapters import RemoteResult
from src.autoslice.manual_lane.runner import Ctx, RunReport, run_clip
from src.autoslice.manual_lane.srt_text import format_srt
from src.autoslice.manual_lane.stages import STEPS
from src.autoslice.manual_lane.state import ClipState
from test_published_revision import _closure
from tests.test_publication_reconciliation import first_publication_registries  # noqa: F401  (the fixture)

HOST, GUEST = speaker_v8.HOST, speaker_v8.GUEST_LABEL
X, Y = "clip", "clip_trim"
Y_RD = "/r/clip_trim"
TITLE = json.dumps({"candidates": [{"title": "【李豆沙】半分钟后她破防了", "on_stage": ["李豆沙"], "punchline_person": "她破防了",
                                    "swap_check": {"holds_with_other_host": False, "reason": "测试"}}],
                    "recommended": 0, "cover_lines": ["破防了", "半分钟"]}, ensure_ascii=False)

# an 18-cue clip of 90 s on the cut clock: cue k lasts 5(k-1)..5(k-1)+4.  Cues 5 and 6 thank a gift, cue 12 thanks everyone.
TEXTS = {5: "谢谢小花的灯牌", 6: "谢谢你", 9: "第九句\n第二行", 12: "感谢大家的陪伴"}


def _text(k: int) -> str:
    return TEXTS.get(k, f"正题第{k}句")


def _cue(k: int) -> dict:
    return {"start_s": 5.0 * (k - 1), "end_s": 5.0 * (k - 1) + 4.0, "text": _text(k), "speaker": HOST if k % 2 else GUEST}


FINAL = [_cue(k) for k in range(1, 19)]


def _doubt(did: str, k: int, **kw) -> dict:
    return {"id": did, "cue_id": k, "current": f"原{k}", "abs_start_ms": 10_000 + round(FINAL[k - 1]["start_s"] * 1000),
            "abs_end_ms": 10_000 + round(FINAL[k - 1]["end_s"] * 1000), "state": "KEEP", "final_text": _text(k),
            "confidence": "高", "affects_meaning": False, "source": "cpa", "basis": f"依据{did}", **kw}


DOUBTS = [_doubt("d003", 3, confidence="低", affects_meaning=True),            # holds, kept
          _doubt("d005", 5, confidence="低", affects_meaning=True),            # holds, on a cut cue
          _doubt("d006", 6, state="UNRESOLVED"),                               # on a cut cue
          _doubt("d008", 8, state="UNRESOLVED"),                               # undecided, meaning effect false: free
          _doubt("d014", 14, confidence="低", affects_meaning=None),           # low, effect unknown: holds
          _doubt("d016", 16)]                                                  # decided


def answer(*spans) -> str:
    return json.dumps({"remove": [{"start_cue": a, "end_cue": b, "thanked": "x", "why": "y"} for a, b in spans]}, ensure_ascii=False)


def _probe(seconds: float) -> str:
    return json.dumps({"format": {"duration": f"{seconds:.6f}"}, "streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "pix_fmt": "yuv420p", "r_frame_rate": "60/1"},
        {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2}]})


def _fake_fmr(*args):
    """The finished-media review lane replaced by a stub: init makes the package directory of the successor."""
    if args[0] == "init":
        root = Path(args[args.index("--out-root") + 1])
        (root / "out" / "2026-10-24" / f"fm-20261024-{sp.member_slug(Y)}").mkdir(parents=True, exist_ok=True)
    return subprocess.CompletedProcess(args, 0, "", "")


def _script(*, jitter_ms: int = 0, cut_rc: int = 0, snap_start: float | None = None):
    def script(name, body, remote):
        if name == "compile-cut":
            spans = [float(m) for m in re.findall(r" -t (\d+\.\d+) ", body)]
            return RemoteResult(0, "".join(f"PROBE {i} {_probe(s + jitter_ms / 1000)}\n" for i, s in enumerate(spans)), "")
        if name == "compile-concat":
            total = sum(float(d) for d in re.findall(r"^duration (\d+\.\d+)$", body, re.M))
            return RemoteResult(0, "PROBE final " + _probe(total) + "\n", "")
        if name.startswith("s4-screen-"):   # step 4 of a single-host successor reads the picture: no video is playing
            mode = name.removeprefix("s4-screen-")
            frames = json.loads(remote.files[f"{Y_RD}/speaker/{mode}-frames.json"])["frames"]
            rows = [{"id": f["id"], "t": f["t"], "ok": True, "video_playing": False, "video_subtitle": "",
                     "subtitle_language": "无", "other_text": ""} for f in frames]
            remote.files[f"{Y_RD}/speaker/{mode}-out.json"] = json.dumps(
                {"question": "q", "model": "gpt-6.1-sol", "frames": rows, "calls": []}, ensure_ascii=False).encode()
            return RemoteResult(0, "", "")
        if name == "s0-dedupe":
            remote.files[f"{Y_RD}/dedup/result.json"] = json.dumps({"decision": "UNKNOWN", "block_upload": False}).encode()
        elif name == "s2-snap" and snap_start is not None:
            remote.files[f"{Y_RD}/cut/snap-start.log"] = f"silence_start: {snap_start}\nsilence_end: {snap_start + 2}\n".encode()
        elif name == "s2-cut":
            if cut_rc:
                return RemoteResult(cut_rc, "", "ffmpeg broke (fake)")
            seconds = float(re.search(r" -t (\d+\.\d+) ", body).group(1))
            remote.files[f"{Y_RD}/cut/probe.json"] = json.dumps({
                "format": {"duration": f"{seconds:.3f}"},
                "streams": [{"codec_type": "video", "width": 1920, "height": 1080}, {"codec_type": "audio"}]}).encode()
            remote.files[f"{Y_RD}/cut/cut.mp4"] = b"cut"
        elif name == "s5-burn":
            remote.files[f"{Y_RD}/burn/intro_result.json"] = json.dumps(
                {"status": "PREPENDED", "intro_id": "Z1", "intro_offset_ms": 0}).encode()
            remote.files[f"{Y_RD}/burn/final.mp4"] = b"mp4"
            remote.files[f"{Y_RD}/burn/final.ass"] = b"ass"
        elif name == "s6-cover":
            fake_cover(remote, Y_RD)
        elif name == "s7-tags":
            remote.files[f"{Y_RD}/tags/tags.json"] = json.dumps(
                {"status": "OK", "final_tags": ["李豆沙", "切片"], "input_binding": {"title": "x"}}, ensure_ascii=False).encode()
        return RemoteResult(0, "", "")
    return script


SINGLE = {"recording_date": "2026-10-24", "speaker_mode": "uniform_host", "enablement_evidence": None,
          "participants": [["李豆沙"]], "own_uploader_mids": [1]}


def make_x(tmp_path: Path, cpa, *, script=None, doubts=None, speakers: bool = True, session=None):
    """Clip X with steps 3 and 4 done: 90 s cut from a 120 s recording, cut-clock cues, speaker labels, doubts and the
    dictation of the cut."""
    remote = FakeRemote(on_script=script or _script())
    ctx = make_ctx(tmp_path, remote=remote, cpa=cpa, bcut_text=srt(60), session=session)
    clip = ctx.state.data["clip"]
    clip.update(end_ms=100_000, hook="选题理由：甲乙丙")
    for rel, body in (
            ("subs/final.srt", subtitle_track.format_blocks(FINAL, labels=False)),
            ("speaker/speaker.srt", subtitle_track.format_blocks(FINAL, labels=speakers)),
            ("subs/doubts.json", json.dumps(DOUBTS if doubts is None else doubts, ensure_ascii=False)),
            ("asr/bcut.srt", format_srt([{"id": k, "start_s": FINAL[k - 1]["start_s"], "end_s": FINAL[k - 1]["end_s"],
                                          "text": f"听写{k}"} for k in range(1, 19)])),
            ("danmaku/lines.json", json.dumps([
                {"t_s": 21.0, "type": "gift", "sender": "路人甲", "text": "", "gift_name": "灯牌", "count": 1},
                {"t_s": 22.0, "text": "一条普通弹幕"}, {"t_s": -50.0, "type": "sc", "sender": "路人乙", "text": "片外的醒目留言"}],
                ensure_ascii=False))):
        path = ctx.clip_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    for key in ("subtitles", "speaker"):
        ctx.state.record(key, ok=True, reason="", products={}, cpa=[], note="")
    ctx.state.save()
    return ctx


def run(ctx, task: Path | None = None):
    return trim_run.trim_clip(task=task or ctx.clip_dir.parent.parent, cid=X, adapters_for=lambda _rd: ctx.adapters)


def digest(root: Path, skip: str = "trim") -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*"))
            if p.is_file() and p.relative_to(root).parts[0] != skip}


def final_of(task: Path, cid: str, rel: str = "subs/final.srt"):
    return subtitle_track.parse_blocks((task / "clips" / cid / rel).read_text(encoding="utf-8"))


# ----- the whole way ---------------------------------------------------------------------------------------------------
def test_two_agreeing_answers_build_the_successor_carry_everything_and_supersede_the_original(tmp_path):
    ctx = make_x(tmp_path, FakeCpa([answer((5, 6), (12, 12)), answer((5, 6), (12, 12))]))
    before = digest(ctx.clip_dir)
    result = run(ctx)
    assert result["status"] == "TRIMMED", result
    assert result["successor"] == Y and result["agreed"] == result["passed"] == [[5, 6], [12, 12]] and result["vetoed"] == []
    assert ctx.adapters.cpa.calls == ["trim-1", "trim-2"]                       # no other model call
    assert "compile-asr" not in [n for n, _ in ctx.adapters.remote.scripts]     # no ASR
    assert not any(n.startswith(("s3-", "s4-")) for n, _ in ctx.adapters.remote.scripts)
    # X: untouched but for trim/, and marked superseded by Y
    assert digest(ctx.clip_dir) == before
    state_x = ClipState.load(tmp_path / "state/clip.json").data["clip"]
    assert state_x["status"] == "SUPERSEDED" and state_x["superseded_by"] == Y
    assert "片中剪除" in state_x["superseded"]["reason"] and state_x["superseded"]["quote"].startswith("谢谢小花的灯牌 谢谢你")
    # Y: the same recording, kept intervals, the same hook, a trim manifest
    y = ClipState.load(tmp_path / "state/clip_trim.json")
    assert y.data["clip"]["hook"] == "选题理由：甲乙丙" and y.data["imported"]["verbatim"] is True
    manifest = json.loads((tmp_path / "clips/clip_trim/compilation.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "trim" and manifest["origin"] == {"cid": X, "removed": [[5, 6], [12, 12]]}
    assert [[p["source_start_ms"], p["source_end_ms"]] for p in manifest["pieces"]] == result["pieces_ms"]
    assert result["pieces_ms"][0][0] == 10_000 and len(result["pieces_ms"]) == 3        # from X's own start on the recording
    assert all(y.step(step)["status"] == "DONE" for step in ("boundary", "dedupe", "cut", "subtitles", "speaker"))
    # the subtitles: X's, without the cut cues, the words exactly as they were, each moved by what was cut before it
    kept = [k for k in range(1, 19) if k not in (5, 6, 12)]
    final, speaker = final_of(tmp_path, Y), final_of(tmp_path, Y, "speaker/speaker.srt")
    assert [c["text"] for c in final] == [_text(k) for k in kept] and "第九句\n第二行" in [c["text"] for c in final]
    assert [c["speaker"] for c in speaker] == [FINAL[k - 1]["speaker"] for k in kept]          # per sentence
    assert [c["text"] for c in speaker] == [_text(k) for k in kept]
    (_, e1), (s2, e2), (s3, _) = result["windows"]
    for c, k in zip(final, kept, strict=True):
        hole = (s2 - e1 if k > 6 else 0.0) + (s3 - e2 if k > 12 else 0.0)
        assert c["start_s"] == pytest.approx(FINAL[k - 1]["start_s"] - hole, abs=0.002)
        assert c["end_s"] - c["start_s"] == pytest.approx(4.0, abs=0.002)
    assert s2 - e1 > 5 and s3 - e2 > 0                                                  # the holes really moved something
    # the doubts: those on cut cues are gone, the others are the original's, and the holding ones are the same ones
    yd = json.loads((tmp_path / "clips/clip_trim/subs/doubts.json").read_text(encoding="utf-8"))
    assert [d["id"] for d in yd] == ["trim-d003", "trim-d008", "trim-d014", "trim-d016"]
    old_hold = {f"trim-{d['id']}" for d in DOUBTS if doubt_policy.is_hold(d) and d["id"] not in ("d005", "d006")}
    assert old_hold == {"trim-d003", "trim-d014"} == {d["id"] for d in doubt_policy.holds(yd)}
    assert (yd[0]["start_s"], yd[0]["end_s"]) == (final[2]["start_s"], final[2]["end_s"])        # d003 sits on cue 3
    assert yd[0]["final_text"] == _text(3) and yd[0]["basis"] == "依据d003" and yd[0]["current"] == "原3"
    # the record
    on_disk = json.loads((ctx.clip_dir / "trim/decision.json").read_text(encoding="utf-8"))
    assert on_disk == result and len(on_disk["answers"]) == 2
    assert [r["call_id"] for r in on_disk["calls"]] == ["trim-1", "trim-2"]
    assert on_disk["removed"][0]["cues"][0] == {"n": 5, "start_s": 20.0, "end_s": 24.0, "text": "谢谢小花的灯牌"}
    # the prompt of both asks carried the clip's own records and the selection reason
    first = ctx.adapters.cpa.prompts[0]
    assert "选题理由：甲乙丙" in first and "21.0s 礼物 路人甲 灯牌×1" in first and "片外" not in first and "一条普通弹幕" not in first
    assert ctx.adapters.cpa.prompts[1] == first.rstrip("\n") + "\n（第 2 遍）\n"


@pytest.mark.parametrize("published", [False, True], ids=["unpublished-original", "published-original"])
def test_the_successor_goes_on_through_the_ordinary_steps_to_the_review(tmp_path, monkeypatch, published,
                                                                       first_publication_registries):  # noqa: F811
    """Step 8 of the successor asks the registry whether the manuscript is published: the registry here is this test's own
    (empty, or holding the original's first publication), never the checkout's."""
    if published:
        fixture = _closure(tmp_path, monkeypatch)
    else:
        monkeypatch.setattr(title_history, "load_publication_registry", lambda: {"entries": []})
    fake_title_skill(tmp_path, monkeypatch)
    monkeypatch.setattr(sp, "_fmr", _fake_fmr)
    monkeypatch.setattr(sp, "description_text", lambda t, p=None: "简介")
    monkeypatch.setattr(sp, "_finish_or_wide", lambda ctx, record, process: sp._finish(ctx, cpa=record["cpa"], process=process))
    for name in ("COVER_RENDERER", "COVER_REDRAW"):            # the title-cover skill's scripts are not in every checkout
        stub = tmp_path / f"{name}.py"
        stub.write_text("# stub\n", encoding="utf-8")
        monkeypatch.setattr(step6_cover, name, stub)
    cpa = ScriptedCpa({"trim": [answer((5, 6)), answer((5, 6))], "title": [TITLE],
                       "review": [_review(verdict="PASS")]})
    ctx = make_x(tmp_path, cpa)
    assert run(ctx)["status"] == "TRIMMED"
    state_y = ClipState.load(tmp_path / "state/clip_trim.json")
    ctx_y = Ctx(state=state_y, session=state_y.data["session"], clip_dir=tmp_path / "clips/clip_trim", remote_dir=Y_RD,
                adapters=ctx.adapters)
    report = run_clip(ctx_y, STEPS)
    assert report.stopped_at is None, (report.stopped_at, report.reason)
    assert report.halted_before_upload is True and 9 in report.passed + report.reused
    later = [c for c in cpa.calls if not c.startswith(("adjudicate", "trim"))]
    # step 6 reads the clip first (two readings asked at once, then the merge), then writes the title
    assert sorted(later[:2]) == ["title-read-1", "title-read-2"] and later[2:] == [
        "title-merge", "title", "title-judge-r0-1", "title-judge-r0-2", "cover-brief", "review"]
    # the doubts that held X back hold the successor back too: not sealed
    assert "未封存" in ctx_y.state.step("review")["note"]
    # a successor of a published manuscript gets its same-BV authority from step 8 and records what it changes; of an
    # unpublished one, neither
    authority, revision = ctx_y.clip_dir / revision_authority.AUTHORITY_FILE, ctx_y.clip_dir / revision_authority.REVISION_FILE
    assert authority.is_file() is revision.is_file() is published
    if published:
        assert json.loads(authority.read_text(encoding="utf-8"))["bvid"] == fixture["bvid"]
    member = json.loads((ctx_y.clip_dir / "pkg/clip_trim.member.json").read_text(encoding="utf-8"))
    assert [p["recording"] for p in member["source"]["pieces"]] == ["src.flv", "src.flv"]
    # the prompts of the title, the cover brief and the review carry no theme-compilation context
    for call_id, prompt in cpa._rows:
        assert "同场主题合集" not in prompt, call_id


# ----- nothing to cut --------------------------------------------------------------------------------------------------
def test_a_span_only_one_answer_proposes_is_not_cut(tmp_path):
    ctx = make_x(tmp_path, FakeCpa([answer((5, 6)), answer()]))
    before = digest(ctx.clip_dir)
    result = run(ctx)
    assert result["status"] == "NONE" and result["passed"] == [] and result["successor"] is None
    assert not (tmp_path / "state/clip_trim.json").exists() and not (tmp_path / "clips/clip_trim").exists()
    assert ClipState.load(tmp_path / "state/clip.json").data["clip"].get("status") is None
    assert digest(ctx.clip_dir) == before and ctx.adapters.remote.scripts == []


@pytest.mark.parametrize("spans, veto", [([(1, 2)], "EDGE"), ([(17, 18)], "EDGE"), ([(7, 7)], "NO_THANKS")])
def test_a_span_the_guard_vetoes_is_not_cut_and_says_why(tmp_path, spans, veto):
    ctx = make_x(tmp_path, FakeCpa([answer(*spans), answer(*spans)]))
    result = run(ctx)
    assert result["status"] == "NONE" and result["passed"] == [] and [v["veto"] for v in result["vetoed"]] == [veto]
    assert not (tmp_path / "state/clip_trim.json").exists()


def test_a_cut_that_leaves_too_little_is_vetoed(tmp_path, monkeypatch):
    monkeypatch.setattr(gates, "CLIP_MIN_SECONDS", 85.0)            # the guard reads the ordinary clip minimum
    ctx = make_x(tmp_path, FakeCpa([answer((5, 6)), answer((5, 6))]))
    result = run(ctx)
    assert result["status"] == "NONE" and [v["veto"] for v in result["vetoed"]] == ["TOO_SHORT"]


# ----- failures ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("first, why", [("这不是 JSON", "UNUSABLE"), (answer((5, 99)), "UNUSABLE"), (None, "CPA_CALL_FAILED")])
def test_an_unusable_answer_or_a_failed_call_fails_without_a_successor_and_is_asked_again_next_time(tmp_path, first, why):
    cpa = FakeCpa([first])
    ctx = make_x(tmp_path, cpa)
    before = digest(ctx.clip_dir)
    result = run(ctx)
    assert result["status"] == "FAILED" and why in result["reason"] and result["successor"] is None
    assert not (tmp_path / "state/clip_trim.json").exists()
    assert ClipState.load(tmp_path / "state/clip.json").data["clip"].get("status") is None
    assert digest(ctx.clip_dir) == before
    # the failed run is not final: the same prompts are asked again and now decide
    cpa.responses.extend([answer((5, 6)), answer((5, 6))])
    assert run(ctx)["status"] == "TRIMMED"
    assert cpa.calls.count("trim-1") == 2


def test_a_second_answer_that_fails_leaves_the_first_on_disk_and_builds_nothing(tmp_path):
    ctx = make_x(tmp_path, FakeCpa([answer((5, 6)), "坏"]))
    result = run(ctx)
    assert result["status"] == "FAILED" and "trim-2" in result["reason"]
    assert (ctx.clip_dir / "trim/answer-1.txt").read_text(encoding="utf-8") == answer((5, 6))
    assert (ctx.clip_dir / "trim/answer-2.txt").read_text(encoding="utf-8") == "坏"
    assert not (tmp_path / "state/clip_trim.json").exists()


def test_a_failing_cut_of_the_successor_fails_and_leaves_the_original_unsuperseded(tmp_path):
    ctx = make_x(tmp_path, FakeCpa([answer((5, 6))] * 4), script=_script(cut_rc=1))
    before = digest(ctx.clip_dir)
    result = run(ctx)
    assert result["status"] == "FAILED" and "stopped at step 2" in result["reason"] and result["successor"] == Y
    assert (tmp_path / "state/clip_trim.json").exists()                    # kept for inspection
    assert ClipState.load(tmp_path / "state/clip.json").data["clip"].get("status") is None
    assert digest(ctx.clip_dir) == before
    again = run(ctx)                                                       # the leftover is moved aside, then built again
    assert again["status"] == "FAILED" and "stopped at step 2" in again["reason"] and len(again["retried_from"]) == 2


def test_a_successor_id_over_the_limit_fails_before_anything_is_built(tmp_path):
    ctx = make_x(tmp_path, FakeCpa([answer((5, 6)), answer((5, 6))]))
    long = "auto_000000_0000_0000_" + "x" * 16                              # 38 characters: 38 + len("_trim") > 41
    data = json.loads((tmp_path / "state/clip.json").read_text(encoding="utf-8"))
    data["clip"]["cid"] = long
    (tmp_path / f"state/{long}.json").write_text(json.dumps(data), encoding="utf-8")
    shutil.copytree(tmp_path / "clips/clip", tmp_path / f"clips/{long}")
    out = trim_run.trim_clip(task=tmp_path, cid=long, adapters_for=lambda _rd: ctx.adapters)
    assert out["status"] == "FAILED" and "longer than 41" in out["reason"]
    assert not (tmp_path / f"state/{long}_trim.json").exists()


# ----- the clock of the successor ---------------------------------------------------------------------------------------
def test_subtitles_follow_the_measured_pieces_and_the_snap_of_the_successors_cut(tmp_path):
    ctx = make_x(tmp_path, FakeCpa([answer((5, 6)), answer((5, 6))]), script=_script(jitter_ms=12, snap_start=0.04))
    result = run(ctx)
    assert result["status"] == "TRIMMED", result
    ystate = ClipState.load(tmp_path / "state/clip_trim.json").data
    assert ystate["clip"]["start_ms"] == 40                                 # step 2 snapped the start into silence
    manifest = json.loads((tmp_path / "clips/clip_trim/compilation.json").read_text(encoding="utf-8"))
    second = manifest["pieces"][1]["compiled_start_ms"] / 1000 - 0.040      # where piece 2 begins on the cut's clock
    final = final_of(tmp_path, Y)
    windows = result["windows"]
    cue7 = next(c for c in final if c["text"] == _text(7))
    assert cue7["start_s"] == pytest.approx(second + FINAL[6]["start_s"] - windows[1][0], abs=0.0015)
    assert final[0]["start_s"] == 0.0 and final[0]["end_s"] == pytest.approx(FINAL[0]["end_s"] - 0.04, abs=0.0015)


def test_a_snap_that_pushes_a_cue_out_of_the_cut_fails(tmp_path):
    ctx = make_x(tmp_path, FakeCpa([answer((5, 6)), answer((5, 6))]), script=_script(snap_start=0.2))
    result = run(ctx)
    assert result["status"] == "FAILED" and "cue 1 " in result["reason"] and "outside the cut" in result["reason"]
    assert ClipState.load(tmp_path / "state/clip.json").data["clip"].get("status") is None


@pytest.mark.parametrize("labelled", [True, False])
def test_a_single_host_clip_is_trimmed_with_or_without_speaker_labels_in_its_speaker_file(tmp_path, labelled):
    # step 4 of a single-host clip writes `[李豆沙] ` labels; a speaker file without any label is not imported and
    # step 4 labels the successor by its own ordinary path
    host_final = [dict(c, speaker=HOST) for c in FINAL]
    ctx = make_x(tmp_path, FakeCpa([answer((5, 6)), answer((5, 6))]), speakers=labelled, session=SINGLE)
    (ctx.clip_dir / "speaker/speaker.srt").write_text(subtitle_track.format_blocks(host_final, labels=labelled), encoding="utf-8")
    result = run(ctx)
    assert result["status"] == "TRIMMED", result
    assert (tmp_path / "clips/clip_trim/import/speaker.srt").exists() is labelled
    assert {c["speaker"] for c in final_of(tmp_path, Y, "speaker/speaker.srt")} == {HOST}
    assert ClipState.load(tmp_path / "state/clip_trim.json").step("speaker")["status"] == "DONE"


# ----- the same answer again ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("responses, status", [([answer((5, 6)), answer((5, 6))], "TRIMMED"), ([answer(), answer()], "NONE")])
def test_a_final_answer_is_returned_again_without_any_call_or_new_clip(tmp_path, responses, status):
    cpa = FakeCpa(responses)
    ctx = make_x(tmp_path, cpa)
    first = run(ctx)
    scripts, calls = len(ctx.adapters.remote.scripts), list(cpa.calls)
    again = run(ctx)
    assert first["status"] == again["status"] == status and again == first
    assert cpa.calls == calls and len(ctx.adapters.remote.scripts) == scripts


# ----- clips the command leaves alone -------------------------------------------------------------------------------------
def _state_edit(task: Path, edit) -> None:
    state = ClipState.load(task / "state/clip.json")
    edit(state)
    state.save()


def test_each_skip_and_not_ready_answer_asks_nothing_and_changes_nothing(tmp_path):
    cpa = FakeCpa([])
    ctx = make_x(tmp_path, cpa)
    before = digest(ctx.clip_dir, skip="none")
    pristine = (tmp_path / "state/clip.json").read_text(encoding="utf-8")

    def reset():
        (tmp_path / "state/clip.json").write_text(pristine, encoding="utf-8")
        (ctx.clip_dir / "compilation.json").unlink(missing_ok=True)

    (ctx.clip_dir / "compilation.json").write_text("{}", encoding="utf-8")
    assert run(ctx)["status"] == "SKIP_COMPILATION"
    reset()
    _state_edit(tmp_path, lambda st: st.data.update(imported={"source": "hand", "files": {}}))
    assert run(ctx)["status"] == "SKIP_IMPORTED"
    reset()
    _state_edit(tmp_path, lambda st: st.data["steps"].update(burn={"status": "DONE"}))
    assert run(ctx)["status"] == "SKIP_ADVANCED"
    reset()
    _state_edit(tmp_path, lambda st: st.data["clip"].update(status="SUPERSEDED", superseded_by="other"))
    assert run(ctx)["status"] == "SKIP_SUPERSEDED"
    reset()
    _state_edit(tmp_path, lambda st: st.data["steps"].pop("speaker"))
    assert run(ctx)["status"] == "NOT_READY"
    reset()
    (ctx.clip_dir / "speaker/speaker.srt").unlink()
    assert run(ctx)["status"] == "NOT_READY"
    assert cpa.calls == [] and not (ctx.clip_dir / "trim").exists() and ctx.adapters.remote.scripts == []
    assert digest(ctx.clip_dir, skip="none") == {k: v for k, v in before.items() if k != "speaker/speaker.srt"}


def test_a_speaker_file_that_does_not_match_the_final_subtitles_fails(tmp_path):
    ctx = make_x(tmp_path, FakeCpa([]))
    (ctx.clip_dir / "speaker/speaker.srt").write_text(subtitle_track.format_blocks(FINAL[:-1], labels=True), encoding="utf-8")
    result = run(ctx)
    assert result["status"] == "FAILED" and "does not match" in result["reason"] and ctx.adapters.cpa.calls == []


# ----- the command line ----------------------------------------------------------------------------------------------------
def _cli(monkeypatch, ctx):
    monkeypatch.setattr(cli, "SshRemote", lambda **_k: ctx.adapters.remote)
    monkeypatch.setattr(cli, "SshCpaJudge", lambda _remote: ctx.adapters.cpa)
    monkeypatch.setattr(cli, "DanmakuSource", lambda: ctx.adapters.danmaku)


def test_trim_is_a_subcommand_and_its_exit_codes_follow_the_status(tmp_path, monkeypatch, capsys):
    assert "trim" in cli.build_parser()._subparsers._group_actions[0].choices
    cpa = FakeCpa([answer((5, 6)), answer((5, 6)), "坏"])
    ctx = make_x(tmp_path, cpa)
    _cli(monkeypatch, ctx)
    assert cli.main(["trim", "--task-dir", str(tmp_path), "--cid", X]) == 0
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(line) == {"cid": X, "status": "TRIMMED", "successor": Y, "removed_cues": 2, "reason": json.loads(line)["reason"]}
    assert cli.main(["trim", "--task-dir", str(tmp_path), "--cid", X]) == 0        # idempotent
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["status"] == "TRIMMED"


def test_trim_exit_codes_for_failed_not_ready_none_and_skip(tmp_path, monkeypatch, capsys):
    ctx = make_x(tmp_path / "failed", FakeCpa(["坏"]))
    _cli(monkeypatch, ctx)
    assert cli.main(["trim", "--task-dir", str(tmp_path / "failed"), "--cid", X]) == 2
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["status"] == "FAILED"
    ctx = make_x(tmp_path / "none", FakeCpa([answer(), answer()]))
    _cli(monkeypatch, ctx)
    assert cli.main(["trim", "--task-dir", str(tmp_path / "none"), "--cid", X]) == 0
    ctx = make_x(tmp_path / "notready", FakeCpa([]))
    ctx.state.data["steps"].pop("speaker")
    ctx.state.save()
    _cli(monkeypatch, ctx)
    assert cli.main(["trim", "--task-dir", str(tmp_path / "notready"), "--cid", X]) == 3
    ctx = make_x(tmp_path / "skip", FakeCpa([]))
    ctx.state.data["imported"] = {"source": "hand", "files": {}}
    ctx.state.save()
    _cli(monkeypatch, ctx)
    assert cli.main(["trim", "--task-dir", str(tmp_path / "skip"), "--cid", X]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["status"] == "SKIP_IMPORTED"


def test_the_trim_call_has_a_registered_effort_tier_and_the_thanks_words_are_a_rule():
    from src.autoslice.manual_lane.lane_rules import rules

    entry = rules("effort")["purposes"][trim_run.PURPOSE]
    boundary = rules("effort")["purposes"]["boundary"]
    assert entry["effort"] == "high" == boundary["effort"] and entry.get("model") == boundary.get("model")
    assert rules("trim_thanks_words")["words"] == ["谢谢", "感谢", "多谢", "ありがと", "thank"]
    assert trim.guard([(2, 2)], [{"text": "开头", "start_s": 0, "end_s": 1}, {"text": "Thank you so much", "start_s": 2, "end_s": 3},
                                 {"text": "结尾", "start_s": 4, "end_s": 5}],
                      thanks_words=rules("trim_thanks_words")["words"], clip_len_s=6.0, min_keep_s=1.0)[0] == [(2, 2)]


# ----- a build that did not finish is moved aside and built again ----------------------------------------------------------
def _stamped(task: Path, rel: str) -> list[Path]:
    return sorted((task / rel).parent.glob(f"{Path(rel).name}.failed-*"))


def test_a_leftover_successor_is_moved_aside_not_deleted_and_the_build_runs_again(tmp_path):
    made: list[str] = []
    cut_ok = _script()

    def script(name, body, remote):
        if name == "compile-concat":              # what the real script does: write the joined file, over an older one
            made.append(f"joined-{len(made) + 1}")
            remote.files["/r/sources/compilation-clip_trim.flv"] = made[-1].encode()
        return cut_ok(name, body, remote)

    ctx = make_x(tmp_path, FakeCpa([answer((5, 6))] * 4), script=_script(cut_rc=1))
    ctx.adapters.remote.on_script = lambda name, body, remote: (
        RemoteResult(1, "", "ffmpeg broke (fake)") if name == "s2-cut" else script(name, body, remote))
    first = run(ctx)
    assert first["status"] == "FAILED" and "retried_from" not in first
    old_state = (tmp_path / "state/clip_trim.json").read_text(encoding="utf-8")
    old_manifest = (tmp_path / "clips/clip_trim/compilation.json").read_text(encoding="utf-8")
    assert ctx.adapters.remote.files["/r/sources/compilation-clip_trim.flv"] == b"joined-1"
    ctx.adapters.remote.on_script = script                                  # the cause is gone

    second = run(ctx)
    assert second["status"] == "TRIMMED", second
    state_aside, = _stamped(tmp_path, "state/clip_trim.json")
    clip_aside, = _stamped(tmp_path, "clips/clip_trim")
    assert second["retried_from"] == [str(state_aside), str(clip_aside)]
    assert re.fullmatch(r"clip_trim\.json\.failed-\d{8}T\d{6}Z", state_aside.name)
    assert re.fullmatch(r"clip_trim\.failed-\d{8}T\d{6}Z", clip_aside.name)
    assert state_aside.read_text(encoding="utf-8") == old_state             # moved, not changed
    assert (clip_aside / "compilation.json").read_text(encoding="utf-8") == old_manifest
    assert ClipState.load(tmp_path / "state/clip_trim.json").step("speaker")["status"] == "DONE"      # a new, finished clip
    assert ClipState.load(tmp_path / "state/clip.json").data["clip"]["superseded_by"] == Y
    assert json.loads((ctx.clip_dir / "trim/decision.json").read_text(encoding="utf-8"))["retried_from"] == second["retried_from"]
    # the media host: both builds ran, the second wrote the joined file over the first one (the manifest was gone with
    # the moved directory, so compile_pieces did not take the old file for finished work)
    names = [n for n, _ in ctx.adapters.remote.scripts]
    assert names.count("compile-cut") == 2 and names.count("compile-concat") == 2
    assert ctx.adapters.remote.files["/r/sources/compilation-clip_trim.flv"] == b"joined-2"


def test_a_leftover_with_no_decision_of_the_original_is_also_moved_aside(tmp_path):
    ctx = make_x(tmp_path, FakeCpa([answer((5, 6))] * 2))
    (tmp_path / "state/clip_trim.json").write_text("{}", encoding="utf-8")
    (tmp_path / "clips/clip_trim").mkdir(parents=True)
    (tmp_path / "clips/clip_trim/marker").write_text("x", encoding="utf-8")
    assert not (ctx.clip_dir / "trim/decision.json").exists()
    result = run(ctx)
    assert result["status"] == "TRIMMED" and len(result["retried_from"]) == 2
    assert (_stamped(tmp_path, "clips/clip_trim")[0] / "marker").read_text(encoding="utf-8") == "x"


def test_a_successor_of_a_superseded_original_is_not_a_leftover(tmp_path):
    cpa = FakeCpa([])
    ctx = make_x(tmp_path, cpa)
    (tmp_path / "state/clip_trim.json").write_text("{}", encoding="utf-8")
    (tmp_path / "clips/clip_trim").mkdir(parents=True)
    _state_edit(tmp_path, lambda st: st.data["clip"].update(status="SUPERSEDED", superseded_by=Y))
    result = run(ctx)
    assert result["status"] == "SKIP_SUPERSEDED" and "retried_from" not in result
    assert (tmp_path / "state/clip_trim.json").read_text(encoding="utf-8") == "{}" and (tmp_path / "clips/clip_trim").is_dir()
    assert not _stamped(tmp_path, "state/clip_trim.json") and not _stamped(tmp_path, "clips/clip_trim") and cpa.calls == []


def test_a_decided_original_does_not_move_its_successor(tmp_path):
    ctx = make_x(tmp_path, FakeCpa([answer((5, 6)), answer((5, 6))]))
    assert run(ctx)["status"] == "TRIMMED"
    state_before = (tmp_path / "state/clip_trim.json").read_text(encoding="utf-8")
    again = run(ctx)
    assert again["status"] == "TRIMMED" and "retried_from" not in again
    assert (tmp_path / "state/clip_trim.json").read_text(encoding="utf-8") == state_before
    assert not _stamped(tmp_path, "state/clip_trim.json")


# ----- `run --trim` ---------------------------------------------------------------------------------------------------------
def _stub_run(monkeypatch, *, stops=None):
    """cli.run_clip replaced: records (cid, until) and stops where `stops` says ((cid, until) -> step number)."""
    calls: list[tuple[str, int | None]] = []

    def fake(ctx, steps, *, until=None, upload=False, quote=None, **_schedule):     # deps, gates, progress: the lane's schedule
        cid = ctx.state.data["clip"]["cid"]
        calls.append((cid, until))
        stop = (stops or {}).get((cid, until))
        return RunReport(passed=[1, 0, 2], stopped_at=stop, reason="boom" if stop else "")

    monkeypatch.setattr(cli, "run_clip", fake)
    return calls


def _no_trim(monkeypatch):
    def refuse(**_k):
        raise AssertionError("trim_clip was called")

    monkeypatch.setattr(cli, "trim_clip", refuse)


def _run_cli(capsys, tmp_path, *extra):
    rc = cli.main(["run", "--task-dir", str(tmp_path), "--cid", X, *extra])
    lines = capsys.readouterr().out.strip().splitlines()
    return rc, lines, json.loads(lines[-1])


def test_run_without_trim_makes_no_trim_call_and_prints_the_line_it_always_did(tmp_path, monkeypatch, capsys):
    ctx = make_x(tmp_path, FakeCpa([]))
    _cli(monkeypatch, ctx)
    _no_trim(monkeypatch)
    calls = _stub_run(monkeypatch)
    rc, lines, out = _run_cli(capsys, tmp_path, "--until", "6")
    assert rc == 0 and calls == [(X, 6)] and len(lines) == 1
    assert list(out) == ["cid", "passed", "reused", "stopped_at", "reason", "halted_before_upload", "cpa_calls"]
    assert not (ctx.clip_dir / "trim").exists() and not (ctx.clip_dir / "PROCESS.md").exists()


def test_run_trim_is_off_by_default_and_run_has_a_source_dir_option():
    parser = cli.build_parser()
    args = parser.parse_args(["run", "--task-dir", "t", "--cid", "c"])
    assert args.trim is False and args.source_dir is None
    assert parser.parse_args(["run", "--task-dir", "t", "--cid", "c", "--trim", "--source-dir", "/s"]).source_dir == "/s"


@pytest.mark.parametrize("until", [None, 7])
def test_run_trim_trimmed_goes_on_with_the_new_clip_and_says_what_replaced_what(tmp_path, monkeypatch, capsys, until):
    ctx = make_x(tmp_path, FakeCpa([answer((5, 6), (12, 12))] * 2))
    _cli(monkeypatch, ctx)
    calls = _stub_run(monkeypatch)
    rc, lines, out = _run_cli(capsys, tmp_path, "--trim", *(["--until", str(until)] if until else []))
    assert rc == 0 and calls == [(X, 4), (Y, until)]
    assert lines[0] == f"片中剪除：原片 {X} 已被 {Y} 取代，剪去 3 句"
    assert out["cid"] == Y and out["trim"] == {"status": "TRIMMED", "replaces": X, "removed_cues": 3}
    assert ClipState.load(tmp_path / "state/clip.json").data["clip"]["superseded_by"] == Y


def test_run_trim_exit_code_and_line_follow_the_new_clip(tmp_path, monkeypatch, capsys):
    ctx = make_x(tmp_path, FakeCpa([answer((5, 6))] * 2))
    _cli(monkeypatch, ctx)
    _stub_run(monkeypatch, stops={(Y, None): 6})
    rc, _lines, out = _run_cli(capsys, tmp_path, "--trim")
    assert rc == 1 and out["cid"] == Y and out["stopped_at"] == 6 and out["trim"]["status"] == "TRIMMED"


def test_run_trim_with_until_at_the_speaker_step_still_cuts_and_asks_the_new_clip_for_the_same_until(tmp_path, monkeypatch, capsys):
    ctx = make_x(tmp_path, FakeCpa([answer((5, 6))] * 2))
    _cli(monkeypatch, ctx)
    calls = _stub_run(monkeypatch)
    rc, _lines, out = _run_cli(capsys, tmp_path, "--trim", "--until", "4")
    assert rc == 0 and calls == [(X, 4), (Y, 4)] and out["trim"]["status"] == "TRIMMED"


def test_run_trim_with_nothing_to_cut_goes_on_with_the_original(tmp_path, monkeypatch, capsys):
    ctx = make_x(tmp_path, FakeCpa([answer(), answer()]))
    _cli(monkeypatch, ctx)
    calls = _stub_run(monkeypatch)
    rc, lines, out = _run_cli(capsys, tmp_path, "--trim")
    assert rc == 0 and calls == [(X, 4), (X, None)] and len(lines) == 1
    assert out["cid"] == X and out["trim"]["status"] == "NONE" and not (tmp_path / "state/clip_trim.json").exists()
    assert not (ctx.clip_dir / "PROCESS.md").exists()


def test_run_trim_that_fails_goes_on_with_the_original_and_leaves_the_reason(tmp_path, monkeypatch, capsys):
    ctx = make_x(tmp_path, FakeCpa(["这不是 JSON"]))
    _cli(monkeypatch, ctx)
    calls = _stub_run(monkeypatch)
    rc, lines, out = _run_cli(capsys, tmp_path, "--trim", "--until", "7")
    assert rc == 0 and calls == [(X, 4), (X, 7)]
    assert lines[0].startswith("片中剪除未完成：") and "CPA_RESPONSE_UNUSABLE" in lines[0]
    assert out["cid"] == X and out["trim"]["status"] == "FAILED"
    process = (ctx.clip_dir / "PROCESS.md").read_text(encoding="utf-8")
    assert "片中剪除未完成：FAILED: CPA_RESPONSE_UNUSABLE" in process


def test_run_trim_that_is_not_ready_also_goes_on_and_leaves_the_reason(tmp_path, monkeypatch, capsys):
    ctx = make_x(tmp_path, FakeCpa([]))
    (ctx.clip_dir / "speaker/speaker.srt").unlink()
    _cli(monkeypatch, ctx)
    calls = _stub_run(monkeypatch)
    rc, lines, out = _run_cli(capsys, tmp_path, "--trim")
    assert rc == 0 and calls == [(X, 4), (X, None)] and out["trim"]["status"] == "NOT_READY"
    assert lines[0].startswith("片中剪除未完成：") and "片中剪除未完成：NOT_READY" in (ctx.clip_dir / "PROCESS.md").read_text(encoding="utf-8")


def test_run_trim_with_an_until_before_the_speaker_step_does_not_cut(tmp_path, monkeypatch, capsys):
    ctx = make_x(tmp_path, FakeCpa([]))
    _cli(monkeypatch, ctx)
    _no_trim(monkeypatch)
    calls = _stub_run(monkeypatch)
    for until in (0, 1, 2, 3):
        rc, _lines, out = _run_cli(capsys, tmp_path, "--trim", "--until", str(until))
        assert rc == 0 and "trim" not in out
    assert calls == [(X, 0), (X, 1), (X, 2), (X, 3)]


def test_run_trim_with_a_speaker_step_that_does_not_pass_does_not_cut(tmp_path, monkeypatch, capsys):
    ctx = make_x(tmp_path, FakeCpa([]))
    _cli(monkeypatch, ctx)
    _no_trim(monkeypatch)
    calls = _stub_run(monkeypatch, stops={(X, 4): 4})
    rc, lines, out = _run_cli(capsys, tmp_path, "--trim")
    assert rc == 1 and calls == [(X, 4)] and len(lines) == 1 and out["stopped_at"] == 4 and "trim" not in out


def test_run_trim_on_a_superseded_clip_still_refuses(tmp_path, monkeypatch, capsys):
    ctx = make_x(tmp_path, FakeCpa([]))
    _cli(monkeypatch, ctx)
    _no_trim(monkeypatch)
    calls = _stub_run(monkeypatch)
    _state_edit(tmp_path, lambda st: st.data["clip"].update(status="SUPERSEDED", superseded_by="other"))
    rc = cli.main(["run", "--task-dir", str(tmp_path), "--cid", X, "--trim"])
    assert rc == 2 and "SUPERSEDED by other" in capsys.readouterr().out and calls == []
