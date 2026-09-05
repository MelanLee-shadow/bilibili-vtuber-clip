"""Hermetic checks for the unified web -> AGY -> API witness route."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import pwd
import signal
import subprocess

import pytest

from src.autoslice import agy_gemini_client
from src.autoslice import entity_audio_verifier as verifier
from src.autoslice.acoustic_witness_adjudication import build_witness_request
from src.autoslice.exact_source_transcript_contract import (
    build_exact_source_transcript_request,
)


class _Completed:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


class _AdapterProcess:
    def __init__(self, command, *, observed=None, response_text=None, returncode=0, timeout=False):
        self.command = command
        self.observed = observed
        self.response_text = response_text
        self.returncode = returncode
        self.timeout = timeout
        self.pid = 12345

    def communicate(self, timeout=None):
        if self.timeout:
            raise subprocess.TimeoutExpired(self.command, timeout)
        if self.observed is not None or self.response_text is not None:
            response = Path(self.command[self.command.index("--response-out") + 1])
            receipt = Path(self.command[self.command.index("--receipt") + 1])
            prompt = Path(self.command[self.command.index("--prompt-file") + 1])
            response.write_text(
                self.response_text
                if self.response_text is not None
                else json.dumps(self.observed),
                encoding="utf-8",
            )
            receipt.write_text(
                json.dumps(
                    verifier_adapter_receipt(
                        Path(self.command[3]), prompt, response, "3.1 Pro"
                    ),
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        return "", ""

    def wait(self, timeout=None):
        return self.returncode


def _request(evidence_id: str = "a" * 64) -> dict[str, object]:
    return build_witness_request(
        {
            "evidence_id": evidence_id,
            "cue_indexes": [1],
            "matched_start_ms": 1_000,
            "matched_end_ms": 2_100,
            "context_start_ms": 500,
            "context_end_ms": 2_600,
            "source_media_timeline_offset_ms": 0,
        }
    )


def _observed() -> dict[str, object]:
    return {
        "schema_version": verifier.WITNESS_SCHEMA,
        "status": "OBSERVED",
        "target_audible": True,
        "heard_pinyin": "hao piao liang o",
        "uncertain_positions": [],
        "syllable_count": 4,
        "confidence": 0.94,
        "reason": "clear blind dictation",
    }


def _set_web_env(monkeypatch, tmp_path: Path, *, enabled: str = "1", shadow: str = "1", upload: str = "0"):
    adapter = Path(__file__).parents[1] / "scripts" / "gemini_web_subscription.py"
    web_user = pwd.getpwuid(os.geteuid()).pw_name
    browser = tmp_path / "chromium"
    browser.write_bytes(b"chromium")
    browser.chmod(0o755)
    profile = tmp_path / "profile"
    profile.mkdir()
    python_target = Path(os.path.realpath(os.sys.executable))
    python_link = tmp_path / "web-python"
    python_link.symlink_to(python_target)
    for name, value in {
        verifier.ENTITY_AUDIO_GEMINI_WEB_ENABLED_ENV: enabled,
        verifier.ENTITY_AUDIO_GEMINI_WEB_MODEL_ENV: "3.1 Pro",
        verifier.ENTITY_AUDIO_GEMINI_WEB_COMMAND_ENV: str(adapter),
        verifier.ENTITY_AUDIO_GEMINI_WEB_PYTHON_ENV: str(python_link),
        verifier.ENTITY_AUDIO_GEMINI_WEB_PROFILE_ENV: str(profile),
        verifier.ENTITY_AUDIO_GEMINI_WEB_BROWSER_ENV: str(browser),
        verifier.ENTITY_AUDIO_GEMINI_WEB_USER_ENV: web_user,
        "AUTOSLICE_SHADOW_ONLY": shadow,
        "AUTOSLICE_UPLOAD_ENABLED": upload,
        verifier.ENTITY_AUDIO_DISABLE_AGY_ENV: "1",
    }.items():
        monkeypatch.setenv(name, value)
    return browser, profile


def _build_verifier(tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source media")
    return verifier.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-09-03",
        source_duration_ms=10_000,
        agy_bin=str(tmp_path / "missing-agy"),
    )


def _fake_crop(**kwargs):
    kwargs["audio_path"].write_bytes(b"cropped source audio")
    return True, ""


def _write_api_outcome(kwargs, observed):
    response_path = Path(kwargs["response_path"])
    prompt_path = response_path.parent / "prompt.gemini-api.md"
    prompt_path.write_text(kwargs["prompt"], encoding="utf-8")
    response_path.write_text(json.dumps(observed), encoding="utf-8")
    return agy_gemini_client.LadderOutcome(
        observed=observed,
        accepted_key_tier="free",
        accepted_key_ordinal=1,
        configured_key_count=1,
    )


@pytest.mark.parametrize(("shadow", "upload"), (("1", "0"), ("0", "0"), ("0", "1")))
def test_web_witness_is_first_and_receipt_bound(tmp_path, monkeypatch, shadow, upload):
    _set_web_env(monkeypatch, tmp_path, shadow=shadow, upload=upload)
    monkeypatch.setenv(verifier.ENTITY_AUDIO_DISABLE_AGY_ENV, "0")
    monkeypatch.setattr(
        agy_gemini_client,
        "run_local_agy",
        lambda *_args, **_kwargs: pytest.fail("valid web witness must preempt AGY"),
    )
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-cross-boundary")
    monkeypatch.setattr(verifier, "_crop_black_frame_audio", _fake_crop)
    calls = []
    observed = _observed()
    monkeypatch.setattr(
        verifier,
        "_run_gemini_api_fallback",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("valid web witness must preempt API ladder")
        ),
    )

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"web webm")
            return _Completed()
        raise AssertionError(f"unexpected subprocess.run: {command}")

    def fake_popen(command, **kwargs):
        calls.append(command)
        assert kwargs["start_new_session"] is True
        assert kwargs["env"] == {
            "HOME": kwargs["env"]["HOME"],
            "USER": pwd.getpwuid(os.geteuid()).pw_name,
            "LOGNAME": pwd.getpwuid(os.geteuid()).pw_name,
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        assert "UNRELATED_SECRET" not in kwargs["env"]
        return _AdapterProcess(command, observed=observed)

    monkeypatch.setattr(verifier.subprocess, "run", fake_run)
    monkeypatch.setattr(verifier.subprocess, "Popen", fake_popen)
    result = _build_verifier(tmp_path)(_request())

    assert result["status"] == "OBSERVED"
    assert result["provider"] == "gemini_web_subscription"
    assert result["provider_model_label"] == "3.1 Pro"
    assert result["provider_model_status"] == "UNVERIFIED"
    assert len(result["provider_receipt_sha256"]) == 64
    assert any("--direct-cdp" in call for call in calls)
    assert any("--browser-executable" in call for call in calls)
    assert any("--model-label" in call and "3.1 Pro" in call for call in calls)
    ffmpeg_call = next(call for call in calls if call[0] == "ffmpeg")
    assert ["-map", "0:v:0", "-map", "0:a:0"] == ffmpeg_call[ffmpeg_call.index("-map") : ffmpeg_call.index("-map") + 4]
    assert ["-c:v", "libvpx-vp9"] == ffmpeg_call[ffmpeg_call.index("-c:v") : ffmpeg_call.index("-c:v") + 2]
    assert ["-c:a", "libopus"] == ffmpeg_call[ffmpeg_call.index("-c:a") : ffmpeg_call.index("-c:a") + 2]
    assert ["-ac", "1", "-ar", "16000"] == ffmpeg_call[ffmpeg_call.index("-ac") : ffmpeg_call.index("-ac") + 4]
    assert "-vn" not in ffmpeg_call
    adapter_call = next(call for call in calls if "--direct-cdp" in call)
    configured_python = os.environ[verifier.ENTITY_AUDIO_GEMINI_WEB_PYTHON_ENV]
    assert Path(configured_python).is_symlink()
    assert configured_python in adapter_call
    job = tmp_path / "out" / "entity_verdicts" / _request()["request_sha256"][:20]
    web_input = (job / "gemini-web" / "input.webm").resolve()
    assert str(web_input) in adapter_call
    assert web_input.read_bytes() == b"web webm"
    assert not (job / "gemini-web" / "input.wav").exists()
    manifest = json.loads((job / "verdict.manifest.json").read_text())
    receipt = json.loads((job / "gemini-web" / "receipt.json").read_text())
    assert manifest["audio_clip_sha256"] == hashlib.sha256((job / "input.mp4").read_bytes()).hexdigest()
    assert manifest["provider_input_sha256"] == hashlib.sha256(web_input.read_bytes()).hexdigest()
    assert receipt["video_sha256"] == manifest["provider_input_sha256"]
    receipt_sha = hashlib.sha256((job / "gemini-web" / "receipt.json").read_bytes()).hexdigest()
    assert manifest["provider_receipt_sha256"] == receipt_sha
    assert not list((tmp_path / "out").rglob("witness-acoustic-cache.v4"))


def test_web_data_path_rejects_symlink(tmp_path):
    target = tmp_path / "data"
    target.write_bytes(b"data")
    link = tmp_path / "data-link"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="invalid file type"):
        verifier._gemini_web._web_path(link, "web data")


def verifier_adapter_receipt(audio: Path, prompt: Path, response: Path, model: str):
    from scripts import gemini_web_subscription as adapter

    return adapter.build_receipt(
        mode="run",
        status="SUCCESS",
        started_at="2026-09-03T00:00:00Z",
        finished_at="2026-09-03T00:00:01Z",
        requested_model_label=model,
        observed_model_label=model,
        video_sha256=hashlib.sha256(audio.read_bytes()).hexdigest(),
        prompt_sha256=hashlib.sha256(prompt.read_bytes()).hexdigest(),
        raw_response_sha256=hashlib.sha256(response.read_bytes()).hexdigest(),
        raw_response_path=str(response.resolve()),
        screenshots=[],
    )


@pytest.mark.parametrize("response_text", [
    "Sorry, something went wrong. Please try your request again.",
    json.dumps({**_observed(), "status": "UNCERTAIN", "reason": "audio missing"}),
    json.dumps({**_observed(), "heard_pinyin": "汉字泄漏"}),
])
def test_web_failure_falls_through_to_existing_api_ladder(tmp_path, monkeypatch, response_text):
    _set_web_env(monkeypatch, tmp_path)
    monkeypatch.setenv(verifier.ENTITY_AUDIO_DISABLE_AGY_ENV, "0")
    monkeypatch.setattr(verifier, "_crop_black_frame_audio", _fake_crop)
    api_calls = []
    route = []

    def fake_agy(*_args, **_kwargs):
        route.append("agy")
        return agy_gemini_client.AgyRun(_Completed(1), "AGY_FAILED_RC_1")

    monkeypatch.setattr(agy_gemini_client, "run_local_agy", fake_agy)

    def fake_api(**kwargs):
        route.append("api")
        api_calls.append(kwargs)
        return _write_api_outcome(kwargs, _observed())

    def fake_run(command, **_kwargs):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"web webm")
            return _Completed()
        raise AssertionError(f"unexpected subprocess.run: {command}")

    def fake_web(command, **_kwargs):
        route.append("web")
        return _AdapterProcess(command, response_text=response_text)

    monkeypatch.setattr(verifier.subprocess, "Popen", fake_web)

    monkeypatch.setattr(verifier.subprocess, "run", fake_run)
    monkeypatch.setattr(verifier, "_run_gemini_api_fallback", fake_api)
    result = _build_verifier(tmp_path)(_request())

    assert result["status"] == "OBSERVED"
    assert result["provider"] == "gemini_api"
    assert api_calls
    assert route == ["web", "agy", "api"]
    job = tmp_path / "out" / "entity_verdicts" / _request()["request_sha256"][:20]
    failures = json.loads((job / "verdict.manifest.json").read_text())["provider_failures"]
    assert any(
        row["provider"] == "gemini_web_subscription"
        and row["category"] == "GEMINI_WEB_INVALID_RESPONSE"
        for row in failures
    )


def test_web_failure_uses_agy_and_skips_web_for_remaining_run(tmp_path, monkeypatch):
    _set_web_env(monkeypatch, tmp_path, shadow="0")
    monkeypatch.setenv(verifier.ENTITY_AUDIO_DISABLE_AGY_ENV, "0")
    monkeypatch.setattr(verifier, "_crop_black_frame_audio", _fake_crop)
    monkeypatch.setattr(verifier, "_replay_provider_witness_cache", lambda **_kwargs: None)
    route = []

    def fake_web(command, **_kwargs):
        route.append("web")
        return _AdapterProcess(command, returncode=1)

    def fake_agy(*_args, **_kwargs):
        route.append("agy")
        completed = _Completed()
        completed.stdout = json.dumps(_observed())
        return agy_gemini_client.AgyRun(completed, None)

    monkeypatch.setattr(verifier.subprocess, "Popen", fake_web)
    monkeypatch.setattr(agy_gemini_client, "run_local_agy", fake_agy)
    monkeypatch.setattr(verifier.subprocess, "run", lambda command, **_kwargs: (_write_api_clip(command) or _Completed()))
    monkeypatch.setattr(verifier, "_run_gemini_api_fallback", lambda **_kwargs: pytest.fail("AGY success must preempt API"))
    call = _build_verifier(tmp_path)
    assert call(_request())["provider"] == "agy"
    second = _request("b" * 64)
    assert call(second)["provider"] == "agy"
    assert route == ["web", "agy", "agy"]
    job = tmp_path / "out" / "entity_verdicts" / second["request_sha256"][:20]
    failures = json.loads((job / "verdict.manifest.json").read_text())["provider_failures"]
    assert failures[0] == {"provider": "gemini_web_subscription", "category": "GEMINI_WEB_CIRCUIT_OPEN", "attempted": False}


def test_web_timeout_terminates_the_adapter_process_group(monkeypatch):
    signals = []

    class TimedOutProcess:
        pid = 42

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("adapter", timeout)
            return 0

    monkeypatch.setattr(verifier.os, "getpgid", lambda _pid: 4242)
    monkeypatch.setattr(
        verifier.os,
        "killpg",
        lambda pgid, sig: signals.append((pgid, sig)),
    )
    process = TimedOutProcess()

    verifier._terminate_web_process_group(process)

    assert signals == [
        (4242, signal.SIGTERM),
        (4242, signal.SIGKILL),
    ]
    assert process.waits == 2


def test_web_disabled_is_not_called(tmp_path, monkeypatch):
    _set_web_env(monkeypatch, tmp_path, enabled="0")
    monkeypatch.setattr(verifier, "_crop_black_frame_audio", _fake_crop)
    web_calls = []
    monkeypatch.setattr(
        verifier,
        "_run_gemini_web_fallback",
        lambda **_kwargs: web_calls.append(True),
    )
    monkeypatch.setattr(
        verifier,
        "_run_gemini_api_fallback",
        lambda **kwargs: _write_api_outcome(kwargs, _observed()),
    )
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda command, **_kwargs: (_write_api_clip(command) or _Completed()),
    )
    result = _build_verifier(tmp_path)(_request())
    assert result["provider"] == "gemini_api"
    assert web_calls == []


def _write_api_clip(command):
    if command[0] == "ffmpeg":
        Path(command[-1]).write_bytes(b"api audio")


def test_exact_transcript_lane_skips_web(tmp_path, monkeypatch):
    _set_web_env(monkeypatch, tmp_path)
    monkeypatch.setattr(verifier, "_crop_black_frame_audio", _fake_crop)
    web_calls = []
    api_calls = []
    monkeypatch.setattr(
        verifier,
        "_run_gemini_web_fallback",
        lambda **_kwargs: web_calls.append(True),
    )

    report = {
        "schema_version": "exact-final-source-transcript-provider-report.v1",
        "status": "OBSERVED",
        "target_audible": True,
        "audible_language": "zh",
        "exact_transcript": "你好",
        "reason": "clear",
    }

    def fake_api(**kwargs):
        api_calls.append(True)
        return _write_api_outcome(kwargs, report)

    monkeypatch.setattr(verifier, "_run_gemini_api_fallback", fake_api)
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda command, **_kwargs: (_write_api_clip(command) or _Completed()),
    )
    exact_request = build_exact_source_transcript_request(_request())
    result = _build_verifier(tmp_path).exact_source_transcript(exact_request)

    assert result["status"] == "OBSERVED"
    assert web_calls == []
    assert api_calls == [True]
