"""Step 4 for a single-participant clip with other voices (the streamer watching a video or an animation, 维护者):
a few frames over the clip say whether a video someone else made is playing; if so every cue gets frames read for the
watched video's own burned-in subtitle, the v8 voiceprint runs, and one CPA review labels each cue host or not-host from
both.  A clip that shows no video keeps uniform host, byte for byte.  No ssh, no network, no model: fakes only."""

from __future__ import annotations

import json
import re

import pytest
from fakes import SESSION, make_ctx, rules_with, screen_remote

from src.autoslice.manual_lane import speaker_v8 as sv
from src.autoslice.manual_lane import step4_other_voices as ov
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import RemoteResult

H, G = sv.HOST, sv.GUEST_LABEL
WORKDIR = f"{si.SPEAKER_V8_ROOT}/clip"
TEXTS = ["大家好呀", "不行不行", "这动画好奇怪", "谢谢你呀"]
SINGLE = dict(SESSION, participants=[["李豆沙"]])


def _srt(texts, step=2.0):
    return "\n\n".join(f"{i}\n{si.fmt_ts((i - 1) * step)} --> {si.fmt_ts(i * step)}\n{t}"
                       for i, t in enumerate(texts, 1)) + "\n"


class Cpa:
    """Scripted speaker-review answers; keeps every prompt with the effort and purpose it was asked at."""

    def __init__(self, answers, rc=0):
        self.answers, self.rc, self.asked = answers, rc, []

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
        self.asked.append({"call_id": call_id, "prompt": prompt_text, "effort": effort})
        return {"call_id": call_id, "model": model, "effort": effort, "rc": self.rc, "elapsed_s": 0.1,
                "response_text": json.dumps(self.answers, ensure_ascii=False) if self.rc == 0 else "",
                "stderr_tail": "boom" if self.rc else ""}


def _v8(host_probs):
    return [{"id": f"clip-{i * 2000}", "ok": True,
             "probs": {"李豆沙": p, "礼墨Sumi": 0.0, "安晚awa": 1 - p}} for i, p in enumerate(host_probs)]


def _ctx(tmp_path, *, host_probs=(0.95, 0.95, 0.95, 0.42), answers=(), subtitles=None, cpa_rc=0, **screen):
    def other(name, body, remote):
        if name == "s4-speaker-v8":
            return RemoteResult(0, "SPEAKER_V8_DONE\n", "")
        return RemoteResult(0, "", "")

    remote = screen_remote(subtitles=subtitles if subtitles is not None else {1: "不行不行 だめだめ", 3: "谢谢你 ありがとう"},
                           on_other=other, **screen)
    remote.files[f"{WORKDIR}/out/pred.json"] = json.dumps({"predictions": _v8(host_probs)}).encode()
    cpa = Cpa(list(answers), rc=cpa_rc)
    ctx = make_ctx(tmp_path, remote=remote, cpa=cpa, session=SINGLE, bcut_text=_srt(TEXTS))
    (ctx.clip_dir / "asr").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "asr/bcut.srt").write_text(_srt(TEXTS), encoding="utf-8")
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(_srt(TEXTS), encoding="utf-8")
    return ctx, remote, cpa


def _labels(ctx):
    return [m.group(1) for m in re.finditer(r"^\[([^\]]+)\] ", (ctx.clip_dir / "speaker/speaker.srt").read_text(
        encoding="utf-8"), re.M)]


def _rows(ctx):
    return json.loads((ctx.clip_dir / "speaker/decision.json").read_text(encoding="utf-8"))["cues"]


# ----- a clip that shows no video is unchanged ------------------------------------------------------------------

def test_a_clip_without_a_playing_video_keeps_uniform_host_byte_for_byte(tmp_path):
    ctx, remote, cpa = _ctx(tmp_path, playing=False)
    out = si.step4_speaker(ctx)
    final = ctx.clip_dir / "subs/final.srt"
    assert out.ok and out.note == "speaker_mode=uniform_host"
    assert (ctx.clip_dir / "speaker/speaker.srt").read_text(encoding="utf-8") == si._label_host_lines(final)
    assert (ctx.clip_dir / "speaker/decision.json").read_text(encoding="utf-8") == json.dumps(
        {"speaker_mode": "uniform_host", "enablement_evidence": None, "rule": None}, ensure_ascii=False, indent=2)
    assert out.process[0] == "uniform host：每句都是主播，不加说话人标签（无分色开启记录）"
    assert [n for n, _ in remote.scripts] == ["s4-screen-screen"] and cpa.asked == []   # no v8 run, no text call
    assert len(out.cpa) == 6 and {c.get("kind") for c in out.cpa} == {"vision"}               # the paid reads are recorded


def test_with_the_screen_reading_off_nothing_is_asked_and_the_output_is_the_old_one(tmp_path, monkeypatch):
    rules_with(tmp_path, monkeypatch, other_voices={"screen_frames": 0})
    ctx, remote, cpa = _ctx(tmp_path / "c", playing=True)
    out = si.step4_speaker(ctx)
    assert out.ok and out.note == "speaker_mode=uniform_host" and out.cpa == [] and out.process == [
        "uniform host：每句都是主播，不加说话人标签（无分色开启记录）"]
    assert remote.scripts == [] and cpa.asked == []


def test_a_linked_session_never_reads_the_picture(tmp_path):
    ctx, remote, _ = _ctx(tmp_path)
    ctx.session["participants"] = [["李豆沙"], ["雾深Girimi"]]
    si.step4_speaker(ctx)
    assert not [n for n, _ in remote.scripts if n.startswith("s4-screen-")]


# ----- a clip with a video playing ------------------------------------------------------------------------------

ANSWERS = [{"n": 1, "speaker": H, "evidence": "声音", "quote": "", "reason": "声纹明确"},
           {"n": 2, "speaker": G, "evidence": "画面字幕", "quote": "不行不行", "reason": "与画面字幕吻合"},
           {"n": 3, "speaker": H, "evidence": "声音", "quote": "", "reason": "评论动画"},
           {"n": 4, "speaker": "不确定", "evidence": "无", "quote": "", "reason": "声纹打平，字幕也没有"}]


def test_a_playing_video_sends_the_cues_to_the_review_with_the_screen_subtitles_and_colours_by_its_answer(tmp_path):
    ctx, remote, cpa = _ctx(tmp_path, answers=ANSWERS)
    out = si.step4_speaker(ctx)
    assert out.ok, out.note
    assert _labels(ctx) == [H, G, H, G]                                  # 2 by the screen subtitle, 4 unsure = guest
    rows = _rows(ctx)
    assert [r["speaker_how"] for r in rows] == ["与声音证据一致", "画面字幕", "与声音证据一致", "拿不准，按嘉宾色"]
    assert rows[1]["speaker_acoustic"] == H and rows[1]["screen_subtitles"] == ["不行不行 だめだめ"]
    assert rows[1]["screen_match"] == 1.0 and rows[0]["screen_match"] == 0.0
    assert json.loads((ctx.clip_dir / "speaker/decision.json").read_text(encoding="utf-8"))["mode"] == "other_voices_video"
    assert out.note.startswith("other_voices host=2 guest=2")
    assert [n for n, _ in remote.scripts] == ["s4-screen-screen", "s4-speaker-v8", "s4-screen-cue"]
    prompt = cpa.asked[0]["prompt"]
    assert "2「不行不行」" in prompt and "字幕（中文）「不行不行 だめだめ」" in prompt and "吻合度 100%" in prompt
    assert "s 无被看视频画面" not in prompt and "[?] 谢谢你呀" in prompt           # the tie cue shows no v8 label
    assert cpa.asked[0]["effort"] == "high" and cpa.asked[0]["call_id"] == "speaker-video"


def test_the_paid_calls_are_all_recorded_with_their_tier(tmp_path):
    ctx, _, _ = _ctx(tmp_path, answers=ANSWERS)
    out = si.step4_speaker(ctx)
    ids = [c["call_id"] for c in out.cpa]
    assert ids == [f"speaker-screen-{i}" for i in range(1, 7)] + [f"speaker-cue-{i}" for i in range(1, 5)] + ["speaker-video"]
    assert {c["effort"] for c in out.cpa if c.get("kind") == "vision"} == {"low"}
    assert out.cpa[-1]["effort"] == "high" and out.cpa[-1]["purpose"] == "speaker-video"


@pytest.mark.parametrize("answer, host_probs, cue, label, refused", [
    # the voiceprint says host; a quote that is not in this cue's frames cannot overturn it
    ({"n": 2, "speaker": G, "evidence": "画面字幕", "quote": "完全没有的字幕", "reason": "x"}, (0.95, 0.95, 0.95, 0.95), 1, H,
     "摘抄的画面字幕不在本句取到的帧里"),
    # a cue with no screen subtitle at all
    ({"n": 3, "speaker": G, "evidence": "画面字幕", "quote": "这动画", "reason": "x"}, (0.95, 0.95, 0.95, 0.95), 2, H,
     "摘抄的画面字幕不在本句取到的帧里"),
    # the screen subtitle only shows the line came from the video: it can never make a cue the host
    ({"n": 2, "speaker": H, "evidence": "画面字幕", "quote": "不行不行", "reason": "x"}, (0.95, 0.10, 0.95, 0.95), 1, G,
     "画面字幕只能证明这句来自被看视频，不能证明是主播"),
    # tone and address are not content evidence
    ({"n": 3, "speaker": G, "evidence": "语境", "quote": "", "reason": "口吻像别人"}, (0.95, 0.95, 0.95, 0.95), 2, H,
     "依据不是明确的内容证据（语境）"),
])
def test_the_review_cannot_overturn_the_voiceprint_without_evidence_the_code_can_check(
        tmp_path, answer, host_probs, cue, label, refused):
    ctx, _, _ = _ctx(tmp_path, host_probs=host_probs, answers=[answer])
    assert si.step4_speaker(ctx).ok
    row = _rows(ctx)[cue]
    assert row["speaker"] == label and row["speaker_cpa_refused"] == refused


def test_on_a_voiceprint_tie_the_review_decides_and_a_missing_answer_keeps_the_voiceprint(tmp_path):
    answers = [{"n": 4, "speaker": G, "evidence": "语境", "quote": "", "reason": "回应动画人物"}]
    ctx, _, _ = _ctx(tmp_path, answers=answers)
    assert si.step4_speaker(ctx).ok
    rows = _rows(ctx)
    assert rows[3]["speaker"] == G and rows[3]["speaker_how"] == "声纹打平，按画面字幕与语境"
    assert [r["speaker_how"] for r in rows[:3]] == ["CPA 未答，按声音证据"] * 3 and _labels(ctx) == [H, H, H, G]


def test_a_cue_is_never_cut_because_no_engine_hears_a_switch_inside_it(tmp_path):
    answer = dict(ANSWERS[2], split={"first": "这动画", "first_speaker": H, "second": "好奇怪", "second_speaker": G})
    ctx, _, _ = _ctx(tmp_path, answers=[answer])
    assert si.step4_speaker(ctx).ok
    row = _rows(ctx)[2]
    assert "split" not in row and row["split_refused"] == "标签引擎在本句里没有换人时刻"
    assert len(_labels(ctx)) == 4


# ----- failures follow the step's existing rule: a failed tool stops it, never a silent default -----------------

@pytest.mark.parametrize("fault", [{"no_output": ("screen",)}, {"broken": ("screen",)}])
def test_a_failed_screen_reading_stops_the_step(tmp_path, fault):
    ctx, remote, cpa = _ctx(tmp_path, answers=ANSWERS, **fault)
    out = si.step4_speaker(ctx)
    assert not out.ok and out.reason == "SPEAKER_SCREEN_FAILED"
    assert "speaker/speaker.srt" not in {p.relative_to(ctx.clip_dir).as_posix() for p in ctx.clip_dir.rglob("*") if p.is_file()}
    assert cpa.asked == [] and "s4-speaker-v8" not in [n for n, _ in remote.scripts]


def test_a_failed_cue_reading_stops_the_step_before_the_review(tmp_path):
    ctx, remote, cpa = _ctx(tmp_path, answers=ANSWERS, broken=("cue",))
    out = si.step4_speaker(ctx)
    assert not out.ok and out.reason == "SPEAKER_SCREEN_FAILED" and cpa.asked == []
    assert len([c for c in out.cpa if c.get("kind") == "vision"]) == 10          # the paid reads are still on the record


def test_a_failed_review_call_stops_the_step_as_for_a_linked_clip(tmp_path):
    ctx, _, _ = _ctx(tmp_path, answers=ANSWERS, cpa_rc=1)
    out = si.step4_speaker(ctx)
    assert not out.ok and out.reason == "CPA_CALL_FAILED"


def test_a_failed_v8_run_stops_the_step(tmp_path):
    ctx, remote, _ = _ctx(tmp_path, answers=ANSWERS)
    remote.on_script = lambda name, body, r: (RemoteResult(1, "SPEAKER_EMBED_FAILED\n", "") if name == "s4-speaker-v8"
                                              else screen_remote(subtitles={}).on_script(name, body, r))
    out = si.step4_speaker(ctx)
    assert not out.ok and out.reason == "SPEAKER_V8_FAILED"


def test_the_same_frames_are_not_read_twice(tmp_path):
    ctx, remote, _ = _ctx(tmp_path, answers=ANSWERS)
    assert si.step4_speaker(ctx).ok
    again = si.step4_speaker(ctx)
    assert again.ok and [n for n, _ in remote.scripts].count("s4-screen-screen") == 1
    assert [n for n, _ in remote.scripts].count("s4-screen-cue") == 1
    assert [c for c in again.cpa if c.get("kind") == "vision"] == []             # nothing asked, nothing to record


# ----- the evidence helpers -------------------------------------------------------------------------------------

def test_cue_frames_follow_the_cue_length_and_the_screen_frames_spread_over_the_clip():
    cues = [{"start_s": 0.0, "end_s": 1.0}, {"start_s": 2.0, "end_s": 4.6}, {"start_s": 5.0, "end_s": 10.2},
            {"start_s": 11.0, "end_s": 31.0}]
    got = ov.cue_requests(cues)
    assert [f["id"] for f in got] == ["c0-0", "c1-0", "c1-1", "c2-0", "c2-1", "c2-2", "c3-0", "c3-1", "c3-2"]
    assert all(c["start_s"] < f["t"] < c["end_s"] for f in got for c in [cues[int(f["id"][1])]])
    screen = ov.screen_requests(cues)
    assert len(screen) == 6 and screen[0]["t"] > 0 and screen[-1]["t"] < 31.0
    assert [f["t"] for f in screen] == sorted(f["t"] for f in screen)


def test_coverage_is_the_share_of_the_cue_found_in_order_in_the_screen_subtitle():
    assert ov.coverage("谢谢你", "谢谢你 ありがとう") == 1.0
    assert ov.coverage("ありがとう", "谢谢你 ありがとう") == 1.0
    assert 0.0 < ov.coverage("谢谢你呀", "谢谢你 ありがとう") < 1.0
    assert ov.coverage("大家好", "") == 0.0 and ov.coverage("", "谢谢") == 0.0


def test_only_a_readable_playing_frame_counts_as_a_video(tmp_path):
    frame = {"id": "s0", "t": 1.0, "ok": True, "video_playing": True}
    assert ov.video_playing([frame])
    assert not ov.video_playing([dict(frame, video_playing=False), dict(frame, ok=False)])
    assert not ov.video_playing([])


def test_the_review_prompt_carries_the_rule_and_names_the_two_labels():
    text = (si.PROMPTS / "speaker_video.md").read_text(encoding="utf-8")
    assert "{host}" in text and "{guest}" in text and "画面字幕" in text and "主播有时会跟着念" in text
    assert "{lines}" in text and "{pending}" in text
