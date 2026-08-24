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
import re
import shlex
import stat
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from src.autoslice.provider_slots import ProviderSlotError, ProviderSlotTimeout, runtime_provider_slot

LlmCall = Callable[[str], str]


LLM_TRANSPORT_REASON_CODES = frozenset(
    {
        "LLM_COMMAND_TIMEOUT",
        "LLM_COMMAND_FAILED",
        "LLM_COMMAND_COMPLETION_MISSING",
        "LLM_COMMAND_COMPLETION_EMPTY",
        "LLM_PROVIDER_CAPACITY_TIMEOUT",
        "LLM_PROVIDER_CAPACITY_UNAVAILABLE",
        "LLM_RUNTIME_CPA_ENV_UNSAFE",
        "LLM_RUNTIME_CPA_ENV_INVALID",
    }
)


class LlmCallError(RuntimeError):
    """Transport failure with an optional closed, receipt-safe reason."""

    def __init__(self, message: str = "", *, safe_reason: str | None = None) -> None:
        if safe_reason is not None and safe_reason not in LLM_TRANSPORT_REASON_CODES:
            raise ValueError("unknown LLM transport reason")
        self.safe_reason = safe_reason
        super().__init__(message)


LLM_JSON_PARSE_REASON_CODES = frozenset(
    {
        "LLM_JSON_NO_OBJECT",
        "LLM_JSON_UNPARSEABLE_OBJECT",
        "LLM_JSON_ROOT_INVALID",
    }
)


class LlmJsonParseError(LlmCallError):
    """A closed JSON-extraction failure, with no completion text attached."""

    def __init__(self, reason_code: str) -> None:
        if reason_code not in LLM_JSON_PARSE_REASON_CODES:
            raise ValueError("unknown LLM JSON parse reason")
        self.reason_code = reason_code
        super().__init__(reason_code)


_RUNTIME_CPA_ENV_MAX_BYTES = 16 * 1024
_RUNTIME_CPA_KEY = re.compile(r"CPA_[A-Z0-9_]+\Z")


class LlmRuntimeEnvironmentError(LlmCallError):
    """A closed runtime-owned command environment failure."""

    _REASONS = frozenset({"LLM_RUNTIME_CPA_ENV_UNSAFE", "LLM_RUNTIME_CPA_ENV_INVALID"})

    def __init__(self, reason_code: str) -> None:
        if reason_code not in self._REASONS:
            raise ValueError("unknown runtime CPA environment reason")
        self.reason_code = reason_code
        super().__init__(reason_code, safe_reason=reason_code)


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
    # The command bridge may need a runtime-owned credential environment.  It
    # is intentionally excluded from config representation/equality so a key
    # cannot enter logs, receipts, or comparison diagnostics.
    command_child_env: Mapping[str, str] | None = field(default=None, repr=False, compare=False)


def runtime_cpa_command_environment(
    runtime_root: Path,
    *,
    _owner_uid: int = 0,
) -> dict[str, str]:
    """Load only root-private ``cpa.env`` values for one command child.

    This is intentionally not an ``os.environ`` loader.  It accepts the
    deployed runtime's optional, root-owned symlink but binds both the link
    and final regular file across a no-follow read.  Non-CPA config lines are
    irrelevant to the command bridge and are ignored before parsing.
    """

    root = Path(runtime_root).absolute()
    cursor = Path(root.anchor)
    try:
        for part in root.parts[1:]:
            cursor /= part
            observed = os.lstat(cursor)
            if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
                raise OSError("unsafe runtime path")
    except OSError as exc:
        raise LlmRuntimeEnvironmentError("LLM_RUNTIME_CPA_ENV_UNSAFE") from exc
    env_path = root / "cpa.env"
    try:
        initial = os.lstat(env_path)
        if stat.S_ISLNK(initial.st_mode):
            if initial.st_uid != _owner_uid:
                raise OSError("untrusted cpa symlink")
            target = env_path.resolve(strict=True)
        else:
            target = env_path
        cursor = Path(target.parent.anchor)
        for part in target.parent.parts[1:]:
            cursor /= part
            observed = os.lstat(cursor)
            if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
                raise OSError("unsafe cpa target parent")
        before = os.lstat(target)
    except OSError as exc:
        raise LlmRuntimeEnvironmentError("LLM_RUNTIME_CPA_ENV_UNSAFE") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_uid != _owner_uid
        or stat.S_IMODE(before.st_mode) & 0o077
        or not 0 < before.st_size <= _RUNTIME_CPA_ENV_MAX_BYTES
    ):
        raise LlmRuntimeEnvironmentError("LLM_RUNTIME_CPA_ENV_UNSAFE")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    identity = (
        before.st_dev, before.st_ino, stat.S_IMODE(before.st_mode), before.st_size,
        before.st_mtime_ns, before.st_ctime_ns,
    )
    try:
        descriptor = os.open(target, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino, stat.S_IMODE(opened.st_mode), opened.st_size,
                    opened.st_mtime_ns, opened.st_ctime_ns) != identity
            ):
                raise LlmRuntimeEnvironmentError("LLM_RUNTIME_CPA_ENV_UNSAFE")
            payload = bytearray()
            while len(payload) <= _RUNTIME_CPA_ENV_MAX_BYTES:
                block = os.read(descriptor, 4096)
                if not block:
                    break
                payload.extend(block)
            after_descriptor = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after_target = os.lstat(target)
        after_link = os.lstat(env_path)
    except LlmRuntimeEnvironmentError:
        raise
    except OSError as exc:
        raise LlmRuntimeEnvironmentError("LLM_RUNTIME_CPA_ENV_UNSAFE") from exc
    if (
        len(payload) != before.st_size
        or len(payload) > _RUNTIME_CPA_ENV_MAX_BYTES
        or (after_descriptor.st_dev, after_descriptor.st_ino, stat.S_IMODE(after_descriptor.st_mode),
            after_descriptor.st_size, after_descriptor.st_mtime_ns, after_descriptor.st_ctime_ns) != identity
        or (after_target.st_dev, after_target.st_ino, stat.S_IMODE(after_target.st_mode),
            after_target.st_size, after_target.st_mtime_ns, after_target.st_ctime_ns) != identity
        or (after_link.st_dev, after_link.st_ino, stat.S_IMODE(after_link.st_mode),
            after_link.st_size, after_link.st_mtime_ns, after_link.st_ctime_ns)
        != (initial.st_dev, initial.st_ino, stat.S_IMODE(initial.st_mode), initial.st_size,
            initial.st_mtime_ns, initial.st_ctime_ns)
    ):
        raise LlmRuntimeEnvironmentError("LLM_RUNTIME_CPA_ENV_UNSAFE")
    try:
        text = bytes(payload).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LlmRuntimeEnvironmentError("LLM_RUNTIME_CPA_ENV_INVALID") from exc
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not key.startswith("CPA_"):
            continue
        if not separator or _RUNTIME_CPA_KEY.fullmatch(key) is None:
            raise LlmRuntimeEnvironmentError("LLM_RUNTIME_CPA_ENV_INVALID")
        try:
            parts = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as exc:
            raise LlmRuntimeEnvironmentError("LLM_RUNTIME_CPA_ENV_INVALID") from exc
        if len(parts) != 1 or not parts[0] or key in values:
            raise LlmRuntimeEnvironmentError("LLM_RUNTIME_CPA_ENV_INVALID")
        values[key] = parts[0]
    if not values.get("CPA_BASE_URL") or not values.get("CPA_API_KEY"):
        raise LlmRuntimeEnvironmentError("LLM_RUNTIME_CPA_ENV_INVALID")
    child = os.environ.copy()
    # The runtime authority wins over an ambient shell's stale CPA settings.
    child.update(values)
    return child


def build_llm_call(config: LlmConfig) -> LlmCall:
    if config.transport == "direct":
        return lambda prompt: _provider_dispatch(lambda: _call_direct(prompt, config), config.runtime_root)
    if config.transport == "command":
        return lambda prompt: _provider_dispatch(lambda: _call_command(prompt, config), config.runtime_root)
    raise ValueError(f"unknown llm transport: {config.transport!r}")


def _provider_dispatch(call: Callable[[], str], configured_root: str | None = None) -> str:
    """Apply the shared process-level permit to real provider transports."""

    try:
        with runtime_provider_slot(runtime_root=configured_root):
            return call()
    except ProviderSlotTimeout as exc:
        raise LlmCallError(
            "provider capacity wait timed out",
            safe_reason="LLM_PROVIDER_CAPACITY_TIMEOUT",
        ) from exc
    except ProviderSlotError as exc:
        raise LlmCallError(
            "provider capacity is unavailable",
            safe_reason="LLM_PROVIDER_CAPACITY_UNAVAILABLE",
        ) from exc


def extract_json_object(completion: str) -> dict[str, object]:
    """Parse the first JSON object out of an LLM completion (fail-closed).

    Models wrap JSON in prose or code fences; scan for the first balanced
    object instead of trusting the whole completion to be JSON.
    """

    if not isinstance(completion, str):
        raise LlmJsonParseError("LLM_JSON_ROOT_INVALID")
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
                        raise LlmJsonParseError(
                            "LLM_JSON_UNPARSEABLE_OBJECT"
                        ) from exc
                    if isinstance(payload, dict):
                        return payload
                    raise LlmJsonParseError("LLM_JSON_ROOT_INVALID")
    if start is not None:
        raise LlmJsonParseError("LLM_JSON_UNPARSEABLE_OBJECT")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        raise LlmJsonParseError("LLM_JSON_NO_OBJECT") from None
    if not isinstance(parsed, dict):
        raise LlmJsonParseError("LLM_JSON_ROOT_INVALID")
    raise LlmJsonParseError("LLM_JSON_NO_OBJECT")


def call_and_extract_json_with_parse_retry(
    prompt: str,
    *,
    llm_call: LlmCall,
    extract_json: Callable[[str], object],
) -> object:
    """Call once, retrying exactly once only for a typed parse failure.

    The same prompt is deliberately reused.  Transport failures and all
    post-extraction schema/coverage validation stay single-attempt and
    fail-closed at their owning gate.
    """

    completion = llm_call(prompt)
    try:
        return extract_json(completion)
    except LlmJsonParseError:
        return extract_json(llm_call(prompt))


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
                env=config.command_child_env,
            )
        except subprocess.TimeoutExpired as exc:
            # Timeouts must surface as LlmCallError like every other transport
            # failure — callers are fail-open repair stages; a raw
            # TimeoutExpired crashed an 11-min clip's produce run (2026-07-06).
            raise LlmCallError(
                f"llm command timed out after {config.timeout_seconds:.0f}s",
                safe_reason="LLM_COMMAND_TIMEOUT",
            ) from exc
        if completed.returncode != 0:
            # Ivan 2026-08-08 工程优化②授权：桥接脚本（llm_via_cpa.sh）在多个
            # 供应商模型间失败转移时，stderr 是逐模型多行级联；旧的 400 字符
            # 尾截断只留最后一个模型的信息，早期模型的失败证据永久丢失
            # （真善美 zsm4 三模型均 400 事故排障时才发现）。留够整条级联。
            raise LlmCallError(
                f"llm command failed rc={completed.returncode}: {completed.stderr.strip()[-4000:]}",
                safe_reason="LLM_COMMAND_FAILED",
            )
        if not completion_file.is_file():
            raise LlmCallError(
                "llm command did not write the completion file",
                safe_reason="LLM_COMMAND_COMPLETION_MISSING",
            )
        content = completion_file.read_text(encoding="utf-8")
        if not content.strip():
            raise LlmCallError(
                "llm command wrote an empty completion",
                safe_reason="LLM_COMMAND_COMPLETION_EMPTY",
            )
        return content
