"""Config-shape lock for the final-review CPA transports.

(维护者): the candidate-level pronoun audit is a closed 4-token
classification rerun every exact-final/self-heal round; it does not need the
same reasoning effort as the A-class entity/boundary judges, so it gets its
own builder (same approved model chain, effort medium -> low).  These tests
pin both command_templates so a future edit cannot silently widen the model
chain or re-couple the two builders while "fixing" one of them.
"""

import os
from pathlib import Path
import shlex

import pytest

from src.autoslice import producer_final_review_transport as transport


def _captured_config(monkeypatch, build_fn):
    captured: list = []

    def _capture(config):
        captured.append(config)
        return lambda _prompt: "{}"

    monkeypatch.setattr(transport, "build_llm_call", _capture)
    monkeypatch.setattr(
        transport,
        "runtime_cpa_command_environment",
        lambda _path: {
            "CPA_BASE_URL": "https://runtime.example.test/v1",
            "CPA_API_KEY": "runtime-secret",
        },
    )
    build_fn(runtime_root="/opt/bilive/autoslice")
    assert len(captured) == 1
    return captured[0]


def test_final_review_builder_keeps_medium_effort(monkeypatch):
    config = _captured_config(monkeypatch, transport.build_final_review_llm_call)

    assert config.transport == "command"
    assert "'gpt-6-sol'" in config.command_template
    assert config.command_template.split()[-2] == "medium"
    assert config.timeout_seconds == 600.0


def test_pronoun_audit_builder_uses_sol_head_and_low_effort(monkeypatch):
    config = _captured_config(monkeypatch, transport.build_pronoun_audit_llm_call)

    assert config.transport == "command"
    assert "'gpt-6-sol'" in config.command_template
    assert config.command_template.split()[-2] == "low"
    assert config.timeout_seconds == 600.0


def test_pronoun_audit_differs_from_final_review_only_in_model_head_and_effort(monkeypatch):
    final_review_config = _captured_config(
        monkeypatch, transport.build_final_review_llm_call
    )
    pronoun_config = _captured_config(
        monkeypatch, transport.build_pronoun_audit_llm_call
    )

    assert final_review_config.command_template.replace(
        "medium", "low"
    ) == pronoun_config.command_template
    assert final_review_config.timeout_seconds == pronoun_config.timeout_seconds


@pytest.mark.parametrize(
    ("build_fn", "effort"),
    (
        (transport.build_final_review_llm_call, "medium"),
        (transport.build_pronoun_audit_llm_call, "low"),
    ),
)
def test_review_builders_bind_explicit_runtime_cpa_environment(
    tmp_path, monkeypatch, build_fn, effort
):
    runtime = tmp_path / "runtime root"
    script = runtime / "repo" / "scripts" / "llm_via_cpa.sh"
    captured: list = []
    runtime_env = {
        "CPA_BASE_URL": "https://runtime.example.test/v1",
        "CPA_API_KEY": "runtime-secret",
        "UNRELATED": "preserved",
    }

    def capture(config):
        captured.append(config)
        return lambda _prompt: "{}"

    monkeypatch.setattr(transport, "build_llm_call", capture)
    monkeypatch.setattr(
        transport,
        "runtime_cpa_command_environment",
        lambda path: runtime_env if path == runtime.absolute() else None,
        raising=False,
    )
    monkeypatch.setenv("CPA_BASE_URL", "https://ambient.invalid/v1")
    monkeypatch.setenv("CPA_API_KEY", "ambient-secret")

    call = build_fn(runtime_root=runtime)

    assert call("fixture") == "{}"
    assert len(captured) == 1
    config = captured[0]
    assert config.runtime_root == str(runtime.absolute())
    assert config.command_child_env == runtime_env
    assert shlex.split(config.command_template)[1] == str(script.absolute())
    assert shlex.split(config.command_template)[-2] == effort
    assert os.environ["CPA_BASE_URL"] == "https://ambient.invalid/v1"
    assert os.environ["CPA_API_KEY"] == "ambient-secret"


@pytest.mark.parametrize(
    "build_fn",
    (
        transport.build_final_review_llm_call,
        transport.build_pronoun_audit_llm_call,
    ),
)
def test_review_builder_rejects_stale_ambient_without_runtime_binding(
    monkeypatch, build_fn
):
    from src.autoslice.llm_client import LlmCallError

    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    monkeypatch.delenv("AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("AUTOSLICE_FINAL_REVIEW_SSH_HOST", raising=False)
    monkeypatch.setenv("CPA_BASE_URL", "https://stale-ambient.invalid/v1")
    monkeypatch.setenv("CPA_API_KEY", "stale-ambient-secret")

    def forbidden_build(_config):
        raise AssertionError("ambient CPA transport must not be constructed")

    monkeypatch.setattr(transport, "build_llm_call", forbidden_build)
    call = build_fn()

    with pytest.raises(
        LlmCallError,
        match="canonical CPA runtime binding required",
    ) as raised:
        call("fixture")

    assert raised.value.safe_reason == "LLM_RUNTIME_CPA_BINDING_REQUIRED"
    assert raised.value.provider_diagnostics == {
        "provider_transport": "runtime_cpa",
        "provider_credential_source": "UNBOUND",
        "provider_error_code": "CANONICAL_RUNTIME_BINDING_REQUIRED",
        "provider_error_message": "canonical CPA runtime binding required",
    }
    assert "stale-ambient" not in str(raised.value)


@pytest.mark.parametrize(
    ("build_fn", "effort"),
    (
        (transport.build_final_review_llm_call, "medium"),
        (transport.build_pronoun_audit_llm_call, "low"),
    ),
)
def test_review_builder_remote_runtime_strips_stale_ambient(
    monkeypatch, build_fn, effort
):
    captured: list = []

    def capture(config):
        captured.append(config)
        return lambda _prompt: "{}"

    monkeypatch.setattr(transport, "build_llm_call", capture)
    monkeypatch.setenv("CPA_BASE_URL", "https://stale-ambient.invalid/v1")
    monkeypatch.setenv("CPA_API_KEY", "stale-ambient-secret")

    call = build_fn(
        runtime_root="/opt/bilive/autoslice",
        runtime_ssh_host="oci3",
    )

    assert call("fixture") == "{}"
    assert len(captured) == 1
    config = captured[0]
    parts = shlex.split(config.command_template)
    assert parts[0] == "python3"
    assert Path(parts[1]).name == "llm_via_runtime_cpa_ssh.py"
    assert parts[parts.index("--ssh-host") + 1] == "oci3"
    assert parts[parts.index("--runtime-root") + 1] == "/opt/bilive/autoslice"
    assert parts[parts.index("--effort") + 1] == effort
    assert config.command_child_env is not None
    assert "CPA_BASE_URL" not in config.command_child_env
    assert "CPA_API_KEY" not in config.command_child_env
    assert config.command_diagnostic_context == {
        "provider_transport": "ssh_runtime_cpa",
        "provider_runtime_host": "oci3",
        "provider_credential_source": "/opt/bilive/autoslice/cpa.env",
    }
