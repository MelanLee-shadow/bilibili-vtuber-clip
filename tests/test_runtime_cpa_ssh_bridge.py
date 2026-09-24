from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import llm_via_runtime_cpa_ssh as bridge


def test_runtime_cpa_ssh_bridge_writes_completion_and_safe_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    prompt = tmp_path / "prompt.txt"
    completion = tmp_path / "completion.txt"
    prompt.write_text("review fixture", encoding="utf-8")
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["input"] = kwargs["input"]
        payload = {
            "status": "PASS",
            "completion": '{"findings":[]}',
            "diagnostics": {
                "provider_transport": "ssh_runtime_cpa",
                "provider_endpoint_host": "cpacn.ivanq.top",
                "provider_endpoint_path": "/v1",
                "provider_credential_source": "/opt/bilive/autoslice/cpa.env",
                "provider_http_status": 200,
            },
        }
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload, ensure_ascii=False),
            stderr="",
        )

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    result = bridge.run_bridge(
        prompt_file=prompt,
        completion_file=completion,
        ssh_host="oci3",
        runtime_root=Path("/opt/bilive/autoslice"),
        model="gpt-6-astra",
        effort="medium",
        attempts=1,
    )

    assert completion.read_text(encoding="utf-8") == '{"findings":[]}'
    assert result["provider_endpoint_host"] == "cpacn.ivanq.top"
    command = observed["command"]
    assert command[:7] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=12",
    ]
    request = json.loads(observed["input"])
    assert request == {
        "attempts": 1,
        "effort": "medium",
        "model": "gpt-6-astra",
        "prompt": "review fixture",
        "runtime_root": "/opt/bilive/autoslice",
    }
    captured = capsys.readouterr()
    marker = bridge.DIAGNOSTIC_MARKER
    assert marker in captured.err
    assert "CPA_API_KEY" not in captured.err
    assert "stale-ambient-secret" not in captured.err


def test_runtime_cpa_ssh_bridge_preserves_only_redacted_failure_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt = tmp_path / "prompt.txt"
    completion = tmp_path / "completion.txt"
    prompt.write_text("review fixture", encoding="utf-8")

    def fake_run(_command, **_kwargs):
        payload = {
            "status": "FAILED",
            "diagnostics": {
                "provider_transport": "ssh_runtime_cpa",
                "provider_endpoint_host": "cpacn.ivanq.top",
                "provider_endpoint_path": "/v1",
                "provider_credential_source": "/opt/bilive/autoslice/cpa.env",
                "provider_http_status": 401,
                "provider_error_code": "invalid_api_key",
                "provider_error_message": ("Invalid API key; Authorization: Bearer remote-secret"),
            },
        }
        return SimpleNamespace(
            returncode=1,
            stdout=json.dumps(payload, ensure_ascii=False),
            stderr="remote-secret must never be copied",
        )

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)

    with pytest.raises(bridge.RuntimeCpaBridgeError) as raised:
        bridge.run_bridge(
            prompt_file=prompt,
            completion_file=completion,
            ssh_host="oci3",
            runtime_root=Path("/opt/bilive/autoslice"),
            model="gpt-6-astra",
            effort="medium",
            attempts=1,
        )

    diagnostics = raised.value.provider_diagnostics
    assert diagnostics["provider_http_status"] == 401
    assert diagnostics["provider_error_code"] == "invalid_api_key"
    assert diagnostics["provider_error_message"] == (
        "Invalid API key; Authorization: Bearer <redacted>"
    )
    assert "remote-secret" not in str(raised.value)
    assert not completion.exists()
