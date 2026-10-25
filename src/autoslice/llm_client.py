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

import hashlib
import datetime as dt
import json
import math
import os
import re
import shlex
import stat
import subprocess
import tempfile
import time
import random
import uuid
import threading
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Mapping

from src.autoslice.cpa_model_ladder import TOP_CPA_MODEL, cpa_call_timeout_seconds
from src.autoslice.cpa_model_ladder import model_ladder, resolve_models, attempt_timeout_seconds, chain_deadline_seconds
from src.autoslice.provider_slots import ProviderSlotError, ProviderSlotTimeout, runtime_provider_slot
from src.autoslice.cpa_runtime import observed_cpa_request, observed_model_request, observation_environment, record_cpa_event
from src.autoslice.cpa_runtime import CPA_RESPONSE_LOCAL_FAILURE
from src.autoslice.structured_output import current_contract, openai_format, contract_receipt, format_retry_call

LlmCall = Callable[[str], str]


LLM_TRANSPORT_REASON_CODES = frozenset(
    {
        "LLM_COMMAND_TIMEOUT",
        "LLM_COMMAND_FAILED",
        "LLM_RESPONSE_LOCAL_FAILURE",
        "LLM_COMMAND_COMPLETION_MISSING",
        "LLM_COMMAND_COMPLETION_EMPTY",
        "LLM_PROVIDER_CAPACITY_TIMEOUT",
        "LLM_PROVIDER_CAPACITY_UNAVAILABLE",
        "LLM_RUNTIME_CPA_ENV_UNSAFE",
        "LLM_RUNTIME_CPA_ENV_INVALID",
        "LLM_RUNTIME_CPA_BINDING_REQUIRED",
    }
)


PROVIDER_DIAGNOSTIC_MARKER = "[autoslice-provider-diagnostic] "
_CPA_COMMAND_ATTEMPT_RX = re.compile(
    r"\[(?:cpa|cpa-transport)\] model=(?P<model>[A-Za-z0-9._:-]+) attempt=[1-9][0-9]* "
    r"http=(?P<http>[0-9]{3}) curl_exit=(?P<curl>[0-9]+) "
    r"(?:empty_completion=(?P<empty>[01]) )?body_bytes=[0-9]+ "
    r"result=(?P<result>ok|failed)"
    r"(?: upstream_code=(?P<upstream>[A-Za-z0-9._:-]+))?"
)
_RETRY_AFTER_RX = re.compile(
    r"(?i)\b(?:retry[-_ ]?after|provider_retry_after_seconds)[=: ]+"
    r"(?P<seconds>[0-9]+(?:\.[0-9]+)?)\b"
)
_PROVIDER_DIAGNOSTIC_KEYS = frozenset(
    {
        "provider_transport",
        "provider_runtime_host",
        "provider_endpoint_host",
        "provider_endpoint_path",
        "provider_credential_source",
        "provider_http_status",
        "provider_error_code",
        "provider_error_message",
        "provider_retry_after_seconds",
        "provider_answering_model",
    }
)
_PROVIDER_HOST_RX = re.compile(r"[A-Za-z0-9._-]{1,253}\Z")
_PROVIDER_CODE_RX = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_PROVIDER_SECRET_RX = re.compile(
    r"(?i)(Authorization:\s*Bearer\s+)\S+|"
    r"((?:api[_ -]?key|token|secret|password)\s*[=:]\s*)\S+"
)


def _redact_provider_text(
    value: object,
    *,
    limit: int = 512,
    keep_tail: bool = False,
) -> str:
    text = " ".join(str(value or "").split())
    text = _PROVIDER_SECRET_RX.sub(
        lambda match: (match.group(1) or match.group(2) or "") + "<redacted>",
        text,
    )
    return text[-limit:] if keep_tail else text[:limit]


def sanitize_provider_diagnostics(
    raw: Mapping[str, object] | None,
) -> dict[str, object]:
    """Return only receipt-safe provider routing and failure diagnostics."""

    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, object] = {}
    for key in _PROVIDER_DIAGNOSTIC_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        if key == "provider_http_status":
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 100 <= value <= 599
            ):
                continue
            result[key] = value
            continue
        if key == "provider_retry_after_seconds":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if value < 0 or not math.isfinite(float(value)):
                continue
            result[key] = int(value) if float(value).is_integer() else float(value)
            continue
        if key in {"provider_endpoint_host", "provider_runtime_host"}:
            normalized = str(value).strip()
            if _PROVIDER_HOST_RX.fullmatch(normalized):
                result[key] = normalized
            continue
        if key == "provider_endpoint_path":
            normalized = str(value).strip()
            if (
                normalized.startswith("/")
                and "\n" not in normalized
                and len(normalized) <= 512
            ):
                result[key] = normalized
            continue
        if key == "provider_credential_source":
            normalized = str(value).strip()
            if normalized == "UNBOUND" or (
                normalized.startswith("/")
                and "\n" not in normalized
                and len(normalized) <= 1024
            ):
                result[key] = normalized
            continue
        if key in {"provider_error_code", "provider_transport", "provider_answering_model"}:
            normalized = str(value).strip()
            if _PROVIDER_CODE_RX.fullmatch(normalized):
                result[key] = normalized
            continue
        if key == "provider_error_message":
            normalized = _redact_provider_text(value)
            if normalized:
                result[key] = normalized
    return result


def _retry_after_seconds(value: object, *, now: dt.datetime | None = None) -> int | None:
    """Normalize the provider's numeric or HTTP-date Retry-After signal."""

    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        seconds = None
    if seconds is not None and math.isfinite(seconds):
        return max(0, int(seconds))
    try:
        observed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if observed is None:
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=dt.timezone.utc)
    current = now or dt.datetime.now(dt.timezone.utc)
    return max(0, int(observed.timestamp() - current.timestamp()))


class LlmCallError(RuntimeError):
    """Transport failure with closed routing diagnostics and no credentials."""

    def __init__(
        self,
        message: str = "",
        *,
        safe_reason: str | None = None,
        provider_diagnostics: Mapping[str, object] | None = None,
        response_received: bool | None = None,
    ) -> None:
        if safe_reason is not None and safe_reason not in LLM_TRANSPORT_REASON_CODES:
            raise ValueError("unknown LLM transport reason")
        self.safe_reason = safe_reason
        self.response_received = response_received
        self.provider_diagnostics = sanitize_provider_diagnostics(provider_diagnostics)
        self.retry_after_seconds = self.provider_diagnostics.get(
            "provider_retry_after_seconds"
        )
        super().__init__(
            _redact_provider_text(message, limit=4000, keep_tail=True)
        )


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
        self.failure_layer = 'CONTRACT_ERROR'
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
    # direct transport routing : gpt-5.x are native Responses-API
    # reasoning models — requesting them on /chat/completions MISROUTES on the
    # CPA proxy (503 auth_unavailable).  "chat" keeps the legacy behaviour.
    api_mode: str = "chat"  # "chat" | "responses"
    reasoning_effort: str | None = None  # responses mode only; None → "medium"
    json_mode: bool = False
    response_schema: Mapping[str, object] | None = None
    runtime_root: str | None = None  # optional runtime-local provider slot pool
    # The command bridge may need a runtime-owned credential environment.  It
    # is intentionally excluded from config representation/equality so a key
    # cannot enter logs, receipts, or comparison diagnostics.
    command_child_env: Mapping[str, str] | None = field(default=None, repr=False, compare=False)
    # Static routing context plus a callback for dynamic, sanitized command
    # diagnostics.  Neither field can enter config repr/equality or receipts.
    command_diagnostic_context: Mapping[str, object] | None = field(
        default=None, repr=False, compare=False
    )
    command_diagnostic_sink: Callable[[dict[str, object]], None] | None = field(
        default=None, repr=False, compare=False
    )


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
    child.update(observation_environment())
    # The runtime authority wins over an ambient shell's stale CPA settings.
    child.update(values)
    return child


def _cpa_cache_identity(config: LlmConfig) -> dict[str, object] | None:
    """Describe model/effort without credentials; unknown commands cannot cache."""
    if config.transport == "direct":
        if not isinstance(config.model, str) or not config.model.strip():
            return None
        return {"transport": "direct", "models": [config.model],
                "effort": config.reasoning_effort or "medium", "api_mode": config.api_mode,
                "max_tokens": config.max_tokens, "temperature": config.temperature,
                "api_base_sha256": hashlib.sha256((config.api_base or "").encode()).hexdigest()}
    parts = shlex.split(config.command_template or "")
    indexes = [i for i, part in enumerate(parts) if Path(part).name == "llm_via_cpa.sh"]
    if len(indexes) != 1:
        return None
    rest = parts[indexes[0] + 1:]
    env = config.command_child_env if config.command_child_env is not None else os.environ
    models = rest[2] if len(rest) > 2 else env.get("CPA_CHAT_MODELS") or env.get("CPA_CHAT_MODEL") or TOP_CPA_MODEL
    effort = rest[3] if len(rest) > 3 else env.get("CPA_REASONING_EFFORT") or "medium"
    if not models.strip() or not effort.strip():
        return None
    # Routing alternatives are execution policy, never reply identity. This
    # preserves existing top-model replies when the ladder changes.
    return {"transport": "cpa_command", "models": models.split()[:1], "effort": effort}



def _archive_text_exchange(prompt: str, config: LlmConfig, call: Callable[[], str]) -> str:
    """Opt-in private capture at a real dispatch, never credentials or a replay.

    STARTED stays immutable. Missing terminal.json means an unknown outcome;
    neither this observer nor its records authorize another provider attempt.
    """
    if os.environ.get("AUTOSLICE_CPA_ARCHIVE_TEXT_EXCHANGES") != "1":
        return call()
    observation = observation_environment()
    trace = observation.get("AUTOSLICE_CPA_USAGE_FILE")
    if not trace:
        raise ValueError("private text archive requires a bound usage trace")
    root = Path(trace).parent / "cpa-text-exchanges"
    if root.is_symlink():
        raise ValueError("private text archive cannot be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="request-", dir=root))

    def save(name: str, data: bytes) -> None:
        fd = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    request = {"schema_version": "private-cpa-text-exchange.v1", "status": "STARTED",
               "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
               "transport": config.transport, "model_identity": _cpa_cache_identity(config),
               "production_attempt_id": observation.get("AUTOSLICE_PRODUCTION_ATTEMPT_ID"),
               "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
               "structured_output": contract_receipt(current_contract(config.response_schema, json_mode=config.json_mode or None), prompt=prompt),
               "mutation_authorized": False, "cost_usd": None}
    save("prompt.txt", prompt.encode())
    save("request.json", (json.dumps(request, ensure_ascii=False, indent=2) + "\n").encode())
    try:
        completion = call()
    except BaseException as exc:
        terminal = {"status": "OUTCOME_UNKNOWN", "error_type": type(exc).__name__,
                    "ended_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
        if getattr(exc, 'safe_reason', None) == 'LLM_RESPONSE_LOCAL_FAILURE':
            terminal.update(status='RESPONSE_LOCAL_FAILURE',
                response_received=getattr(exc, 'response_received', None),
                provider_diagnostics=getattr(exc, 'provider_diagnostics', {}))
            raw = getattr(exc, 'command_response_body', None)
            if isinstance(raw, bytes):
                save('response.json', raw)
        # Exception messages can contain credentials. They are never stored.
        save("terminal.json", (json.dumps(terminal, indent=2) + "\n").encode())
        raise
    save("completion.txt", completion.encode())
    terminal = {"status": "COMPLETED", "completion_sha256": hashlib.sha256(completion.encode()).hexdigest(),
                "answering_model": getattr(completion, 'answering_model', None),
                "ended_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
    if getattr(completion, 'local_failure', None):
        terminal['local_failure'] = dict(completion.local_failure)
    save("terminal.json", (json.dumps(terminal, indent=2) + "\n").encode())
    return completion


def build_llm_call(config: LlmConfig) -> LlmCall:
    identity = _cpa_cache_identity(config)
    cpa_text = ((config.transport == 'direct' and (
            config.model in model_ladder() or config.api_key_env == 'CPA_API_KEY'))
        or config.transport == 'command' and (
            identity is not None
            or (config.command_diagnostic_context or {}).get('provider_transport')
            in {'runtime_cpa', 'ssh_runtime_cpa'}))
    command_rest = []
    command_selection = None
    opaque_models = False
    if identity and config.transport == 'command':
        parts = shlex.split(config.command_template)
        command_rest = parts[next(i for i, p in enumerate(parts) if Path(p).name == 'llm_via_cpa.sh') + 1:]
        env = config.command_child_env if config.command_child_env is not None else os.environ
        command_selection = command_rest[2] if len(command_rest) > 2 else env.get('CPA_CHAT_MODELS') or env.get('CPA_CHAT_MODEL')
        # Routing choices do not belong in reply identity, but the existing
        # format-retry guard still needs to see an explicitly multi-model route.
        opaque_models = len((command_selection or TOP_CPA_MODEL).split()) != 1
    if identity and identity.get('models', [None])[0] in model_ladder():
        env = config.command_child_env if config.transport == 'command' and config.command_child_env is not None else os.environ
        models, attempts = len(model_ladder(str(identity['models'][0]))), 3
        if config.transport == 'command':
            models = len(resolve_models(command_selection))
            attempts = int(command_rest[4]) if len(command_rest) > 4 else 3
        config = replace(config, timeout_seconds=max(config.timeout_seconds,
            cpa_call_timeout_seconds(env, models=models, attempts=attempts)))
    retry_ledger = None
    retry_ledger_lock = threading.Lock()

    def record_unknown(row):
        nonlocal retry_ledger
        from .cpa_runtime import bind_observation

        observation = observation_environment()
        if observation.get('AUTOSLICE_CPA_USAGE_FILE'):
            record_cpa_event('text_retry_attempt', row)
            return
        # Standalone callers also retain UNKNOWN. Production normally already
        # binds its usage trace; no-usage callers get a durable local trace,
        # exposed on the callable for readback, never a temporary context that
        # disappears when a later retry succeeds.
        with retry_ledger_lock:
            if retry_ledger is None:
                if config.runtime_root:
                    folder = Path(config.runtime_root) / 'logs'
                    folder.mkdir(parents=True, exist_ok=True)
                else:
                    folder = Path(tempfile.mkdtemp(prefix='cpa-text-retry-'))
                retry_ledger = folder / 'cpa-text-retry.jsonl'
                call.cpa_text_retry_ledger = retry_ledger
        with bind_observation(retry_ledger, observation.get('AUTOSLICE_PRODUCTION_ATTEMPT_ID', 'standalone')):
            record_cpa_event('text_retry_attempt', row)

    def observed(prompt, transport_call):
        from .infra_retry_policy import cpa_text_retry_call

        def physical(actual):
            if not cpa_text:
                return _archive_text_exchange(actual, config, lambda: transport_call(actual))
            logical_call_id = uuid.uuid4().hex
            return cpa_text_retry_call(
                lambda: _archive_text_exchange(actual, config, lambda: transport_call(actual)),
                record=lambda row: record_unknown({**row, 'logical_call_id': logical_call_id,
                    'prompt_sha256': hashlib.sha256(actual.encode()).hexdigest(),
                    'transport': config.transport}))

        with observed_model_request(model=str(config.model or (_cpa_cache_identity(config) or {}).get('models') or 'command'),
                kind='text', transport=config.transport):
            identity = _cpa_cache_identity(config)
            # An opaque/multi-model command cannot guarantee that a format
            # retry stays on the model that produced the first answer.
            if config.transport == 'command' and (identity is None or opaque_models):
                current_contract()['format_retry_handled'] = True
                return physical(prompt)
            return format_retry_call(prompt,
                physical,
                return_invalid=True,
                record=lambda receipt: record_cpa_event('structured_output', {
                    **receipt, 'kind': 'text', 'model_identity': identity,
                    'observation_input_sha256': hashlib.sha256(prompt.encode()).hexdigest()}))
    if config.transport == "direct":
        def call(prompt):
            from src.autoslice.infra_retry_policy import terminal_retry_call
            return terminal_retry_call(
                lambda: _provider_dispatch(
                    lambda: observed(
                        prompt,
                        lambda actual: _call_direct(
                            actual, _terminal_transport_config(config)
                        ),
                    ),
                    config.runtime_root,
                    timeout_seconds=config.timeout_seconds,
                ),
                # The physical transport clamps its own timeout from the
                # current terminal remaining budget on every attempt.  Do not
                # apply the generic adapter's full-timeout admission check to
                # this native path: a retry may legitimately use the shorter
                # remaining deadline.
                timeout_seconds=None,
            )
    elif config.transport == "command":
        def call(prompt):
            from src.autoslice.infra_retry_policy import terminal_retry_call
            return terminal_retry_call(
                lambda: _provider_dispatch(
                    lambda: observed(
                        prompt,
                        lambda actual: _call_command(
                            actual, _terminal_transport_config(config)
                        ),
                    ),
                    config.runtime_root,
                    timeout_seconds=config.timeout_seconds,
                ),
                # See the direct transport above: native dispatch owns the
                # per-attempt deadline and may retry with a shorter remainder.
                timeout_seconds=None,
            )
    else:
        raise ValueError(f"unknown llm transport: {config.transport!r}")
    call.cpa_cache_identity = _cpa_cache_identity(config)
    if config.runtime_root:
        call.completed_review_root = Path(config.runtime_root).absolute() / 'cache/completed-reviews'
    call.structured_output_transport = True
    call.terminal_retry_capable = True
    call.timeout_seconds = config.timeout_seconds
    return call


def _terminal_transport_config(config: LlmConfig) -> LlmConfig:
    """Bind one physical transport to the active terminal budget."""

    from src.autoslice.infra_retry_policy import terminal_retry_remaining_seconds

    remaining = terminal_retry_remaining_seconds()
    if remaining is None:
        return config
    timeout = min(float(config.timeout_seconds), remaining)
    if timeout <= 0:
        # The dispatch wrapper turns this into a closed transport timeout; the
        # retry scope then records the wall-clock exhaustion explicitly.
        raise ProviderSlotTimeout(
            "terminal retry wall-clock budget exhausted before provider call"
        )
    if timeout == float(config.timeout_seconds):
        return config
    return replace(config, timeout_seconds=timeout)


def _provider_dispatch(
    call: Callable[[], str],
    configured_root: str | None = None,
    *,
    timeout_seconds: float | int | None = None,
) -> str:
    """Apply the shared process-level permit to real provider transports."""

    from src.autoslice.infra_retry_policy import terminal_retry_remaining_seconds

    remaining = terminal_retry_remaining_seconds()
    # Pass the whole remaining budget to the slot queue.  The provider-slot
    # helper resolves an omitted runtime root from the live runtime
    # environment, so this must not depend on ``configured_root`` being
    # populated in the LLM config.  The physical transport is clamped again
    # after the permit is acquired; reserving its full configured timeout here
    # would reject an idle slot whenever remaining < timeout_seconds.
    slot_timeout = remaining if remaining is not None else None
    try:
        if slot_timeout is not None and slot_timeout <= 0:
            # provider_slot() deliberately requires a positive timeout. Keep
            # this inside the same boundary as the real slot call so the
            # caller receives the closed LLM capacity error below.
            raise ProviderSlotTimeout(
                "terminal retry wall-clock budget exhausted before provider slot"
            )
        with runtime_provider_slot(
            runtime_root=configured_root, timeout_seconds=slot_timeout
        ):
            if (
                terminal_retry_remaining_seconds() is not None
                and terminal_retry_remaining_seconds() <= 0
            ):
                raise ProviderSlotTimeout(
                    "terminal retry wall-clock budget exhausted before provider call"
                )
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
    response_schema=None,
    json_mode: bool = True,
) -> object:
    """Retry once only for a typed JSON parse failure, with the same prompt.

    Transport failures and post-extraction schema/coverage failures remain
    single-attempt. The transport and caller share one retry budget.
    """
    if getattr(llm_call, 'cpa_cache_identity', None):
        from src.autoslice.structured_output import call_json
        return extract_json(call_json(llm_call, prompt, response_schema=response_schema))
    from src.autoslice.structured_output import response_contract
    with response_contract(response_schema, json_mode=json_mode, validate=extract_json) as contract:
        contract['parse_retry_only'] = True
        completion = llm_call(prompt)
        try:
            return extract_json(completion)
        except LlmJsonParseError:
            if contract.get('format_retry_handled'):
                raise  # A wrapper must not multiply the transport's two attempts.
            return extract_json(llm_call(prompt))


def _call_direct(prompt: str, config: LlmConfig) -> str:
    """Exhaust transient retries on each ranked model; never downgrade a request error."""
    if not config.api_base or not config.model or not config.api_key_env or not os.environ.get(config.api_key_env):
        return _call_direct_once(prompt, config)
    models = model_ladder(config.model) if config.model else ()
    if not models:
        return _call_direct_once(prompt, config)
    env = os.environ
    attempts = int(env.get('CPA_TRANSIENT_ATTEMPTS_PER_MODEL', '3'))
    if attempts < 1:
        raise ValueError('CPA_TRANSIENT_ATTEMPTS_PER_MODEL must be positive')
    # Preserve explicit non-CPA transports' existing one-shot contract.
    if config.model not in model_ladder():
        return _call_direct_once(prompt, config)
    deadline = chain_deadline_seconds(models=len(models), attempts=attempts)
    started, slept = time.monotonic(), 0.0
    last = None
    for model in models:
        for attempt in range(attempts):
            elapsed = time.monotonic() - started
            remaining = config.timeout_seconds - elapsed
            if remaining <= 0 or (deadline and elapsed >= deadline):
                raise last or LlmCallError('CPA chain deadline reached')
            try:
                return _call_direct_once(prompt, replace(config, model=model,
                    timeout_seconds=min(attempt_timeout_seconds(), remaining)))
            except LlmCallError as exc:
                last = exc
                from .infra_retry_policy import cpa_text_failure_has_no_response, cpa_text_retry_active
                if cpa_text_retry_active() and cpa_text_failure_has_no_response(exc):
                    # The shared one-shot owner journals UNKNOWN and budgets
                    # this failure. Do not hide more response-less attempts in
                    # the direct model ladder before it sees the exception.
                    raise
                code = exc.provider_diagnostics.get('provider_http_status')
                if code and 400 <= code < 500 and code not in (408, 429) and not (
                        code == 400 and exc.provider_diagnostics.get('provider_error_code') == 'group_capability_unavailable'):
                    raise
                if attempt + 1 < attempts:
                    delay = min(max(0, config.timeout_seconds - (time.monotonic() - started)),
                        max(0, float(env.get('CPA_BACKOFF_MAX_TOTAL_SECONDS', '30')) - slept),
                        float(env.get('CPA_BACKOFF_BASE_SECONDS', '2')) * 2 ** attempt * (1 + random.random()))
                    if delay:
                        time.sleep(delay)
                        slept += delay
    raise last or LlmCallError('CPA ladder exhausted')


class CpaCompletion(str):
    """Text is unchanged; physical answering-model provenance travels with it."""
    def __new__(cls, text: str, model: str):
        result = super().__new__(cls, text)
        result.answering_model = model
        return result

    def __getnewargs__(self):
        # Existing consumers copy evidence before adjudication or persistence.
        # The immutable text and its answering-model provenance must travel together.
        return str(self), self.answering_model


def completion_identity(identity: Mapping[str, object] | None, completion: str):
    if identity is None:
        return None
    result = dict(identity)
    if getattr(completion, 'answering_model', None):
        result['models'] = [completion.answering_model]
    return result


def cached_completion(text: str, identity: Mapping[str, object]) -> str:
    """Restore answering-model provenance without changing cached text bytes."""
    models = identity.get('models')
    if isinstance(models, (list, tuple)) and len(models) == 1:
        result = CpaCompletion(text, models[0])
        result.ruling_cache_hit = True
        return result
    return text


def _call_direct_once(prompt: str, config: LlmConfig) -> str:
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
    contract = current_contract(config.response_schema, json_mode=config.json_mode or None)
    response_format = openai_format(contract, prompt=prompt)
    record_cpa_event('structured_output_request', {'kind': 'text', **contract_receipt(contract, prompt=prompt)})
    if response_format:
        if responses_mode:
            request_body['text'] = {'format': response_format}
        else:
            request_body['response_format'] = ({'type': 'json_schema', 'json_schema': {
                key: value for key, value in response_format.items() if key != 'type'}}
                if response_format['type'] == 'json_schema' else response_format)
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
    response_received = False
    try:
        with observed_cpa_request(request, timeout=config.timeout_seconds, model=config.model, kind="text") as response:
            response_received = True
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        headers = getattr(exc, "headers", None)
        header_value = headers.get("Retry-After") if hasattr(headers, "get") else None
        retry_after = _retry_after_seconds(header_value)
        # The existing command route treats this precise 400 as a transient
        # credential-group routing failure, rather than a request rejection.
        group_routing = False
        if exc.code == 400:
            try:
                body = exc.read(65536)
                group_routing = any(token in body for token in (
                    b'group_capability_unavailable', '当前分组不支持'.encode(),
                    b'current group does not support'))
            except (OSError, TypeError, AttributeError):
                pass
        diagnostics = sanitize_provider_diagnostics(
            {
                "provider_http_status": int(exc.code),
                "provider_error_code": 'group_capability_unavailable' if group_routing else f"HTTP_STATUS_{int(exc.code)}",
                "provider_error_message": f"HTTP Error {int(exc.code)}",
                "provider_retry_after_seconds": retry_after,
            }
        )
        raise LlmCallError(
            f"direct LLM call failed: HTTPError: HTTP Error {int(exc.code)}",
            provider_diagnostics=diagnostics,
            response_received=True,
        ) from exc
    except Exception as exc:
        raise LlmCallError(
            f"direct LLM call failed: {type(exc).__name__}: {exc}",
            response_received=response_received,
        ) from exc
    if not isinstance(payload, dict):
        raise LlmCallError('unexpected completion payload shape')
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
        if payload.get('status') not in (None, 'completed') or not isinstance(content, str) or not content.strip():
            raise LlmCallError(f"empty responses completion (status={payload.get('status')})")
        model = payload.get('model') or config.model
        record_cpa_event('completion', {'kind': 'text', 'model': model, 'answering_model': model})
        return CpaCompletion(content, model)
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmCallError(f"unexpected completion payload shape: {exc}") from exc
    if not isinstance(content, str) or not content.strip():
        raise LlmCallError("empty completion content")
    model = payload.get('model') or config.model
    record_cpa_event('completion', {'kind': 'text', 'model': model, 'answering_model': model})
    return CpaCompletion(content, model)



def _command_provider_diagnostics(
    stderr: str,
    context: Mapping[str, object] | None,
) -> dict[str, object]:
    diagnostics = sanitize_provider_diagnostics(context)
    for line in str(stderr or "").splitlines():
        retry_after = _RETRY_AFTER_RX.search(line)
        if retry_after is not None:
            diagnostics["provider_retry_after_seconds"] = int(
                float(retry_after.group("seconds"))
            )
        attempt = _CPA_COMMAND_ATTEMPT_RX.fullmatch(line.strip())
        if attempt is not None:
            # The local CPA bridge emits these closed fields rather than the
            # SSH bridge's JSON marker. Retain the final real attempt without
            # carrying response bodies, curl stderr, or credentials to receipts.
            http_status = int(attempt["http"])
            curl_exit = int(attempt["curl"])
            empty = attempt["empty"] == "1"
            diagnostics.pop("provider_http_status", None)
            diagnostics.pop("provider_error_code", None)
            diagnostics.pop("provider_error_message", None)
            if 100 <= http_status <= 599:
                diagnostics["provider_http_status"] = http_status
            if attempt["result"] == "failed":
                code = attempt["upstream"]
                if code is None:
                    if curl_exit not in (0, 22):
                        code = f"CURL_EXIT_{curl_exit}"
                    elif empty:
                        code = "CPA_EMPTY_OR_INVALID_COMPLETION"
                    elif 400 <= http_status <= 599:
                        code = f"HTTP_STATUS_{http_status}"
                    else:
                        code = "CPA_COMMAND_ATTEMPT_FAILED"
                diagnostics.update(sanitize_provider_diagnostics({
                    "provider_error_code": code,
                    "provider_error_message": (
                        f"CPA http={http_status:03d} curl_exit={curl_exit} "
                        f"empty_completion={int(empty)}"
                    ),
                }))
            continue
        marker_index = line.find(PROVIDER_DIAGNOSTIC_MARKER)
        if marker_index < 0:
            continue
        raw = line[marker_index + len(PROVIDER_DIAGNOSTIC_MARKER) :]
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        diagnostics.update(sanitize_provider_diagnostics(parsed))
    return diagnostics


def _emit_command_provider_diagnostics(
    config: LlmConfig,
    diagnostics: Mapping[str, object],
) -> None:
    if config.command_diagnostic_sink is not None:
        config.command_diagnostic_sink(dict(diagnostics))


def _diagnostic_failure_message(
    *,
    returncode: int,
    stderr: str,
    diagnostics: Mapping[str, object],
) -> str:
    safe_message = str(diagnostics.get("provider_error_message") or "").strip()
    safe_code = str(diagnostics.get("provider_error_code") or "").strip()
    if safe_message:
        return f"llm command failed rc={returncode}: {safe_message}"
    if safe_code:
        return f"llm command failed rc={returncode}: {safe_code}"
    return (
        f"llm command failed rc={returncode}: "
        + _redact_provider_text(str(stderr or "")[-4000:], limit=4000)
    )

def _command_received_response(completion_file: Path, stderr: str, diagnostics: Mapping) -> bool:
    """Do not mistake a bridge's final no-response attempt for the whole call.

    Earlier attempts may have had a real 4xx or a body; their closed bridge
    markers remain decisive even if the last marker has http=000.
    """
    if completion_file.is_file() and completion_file.stat().st_size:
        return True
    raw_response = Path(str(completion_file) + '.response.json')
    if raw_response.is_file() and raw_response.stat().st_size:
        return True
    status = diagnostics.get('provider_http_status')
    if isinstance(status, int) and 100 <= status <= 599:
        return True
    code = str(diagnostics.get('provider_error_code') or '')
    from .infra_retry_policy import CPA_TEXT_NO_RESPONSE_ERROR_CODES
    if (code != CPA_RESPONSE_LOCAL_FAILURE
            and code not in CPA_TEXT_NO_RESPONSE_ERROR_CODES and not code.startswith('CURL_EXIT_')):
        return True
    for line in str(stderr or '').splitlines():
        attempt = _CPA_COMMAND_ATTEMPT_RX.fullmatch(line.strip())
        # body_bytes is the REQUEST size, not evidence of a response.
        if attempt is not None and 100 <= int(attempt['http']) <= 599:
            return True
        if PROVIDER_DIAGNOSTIC_MARKER in line:
            historical = _command_provider_diagnostics(line, None)
            status = historical.get('provider_http_status')
            if isinstance(status, int) and 100 <= status <= 599:
                return True
    # Older bridges have no JSON marker. An explicit HTTP rejection in their
    # stderr still proves a response; inspect only the closed status parser.
    from .provider_failure import provider_failure_status_codes
    return bool(provider_failure_status_codes(stderr))


def _report_command_admission(stderr: str | None) -> None:
    """Retain structured per-attempt limits even if a bridge later succeeds."""
    from .provider_slots import report_provider_feedback
    for line in (stderr or '').splitlines():
        if _CPA_COMMAND_ATTEMPT_RX.fullmatch(line.strip()) or PROVIDER_DIAGNOSTIC_MARKER in line:
            report_provider_feedback(_command_provider_diagnostics(line, None))


def _call_command(prompt: str, config: LlmConfig) -> str:
    if not config.command_template:
        raise LlmCallError("command transport requires command_template")
    with tempfile.TemporaryDirectory(prefix="llm_bridge_") as tmp:
        prompt_file = Path(tmp) / "prompt.txt"
        completion_file = Path(tmp) / "completion.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        command = [
            part.replace("{prompt_file}", str(prompt_file)).replace(
                "{completion_file}", str(completion_file)
            )
            for part in shlex.split(config.command_template)
        ]
        contract = current_contract(config.response_schema, json_mode=config.json_mode or None)
        record_cpa_event('structured_output_request', {'kind': 'text', **contract_receipt(contract, prompt=prompt)})
        # The bridge reads request metadata beside the prompt. Preserve both
        # inherited env=None and an explicitly captured child environment.
        Path(str(prompt_file) + '.response-format.json').write_text(
            json.dumps(openai_format(contract, prompt=prompt)), encoding='utf-8')
        child_env = config.command_child_env
        observation=observation_environment()
        if observation:
            # Credentials/routing remain captured. Observation belongs to the
            # current attempt even when this adapter predates producer startup.
            child_env = {**(child_env if child_env is not None else os.environ), **observation}
        from .infra_retry_policy import cpa_text_attempt_environment, terminal_retry_state, CPA_TEXT_MAX_ATTEMPTS, CPA_TEXT_RETRY_EXHAUSTED
        budget = cpa_text_attempt_environment()
        if _cpa_cache_identity(config) is not None:
            terminal = terminal_retry_state()
            if not budget and terminal is not None:
                used = int(terminal.get('cpa_bridge_attempts', 0))
                if used >= CPA_TEXT_MAX_ATTEMPTS:
                    raise LlmCallError(CPA_TEXT_RETRY_EXHAUSTED,
                        provider_diagnostics={'provider_error_code': CPA_TEXT_RETRY_EXHAUSTED})
                terminal['cpa_bridge_attempts'] = used + 1
                budget = {'AUTOSLICE_CPA_TEXT_ATTEMPT': str(used + 1)}
            if budget:
                child_env = {**(child_env if child_env is not None else os.environ), **budget}
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=config.timeout_seconds,
                env=child_env,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = exc.stderr or ''
            if isinstance(stderr, bytes):
                stderr = stderr.decode('utf-8', errors='replace')
            _report_command_admission(stderr)
            diagnostics = _command_provider_diagnostics(stderr, config.command_diagnostic_context)
            response_received = _command_received_response(completion_file, stderr, diagnostics)
            diagnostics.setdefault('provider_error_code', 'LLM_COMMAND_TIMEOUT')
            diagnostics.setdefault('provider_error_message',
                f'LLM command timed out after {config.timeout_seconds:.0f}s')
            _emit_command_provider_diagnostics(config, diagnostics)
            raise LlmCallError(
                f"llm command timed out after {config.timeout_seconds:.0f}s",
                safe_reason="LLM_COMMAND_TIMEOUT",
                provider_diagnostics=diagnostics,
                response_received=response_received,
            ) from exc
        _report_command_admission(completed.stderr)
        diagnostics = _command_provider_diagnostics(
            completed.stderr,
            config.command_diagnostic_context,
        )
        if completed.returncode != 0:
            _emit_command_provider_diagnostics(config, diagnostics)
            local_failure = diagnostics.get('provider_error_code') == CPA_RESPONSE_LOCAL_FAILURE
            saved_content = None
            if local_failure and completion_file.is_file():
                # Engineering recovery consumes the already-paid answer. The
                # diagnostic remains visible; no second provider dispatch occurs.
                provenance = Path(str(completion_file) + '.provenance.json')
                try:
                    saved_content = completion_file.read_text(encoding='utf-8')
                    if saved_content.strip() and provenance.is_file():
                        model = json.loads(provenance.read_text())['answering_model']
                        if isinstance(model, str) and model:
                            answer = CpaCompletion(saved_content, model)
                            answer.local_failure = dict(diagnostics)
                            return answer
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            failure = LlmCallError(
                _diagnostic_failure_message(
                    returncode=completed.returncode,
                    stderr=completed.stderr,
                    diagnostics=diagnostics,
                ),
                safe_reason="LLM_RESPONSE_LOCAL_FAILURE" if local_failure else "LLM_COMMAND_FAILED",
                provider_diagnostics=diagnostics,
                response_received=_command_received_response(completion_file, completed.stderr, diagnostics),
            )
            failure.cpa_bridge_retryable = bool(budget and _cpa_cache_identity(config) is not None
                and '[cpa-retry] transient=1' in completed.stderr.splitlines())
            if local_failure:
                raw = Path(str(completion_file) + '.response.json')
                try:
                    failure.command_response_body = raw.read_bytes() if raw.is_file() else None
                except OSError:
                    failure.command_response_body = None
                failure.completion = saved_content
            raise failure
        if not completion_file.is_file():
            diagnostics.update(
                sanitize_provider_diagnostics(
                    {
                        "provider_error_code": "LLM_COMMAND_COMPLETION_MISSING",
                        "provider_error_message": (
                            "LLM command did not write the completion file"
                        ),
                    }
                )
            )
            _emit_command_provider_diagnostics(config, diagnostics)
            raise LlmCallError(
                "llm command did not write the completion file",
                safe_reason="LLM_COMMAND_COMPLETION_MISSING",
                provider_diagnostics=diagnostics,
            )
        content = completion_file.read_text(encoding="utf-8")
        if not content.strip():
            diagnostics.update(
                sanitize_provider_diagnostics(
                    {
                        "provider_error_code": "LLM_COMMAND_COMPLETION_EMPTY",
                        "provider_error_message": "LLM command wrote an empty completion",
                    }
                )
            )
            _emit_command_provider_diagnostics(config, diagnostics)
            raise LlmCallError(
                "llm command wrote an empty completion",
                safe_reason="LLM_COMMAND_COMPLETION_EMPTY",
                provider_diagnostics=diagnostics,
            )
        _emit_command_provider_diagnostics(config, diagnostics)
        provenance = Path(str(completion_file) + '.provenance.json')
        model = diagnostics.get('provider_answering_model')
        if provenance.is_file():
            model = json.loads(provenance.read_text()).get('answering_model')
        if model:
            record_cpa_event('completion', {'kind': 'text', 'model': model, 'answering_model': model})
            return CpaCompletion(content, model)
        # Older deployed bridges still disclose the actual successful route in stderr.
        successes = [m for m in _CPA_COMMAND_ATTEMPT_RX.finditer(completed.stderr) if m['result'] == 'ok']
        if successes:
            model = successes[-1]['model']
            record_cpa_event('completion', {'kind': 'text', 'model': model, 'answering_model': model})
            return CpaCompletion(content, model)
        return content
