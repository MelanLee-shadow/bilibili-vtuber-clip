"""TASK-35: a CPA answer that was cut short is not a smaller answer.

What these tests hold (the first four fail on 0a952e20a):

- `_json_from_text` does not fall back to an object inside an unfinished value: a cut array raises a truncated-answer
  error and that error is distinguishable from "no JSON here" (TASK-32b §4.1: ten-doubt batches came back as 7 and
  5 rows);
- the step-3 adjudication asks again when the answer to a batch is cut, the second (whole) answer decides the doubts,
  and the cut answer is not in the cache;
- when every try is cut, step 3 stops as a tool failure — no UNRESOLVED doubt, no doubts.json;
- controls: a whole answer, and a whole answer with prose or a code fence around it, parse as before.

Every error here is checked by class name rather than by importing the class: the guard tests have to run against the
pre-TASK-35 parser too, where neither class exists, so that their failure shows what the old parser did instead of an
import error for the whole file.

No ssh, no ffmpeg, no network, no paid model: the fakes of tests/manual_lane/fakes.py answer the calls.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest
from fakes import FakeRemote, IdCpa, make_ctx, srt

from src.autoslice.manual_lane import adapters, srt_text
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import RemoteResult

RD = "/r/clip"
TRIES = 4     # what cpa_text.py retries with (its --tries default); TASK-35 reuses that number, no second one


# ----- the two cut shapes TASK-32b found on OCI3 ---------------------------------------------------------------
def cut_array(ids, rows=2):
    """A batch answer cut mid-string after `rows` whole rows: the outer `[` never closes (1323 L1-2 / L2-4)."""
    body = ",\n".join(json.dumps({"id": d, "state": "KEEP", "final_text": "", "confidence": "高",
                                  "affects_meaning": False, "reason_key": "同音", "basis": "默认保留"},
                                 ensure_ascii=False) for d in ids[:rows])
    return "[\n  " + body + ',\n  {\n    "id": "' + ids[rows] if len(ids) > rows else "[\n  " + body


CUT_OBJECT = '{\n  "id": "d002",\n  "state": "KEEP",\n  "final_text": "来一个",\n  "reason_key": "同'


def whole(ids):
    return json.dumps([{"id": d, "state": "KEEP", "final_text": "", "confidence": "高", "affects_meaning": False,
                        "reason_key": "同音", "basis": "默认保留"} for d in ids], ensure_ascii=False)


def error_kind(text):
    """What `_json_from_text` says about `text`: (value, None) when it parses, else (None, the error's class name)."""
    try:
        return srt_text._json_from_text(text), None
    except ValueError as exc:
        return None, type(exc).__name__


# ----- the parser ---------------------------------------------------------------------------------------------
def test_a_cut_array_does_not_come_back_as_its_first_object():
    """0a952e20a: the `[` fails to decode, so it restarts at the first `{` and returns that one row — a ten-doubt
    batch is read as a two-row answer."""
    cut = cut_array(["d016", "d017", "d018", "d019"], rows=2)
    value, kind = error_kind(cut)
    assert value is None, f"a cut answer was returned as {value!r}"
    assert kind == "JSONTruncated", f"a cut answer was reported as {kind}"


def test_a_cut_object_is_not_reported_as_no_json_at_all():
    """0a952e20a: a cut dict with no inner object raises the same "no JSON object found" as prose, so a cut answer
    looked like a refusal and was never asked again.  1722's L3 answer was this shape (561 chars, cut mid-string)."""
    _, kind = error_kind(CUT_OBJECT)
    assert kind == "JSONTruncated", f"a cut answer was reported as {kind}"


def test_a_whole_answer_parses_as_before():
    assert [r["id"] for r in srt_text._json_from_text(whole(["d1", "d2"]))] == ["d1", "d2"]
    value, kind = error_kind(CUT_OBJECT + '",\n  "basis": "x"\n}')
    assert kind is None and value["id"] == "d002"


@pytest.mark.parametrize("wrap", ["{}", "{}\n\n以上。", "说明如下：\n```json\n{}\n```\n完", "```\n{}\n```"])
def test_prose_or_a_fence_around_a_whole_answer_still_parses(wrap):
    value, kind = error_kind(wrap.format(whole(["d1"])))
    assert kind is None and [r["id"] for r in value] == ["d1"]


def test_a_broken_first_value_does_not_come_back_as_an_object_inside_it():
    """The other way the old parser returned a fragment: the outer array closes but does not parse (a missing comma
    between rows), so the old code moved on to the first `{` inside it and returned that one row."""
    value, kind = error_kind('[{"id": "d01", "basis": "x"} {"id": "d02", "basis": "y"}]')
    assert value is None, f"a broken answer was returned as {value!r}"
    assert kind == "JSONMalformed", f"a broken answer was reported as {kind}"


def test_prose_with_no_json_is_still_no_json():
    """Control on the other side: prose and a bracketed laugh in prose are not a cut answer, so they keep the
    "no JSON here" path (the caller's own CPA_RESPONSE_UNPARSEABLE) and are not re-sent as if they were cut."""
    for text in ("我不确定，这里听不清楚。", "[笑] 这句是弹幕念的"):
        value, kind = error_kind(text)
        assert value is None and kind not in (None, "JSONTruncated"), f"{text!r} came back as {value!r} / {kind}"


# ----- step 3: the adjudication ----------------------------------------------------------------------------------
class ScriptedCpa:
    """One scripted text per adjudication call, in call order; thread-safe because the batches run at once.
    After the script runs out it answers every batch whole (`fallback`), or by `whole`."""

    def __init__(self, texts, *, fallback=None):
        self.texts = list(texts)
        self.fallback = fallback
        self._rows: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    @property
    def calls(self) -> list[str]:
        with self._lock:
            return [call_id for call_id, _prompt in self._rows]

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
        ids = list(dict.fromkeys(re.findall(r'"id": "(d\d+)"', prompt_text)))
        with self._lock:
            self._rows.append((call_id, prompt_text))
            text = self.texts.pop(0) if self.texts else None
        if text is None:
            text = self.fallback(ids) if self.fallback else whole(ids)
        return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                "response_text": text, "stderr_tail": ""}


def _listen_records(*rows):
    names = {m["slug"]: m["name"] for m in si.LISTEN_MODELS}
    return [{"win": win, "model": names[slug], "slug": slug, "status": "OK",
             "lines": [{"start_s": 0.0, "text": text}]} for win, slug, text in rows]


def _subs_ctx(tmp_path, cpa, records=()):
    def script(name, body, remote):
        if name.startswith("s3-listen"):
            remote.files[f"{RD}/listen/out/all.json"] = json.dumps(records, ensure_ascii=False).encode()
            return RemoteResult(0, "LISTEN_DONE\n", "")
        if name == "s3-asr":
            return RemoteResult(0, "ASR_OK\n", "")
        return RemoteResult(0, "", "")

    remote = FakeRemote(on_script=script)
    text = srt(3, step=2.0).encode()
    remote.files[f"{RD}/asr/bcut.srt"] = text
    remote.files[f"{RD}/asr/jianying.srt"] = text
    return make_ctx(tmp_path, remote=remote, cpa=cpa, bcut_text=srt(3, step=2.0))


def _doubts(n):
    return [{"id": f"d{i:03d}", "cue_id": i, "current": f"句子{i}", "readings": {"bcut": f"句子{i}"},
             "differing": [], "maintenance": {"maintained": False}, "candidates": [{"text": f"句子{i}"}],
             "danmaku": []} for i in range(1, n + 1)]


def test_the_adjudication_asks_again_when_the_answer_is_cut(tmp_path):
    """0a952e20a: the cut answer is taken as two rows, two doubts are decided and the rest stop the clip at
    DOUBT_UNRESOLVED / CPA_ROW_MISSING; the cut answer also goes into the cache under the batch prompt."""
    ids = [f"d{i:03d}" for i in range(1, 4)]
    cut = cut_array(ids, rows=1)
    cpa = ScriptedCpa([cut], fallback=whole)
    ctx = _subs_ctx(tmp_path, cpa)
    calls: list[dict] = []
    rows, failure = si._adjudicate_all(ctx, _doubts(3), "上下文", calls, tag="p1")
    assert failure is None, failure and failure.note
    assert sorted(rows) == ids, "a doubt the cut answer skipped was decided by a later answer"
    assert cpa.calls == ["adjudicate-p1-1", "adjudicate-p1-1--reply-2"], "the same prompt was asked again once"
    # the whole answer is the cached one; the cut original is kept beside it, and never in the cache
    entries = list((tmp_path / "cache" / "cpa").glob("*.json"))
    assert len(entries) == 1
    assert json.loads(entries[0].read_text(encoding="utf-8"))["response_text"] == whole(ids)
    cut_files = list((ctx.clip_dir / "cpa").glob("adjudicate-p1-1.rejected-*.completion.txt"))
    assert len(cut_files) == 1 and cut_files[0].read_text(encoding="utf-8") == cut
    assert (ctx.clip_dir / "cpa/adjudicate-p1-1.completion.txt").read_text(encoding="utf-8") == whole(ids)


def test_a_cut_answer_left_in_the_cache_is_voided_and_asked_again(tmp_path):
    """A run that stopped while a cut answer was already cached (TASK-32b §4.3 deleted one by hand): the next run
    must not read it as the answer.  The cache entry is dropped and the prompt is asked again."""
    ids = [f"d{i:03d}" for i in range(1, 4)]
    cpa = ScriptedCpa([], fallback=whole)
    ctx = _subs_ctx(tmp_path, cpa)
    calls: list[dict] = []
    doubts = _doubts(3)
    first, failure = si._adjudicate_all(ctx, doubts, "上下文", calls, tag="p1")
    assert failure is None and sorted(first) == ids
    cache = Path(calls[0]["cache_path"])
    cache.write_text(json.dumps({"rc": 0, "complete": True, "response_text": cut_array(ids, rows=1),
                                 "stderr_tail": ""}, ensure_ascii=False), encoding="utf-8")
    again: list[dict] = []
    rows, failure = si._adjudicate_all(ctx, doubts, "上下文", again, tag="p1")
    assert failure is None and sorted(rows) == ids
    assert again[0].get("cached") is None            # it was asked again, not read from the cache
    assert json.loads(cache.read_text(encoding="utf-8"))["response_text"] == whole(ids)


def test_a_whole_answer_missing_a_doubt_id_is_asked_again_too(tmp_path):
    """The same failure seen from the id side: a whole JSON list that leaves a doubt of the batch out is not a
    smaller answer.  It takes the cut answer's path — asked again, the same tries."""
    ids = [f"d{i:03d}" for i in range(1, 4)]
    short = json.dumps(json.loads(whole(ids))[:2], ensure_ascii=False)
    cpa = ScriptedCpa([short], fallback=whole)
    ctx = _subs_ctx(tmp_path, cpa)
    calls: list[dict] = []
    rows, failure = si._adjudicate_all(ctx, _doubts(3), "上下文", calls, tag="p1")
    assert failure is None and sorted(rows) == ids
    assert cpa.calls == ["adjudicate-p1-1", "adjudicate-p1-1--reply-2"]
    assert calls[0]["rejected_answers"][0]["reason"].startswith("CPA_ANSWER_MISSING_IDS")


def test_step3_stops_as_a_tool_failure_when_every_answer_is_cut(tmp_path):
    """0a952e20a: step 3 decided the doubts it got and left the others UNRESOLVED (1722 stopped on DOUBT_UNRESOLVED;
    1323's leftovers were quietly filled by a later level).  Now the call is a failure and the step stops on it."""
    ids = [f"d{i:03d}" for i in range(1, 4)]
    cpa = ScriptedCpa([], fallback=lambda _ids: cut_array(ids, rows=1))
    ctx = _subs_ctx(tmp_path, cpa, records=_listen_records(("C000", "38m", "句子乙")))
    out = si.step3_subtitles(ctx)
    assert not out.ok and out.reason == "CPA_CALL_FAILED"
    assert "CPA_ANSWER_INCOMPLETE" in out.note
    assert not (ctx.clip_dir / "subs/doubts.json").exists(), "a cut answer produced subtitles"
    assert not list((tmp_path / "cache" / "cpa").glob("*.json")), "a cut answer was cached"
    asked = sum(1 for call in cpa.calls if call.startswith("adjudicate-p1-1"))
    assert asked == getattr(adapters, "CPA_TEXT_TRIES", TRIES), f"{asked} calls; the retry count is cpa_text.py's"
    assert not any("UNRESOLVED" in line for line in out.process)


def test_step3_decides_on_the_whole_answer_after_one_cut_answer(tmp_path):
    """The ordinary path end to end: one cut answer, then a whole one; the clip goes on and both are on disk."""
    ids = [f"d{i:03d}" for i in range(1, 4)]
    cpa = ScriptedCpa([cut_array(ids, rows=1)], fallback=whole)
    ctx = _subs_ctx(tmp_path, cpa, records=_listen_records(("C000", "38m", "句子乙")))
    out = si.step3_subtitles(ctx)
    assert out.ok, out.note
    doubts = json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8"))
    assert all(d["state"] != "UNRESOLVED" for d in doubts)
    assert len(list((ctx.clip_dir / "cpa").glob("*.rejected-1.completion.txt"))) == 1


def test_a_call_the_provider_failed_is_not_asked_again_here(tmp_path):
    """The adapter's own tries already ran for a failed call; the retry count lives in one place, so _ask_cpa sends
    it once.  Control for the new loop."""
    class Failing(IdCpa):
        def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
            with self._lock:
                self._rows.append((call_id, prompt_text))
            return {"call_id": call_id, "model": model, "effort": effort, "rc": 1, "elapsed_s": 0.1,
                    "response_text": "", "stderr_tail": "CPA_HTTP_ERROR (fake)"}

    cpa = Failing([])
    ctx = _subs_ctx(tmp_path, cpa)
    calls: list[dict] = []
    rows, failure = si._adjudicate_all(ctx, _doubts(3), "上下文", calls, tag="p1")
    assert rows == {} and failure is not None
    assert failure.reason == "CPA_CALL_FAILED" and len(cpa.calls) == 1
