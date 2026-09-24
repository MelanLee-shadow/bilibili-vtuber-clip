"""Provider execution observability, without changing evidence acceptance.

Subprocess/model results are synthetic. Real SC evidence is checked separately;
these tests never dispatch audio or treat a diagnostic as a quality verdict.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import urllib.error

import pytest

from src.autoslice import entity_audio_verifier as v
from tests.test_entity_audio_verifier import (
    _Completed,
    _blind_witness_request,
    _observed_blind_witness,
)


def _observe(
    tmp_path,
    monkeypatch,
    *,
    stdout="",
    stderr="",
    rc=0,
    verdict=None,
    free_keys=0,
    blocked_diagnostic=False,
):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    monkeypatch.setenv("GEMINI_PAID_BACKUP_DAILY_CAP", "0")
    monkeypatch.delenv("ENTITY_AUDIO_GEMINI_WEB_ENABLED", raising=False)
    monkeypatch.delenv("ENTITY_AUDIO_DISABLE_AGY", raising=False)
    for name in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3", "GEMINI_KEY_BACKUP"):
        monkeypatch.delenv(name, raising=False)
    for i in range(free_keys):
        name = "GEMINI_API_KEY" + ("" if i == 0 else "_" + str(i + 1))
        monkeypatch.setenv(name, f"synthetic-private-key-{i}")
    job = tmp_path / "job"
    job.mkdir()
    audio = job / "input.mp4"
    audio.write_bytes(b"synthetic-no-provider-media")
    if verdict is not None:
        (job / "verdict.json").write_text(verdict)
    if blocked_diagnostic:
        (job / "agy.execution.json").mkdir()
    completed = _Completed(rc, stdout, stderr)
    monkeypatch.setattr(
        v.agy_gemini_client,
        "run_local_agy",
        lambda *a, **k: v.agy_gemini_client.AgyRun(completed=completed, failure_category=None),
    )

    def extract(command, **kwargs):
        assert command[0] == "ffmpeg"
        Path(command[-1]).write_bytes(b"synthetic-mp3")
        return _Completed()

    monkeypatch.setattr(v.subprocess, "run", extract)
    api_calls = []

    def api(**kwargs):
        api_calls.append(kwargs["key"])
        raise urllib.error.HTTPError("https://example.invalid", 503, "test", None, None)

    monkeypatch.setattr(v, "_gemini_api_observe_witness", api)
    req = _blind_witness_request(evidence_id="a" * 64, start_ms=2000)
    req.update(target_audio_start_ms=400, target_audio_end_ms=1500)
    out = v._observe_entity_audio(
        request=req,
        candidates=[],
        audio_path=audio,
        job_dir=job,
        recording_date="2026-08-20",
        timely_context="",
        binary="synthetic-agy",
        model="gemini-3.8-flash-high",
        timeout="1s",
        agy_quota_circuit=v._AgyQuotaCircuitBreaker(),
    )
    return job, out, api_calls


@pytest.mark.parametrize(
    "stdout,verdict,expected_source",
    [
        ("", None, "stdout"),
        ("stdout explanation not a verdict", "", "verdict_file"),
        ("invalid json", None, "stdout"),
    ],
)
def test_invalid_responding_process_retains_actual_response_selection(
    tmp_path, monkeypatch, stdout, verdict, expected_source
):
    job, outcome, calls = _observe(tmp_path, monkeypatch, stdout=stdout, verdict=verdict)
    diagnostic = json.loads((job / "agy.execution.json").read_text())
    raw = verdict if verdict is not None else stdout
    assert diagnostic["returncode"] == 0
    assert diagnostic["response_source"] == expected_source
    assert diagnostic["response_bytes"] == len(raw.encode())
    assert diagnostic["response_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert diagnostic["stdout_bytes"] == len(stdout.encode())
    assert diagnostic["verdict_file_present"] is (verdict is not None)
    assert outcome.observed is None
    assert outcome.provider_failures[0]["category"] == "AGY_INVALID_OUTPUT"
    assert not calls
    route = json.loads((job / "gemini-api-route.json").read_text())
    assert route["configured_free_key_count"] == 0
    assert route["accepted_key_tier"] is None
    assert route["observation_returned"] is False


@pytest.mark.parametrize("rc", [0, 2])
def test_quota_remains_quota_even_when_process_exit_is_zero(tmp_path, monkeypatch, rc):
    job, outcome, calls = _observe(tmp_path, monkeypatch, rc=rc, stderr="Individual quota reached.")
    assert json.loads((job / "agy.execution.json").read_text())["returncode"] == rc
    assert outcome.provider_failures[0]["category"] == "AGY_QUOTA_EXHAUSTED"
    assert outcome.observed is None and not calls


def test_loaded_but_failing_keys_are_distinguishable_from_no_configuration(tmp_path, monkeypatch):
    job, outcome, calls = _observe(tmp_path, monkeypatch, free_keys=3)
    assert len(calls) == 3
    route = json.loads((job / "gemini-api-route.json").read_text())
    assert route["configured_free_key_count"] == 3
    assert outcome.observed is None
    assert all(
        f["category"] == "GEMINI_API_SERVER_ERROR"
        for f in outcome.provider_failures
        if f["provider"] == "gemini_api"
    )
    assert not any(key in (job / "gemini-api-route.json").read_text() for key in calls)


def test_valid_observation_and_no_fallback_are_unchanged(tmp_path, monkeypatch):
    raw = _observed_blind_witness()
    job, outcome, calls = _observe(tmp_path, monkeypatch, stdout=raw)
    assert outcome.observed == json.loads(raw)
    assert outcome.provider_failures == [] and not calls
    assert json.loads((job / "agy.execution.json").read_text())["response_bytes"] == len(
        raw.encode()
    )
    assert not (job / "gemini-api-route.json").exists()


def test_optional_diagnostic_write_is_not_a_new_quality_gate(tmp_path, monkeypatch):
    raw = _observed_blind_witness()
    _, outcome, calls = _observe(tmp_path, monkeypatch, stdout=raw, blocked_diagnostic=True)
    assert outcome.observed == json.loads(raw)
    assert outcome.provider_failures == [] and not calls


@pytest.mark.parametrize("link_kind", ["symbolic", "hard"])
def test_diagnostic_does_not_overwrite_a_linked_file(tmp_path, link_kind):
    import os

    protected = tmp_path / "protected.json"
    protected.write_bytes(b"unchanged independent file")
    destination = tmp_path / "execution.json"
    if link_kind == "symbolic":
        destination.symlink_to(protected)
    else:
        os.link(protected, destination)
    v._write_execution_diagnostic(destination, {"diagnostic": True})
    assert protected.read_bytes() == b"unchanged independent file"
