"""CPA text path: streaming request shape, medium effort, cooldown after 524/503.  No network, no ssh."""

from __future__ import annotations

import hashlib
import http.server
import importlib.util
import io
import json
import threading
import time
import urllib.error

import pytest

from src.autoslice.manual_lane import adapters
from src.autoslice.manual_lane.adapters import RemoteResult, SshCpaJudge


def _load_cpa_text():
    spec = importlib.util.spec_from_file_location("cpa_text_under_test", adapters.CPA_TEXT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cpa_text = _load_cpa_text()


def test_request_body_asks_for_stream_and_medium_effort():
    body = cpa_text.build_body("prompt", "gpt-6.1-sol", "medium", 16000, stream=True)
    assert body["stream"] is True
    assert body["reasoning"] == {"effort": "medium"}
    assert body["model"] == "gpt-6.1-sol"


def test_non_streaming_body_has_no_stream_key():
    assert "stream" not in cpa_text.build_body("p", stream=False)


def test_terminal_identity_includes_output_budget_and_preserves_legacy_default(tmp_path):
    from types import SimpleNamespace

    path = tmp_path / 'out.json'
    prompt = 'synthetic prompt'
    record = {'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(), 'model': 'm',
              'effort': 'high', 'stream': True, 'complete': True, 'text': '{}'}
    path.write_text(json.dumps(record), encoding='utf-8')
    args = SimpleNamespace(model='m', effort='high', stream=True, max_tokens=16000)
    assert cpa_text.previous_output(path, prompt, args) == record
    args.max_tokens = 32768
    assert cpa_text.previous_output(path, prompt, args) is None
    record['max_output_tokens'] = 32768
    path.write_text(json.dumps(record), encoding='utf-8')
    assert cpa_text.previous_output(path, prompt, args) == record
    args.max_tokens = 16000
    assert cpa_text.previous_output(path, prompt, args) is None


def test_retry_schedule_respects_server_minimum_and_retries_no_reply_once_immediately():
    assert cpa_text.wait_seconds(1) == 0
    assert 2 <= cpa_text.wait_seconds(2) < 3
    assert cpa_text.wait_seconds(1, 429, "240") >= 240
    assert cpa_text.wait_seconds(1, 503, "300") >= 300
    assert adapters.CPA_TEXT_TRIES == 10
    assert cpa_text.IDLE_TIMEOUT_SECONDS > 230


def test_legacy_unknown_attempts_are_counted_and_exhaustion_survives_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.invalid/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-only")
    monkeypatch.setattr(cpa_text.time, "sleep", lambda _delay: None)
    prompt, out, journal = tmp_path / "prompt.txt", tmp_path / "reply.json", tmp_path / "journal"
    prompt.write_text("问题\n", encoding="utf-8")
    old = {"model": "gpt-6.1-sol", "effort": "medium", "stream": True,
           "prompt_sha256": hashlib.sha256(prompt.read_bytes()).hexdigest(), "complete": False,
           "reason": "ERROR TimeoutError", "attempts": 4,
           "http_log": [{"attempt": i, "error": "TimeoutError"} for i in range(1, 5)]}
    out.write_text(json.dumps(old), encoding="utf-8")
    old_bytes = out.read_bytes()
    calls = []

    def no_reply(req, timeout):
        calls.append((req.get_header("Idempotency-key"), timeout))
        raise urllib.error.URLError("timeout")

    monkeypatch.setattr(cpa_text.urllib.request, "urlopen", no_reply)
    args = [str(prompt), str(out), "--stream", "--journal-dir", str(journal), "--logical-id", "a" * 64]
    assert cpa_text.main(args) == 1
    assert len(calls) == 6 and len({key for key, _ in calls}) == 1
    assert json.loads(out.read_text())["attempts"] == 10
    assert (journal / (hashlib.sha256(old_bytes).hexdigest() + ".legacy-output.json")).read_bytes() == old_bytes
    assert cpa_text.main(args) == 1
    assert len(calls) == 6
    assert json.loads(out.read_text())["reason"] == "CPA_TEXT_RETRY_EXHAUSTED"


@pytest.mark.parametrize("existing_journal", [False, True])
def test_legacy_quota_refusals_survive_and_do_not_exhaust_resume(tmp_path, monkeypatch, existing_journal):
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.invalid/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-only")
    monkeypatch.setattr(cpa_text.time, "sleep", lambda _delay: None)
    prompt, out, journal = tmp_path / "prompt.txt", tmp_path / "reply.json", tmp_path / "journal"
    prompt.write_text("问题\n", encoding="utf-8")
    error = {"code": "organization_spend_limit_exceeded", "type": "insufficient_quota", "message": "any language"}
    reason = "TERMINAL error: " + json.dumps(error, ensure_ascii=False)
    old_rows = [{"attempt": i, "status": "FINISHED", "http": 200, "complete": False, "chars": 0,
                 "reason": reason} for i in range(1, 11)]
    old = {"model": "gpt-6.1-sol", "effort": "medium", "stream": True,
           "prompt_sha256": hashlib.sha256(prompt.read_bytes()).hexdigest(), "complete": False,
           "reason": reason, "attempts": 10, "http_log": old_rows}
    out.write_text(json.dumps(old), encoding="utf-8")
    original = out.read_bytes()
    if existing_journal:
        journal.mkdir()
        (journal / ("a" * 64 + ".json")).write_text(json.dumps({"request_key": "a" * 64,
                                                                  "http_log": old_rows,
                                                                  "next_not_before": 0}))
    calls = []

    def response(req, timeout):
        calls.append(req.get_header("Idempotency-key"))
        event = ({"type": "error", "error": error} if len(calls) <= 10 else
                 {"type": "response.completed", "response": {"status": "completed", "output_text": "完整"}})
        return io.BytesIO(("data: " + json.dumps(event) + "\n").encode())

    monkeypatch.setattr(cpa_text.urllib.request, "urlopen", response)
    args = [str(prompt), str(out), "--stream", "--journal-dir", str(journal), "--logical-id", "a" * 64]
    assert cpa_text.main(args) == 1
    failed = json.loads(out.read_text())
    assert len(calls) == 10 and failed["attempts"] == 20 and failed["budget_attempts"] == 0
    assert "QUOTA" in failed["reason"]
    assert all(row["quota_refusal"] is True for row in failed["http_log"])
    assert (journal / (hashlib.sha256(original).hexdigest() + ".legacy-output.json")).read_bytes() == original
    assert cpa_text.main(args) == 0
    finished = json.loads(out.read_text())
    assert len(calls) == 11 and len(set(calls)) == 1 and finished["attempts"] == 21
    assert finished["complete"] is True and finished["text"] == "完整"
    assert cpa_text.main(args) == 0 and len(calls) == 11


def test_message_text_alone_does_not_exempt_unknown_attempts(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.invalid/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-only")
    prompt, out, journal = tmp_path / "prompt", tmp_path / "out", tmp_path / "journal"
    prompt.write_text("same question")
    reason = 'TERMINAL error: {"code":"server_error","type":"server_error","message":"insufficient_quota"}'
    rows = [{"attempt": i, "status": "FINISHED", "http": 200, "complete": False, "chars": 0,
             "reason": reason} for i in range(1, 11)]
    out.write_text(json.dumps({"model": "gpt-6.1-sol", "effort": "medium", "stream": True,
                               "prompt_sha256": hashlib.sha256(prompt.read_bytes()).hexdigest(),
                               "complete": False, "attempts": 10, "http_log": rows}))
    monkeypatch.setattr(cpa_text.urllib.request, "urlopen", lambda *_a, **_kw: pytest.fail("budget bypass"))
    assert cpa_text.main([str(prompt), str(out), "--stream", "--journal-dir", str(journal),
                          "--logical-id", "b" * 64]) == 1
    assert json.loads(out.read_text())["reason"] == "CPA_TEXT_RETRY_EXHAUSTED"


@pytest.mark.parametrize("overrides", [
    {"status": "STARTED"}, {"http": None}, {"chars": 2}, {"complete": True}, {"complete": None},
])
def test_quota_code_without_a_definite_text_free_refusal_is_not_exempt(overrides):
    row = {"status": "FINISHED", "http": 200, "chars": 0, "complete": False,
           "provider_error": {"code": "organization_spend_limit_exceeded", "type": "insufficient_quota"}}
    assert not cpa_text.quota_refusal({**row, **overrides})


def test_mixed_quota_and_unknown_attempts_remain_bounded_across_resumes(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.invalid/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-only")
    monkeypatch.setattr(cpa_text.time, "sleep", lambda _delay: None)
    prompt, out, journal = tmp_path / "prompt", tmp_path / "out", tmp_path / "journal"
    prompt.write_text("mixed")
    calls = []

    def response(req, timeout):
        calls.append(req.get_header("Idempotency-key"))
        if len(calls) % 2 == 0:
            raise urllib.error.URLError("timeout")
        event = {"type": "error", "error": {"code": "insufficient_quota", "type": "insufficient_quota"}}
        return io.BytesIO(("data: " + json.dumps(event) + "\n").encode())

    monkeypatch.setattr(cpa_text.urllib.request, "urlopen", response)
    args = [str(prompt), str(out), "--stream", "--journal-dir", str(journal), "--logical-id", "c" * 64]
    assert cpa_text.main(args) == 1
    assert len(calls) == 10 and json.loads(out.read_text())["budget_attempts"] == 5
    assert cpa_text.main(args) == 1
    assert len(calls) == 20 and json.loads(out.read_text())["budget_attempts"] == 10
    assert cpa_text.main(args) == 1 and len(calls) == 20
    assert json.loads(out.read_text())["reason"] == "CPA_TEXT_RETRY_EXHAUSTED"
    assert len(set(calls)) == 1


@pytest.mark.parametrize("status", [400, 429])
def test_structured_http_quota_error_is_recorded_and_does_not_spend_budget(tmp_path, monkeypatch, status):
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.invalid/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-only")
    monkeypatch.setattr(cpa_text.time, "sleep", lambda _delay: None)
    prompt, out, journal = tmp_path / "prompt", tmp_path / "out", tmp_path / "journal"
    prompt.write_text("http quota")
    calls = []

    def response(req, timeout):
        calls.append(1)
        payload = {"error": {"message": "x" * 400, "code": "organization_spend_limit_exceeded",
                             "type": "insufficient_quota"}}
        raise urllib.error.HTTPError(req.full_url, status, "quota", {}, io.BytesIO(json.dumps(payload).encode()))

    monkeypatch.setattr(cpa_text.urllib.request, "urlopen", response)
    assert cpa_text.main([str(prompt), str(out), "--stream", "--journal-dir", str(journal),
                          "--logical-id", "d" * 64]) == 1
    result = json.loads(out.read_text())
    assert len(calls) == (10 if status == 429 else 1)
    assert result["budget_attempts"] == 0 and "QUOTA" in result["reason"]
    assert all(row["quota_refusal"] is True for row in result["http_log"])


def test_completed_stream_is_reused_across_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.invalid/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-only")
    prompt, out, journal = tmp_path / "prompt.txt", tmp_path / "reply.json", tmp_path / "journal"
    prompt.write_text("同一问题\n", encoding="utf-8")
    lines = [b'data: {"type":"response.output_text.delta","delta":"ok"}\n',
             b'data: {"type":"response.completed","response":{"status":"completed"}}\n']
    calls = []

    def response(req, timeout):
        calls.append(timeout)
        return io.BytesIO(b"".join(lines))

    monkeypatch.setattr(cpa_text.urllib.request, "urlopen", response)
    options = ["--stream", "--journal-dir", str(journal), "--logical-id", "b" * 64]
    assert cpa_text.main([str(prompt), str(out), *options]) == 0
    other = tmp_path / "other.json"
    assert cpa_text.main([str(prompt), str(other), *options]) == 0
    assert calls == [600]
    assert json.loads(other.read_text())["text"] == "ok"


def test_a_live_stream_can_outlast_its_socket_idle_timeout(tmp_path, monkeypatch):
    """Real local SSE bytes keep arriving: total duration may exceed the idle limit."""
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):  # noqa: N802 - stdlib's handler method
            self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(1)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for char in "abcd":
                self.wfile.write(f'data: {{"type":"response.output_text.delta","delta":"{char}"}}\n\n'.encode())
                self.wfile.flush()
                time.sleep(0.35)
            self.wfile.write(b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n')
            self.wfile.flush()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        monkeypatch.setenv("CPA_BASE_URL", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setenv("CPA_API_KEY", "test-only")
        monkeypatch.setattr(cpa_text, "IDLE_TIMEOUT_SECONDS", 1)
        prompt, out = tmp_path / "prompt.txt", tmp_path / "out.json"
        prompt.write_text("慢回复", encoding="utf-8")
        started = time.monotonic()
        assert cpa_text.main([str(prompt), str(out), "--stream"]) == 0
        assert time.monotonic() - started > cpa_text.IDLE_TIMEOUT_SECONDS
        assert json.loads(out.read_text())["text"] == "abcd" and len(requests) == 1
    finally:
        server.shutdown()
        worker.join()
        server.server_close()


def test_judge_constants_are_streaming_medium_on_gpt_6_1_sol():
    assert adapters.CPA_JUDGE_MODEL == "gpt-6.1-sol"
    assert adapters.CPA_JUDGE_EFFORT == "medium"


def test_judge_adapter_sends_stream_and_medium_effort(monkeypatch):
    seen = {}

    class Remote:
        def put(self, local, remote_path):
            seen.setdefault("puts", []).append(remote_path)

        def run_script(self, *, name, body, timeout):
            if name == "cpa-read-s-test":
                payload = {"text": "判定结果", "complete": True, "attempts": 1, "http_log": [{"attempt": 1, "http": 200}],
                           "model": "gpt-6.1-sol", "effort": "medium", "stream": True,
                           "prompt_sha256": hashlib.sha256("问题\n".encode()).hexdigest()}
                return RemoteResult(0, json.dumps(payload, ensure_ascii=False), "")
            seen["body"] = body
            seen["timeout"] = timeout
            return RemoteResult(0, "", "")

    def no_ssh(*args, **kwargs):
        raise AssertionError("CPA should read through its Remote")

    monkeypatch.setattr(adapters, "_ssh", no_ssh)
    record = SshCpaJudge(Remote()).ask(call_id="s-test", prompt_text="问题", remote_dir="/r/clip")
    body = seen["body"]
    assert "cpa_text.py" in body
    assert "--stream" in body
    assert "--effort medium" in body
    assert "--model gpt-6.1-sol" in body
    assert seen["timeout"] is None
    assert record["response_text"] == "判定结果"
    assert record["stream"] is True and record["effort"] == "medium" and record["rc"] == 0


@pytest.mark.parametrize('receipt_budget,accepted', [(32768, True), (16000, False)])
def test_judge_adapter_binds_the_requested_output_budget(receipt_budget, accepted):
    seen = {}

    class Remote:
        def put(self, local, remote_path):
            pass

        def run_script(self, *, name, body, timeout):
            if name == 'cpa-read-agent-001':
                payload = {'text': '{}', 'complete': True, 'model': 'gpt-6.1-sol', 'effort': 'high',
                           'max_output_tokens': receipt_budget, 'stream': True,
                           'prompt_sha256': hashlib.sha256('prompt\n'.encode()).hexdigest()}
                return RemoteResult(0, json.dumps(payload), '')
            seen['body'] = body
            return RemoteResult(0, '', '')

    record = SshCpaJudge(Remote()).ask(call_id='agent-001', prompt_text='prompt', effort='high',
                                       remote_dir='/r/clip', max_output_tokens=32768)
    assert '--max-tokens 32768' in seen['body']
    assert (record['rc'] == 0) is accepted
    if not accepted:
        assert 'CPA_RESULT_IDENTITY_MISMATCH' in record['stderr_tail']


def test_stream_aggregation_joins_deltas():
    lines = [b'data: {"type":"response.output_text.delta","delta":"{\\"a\\""}',
             b'data: {"type":"response.output_text.delta","delta":": 1}"}',
             b'data: {"type":"response.completed","response":{"status":"completed","usage":{"output_tokens":3}}}',
             b"data: [DONE]"]
    text, usage, completion = cpa_text.collect_stream_text(lines)
    assert text == '{"a": 1}'
    assert usage == {"output_tokens": 3}
    assert completion["complete"] is True


def test_stream_falls_back_to_completed_response_when_no_delta():
    lines = ['data: {"type":"response.completed","response":{"status":"completed","output":[{"type":"message",'
             '"status":"completed","content":[{"text":"完整"}]}]}}'.encode()]
    text, _, completion = cpa_text.collect_stream_text(lines)
    assert text == "完整" and completion["complete"] is True


@pytest.mark.parametrize("status", [524, 503])
def test_cooldown_after_524_or_503_is_at_least_150_seconds(status):
    assert cpa_text.wait_seconds(1, status) >= cpa_text.COOLDOWN_SECONDS


def test_ssh_retries_connection_level_failures_only(monkeypatch):
    from src.autoslice.manual_lane import adapters as ad

    codes = iter([255, 255, 0])
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return type("P", (), {"returncode": next(codes), "stdout": b"ok", "stderr": b""})()

    monkeypatch.setattr(ad.subprocess, "run", fake_run)
    monkeypatch.setattr(ad.time, "sleep", lambda s: None)
    assert ad._ssh("oci3", "true", timeout=5).rc == 0
    assert len(calls) == 3

    calls.clear()
    monkeypatch.setattr(ad.subprocess, "run", lambda argv, **kw: type("P", (), {
        "returncode": 3, "stdout": b"", "stderr": b"remote failed"})())
    assert ad._ssh("oci3", "false", timeout=5).rc == 3   # a remote exit code is never retried
