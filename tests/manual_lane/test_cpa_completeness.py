"""TASK-26 guard: a CPA answer is the answer only when the provider says it completed.

The ordinary path is run end to end with only the network faked: cpa_text.py (the client that runs on OCI3) reads a
scripted /responses stream and writes OUT_JSON; SshCpaJudge.ask reads that OUT_JSON back; _ask_cpa decides the cache.
The streams are the event sequence the 11-02 probe recorded (sudocode-chat route) and the shapes CLIProxyAPI emits
for a cut answer (response.incomplete, incomplete_details) and for a stream that just stops.  No network, no ssh.
"""

from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import re
import shlex
from pathlib import Path

import pytest

from src.autoslice.manual_lane import adapters
from src.autoslice.manual_lane.adapters import RemoteResult, SshCpaJudge
from src.autoslice.manual_lane.step_io import _ask_cpa, _cpa_ok, _cpa_cache_path
from tests.manual_lane.fakes import FakeRemote, make_ctx

REMOTE = Path(adapters.CPA_TEXT_SCRIPT).parent


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sse(events):
    return [f"event: {e['type']}\n".encode() if i % 2 == 0 else f"data: {json.dumps(e, ensure_ascii=False)}\n".encode()
            for e in events for i in (0, 1)]


HEAD = [{"type": "response.created", "response": {"status": "in_progress"}},
        {"type": "response.in_progress", "response": {"status": "in_progress"}},
        {"type": "response.output_item.added", "item": {"type": "message", "status": "in_progress"}},
        {"type": "response.content_part.added", "part": {"type": "output_text", "text": ""}}]
WHOLE = '{"candidates": [{"title": "完整的标题"}]}'
HALF = '{"candidates": [{"title": "被截断的标'


def _message(text, status):
    return {"type": "message", "status": status, "content": [{"type": "output_text", "text": text}], "role": "assistant"}


def _stream(kind):
    """One scripted /responses stream: complete, cut (stops after some deltas), incomplete (CPA's response.incomplete),
    empty (completed with no text)."""
    if kind == "cut":
        return _sse(HEAD + [{"type": "response.output_text.delta", "delta": HALF}])
    if kind == "empty":
        return _sse(HEAD + [{"type": "response.completed", "response": {
            "status": "completed", "output": [], "usage": {"output_tokens": 0}}}])
    text, status = (WHOLE, "completed") if kind == "complete" else (HALF, "incomplete")
    tail = [{"type": "response.output_text.done", "text": text},
            {"type": "response.output_item.done", "item": _message(text, status)}]
    if kind == "complete":
        tail.append({"type": "response.completed", "response": {
            "status": "completed", "output": [_message(text, status)], "usage": {"output_tokens": 12}}})
    else:
        tail.append({"type": "response.incomplete", "response": {
            "status": "incomplete", "incomplete_details": {"reason": "content_filter"},
            "output": [_message(text, status)], "usage": {"output_tokens": 509}}})
    return _sse(HEAD + [{"type": "response.output_text.delta", "delta": text[:9]},
                        {"type": "response.output_text.delta", "delta": text[9:]}] + tail)


class _Resp(io.BytesIO):
    def __init__(self, lines):
        super().__init__(b"".join(lines))


class OciCpa:
    """OCI3 + CPA, local: run_script runs the uploaded cpa_text.py's main() on a local copy of the prompt with urlopen
    answering the scripted streams in order; the Remote reads the OUT_JSON that main() wrote."""

    def __init__(self, tmp_path, monkeypatch, streams):
        self.tmp, self.streams, self.requests = tmp_path, list(streams), 0
        self.request_bodies = []
        self.cpa_text = _load(adapters.CPA_TEXT_SCRIPT, "cpa_text_guard")
        monkeypatch.setattr(self.cpa_text.urllib.request, "urlopen", self._urlopen)
        monkeypatch.setattr(self.cpa_text.time, "sleep", lambda s: None)
        monkeypatch.setenv("CPA_BASE_URL", "https://cpa.invalid/v1")
        monkeypatch.setenv("CPA_API_KEY", "test-only")
        self.out = tmp_path / "out.json"

    def _urlopen(self, req, timeout):
        self.requests += 1
        self.request_bodies.append(json.loads(req.data))
        return _Resp(self.streams.pop(0) if self.streams else _stream("cut"))

    def put(self, local, remote_path):
        pass

    def run_script(self, *, name, body, timeout):
        if name.startswith("cpa-read-"):
            assert body.startswith("cat ")
            return RemoteResult(0, self.out.read_text(encoding="utf-8"), "") if self.out.exists() else RemoteResult(1, "", "")
        prompt = self.tmp / "prompt.txt"
        prompt.write_text(body.split("<<'MANUAL_LANE_PROMPT_EOF'\n", 1)[1].split("\nMANUAL_LANE_PROMPT_EOF")[0] + "\n",
                          encoding="utf-8")
        flags = re.search(r"--model\s+\S+\s+--effort\s+\S+\s+--tries\s+\d+\s+--stream", body)
        assert flags is not None
        rc = self.cpa_text.main([str(prompt), str(self.out), *shlex.split(flags.group())])
        return RemoteResult(rc, "", "")

def _ask(tmp_path, monkeypatch, streams):
    oci = OciCpa(tmp_path, monkeypatch, streams)
    ctx = make_ctx(tmp_path / "clip", remote=FakeRemote(), cpa=SshCpaJudge(oci))
    record = _ask_cpa(ctx, "title-0", "提示词：写标题")
    return oci, ctx, record


@pytest.mark.parametrize("kind", ["cut", "incomplete", "empty"])
def test_an_answer_the_provider_did_not_complete_fails_retries_and_is_not_cached(tmp_path, monkeypatch, kind):
    oci, ctx, record = _ask(tmp_path, monkeypatch, [_stream(kind)] * adapters.CPA_TEXT_TRIES)
    assert record["rc"] != 0, f"{kind}: rc 0 for an answer the provider did not complete"
    assert not _cpa_ok(record)
    assert oci.requests == 10, f"{kind}: {oci.requests} request(s); the client must retry up to its tries"
    assert not Path(record["cache_path"]).exists(), f"{kind}: the incomplete answer was cached"
    assert record["response_text"] == ""   # half an answer never reaches a parser
    assert (ctx.clip_dir / "cpa/title-0.completion.txt").read_text(encoding="utf-8") == ""
    out = json.loads(oci.out.read_text(encoding="utf-8"))
    assert out["complete"] is False and out["text"] == "" and out["reason"]
    assert record["reason"] and record["stderr_tail"].startswith("CPA_NOT_COMPLETE")


def test_an_incomplete_attempt_is_retried_and_the_complete_one_is_kept(tmp_path, monkeypatch):
    oci, ctx, record = _ask(tmp_path, monkeypatch, [_stream("cut"), _stream("incomplete"), _stream("complete")])
    assert oci.requests == 3
    assert record["rc"] == 0 and _cpa_ok(record) and record["response_text"] == WHOLE
    cached = json.loads(Path(record["cache_path"]).read_text(encoding="utf-8"))
    assert cached["response_text"] == WHOLE and cached["complete"] is True
    log = json.loads(oci.out.read_text(encoding="utf-8"))["http_log"]
    assert [a["complete"] for a in log] == [False, False, True]
    assert log[1]["reason"].startswith("TERMINAL response.incomplete") and "content_filter" in log[1]["reason"]
    assert log[0]["reason"].startswith("NO_TERMINAL_EVENT")


def test_a_complete_answer_is_one_request_and_cached(tmp_path, monkeypatch):
    oci, ctx, record = _ask(tmp_path, monkeypatch, [_stream("complete")])
    assert oci.requests == 1 and record["rc"] == 0 and record["response_text"] == WHOLE
    assert Path(record["cache_path"]) == _cpa_cache_path(ctx, "提示词：写标题")
    assert Path(record["cache_path"]).exists()
    out = json.loads(oci.out.read_text(encoding="utf-8"))
    assert oci.request_bodies[0]["input"] == "提示词：写标题\n"
    assert out["prompt_sha256"] == hashlib.sha256("提示词：写标题\n".encode()).hexdigest()
    assert (out["model"], out["effort"], out["stream"]) == ("gpt-6.1-sol", "medium", True)


def test_normal_ask_still_dispatches_a_changed_question(tmp_path, monkeypatch):
    oci = OciCpa(tmp_path, monkeypatch, [_stream("complete"), _stream("complete")])
    judge = SshCpaJudge(oci)
    assert judge.ask(call_id="same", prompt_text="合成问题甲", remote_dir="/r/clip")["rc"] == 0
    assert judge.ask(call_id="same", prompt_text="合成问题乙", remote_dir="/r/clip")["rc"] == 0
    assert oci.requests == 2 and [x["input"] for x in oci.request_bodies] == ["合成问题甲\n", "合成问题乙\n"]


def test_an_out_json_without_the_completion_word_is_a_failed_call():
    # the client is uploaded with every call; an OUT_JSON that does not say complete (an older client, a hand-made
    # file) is not taken as an answer
    class Remote:
        def put(self, local, remote_path):
            pass

        def run_script(self, *, name, body, timeout):
            if name == "cpa-read-x":
                return RemoteResult(0, json.dumps({"text": HALF, "attempts": 1}, ensure_ascii=False), "")
            return RemoteResult(0, "", "")

    record = SshCpaJudge(Remote()).ask(call_id="x", prompt_text="p", remote_dir="/r/clip")
    assert record["rc"] != 0 and record["response_text"] == ""


def test_stream_mismatch_between_deltas_and_the_provider_final_text_is_incomplete():
    cpa_text = _load(adapters.CPA_TEXT_SCRIPT, "cpa_text_mismatch")
    lines = _sse(HEAD + [{"type": "response.output_text.delta", "delta": HALF},
                         {"type": "response.output_text.done", "text": WHOLE},
                         {"type": "response.completed", "response": {"status": "completed",
                                                                     "output": [_message(WHOLE, "completed")]}}])
    _, _, completion = cpa_text.collect_stream_text(lines)
    assert completion["complete"] is False and completion["reason"].startswith("TEXT_MISMATCH")


def test_mid_stream_error_event_is_incomplete():
    # CLIProxyAPI writes an upstream failure after the stream started as `event: error` (responses handler)
    cpa_text = _load(adapters.CPA_TEXT_SCRIPT, "cpa_text_error")
    lines = _sse(HEAD + [{"type": "response.output_text.delta", "delta": HALF},
                         {"type": "error", "error": {"message": "upstream stream closed before [DONE]"}}])
    _, _, completion = cpa_text.collect_stream_text(lines)
    assert completion["complete"] is False and completion["reason"].startswith("TERMINAL error")


# ----- the vision producers (non-streamed /responses on OCI3) ---------------------------------------------------
def _payload(status, text, details=None):
    out = {"status": status, "output": [_message(text, status)], "usage": {"output_tokens": 64}}
    if details:
        out["incomplete_details"] = details
    return out


class _JsonResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_pick_frame_reading_cut_by_the_provider_is_an_error_not_a_verdict(tmp_path, monkeypatch):
    pf = _load(REMOTE / "pick_frame.py", "pick_frame_guard")
    payload = _payload("incomplete", "CLE", {"reason": "max_output_tokens"})
    monkeypatch.setattr(pf.urllib.request, "urlopen",
                        lambda req, timeout: _JsonResp(json.dumps(payload).encode()))
    monkeypatch.setattr(pf, "extract", lambda cut, t, path: "aW1n")
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.invalid/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-only")
    out = tmp_path / "out.json"
    pf.main(["pick_frame.py", "cut.mp4", str(tmp_path / "frames"), str(out), "1.0"])
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["checked"][0]["verdict"] == "ERROR" and doc["chosen"] is None
    assert doc["calls"][0]["rc"] == 1


def test_pick_frame_complete_reading_still_gives_its_verdict(tmp_path, monkeypatch):
    pf = _load(REMOTE / "pick_frame.py", "pick_frame_control")
    answers = iter([_payload("completed", "CLEAN"), _payload("completed", '{"danmaku": [], "person": [], "guest": []}')])
    monkeypatch.setattr(pf.urllib.request, "urlopen",
                        lambda req, timeout: _JsonResp(json.dumps(next(answers)).encode()))
    monkeypatch.setattr(pf, "extract", lambda cut, t, path: "aW1n")
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.invalid/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-only")
    out = tmp_path / "out.json"
    pf.main(["pick_frame.py", "cut.mp4", str(tmp_path / "frames"), str(out), "1.0"])
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["chosen"] == 1.0 and doc["checked"][0]["verdict"] == "CLEAN" and doc["calls"][0]["rc"] == 0


def test_cover_vision_answer_cut_by_the_provider_is_raised_and_not_kept(tmp_path, monkeypatch):
    cc = _load(REMOTE / "cover_candidates.py", "cover_candidates_guard")
    payload = _payload("incomplete", '{"face": [1, 2', {"reason": "max_output_tokens"})
    monkeypatch.setattr(cc.urllib.request, "urlopen",
                        lambda req, timeout: _JsonResp(json.dumps(payload).encode()))
    monkeypatch.setattr(cc, "b64_jpeg", lambda path, width=1280: "aW1n")
    monkeypatch.setattr(cc, "sha", lambda path: "0" * 64)
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.invalid/v1")
    job = {"judge_model": "gpt-6.1-sol"}
    with pytest.raises(ValueError):
        cc.vision(job, "c1.judge", "问题", [(str(tmp_path / "a.png"), 1920)], str(tmp_path))
    assert not (tmp_path / "c1.judge.response.json").exists()
    assert not (tmp_path / "c1.judge.response.json.key.json").exists()
