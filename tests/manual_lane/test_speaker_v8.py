"""Linked-clip speakers from the frozen v8 classifier (TASK-5 item 8): the rule is the classifier's own (top class
李豆沙 with p >= 0.6 is the host; anything else, including no usable unit, is the guest colour).  No truth label is
read by the pipeline."""

from __future__ import annotations

import json

from fakes import SESSION, FakeRemote, IdCpa, make_ctx, screen_remote, srt

from src.autoslice.manual_lane import speaker_v8 as sv
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import RemoteResult

RD = "/r/clip"
WORKDIR = f"{si.SPEAKER_V8_ROOT}/clip"   # the layout's per-clip v8 workspace (make_ctx: cid "clip")


def _row(uid, host, guest=0.0, ok=True):
    return {"id": uid, "ok": ok, "probs": {"李豆沙": host, "礼墨Sumi": 0.0, "安晚awa": guest}}


def test_a_unit_is_the_host_only_when_the_top_class_is_the_host_at_the_v8_mid_confidence():
    d = sv.decide_units({"a": _row("a", 0.9), "b": _row("b", 0.55, 0.4), "c": _row("c", 0.05, 0.9),
                         "e": _row("e", 0.9, ok=False)})
    assert d["a"]["host"] is True and d["b"]["host"] is False       # 0.55 is below the v8 mid tier
    assert d["c"]["host"] is False and "e" not in d                 # other class on top; failed embedding ignored


def test_a_cue_takes_the_overlap_weighted_host_probability_and_no_unit_means_guest():
    units = [{"id": "u1", "start_ms": 0, "end_ms": 1000}, {"id": "u2", "start_ms": 1000, "end_ms": 2000}]
    preds = {"u1": _row("u1", 0.95), "u2": _row("u2", 0.05, 0.9)}
    cues = [{"start_ms": 0, "end_ms": 1000}, {"start_ms": 1000, "end_ms": 2000}, {"start_ms": 5000, "end_ms": 6000}]
    out = sv.cue_speakers(cues, units, preds)
    assert [c["speaker"] for c in out] == [sv.HOST, sv.GUEST_LABEL, sv.GUEST_LABEL]
    assert out[2]["units"] == 0


def _linked_ctx(tmp_path, pred_rows, *, remote_ok=True):
    def script(name, body, remote):
        if name == "s4-speaker-v8":
            return RemoteResult(0, "SPEAKER_V8_DONE\n" if remote_ok else "SPEAKER_EMBED_FAILED\n", "")
        return RemoteResult(0, "", "")
    remote = FakeRemote(on_script=script)
    if pred_rows is not None:
        remote.files[f"{WORKDIR}/out/pred.json"] = json.dumps({"predictions": pred_rows}, ensure_ascii=False).encode()
    session = dict(SESSION, participants=[["李豆沙", "小李"], ["雾深Girimi", "雾深"]])
    ctx = make_ctx(tmp_path, remote=remote, cpa=IdCpa([]), session=session, bcut_text=srt(3, step=2.0))
    (ctx.clip_dir / "asr").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "asr" / "bcut.srt").write_text(srt(3, step=2.0), encoding="utf-8")
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs" / "final.srt").write_text(srt(3, step=2.0), encoding="utf-8")
    return ctx, remote


def test_a_linked_clip_writes_host_and_guest_labels_from_the_v8_pipeline(tmp_path):
    rows = [{"id": "clip-0", "ok": True, "probs": {"李豆沙": 0.95, "礼墨Sumi": 0.02, "安晚awa": 0.03}},
            {"id": "clip-2000", "ok": True, "probs": {"李豆沙": 0.10, "礼墨Sumi": 0.10, "安晚awa": 0.80}},
            {"id": "clip-4000", "ok": True, "probs": {"李豆沙": 0.97, "礼墨Sumi": 0.01, "安晚awa": 0.02}}]
    ctx, remote = _linked_ctx(tmp_path, rows)
    out = si.step4_speaker(ctx)
    assert out.ok, out.note
    labels = [ln[1:].split("]")[0] for ln in (ctx.clip_dir / "speaker" / "speaker.srt").read_text(encoding="utf-8").splitlines()
              if ln.startswith("[")]
    assert labels == [sv.HOST, sv.GUEST_LABEL, sv.HOST]
    assert "linked_v8" in out.note


def test_a_failed_v8_run_stops_the_step_instead_of_guessing(tmp_path):
    ctx, _ = _linked_ctx(tmp_path, None, remote_ok=False)
    out = si.step4_speaker(ctx)
    assert not out.ok and out.reason == "SPEAKER_V8_FAILED"


def test_a_single_host_session_keeps_the_uniform_host_path(tmp_path):
    # no video on the screen: the picture reading finds no other voices and the clip stays uniform host
    ctx = make_ctx(tmp_path, remote=screen_remote(playing=False), cpa=IdCpa([]),
                   session=dict(SESSION, participants=[["李豆沙"]]))
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs" / "final.srt").write_text(srt(2, step=2.0), encoding="utf-8")
    out = si.step4_speaker(ctx)
    assert out.ok and "uniform host" in "\n".join(out.process)
