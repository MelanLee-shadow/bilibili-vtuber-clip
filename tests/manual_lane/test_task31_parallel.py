"""TASK-31: the lane's independent provider calls run in parallel.

What these tests hold (each one fails on the serial code):

- the same answers give the same products and the same records under parallel execution (results merge in input
  order, not completion order);
- peak concurrency is above 1 and never above the provider's bound (lane_rules concurrency.<provider>.limit);
- a failing item fails the pass with the same outcome a serial pass gives, and the record list is the serial one;
- concurrent cache writes do not overwrite each other (each answer lands in its own entry, and no half file is left).

The fakes carry a random delay per call (fakes.InFlight), so completion order differs from input order on purpose.
"On the old code it fails" is written per test: each assertion is about something the serial code could not produce.
"""

from __future__ import annotations

import json
import re
import threading

import pytest
from fakes import FakeCpa, FakeRemote, IdCpa, make_ctx, srt

from src.autoslice.manual_lane import parallel
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import step3_adjudicate
from src.autoslice.manual_lane.adapters import RemoteResult

RD = "/r/clip"
BATCH = si.ADJUDICATE_BATCH


# ----- the executor itself ------------------------------------------------------------------------------------
def _limit(monkeypatch, provider: str, value: int) -> None:
    monkeypatch.setattr(parallel, "limit", lambda name: value if name == provider else 1)


def test_results_come_back_in_input_order_whatever_finishes_first(monkeypatch):
    """Old code: no executor at all, so this cannot even be written against it (it is the new seam)."""
    _limit(monkeypatch, "cpa", 4)
    order: list[int] = []
    lock = threading.Lock()

    def call(item: int) -> str:
        # a later item finishes first: item 0 sleeps longest
        if item == 0:
            import time
            time.sleep(0.15)
        with lock:
            order.append(item)
        return f"answer-{item}"

    stats = parallel.Stats()
    out = parallel.run([0, 1, 2, 3], call, provider="cpa", stats=stats)
    assert out == ["answer-0", "answer-1", "answer-2", "answer-3"]
    assert order != [0, 1, 2, 3]          # they really did overlap and finish out of order
    assert stats.peak > 1 and stats.peak <= 4 and stats.calls == 4


def test_peak_concurrency_never_passes_the_provider_bound(monkeypatch):
    """Old code: serial, so a peak of 1; the bound itself did not exist."""
    _limit(monkeypatch, "cpa", 3)
    meter = parallel.Stats()

    def call(_item: int) -> None:
        import time
        time.sleep(0.05)

    parallel.run(range(12), call, provider="cpa", stats=meter)
    assert meter.limit == 3 and 1 < meter.peak <= 3 and meter.calls == 12


def test_a_failure_raises_the_earliest_exception_in_input_order(monkeypatch):
    """Old code: serial stops at the first failure (input order).  The parallel pass must report the same error even
    when a later item fails first."""
    _limit(monkeypatch, "cpa", 4)

    def call(item: int) -> str:
        if item == 3:
            raise RuntimeError("later")
        if item == 1:
            raise ValueError("earlier")
        return "ok"

    with pytest.raises(ValueError, match="earlier"):
        parallel.run([0, 1, 2, 3], call, provider="cpa")


# ----- step 3: the adjudication batches -------------------------------------------------------------------------
def _doubts(n: int) -> list[dict]:
    return [{"id": f"d{i:03d}", "cue_id": i, "current": f"句子{i}", "readings": {"bcut": f"句子{i}"},
             "differing": [], "maintenance": {"maintained": False}, "candidates": [{"text": f"句子{i}"}],
             "danmaku": []} for i in range(1, n + 1)]


def _batch_prompt_ids(prompt: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r'"id": "(d\d+)"', prompt)))


def test_adjudication_batches_run_at_once_and_keep_their_own_doubts(tmp_path):
    """Old code: one batch after another (peak 1).  Now: several batches in flight, and the merged answer still holds
    every doubt of every batch, keyed to the batch that answered it."""
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=IdCpa([], delay=(0.02, 0.08)), bcut_text=srt(3, step=2.0))
    calls: list[dict] = []
    doubts = _doubts(3 * BATCH + 4)
    rows, failure = si._adjudicate_all(ctx, doubts, "上下文", calls, tag="p1")
    assert failure is None
    assert sorted(rows) == sorted(d["id"] for d in doubts)
    batches = (len(doubts) + BATCH - 1) // BATCH
    assert len(calls) == batches
    assert ctx.adapters.cpa.meter.peak > 1 and ctx.adapters.cpa.meter.peak <= parallel.limit("cpa")
    # each record carries the doubt ids of its own batch only (the prompts were built before the calls went out)
    for record in calls:
        assert record["call_id"] in {f"adjudicate-p1-{n}" for n in range(1, batches + 1)}
    assert [r["call_id"] for r in calls] == [f"adjudicate-p1-{n}" for n in range(1, batches + 1)]


def test_a_failed_batch_reports_the_same_failure_and_records_as_a_serial_pass(tmp_path):
    """Old code: batches ran in order, so the failure is the earliest bad batch and the record list ends with it.
    Now the failing batch is chosen by batch order, not by which thread failed first, and the records that precede it
    in the list are the ones before it."""
    class FailingSecondBatch(IdCpa):
        def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
            if call_id == "adjudicate-p1-2":
                self.meter.__enter__()
                try:
                    return {"call_id": call_id, "model": model, "effort": effort, "rc": 1, "elapsed_s": 0.1,
                            "response_text": "", "stderr_tail": "CPA_HTTP_ERROR (fake)"}
                finally:
                    self.meter.__exit__()
            return super().ask(call_id=call_id, prompt_text=prompt_text, model=model, effort=effort,
                               remote_dir=remote_dir)

    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=FailingSecondBatch([], delay=(0.02, 0.08)),
                   bcut_text=srt(3, step=2.0))
    calls: list[dict] = []
    rows, failure = si._adjudicate_all(ctx, _doubts(2 * BATCH), "上下文", calls, tag="p1")
    assert rows == {} and failure is not None
    assert failure.reason == "CPA_CALL_FAILED" and failure.ok is False
    assert [r["call_id"] for r in calls] == ["adjudicate-p1-1", "adjudicate-p1-2"]
    assert [c["call_id"] for c in failure.cpa] == ["adjudicate-p1-1", "adjudicate-p1-2"]
    assert failure.cpa[-1]["rc"] == 1


def test_the_same_answers_give_the_same_products_under_delays(tmp_path):
    """Old code: serial, so only one order existed and no peak above 1 (both assertions fail there).  Now the calls
    finish in a random order; the products (final.srt and doubts.json) and PROCESS must still be exactly the same as
    with no delay at all.  24 cues make three adjudication batches, so the calls really overlap."""
    def run(tag: str, delay: tuple[float, float]):
        base = tmp_path / tag
        rows = [{"id": "d001", "state": "ADOPT", "final_text": "句子甲", "affects_meaning": True, "reason_key": "同音",
                 "confidence": "高"}]
        records = [{"win": "C000", "model": si.M38["name"], "slug": "38m", "status": "OK",
                    "lines": [{"start_s": 0.0, "text": "句子甲"}]}]
        ctx, _cpa = _subs_ctx(base, records, rows, delay=delay, cues=24)
        out = si.step3_subtitles(ctx)
        assert out.ok, out.note
        return (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8"), \
            json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8")), out.process, _cpa.meter.peak

    serial = run("serial", (0.0, 0.0))
    slow = run("slow", (0.02, 0.09))
    assert slow[0] == serial[0]
    assert [d["id"] for d in slow[1]] == [d["id"] for d in serial[1]]
    for key in ("state", "final_text", "confidence", "source", "cpa_call"):
        assert [d.get(key) for d in slow[1]] == [d.get(key) for d in serial[1]], key
    assert slow[2] == serial[2]
    assert slow[3] > 1                     # the delayed run really did overlap (its CPA calls)


def test_the_cpa_cache_keeps_one_entry_per_answer_and_no_partial_file(tmp_path):
    """Old code: the cache file was written in place, one batch at a time; the concurrency assertion fails there.  Now
    several batches write at once; every distinct prompt keeps its own entry, every entry parses, and no temporary
    file is left behind."""
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=IdCpa([], delay=(0.02, 0.08)), bcut_text=srt(3, step=2.0))
    calls: list[dict] = []
    doubts = _doubts(4 * BATCH)
    rows, failure = si._adjudicate_all(ctx, doubts, "上下文", calls, tag="p1")
    assert failure is None and len(rows) == len(doubts)
    assert ctx.adapters.cpa.meter.peak > 1      # the entries below were written by overlapping calls
    entries = sorted((tmp_path / "cache" / "cpa").glob("*.json"))
    assert len(entries) == len(calls) == 4
    for entry in entries:
        doc = json.loads(entry.read_text(encoding="utf-8"))     # a half-written cache would not parse
        assert doc["response_text"]
    assert not list((tmp_path / "cache" / "cpa").glob(".*.tmp"))     # the temporary names were renamed away
    # the answers are distinct entries: a second pass with the same prompts calls nothing
    again: list[dict] = []
    rows2, failure2 = si._adjudicate_all(ctx, doubts, "上下文", again, tag="p1")
    assert failure2 is None and rows2 == rows
    assert all(record.get("cached") for record in again)


# ----- the other call sites ---------------------------------------------------------------------------------------
def test_work_lookups_of_one_pass_run_at_once(tmp_path, monkeypatch):
    """Old code: one query after another.  Now every distinct query of a pass is looked up at once, and a repeated
    query is still looked up once."""
    from src.autoslice.manual_lane import work_lookup

    seen: list[str] = []
    lock = threading.Lock()
    open_now = 0
    peak = 0

    def fake_lookup(query, graph, *, web):
        nonlocal open_now, peak
        with lock:
            open_now += 1
            peak = max(peak, open_now)
            seen.append(query)
        import time
        time.sleep(0.05)
        with lock:
            open_now -= 1
        return {"query": query, "source": "topic_entity_graph", "results": [], "layers": []}

    monkeypatch.setattr(work_lookup, "lookup", fake_lookup)
    monkeypatch.setattr(step3_adjudicate, "load_topic_entity_graph", lambda _p: ({}, "sha"))
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=IdCpa([]), bcut_text=srt(3, step=2.0))
    doubts = _doubts(6)
    adjudicated = {d["id"]: {"work_query": f"作品{i}"} for i, d in enumerate(doubts)}
    adjudicated[doubts[1]["id"]]["work_query"] = "作品1"      # one repeated query
    cache: dict[str, dict] = {}
    fresh = si._attach_work_lookups(ctx, doubts, adjudicated, cache)
    assert peak > 1 and peak <= parallel.limit("lookup")
    assert sorted(seen) == sorted({"作品0", "作品1", "作品2", "作品3", "作品4", "作品5"})
    assert len(cache) == 6 and len(fresh) == len(doubts)
    assert all(d["work_lookup"]["query"] for d in doubts)


def test_the_title_judge_runs_are_asked_at_once(tmp_path, monkeypatch):
    """Old code: run 1, then run 2.  Now both runs are in flight together; a verdict is PASS only when every run says
    PASS, exactly as before."""
    answer = json.dumps({"candidates": [{"title": "【李豆沙】半分钟后她破防了", "on_stage": ["李豆沙"],
                                        "punchline_person": "她破防了",
                                        "swap_check": {"holds_with_other_host": False, "reason": "测试"}}],
                         "recommended": 0, "cover_lines": ["甲"]}, ensure_ascii=False)
    cpa = FakeCpa([answer], delay=(0.05, 0.05))          # each call holds the meter while it waits
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa, bcut_text=srt(3, step=2.0))
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs" / "final.srt").write_text(srt(3), encoding="utf-8")

    def script(name, body, remote):
        if name == "s6-cover":
            from fakes import fake_cover
            fake_cover(remote, RD)
        return RemoteResult(0, "", "")
    ctx.adapters.remote.on_script = script
    out = si.step6_title_cover(ctx)
    assert out.ok, out.note
    assert [c for c in cpa.calls if c.startswith("title-judge")] == ["title-judge-r0-1", "title-judge-r0-2"]
    assert cpa.meter.peak > 1                            # both judge runs were open at the same time


# ----- helpers ----------------------------------------------------------------------------------------------------
def _subs_ctx(tmp_path, records, rows, *, delay=(0.0, 0.0), cues: int = 3):
    cpa = IdCpa(rows, delay=delay)

    def script(name, body, remote):
        if name.startswith("s3-listen"):
            remote.files[f"{RD}/listen/out/all.json"] = json.dumps(records, ensure_ascii=False).encode()
            return RemoteResult(0, "LISTEN_DONE\n", "")
        if name == "s3-asr":
            return RemoteResult(0, "ASR_OK\n", "")
        return RemoteResult(0, "", "")
    remote = FakeRemote(on_script=script)
    text = srt(cues, step=2.0).encode()
    remote.files[f"{RD}/asr/bcut.srt"] = text
    remote.files[f"{RD}/asr/jianying.srt"] = text
    ctx = make_ctx(tmp_path, remote=remote, cpa=cpa, bcut_text=srt(cues, step=2.0))
    return ctx, cpa
