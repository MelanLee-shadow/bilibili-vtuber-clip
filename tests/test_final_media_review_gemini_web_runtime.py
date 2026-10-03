from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import stat
import subprocess

import pytest

from src.autoslice.final_media_review_capability_autobootstrap import (
    FinalMediaReviewCapabilityAutobootstrapError,
    ensure_runtime_raw_av_capability,
)
from src.autoslice.final_media_review_gemini_web_runtime import (
    ADAPTER_RELATIVE,
    CAPABILITY_ID,
    ENABLED_ENV,
    ENDPOINT_FAMILY,
    FFMPEG_ENV,
    FFPROBE_ENV,
    MODEL_LABEL_ENV,
    PROFILE_ENV,
    PROVIDER,
    RUNNER_RELATIVE,
    FinalMediaReviewGeminiWebRuntimeError,
    ensure_gemini_web_sentinel_runtime,
    gemini_web_route_model_id,
)
from src.autoslice.final_media_review_model_capability_sentinel import (
    SENTINEL_RUNTIME_FILENAME,
    load_sentinel_runtime,
)

START = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
REPO_ROOT = Path(__file__).resolve().parents[1]


def _copy_executable(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.write_bytes(source.read_bytes())
    destination.chmod(0o700)


def _fake_tool(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)


def _runtime_tree(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    _copy_executable(
        REPO_ROOT / "scripts/run_final_media_review_model_capability_sentinel.py",
        root / RUNNER_RELATIVE,
    )
    _copy_executable(
        REPO_ROOT / "scripts/final_media_review_gemini_web_adapter.py",
        root / ADAPTER_RELATIVE,
    )
    profile = root / "profile"
    profile.mkdir(mode=0o700)
    ffmpeg = tmp_path / "tools/ffmpeg"
    ffprobe = tmp_path / "tools/ffprobe"
    _fake_tool(ffmpeg)
    _fake_tool(ffprobe)
    environment = {
        ENABLED_ENV: "1",
        PROFILE_ENV: str(profile),
        MODEL_LABEL_ENV: "3.1 Pro",
        FFMPEG_ENV: str(ffmpeg),
        FFPROBE_ENV: str(ffprobe),
        "PATH": "/usr/bin:/bin",
    }
    return root, environment, profile.resolve()


def test_disabled_route_writes_nothing(tmp_path: Path):
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    result = ensure_gemini_web_sentinel_runtime(root, environment={})
    assert result == {
        "status": "GEMINI_WEB_RUNTIME_DISABLED",
        "created": False,
        "provider_calls": 0,
    }
    assert list(root.iterdir()) == []


def test_explicit_route_create_only_provisions_replayable_manifest(tmp_path: Path):
    root, environment, profile = _runtime_tree(tmp_path)
    first = ensure_gemini_web_sentinel_runtime(root, environment=environment)
    manifest_path = root / SENTINEL_RUNTIME_FILENAME
    first_bytes = manifest_path.read_bytes()
    second = ensure_gemini_web_sentinel_runtime(root, environment=environment)
    replayed = load_sentinel_runtime(root)
    assert first["status"] == "GEMINI_WEB_SENTINEL_RUNTIME_CREATED"
    assert first["created"] is True and first["provider_calls"] == 0
    assert second["status"] == "GEMINI_WEB_SENTINEL_RUNTIME_REUSED"
    assert second["created"] is False
    assert manifest_path.read_bytes() == first_bytes
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert replayed is not None
    assert replayed["capability_id"] == CAPABILITY_ID
    assert replayed["provider"] == PROVIDER
    assert replayed["endpoint_family"] == ENDPOINT_FAMILY
    assert replayed["model"] == gemini_web_route_model_id("3.1 Pro", profile)
    assert replayed["runner"]["path"] == RUNNER_RELATIVE.as_posix()
    assert replayed["adapter"]["path"] == ADAPTER_RELATIVE.as_posix()
    assert first["runtime_contract_sha256"] == replayed["runtime_contract_sha256"]


def test_visible_label_change_conflicts_instead_of_reusing_old_seal_route(
    tmp_path: Path,
):
    root, environment, _profile = _runtime_tree(tmp_path)
    ensure_gemini_web_sentinel_runtime(root, environment=environment)
    environment[MODEL_LABEL_ENV] = "Different Pro"
    with pytest.raises(
        FinalMediaReviewGeminiWebRuntimeError,
        match="existing sentinel runtime manifest differs",
    ):
        ensure_gemini_web_sentinel_runtime(root, environment=environment)


def test_missing_profile_fails_before_manifest_or_provider(tmp_path: Path):
    root, environment, _profile = _runtime_tree(tmp_path)
    environment[PROFILE_ENV] = str(tmp_path / "missing-profile")
    with pytest.raises(
        FinalMediaReviewGeminiWebRuntimeError,
        match="profile directory is unavailable",
    ):
        ensure_gemini_web_sentinel_runtime(root, environment=environment)
    assert not (root / SENTINEL_RUNTIME_FILENAME).exists()


def test_autobootstrap_provisions_then_calls_bound_runner_without_provider_claim(
    tmp_path: Path,
):
    root, environment, _profile = _runtime_tree(tmp_path)
    commands: list[list[str]] = []

    def prepared(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        value = {
            "status": "PREPARED",
            "provider_calls": 0,
            "reason_code": "SENTINEL_PROVIDER_CAPACITY_UNAVAILABLE",
            "challenge_states": {},
        }
        return subprocess.CompletedProcess(command, 75, json.dumps(value), "")

    result = ensure_runtime_raw_av_capability(
        root, now=START, environment=environment, run=prepared
    )
    assert result["status"] == "PREPARED"
    assert result["runner_called"] is True and result["provider_calls"] == 0
    assert result["runtime_provisioning"]["status"] == (
        "GEMINI_WEB_SENTINEL_RUNTIME_CREATED"
    )
    assert commands[0][0] == str((root / RUNNER_RELATIVE).resolve())
    assert (root / SENTINEL_RUNTIME_FILENAME).is_file()
    assert not (root / "final-media-review-raw-av-capability.json").exists()


def test_autobootstrap_translates_explicit_runtime_config_error(tmp_path: Path):
    root, environment, _profile = _runtime_tree(tmp_path)
    environment[MODEL_LABEL_ENV] = ""
    with pytest.raises(
        FinalMediaReviewCapabilityAutobootstrapError,
        match="FINAL_MEDIA_REVIEW_GEMINI_WEB_CONFIG_MISSING",
    ):
        ensure_runtime_raw_av_capability(
            root,
            now=START,
            environment=environment,
            run=lambda *_args, **_kwargs: pytest.fail(
                "runner must not start when explicit config is incomplete"
            ),
        )
    assert not (root / SENTINEL_RUNTIME_FILENAME).exists()
