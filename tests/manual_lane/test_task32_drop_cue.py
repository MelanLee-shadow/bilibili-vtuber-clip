"""Ordinary subtitle writer honors CPA's explicit whole-cue editorial decisions.

Synthetic adapters exercise review, adjudication, audit records and the existing gate without network or media.
"""

from __future__ import annotations

import json

from fakes import FakeRemote, make_ctx, srt

from src.autoslice.manual_lane import doubt_policy as dp
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import suspicion
from src.autoslice.manual_lane.adapters import RemoteResult


def _drop(**extra):
    """CPA's answer for a sentence it decided is not subtitled: ADOPT, drop true, no text written."""
    return {"state": "ADOPT", "final_text": "", "drop": True, "confidence": "高", "affects_meaning": False,
            "reason": "本句是笑声或无实意的声音，按 U141 不写", **extra}


def _drop_saying(what, **extra):
    """CPA's answer for a drop that also says what the sentence really was (TASK-32: it writes the actual words)."""
    return {"state": "ADOPT", "final_text": what, "drop": True, "confidence": "高", "affects_meaning": False,
            "reason": "这句只是一个重复的字／无实意的声音", **extra}


class _Cpa:
    """Variant C's CPA: the full-text review names cue 2 and nothing else; every adjudication call answers with
    `row` (the same answer at every level, as the training clips produced)."""

    def __init__(self, row, *, cue_id=2):
        self.row, self.cue_id = row, cue_id
        self.calls: list[str] = []
        self.prompts: list[str] = []

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
        import re
        self.calls.append(call_id)
        self.prompts.append(prompt_text)
        if call_id == "review-c":
            text = json.dumps({"suspicious": [{"cue_id": self.cue_id, "reason": "读不通"}]}, ensure_ascii=False)
        else:
            ids = list(dict.fromkeys(re.findall(r'"id": "([^"]+)"', prompt_text)))
            text = json.dumps([dict(self.row, id=i) for i in ids], ensure_ascii=False)
        return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                "response_text": text, "stderr_tail": ""}


def _ctx(tmp_path, monkeypatch, bcut_text, row):
    """Variant C on the production ladder (asr_first): one clip whose BCUT and jianying agree, cue 2 the only doubt."""
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "C")
    monkeypatch.setenv("MANUAL_LANE_LADDER", "asr_first")

    def script(name, body, remote):
        if name == "s3-asr":
            return RemoteResult(0, "ASR_OK\n", "")
        if name.startswith("s3-asrwin-"):
            label = name[len("s3-asrwin-"):]
            wins = json.loads(remote.files[f"/r/clip/witness/win/windows-{label}.json"])
            recs = [{"win": w["win"], "provider": p, "status": "OK", "excerpt_ms": 2000, "reused": False,
                     "billed": {"duration_ms": 2000},
                     "segments": [{"start_ms": 0, "end_ms": 1500, "text": w["core"] and "句子丙"}]}
                    for w in wins for p in ("mai", "moss")]
            remote.files[f"/r/clip/witness/win/result-windows-{label}.json"] = json.dumps(recs, ensure_ascii=False).encode()
            return RemoteResult(0, 'WITNESS_WINDOWS_DONE 2 {"reused": 0, "OK": 2, "new_audio_ms": 4000}\n', "")
        if name.startswith("s3-listen-"):
            remote.files["/r/clip/listen/out/all.json"] = b"[]"
            return RemoteResult(0, "LISTEN_DONE 1 records 0 {}\n", "")
        return RemoteResult(0, "", "")

    remote = FakeRemote(on_script=script)
    data = bcut_text.encode()
    remote.files["/r/clip/asr/bcut.srt"] = data
    remote.files["/r/clip/asr/jianying.srt"] = data
    return make_ctx(tmp_path, remote=remote, cpa=_Cpa(row), bcut_text=bcut_text)


def _dropped_record(ctx):
    """subs/dropped_cues.json, or [] when the writer did not create it (the code before TASK-32)."""
    path = ctx.clip_dir / "subs/dropped_cues.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _final_srt(ctx):
    return (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")


def _doubt(ctx, doubt_id):
    return next(d for d in json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8"))
                if d["id"] == doubt_id)


def test_a_repeated_character_cpa_drops_is_omitted_and_recorded(tmp_path, monkeypatch):
    text = srt(3, step=2.0).replace("句子2", "当当当当当")
    ctx = _ctx(tmp_path, monkeypatch, text, _drop_saying("当当当当当", reason_key="拟声"))
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    assert "当当当当当" not in _final_srt(ctx)
    assert len(si.parse_srt(_final_srt(ctx))) == 2
    doubt = _doubt(ctx, "d002")
    assert doubt["state"] == "ADOPT" and doubt["dropped"] is True and doubt["final_text"] == ""
    assert doubt["drop_said"] == "当当当当当" and not doubt["drop_refused_written"]
    assert not dp.is_hold(doubt)
    record = _dropped_record(ctx)
    assert len(record) == 1 and record[0]["text"] == record[0]["said"] == "当当当当当"
    assert "decision" not in record[0] and record[0]["drop_kind"] == dp.DROP_TO_REPEAT
    assert any("CPA 判定不出字幕" in line and "当当当当当" in line for line in out.process)


def test_a_meaningful_repeat_drop_records_cpa_words_and_original(tmp_path, monkeypatch):
    text = srt(3, step=2.0).replace("句子2", "对不起哈，对不太理")
    ctx = _ctx(tmp_path, monkeypatch, text, _drop_saying("对对对", reason_key="重叠"))
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    assert "对对对" not in _final_srt(ctx) and "对不起哈" not in _final_srt(ctx)
    doubt = _doubt(ctx, "d002")
    assert doubt["dropped"] is True and doubt["final_text"] == "" and doubt["drop_said"] == "对对对"
    record = _dropped_record(ctx)
    assert record[0]["text"] == "对不起哈，对不太理" and record[0]["said"] == "对对对"
    assert "decision" not in record[0]


def test_a_laughter_line_cpa_drops_leaves_no_cue_and_the_clip_runs(tmp_path, monkeypatch):
    text = srt(3, step=2.0).replace("句子2", "哈哈哈")
    ctx = _ctx(tmp_path, monkeypatch, text, _drop())
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    final = _final_srt(ctx)
    assert "哈哈哈" not in final and "句子1" in final and "句子3" in final
    assert len(si.parse_srt(final)) == 2
    doubt = _doubt(ctx, "d002")
    assert doubt["state"] == "ADOPT" and doubt["dropped"] is True and doubt["final_text"] == ""
    assert not dp.is_hold(doubt)
    record = _dropped_record(ctx)
    assert [r["text"] for r in record] == ["哈哈哈"]
    assert record[0]["doubt_id"] == "d002" and record[0]["drop_kind"] == dp.DROP_TO_NOTHING
    assert "decision" not in record[0]
    assert any("CPA 判定不出字幕" in line and "哈哈哈" in line for line in out.process)


def test_a_drop_of_a_sentence_with_words_is_omitted_and_records_reason(tmp_path, monkeypatch):
    text = srt(3, step=2.0).replace("句子2", "我觉得她很好看")
    ctx = _ctx(tmp_path, monkeypatch, text, _drop_saying("我觉得她很好看", affects_meaning=True))
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    assert "我觉得她很好看" not in _final_srt(ctx)
    doubt = _doubt(ctx, "d002")
    assert doubt["state"] == "ADOPT" and doubt["dropped"] is True and doubt["affects_meaning"] is True
    record = _dropped_record(ctx)
    assert record[0]["said"] == "我觉得她很好看" and record[0]["reason"]


def test_drop_with_no_words_does_not_claim_current_text_was_heard(tmp_path, monkeypatch):
    text = srt(3, step=2.0).replace("句子2", "我觉得她很好看")
    ctx = _ctx(tmp_path, monkeypatch, text, _drop())
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    assert "我觉得她很好看" not in _final_srt(ctx)
    doubt = _doubt(ctx, "d002")
    assert doubt["state"] == "ADOPT" and doubt["dropped"] is True and doubt["drop_said"] == ""
    record = _dropped_record(ctx)
    assert record[0]["text"] == "我觉得她很好看" and record[0]["said"] == ""


def test_an_unresolved_doubt_without_a_text_still_stops_the_clip(tmp_path, monkeypatch):
    text = srt(3, step=2.0).replace("句子2", "哈哈哈")
    ctx = _ctx(tmp_path, monkeypatch, text,
               {"state": "UNRESOLVED", "final_text": "", "confidence": "高", "affects_meaning": False,
                "reason_key": "笑声"})
    out = si.step3_subtitles(ctx)
    assert not out.ok and out.reason == "DOUBT_UNRESOLVED"
    assert _dropped_record(ctx) == []


def test_a_sound_drop_settles_the_doubt_at_the_level_it_was_decided(tmp_path, monkeypatch):
    text = srt(3, step=2.0).replace("句子2", "哈哈哈")
    ctx = _ctx(tmp_path, monkeypatch, text, _drop())
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    assert _doubt(ctx, "d002")["settled_at_level"] == "L1"
    assert not any(c.startswith("s3-listen-") for c in ctx.adapters.cpa.calls)


def test_is_settled_counts_a_drop_and_still_refuses_a_low_confidence_one():
    assert suspicion.is_settled({"state": "ADOPT", "final_text": "", "dropped": True, "confidence": "高"})
    assert not suspicion.is_settled({"state": "ADOPT", "final_text": "", "dropped": True, "confidence": "低"})
    assert not suspicion.is_settled({"state": "ADOPT", "final_text": "", "confidence": "高"})


def test_drop_kind_is_description_not_authority():
    for text in ("哈哈哈", "啊哈哈", "（笑）", "嗯", "啊", ""):
        assert dp.drop_kind(text) == dp.DROP_TO_NOTHING
    for text in ("当当当当当", "对对对", "谢谢谢谢", "咳咳"):
        assert dp.drop_kind(text) == dp.DROP_TO_REPEAT
    for text in ("对", "那", "好"):
        assert dp.drop_kind(text) == dp.DROP_TO_LONE_CHAR
    for text in ("不行", "我觉得她很好看", "不行不行"):
        assert dp.drop_kind(text) == ""


def test_decide_applies_drop_and_keeps_invalid_or_incomplete_answers_unresolved():
    assert dp.decide({"state": "ADOPT", "final_text": "", "affects_meaning": True}, "哈哈哈",
                     maintained=False)["state"] == "UNRESOLVED"
    for state, said in (("ADOPT", "对对对"), ("FIT_CONTEXT", "对"), ("ADOPT", "我觉得她很好看")):
        decided = dp.decide({"state": state, "final_text": said, "drop": True, "affects_meaning": True,
                             "confidence": "低"}, "现稿", maintained=False)
        assert decided["state"] == state and decided["dropped"] is True and decided["final_text"] == ""
        assert decided["drop_said"] == said and decided["affects_meaning"] is True and dp.is_hold(decided)
        assert not decided["drop_refused_written"] and not decided["drop_routed"]
    for row in (None, {"state": "invalid", "final_text": "文字", "drop": True},
                {"state": "ADOPT", "final_text": "", "drop": False}):
        assert dp.decide(row, "现稿", maintained=False)["state"] == "UNRESOLVED"
