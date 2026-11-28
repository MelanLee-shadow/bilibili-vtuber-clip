"""Tests for recut invalidation after `redo` (steps A-E): recut_pending, remote/local cleanup,
ASR interval stamping, transcript-cut duration mismatch guard, and import-subtitles cut guard.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from fakes import FakeCpa, FakeRemote, IdCpa, accept_title, fake_cover, make_ctx, push_dir, srt
from test_pipeline_fake_e2e import ScriptedCpa, _fake_fmr

from src.autoslice.manual_lane import compilation, redo, stage_impl as si, stage_package as sp, step_import, step3_subtitles
from src.autoslice.manual_lane.adapters import Adapters, LocalRemote, RemoteResult
from src.autoslice.manual_lane.runner import run_clip
from src.autoslice.manual_lane.stages import STEPS
from src.autoslice.manual_lane.state import ClipState, StateError

RD = "/r/clip"


def _no_adapters(_remote_dir: str) -> Adapters:
    return Adapters()


def _srt_text(prefix: str, n: int, step: float = 2.0) -> str:
    blocks = []
    for i in range(1, n + 1):
        s = (i - 1) * step
        blocks.append(f"{i}\n{si.fmt_ts(s)} --> {si.fmt_ts(s + step)}\n{prefix}{i}")
    return "\n\n".join(blocks) + "\n"


class _Step1To3Cpa:
    """Answers boundary calls from a scripted list and delegates step 3 adjudicate/canon calls to IdCpa."""

    def __init__(self, boundary_answers: list[str]) -> None:
        self._boundary = list(boundary_answers)
        self._id = IdCpa()
        self.calls: list[str] = []

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir, **_kw):
        self.calls.append(call_id)
        if call_id.startswith("boundary"):
            return {
                "call_id": call_id,
                "model": model,
                "effort": effort,
                "rc": 0,
                "elapsed_s": 0.1,
                "response_text": self._boundary.pop(0),
                "stderr_tail": "",
            }
        return self._id.ask(
            call_id=call_id,
            prompt_text=prompt_text,
            model=model,
            effort=effort,
            remote_dir=remote_dir,
        )


def test_witness_redo_from_boundary_cleans_remote_and_subtitles_from_new_transcripts(tmp_path):
    # 1) Verify the generated `redo-clean-remote` shell script on a real directory via LocalRemote.
    real_task = tmp_path / "real-host-check"
    real_remote_root = tmp_path / "media-root" / "2026-10-24" / "clip"
    local_remote = LocalRemote(scripts_dir=str(tmp_path / "scripts"))
    real_ctx = make_ctx(
        real_task,
        remote=local_remote,
        cpa=FakeCpa([]),
        remote_dir=str(real_remote_root),
    )
    derived_remote = ("asr", "listen", "witness", "speaker", "burn", "cover", "tags", "fmr", "pkg")
    derived_local = ("asr", "listen", "witness")
    preserved = (
        "import/final.srt",
        "import_cover/cover.png",
        "upload/online.json",
        "title/accepted.json",
        "compilation/manifest.json",
        "danmaku/recording.xml",
    )
    for d in derived_remote:
        p = real_remote_root / d / "stale.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("stale", encoding="utf-8")
    for d in derived_local:
        p = real_ctx.clip_dir / d / "stale.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("stale", encoding="utf-8")
    for rel in preserved:
        content = "<i>keep:danmaku</i>" if rel.endswith(".xml") else f"keep:{rel}"
        for base in (real_remote_root, real_ctx.clip_dir):
            p = base / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
    real_ctx.state.record("cut", ok=True, reason="", products={}, cpa=[], note="done")

    real_out = redo.redo_clip(
        task=real_task,
        cid="clip",
        start="1",
        adapters_for=lambda _rd: Adapters(remote=local_remote),
    )
    assert real_out["remote_removed"] == list(derived_remote)
    for d in derived_remote:
        assert not (real_remote_root / d).exists(), f"remote {d} should have been removed"
        assert d in real_out["summary"]
    for d in derived_local:
        assert not (real_ctx.clip_dir / d).exists(), f"local {d} should have been removed"
    for rel in preserved:
        content = "<i>keep:danmaku</i>" if rel.endswith(".xml") else f"keep:{rel}"
        assert (real_remote_root / rel).read_text(encoding="utf-8") == content
        assert (real_ctx.clip_dir / rel).read_text(encoding="utf-8") == content
    assert real_out["summary"] in (real_ctx.clip_dir / "PROCESS.md").read_text(encoding="utf-8")

    # 2) Full e2e witness on FakeRemote: run 1..3 -> redo from 1 -> run 1..3 with shorter boundary.
    e2e_task = tmp_path / "e2e"
    asr_calls: list[str] = []
    recut_pending_during_clean: list[object] = []
    cut_durations = [70.0, 60.0]
    asr_batches = [
        ("旧句", 35),
        ("新句", 30),
    ]

    def on_script(name: str, body: str, remote: FakeRemote) -> RemoteResult:
        if name == "s0-dedupe":
            remote.files[f"{RD}/dedup/result.json"] = json.dumps(
                {"decision": "UNKNOWN", "block_upload": False}
            ).encode()
        elif name == "s2-cut":
            dur = cut_durations.pop(0)
            remote.files[f"{RD}/cut/probe.json"] = json.dumps(
                {
                    "format": {"duration": f"{dur:.3f}"},
                    "streams": [{"codec_type": "video", "width": 1920, "height": 1080}, {"codec_type": "audio"}],
                }
            ).encode()
            remote.files[f"{RD}/cut/cut.mp4"] = f"cut-{dur}".encode()
        elif name == "s3-asr":
            asr_calls.append(body)
            prefix, count = asr_batches.pop(0)
            # Simulate free_asr_paced.py writing fresh transcripts only after any rm -f in the script
            srt_bytes = _srt_text(prefix, count, step=2.0).encode("utf-8")
            remote.files[f"{RD}/asr/bcut.srt"] = srt_bytes
            remote.files[f"{RD}/asr/jianying.srt"] = srt_bytes
            return RemoteResult(0, "ASR_OK\n", "")
        elif name.startswith("s3-listen"):
            remote.files[f"{RD}/listen/out/all.json"] = b"[]"
            return RemoteResult(0, "LISTEN_DONE\n", "")
        elif name == "redo-clean-remote":
            on_disk = ClipState.load(e2e_task / "state/clip.json").data
            recut_pending_during_clean.append(on_disk.get("recut_pending"))
            assert "cut" not in on_disk["steps"] and "subtitles" not in on_disk["steps"]
            removed = []
            for d in derived_remote:
                prefix = f"{RD}/{d}/"
                matched = [k for k in list(remote.files) if k.startswith(prefix)]
                if matched:
                    for k in matched:
                        del remote.files[k]
                    removed.append(d)
            return RemoteResult(0, "".join(f"REMOVED {d}\n" for d in removed), "")
        return RemoteResult(0, "", "")

    remote = FakeRemote(on_script=on_script)
    cpa = _Step1To3Cpa(
        [
            json.dumps({"verdict": "ACCEPT", "start_cue": 6, "end_cue": 40, "reason": "第一轮"}),
            json.dumps({"verdict": "ACCEPT", "start_cue": 11, "end_cue": 40, "reason": "重切缩短"}),
        ]
    )
    ctx = make_ctx(e2e_task, remote=remote, cpa=cpa)
    steps_1_to_3 = [s for s in STEPS if s.number in (1, 0, 2, 3)]

    first = run_clip(ctx, steps_1_to_3)
    assert first.stopped_at is None, (first.stopped_at, first.reason)
    assert first.passed == [1, 0, 2, 3]
    assert "旧句1" in (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")
    assert len(asr_calls) == 1
    stamp1 = json.loads((ctx.clip_dir / "asr/interval.json").read_text(encoding="utf-8"))
    assert stamp1 == {"source_name": "src.flv", "start_ms": 10_000, "end_ms": 80_000}

    # Plant extra derived files & preserved files on remote + local before redo
    for d in derived_remote:
        remote.files.setdefault(f"{RD}/{d}/extra.dat", b"stale")
    for rel in preserved:
        content = "<i>keep:danmaku</i>" if rel.endswith(".xml") else f"keep:{rel}"
        remote.files[f"{RD}/{rel}"] = content.encode()
        p = ctx.clip_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    for d in derived_local:
        p = ctx.clip_dir / d / "extra.dat"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("stale", encoding="utf-8")

    redo_out = redo.redo_clip(
        task=e2e_task,
        cid="clip",
        start="1",
        adapters_for=lambda _rd: Adapters(remote=remote),
    )
    assert len(recut_pending_during_clean) == 1
    assert isinstance(recut_pending_during_clean[0], dict)
    assert recut_pending_during_clean[0]["cleaned"] is False
    assert recut_pending_during_clean[0]["rebuild"] is False
    assert isinstance(recut_pending_during_clean[0]["products"], list)
    assert set(redo_out["remote_removed"]) == set(derived_remote)
    assert ClipState.load(ctx.state.path).data.get("recut_pending") == {"cleaned": True, "rebuild": False}
    for d in derived_local:
        assert not (ctx.clip_dir / d).exists()
    for rel in preserved:
        content = "<i>keep:danmaku</i>" if rel.endswith(".xml") else f"keep:{rel}"
        assert remote.files[f"{RD}/{rel}"] == content.encode()
        assert (ctx.clip_dir / rel).read_text(encoding="utf-8") == content

    # Re-run steps 1..3 with a new boundary proposal (cues 11..40 => 20_000..80_000 ms, 60 s)
    state2 = ClipState.load(ctx.state.path)
    state2.data["clip"]["proposal"] = {"start_cue": 11, "end_cue": 40}
    state2.save()
    ctx_after = type(ctx)(
        state=state2,
        session=state2.data["session"],
        clip_dir=ctx.clip_dir,
        remote_dir=ctx.remote_dir,
        adapters=ctx.adapters,
    )
    second = run_clip(ctx_after, steps_1_to_3)
    assert second.stopped_at is None, (second.stopped_at, second.reason)
    assert second.passed == [1, 0, 2, 3]
    assert len(asr_calls) == 2
    assert f"rm -f {RD}/asr/bcut.srt {RD}/asr/bcut.json {RD}/asr/jianying.srt {RD}/asr/jianying.json" in asr_calls[1]
    assert "recut_pending" not in ClipState.load(ctx.state.path).data
    final_text = (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")
    assert "新句1" in final_text and "旧句1" not in final_text
    stamp2 = json.loads((ctx.clip_dir / "asr/interval.json").read_text(encoding="utf-8"))
    assert stamp2 == {"source_name": "src.flv", "start_ms": 20_000, "end_ms": 80_000}


def test_recut_pending_forces_step2_rerun_and_clears_marker_on_cut_completion(tmp_path):
    cut_calls: list[str] = []
    fail_cut = True

    def on_script(name: str, _body: str, remote: FakeRemote) -> RemoteResult:
        if name == "s2-cut":
            cut_calls.append(name)
            if fail_cut:
                return RemoteResult(1, "", "ffmpeg failed")
            remote.files[f"{RD}/cut/probe.json"] = json.dumps(
                {
                    "format": {"duration": "70.000"},
                    "streams": [{"codec_type": "video", "width": 1920, "height": 1080}],
                }
            ).encode()
            return RemoteResult(0, "", "")
        return RemoteResult(0, "", "")

    ctx = make_ctx(tmp_path, remote=FakeRemote(on_script=on_script), cpa=FakeCpa([]))
    for rel, content in (
        ("boundary/decision.json", "{}"),
        ("dedup/result.json", "{}"),
        ("cut/probe.json", json.dumps({"format": {"duration": "70.000"}})),
    ):
        p = ctx.clip_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    ctx.state.record("boundary", ok=True, reason="", products={"boundary": str(ctx.clip_dir / "boundary/decision.json")}, cpa=[], note="b")
    ctx.state.record("dedupe", ok=True, reason="", products={"dedup_result": str(ctx.clip_dir / "dedup/result.json")}, cpa=[], note="d")
    ctx.state.record("cut", ok=True, reason="", products={"cut_probe": str(ctx.clip_dir / "cut/probe.json")}, cpa=[], note="c")
    ctx.state.data["recut_pending"] = {"cleaned": True, "rebuild": False}
    ctx.state.save()

    steps = [s for s in STEPS if s.number in (1, 0, 2)]
    failed = run_clip(ctx, steps)
    assert failed.stopped_at == 2 and failed.reason == "CUT_FFMPEG_FAILED"
    assert failed.reused == [1, 0]
    assert cut_calls == ["s2-cut"]
    assert ClipState.load(ctx.state.path).data.get("recut_pending") == {"cleaned": True, "rebuild": False}

    fail_cut = False
    passed = run_clip(ctx, steps)
    assert passed.stopped_at is None
    assert passed.reused == [1, 0] and passed.passed == [2]
    assert cut_calls == ["s2-cut", "s2-cut"]
    assert "recut_pending" not in ClipState.load(ctx.state.path).data


def test_redo_from_step3_leaves_remote_untouched_unlike_redo_from_cut(tmp_path):
    # Case 1: redo from step 3 (not including cut) leaves remote and local asr/ alone and does not set recut_pending
    task_a = tmp_path / "from-step3"
    ctx_a = make_ctx(task_a, remote=None, cpa=None)
    for step in STEPS:
        if step.number == 10:
            continue
        p = ctx_a.clip_dir / "products" / f"{step.key}.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(step.key, encoding="utf-8")
        ctx_a.state.record(step.key, ok=True, reason="", products={"p": str(p)}, cpa=[], note=step.key)
    (ctx_a.clip_dir / "asr").mkdir(parents=True, exist_ok=True)
    (ctx_a.clip_dir / "asr/bcut.srt").write_text(srt(3), encoding="utf-8")

    out_3 = redo.redo_clip(task=task_a, cid="clip", start="3", adapters_for=_no_adapters)
    assert out_3["remote_removed"] == []
    assert "主机已清派生目录" not in out_3["summary"]
    assert "recut_pending" not in ClipState.load(ctx_a.state.path).data
    assert (ctx_a.clip_dir / "asr/bcut.srt").is_file()

    # Case 2: redo from step 2 (cut) cleans remote & local asr/listen/witness and sets recut_pending
    task_b = tmp_path / "from-step2"
    remote_b = FakeRemote(on_script=lambda name, _b, _r: RemoteResult(0, "REMOVED asr\nREMOVED burn\n", "")
                          if name == "redo-clean-remote" else RemoteResult(0, "", ""))
    ctx_b = make_ctx(task_b, remote=remote_b, cpa=None)
    for step in STEPS:
        if step.number == 10:
            continue
        p = ctx_b.clip_dir / "products" / f"{step.key}.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(step.key, encoding="utf-8")
        ctx_b.state.record(step.key, ok=True, reason="", products={"p": str(p)}, cpa=[], note=step.key)
    (ctx_b.clip_dir / "asr").mkdir(parents=True, exist_ok=True)
    (ctx_b.clip_dir / "asr/bcut.srt").write_text(srt(3), encoding="utf-8")

    out_2 = redo.redo_clip(task=task_b, cid="clip", start="2", adapters_for=lambda _rd: Adapters(remote=remote_b))
    assert [n for n, _ in remote_b.scripts] == ["redo-clean-remote"]
    assert out_2["remote_removed"] == ["asr", "burn"]
    assert "主机已清派生目录：asr, burn" in out_2["summary"]
    assert ClipState.load(ctx_b.state.path).data.get("recut_pending") == {"cleaned": True, "rebuild": False}
    assert not (ctx_b.clip_dir / "asr").exists()


def test_asr_reuse_for_legacy_unstamped_vs_stamped_and_recut_pending(tmp_path):
    # Verify free_asr_body checks remote stamp and overwrites stale remote files before invoking free_asr_paced.py
    body, _timeout = si.free_asr_body("/r/clip", stamp={"source_name": "src.flv", "start_ms": 10_000, "end_ms": 80_000})
    rm_line = "rm -f /r/clip/asr/bcut.srt /r/clip/asr/bcut.json /r/clip/asr/jianying.srt /r/clip/asr/jianying.json"
    assert rm_line in body
    assert body.index(rm_line) < body.index("free_asr_paced.py")

    def _run_step3(sub: str, *, stamp: dict | None, recut_pending: bool, clip_override: dict | None = None):
        s3_calls: list[str] = []

        def on_script(name: str, body_txt: str, remote: FakeRemote) -> RemoteResult:
            if name == "s3-asr":
                s3_calls.append(body_txt)
                fresh = _srt_text("新识别", 3, step=2.0).encode("utf-8")
                remote.files[f"{RD}/asr/bcut.srt"] = fresh
                remote.files[f"{RD}/asr/jianying.srt"] = fresh
                return RemoteResult(0, "ASR_OK\n", "")
            if name.startswith("s3-listen"):
                remote.files[f"{RD}/listen/out/all.json"] = b"[]"
                return RemoteResult(0, "LISTEN_DONE\n", "")
            return RemoteResult(0, "", "")

        ctx = make_ctx(tmp_path / sub, remote=FakeRemote(on_script=on_script), cpa=IdCpa(), bcut_text=srt(3, step=2.0))
        if clip_override:
            ctx.state.data["clip"].update(clip_override)
        if recut_pending:
            ctx.state.data["recut_pending"] = True
        ctx.state.save()
        asr_dir = ctx.clip_dir / "asr"
        asr_dir.mkdir(parents=True, exist_ok=True)
        old_srt = _srt_text("旧识别", 3, step=2.0)
        (asr_dir / "bcut.srt").write_text(old_srt, encoding="utf-8")
        (asr_dir / "jianying.srt").write_text(old_srt, encoding="utf-8")
        if stamp is not None:
            (asr_dir / "interval.json").write_text(json.dumps(stamp), encoding="utf-8")
        out = si.step3_subtitles(ctx)
        assert out.ok, (out.reason, out.note)
        return ctx, s3_calls

    # Case A: unchanged legacy clip without stamp and no recut_pending -> reused without s3-asr
    ctx_a, calls_a = _run_step3("legacy-ok", stamp=None, recut_pending=False)
    assert calls_a == []
    assert "旧识别1" in (ctx_a.clip_dir / "subs/final.srt").read_text(encoding="utf-8")

    # Case B: legacy clip without stamp, but recut_pending=True -> must re-recognise
    ctx_b, calls_b = _run_step3("legacy-recut", stamp=None, recut_pending=True)
    assert len(calls_b) == 1
    assert "新识别1" in (ctx_b.clip_dir / "subs/final.srt").read_text(encoding="utf-8")
    assert json.loads((ctx_b.clip_dir / "asr/interval.json").read_text(encoding="utf-8")) == {
        "source_name": "src.flv",
        "start_ms": 10_000,
        "end_ms": 80_000,
    }

    # Case C1: stamped clip with changed interval -> must re-recognise
    ctx_c1, calls_c1 = _run_step3(
        "stamped-diff-interval",
        stamp={"source_name": "src.flv", "start_ms": 10_000, "end_ms": 80_000},
        recut_pending=False,
        clip_override={"start_ms": 20_000, "end_ms": 80_000},
    )
    assert len(calls_c1) == 1
    assert json.loads((ctx_c1.clip_dir / "asr/interval.json").read_text(encoding="utf-8"))["start_ms"] == 20_000

    # Case C2: stamped clip with changed source_name -> must re-recognise
    _ctx_c2, calls_c2 = _run_step3(
        "stamped-diff-source",
        stamp={"source_name": "old.flv", "start_ms": 10_000, "end_ms": 80_000},
        recut_pending=False,
    )
    assert len(calls_c2) == 1

    # Case D: stamped clip with matching interval and no recut_pending -> reused without s3-asr
    ctx_d, calls_d = _run_step3(
        "stamped-match",
        stamp={"source_name": "src.flv", "start_ms": 10_000, "end_ms": 80_000},
        recut_pending=False,
    )
    assert calls_d == []
    assert "旧识别1" in (ctx_d.clip_dir / "subs/final.srt").read_text(encoding="utf-8")


def _write_probe(clip_dir: Path, duration_s: float) -> None:
    p = clip_dir / "cut/probe.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"format": {"duration": f"{duration_s:.3f}"}}), encoding="utf-8")


def _plant_asr(clip_dir: Path, bcut_end_s: float, jy_end_s: float) -> None:
    asr_dir = clip_dir / "asr"
    asr_dir.mkdir(parents=True, exist_ok=True)
    (asr_dir / "bcut.srt").write_text(f"1\n00:00:00,000 --> {si.fmt_ts(bcut_end_s)}\n听写甲\n", encoding="utf-8")
    (asr_dir / "jianying.srt").write_text(f"1\n00:00:00,000 --> {si.fmt_ts(jy_end_s)}\n听写乙\n", encoding="utf-8")


def test_stale_transcript_or_final_srt_past_cut_duration_stops_with_transcript_cut_mismatch(tmp_path):
    # 1) Stale asr/bcut.srt ending at 60.6s > 60.0 + 0.5 -> TRANSCRIPT_CUT_MISMATCH, 0 CPA calls
    cpa1 = IdCpa()
    ctx1 = make_ctx(tmp_path / "bcut-over", remote=FakeRemote(), cpa=cpa1)
    _write_probe(ctx1.clip_dir, 60.0)
    _plant_asr(ctx1.clip_dir, 60.6, 55.0)
    out1 = si.step3_subtitles(ctx1)
    assert not out1.ok and out1.reason == "TRANSCRIPT_CUT_MISMATCH" and cpa1.calls == []

    # 2) Stale asr/jianying.srt ending at 62.0s -> TRANSCRIPT_CUT_MISMATCH
    cpa2 = IdCpa()
    ctx2 = make_ctx(tmp_path / "jy-over", remote=FakeRemote(), cpa=cpa2)
    _write_probe(ctx2.clip_dir, 60.0)
    _plant_asr(ctx2.clip_dir, 55.0, 62.0)
    out2 = si.step3_subtitles(ctx2)
    assert not out2.ok and out2.reason == "TRANSCRIPT_CUT_MISMATCH" and cpa2.calls == []

    # 3) Exact tolerance boundary 60.5s <= 60.0 + 0.5 -> passes step 3
    remote3 = FakeRemote(
        on_script=lambda name, _b, r: (
            r.files.__setitem__(f"{RD}/listen/out/all.json", b"[]") or RemoteResult(0, "LISTEN_DONE\n", "")
            if name.startswith("s3-listen")
            else RemoteResult(0, "", "")
        )
    )
    ctx3 = make_ctx(tmp_path / "boundary-ok", remote=remote3, cpa=IdCpa())
    _write_probe(ctx3.clip_dir, 60.0)
    _plant_asr(ctx3.clip_dir, 60.5, 60.5)
    out3 = si.step3_subtitles(ctx3)
    assert out3.ok, (out3.reason, out3.note)

    # 4) Imported subs/final.srt ending at 65.0s for a 60.0s cut -> TRANSCRIPT_CUT_MISMATCH
    ctx4 = make_ctx(tmp_path / "import-final-over", remote=FakeRemote(), cpa=FakeCpa([]))
    _write_probe(ctx4.clip_dir, 60.0)
    push = push_dir(tmp_path / "push4", ctx4.state.data["clip"], [(0.0, 65.0, "李豆沙", "超时的一句")])
    step_import.record_import(ctx4.state, ctx4.clip_dir, push, source="hand", authority="U130")
    out4 = si.step3_subtitles(ctx4)
    assert not out4.ok and out4.reason == "TRANSCRIPT_CUT_MISMATCH"

    # 5) Step 8 guard: planted stale asr/bcut.srt (and separately subs/final.srt) stops with TRANSCRIPT_CUT_MISMATCH
    for variant, bcut_end, final_end in (("stale-bcut", 65.0, 10.0), ("stale-final", 10.0, 65.0)):
        ctx8 = make_ctx(tmp_path / f"step8-{variant}", remote=FakeRemote(), cpa=FakeCpa([]))
        c = ctx8.clip_dir
        for rel, body in (
            ("title/title.txt", "【李豆沙】标题\n"),
            ("title/candidates.json", json.dumps({"cover_lines": ["甲", "乙"]})),
            ("cover/cover.png", "png"),
            ("speaker/speaker.srt", "1\n00:00:00,000 --> 00:00:10,000\n[李豆沙] 句\n"),
            ("tags/tags.json", "{}"),
            ("burn/intro_result.json", json.dumps({"status": "PREPENDED", "intro_id": "Z1", "intro_offset_ms": 0})),
            ("dedup/result.json", json.dumps({"decision": "UNKNOWN", "block_upload": False})),
            ("subs/doubts.json", "[]"),
        ):
            (c / rel).parent.mkdir(parents=True, exist_ok=True)
            (c / rel).write_text(body, encoding="utf-8")
        accept_title(c, "【李豆沙】标题")
        _write_probe(c, 60.0)
        (c / "asr").mkdir(parents=True, exist_ok=True)
        (c / "asr/bcut.srt").write_text(f"1\n00:00:00,000 --> {si.fmt_ts(bcut_end)}\n听写\n", encoding="utf-8")
        (c / "subs").mkdir(parents=True, exist_ok=True)
        (c / "subs/final.srt").write_text(f"1\n00:00:00,000 --> {si.fmt_ts(final_end)}\n正片\n", encoding="utf-8")
        ctx8.adapters.remote.files[f"{RD}/burn/final.mp4"] = b"mp4"
        ctx8.adapters.remote.files[f"{RD}/burn/final.ass"] = b"ass"
        ctx8.adapters.remote.files[f"{RD}/cut/cut.mp4"] = b"cut"
        out8 = sp.step8_package(ctx8)
        assert not out8.ok and out8.reason == "TRANSCRIPT_CUT_MISMATCH"
        assert not (c / "pkg").exists()


def test_import_after_recut_stops_with_import_cut_mismatch_and_fresh_import_passes(tmp_path):
    # 1) Normal import records clip interval; redo + recut -> IMPORT_CUT_MISMATCH; fresh import -> passes
    ctx = make_ctx(tmp_path / "normal", remote=FakeRemote(), cpa=FakeCpa([]))
    push1 = push_dir(tmp_path / "push1", ctx.state.data["clip"], [(0.0, 4.0, "李豆沙", "第一版字幕")])
    rec = step_import.record_import(ctx.state, ctx.clip_dir, push1, source="hand", authority="U130")
    assert rec["cut_start_ms"] == 10_000 and rec["cut_end_ms"] == 80_000

    redo.redo_clip(task=tmp_path / "normal", cid="clip", start="1", adapters_for=lambda _rd: ctx.adapters)
    ctx.state.data["clip"].update(start_ms=28_000, end_ms=80_000)
    ctx.state.data.pop("recut_pending", None)  # simulate step 2 finishing on the new boundary
    ctx.state.save()
    _write_probe(ctx.clip_dir, 52.0)

    stopped = si.step3_subtitles(ctx)
    assert not stopped.ok and stopped.reason == "IMPORT_CUT_MISMATCH"

    # Fresh explicit import on the new cut (without boundary/decision.json) records new interval and passes
    push2 = push_dir(tmp_path / "push2", ctx.state.data["clip"], [(0.0, 4.0, "李豆沙", "第二版字幕")])
    (push2 / "boundary/decision.json").unlink()
    rec2 = step_import.record_import(ctx.state, ctx.clip_dir, push2, source="hand", authority="U130")
    assert rec2["cut_start_ms"] == 28_000 and rec2["cut_end_ms"] == 80_000
    passed = si.step3_subtitles(ctx)
    assert passed.ok, (passed.reason, passed.note)
    assert "第二版字幕" in (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")

    # 2) Legacy import (no cut_start_ms/cut_end_ms)
    # 2a) Unchanged legacy import with no recut_pending -> passes
    ctx_leg = make_ctx(tmp_path / "legacy-ok", remote=FakeRemote(), cpa=FakeCpa([]))
    push_leg = push_dir(tmp_path / "push-leg", ctx_leg.state.data["clip"], [(0.0, 4.0, "李豆沙", "旧导入")])
    step_import.record_import(ctx_leg.state, ctx_leg.clip_dir, push_leg, source="hand", authority="U130")
    ctx_leg.state.data["imported"].pop("cut_start_ms", None)
    ctx_leg.state.data["imported"].pop("cut_end_ms", None)
    ctx_leg.state.save()
    assert si.step3_subtitles(ctx_leg).ok

    # 2b) Legacy import with recut_pending set -> stops with IMPORT_CUT_MISMATCH
    ctx_leg.state.data["recut_pending"] = {"cleaned": True}
    ctx_leg.state.save()
    out_pending = si.step3_subtitles(ctx_leg)
    assert not out_pending.ok and out_pending.reason == "IMPORT_CUT_MISMATCH"

    # 2c) Legacy import after redo from cut + boundary change + step 2 clearing recut_pending -> still IMPORT_CUT_MISMATCH
    ctx_leg2 = make_ctx(tmp_path / "legacy-recut", remote=FakeRemote(), cpa=FakeCpa([]))
    push_leg2 = push_dir(tmp_path / "push-leg2", ctx_leg2.state.data["clip"], [(0.0, 4.0, "李豆沙", "旧导入")])
    step_import.record_import(ctx_leg2.state, ctx_leg2.clip_dir, push_leg2, source="hand", authority="U130")
    ctx_leg2.state.data["imported"].pop("cut_start_ms", None)
    ctx_leg2.state.data["imported"].pop("cut_end_ms", None)
    ctx_leg2.state.save()
    redo.redo_clip(task=tmp_path / "legacy-recut", cid="clip", start="1", adapters_for=lambda _rd: ctx_leg2.adapters)
    ctx_leg2.state = ClipState.load(ctx_leg2.state.path)
    ctx_leg2.state.data["clip"].update(start_ms=28_000, end_ms=80_000)
    ctx_leg2.state.data.pop("recut_pending", None)
    ctx_leg2.state.save()
    out_leg_after = si.step3_subtitles(ctx_leg2)
    assert not out_leg_after.ok and out_leg_after.reason == "IMPORT_CUT_MISMATCH"


@pytest.mark.parametrize(
    ("cid", "clip_cid", "remote_dir"),
    [
        ("clip ", "clip ", "/r/clip "),
        ("clip", "clip ", "/r/clip"),
        ("..", "..", "/r/.."),
        (".", ".", "/r/."),
        ("clip\n", "clip\n", "/r/clip\n"),
        ("clip", "clip", "/r/other"),
        ("clip", "clip", "/r/clip/"),
        ("clip", "clip", "/r/clip "),
        ("clip", "clip", "/r/../clip"),
        ("clip", "clip", "/r/clip/sub"),
        ("clip", "clip", "/"),
        ("clip", "clip", "clip"),
    ],
)
def test_redo_refuses_invalid_cid_or_mismatched_remote_dir_and_removes_nothing(
    tmp_path, cid, clip_cid, remote_dir
):
    task = tmp_path / "bad"
    remote = FakeRemote()
    # Build valid clip first, then mutate state path/fields to the invalid case
    seed = make_ctx(task, remote=remote, cpa=FakeCpa([]))
    seed_data = json.loads(seed.state.path.read_text(encoding="utf-8"))
    seed_data["clip"]["cid"] = clip_cid
    seed_data["clip"]["remote_dir"] = remote_dir
    state_path = task / "state" / f"{cid}.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(seed_data, ensure_ascii=False, indent=2), encoding="utf-8")

    # Plant local files under task/clips/<cid>/asr (unless cid is "." or "..") and remote files
    local_sentinel: Path | None = None
    if cid not in (".", ".."):
        local_sentinel = task / "clips" / cid / "asr/keep.srt"
        local_sentinel.parent.mkdir(parents=True, exist_ok=True)
        local_sentinel.write_text("keep", encoding="utf-8")
    remote.files[f"{remote_dir}/asr/keep.srt"] = b"keep"
    state_before = state_path.read_bytes()

    with pytest.raises(StateError):
        redo.redo_clip(task=task, cid=cid, start="1", adapters_for=lambda _rd: Adapters(remote=remote))

    assert remote.scripts == []
    assert f"{remote_dir}/asr/keep.srt" in remote.files
    if local_sentinel is not None:
        assert local_sentinel.is_file()
        assert not (task / "clips" / cid / "redo").exists()
    assert state_path.read_bytes() == state_before
    assert "recut_pending" not in json.loads(state_path.read_text(encoding="utf-8"))
    # Ensure shlex is imported/used cleanly if needed
    assert shlex.quote("clip") == "clip"


def test_failed_redo_cleanup_never_reuses_old_subtitles_and_step2_finishes_cleanup(tmp_path):
    task = tmp_path / "failed-cleanup"
    cut_calls: list[str] = []
    asr_calls: list[str] = []
    cut_durations = [70.0, 60.0]
    asr_batches = [("旧句", 35), ("新句", 30)]
    fail_cleanup = False

    def on_script(name: str, body: str, remote: FakeRemote) -> RemoteResult:
        if name == "s0-dedupe":
            remote.files[f"{RD}/dedup/result.json"] = json.dumps(
                {"decision": "UNKNOWN", "block_upload": False}
            ).encode()
        elif name == "s2-cut":
            cut_calls.append(name)
            dur = cut_durations.pop(0)
            remote.files[f"{RD}/cut/probe.json"] = json.dumps(
                {
                    "format": {"duration": f"{dur:.3f}"},
                    "streams": [{"codec_type": "video", "width": 1920, "height": 1080}, {"codec_type": "audio"}],
                }
            ).encode()
            remote.files[f"{RD}/cut/cut.mp4"] = f"cut-{dur}".encode()
        elif name == "s3-asr":
            asr_calls.append(body)
            prefix, count = asr_batches.pop(0)
            srt_bytes = _srt_text(prefix, count, step=2.0).encode("utf-8")
            remote.files[f"{RD}/asr/bcut.srt"] = srt_bytes
            remote.files[f"{RD}/asr/jianying.srt"] = srt_bytes
            return RemoteResult(0, "ASR_OK\n", "")
        elif name.startswith("s3-listen"):
            remote.files[f"{RD}/listen/out/all.json"] = b"[]"
            return RemoteResult(0, "LISTEN_DONE\n", "")
        elif name == "redo-clean-remote":
            if fail_cleanup:
                return RemoteResult(1, "", "disk error")
            removed = []
            for d in ("asr", "listen", "witness", "speaker", "burn", "cover", "tags", "fmr", "pkg"):
                prefix = f"{RD}/{d}/"
                for k in [key for key in remote.files if key.startswith(prefix)]:
                    del remote.files[k]
                removed.append(d)
            return RemoteResult(0, "".join(f"REMOVED {d}\n" for d in removed), "")
        return RemoteResult(0, "", "")

    remote = FakeRemote(on_script=on_script)
    cpa = _Step1To3Cpa([json.dumps({"verdict": "ACCEPT", "start_cue": 6, "end_cue": 40, "reason": "第一轮"})])
    ctx = make_ctx(task, remote=remote, cpa=cpa)
    steps_1_to_3 = [s for s in STEPS if s.number in (1, 0, 2, 3)]

    first = run_clip(ctx, steps_1_to_3)
    assert first.stopped_at is None, (first.stopped_at, first.reason)
    assert first.passed == [1, 0, 2, 3]
    assert "旧句1" in (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")

    # Simulate boundary updated before redo from cut, and cleanup fails during redo.
    ctx.state.data["clip"].update(start_ms=20_000, end_ms=80_000)
    ctx.state.save()
    fail_cleanup = True
    with pytest.raises(StateError, match="remote recut cleanup failed"):
        redo.redo_clip(task=task, cid="clip", start="2", adapters_for=lambda _rd: Adapters(remote=remote))

    after_failed_redo = ClipState.load(ctx.state.path).data
    pending_after = after_failed_redo.get("recut_pending")
    assert isinstance(pending_after, dict)
    assert pending_after.get("cleaned") is False and pending_after.get("rebuild") is False
    assert isinstance(pending_after.get("products"), list)
    assert "cut" not in after_failed_redo["steps"]
    assert "subtitles" not in after_failed_redo["steps"]

    # Ordinary run while cleanup still fails must stop at step 2 without cutting or reusing step 3.
    retry_state = ClipState.load(ctx.state.path)
    ctx_retry = type(ctx)(
        state=retry_state,
        session=retry_state.data["session"],
        clip_dir=ctx.clip_dir,
        remote_dir=ctx.remote_dir,
        adapters=ctx.adapters,
    )
    blocked = run_clip(ctx_retry, steps_1_to_3)
    assert blocked.stopped_at == 2
    assert blocked.reason == "RECUT_CLEANUP_FAILED"
    assert blocked.reused == [1, 0]
    assert blocked.passed == []
    assert len(cut_calls) == 1
    pending_blocked = ClipState.load(ctx.state.path).data.get("recut_pending")
    assert isinstance(pending_blocked, dict) and pending_blocked.get("cleaned") is False

    # Once cleanup succeeds on a subsequent ordinary run, step 2 cleans then cuts, and step 3 runs fresh.
    fail_cleanup = False
    ok_state = ClipState.load(ctx.state.path)
    ctx_ok = type(ctx)(
        state=ok_state,
        session=ok_state.data["session"],
        clip_dir=ctx.clip_dir,
        remote_dir=ctx.remote_dir,
        adapters=ctx.adapters,
    )
    recovered = run_clip(ctx_ok, steps_1_to_3)
    assert recovered.stopped_at is None, (recovered.stopped_at, recovered.reason)
    assert recovered.reused == [1, 0]
    assert recovered.passed == [2, 3]
    assert len(cut_calls) == 2
    assert len(asr_calls) == 2
    final_text = (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")
    assert "新句1" in final_text and "旧句1" not in final_text
    assert "recut_pending" not in ClipState.load(ctx.state.path).data


def test_same_interval_asr_accumulates_across_rate_limited_attempts(tmp_path):
    # Test both separate remote_dir (FakeRemote) and same-dir media host (real filesystem rm -f checks)
    for mode in ("remote", "same_dir"):
        task = tmp_path / mode
        asr_calls: list[str] = []
        attempt = 0

        def on_script(name: str, body: str, remote: FakeRemote, *, _mode=mode, _task=task) -> RemoteResult:
            nonlocal attempt
            clip_dir = _task / "clips/clip"
            rd = str(clip_dir) if _mode == "same_dir" else RD
            if name == "s3-asr":
                asr_calls.append(body)
                attempt += 1
                want_line = next(line for line in body.splitlines() if line.startswith("want_stamp="))
                want_stamp = shlex.split(want_line.split("=", 1)[1])[0]
                remote_stamp_bytes = remote.files.get(f"{rd}/asr/interval.json")
                if remote_stamp_bytes != want_stamp.encode():
                    for line in body.splitlines():
                        if "rm -f " in line:
                            for tok in shlex.split(line.strip())[2:]:
                                remote.files.pop(tok, None)
                                if _mode == "same_dir":
                                    Path(tok).unlink(missing_ok=True)
                    remote.files[f"{rd}/asr/interval.json"] = want_stamp.encode()
                    if _mode == "same_dir":
                        (clip_dir / "asr").mkdir(parents=True, exist_ok=True)
                        (clip_dir / "asr/interval.json").write_text(want_stamp + "\n", encoding="utf-8")
                if attempt == 1:
                    bcut_bytes = _srt_text("冰糖", 35, step=2.0).encode("utf-8")
                    remote.files[f"{rd}/asr/bcut.srt"] = bcut_bytes
                    if _mode == "same_dir":
                        (clip_dir / "asr").mkdir(parents=True, exist_ok=True)
                        (clip_dir / "asr/bcut.srt").write_bytes(bcut_bytes)
                    return RemoteResult(0, f"ASR_STAMP {want_stamp}\nFREE_ASR bcut OK\nFREE_ASR jianying RATE_LIMITED\nASR_MISSING\n", "")
                if attempt == 2:
                    # Must only fetch the missing provider (jianying), not re-request or delete bcut
                    assert "for p in jianying;" in body
                    assert (clip_dir / "asr/bcut.srt").is_file()
                    assert f"{rd}/asr/bcut.srt" in remote.files
                    jy_bytes = _srt_text("冰糖", 35, step=2.0).encode("utf-8")
                    remote.files[f"{rd}/asr/jianying.srt"] = jy_bytes
                    if _mode == "same_dir":
                        (clip_dir / "asr/jianying.srt").write_bytes(jy_bytes)
                    return RemoteResult(0, f"ASR_STAMP {want_stamp}\nFREE_ASR jianying OK\nASR_OK\n", "")
            elif name.startswith("s3-listen"):
                remote.files[f"{rd}/listen/out/all.json"] = b"[]"
                return RemoteResult(0, "LISTEN_DONE\n", "")
            return RemoteResult(0, "", "")

        rd_arg = str(task / "clips/clip") if mode == "same_dir" else RD
        remote = FakeRemote(on_script=on_script)
        ctx = make_ctx(task, remote=remote, cpa=IdCpa(), remote_dir=rd_arg)
        (ctx.clip_dir / "cut").mkdir(parents=True, exist_ok=True)
        (ctx.clip_dir / "cut/probe.json").write_text(json.dumps({"format": {"duration": "70.000"}}), encoding="utf-8")

        out1 = si.step3_subtitles(ctx)
        assert not out1.ok and out1.reason == "ASR_MISSING_SOURCE"
        assert (ctx.clip_dir / "asr/interval.json").is_file()
        assert (ctx.clip_dir / "asr/bcut.srt").is_file()
        assert not (ctx.clip_dir / "asr/jianying.srt").exists()

        out2 = si.step3_subtitles(ctx)
        assert out2.ok, out2.reason
        assert (ctx.clip_dir / "asr/bcut.srt").is_file()
        assert (ctx.clip_dir / "asr/jianying.srt").is_file()
        assert len(asr_calls) == 2


def test_import_redo_in_fresh_subprocess_has_no_circular_import():
    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-c", "import src.autoslice.manual_lane.redo"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"rc={proc.returncode}\n{proc.stderr}"


def test_failed_redo_cleanup_invalidates_fmr_source_review_and_reviewed_tags_before_new_cut(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_fmr", _fake_fmr)
    monkeypatch.setattr(sp, "description_text", lambda t, p=None: "简介")
    monkeypatch.setattr(sp, "_finish_or_wide", lambda ctx, record, process: sp._finish(ctx, cpa=record["cpa"], process=process))
    task = tmp_path / "s1a"
    fail_cleanup = False
    cut_payloads = [b"OLD_CUT", b"NEW_CUT"]
    asr_batches = [("旧句", 35), ("新句", 30)]
    seen_at_s2_cut: list[tuple[bool, bool, bool]] = []

    def on_script(name: str, body: str, remote: FakeRemote) -> RemoteResult:
        if name == "s0-dedupe":
            remote.files[f"{RD}/dedup/result.json"] = json.dumps({"decision": "UNKNOWN", "block_upload": False}).encode()
        elif name == "s2-cut":
            clip_dir = task / "clips/clip"
            st = ClipState.load(task / "state/clip.json").data
            seen_at_s2_cut.append((
                (clip_dir / "fmr_source/cut.mp4").exists(),
                "review" in st,
                "reviewed_tags" in st,
            ))
            remote.files[f"{RD}/cut/probe.json"] = json.dumps({
                "format": {"duration": "70.000"},
                "streams": [{"codec_type": "video", "width": 1920, "height": 1080}, {"codec_type": "audio"}],
            }).encode()
            remote.files[f"{RD}/cut/cut.mp4"] = cut_payloads.pop(0)
        elif name.startswith("s3-listen"):
            remote.files[f"{RD}/listen/out/all.json"] = b"[]"
            return RemoteResult(0, "LISTEN_DONE\n", "")
        elif name == "s3-asr":
            prefix, count = asr_batches.pop(0)
            srt_bytes = _srt_text(prefix, count, step=2.0).encode("utf-8")
            remote.files[f"{RD}/asr/bcut.srt"] = srt_bytes
            remote.files[f"{RD}/asr/jianying.srt"] = srt_bytes
            return RemoteResult(0, "ASR_OK\n", "")
        elif name == "s4-speaker-v8":
            remote.files[f"{si.SPEAKER_V8_ROOT}/clip/out/pred.json"] = json.dumps({"predictions": []}).encode()
            return RemoteResult(0, "SPEAKER_V8_DONE\n", "")
        elif name == "s5-burn":
            remote.files[f"{RD}/burn/intro_result.json"] = json.dumps(
                {"status": "PREPENDED", "intro_id": "Z1", "intro_offset_ms": 0}).encode()
            remote.files[f"{RD}/burn/final.mp4"] = b"mp4"
            remote.files[f"{RD}/burn/final.ass"] = b"ass"
        elif name.startswith("s6-cover-job"):
            fake_cover(remote, RD)
        elif name == "s7-tags":
            remote.files[f"{RD}/tags/tags.json"] = json.dumps(
                {"status": "OK", "final_tags": ["李豆沙", "切片"], "input_binding": {"title": "x"}},
                ensure_ascii=False).encode()
        elif name == "redo-clean-remote":
            if fail_cleanup:
                return RemoteResult(1, "", "disk error")
            removed = []
            for d in ("asr", "listen", "witness", "speaker", "burn", "cover", "tags", "fmr", "pkg"):
                prefix = f"{RD}/{d}/"
                for k in [key for key in remote.files if key.startswith(prefix)]:
                    del remote.files[k]
                removed.append(d)
            return RemoteResult(0, "".join(f"REMOVED {d}\n" for d in removed), "")
        return RemoteResult(0, "", "")

    title_json = json.dumps({
        "candidates": [{"title": "【李豆沙】半分钟后她破防了", "on_stage": ["李豆沙"], "punchline_person": "她破防了",
                        "swap_check": {"holds_with_other_host": False, "reason": "测试"}}],
        "recommended": 0, "cover_lines": ["破防了", "半分钟"],
    }, ensure_ascii=False)
    cpa = ScriptedCpa({
        "boundary": json.dumps({"verdict": "ACCEPT", "start_cue": 6, "end_cue": 40, "reason": "完整"}),
        "title": [title_json, title_json],
        "review": [
            json.dumps({"verdict": "PASS", "findings": [], "review_md": "复审第一轮"}, ensure_ascii=False),
            json.dumps({"verdict": "PASS", "findings": [], "review_md": "复审第二轮"}, ensure_ascii=False),
        ],
    })
    ctx = make_ctx(task, remote=FakeRemote(on_script=on_script), cpa=cpa)
    first = run_clip(ctx, STEPS)
    assert first.stopped_at is None, (first.stopped_at, first.reason)
    assert first.passed == [1, 0, 2, 3, 4, 5, 6, 7, 8, 9]
    assert (ctx.clip_dir / "fmr_source/cut.mp4").read_bytes() == b"OLD_CUT"
    st1 = ClipState.load(ctx.state.path)
    st1.data["reviewed_tags"] = {"kept": ["旧标签"], "removed": ["切片"], "proper_noun_tags": []}
    st1.save()

    fail_cleanup = True
    with pytest.raises(StateError, match="remote recut cleanup failed"):
        redo.redo_clip(task=task, cid="clip", start="2", adapters_for=lambda _rd: ctx.adapters)

    fail_cleanup = False
    state2 = ClipState.load(ctx.state.path)
    ctx2 = type(ctx)(state=state2, session=state2.data["session"], clip_dir=ctx.clip_dir,
                     remote_dir=ctx.remote_dir, adapters=ctx.adapters)
    second = run_clip(ctx2, STEPS)
    assert second.stopped_at is None, (second.stopped_at, second.reason)
    assert second.passed == [2, 3, 4, 5, 6, 7, 8, 9]
    # Before s2-cut executes, local fmr_source, review state, and reviewed_tags must already be gone.
    assert seen_at_s2_cut == [(False, False, False), (False, False, False)]
    assert (ctx.clip_dir / "fmr_source/cut.mp4").read_bytes() == b"NEW_CUT"
    assert "旧标签" not in json.loads((ctx.clip_dir / "tags/tags.json").read_text(encoding="utf-8"))["final_tags"]
    assert "复审第二轮" in (ctx.clip_dir / "pkg/REVIEW.md").read_text(encoding="utf-8")
    assert "复用已记录的 CPA 复审终态" not in (ctx.clip_dir / "PROCESS.md").read_text(encoding="utf-8")
    assert [c for c in cpa.calls if c == "review"] == ["review", "review"]


def test_failed_rebuild_in_redo_is_retried_by_step2_and_never_cuts_old_source(tmp_path):
    task = tmp_path / "s1b"
    fail_rebuild = True
    cut_sources: list[bytes] = []
    rebuild_calls = 0

    def on_script(name: str, body: str, remote: FakeRemote) -> RemoteResult:
        nonlocal rebuild_calls
        if name == "source-exists":
            return RemoteResult(0, "", "")
        if name == "compile-cut":
            p1_dur = "30.000" if fail_rebuild else "40.000"
            streams = '[{"codec_type":"video","codec_name":"h264","width":1920,"height":1080,"pix_fmt":"yuv420p","r_frame_rate":"30/1"},{"codec_type":"audio","codec_name":"aac","sample_rate":"48000","channels":2}]'
            return RemoteResult(
                0,
                f'PROBE 0 {{"format":{{"duration":"30.000"}},"streams":{streams}}}\n'
                f'PROBE 1 {{"format":{{"duration":"{p1_dur}"}},"streams":{streams}}}\n',
                "",
            )
        if name == "compile-concat":
            rebuild_calls += 1
            dur = "60.000" if fail_rebuild else "70.000"
            if not fail_rebuild:
                remote.files["/r/sources/compilation-clip.flv.redo"] = b"NEW_JOINED"
            return RemoteResult(0, f'PROBE final {{"format": {{"duration": "{dur}"}}}}\n', "")
        if name == "redo-replace-source":
            remote.files["/r/sources/compilation-clip.flv"] = remote.files.pop("/r/sources/compilation-clip.flv.redo")
            return RemoteResult(0, "", "")
        if name == "redo-clean-remote":
            return RemoteResult(0, "REMOVED asr\n", "")
        if name == "s2-cut":
            cut_sources.append(remote.files["/r/sources/compilation-clip.flv"])
            remote.files[f"{RD}/cut/probe.json"] = json.dumps({
                "format": {"duration": "70.000"},
                "streams": [{"codec_type": "video", "width": 1920, "height": 1080}, {"codec_type": "audio"}],
            }).encode()
            remote.files[f"{RD}/cut/cut.mp4"] = b"cut"
            return RemoteResult(0, "", "")
        return RemoteResult(0, "", "")

    remote = FakeRemote(on_script=on_script)
    remote.files["/r/sources/compilation-clip.flv"] = b"OLD_JOINED"
    ctx = make_ctx(task, remote=remote, cpa=FakeCpa([]))
    ctx.state.data["clip"]["source_name"] = "compilation-clip.flv"
    ctx.state.save()
    manifest = {
        "schema": "manual-lane.compilation.v1",
        "cid": "clip",
        "theme": "t",
        "hook": "h",
        "reason": "r",
        "virtual_source": "compilation-clip.flv",
        "virtual_path": "/r/sources/compilation-clip.flv",
        "duration_ms": 70000,
        "recordings": [
            {"segment": 1, "name": "rec-a.flv", "path": "/r/a/rec-a.flv", "bcut_srt": "/tmp/a.srt", "bcut_source_zero_ms": 0},
            {"segment": 2, "name": "rec-b.flv", "path": "/r/b/rec-b.flv", "bcut_srt": "/tmp/b.srt", "bcut_source_zero_ms": 0},
        ],
        "pieces": [
            {"index": 0, "segment": 1, "recording": "rec-a.flv", "start_cue": 1, "end_cue": 15, "why": "",
             "source_start_ms": 0, "source_end_ms": 30000, "measured_ms": 30000, "compiled_start_ms": 0, "compiled_end_ms": 30000},
            {"index": 1, "segment": 2, "recording": "rec-b.flv", "start_cue": 1, "end_cue": 20, "why": "",
             "source_start_ms": 0, "source_end_ms": 40000, "measured_ms": 40000, "compiled_start_ms": 30000, "compiled_end_ms": 70000},
        ],
    }
    ctx.clip_dir.mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "compilation.json").write_text(json.dumps(manifest), encoding="utf-8")
    for key, rel in (("boundary", "boundary/decision.json"), ("dedupe", "dedup/result.json"), ("cut", "cut/probe.json")):
        p = ctx.clip_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}", encoding="utf-8")
        ctx.state.record(key, ok=True, reason="", products={key: str(p)}, cpa=[], note=key)
    ctx.state.save()

    with pytest.raises(compilation.CompilationError) as exc_info:
        redo.redo_clip(task=task, cid="clip", start="2", adapters_for=lambda _rd: ctx.adapters)
    assert exc_info.value.code == "COMPILATION_REBUILD_DURATION"
    assert cut_sources == []

    steps_0_to_2 = [s for s in STEPS if s.number in (1, 0, 2)]
    state_mid = ClipState.load(ctx.state.path)
    ctx_fail = type(ctx)(state=state_mid, session=state_mid.data["session"], clip_dir=ctx.clip_dir,
                         remote_dir=ctx.remote_dir, adapters=ctx.adapters)
    blocked = run_clip(ctx_fail, steps_0_to_2)
    assert blocked.stopped_at == 2
    assert blocked.reason == "COMPILATION_REBUILD_DURATION"
    assert cut_sources == []
    assert rebuild_calls == 2

    fail_rebuild = False
    state_retry = ClipState.load(ctx.state.path)
    ctx_ok = type(ctx)(state=state_retry, session=state_retry.data["session"], clip_dir=ctx.clip_dir,
                       remote_dir=ctx.remote_dir, adapters=ctx.adapters)
    recovered = run_clip(ctx_ok, steps_0_to_2)
    assert recovered.stopped_at is None, (recovered.stopped_at, recovered.reason)
    assert recovered.passed == [2]
    assert cut_sources == [b"NEW_JOINED"]
    assert rebuild_calls == 3
    assert "recut_pending" not in ClipState.load(ctx.state.path).data


def _setup_real_shell_asr_harness(tmp_path: Path, monkeypatch, *, remote_root: Path):
    """Real `LocalRemote` shell execution for `s3-asr` with a tiny local `free_asr_paced.py` stub."""
    control = tmp_path / "asr_control.json"
    calls_log = tmp_path / "asr_calls.json"
    calls_log.write_text("[]", encoding="utf-8")
    tool = tmp_path / "fake_free_asr_paced.py"
    tool.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "asr_dir = Path(sys.argv[2])\n"
        "providers = sys.argv[8:]\n"
        "log_p = Path(" + repr(str(calls_log)) + ")\n"
        "calls = json.loads(log_p.read_text(encoding='utf-8'))\n"
        "calls.append(providers)\n"
        "log_p.write_text(json.dumps(calls), encoding='utf-8')\n"
        "cfg = json.loads(Path(" + repr(str(control)) + ").read_text(encoding='utf-8'))\n"
        "asr_dir.mkdir(parents=True, exist_ok=True)\n"
        "for p in providers:\n"
        "    spec = cfg[p]\n"
        "    status = spec['status']\n"
        "    if 'srt' in spec:\n"
        "        (asr_dir / f'{p}.srt').write_text(spec['srt'], encoding='utf-8')\n"
        "    print(f'FREE_ASR {p} {status}')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(step3_subtitles, "FREE_ASR_TOOL", tool)
    monkeypatch.setattr(step3_subtitles, "remote_python", lambda args, **_kw: f"{shlex.quote(sys.executable)} -B {args}")
    fail_put = {"flag": False}

    class _HarnessRemote(LocalRemote):
        def put(self, local: Path, remote_path: str) -> None:
            if fail_put["flag"] and remote_path.endswith("free_asr_paced.py"):
                raise RuntimeError("upload failed")
            super().put(local, remote_path)

        def run_script(self, *, name: str, body: str, timeout: int) -> RemoteResult:
            if name.startswith("s3-listen"):
                out = remote_root / "listen/out/all.json"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"[]")
                return RemoteResult(0, "LISTEN_DONE\n", "")
            return super().run_script(name=name, body=body, timeout=timeout)

    return _HarnessRemote(scripts_dir=str(tmp_path / "scripts")), control, calls_log, fail_put


def test_asr_remote_stamp_prevents_stale_transcript_after_upload_failure(tmp_path, monkeypatch):
    remote_root = tmp_path / "remote/clip"
    remote, control, calls_log, fail_put = _setup_real_shell_asr_harness(tmp_path, monkeypatch, remote_root=remote_root)
    ctx = make_ctx(tmp_path / "task", remote=remote, cpa=IdCpa(), remote_dir=str(remote_root))
    ctx.state.data["clip"].update(start_ms=20_000, end_ms=80_000)
    ctx.state.save()
    (ctx.clip_dir / "cut").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "cut/probe.json").write_text(json.dumps({"format": {"duration": "60.000"}}), encoding="utf-8")

    # Seed old remote and local files + old stamp for 10_000..80_000 on the same clip id
    old_stamp = json.dumps({"source_name": "src.flv", "start_ms": 10_000, "end_ms": 80_000}, sort_keys=True)
    old_bcut = _srt_text("旧必剪", 2, step=1.0)
    old_jy = _srt_text("旧剪映", 2, step=1.0)
    (remote_root / "asr").mkdir(parents=True, exist_ok=True)
    (remote_root / "asr/interval.json").write_text(old_stamp, encoding="utf-8")
    (remote_root / "asr/bcut.srt").write_text(old_bcut, encoding="utf-8")
    (remote_root / "asr/jianying.srt").write_text(old_jy, encoding="utf-8")
    (ctx.clip_dir / "asr").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "asr/interval.json").write_text(old_stamp, encoding="utf-8")
    (ctx.clip_dir / "asr/bcut.srt").write_text(old_bcut, encoding="utf-8")
    (ctx.clip_dir / "asr/jianying.srt").write_text(old_jy, encoding="utf-8")

    # Attempt 1 fails on tool upload before remote script ever runs
    fail_put["flag"] = True
    with pytest.raises(RuntimeError, match="upload failed"):
        si.step3_subtitles(ctx)
    assert not (ctx.clip_dir / "asr/interval.json").exists()

    # Attempt 2: bcut succeeds, jianying is rate-limited; old remote jianying MUST have been deleted!
    fail_put["flag"] = False
    new_bcut = _srt_text("新必剪", 30, step=2.0)
    control.write_text(json.dumps({
        "bcut": {"status": "OK", "srt": new_bcut},
        "jianying": {"status": "RATE_LIMITED"},
    }), encoding="utf-8")
    out2 = si.step3_subtitles(ctx)
    assert not out2.ok and out2.reason == "ASR_MISSING_SOURCE"
    assert not (remote_root / "asr/jianying.srt").exists()
    assert not (ctx.clip_dir / "asr/jianying.srt").exists()
    assert "新必剪" in (ctx.clip_dir / "asr/bcut.srt").read_text(encoding="utf-8")

    # Attempt 3: bcut would fail/rate-limit, jianying succeeds -> step 3 reuses bcut and finishes with new transcripts
    new_jy = _srt_text("新必剪", 30, step=2.0)
    control.write_text(json.dumps({
        "bcut": {"status": "RATE_LIMITED"},
        "jianying": {"status": "OK", "srt": new_jy},
    }), encoding="utf-8")
    out3 = si.step3_subtitles(ctx)
    assert out3.ok, (out3.reason, out3.note)
    calls = json.loads(calls_log.read_text(encoding="utf-8"))
    assert calls[-1] == ["jianying"]
    final_txt = (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")
    assert "新必剪1" in final_txt and "旧剪映" not in final_txt


def test_empty_zero_byte_asr_transcript_is_not_treated_as_present(tmp_path, monkeypatch):
    remote_root = tmp_path / "remote/clip"
    remote, control, calls_log, _fail_put = _setup_real_shell_asr_harness(tmp_path, monkeypatch, remote_root=remote_root)
    ctx = make_ctx(tmp_path / "task", remote=remote, cpa=IdCpa(), remote_dir=str(remote_root))
    (ctx.clip_dir / "cut").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "cut/probe.json").write_text(json.dumps({"format": {"duration": "70.000"}}), encoding="utf-8")

    # Attempt 1: bcut answers OK with a 0-byte SRT while jianying is rate-limited
    control.write_text(json.dumps({
        "bcut": {"status": "OK", "srt": ""},
        "jianying": {"status": "RATE_LIMITED"},
    }), encoding="utf-8")
    out1 = si.step3_subtitles(ctx)
    assert not out1.ok and out1.reason == "ASR_MISSING_SOURCE"

    # Attempt 2: both must be asked again because 0-byte bcut is not a valid result
    nonempty = _srt_text("非空句", 35, step=2.0)
    control.write_text(json.dumps({
        "bcut": {"status": "OK", "srt": nonempty},
        "jianying": {"status": "OK", "srt": nonempty},
    }), encoding="utf-8")
    out2 = si.step3_subtitles(ctx)
    assert out2.ok, (out2.reason, out2.note)
    calls = json.loads(calls_log.read_text(encoding="utf-8"))
    assert calls[-1] == ["bcut", "jianying"]
    assert "非空句1" in (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")


def test_standalone_imports_in_fresh_subprocesses_have_no_circular_import():
    repo_root = Path(__file__).resolve().parents[2]
    for expr in (
        "import src.autoslice.manual_lane.redo",
        "import src.autoslice.manual_lane.step3_subtitles",
        "import src.autoslice.manual_lane.discover_media; from src.autoslice.manual_lane.step3_subtitles import FREE_ASR_TOOL, free_asr_body",
    ):
        proc = subprocess.run(
            [sys.executable, "-c", expr],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"{expr!r} failed rc={proc.returncode}\n{proc.stderr}"


def test_a_residue_directory_that_cannot_be_removed_stops_the_redo(tmp_path, monkeypatch):
    """The package step finds `fmr_source/` and reuses it; until now a failed removal was ignored and the re-cut was
    marked clean, so the new cut was reviewed against the old source."""
    import shutil as _shutil

    from src.autoslice.manual_lane import redo as redo_mod
    from src.autoslice.manual_lane.stages import STEPS
    from src.autoslice.manual_lane.state import StateError

    clip_dir = tmp_path / "clip"
    (clip_dir / "fmr_source").mkdir(parents=True)
    (clip_dir / "fmr_source/cut.mp4").write_bytes(b"OLD_CUT")

    def refuse(path, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(_shutil, "rmtree", refuse)
    package = [step for step in STEPS if step.key == "package"]
    with pytest.raises(StateError, match="cannot remove"):
        redo_mod._delete_products([], package, clip_dir)
    assert (clip_dir / "fmr_source/cut.mp4").read_bytes() == b"OLD_CUT"
    monkeypatch.undo()
    assert redo_mod._delete_products([], package, clip_dir) == 0 and not (clip_dir / "fmr_source").exists()
    assert redo_mod._delete_products([], package, clip_dir) == 0            # nothing there: nothing to do
