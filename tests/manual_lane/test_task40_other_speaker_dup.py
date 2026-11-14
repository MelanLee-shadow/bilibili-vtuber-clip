"""TASK-40: one timed utterance, one owner; real repetitions remain speech."""
from __future__ import annotations

import json

import pytest
from fakes import FakeRemote, IdCpa, make_ctx
from test_evidence_attach import RD, _srt

from src.autoslice.manual_lane import overlap_join
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import RemoteResult

CUES = [(0.0, 2.0, "今天讨论天气"), (2.0, 6.0, "明天准备出门"), (6.0, 8.0, "后天继续工作")]
EXTRA = "请记得带上雨伞"


class SegmentCpa(IdCpa):
    """Consume the speaker evidence that ordinary step 3 actually supplies."""

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
        if call_id == "review-c":
            return {"call_id": call_id, "rc": 0, "response_text": json.dumps({
                "suspicious": [{"cue_id": i, "reason": "合成分人疑点"} for i in range(1, 4)]})}
        if call_id.startswith("adjudicate-"):
            tail = prompt_text.split("输入疑点（含候选读法及其支持来源、各来源读法与弹幕线索）：", 1)[1]
            for d in json.JSONDecoder().raw_decode(tail.lstrip())[0]:
                extra = " ".join(s["text"] for s in d.get("speaker_segments", []) if s["speaker"] == "guest")
                self.rows[d["id"]] = {"id": d["id"], "state": "KEEP", "final_text": d["current"],
                                      "other_speaker_text": extra, "confidence": "高", "affects_meaning": False}
        return super().ask(call_id=call_id, prompt_text=prompt_text, model=model, effort=effort, remote_dir=remote_dir)


def _run(tmp_path, monkeypatch, guest, mode="C"):
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", mode)
    monkeypatch.setenv("MANUAL_LANE_WHOLE_WITNESS", "moss")

    def script(name, body, remote):
        if name.startswith("s3-listen-"):
            remote.files[f"{RD}/listen/out/all.json"] = b"[]"
            return RemoteResult(0, 'LISTEN_DONE 0 records 0 {"OK": 0}\n', "")
        return RemoteResult(0, "ASR_OK\n" if name == "s3-asr" else "", "")

    remote = FakeRemote(script)
    remote.files[f"{RD}/asr/bcut.srt"] = _srt(CUES).encode()
    remote.files[f"{RD}/asr/jianying.srt"] = _srt(CUES).encode()
    segments = [{"start_ms": int(a * 1000), "end_ms": int(b * 1000), "text": t, "speaker": "main"}
                for a, b, t in CUES]
    segments += [{"start_ms": a, "end_ms": b, "text": EXTRA, "speaker": "guest"} for a, b in guest]
    remote.files[f"{RD}/witness/out/moss.json"] = json.dumps({"status": "OK", "segments": segments}).encode()
    ctx = make_ctx(tmp_path, remote=remote, cpa=SegmentCpa(), bcut_text=_srt(CUES))
    result = si.step3_subtitles(ctx)
    assert result.ok, (result.reason, result.note)
    return json.loads((ctx.clip_dir / "subs/doubts.json").read_text()), ctx


@pytest.mark.parametrize("mode", ["C", "full"])
def test_one_guest_segment_across_three_cues_has_only_the_max_overlap_owner(tmp_path, monkeypatch, mode):
    rows, ctx = _run(tmp_path, monkeypatch, [(1000, 7000)], mode)
    assert [(d["cue_id"], d["other_speaker_text"]) for d in rows if d.get("other_speaker_text")] == [(2, EXTRA)]
    assert (ctx.clip_dir / "subs/final.srt").read_text().count(EXTRA) == 1


def test_two_real_repetitions_by_the_same_speaker_are_both_kept(tmp_path, monkeypatch):
    rows, ctx = _run(tmp_path, monkeypatch, [(500, 1800), (6200, 7800)])
    assert [(d["cue_id"], d["other_speaker_text"]) for d in rows if d.get("other_speaker_text")] == [(1, EXTRA), (3, EXTRA)]
    assert (ctx.clip_dir / "subs/final.srt").read_text().count(EXTRA) == 2


def test_existing_neighbour_trim_includes_the_other_speakers_text_and_preserves_real_repeat():
    prev = {"start_s": 0.0, "end_s": 2.0, "text": "主句不同", "other_speaker_text": EXTRA}
    cue = {"start_s": 2.0, "end_s": 4.0}
    once = {"asr:moss:guest": [{"start_s": 0.0, "end_s": 4.0, "text": EXTRA + "然后出门"}]}
    twice = {"asr:moss:guest": [{"start_s": 0.0, "end_s": 4.0, "text": EXTRA + EXTRA + "然后出门"}]}
    assert overlap_join.trim_reading(EXTRA + "然后出门", cue, prev, None, once)[0] == "然后出门"
    assert overlap_join.trim_reading(EXTRA + "然后出门", cue, prev, None, twice)[0] == EXTRA + "然后出门"


def test_clip_relevance_filters_only_hints_not_names_and_groups_identical_scope(tmp_path, monkeypatch):
    from src.autoslice.manual_lane import step3_adjudicate as adjudicate

    scope = {"scope": {"kind": "context", "description": "合成范围不得扩大"}, "authority": "合成"}
    pairs = [{"misheard": a, "standard": b, "evidence": [scope]} for a, b in [
        ("错字甲", "标准甲"), ("错字乙", "标准乙"), ("无关错字", "无关标准"), ("别名丙", "标准丙")]]
    pair_path, name_path = tmp_path / "pairs.json", tmp_path / "names.json"
    pair_path.write_text(json.dumps(pairs))
    name_path.write_text(json.dumps([{"role": "专名", "name": "全部专名", "note": "保留这个名字"}]))
    monkeypatch.setattr(adjudicate, "MISHEARING_PAIRS", pair_path)
    monkeypatch.setattr(adjudicate, "PROPER_NOUNS", name_path)
    ctx = make_ctx(tmp_path, remote=None, cpa=None, session={"participants": [["标准丙", "别名丙"]]})
    (ctx.clip_dir / "asr").mkdir(parents=True)
    (ctx.clip_dir / "asr/bcut.srt").write_text(_srt([(0, 1, "今天出现错字甲")]))
    (ctx.clip_dir / "listen").mkdir()
    (ctx.clip_dir / "listen/readings.json").write_text(json.dumps([{"status": "OK", "lines": ["标准乙"]}]))
    # Ordinary adjudication must select against all clip readings, not only this batch/context.
    calls = []
    cpa = IdCpa()
    ctx.adapters.cpa = cpa
    rows, failure = adjudicate._adjudicate_all(ctx, [{"id": "d001", "current": "别的句子"}], "无关本批", calls, tag="test")
    assert failure is None and rows
    text = cpa.prompts[0]
    assert "无关错字" not in text and "无关标准" not in text
    assert all(word in text for word in ["错字甲", "标准甲", "错字乙", "标准乙", "标准丙", "全部专名"])
    assert text.count("合成范围不得扩大") == 1
    assert "不是替换规则" in text

@pytest.mark.parametrize("repeated", [False, True])
def test_parallel_answers_are_reconciled_by_the_existing_join_in_the_ordinary_writer(tmp_path, repeated):
    from src.autoslice.manual_lane.step3_doubts import write_subtitles

    ctx = make_ctx(tmp_path, remote=None, cpa=None)
    cues = [{"id": i + 1, "start_s": i * 2.0, "end_s": (i + 1) * 2.0, "text": t}
            for i, t in enumerate(["主句内容", "后句内容"])]
    rows = [{"id": f"d{i + 1:03d}", "cue_id": i + 1, "state": "KEEP", "current": c["text"],
             "final_text": c["text"], "other_speaker_text": EXTRA + "。"} for i, c in enumerate(cues)]
    (ctx.clip_dir / "witness").mkdir(parents=True)
    # Same source and same speaker, one spanning utterance vs two real utterances at different times.
    segments = ([{"start_ms": 0, "end_ms": 2000, "text": EXTRA, "speaker": "guest"},
                 {"start_ms": 2000, "end_ms": 4000, "text": EXTRA, "speaker": "guest"}] if repeated else
                [{"start_ms": 0, "end_ms": 4000, "text": EXTRA, "speaker": "guest"}])
    (ctx.clip_dir / "witness/moss.json").write_text(json.dumps({"status": "OK", "segments": segments}))
    path, final, _ = write_subtitles(ctx, cues, rows)
    stored = json.loads(path.read_text())
    assert sum(bool(r["other_speaker_text"]) for r in stored) == (2 if repeated else 1)
    assert final.read_text().count(EXTRA) == (2 if repeated else 1)


def test_separately_timed_repetition_survives_a_mismatching_stutter_in_the_previous_answer(tmp_path):
    from src.autoslice.manual_lane.step3_doubts import write_subtitles

    ctx = make_ctx(tmp_path, remote=None, cpa=None)
    phrase = "请带伞"
    cues = [{"id": 1, "start_s": 0.0, "end_s": 2.0, "text": "请带请带伞"},
            {"id": 2, "start_s": 3.0, "end_s": 6.0, "text": "另外一句主句"}]
    rows = [{"id": "d001", "cue_id": 1, "state": "KEEP", "current": cues[0]["text"],
             "final_text": cues[0]["text"]},
            {"id": "d002", "cue_id": 2, "state": "KEEP", "current": cues[1]["text"],
             "final_text": cues[1]["text"], "other_speaker_text": phrase + "。"}]
    (ctx.clip_dir / "witness").mkdir(parents=True)
    segments = [{"start_ms": 200, "end_ms": 1800, "text": phrase, "speaker": "guest"},
                {"start_ms": 4000, "end_ms": 5000, "text": phrase, "speaker": "guest"}]
    (ctx.clip_dir / "witness/moss.json").write_text(json.dumps({"status": "OK", "segments": segments}))
    path, final, _ = write_subtitles(ctx, cues, rows)
    assert json.loads(path.read_text())[1]["other_speaker_text"] == phrase + "。"
    assert phrase in final.read_text()
    k, info = overlap_join.overlap_k(cues[0]["text"], phrase, boundary_s=2.5, lo_s=0, hi_s=6,
                                    sources={"source:guest": [{"start_s": 0.2, "end_s": 1.8, "text": phrase},
                                                             {"start_s": 4, "end_s": 5, "text": phrase}]})
    assert k == 0 and info["timed_repetitions"]
