"""Working decisions travel between C levels without becoming another evidence source."""

from __future__ import annotations

import json
import threading

from fakes import FakeRemote, IdCpa, make_ctx

from src.autoslice.manual_lane import evidence_attach, parallel
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import step3_adjudicate, variant_c


DOUBTS = "输入疑点（含候选读法及其支持来源、各来源读法与弹幕线索）：\n"
CONTEXT = "\n\n全片字幕（序号 时间 文字）：\n"
WORKING = "\n\n【先前裁决工作稿】\n"


def _parts(prompt):
    doubts, context = prompt.split(DOUBTS, 1)[1].split(CONTEXT, 1)
    raw, separator, working = context.partition(WORKING)
    return json.loads(doubts), raw if separator else raw.removesuffix("\n"), json.loads(working) if separator else None


class _Answers:
    """Offline CPA fake: two ordinary L2 batches must actually overlap."""

    def __init__(self):
        self.prompts = {}
        self.lock = threading.Lock()
        self.l2_barrier = threading.Barrier(2, timeout=5)

    def ask(self, ctx, call_id, prompt_text, *, accepts=None, **_kwargs):
        if call_id in {"adjudicate-c-L2-1", "adjudicate-c-L2-2"}:
            self.l2_barrier.wait()
        doubts, _, _ = _parts(prompt_text)
        rows = []
        for doubt in doubts:
            did = doubt["id"]
            row = {"id": did, "state": "KEEP", "final_text": "", "confidence": "高",
                   "affects_meaning": False}
            if call_id.startswith("adjudicate-c-L1-"):
                if did == "d001":
                    row.update(state="ADOPT", final_text="第一句修订正文", other_speaker_text="另一人简短回话", drop=False)
                elif did == "d002":
                    row.update(state="ADOPT", final_text="第二句暂定正文")
                elif did == "d003":
                    row.update(state="ADOPT", final_text="哈哈", drop=True)
            elif call_id.startswith("adjudicate-c-L2-lookup-") and did == "d002":
                row.update(state="ADOPT", final_text="查证后选定正文")
            elif call_id.startswith("adjudicate-c-L2-") and did == "d002":
                row.update(state="ADOPT", final_text="查证前候选正文", work_query="合成作品")
            rows.append(row)
        assert accepts is None or accepts(rows) == ""
        with self.lock:
            self.prompts[call_id] = prompt_text
        return {"call_id": call_id, "rc": 0, "response_text": json.dumps(rows, ensure_ascii=False), "file": "fake"}


def _lookup(_ctx, doubts, answers, _cache):
    fresh = []
    for doubt in doubts:
        query = answers[doubt["id"]].get("work_query")
        if query:
            doubt["work_lookup"] = {"query": query, "source": "synthetic"}
            fresh.append(doubt["id"])
    return fresh


def _evidence(cue, *_args):
    readings = {"bcut": cue["text"]}
    if cue["id"] in {1, 3}:
        readings["audio:38m@P0"] = "合成听音结果"
    return {"readings": readings, "danmaku": []}


def test_prior_decisions_reach_later_parallel_and_lookup_prompts_without_changing_decisions(tmp_path, monkeypatch):
    monkeypatch.setattr(parallel, "limit", lambda _provider: 2)
    monkeypatch.setattr(si, "_evidence_for_cue", _evidence)
    answers = _Answers()
    monkeypatch.setattr(step3_adjudicate, "_ask_cpa", answers.ask)

    cues = [{"id": n, "start_s": float(2 * (n - 1)), "end_s": float(2 * n), "text": f"原稿第{n}句"}
            for n in range(1, 16)]
    original = [dict(cue) for cue in cues]
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=IdCpa(),
                   session={"participants": [["甲"]], "speaker_mode": "uniform_host"})
    att = evidence_attach.attach_asr(cues, [], source="jianying")
    det = variant_c._Detection(by_id={c["id"]: c for c in cues},
                               full_source_context=variant_c._full_source_context(cues, [], []),
                               asr_lines={}, danmaku=[], jy_att=att,
                               witness_att={}, jy_by_cue={c["id"]: "" for c in cues}, mixed={},
                               reasons={c["id"]: ["合成疑点"] for c in cues})
    esc = variant_c._Escalation(pending=set(det.by_id))
    def lookup_and_change_source(*args):
        result = _lookup(*args)
        if result:
            esc.lines_by_source["audio:38m@P0"][0]["text"] = "查证期间才变的内容"
        return result
    monkeypatch.setattr(si, "_attach_work_lookups", lookup_and_change_source)
    calls = []
    clip = ctx.state.data["clip"]

    assert variant_c._settle_level(ctx, si, det, esc, "L1", "first", cues, clip, calls, heard=("38m",)) is None
    assert 1 not in esc.pending and 3 not in esc.pending
    assert 2 in esc.pending and esc.latest[2]["decided"]["confidence"] == "高"
    assert all(_parts(prompt)[2] is None for prompt in answers.prompts.values())

    # Snapshot order must follow source cues, not insertion order of the internal dictionary.
    esc.latest = dict(reversed(list(esc.latest.items())))
    esc.lines_by_source = {"audio:38m@P0": [{"abs_start_s": 20.0, "text": "本级进入时已听",
                                              "owners": [11]}]}
    assert variant_c._settle_level(ctx, si, det, esc, "L2", "second", cues, clip, calls) is None
    expected_calls = ["adjudicate-c-L1-1", "adjudicate-c-L1-2", "adjudicate-c-L2-1", "adjudicate-c-L2-2",
                      "adjudicate-c-L2-lookup-1"]
    assert [call["call_id"] for call in calls] == expected_calls
    assert set(answers.prompts) == set(expected_calls)
    parsed = [_parts(answers.prompts[name]) for name in expected_calls[2:]]
    expected_context = (variant_c._full_source_context(original, [], [])
                        + variant_c._primary_witness_context({}, {"audio:38m@P0": [
                            {"abs_start_s": 20.0, "text": "本级进入时已听", "owners": [11]}]}))
    assert all(raw == expected_context for _, raw, _ in parsed)
    assert esc.lines_by_source["audio:38m@P0"][0]["text"] == "查证期间才变的内容"
    assert cues == original
    snapshots = [snapshot for _, _, snapshot in parsed]
    assert snapshots[0] is not None
    assert snapshots[0] == snapshots[1] == snapshots[2]
    assert [row["cue_id"] for row in snapshots[0]] == list(range(1, 16))
    first, provisional, dropped = snapshots[0][:3]
    assert first == {"cue_id": 1, "start_s": 0.0, "end_s": 2.0, "source_text": "原稿第1句", "level": "L1",
                     "status": "settled", "state": "ADOPT", "final_text": "第一句修订正文", "dropped": False,
                     "other_speaker_text": "另一人简短回话", "drop": False}
    assert provisional["cue_id"] == 2 and provisional["status"] == "provisional"
    assert provisional["state"] == "ADOPT" and provisional["final_text"] == "第二句暂定正文"
    assert dropped["cue_id"] == 3 and dropped["status"] == "settled"
    assert dropped["drop"] is True and dropped["dropped"] is True and dropped["final_text"] == ""
    second_doubts = parsed[0][0] + parsed[1][0]
    assert {row["cue_id"] for row in second_doubts} == set(range(2, 16)) - {3}
    assert all(set(row["readings"]) == {"bcut"} for row in second_doubts)
    lookup_doubt = parsed[2][0][0]
    assert lookup_doubt["work_lookup"]["query"] == "合成作品"
    assert lookup_doubt["first_pass"]["final_text"] == "查证前候选正文"
    assert esc.latest[2]["decided"]["final_text"] == "查证后选定正文"
    assert not esc.pending

    final = variant_c._assemble(ctx, si, det, esc, {}, [])
    _, path, _ = si.write_subtitles(ctx, cues, final)
    text = path.read_text(encoding="utf-8")
    assert "第一句修订正文" in text and "另一人简短回话" in text and "查证后选定正文" in text
    assert "原稿第3句" not in text and "哈哈" not in text
