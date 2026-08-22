"""Minimal LLM caller used by the repair-first stages (CPA judge, song hints).

Two transports:

- ``direct``: POST an OpenAI-compatible ``/chat/completions`` endpoint.  The
  API key is read from an environment variable so it never lands in job
  manifests or logs.
- ``command``: run an external command template with ``{prompt_file}`` and
  ``{completion_file}`` placeholders.  This keeps provider keys on the host
  that owns them (e.g. the media host) — the local pipeline ships a prompt file over
  the bridge and reads back plain-text completion.

Both raise ``LlmCallError`` on failure; callers are repair stages that must
record the failure and fall back fail-closed, never crash the review.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from src.autoslice.provider_slots import ProviderSlotBusy, ProviderSlotError, provider_slot

LlmCall = Callable[[str], str]


class LlmCallError(RuntimeError):
    pass


@dataclass(frozen=True)
class LlmConfig:
    transport: str  # "direct" | "command"
    model: str | None = None
    api_base: str | None = None
    api_key_env: str | None = None
    command_template: str | None = None
    temperature: float = 0.2
    max_tokens: int = 1024
    timeout_seconds: float = 60.0
    # direct transport routing (2026-07-10): gpt-5.x are native Responses-API
    # reasoning models — requesting them on /chat/completions MISROUTES on the
    # CPA proxy (503 auth_unavailable).  "chat" keeps the legacy behaviour.
    api_mode: str = "chat"  # "chat" | "responses"
    reasoning_effort: str | None = None  # responses mode only; None → "medium"
    runtime_root: str | None = None  # optional runtime-local provider slot pool


def build_llm_call(config: LlmConfig) -> LlmCall:
    if config.transport == "direct":
        return lambda prompt: _provider_dispatch(lambda: _call_direct(prompt, config), config.runtime_root)
    if config.transport == "command":
        return lambda prompt: _provider_dispatch(lambda: _call_command(prompt, config), config.runtime_root)
    raise ValueError(f"unknown llm transport: {config.transport!r}")


def _provider_dispatch(call: Callable[[], str], configured_root: str | None = None) -> str:
    """Apply the shared process-level permit to real provider transports."""

    root = configured_root or os.environ.get("AUTOSLICE_BASE")
    if not root:
        # Standalone tooling has no declared runtime to share.  Production
        # runner/Qixi callers bind one explicitly; silently inventing /opt
        # would turn unrelated local commands into a different runtime.
        return call()
    runtime_root = Path(root)
    try:
        with provider_slot(runtime_root):
            return call()
    except ProviderSlotBusy as exc:
        raise LlmCallError("provider capacity is busy") from exc
    except ProviderSlotError as exc:
        raise LlmCallError("provider capacity is unavailable") from exc


def extract_json_object(completion: str) -> dict[str, object]:
    """Parse the first JSON object out of an LLM completion (fail-closed).

    Models wrap JSON in prose or code fences; scan for the first balanced
    object instead of trusting the whole completion to be JSON.
    """

    text = completion.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    depth = 0
    start = None
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if escape:
            escape = False
            continue
        if char == "\\" and in_string:
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    candidate = text[start : index + 1]
                    try:
                        payload = json.loads(candidate)
                    except json.JSONDecodeError as exc:
                        raise LlmCallError(f"completion contained unparseable JSON object: {exc}") from exc
                    if isinstance(payload, dict):
                        return payload
                    raise LlmCallError("completion JSON was not an object")
    raise LlmCallError("no JSON object found in completion")


def _call_direct(prompt: str, config: LlmConfig) -> str:
    if not config.api_base or not config.model or not config.api_key_env:
        raise LlmCallError("direct transport requires api_base, model and api_key_env")
    api_key = os.environ.get(config.api_key_env, "")
    if not api_key:
        raise LlmCallError(f"environment variable {config.api_key_env} is empty")
    responses_mode = config.api_mode == "responses"
    if responses_mode:
        endpoint = config.api_base.rstrip("/") + "/responses"
        request_body: dict = {
            "model": config.model,
            "input": prompt,
            "reasoning": {"effort": config.reasoning_effort or "medium"},
            "max_output_tokens": config.max_tokens,
        }
    else:
        endpoint = config.api_base.rstrip("/") + "/chat/completions"
        request_body = {
            "model": config.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
        }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            # CPA-style endpoints behind Cloudflare 403 (error 1010) the
            # default Python-urllib user agent
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        raise LlmCallError(f"direct LLM call failed: {type(exc).__name__}: {exc}") from exc
    if responses_mode:
        # Responses API: prefer the convenience output_text, else walk output[]
        # for the assistant message parts (skipping reasoning items).  An empty
        # completion (~15% upstream quirk) raises → the caller's bounded retry
        # (e.g. judge_request --retries) covers it.
        content = payload.get("output_text")
        if not content:
            parts = []
            for item in payload.get("output") or []:
                if isinstance(item, dict) and item.get("type") == "message":
                    for chunk in item.get("content") or []:
                        if isinstance(chunk, dict) and chunk.get("type") in ("output_text", "text") and chunk.get("text"):
                            parts.append(chunk["text"])
            content = "".join(parts)
        if not isinstance(content, str) or not content.strip():
            raise LlmCallError(f"empty responses completion (status={payload.get('status')})")
        return content
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmCallError(f"unexpected completion payload shape: {exc}") from exc
    if not isinstance(content, str) or not content.strip():
        raise LlmCallError("empty completion content")
    return content


def _call_command(prompt: str, config: LlmConfig) -> str:
    if not config.command_template:
        raise LlmCallError("command transport requires command_template")
    with tempfile.TemporaryDirectory(prefix="llm_bridge_") as tmp:
        prompt_file = Path(tmp) / "prompt.txt"
        completion_file = Path(tmp) / "completion.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        command = [
            part.replace("{prompt_file}", str(prompt_file)).replace("{completion_file}", str(completion_file))
            for part in shlex.split(config.command_template)
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=config.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            # Timeouts must surface as LlmCallError like every other transport
            # failure — callers are fail-open repair stages; a raw
            # TimeoutExpired crashed an 11-min clip's produce run (2026-07-06).
            raise LlmCallError(f"llm command timed out after {config.timeout_seconds:.0f}s") from exc
        if completed.returncode != 0:
            # Ivan 2026-08-08 工程优化②授权：桥接脚本（llm_via_cpa.sh）在多个
            # 供应商模型间失败转移时，stderr 是逐模型多行级联；旧的 400 字符
            # 尾截断只留最后一个模型的信息，早期模型的失败证据永久丢失
            # （真善美 zsm4 三模型均 400 事故排障时才发现）。留够整条级联。
            raise LlmCallError(
                f"llm command failed rc={completed.returncode}: {completed.stderr.strip()[-4000:]}"
            )
        if not completion_file.is_file():
            raise LlmCallError("llm command did not write the completion file")
        content = completion_file.read_text(encoding="utf-8")
        if not content.strip():
            raise LlmCallError("llm command wrote an empty completion")
        return content
