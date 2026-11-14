"""Two people inside one sentence (U111, U129): the v8 voiceprint on ~1.2 s chunks of every cue runs before the
subtitles are decided; a cue with a confident host chunk and a confident other-speaker chunk becomes a doubt, is heard
once more with the per-speaker prompt, and CPA's other_speaker_text becomes an extra line overlapping the sentence in
time while the sentence keeps its BCUT time (COACH-ADVICE 1).  Through step 3 with fakes; synthetic texts."""

from __future__ import annotations

import json

from fakes import IdCpa, make_ctx
from test_evidence_attach import BCUT, RD, ReviewCpa, _doubts, _remote, _srt

from src.autoslice.manual_lane import speaker_v8
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import RemoteResult


def _remote_with_chunks(host_probs, listen_records_by_label=None, scripts=None):
    base = _remote()
    prior = base.on_script

    def script(name, body, remote):
        if scripts is not None:
            scripts.append(name)
        if name == "s3-speaker-chunks":
            units = json.loads(remote.files[f"{RD}/speaker/chunks-units.json"])["units"]
            preds = [{"id": u["id"], "ok": True, "probs": {"李豆沙": host_probs.get(u["id"], 0.95)}} for u in units]
            remote.files[f"{si.SPEAKER_V8_ROOT}/clip-chunks/out/pred.json"] = json.dumps({"predictions": preds}).encode()
            return RemoteResult(0, "SPEAKER_V8_DONE\n", "")
        if name.startswith("s3-listen-") and listen_records_by_label is not None:
            label = name[len("s3-listen-"):]
            remote.files[f"{RD}/listen/out/all.json"] = json.dumps(listen_records_by_label.get(label, []),
                                                                  ensure_ascii=False).encode()
            return RemoteResult(0, 'LISTEN_DONE 1 records 1 {"OK": 1}\n', "")
        return prior(name, body, remote)
    base.on_script = script
    return base


class TwoCpa(IdCpa):
    def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
        if call_id == "review-c":
            self.prompts.append(prompt_text)
            return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                    "response_text": json.dumps({"suspicious": []}), "stderr_tail": ""}
        return super().ask(call_id=call_id, prompt_text=prompt_text, model=model, effort=effort, remote_dir=remote_dir)


def test_with_the_switch_on_a_two_speaker_sentence_is_heard_per_speaker_and_split_into_two_lines(
        tmp_path, monkeypatch):
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "C")
    monkeypatch.setenv("MANUAL_LANE_TWO_SPEAKER_DOUBTS", "1")
    two = {"win": "S-C002", "model": si.M38["name"], "slug": "38m", "status": "OK",
           "lines": [{"start_s": 0.0, "text": "甲：子丑寅卯"}, {"start_s": 1.0, "text": "乙：嗯对"}]}
    scripts: list[str] = []
    cpa = TwoCpa(rows=[{"id": "d003", "state": "KEEP", "final_text": "", "confidence": "高", "affects_meaning": False,
                        "other_speaker_text": "嗯对", "basis": "分人听写"}])
    ctx = make_ctx(tmp_path, remote=_remote_with_chunks({"clip-k3-0": 0.92, "clip-k3-1": 0.08}, {"C-S": [two]}, scripts),
                   cpa=cpa, bcut_text=_srt(BCUT))
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    d3 = _doubts(ctx)[3]
    assert d3["id"] == "d003" and "声纹显示句内可能有两人说话" in d3["reasons"] and d3["speaker_note"]
    assert "s3-listen-C-S" in scripts                        # heard once more with the per-speaker prompt
    assert any("甲：子丑寅卯" in p and "speakers:38m" in p for p in cpa.prompts)
    cues = si.parse_srt((ctx.clip_dir / "subs" / "final.srt").read_text(encoding="utf-8"))
    main = next(c for c in cues if c["text"] == "子丑寅卯")
    extra = next(c for c in cues if c["text"] == "嗯对")
    assert main["start_s"] == 8.0                             # the sentence keeps its BCUT start
    # release subtitles never overlap (TASK-9): the other speaker's line follows, from its own chunk
    assert main["end_s"] == extra["start_s"] and 9.0 <= extra["start_s"] < extra["end_s"] <= 10.0


def test_a_sentence_with_one_voice_is_not_a_two_speaker_doubt(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "C")
    scripts: list[str] = []
    ctx = make_ctx(tmp_path, remote=_remote_with_chunks({}, {}, scripts), cpa=ReviewCpa(), bcut_text=_srt(BCUT))
    assert si.step3_subtitles(ctx).ok
    assert "s3-listen-C-S" not in scripts
    assert all("声纹显示句内可能有两人说话" not in d.get("reasons", []) for d in _doubts(ctx).values())


def test_mixed_needs_a_confident_chunk_on_each_side():
    chunks = [{"s": 0, "e": 1200, "host_prob": 0.9}, {"s": 1200, "e": 2400, "host_prob": 0.5}]
    assert speaker_v8.mixed_speakers(chunks) is None             # 0.5 is uncertain, not the other speaker
    chunks[1]["host_prob"] = 0.3
    m = speaker_v8.mixed_speakers(chunks)
    assert m is not None and m["other_span_s"] == [1.2, 2.4]


def test_the_other_persons_line_is_placed_in_time_order_and_the_sentence_keeps_its_time():
    bcut = [{"id": 1, "start_s": 0.0, "end_s": 2.0, "text": "现稿"}]
    first = {"cue_id": 1, "state": "KEEP", "final_text": "主句", "other_speaker_text": "插话", "other_first": True}
    cues, _ = si._final_cues(bcut, [dict(first, state="ADOPT")])
    # in time order and never overlapping (TASK-9): the two share the sentence's time by text length
    assert [c["text"] for c in cues] == ["插话", "主句"] and cues[0]["start_s"] == 0.0 and cues[1]["end_s"] == 2.0
    assert cues[0]["end_s"] == cues[1]["start_s"]
    later = dict(first, state="ADOPT", other_first=False, other_span_s=[1.2, 2.0])
    cues, _ = si._final_cues(bcut, [later])
    assert [c["text"] for c in cues] == ["主句", "插话"] and (cues[0]["start_s"], cues[0]["end_s"]) == (0.0, 1.2)
    assert (cues[1]["start_s"], cues[1]["end_s"]) == (1.2, 2.0)


def test_step4_uses_the_chunk_voiceprint_from_step3_without_another_remote_run(tmp_path):
    class NoRemote:
        def run_script(self, **_):
            raise AssertionError("step 4 must reuse the chunk predictions of step 3")

    ctx = make_ctx(tmp_path, remote=NoRemote(), cpa=IdCpa(), bcut_text=_srt(BCUT))
    (ctx.clip_dir / "asr").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "asr" / "bcut.srt").write_text(_srt(BCUT), encoding="utf-8")
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs" / "final.srt").write_text(_srt([(8.0, 10.0, "子丑寅卯"), (9.0, 10.0, "嗯对")]), encoding="utf-8")
    units = [{"id": "u0", "s": 8000, "e": 9000, "lw": 9000, "cue": 3}, {"id": "u1", "s": 9000, "e": 10000, "lw": 10000, "cue": 3}]
    (ctx.clip_dir / "speaker").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "speaker" / "chunks-units.json").write_text(json.dumps({"units": units}), encoding="utf-8")
    preds = [{"id": "u0", "ok": True, "probs": {"李豆沙": 0.95}}, {"id": "u1", "ok": True, "probs": {"李豆沙": 0.05}}]
    (ctx.clip_dir / "speaker" / "chunks-pred.json").write_text(json.dumps({"predictions": preds}), encoding="utf-8")
    out = si.step4_speaker(ctx)
    assert out.ok, out.note
    text = (ctx.clip_dir / "speaker" / "speaker.srt").read_text(encoding="utf-8")
    assert f"[{speaker_v8.GUEST_LABEL}] 嗯对" in text                        # the interjection line gets the other speaker


def test_by_default_the_voiceprint_is_recorded_but_does_not_open_a_doubt(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "C")
    monkeypatch.delenv("MANUAL_LANE_TWO_SPEAKER_DOUBTS", raising=False)
    scripts: list[str] = []
    ctx = make_ctx(tmp_path, remote=_remote_with_chunks({"clip-k3-0": 0.92, "clip-k3-1": 0.08}, {}, scripts),
                   cpa=ReviewCpa(), bcut_text=_srt(BCUT))
    out = si.step3_subtitles(ctx)
    assert out.ok and "s3-speaker-chunks" in scripts and "s3-listen-C-S" not in scripts
    assert any("句内像有两人的 1 句" in line and "两人疑点默认关闭" in line for line in out.process)
    assert _doubts(ctx)[3]["source"] == "NOT_SUSPICIOUS"
    assert (ctx.clip_dir / "speaker" / "chunks-pred.json").exists()     # kept for step 4
