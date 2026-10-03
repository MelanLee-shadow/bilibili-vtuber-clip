"""Exact-byte Gemini Files API transport without an SDK dependency.

The module is intentionally lower-level than the final-media adapter.  It
uploads one already hash-bound local file through the documented resumable
Files API, waits for an ACTIVE file resource, calls ``generateContent`` with a
``file_data`` reference, and attempts remote cleanup.  Only configured free
keys are considered; the paid backup key is never read here.

No method acquires the autoslice provider slot.  Callers already hold that
slot around the hash-bound adapter process, so acquiring it again would
self-deadlock.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import time
from typing import Callable, Iterator, Mapping, Protocol, Sequence
import urllib.error
import urllib.parse
import urllib.request


FILES_UPLOAD_URL = (
    "https://generativelanguage.googleapis.com/upload/v1beta/files"
)
FILES_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GENERATE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)
FREE_KEY_ENV_NAMES: tuple[str, ...] = (
    "GEMINI_API_KEY",
    "GEMINI_API_KEY_2",
    "GEMINI_API_KEY_3",
)
_MAX_FILE_BYTES = 2_000_000_000
_MAX_JSON_BYTES = 8_000_000
_MAX_TEXT_BYTES = 2_000_000
_CHUNK_BYTES = 1024 * 1024
_FILE_NAME_RE = re.compile(r"^files/[A-Za-z0-9._~-]{1,512}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MIME_RE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")


class GeminiFileApiError(RuntimeError):
    """Typed Gemini Files API transport or response failure."""

    def __init__(
        self,
        reason_code: str,
        detail: str,
        *,
        phase: str,
        http_status: int | None = None,
        ambiguous: bool = False,
        failures: Sequence[Mapping[str, object]] = (),
        cleanup_status: str | None = None,
    ):
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail
        self.phase = phase
        self.http_status = http_status
        self.ambiguous = ambiguous
        self.failures = [dict(row) for row in failures]
        self.cleanup_status = cleanup_status


@dataclass(frozen=True)
class GeminiFileCallOutcome:
    text: str
    evidence: dict[str, object]


@dataclass(frozen=True)
class _StableFile:
    path: Path
    descriptor: int
    size: int
    sha256: str
    identity: tuple[int, int, int, int, int, int]


class GeminiFileTransport(Protocol):
    def start_upload(
        self,
        *,
        key: str,
        size: int,
        mime_type: str,
        display_name: str,
        timeout_seconds: float,
    ) -> str: ...

    def upload(
        self,
        *,
        upload_url: str,
        descriptor: int,
        size: int,
        timeout_seconds: float,
    ) -> Mapping[str, object]: ...

    def get_file(
        self,
        *,
        key: str,
        name: str,
        timeout_seconds: float,
    ) -> Mapping[str, object]: ...

    def generate(
        self,
        *,
        key: str,
        model: str,
        file_uri: str,
        mime_type: str,
        prompt: str,
        video_fps: float | None,
        timeout_seconds: float,
    ) -> Mapping[str, object]: ...

    def delete_file(
        self,
        *,
        key: str,
        name: str,
        timeout_seconds: float,
    ) -> None: ...


def _error_reason(status: int) -> str:
    if status == 429:
        return "GEMINI_API_QUOTA_EXHAUSTED"
    if status in {401, 403}:
        return "GEMINI_API_AUTH_FAILED"
    if 500 <= status <= 599:
        return "GEMINI_API_SERVER_ERROR"
    return "GEMINI_API_REQUEST_FAILED"


def _http_error(
    exc: urllib.error.HTTPError,
    *,
    phase: str,
) -> GeminiFileApiError:
    try:
        body = exc.read(4096).decode("utf-8", errors="replace")
    except Exception:
        body = ""
    detail = re.sub(r"\s+", " ", body).strip()[:500]
    return GeminiFileApiError(
        _error_reason(exc.code),
        detail or f"Gemini API returned HTTP {exc.code}",
        phase=phase,
        http_status=exc.code,
        ambiguous=500 <= exc.code <= 599,
    )


def _network_error(exc: BaseException, *, phase: str) -> GeminiFileApiError:
    timeout = isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(
        getattr(exc, "reason", None), (TimeoutError, socket.timeout)
    )
    return GeminiFileApiError(
        "GEMINI_API_TIMEOUT" if timeout else "GEMINI_API_NETWORK_ERROR",
        f"{phase} did not return a definite response",
        phase=phase,
        ambiguous=True,
    )


def _json_bytes(value: Mapping[str, object]) -> bytes:
    try:
        payload = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise GeminiFileApiError(
            "GEMINI_API_REQUEST_INVALID",
            "request cannot be encoded as canonical JSON",
            phase="local_request",
        ) from exc
    if len(payload) > _MAX_JSON_BYTES:
        raise GeminiFileApiError(
            "GEMINI_API_REQUEST_INVALID",
            "request JSON exceeds the bounded transport size",
            phase="local_request",
        )
    return payload


def _response_json(response: object, *, phase: str) -> dict[str, object]:
    read = getattr(response, "read", None)
    if not callable(read):
        raise GeminiFileApiError(
            "GEMINI_API_INVALID_OUTPUT",
            f"{phase} response has no readable body",
            phase=phase,
        )
    data = read(_MAX_JSON_BYTES + 1)
    if not isinstance(data, bytes) or not 0 < len(data) <= _MAX_JSON_BYTES:
        raise GeminiFileApiError(
            "GEMINI_API_INVALID_OUTPUT",
            f"{phase} response body is empty or oversized",
            phase=phase,
        )
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GeminiFileApiError(
            "GEMINI_API_INVALID_OUTPUT",
            f"{phase} response is not UTF-8 JSON",
            phase=phase,
        ) from exc
    if not isinstance(value, dict):
        raise GeminiFileApiError(
            "GEMINI_API_INVALID_OUTPUT",
            f"{phase} response root is not an object",
            phase=phase,
        )
    return value


def _urlopen_json(
    request: urllib.request.Request,
    *,
    phase: str,
    timeout_seconds: float,
    urlopen: Callable[..., object],
) -> tuple[dict[str, object], object]:
    try:
        response = urlopen(request, timeout=timeout_seconds)
        enter = getattr(response, "__enter__", None)
        if callable(enter):
            with response as managed:
                return _response_json(managed, phase=phase), managed
        return _response_json(response, phase=phase), response
    except urllib.error.HTTPError as exc:
        raise _http_error(exc, phase=phase) from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise _network_error(exc, phase=phase) from exc


def _validated_upload_url(value: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not host
        or not (host == "googleapis.com" or host.endswith(".googleapis.com"))
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or not parsed.path.startswith("/")
    ):
        raise GeminiFileApiError(
            "GEMINI_API_UPLOAD_URL_INVALID",
            "resumable upload URL is outside the accepted Google HTTPS origin",
            phase="upload_start",
        )
    return parsed


def _bounded_http_json(
    response: http.client.HTTPResponse,
    *,
    phase: str,
) -> dict[str, object]:
    data = response.read(_MAX_JSON_BYTES + 1)
    if not 200 <= response.status <= 299:
        detail = data.decode("utf-8", errors="replace")[:500]
        raise GeminiFileApiError(
            _error_reason(response.status),
            re.sub(r"\s+", " ", detail).strip()
            or f"Gemini API returned HTTP {response.status}",
            phase=phase,
            http_status=response.status,
            ambiguous=500 <= response.status <= 599,
        )
    if not 0 < len(data) <= _MAX_JSON_BYTES:
        raise GeminiFileApiError(
            "GEMINI_API_INVALID_OUTPUT",
            f"{phase} response body is empty or oversized",
            phase=phase,
        )
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GeminiFileApiError(
            "GEMINI_API_INVALID_OUTPUT",
            f"{phase} response is not UTF-8 JSON",
            phase=phase,
        ) from exc
    if not isinstance(value, dict):
        raise GeminiFileApiError(
            "GEMINI_API_INVALID_OUTPUT",
            f"{phase} response root is not an object",
            phase=phase,
        )
    return value


class UrllibGeminiFileTransport:
    """REST implementation using only the Python standard library."""

    def __init__(
        self,
        *,
        urlopen: Callable[..., object] = urllib.request.urlopen,
        connection_factory: Callable[..., http.client.HTTPSConnection] = (
            http.client.HTTPSConnection
        ),
    ):
        self._urlopen = urlopen
        self._connection_factory = connection_factory

    def start_upload(
        self,
        *,
        key: str,
        size: int,
        mime_type: str,
        display_name: str,
        timeout_seconds: float,
    ) -> str:
        body = _json_bytes({"file": {"display_name": display_name}})
        request = urllib.request.Request(
            FILES_UPLOAD_URL,
            data=body,
            method="POST",
            headers={
                "content-type": "application/json",
                "x-goog-api-key": key,
                "x-goog-upload-protocol": "resumable",
                "x-goog-upload-command": "start",
                "x-goog-upload-header-content-length": str(size),
                "x-goog-upload-header-content-type": mime_type,
            },
        )
        try:
            response = self._urlopen(request, timeout=timeout_seconds)
            enter = getattr(response, "__enter__", None)
            if callable(enter):
                with response as managed:
                    headers = getattr(managed, "headers", None)
                    upload_url = headers.get("x-goog-upload-url") if headers else None
                    read = getattr(managed, "read", None)
                    if callable(read):
                        read(_MAX_JSON_BYTES + 1)
            else:
                headers = getattr(response, "headers", None)
                upload_url = headers.get("x-goog-upload-url") if headers else None
                read = getattr(response, "read", None)
                if callable(read):
                    read(_MAX_JSON_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise _http_error(exc, phase="upload_start") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise _network_error(exc, phase="upload_start") from exc
        if not isinstance(upload_url, str) or not upload_url:
            raise GeminiFileApiError(
                "GEMINI_API_INVALID_OUTPUT",
                "upload start response omitted x-goog-upload-url",
                phase="upload_start",
            )
        _validated_upload_url(upload_url)
        return upload_url

    def upload(
        self,
        *,
        upload_url: str,
        descriptor: int,
        size: int,
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        parsed = _validated_upload_url(upload_url)
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        connection = self._connection_factory(
            parsed.hostname,
            parsed.port or 443,
            timeout=timeout_seconds,
            context=ssl.create_default_context(),
        )
        duplicate = os.dup(descriptor)
        sent = 0
        try:
            connection.putrequest("POST", target)
            connection.putheader("content-length", str(size))
            connection.putheader("x-goog-upload-offset", "0")
            connection.putheader("x-goog-upload-command", "upload, finalize")
            connection.endheaders()
            with os.fdopen(duplicate, "rb", closefd=True) as handle:
                while sent < size:
                    chunk = handle.read(min(_CHUNK_BYTES, size - sent))
                    if not chunk:
                        break
                    connection.send(chunk)
                    sent += len(chunk)
            duplicate = -1
            if sent != size:
                raise GeminiFileApiError(
                    "GEMINI_API_LOCAL_FILE_DRIFT",
                    "exact upload ended before the declared byte count",
                    phase="upload_bytes",
                    ambiguous=sent > 0,
                )
            return _bounded_http_json(
                connection.getresponse(), phase="upload_bytes"
            )
        except GeminiFileApiError:
            raise
        except (TimeoutError, socket.timeout, OSError, http.client.HTTPException) as exc:
            raise _network_error(exc, phase="upload_bytes") from exc
        finally:
            if duplicate >= 0:
                os.close(duplicate)
            connection.close()

    def get_file(
        self,
        *,
        key: str,
        name: str,
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        request = urllib.request.Request(
            f"{FILES_API_BASE}/{urllib.parse.quote(name, safe='/')}",
            headers={"x-goog-api-key": key},
        )
        value, _response = _urlopen_json(
            request,
            phase="file_poll",
            timeout_seconds=timeout_seconds,
            urlopen=self._urlopen,
        )
        return value

    def generate(
        self,
        *,
        key: str,
        model: str,
        file_uri: str,
        mime_type: str,
        prompt: str,
        video_fps: float | None,
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        media_part: dict[str, object] = {
            "file_data": {
                "mime_type": mime_type,
                "file_uri": file_uri,
            }
        }
        if video_fps is not None:
            media_part["video_metadata"] = {"fps": video_fps}
        body = _json_bytes(
            {
                "contents": [
                    {
                        "parts": [
                            media_part,
                            {"text": prompt},
                        ]
                    }
                ],
                "generationConfig": {
                    "temperature": 0.1,
                    "maxOutputTokens": 65_536,
                    "responseMimeType": "application/json",
                },
            }
        )
        request = urllib.request.Request(
            GENERATE_URL.format(model=urllib.parse.quote(model, safe="")),
            data=body,
            method="POST",
            headers={
                "content-type": "application/json",
                "x-goog-api-key": key,
            },
        )
        value, _response = _urlopen_json(
            request,
            phase="generate_content",
            timeout_seconds=timeout_seconds,
            urlopen=self._urlopen,
        )
        return value

    def delete_file(
        self,
        *,
        key: str,
        name: str,
        timeout_seconds: float,
    ) -> None:
        request = urllib.request.Request(
            f"{FILES_API_BASE}/{urllib.parse.quote(name, safe='/')}",
            method="DELETE",
            headers={"x-goog-api-key": key},
        )
        try:
            response = self._urlopen(request, timeout=timeout_seconds)
            enter = getattr(response, "__enter__", None)
            if callable(enter):
                with response as managed:
                    read = getattr(managed, "read", None)
                    if callable(read):
                        read(_MAX_JSON_BYTES + 1)
            else:
                read = getattr(response, "read", None)
                if callable(read):
                    read(_MAX_JSON_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise _http_error(exc, phase="file_delete") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise _network_error(exc, phase="file_delete") from exc


@contextmanager
def _stable_file(
    path: Path,
    *,
    expected_sha256: str,
    expected_bytes: int,
) -> Iterator[_StableFile]:
    raw = path.expanduser()
    try:
        info = raw.lstat()
        resolved = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GeminiFileApiError(
            "GEMINI_API_LOCAL_FILE_INVALID",
            "bound media file is unavailable",
            phase="local_file",
        ) from exc
    if (
        raw.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or not resolved.is_file()
        or info.st_nlink != 1
        or not 0 < info.st_size <= _MAX_FILE_BYTES
        or info.st_size != expected_bytes
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
    ):
        raise GeminiFileApiError(
            "GEMINI_API_LOCAL_FILE_INVALID",
            "bound media file type, links, size, or expected hash is invalid",
            phase="local_file",
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise GeminiFileApiError(
            "GEMINI_API_LOCAL_FILE_INVALID",
            "bound media file could not be opened safely",
            phase="local_file",
        ) from exc
    identity = (
        info.st_dev,
        info.st_ino,
        stat.S_IMODE(info.st_mode),
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )
    try:
        opened = os.fstat(descriptor)
        digest = hashlib.sha256()
        total = 0
        while total <= expected_bytes:
            chunk = os.read(
                descriptor,
                min(_CHUNK_BYTES, expected_bytes + 1 - total),
            )
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        observed = (
            opened.st_dev,
            opened.st_ino,
            stat.S_IMODE(opened.st_mode),
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        if (
            observed != identity
            or total != expected_bytes
            or digest.hexdigest() != expected_sha256
        ):
            raise GeminiFileApiError(
                "GEMINI_API_LOCAL_FILE_DRIFT",
                "bound media file differs from its exact request binding",
                phase="local_file",
            )
        os.lseek(descriptor, 0, os.SEEK_SET)
        stable = _StableFile(
            path=resolved,
            descriptor=descriptor,
            size=expected_bytes,
            sha256=expected_sha256,
            identity=identity,
        )
        try:
            yield stable
        finally:
            after = os.fstat(descriptor)
            final = os.lstat(resolved)
            after_identity = (
                after.st_dev,
                after.st_ino,
                stat.S_IMODE(after.st_mode),
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            final_identity = (
                final.st_dev,
                final.st_ino,
                stat.S_IMODE(final.st_mode),
                final.st_size,
                final.st_mtime_ns,
                final.st_ctime_ns,
            )
            if after_identity != identity or final_identity != identity:
                raise GeminiFileApiError(
                    "GEMINI_API_LOCAL_FILE_DRIFT",
                    "bound media file identity changed during provider use",
                    phase="local_file_postflight",
                    ambiguous=True,
                )
    finally:
        os.close(descriptor)

def _file_object(value: Mapping[str, object], *, phase: str) -> dict[str, object]:
    """Normalize one possibly-partial Files API resource.

    The upload-finalize response may expose only ``name`` before processing
    metadata is populated.  ``uri``, MIME type and state therefore remain
    optional until a later poll reports an ACTIVE resource.
    """

    raw = value.get("file") if isinstance(value.get("file"), Mapping) else value
    if not isinstance(raw, Mapping):
        raise GeminiFileApiError(
            "GEMINI_API_INVALID_OUTPUT",
            f"{phase} response omitted the file resource",
            phase=phase,
        )
    name = raw.get("name")
    uri = raw.get("uri")
    mime = raw.get("mimeType") or raw.get("mime_type")
    state = raw.get("state")
    if isinstance(state, Mapping):
        state = state.get("name")
    if not isinstance(name, str) or _FILE_NAME_RE.fullmatch(name) is None:
        raise GeminiFileApiError(
            "GEMINI_API_INVALID_OUTPUT",
            f"{phase} file resource name is invalid",
            phase=phase,
        )
    if uri is not None and (
        not isinstance(uri, str)
        or not uri.startswith(f"{FILES_API_BASE}/files/")
    ):
        raise GeminiFileApiError(
            "GEMINI_API_INVALID_OUTPUT",
            f"{phase} file resource URI is invalid",
            phase=phase,
        )
    if mime is not None and (
        not isinstance(mime, str) or _MIME_RE.fullmatch(mime) is None
    ):
        raise GeminiFileApiError(
            "GEMINI_API_INVALID_OUTPUT",
            f"{phase} file resource MIME type is invalid",
            phase=phase,
        )
    if state is not None and not isinstance(state, str):
        raise GeminiFileApiError(
            "GEMINI_API_INVALID_OUTPUT",
            f"{phase} file resource state is invalid",
            phase=phase,
        )
    return {
        "name": name,
        "uri": uri,
        "mime_type": mime,
        "state": state.upper() if isinstance(state, str) else "UNSPECIFIED",
    }


def _response_text(value: Mapping[str, object]) -> str:
    candidates = value.get("candidates")
    candidate = candidates[0] if isinstance(candidates, list) and candidates else None
    content = candidate.get("content") if isinstance(candidate, Mapping) else None
    parts = content.get("parts") if isinstance(content, Mapping) else None
    if not isinstance(parts, list):
        return ""
    text = "".join(
        str(part.get("text") or "") for part in parts if isinstance(part, Mapping)
    )
    if not text.strip() or len(text.encode("utf-8")) > _MAX_TEXT_BYTES:
        return ""
    return text


def configured_free_keys(
    environment: Mapping[str, str],
) -> list[tuple[str, str]]:
    """Return distinct free-key values with their non-secret env identities."""

    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name in FREE_KEY_ENV_NAMES:
        value = environment.get(name)
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        rows.append((name, value))
    return rows


def call_file_model(
    *,
    media_path: Path,
    expected_sha256: str,
    expected_bytes: int,
    mime_type: str,
    prompt: str,
    key: str,
    model: str,
    video_fps: float | None,
    timeout_seconds: float,
    poll_timeout_seconds: float,
    poll_interval_seconds: float = 2.0,
    transport: GeminiFileTransport | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> GeminiFileCallOutcome:
    """Upload exact bytes, wait for ACTIVE, generate, and attempt cleanup."""

    if (
        _MODEL_RE.fullmatch(model) is None
        or _MIME_RE.fullmatch(mime_type) is None
        or not prompt.strip()
        or not key
        or (
            mime_type.startswith("video/")
            and (
                isinstance(video_fps, bool)
                or not isinstance(video_fps, (int, float))
                or not 0.0 < float(video_fps) <= 24.0
            )
        )
        or (not mime_type.startswith("video/") and video_fps is not None)
        or not 1 <= timeout_seconds <= 3600
        or not 1 <= poll_timeout_seconds <= 3600
        or not 0 <= poll_interval_seconds <= 60
    ):
        raise GeminiFileApiError(
            "GEMINI_API_REQUEST_INVALID",
            "model, MIME type, video FPS, prompt, key, or timeout configuration is invalid",
            phase="local_request",
        )
    selected = transport or UrllibGeminiFileTransport()
    resource: dict[str, object] | None = None
    cleanup_status = "NOT_STARTED"
    poll_count = 0
    http_request_count = 0
    with _stable_file(
        media_path,
        expected_sha256=expected_sha256,
        expected_bytes=expected_bytes,
    ) as stable:
        try:
            upload_url = selected.start_upload(
                key=key,
                size=stable.size,
                mime_type=mime_type,
                display_name=f"autoslice-{stable.sha256[:20]}",
                timeout_seconds=timeout_seconds,
            )
            http_request_count += 1
            uploaded = selected.upload(
                upload_url=upload_url,
                descriptor=stable.descriptor,
                size=stable.size,
                timeout_seconds=timeout_seconds,
            )
            http_request_count += 1
            resource = _file_object(uploaded, phase="upload_bytes")
            deadline = time.monotonic() + poll_timeout_seconds
            while True:
                state = str(resource["state"])
                ready = (
                    state == "ACTIVE"
                    and isinstance(resource.get("uri"), str)
                    and isinstance(resource.get("mime_type"), str)
                )
                if ready:
                    break
                if state not in {"UNSPECIFIED", "PROCESSING", "ACTIVE"}:
                    raise GeminiFileApiError(
                        "GEMINI_API_FILE_PROCESSING_FAILED",
                        f"uploaded file entered terminal state {state}",
                        phase="file_poll",
                    )
                if time.monotonic() >= deadline:
                    raise GeminiFileApiError(
                        "GEMINI_API_FILE_PROCESSING_TIMEOUT",
                        "uploaded file did not become a complete ACTIVE resource before the deadline",
                        phase="file_poll",
                        ambiguous=False,
                    )
                sleeper(poll_interval_seconds)
                resource = _file_object(
                    selected.get_file(
                        key=key,
                        name=str(resource["name"]),
                        timeout_seconds=timeout_seconds,
                    ),
                    phase="file_poll",
                )
                http_request_count += 1
                poll_count += 1
            if resource["mime_type"] != mime_type:
                raise GeminiFileApiError(
                    "GEMINI_API_INVALID_OUTPUT",
                    "ACTIVE file resource MIME type differs from exact input",
                    phase="file_poll",
                )
            generated = selected.generate(
                key=key,
                model=model,
                file_uri=str(resource["uri"]),
                mime_type=mime_type,
                prompt=prompt,
                video_fps=(float(video_fps) if video_fps is not None else None),
                timeout_seconds=timeout_seconds,
            )
            http_request_count += 1
            text = _response_text(generated)
            if not text:
                raise GeminiFileApiError(
                    "GEMINI_API_INVALID_OUTPUT",
                    "generateContent returned no bounded text response",
                    phase="generate_content",
                )
            response_sha = hashlib.sha256(
                json.dumps(
                    generated,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
        except GeminiFileApiError as exc:
            if resource is not None:
                try:
                    selected.delete_file(
                        key=key,
                        name=str(resource["name"]),
                        timeout_seconds=timeout_seconds,
                    )
                    cleanup_status = "DELETED_AFTER_FAILURE"
                    http_request_count += 1
                except GeminiFileApiError:
                    cleanup_status = "DELETE_FAILED_EXPIRES_48H"
            exc.cleanup_status = cleanup_status
            raise
        try:
            selected.delete_file(
                key=key,
                name=str(resource["name"]),
                timeout_seconds=timeout_seconds,
            )
            cleanup_status = "DELETED"
            http_request_count += 1
        except GeminiFileApiError:
            cleanup_status = "DELETE_FAILED_EXPIRES_48H"
        return GeminiFileCallOutcome(
            text=text,
            evidence={
                "provider": "gemini_api",
                "model": model,
                "media_sha256": stable.sha256,
                "media_bytes": stable.size,
                "mime_type": mime_type,
                "video_fps": (
                    float(video_fps) if video_fps is not None else None
                ),
                "resource_name_sha256": hashlib.sha256(
                    str(resource["name"]).encode("utf-8")
                ).hexdigest(),
                "file_uri_sha256": hashlib.sha256(
                    str(resource["uri"]).encode("utf-8")
                ).hexdigest(),
                "active_state": resource["state"],
                "poll_count": poll_count,
                "http_request_count": http_request_count,
                "response_payload_sha256": response_sha,
                "response_text_sha256": hashlib.sha256(
                    text.encode("utf-8")
                ).hexdigest(),
                "cleanup_status": cleanup_status,
            },
        )


def run_free_key_file_call(
    *,
    media_path: Path,
    expected_sha256: str,
    expected_bytes: int,
    mime_type: str,
    prompt: str,
    model: str,
    video_fps: float | None,
    environment: Mapping[str, str],
    timeout_seconds: float,
    poll_timeout_seconds: float,
    transport_factory: Callable[[], GeminiFileTransport] = (
        UrllibGeminiFileTransport
    ),
    sleeper: Callable[[float], None] = time.sleep,
) -> GeminiFileCallOutcome:
    """Try configured free keys only; never read or spend the paid backup key."""

    keys = configured_free_keys(environment)
    if not keys:
        raise GeminiFileApiError(
            "GEMINI_API_KEYS_MISSING",
            "no configured free Gemini API key is available",
            phase="key_selection",
        )
    failures: list[dict[str, object]] = []
    last_error: GeminiFileApiError | None = None
    for ordinal, (name, key) in enumerate(keys, start=1):
        try:
            outcome = call_file_model(
                media_path=media_path,
                expected_sha256=expected_sha256,
                expected_bytes=expected_bytes,
                mime_type=mime_type,
                prompt=prompt,
                key=key,
                model=model,
                video_fps=video_fps,
                timeout_seconds=timeout_seconds,
                poll_timeout_seconds=poll_timeout_seconds,
                transport=transport_factory(),
                sleeper=sleeper,
            )
        except GeminiFileApiError as exc:
            last_error = exc
            row: dict[str, object] = {
                "key_tier": "free",
                "key_name": name,
                "key_ordinal": ordinal,
                "reason_code": exc.reason_code,
                "phase": exc.phase,
                "ambiguous": exc.ambiguous,
                "cleanup_status": exc.cleanup_status,
            }
            if exc.http_status is not None:
                row["http_status"] = exc.http_status
            failures.append(row)
            if exc.ambiguous or exc.http_status not in {401, 403, 429}:
                exc.failures = failures
                raise
            continue
        evidence = dict(outcome.evidence)
        evidence.update(
            {
                "key_tier": "free",
                "key_name": name,
                "key_ordinal": ordinal,
                "configured_free_key_count": len(keys),
                "prior_failures": failures,
                "paid_backup_used": False,
            }
        )
        return GeminiFileCallOutcome(text=outcome.text, evidence=evidence)
    assert last_error is not None
    last_error.failures = failures
    raise last_error


__all__ = [
    "FILES_API_BASE",
    "FILES_UPLOAD_URL",
    "FREE_KEY_ENV_NAMES",
    "GENERATE_URL",
    "GeminiFileApiError",
    "GeminiFileCallOutcome",
    "GeminiFileTransport",
    "UrllibGeminiFileTransport",
    "call_file_model",
    "configured_free_keys",
    "run_free_key_file_call",
]
