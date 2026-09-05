"""Small, strict MOSS Pro client for draft talk transcription.

The returned SRT is a draft only.  Downstream CPA remains responsible for
semantic correction and acceptance.
"""

from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import stat
import time
import urllib.error
import urllib.request
from typing import Any, Mapping

MOSS_ENDPOINT = "https://api.mosi.cn/v1/audio/transcriptions"
MOSS_MODEL = "moss-transcribe-diarize-pro"
MOSS_TIMEOUT_SECONDS = 600
ENCODER_TOLERANCE_MS = 500
AudioInput = bytes | Path


class MossTranscriptionError(RuntimeError):
    """A bounded, sanitized failure from the MOSS draft lane."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def _error(reason_code: str, message: str) -> MossTranscriptionError:
    return MossTranscriptionError(reason_code, message)


def _read_api_key(environ: Mapping[str, str]) -> str:
    key_file = str(environ.get("AUTOSLICE_MOSS_API_KEY_FILE", "")).strip()
    if key_file:
        path = Path(key_file).expanduser()
        try:
            info = path.lstat()
        except OSError:
            raise _error("MOSS_API_KEY_FILE_INVALID", "MOSS API key file cannot be read") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise _error("MOSS_API_KEY_FILE_INVALID", "MOSS API key file must be regular")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise _error("MOSS_API_KEY_FILE_PERMISSIONS", "MOSS API key file is not owner-only")
        try:
            value = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            raise _error("MOSS_API_KEY_FILE_INVALID", "MOSS API key file cannot be decoded") from None
    else:
        value = str(environ.get("AUTOSLICE_MOSS_API_KEY", "")).strip()

    if not value:
        raise _error("MOSS_API_KEY_MISSING", "MOSS API key is not configured")
    if any(ord(char) < 0x20 for char in value):
        raise _error("MOSS_API_KEY_INVALID", "MOSS API key is invalid")
    return value


def _read_audio(audio_input: AudioInput) -> bytes:
    if isinstance(audio_input, bytes):
        if not audio_input:
            raise _error("MOSS_AUDIO_INVALID", "MOSS audio input is empty")
        return audio_input
    if not isinstance(audio_input, Path):
        raise _error("MOSS_AUDIO_INVALID", "MOSS audio input must be bytes or a path")
    try:
        audio = audio_input.read_bytes()
    except OSError:
        raise _error("MOSS_AUDIO_INVALID", "MOSS audio input cannot be read") from None
    if not audio:
        raise _error("MOSS_AUDIO_INVALID", "MOSS audio input is empty")
    return audio


def _milliseconds(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _error("MOSS_SEGMENT_INVALID", f"MOSS {field} must be a non-negative integer")
    return value


def _seconds_to_milliseconds(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error("MOSS_SEGMENT_INVALID", f"MOSS {field} must be numeric seconds")
    try:
        seconds = float(value)
    except (OverflowError, ValueError):
        raise _error("MOSS_SEGMENT_INVALID", f"MOSS {field} is invalid") from None
    if not math.isfinite(seconds) or seconds < 0:
        raise _error("MOSS_SEGMENT_INVALID", f"MOSS {field} is invalid")
    try:
        return int(round(seconds * 1000))
    except OverflowError:
        raise _error("MOSS_SEGMENT_INVALID", f"MOSS {field} is out of range") from None


def _segment_time(segment: Mapping[str, Any], seconds_field: str, milliseconds_field: str) -> int:
    has_seconds = seconds_field in segment
    has_milliseconds = milliseconds_field in segment
    if has_seconds and has_milliseconds:
        raise _error("MOSS_SEGMENT_INVALID", "MOSS segment has ambiguous timestamp fields")
    if has_milliseconds:
        return _milliseconds(segment[milliseconds_field], milliseconds_field)
    if has_seconds:
        return _seconds_to_milliseconds(segment[seconds_field], seconds_field)
    raise _error("MOSS_SEGMENT_INVALID", "MOSS segment is missing timestamps")


_ANONYMOUS_SPEAKER = re.compile(r"^(?:s|speaker|spk|cluster)[ _-]?0*(\d+)$", re.IGNORECASE)


def _anonymous_speaker(value: Any, mapping: dict[str, str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _error("MOSS_SEGMENT_INVALID", "MOSS speaker label is invalid")
    token = value.strip()
    match = _ANONYMOUS_SPEAKER.fullmatch(token)
    if match:
        return f"S{int(match.group(1)):02d}"
    # Provider labels are not treated as identities; arbitrary labels become
    # stable anonymous clusters for metadata only.
    if token not in mapping:
        mapping[token] = f"SPEAKER_{len(mapping):02d}"
    return mapping[token]


def _parse_segments(payload: Any, duration_ms: int | None) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
        raise _error("MOSS_RESPONSE_INVALID", "MOSS response has no segment list")
    raw_segments = payload["segments"]
    if not raw_segments:
        raise _error("MOSS_EMPTY_RESULT", "MOSS response contains no segments")

    parsed: list[dict[str, Any]] = []
    speaker_mapping: dict[str, str] = {}
    speaker_labels: list[str] = []
    for index, raw in enumerate(raw_segments, 1):
        if not isinstance(raw, dict):
            raise _error("MOSS_SEGMENT_INVALID", f"MOSS segment {index} is not an object")
        text = raw.get("text")
        if not isinstance(text, str) or not text.strip():
            raise _error("MOSS_SEGMENT_INVALID", f"MOSS segment {index} has empty text")
        start_ms = _segment_time(raw, "start", "start_ms")
        end_ms = _segment_time(raw, "end", "end_ms")
        if end_ms <= start_ms:
            raise _error("MOSS_SEGMENT_INVALID", f"MOSS segment {index} has invalid range")
        if parsed:
            previous = parsed[-1]
            if start_ms < previous["start_ms"]:
                raise _error("MOSS_SEGMENTS_OUT_OF_ORDER", "MOSS segments are out of order")
            if start_ms < previous["end_ms"]:
                raise _error("MOSS_OVERLAP", "MOSS segments overlap")
        if duration_ms is not None and (
            start_ms > duration_ms + ENCODER_TOLERANCE_MS
            or end_ms > duration_ms + ENCODER_TOLERANCE_MS
        ):
            raise _error("MOSS_DURATION_OUT_OF_BOUNDS", "MOSS segment exceeds input duration")

        speaker = _anonymous_speaker(raw.get("speaker"), speaker_mapping)
        if speaker is not None and speaker not in speaker_labels:
            speaker_labels.append(speaker)
        parsed.append({"start_ms": start_ms, "end_ms": end_ms, "text": text, "speaker": speaker})
    return parsed, speaker_labels


def _timestamp(value_ms: int) -> str:
    hours, remainder = divmod(value_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def _render_srt(segments: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, segment in enumerate(segments, 1):
        lines.extend(
            [
                str(index),
                f"{_timestamp(segment['start_ms'])} --> {_timestamp(segment['end_ms'])}",
                segment["text"],
                "",
            ]
        )
    return "\n".join(lines)


def _upload_details(audio_input: AudioInput) -> tuple[str, str]:
    name = audio_input.name if isinstance(audio_input, Path) else "audio.mp3"
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return name, mime


def _multipart_body(audio: bytes, filename: str, mime: str) -> tuple[bytes, str]:
    boundary = "----autoslice-moss"
    chunks = [
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'
        ).encode(),
        audio,
        b"\r\n",
    ]
    for name, value in (
        ("model", MOSS_MODEL),
        ("diarize", "true"),
        ("response_format", "diarized_json"),
    ):
        chunks.extend(
            [
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, new_url):  # noqa: N802
        raise urllib.error.HTTPError(request.full_url, code, "redirect disabled", headers, fp)


def _request_once(audio_input: AudioInput, audio: bytes, api_key: str) -> tuple[int, bytes]:
    filename, mime = _upload_details(audio_input)
    body, boundary = _multipart_body(audio, filename, mime)
    request = urllib.request.Request(
        MOSS_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    response = None
    try:
        response = urllib.request.build_opener(_NoRedirect()).open(
            request,
            timeout=MOSS_TIMEOUT_SECONDS,
        )
        status = response.getcode()
        if not isinstance(status, int) or isinstance(status, bool):
            raise _error("MOSS_TRANSPORT_ERROR", "MOSS response status is invalid")
        if 300 <= status < 400:
            raise _error("MOSS_REDIRECT", "MOSS endpoint returned a redirect")
        if status < 200 or status >= 300:
            raise _error("MOSS_HTTP_ERROR", "MOSS endpoint returned an HTTP error")
        response_body = response.read()
        if not isinstance(response_body, bytes):
            raise _error("MOSS_RESPONSE_INVALID", "MOSS response body is invalid")
        return status, response_body
    except MossTranscriptionError:
        raise
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise _error("MOSS_REDIRECT", "MOSS endpoint returned a redirect") from None
        raise _error("MOSS_HTTP_ERROR", "MOSS endpoint returned an HTTP error") from None
    except TimeoutError:
        raise _error("MOSS_TIMEOUT", "MOSS request timed out") from None
    except (OSError, urllib.error.URLError):
        raise _error("MOSS_TRANSPORT_ERROR", "MOSS request failed") from None
    except Exception:
        raise _error("MOSS_TRANSPORT_ERROR", "MOSS request failed") from None
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def transcribe_moss(
    audio_path: AudioInput,
    *,
    duration_ms: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Transcribe one audio file with MOSS Pro and return native-timeline SRT."""

    if duration_ms is not None and (
        isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0
    ):
        raise _error("MOSS_DURATION_INVALID", "duration_ms must be a non-negative integer")
    audio = _read_audio(audio_path)
    input_audio_sha256 = hashlib.sha256(audio).hexdigest()
    api_key = _read_api_key(os.environ)
    started = time.monotonic()
    _, response_body = _request_once(audio_path, audio, api_key)
    response_sha256 = hashlib.sha256(response_body).hexdigest()
    try:
        payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _error("MOSS_RESPONSE_NOT_JSON", "MOSS response is not JSON") from None
    segments, speaker_labels = _parse_segments(payload, duration_ms)
    metadata: dict[str, Any] = {
        "provider": "moss",
        "model": MOSS_MODEL,
        "input_audio_sha256": input_audio_sha256,
        "response_sha256": response_sha256,
        "elapsed_seconds": max(0.0, time.monotonic() - started),
        "segment_count": len(segments),
        "speaker_labels": speaker_labels,
        "raw_response": payload,
    }
    return _render_srt(segments), metadata


__all__ = ["MOSS_ENDPOINT", "MOSS_MODEL", "MOSS_TIMEOUT_SECONDS", "MossTranscriptionError", "transcribe_moss"]
