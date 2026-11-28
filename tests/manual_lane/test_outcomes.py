"""An owner-decided import writes down how each of the pipeline's sentences turned out (outcomes.py; owner)."""

from __future__ import annotations

import json

from fakes import FakeRemote, make_ctx

from src.autoslice.manual_lane import outcomes, step_import


def _srt(lines):
    return "\n\n".join(f"{i}\n00:00:0{i},000 --> 00:00:0{i + 1},000\n{t}" for i, t in enumerate(lines, 1)) + "\n"


def _push(root, final, baseline=None):
    (root / "subs").mkdir(parents=True)
    (root / "subs/final.srt").write_text(_srt(final), encoding="utf-8")
    if baseline is not None:
        (root / "subs/baseline.srt").write_text(_srt(baseline), encoding="utf-8")
    return root


def test_each_sentence_the_owner_saw_is_one_outcome_with_what_the_pipeline_knew(tmp_path):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=None)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/doubts.json").write_text(json.dumps([
        {"id": "d001", "final_text": "谢谢你喜欢熊老哥", "state": "ADOPT", "confidence": "低", "runs_same": 1, "affects_meaning": True},
        {"id": "d002", "final_text": "今天吃了火锅", "state": "KEEP", "confidence": "高", "runs_same": 3, "affects_meaning": False}],
        ensure_ascii=False), encoding="utf-8")
    push = _push(tmp_path / "push", ["谢谢你喜欢熊猫", "今天吃了火锅"], baseline=["谢谢你喜欢熊老哥", "今天吃了火锅"])
    step_import.record_import(ctx.state, ctx.clip_dir, push, source="push", authority="a", decided_by="owner")
    rows = [json.loads(line) for line in (tmp_path / outcomes.FILE).read_text(encoding="utf-8").splitlines()]
    assert [(r["n"], r["pipeline"], r["owner"], r["text_changed"]) for r in rows] == [
        (1, "谢谢你喜欢熊老哥", "谢谢你喜欢熊猫", True), (2, "今天吃了火锅", "今天吃了火锅", False)]
    assert (rows[0]["confidence"], rows[0]["runs_same"], rows[0]["state"]) == ("低", 1, "ADOPT")
    assert (rows[1]["confidence"], rows[1]["runs_same"]) == ("高", 3) and rows[0]["cid"] == "clip"


def test_a_push_without_what_he_saw_or_not_his_records_nothing(tmp_path):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=None)
    step_import.record_import(ctx.state, ctx.clip_dir, _push(tmp_path / "a", ["一句"]), source="push", authority="a", decided_by="owner")
    step_import.record_import(ctx.state, ctx.clip_dir, _push(tmp_path / "b", ["一句"], baseline=["一句"]), source="push", authority="a")
    assert not (tmp_path / outcomes.FILE).exists()


def test_his_names_on_an_import_record_what_step_4_had_said_of_each_line(tmp_path):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=None)
    (ctx.clip_dir / "speaker").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "speaker/decision.json").write_text(json.dumps({"mode": "linked_v8_clusters", "cues": [
        {"start_ms": 1000, "end_ms": 2000, "speaker": "李豆沙", "speaker_confidence": "高", "label_from": "arrival", "host_prob": 0.97},
        {"start_ms": 2000, "end_ms": 3000, "speaker": "连线", "speaker_confidence": "低", "label_from": "v8", "host_prob": 0.55},
        {"start_ms": 3000, "end_ms": 4000, "speaker": "连线", "speaker_confidence": "高", "label_from": "arrival", "host_prob": 0.1}]},
        ensure_ascii=False), encoding="utf-8")
    push = _push(tmp_path / "push", ["一", "二", "三"])
    (push / "speaker").mkdir()
    (push / "speaker/speakers.json").write_text(json.dumps([
        {"start_s": 1.0, "end_s": 2.0, "text": "一", "speaker": "李豆沙"}, {"start_s": 2.0, "end_s": 3.0, "text": "二", "speaker": "李豆沙"},
        {"start_s": 3.0, "end_s": 4.0, "text": "三", "speaker": "未知"}], ensure_ascii=False), encoding="utf-8")
    step_import.record_import(ctx.state, ctx.clip_dir, push, source="push", authority="a", decided_by="owner")
    rows = [json.loads(line) for line in (tmp_path / outcomes.SPEAKER_FILE).read_text(encoding="utf-8").splitlines()]
    # line 3 he left 未知: no outcome; line 2 the voiceprint's unsure guest was the host
    assert [(r["n"], r["pipeline"], r["owner"], r["confidence"], r["label_from"], r["host_guest_changed"]) for r in rows] == [
        (1, "李豆沙", "李豆沙", "高", "arrival", False), (2, "连线", "李豆沙", "低", "v8", True)]
