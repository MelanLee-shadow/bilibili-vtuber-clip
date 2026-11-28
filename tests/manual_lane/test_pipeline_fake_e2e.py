"""End to end on fakes: every step 1-9 runs in the real order with the real step code; only ssh and CPA are faked.
This catches a step that crashes on its own inputs (a missing product, a bad argument), which unit tests of single
steps can miss.  Step 10 stays behind the upload switch."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

from fakes import fmr_check_wrote
from fakes import (COVER_BRIEF, COVER_EMOTE, JUDGE_PASS, judge_pass, UNDERSTANDING_CALLS, FakeRemote, IdCpa, fake_cover, is_judge, make_ctx,
                   serial_order, srt, understanding_answer)

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import stage_package as sp
from src.autoslice.manual_lane.adapters import RemoteResult
from src.autoslice.manual_lane.runner import run_clip
from src.autoslice.manual_lane.stages import STEPS

RD = "/r/clip"
OK = RemoteResult(0, "", "")


class ScriptedCpa:
    """Answers by call id.  Logs (call_id, prompt) under a lock and reads them back through `serial_order`:
    step 6 asks its two judge runs at once (TASK-31), so a raw append is a completion-order log and the judge runs
    could be listed as -r0-2, -r0-1 on one run and -r0-1, -r0-2 on the next.  The lane's own record list is in input
    order either way (TASK-37 measured 0/200 out of order); this fake now reads the same way, so the assertion below
    states the lane's contract and not the thread race."""

    def __init__(self, answers: dict[str, str]):
        self.answers = answers
        self._rows: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    @property
    def calls(self) -> list[str]:
        return serial_order([call_id for call_id, _prompt in self._rows])

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
        with self._lock:
            self._rows.append((call_id, prompt_text))
        key = call_id.split("-")[0]
        if call_id in ("cover-brief", "cover-emote"):
            return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                    "response_text": COVER_BRIEF if call_id == "cover-brief" else COVER_EMOTE, "stderr_tail": ""}
        if (reading := understanding_answer(call_id)) is not None:   # step 6's clip understanding (reads, merge)
            return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                    "response_text": reading, "stderr_tail": ""}
        if is_judge(call_id):   # the skill's title judge (step 6, twice; step 9 on a review title)
            return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                    "response_text": judge_pass(call_id), "stderr_tail": ""}
        if key == "adjudicate":   # every cue is a doubt now: keep each one (IdCpa answers by the ids in the prompt)
            return IdCpa().ask(call_id=call_id, prompt_text=prompt_text, model=model, effort=effort,
                               remote_dir=remote_dir)
        answer = self.answers[key]
        if isinstance(answer, list):          # a role asked several times answers in order
            answer = answer.pop(0)
        return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                "response_text": answer, "stderr_tail": ""}


def _remote(dedupe=None):
    def script(name, body, remote):
        if name == "s0-dedupe":
            remote.files[f"{RD}/dedup/result.json"] = json.dumps(dedupe or {"decision": "UNKNOWN", "block_upload": False}).encode()
        elif name == "s2-cut":
            remote.files[f"{RD}/cut/probe.json"] = json.dumps({
                "format": {"duration": "70.000"},
                "streams": [{"codec_type": "video", "width": 1920, "height": 1080},
                            {"codec_type": "audio"}]}).encode()
        elif name.startswith("s3-listen"):
            remote.files[f"{RD}/listen/out/all.json"] = b"[]"
            return RemoteResult(0, "LISTEN_DONE\n", "")
        elif name == "s3-asr":
            return RemoteResult(0, "ASR_OK\n", "")
        elif name == "s4-speaker-v8":   # linked clip: the v8 run answers with no usable prediction (all guest)
            remote.files[f"{si.SPEAKER_V8_ROOT}/clip/out/pred.json"] = json.dumps(
                {"predictions": []}).encode()
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
        return OK
    remote = FakeRemote(on_script=script)
    text = srt(35).encode()
    remote.files[f"{RD}/asr/bcut.srt"] = text
    remote.files[f"{RD}/cut/cut.mp4"] = b"cut"
    remote.files[f"{RD}/asr/jianying.srt"] = text
    return remote


def _fake_fmr(*args):
    """The lane replaced by a stub: init creates the package dir it would create, the rest succeed."""
    cmd = args[0]
    if cmd == "init":
        root = Path(args[args.index("--out-root") + 1])
        (root / "out" / "2026-10-24" / "fm-20261024-clip").mkdir(parents=True, exist_ok=True)
    fmr_check_wrote(args)
    return subprocess.CompletedProcess(args, 0, "", "")


def test_steps_1_to_9_run_in_order_on_fakes_and_step_10_stays_behind_the_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_fmr", _fake_fmr)
    monkeypatch.setattr(sp, "description_text", lambda t, p=None: "简介")
    cpa = ScriptedCpa({
        "boundary": json.dumps({"verdict": "ACCEPT", "start_cue": 6, "end_cue": 40, "reason": "完整"}),
        "title": json.dumps({"candidates": [{"title": "【李豆沙】半分钟后她破防了", "on_stage": ["李豆沙"], "punchline_person": "她破防了", "swap_check": {"holds_with_other_host": False, "reason": "测试"}}], "recommended": 0,
                             "cover_lines": ["破防了", "半分钟"]}, ensure_ascii=False),
        "review": json.dumps({"verdict": "PASS", "findings": [], "review_md": "复审通过。"}, ensure_ascii=False),
    })
    ctx = make_ctx(tmp_path, remote=_remote(), cpa=cpa)
    report = run_clip(ctx, STEPS)
    assert report.stopped_at is None, (report.stopped_at, report.reason)
    assert report.passed == [1, 0, 2, 3, 4, 5, 6, 7, 8, 9]
    assert report.halted_before_upload is True
    assert [c for c in cpa.calls if not c.startswith("adjudicate")] == ["boundary", *UNDERSTANDING_CALLS, "title", "title-judge-r0-1", "title-judge-r0-2", "title-judge-r0-q1", "title-judge-r0-q2", "cover-brief", "cover-emote", "review"]
    assert (ctx.clip_dir / "pkg" / "clip.member.json").exists()
    assert (ctx.clip_dir / "pkg" / "REVIEW.md").read_text(encoding="utf-8").startswith("复审通过。\n\n## 流水线采用记录")
    process = (ctx.clip_dir / "PROCESS.md").read_text(encoding="utf-8")
    for number in range(10):
        assert f"## 步骤 {number} ·" in process

    again = run_clip(ctx, STEPS)
    assert again.passed == [] and again.stopped_at is None and len(again.reused) == 10
    assert [c for c in cpa.calls if not c.startswith("adjudicate")] == ["boundary", *UNDERSTANDING_CALLS, "title", "title-judge-r0-1", "title-judge-r0-2", "title-judge-r0-q1", "title-judge-r0-q2", "cover-brief", "cover-emote", "review"]   # nothing re-asked


def _covering(total, covered, unmatched_seconds, *, runs=True, **video):
    """A receipt whose one part has `covered` of our `total` sentences inside its timeline runs (runs=False: found
    character by character only, at no single offset)."""
    part = {"decision": "UNKNOWN", "total_sentences": total, "covered_sentences": covered if not runs else 0,
            "unmatched_seconds": unmatched_seconds if not runs else 99.0, "timeline_runs": [{"own_start": 0.0}] if runs else [],
            "outside_sentences": [{"text": "我方多出的一句"}] * (total - covered if runs else total),
            "outside_seconds": unmatched_seconds if runs else 99.0}
    return {"schema_version": "external-duplicate-check.v2", "decision": "UNKNOWN", "block_upload": False, "videos": [{
        "bvid": "BV1other", "title": "【李豆沙】别人的切片", "uploader": "别人", "published_at": "2026-10-24T15:00:00+00:00",
        "duration_seconds": 300.0, "decision": "UNKNOWN", **video, "parts": [part]}]}


def test_step_0_stops_a_window_another_uploader_already_clipped_without_anything_new(tmp_path):
    """20-selection §2b (U432; 花礼 11-18: BV1017808426 had 28 of our 29 sentences): covered and nothing new -> not made."""
    ctx = make_ctx(tmp_path, remote=_remote(_covering(29, 28, 2.6)), cpa=ScriptedCpa({
        "boundary": json.dumps({"verdict": "ACCEPT", "start_cue": 6, "end_cue": 40, "reason": "完整"})}))
    report = run_clip(ctx, STEPS)
    assert report.stopped_at == 0 and report.reason == "COVERED_BY_EXTERNAL_CLIP"
    assert "BV1other" in (ctx.clip_dir / "PROCESS.md").read_text(encoding="utf-8")


def test_step_0_stops_a_covered_window_whose_sentences_are_found_at_no_single_offset(tmp_path):
    """N8 review: a clip played 1.4x faster has every sentence found but no timeline run; the found count still covers it."""
    ctx = make_ctx(tmp_path, remote=_remote(_covering(30, 30, 0.0, runs=False)), cpa=ScriptedCpa({
        "boundary": json.dumps({"verdict": "ACCEPT", "start_cue": 6, "end_cue": 40, "reason": "完整"})}))
    report = run_clip(ctx, STEPS)
    assert report.stopped_at == 0 and report.reason == "COVERED_BY_EXTERNAL_CLIP"


def test_step_0_stops_on_a_compared_part_from_an_older_checker(tmp_path):
    """The checker runs on the media host: a receipt whose compared part lacks the timeline counts is refused, not read
    as 'covers nothing'."""
    old = _covering(29, 28, 2.6)
    old.pop("schema_version")
    for key in ("timeline_runs", "outside_sentences", "outside_seconds"):
        old["videos"][0]["parts"][0].pop(key)
    ctx = make_ctx(tmp_path, remote=_remote(old), cpa=ScriptedCpa({
        "boundary": json.dumps({"verdict": "ACCEPT", "start_cue": 6, "end_cue": 40, "reason": "完整"})}))
    report = run_clip(ctx, STEPS)
    assert report.stopped_at == 0 and report.reason == "STEP_EXCEPTION:ValueError"


def test_step_0_records_whether_another_uploader_already_has_the_moment(tmp_path):
    """N8 (11-25): the record says 撞了 / 没撞 / 查不清 and why, not only the H62 decision."""
    from src.autoslice.manual_lane.gates import overlap_line
    largest = {"bvid": "BV1other", "title": "别人的切片", "uploader": "别人", "shared_seconds": 87.8, "own_seconds": 107.18,
               "outside_sentences": 8, "outside_seconds": 17.6}
    assert overlap_line({"overlap": {"verdict": "OVERLAP", "largest": largest}}).startswith("撞了：BV1other")
    assert overlap_line({"overlap": {"verdict": "NONE_FOUND"}}).startswith("没撞")
    line = overlap_line({"overlap": {"verdict": "UNDETERMINED", "undetermined": [{"bvid": "BV1x", "reason": "AUDIO_ASR_FAILED"}]}})
    assert line == "撞没撞查不清：BV1x：AUDIO_ASR_FAILED"


def test_a_window_with_a_more_complete_arc_or_only_our_own_upload_goes_on(tmp_path):
    for n, dedupe in enumerate((_covering(29, 26, 12.0),          # 3 sentences they do not have: a more complete arc
                                _covering(29, 28, 31.0),          # 30 s they do not have
                                _covering(29, 29, 0.0, excluded=True))):   # our own earlier upload
        ctx = make_ctx(tmp_path / str(n), remote=_remote(dedupe), cpa=ScriptedCpa({
            "boundary": json.dumps({"verdict": "ACCEPT", "start_cue": 6, "end_cue": 40, "reason": "完整"})}))
        report = run_clip(ctx, [s for s in STEPS if s.number in (1, 0)])
        assert report.stopped_at is None, (n, report.reason)
