"""Variant C (TASK-5, U119): listen only the doubt windows, escalate only while a doubt is unsettled, never block.

- a cue with no doubt is never sent to the listening model;
- a doubt settled at the first level is not escalated;
- a doubt unsettled at both levels (3.8, argon) goes to CPA's best reading, marked low confidence, and is not blocked;
- the windows are the reference round's per-cue windows (same names, audio ranges and tempo), so the cache is hit.
"""

from __future__ import annotations

import json
import re

from fakes import FakeRemote, make_ctx, srt

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import RemoteResult

RD = "/r/clip"
CUT_LEN = 70.0   # make_ctx: clip 10 s .. 80 s


class CpaFake:
    """Answers the review call with `review`, and each adjudication call from `rows` (by call id) or KEEP per doubt id."""

    def __init__(self, review, rows=None):
        self.review = json.dumps(review, ensure_ascii=False)
        self.rows = rows or {}
        self.calls: list[str] = []
        self.prompts: list[str] = []

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
        self.calls.append(call_id)
        self.prompts.append(prompt_text)
        if call_id == "review-c":
            text = self.review
        elif call_id in self.rows:
            text = json.dumps(self.rows[call_id], ensure_ascii=False)
        else:
            ids = list(dict.fromkeys(re.findall(r'"id": "([^"]+)"', prompt_text)))
            text = json.dumps([{"id": i, "state": "KEEP", "final_text": "", "confidence": "高",
                                "affects_meaning": False, "basis": "默认保留"} for i in ids], ensure_ascii=False)
        return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                "response_text": text, "stderr_tail": ""}


def _remote(records_by_label, scripts):
    """records_by_label: label -> the cumulative all.json records the remote tool would hold after that pass."""
    def script(name, body, remote):
        if name == "s3-asr":
            return RemoteResult(0, "ASR_OK\n", "")
        if name.startswith("s3-listen-"):
            label = name[len("s3-listen-"):]
            scripts.append(label)
            remote.files[f"{RD}/listen/out/all.json"] = json.dumps(records_by_label.get(label, []),
                                                                  ensure_ascii=False).encode()
            return RemoteResult(0, 'LISTEN_DONE 1 records 1 {"OK": 1}\n', "")
        return RemoteResult(0, "", "")
    remote = FakeRemote(on_script=script)
    text = srt(3, step=2.0).encode()
    remote.files[f"{RD}/asr/bcut.srt"] = text
    remote.files[f"{RD}/asr/jianying.srt"] = text
    return remote


def _rec(win, slug, text):
    """A record as the remote tool writes it: it carries the window and the model NAME (the scope key)."""
    name = {m["slug"]: m["name"] for m in si.LISTEN_MODELS}[slug]
    return {"win": win, "model": name, "slug": slug, "status": "OK", "lines": [{"start_s": 0.0, "text": text}]}


def _ctx(tmp_path, monkeypatch, *, review, rows, records_by_label, scripts):
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "C")
    cpa = CpaFake(review, rows)
    ctx = make_ctx(tmp_path, remote=_remote(records_by_label, scripts), cpa=cpa, bcut_text=srt(3, step=2.0))
    return ctx, cpa


def _suspicious(*cue_ids):
    return {"suspicious": [{"cue_id": c, "reason": "读不通"} for c in cue_ids]}


def test_a_cue_with_no_doubt_is_never_sent_to_the_listening_model(tmp_path, monkeypatch):
    scripts: list[str] = []
    records = {"C-L1": [_rec("C000", "38m", "句子1")]}
    ctx, cpa = _ctx(tmp_path, monkeypatch, review=_suspicious(1), records_by_label=records, scripts=scripts,
                    rows={"adjudicate-c-L1-1": [{"id": "d001", "state": "KEEP", "final_text": "", "confidence": "高",
                                                 "affects_meaning": False}]})
    out = si.step3_subtitles(ctx)
    assert out.ok, out.note
    assert scripts == ["C-L1"]
    windows = json.loads((ctx.clip_dir / "listen" / "windows-C-L1.json").read_text(encoding="utf-8"))
    assert [w["win"] for w in windows] == ["C000"]      # cues 2 and 3 are not listened to


def test_a_doubt_settled_at_the_first_level_is_not_escalated(tmp_path, monkeypatch):
    scripts: list[str] = []
    records = {"C-L1": [_rec("C000", "38m", "句子甲")]}
    ctx, cpa = _ctx(tmp_path, monkeypatch, review=_suspicious(1), records_by_label=records, scripts=scripts,
                    rows={"adjudicate-c-L1-1": [{"id": "d001", "state": "ADOPT", "final_text": "句子甲",
                                                 "confidence": "高", "affects_meaning": True, "reason_key": "同音",
                                                 "basis": "3.8 与上下文一致"}]})
    out = si.step3_subtitles(ctx)
    assert out.ok, out.note
    assert scripts == ["C-L1"]                          # no L2 or L3 listening pass
    assert not any(c.startswith("adjudicate-c-L2") or c.startswith("adjudicate-c-L3") for c in cpa.calls)
    doubt = json.loads((ctx.clip_dir / "subs" / "doubts.json").read_text(encoding="utf-8"))
    d1 = next(d for d in doubt if d["cue_id"] == 1)
    assert d1["settled_at_level"] == "L1" and d1["final_text"] == "句子甲" and d1["unsettled"] is False


def test_a_doubt_unsettled_at_both_levels_goes_to_cpa_as_low_confidence_and_is_not_blocked(tmp_path, monkeypatch):
    scripts: list[str] = []
    low = {"id": "d001", "state": "FIT_CONTEXT", "final_text": "句子丙", "confidence": "低", "affects_meaning": True,
           "reason_key": "数字", "basis": "两级都定不下，给最可能的写法"}
    records = {
        "C-L1": [_rec("C000", "38m", "句子甲")],
        "C-L2": [_rec("C000", "38m", "句子甲"), _rec("C000", "argon", "句子丙")],
    }
    ctx, cpa = _ctx(tmp_path, monkeypatch, review=_suspicious(1), records_by_label=records, scripts=scripts,
                    rows={"adjudicate-c-L1-1": [dict(low, state="UNRESOLVED", final_text="")],
                          "adjudicate-c-L2-1": [low]})
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)               # not blocked
    assert scripts == ["C-L1", "C-L2"]                  # two levels (3.8, argon), each for the still-open doubt
    assert not any(line.startswith("听写 C-L3") for line in out.process)
    doubts = json.loads((ctx.clip_dir / "subs" / "doubts.json").read_text(encoding="utf-8"))
    d1 = next(d for d in doubts if d["cue_id"] == 1)
    assert d1["confidence"] == "低" and d1["unsettled"] is True and d1["final_text"] == "句子丙"
    assert any("未定（已到上限，只记录，不拦截）" in line for line in out.process)


def test_variant_c_windows_are_the_reference_round_per_cue_windows(tmp_path, monkeypatch):
    scripts: list[str] = []
    ctx, _ = _ctx(tmp_path, monkeypatch, review=_suspicious(2), records_by_label={"C-L1": []}, scripts=scripts,
                  rows={})
    si.step3_subtitles(ctx)
    windows = json.loads((ctx.clip_dir / "listen" / "windows-C-L1.json").read_text(encoding="utf-8"))
    reference = si.listen_windows(si.parse_srt(srt(3, step=2.0)), CUT_LEN)
    ref = {w["win"]: w for w in reference}
    assert [w["win"] for w in windows] == ["C001"]
    assert windows[0] == ref["C001"]                    # same name, audio range, core and tempo: the cache is hit


def test_variant_c_reports_the_listening_budget_and_the_usage_line_in_process(tmp_path, monkeypatch):
    scripts: list[str] = []
    records = {"C-L1": [_rec("C000", "38m", "句子甲")]}
    ctx, _ = _ctx(tmp_path, monkeypatch, review=_suspicious(1), records_by_label=records, scripts=scripts,
                  rows={"adjudicate-c-L1-1": [{"id": "d001", "state": "ADOPT", "final_text": "句子甲", "confidence": "高",
                                               "affects_meaning": True, "reason_key": "同音", "basis": "x"}]})
    out = si.step3_subtitles(ctx)
    text = "\n".join(out.process)
    assert "疑点检测" in text and "软预算（只记账）" in text and "听写 L1" in text
    assert "升级计划（gemini）：L1 Gemini 3.8 Flash (Low)；L2 gemini-4-argon" in text and "3.6" not in text and "3.7" not in text
    assert any(line.startswith("Gemini 用量") or "用量统计失败" in line for line in out.process)


def test_every_level_looks_up_the_work_cpa_named_and_asks_again_with_it(tmp_path, monkeypatch):
    scripts: list[str] = []
    asked = {"adjudicate-c-L1-1": [{"id": "d001", "state": "FIT_CONTEXT", "final_text": "句子乙", "confidence": "低",
                                    "work_query": "某作品", "affects_meaning": True, "reason": "像在说某作品",
                                    "basis": "上下文"}],
             "adjudicate-c-L1-lookup-1": [{"id": "d001", "state": "FIT_CONTEXT", "final_text": "句子乙", "confidence": "低",
                                           "work_query": "某作品", "affects_meaning": True, "basis": "查不到"}],
             "adjudicate-c-L2-1": [{"id": "d001", "state": "FIT_CONTEXT", "final_text": "句子乙", "confidence": "低",
                                    "work_query": "另一作品", "affects_meaning": True, "basis": "x"}]}
    ctx, cpa = _ctx(tmp_path, monkeypatch, review=_suspicious(1), rows=asked, scripts=scripts,
                    records_by_label={"C-L1": [_rec("C000", "38m", "句子甲")], "C-L2": [_rec("C000", "argon", "句子乙")]})
    out = si.step3_subtitles(ctx)
    assert out.ok, out.note
    # L1 named a work: it was looked up (no network adapter in tests: every layer fails and is recorded) and the doubt
    # was asked again with the lookup; L2 named another work: looked up and asked again too
    assert "adjudicate-c-L1-lookup-1" in cpa.calls and "adjudicate-c-L2-lookup-1" in cpa.calls
    again = cpa.prompts[cpa.calls.index("adjudicate-c-L1-lookup-1")]
    assert '"work_lookup"' in again and "某作品" in again and '"first_pass"' in again
    assert any(line.startswith("作品查证「某作品」") for line in out.process)
    assert any(line.startswith("作品查证「另一作品」") for line in out.process)


def test_the_second_level_sees_the_first_levels_answer_and_reasons(tmp_path, monkeypatch):
    scripts: list[str] = []
    l1 = {"id": "d001", "state": "FIT_CONTEXT", "final_text": "句子乙", "confidence": "低", "affects_meaning": True,
          "reason": "第一级的理由", "basis": "第一级的依据"}
    ctx, cpa = _ctx(tmp_path, monkeypatch, review=_suspicious(1), scripts=scripts,
                    rows={"adjudicate-c-L1-1": [l1], "adjudicate-c-L2-1": [dict(l1, confidence="高")]},
                    records_by_label={"C-L1": [_rec("C000", "38m", "句子甲")], "C-L2": [_rec("C000", "argon", "句子乙")]})
    assert si.step3_subtitles(ctx).ok
    l2 = cpa.prompts[cpa.calls.index("adjudicate-c-L2-1")]
    fp = json.loads(re.search(r'"first_pass": (\{.*?\})', l2, re.DOTALL).group(1))
    assert fp["final_text"] == "句子乙" and fp["confidence"] == "低" and fp["reason"] == "第一级的理由"
    assert fp["basis"] == "第一级的依据"
