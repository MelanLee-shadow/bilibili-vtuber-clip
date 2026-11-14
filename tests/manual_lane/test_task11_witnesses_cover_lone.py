"""TASK-11 guards (维护者 11-02, U138): the producers of step 3's ASR evidence, the cover's face measurement, and the
isolated single characters.

- MOSS reaches a doubt: the whole-clip witness uses MOSS's evidence API, whose overlapping native segments (two people
  at once) are evidence; the strict one-track API raised MOSS_OVERLAP on every clip, so MOSS never reached step 3.
- A whole-clip witness is asked once per cut: an OK result of the same audio is reused.  Its status (and a failure's
  reason) is in step 3's PROCESS for every mode, never silent.
- One engine is one family whether it heard the whole clip or only the cue's window.
- The cover's main face is measured on the picture it comes from, so the same picture gives the same box whatever the
  rendered candidate's bytes are.
- An isolated single character no same-speaker neighbour can take is not subtitled, and the scorer counts it on
  neither side (the writers' own rule on both sides).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from fakes import FakeRemote, make_ctx, srt

from src.autoslice.manual_lane import doubt_policy as dp
from src.autoslice.manual_lane import score as sc
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import RemoteResult
from src.autoslice.manual_lane.lane_rules import rules

REMOTE = Path(si.REMOTE_TOOLS)


def _load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", REMOTE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ----- the whole-clip witness producer (witness_asr.py) -------------------------------------------------------------
def test_moss_uses_its_evidence_api_and_overlapping_segments_are_evidence(tmp_path):
    wa = _load("witness_asr")
    assert wa.PROVIDERS["moss"] == ("src.autoslice.moss_transcription", "transcribe_moss_evidence")
    overlapping = {"native_segments": [{"start_ms": 0, "end_ms": 3000, "text": "我想看李豆沙演霸总", "speaker": "S01"},
                                       {"start_ms": 2500, "end_ms": 3200, "text": "啊我不能", "speaker": "S02"}],
                   "raw_response": {"usage": {"type": "duration", "input_audio_seconds": 3.2}, "duration": 3.2}}
    out = wa.record("moss", lambda audio, duration_ms: overlapping, tmp_path / "a.mp3", tmp_path, 3200, "sha-1")
    assert out["status"] == "OK" and out["segment_count"] == 2
    assert out["billed"]["usage"]["input_audio_seconds"] == 3.2          # the provider's own usage is recorded
    assert json.loads((tmp_path / "moss.json").read_text(encoding="utf-8"))["input_sha256"] == "sha-1"


def test_a_whole_clip_witness_of_the_same_audio_is_not_asked_again(tmp_path):
    wa = _load("witness_asr")
    calls = []

    def call(audio, duration_ms):
        calls.append(audio)
        return {"segments": [{"start_ms": 0, "end_ms": 1000, "text": "有"}], "raw_response": {"durationMilliseconds": 1000}}
    wa.record("mai", call, tmp_path / "a.mp3", tmp_path, 1000, "sha-1")
    wa.record("mai", call, tmp_path / "a.mp3", tmp_path, 1000, "sha-1")
    assert len(calls) == 1
    wa.record("mai", call, tmp_path / "a.mp3", tmp_path, 1000, "sha-2")    # another cut: asked
    assert len(calls) == 2


def test_step_3_names_every_whole_clip_witness_and_why_one_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "C")

    def script(name, body, remote):
        if name == "s3-asr":
            return RemoteResult(0, "ASR_OK\n", "")
        if name == "s3-witness":
            remote.files["/r/clip/witness/out/moss.json"] = json.dumps(
                {"status": "UNAVAILABLE", "reason_code": "MOSS_OVERLAP", "message": "MOSS segments overlap"}).encode()
            remote.files["/r/clip/witness/out/mai.json"] = json.dumps(
                {"status": "OK", "segment_count": 1, "segments": [{"start_ms": 0, "end_ms": 2000, "text": "句子1"}],
                 "billed": {"duration_ms": 70000}}).encode()
        return RemoteResult(0, "", "")

    class Cpa:
        def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
            return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                    "response_text": json.dumps({"suspicious": []}), "stderr_tail": ""}

    remote = FakeRemote(on_script=script)
    text = srt(3, step=2.0).encode()
    remote.files["/r/clip/asr/bcut.srt"] = remote.files["/r/clip/asr/jianying.srt"] = text
    ctx = make_ctx(tmp_path, remote=remote, cpa=Cpa(), bcut_text=srt(3, step=2.0))
    out = si.step3_subtitles(ctx)
    line = next(p for p in out.process if p.startswith("整片 ASR 证人"))
    assert "MOSS 不可用（MOSS_OVERLAP" in line and "MAI 成功 1 段" in line


def test_one_engine_is_one_family_for_the_whole_clip_and_the_window():
    assert dp.witness_family("asr:mai@win") == dp.witness_family("asr:mai") == "asr:mai"
    assert dp.witness_family("asr:moss@win") == "asr:moss"
    readings = {"bcut": "现稿", "asr:mai": "圣母在上", "asr:mai@win": "圣母在上", "jianying": "现稿"}
    by_text = {c["text"]: c for c in dp.candidates(readings, "现稿")}
    assert by_text["圣母在上"]["supporters"] == 1  # whole clip and window are the same vendor
    short = {c["text"]: c for c in dp.candidates({"asr:mai": "现文", "asr:mai@win": "现文"}, "现稿")}
    assert short["现文"]["supporters"] == 1  # same vendor still counts once for ordinary local corrections
    assert not dp.maintenance(readings, "现稿")["maintained"]


# ----- the cover's face measurement (cover_candidates.py) ----------------------------------------------------------
def test_the_same_picture_gives_the_same_face_box_whatever_the_rendered_bytes(tmp_path, monkeypatch):
    from PIL import Image

    cc = _load("cover_candidates")
    frame = tmp_path / "frame.png"
    Image.new("RGB", (1920, 1080), "white").save(frame)
    asked, seen = [], {}

    def vision(job, name, question, images, out_dir):
        # one reading per (question, picture bytes), as the real tool's cache keys it; a fresh reading would differ
        key = (question, tuple(cc.sha(p) for p, _ in images))
        if key not in seen:
            asked.append(key)
            seen[key] = (json.dumps({"host_face": [1300, 300, 1600, 560 + len(asked) * 40]}), False)
        return seen[key]

    monkeypatch.setattr(cc, "vision", vision)
    ref = {"kind": "picture", "src": str(frame)}
    spec_a = {"crop": [0, 0, 1920], "blur": [[1650, 700, 1830, 1060]]}
    spec_b = {"crop": [0, 0, 1920], "blur": [[1640, 690, 1830, 1070]]}   # another blur box: other rendered bytes
    box_a, how = cc.measured_face({}, "screenshot", spec_a, ref, str(tmp_path), None)
    box_b, _ = cc.measured_face({}, "screenshot", spec_b, ref, str(tmp_path), None)
    assert box_a == box_b == [1300, 300, 1600, 600] and "源图定位" in how
    assert len(asked) == 1                                              # the frame is read once
    assert cc.frame_face([1300, 300, 1600, 600], [960, 0, 960]) == [680, 600, 1280, 1200]   # crop geometry


def test_solo_small_face_crop_reaches_the_target_keeps_the_boxes_and_records_what_limits_it():
    fc = _load('cover_face_crop')
    cover_rules = rules('cover')
    target = round(cover_rules['face_target_frac'] * 1080)
    spec = {'crop': [0, 0, 1920], 'blur': [[0, 300, 500, 800]], 'keep': [[1050, 400, 1350, 700]]}
    face = [1100, 420, 1330, 620]
    plan = fc.plan_crop(face, 330, [[1050, 400, 1350, 700]], cover_rules)
    fc.apply_crop(spec, plan['crop'])
    measured = fc.frame_face(face, spec['crop'])
    assert plan['limited_by'] is None and measured[3] - measured[1] >= target - 1 and plan['face_px'] == measured[3] - measured[1]
    assert plan['crop'][1] <= 330                                                              # the head top is inside the crop
    assert all(0 <= b[0] < b[2] <= 1920 and 0 <= b[1] < b[3] <= 1080 for b in spec['blur'] + spec['keep'])
    assert spec['keep'][0] == fc.frame_face([1050, 400, 1350, 700], spec['crop'])
    # a face already at the target is left as it is; a head that leaves no room records the face height it really got
    assert fc.plan_crop([800, 300, 1300, 800], 200, [], cover_rules)['crop'] == [0, 0, 1920]
    tight = fc.plan_crop([800, 700, 1100, 1000], 20, [], cover_rules)
    assert tight['limited_by'] == 'REGION' and 300 < tight['face_px'] < target
    # no measured head top: the head cannot be protected, so the frame stays whole and says why
    blind = fc.plan_crop(face, None, [], cover_rules)
    assert blind['crop'] == [0, 0, 1920] and blind['limited_by'] == 'HEAD_TOP_UNMEASURED'


# ----- isolated single characters: not subtitled, not scored --------------------------------------------------------
def test_the_scorer_counts_a_character_the_writers_leave_out_on_neither_side():
    truth = [{"id": "c-1", "abs_start_ms": 0, "abs_end_ms": 1500, "text": "这样子啊，对", "speaker": "李豆沙"}]
    pipe = [{"abs_start_ms": 0, "abs_end_ms": 1000, "text": "这样子啊", "speaker": "雾深Girimi"},
            {"abs_start_ms": 1000, "abs_end_ms": 1500, "text": "对", "speaker": "李豆沙"}]
    out, rows = sc.lone_char_rule(pipe)
    assert [r["action"] for r in rows] == ["dropped"] and [p["text"] for p in out] == ["这样子啊"]
    assert sc.remove_char_at(truth, rows[0])[0]["text"] == "这样子啊，"
    keep = sc.lone_char_rule([{"abs_start_ms": 0, "abs_end_ms": 900, "text": "有", "speaker": "李豆沙"}])[0]
    assert [k["text"] for k in keep] == ["有"]                       # a standalone answer is not a lone character


def test_the_v4_score_of_a_clip_does_not_count_the_dropped_character(tmp_path, monkeypatch):
    clip = tmp_path / "c"
    (clip / "subs").mkdir(parents=True)
    (clip / "boundary").mkdir()
    (clip / "speaker").mkdir()
    (clip / "boundary/decision.json").write_text(json.dumps({"adopted": {"start_ms": 0}}), encoding="utf-8")
    (clip / "subs/final.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n这样子啊\n", encoding="utf-8")
    (clip / "speaker/speaker.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n[雾深Girimi] 这样子啊\n",
                                              encoding="utf-8")
    truth = {"c-1": {"id": "c-1", "abs_start_ms": 0, "abs_end_ms": 1000, "text": "这样子啊", "speaker": "雾深Girimi"},
             "c-2": {"id": "c-2", "abs_start_ms": 1000, "abs_end_ms": 1500, "text": "对", "speaker": "李豆沙"}}
    monkeypatch.setenv("MANUAL_LANE_SCORER", "v3")
    assert sc.score_clip("c", tmp_path, truth)["summary"]["exact"] == 1     # v3: the truth's 对 is a miss
    monkeypatch.setenv("MANUAL_LANE_SCORER", "v4")
    res = sc.score_clip("c", tmp_path, truth)
    assert res["summary"]["n"] == 1 and res["summary"]["exact"] == 1       # v4: not counted on either side


def test_once_the_speakers_are_known_a_lone_character_between_other_lines_is_not_subtitled():
    from src.autoslice.manual_lane import normalize

    cues = [{"start_s": 0.0, "end_s": 1.0, "text": "这样子啊", "speaker": "雾深Girimi"},
            {"start_s": 1.0, "end_s": 1.4, "text": "对", "speaker": "李豆沙"},          # step 4 labelled it the host
            {"start_s": 1.4, "end_s": 3.0, "text": "你是侄女吗", "speaker": "雾深Girimi"}]
    out, rows = normalize.join_lone_chars(cues)
    assert [c["text"] for c in out] == ["这样子啊", "你是侄女吗"]
    assert [(r["action"], r["text"]) for r in rows] == [("dropped", "对")]
    unlabelled = [{k: v for k, v in c.items() if k != "speaker"} for c in cues]
    assert normalize.join_lone_chars(unlabelled)[0] == unlabelled      # before step 4: nothing is dropped


# ----- the escalation ladder with window ASR or text first (MANUAL_LANE_LADDER) -----------------------------------
def _ladder_ctx(tmp_path, monkeypatch, ladder, rows):
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "C")
    monkeypatch.setenv("MANUAL_LANE_LADDER", ladder)
    calls: list[str] = []

    def script(name, body, remote):
        calls.append(name)
        if name == "s3-asr":
            return RemoteResult(0, "ASR_OK\n", "")
        if name.startswith("s3-asrwin-"):
            label = name[len("s3-asrwin-"):]
            wins = json.loads(remote.files[f"/r/clip/witness/win/windows-{label}.json"])
            recs = [{"win": w["win"], "provider": p, "status": "OK", "excerpt_ms": 2000, "reused": False,
                     "billed": {"duration_ms": 2000}, "segments": [{"start_ms": 0, "end_ms": 1500, "text": "句子甲"}]}
                    for w in wins for p in ("mai", "moss")]
            remote.files[f"/r/clip/witness/win/result-windows-{label}.json"] = json.dumps(recs, ensure_ascii=False).encode()
            return RemoteResult(0, 'WITNESS_WINDOWS_DONE 2 {"reused": 0, "OK": 2, "new_audio_ms": 4000}\n', "")
        if name.startswith("s3-listen-"):
            remote.files["/r/clip/listen/out/all.json"] = b"[]"
            return RemoteResult(0, "LISTEN_DONE 1 records 0 {}\n", "")
        return RemoteResult(0, "", "")

    class Cpa:
        def __init__(self):
            self.calls, self.prompts = [], []

        def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
            self.calls.append(call_id)
            self.prompts.append(prompt_text)
            low = [{"id": "d001", "state": "KEEP", "final_text": "", "confidence": "低", "affects_meaning": False}]
            text = (json.dumps({"suspicious": [{"cue_id": 1, "reason": "读不通"}]}) if call_id == "review-c"
                    else json.dumps(rows.get(call_id, low), ensure_ascii=False))
            return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                    "response_text": text, "stderr_tail": ""}

    remote = FakeRemote(on_script=script)
    text = srt(3, step=2.0).encode()
    remote.files["/r/clip/asr/bcut.srt"] = remote.files["/r/clip/asr/jianying.srt"] = text
    cpa = Cpa()
    return make_ctx(tmp_path, remote=remote, cpa=cpa, bcut_text=srt(3, step=2.0)), calls, cpa


def test_a_doubt_the_window_asr_settles_is_never_sent_to_gemini(tmp_path, monkeypatch):
    # TASK-30: a KEEP the window ASR settles stays at L1; a change of the text goes on to listening
    # (test_task30_change_needs_listening)
    keep = [{"id": "d001", "state": "KEEP", "final_text": "", "confidence": "高", "affects_meaning": False,
             "basis": "现稿读得通，窗口 MAI 与 MOSS 的读法不影响意思"}]
    ctx, calls, cpa = _ladder_ctx(tmp_path, monkeypatch, "asr_first", {"adjudicate-c-L1-1": keep})
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    assert "s3-asrwin-C-L1" in calls and not any(c.startswith("s3-listen-") for c in calls)
    doubt = next(d for d in json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8"))
                 if d["cue_id"] == 1)
    assert doubt["readings"]["asr:mai@win"] == "句子甲" and doubt["settled_at_level"] == "L1"
    assert next(c for c in doubt["candidates"] if c["text"] == "句子甲")["families"] == ["asr:mai", "asr:moss"]
    assert "带 @win 的" in cpa.prompts[1]                                # the prompt says what a window reading is
    assert any(p.startswith("窗口重识别用量") and "MAI 1 次" in p for p in out.process)


def test_text_first_asks_cpa_before_any_listening_and_escalates_only_the_unsettled(tmp_path, monkeypatch):
    ctx, calls, cpa = _ladder_ctx(tmp_path, monkeypatch, "text_first", {})
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    assert cpa.calls[:2] == ["review-c", "adjudicate-c-L0-1"]            # CPA decides from the text evidence first
    assert [c for c in calls if c.startswith("s3-listen-")] == ["s3-listen-C-L1", "s3-listen-C-L2"]
