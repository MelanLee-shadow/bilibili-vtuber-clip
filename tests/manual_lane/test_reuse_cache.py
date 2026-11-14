"""Reuse is real, not by file name (COST-07), and quota is respected.

- The same listening call (window audio/tempo, model, prompt) is never requested twice; the second run makes 0 calls.
- A 429 marks the model exhausted for the rest of the run, records status=QUOTA (not ERROR), and is retried only after
  retry_at (Retry-After).  reuse-only never calls a model.
- The same adjudication prompt is answered from the CPA cache: the second run of the step makes 0 CPA calls.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from fakes import FakeRemote, IdCpa, make_ctx, srt

from src.autoslice.manual_lane import stage_impl as si

REMOTE = Path(__file__).resolve().parents[2] / "assets" / "manual_lane" / "remote" / "listen_windows.py"
_spec = importlib.util.spec_from_file_location("listen_windows_remote", REMOTE)
lw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lw)

PROMPT = "听写 {lead} {tail}"
MODELS = [{"name": "M-A", "slug": "aa"}, {"name": "M-B", "slug": "bb"}]
WINDOWS = [{"win": "C000", "core": [1.0, 2.0], "audio": [0.5, 2.5], "tempo": 1.0},
           {"win": "C001", "core": [3.0, 4.0], "audio": [2.5, 4.5], "tempo": 1.0}]


def _excerpt(win, cut, audio_dir):
    out = audio_dir / f"{win['win']}.mp3"
    out.write_bytes(b"x")
    return out


class Quota429(Exception):
    def __init__(self, retry_after=None):
        super().__init__("CPA_GEMINI_HTTP_ERROR")
        self.code = 429
        self.retry_after_seconds = retry_after


class Gen:
    """Counts calls per model name; models in `quota` raise a 429 on every call."""

    def __init__(self, quota=(), retry_after=1200):
        self.calls: dict[str, int] = {}
        self.quota = set(quota)
        self.retry_after = retry_after

    def __call__(self, prompt, model_name, mp3):
        self.calls[model_name] = self.calls.get(model_name, 0) + 1
        if model_name in self.quota:
            raise Quota429(self.retry_after)
        return json.dumps({"lines": [{"start_s": 0.0, "text": "好"}]}, ensure_ascii=False)

    def total(self):
        return sum(self.calls.values())


def _run(out, gen, *, prompt=PROMPT, clock=lambda: 1000.0, reuse_only=False, windows=WINDOWS, models=MODELS):
    # one worker: the order of the calls is then fixed, so the quota assertions are deterministic
    return lw.run([dict(w) for w in windows], models, Path("cut.mp4"), out, prompt, excerpt_fn=_excerpt,
                  generate=gen, clock=clock, reuse_only=reuse_only, workers=1)


def test_listening_second_run_with_the_same_inputs_makes_no_model_call(tmp_path):
    out = tmp_path / "out"
    gen = Gen()
    _run(out, gen)
    assert gen.total() == 4
    gen2 = Gen()
    results = _run(out, gen2)
    assert gen2.total() == 0
    assert all(status == "reused" for _, status in results)


def test_a_changed_prompt_is_a_new_request_for_every_window(tmp_path):
    out = tmp_path / "out"
    _run(out, Gen())
    gen = Gen()
    _run(out, gen, prompt=PROMPT + "（改）")
    assert gen.total() == 4


def test_a_429_marks_the_model_exhausted_for_the_run_and_records_quota_not_error(tmp_path):
    out = tmp_path / "out"
    gen = Gen(quota={"M-A"}, retry_after=1200)
    results = _run(out, gen, clock=lambda: 1000.0)
    assert gen.calls.get("M-A") == 1          # one request, then the model is not asked again in this run
    assert gen.calls.get("M-B") == 2
    statuses = dict(results)
    assert statuses["C000.aa.json"] == "QUOTA" and statuses["C001.aa.json"] == "quota_wait"
    rec = json.loads((out / "C000.aa.json").read_text(encoding="utf-8"))
    assert rec["status"] == "QUOTA" and rec["retry_at"] == 2200.0 and rec["retry_after_seconds"] == 1200
    assert all(json.loads(p.read_text(encoding="utf-8"))["status"] != "ERROR" for p in out.glob("*.json")
               if p.name != "all.json")


def test_a_quota_record_is_not_retried_before_retry_at_and_is_after(tmp_path):
    out = tmp_path / "out"
    _run(out, Gen(quota={"M-A"}, retry_after=1200), clock=lambda: 1000.0)
    early = Gen()
    _run(out, early, clock=lambda: 1500.0)               # before retry_at=2200
    assert early.calls.get("M-A", 0) == 0
    late = Gen()
    _run(out, late, clock=lambda: 2300.0)                # after retry_at
    assert late.calls.get("M-A") == 2                    # both windows of the model are asked again


def test_a_429_without_retry_after_waits_a_minute_first_and_longer_each_time_it_is_refused_again(tmp_path):
    out = tmp_path / "out"
    now = [1000.0]
    waits = []
    for _ in range(5):
        _run(out, Gen(quota={"M-A"}, retry_after=None), clock=lambda: now[0], windows=WINDOWS[:1], models=MODELS[:1])
        rec = json.loads((out / "C000.aa.json").read_text(encoding="utf-8"))
        assert rec["status"] == "QUOTA" and rec["error"].startswith("429") and rec["refusals"] == len(waits) + 1
        waits.append(rec["retry_at"] - now[0])
        now[0] = rec["retry_at"] + 1
    assert waits == [60, 300, 1500, lw.DEFAULT_QUOTA_WAIT_S, lw.DEFAULT_QUOTA_WAIT_S]
    now[0] += 3 * 3600                                                    # hours later the first wait is short again
    _run(out, Gen(quota={"M-A"}, retry_after=None), clock=lambda: now[0], windows=WINDOWS[:1], models=MODELS[:1])
    assert json.loads((out / "C000.aa.json").read_text(encoding="utf-8"))["retry_at"] - now[0] == 60


class Refused(Exception):
    def __init__(self, code):
        super().__init__("CPA_GEMINI_HTTP_ERROR")
        self.code = code


class LevelGen(Gen):
    """Models in `refused` answer with that HTTP status (the account or the route refuses them)."""

    def __init__(self, refused, **kw):
        super().__init__(**kw)
        self.refused = dict(refused)

    def __call__(self, prompt, model_name, mp3):
        if model_name in self.refused:
            self.calls[model_name] = self.calls.get(model_name, 0) + 1
            raise Refused(self.refused[model_name])
        return super().__call__(prompt, model_name, mp3)


LEVEL = [{"name": "M-A", "slug": "aa", "then": ["M-B"]}]


def _records(out):
    return {p.name: json.loads(p.read_text(encoding="utf-8")) for p in out.glob("*.aa.json")}


def test_a_level_takes_its_first_model_that_answers_and_does_not_ask_a_refused_one_again(tmp_path):
    out = tmp_path / "out"
    gen = LevelGen({"M-A": 403})
    assert dict(_run(out, gen, models=LEVEL)) == {"C000.aa.json": "OK", "C001.aa.json": "OK"}
    assert gen.calls == {"M-A": 1, "M-B": 2}          # refused once: no retry, not asked for the next window
    assert {r["model"] for r in _records(out).values()} == {"M-B"}
    assert dict(_run(out, gen, models=LEVEL)) == {"C000.aa.json": "reused", "C001.aa.json": "reused"} and gen.total() == 3
    later = [{**WINDOWS[0], "win": "C002"}]
    _run(out, gen, models=LEVEL, windows=later, clock=lambda: 1000.0 + lw.UNAVAILABLE_S - 1)
    assert gen.calls == {"M-A": 1, "M-B": 3}          # still remembered by the next run of the clip
    gen.refused = {}
    _run(out, gen, models=LEVEL, windows=[{**WINDOWS[0], "win": "C003"}], clock=lambda: 1001.0 + lw.UNAVAILABLE_S)
    assert gen.calls == {"M-A": 2, "M-B": 3} and _records(out)["C003.aa.json"]["model"] == "M-A"   # asked again later


def test_a_level_nobody_answers_says_why_and_a_model_that_only_asked_to_wait_keeps_it_waiting(tmp_path):
    out = tmp_path / "out"
    gen = LevelGen({"M-A": 400, "M-B": 503})
    assert dict(_run(out, gen, models=LEVEL)) == {"C000.aa.json": "UNAVAILABLE", "C001.aa.json": "UNAVAILABLE"}
    assert gen.calls == {"M-A": 1, "M-B": 1}
    first, second = _records(out)["C000.aa.json"], _records(out)["C001.aa.json"]
    assert [(t["model"], t["status"], t.get("http_status")) for t in first["tried"]] == [
        ("M-A", "UNAVAILABLE", 400), ("M-B", "UNAVAILABLE", 503)]
    assert second["status"] == "UNAVAILABLE" and second["lines"] == [] and len(second["tried"]) == 2
    out = tmp_path / "waiting"
    gen = LevelGen({"M-B": 403}, quota={"M-A"}, retry_after=1200)
    assert dict(_run(out, gen, models=LEVEL, windows=WINDOWS[:1])) == {"C000.aa.json": "QUOTA"}
    record = _records(out)["C000.aa.json"]
    assert (record["model"], record["retry_at"]) == ("M-A", 2200.0) and record["tried"][1]["status"] == "UNAVAILABLE"


def test_a_level_with_one_model_is_still_asked_again_after_a_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(lw.time, "sleep", lambda _s: None)
    gen = LevelGen({"M-A": 403})
    assert dict(_run(tmp_path / "out", gen, models=MODELS[:1], windows=WINDOWS[:1])) == {"C000.aa.json": "ERROR"}
    assert gen.calls == {"M-A": 3}


def test_reuse_only_never_calls_a_model_and_reports_missing(tmp_path):
    out = tmp_path / "out"
    gen = Gen()
    results = _run(out, gen, reuse_only=True)
    assert gen.total() == 0 and all(status == "missing" for _, status in results)


def test_a_failed_record_is_retried_and_an_ok_one_is_not(tmp_path):
    out = tmp_path / "out"
    (out / "audio").mkdir(parents=True)
    dest = out / "C000.aa.json"
    dest.write_text(json.dumps({"win": "C000", "status": "ERROR", "audio": [0.5, 2.5], "tempo": 1.0,
                                "model": "M-A", "slug": "aa", "lines": []}), encoding="utf-8")
    gen = Gen()
    lw.run([dict(WINDOWS[0])], [MODELS[0]], Path("cut.mp4"), out, PROMPT, excerpt_fn=_excerpt, generate=gen)
    assert gen.total() == 1 and json.loads(dest.read_text(encoding="utf-8"))["status"] == "OK"


def test_a_record_written_before_keys_existed_is_reused_when_its_inputs_match(tmp_path):
    out = tmp_path / "out"
    (out / "audio").mkdir(parents=True)
    legacy = {"win": "C000", "model": "M-A", "slug": "aa", "tempo": 1.0, "audio": [0.5, 2.5], "core": [1.0, 2.0],
              "status": "OK", "lines": [{"start_s": 0.0, "text": "旧"}], "error": "", "attempts": 1}
    (out / "C000.aa.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    gen = Gen()
    results = _run(out, gen, windows=WINDOWS[:1], models=MODELS[:1])
    assert gen.total() == 0 and results == [("C000.aa.json", "reused")]


def test_cpa_second_run_of_the_same_step_makes_no_cpa_call(tmp_path):
    rows = [{"id": "d001", "state": "KEEP", "final_text": "", "confidence": "高", "affects_meaning": False}]
    cpa = IdCpa(rows)
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa, bcut_text=srt(3, step=2.0))
    from test_steps import _listen_records, _subs_remote  # the step tests' fixtures
    ctx.adapters.remote = _subs_remote(_listen_records(("C000", "38m", "句子甲")))
    assert si.step3_subtitles(ctx).ok
    first = len(cpa.calls)
    assert first == 2                      # pass 1 and pass 2
    assert si.step3_subtitles(ctx).ok
    assert len(cpa.calls) == first         # identical prompts: answered from the cache
