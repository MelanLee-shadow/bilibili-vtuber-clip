"""Validate source-bound, tone-only acoustic observations as CPA context.

This lane consumes a saved native acoustic feature packet whose model was not
shown a transcript or candidate words.  The packet is deliberately narrower
than the normal blind-pinyin/transcript witness contract: it can describe
pitch contours in a partial target interval, but it cannot establish a word,
speaker, complete utterance, subtitle mutation, or release decision.

The validator reads the original request, observation, execution log, stdout,
audio bytes, and source-media bytes through an explicit consumption envelope.
It returns a context row only after all of those immutable bindings and the
current SRT/source request agree.  It never reads an interpretation file.
"""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import hashlib
import json
import math
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from src.autoslice.acoustic_witness_adjudication import build_witness_request
from src.autoslice.jingting_chunker import parse_srt_cues


TONE_CONTEXT_SCHEMA = "tone-acoustic-feature-context-observation.v1"

_STATUSES = frozenset({"OBSERVED", "UNKNOWN", "UNSUPPORTED_INPUT"})
_CONTOURS = frozenset({"LEVEL", "RISING", "DIPPING", "FALLING", "NEUTRAL", "UNKNOWN"})
_TONES = frozenset({"0", "1", "2", "3", "4", "UNKNOWN"})
_ARTIFACT_ROLES = ("request", "observation", "execution", "stdout", "audio", "source_media")
_FORBIDDEN_EXPOSURE_KEYS = frozenset({
    "candidate", "candidate_id", "candidate_entities", "current_cue", "proposed_cue",
    "replacement", "suspect", "exact_text", "pinyin", "heard_pinyin", "transcript",
    "word", "word_segments", "subtitle", "alternative", "alternatives",
})


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path_value: object) -> str | None:
    if not isinstance(path_value, str) or not path_value:
        return None
    try:
        path = Path(path_value)
        if path.is_symlink() or not path.is_file():
            return None
        digest = _evidence_file_sha256(path)
        return digest
    except (OSError, ValueError):
        return None


def _read_json(path_value: object) -> Mapping[str, Any] | None:
    if not isinstance(path_value, str) or not path_value:
        return None
    try:
        path = Path(path_value)
        if path.is_symlink() or not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


def _same_digest(left: object, right: object) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    left = left.removeprefix("sha256:").lower()
    right = right.removeprefix("sha256:").lower()
    return len(left) == len(right) == 64 and left == right and all(
        char in "0123456789abcdef" for char in left
    )


def _strict_interval(value: object) -> tuple[int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if any(type(item) is not int for item in value):
        return None
    start, end = int(value[0]), int(value[1])
    if start < 0 or end <= start:
        return None
    return start, end


def _seconds_interval(value: object) -> tuple[Decimal, Decimal] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    result: list[Decimal] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        if not math.isfinite(float(item)):
            return None
        try:
            result.append(Decimal(str(item)))
        except (InvalidOperation, ValueError):
            return None
    if result[0] < 0 or result[1] <= result[0]:
        return None
    return result[0], result[1]


def _seconds_equal_ms(seconds: object, interval_ms: tuple[int, int]) -> bool:
    parsed = _seconds_interval(seconds)
    if parsed is None:
        return False
    expected = tuple(Decimal(item) / Decimal(1000) for item in interval_ms)
    return parsed == expected


def _binding_section(observation: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = observation.get("source_binding")
    return value if isinstance(value, Mapping) else None


def _artifact_bindings(observation: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = observation.get("artifact_bindings")
    return value if isinstance(value, Mapping) else None


def _artifact_value(bindings: Mapping[str, Any], role: str, *, hash_value: bool) -> object:
    suffix = "sha256" if hash_value else "path"
    return bindings.get(f"{role}_{suffix}")


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _forbidden_keys(value: Mapping[str, Any]) -> bool:
    return any(key in _FORBIDDEN_EXPOSURE_KEYS for key in value)


def _stdout_evidence(
    stdout_path: str,
    *,
    audio_request_path: str,
    model: str,
    observation: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        lines = Path(stdout_path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None, "TONE_CONTEXT_STDOUT_UNREADABLE"
    result_rows: list[Mapping[str, Any]] = []
    viewed_paths: list[str] = []
    init_models: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, Mapping):
            continue
        if event.get("event") == "result" and isinstance(event.get("result"), Mapping):
            result_rows.append(event["result"])
        step_update = event.get("step_update")
        if isinstance(step_update, Mapping):
            tool_name = step_update.get("tool_name")
            tool_info = step_update.get("tool_info")
            if tool_name == "view_file" and isinstance(tool_info, Mapping):
                parameters = tool_info.get("parameters")
                if isinstance(parameters, Mapping) and isinstance(parameters.get("AbsolutePath"), str):
                    viewed_paths.append(parameters["AbsolutePath"])
        if event.get("event") == "init" and isinstance(event.get("init"), Mapping):
            init_model = event["init"].get("model")
            if isinstance(init_model, str):
                init_models.append(init_model)
    if len(result_rows) != 1:
        return None, "TONE_CONTEXT_STDOUT_RESULT_MISSING_OR_DUPLICATE"
    result = result_rows[0]
    if result.get("status") != "SUCCESS" or not isinstance(result.get("structured_output"), Mapping):
        return None, "TONE_CONTEXT_STDOUT_RESULT_FAILED"
    if sorted(set(viewed_paths)) != [audio_request_path]:
        return None, "TONE_CONTEXT_AUDIO_READ_NOT_SOLE_OR_BOUND"
    if init_models and any(value != model for value in init_models):
        return None, "TONE_CONTEXT_STDOUT_MODEL_DRIFT"
    structured = result["structured_output"]
    required = (
        "status", "target_interval_seconds", "heard_speech_interval_seconds",
        "speech_syllable_count", "syllables", "third_syllable_assessable",
        "evidence_basis", "uncertainty",
    )
    if any(structured.get(key) != observation.get(key) for key in required):
        return None, "TONE_CONTEXT_STDOUT_OBSERVATION_DRIFT"
    if _forbidden_keys(structured):
        return None, "TONE_CONTEXT_STDOUT_CANDIDATE_EXPOSURE"
    return {
        "result_status": result.get("status"),
        "view_file_path": audio_request_path,
        "init_models": init_models,
        "structured_output": dict(structured),
    }, None


def _validate_observation_payload(observation: Mapping[str, Any]) -> str | None:
    if _forbidden_keys(observation):
        return "TONE_CONTEXT_OBSERVATION_CANDIDATE_EXPOSURE"
    status = observation.get("status")
    if status not in _STATUSES:
        return "TONE_CONTEXT_OBSERVATION_STATUS_INVALID"
    target = _seconds_interval(observation.get("target_interval_seconds"))
    if target is None:
        return "TONE_CONTEXT_TARGET_INTERVAL_INVALID"
    heard = observation.get("heard_speech_interval_seconds")
    if heard is not None:
        heard_interval = _seconds_interval(heard)
        if heard_interval is None or heard_interval[0] < target[0] or heard_interval[1] > target[1]:
            return "TONE_CONTEXT_HEARD_INTERVAL_INVALID"
    count = observation.get("speech_syllable_count")
    syllables = observation.get("syllables")
    if not isinstance(syllables, list):
        return "TONE_CONTEXT_SYLLABLES_INVALID"
    if count is not None and type(count) is not int:
        return "TONE_CONTEXT_SYLLABLE_COUNT_INVALID"
    if status == "OBSERVED":
        if not isinstance(count, int) or count <= 0 or len(syllables) != count:
            return "TONE_CONTEXT_SYLLABLE_COUNT_INVALID"
    elif count not in {None, 0} or syllables:
        return "TONE_CONTEXT_NONOBSERVED_SYLLABLES_INVALID"
    seen_indexes: list[int] = []
    for row in syllables:
        if not isinstance(row, Mapping) or _forbidden_keys(row):
            return "TONE_CONTEXT_SYLLABLE_ROW_INVALID"
        index = row.get("index")
        interval = _seconds_interval(row.get("interval_seconds"))
        if type(index) is not int or index < 1 or interval is None:
            return "TONE_CONTEXT_SYLLABLE_ROW_INVALID"
        if interval[0] < target[0] or interval[1] > target[1]:
            return "TONE_CONTEXT_SYLLABLE_INTERVAL_INVALID"
        if row.get("tone") not in _TONES or row.get("pitch_contour") not in _CONTOURS:
            return "TONE_CONTEXT_SYLLABLE_FEATURE_INVALID"
        if not _nonempty_string(row.get("acoustic_note")):
            return "TONE_CONTEXT_SYLLABLE_NOTE_INVALID"
        seen_indexes.append(index)
    if seen_indexes != list(range(1, len(seen_indexes) + 1)):
        return "TONE_CONTEXT_SYLLABLE_INDEX_INVALID"
    if type(observation.get("third_syllable_assessable")) is not bool:
        return "TONE_CONTEXT_THIRD_SYLLABLE_FLAG_INVALID"
    if observation.get("third_syllable_assessable") and len(syllables) < 3:
        return "TONE_CONTEXT_THIRD_SYLLABLE_FLAG_INVALID"
    if not _nonempty_string(observation.get("evidence_basis")) or not _nonempty_string(
        observation.get("uncertainty")
    ):
        return "TONE_CONTEXT_EXPLANATION_MISSING"
    return None


def validate_tone_context_observation(
    srt_text: str,
    current_request: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate one saved tone-only packet as partial CPA context.

    The returned row deliberately has no ``heard_pinyin`` or transcript.  It
    is suitable for ``prior_range_acoustic_observations`` only as context and
    carries no mutation or release authority.
    """

    if not isinstance(srt_text, str) or not isinstance(current_request, Mapping):
        return None, "TONE_CONTEXT_INPUT_INVALID"
    if not isinstance(observation, Mapping):
        return None, "TONE_CONTEXT_OBSERVATION_INVALID"

    if observation.get("schema_version") != TONE_CONTEXT_SCHEMA:
        return None, "TONE_CONTEXT_SCHEMA_INVALID"
    if observation.get("authority") not in {None, "EVIDENCE_ONLY"}:
        return None, "TONE_CONTEXT_AUTHORITY_INVALID"
    if observation.get("mutation_authorized") is not False or observation.get("release_authorized") is not False:
        return None, "TONE_CONTEXT_MUTATION_AUTHORITY"
    if observation.get("candidate_exposure") not in {None, "none"}:
        return None, "TONE_CONTEXT_CANDIDATE_EXPOSURE"

    artifacts = _artifact_bindings(observation)
    binding = _binding_section(observation)
    if artifacts is None or binding is None:
        return None, "TONE_CONTEXT_BINDINGS_MISSING"
    if binding.get("scope") not in {None, "PARTIAL_CONTEXT"}:
        return None, "TONE_CONTEXT_SCOPE_INVALID"
    paths: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for role in _ARTIFACT_ROLES:
        path_value = _artifact_value(artifacts, role, hash_value=False)
        declared_hash = _artifact_value(artifacts, role, hash_value=True)
        actual_hash = _sha256_file(path_value)
        if not isinstance(path_value, str) or actual_hash is None:
            return None, f"TONE_CONTEXT_{role.upper()}_MISSING"
        if not _same_digest(declared_hash, actual_hash):
            return None, f"TONE_CONTEXT_{role.upper()}_HASH_DRIFT"
        paths[role] = path_value
        hashes[role] = actual_hash

    request_doc = _read_json(paths["request"])
    observation_doc = _read_json(paths["observation"])
    execution_doc = _read_json(paths["execution"])
    if any(value is None for value in (request_doc, observation_doc, execution_doc)):
        return None, "TONE_CONTEXT_PACKET_JSON_INVALID"
    assert request_doc is not None and observation_doc is not None and execution_doc is not None
    if _forbidden_keys(request_doc) or _forbidden_keys(execution_doc):
        return None, "TONE_CONTEXT_PACKET_CANDIDATE_EXPOSURE"
    if request_doc.get("mutations_authorized") is not False or request_doc.get("release_authorized") is not False:
        return None, "TONE_CONTEXT_REQUEST_AUTHORITY_INVALID"
    if not isinstance(request_doc.get("candidate_exposure"), str) or not request_doc[
        "candidate_exposure"
    ].strip().lower().startswith("none"):
        return None, "TONE_CONTEXT_REQUEST_CANDIDATE_EXPOSURE"
    model = binding.get("model")
    if not _nonempty_string(model) or request_doc.get("model") != model:
        return None, "TONE_CONTEXT_MODEL_BINDING_INVALID"
    if execution_doc.get("model_requested") != model:
        return None, "TONE_CONTEXT_EXECUTION_MODEL_DRIFT"
    if execution_doc.get("returncode") != 0:
        return None, "TONE_CONTEXT_EXECUTION_FAILED"
    if execution_doc.get("provider_cli_dispatches") != 1:
        return None, "TONE_CONTEXT_EXECUTION_DISPATCH_COUNT_INVALID"
    if execution_doc.get("authority") != "EVIDENCE_ONLY":
        return None, "TONE_CONTEXT_EXECUTION_AUTHORITY_INVALID"
    if execution_doc.get("request_sha256") != hashes["request"]:
        return None, "TONE_CONTEXT_EXECUTION_REQUEST_DRIFT"
    if execution_doc.get("stdout_sha256") != hashes["stdout"]:
        return None, "TONE_CONTEXT_EXECUTION_STDOUT_DRIFT"
    for key in ("mutations_authorized", "release_authorized"):
        if key in execution_doc and execution_doc.get(key) is not False:
            return None, "TONE_CONTEXT_EXECUTION_AUTHORITY_INVALID"
    if request_doc.get("audio_sha256") != hashes["audio"]:
        return None, "TONE_CONTEXT_REQUEST_AUDIO_DRIFT"
    request_audio_path = request_doc.get("audio_path")
    if not isinstance(request_audio_path, str) or not request_audio_path:
        return None, "TONE_CONTEXT_REQUEST_AUDIO_PATH_MISSING"
    request_audio_hash = _sha256_file(request_audio_path)
    if request_audio_hash is not None and request_audio_hash != hashes["audio"]:
        return None, "TONE_CONTEXT_REQUEST_AUDIO_PATH_DRIFT"
    if request_doc.get("target_attachment_ms") is None:
        return None, "TONE_CONTEXT_TARGET_ATTACHMENT_MISSING"
    target_attachment = _strict_interval(request_doc.get("target_attachment_ms"))
    bound_attachment = _strict_interval(binding.get("target_attachment_ms"))
    if target_attachment is None or bound_attachment != target_attachment:
        return None, "TONE_CONTEXT_TARGET_ATTACHMENT_DRIFT"

    observation_error = _validate_observation_payload(observation_doc)
    if observation_error is not None:
        return None, observation_error
    if not _seconds_equal_ms(observation_doc.get("target_interval_seconds"), target_attachment):
        return None, "TONE_CONTEXT_OBSERVATION_TARGET_DRIFT"
    stdout_result, stdout_error = _stdout_evidence(
        paths["stdout"], audio_request_path=request_audio_path,
        model=model, observation=observation_doc,
    )
    if stdout_error is not None:
        return None, stdout_error
    assert stdout_result is not None

    source_media_sha = hashes["source_media"]
    audio_sha = hashes["audio"]
    source_media_declared = binding.get("source_media_sha256")
    audio_declared = binding.get("audio_sha256")
    if not _same_digest(source_media_declared, source_media_sha) or not _same_digest(audio_declared, audio_sha):
        return None, "TONE_CONTEXT_MEDIA_HASH_BINDING_DRIFT"
    offset = binding.get("source_media_timeline_offset_ms")
    if type(offset) is not int or offset < 0:
        return None, "TONE_CONTEXT_OFFSET_INVALID"
    delivery = _strict_interval(binding.get("delivery_target_ms"))
    source_target = _strict_interval(binding.get("source_target_ms"))
    audio_window = _strict_interval(binding.get("audio_window_ms"))
    if delivery is None or source_target is None or audio_window is None:
        return None, "TONE_CONTEXT_GEOMETRY_MISSING"
    if source_target != (delivery[0] + offset, delivery[1] + offset):
        return None, "TONE_CONTEXT_SOURCE_TARGET_DRIFT"
    if target_attachment[1] >= audio_window[1] - audio_window[0] + 1:
        return None, "TONE_CONTEXT_ATTACHMENT_OUTSIDE_AUDIO"
    feature_source = (
        audio_window[0] + target_attachment[0],
        audio_window[0] + target_attachment[1],
    )
    feature_delivery = (feature_source[0] - offset, feature_source[1] - offset)
    if feature_source[0] < source_target[0] or feature_source[1] > source_target[1]:
        return None, "TONE_CONTEXT_FEATURE_OUTSIDE_SOURCE_TARGET"
    if feature_delivery[0] < delivery[0] or feature_delivery[1] > delivery[1]:
        return None, "TONE_CONTEXT_FEATURE_OUTSIDE_DELIVERY_TARGET"
    if feature_delivery == delivery:
        return None, "TONE_CONTEXT_NOT_PARTIAL"

    current_srt_sha = _sha256_bytes(srt_text.encode("utf-8"))
    if current_request.get("whole_clip_current_srt") != srt_text:
        return None, "TONE_CONTEXT_CURRENT_SRT_REQUEST_DRIFT"
    if not _same_digest(binding.get("current_srt_sha256"), current_srt_sha):
        return None, "TONE_CONTEXT_CURRENT_SRT_HASH_DRIFT"
    try:
        cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    except (TypeError, ValueError):
        return None, "TONE_CONTEXT_CURRENT_SRT_INVALID"
    cue_index = binding.get("cue_index")
    if type(cue_index) is not int or not 1 <= cue_index <= len(cues):
        return None, "TONE_CONTEXT_CUE_INDEX_INVALID"
    if current_request.get("cue_indexes") != [cue_index]:
        return None, "TONE_CONTEXT_CURRENT_CUE_INDEX_DRIFT"
    cue = cues[cue_index - 1]
    base_sha = hashlib.sha256(cue.text.encode("utf-8")).hexdigest()
    if not _same_digest(binding.get("base_text_sha256"), base_sha):
        return None, "TONE_CONTEXT_BASE_TEXT_HASH_DRIFT"
    if current_request.get("base_text_sha256") not in {None, base_sha}:
        return None, "TONE_CONTEXT_CURRENT_BASE_TEXT_DRIFT"
    if current_request.get("current_cue") not in {None, cue.text}:
        return None, "TONE_CONTEXT_CURRENT_TEXT_DRIFT"
    current_target = _strict_interval([
        current_request.get("matched_start_ms"), current_request.get("matched_end_ms"),
    ])
    current_offset = current_request.get("source_media_timeline_offset_ms")
    if type(current_offset) is not int or current_offset < 0:
        return None, "TONE_CONTEXT_CURRENT_OFFSET_DRIFT"
    if current_target is None or (
        current_target[0] + current_offset, current_target[1] + current_offset
    ) != source_target:
        return None, "TONE_CONTEXT_CURRENT_TARGET_DRIFT"
    clip = current_request.get("clip_context_binding")
    if not isinstance(clip, Mapping):
        return None, "TONE_CONTEXT_CURRENT_CLIP_BINDING_MISSING"
    candidate_id = binding.get("candidate_id")
    if not _nonempty_string(candidate_id) or clip.get("candidate_id") != candidate_id:
        return None, "TONE_CONTEXT_CURRENT_CANDIDATE_DRIFT"
    # The clip keeps the recording's identity while audio may be extracted
    # from its padded recut. Replay the ordinary range witness provenance,
    # including actual parent/piece bytes; two declared hashes are not a link.
    from src.autoslice.independent_context_adjudication import _range_media_source_binding

    media_source_binding = _range_media_source_binding(
        current_request, paths["source_media"], source_media_sha,
    )
    if media_source_binding is None:
        return None, "TONE_CONTEXT_CURRENT_SOURCE_DRIFT"
    try:
        narrow = build_witness_request(current_request)
    except (KeyError, TypeError, ValueError):
        return None, "TONE_CONTEXT_CURRENT_REQUEST_INVALID"
    packet_hashes = {
        "request_sha256": hashes["request"],
        "observation_sha256": hashes["observation"],
        "execution_sha256": hashes["execution"],
        "stdout_sha256": hashes["stdout"],
        "audio_sha256": audio_sha,
        "source_media_sha256": source_media_sha,
    }
    row = {
        "schema_version": TONE_CONTEXT_SCHEMA,
        "status": observation_doc["status"],
        "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False,
        "release_authorized": False,
        "candidate_blind": True,
        "candidate_exposure": "none",
        "candidate_id": candidate_id,
        "provider": "agy",
        "model": model,
        "media_source_binding": media_source_binding,
        "verdict": {
            "schema_version": "tone-only-native-observation.v1",
            "status": observation_doc["status"],
            "authority": "EVIDENCE_ONLY",
            "mutation_authorized": False,
            "candidate_exposure": "none",
            "scope": "PARTIAL_CONTEXT",
            "tone_sequence": [row.get("tone") for row in observation_doc.get("syllables", [])],
            "pitch_contours": [row.get("pitch_contour") for row in observation_doc.get("syllables", [])],
            "syllables": [dict(row) for row in observation_doc.get("syllables", [])],
            "speech_syllable_count": observation_doc.get("speech_syllable_count"),
            "third_syllable_assessable": observation_doc.get("third_syllable_assessable"),
            "target_interval_seconds": observation_doc.get("target_interval_seconds"),
            "heard_speech_interval_seconds": observation_doc.get("heard_speech_interval_seconds"),
            "evidence_basis": observation_doc.get("evidence_basis"),
            "uncertainty": observation_doc.get("uncertainty"),
            "model": model,
            **packet_hashes,
        },
        "range": {
            "cue_indexes": [cue_index],
            "matched_start_ms": delivery[0],
            "matched_end_ms": delivery[1],
            "context_start_ms": delivery[0],
            "context_end_ms": delivery[1],
            "source_media_timeline_offset_ms": offset,
            "coverage": "PARTIAL_CONTEXT",
            "target_attachment_ms": list(target_attachment),
        },
        "source_window": {
            "target_start_ms": source_target[0],
            "target_end_ms": source_target[1],
            "crop_start_ms": audio_window[0],
            "crop_end_ms": audio_window[1],
        },
        "tone_feature_window": {
            "attachment_ms": list(target_attachment),
            "source_media_ms": list(feature_source),
            "delivery_ms": list(feature_delivery),
            "coverage": "PARTIAL_CONTEXT",
        },
        "cue_texts": [{
            "cue_index": cue_index,
            "start_ms": cue.start_ms,
            "end_ms": cue.end_ms,
            "text": cue.text,
            "text_sha256": base_sha,
            "covered_start_ms": feature_delivery[0],
            "covered_end_ms": feature_delivery[1],
            "coverage": "PARTIAL",
            "coverage_scope": "TONE_FEATURE_ONLY",
        }],
        "current_cue_index": cue_index,
        "single_cue_witness_request_sha256": narrow["request_sha256"],
        "tone_feature_only": True,
        "context_only": True,
        "evidence_scope": "PARTIAL_CONTEXT",
        "transcript_scope": "TONE_FEATURES_ONLY_NO_TRANSCRIPT",
        "original_tone_request_sha256": hashes["request"],
        "original_tone_observation_sha256": hashes["observation"],
        "original_tone_execution_sha256": hashes["execution"],
        "original_tone_stdout_sha256": hashes["stdout"],
        "artifact_bindings": {
            **{f"{role}_path": paths[role] for role in _ARTIFACT_ROLES},
            **{f"{role}_sha256": hashes[role] for role in _ARTIFACT_ROLES},
        },
        "source_binding": {
            "source_media_sha256": source_media_sha,
            "audio_sha256": audio_sha,
            "source_media_timeline_offset_ms": offset,
            "candidate_id": candidate_id,
            "cue_index": cue_index,
            "base_text_sha256": base_sha,
            "current_srt_sha256": current_srt_sha,
            "delivery_target_ms": list(delivery),
            "source_target_ms": list(source_target),
            "audio_window_ms": list(audio_window),
            "target_attachment_ms": list(target_attachment),
            "model": model,
            "scope": "PARTIAL_CONTEXT",
        },
    }
    return row, None
__all__ = [
    "TONE_CONTEXT_SCHEMA",
    "validate_tone_context_observation",
]
