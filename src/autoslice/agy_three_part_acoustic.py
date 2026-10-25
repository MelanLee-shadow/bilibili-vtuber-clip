"""One-shot native AGY transport for three hash-bound acoustic parts.

The normal observer already validates the three source WAVs before invoking a
transport.  This module adds the native AGY attachment shape used by the
bounded C07 probe: each WAV is rendered independently into a black MP4, the
full prompt and conversion facts are bound into the normal request identity,
and one local AGY subprocess consumes those three files in role order.

There is no concatenation, transcription, Gemini fallback, or publication
authority in this adapter.  Provider failures are surfaced to the existing
observer runtime, which preserves UNKNOWN/KEEP semantics.
"""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import wave
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from scripts.gemini_slice_jingting import agy_subprocess_env, parse_timeout_seconds
from src.autoslice import agy_gemini_client
from src.autoslice.nested_media_acoustic_runtime import (
    CPA_PROMPT,
    OBSERVER_PROMPT,
    RuntimeAcousticCallbacks,
    bound_observer_prompt,
    build_runtime_acoustic_callbacks,
)
from src.autoslice.package_import import atomic_write_json


SCHEMA = "agy-three-part-acoustic.v1"
PROCESS_SCHEMA = "agy-three-part-acoustic-process.v1"
ROLE_ORDER = ("host_enrollment", "independent_source", "current_target")
STRUCTURED_OUTPUT_FORMAT = "stream-json"
LEGACY_OUTPUT_FORMAT = "text"
STRUCTURED_SCHEMA_VERSION = "agy-three-part-acoustic-role.v1"
STRUCTURED_SCHEMA_FILENAME = "observer-role-schema.json"
STRUCTURED_LOG_FILENAME = "agy.log"

# This is the model-payload schema passed to ``agy --json-schema``.  The
# stream-json envelope is deliberately outside this object: the adapter only
# accepts the terminal result event and hands this object to the existing
# nested-media observer validator.  ``anyOf`` keeps the two role layouts that
# the current validator accepts (flat roles or a single ``states`` object).
STRUCTURED_ROLE_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": STRUCTURED_SCHEMA_VERSION,
    "title": "Nested media acoustic observer result",
    "type": "object",
    "required": ["status"],
    "properties": {
        "status": {"type": "string", "enum": ["OBSERVED", "UNKNOWN"]},
        "source_media_speech": {
            "type": "string", "enum": ["PRESENT", "ABSENT", "UNKNOWN"],
        },
        "host_speech": {
            "type": "string", "enum": ["PRESENT", "ABSENT", "UNKNOWN"],
        },
        "overlap_speech": {
            "type": "string", "enum": ["PRESENT", "ABSENT", "UNKNOWN"],
        },
        "states": {
            "type": "object",
            "required": ["source_media_speech", "host_speech", "overlap_speech"],
            "properties": {
                "source_media_speech": {
                    "type": "string", "enum": ["PRESENT", "ABSENT", "UNKNOWN"],
                },
                "host_speech": {
                    "type": "string", "enum": ["PRESENT", "ABSENT", "UNKNOWN"],
                },
                "overlap_speech": {
                    "type": "string", "enum": ["PRESENT", "ABSENT", "UNKNOWN"],
                },
            },
            "additionalProperties": True,
        },
        "evidence": {"oneOf": [{"type": "object"}, {"type": "string"}]},
        "instance_evidence": {"oneOf": [{"type": "object"}, {"type": "string"}]},
        "target_interval_ms": {
            "type": "array", "items": {"type": "integer"},
            "minItems": 2, "maxItems": 2,
        },
        "target_audio_offset_ms": {
            "type": "array", "items": {"type": "integer"},
            "minItems": 2, "maxItems": 2,
        },
        "target_cue_indexes": {
            "type": "array", "items": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
            "minItems": 1,
        },
    },
    "allOf": [
        {
            "anyOf": [
                {"required": ["source_media_speech", "host_speech", "overlap_speech"]},
                {"required": ["states"]},
            ],
        },
        {"anyOf": [{"required": ["evidence"]}, {"required": ["instance_evidence"]}]},
    ],
    "additionalProperties": True,
}
ATTACHMENT_INSTRUCTIONS = """Open prompt.md with view_file first and follow it exactly.
Then use view_file exactly once on each attachment in this order:
1. neutralpart-1.mp4 (trusted host enrollment reference)
2. neutralpart-2.mp4 (independently identified watched-source reference)
3. neutralpart-3.mp4 (current mixed target from the outer recording)
Do not open or inspect any other file. Do not use shell, terminal, browser, web, search, transcripts, captions, filenames as evidence, person names, or candidate wording. Do not write files. Return exactly one JSON object to stdout after the three view_file calls."""
VOICE_REFERENCE_ATTACHMENT_INSTRUCTIONS = ATTACHMENT_INSTRUCTIONS.replace(
    "2. neutralpart-2.mp4 (independently identified watched-source reference)",
    "2. neutralpart-2.mp4 (independently identified source recording used only as an UNALIGNED VOICE REFERENCE)",
)

_SAFE_SEGMENT = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
_UNKNOWN = "UNKNOWN_UNEXPOSED"
_DERIVATION_REUSABLE_STATUSES = frozenset(
    {"PREPARED", "AGY_RUNNING", "AGY_COMPLETED", "AGY_FAILED"}
)


class AgyThreePartAcousticError(ValueError):
    """A safe preparation, binding, or native AGY execution error."""

    def __init__(
        self,
        reason_code: str,
        *,
        terminal_status: str | None = None,
        http_status: int | None = None,
        provider_error: bool = False,
    ):
        self.reason_code = reason_code
        self.terminal_status = (
            terminal_status
            if isinstance(terminal_status, str)
            and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", terminal_status)
            else None
        )
        self.http_status = (
            http_status
            if type(http_status) is int and 100 <= http_status <= 599
            else None
        )
        self.provider_error = provider_error is True
        super().__init__(reason_code)


@dataclass(frozen=True)
class PreparedThreePartAttachments:
    """Hash-bound files and prompt prepared before the observer dispatch."""

    job_dir: Path
    prompt_path: Path
    process_path: Path
    bound_prompt: str
    attachment_prompt: str
    attachment_prompt_sha256: str
    source_parts: tuple[Mapping[str, object], ...]
    derived_parts: tuple[Mapping[str, object], ...]
    transport_binding: Mapping[str, object]
    binary: str
    model: str
    print_timeout: str
    output_format: str = LEGACY_OUTPUT_FORMAT
    schema_path: Path | None = None
    schema_sha256: str | None = None
    log_path: Path | None = None


def _sha_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    try:
        digest = _evidence_file_sha256(path)
    except OSError as exc:
        raise AgyThreePartAcousticError("AUDIO_PART_UNAVAILABLE") from exc
    return "sha256:" + digest


def _canonical_hash(value: object) -> str:
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise AgyThreePartAcousticError("IDENTITY_UNSERIALIZABLE") from exc
    return _sha_bytes(payload.encode("utf-8"))


def _structured_schema_text() -> str:
    """Return the exact schema bytes passed to the native AGY process."""

    return json.dumps(
        STRUCTURED_ROLE_SCHEMA,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"


def _resolve_output_format(value: str | None) -> str:
    resolved = STRUCTURED_OUTPUT_FORMAT if value is None else value.strip().lower()
    if resolved not in {LEGACY_OUTPUT_FORMAT, STRUCTURED_OUTPUT_FORMAT}:
        raise AgyThreePartAcousticError("AGY_OUTPUT_FORMAT_INVALID")
    return resolved


def _stream_event_kinds(event: Mapping[str, object]) -> set[str]:
    kinds: set[str] = set()
    for key in ("type", "event", "step_type"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            kinds.add(value.strip().lower())
    return kinds


_HTTP_STATUS_TEXT_RE = re.compile(
    r"\b(?:http(?:[_ -]?status)?|status(?:[_ -]?code)?|"
    r"error(?:[_ -]?code)?|code)\s*[:=]?\s*\(?\s*([1-5]\d{2})\b",
    re.IGNORECASE,
)


def _typed_http_status(value: Mapping[str, object]) -> int | None:
    """Extract only an explicitly typed or labelled HTTP status.

    Error text is never retained.  A bare number in arbitrary prose is not
    enough to become a provider status; it must be attached to a recognised
    status/code label.
    """

    for key in ("http_status", "status_code", "error_code", "code"):
        candidate = value.get(key)
        if type(candidate) is int and 100 <= candidate <= 599:
            return candidate
        if isinstance(candidate, str) and re.fullmatch(r"\s*[1-5]\d{2}\s*", candidate):
            return int(candidate)
    for key in ("error", "error_message"):
        candidate = value.get(key)
        if not isinstance(candidate, str):
            continue
        match = _HTTP_STATUS_TEXT_RE.search(candidate)
        if match is not None:
            return int(match.group(1))
    return None


def _structured_result_error(
    value: Mapping[str, object], *, terminal_status: object = None,
) -> AgyThreePartAcousticError:
    """Build a safe terminal-error exception without retaining provider text."""

    status = terminal_status
    nested_result = value.get("result")
    if status is None and isinstance(nested_result, Mapping):
        status = nested_result.get("status")
    http_status = _typed_http_status(value)
    if http_status is None and isinstance(nested_result, Mapping):
        http_status = _typed_http_status(nested_result)
    if status is None:
        status = value.get("status")
    return AgyThreePartAcousticError(
        "AGY_STRUCTURED_RESULT_ERROR",
        terminal_status=status if isinstance(status, str) else None,
        http_status=http_status,
        provider_error=True,
    )


def _structured_result_payload(event: Mapping[str, object]) -> Mapping[str, object] | None:
    """Extract the role object from the observed terminal result envelope."""

    result = event.get("result")
    if not isinstance(result, Mapping):
        return None
    payload = result.get("structured_output")
    return payload if isinstance(payload, Mapping) else None


def _validate_structured_role_payload(payload: Mapping[str, object]) -> None:
    """Apply the local fail-closed portion of the JSON schema.

    The CLI is the first schema validator, but a saved/replayed stream must
    not become trusted merely because an older wrapper ignored ``--json-schema``.
    The existing runtime still performs its richer scope and cross-container
    checks after this structural gate.
    """

    if payload.get("status") not in {"OBSERVED", "UNKNOWN"}:
        raise AgyThreePartAcousticError("AGY_STRUCTURED_SCHEMA_INVALID")
    evidence = payload.get("evidence", payload.get("instance_evidence"))
    if not isinstance(evidence, (Mapping, str)):
        raise AgyThreePartAcousticError("AGY_STRUCTURED_SCHEMA_INVALID")
    roles = ("source_media_speech", "host_speech", "overlap_speech")
    containers: list[Mapping[str, object]] = []
    if any(role in payload for role in roles):
        containers.append(payload)
    states = payload.get("states")
    if states is not None:
        if not isinstance(states, Mapping):
            raise AgyThreePartAcousticError("AGY_STRUCTURED_SCHEMA_INVALID")
        containers.append(states)
    if not containers:
        raise AgyThreePartAcousticError("AGY_STRUCTURED_SCHEMA_INVALID")
    for container in containers:
        if any(container.get(role) not in {"PRESENT", "ABSENT", "UNKNOWN"} for role in roles):
            raise AgyThreePartAcousticError("AGY_STRUCTURED_SCHEMA_INVALID")
    interval = payload.get("target_interval_ms")
    if interval is not None and (
        not isinstance(interval, list) or len(interval) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in interval)
    ):
        raise AgyThreePartAcousticError("AGY_STRUCTURED_SCHEMA_INVALID")
    cues = payload.get("target_cue_indexes")
    if cues is not None and (
        not isinstance(cues, list) or not cues
        or any(isinstance(value, bool) or not isinstance(value, (int, str)) for value in cues)
    ):
        raise AgyThreePartAcousticError("AGY_STRUCTURED_SCHEMA_INVALID")


def _parse_structured_stream(raw: str) -> tuple[str, dict[str, object]]:
    """Parse one AGY stream-json response and return the role JSON text.

    Every non-empty line must be a JSON object.  Exactly one terminal result
    event is required, and any error event or error-bearing event invalidates
    the response.  The raw NDJSON remains on disk; this function only returns
    the terminal model payload to the existing observer runtime.
    """

    if not isinstance(raw, str) or not raw.strip():
        raise AgyThreePartAcousticError("AGY_STRUCTURED_OUTPUT_EMPTY")
    events: list[Mapping[str, object]] = []
    for _line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise AgyThreePartAcousticError("AGY_STRUCTURED_OUTPUT_INVALID") from exc
        if not isinstance(value, Mapping):
            raise AgyThreePartAcousticError("AGY_STRUCTURED_EVENT_INVALID")
        events.append(value)
    if not events:
        raise AgyThreePartAcousticError("AGY_STRUCTURED_OUTPUT_EMPTY")

    terminal_indexes: list[int] = []
    event_types: list[str] = []
    tool_info_count = 0
    subagent_info_count = 0
    step_state_counts: dict[str, int] = {}
    done_tool_summaries: list[dict[str, object]] = []
    for index, event in enumerate(events):
        kinds = _stream_event_kinds(event)
        event_types.extend(sorted(kinds))
        if kinds & {"error", "failed", "failure"}:
            raise _structured_result_error(event)
        for key in ("error", "error_message", "error_code"):
            if event.get(key) not in (None, "", False, []):
                raise _structured_result_error(event)
        if event.get("is_error") is True or event.get("failed") is True:
            raise _structured_result_error(event)
        if kinds & {"result"}:
            result = event.get("result")
            if not isinstance(result, Mapping):
                raise AgyThreePartAcousticError("AGY_STRUCTURED_RESULT_PAYLOAD_INVALID")
            if result.get("status") != "SUCCESS":
                raise _structured_result_error(
                    result, terminal_status=result.get("status")
                )
            for key in ("error", "error_message", "error_code"):
                if result.get(key) not in (None, "", False, []):
                    raise _structured_result_error(
                        result, terminal_status=result.get("status")
                    )
            if result.get("is_error") is True or result.get("failed") is True:
                raise _structured_result_error(
                    result, terminal_status=result.get("status")
                )
            terminal_indexes.append(index)
        step_update = event.get("step_update")
        if isinstance(step_update, Mapping):
            step_type = step_update.get("step_type")
            if isinstance(step_type, str) and step_type.strip():
                event_types.append(step_type.strip().lower())
            state = step_update.get("state")
            state_key = state.strip().upper() if isinstance(state, str) and state.strip() else "UNKNOWN"
            step_state_counts[state_key] = step_state_counts.get(state_key, 0) + 1
            tool_info = step_update.get("tool_info")
            if isinstance(tool_info, Mapping):
                tool_info_count += 1
                if isinstance(step_type, str) and step_type.strip().lower() == "tool" and state_key == "DONE":
                    tool_name = step_update.get("tool_name")
                    if not isinstance(tool_name, str) or not tool_name.strip():
                        name = tool_info.get("name")
                        tool_name = name if isinstance(name, str) else None
                    if isinstance(tool_name, str) and tool_name.strip():
                        summary: dict[str, object] = {
                            "step_type": "tool",
                            "tool_name": tool_name.strip(),
                        }
                        step_index = step_update.get("step_index")
                        if isinstance(step_index, int) and not isinstance(step_index, bool):
                            summary["step_index"] = step_index
                        parameters = tool_info.get("parameters")
                        if isinstance(parameters, Mapping):
                            path = parameters.get("AbsolutePath", parameters.get("path"))
                            if isinstance(path, str) and path:
                                summary["path"] = path
                        done_tool_summaries.append(summary)
            if isinstance(step_update.get("subagent_info"), Mapping):
                subagent_info_count += 1

    if len(terminal_indexes) == 0:
        raise AgyThreePartAcousticError("AGY_STRUCTURED_RESULT_MISSING")
    if len(terminal_indexes) != 1:
        raise AgyThreePartAcousticError("AGY_STRUCTURED_RESULT_MULTIPLE")
    terminal_index = terminal_indexes[0]
    payload = _structured_result_payload(events[terminal_index])
    if payload is None:
        raise AgyThreePartAcousticError("AGY_STRUCTURED_RESULT_PAYLOAD_INVALID")
    _validate_structured_role_payload(payload)
    from .structured_output import parse_agy_stream
    payload_text = json.dumps(json.loads(parse_agy_stream(raw)), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    metadata = {
        "event_count": len(events),
        "event_types": sorted(set(event_types)),
        "terminal_result_event_index": terminal_index,
        "tool_info_event_count": tool_info_count,
        "subagent_info_event_count": subagent_info_count,
        "step_state_counts": step_state_counts,
        "done_tool_summaries": done_tool_summaries,
        "payload_sha256": _sha_bytes(payload_text.encode("utf-8")),
    }
    return payload_text, metadata


def _safe_hash(value: object, *, code: str = "AUDIO_PART_HASH_INVALID") -> str:
    if not isinstance(value, str):
        raise AgyThreePartAcousticError(code)
    raw = value.removeprefix("sha256:").lower()
    if len(raw) != 64 or any(char not in "0123456789abcdef" for char in raw):
        raise AgyThreePartAcousticError(code)
    return "sha256:" + raw


def _safe_segment(value: object, *, code: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 180
        or any(char not in _SAFE_SEGMENT for char in value)
        or value in {".", ".."}
    ):
        raise AgyThreePartAcousticError(code)
    return value


def _source_duration_seconds(path: Path) -> float:
    if path.suffix.lower() != ".wav":
        raise AgyThreePartAcousticError("AUDIO_PART_WAV_REQUIRED")
    try:
        with wave.open(str(path), "rb") as handle:
            frame_count = handle.getnframes()
            sample_rate = handle.getframerate()
    except (OSError, EOFError, wave.Error) as exc:
        raise AgyThreePartAcousticError("AUDIO_PART_WAV_INVALID") from exc
    if frame_count <= 0 or sample_rate <= 0:
        raise AgyThreePartAcousticError("AUDIO_PART_DURATION_INVALID")
    duration = frame_count / sample_rate
    if not math.isfinite(duration) or duration <= 0:
        raise AgyThreePartAcousticError("AUDIO_PART_DURATION_INVALID")
    return duration


def _first_line(value: str) -> str:
    line = value.splitlines()[0].strip() if value.splitlines() else ""
    return line[:256] if line else _UNKNOWN


def _probe_binary_version(binary: str) -> dict[str, object]:
    """Return bounded local version evidence without exposing command output."""

    last_completed = None
    used_flag = "--version"
    for flag in ("--version", "-version"):
        try:
            completed = subprocess.run(
                [binary, flag],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            if flag == "-version":
                return {"version": _UNKNOWN, "status": "UNAVAILABLE", "error_type": type(exc).__name__}
            continue
        last_completed = completed
        used_flag = flag
        if completed.returncode == 0:
            break
    if last_completed is None:
        return {"version": _UNKNOWN, "status": "UNAVAILABLE"}
    output = (last_completed.stdout or "") or (last_completed.stderr or "")
    return {
        "version": _first_line(output),
        "status": "AVAILABLE" if last_completed.returncode == 0 else "UNAVAILABLE",
        "returncode": last_completed.returncode,
        "version_flag": used_flag,
        "output_sha256": _sha_bytes(output.encode("utf-8")),
    }


def _duration_text(duration: float) -> str:
    # Six decimals preserve prepared PCM16 durations while keeping argv stable.
    return f"{duration:.6f}"


def _write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _write_process(path: Path, payload: Mapping[str, object]) -> None:
    atomic_write_json(path, dict(payload))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _source_parts_from_descriptor(descriptor: Mapping[str, object]) -> list[dict[str, object]]:
    fields = (
        ("host_enrollment", "host_enrollment_path", "host_enrollment_sha256"),
        ("independent_source", "source_audio_path", "source_audio_sha256"),
        ("current_target", "target_audio_path", "target_audio_sha256"),
    )
    rows: list[dict[str, object]] = []
    for role, path_field, hash_field in fields:
        value = descriptor.get(path_field)
        if not isinstance(value, (str, os.PathLike)):
            raise AgyThreePartAcousticError("AUDIO_PART_UNAVAILABLE")
        path = Path(value).expanduser()
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise AgyThreePartAcousticError("AUDIO_PART_UNAVAILABLE") from exc
        if path.is_symlink() or not resolved.is_file():
            raise AgyThreePartAcousticError("AUDIO_PART_UNAVAILABLE")
        expected = _safe_hash(descriptor.get(hash_field))
        actual = _sha_file(resolved)
        if actual != expected:
            raise AgyThreePartAcousticError("AUDIO_PART_HASH_MISMATCH")
        duration = _source_duration_seconds(resolved)
        rows.append({
            "role": role,
            "path": str(resolved),
            "sha256": actual,
            "duration_seconds": duration,
        })
    return rows


def _render_argv(*, source: Mapping[str, object], output: Path) -> list[str]:
    duration = _duration_text(float(source["duration_seconds"]))
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=320x240:r=50",
        "-i", str(source["path"]),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-t", duration, "-shortest", str(output),
    ]


def _validate_existing_process(
    *, process_path: Path, binding: Mapping[str, object], derived: Sequence[Mapping[str, object]],
    prompt_path: Path, prompt_sha256: str, schema_path: Path | None = None,
    schema_sha256: str | None = None,
) -> bool:
    if not process_path.is_file() or process_path.is_symlink():
        return False
    try:
        payload = json.loads(process_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, Mapping) or payload.get("transport_binding") != dict(binding):
        return False
    if (
        not prompt_path.is_file()
        or prompt_path.is_symlink()
        or _sha_file(prompt_path) != prompt_sha256
    ):
        return False
    recorded = payload.get("derived_parts")
    if not isinstance(recorded, list) or len(recorded) != len(derived):
        return False
    for saved, current in zip(recorded, derived, strict=True):
        if not isinstance(saved, Mapping):
            return False
        if any(saved.get(key) != value for key, value in current.items()):
            return False
    for item in derived:
        path_value = item.get("path")
        if not isinstance(path_value, str):
            return False
        path = Path(path_value)
        if path.is_symlink() or not path.is_file():
            return False
        if _sha_file(path) != item.get("sha256"):
            return False
    if schema_path is not None:
        if (
            schema_sha256 is None
            or schema_path.is_symlink()
            or not schema_path.is_file()
            or _sha_file(schema_path) != schema_sha256
        ):
            return False
    return True


def _read_process(path: Path) -> Mapping[str, object] | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _new_attempt_dir(base_dir: Path, artifact_key: str) -> Path:
    """Reserve a sibling attempt without touching an incomplete prior job."""

    base_dir.mkdir(parents=True, exist_ok=True)
    for ordinal in range(1, 10000):
        candidate = base_dir / f"{artifact_key}.attempt-{ordinal:04d}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        try:
            os.chmod(candidate, 0o700)
        except OSError:
            pass
        return candidate
    raise AgyThreePartAcousticError("AGY_ATTEMPT_SPACE_EXHAUSTED")


def _derived_parts(
    source_parts: Sequence[Mapping[str, object]], job_dir: Path,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, source in enumerate(source_parts, start=1):
        output = job_dir / f"neutralpart-{index}.mp4"
        rows.append({
            "index": index,
            "role": source["role"],
            "path": str(output),
            "source_sha256": source["sha256"],
            "source_duration_seconds": source["duration_seconds"],
            "ffmpeg_argv": _render_argv(source=source, output=output),
        })
    return rows


def _transport_binding(
    *,
    source_parts: Sequence[Mapping[str, object]],
    derived_parts: Sequence[Mapping[str, object]],
    attachment_prompt_sha256: str,
    ffmpeg_binary: str,
    ffmpeg_probe: Mapping[str, object],
    agy_binary: str,
    agy_probe: Mapping[str, object],
    model_value: str,
    timeout_value: str,
    adapter_source_sha256: str,
    job_dir: Path,
    output_format: str = LEGACY_OUTPUT_FORMAT,
    schema_sha256: str | None = None,
) -> dict[str, object]:
    source_hashes = {str(row["role"]): str(row["sha256"]) for row in source_parts}
    source_durations = {
        str(row["role"]): float(row["duration_seconds"]) for row in source_parts
    }
    pending_hashes = {str(row["role"]): "PENDING" for row in source_parts}
    binding: dict[str, object] = {
        "schema_version": SCHEMA,
        "observer_transport": "agy_three_part",
        "part_order": list(ROLE_ORDER),
        "source_audio_sha256": source_hashes,
        "source_audio_hashes": dict(source_hashes),
        "source_audio_durations_seconds": source_durations,
        "source_durations_seconds": dict(source_durations),
        "derived_attachment_sha256": pending_hashes,
        "derived_attachment_hashes": dict(pending_hashes),
        "attachment_prompt_sha256": attachment_prompt_sha256,
        "ffmpeg": {
            "binary": ffmpeg_binary,
            "version": dict(ffmpeg_probe),
            "conversion": "independent black video at 50fps + original WAV audio; -t source duration; -shortest",
            "argv": [row["ffmpeg_argv"] for row in derived_parts],
        },
        "agy": {
            "binary": agy_binary,
            "version": dict(agy_probe),
            "model": model_value,
            "print_timeout": timeout_value,
        },
        "adapter_source_sha256": adapter_source_sha256,
        "job_dir": str(job_dir),
    }
    if output_format == STRUCTURED_OUTPUT_FORMAT:
        if schema_sha256 is None:
            raise AgyThreePartAcousticError("AGY_STRUCTURED_SCHEMA_MISSING")
        binding["output_format"] = STRUCTURED_OUTPUT_FORMAT
        binding["structured_schema_version"] = STRUCTURED_SCHEMA_VERSION
        binding["structured_schema_sha256"] = schema_sha256
        binding["structured_log_filename"] = STRUCTURED_LOG_FILENAME
    elif output_format != LEGACY_OUTPUT_FORMAT:
        raise AgyThreePartAcousticError("AGY_OUTPUT_FORMAT_INVALID")
    return binding


def _reusable_attempt(
    *,
    job_dir: Path,
    source_parts: Sequence[Mapping[str, object]],
    attachment_prompt: str,
    attachment_prompt_sha256: str,
    bound_prompt: str,
    ffmpeg_binary: str,
    ffmpeg_probe: Mapping[str, object],
    agy_binary: str,
    agy_probe: Mapping[str, object],
    model_value: str,
    timeout_value: str,
    adapter_source_sha256: str,
    output_format: str,
    schema_sha256: str | None,
) -> PreparedThreePartAttachments | None:
    process_path = job_dir / "PROCESS.json"
    process = _read_process(process_path)
    if process is None or process.get("status") not in _DERIVATION_REUSABLE_STATUSES:
        return None
    derived_parts = _derived_parts(source_parts, job_dir)
    binding = _transport_binding(
        source_parts=source_parts, derived_parts=derived_parts,
        attachment_prompt_sha256=attachment_prompt_sha256,
        ffmpeg_binary=ffmpeg_binary, ffmpeg_probe=ffmpeg_probe,
        agy_binary=agy_binary, agy_probe=agy_probe, model_value=model_value,
        timeout_value=timeout_value, adapter_source_sha256=adapter_source_sha256,
        job_dir=job_dir, output_format=output_format, schema_sha256=schema_sha256,
    )
    old_binding = process.get("transport_binding")
    old_hashes = old_binding.get("derived_attachment_sha256") if isinstance(old_binding, Mapping) else None
    if not isinstance(old_hashes, Mapping) and isinstance(old_binding, Mapping):
        old_hashes = old_binding.get("derived_attachment_hashes")
    if not isinstance(old_hashes, Mapping):
        return None
    binding["derived_attachment_sha256"] = dict(old_hashes)
    binding["derived_attachment_hashes"] = dict(old_hashes)
    for row in derived_parts:
        output = Path(str(row["path"]))
        if output.is_file() and not output.is_symlink():
            try:
                row["sha256"] = _sha_file(output)
            except AgyThreePartAcousticError:
                return None
    try:
        valid = _validate_existing_process(
            process_path=process_path, binding=binding, derived=derived_parts,
            prompt_path=job_dir / "prompt.md", prompt_sha256=attachment_prompt_sha256,
            schema_path=(job_dir / STRUCTURED_SCHEMA_FILENAME
                         if output_format == STRUCTURED_OUTPUT_FORMAT else None),
            schema_sha256=schema_sha256,
        )
    except AgyThreePartAcousticError:
        valid = False
    if not valid:
        return None
    return PreparedThreePartAttachments(
        job_dir=job_dir, prompt_path=job_dir / "prompt.md", process_path=process_path,
        bound_prompt=bound_prompt, attachment_prompt=attachment_prompt,
        attachment_prompt_sha256=attachment_prompt_sha256,
        source_parts=tuple(source_parts), derived_parts=tuple(derived_parts),
        transport_binding=binding, binary=agy_binary, model=model_value,
        print_timeout=timeout_value,
        output_format=output_format,
        schema_path=(job_dir / STRUCTURED_SCHEMA_FILENAME
                     if output_format == STRUCTURED_OUTPUT_FORMAT else None),
        schema_sha256=schema_sha256,
        log_path=(job_dir / STRUCTURED_LOG_FILENAME
                  if output_format == STRUCTURED_OUTPUT_FORMAT else None),
    )


def _completed_agy_stdout(
    *, process: Mapping[str, object], job_dir: Path,
) -> tuple[str | None, str | None]:
    """Return a completed stdout only when its private receipt is intact."""

    if process.get("returncode") != 0:
        return None, "AGY_COMPLETED_RETURN_CODE_INVALID"
    paths = {
        "stdout": job_dir / "agy.stdout",
        "stderr": job_dir / "agy.stderr",
        "rc": job_dir / "agy.rc",
    }
    binding = process.get("transport_binding")
    structured = isinstance(binding, Mapping) and binding.get("output_format") == STRUCTURED_OUTPUT_FORMAT
    if structured:
        paths["log"] = job_dir / STRUCTURED_LOG_FILENAME
        paths["schema"] = job_dir / STRUCTURED_SCHEMA_FILENAME
    raw: dict[str, str] = {}
    for name, path in paths.items():
        if path.is_symlink() or not path.is_file():
            return None, "AGY_COMPLETED_RAW_UNAVAILABLE"
        try:
            raw[name] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None, "AGY_COMPLETED_RAW_UNAVAILABLE"
    try:
        if int(raw["rc"].strip()) != 0:
            return None, "AGY_COMPLETED_RETURN_CODE_INVALID"
        stdout_sha = _safe_hash(process.get("stdout_sha256"), code="AGY_COMPLETED_RAW_INVALID")
        stderr_sha = _safe_hash(process.get("stderr_sha256"), code="AGY_COMPLETED_RAW_INVALID")
    except (AgyThreePartAcousticError, ValueError):
        return None, "AGY_COMPLETED_RAW_INVALID"
    if _sha_bytes(raw["stdout"].encode("utf-8")) != stdout_sha:
        return None, "AGY_COMPLETED_STDOUT_DRIFT"
    if _sha_bytes(raw["stderr"].encode("utf-8")) != stderr_sha:
        return None, "AGY_COMPLETED_STDERR_DRIFT"
    if structured:
        schema_sha = binding.get("structured_schema_sha256") if isinstance(binding, Mapping) else None
        try:
            expected_schema_sha = _safe_hash(schema_sha, code="AGY_COMPLETED_SCHEMA_INVALID")
        except AgyThreePartAcousticError:
            return None, "AGY_COMPLETED_SCHEMA_INVALID"
        schema_path = paths["schema"]
        log_path = paths["log"]
        if schema_path.is_symlink() or log_path.is_symlink() or not schema_path.is_file() or not log_path.is_file():
            return None, "AGY_COMPLETED_STRUCTURED_ARTIFACT_UNAVAILABLE"
        if _sha_file(schema_path) != expected_schema_sha:
            return None, "AGY_COMPLETED_SCHEMA_DRIFT"
        try:
            expected_log_sha = _safe_hash(process.get("log_sha256"), code="AGY_COMPLETED_LOG_INVALID")
        except AgyThreePartAcousticError:
            return None, "AGY_COMPLETED_LOG_INVALID"
        if _sha_file(log_path) != expected_log_sha:
            return None, "AGY_COMPLETED_LOG_DRIFT"
    return raw["stdout"], None


def prepare_three_part_attachments(
    *,
    descriptor: Mapping[str, object],
    candidate_id: str,
    observation_id: str,
    evidence_root: str | Path,
    model: str | None = None,
    print_timeout: str | None = None,
    agy_binary: str | None = None,
    observer_prompt: str | None = None,
    attachment_instructions: str | None = None,
    output_format: str | None = None,
    playback_locator: Mapping[str, object] | None = None,
) -> PreparedThreePartAttachments:
    """Verify WAVs, derive three independent black MP4 attachments, and bind facts."""

    candidate = _safe_segment(candidate_id, code="CANDIDATE_ID_INVALID")
    observation = _safe_segment(observation_id, code="OBSERVATION_ID_INVALID")
    output_format_value = _resolve_output_format(output_format)
    schema_text = _structured_schema_text() if output_format_value == STRUCTURED_OUTPUT_FORMAT else None
    schema_sha256 = (
        _sha_bytes(schema_text.encode("utf-8")) if schema_text is not None else None
    )
    source_parts = _source_parts_from_descriptor(descriptor)
    target_interval = descriptor.get("target_interval_ms")
    target_cues = descriptor.get("target_cue_indexes")
    interval = descriptor.get("interval_ms")
    audio_window = descriptor.get("target_audio_window_ms")
    if audio_window is None:
        audio_window = (target_interval
            if descriptor.get("observer_mode") == "VOICE_REFERENCE_ONLY_UNALIGNED" else interval)
    observer_prompt_value = observer_prompt if observer_prompt is not None else OBSERVER_PROMPT
    try:
        bound_prompt = bound_observer_prompt(
            observer_prompt=observer_prompt_value,
            interval_ms=interval,
            target_interval_ms=target_interval,
            target_cue_indexes=target_cues,
            target_audio_window_ms=audio_window,
            playback_locator=(
                (
                    playback_locator
                    if playback_locator is not None
                    else descriptor.get("playback_locator")
                )
                if descriptor.get("observer_mode") == "VOICE_REFERENCE_ONLY_UNALIGNED"
                else None
            ),
        )
    except Exception as exc:
        if isinstance(exc, AgyThreePartAcousticError):
            raise
        raise AgyThreePartAcousticError("TARGET_SCOPE_INVALID") from exc
    if target_interval is not None:
        # A full-context clock must not be attached to a one-cue WAV (or vice
        # versa). The actual source WAV has already been hash/duration checked.
        expected_ms = audio_window[1] - audio_window[0]
        actual_ms = float(source_parts[2]["duration_seconds"]) * 1000
        if abs(actual_ms - expected_ms) > 20:
            raise AgyThreePartAcousticError("TARGET_AUDIO_WINDOW_DURATION_MISMATCH")
    attachment_prompt = (
        attachment_instructions if attachment_instructions is not None else ATTACHMENT_INSTRUCTIONS
    ) + "\n\n" + bound_prompt
    attachment_prompt_sha256 = _sha_bytes(attachment_prompt.encode("utf-8"))
    binary = agy_gemini_client.resolve_local_agy_executable(agy_binary)
    model_value = (model or os.environ.get("AGY_THREE_PART_MODEL", agy_gemini_client.DEFAULT_AGY_FLASH_LOW)).strip()
    timeout_value = (print_timeout or os.environ.get("AGY_THREE_PART_PRINT_TIMEOUT")
                     or os.environ.get("AGY_PRINT_TIMEOUT", "15m")).strip()
    if not model_value or not timeout_value:
        raise AgyThreePartAcousticError("AGY_CONFIGURATION_INVALID")
    ffmpeg_binary = shutil.which("ffmpeg") or "ffmpeg"
    ffmpeg_probe = _probe_binary_version(ffmpeg_binary)
    agy_probe = _probe_binary_version(binary)
    adapter_source_sha256 = _sha_file(Path(__file__))
    artifact_seed = {
        "schema_version": SCHEMA,
        "part_order": list(ROLE_ORDER),
        "source_audio_sha256": {str(row["role"]): row["sha256"] for row in source_parts},
        "source_durations_seconds": {str(row["role"]): row["duration_seconds"] for row in source_parts},
        "attachment_prompt_sha256": attachment_prompt_sha256,
        "ffmpeg_binary": ffmpeg_binary,
        "ffmpeg_version": ffmpeg_probe,
        "model": model_value,
        "agy_binary": binary,
        "agy_version": agy_probe,
        "adapter_source_sha256": adapter_source_sha256,
    }
    if output_format_value == STRUCTURED_OUTPUT_FORMAT:
        artifact_seed.update({
            "output_format": STRUCTURED_OUTPUT_FORMAT,
            "structured_schema_version": STRUCTURED_SCHEMA_VERSION,
            "structured_schema_sha256": schema_sha256,
            "structured_log_filename": STRUCTURED_LOG_FILENAME,
        })
    artifact_key = _canonical_hash(artifact_seed).removeprefix("sha256:")
    root = Path(evidence_root).expanduser().absolute()
    base_dir = root / "agy-three-part" / candidate / observation
    if root.exists() and root.is_symlink():
        raise AgyThreePartAcousticError("EVIDENCE_ROOT_UNSAFE")
    canonical_job_dir = base_dir / artifact_key
    if canonical_job_dir.exists() and (
        canonical_job_dir.is_symlink() or not canonical_job_dir.is_dir()
    ):
        raise AgyThreePartAcousticError("EVIDENCE_ROOT_UNSAFE")
    canonical_job_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base_dir, 0o700)
        os.chmod(canonical_job_dir, 0o700)
    except OSError:
        pass
    job_dir = canonical_job_dir
    derived_parts = _derived_parts(source_parts, job_dir)
    transport_binding = _transport_binding(
        source_parts=source_parts, derived_parts=derived_parts,
        attachment_prompt_sha256=attachment_prompt_sha256,
        ffmpeg_binary=ffmpeg_binary, ffmpeg_probe=ffmpeg_probe,
        agy_binary=binary, agy_probe=agy_probe, model_value=model_value,
        timeout_value=timeout_value, adapter_source_sha256=adapter_source_sha256,
        job_dir=job_dir, output_format=output_format_value, schema_sha256=schema_sha256,
    )
    prompt_path = job_dir / "prompt.md"
    process_path = job_dir / "PROCESS.json"
    old_process = _read_process(process_path)
    old_status = old_process.get("status") if old_process is not None else None
    old_binding = old_process.get("transport_binding") if old_process is not None else None
    old_hashes = None
    if isinstance(old_binding, Mapping):
        old_hashes = old_binding.get("derived_attachment_sha256")
        if not isinstance(old_hashes, Mapping):
            old_hashes = old_binding.get("derived_attachment_hashes")
        if isinstance(old_hashes, Mapping):
            transport_binding["derived_attachment_sha256"] = dict(old_hashes)
            transport_binding["derived_attachment_hashes"] = dict(old_hashes)
    for row in derived_parts:
        output = Path(str(row["path"]))
        if output.is_file() and not output.is_symlink():
            row["sha256"] = _sha_file(output)
    reusable = (
        old_process is not None
        and old_status in _DERIVATION_REUSABLE_STATUSES
        and _validate_existing_process(
            process_path=process_path, binding=transport_binding, derived=derived_parts,
            prompt_path=prompt_path, prompt_sha256=attachment_prompt_sha256,
            schema_path=(job_dir / STRUCTURED_SCHEMA_FILENAME
                         if output_format_value == STRUCTURED_OUTPUT_FORMAT else None),
            schema_sha256=schema_sha256,
        )
    )
    if reusable:
        return PreparedThreePartAttachments(
            job_dir=job_dir, prompt_path=prompt_path, process_path=process_path,
            bound_prompt=bound_prompt, attachment_prompt=attachment_prompt,
            attachment_prompt_sha256=attachment_prompt_sha256,
            source_parts=tuple(source_parts), derived_parts=tuple(derived_parts),
            transport_binding=transport_binding, binary=binary, model=model_value,
            print_timeout=timeout_value,
            output_format=output_format_value,
            schema_path=(job_dir / STRUCTURED_SCHEMA_FILENAME
                         if output_format_value == STRUCTURED_OUTPUT_FORMAT else None),
            schema_sha256=schema_sha256,
            log_path=(job_dir / STRUCTURED_LOG_FILENAME
                      if output_format_value == STRUCTURED_OUTPUT_FORMAT else None),
        )
    if old_process is not None and old_status in _DERIVATION_REUSABLE_STATUSES:
        raise AgyThreePartAcousticError("AGY_ARTIFACT_BINDING_MISMATCH")
    for sibling in sorted(base_dir.glob(f"{artifact_key}.attempt-*")):
        if not sibling.is_dir() or sibling.is_symlink():
            continue
        recovered = _reusable_attempt(
            job_dir=sibling, source_parts=source_parts,
            attachment_prompt=attachment_prompt,
            attachment_prompt_sha256=attachment_prompt_sha256,
            bound_prompt=bound_prompt, ffmpeg_binary=ffmpeg_binary,
            ffmpeg_probe=ffmpeg_probe, agy_binary=binary, agy_probe=agy_probe,
            model_value=model_value, timeout_value=timeout_value,
            adapter_source_sha256=adapter_source_sha256,
            output_format=output_format_value, schema_sha256=schema_sha256,
        )
        if recovered is not None:
            return recovered
    if old_process is None and any(canonical_job_dir.iterdir()):
        job_dir = _new_attempt_dir(base_dir, artifact_key)
    elif old_process is not None:
        job_dir = _new_attempt_dir(base_dir, artifact_key)
    if job_dir != canonical_job_dir:
        derived_parts = _derived_parts(source_parts, job_dir)
        transport_binding = _transport_binding(
            source_parts=source_parts, derived_parts=derived_parts,
            attachment_prompt_sha256=attachment_prompt_sha256,
            ffmpeg_binary=ffmpeg_binary, ffmpeg_probe=ffmpeg_probe,
            agy_binary=binary, agy_probe=agy_probe, model_value=model_value,
            timeout_value=timeout_value, adapter_source_sha256=adapter_source_sha256,
            job_dir=job_dir, output_format=output_format_value, schema_sha256=schema_sha256,
        )
        prompt_path = job_dir / "prompt.md"
        process_path = job_dir / "PROCESS.json"
    schema_path = job_dir / STRUCTURED_SCHEMA_FILENAME if schema_text is not None else None
    log_path = job_dir / STRUCTURED_LOG_FILENAME if schema_text is not None else None
    if schema_text is not None:
        _write_text(schema_path, schema_text)
    _write_text(prompt_path, attachment_prompt)
    process: dict[str, object] = {
        "schema_version": PROCESS_SCHEMA,
        "status": "PREPARING",
        "job_invocations": 0,
        "candidate_id": candidate,
        "observation_id": observation,
        "transport_binding": transport_binding,
        "source_parts": source_parts,
        "derived_parts": derived_parts,
        "prompt_path": str(prompt_path),
        "prompt_sha256": attachment_prompt_sha256,
        "output_format": output_format_value,
        "recovery": {
            "job_dir": str(job_dir),
            "stdout_path": str(job_dir / "agy.stdout"),
            "stderr_path": str(job_dir / "agy.stderr"),
            "rc_path": str(job_dir / "agy.rc"),
        },
    }
    if schema_path is not None and log_path is not None:
        process["structured_output"] = {
            "format": STRUCTURED_OUTPUT_FORMAT,
            "schema_path": str(schema_path),
            "schema_sha256": schema_sha256,
            "log_path": str(log_path),
            "log_filename": STRUCTURED_LOG_FILENAME,
        }
        process["recovery"] = {
            **dict(process["recovery"]),
            "schema_path": str(schema_path),
            "log_path": str(log_path),
        }
    _write_process(process_path, process)
    for row in derived_parts:
        output = Path(str(row["path"]))
        rendered = subprocess.run(
            list(row["ffmpeg_argv"]), check=False, capture_output=True,
            text=True, timeout=180,
        )
        if rendered.returncode != 0 or not output.is_file() or output.is_symlink():
            process.update({"status": "FAILED_RENDER", "ffmpeg_returncode": rendered.returncode})
            _write_process(process_path, process)
            raise AgyThreePartAcousticError("AGY_ATTACHMENT_RENDER_FAILED")
        row["sha256"] = _sha_file(output)
        try:
            os.chmod(output, 0o600)
        except OSError:
            pass
        row["ffmpeg_stderr_sha256"] = _sha_bytes((rendered.stderr or "").encode("utf-8"))
        process["derived_parts"] = derived_parts
        _write_process(process_path, process)
    derived_hashes = {
        str(row["role"]): str(row["sha256"]) for row in derived_parts
    }
    transport_binding["derived_attachment_sha256"] = derived_hashes
    transport_binding["derived_attachment_hashes"] = dict(derived_hashes)
    process["transport_binding"] = transport_binding
    process["status"] = "PREPARED"
    if schema_path is not None:
        process["schema_sha256"] = schema_sha256
    _write_process(process_path, process)
    return PreparedThreePartAttachments(
        job_dir=job_dir, prompt_path=prompt_path, process_path=process_path,
        bound_prompt=bound_prompt, attachment_prompt=attachment_prompt,
        attachment_prompt_sha256=attachment_prompt_sha256,
        source_parts=tuple(source_parts), derived_parts=tuple(derived_parts),
        transport_binding=transport_binding, binary=binary, model=model_value,
        print_timeout=timeout_value,
        output_format=output_format_value,
        schema_path=schema_path,
        schema_sha256=schema_sha256,
        log_path=log_path,
    )


def build_three_part_callbacks(
    *,
    descriptor: Mapping[str, object],
    candidate_id: str,
    observation_id: str,
    evidence_root: str | Path,
    cpa_call: Callable[[str], object],
    model: str | None = None,
    print_timeout: str | None = None,
    agy_binary: str | None = None,
    observer_prompt: str | None = None,
    cpa_prompt: str | None = None,
    attachment_instructions: str | None = None,
    output_format: str | None = None,
    playback_locator: Mapping[str, object] | None = None,
) -> RuntimeAcousticCallbacks:
    """Prepare one target's attachments, then bind the native AGY callbacks."""

    resolved_attachment_instructions = (
        attachment_instructions
        if attachment_instructions is not None
        else ATTACHMENT_INSTRUCTIONS
    )
    prepared = prepare_three_part_attachments(
        descriptor=descriptor, candidate_id=candidate_id, observation_id=observation_id,
        evidence_root=evidence_root, model=model, print_timeout=print_timeout,
        agy_binary=agy_binary, observer_prompt=observer_prompt,
        attachment_instructions=resolved_attachment_instructions,
        output_format=output_format,
        playback_locator=playback_locator,
    )
    callback_invocations = 0
    dispatch_calls = 0
    process: dict[str, object]
    try:
        process_value = json.loads(prepared.process_path.read_text(encoding="utf-8"))
        process = dict(process_value) if isinstance(process_value, Mapping) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        process = {}

    expected_source_hashes = {
        str(row["role"]): str(row["sha256"]) for row in prepared.source_parts
    }
    expected_derived_hashes = {
        str(row["role"]): str(row["sha256"]) for row in prepared.derived_parts
    }
    initial_status = process.get("status")
    historical_job_invocations: int | str = _UNKNOWN
    raw_invocations = process.get("job_invocations")
    if isinstance(raw_invocations, int) and not isinstance(raw_invocations, bool) and raw_invocations >= 0:
        historical_job_invocations = raw_invocations
    completion_replay: str | None = None
    replay_block_reason: str | None = None
    if initial_status == "AGY_COMPLETED":
        completion_replay, replay_block_reason = _completed_agy_stdout(
            process=process, job_dir=prepared.job_dir,
        )
    elif initial_status in {"AGY_RUNNING", "AGY_FAILED"}:
        replay_block_reason = f"{initial_status}_REPLAY_BLOCKED"
    elif initial_status != "PREPARED":
        replay_block_reason = "AGY_PROCESS_STATUS_UNUSABLE"

    def completion_for_runtime(raw: str) -> str:
        if prepared.output_format != STRUCTURED_OUTPUT_FORMAT and '"structured_output"' not in raw:
            return raw
        try:
            payload, metadata = _parse_structured_stream(raw)
        except AgyThreePartAcousticError as exc:
            structured_result: dict[str, object] = {
                "status": "INVALID",
                "reason_code": exc.reason_code,
                "raw_sha256": _sha_bytes(raw.encode("utf-8")),
            }
            if exc.terminal_status is not None:
                structured_result["terminal_status"] = exc.terminal_status
            if exc.http_status is not None:
                structured_result["http_status"] = exc.http_status
            process["structured_result"] = structured_result
            _write_process(prepared.process_path, process)
            raise
        process["structured_result"] = {"status": "VALID", **metadata}
        _write_process(prepared.process_path, process)
        return payload

    def audio_generate(**arguments: object) -> str:
        nonlocal callback_invocations, dispatch_calls
        if callback_invocations != 0:
            raise AgyThreePartAcousticError("AGY_JOB_DUPLICATE_BLOCKED")
        callback_invocations += 1
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str):
            raise AgyThreePartAcousticError("OBSERVER_PROMPT_INVALID")
        actual_attachment_prompt = resolved_attachment_instructions + "\n\n" + prompt
        if _sha_bytes(actual_attachment_prompt.encode("utf-8")) != prepared.attachment_prompt_sha256:
            raise AgyThreePartAcousticError("ATTACHMENT_PROMPT_BINDING_MISMATCH")
        parts = arguments.get("inline_parts")
        if not isinstance(parts, list) or len(parts) != len(ROLE_ORDER):
            raise AgyThreePartAcousticError("AUDIO_PART_ORDER_INVALID")
        for index, item in enumerate(parts):
            if not isinstance(item, tuple) or len(item) != 2 or not isinstance(item[0], bytes):
                raise AgyThreePartAcousticError("AUDIO_PART_INVALID")
            role = ROLE_ORDER[index]
            if _sha_bytes(item[0]) != expected_source_hashes[role]:
                raise AgyThreePartAcousticError("AUDIO_PART_HASH_MISMATCH")
        if _sha_file(prepared.prompt_path) != prepared.attachment_prompt_sha256:
            process.update({"status": "BINDING_DRIFT", "failure_category": "PROMPT_FILE_HASH_MISMATCH"})
            _write_process(prepared.process_path, process)
            raise AgyThreePartAcousticError("ATTACHMENT_PROMPT_FILE_MISMATCH")
        for row in prepared.derived_parts:
            path_value = row.get("path")
            if (
                not isinstance(path_value, str)
                or Path(path_value).is_symlink()
                or not Path(path_value).is_file()
                or _sha_file(Path(path_value)) != expected_derived_hashes.get(str(row["role"]))
            ):
                process.update({"status": "BINDING_DRIFT", "failure_category": "DERIVED_ATTACHMENT_HASH_MISMATCH"})
                _write_process(prepared.process_path, process)
                raise AgyThreePartAcousticError("AGY_DERIVED_ATTACHMENT_HASH_MISMATCH")
        if replay_block_reason is not None:
            raise AgyThreePartAcousticError(replay_block_reason)
        if initial_status == "AGY_COMPLETED":
            if completion_replay is None:
                raise AgyThreePartAcousticError("AGY_COMPLETED_RAW_INVALID")
            return completion_for_runtime(completion_replay)
        argv = agy_gemini_client.agy_argv(
            prepared.binary, job_dir=prepared.job_dir, model=prepared.model,
            prompt=("Open prompt.md with view_file and follow it exactly. Use only "
                    "prompt.md and the three neutralpart MP4 files. Do not use any "
                    "other tool or write files. Return one JSON object to stdout."),
            print_timeout=prepared.print_timeout,
        )
        if prepared.output_format == STRUCTURED_OUTPUT_FORMAT:
            if prepared.schema_path is None or prepared.log_path is None:
                raise AgyThreePartAcousticError("AGY_STRUCTURED_ARTIFACT_UNAVAILABLE")
            if prepared.schema_path.is_symlink() or not prepared.schema_path.is_file():
                raise AgyThreePartAcousticError("AGY_STRUCTURED_SCHEMA_UNAVAILABLE")
            try:
                schema_matches = _sha_file(prepared.schema_path) == prepared.schema_sha256
            except AgyThreePartAcousticError:
                schema_matches = False
            if not schema_matches:
                process.update({
                    "status": "AGY_FAILED",
                    "failure_category": "STRUCTURED_SCHEMA_HASH_MISMATCH",
                })
                _write_process(prepared.process_path, process)
                raise AgyThreePartAcousticError("AGY_STRUCTURED_SCHEMA_HASH_MISMATCH")
            from .structured_output import agy_structured_args
            argv.extend(agy_structured_args(prepared.schema_path))
            argv.extend(["--log-file", str(prepared.log_path)])
        else:
            # Keep historical text cache identity and replay intact. New
            # dispatches still declare the role schema at the wire boundary.
            from .structured_output import agy_structured_args
            wire_schema = prepared.job_dir / 'observer-wire-schema.json'
            _write_text(wire_schema, _structured_schema_text())
            argv.extend(agy_structured_args(wire_schema))
        process['wire_output_format'] = STRUCTURED_OUTPUT_FORMAT
        process['wire_schema_sha256'] = _sha_bytes(_structured_schema_text().encode())
        log_existed_before_dispatch = (
            prepared.output_format == STRUCTURED_OUTPUT_FORMAT
            and prepared.log_path is not None
            and prepared.log_path.is_file()
            and not prepared.log_path.is_symlink()
        )
        dispatch_calls += 1
        process.update({"status": "AGY_RUNNING", "job_invocations": dispatch_calls,
                        "agy_argv": argv, "attachment_prompt_sha256": prepared.attachment_prompt_sha256,
                        "output_format": prepared.output_format})
        _write_process(prepared.process_path, process)
        run = agy_gemini_client.run_local_agy(
            argv, cwd=prepared.job_dir, env=agy_subprocess_env(),
            timeout=parse_timeout_seconds(prepared.print_timeout) + 120,
        )
        if run.completed is None:
            stdout, stderr = run.partial_output()
            _write_text(prepared.job_dir / "agy.stdout", stdout)
            _write_text(prepared.job_dir / "agy.stderr", stderr)
            _write_text(prepared.job_dir / "agy.rc", "")
            if prepared.output_format == STRUCTURED_OUTPUT_FORMAT and prepared.log_path is not None:
                log_created_by_cli = (
                    not log_existed_before_dispatch
                    and prepared.log_path.is_file()
                    and not prepared.log_path.is_symlink()
                )
                if not prepared.log_path.exists():
                    _write_text(prepared.log_path, "")
                process["log_created_by_cli"] = log_created_by_cli
                process["log_file_exists"] = (
                    prepared.log_path.is_file() and not prepared.log_path.is_symlink()
                )
                if prepared.log_path.is_file() and not prepared.log_path.is_symlink():
                    process["log_sha256"] = _sha_file(prepared.log_path)
            process.update({"status": "AGY_FAILED", "failure_category": run.failure_category,
                            "launch_error_type": run.launch_error_type})
            _write_process(prepared.process_path, process)
            raise AgyThreePartAcousticError("AGY_JOB_FAILED") from run.launch_error
        completed = run.completed
        _write_text(prepared.job_dir / "agy.stdout", completed.stdout or "")
        _write_text(prepared.job_dir / "agy.stderr", completed.stderr or "")
        _write_text(prepared.job_dir / "agy.rc", f"{completed.returncode}\n")
        if prepared.output_format == STRUCTURED_OUTPUT_FORMAT and prepared.log_path is not None:
            if prepared.log_path.is_symlink():
                process.update({"status": "AGY_FAILED", "failure_category": "AGY_LOG_PATH_UNSAFE"})
                _write_process(prepared.process_path, process)
                raise AgyThreePartAcousticError("AGY_LOG_PATH_UNSAFE")
            if not prepared.log_path.exists():
                # Keep an explicit empty placeholder when the wrapper leaves
                # the optional diagnostic file absent; raw NDJSON stdout is
                # the primary tool audit.
                log_created_by_cli = (
                    not log_existed_before_dispatch
                    and prepared.log_path.is_file()
                    and not prepared.log_path.is_symlink()
                )
                _write_text(prepared.log_path, "")
            else:
                log_created_by_cli = (
                    not log_existed_before_dispatch
                    and prepared.log_path.is_file()
                    and not prepared.log_path.is_symlink()
                )
            process["log_created_by_cli"] = log_created_by_cli
            process["log_file_exists"] = (
                prepared.log_path.is_file() and not prepared.log_path.is_symlink()
            )
            process["log_sha256"] = _sha_file(prepared.log_path)
        process.update({
            "status": "AGY_COMPLETED" if completed.returncode == 0 else "AGY_FAILED",
            "returncode": completed.returncode,
            "stdout_sha256": _sha_bytes((completed.stdout or "").encode("utf-8")),
            "stderr_sha256": _sha_bytes((completed.stderr or "").encode("utf-8")),
        })
        _write_process(prepared.process_path, process)
        if completed.returncode != 0:
            raise AgyThreePartAcousticError("AGY_JOB_FAILED")
        return completion_for_runtime(completed.stdout or "")

    def audio_attempts() -> list[dict[str, object]]:
        return [{
            "provider": "agy",
            "transport": "agy_three_part",
            "job_invocations": dispatch_calls,
            "historical_job_invocations": historical_job_invocations,
            "replayed_completion": initial_status == "AGY_COMPLETED" and completion_replay is not None,
            **({"replay_block_reason": replay_block_reason} if replay_block_reason else {}),
            "underlying_api_call_count": _UNKNOWN,
            "source_audio_hashes": expected_source_hashes,
            "derived_attachment_hashes": expected_derived_hashes,
            "attachment_prompt_sha256": prepared.attachment_prompt_sha256,
            "output_format": prepared.output_format,
            **({
                "structured_schema_sha256": prepared.schema_sha256,
                "structured_log_path": str(prepared.log_path),
                "structured_result": process.get("structured_result"),
            } if prepared.output_format == STRUCTURED_OUTPUT_FORMAT else {}),
            "process_path": str(prepared.process_path),
        }]

    runtime_model = "".join(
        character.lower() if character.isalnum() else "-"
        for character in prepared.model
    ).strip("-")
    if not runtime_model:
        raise AgyThreePartAcousticError("AGY_CONFIGURATION_INVALID")
    return build_runtime_acoustic_callbacks(
        audio_generate=audio_generate,
        cpa_call=cpa_call,
        provider="agy",
        model=runtime_model,
        transport="agy_three_part",
        transport_module="src.autoslice.agy_three_part_acoustic",
        transport_module_sha256=_sha_file(Path(__file__)),
        transport_binding=prepared.transport_binding,
        observer_prompt=(observer_prompt if observer_prompt is not None else OBSERVER_PROMPT),
        cpa_prompt=(cpa_prompt if cpa_prompt is not None else CPA_PROMPT),
        audio_attempts=audio_attempts,
        playback_locator=playback_locator,
    )


__all__ = [
    "ATTACHMENT_INSTRUCTIONS",
    "VOICE_REFERENCE_ATTACHMENT_INSTRUCTIONS",
    "AgyThreePartAcousticError",
    "PreparedThreePartAttachments",
    "ROLE_ORDER",
    "SCHEMA",
    "STRUCTURED_OUTPUT_FORMAT",
    "STRUCTURED_ROLE_SCHEMA",
    "build_three_part_callbacks",
    "prepare_three_part_attachments",
]
