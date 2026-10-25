"""Every ending of a final-media provider dispatch leaves auditable originals.

Real incident (run auto-462db5d83eb6c4ec-00000000510b): the adapter reached the
provider call, ended through the ambiguous branch (rc 2, no response by
design), the adapter attempt directory stayed empty and the runner discarded
the adapter stderr. 41:1648 keeps such a dispatch UNKNOWN and its failure on
the books; these tests pin that every ending (normal, HTTP error, timeout,
unexpected exception) now leaves create-only records, and that "dispatched
but no reply" is never recorded as NOT_DISPATCHED.
"""
from __future__ import annotations

from datetime import timedelta
import functools
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error

import pytest

import scripts.final_media_review_gemini_api_adapter as adapter
from src.autoslice.final_media_review_model_capability_sentinel import (
    run_model_capability_sentinel,
)
from src.autoslice.gemini_file_api import (
    GeminiFileApiError,
    GeminiFileCallOutcome,
    UrllibGeminiFileTransport,
    call_file_model,
    run_free_key_file_call,
)
from tests.test_final_media_review_gemini_api_adapter import (
    FakeFileRunner,
    _final_fixture,
    _review,
    _sentinel_fixture,
)
from tests.test_final_media_review_model_capability_sentinel import (
    START,
    _runtime as _sentinel_runtime,
    _tokens,
)
from tests.test_gemini_file_api import FakeTransport


class AudioTransport(FakeTransport):
    """Helper fake transport whose remote resource reports the audio MIME type."""

    @staticmethod
    def _file(state, mime):
        return FakeTransport._file(state, "audio/wav")


def _attempt_dir(root: Path) -> Path:
    attempt, = root.glob("final-media-review-gemini-api-runs/*/attempt-*")
    return attempt


def _main(request, response, media, prompt, environment, fake, script, monkeypatch):
    monkeypatch.setattr(adapter, "_file_runner", lambda: fake)
    monkeypatch.setattr(adapter, "__file__", str(script))
    monkeypatch.setattr(os, "environ", environment)
    return adapter.main(["--request", str(request), "--response", str(response),
                         "--input", str(media), "--prompt", str(prompt)])


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------ adapter endings

def test_normal_outcome_leaves_dispatch_raw_and_evidence(tmp_path):
    root, script, request, response, media, prompt, environment = _sentinel_fixture(tmp_path)
    adapter.run_sentinel(request_path=request, response_path=response, input_path=media,
                         prompt_path=prompt, environment=environment,
                         file_runner=FakeFileRunner(["12345678"]), adapter_path=script)
    attempt = _attempt_dir(root)
    assert sorted(p.name for p in attempt.iterdir()) == [
        "adapter-evidence.json", "sentinel-dispatch.json", "sentinel-provider-outcome.json"]
    dispatch = _read(attempt / "sentinel-dispatch.json")
    assert dispatch["status"] == "DISPATCH_STARTED"
    assert dispatch["request_sha256"] == _read(attempt / "adapter-evidence.json")["request_sha256"]
    assert dispatch["media_sha256"] == adapter._sha256(media)
    assert _read(attempt / "sentinel-provider-outcome.json")["raw_response_text"] == "12345678"
    assert "free-key-for-test" not in "".join(p.read_text() for p in attempt.iterdir())


def test_incident_shape_http_5xx_keeps_error_original_and_no_response(tmp_path, monkeypatch):
    """rc 2 + no response (runner -> DISPATCH_AMBIGUOUS), but the attempt dir is not empty."""
    root, script, request, response, media, prompt, environment = _sentinel_fixture(tmp_path)
    body = '{"error": {"code": 503, "message": "The model is overloaded.", "status": "UNAVAILABLE"}}'
    error = GeminiFileApiError("GEMINI_API_SERVER_ERROR", "The model is overloaded.",
                               phase="generate_content", http_status=503, ambiguous=True,
                               raw_body=body,
                               failures=[{"key_name": "GEMINI_API_KEY", "phase": "generate_content",
                                          "http_status": 503, "ambiguous": True}],
                               cleanup_status="DELETED_AFTER_FAILURE")
    error.http_requests_completed = 3
    rc = _main(request, response, media, prompt, environment,
               FakeFileRunner([], error=error), script, monkeypatch)
    assert rc == 2
    assert not response.exists()  # ambiguous: no explicit verdict for the outer dedupe
    attempt = _attempt_dir(root)
    assert sorted(p.name for p in attempt.iterdir()) == [
        "sentinel-dispatch.json", "sentinel-provider-error.json"]
    record = _read(attempt / "sentinel-provider-error.json")
    assert record["dispatch_outcome"] == "UNKNOWN"
    assert record["ambiguous"] is True
    assert (record["phase"], record["http_status"]) == ("generate_content", 503)
    assert record["raw_error_body"] == body
    assert record["http_requests_completed"] == 3
    assert record["cleanup_status"] == "DELETED_AFTER_FAILURE"
    assert record["failures"][0]["http_status"] == 503


def test_explicit_http_4xx_keeps_error_original_and_writes_error_response(tmp_path, monkeypatch):
    root, script, request, response, media, prompt, environment = _sentinel_fixture(tmp_path)
    error = GeminiFileApiError("GEMINI_API_REQUEST_FAILED", "model not found",
                               phase="generate_content", http_status=404,
                               raw_body='{"error": {"code": 404}}',
                               failures=[{"key_name": "GEMINI_API_KEY", "http_status": 404}])
    rc = _main(request, response, media, prompt, environment,
               FakeFileRunner([], error=error), script, monkeypatch)
    assert rc == 2
    assert _read(response)["error"]["provider_http_status"] == 404
    record = _read(_attempt_dir(root) / "sentinel-provider-error.json")
    assert record["dispatch_outcome"] == "PROVIDER_RESPONDED"
    assert record["raw_error_body"] == '{"error": {"code": 404}}'


def test_timeout_keeps_error_record_and_stays_unknown(tmp_path, monkeypatch):
    root, script, request, response, media, prompt, environment = _sentinel_fixture(tmp_path)
    error = GeminiFileApiError("GEMINI_API_TIMEOUT", "upload_bytes did not return a definite response",
                               phase="upload_bytes", ambiguous=True)
    rc = _main(request, response, media, prompt, environment,
               FakeFileRunner([], error=error), script, monkeypatch)
    assert rc == 2 and not response.exists()
    record = _read(_attempt_dir(root) / "sentinel-provider-error.json")
    assert (record["reason_code"], record["phase"], record["dispatch_outcome"]) == (
        "GEMINI_API_TIMEOUT", "upload_bytes", "UNKNOWN")


def test_unexpected_exception_is_recorded_and_ambiguous(tmp_path, monkeypatch):
    root, script, request, response, media, prompt, environment = _sentinel_fixture(tmp_path)
    rc = _main(request, response, media, prompt, environment,
               FakeFileRunner([], error=RuntimeError("socket wrapper exploded")),
               script, monkeypatch)
    assert rc == 2
    assert not response.exists()  # never converted into an explicit, retryable verdict
    record = _read(_attempt_dir(root) / "sentinel-provider-error.json")
    assert record["error_type"] == "RuntimeError"
    assert record["reason_code"] == "GEMINI_API_ADAPTER_EXCEPTION"
    assert record["ambiguous"] is True and record["dispatch_outcome"] == "UNKNOWN"
    assert "socket wrapper exploded" in record["detail"]


def test_returned_outcome_is_saved_before_its_binding_is_rejected(tmp_path):
    root, script, request, response, media, prompt, environment = _sentinel_fixture(tmp_path)

    def drifted(**kwargs):
        outcome = FakeFileRunner(["12345678"])(**kwargs)
        return GeminiFileCallOutcome(text=outcome.text,
                                     evidence={**outcome.evidence, "media_sha256": "f" * 64})

    with pytest.raises(adapter.AdapterError, match="does not bind"):
        adapter.run_sentinel(request_path=request, response_path=response, input_path=media,
                             prompt_path=prompt, environment=environment, file_runner=drifted,
                             adapter_path=script)
    saved = _read(_attempt_dir(root) / "sentinel-provider-outcome.json")
    assert saved["raw_response_text"] == "12345678"
    assert saved["provider_usage"]["media_sha256"] == "f" * 64


def test_only_first_key_local_rejection_is_not_dispatched():
    local = GeminiFileApiError("GEMINI_API_KEYS_MISSING", "none", phase="key_selection")
    assert adapter._dispatch_outcome(local) == "NOT_DISPATCHED"
    after_403 = GeminiFileApiError("GEMINI_API_LOCAL_FILE_INVALID", "gone", phase="local_file",
                                   failures=[{"http_status": 403}, {"phase": "local_file"}])
    assert adapter._dispatch_outcome(after_403) == "PROVIDER_RESPONDED"
    after_upload = GeminiFileApiError("GEMINI_API_REQUEST_INVALID", "generate body",
                                      phase="local_request", failures=[{"phase": "local_request"}])
    after_upload.http_requests_completed = 2  # upload answered before the local generate rejection
    assert adapter._dispatch_outcome(after_upload) == "PROVIDER_RESPONDED"
    ambiguous = GeminiFileApiError("GEMINI_API_LOCAL_FILE_DRIFT", "changed",
                                   phase="local_file_postflight", ambiguous=True)
    assert adapter._dispatch_outcome(ambiguous) == "UNKNOWN"


def test_final_review_keeps_first_modality_raw_and_second_modality_error(tmp_path):
    root, script, request, response, _video, _audio, environment = _final_fixture(tmp_path)
    calls = []

    def runner(**kwargs):
        calls.append(kwargs["mime_type"])
        if kwargs["mime_type"] == "audio/wav":
            raise GeminiFileApiError("GEMINI_API_NETWORK_ERROR", "reset", phase="upload_start",
                                     ambiguous=True)
        return FakeFileRunner([_review("PASS", "video-whole")])(**kwargs)

    with pytest.raises(adapter.AdapterError) as caught:
        adapter.run_final_review(request_path=request, response_path=response,
                                 environment=environment, file_runner=runner, adapter_path=script)
    assert caught.value.ambiguous is True
    attempt = _attempt_dir(root)
    names = {p.name for p in attempt.iterdir()}
    assert {"continuous_source_video-dispatch.json", "continuous_source_video-provider-outcome.json",
            "raw_audio-dispatch.json", "raw_audio-provider-error.json"} <= names
    assert _read(attempt / "raw_audio-provider-error.json")["dispatch_outcome"] == "UNKNOWN"
    assert not response.exists()


def test_real_helper_chain_records_phase_progress_and_cleanup(tmp_path):
    """The production helper (fake transport, no network) feeds the same record."""
    root, script, request, response, media, prompt, environment = _sentinel_fixture(tmp_path)

    class Overloaded(AudioTransport):
        def generate(self, **kwargs):
            self.calls.append({"phase": "generate_content"})
            raise GeminiFileApiError("GEMINI_API_SERVER_ERROR", "overloaded",
                                     phase="generate_content", http_status=503, ambiguous=True,
                                     raw_body='{"error": {"code": 503}}')

    transport = Overloaded()
    real = functools.partial(run_free_key_file_call, transport_factory=lambda: transport,
                             sleeper=lambda _s: None)
    with pytest.raises(adapter.AdapterError) as caught:
        adapter.run_sentinel(request_path=request, response_path=response, input_path=media,
                             prompt_path=prompt, environment=environment, file_runner=real,
                             adapter_path=script)
    assert caught.value.ambiguous is True
    record = _read(_attempt_dir(root) / "sentinel-provider-error.json")
    assert record["http_requests_completed"] == 2  # upload start + bytes; generate unanswered
    assert record["cleanup_status"] == "DELETED_AFTER_FAILURE"
    assert record["failures"] == [{
        "key_tier": "free", "key_name": "GEMINI_API_KEY", "key_ordinal": 1,
        "reason_code": "GEMINI_API_SERVER_ERROR", "phase": "generate_content",
        "ambiguous": True, "cleanup_status": "DELETED_AFTER_FAILURE", "http_status": 503}]
    assert record["raw_error_body"] == '{"error": {"code": 503}}'


# ------------------------------------------------------------- helper originals

def test_generate_without_text_keeps_returned_payload(tmp_path):
    media = tmp_path / "a.wav"
    media.write_bytes(b"RIFF bytes")

    class Blocked(AudioTransport):
        def generate(self, **kwargs):
            self.calls.append({"phase": "generate_content"})
            return {"candidates": [{"finishReason": "SAFETY"}], "modelVersion": "gemini-3.8-flash",
                    "usageMetadata": {"promptTokenCount": 120, "totalTokenCount": 120}}

    with pytest.raises(GeminiFileApiError) as caught:
        call_file_model(media_path=media, expected_sha256=adapter._sha256(media),
                        expected_bytes=media.stat().st_size, mime_type="audio/wav",
                        prompt="digits", key="free-key", model="gemini-3.8-flash", video_fps=None,
                        timeout_seconds=30, poll_timeout_seconds=30, poll_interval_seconds=0,
                        transport=Blocked(), sleeper=lambda _s: None)
    assert caught.value.ambiguous is False
    assert caught.value.response_payload["usageMetadata"]["totalTokenCount"] == 120
    assert caught.value.http_requests_completed == 3


def test_http_error_keeps_the_returned_body():
    def urlopen(_request, timeout):
        raise urllib.error.HTTPError("https://generativelanguage.googleapis.com", 503,
                                     "Service Unavailable", {},
                                     io.BytesIO(b'{"error":\n  {"code": 503, "status": "UNAVAILABLE"}}'))

    with pytest.raises(GeminiFileApiError) as caught:
        UrllibGeminiFileTransport(urlopen=urlopen).generate(
            key="k", model="gemini-3.8-flash",
            file_uri="https://generativelanguage.googleapis.com/v1beta/files/x",
            mime_type="audio/wav", prompt="p", video_fps=None, timeout_seconds=30)
    assert caught.value.ambiguous is True and caught.value.http_status == 503
    assert caught.value.raw_body == '{"error":\n  {"code": 503, "status": "UNAVAILABLE"}}'


# -------------------------------------------------------------- runner endings

def _sentinel_run(root, runner, run, environment, *, now=START):
    return run_model_capability_sentinel(
        runtime_root=root, run_id="sentinel-run-1", valid_for_seconds=86_400,
        runner_path=runner, now=now, environment=environment, run=run,
        token_factory=_tokens)


def _process_record(root: Path) -> tuple[dict[str, object], dict[str, object]]:
    state = _read(root / "model-capability-sentinel-runs/sentinel-run-1/raw_audio/state.json")
    binding = state["adapter_process"]
    path = root / "model-capability-sentinel-runs/sentinel-run-1" / binding["path"]
    return state, _read(path)


def test_runner_keeps_adapter_stderr_when_no_response_is_written(tmp_path):
    root, runner, _adapter = _sentinel_runtime(tmp_path)
    environment = {"PATH": str(Path(sys.executable).parent),
                   "GEMINI_API_KEY": "free-key-secret-value"}
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 2, "", "GEMINI_API_SERVER_ERROR: overloaded (key free-key-secret-value)\n")

    first = _sentinel_run(root, runner, run, environment)
    assert (first["status"], first["reason_code"]) == ("DISPATCH_AMBIGUOUS", "SENTINEL_RESPONSE_MISSING")
    state, record = _process_record(root)
    assert state["adapter_returncode"] == 2
    assert record["returncode"] == 2 and record["response_present"] is False
    assert "GEMINI_API_SERVER_ERROR: overloaded" in record["stderr_tail"]
    assert "free-key-secret-value" not in json.dumps(record)
    assert "<redacted:GEMINI_API_KEY>" in record["stderr_tail"]
    again = _sentinel_run(root, runner, run, environment, now=START + timedelta(minutes=1))
    assert again["status"] == "DISPATCH_AMBIGUOUS" and again["provider_calls"] == 0
    assert len(calls) == 1  # UNKNOWN is never replayed


def test_runner_timeout_keeps_partial_streams_and_stays_unknown(tmp_path):
    root, runner, _adapter = _sentinel_runtime(tmp_path)
    environment = {"PATH": str(Path(sys.executable).parent)}

    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=b"",
                                        stderr=b"still waiting for generate_content")

    result = _sentinel_run(root, runner, run, environment)
    assert (result["status"], result["reason_code"]) == (
        "DISPATCH_AMBIGUOUS", "SENTINEL_DISPATCH_OUTCOME_UNKNOWN")
    assert result["provider_calls"] == 1
    _state, record = _process_record(root)
    assert record["timed_out"] is True and record["returncode"] is None
    assert record["stderr_tail"] == "still waiting for generate_content"


def test_runner_spawn_failure_is_recorded_and_not_claimed_as_not_dispatched(tmp_path):
    root, runner, _adapter = _sentinel_runtime(tmp_path)

    def run(argv, **kwargs):
        raise PermissionError("adapter not executable")

    result = _sentinel_run(root, runner, run, {"PATH": str(Path(sys.executable).parent)})
    assert result["status"] == "DISPATCH_AMBIGUOUS"
    _state, record = _process_record(root)
    assert record["spawn_error"]["type"] == "PermissionError"
