"""Strict experimental client for Volcengine recording-file lite ASR.

Only the officially documented ``recognize/flash`` route is implemented. A
short local crop is sent as ``audio.data`` base64, so no public object-storage
URL is created. The route is identified by its documented endpoint and
``volc.bigasr.auc_turbo`` resource ID; this module intentionally does not label
it as Seed-ASR or Doubao ASR 2.0 because that product/version mapping has not
been verified from an official API contract.

The returned provider-native text is ``EVIDENCE_ONLY``. It is not a subtitle
authority and is not wired into the normal MAI/MOSS evidence router.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from typing import Any, Callable, Mapping
import urllib.error
import urllib.request
import uuid


DOUBAO_FLASH_ENDPOINT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
DOUBAO_FLASH_RESOURCE = "volc.bigasr.auc_turbo"
DOUBAO_FLASH_MODEL = "volc-recording-file-lite-http"
DOUBAO_TIMEOUT_SECONDS = 300
MAX_FLASH_AUDIO_BYTES = 20_000_000
MAX_RESPONSE_BYTES = 20_000_000
MAX_API_KEY_BYTES = 16_384
ENCODER_TOLERANCE_MS = 500
MIN_AUDIO_DURATION_MS = 1_000

AudioInput = bytes | Path


class DoubaoTranscriptionError(RuntimeError):
    """A bounded, credential-free failure from a Doubao experimental route."""

    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.metadata = dict(metadata) if metadata is not None else None


def _error(
    reason_code: str,
    message: str,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> DoubaoTranscriptionError:
    return DoubaoTranscriptionError(reason_code, message, metadata=metadata)


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise _error("DOUBAO_VALUE_INVALID", "Doubao value is not canonical JSON") from None


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _read_owner_only_file(path: Path, *, max_bytes: int) -> bytes:
    """Read one owner-only regular file without following the final symlink."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise _error("DOUBAO_API_KEY_FILE_INVALID", "Doubao API key file cannot be read") from None
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or not 0 < before.st_size <= max_bytes
        ):
            raise _error(
                "DOUBAO_API_KEY_FILE_INVALID",
                "Doubao API key file must be one user-owned regular file",
            )
        if stat.S_IMODE(before.st_mode) & 0o077:
            raise _error(
                "DOUBAO_API_KEY_FILE_PERMISSIONS",
                "Doubao API key file is not owner-only",
            )
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(max_bytes + 1)
        after = os.fstat(fd)
        if (
            len(raw) > max_bytes
            or before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise _error("DOUBAO_API_KEY_FILE_INVALID", "Doubao API key file changed while read")
        return raw
    finally:
        os.close(fd)


def _read_api_key(environ: Mapping[str, str]) -> str:
    key_file = str(environ.get("AUTOSLICE_DOUBAO_API_KEY_FILE", "")).strip()
    if key_file:
        path = Path(key_file).expanduser()
        try:
            value = _read_owner_only_file(path, max_bytes=MAX_API_KEY_BYTES).decode("utf-8").strip()
        except DoubaoTranscriptionError:
            raise
        except UnicodeDecodeError:
            raise _error("DOUBAO_API_KEY_FILE_INVALID", "Doubao API key file is invalid") from None
    else:
        value = str(
            environ.get("AUTOSLICE_DOUBAO_API_KEY")
            or environ.get("VOLC_ASR_API_KEY")
            or ""
        ).strip()
    if not value:
        raise _error("DOUBAO_API_KEY_MISSING", "Doubao API key is not configured")
    if len(value.encode("utf-8")) > MAX_API_KEY_BYTES or any(
        ord(char) < 0x20 or ord(char) == 0x7F for char in value
    ):
        raise _error("DOUBAO_API_KEY_INVALID", "Doubao API key is invalid")
    return value

def doubao_api_key_status(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str | bool]:
    """Validate local credential configuration without exposing its value."""

    values = os.environ if environ is None else environ
    try:
        _read_api_key(values)
    except DoubaoTranscriptionError as exc:
        return {
            "configured": False,
            "source": "none",
            "reason_code": exc.reason_code,
        }
    source = (
        "owner_only_file"
        if str(values.get("AUTOSLICE_DOUBAO_API_KEY_FILE", "")).strip()
        else "environment"
    )
    return {"configured": True, "source": source, "reason_code": "DOUBAO_API_KEY_READY"}


def _validate_duration(duration_ms: int | None) -> None:
    if duration_ms is None:
        return
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
        raise _error("DOUBAO_DURATION_INVALID", "duration_ms must be a non-negative integer")
    if duration_ms < MIN_AUDIO_DURATION_MS:
        raise _error(
            "DOUBAO_AUDIO_TOO_SHORT",
            "Doubao recording-file ASR requires at least one second of audio",
        )


def _read_audio_path(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise _error("DOUBAO_AUDIO_INVALID", "Doubao audio cannot be read") from None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise _error("DOUBAO_AUDIO_INVALID", "Doubao audio path must be one regular file")
        if not 0 < before.st_size <= MAX_FLASH_AUDIO_BYTES:
            reason = (
                "DOUBAO_AUDIO_TOO_LARGE"
                if before.st_size > MAX_FLASH_AUDIO_BYTES
                else "DOUBAO_AUDIO_INVALID"
            )
            raise _error(reason, "Doubao audio file size is outside the local limit")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_FLASH_AUDIO_BYTES + 1)
        after = os.fstat(fd)
        if len(raw) > MAX_FLASH_AUDIO_BYTES:
            raise _error(
                "DOUBAO_AUDIO_TOO_LARGE",
                "Doubao direct-data evidence crop exceeds the local 20 MB limit",
            )
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or len(raw) != after.st_size
        ):
            raise _error("DOUBAO_AUDIO_CHANGED", "Doubao audio changed while read")
        return raw
    finally:
        os.close(fd)


def _read_audio(audio_input: AudioInput) -> tuple[bytes, str]:
    if isinstance(audio_input, bytes):
        audio = audio_input
        audio_format = "mp3"
    elif isinstance(audio_input, Path):
        suffix = audio_input.suffix.casefold().lstrip(".")
        if suffix not in {"mp3", "wav", "ogg"}:
            raise _error("DOUBAO_AUDIO_INVALID", "Doubao audio must be MP3, WAV, or OGG")
        audio = _read_audio_path(audio_input)
        audio_format = suffix
    else:
        raise _error("DOUBAO_AUDIO_INVALID", "Doubao audio must be bytes or a path")
    if not audio:
        raise _error("DOUBAO_AUDIO_INVALID", "Doubao audio is empty")
    if len(audio) > MAX_FLASH_AUDIO_BYTES:
        raise _error(
            "DOUBAO_AUDIO_TOO_LARGE",
            "Doubao direct-data evidence crop exceeds the local 20 MB limit",
        )
    return audio, audio_format

def _request_options() -> dict[str, Any]:
    # Deliberately verbatim-ish and candidate-blind: no semantic smoothing,
    # hotwords, replacement tables, prior transcript, image, or dialog context.
    return {
        "model_name": "bigmodel",
        "enable_itn": False,
        "enable_punc": True,
        "enable_ddc": False,
        "enable_speaker_info": False,
        "show_utterances": True,
        "enable_lid": False,
    }


def _headers(api_key: str, *, resource_id: str, request_id: str, sequence: bool) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": request_id,
    }
    if sequence:
        headers["X-Api-Sequence"] = "-1"
    return headers


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, new_url):  # noqa: N802
        raise urllib.error.HTTPError(request.full_url, code, "redirect disabled", headers, fp)


def _response_header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if value is not None:
            return str(value)
    getter = getattr(response, "getheader", None)
    if callable(getter):
        value = getter(name)
        if value is not None:
            return str(value)
    return None


def _safe_response_header(response: Any, name: str, *, api_key: str) -> str | None:
    value = _response_header(response, name)
    if value is None:
        return None
    if (
        len(value.encode("utf-8")) > 4_096
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        raise _error("DOUBAO_RESPONSE_HEADER_INVALID", "Doubao response header is invalid")
    if api_key in value:
        raise _error("DOUBAO_CREDENTIAL_ECHO", "Doubao response echoed the API key")
    return value


def _request_once(
    endpoint: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    api_key: str,
) -> tuple[int, dict[str, str | None], bytes]:
    body = _canonical(dict(payload))
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers=dict(headers),
    )
    response = None
    try:
        response = urllib.request.build_opener(_NoRedirect()).open(
            request,
            timeout=DOUBAO_TIMEOUT_SECONDS,
        )
        status = response.getcode()
        if isinstance(status, bool) or not isinstance(status, int):
            raise _error("DOUBAO_TRANSPORT_ERROR", "Doubao response status is invalid")
        if 300 <= status < 400:
            raise _error(
                "DOUBAO_REDIRECT",
                "Doubao endpoint returned a redirect",
                metadata={"http_status": status},
            )
        if status < 200 or status >= 300:
            raise _error(
                "DOUBAO_HTTP_ERROR",
                "Doubao endpoint returned an HTTP error",
                metadata={"http_status": status},
            )
        response_body = response.read(MAX_RESPONSE_BYTES + 1)
        if not isinstance(response_body, bytes) or len(response_body) > MAX_RESPONSE_BYTES:
            raise _error("DOUBAO_RESPONSE_INVALID", "Doubao response body is invalid")
        if api_key.encode("utf-8") in response_body:
            raise _error("DOUBAO_CREDENTIAL_ECHO", "Doubao response echoed the API key")
        response_headers = {
            "provider_status_code": _safe_response_header(
                response, "X-Api-Status-Code", api_key=api_key
            ),
            "provider_message": _safe_response_header(
                response, "X-Api-Message", api_key=api_key
            ),
            "log_id": _safe_response_header(response, "X-Tt-Logid", api_key=api_key),
        }
        return status, response_headers, response_body
    except DoubaoTranscriptionError:
        raise
    except urllib.error.HTTPError as exc:
        status = exc.code if type(exc.code) is int else None
        metadata = {"http_status": status} if status is not None else None
        if status is not None and 300 <= status < 400:
            raise _error("DOUBAO_REDIRECT", "Doubao endpoint returned a redirect", metadata=metadata) from None
        raise _error("DOUBAO_HTTP_ERROR", "Doubao endpoint returned an HTTP error", metadata=metadata) from None
    except TimeoutError:
        raise _error("DOUBAO_TIMEOUT", "Doubao request timed out") from None
    except (OSError, urllib.error.URLError):
        raise _error("DOUBAO_TRANSPORT_ERROR", "Doubao request failed") from None
    except Exception:
        raise _error("DOUBAO_TRANSPORT_ERROR", "Doubao request failed") from None
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _provider_status(headers: Mapping[str, str | None]) -> int:
    raw = headers.get("provider_status_code")
    try:
        status = int(str(raw))
    except (TypeError, ValueError):
        raise _error("DOUBAO_STATUS_MISSING", "Doubao status header is missing or invalid") from None
    return status


def _decode_json(body: bytes, *, metadata: Mapping[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _error("DOUBAO_RESPONSE_NOT_JSON", "Doubao response is not JSON", metadata=metadata) from None
    if not isinstance(payload, dict):
        raise _error("DOUBAO_RESPONSE_INVALID", "Doubao response is not an object", metadata=metadata)
    return payload


def _milliseconds(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _error("DOUBAO_SEGMENT_INVALID", f"Doubao {field} must be a non-negative integer")
    return value


def _parse_words(
    raw_words: object,
    *,
    utterance_start: int,
    utterance_end: int,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    if raw_words is None:
        return None, "DOUBAO_WORDS_MISSING"
    if not isinstance(raw_words, list):
        return None, "DOUBAO_WORDS_INVALID"
    words: list[dict[str, Any]] = []
    previous_start = -1
    for raw in raw_words:
        if not isinstance(raw, dict):
            return None, "DOUBAO_WORDS_INVALID"
        text = raw.get("text")
        if not isinstance(text, str) or not text.strip():
            return None, "DOUBAO_WORDS_INVALID"
        try:
            start = _milliseconds(raw.get("start_time"), "word start_time")
            end = _milliseconds(raw.get("end_time"), "word end_time")
        except DoubaoTranscriptionError:
            return None, "DOUBAO_WORDS_INVALID"
        # The official example contains a zero-duration final character. Keep
        # such native evidence, but never accept a reversed or unbounded word.
        if end < start or start < previous_start:
            return None, "DOUBAO_WORDS_INVALID"
        if start < max(0, utterance_start - ENCODER_TOLERANCE_MS) or end > (
            utterance_end + ENCODER_TOLERANCE_MS
        ):
            return None, "DOUBAO_WORDS_OUT_OF_BOUNDS"
        words.append({"text": text, "start_ms": start, "end_ms": end})
        previous_start = start
    return (words or None), None if words else "DOUBAO_WORDS_MISSING"


def _timeline_flags(segments: list[Mapping[str, Any]]) -> tuple[bool, bool]:
    out_of_order = any(
        current["start_ms"] < previous["start_ms"]
        for previous, current in zip(segments, segments[1:])
    )
    overlap = any(
        current["start_ms"] < previous["end_ms"]
        and current["end_ms"] > previous["start_ms"]
        for index, current in enumerate(segments)
        for previous in segments[:index]
    )
    return overlap, out_of_order


def _parse_evidence_payload(
    payload: Mapping[str, Any],
    *,
    duration_ms: int | None,
) -> dict[str, Any]:
    result = payload.get("result")
    if not isinstance(result, dict):
        raise _error("DOUBAO_RESPONSE_INVALID", "Doubao response has no result object")
    text = result.get("text")
    if not isinstance(text, str):
        raise _error("DOUBAO_RESPONSE_INVALID", "Doubao result text is invalid")
    raw_utterances = result.get("utterances", [])
    if raw_utterances is None:
        raw_utterances = []
    if not isinstance(raw_utterances, list):
        raise _error("DOUBAO_RESPONSE_INVALID", "Doubao utterances are invalid")

    audio_info = payload.get("audio_info")
    provider_duration_ms: int | None = None
    if isinstance(audio_info, dict) and audio_info.get("duration") is not None:
        provider_duration_ms = _milliseconds(audio_info.get("duration"), "audio_info.duration")
        if duration_ms is not None and provider_duration_ms > duration_ms + ENCODER_TOLERANCE_MS:
            raise _error("DOUBAO_DURATION_OUT_OF_BOUNDS", "Doubao duration exceeds input")

    segments: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_utterances, 1):
        if not isinstance(raw, dict):
            raise _error("DOUBAO_SEGMENT_INVALID", f"Doubao utterance {index} is invalid")
        utterance_text = raw.get("text")
        if not isinstance(utterance_text, str) or not utterance_text.strip():
            raise _error("DOUBAO_SEGMENT_INVALID", f"Doubao utterance {index} has empty text")
        start = _milliseconds(raw.get("start_time"), "utterance start_time")
        end = _milliseconds(raw.get("end_time"), "utterance end_time")
        if end <= start:
            raise _error("DOUBAO_SEGMENT_INVALID", f"Doubao utterance {index} has invalid range")
        if duration_ms is not None and end > duration_ms + ENCODER_TOLERANCE_MS:
            raise _error("DOUBAO_DURATION_OUT_OF_BOUNDS", "Doubao utterance exceeds input")
        words, words_reason = _parse_words(
            raw.get("words"),
            utterance_start=start,
            utterance_end=end,
        )
        row: dict[str, Any] = {
            "start_ms": start,
            "end_ms": end,
            "text": utterance_text,
            "speaker": None,
            "words": words,
            "words_available": words is not None,
        }
        if words_reason is not None:
            row["words_unavailable_reason"] = words_reason
            diagnostics.append({"utterance_index": index, "reason_code": words_reason})
        segments.append(row)

    overlap, out_of_order = _timeline_flags(segments)
    if overlap:
        diagnostics.append({"reason_code": "DOUBAO_NATIVE_OVERLAP"})
    if out_of_order:
        diagnostics.append({"reason_code": "DOUBAO_NATIVE_OUT_OF_ORDER"})
    compact_top = "".join(text.split())
    compact_segments = "".join("".join(row["text"].split()) for row in segments)
    if segments and compact_top and compact_top != compact_segments:
        diagnostics.append({"reason_code": "DOUBAO_TOP_TEXT_DIFFERS_FROM_UTTERANCES"})

    if segments:
        status = "OK"
    elif text.strip():
        status = "TEXT_UNLOCATED"
        diagnostics.append({"reason_code": "DOUBAO_TEXT_UNLOCATED"})
    else:
        status = "NO_SPEECH"
    one_track_srt_eligible = status == "OK" and not overlap and not out_of_order
    return {
        "status": status,
        "text": text,
        "provider_duration_ms": provider_duration_ms,
        "native_segments": segments,
        "segment_count": len(segments),
        "speaker_labels": [],
        "diagnostics": diagnostics,
        "native_timeline": {
            "has_overlap": overlap,
            "has_out_of_order": out_of_order,
            "one_track_srt_eligible": one_track_srt_eligible,
        },
        "one_track_srt_eligible": one_track_srt_eligible,
    }


def transcribe_doubao_flash_evidence(
    audio_input: AudioInput,
    *,
    duration_ms: int | None = None,
    before_request: Callable[[], None] | None = None,
    request_id: str | None = None,
    expected_audio_sha256: str | None = None,
) -> dict[str, Any]:
    """Send one short, candidate-blind local crop via official base64 input."""

    _validate_duration(duration_ms)
    if expected_audio_sha256 is not None and (
        not isinstance(expected_audio_sha256, str)
        or len(expected_audio_sha256) != 64
        or any(char not in "0123456789abcdef" for char in expected_audio_sha256)
    ):
        raise _error("DOUBAO_AUDIO_INVALID", "expected audio SHA-256 is invalid")
    audio, audio_format = _read_audio(audio_input)
    audio_sha256 = hashlib.sha256(audio).hexdigest()
    # Bind the actual immutable request bytes, not an earlier read of this path.
    # A changed file must stop before credential access or dispatch reservation.
    if expected_audio_sha256 is not None and audio_sha256 != expected_audio_sha256:
        raise _error("DOUBAO_AUDIO_CHANGED", "audio differs from the frozen request input")
    api_key = _read_api_key(os.environ)
    try:
        request_id = str(uuid.UUID(request_id)) if request_id is not None else str(uuid.uuid4())
    except (ValueError, AttributeError):
        raise _error("DOUBAO_REQUEST_ID_INVALID", "Doubao request ID is invalid") from None
    request_options = _request_options()
    payload = {
        "user": {"uid": "autoslice-evidence"},
        "audio": {
            "format": audio_format,
            "data": base64.b64encode(audio).decode("ascii"),
        },
        "request": request_options,
    }
    if before_request is not None:
        before_request()
    started = time.monotonic()
    http_status, response_headers, response_body = _request_once(
        DOUBAO_FLASH_ENDPOINT,
        payload,
        _headers(
            api_key,
            resource_id=DOUBAO_FLASH_RESOURCE,
            request_id=request_id,
            sequence=True,
        ),
        api_key,
    )
    provider_status = _provider_status(response_headers)
    metadata: dict[str, Any] = {
        "provider": "doubao_flash",
        "model": DOUBAO_FLASH_MODEL,
        "resource_id": DOUBAO_FLASH_RESOURCE,
        "input_audio_sha256": audio_sha256,
        "request_config_sha256": _digest(request_options),
        "response_sha256": hashlib.sha256(response_body).hexdigest(),
        "request_id": request_id,
        "log_id": response_headers.get("log_id"),
        "http_status": http_status,
        "provider_status_code": provider_status,
        "elapsed_seconds": max(0.0, time.monotonic() - started),
        "candidate_exposure": "none",
        "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False,
    }
    if provider_status == 20000003:
        return {
            **metadata,
            "status": "NO_SPEECH",
            "text": "",
            "native_segments": [],
            "segment_count": 0,
            "diagnostics": [{"reason_code": "DOUBAO_NO_SPEECH"}],
            "native_timeline": {
                "has_overlap": False,
                "has_out_of_order": False,
                "one_track_srt_eligible": False,
            },
            "one_track_srt_eligible": False,
        }
    if provider_status != 20000000:
        raise _error(
            "DOUBAO_PROVIDER_ERROR",
            "Doubao provider rejected the flash request",
            metadata={**metadata, "provider_message": response_headers.get("provider_message")},
        )
    payload_json = _decode_json(response_body, metadata=metadata)
    parsed = _parse_evidence_payload(payload_json, duration_ms=duration_ms)
    return {**metadata, **parsed, "raw_response": payload_json}


__all__ = [
    "DOUBAO_FLASH_ENDPOINT",
    "DOUBAO_FLASH_MODEL",
    "DOUBAO_FLASH_RESOURCE",
    "DOUBAO_TIMEOUT_SECONDS",
    "DoubaoTranscriptionError",
    "doubao_api_key_status",
    "transcribe_doubao_flash_evidence",
]
