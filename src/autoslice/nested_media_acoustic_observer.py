"""Hash-bound injected observer seam for nested-media acoustic evidence.

The caller chooses the runtime and supplies ``observe_audio`` and
``cpa_judge``.  This module only validates their evidence and binds it to the
current outer media, SRT/frame, independent source mapping, and three audio
parts before writing the existing acoustic receipt.  A receipt is evidence
only; it never edits subtitles or authorizes publication.
"""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import hashlib
import json
import math
import re
from urllib.parse import urlparse
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from src.autoslice.nested_media_caption_attribution import ACOUSTIC_SCHEMA
from src.autoslice.package_import import atomic_write_json, exclusive_lock
from src.autoslice.short_reference_playback_locator import (
    ShortReferencePlaybackLocatorError,
    validate_locator,
)

REQUEST_SCHEMA = "nested-media-acoustic-observer-request.v1"
OBSERVER_SCHEMA = "nested-media-acoustic-observation.v1"
CPA_SCHEMA = "nested-media-acoustic-cpa-judgement.v1"
RESULT_SCHEMA = "nested-media-acoustic-observer-result.v1"
STATE_SCHEMA = "nested-media-acoustic-observer-state.v1"
METHOD = "HASH_BOUND_INJECTED_OBSERVER_CPA"
VOICE_REFERENCE_MODE = "VOICE_REFERENCE_ONLY_UNALIGNED"
VOICE_REFERENCE_BINDING_SCHEMA = "nested-media-voice-reference-binding.v1"
VOICE_REFERENCE_BINDING_STATUS = "REFERENCE_BOUND"
VOICE_REFERENCE_METHOD = "HASH_BOUND_UNALIGNED_VOICE_REFERENCE"
_ROLES = ("source_media_speech", "host_speech", "overlap_speech")
_STATES = frozenset({"PRESENT", "ABSENT", "UNKNOWN"})
_SAFE = re.compile(r"[A-Za-z0-9_.-]{1,180}\Z")


class NestedMediaAcousticObserverError(ValueError):
    """Input, cache, or callback evidence cannot be safely bound."""


ObserveAudio = Callable[[Mapping[str, object]], Mapping[str, object]]
CpaJudge = Callable[[Mapping[str, object], Mapping[str, object]], Mapping[str, object]]
RuntimeIdentity = Mapping[str, object]

_RUNTIME_IDENTITY_FIELDS = (
    "provider",
    "model",
    "transport",
    "transport_module",
    "transport_module_sha256",
    "observer_prompt_sha256",
    "cpa_prompt_sha256",
)
# These versions describe protocol semantics, never a deployment. Historical
# identities without a version implement v1; changing this constant must not
# relabel those historical observations as the new contract.
LEGACY_RESPONSE_ADAPTER_CONTRACT = "nested-acoustic-response.v1"
LEGACY_TRANSPORT_CONTRACT = "agy-three-part.v1"
LEGACY_WIRE_SCHEMA_SHA256 = "sha256:8c6476ce82729503ec7aba960df0e19d11f8a43c2cc0a47e0d38de7bd1f9f54c"


def _transport_protocol(binding: Mapping[str, object]) -> dict[str, object]:
    """Project the three-part wire contract; keep execution facts in PROCESS."""
    if binding.get("observer_transport") != "agy_three_part":
        # An unknown transport has no authority to discard binding fields.
        return dict(binding)
    agy = binding.get("agy")
    ffmpeg = binding.get("ffmpeg")
    protocol = {
        **{key: binding[key] for key in (
            "schema_version", "observer_transport", "part_order",
            "source_audio_sha256", "source_audio_durations_seconds",
            "derived_attachment_sha256", "attachment_prompt_sha256",
            "output_format", "structured_schema_version", "structured_schema_sha256",
        ) if key in binding},
        "transport_contract_version": binding.get("transport_contract_version", LEGACY_TRANSPORT_CONTRACT),
        "wire_schema_sha256": binding.get("wire_schema_sha256", LEGACY_WIRE_SCHEMA_SHA256),
        "model": agy.get("model") if isinstance(agy, Mapping) else binding.get("model"),
        "conversion": ffmpeg.get("conversion") if isinstance(ffmpeg, Mapping) else binding.get("conversion"),
    }
    # Preserve unfamiliar fields as guards. Only this deployed producer's
    # known execution telemetry and duplicate aliases may disappear.
    known = {
        "schema_version", "observer_transport", "part_order", "source_audio_sha256",
        "source_audio_durations_seconds", "derived_attachment_sha256", "attachment_prompt_sha256",
        "output_format", "structured_schema_version", "structured_schema_sha256",
        "transport_contract_version", "wire_schema_sha256", "model", "conversion",
        "source_audio_hashes", "source_durations_seconds", "derived_attachment_hashes",
        "agy", "ffmpeg", "adapter_source_sha256", "job_dir", "structured_log_filename",
    }
    protocol.update({key: item for key, item in binding.items() if key not in known})
    return protocol


def _protocol_identity(value: object) -> dict[str, object] | None:
    """Compare protocols after validating each identity's original hash chain."""
    identity = _runtime_identity(value)
    if identity is None:
        return None
    result = {key: identity[key] for key in (
        "provider", "model", "transport", "observer_prompt_sha256", "cpa_prompt_sha256",
        "cpa_call_identity", "cpa_call_identity_sha256",
    )}
    result["response_adapter_contract_version"] = identity.get(
        "response_adapter_contract_version", LEGACY_RESPONSE_ADAPTER_CONTRACT)
    result["observer_schema"] = identity.get("observer_schema", "nested-media-acoustic-observation.v1")
    result["cpa_schema"] = identity.get("cpa_schema", "nested-media-acoustic-cpa-judgement.v1")
    if "transport_binding" in identity:
        result["transport_binding"] = _transport_protocol(identity["transport_binding"])
    return result


_IDENTITY_KEY = re.compile(r"[A-Za-z0-9_.-]{1,80}\Z")
_IDENTITY_SECRET_KEY = re.compile(r"(?i)(?:api[_-]?key|authorization|credential|cookie|password|secret|token)")
_IDENTITY_STRING_MAX = 256
_IDENTITY_PATH_MAX = 4096
_IDENTITY_PATH_KEYS = frozenset({"argv", "binary", "cwd", "directory", "job_dir", "path"})


def _identity_path_context(key: str) -> bool:
    return key in _IDENTITY_PATH_KEYS or key.endswith(("_dir", "_path"))


def _is_absolute_identity_path(value: str) -> bool:
    return value == value.strip() and Path(value).is_absolute()


def _safe_identity_value(value: object, *, allow_long_paths: bool = False) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not _IDENTITY_KEY.fullmatch(key)
                or _IDENTITY_SECRET_KEY.search(key)
            ):
                raise NestedMediaAcousticObserverError("RUNTIME_IDENTITY_INVALID")
            result[key] = _safe_identity_value(
                item, allow_long_paths=_identity_path_context(key)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_identity_value(item, allow_long_paths=allow_long_paths) for item in value]
    if isinstance(value, str):
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
            raise NestedMediaAcousticObserverError("RUNTIME_IDENTITY_INVALID")
        is_path = allow_long_paths and _is_absolute_identity_path(value)
        if len(value) > (_IDENTITY_PATH_MAX if is_path else _IDENTITY_STRING_MAX):
            raise NestedMediaAcousticObserverError("RUNTIME_IDENTITY_INVALID")
        return value if is_path else value.strip()
    if value is None or isinstance(value, (bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise NestedMediaAcousticObserverError("RUNTIME_IDENTITY_INVALID")
        return value
    raise NestedMediaAcousticObserverError("RUNTIME_IDENTITY_INVALID")


def _normalize_cpa_identity(value: object) -> dict[str, object]:
    """Keep the existing non-secret CPA cache identity bound to the request."""

    if not isinstance(value, Mapping):
        raise NestedMediaAcousticObserverError("RUNTIME_IDENTITY_INVALID")
    transport = value.get("transport")
    models = value.get("models")
    effort = value.get("effort")
    if (
        not isinstance(transport, str)
        or not transport.strip()
        or not isinstance(models, (list, tuple))
        or not models
        or any(not isinstance(model, str) or not model.strip() for model in models)
        or not isinstance(effort, str)
        or not effort.strip()
    ):
        raise NestedMediaAcousticObserverError("RUNTIME_IDENTITY_INVALID")
    normalized = _safe_identity_value(value)
    if not isinstance(normalized, dict):
        raise NestedMediaAcousticObserverError("RUNTIME_IDENTITY_INVALID")
    normalized["transport"] = transport.strip()
    normalized["models"] = [model.strip() for model in models]
    normalized["effort"] = effort.strip()
    try:
        json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise NestedMediaAcousticObserverError("RUNTIME_IDENTITY_INVALID") from exc
    return normalized


def _canonical_hash(value: object) -> str:
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise NestedMediaAcousticObserverError("JSON_EVIDENCE_UNSERIALIZABLE") from exc
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def _normal_hash(value: object, *, code: str = "HASH_INVALID") -> str:
    if not isinstance(value, str):
        raise NestedMediaAcousticObserverError(code)
    raw = value.removeprefix("sha256:").lower()
    if len(raw) != 64 or any(char not in "0123456789abcdef" for char in raw):
        raise NestedMediaAcousticObserverError(code)
    return "sha256:" + raw


def _runtime_identity(value: object) -> dict[str, object] | None:
    """Normalize the non-secret runtime contract used by request caching.

    Old evidence predates this identity.  It may remain historical evidence,
    but it must never be promoted to a new typed PASS, so ``None`` is retained
    as an explicit missing identity rather than silently accepting a default.
    """

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise NestedMediaAcousticObserverError("RUNTIME_IDENTITY_INVALID")
    normalized: dict[str, object] = {}
    fields = _RUNTIME_IDENTITY_FIELDS
    if "response_adapter_contract_version" in value:
        fields = tuple(field for field in fields if field not in (
            "transport_module", "transport_module_sha256")) + (
                "response_adapter_contract_version", "observer_schema", "cpa_schema")
    for field in fields:
        raw = value.get(field)
        if not isinstance(raw, str):
            raise NestedMediaAcousticObserverError("RUNTIME_IDENTITY_INVALID")
        if field.endswith("_sha256"):
            normalized[field] = _normal_hash(raw, code="RUNTIME_IDENTITY_INVALID")
        else:
            normalized_value = _safe_identity_value(raw)
            if not isinstance(normalized_value, str) or not normalized_value:
                raise NestedMediaAcousticObserverError("RUNTIME_IDENTITY_INVALID")
            normalized[field] = normalized_value
    cpa_identity = _normalize_cpa_identity(value.get("cpa_call_identity"))
    cpa_identity_sha = _canonical_hash(cpa_identity)
    if _normal_hash(value.get("cpa_call_identity_sha256"), code="RUNTIME_IDENTITY_INVALID") != cpa_identity_sha:
        raise NestedMediaAcousticObserverError("RUNTIME_IDENTITY_INVALID")
    normalized["cpa_call_identity"] = cpa_identity
    normalized["cpa_call_identity_sha256"] = cpa_identity_sha
    # Historical source-signed identities retain their original hash chain.
    # Modern identities use the explicit contract, even if a caller attaches
    # legacy execution telemetry alongside the protocol fields.
    if "response_adapter_sha256" in value and "response_adapter_contract_version" not in value:
        normalized["response_adapter_sha256"] = _normal_hash(
            value["response_adapter_sha256"], code="RUNTIME_IDENTITY_INVALID"
        )
    if "transport_binding" in value:
        transport_binding = _safe_identity_value(value["transport_binding"])
        if not isinstance(transport_binding, Mapping):
            raise NestedMediaAcousticObserverError("RUNTIME_IDENTITY_INVALID")
        if "response_adapter_contract_version" in value:
            transport_binding = _transport_protocol(transport_binding)
        normalized["transport_binding"] = transport_binding
    identity_sha = _canonical_hash(normalized)
    supplied = value.get("identity_sha256")
    if supplied is not None and _normal_hash(supplied, code="RUNTIME_IDENTITY_INVALID") != identity_sha:
        raise NestedMediaAcousticObserverError("RUNTIME_IDENTITY_INVALID")
    normalized["identity_sha256"] = identity_sha
    return normalized


def _file_hash(path: Path, *, code: str) -> str:
    try:
        digest = _evidence_file_sha256(path)
    except OSError as exc:
        raise NestedMediaAcousticObserverError(code) from exc
    return "sha256:" + digest


def _verify_file(
    path: str | Path,
    expected: object,
    *,
    unavailable: str,
    mismatch: str,
) -> tuple[Path, str]:
    candidate = Path(path)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise NestedMediaAcousticObserverError(unavailable) from exc
    if candidate.is_symlink() or not resolved.is_file():
        raise NestedMediaAcousticObserverError(unavailable)
    wanted = _normal_hash(expected, code=mismatch)
    actual = _file_hash(resolved, code=unavailable)
    if actual != wanted:
        raise NestedMediaAcousticObserverError(mismatch)
    return resolved, actual


def _interval(value: object, *, code: str, integer: bool = False) -> list[int | float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        raise NestedMediaAcousticObserverError(code)
    values = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise NestedMediaAcousticObserverError(code)
        number = float(item)
        if not math.isfinite(number):
            raise NestedMediaAcousticObserverError(code)
        values.append(number)
    if values[0] < 0 or values[1] <= values[0] or integer and any(not v.is_integer() for v in values):
        raise NestedMediaAcousticObserverError(code)
    return [int(v) for v in values] if integer else values


def _cue_indexes(value: object, *, code: str) -> list[int | str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise NestedMediaAcousticObserverError(code)
    indexes: list[int | str] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            raise NestedMediaAcousticObserverError(code)
        if isinstance(item, str) and (
            not item.strip() or len(item) > 180 or "\n" in item or "\r" in item
        ):
            raise NestedMediaAcousticObserverError(code)
        if item in indexes:
            raise NestedMediaAcousticObserverError(code)
        indexes.append(item)
    return indexes


def _target_binding(
    *,
    interval: list[int],
    frame_ms: int,
    target_interval_ms: object,
    target_cue_indexes: object,
) -> tuple[list[int] | None, list[int | str] | None]:
    if target_interval_ms is None and target_cue_indexes is None:
        return None, None
    if target_interval_ms is None or target_cue_indexes is None:
        raise NestedMediaAcousticObserverError("TARGET_BINDING_INCOMPLETE")
    target_interval = [
        int(item)
        for item in _interval(target_interval_ms, code="TARGET_INTERVAL_INVALID", integer=True)
    ]
    if target_interval[0] < interval[0] or target_interval[1] > interval[1]:
        raise NestedMediaAcousticObserverError("TARGET_INTERVAL_OUTSIDE_CONTEXT")
    if not target_interval[0] <= frame_ms < target_interval[1]:
        raise NestedMediaAcousticObserverError("TARGET_FRAME_INTERVAL_MISMATCH")
    target_cues = _cue_indexes(target_cue_indexes, code="TARGET_CUE_INDEXES_INVALID")
    return target_interval, target_cues


def observer_target_scope(
    *, interval_ms: Sequence[int], target_interval_ms: object,
    target_cue_indexes: object, target_audio_window_ms: object = None,
) -> dict[str, object]:
    """Map the outer cue clock to the actual third attachment's clock."""
    if target_interval_ms is None and target_cue_indexes is None:
        return {}
    if target_interval_ms is None or target_cue_indexes is None:
        raise NestedMediaAcousticObserverError("TARGET_SCOPE_INVALID")
    context = [int(item) for item in _interval(interval_ms, code="TARGET_SCOPE_INVALID", integer=True)]
    target = [int(item) for item in _interval(target_interval_ms, code="TARGET_SCOPE_INVALID", integer=True)]
    window = context if target_audio_window_ms is None else [
        int(item) for item in _interval(target_audio_window_ms, code="TARGET_SCOPE_INVALID", integer=True)
    ]
    if not context[0] <= window[0] <= target[0] < target[1] <= window[1] <= context[1]:
        raise NestedMediaAcousticObserverError("TARGET_SCOPE_INVALID")
    return {
        "target_interval_ms": target,
        "target_cue_indexes": _cue_indexes(target_cue_indexes, code="TARGET_SCOPE_INVALID"),
        "target_audio_window_ms": window,
        "target_audio_offset_ms": [target[0] - window[0], target[1] - window[0]],
    }


def bound_observer_prompt(
    *,
    observer_prompt: str,
    interval_ms: Sequence[int],
    target_interval_ms: object,
    target_cue_indexes: object,
    target_audio_window_ms: object = None,
    playback_locator: Mapping[str, object] | None = None,
) -> str:
    """Build the exact prompt suffix used by the normal observer runtime.

    A transport that writes an attachment prompt before dispatch must hash the
    same target-bound bytes that ``observe_audio`` will receive.  Keeping this
    small helper beside the request validation prevents a second transport
    from reimplementing target-scope serialization.
    """

    if not isinstance(observer_prompt, str) or not observer_prompt.strip():
        raise NestedMediaAcousticObserverError("OBSERVER_PROMPT_INVALID")
    scope = observer_target_scope(
        interval_ms=interval_ms, target_interval_ms=target_interval_ms,
        target_cue_indexes=target_cue_indexes, target_audio_window_ms=target_audio_window_ms,
    )
    if not scope:
        return observer_prompt
    clock_instruction = (
        "\nPart 3 starts at local time 0. Listen ONLY at target_audio_offset_ms "
        "in part 3. target_interval_ms and target_audio_window_ms use the OUTER "
        "recording clock; never use them as attachment playback times. "
        "Return target_audio_offset_ms as well as the outer target_interval_ms "
        "and target_cue_indexes. If you cannot locate that local interval, "
        "return UNKNOWN. Part 2 has its own clock; its ending time cannot "
        "establish absence in part 3.\n"
    )
    result = observer_prompt + clock_instruction + "TARGET_SCOPE_JSON:\n" + json.dumps(
        scope, sort_keys=True, separators=(",", ":")
    )
    if playback_locator is not None:
        if (
            not isinstance(playback_locator, Mapping)
            or playback_locator.get("schema_version") != "short-reference-playback-locator.v1"
            or playback_locator.get("status") != "LOCATED"
            or playback_locator.get("authority") != "EVIDENCE_ONLY"
            or playback_locator.get("time_alignment_performed") is not False
        ):
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_PLAYBACK_LOCATOR_INVALID")
        result += (
            "\nPLAYBACK_LOCATOR_HINT_JSON:\n"
            + json.dumps(
                {
                    "instruction": (
                        "Use this deterministic hint only to focus listening on the matched "
                        "target portion. It is not a linear mapping, source-playback proof, "
                        "speaker identity, caption authority, or permission to change text."
                    ),
                    "locator": dict(playback_locator),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return result


def _request_target_binding(
    request: Mapping[str, object],
) -> tuple[list[int] | None, list[int | str] | None]:
    return _target_binding(
        interval=[
            int(item)
            for item in _interval(request.get("interval_ms"), code="INTERVAL_INVALID", integer=True)
        ],
        frame_ms=request.get("caption_frame", {}).get("frame_ms")
        if isinstance(request.get("caption_frame"), Mapping)
        else -1,
        target_interval_ms=request.get("target_interval_ms"),
        target_cue_indexes=request.get("target_cue_indexes"),
    )


def _validate_target_echo(
    result: Mapping[str, object], request: Mapping[str, object], *, code: str
) -> None:
    target_interval, target_cues = _request_target_binding(request)
    if target_interval is None:
        return
    frame = request.get("caption_frame")
    if not isinstance(frame, Mapping):
        raise NestedMediaAcousticObserverError(code)
    try:
        response_interval, response_cues = _target_binding(
            interval=[
                int(item)
                for item in _interval(request.get("interval_ms"), code=code, integer=True)
            ],
            frame_ms=frame.get("frame_ms"),
            target_interval_ms=result.get("target_interval_ms"),
            target_cue_indexes=result.get("target_cue_indexes"),
        )
    except NestedMediaAcousticObserverError as exc:
        raise NestedMediaAcousticObserverError(code) from exc
    if (
        response_interval != target_interval
        or response_cues != target_cues
    ):
        raise NestedMediaAcousticObserverError(code)


def _json_mapping(path: Path, *, code: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise NestedMediaAcousticObserverError(code)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NestedMediaAcousticObserverError(code) from exc
    if not isinstance(value, Mapping):
        raise NestedMediaAcousticObserverError(code)
    return dict(value)


def _identity(value: object, *, code: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise NestedMediaAcousticObserverError(code)
    raw_sha = value.get("sha256") or value.get("media_sha256") or value.get("source_media_sha256")
    result = {"sha256": _normal_hash(raw_sha, code=code)}
    names = ("bvid", "cid", "source_id", "source_uri", "url", "path", "name")
    if not any(isinstance(value.get(name), str) and value[name].strip() for name in names):
        raise NestedMediaAcousticObserverError(code)
    result.update({name: value[name] for name in names if isinstance(value.get(name), str) and value[name].strip()})
    return result


def _mapping_info(
    mapping: Mapping[str, object], *, outer_sha: str, source_sha: str, interval: list[int]
) -> dict[str, object]:
    status = mapping.get("status")
    media, lineage = mapping.get("media"), mapping.get("lineage_mapping")
    if not isinstance(status, str) or not status.strip() or status.upper() in {"FAILED", "ERROR", "INVALID"}:
        raise NestedMediaAcousticObserverError("MAPPING_RECEIPT_INVALID")
    if not isinstance(media, Mapping) or not isinstance(lineage, Mapping):
        raise NestedMediaAcousticObserverError("MAPPING_RECEIPT_INVALID")
    outer = _identity(media.get("outer"), code="MAPPING_OUTER_IDENTITY_INVALID")
    inner = _identity(media.get("inner"), code="MAPPING_SOURCE_IDENTITY_INVALID")
    if outer["sha256"] != outer_sha:
        raise NestedMediaAcousticObserverError("MAPPING_OUTER_HASH_MISMATCH")
    if inner["sha256"] != source_sha:
        raise NestedMediaAcousticObserverError("MAPPING_SOURCE_HASH_MISMATCH")
    outer_window = _interval(lineage.get("outer_window_ms"), code="MAPPING_OUTER_WINDOW_INVALID")
    source_value = lineage.get("inner_window_ms") or lineage.get("source_window_ms") or lineage.get("independent_source_window_ms")
    source_window = _interval(source_value, code="MAPPING_SOURCE_WINDOW_INVALID")
    lag_value = lineage.get("lag_ms")
    lag = float(lag_value) if isinstance(lag_value, (int, float)) and not isinstance(lag_value, bool) else math.nan
    if not math.isfinite(lag):
        raise NestedMediaAcousticObserverError("MAPPING_LAG_INVALID")
    if any(abs(float(a) - b) > 1e-6 for a, b in zip(outer_window, interval)):
        raise NestedMediaAcousticObserverError("MAPPING_OUTER_WINDOW_MISMATCH")
    if any(abs(float(outer_window[i]) - float(source_window[i]) - lag) > 1e-3 for i in range(2)):
        raise NestedMediaAcousticObserverError("MAPPING_LAG_WINDOW_MISMATCH")
    return {
        "schema_version": mapping.get("schema_version"), "status": status,
        "outer_window_ms": outer_window, "source_window_ms": source_window, "lag_ms": lag,
        "outer_media_sha256": outer_sha, "independent_source_media_sha256": source_sha,
        "outer_identity": outer, "source_identity": inner,
    }


def _binding_file(
    payload: Mapping[str, object],
    field: str,
    *,
    expected_path: Path,
    expected_sha: str,
    code: str,
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    value = payload.get(field)
    if not isinstance(value, Mapping):
        raise NestedMediaAcousticObserverError(code)
    path_value = value.get("path")
    if not isinstance(path_value, str) or not path_value:
        raise NestedMediaAcousticObserverError(code)
    try:
        if Path(path_value).resolve() != expected_path.resolve():
            raise NestedMediaAcousticObserverError(code)
    except (OSError, RuntimeError) as exc:
        raise NestedMediaAcousticObserverError(code) from exc
    if _normal_hash(value.get("sha256"), code=code) != expected_sha:
        raise NestedMediaAcousticObserverError(code)
    result = {"path": str(expected_path), "sha256": expected_sha}
    if extra:
        for key, expected_value in extra.items():
            if key in value and value.get(key) != expected_value:
                raise NestedMediaAcousticObserverError(code)
        result.update(dict(extra))
    return result


def _external_source_identity(
    payload: Mapping[str, object], *, source_sha: str,
) -> tuple[dict[str, object], bool]:
    """Normalize the external-source union without weakening the legacy branch.

    Bilibili voice references continue to use the top-level ``bvid``/``cid``
    pair.  An official external audio reference carries a URI and the hash of
    the complete downloaded source file.  The URI is provenance only; the
    local file and its bytes are still checked by ``_binding_file``.
    """

    source_identity = payload.get("source_identity")
    has_legacy = payload.get("bvid") is not None or payload.get("cid") is not None
    if source_identity is not None:
        if has_legacy or not isinstance(source_identity, Mapping):
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_SOURCE_IDENTITY_INVALID")
        kind = source_identity.get("kind")
        uri = source_identity.get("source_uri")
        if kind != "OFFICIAL_EXTERNAL_AUDIO" or not isinstance(uri, str) or not uri.strip():
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_SOURCE_IDENTITY_INVALID")
        parsed = urlparse(uri.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or any(
            ord(char) < 0x20 or char.isspace() for char in uri
        ):
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_SOURCE_IDENTITY_INVALID")
        full_sha = _normal_hash(
            source_identity.get("source_sha256"),
            code="VOICE_REFERENCE_SOURCE_IDENTITY_INVALID",
        )
        if full_sha != source_sha:
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_SOURCE_IDENTITY_MISMATCH")
        source_media = payload.get("source_media")
        if not isinstance(source_media, Mapping):
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_SOURCE_IDENTITY_INVALID")
        media_uri = source_media.get("source_uri")
        media_sha = _normal_hash(
            source_media.get("source_sha256"),
            code="VOICE_REFERENCE_SOURCE_IDENTITY_INVALID",
        )
        if media_uri != uri.strip() or media_sha != full_sha:
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_SOURCE_IDENTITY_MISMATCH")
        return {
            "kind": "OFFICIAL_EXTERNAL_AUDIO",
            "source_uri": uri.strip(),
            "source_sha256": full_sha,
        }, True
    bvid = payload.get("bvid")
    cid = payload.get("cid")
    if not isinstance(bvid, str) or not bvid.strip():
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_SOURCE_IDENTITY_INVALID")
    if not isinstance(cid, (str, int)) or isinstance(cid, bool) or str(cid).strip() == "":
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_SOURCE_IDENTITY_INVALID")
    return {"bvid": bvid.strip(), "cid": str(cid)}, False


def _validate_voice_reference_binding(
    *,
    binding_path: str | Path,
    binding_sha256: object,
    candidate_id: str,
    observation_id: str,
    outer: tuple[Path, str],
    srt: tuple[Path, str],
    frame: tuple[Path, str],
    source_media: tuple[Path, str],
    host: tuple[Path, str],
    source_audio: tuple[Path, str] | None,
    target_audio: tuple[Path, str] | None,
    interval: list[int],
    frame_ms: int,
    target_interval: list[int] | None,
    target_cues: list[int | str] | None,
    target_audio_window_ms: Sequence[int] | None = None,
) -> tuple[Path, str, dict[str, object]]:
    """Validate an explicit unaligned voice-reference binding.

    This deliberately has no mapping/NCC input.  The reference media may be a
    different time window from the current outer context; only the current
    target, file identities and the reference source identity are bound here.
    """

    path, sha = _verify_file(
        binding_path, binding_sha256,
        unavailable="VOICE_REFERENCE_BINDING_UNAVAILABLE",
        mismatch="VOICE_REFERENCE_BINDING_HASH_MISMATCH",
    )
    payload = _json_mapping(path, code="VOICE_REFERENCE_BINDING_INVALID")
    if (
        payload.get("schema_version") != VOICE_REFERENCE_BINDING_SCHEMA
        or payload.get("status") != VOICE_REFERENCE_BINDING_STATUS
        or payload.get("mode") != VOICE_REFERENCE_MODE
        or payload.get("candidate_id") != candidate_id
        or payload.get("observation_id") != observation_id
        or payload.get("mutation_authorized") is not False
        or payload.get("time_alignment_performed") is not False
        or payload.get("source_subtraction_authorized") is not False
        or payload.get("caption_dedup_authority") is not False
        or payload.get("source_role_authority") is not False
    ):
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_BINDING_INVALID")
    if payload.get("mapping_receipt") not in (None, {}):
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_MAPPING_FORBIDDEN")
    if any(payload.get(field) is not None for field in ("lag_ms", "ncc", "mapping_pass")):
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_MAPPING_FORBIDDEN")
    source_identity, external_source = _external_source_identity(
        payload, source_sha=source_media[1]
    )
    try:
        context = [int(item) for item in _interval(
            payload.get("outer_context_interval_ms"),
            code="VOICE_REFERENCE_CONTEXT_INTERVAL_INVALID", integer=True,
        )]
        if context != interval:
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_CONTEXT_INTERVAL_MISMATCH")
        # Complete reference audio can end between integer milliseconds.  It
        # has its own clock; never truncate its tail to the cue/grid clock.
        reference_window = _interval(
            payload.get("reference_window_ms"),
            code="VOICE_REFERENCE_WINDOW_INVALID",
        )
        if all(value.is_integer() for value in reference_window):
            # Preserve existing integer-window request/cache bytes.
            reference_window = [int(value) for value in reference_window]
        if target_interval is None or target_cues is None:
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_TARGET_REQUIRED")
        bound_target = [int(item) for item in _interval(
            payload.get("target_interval_ms"),
            code="VOICE_REFERENCE_TARGET_INTERVAL_INVALID", integer=True,
        )]
        bound_cues = _cue_indexes(
            payload.get("target_cue_indexes"), code="VOICE_REFERENCE_TARGET_CUES_INVALID"
        )
    except NestedMediaAcousticObserverError:
        raise
    if bound_target != target_interval or bound_cues != target_cues:
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_TARGET_SCOPE_MISMATCH")
    if len(bound_cues) != 1:
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_TARGET_SINGLE_CUE_REQUIRED")
    if not bound_target[0] <= frame_ms < bound_target[1]:
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_TARGET_FRAME_MISMATCH")

    # New automatic preparations carry the actual third-attachment window and
    # its cue-local offset independently from the exact role cue. Older
    # explicit bindings predate these fields and remain valid as narrow
    # target-only attachments. A partial modern pair is never inferred.
    binding_window_value = payload.get("target_audio_window_ms")
    binding_offset_value = payload.get("target_audio_offset_ms")
    if binding_window_value is None and binding_offset_value is None:
        binding_window = list(bound_target)
        binding_offset = [0, bound_target[1] - bound_target[0]]
        modern_audio_window = False
    elif binding_window_value is None or binding_offset_value is None:
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_TARGET_AUDIO_WINDOW_INVALID")
    else:
        try:
            binding_scope = observer_target_scope(
                interval_ms=context,
                target_interval_ms=bound_target,
                target_cue_indexes=bound_cues,
                target_audio_window_ms=binding_window_value,
            )
            binding_window = list(binding_scope["target_audio_window_ms"])
            binding_offset = [
                int(item) for item in _interval(
                    binding_offset_value,
                    code="VOICE_REFERENCE_TARGET_AUDIO_OFFSET_INVALID",
                    integer=True,
                )
            ]
        except NestedMediaAcousticObserverError as exc:
            if str(exc) in {"TARGET_SCOPE_INVALID", "TARGET_CUE_INDEXES_INVALID"}:
                raise NestedMediaAcousticObserverError(
                    "VOICE_REFERENCE_TARGET_AUDIO_WINDOW_INVALID"
                ) from exc
            raise
        expected_offset = [
            bound_target[0] - binding_window[0],
            bound_target[1] - binding_window[0],
        ]
        if binding_offset != expected_offset:
            raise NestedMediaAcousticObserverError(
                "VOICE_REFERENCE_TARGET_AUDIO_OFFSET_INVALID"
            )
        modern_audio_window = True

    if target_audio_window_ms is not None:
        try:
            requested_scope = observer_target_scope(
                interval_ms=context,
                target_interval_ms=bound_target,
                target_cue_indexes=bound_cues,
                target_audio_window_ms=target_audio_window_ms,
            )
            requested_window = list(requested_scope["target_audio_window_ms"])
        except NestedMediaAcousticObserverError as exc:
            raise NestedMediaAcousticObserverError(
                "VOICE_REFERENCE_TARGET_AUDIO_WINDOW_INVALID"
            ) from exc
        if requested_window != binding_window:
            raise NestedMediaAcousticObserverError(
                "VOICE_REFERENCE_TARGET_AUDIO_WINDOW_MISMATCH"
            )
    elif modern_audio_window:
        raise NestedMediaAcousticObserverError(
            "VOICE_REFERENCE_TARGET_AUDIO_WINDOW_REQUIRED"
        )

    normalized: dict[str, object] = {
        "schema_version": VOICE_REFERENCE_BINDING_SCHEMA,
        "status": VOICE_REFERENCE_BINDING_STATUS,
        "mode": VOICE_REFERENCE_MODE,
        "candidate_id": candidate_id,
        "observation_id": observation_id,
        "outer_media": _binding_file(
            payload, "outer_media", expected_path=outer[0], expected_sha=outer[1],
            code="VOICE_REFERENCE_OUTER_BINDING_MISMATCH",
        ),
        "final_srt": _binding_file(
            payload, "final_srt", expected_path=srt[0], expected_sha=srt[1],
            code="VOICE_REFERENCE_SRT_BINDING_MISMATCH",
        ),
        "caption_frame": _binding_file(
            payload, "caption_frame", expected_path=frame[0], expected_sha=frame[1],
            code="VOICE_REFERENCE_FRAME_BINDING_MISMATCH",
            extra={"frame_ms": frame_ms},
        ),
        "source_media": _binding_file(
            payload, "source_media", expected_path=source_media[0], expected_sha=source_media[1],
            code="VOICE_REFERENCE_SOURCE_BINDING_MISMATCH",
            extra=(
                {
                    "source_uri": source_identity["source_uri"],
                    "source_sha256": source_identity["source_sha256"],
                }
                if external_source
                else {"bvid": source_identity["bvid"], "cid": source_identity["cid"]}
            ),
        ),
        "host_enrollment": _binding_file(
            payload, "host_enrollment", expected_path=host[0], expected_sha=host[1],
            code="VOICE_REFERENCE_HOST_BINDING_MISMATCH",
        ),
        "outer_context_interval_ms": context,
        "reference_window_ms": reference_window,
        "target_interval_ms": bound_target,
        "target_cue_indexes": bound_cues,
        "time_alignment_performed": False,
        "source_subtraction_authorized": False,
        "caption_dedup_authority": False,
        "source_role_authority": False,
        "mutation_authorized": False,
    }
    if modern_audio_window:
        normalized["target_audio_window_ms"] = binding_window
        normalized["target_audio_offset_ms"] = binding_offset
    if external_source:
        normalized["source_identity"] = dict(source_identity)
    if source_audio is not None:
        normalized["source_audio"] = _binding_file(
            payload, "source_audio", expected_path=source_audio[0], expected_sha=source_audio[1],
            code="VOICE_REFERENCE_SOURCE_AUDIO_BINDING_MISMATCH",
        )
    if target_audio is not None:
        normalized["target_audio"] = _binding_file(
            payload, "target_audio", expected_path=target_audio[0], expected_sha=target_audio[1],
            code="VOICE_REFERENCE_TARGET_AUDIO_BINDING_MISMATCH",
        )
    persisted_locator = payload.get("playback_locator")
    if persisted_locator is not None:
        if (
            not external_source
            or source_audio is None
            or target_audio is None
            or not isinstance(persisted_locator, Mapping)
        ):
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_PLAYBACK_LOCATOR_INVALID")
        locator_context = {
            "candidate_id": candidate_id,
            "observation_id": observation_id,
            "outer_media": {"path": str(outer[0]), "sha256": outer[1]},
            "final_srt": {"path": str(srt[0]), "sha256": srt[1]},
            "caption_frame": {
                "path": str(frame[0]), "sha256": frame[1], "frame_ms": frame_ms,
            },
            "target_interval_ms": list(bound_target),
            "target_cue_indexes": list(bound_cues),
        }
        try:
            validated_locator = validate_locator(
                persisted_locator,
                reference_audio_path=source_audio[0], reference_audio_sha256=source_audio[1],
                target_audio_path=target_audio[0], target_audio_sha256=target_audio[1],
                target_audio_window_ms=binding_window, target_interval_ms=bound_target,
                reference_source_path=source_media[0], reference_source_sha256=source_media[1],
                source_uri=str(source_identity["source_uri"]),
                binding_context=locator_context,
            )
        except (ShortReferencePlaybackLocatorError, KeyError, TypeError, ValueError) as exc:
            raise NestedMediaAcousticObserverError(
                "VOICE_REFERENCE_PLAYBACK_LOCATOR_INVALID"
            ) from exc
        normalized["playback_locator"] = validated_locator
    return path, sha, normalized


def _classify(value: Mapping[str, object]) -> str:
    source, host, overlap = (value.get(role) for role in _ROLES)
    if source == "PRESENT" and host == "ABSENT" and overlap == "ABSENT":
        return "source_media"
    if source == "PRESENT" and overlap == "PRESENT":
        return "overlap"
    if source == "ABSENT" and host == "PRESENT" and overlap == "ABSENT":
        return "host"
    return "unknown"


def _validate_states(value: Mapping[str, object], code: str) -> None:
    if any(value.get(role) not in _STATES for role in _ROLES):
        raise NestedMediaAcousticObserverError(code)


def _observation(
    value: object,
    *,
    candidate: str,
    observation_id: str,
    request_sha: str,
    request: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise NestedMediaAcousticObserverError("OBSERVER_RESPONSE_INVALID")
    result = dict(value)
    if (
        result.get("schema_version") != OBSERVER_SCHEMA
        or result.get("status") not in {"OBSERVED", "UNKNOWN"}
        or result.get("authority") != "EVIDENCE_ONLY"
        or result.get("candidate_id") != candidate
        or result.get("observation_id") != observation_id
        or result.get("evidence") in (None, {}, [])
    ):
        raise NestedMediaAcousticObserverError("OBSERVER_RESPONSE_INVALID")
    if _normal_hash(result.get("request_sha256"), code="OBSERVER_REQUEST_BINDING_INVALID") != request_sha:
        raise NestedMediaAcousticObserverError("OBSERVER_REQUEST_BINDING_INVALID")
    _validate_states(result, "OBSERVER_STATES_INVALID")
    if request is not None:
        _validate_target_echo(result, request, code="OBSERVER_TARGET_BINDING_MISMATCH")
    return result


def _judgement(
    value: object, *, candidate: str, observation_id: str, request_sha: str,
    observation: Mapping[str, object], request: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise NestedMediaAcousticObserverError("CPA_RESPONSE_INVALID")
    result = dict(value)
    if (
        result.get("schema_version") != CPA_SCHEMA
        or result.get("status") != "JUDGED"
        or result.get("authority") not in {"CPA", "CPA_JUDGE"}
        or result.get("candidate_id") != candidate
        or result.get("observation_id") != observation_id
        or result.get("evidence") in (None, {}, [])
    ):
        raise NestedMediaAcousticObserverError("CPA_RESPONSE_INVALID")
    if _normal_hash(result.get("request_sha256"), code="CPA_REQUEST_BINDING_INVALID") != request_sha:
        raise NestedMediaAcousticObserverError("CPA_REQUEST_BINDING_INVALID")
    if _normal_hash(result.get("observation_sha256"), code="CPA_OBSERVATION_BINDING_INVALID") != _canonical_hash(observation):
        raise NestedMediaAcousticObserverError("CPA_OBSERVATION_BINDING_INVALID")
    _validate_states(result, "CPA_STATES_INVALID")
    if any(result[role] not in {observation[role], "UNKNOWN"} for role in _ROLES):
        raise NestedMediaAcousticObserverError("CPA_STATE_NOT_MONOTONIC")
    if result.get("final_class", _classify(result)) != _classify(result):
        raise NestedMediaAcousticObserverError("CPA_CLASS_INVALID")
    if request is not None:
        _validate_target_echo(result, request, code="CPA_TARGET_BINDING_MISMATCH")
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_http_status(error: Exception) -> int | None:
    for name in ("status_code", "http_status"):
        value = getattr(error, name, None)
        if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
            return value
    return None


def _result(
    *, status: str, request_sha: str, request_path: Path, request_dir: Path,
    reason: str, replayed: bool, observer_path: Path | None = None,
    cpa_path: Path | None = None, receipt_path: Path | None = None,
    receipt_sha: str | None = None, final_class: str = "unknown",
) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": RESULT_SCHEMA, "status": status, "reason_code": reason,
        "request_sha256": request_sha, "request_path": str(request_path),
        "request_dir": str(request_dir), "replayed": replayed, "final_class": final_class,
        "authority": "EVIDENCE_ONLY", "mutation_authorized": False,
    }
    if observer_path is not None:
        result["observer_response_path"] = str(observer_path)
    if cpa_path is not None:
        result["cpa_response_path"] = str(cpa_path)
    if receipt_path is not None and receipt_sha is not None:
        result["receipt_binding"] = {"path": str(receipt_path), "sha256": receipt_sha}
    return result


def _load_result(path: Path, request_sha: str, request_path: Path, request_dir: Path) -> dict[str, object] | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping) or value.get("request_sha256") != request_sha:
        return None
    result = dict(value)
    result.update(request_path=str(request_path), request_dir=str(request_dir), replayed=True)
    return result


def _failure(
    *, request: Path, request_dir: Path, state: Path, result_path: Path,
    request_sha: str, reason: str, error: Exception,
    observer_path: Path | None = None, cpa_path: Path | None = None,
) -> dict[str, object]:
    error_path = request_dir / "error.json"
    error_document: dict[str, object] = {
        "schema_version": "nested-media-acoustic-observer-error.v1",
        "request_sha256": request_sha, "reason_code": reason,
        "error_type": type(error).__name__, "created_at": _now(),
    }
    http_status = _safe_http_status(error)
    if http_status is not None:
        error_document["http_status"] = http_status
    atomic_write_json(error_path, error_document)
    value = _result(
        status="EVIDENCE_ONLY", request_sha=request_sha, request_path=request,
        request_dir=request_dir, reason=reason, replayed=False,
        observer_path=observer_path, cpa_path=cpa_path,
    )
    value["error_path"] = str(error_path)
    atomic_write_json(result_path, value)
    atomic_write_json(state, {
        "schema_version": STATE_SCHEMA, "status": "FAILED", "request_sha256": request_sha,
        "reason_code": reason, "result_path": str(result_path), "updated_at": _now(),
    })
    return value


def _dispatch_reason(default: str, error: Exception) -> str:
    code = str(error)
    if code in {"OBSERVER_TARGET_BINDING_MISMATCH", "CPA_TARGET_BINDING_MISMATCH"}:
        return code
    return default


def _complete(
    value: dict[str, object], *, result_path: Path, state: Path, request_sha: str,
    extra_state: Mapping[str, object] | None = None,
) -> dict[str, object]:
    atomic_write_json(result_path, value)
    atomic_write_json(state, {
        "schema_version": STATE_SCHEMA, "status": "COMPLETE", "request_sha256": request_sha,
        "result_path": str(result_path), "completed_at": _now(), **(dict(extra_state or {})),
    })
    return value


def _validate_cached_pass(
    *,
    receipt: Mapping[str, object],
    receipt_path: Path,
    request_path: Path,
    observer_path: Path,
    cpa_path: Path,
    request: Mapping[str, object],
    request_sha: str,
    candidate_id: str,
    observation_id: str,
    interval: list[int],
    target_interval: list[int] | None,
    target_cue_indexes: list[int | str] | None,
    files: Mapping[str, tuple[Path, str]],
    mapping_info: Mapping[str, object] | None,
    observer_mode: str | None = None,
    voice_reference_info: Mapping[str, object] | None = None,
) -> str:
    runtime_identity = request.get("runtime_identity")
    if not isinstance(runtime_identity, Mapping):
        raise NestedMediaAcousticObserverError("CACHED_RECEIPT_INVALID")
    normalized_identity = _runtime_identity(runtime_identity)
    if normalized_identity is None:
        raise NestedMediaAcousticObserverError("CACHED_RECEIPT_INVALID")
    saved_request = _json_mapping(request_path, code="CACHED_RECEIPT_INVALID")
    request_body = dict(saved_request)
    saved_request_sha = request_body.pop("request_sha256", None)
    if (
        saved_request_sha != request_sha
        or _canonical_hash(request_body) != request_sha
        or saved_request != dict(request)
    ):
        raise NestedMediaAcousticObserverError("CACHED_RECEIPT_INVALID")
    try:
        observed = _observation(
            _json_mapping(observer_path, code="CACHED_RECEIPT_INVALID"),
            candidate=candidate_id,
            observation_id=observation_id,
            request_sha=request_sha,
            request=request,
        )
        judged = _judgement(
            _json_mapping(cpa_path, code="CACHED_RECEIPT_INVALID"),
            candidate=candidate_id,
            observation_id=observation_id,
            request_sha=request_sha,
            observation=observed,
            request=request,
        )
    except Exception as exc:
        raise NestedMediaAcousticObserverError("CACHED_RECEIPT_INVALID") from exc
    evidence = receipt.get("evidence")
    bindings = receipt.get("bindings")
    if not isinstance(bindings, Mapping):
        raise NestedMediaAcousticObserverError("CACHED_RECEIPT_INVALID")
    expected_bindings = {
        "source_media_sha256": files["outer"][1],
        "final_srt_sha256": files["srt"][1],
        "caption_frame_sha256": files["frame"][1],
        "candidate_id": candidate_id,
        "observation_id": observation_id,
        "interval_ms": interval,
        "independent_source_media_sha256": files["source_media"][1],
        "host_audio_sha256": files["host"][1],
        "source_audio_sha256": files["source_audio"][1],
        "target_audio_sha256": files["target_audio"][1],
        "runtime_identity_sha256": normalized_identity["identity_sha256"],
    }
    if observer_mode == VOICE_REFERENCE_MODE:
        if not isinstance(voice_reference_info, Mapping):
            raise NestedMediaAcousticObserverError("CACHED_RECEIPT_INVALID")
        expected_bindings.update(
            observer_mode=VOICE_REFERENCE_MODE,
            voice_reference_binding_sha256=request.get("voice_reference_binding", {}).get("sha256")
            if isinstance(request.get("voice_reference_binding"), Mapping) else None,
        )
    else:
        if not isinstance(mapping_info, Mapping) or "mapping_receipt" not in request:
            raise NestedMediaAcousticObserverError("CACHED_RECEIPT_INVALID")
        expected_bindings["mapping_receipt_sha256"] = files["mapping"][1]
    if target_interval is not None and target_cue_indexes is not None:
        expected_bindings.update(
            target_interval_ms=target_interval,
            target_cue_indexes=target_cue_indexes,
        )
    target_fields_match = (
        target_interval is not None
        and target_cue_indexes is not None
        and receipt.get("target_interval_ms") == target_interval
        and receipt.get("target_cue_indexes") == target_cue_indexes
        and bindings.get("target_interval_ms") == target_interval
        and bindings.get("target_cue_indexes") == target_cue_indexes
    ) or (
        target_interval is None
        and target_cue_indexes is None
        and "target_interval_ms" not in receipt
        and "target_cue_indexes" not in receipt
        and "target_interval_ms" not in bindings
        and "target_cue_indexes" not in bindings
    )
    if not isinstance(evidence, Mapping):
        evidence_mapping_match = False
    elif observer_mode == VOICE_REFERENCE_MODE:
        evidence_mapping_match = (
            evidence.get("mapping") is None
            and evidence.get("voice_reference_binding") == dict(voice_reference_info or {})
        )
    else:
        evidence_mapping_match = evidence.get("mapping") == dict(mapping_info or {})
    if (
        not isinstance(evidence, Mapping)
        or not isinstance(bindings, Mapping)
        or any(bindings.get(key) != value for key, value in expected_bindings.items())
        or evidence.get("request_sha256") != request_sha
        or evidence.get("observer_response_sha256") != _canonical_hash(observed)
        or evidence.get("cpa_response_sha256") != _canonical_hash(judged)
        or evidence.get("observer_response_path") != str(observer_path)
        or evidence.get("cpa_response_path") != str(cpa_path)
        or not evidence_mapping_match
        or evidence.get("part_order") != request.get("part_order")
        or evidence.get("runtime_identity_sha256") != normalized_identity["identity_sha256"]
        or receipt.get("runtime_identity") != normalized_identity
        or receipt.get("runtime_identity_sha256") != normalized_identity["identity_sha256"]
        or receipt.get("source_media_sha256") != files["outer"][1]
        or receipt.get("final_srt_sha256") != files["srt"][1]
        or receipt.get("caption_frame_sha256") != files["frame"][1]
        or not target_fields_match
        or receipt.get("authority") != "EVIDENCE_ONLY"
        or not isinstance(receipt.get("accepted_at"), str)
        or not str(receipt.get("accepted_at")).strip()
        or any(receipt.get(role) != judged.get(role) for role in _ROLES)
        or any(receipt.get(role) == "UNKNOWN" for role in _ROLES)
        or receipt.get("final_class") != _classify(judged)
        or (
            observer_mode == VOICE_REFERENCE_MODE
            and (
                receipt.get("observer_mode") != VOICE_REFERENCE_MODE
                or receipt.get("voice_reference_binding") != dict(voice_reference_info or {})
                or receipt.get("method") != VOICE_REFERENCE_METHOD
                or request.get("observer_mode") != VOICE_REFERENCE_MODE
            )
        )
    ):
        raise NestedMediaAcousticObserverError("CACHED_RECEIPT_INVALID")
    return _file_hash(receipt_path, code="CACHED_RECEIPT_INVALID")


def _compatible_receipt_request(
    observation_dir: Path, request: Mapping[str, object],
) -> tuple[dict[str, object], Path] | None:
    """Locate immutable receipts by protocol, then use the ordinary validator.

    Never rewrite/re-sign a historical request or use its new projected hash
    as evidence. All original request/response/receipt bindings are checked by
    _validate_cached_pass before the caller returns any PASS.
    """
    wanted = dict(request)
    wanted.pop("request_sha256", None)
    wanted["runtime_identity"] = _protocol_identity(wanted.get("runtime_identity"))
    if wanted["runtime_identity"] is None or not observation_dir.is_dir():
        return None
    wanted_inputs = {key: item for key, item in wanted.items() if key != "runtime_identity"}
    exact_dir = observation_dir / str(request.get("request_sha256", "")).removeprefix("sha256:")
    directories = [exact_dir, *(p for p in sorted(observation_dir.iterdir()) if p != exact_dir)]
    for directory in directories:
        if not directory.is_dir() or directory.is_symlink():
            continue
        path = directory / "request.json"
        # Incomplete dispatches and request-only crash remnants must reach
        # the same ordinary no-redispatch guards as completed receipts.
        if path.is_symlink() or not path.is_file():
            continue
        saved = dict(_json_mapping(path, code="CACHED_RECEIPT_INVALID"))
        body = dict(saved)
        sha = body.pop("request_sha256", None)
        # Only consider the exact original content-addressed request directory.
        if sha != _canonical_hash(body) or directory.name != str(sha).removeprefix("sha256:"):
            continue
        if {key: item for key, item in body.items() if key != "runtime_identity"} != wanted_inputs:
            continue
        body["runtime_identity"] = _protocol_identity(body.get("runtime_identity"))
        if body == wanted:
            return saved, directory
    return None


def observe_nested_media_acoustics(
    *,
    candidate_id: str,
    observation_id: str,
    outer_media_path: str | Path,
    outer_media_sha256: str,
    final_srt_path: str | Path,
    final_srt_sha256: str,
    caption_frame_path: str | Path,
    caption_frame_sha256: str,
    interval_ms: Sequence[int],
    independent_source_media_path: str | Path,
    independent_source_media_sha256: str,
    mapping_receipt_path: str | Path | None,
    mapping_receipt_sha256: str | None,
    host_enrollment_path: str | Path,
    host_enrollment_sha256: str,
    source_audio_path: str | Path,
    source_audio_sha256: str,
    target_audio_path: str | Path,
    target_audio_sha256: str,
    evidence_root: str | Path,
    observe_audio: ObserveAudio,
    cpa_judge: CpaJudge,
    runtime_identity: RuntimeIdentity | None = None,
    caption_frame_ms: int | None = None,
    target_interval_ms: Sequence[int] | None = None,
    target_cue_indexes: Sequence[int | str] | None = None,
    target_audio_window_ms: Sequence[int] | None = None,
    observer_mode: str | None = None,
    voice_reference_binding_path: str | Path | None = None,
    voice_reference_binding_sha256: str | None = None,
) -> dict[str, object]:
    """Run one injected observer then a bounded CPA judgement.

    The callbacks receive ``request`` and ``(request, observation)``,
    respectively.  They choose all provider details; this function never
    retries or invokes an external service.
    """
    if not isinstance(candidate_id, str) or not _SAFE.fullmatch(candidate_id):
        raise NestedMediaAcousticObserverError("CANDIDATE_ID_INVALID")
    if not isinstance(observation_id, str) or not _SAFE.fullmatch(observation_id):
        raise NestedMediaAcousticObserverError("OBSERVATION_ID_INVALID")
    if not callable(observe_audio) or not callable(cpa_judge):
        raise NestedMediaAcousticObserverError("CALLBACK_INVALID")
    normalized_runtime_identity = _runtime_identity(runtime_identity)
    voice_mode = observer_mode == VOICE_REFERENCE_MODE
    if observer_mode not in (None, VOICE_REFERENCE_MODE):
        raise NestedMediaAcousticObserverError("OBSERVER_MODE_INVALID")
    if voice_mode:
        if mapping_receipt_path not in (None, "") or mapping_receipt_sha256 not in (None, ""):
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_MAPPING_FORBIDDEN")
        if voice_reference_binding_path in (None, "") or voice_reference_binding_sha256 in (None, ""):
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_BINDING_REQUIRED")
    elif voice_reference_binding_path not in (None, "") or voice_reference_binding_sha256 not in (None, ""):
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_MODE_REQUIRED")
    interval_value = _interval(interval_ms, code="INTERVAL_INVALID", integer=True)
    interval = [int(item) for item in interval_value]
    frame_ms = (sum(interval) // 2) if caption_frame_ms is None else caption_frame_ms
    if isinstance(frame_ms, bool) or not isinstance(frame_ms, int) or not interval[0] <= frame_ms < interval[1]:
        raise NestedMediaAcousticObserverError("CAPTION_FRAME_INTERVAL_MISMATCH")
    target_interval, target_cues = _target_binding(
        interval=interval,
        frame_ms=frame_ms,
        target_interval_ms=target_interval_ms,
        target_cue_indexes=target_cue_indexes,
    )

    files: dict[str, tuple[Path, str] | None] = {
        "outer": _verify_file(outer_media_path, outer_media_sha256, unavailable="OUTER_MEDIA_UNAVAILABLE", mismatch="OUTER_MEDIA_HASH_MISMATCH"),
        "srt": _verify_file(final_srt_path, final_srt_sha256, unavailable="FINAL_SRT_UNAVAILABLE", mismatch="FINAL_SRT_HASH_MISMATCH"),
        "frame": _verify_file(caption_frame_path, caption_frame_sha256, unavailable="CAPTION_FRAME_UNAVAILABLE", mismatch="CAPTION_FRAME_HASH_MISMATCH"),
        "source_media": _verify_file(independent_source_media_path, independent_source_media_sha256, unavailable="INDEPENDENT_SOURCE_UNAVAILABLE", mismatch="INDEPENDENT_SOURCE_HASH_MISMATCH"),
        "mapping": None if voice_mode else _verify_file(
            mapping_receipt_path, mapping_receipt_sha256,
            unavailable="MAPPING_RECEIPT_UNAVAILABLE", mismatch="MAPPING_RECEIPT_HASH_MISMATCH",
        ),
        "host": _verify_file(host_enrollment_path, host_enrollment_sha256, unavailable="HOST_ENROLLMENT_UNAVAILABLE", mismatch="HOST_ENROLLMENT_HASH_MISMATCH"),
        "source_audio": _verify_file(source_audio_path, source_audio_sha256, unavailable="SOURCE_AUDIO_UNAVAILABLE", mismatch="SOURCE_AUDIO_HASH_MISMATCH"),
        "target_audio": _verify_file(target_audio_path, target_audio_sha256, unavailable="TARGET_AUDIO_UNAVAILABLE", mismatch="TARGET_AUDIO_HASH_MISMATCH"),
    }
    mapping_info: dict[str, object] | None = None
    voice_reference_info: dict[str, object] | None = None
    voice_binding_path: Path | None = None
    voice_binding_sha: str | None = None
    if voice_mode:
        voice_binding_path, voice_binding_sha, voice_reference_info = _validate_voice_reference_binding(
            binding_path=voice_reference_binding_path,
            binding_sha256=voice_reference_binding_sha256,
            candidate_id=candidate_id, observation_id=observation_id,
            outer=files["outer"], srt=files["srt"], frame=files["frame"],
            source_media=files["source_media"], host=files["host"],
            source_audio=files["source_audio"], target_audio=files["target_audio"],
            interval=interval, frame_ms=frame_ms,
            target_interval=target_interval, target_cues=target_cues,
            target_audio_window_ms=target_audio_window_ms,
        )
        assert voice_binding_path is not None and voice_binding_sha is not None
        # Recheck the immutable binding after both callbacks return.  Keep it
        # in the same file-integrity set as aligned mapping receipts so a
        # provider-side edit cannot yield a PASS receipt for a different
        # voice-reference binding.
        files["voice_reference_binding"] = (voice_binding_path, voice_binding_sha)
    else:
        mapping = _json_mapping(files["mapping"][0], code="MAPPING_RECEIPT_INVALID")
        mapping_info = _mapping_info(
            mapping, outer_sha=files["outer"][1], source_sha=files["source_media"][1], interval=interval
        )
    request: dict[str, object] = {
        "schema_version": REQUEST_SCHEMA, "candidate_id": candidate_id, "observation_id": observation_id,
        "outer_media": {"path": str(files["outer"][0]), "sha256": files["outer"][1]},
        "final_srt": {"path": str(files["srt"][0]), "sha256": files["srt"][1]},
        "caption_frame": {"path": str(files["frame"][0]), "sha256": files["frame"][1], "frame_ms": frame_ms},
        "interval_ms": interval,
        "independent_source_media": {"path": str(files["source_media"][0]), "sha256": files["source_media"][1]},
        "audio_parts": [
            {"role": "host_enrollment", "path": str(files["host"][0]), "sha256": files["host"][1]},
            {"role": "independent_source", "path": str(files["source_audio"][0]), "sha256": files["source_audio"][1]},
            {"role": "current_target", "path": str(files["target_audio"][0]), "sha256": files["target_audio"][1]},
        ],
        "part_order": ["host_enrollment", "independent_source", "current_target"],
        "policy": {"correlation_alone_is_insufficient": True, "unknown_action": "KEEP", "mutation_authorized": False},
        "runtime_identity": normalized_runtime_identity,
    }
    if voice_mode:
        assert voice_binding_path is not None and voice_binding_sha is not None
        request.update(
            observer_mode=VOICE_REFERENCE_MODE,
            voice_reference_binding={
                "path": str(voice_binding_path), "sha256": voice_binding_sha,
                **dict(voice_reference_info or {}),
            },
        )
        request["policy"] = {
            "correlation_alone_is_insufficient": True,
            "unknown_action": "KEEP",
            "mutation_authorized": False,
            "time_alignment_performed": False,
            "source_subtraction_authorized": False,
            "caption_dedup_authority": False,
            "source_role_authority": False,
        }
    else:
        request["mapping_receipt"] = {
            "path": str(files["mapping"][0]), "sha256": files["mapping"][1],
            **dict(mapping_info or {}),
        }
    if target_interval is not None and target_cues is not None:
        scope = observer_target_scope(
            interval_ms=interval, target_interval_ms=target_interval,
            target_cue_indexes=target_cues,
            target_audio_window_ms=(target_audio_window_ms if target_audio_window_ms is not None
                                    else target_interval if voice_mode else interval),
        )
        request["target_audio_window_ms"] = scope["target_audio_window_ms"]
        request["target_audio_offset_ms"] = scope["target_audio_offset_ms"]
        request.update(
            target_interval_ms=target_interval,
            target_cue_indexes=target_cues,
        )
    request["request_sha256"] = _canonical_hash(request)
    request_sha = str(request["request_sha256"])

    root = Path(evidence_root).expanduser().absolute()
    candidate_dir, observation_dir = root / candidate_id, root / candidate_id / observation_id
    for directory in (root, candidate_dir, observation_dir):
        if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
            raise NestedMediaAcousticObserverError("EVIDENCE_ROOT_UNSAFE")
        directory.mkdir(parents=True, exist_ok=True)
    request_dir = observation_dir / request_sha.removeprefix("sha256:")
    if request_dir.exists() and (request_dir.is_symlink() or not request_dir.is_dir()):
        raise NestedMediaAcousticObserverError("EVIDENCE_ROOT_UNSAFE")
    with exclusive_lock(observation_dir / "observer.lock", label="nested acoustic observer"):
        compatible = _compatible_receipt_request(observation_dir, request)
        if compatible is not None:
            request, request_dir = compatible
            request_sha = str(request["request_sha256"])
        request_dir.mkdir(parents=True, exist_ok=True)
        request_path, state_path, result_path = request_dir / "request.json", request_dir / "state.json", request_dir / "RESULT.json"
        receipt_path = request_dir / "acoustic-receipt.json"
        observer_path, cpa_path = request_dir / "observer-response.json", request_dir / "cpa-response.json"
        if normalized_runtime_identity is None:
            # A historical request may lack the runtime contract.  Preserve it
            # as evidence-only and never dispatch or promote its result here.
            cached = _load_result(result_path, request_sha, request_path, request_dir)
            if cached is not None:
                return _result(
                    status="EVIDENCE_ONLY", request_sha=request_sha,
                    request_path=request_path, request_dir=request_dir,
                    reason="RUNTIME_IDENTITY_REQUIRED", replayed=True,
                )
            if any(path.exists() for path in (request_path, state_path, receipt_path)):
                return _result(
                    status="EVIDENCE_ONLY", request_sha=request_sha,
                    request_path=request_path, request_dir=request_dir,
                    reason="RUNTIME_IDENTITY_REQUIRED", replayed=True,
                )
            atomic_write_json(request_path, request)
            return _complete(
                _result(
                    status="EVIDENCE_ONLY", request_sha=request_sha,
                    request_path=request_path, request_dir=request_dir,
                    reason="RUNTIME_IDENTITY_REQUIRED", replayed=False,
                ),
                result_path=result_path, state=state_path, request_sha=request_sha,
            )
        if receipt_path.is_file() and not receipt_path.is_symlink():
            receipt = _json_mapping(receipt_path, code="CACHED_RECEIPT_INVALID")
            if (
                receipt.get("schema_version") != ACOUSTIC_SCHEMA or receipt.get("status") != "PASS"
                or receipt.get("candidate_id") != candidate_id or receipt.get("observation_id") != observation_id
                or receipt.get("interval_ms") != interval
                or receipt.get("method") != (VOICE_REFERENCE_METHOD if voice_mode else METHOD)
                or receipt.get("accepted_by") != "CPA" or receipt.get("mutation_authorized") is not False
                or (voice_mode and receipt.get("observer_mode") != VOICE_REFERENCE_MODE)
            ):
                raise NestedMediaAcousticObserverError("CACHED_RECEIPT_INVALID")
            receipt_sha = _validate_cached_pass(
                receipt=receipt, receipt_path=receipt_path, request_path=request_path,
                observer_path=observer_path, cpa_path=cpa_path, request=request,
                request_sha=request_sha, candidate_id=candidate_id, observation_id=observation_id,
                interval=interval, target_interval=target_interval,
                target_cue_indexes=target_cues, files=files, mapping_info=mapping_info,
                observer_mode=observer_mode, voice_reference_info=voice_reference_info,
            )
            cached = _load_result(result_path, request_sha, request_path, request_dir)
            if cached is not None:
                if (
                    cached.get("status") != "PASS"
                    or cached.get("final_class") != receipt.get("final_class")
                    or cached.get("authority") != "EVIDENCE_ONLY"
                    or cached.get("mutation_authorized") is not False
                ):
                    raise NestedMediaAcousticObserverError("CACHED_RECEIPT_INVALID")
                cached["receipt_binding"] = {"path": str(receipt_path), "sha256": receipt_sha}
                return cached
            return _result(
                status="PASS", request_sha=request_sha, request_path=request_path, request_dir=request_dir,
                reason="COMPLETED_RECEIPT_REPLAY", replayed=True, receipt_path=receipt_path,
                receipt_sha=receipt_sha, final_class=str(receipt.get("final_class", "unknown")),
            )
        if state_path.is_file() and not state_path.is_symlink():
            state = _json_mapping(state_path, code="CACHE_STATE_INVALID")
            if state.get("request_sha256") != request_sha:
                raise NestedMediaAcousticObserverError("CACHE_REQUEST_BINDING_MISMATCH")
            if state.get("status") != "COMPLETE":
                cached = _load_result(result_path, request_sha, request_path, request_dir)
                if cached is not None:
                    if cached.get("status") == "PASS":
                        return _result(
                            status="EVIDENCE_ONLY", request_sha=request_sha,
                            request_path=request_path, request_dir=request_dir,
                            reason="COMPLETED_RECEIPT_MISSING", replayed=True,
                        )
                    return cached
                return _result(
                    status="EVIDENCE_ONLY", request_sha=request_sha, request_path=request_path, request_dir=request_dir,
                    reason="PRIOR_DISPATCH_INCOMPLETE", replayed=True,
                )
            cached = _load_result(result_path, request_sha, request_path, request_dir)
            if cached is not None:
                if cached.get("status") == "PASS":
                    return _result(
                        status="EVIDENCE_ONLY", request_sha=request_sha, request_path=request_path,
                        request_dir=request_dir, reason="COMPLETED_RECEIPT_MISSING", replayed=True,
                    )
                return cached
            return _result(
                status="EVIDENCE_ONLY", request_sha=request_sha, request_path=request_path, request_dir=request_dir,
                reason="COMPLETED_RESULT_MISSING", replayed=True,
            )
        if request_path.exists():
            raise NestedMediaAcousticObserverError("REQUEST_WITHOUT_CACHE_STATE")

        atomic_write_json(request_path, request)
        atomic_write_json(state_path, {
            "schema_version": STATE_SCHEMA, "status": "RUNNING", "request_sha256": request_sha,
            "request_path": str(request_path), "started_at": _now(),
        })
        try:
            raw_observed = observe_audio(request)
            if isinstance(raw_observed, Mapping):
                atomic_write_json(observer_path, dict(raw_observed))
            observed = _observation(
                raw_observed, candidate=candidate_id, observation_id=observation_id,
                request_sha=request_sha, request=request,
            )
        except Exception as exc:
            return _failure(
                request=request_path, request_dir=request_dir, state=state_path, result_path=result_path,
                request_sha=request_sha, reason=_dispatch_reason("OBSERVER_DISPATCH_FAILED", exc), error=exc,
                observer_path=observer_path if observer_path.is_file() else None,
            )
        atomic_write_json(observer_path, observed)
        if observed["status"] == "UNKNOWN":
            return _complete(
                _result(
                    status="EVIDENCE_ONLY", request_sha=request_sha, request_path=request_path, request_dir=request_dir,
                    reason="OBSERVER_UNKNOWN_KEEP", replayed=False, observer_path=observer_path,
                ),
                result_path=result_path, state=state_path, request_sha=request_sha,
            )
        atomic_write_json(state_path, {
            "schema_version": STATE_SCHEMA, "status": "CPA_RUNNING", "request_sha256": request_sha,
            "observer_response_path": str(observer_path), "updated_at": _now(),
        })
        try:
            raw_judged = cpa_judge(request, observed)
            if isinstance(raw_judged, Mapping):
                atomic_write_json(cpa_path, dict(raw_judged))
            judged = _judgement(
                raw_judged, candidate=candidate_id, observation_id=observation_id,
                request_sha=request_sha, observation=observed, request=request,
            )
        except Exception as exc:
            return _failure(
                request=request_path, request_dir=request_dir, state=state_path, result_path=result_path,
                request_sha=request_sha, reason=_dispatch_reason("CPA_DISPATCH_FAILED", exc), error=exc, observer_path=observer_path,
                cpa_path=cpa_path if cpa_path.is_file() else None,
            )
        try:
            for file_binding in files.values():
                if file_binding is None:
                    continue
                path, expected = file_binding
                _verify_file(path, expected, unavailable="INPUT_UNAVAILABLE", mismatch="INPUT_CHANGED")
        except Exception as exc:
            return _failure(
                request=request_path, request_dir=request_dir, state=state_path, result_path=result_path,
                request_sha=request_sha, reason="INPUT_DRIFT_AFTER_OBSERVATION", error=exc,
                observer_path=observer_path, cpa_path=cpa_path,
            )
        final_class = _classify(judged)
        if final_class == "unknown" or any(judged[role] == "UNKNOWN" for role in _ROLES):
            return _complete(
                _result(
                    status="EVIDENCE_ONLY", request_sha=request_sha, request_path=request_path, request_dir=request_dir,
                    reason="CPA_UNKNOWN_KEEP", replayed=False, observer_path=observer_path, cpa_path=cpa_path,
                    final_class=final_class,
                ),
                result_path=result_path, state=state_path, request_sha=request_sha,
            )

        receipt: dict[str, object] = {
            "schema_version": ACOUSTIC_SCHEMA, "status": "PASS", "authority": "EVIDENCE_ONLY",
            "candidate_id": candidate_id, "observation_id": observation_id, "interval_ms": interval,
            "source_media_speech": judged["source_media_speech"], "host_speech": judged["host_speech"],
            "overlap_speech": judged["overlap_speech"], "final_class": final_class,
            "method": VOICE_REFERENCE_METHOD if voice_mode else METHOD,
            "accepted_by": "CPA", "accepted_at": _now(), "mutation_authorized": False,
            "source_media_sha256": files["outer"][1], "final_srt_sha256": files["srt"][1],
            "caption_frame_sha256": files["frame"][1],
            "runtime_identity": normalized_runtime_identity,
            "runtime_identity_sha256": normalized_runtime_identity["identity_sha256"],
            "bindings": {
                "source_media_sha256": files["outer"][1], "final_srt_sha256": files["srt"][1],
                "caption_frame_sha256": files["frame"][1], "candidate_id": candidate_id,
                "observation_id": observation_id, "interval_ms": interval,
                "independent_source_media_sha256": files["source_media"][1],
                "host_audio_sha256": files["host"][1],
                "source_audio_sha256": files["source_audio"][1], "target_audio_sha256": files["target_audio"][1],
                "runtime_identity_sha256": normalized_runtime_identity["identity_sha256"],
            },
            "evidence": {
                "request_sha256": request_sha, "observer_response_sha256": _canonical_hash(observed),
                "cpa_response_sha256": _canonical_hash(judged), "observer_response_path": str(observer_path),
                "cpa_response_path": str(cpa_path),
                "part_order": ["host_enrollment", "independent_source", "current_target"],
                "runtime_identity_sha256": normalized_runtime_identity["identity_sha256"],
            },
        }
        if voice_mode:
            receipt.update(
                observer_mode=VOICE_REFERENCE_MODE,
                voice_reference_binding=dict(voice_reference_info or {}),
                time_alignment_performed=False,
                source_subtraction_authorized=False,
                caption_dedup_authority=False,
                source_role_authority=False,
            )
            receipt["bindings"]["observer_mode"] = VOICE_REFERENCE_MODE
            receipt["bindings"]["voice_reference_binding_sha256"] = voice_binding_sha
            receipt["evidence"]["voice_reference_binding"] = dict(voice_reference_info or {})
        else:
            receipt["evidence"]["mapping"] = dict(mapping_info or {})
            receipt["bindings"]["mapping_receipt_sha256"] = files["mapping"][1]
        if target_interval is not None and target_cues is not None:
            receipt["target_interval_ms"] = target_interval
            receipt["target_cue_indexes"] = target_cues
            bindings = receipt["bindings"]
            assert isinstance(bindings, dict)
            bindings["target_interval_ms"] = target_interval
            bindings["target_cue_indexes"] = target_cues
        receipt_sha = "sha256:" + atomic_write_json(receipt_path, receipt)
        result = _result(
            status="PASS", request_sha=request_sha, request_path=request_path, request_dir=request_dir,
            reason="CPA_TYPED_RECEIPT_CREATED", replayed=False, observer_path=observer_path, cpa_path=cpa_path,
            receipt_path=receipt_path, receipt_sha=receipt_sha, final_class=final_class,
        )
        return _complete(
            result, result_path=result_path, state=state_path, request_sha=request_sha,
            extra_state={"receipt_path": str(receipt_path), "receipt_sha256": receipt_sha},
        )
