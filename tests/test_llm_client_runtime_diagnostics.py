from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice import llm_client


def test_command_transport_merges_safe_runtime_diagnostics_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, **_kwargs):
        completion = Path(command[2])
        completion.write_text('{"findings":[]}', encoding="utf-8")
        marker = llm_client.PROVIDER_DIAGNOSTIC_MARKER
        return SimpleNamespace(
            returncode=0,
            stdout="",
            stderr=(
                marker
                + json.dumps(
                    {
                        "provider_endpoint_host": "cpacn.ivanq.top",
                        "provider_endpoint_path": "/v1",
                        "provider_http_status": 200,
                    }
                )
            ),
        )

    monkeypatch.setattr(llm_client.subprocess, "run", fake_run)
    config = llm_client.LlmConfig(
        transport="command",
        command_template="bridge {prompt_file} {completion_file}",
        command_diagnostic_context={
            "provider_transport": "ssh_runtime_cpa",
            "provider_runtime_host": "oci3",
            "provider_credential_source": "/opt/bilive/autoslice/cpa.env",
        },
        command_diagnostic_sink=lambda row: captured.update(row),
    )
    call = llm_client.build_llm_call(config)

    assert call("fixture") == '{"findings":[]}'
    assert captured == {
        "provider_transport": "ssh_runtime_cpa",
        "provider_runtime_host": "oci3",
        "provider_credential_source": "/opt/bilive/autoslice/cpa.env",
        "provider_endpoint_host": "cpacn.ivanq.top",
        "provider_endpoint_path": "/v1",
        "provider_http_status": 200,
    }


def test_command_transport_redacts_failure_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(_command, **_kwargs):
        marker = llm_client.PROVIDER_DIAGNOSTIC_MARKER
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=(
                marker
                + json.dumps(
                    {
                        "provider_endpoint_host": "cpacn.ivanq.top",
                        "provider_http_status": 401,
                        "provider_error_code": "invalid_api_key",
                        "provider_error_message": ("Invalid API key token=remote-secret"),
                    }
                )
            ),
        )

    monkeypatch.setattr(llm_client.subprocess, "run", fake_run)
    call = llm_client.build_llm_call(
        llm_client.LlmConfig(
            transport="command",
            command_template="bridge {prompt_file} {completion_file}",
            command_diagnostic_context={
                "provider_transport": "ssh_runtime_cpa",
                "provider_credential_source": "/opt/bilive/autoslice/cpa.env",
            },
        )
    )

    with pytest.raises(llm_client.LlmCallError) as raised:
        call("fixture")

    assert raised.value.provider_diagnostics == {
        "provider_transport": "ssh_runtime_cpa",
        "provider_credential_source": "/opt/bilive/autoslice/cpa.env",
        "provider_endpoint_host": "cpacn.ivanq.top",
        "provider_http_status": 401,
        "provider_error_code": "invalid_api_key",
        "provider_error_message": "Invalid API key token=<redacted>",
    }
    assert "remote-secret" not in str(raised.value)
