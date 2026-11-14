"""TASK-17: a reading is the words said in its own sentence; the whole-clip CER sees what the per-sentence scores miss.

Relation: for every timed reading an engine gives over the sentences (a jianying cue, a whole-clip MAI / MOSS segment,
a window MAI / MOSS segment), each sentence receives only its own slice of it (evidence_attach.segment_slices), so the
neighbour's sentence never reaches the adjudication as a reading of this one.  Scoring: an inserted or repeated
sentence is an error of the whole-clip CER (score.cer_whole), the first measure, though exact / exact_flat do not see
it.  Synthetic texts."""

from __future__ import annotations

import json

from fakes import FakeRemote, make_ctx, rules_with

from src.autoslice.manual_lane import evidence_attach as ea
from src.autoslice.manual_lane import score as sc
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import RemoteResult

CUES = [{"id": 1, "start_s": 0.0, "end_s": 2.0, "text": "其实她的爽点"},
        {"id": 2, "start_s": 2.0, "end_s": 4.0, "text": "是一种爽文感的爽点"},
        {"id": 3, "start_s": 4.0, "end_s": 6.0, "text": "不是 XP 方向的一个爽点"}]
# one MAI segment over all three sentences (the engine put them in one segment), with its own small differences
WHOLE = (0.1, 5.9, "嗯。其实。 所以其实他的爽点是一种爽文感的爽点，不是 xp 方向的一个爽点。")


def test_a_segment_over_three_sentences_gives_each_sentence_only_its_own_slice():
    slices, orphan, _ = ea.segment_slices(CUES, *WHOLE)
    assert not orphan
    assert slices == {1: "嗯。其实。 所以其实他的爽点", 2: "是一种爽文感的爽点", 3: "不是 xp 方向的一个爽点"}


def test_the_whole_clip_witness_reading_of_a_sentence_is_its_slice_not_the_neighbours():
    lines = {"asr:mai": [{"abs_start_s": WHOLE[0], "end_s": WHOLE[1], "text": WHOLE[2]}]}
    att = ea.own_witness_lines(CUES, lines)
    assert si._readings_for_cue(CUES[0], lines) == {"asr:mai": "嗯。其实。 所以其实他的爽点"}
    assert att["asr:mai"].stats()["unhandled"] == 0 and att["asr:mai"].stats()["attached"] == 1


def test_jianying_segments_over_two_sentences_are_shared_by_text_not_copied():
    segs = [{"start_s": 0.0, "end_s": 3.1, "text": "其实 其实他的爽点是一种爽文感的爽点"},
            {"start_s": 3.9, "end_s": 6.0, "text": "不是XP方向的一个爽点"}]
    att = ea.attach_asr(CUES, segs, source="jianying")
    assert att.text(1) == "其实 其实他的爽点" and att.text(2) == "是一种爽文感的爽点"
    assert att.text(3) == "不是XP方向的一个爽点"
    assert att.stats() == {"total": 2, "attached": 2, "orphans": 0, "orphan_cues": 0, "extra_cues": 0, "unhandled": 0}


def test_a_word_in_latin_letters_is_never_cut_and_with_no_shared_text_time_decides():
    cues = [{"id": 1, "start_s": 0.0, "end_s": 1.0, "text": "甲乙"}, {"id": 2, "start_s": 1.0, "end_s": 3.0, "text": "丙丁"}]
    slices, _, _ = ea.segment_slices(cues, 0.8, 3.0, "当时get不到蓝黑")
    assert all("ge" not in v or "get" in v for v in slices.values())    # "get" stays one word
    slices, _, _ = ea.segment_slices(cues, 0.0, 3.0, "子丑，寅卯辰巳午未")   # nothing shared: the engine's break at 1/3
    assert slices == {1: "子丑", 2: "寅卯辰巳午未"}


def test_controls_a_segment_inside_one_sentence_an_orphan_and_an_interjection_are_unchanged():
    segs = [{"start_s": 2.2, "end_s": 3.0, "text": "是一种爽文感的爽点啊"}, {"start_s": 3.1, "end_s": 3.8, "text": "插话的人"},
            {"start_s": 7.0, "end_s": 8.0, "text": "漏掉的一句"}]
    att = ea.attach_asr(CUES, segs, source="asr:mai")
    assert att.text(2) == "是一种爽文感的爽点啊 插话的人"                     # inside one sentence: whole
    assert [s["text"] for s in att.extra[2]] == ["插话的人"]                  # still noted as another utterance
    assert att.text(3) == "漏掉的一句" and att.orphans[0]["cue_id"] == 3       # an orphan: whole, to the nearest cue
    assert att.stats()["unhandled"] == 0


def test_a_window_asr_segment_lying_in_the_next_sentence_is_not_the_window_sentences_reading():
    windows = {"C000": {"win": "C000", "core": [0.0, 2.0], "audio": [0.0, 3.0], "cue_ids": [1], "tempo": 1.0}}
    recs = [{"win": "C000", "provider": "mai", "status": "OK",
             "segments": [{"start_ms": 0, "end_ms": 1900, "text": "其实她的爽点"},
                          {"start_ms": 2050, "end_ms": 2950, "text": "是一种"}]}]
    out = si._asr_window_lines(recs, windows, CUES)
    assert [ln["text"] for ln in out["asr:mai@win"]] == ["其实她的爽点"]


# ----- the ordinary path: step 3 (variant C) shows CPA the slice, not the neighbour's sentence --------------------
def _srt(rows):
    return "\n\n".join(f"{i}\n{si.fmt_ts(a)} --> {si.fmt_ts(b)}\n{t}" for i, (a, b, t) in enumerate(rows, 1)) + "\n"


def test_step_3_shows_cpa_only_the_sentences_own_slice_of_a_whole_clip_segment(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "C")
    rules_with(tmp_path, monkeypatch, detection={"latin_unknown": False})   # 「XP」 would be a doubt CPA is not asked
    bcut = [(c["start_s"], c["end_s"], c["text"]) for c in CUES]

    def script(name, body, remote):
        if name == "s3-asr":
            return RemoteResult(0, "ASR_OK\n", "")
        if name.startswith("s3-listen-"):
            remote.files["/r/clip/listen/out/all.json"] = b"[]"
            return RemoteResult(0, "LISTEN_DONE 1 records 0 {}\n", "")
        return RemoteResult(0, "", "")

    remote = FakeRemote(on_script=script)
    remote.files["/r/clip/asr/bcut.srt"] = remote.files["/r/clip/asr/jianying.srt"] = _srt(bcut).encode()
    remote.files["/r/clip/witness/out/mai.json"] = json.dumps(
        {"status": "OK", "segments": [{"start_ms": int(WHOLE[0] * 1000), "end_ms": int(WHOLE[1] * 1000),
                                       "text": WHOLE[2]}]}, ensure_ascii=False).encode()

    class Cpa:
        def __init__(self):
            self.prompts = []

        def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
            self.prompts.append(prompt_text)
            text = (json.dumps({"suspicious": [{"cue_id": 1, "reason": "读不通"}]}) if call_id == "review-c" else
                    json.dumps([{"id": "d001", "state": "KEEP", "final_text": "", "confidence": "高",
                                 "affects_meaning": False}]))
            return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                    "response_text": text, "stderr_tail": ""}

    cpa = Cpa()
    ctx = make_ctx(tmp_path, remote=remote, cpa=cpa, bcut_text=_srt(bcut))
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    d1 = next(d for d in json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8"))
              if d["cue_id"] == 1)
    assert d1["readings"]["asr:mai"] == "嗯。其实。 所以其实他的爽点"
    adjudicate = next(p for p in cpa.prompts if "字幕疑点裁决" in p)
    doubts_part = adjudicate.split("输入疑点", 1)[1].split("全片字幕", 1)[0]
    assert "爽文感" not in doubts_part and "方向" not in doubts_part      # the neighbours' sentences are not readings
    assert "每一句只写本句时间里说的话" in adjudicate                      # and CPA is told the bound


# ----- scoring: the whole-clip CER sees an inserted neighbour sentence; exact_flat does not --------------------------
def _clip(tmp_path, srt_rows, truth_rows, bcut_rows=None):
    clip = tmp_path / "clips" / "c"
    (clip / "subs").mkdir(parents=True)
    (clip / "boundary").mkdir()
    (clip / "boundary" / "decision.json").write_text(json.dumps({"adopted": {"start_ms": 0}}), encoding="utf-8")
    (clip / "subs" / "final.srt").write_text(_srt(srt_rows), encoding="utf-8")
    if bcut_rows:   # the clip's whole-clip reading, for the truth joins (U159)
        (clip / "asr").mkdir()
        (clip / "asr" / "bcut.srt").write_text(_srt(bcut_rows), encoding="utf-8")
    truth = {f"c-{int(a * 1000)}": {"id": f"c-{int(a * 1000)}", "abs_start_ms": int(a * 1000), "abs_end_ms": int(b * 1000),
                                    "text": t, "speaker": None} for a, b, t in truth_rows}
    return sc.score_clip("c", tmp_path / "clips", truth)


def test_the_whole_clip_cer_counts_a_neighbour_sentence_carried_in_and_exact_flat_does_not(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUAL_LANE_SCORER", "v3")
    truth = [(0.0, 2.0, "其实她的爽点"), (2.0, 4.0, "是一种爽文感的爽点")]
    clean = _clip(tmp_path / "a", truth, truth)["summary"]
    leaked = _clip(tmp_path / "b", [(0.0, 2.0, "其实她的爽点是一种爽文感的爽点"), (2.0, 4.0, "是一种爽文感的爽点")],
                   truth)["summary"]
    assert clean["cer_whole"] == 0.0 and leaked["cer_whole"] > 0.5
    assert leaked["exact_flat"] == clean["exact_flat"] == 2                # the blind spot the first measure closes
    assert "cer_whole=" in sc.format_table([{"cid": "c", "summary": leaked, "uncovered": []}])


def test_truth_written_per_window_counts_the_repeat_at_a_join_once(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUAL_LANE_SCORER", "v3")
    # the listener typed the start of the next sentence into this one's window as well; the clip's whole-clip reading
    # (one segment over both sentences) has it once, so it counts once (U159: overlap_join decides)
    truth = [(0.0, 2.0, "其实她的爽点是一种"), (2.0, 4.0, "是一种爽文感的爽点")]
    bcut = [(0.0, 4.0, "其实她的爽点是一种爽文感的爽点")]
    right = _clip(tmp_path / "a", [(0.0, 2.0, "其实她的爽点"), (2.0, 4.0, "是一种爽文感的爽点")], truth, bcut)
    items, log = sc.dedup_truth([{"id": f"t{i}", "abs_start_ms": int(a * 1000), "abs_end_ms": int(b * 1000), "text": t}
                                 for i, (a, b, t) in enumerate(truth)], sc.clip_sources(tmp_path / "a" / "clips" / "c"))
    assert [i["text"] for i in items] == [sc.normalize("其实她的爽点"), sc.normalize("是一种爽文感的爽点")]
    assert log[0]["deleted"] == sc.normalize("是一种")
    right = right["summary"]
    twice = _clip(tmp_path / "b", [(0.0, 2.0, "其实她的爽点是一种"), (2.0, 4.0, "是一种爽文感的爽点")], truth, bcut)["summary"]
    assert right["cer_whole"] == 0.0 < twice["cer_whole"]


def test_the_scores_table_row_carries_the_whole_clip_cer_first(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUAL_LANE_SCORER", "v3")
    truth = [(0.0, 2.0, "其实她的爽点")]
    res = _clip(tmp_path, truth, truth)
    rows = sc.tsv_rows([res], "e01 train")
    assert sc.TSV_COLUMNS[3] == "cer_whole"
    assert rows[0].split("\t")[:4] == ["e01 train", "c", "v3", "0.0"] and rows[-1].split("\t")[1] == "TOTAL"
