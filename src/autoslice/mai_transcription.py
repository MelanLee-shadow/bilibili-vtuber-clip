"""Small, strict Azure MAI-Transcribe-2 draft client.

The response is kept as native evidence in the returned metadata.  This
module does not identify speakers or make a release/accuracy decision; the
caller remains responsible for downstream CPA and acceptance.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping


MAI_MODEL = "MAI-Transcribe-2"
MAI_API_VERSION = "2025-10-15"
MAI_TIMEOUT_SECONDS = 300
MAX_AUDIO_BYTES = 300_000_000
ENCODER_TOLERANCE_MS = 500
MAX_UNWORD_SEGMENT_CHARS = 32
MAX_UNWORD_SEGMENT_DURATION_MS = 8_000
MAX_SPLIT_CHARS = 48
MAX_SPLIT_DURATION_MS = 10_000

_HOST_SUFFIXES = (".services.ai.azure.com", ".cognitiveservices.azure.com")
_PROJECT_PATH = re.compile(r"^/api/projects/[^/]+$")
_SENTENCE_FINAL = frozenset("。！？!?；;…")
_MAI_DEFINITION: dict[str, Any] = {
    "enhancedMode": {
        "enabled": True,
        "model": MAI_MODEL,
        "modelOptions": {"transcribeStyle": "verbatim", "timestamps": "word"},
    },
    "diarization": {"enabled": True},
    "profanityFilterMode": "None",
}

AudioInput = bytes | Path


class MaiTranscriptionError(RuntimeError):
    """A bounded, sanitized failure from the MAI draft lane."""

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
) -> MaiTranscriptionError:
    return MaiTranscriptionError(reason_code, message, metadata=metadata)


def _read_api_key(environ: Mapping[str, str]) -> str:
    key_file = str(environ.get("AUTOSLICE_MAI_API_KEY_FILE", "")).strip()
    if key_file:
        path = Path(key_file).expanduser()
        try:
            info = path.lstat()
        except OSError:
            raise _error(
                "MAI_API_KEY_FILE_INVALID", "MAI API key file cannot be read"
            ) from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise _error(
                "MAI_API_KEY_FILE_INVALID", "MAI API key file must be regular"
            )
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise _error(
                "MAI_API_KEY_FILE_PERMISSIONS", "MAI API key file is not owner-only"
            )
        try:
            value = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            raise _error(
                "MAI_API_KEY_FILE_INVALID", "MAI API key file cannot be decoded"
            ) from None
    else:
        value = str(environ.get("AZURE_API_KEY", "")).strip()

    if not value:
        raise _error("MAI_API_KEY_MISSING", "MAI API key is not configured")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise _error("MAI_API_KEY_INVALID", "MAI API key is invalid")
    return value


def _read_audio(audio_input: AudioInput) -> tuple[bytes, str, str]:
    if isinstance(audio_input, bytes):
        if not audio_input:
            raise _error("MAI_AUDIO_INVALID", "MAI audio input is empty")
        if len(audio_input) >= MAX_AUDIO_BYTES:
            raise _error("MAI_AUDIO_TOO_LARGE", "MAI audio input is too large")
        return audio_input, "audio.mp3", "audio/mpeg"

    if not isinstance(audio_input, Path):
        raise _error("MAI_AUDIO_INVALID", "MAI audio input must be bytes or a path")
    suffix = audio_input.suffix.lower()
    mime_by_suffix = {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
    }
    mime = mime_by_suffix.get(suffix)
    if mime is None:
        raise _error("MAI_AUDIO_INVALID", "MAI audio path must be WAV, MP3, or FLAC")
    try:
        if audio_input.stat().st_size >= MAX_AUDIO_BYTES:
            raise _error("MAI_AUDIO_TOO_LARGE", "MAI audio input is too large")
        audio = audio_input.read_bytes()
    except MaiTranscriptionError:
        raise
    except OSError:
        raise _error("MAI_AUDIO_INVALID", "MAI audio input cannot be read") from None
    if not audio:
        raise _error("MAI_AUDIO_INVALID", "MAI audio input is empty")
    if len(audio) >= MAX_AUDIO_BYTES:
        raise _error("MAI_AUDIO_TOO_LARGE", "MAI audio input is too large")
    filename = audio_input.name.replace("\r", "_").replace("\n", "_").replace('"', "_")
    if not filename:
        filename = "audio" + suffix
    return audio, filename.replace("\\", "_"), mime


def _endpoint_url(environ: Mapping[str, str]) -> str:
    value = str(environ.get("AZURE_ENDPOINT", "")).strip()
    if not value or any(char in value for char in "\r\n"):
        raise _error("MAI_ENDPOINT_INVALID", "Azure endpoint is invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise _error("MAI_ENDPOINT_INVALID", "Azure endpoint is invalid") from None
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or ":" in parsed.netloc
        or not host
        or parsed.query
        or parsed.fragment
    ):
        raise _error("MAI_ENDPOINT_INVALID", "Azure endpoint is invalid")
    host = host.lower()
    if not any(host.endswith(suffix) and host[: -len(suffix)] for suffix in _HOST_SUFFIXES):
        raise _error("MAI_ENDPOINT_INVALID", "Azure endpoint is not an Azure Speech resource")
    if parsed.path not in ("", "/") and not _PROJECT_PATH.fullmatch(parsed.path):
        raise _error("MAI_ENDPOINT_INVALID", "Azure endpoint path is invalid")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in parsed.path):
        raise _error("MAI_ENDPOINT_INVALID", "Azure endpoint path is invalid")
    return (
        f"https://{host}/speechtotext/transcriptions:transcribe"
        f"?api-version={MAI_API_VERSION}"
    )


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


def _has_overlap(segments: list[Mapping[str, Any]]) -> bool:
    previous_end = -1
    for segment in segments:
        if segment["start_ms"] < previous_end:
            return True
        previous_end = max(previous_end, segment["end_ms"])
    return False


def _timeline_flags(segments: list[Mapping[str, Any]]) -> tuple[bool, bool]:
    has_out_of_order = any(
        current["start_ms"] < previous["start_ms"]
        for previous, current in zip(segments, segments[1:])
    )
    has_overlap = any(
        current["start_ms"] < previous["end_ms"]
        and current["end_ms"] > previous["start_ms"]
        for index, current in enumerate(segments)
        for previous in segments[:index]
    )
    return has_overlap, has_out_of_order


def _milliseconds(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error("MAI_TIME_INVALID", f"MAI {field} must be an integer")
    if value < 0:
        raise _error("MAI_TIME_INVALID", f"MAI {field} must be non-negative")
    return value


def _anonymous_speaker(value: Any, mapping: dict[int, str], labels: list[str]) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _error("MAI_SPEAKER_INVALID", "MAI speaker label is invalid")
    if value not in mapping:
        label = f"S{value:02d}"
        mapping[value] = label
        labels.append(label)
    return mapping[value]


def _compact(value: str) -> str:
    return "".join(value.split())


def _word_chunk(text: str, compact_positions: list[int], start: int, end: int, previous_end: int | None) -> tuple[str, int]:
    first = compact_positions[start]
    raw_start = first if previous_end is None else previous_end
    raw_end = compact_positions[end - 1] + 1
    return text[raw_start:raw_end], raw_end


def _has_sentence_final(text: str) -> bool:
    return any(char in _SENTENCE_FINAL for char in text)


def _split_worded_phrase(phrase: Mapping[str, Any]) -> list[dict[str, Any]]:
    words = phrase["words"]
    text = phrase["text"]
    compact_positions = [index for index, char in enumerate(text) if not char.isspace()]
    if not compact_positions:
        raise _error("MAI_PHRASE_INVALID", "MAI phrase text is empty")

    groups: list[tuple[int, int]] = []
    start = 0
    while start < len(words):
        chars = 0
        end = start
        sentence_end: int | None = None
        first_start = words[start]["start_ms"]
        while end < len(words):
            word = words[end]
            next_chars = chars + len(_compact(word["text"]))
            next_elapsed = word["end_ms"] - first_start
            if end > start and (
                next_chars > MAX_SPLIT_CHARS or next_elapsed > MAX_SPLIT_DURATION_MS
            ):
                if sentence_end is not None:
                    end = sentence_end
                break
            chars = next_chars
            end += 1
            if _has_sentence_final(word["text"]):
                sentence_end = end
            if (
                chars >= MAX_UNWORD_SEGMENT_CHARS
                or next_elapsed >= MAX_UNWORD_SEGMENT_DURATION_MS
            ) and sentence_end is not None:
                end = sentence_end
                break
        if end <= start:
            end = start + 1
        groups.append((start, end))
        start = end

    segments: list[dict[str, Any]] = []
    previous_raw_end: int | None = None
    for start, end in groups:
        segment_text, previous_raw_end = _word_chunk(
            text, compact_positions, words[start]["compact_start"], words[end - 1]["compact_end"], previous_raw_end
        )
        if not segment_text.strip():
            raise _error("MAI_PHRASE_INVALID", "MAI phrase produced an empty segment")
        segments.append(
            {
                "start_ms": words[start]["start_ms"],
                "end_ms": words[end - 1]["end_ms"],
                "text": segment_text,
                "speaker": phrase["speaker"],
            }
        )
    return segments


def _parse_response(
    payload: Any,
    *,
    duration_ms: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], int]:
    if not isinstance(payload, dict):
        raise _error("MAI_RESPONSE_INVALID", "MAI response is not an object")
    response_duration = _milliseconds(payload.get("durationMilliseconds"), "durationMilliseconds")
    if duration_ms is not None and response_duration > duration_ms + ENCODER_TOLERANCE_MS:
        raise _error("MAI_DURATION_OUT_OF_BOUNDS", "MAI response exceeds input duration")
    phrases = payload.get("phrases")
    if not isinstance(phrases, list):
        raise _error("MAI_RESPONSE_INVALID", "MAI response has no phrase list")
    combined_text: str | None = None
    combined = payload.get("combinedPhrases")
    if combined is not None:
        if not isinstance(combined, list) or not combined or any(
            not isinstance(item, dict)
            or not isinstance(item.get("text"), str)
            for item in combined
        ):
            raise _error("MAI_RESPONSE_INVALID", "MAI combined phrases are invalid")
        combined_text = "".join(item["text"] for item in combined)
    if not phrases:
        raise _error("MAI_EMPTY_RESULT", "MAI response contains no phrases")

    parsed: list[dict[str, Any]] = []
    native: list[dict[str, Any]] = []
    phrase_texts: list[str] = []
    speaker_mapping: dict[int, str] = {}
    speaker_labels: list[str] = []
    previous_start = -1
    for index, raw in enumerate(phrases, 1):
        shell = _phrase_shell(
            raw,
            index=index,
            response_duration=response_duration,
            duration_ms=duration_ms,
            speaker_mapping=speaker_mapping,
            speaker_labels=speaker_labels,
        )
        text = shell["text"]
        phrase_texts.append(text)
        start_ms = shell["start_ms"]
        end_ms = shell["end_ms"]
        phrase_duration = end_ms - start_ms
        if start_ms < previous_start:
            raise _error("MAI_SEGMENTS_OUT_OF_ORDER", "MAI phrases are out of order")
        previous_start = start_ms
        speaker = shell["speaker"]

        raw_words = raw.get("words", [])
        if raw_words is None:
            raw_words = []
        if not isinstance(raw_words, list):
            raise _error("MAI_WORD_INVALID", f"MAI phrase {index} words are invalid")
        words: list[dict[str, Any]] = []
        previous_word_start = -1
        previous_word_end = -1
        compact_count = 0
        for word_index, raw_word in enumerate(raw_words, 1):
            if not isinstance(raw_word, dict):
                raise _error("MAI_WORD_INVALID", f"MAI word {word_index} is not an object")
            word_text = raw_word.get("text")
            if not isinstance(word_text, str) or not word_text.strip():
                raise _error("MAI_WORD_INVALID", f"MAI word {word_index} has empty text")
            word_start = _milliseconds(raw_word.get("offsetMilliseconds"), "word offsetMilliseconds")
            word_duration = _milliseconds(
                raw_word.get("durationMilliseconds"), "word durationMilliseconds"
            )
            if word_duration <= 0:
                raise _error("MAI_TIME_INVALID", "MAI word duration must be positive")
            word_end = word_start + word_duration
            if word_start < start_ms or word_end > end_ms + 1:
                raise _error("MAI_DURATION_OUT_OF_BOUNDS", "MAI word exceeds phrase duration")
            if word_start < previous_word_start or word_start < previous_word_end:
                raise _error("MAI_WORDS_OUT_OF_ORDER", "MAI words overlap or are out of order")
            compact_start = compact_count
            compact_count += len(_compact(word_text))
            words.append(
                {
                    "text": word_text,
                    "start_ms": word_start,
                    "end_ms": word_end,
                    "compact_start": compact_start,
                    "compact_end": compact_count,
                }
            )
            previous_word_start = word_start
            previous_word_end = word_end

        compact_text = _compact(text)
        if words and "".join(_compact(word["text"]) for word in words) != compact_text:
            raise _error("MAI_WORD_TEXT_MISMATCH", f"MAI phrase {index} words do not match text")
        if not words and (
            len(compact_text) > MAX_UNWORD_SEGMENT_CHARS
            or phrase_duration > MAX_UNWORD_SEGMENT_DURATION_MS
        ):
            raise _error("MAI_WORDS_MISSING", "MAI long phrase has no word timestamps")

        native_words = [
            {"text": word["text"], "start_ms": word["start_ms"], "end_ms": word["end_ms"]}
            for word in words
        ]
        native_row = {
            "start_ms": start_ms,
            "end_ms": end_ms,
            "text": text,
            "speaker": speaker,
            "words": native_words,
        }
        native.append(native_row)
        if words:
            parsed.extend(_split_worded_phrase({**native_row, "words": words}))
        else:
            parsed.append({"start_ms": start_ms, "end_ms": end_ms, "text": text, "speaker": speaker})

    if combined_text is not None and _compact(combined_text) != _compact("".join(phrase_texts)):
        raise _error("MAI_COMBINED_TEXT_MISMATCH", "MAI combined text does not match phrases")

    return parsed, native, speaker_labels, response_duration


_WORD_EVIDENCE_REASONS = frozenset(
    {
        "MAI_WORDS_MISSING",
        "MAI_WORD_INVALID",
        "MAI_TIME_INVALID",
        "MAI_DURATION_OUT_OF_BOUNDS",
        "MAI_WORDS_OUT_OF_ORDER",
        "MAI_WORD_TEXT_MISMATCH",
    }
)


def _phrase_shell(
    raw: Any,
    *,
    index: int,
    response_duration: int,
    duration_ms: int | None,
    speaker_mapping: dict[int, str],
    speaker_labels: list[str],
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _error("MAI_PHRASE_INVALID", f"MAI phrase {index} is not an object")
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        raise _error("MAI_PHRASE_INVALID", f"MAI phrase {index} has empty text")
    start_ms = _milliseconds(raw.get("offsetMilliseconds"), "phrase offsetMilliseconds")
    phrase_duration = _milliseconds(raw.get("durationMilliseconds"), "phrase durationMilliseconds")
    if phrase_duration <= 0:
        raise _error("MAI_TIME_INVALID", "MAI phrase duration must be positive")
    end_ms = start_ms + phrase_duration
    # Native evidence can retain a bounded encoder tail, but may not invent a
    # shorter timestamp. The physical input duration must independently bind it.
    bounded_encoder_tail = (
        duration_ms is not None
        and abs(response_duration - duration_ms) <= ENCODER_TOLERANCE_MS
        and end_ms <= min(response_duration, duration_ms) + ENCODER_TOLERANCE_MS
    )
    if end_ms > response_duration and not bounded_encoder_tail:
        raise _error("MAI_DURATION_OUT_OF_BOUNDS", "MAI phrase exceeds response duration")
    if duration_ms is not None and end_ms > duration_ms + ENCODER_TOLERANCE_MS:
        raise _error("MAI_DURATION_OUT_OF_BOUNDS", "MAI phrase exceeds input duration")
    speaker = _anonymous_speaker(raw.get("speaker"), speaker_mapping, speaker_labels)
    return {"start_ms": start_ms, "end_ms": end_ms, "text": text, "speaker": speaker}


def _parse_evidence_response(
    payload: Any,
    *,
    duration_ms: int | None,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise _error("MAI_RESPONSE_INVALID", "MAI response is not an object")
    response_duration = _milliseconds(payload.get("durationMilliseconds"), "durationMilliseconds")
    if duration_ms is not None and response_duration > duration_ms + ENCODER_TOLERANCE_MS:
        raise _error("MAI_DURATION_OUT_OF_BOUNDS", "MAI response exceeds input duration")
    phrases = payload.get("phrases")
    if not isinstance(phrases, list):
        raise _error("MAI_RESPONSE_INVALID", "MAI response has no phrase list")
    combined = payload.get("combinedPhrases")
    combined_text: str | None = None
    if combined is not None:
        if not isinstance(combined, list) or not combined or any(
            not isinstance(item, dict) or not isinstance(item.get("text"), str)
            for item in combined
        ):
            raise _error("MAI_RESPONSE_INVALID", "MAI combined phrases are invalid")
        combined_text = "".join(item["text"] for item in combined)

    speaker_mapping: dict[int, str] = {}
    speaker_labels: list[str] = []
    native_segments: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    phrase_texts: list[str] = []
    for index, raw in enumerate(phrases, 1):
        shell = _phrase_shell(
            raw,
            index=index,
            response_duration=response_duration,
            duration_ms=duration_ms,
            speaker_mapping=speaker_mapping,
            speaker_labels=speaker_labels,
        )
        phrase_texts.append(shell["text"])
        overhang = max(0, shell["end_ms"] - response_duration)
        if overhang:
            diagnostics.append({
                "phrase_index": index,
                "reason_code": "MAI_NATIVE_ENCODER_TAIL_OVERHANG",
                "overhang_ms": overhang,
                "declared_duration_ms": response_duration,
                "physical_input_duration_ms": duration_ms,
                "native_end_ms": shell["end_ms"],
                "native_timestamps_preserved": True,
            })
        synthetic = {
            # A local word-validation envelope, not a rewritten provider reply.
            "durationMilliseconds": max(response_duration, shell["end_ms"]),
            "combinedPhrases": [{"text": shell["text"]}],
            "phrases": [raw],
        }
        words_reason: str | None = None
        words: list[dict[str, Any]] | None = None
        try:
            _, parsed_native, _, _ = _parse_response(synthetic, duration_ms=None)
            candidate_words = parsed_native[0]["words"]
            if candidate_words:
                words = candidate_words
            else:
                words_reason = "MAI_WORDS_MISSING"
        except MaiTranscriptionError as exc:
            if exc.reason_code not in _WORD_EVIDENCE_REASONS:
                raise
            words_reason = exc.reason_code
        if words_reason is not None:
            diagnostics.append({"phrase_index": index, "reason_code": words_reason})
        native_row = {
            **shell,
            "words": words,
            "words_available": words is not None,
        }
        if words_reason is not None:
            native_row["words_unavailable_reason"] = words_reason
        native_segments.append(native_row)

    if combined_text is not None and _compact(combined_text) != _compact("".join(phrase_texts)):
        raise _error("MAI_COMBINED_TEXT_MISMATCH", "MAI combined text does not match phrases")
    return native_segments, speaker_labels, diagnostics


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, new_url):  # noqa: N802
        raise urllib.error.HTTPError(request.full_url, code, "redirect disabled", headers, fp)


def _multipart_body(audio: bytes, filename: str, mime: str) -> tuple[bytes, str]:
    boundary = "----autoslice-mai-transcribe"
    definition = json.dumps(_MAI_DEFINITION, ensure_ascii=False, separators=(",", ":"))
    chunks = [
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; '
            f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'
        ).encode("utf-8"),
        audio,
        b"\r\n",
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="definition"\r\n\r\n'
        ).encode("utf-8"),
        definition.encode("utf-8"),
        b"\r\n",
        f"--{boundary}--\r\n".encode("ascii"),
    ]
    return b"".join(chunks), boundary


def _request_once(
    audio: bytes, filename: str, mime: str, endpoint: str, api_key: str
) -> tuple[int, bytes]:
    body, boundary = _multipart_body(audio, filename, mime)
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Ocp-Apim-Subscription-Key": api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    response = None
    try:
        response = urllib.request.build_opener(_NoRedirect()).open(
            request,
            timeout=MAI_TIMEOUT_SECONDS,
        )
        status = response.getcode()
        if isinstance(status, bool) or not isinstance(status, int):
            raise _error("MAI_TRANSPORT_ERROR", "MAI response status is invalid")
        if 300 <= status < 400:
            raise _error(
                "MAI_REDIRECT", "MAI endpoint returned a redirect",
                metadata={"http_status": status},
            )
        if status < 200 or status >= 300:
            raise _error(
                "MAI_HTTP_ERROR", "MAI endpoint returned an HTTP error",
                metadata={"http_status": status},
            )
        response_body = response.read()
        if not isinstance(response_body, bytes):
            raise _error("MAI_RESPONSE_INVALID", "MAI response body is invalid")
        if api_key.encode("utf-8") in response_body:
            raise _error("MAI_CREDENTIAL_ECHO", "MAI response echoed the API key")
        return status, response_body
    except MaiTranscriptionError:
        raise
    except urllib.error.HTTPError as exc:
        status = exc.code if type(exc.code) is int else None
        metadata = {"http_status": status} if status is not None else None
        if status is not None and 300 <= status < 400:
            raise _error(
                "MAI_REDIRECT", "MAI endpoint returned a redirect",
                metadata=metadata,
            ) from None
        raise _error(
            "MAI_HTTP_ERROR", "MAI endpoint returned an HTTP error",
            metadata=metadata,
        ) from None
    except TimeoutError:
        raise _error("MAI_TIMEOUT", "MAI request timed out") from None
    except urllib.error.URLError:
        raise _error("MAI_TRANSPORT_ERROR", "MAI request failed") from None
    except OSError:
        raise _error("MAI_TRANSPORT_ERROR", "MAI request failed") from None
    except Exception:
        raise _error("MAI_TRANSPORT_ERROR", "MAI request failed") from None
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _fetch_payload(
    audio_path: AudioInput,
    *,
    before_request: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], Any]:
    audio, filename, mime = _read_audio(audio_path)
    input_audio_sha256 = hashlib.sha256(audio).hexdigest()
    endpoint = _endpoint_url(os.environ)
    api_key = _read_api_key(os.environ)
    if before_request is not None:
        before_request()
    started = time.monotonic()
    http_status, response_body = _request_once(audio, filename, mime, endpoint, api_key)
    response_sha256 = hashlib.sha256(response_body).hexdigest()
    metadata: dict[str, Any] = {
        "provider": "mai",
        "model": MAI_MODEL,
        "input_audio_sha256": input_audio_sha256,
        "response_sha256": response_sha256,
        "elapsed_seconds": max(0.0, time.monotonic() - started),
        "http_status": http_status,
    }
    try:
        payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _error("MAI_RESPONSE_NOT_JSON", "MAI response is not JSON", metadata=metadata) from None
    metadata["raw_response"] = payload
    return metadata, payload


def transcribe_mai(
    audio_path: AudioInput,
    *,
    duration_ms: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Transcribe one audio input with MAI-Transcribe-2.

    The returned SRT is a native-timeline draft.  A native phrase overlap is
    rejected because one-track SRT cannot represent it without changing the
    provider timeline; the exception's ``metadata`` retains the evidence for
    the caller's fallback path.
    """

    if duration_ms is not None and (
        isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0
    ):
        raise _error("MAI_DURATION_INVALID", "duration_ms must be a non-negative integer")
    metadata, payload = _fetch_payload(audio_path)
    try:
        segments, native_segments, speaker_labels, _ = _parse_response(
            payload, duration_ms=duration_ms,
        )
    except MaiTranscriptionError as exc:
        # A rejected subtitle timeline is still useful native ASR evidence.
        exc.metadata = metadata
        raise
    metadata.update(
        segment_count=len(segments),
        speaker_labels=speaker_labels,
        segments=segments,
        native_segments=native_segments,
    )
    if _has_overlap(native_segments) or _has_overlap(segments):
        raise _error(
            "MAI_OVERLAP",
            "MAI native timeline overlaps and cannot be represented as one-track SRT",
            metadata=metadata,
        )
    return _render_srt(segments), metadata


def transcribe_mai_evidence(
    audio_path: AudioInput,
    *,
    duration_ms: int | None = None,
    before_request: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Return validated native MAI evidence without forcing an SRT timeline."""

    if duration_ms is not None and (
        isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0
    ):
        raise _error("MAI_DURATION_INVALID", "duration_ms must be a non-negative integer")
    if before_request is None:
        metadata, payload = _fetch_payload(audio_path)
    else:
        metadata, payload = _fetch_payload(audio_path, before_request=before_request)
    try:
        native_segments, speaker_labels, diagnostics = _parse_evidence_response(
            payload, duration_ms=duration_ms,
        )
    except MaiTranscriptionError as exc:
        exc.metadata = metadata
        raise
    has_overlap, has_out_of_order = _timeline_flags(native_segments)
    if has_overlap:
        diagnostics.append({"reason_code": "MAI_NATIVE_OVERLAP"})
    if has_out_of_order:
        diagnostics.append({"reason_code": "MAI_NATIVE_OUT_OF_ORDER"})
    has_encoder_tail = any(
        row.get("reason_code") == "MAI_NATIVE_ENCODER_TAIL_OVERHANG"
        for row in diagnostics
    )
    one_track_srt_eligible = (
        bool(native_segments) and not has_overlap and not has_out_of_order
        and not has_encoder_tail
    )
    metadata.update(
        status="NO_SPEECH" if not native_segments else "OK",
        segment_count=len(native_segments),
        speaker_labels=speaker_labels,
        native_segments=native_segments,
        diagnostics=diagnostics,
        native_timeline={
            "has_overlap": has_overlap,
            "has_out_of_order": has_out_of_order,
            **({"has_encoder_tail_overhang": True} if has_encoder_tail else {}),
            "one_track_srt_eligible": one_track_srt_eligible,
        },
        one_track_srt_eligible=one_track_srt_eligible,
    )
    return metadata


__all__ = [
    "MAI_API_VERSION",
    "MAI_MODEL",
    "MAI_TIMEOUT_SECONDS",
    "MaiTranscriptionError",
    "transcribe_mai",
    "transcribe_mai_evidence",
]
