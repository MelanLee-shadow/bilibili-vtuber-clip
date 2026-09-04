"""Focused tests for the runner's explicit CPA credential-file binding."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import scripts.session_autoslice as runner


def _write_cpa_env(path: Path, *, base: str = "https://override.example.test/v1") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"CPA_BASE_URL={base}\nCPA_API_KEY=local-test-secret\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_cpa_env_default_preserves_free_base_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTOSLICE_CPA_ENV", raising=False)

    assert runner._cpa_env_path() == runner.CPA_ENV
    assert runner.CPA_ENV == runner.BASE / "cpa.env"


def test_cpa_env_override_drives_direct_song_judge_without_copying_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical_env = _write_cpa_env(tmp_path / "canonical" / "cpa.env")
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    monkeypatch.setenv("AUTOSLICE_CPA_ENV", str(canonical_env))
    monkeypatch.setattr(runner, "CPA_ENV", shadow / "cpa.env")

    command = runner.cpa_qa_cmd()

    assert runner._cpa_env_path() == canonical_env
    assert "--api-base https://override.example.test/v1" in command
    assert "local-test-secret" not in command
    assert not (shadow / "cpa.env").exists()


def test_explicit_paid_backup_cap_stays_authoritative_for_main_and_child_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cpa_env = tmp_path / "cpa.env"
    cpa_env.write_text(
        "GEMINI_PAID_BACKUP_DAILY_CAP=2000\nCPA_BASE_URL=https://cpa.example.test/v1\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("AUTOSLICE_CPA_ENV", raising=False)
    monkeypatch.setattr(runner, "CPA_ENV", cpa_env)
    monkeypatch.setenv("GEMINI_PAID_BACKUP_DAILY_CAP", "0")
    monkeypatch.setattr(runner, "BASE", tmp_path / "base")
    monkeypatch.setattr(runner, "list_segments", lambda _date: [])
    monkeypatch.setattr(runner, "write_state", lambda *_args, **_kwargs: None)

    assert runner.child_env()["GEMINI_PAID_BACKUP_DAILY_CAP"] == "0"
    assert runner.main(["--preclaim", "2099-01-01"]) == 0
    assert os.environ["GEMINI_PAID_BACKUP_DAILY_CAP"] == "0"


def test_paid_backup_cap_falls_back_to_cpa_env_for_main_and_child_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cpa_env = tmp_path / "cpa.env"
    cpa_env.write_text("GEMINI_PAID_BACKUP_DAILY_CAP=2000\n", encoding="utf-8")
    monkeypatch.delenv("AUTOSLICE_CPA_ENV", raising=False)
    monkeypatch.setattr(runner, "CPA_ENV", cpa_env)
    monkeypatch.delenv("GEMINI_PAID_BACKUP_DAILY_CAP", raising=False)
    monkeypatch.setattr(runner, "BASE", tmp_path / "base")
    monkeypatch.setattr(runner, "list_segments", lambda _date: [])
    monkeypatch.setattr(runner, "write_state", lambda *_args, **_kwargs: None)

    assert runner.child_env()["GEMINI_PAID_BACKUP_DAILY_CAP"] == "2000"
    assert runner.main(["--preclaim", "2099-01-01"]) == 0
    assert os.environ["GEMINI_PAID_BACKUP_DAILY_CAP"] == "2000"


class _Response:
    status = 200

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_cpa_health_probe_reads_explicit_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    canonical_env = _write_cpa_env(tmp_path / "canonical" / "cpa.env")
    monkeypatch.setenv("AUTOSLICE_CPA_ENV", str(canonical_env))
    monkeypatch.setattr(runner, "CPA_ENV", tmp_path / "shadow" / "cpa.env")
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_urlopen(request: object, timeout: int) -> _Response:
        del timeout
        calls.append((request.full_url, json.loads(request.data)))  # type: ignore[attr-defined]
        return _Response()

    monkeypatch.setattr(runner.urllib.request, "urlopen", fake_urlopen)

    assert runner.cpa_healthy() is True
    assert len(calls) == 1
    assert calls[0][0] == "https://override.example.test/v1/responses"
    assert calls[0][1]["model"] == "gpt-5.6-sol"


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ("relative", "absolute path"),
        ("missing", "unavailable"),
        ("symlink", "regular non-symlink"),
        ("mode", "mode 0600"),
    ],
)
def test_cpa_env_override_fails_closed_before_provider_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup: str,
    message: str,
) -> None:
    target = tmp_path / "canonical" / "cpa.env"
    if setup == "relative":
        raw = "relative/cpa.env"
    elif setup == "missing":
        raw = str(target)
    elif setup == "symlink":
        actual = _write_cpa_env(tmp_path / "actual" / "cpa.env")
        target.parent.mkdir(parents=True)
        target.symlink_to(actual)
        raw = str(target)
    else:
        _write_cpa_env(target)
        target.chmod(0o644)
        raw = str(target)

    monkeypatch.setenv("AUTOSLICE_CPA_ENV", raw)

    with pytest.raises(RuntimeError, match=message):
        runner._cpa_env_path()
