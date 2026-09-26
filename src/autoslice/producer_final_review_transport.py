"""Canonical CPA transports for producer final subtitle review."""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urlsplit

from src.autoslice.llm_client import (
    LlmCallError,
    LlmConfig,
    build_llm_call,
    runtime_cpa_command_environment,
)

ROOT = Path(__file__).resolve().parents[2]
_REMOTE_BRIDGE = ROOT / "scripts" / "llm_via_runtime_cpa_ssh.py"
_RUNTIME_ROOT_ENV = "AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT"
_RUNTIME_SSH_HOST_ENV = "AUTOSLICE_FINAL_REVIEW_SSH_HOST"


def _without_ambient_cpa_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CPA_") and key != "AUTOSLICE_CPA_ENV"
    }


def _resolved_runtime_root(runtime_root: str | Path | None) -> Path | None:
    raw = runtime_root
    if raw is None:
        raw = os.environ.get(_RUNTIME_ROOT_ENV) or os.environ.get("AUTOSLICE_BASE")
    if raw is None or not str(raw).strip():
        return None
    runtime = Path(str(raw)).expanduser()
    if not runtime.is_absolute():
        raise ValueError("final-review runtime root must be absolute")
    return runtime.absolute()


def _resolved_runtime_ssh_host(runtime_ssh_host: str | None) -> str | None:
    raw = runtime_ssh_host
    if raw is None:
        raw = os.environ.get(_RUNTIME_SSH_HOST_ENV)
    normalized = str(raw or "").strip()
    if not normalized:
        return None
    if any(
        char
        not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        for char in normalized
    ):
        raise ValueError("final-review SSH host is invalid")
    return normalized


def _missing_runtime_call() -> Callable[[str], str]:
    diagnostics = {
        "provider_transport": "runtime_cpa",
        "provider_credential_source": "UNBOUND",
        "provider_error_code": "CANONICAL_RUNTIME_BINDING_REQUIRED",
        "provider_error_message": "canonical CPA runtime binding required",
    }

    def call(_prompt: str) -> str:
        raise LlmCallError(
            "canonical CPA runtime binding required",
            safe_reason="LLM_RUNTIME_CPA_BINDING_REQUIRED",
            provider_diagnostics=diagnostics,
        )

    call.provider_diagnostics = diagnostics  # type: ignore[attr-defined]
    call.provider_runtime_binding = diagnostics  # type: ignore[attr-defined]
    return call


def _bound_call(
    config: LlmConfig,
    runtime_binding: Mapping[str, object],
) -> Callable[[str], str]:
    diagnostics = dict(runtime_binding)

    def sink(row: dict[str, object]) -> None:
        diagnostics.clear()
        diagnostics.update(row)

    bound_config = LlmConfig(
        **{
            **config.__dict__,
            "command_diagnostic_sink": sink,
        }
    )
    call = build_llm_call(bound_config)
    call.provider_diagnostics = diagnostics  # type: ignore[attr-defined]
    call.provider_runtime_binding = dict(runtime_binding)  # type: ignore[attr-defined]
    return call


def _local_runtime_call(
    *,
    runtime: Path,
    effort: str,
) -> Callable[[str], str]:
    child_env = runtime_cpa_command_environment(runtime)
    parsed = urlsplit(child_env["CPA_BASE_URL"])
    runtime_binding = {
        "provider_transport": "runtime_cpa",
        "provider_endpoint_host": parsed.hostname or "",
        "provider_endpoint_path": parsed.path or "/",
        "provider_credential_source": str(runtime / "cpa.env"),
    }
    script = runtime / "repo" / "scripts" / "llm_via_cpa.sh"
    return _bound_call(
        LlmConfig(
            transport="command",
            command_template=(
                f"bash {shlex.quote(str(script))} "
                "{prompt_file} {completion_file} "
                f"'gpt-6-sol' {effort} 1"
            ),
            timeout_seconds=600.0,
            runtime_root=str(runtime),
            command_child_env=child_env,
            command_diagnostic_context=runtime_binding,
        ),
        runtime_binding,
    )


def _remote_runtime_call(
    *,
    runtime: Path,
    ssh_host: str,
    effort: str,
) -> Callable[[str], str]:
    runtime_binding = {
        "provider_transport": "ssh_runtime_cpa",
        "provider_runtime_host": ssh_host,
        "provider_credential_source": str(runtime / "cpa.env"),
    }
    return _bound_call(
        LlmConfig(
            transport="command",
            command_template=(
                f"python3 {shlex.quote(str(_REMOTE_BRIDGE))} "
                "{prompt_file} {completion_file} "
                f"--ssh-host {shlex.quote(ssh_host)} "
                f"--runtime-root {shlex.quote(str(runtime))} "
                f"--model gpt-6-sol --effort {effort} --attempts 1"
            ),
            timeout_seconds=720.0,
            command_child_env=_without_ambient_cpa_environment(),
            command_diagnostic_context=runtime_binding,
        ),
        runtime_binding,
    )


def _build_review_llm_call(
    *,
    effort: str,
    runtime_root: str | Path | None,
    runtime_ssh_host: str | None,
) -> Callable[[str], str]:
    runtime = _resolved_runtime_root(runtime_root)
    ssh_host = _resolved_runtime_ssh_host(runtime_ssh_host)
    if runtime is None:
        return _missing_runtime_call()
    if ssh_host is not None:
        return _remote_runtime_call(
            runtime=runtime,
            ssh_host=ssh_host,
            effort=effort,
        )
    return _local_runtime_call(runtime=runtime, effort=effort)


def build_final_review_llm_call(
    *,
    runtime_root: str | Path | None = None,
    runtime_ssh_host: str | None = None,
) -> Callable[[str], str]:
    """Build the medium-effort exact-final CPA call from canonical runtime."""

    return _build_review_llm_call(
        effort="medium",
        runtime_root=runtime_root,
        runtime_ssh_host=runtime_ssh_host,
    )


def build_pronoun_audit_llm_call(
    *,
    runtime_root: str | Path | None = None,
    runtime_ssh_host: str | None = None,
) -> Callable[[str], str]:
    """Build the low-effort pronoun audit from the same canonical runtime."""

    return _build_review_llm_call(
        effort="low",
        runtime_root=runtime_root,
        runtime_ssh_host=runtime_ssh_host,
    )


def build_publication_llm_call(*, effort: str) -> Callable[[str], str]:
    """Title, source-fact and art direction use the same canonical CPA binding."""
    if effort not in {"high", "medium"}:
        raise ValueError("unsupported publication effort")
    return _build_review_llm_call(
        effort=effort, runtime_root=None, runtime_ssh_host=None,
    )
