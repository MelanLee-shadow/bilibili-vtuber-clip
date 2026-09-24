#!/usr/bin/env python3
"""Run the deployed CPA wrapper through a canonical runtime over strict SSH."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.autoslice.llm_client import (  # noqa: E402
    PROVIDER_DIAGNOSTIC_MARKER,
    sanitize_provider_diagnostics,
)

DIAGNOSTIC_MARKER = PROVIDER_DIAGNOSTIC_MARKER
SSH_PREFIX = [
    "ssh",
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=yes",
    "-o",
    "ConnectTimeout=12",
]

REMOTE_PROGRAM = r"""
from __future__ import annotations
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

request = json.load(sys.stdin)
runtime = Path(request["runtime_root"]).absolute()
sys.path.insert(0, str(runtime / "repo"))
from src.autoslice.llm_client import runtime_cpa_command_environment
from src.autoslice.provider_slots import runtime_provider_slot


def safe_failure(code, message, diagnostics):
    diagnostics.update(
        provider_error_code=code,
        provider_error_message=message,
    )
    print(
        json.dumps(
            {"status": "FAILED", "diagnostics": diagnostics},
            ensure_ascii=False,
        )
    )
    raise SystemExit(1)


try:
    env = runtime_cpa_command_environment(runtime)
except Exception as exc:
    code = str(getattr(exc, "safe_reason", None) or type(exc).__name__)
    safe_failure(
        code,
        "canonical runtime CPA environment could not be loaded",
        {
            "provider_transport": "ssh_runtime_cpa",
            "provider_credential_source": str(runtime / "cpa.env"),
        },
    )

endpoint = urlsplit(env["CPA_BASE_URL"])
diagnostics = {
    "provider_transport": "ssh_runtime_cpa",
    "provider_endpoint_host": endpoint.hostname or "",
    "provider_endpoint_path": endpoint.path or "/",
    "provider_credential_source": str(runtime / "cpa.env"),
}
wrapper = runtime / "repo" / "scripts" / "llm_via_cpa.sh"
with tempfile.TemporaryDirectory(prefix="runtime_cpa_review_") as tmp:
    prompt_file = Path(tmp) / "prompt.txt"
    completion_file = Path(tmp) / "completion.txt"
    prompt_file.write_text(request["prompt"], encoding="utf-8")
    try:
        with runtime_provider_slot(
            runtime_root=runtime,
            timeout_seconds=1800,
        ):
            completed = subprocess.run(
                [
                    "bash",
                    str(wrapper),
                    str(prompt_file),
                    str(completion_file),
                    request["model"],
                    request["effort"],
                    str(request["attempts"]),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=660,
                env=env,
            )
    except subprocess.TimeoutExpired:
        safe_failure(
            "LLM_COMMAND_TIMEOUT",
            "canonical CPA wrapper timed out",
            diagnostics,
        )
    statuses = [
        int(value)
        for value in re.findall(r"\bhttp=(\d{3})\b", completed.stderr)
    ]
    if statuses:
        diagnostics["provider_http_status"] = statuses[-1]
    upstream = re.findall(
        r"\bupstream_code=([A-Za-z0-9._:-]+)", completed.stderr
    )
    if completed.returncode != 0:
        status = diagnostics.get("provider_http_status")
        code = (
            upstream[-1]
            if upstream
            else (f"HTTP_{status}" if status else "CPA_WRAPPER_FAILED")
        )
        message = (
            f"CPA request rejected with HTTP {status}"
            if status
            else "canonical CPA wrapper failed"
        )
        safe_failure(code, message, diagnostics)
    if not completion_file.is_file():
        safe_failure(
            "LLM_COMMAND_COMPLETION_MISSING",
            "canonical CPA wrapper did not write a completion",
            diagnostics,
        )
    completion = completion_file.read_text(encoding="utf-8")
    if not completion.strip():
        safe_failure(
            "LLM_COMMAND_COMPLETION_EMPTY",
            "canonical CPA wrapper wrote an empty completion",
            diagnostics,
        )
    if env["CPA_API_KEY"] in completion or env["CPA_API_KEY"] in completed.stderr:
        safe_failure(
            "CPA_SECRET_DISCLOSURE_GUARD",
            "CPA secret disclosure guard fired",
            diagnostics,
        )
    diagnostics.setdefault("provider_http_status", 200)
    print(
        json.dumps(
            {
                "status": "PASS",
                "completion": completion,
                "diagnostics": diagnostics,
            },
            ensure_ascii=False,
        )
    )
"""


class RuntimeCpaBridgeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        provider_diagnostics: Mapping[str, object] | None = None,
    ) -> None:
        self.provider_diagnostics = sanitize_provider_diagnostics(provider_diagnostics)
        safe_message = str(self.provider_diagnostics.get("provider_error_message") or message)
        super().__init__(safe_message)


def _emit_diagnostics(
    diagnostics: Mapping[str, object],
) -> dict[str, object]:
    safe = sanitize_provider_diagnostics(diagnostics)
    print(
        DIAGNOSTIC_MARKER + json.dumps(safe, ensure_ascii=False, sort_keys=True),
        file=sys.stderr,
    )
    return safe


def run_bridge(
    *,
    prompt_file: Path,
    completion_file: Path,
    ssh_host: str,
    runtime_root: Path,
    model: str,
    effort: str,
    attempts: int,
) -> dict[str, object]:
    request = {
        "prompt": prompt_file.read_text(encoding="utf-8"),
        "runtime_root": str(runtime_root),
        "model": model,
        "effort": effort,
        "attempts": attempts,
    }
    remote_command = "sudo -n /opt/bilive/autoslice/venv-main/bin/python -B -c " + shlex.quote(
        REMOTE_PROGRAM
    )
    completed = subprocess.run(
        [*SSH_PREFIX, ssh_host, remote_command],
        input=json.dumps(request, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=720,
        check=False,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        diagnostics = _emit_diagnostics(
            {
                "provider_transport": "ssh_runtime_cpa",
                "provider_runtime_host": ssh_host,
                "provider_credential_source": str(runtime_root / "cpa.env"),
                "provider_error_code": "SSH_RUNTIME_RESPONSE_INVALID",
                "provider_error_message": ("canonical runtime SSH response was invalid"),
            }
        )
        raise RuntimeCpaBridgeError(
            "canonical runtime SSH response was invalid",
            provider_diagnostics=diagnostics,
        ) from None
    diagnostics = dict(payload.get("diagnostics") or {})
    diagnostics["provider_runtime_host"] = ssh_host
    safe = _emit_diagnostics(diagnostics)
    if completed.returncode != 0 or payload.get("status") != "PASS":
        raise RuntimeCpaBridgeError(
            "canonical runtime CPA call failed",
            provider_diagnostics=safe,
        )
    completion = payload.get("completion")
    if not isinstance(completion, str) or not completion.strip():
        safe = _emit_diagnostics(
            {
                **safe,
                "provider_error_code": "LLM_COMMAND_COMPLETION_EMPTY",
                "provider_error_message": ("canonical runtime returned an empty completion"),
            }
        )
        raise RuntimeCpaBridgeError(
            "canonical runtime returned an empty completion",
            provider_diagnostics=safe,
        )
    completion_file.write_text(completion, encoding="utf-8")
    return safe


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt_file", type=Path)
    parser.add_argument("completion_file", type=Path)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument("--attempts", type=int, default=1)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        run_bridge(
            prompt_file=args.prompt_file,
            completion_file=args.completion_file,
            ssh_host=args.ssh_host,
            runtime_root=args.runtime_root,
            model=args.model,
            effort=args.effort,
            attempts=args.attempts,
        )
    except RuntimeCpaBridgeError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
