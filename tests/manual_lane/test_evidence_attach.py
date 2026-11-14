"""Evidence attachment (TASK-6, U123): every ASR segment, listening line and danmaku comment reaches the doubt it
belongs to and is shown to CPA, or is counted in PROCESS; nothing is dropped silently.  These go through step 3 with
the fake remote (the ordinary path), in variant C and in the full / A paths, not through the helper alone.

All texts are synthetic."""

from __future__ import annotations

import json
import re

import pytest
from fakes import FakeRemote, IdCpa, make_ctx, rules_with

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import RemoteResult

RD = "/r/clip"

# three BCUT cues with a gap between cue 2 and cue 3
BCUT = [(0.0, 2.0, "甲乙丙丁"), (2.0, 4.0, "戊己庚辛"), (8.0, 10.0, "子丑寅卯")]
# jianying: cue 1 as one segment; cue 2 split in two, the second part (3.4-4.4) contains no BCUT midpoint and carries
# words BCUT does not have; a segment in the gap (5.0-6.5) overlaps no cue; cue 3 as one segment.
JIANYING = [(0.0, 2.0, "甲乙丙丁"), (2.0, 3.3, "戊己庚辛"), (3.4, 4.4, "壬癸插话"), (5.0, 6.5, "孤立的一句"),
            (8.0, 10.0, "子丑寅卯")]


def _srt(rows):
    return "\n\n".join(f"{i}\n{si.fmt_ts(a)} --> {si.fmt_ts(b)}\n{t}" for i, (a, b, t) in enumerate(rows, 1)) + "\n"


class ReviewCpa(IdCpa):
    """The full-text review marks nothing; every adjudication answers KEEP (IdCpa)."""

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
        if call_id == "review-c":
            self.calls.append(call_id)
            self.prompts.append(prompt_text)
            return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                    "response_text": json.dumps({"suspicious": []}), "stderr_tail": ""}
        return super().ask(call_id=call_id, prompt_text=prompt_text, model=model, effort=effort, remote_dir=remote_dir)


def _remote(listen_records=None, mai=None):
    def script(name, body, remote):
        if name == "s3-asr":
            return RemoteResult(0, "ASR_OK\n", "")
        if name.startswith("s3-listen-"):
            remote.files[f"{RD}/listen/out/all.json"] = json.dumps(listen_records or [], ensure_ascii=False).encode()
            return RemoteResult(0, 'LISTEN_DONE 1 records 1 {"OK": 1}\n', "")
        return RemoteResult(0, "", "")
    remote = FakeRemote(on_script=script)
    remote.files[f"{RD}/asr/bcut.srt"] = _srt(BCUT).encode()
    remote.files[f"{RD}/asr/jianying.srt"] = _srt(JIANYING).encode()
    if mai is not None:
        remote.files[f"{RD}/witness/out/mai.json"] = json.dumps(
            {"status": "OK", "segments": [{"start_ms": int(a * 1000), "end_ms": int(b * 1000), "text": t}
                                          for a, b, t in mai]}, ensure_ascii=False).encode()
    return remote


def _danmaku_xml(rows):
    """rows: (seconds on the cut timeline, text); make_ctx's clip starts at 10 s of the source."""
    body = "".join(f'<d p="{10.0 + t:.3f},1,25,16777215,0,0,0,0">{text}</d>' for t, text in rows)
    return f"<i>{body}</i>".encode()


def _doubts(ctx):
    return {d["cue_id"]: d for d in json.loads((ctx.clip_dir / "subs" / "doubts.json").read_text(encoding="utf-8"))}


def _run(tmp_path, monkeypatch, mode, *, listen_records=None, mai=None, danmaku=()):
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", mode)
    cpa = ReviewCpa()
    ctx = make_ctx(tmp_path, remote=_remote(listen_records, mai), cpa=cpa, bcut_text=_srt(BCUT),
                   danmaku_xml=_danmaku_xml(danmaku))
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    return ctx, cpa, out


@pytest.mark.parametrize("mode", ["C", "full", "A"])
def test_a_segment_without_any_bcut_midpoint_reaches_its_cue_and_is_shown_to_cpa(tmp_path, monkeypatch, mode):
    ctx, cpa, out = _run(tmp_path, monkeypatch, mode)
    d2 = _doubts(ctx)[2]
    assert "壬癸插话" in d2["readings"]["jianying"]            # the old midpoint rule dropped this segment
    assert any("壬癸插话" in n for n in d2.get("asr_notes", []))
    assert any("壬癸插话" in p for p in cpa.prompts if "adjudicate" in p or "字幕疑点裁决" in p)


@pytest.mark.parametrize("mode", ["C", "full"])
def test_a_jianying_orphan_with_four_content_characters_becomes_its_own_line(tmp_path, monkeypatch, mode):
    """TASK-28 (COACH e05 §3, lane_rules orphans): the orphan is a line of its own with jianying's text and time; it
    is no longer a reading of the nearest cue nor told to CPA as a sentence to merge there."""
    ctx, _cpa, out = _run(tmp_path, monkeypatch, mode)
    d2 = _doubts(ctx).get(2) or {}
    assert "孤立的一句" not in str((d2.get("readings") or {}).get("jianying", ""))
    assert not any("BCUT 可能漏句" in n for n in d2.get("asr_notes", []))
    final = (ctx.clip_dir / "subs" / "final.srt").read_text(encoding="utf-8")
    assert re.search(r"00:00:05,000 --> 00:00:06,500\n孤立的一句", final)
    text = "\n".join(out.process)
    assert "单独成句 1 段" in text
    assert re.search(r"ASR 段挂接（jianying）：共 5 段，已挂接 5 段.*未处理 0 段", text)


@pytest.mark.parametrize("mode", ["C", "full"])
def test_a_segment_overlapping_no_cue_goes_to_the_nearest_cue_as_a_missed_sentence_doubt(tmp_path, monkeypatch, mode):
    rules_with(tmp_path, monkeypatch, orphans={"enabled": False})   # the rule before TASK-28
    ctx, _cpa, out = _run(tmp_path, monkeypatch, mode)
    d2 = _doubts(ctx)[2]                                      # 5.0-6.5 is 1.0 s from cue 2 and 1.5 s from cue 3
    assert "孤立的一句" in d2["readings"]["jianying"]
    assert any("BCUT 可能漏句" in n and "孤立的一句" in n for n in d2["asr_notes"])
    text = "\n".join(out.process)
    assert re.search(r"ASR 段挂接（jianying）：共 5 段，已挂接 5 段.*漏句」疑点 1 句.*未处理 0 段", text)


def test_in_variant_c_the_missed_sentence_and_the_extra_segment_make_their_cue_a_doubt(tmp_path, monkeypatch):
    ctx, _cpa, out = _run(tmp_path, monkeypatch, "C")
    d2 = _doubts(ctx)[2]
    assert d2["id"] == "d002" and any(r.startswith("ASR 挂接") for r in d2["reasons"])
    assert _doubts(ctx)[1]["source"] == "NOT_SUSPICIOUS"     # an agreeing cue is still not listened to


def test_a_mai_segment_starting_before_its_cue_is_attached_by_overlap(tmp_path, monkeypatch):
    # starts 0.9 s before cue 3 (outside the old start +-0.5 s rule) and covers it
    ctx, _cpa, out = _run(tmp_path, monkeypatch, "full", mai=[(7.1, 9.9, "子丑寅卯辰")])
    assert _doubts(ctx)[3]["readings"]["asr:mai"] == "子丑寅卯辰"
    # the segment's end time reaches the attachment: it overlaps cue 3, so it is not a missed-sentence orphan
    line = next(x for x in out.process if x.startswith("ASR 段挂接（asr:mai）"))
    assert "共 1 段，已挂接 1 段（其中与任何句都不重叠、挂到最近句的 0 段" in line
    assert not _doubts(ctx)[3].get("asr_notes")


def test_a_listening_line_at_the_start_of_its_window_belongs_to_the_window_cue(tmp_path, monkeypatch):
    win = next(w for w in si.listen_windows([dict(id=i, start_s=a, end_s=b, text=t) for i, (a, b, t) in
                                             enumerate(BCUT, 1)], 70.0) if w["cue_ids"][0] == 3)
    assert win["audio"][0] < 8.0 - 0.5                       # the window's lead is longer than the old tolerance
    rec = {"win": win["win"], "model": si.M38["name"], "slug": "38m", "status": "OK",
           "lines": [{"start_s": 0.0, "text": "子丑寅卯辰"}]}
    ctx, _cpa, _out = _run(tmp_path, monkeypatch, "full", listen_records=[rec])
    assert _doubts(ctx)[3]["readings"]["audio:38m@P0"] == "子丑寅卯辰"


def test_a_danmaku_sent_12_s_after_the_sentence_is_attached_to_it(tmp_path, monkeypatch):
    ctx, _cpa, _out = _run(tmp_path, monkeypatch, "full", danmaku=[(16.0, "这句真离谱了")])
    assert any("这句真离谱了" in x for x in _doubts(ctx)[2]["danmaku"])   # cue 2 ends at 4.0 s


def test_a_danmaku_read_aloud_later_makes_the_cue_a_doubt_with_the_original_text(tmp_path, monkeypatch):
    # sent 40 s before the cue it is read in; the cue's text is close to it but not the same
    ctx, cpa, _out = _run(tmp_path, monkeypatch, "C", danmaku=[(-32.0, "子丑寅卯辰巳")])
    d3 = _doubts(ctx)[3]
    assert d3["id"] == "d003" and "子丑寅卯辰巳" in d3["read_aloud"]
    assert any(r.startswith("疑似念弹幕") for r in d3["reasons"])
