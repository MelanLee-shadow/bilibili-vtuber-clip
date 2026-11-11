"""Speaker provenance survives ordinary subtitle production; all speech here is synthetic."""
from __future__ import annotations

import json

import pytest
from fakes import IdCpa, make_ctx
from test_evidence_attach import BCUT, RD, _remote, _srt

from src.autoslice.manual_lane import evidence_attach
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.step3_doubts import evidence_extra


class ProvenanceCpa(IdCpa):
    """Inspect the actual ordinary-path input before returning a separated answer."""

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
        if call_id == "review-c":
            return {"call_id": call_id, "rc": 0, "elapsed_s": 0.1,
                    "response_text": '{"suspicious": []}', "stderr_tail": ""}
        if call_id.startswith("adjudicate-"):
            tail = prompt_text.split("输入疑点（含候选读法及其支持来源、各来源读法与弹幕线索）：", 1)[1]
            rows = json.JSONDecoder().raw_decode(tail.lstrip())[0]
            d = next(r for r in rows if r["cue_id"] == 1)
            assert [(s["speaker"], s["text"]) for s in d["speaker_segments"]] == [
                ("speaker0", "甲乙丙丁"), ("speaker1", "这话让我来说")]
            assert '"other_speaker_text":' in prompt_text
            assert "不同人的话串进同一个 final_text" in prompt_text
        return super().ask(call_id=call_id, prompt_text=prompt_text, model=model,
                           effort=effort, remote_dir=remote_dir)


@pytest.mark.parametrize("mode", ["C", "A", "full"])
def test_diarized_witness_reaches_default_adjudication_without_extra_listening(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", mode)
    monkeypatch.delenv("MANUAL_LANE_TWO_SPEAKER_DOUBTS", raising=False)
    monkeypatch.setenv("MANUAL_LANE_WHOLE_WITNESS", "moss")
    remote = _remote()
    remote.files[f"{RD}/witness/out/moss.json"] = json.dumps({"status": "OK", "segments": [
        {"start_ms": 0, "end_ms": 2000, "text": "甲乙丙丁", "speaker": "speaker0"},
        {"start_ms": 800, "end_ms": 1800, "text": "这话让我来说", "speaker": "speaker1"},
    ]}, ensure_ascii=False).encode()
    cpa = ProvenanceCpa(rows=[{"id": "d001", "state": "KEEP", "final_text": "甲乙丙丁",
                              "other_speaker_text": "这话让我来说", "other_first": False,
                              "confidence": "高", "affects_meaning": False, "basis": "分簇原文"}])
    ctx = make_ctx(tmp_path, remote=remote, cpa=cpa, bcut_text=_srt(BCUT))
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    cues = si.parse_srt((ctx.clip_dir / "subs/final.srt").read_text())
    assert any(c["text"] == "甲乙丙丁" for c in cues)
    assert any(c["text"] == "这话让我来说" for c in cues)  # real speech is retained, not suppressed
    assert not any("甲乙丙丁" in c["text"] and "这话让我来说" in c["text"] for c in cues)
    assert not any(name == "s3-listen-C-S" for name, _ in remote.scripts)


def test_single_speaker_segments_do_not_authorize_deleting_or_splitting_words():
    cues = [{"id": 1, "start_s": 0.0, "end_s": 3.0, "text": "今天讨论天气"}]
    lines = {"asr:moss": [{"abs_start_s": 0.0, "end_s": 1.5, "text": "今天讨论天气", "speaker": "speaker0"},
                          {"abs_start_s": 1.5, "end_s": 3.0, "text": "顺便准备雨伞", "speaker": "speaker0"}]}
    attached = evidence_attach.own_witness_lines(cues, lines)
    jy = evidence_attach.attach_asr(cues, [], source="jianying")
    ev = si._evidence_for_cue(cues[0], 0, cues, jy, attached, lines, [])
    assert {s["speaker"] for s in evidence_extra(ev)["speaker_segments"]} == {"speaker0"}
    final, _ = si._final_cues(cues, [{"cue_id": 1, "state": "ADOPT", "final_text": "今天讨论天气顺便准备雨伞"}])
    assert "".join(c["text"].replace(" ", "") for c in final) == "今天讨论天气顺便准备雨伞"


def test_escalation_keeps_both_speakers_words_in_previous_answer():
    from src.autoslice.manual_lane.step3_adjudicate import first_pass_view

    row = {"state": "ADOPT", "final_text": "今天讨论天气", "other_speaker_text": "请带一把雨伞",
           "other_first": True, "confidence": "低", "basis": "有交叠"}
    view = first_pass_view(row)
    assert view["other_speaker_text"] == "请带一把雨伞"
    assert view["other_first"] is True


def test_window_diarization_preserves_source_local_clusters_and_owned_text():
    cues = [{"id": 1, "start_s": 1.0, "end_s": 4.0, "text": "今天讨论天气"}]
    records = [{"win": "w", "provider": "moss", "status": "OK", "segments": [
        {"start_ms": 1000, "end_ms": 4000, "text": "今天讨论天气", "speaker": "speaker0"},
        {"start_ms": 2000, "end_ms": 3000, "text": "请带一把雨伞", "speaker": "speaker1"}]}]
    lines = si._asr_window_lines(records, {"w": {"audio": [0.0, 5.0], "cue_ids": [1]}}, cues)
    jy = evidence_attach.attach_asr(cues, [], source="jianying")
    ev = si._evidence_for_cue(cues[0], 0, cues, jy, {}, lines, [])
    assert [(s["source"], s["speaker"], s["text"]) for s in evidence_extra(ev)["speaker_segments"]] == [
        ("asr:moss@win", "speaker0", "今天讨论天气"), ("asr:moss@win", "speaker1", "请带一把雨伞")]
