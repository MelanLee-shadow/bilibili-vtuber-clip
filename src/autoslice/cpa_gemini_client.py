"""Opt-in native Gemini transport. CPA owns credential selection and retries.

The switch is read at dispatch, defaults off, and is never set by this module.
Callers retain their model/provider/cache identity; transport evidence is separate.
No Google key, OAuth file, management endpoint or attribution file is read here.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import stat
import time
from typing import Any, Callable, Mapping, Sequence, TypeAlias, cast
import urllib.error
import urllib.parse
import urllib.request
import uuid

JsonValue: TypeAlias = "None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]"
JsonObject: TypeAlias = dict[str, JsonValue]
JsonMap: TypeAlias = Mapping[str, JsonValue]

ENABLE_ENV = "AUTOSLICE_CPA_GEMINI_ENABLED"
UNKNOWN = "UNKNOWN"
MAX_REQUEST_BYTES = 20_000_000
MAX_RESPONSE_BYTES = 8_000_000


def enabled(environment: Mapping[str, str] | None = None) -> bool:
    return (os.environ if environment is None else environment).get(ENABLE_ENV, "") == "1"


def wire_model(model: str) -> str:
    """Translate spelling only. Never upgrade a different model generation."""
    display = re.fullmatch(r"Gemini (\d+(?:\.\d+)?) (Flash|Argon)(?: \((High|Medium|Low)\))?", model, re.IGNORECASE)
    value = (f"gemini-{display[1]}-{display[2].lower()}" if display else model)
    if re.fullmatch(r'gemini-[0-9.]+-(?:flash|argon)(?:-(?:high|medium|low))?', value, re.IGNORECASE):
        value = value.lower()
    value = re.sub(r'^(gemini-[0-9.]+-(?:flash|argon))-(?:high|medium|low)$', r'\1', value)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError("CPA_GEMINI_MODEL_INVALID")
    return value


def model_thinking(model: str) -> dict[str, str]:
    match = re.fullmatch(r"Gemini \d+(?:\.\d+)? (?:Flash|Argon) \((High|Medium|Low)\)", model, re.IGNORECASE)
    slug = re.fullmatch(r'gemini-[0-9.]+-(?:flash|argon)-(high|medium|low)', model, re.IGNORECASE)
    if match is not None:
        return {"thinkingLevel": match[1].upper()}
    if slug is not None:
        return {"thinkingLevel": slug[1].upper()}
    return {}


def judgment_identity(provider: str, model: str) -> tuple[str, str]:
    """A recognized Gemini model is one witness across all credential transports.

    Preserve raw receipts and unknown model names; this key is for independence
    comparisons, never a claim that an unknown provider/model is supported.
    """
    value = model.strip().rsplit('/', 1)[-1].removesuffix(':latest').strip()
    try:
        canonical = wire_model(value)
    except ValueError:
        return provider, model
    return ('gemini', canonical.lower()) if canonical.lower().startswith('gemini-') else (provider, model)


def file_part(path: Path, mime_type: str | None = None) -> tuple[bytes, str]:
    """Read one bounded regular input, rejecting mutation during acquisition."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_REQUEST_BYTES * 3 // 4:
            raise ValueError("CPA_GEMINI_INLINE_INPUT_INVALID_OR_TOO_LARGE")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(MAX_REQUEST_BYTES * 3 // 4 + 1)
        after = os.fstat(descriptor)
        current = path.stat(follow_symlinks=False)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, key) != getattr(after, key) or
               getattr(after, key) != getattr(current, key) for key in fields):
            raise ValueError("CPA_GEMINI_INPUT_DRIFT")
    finally:
        os.close(descriptor)
    mime = mime_type or mimetypes.guess_type(path.name)[0]
    if not mime:
        raise ValueError("CPA_GEMINI_MIME_REQUIRED")
    return data, mime


@dataclass(frozen=True)
class Completion:
    text: str
    payload: JsonMap
    receipt: JsonMap


class CpaGeminiError(RuntimeError):
    def __init__(self, code: int | None, reason_code: str, retry_after_seconds: int | None = None):
        super().__init__(reason_code)
        self.code = code
        self.reason_code = reason_code
        self.retry_after_seconds = retry_after_seconds


def require_operation(operation: str) -> None:
    """Never fall through to direct Google for an unsupported CPA protocol."""
    if operation not in {'generateContent', 'inline_media'}:
        raise CpaGeminiError(None, 'CPA_GEMINI_UNSUPPORTED_OPERATION:' + operation)


def _append(path: Path, row: JsonMap) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("CPA_GEMINI_LEDGER_INVALID")
        data = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode()
        if os.write(descriptor, data) != len(data):
            raise OSError("CPA_GEMINI_LEDGER_SHORT_WRITE")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _text(payload: JsonMap) -> str:
    candidates = payload.get("candidates") or []
    if not candidates or not isinstance(candidates, list) or not isinstance(candidates[0], dict):
        raise CpaGeminiError(200, "CPA_GEMINI_EMPTY_RESPONSE")
    content = candidates[0].get('content')
    if not isinstance(content, dict) or not isinstance(content.get('parts'), list):
        raise CpaGeminiError(200, 'CPA_GEMINI_EMPTY_RESPONSE')
    parts = cast(list[JsonValue], content['parts'])
    texts: list[str] = []
    for part in parts:
        if isinstance(part, dict):
            text = part.get('text')
            if isinstance(text, str) and not part.get('thought'):
                texts.append(text)
    result = ''.join(texts)
    if not result.strip():
        raise CpaGeminiError(200, "CPA_GEMINI_EMPTY_RESPONSE")
    return result


def _attribute(row: JsonObject, headers: Any, tiers: Mapping[str, str]) -> None:
    trace = headers.get('X-CPA-TRACE-ID', '')
    if isinstance(trace, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', trace):
        row['trace_id'] = trace
        bits = trace.split('-')
        if len(bits) >= 3 and re.fullmatch(r'[a-fA-F0-9]{16}', bits[1]):
            row['auth_index'] = bits[1]
    index = row['auth_index']
    if isinstance(index, str) and index in tiers:
        row['key_tier'] = tiers[index]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        raise CpaGeminiError(code, 'CPA_GEMINI_REDIRECT_REJECTED')


def _open(request: urllib.request.Request, *, timeout: float) -> Any:
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def generate_native(*, model: str, body: JsonMap, purpose: str,
                    timeout_seconds: float = 180,
                    environment: Mapping[str, str] | None = None,
                    opener: Callable[..., Any] | None = None) -> Completion:
    """One CPA POST, with durable pre-dispatch and terminal cost records.

    Unknown accounting remains unknown, including failed/ambiguous requests.
    A trace identifies the final credential selected by CPA, not all its internal
    attempts. No client rotation, cooldown, paid gate or retry is performed.
    """
    env = os.environ if environment is None else environment
    if not enabled(env):
        raise ValueError("CPA_GEMINI_SWITCH_DISABLED")
    base = env.get("CPA_BASE_URL", "").rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("CPA_GEMINI_BASE_URL_INVALID")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("CPA_GEMINI_TIMEOUT_INVALID")
    key = env.get("CPA_API_KEY", "")
    if not key:
        raise ValueError("CPA_GEMINI_KEY_MISSING")
    actual_model = wire_model(model)
    tiers = json.loads(env.get("AUTOSLICE_CPA_GEMINI_AUTH_TIERS", "{}"))
    if not isinstance(tiers, dict) or any(not isinstance(index, str) or
            tier not in {"subscription", "free", "paid"} for index, tier in tiers.items()):
        raise ValueError("CPA_GEMINI_AUTH_TIERS_INVALID")
    request_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if len(request_bytes) > MAX_REQUEST_BYTES:
        raise ValueError("CPA_GEMINI_REQUEST_TOO_LARGE")
    # Accept the root or an explicitly bound /v1beta native API base.
    if parsed.path.rstrip('/').endswith('/v1'):
        base = base[:-3]
    native_base = base if base.endswith("/v1beta") else base + "/v1beta"
    request = urllib.request.Request(
        native_base + "/models/" + urllib.parse.quote(actual_model, safe="") + ":generateContent",
        data=request_bytes, headers={"Content-Type": "application/json",
            "Authorization": "Bearer " + key, "User-Agent": "Mozilla/5.0 autoslice-cpa-gemini"})
    ledger = Path(env.get("AUTOSLICE_CPA_GEMINI_LEDGER") or
                  str(Path(env.get("AUTOSLICE_BASE", "/opt/bilive/autoslice")) / "state/cpa-gemini/requests.jsonl"))
    row: JsonObject = {"schema_version": "cpa-gemini-attempt.v1", "request_id": uuid.uuid4().hex,
        "recorded_at": datetime.now(timezone.utc).isoformat(), "purpose": purpose,
        "identity_model": model, "wire_model": actual_model, "transport": "cpa_gemini_native",
        "request_sha256": hashlib.sha256(request_bytes).hexdigest(), "request_bytes": len(request_bytes),
        "key_tier": UNKNOWN, "auth_index": UNKNOWN, "trace_id": UNKNOWN, "cost_usd": UNKNOWN,
        "upstream_attempts": UNKNOWN, "status": "DISPATCH_RESERVED"}
    _append(ledger, row)  # Persistence failure must prevent unaccounted spend.
    started = time.monotonic()
    payload: JsonMap = {}
    result = ""
    try:
        with (opener or _open)(request, timeout=timeout_seconds) as response:
            row["http_status"] = getattr(response, "status", 200)
            _attribute(row, response.headers, tiers)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("CPA_GEMINI_RESPONSE_TOO_LARGE")
            decoded = json.loads(raw)
            if not isinstance(decoded, dict):
                raise ValueError("CPA_GEMINI_RESPONSE_INVALID")
            payload = decoded
            row["response_sha256"] = hashlib.sha256(raw).hexdigest()
            usage = payload.get('usageMetadata')
            row["usage"] = {name: value for name, value in (usage if isinstance(usage, dict) else {}).items()
                            if isinstance(value, int) and not isinstance(value, bool)}
            row["response_model"] = payload.get("modelVersion", UNKNOWN)
            result = _text(payload)
            row['text_sha256'] = hashlib.sha256(result.encode('utf-8')).hexdigest()
            row["status"] = "RESPONDED"
    except urllib.error.HTTPError as exc:
        _attribute(row, exc.headers or {}, tiers)
        retry = exc.headers.get("Retry-After", "") if exc.headers else ""
        delay = int(retry) if retry.isdigit() else None
        row.update(status="HTTP_ERROR", http_status=exc.code, retry_after_seconds=delay)
        # No raw error text: upstream errors may contain credential material.
        raise CpaGeminiError(exc.code, "CPA_GEMINI_HTTP_ERROR", delay) from None
    except BaseException as exc:
        row.update(status="FAILED_OR_AMBIGUOUS", error_type=type(exc).__name__)
        raise
    finally:
        row["elapsed_seconds"] = round(time.monotonic() - started, 3)
        _append(ledger, row)
    return Completion(result, payload, row)


def generate(*, prompt: str, model: str, purpose: str,
             inline_parts: Sequence[tuple[bytes, str]] = (),
             generation_config: JsonMap | None = None,
             timeout_seconds: float = 180, **kwargs: Any) -> Completion:
    return generate_native(model=model, purpose=purpose, body=inline_body(prompt=prompt, model=model,
        inline_parts=inline_parts, generation_config=generation_config), timeout_seconds=timeout_seconds, **kwargs)


def inline_body(*, prompt: str, model: str, inline_parts: Sequence[tuple[bytes, str]],
                generation_config: JsonMap | None = None) -> JsonObject:
    parts: list[JsonValue] = [{"text": prompt}]
    for data, mime in inline_parts:
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode("ascii")}})
    config: JsonObject = dict(generation_config or {})
    thinking = model_thinking(model)
    if thinking and "thinkingConfig" not in config:
        config["thinkingConfig"] = JsonObject(thinking)
    return {'contents': [{'parts': parts}], 'generationConfig': config}


def generate_files(*, prompt: str, model: str, purpose: str, files: Sequence[Path],
                   timeout_seconds: float = 180, response_mime_type: str | None = "application/json",
                   response_schema: JsonMap | None = None) -> Completion:
    config: JsonObject = {"maxOutputTokens": 65536}
    if response_mime_type:
        config["responseMimeType"] = response_mime_type
    if response_schema:
        config["responseJsonSchema"] = dict(response_schema)
    body = files_body(prompt=prompt, model=model, files=files, generation_config=config)
    return generate_native(model=model, purpose=purpose, body=body, timeout_seconds=timeout_seconds)


def files_body(*, prompt: str, model: str, files: Sequence[Path],
               generation_config: JsonMap | None = None) -> JsonObject:
    """Faithful file-tool rendering: content is attached, output is returned."""
    rendered = prompt + ('\n\nTransport instructions: all listed input files are supplied below in order. '
        'Use only these attached bytes. You have no file tools. Return the requested output file content '
        'directly in your response, without claiming to open or write local files.\n')
    parts: list[JsonValue] = [{'text': rendered}]
    for path in files:
        if path.suffix.lower() in {'.txt', '.srt', '.lrc', '.md', '.json', '.jsonl'}:
            data, _ = file_part(path, 'text/plain')
            parts.append({'text': data.decode('utf-8')})
        else:
            data, mime = file_part(path)
            parts.append({'inline_data': {'mime_type': mime, 'data': base64.b64encode(data).decode('ascii')}})
    config: JsonObject = dict(generation_config) if generation_config is not None else {
        'maxOutputTokens': 65536, 'responseMimeType': 'application/json'}
    thinking = model_thinking(model)
    if thinking and 'thinkingConfig' not in config:
        config['thinkingConfig'] = JsonObject(thinking)
    return {'contents': [{'parts': parts}], 'generationConfig': config}


def validate_file_receipt(*, receipt: JsonMap, prompt: str, model: str,
                          files: Sequence[Path], text: str,
                          generation_config: JsonMap | None = None) -> None:
    expected = hashlib.sha256(json.dumps(files_body(prompt=prompt, model=model, files=files,
                                                   generation_config=generation_config),
                                        ensure_ascii=False).encode('utf-8')).hexdigest()
    if (receipt.get('schema_version') != 'cpa-gemini-attempt.v1'
            or receipt.get('transport') != 'cpa_gemini_native'
            or receipt.get('identity_model') != model or receipt.get('wire_model') != wire_model(model)
            or receipt.get('status') != 'RESPONDED' or receipt.get('http_status') != 200
            or receipt.get('request_sha256') != expected
            or receipt.get('text_sha256') != hashlib.sha256(text.encode('utf-8')).hexdigest()):
        raise ValueError('CPA_GEMINI_INPUT_RECEIPT_MISMATCH')


def valid_lrc_transport(manifest: JsonMap) -> bool:
    """Cold validation of exact audio, untimed lyrics, prompt and output bytes."""
    receipt = manifest.get('cpa_transport_receipt')
    if not isinstance(receipt, dict):
        return False
    try:
        evidence = manifest['observation_input']
        artifacts = manifest['artifacts']
        model = manifest['model']
        if not isinstance(evidence, dict) or not isinstance(artifacts, dict) or not isinstance(model, str):
            return False
        def bound(path_key: str, hash_key: str, values: JsonMap) -> Path:
            raw_path = values[path_key]
            if not isinstance(raw_path, str):
                raise ValueError('CPA_GEMINI_BOUND_PATH_INVALID')
            path = Path(raw_path)
            data, _ = file_part(path, 'application/octet-stream')
            if hashlib.sha256(data).hexdigest() != values[hash_key]:
                raise ValueError('CPA_GEMINI_BOUND_INPUT_DRIFT')
            return path
        audio = bound('audio_path', 'audio_sha256', evidence)
        lyrics = bound('lyrics_path', 'lyrics_sha256', evidence)
        prompt = bound('prompt_path', 'prompt_sha256', artifacts)
        output = bound('provider_raw_output_path', 'provider_raw_output_sha256', artifacts)
        validate_file_receipt(receipt=receipt, prompt=prompt.read_text(encoding='utf-8'),
            model=model, files=[audio, lyrics], text=output.read_text(encoding='utf-8'))
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False
