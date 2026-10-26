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
                        "provider_endpoint_host": "cpa.example.invalid",
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
        "provider_endpoint_host": "cpa.example.invalid",
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
                        "provider_endpoint_host": "cpa.example.invalid",
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
        "provider_endpoint_host": "cpa.example.invalid",
        "provider_http_status": 401,
        "provider_error_code": "invalid_api_key",
        "provider_error_message": "Invalid API key token=<redacted>",
    }
    assert "remote-secret" not in str(raised.value)


def test_local_cpa_transport_failure_retains_closed_curl_fields_only():
    stderr = (
        "curl: unsafe response token=not-for-a-receipt\n"
        "[cpa] model=gpt-6.1-sol attempt=1 http=000 curl_exit=28 "
        "empty_completion=0 body_bytes=123 result=failed\n"
    )
    diagnostics = llm_client._command_provider_diagnostics(stderr, None)
    assert diagnostics == {
        "provider_error_code": "CURL_EXIT_28",
        "provider_error_message": "CPA http=000 curl_exit=28 empty_completion=0",
    }
    assert "not-for-a-receipt" not in json.dumps(diagnostics)


def test_malformed_local_cpa_line_cannot_supply_diagnostics():
    assert llm_client._command_provider_diagnostics(
        "[cpa] model=gpt-6.1-sol attempt=1 http=401 curl_exit=22 "
        "empty_completion=0 body_bytes=123 result=failed token=secret", None,
    ) == {}
