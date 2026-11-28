"""bcut's rate limit is waited out across the host, not turned into a failed clip (the channel's request (/U304);
11-23: eleven clips at once got 412 at task/result and 「bcut task failed」, and seven stopped at step 8)."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from scripts import free_asr_client as fac

RESULT = json.dumps({"utterances": [{"start_time": 0, "end_time": 900, "transcript": "好的"}]})


class Clock:
    def __init__(self):
        self.t = 1_000_000.0
        self.slept: list[float] = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(round(s, 1))
        self.t += s


@pytest.fixture
def bcut(tmp_path, monkeypatch):
    """A scripted bcut: `script[path]` is a list of answers given in turn (an int is an HTTP error code)."""
    clock = Clock()
    monkeypatch.setattr(fac, "_now", clock.now, raising=False)
    monkeypatch.setattr(fac, "_sleep", clock.sleep, raising=False)
    monkeypatch.setattr(fac.time, "sleep", clock.sleep)          # nothing in the client sleeps for real
    monkeypatch.setattr(fac, "COOLDOWN_FILE", tmp_path / "cooldown.json", raising=False)
    monkeypatch.setattr("random.uniform", lambda a, b: 0.0)
    script: dict[str, list] = {}
    asked: list[str] = []

    def request_json(url, **_kw):
        path = url.split("rubick-interface/")[1].split("?")[0]
        asked.append(path)
        answers = script.get(path) or [{"code": 0, "data": {}}]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, int):
            raise urllib.error.HTTPError(url, answer, "x", {}, io.BytesIO(b"<!DOCTYPE html>"))
        return answer

    monkeypatch.setattr(fac, "_request_json", request_json)
    monkeypatch.setattr(fac, "_request", lambda *a, **k: (b"", {"Etag": '"e"'}))
    script.update({
        "resource/create": [{"code": 0, "data": {"per_size": 10, "upload_urls": ["u"], "in_boss_key": "k",
                                                  "resource_id": "r", "upload_id": "i"}}],
        "resource/create/complete": [{"code": 0, "data": {"download_url": "d"}}],
        "task": [{"code": 0, "data": {"task_id": "t"}}],
        "task/result": [{"code": 0, "data": {"state": 4, "result": RESULT}}],
    })
    return clock, script, asked


def _run():
    return fac.transcribe_bcut(b"audio", poll_interval=0.0, log=lambda *_: None)


def test_a_412_run_at_task_result_is_waited_out_with_a_growing_cooldown(bcut):
    clock, script, _asked = bcut
    script["task/result"] = [412] * 5 + [{"code": 0, "data": {"state": 4, "result": RESULT}}]
    assert _run()["utterances"][0]["transcript"] == "好的"
    assert clock.slept == [15.0, 30.0, 60.0, 120.0, 240.0]


def test_a_task_bcut_reports_failed_is_submitted_again(bcut):
    _clock, script, asked = bcut
    script["task/result"] = [{"code": 0, "data": {"state": 3, "remark": ""}},
                             {"code": 0, "data": {"state": 4, "result": RESULT}}]
    assert _run()["utterances"]
    assert asked.count("task") == 2 and asked.count("resource/create") == 2


def test_a_throttled_answer_makes_every_caller_on_the_host_wait_before_asking(bcut):
    clock, _script, asked = bcut
    fac._note_throttled("HTTP 429 seen by another clip")
    _run()
    assert clock.slept[0] == 15.0 and asked[0] == "resource/create"
    clock.slept.clear()
    _run()                      # the cooldown has passed and an answer came: nobody waits now
    assert clock.slept == []


def test_throttling_beyond_the_budget_raises_and_other_errors_are_not_retried(bcut, monkeypatch):
    clock, script, _asked = bcut
    monkeypatch.setattr(fac, "RATE_LIMIT_BUDGET_S", 400.0)
    script["task/result"] = [429]
    with pytest.raises(fac.AsrThrottled):
        _run()
    assert sum(clock.slept) <= 400.0
    clock.slept.clear()
    fac.COOLDOWN_FILE.unlink()
    script["task/result"] = [400]
    with pytest.raises(fac.AsrError, match="HTTP 400"):
        _run()
    assert clock.slept == []


def test_task_failures_stop_after_the_task_tries(bcut):
    _clock, script, asked = bcut
    script["task/result"] = [{"code": 0, "data": {"state": 3, "remark": "bad audio"}}]
    with pytest.raises(fac.AsrError, match="bcut task failed: bad audio"):
        _run()
    assert asked.count("task") == fac.BCUT_TASK_TRIES
