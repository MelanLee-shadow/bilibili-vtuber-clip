"""Several independent agent runs and the per-cue majority (playbook_vote, playbook_step3.run).  Fakes only."""
import copy
import json
import threading

import pytest

from agent_fakes import AgentCpa, answer_rows
from fakes import FakeRemote, make_ctx
from src.autoslice.manual_lane import playbook_step3 as pb, playbook_vote as pv, step3_subtitles as step
from src.autoslice.manual_lane.lane_rules import rules


def r(cue, text, drop=False, **more):
    return dict({"id": f"d{cue:03d}", "final_text": text, "drop": drop, "state": "ADOPT", "reason": "证据"}, **more)


def texts(rows):
    return [x["final_text"] for x in rows]


def test_the_majority_wording_is_written_into_the_lead_whatever_the_sentence_cuts():
    a = [r(1, "我觉得，我觉得"), r(2, "还有点太那个了"), r(3, "我不太能接受")]
    b = [r(1, "我觉得，我觉得，还是有点太那个了"), r(2, "", drop=True), r(3, "我不太能接受")]     # another cut
    c = [r(1, "我觉得我觉得"), r(2, "还是有点太那个了"), r(3, "我不太能接受。")]                  # other punctuation
    rows, record = pv.merge([a, b, c])
    assert record["lead"] == 1 and texts(rows) == texts(b) and record["applied"] == []          # b and c say the same
    rows, record = pv.merge([a, c, b])
    assert record["lead"] == 1 and texts(rows) == texts(c)
    first = [r(1, "我觉得，我觉得"), r(2, "还有点太那个了", state="KEEP"), r(3, "我不太能接受")]
    rows, record = pv.merge([first, [r(1, "我觉得我觉得"), r(2, "还是有点太那个了"), r(3, "我不太能接受")],
                             [r(1, "我觉得，我觉得"), r(2, "还是有点太这个了"), r(3, "我不太能接受")]])
    assert record["lead"] == 1 and record["applied"] == []          # the lead already holds the majority at both places
    one = [r(1, "甲乙丙丁戊"), r(2, "己庚辛壬癸，子丑", state="KEEP"), r(3, "寅卯辰巳")]
    two = [r(1, "甲乙丙丁戊己庚新壬癸"), r(2, "子丑"), r(3, "寅卯辰巳")]
    three = [r(1, "甲乙丙丁戊"), r(2, "己庚新壬癸子丑寅卯"), r(3, "辰巳")]
    rows, record = pv.merge([one, two, three])                        # whichever leads, the text is the majority's
    assert pv.spoken(rows) == "甲乙丙丁戊己庚新壬癸子丑寅卯辰巳"
    rows, record = pv.merge([one, [r(1, "甲乙丙丁戊"), r(2, "己庚新壬癸，子丑"), r(3, "寅卯辰午")],
                             [r(1, "甲乙丙丁戊"), r(2, "己庚新壬癸，子丑"), r(3, "寅卯辰未")]])
    assert record["lead"] == 1 and texts(rows) == ["甲乙丙丁戊", "己庚新壬癸，子丑", "寅卯辰午"]   # no majority on the last


def test_a_patch_keeps_punctuation_times_and_parts_and_a_kept_row_is_no_longer_kept():
    lead = [r(1, "今天，我们去立街吧！", state="KEEP", parts=[{"text": "今天，", "first_word": 0, "last_word": 1},
                                                    {"text": "我们去立街吧！", "first_word": 2, "last_word": 6}]),
            r(2, "好的没问题")]
    other = [r(1, "今天我们去梨街吧"), r(2, "好的没问题")]
    rows, record = pv.merge([lead, other, copy.deepcopy(other)])
    assert record["lead"] in (1, 2)                                   # the two equal runs lead; nothing to patch
    rows, record = pv.merge([lead, [r(1, "今天，我们去梨街吧"), r(2, "好的没毛病")], [r(1, "今天我们去梨街吧"), r(2, "行吧没问题")]])
    assert record["lead"] == 0 and texts(rows) == ["今天，我们去梨街吧！", "好的没问题"]
    assert rows[0]["parts"][1] == {"text": "我们去梨街吧！", "first_word": 2, "last_word": 6}
    assert rows[0]["state"] == "ADOPT" and "「立」→「梨」" in rows[0]["reason"]
    assert record["applied"] == [{"from": "立", "to": "梨", "runs": 2, "id": "d001"}]
    assert lead[0]["final_text"] == "今天，我们去立街吧！" and lead[0]["state"] == "KEEP"       # the input is not touched


def test_what_cannot_be_written_in_is_recorded_and_left_to_the_lead():
    long = "这一整句只有另外两遍写了出来"
    x, y = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳", "一二三四五六七八九十百千万亿兆京"
    still = "中间有一句三遍都一样的话"
    lead = [r(1, "开头"), r(2, still), r(3, x), r(4, y), r(5, "结尾")]
    rows, record = pv.merge([lead, [r(1, "开头" + long), r(2, still), r(3, x[::-1]), r(4, y), r(5, "结尾")],
                             [r(1, "开头"), r(2, long + still), r(3, x), r(4, y[::-1]), r(5, "结尾")]])
    assert record["lead"] == 0 and texts(rows) == texts(lead)
    assert [(u["to"], u["why"]) for u in record["unapplied"]] == [(long, "longer than a wording")]
    rows, record = pv.merge([[r(1, "甲"), r(2, "乙")], [r(1, "甲"), r(2, "丙")]])
    assert texts(rows) == ["甲", "乙"] and record["applied"] == []     # two runs: no majority
    only = [r(1, "丁")]
    rows, record = pv.merge([only, [r(1, "戊己")], [r(1, "戊己")]])
    assert record["lead"] in (1, 2) and texts(rows) == ["戊己"]


class _TaggedCpa(AgentCpa):
    """Each run (its call ids carry its tag) gets its own scripted conversation."""

    def __init__(self, finals):
        super().__init__([])
        self.finals, self.turn, self.calls_by_tag, self.lock = finals, {}, {}, threading.Lock()

    def ask(self, *, call_id, **kwargs):
        with self.lock:                       # the runs ask from their own threads; the scripted fake is one object
            tag = call_id.split("-")[0][len("agent"):]
            n = self.turn[tag] = self.turn.get(tag, 0) + 1
            self.calls_by_tag.setdefault(tag, []).append(call_id)
            rows = answer_rows([{"id": "d001", "state": "ADOPT", "final_text": self.finals[tag], "confidence": "高",
                                 "affects_meaning": False}])
            answer = [{"action": "plan", "doubts": [], "tools": []}, {"action": "draft", "rows": rows},
                      {"action": "final", "self_check": "checked", "rows": rows}][n - 1]
            self.responses = [json.dumps(answer, ensure_ascii=False)]
            return super().ask(call_id=call_id, **kwargs)


def _clip(tmp_path, monkeypatch, cpa):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    ctx.session["participants"] = [["single speaker"]]
    for name, text in [("bcut", "原句"), ("jianying", "另一句")]:
        path = ctx.clip_dir / f"asr/{name}.srt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"1\n00:00:00,000 --> 00:00:02,000\n{text}\n")
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "C")
    monkeypatch.setattr(pb, "samples", lambda: 3)
    return ctx


def test_three_runs_decide_a_clip_and_every_run_keeps_its_own_records(tmp_path, monkeypatch):
    cpa = _TaggedCpa({"": "少数写法", "b": "多数写法", "c": "多数写法"})
    ctx = _clip(tmp_path, monkeypatch, cpa)
    out = step._step3_body(ctx)
    assert out.ok, (out.reason, out.note)
    assert "多数写法" in (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")
    assert {t: len(c) for t, c in cpa.calls_by_tag.items()} == {"": 3, "b": 3, "c": 3}
    for name in ("agent-trace.json", "agent-trace.b.json", "agent-trace.c.json"):
        assert json.loads((ctx.clip_dir / "subs" / name).read_text(encoding="utf-8"))["status"] == "COMPLETE"
    vote = json.loads((ctx.clip_dir / "subs/agent-vote.json").read_text(encoding="utf-8"))
    assert vote["samples"] == 3 and vote["lead"] in ("b", "c") and len(vote["runs"]) == 3
    assert sorted(c["call_id"] for c in out.cpa) == sorted(
        f"agent{tag}-{n:03d}" for tag in ("", "b", "c") for n in (1, 2, 3)), [c.get("call_id") for c in out.cpa]
    assert any("逐字取多数" in line for line in out.process)


def test_a_second_attempt_of_the_step_reuses_the_finished_runs_without_asking(tmp_path, monkeypatch):
    cpa = _TaggedCpa({"": "少数写法", "b": "多数写法", "c": "多数写法"})
    ctx = _clip(tmp_path, monkeypatch, cpa)
    assert step._step3_body(ctx).ok
    first = (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")
    (ctx.clip_dir / "subs/final.srt").unlink()
    asked = sum(len(c) for c in cpa.calls_by_tag.values())
    out = step._step3_body(ctx)
    assert out.ok and (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8") == first
    assert sum(len(c) for c in cpa.calls_by_tag.values()) == asked


def test_a_run_that_fails_does_not_stop_the_clip(tmp_path, monkeypatch):
    class OneFails(_TaggedCpa):
        def ask(self, *, call_id, **kwargs):
            if call_id.startswith("agentb-"):
                return {"call_id": call_id, "model": kwargs["model"], "effort": kwargs["effort"], "rc": 1,
                        "complete": False, "elapsed_s": 0.1, "response_text": "", "usage": None, "attempts": 1,
                        "stderr_tail": "CPA_HTTP_ERROR"}
            return super().ask(call_id=call_id, **kwargs)

    ctx = _clip(tmp_path, monkeypatch, OneFails({"": "甲写法", "b": "", "c": "甲写法"}))
    out = step._step3_body(ctx)
    assert out.ok and "甲写法" in (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")
    vote = json.loads((ctx.clip_dir / "subs/agent-vote.json").read_text(encoding="utf-8"))
    assert [run["status"] for run in vote["runs"]] == ["COMPLETE", "REFUSED", "COMPLETE"]


def test_the_shipped_rules_run_several_samples():
    assert int(rules("subtitle_agent")["samples"]) >= 3 and int(rules("concurrency")["subtitle_agent"]["limit"]) >= 3


def test_every_sentence_carries_how_many_runs_wrote_it_and_the_riskiest_are_listed():
    a = [r(1, "甲乙丙丁", state="KEEP", confidence="高"), r(2, "戊己庚辛", confidence="低"), r(3, "壬癸子丑", confidence="低")]
    b = [r(1, "甲乙丙丁", confidence="高"), r(2, "戊己庚新", confidence="低"), r(3, "壬癸子寅", confidence="低")]
    c = [r(1, "甲乙丙丁", confidence="高"), r(2, "戊己庚辛", confidence="高"), r(3, "壬癸子卯", confidence="低")]
    rows, _record = pv.merge([a, b, c])
    assert [x["runs_same"] for x in rows] == [3, 2, 1]               # all three, two of three, nobody else
    assert [pv.risk(x) for x in rows] == [0 if rows[0]["state"] == "KEEP" else 1, 4, 5]
    line = pv.risk_lines(rows)
    assert len(line) == 1 and line[0].index("d003") < line[0].index("d002") and "d001" not in line[0]
    rows, _record = pv.merge([a, b])                                  # fewer than three runs: no majority, no count
    assert all("runs_same" not in x for x in rows) and pv.risk_lines([r(1, "甲", state="KEEP", confidence="高")]) == []
