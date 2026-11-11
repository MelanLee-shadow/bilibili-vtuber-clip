"""TASK-18 (U150, U151, U144): the step-4 context review treats the acoustic label (8c1) as the default.  A reading
changes a label only where the acoustic routes tie, or with explicit content evidence quoted from the cue itself
(self-identification, content only one side could say about itself); address, question/answer order, a commenting tone
or a third-person name never overturn a decided label.  A cue is cut only at the label engine's switch and each part
takes that engine's label.  Step 9's review speaker fixes go through the same evidence rule."""

from __future__ import annotations

import json
from pathlib import Path

from fakes import FakeRemote, make_ctx

from src.autoslice.manual_lane import speaker_v8 as sv
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import RemoteResult

H, G = sv.HOST, sv.GUEST_LABEL
PROMPTS = Path(si.PROMPTS)

# v8 per cue; MOSS clusters S1 (the host voiceprint) / S2; no MAI, so MOSS and v8 are the two acoustic routes
V8 = [(0, 3000, H), (3000, 5000, H), (5000, 8000, G), (8000, 10000, H), (10000, 13000, H), (13000, 15000, H)]
MOSS = [(0, 4200, "S1"), (4200, 5000, "S2"), (5000, 10000, "S2"), (10000, 12000, "S1"), (12000, 13000, "S2"),
        (13000, 14000, "S1"), (14000, 15000, "S2")]
TEXTS = ["大家好", "这就是李豆沙", "我们开始吧", "不是我", "刚刚还在哭", "我是雾深呀"]


def _rows():
    return [{"start_ms": a, "end_ms": b, "speaker": s, "conf": 0.8, "units": 1} for a, b, s in V8]


def _cues():
    return [{"start_ms": a, "end_ms": b} for a, b, _ in V8]


def _ctx(tmp_path, answers, *, mai=None):
    asked = []

    class Cpa:
        def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
            asked.append(prompt_text)
            return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                    "response_text": json.dumps(answers, ensure_ascii=False), "stderr_tail": ""}

    def script(name, body, remote):
        if name == "s4-moss-voiceprint":
            remote.files[f"{si.SPEAKER_V8_ROOT}/clip-moss/out/centroids.json"] = json.dumps(
                {"S1": {"n": 4, "dur_ms": 8000, "cos": 0.68}, "S2": {"n": 3, "dur_ms": 7000, "cos": 0.31}}).encode()
            return RemoteResult(0, "CLUSTER_COS_DONE\n", "")
        return RemoteResult(0, "", "")

    ctx = make_ctx(tmp_path, remote=FakeRemote(on_script=script), cpa=Cpa())
    (ctx.clip_dir / "witness").mkdir(parents=True)
    (ctx.clip_dir / "witness/moss.json").write_text(json.dumps({"status": "OK", "segments": [
        {"start_ms": a, "end_ms": b, "speaker": s, "text": "x"} for a, b, s in MOSS]}), encoding="utf-8")
    if mai is not None:
        (ctx.clip_dir / "witness/mai.json").write_text(json.dumps({"status": "OK", "segments": [
            {"start_ms": a, "end_ms": b, "speaker": s, "text": "x"} for a, b, s in mai]}), encoding="utf-8")
    return ctx, asked


def _run(tmp_path, answers, **kw):
    ctx, asked = _ctx(tmp_path, answers, **kw)
    rows, calls, process = si._speaker_clusters(ctx, [{"text": t} for t in TEXTS], _cues(), _rows())
    return rows, asked, process


def test_a_tendency_reading_does_not_overturn_a_decided_label(tmp_path):
    # the witness (200736): all routes say the host, the engine hears two people; CPA relabelled on the third person
    rows, asked, _ = _run(tmp_path, [{"n": 2, "speaker": G, "evidence": "无", "reason": "用第三人称评论李豆沙"},
                                     {"n": 5, "speaker": G, "evidence": "只能出自一方", "quote": "刚刚还在哭",
                                      "reason": "评论她刚才的状态"}])
    assert rows[1]["speaker_confidence"] == "低" and rows[1]["speaker"] == H     # third person: not evidence
    assert rows[4]["speaker"] == H and "没有说话人说自己" in rows[4]["speaker_cpa_refused"]
    assert "2「这就是李豆沙」" in asked[0] and "声音证据倾向李豆沙，只有明确的内容证据才能改" in asked[0]


def test_unsure_on_a_decided_cue_keeps_its_acoustic_label_and_on_a_tie_is_the_guest(tmp_path):
    rows, _, process = _run(tmp_path, [{"n": 2, "speaker": "不确定"}, {"n": 4, "speaker": "不确定"}])
    assert rows[1]["speaker"] == H                                   # decided: 不确定 changes nothing
    assert sv.acoustic_tie(rows[3]) and rows[3]["speaker"] == G       # MOSS guest, v8 host: tie, unsure = guest
    assert rows[4]["speaker"] == H                                   # no answer at all: the acoustic label
    assert any("打平且拿不准按嘉宾色 1 句" in p for p in process)


def test_on_a_tie_the_context_decides_and_explicit_self_identification_overturns(tmp_path):
    rows, asked, _ = _run(tmp_path, [{"n": 4, "speaker": H, "evidence": "语境", "reason": "接着否认"},
                                     {"n": 6, "speaker": G, "evidence": "自称", "quote": "我是雾深", "reason": "自报身份"}])
    assert rows[3]["speaker"] == H and rows[5]["speaker"] == G          # control: the same on the old code
    assert rows[3]["speaker_how"] == "打平，按语境" and rows[5]["speaker_how"] == "内容证据"
    assert "4「不是我」" in asked[0] and "声音证据打平，可以按语境选一方" in asked[0]
    for bad in ({"evidence": "自称", "quote": "我是嘉宾"}, {"evidence": "语境", "quote": "我是雾深"},
                {"evidence": "只能出自一方", "quote": "我们"}):
        assert sv.adopt_review(dict(rows[5], speaker=H), dict(bad, speaker=G), "我是雾深呀我们")["speaker"] == H


def test_a_cut_is_made_only_at_the_label_engines_switch_with_its_own_labels(tmp_path):
    split = {"first": "刚刚", "first_speaker": H, "second": "还在哭", "second_speaker": G}
    rows, asked, _ = _run(tmp_path, [{"n": 5, "speaker": H, "split": split}])
    assert [(p["start_ms"], p["end_ms"], p["speaker"]) for p in rows[4]["split"]] == [(10000, 12000, H),
                                                                                    (12000, 13000, G)]
    assert "5「刚刚还在哭」" in asked[0] and "MOSS 在本句（共 3.0 秒）里换人：第 2.0 秒处由李豆沙换成连线" in asked[0]
    assert "切分不需要内容证据" in asked[0]
    reversed_ = dict(split, first_speaker=G, second_speaker=H)       # the reading and the label engine disagree
    rows, _, _ = _run(tmp_path / "r", [{"n": 5, "speaker": H, "split": reversed_}])
    assert "split" not in rows[4] and rows[4]["split_refused"]
    # MAI hears a switch inside cue 3 where MOSS hears one speaker: MAI only flags, it does not cut
    mai = [(0, 5000, "A"), (5000, 6500, "A"), (6500, 8000, "B"), (8000, 15000, "A")]
    cut3 = {"first": "我们", "first_speaker": G, "second": "开始吧", "second_speaker": H}
    rows, _, _ = _run(tmp_path / "m", [{"n": 3, "speaker": G, "split": cut3}], mai=mai)
    assert rows[2]["speaker_confidence"] == "低" and "split" not in rows[2]


def test_the_prompts_carry_the_rule_and_no_reading_aloud_heuristic():
    review = (PROMPTS / "speaker_review.md").read_text(encoding="utf-8")
    assert "不是内容证据，不能用来推翻有倾向的声音证据" in review and "大概率不是她" not in review
    for name in ("speaker_review.md", "review.md"):
        text = (PROMPTS / name).read_text(encoding="utf-8")
        assert "念题" not in text and all(k in text for k in sv.EVIDENCE_KINDS)
