"""Read an aligned watched-source transcript as context, never target dictation."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any


SCHEMA = "mapped-source-audio-context.v1"
SCOPE = "INDEPENDENT_SOURCE_CONTEXT_ONLY"


def validate_mapped_source_context(
    srt_text: str, current_request: Mapping[str, Any], observation: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Revalidate original mapping and native receipts without changing clocks.

    A source playing beneath a host's reaction is a different recording. Its
    transcript may explain the reaction, but cannot prove what the host said or
    establish a speaker role. All artifact paths retain their original identity.
    """
    from src.autoslice.independent_context_adjudication import (
        _file_sha256, _read_json, _same_digest,
    )
    from src.autoslice.local_asr_target_evidence import exact_target_evidence
    from src.autoslice.native_context_witness import _native_transcript_witness, _prepare_request
    from src.autoslice.producer_nested_acoustic import (
        NestedMediaAcousticObserverError, _descriptor_binding,
    )
    from src.autoslice.subtitle_audio_evidence import _digest

    def blocked(reason: str):
        return None, "MAPPED_SOURCE_CONTEXT_" + reason

    if (observation.get("schema_version") != SCHEMA
            or observation.get("authority") != "EVIDENCE_ONLY"
            or observation.get("mutation_authorized") is not False):
        return blocked("AUTHORITY_INVALID")
    artifacts = observation.get("artifact_bindings")
    descriptor = observation.get("descriptor")
    clip = current_request.get("clip_context_binding")
    if not all(isinstance(value, Mapping) for value in (artifacts, descriptor, clip)):
        return blocked("BINDINGS_MISSING")
    roles = ("outer_media", "reviewed_srt", "source_media", "native_envelope",
             "provider_receipt", "audio_clip", "raw_response")
    paths = {role: artifacts.get(role + "_path") for role in roles}
    hashes = {role: _file_sha256(path) for role, path in paths.items()}
    if any(value is None for value in hashes.values()) or any(
        not _same_digest(artifacts.get(role + "_sha256"), hashes[role]) for role in roles
    ):
        return blocked("ARTIFACT_DRIFT")
    if (current_request.get("whole_clip_current_srt") != srt_text
            or clip.get("candidate_id") != observation.get("candidate_id")
            or not _same_digest(clip.get("context_sha256"), observation.get("clip_context_sha256"))):
        return blocked("CURRENT_BINDING_DRIFT")
    current_srt_sha = hashlib.sha256(srt_text.encode()).hexdigest()
    if hashes["reviewed_srt"] != current_srt_sha:
        # The source recording and measured alignment do not change when CPA
        # corrects outer subtitle text. Validate the immutable original receipt
        # first, then permit only text changes at identical cue geometry. Never
        # rewrite the mapping's SRT hash or turn source speech into host speech.
        from src.autoslice.subtitle_audio_correspondence import parse_timed_srt

        try:
            original_srt = Path(paths["reviewed_srt"]).read_text(encoding="utf-8")
            original_cues = parse_timed_srt(original_srt)
            current_cues = parse_timed_srt(srt_text)
        except (OSError, UnicodeError, ValueError):
            return blocked("SRT_UNREADABLE")
        if [(cue.index, cue.start_ms, cue.end_ms) for cue in original_cues] != [
            (cue.index, cue.start_ms, cue.end_ms) for cue in current_cues
        ]:
            return blocked("CURRENT_GEOMETRY_DRIFT")
        original_request = dict(current_request)
        original_request["whole_clip_current_srt"] = original_srt
        validated, reason = validate_mapped_source_context(original_srt, original_request, observation)
        if validated is None:
            return None, reason
        return {
            **validated,
            "context_only_text_carryover": True,
            "original_reviewed_srt_sha256": hashes["reviewed_srt"],
            "current_reviewed_srt_sha256": current_srt_sha,
        }, None
    try:
        binding, geometry = _descriptor_binding(
            descriptor=descriptor, candidate_id=observation["candidate_id"],
            source_media=Path(paths["outer_media"]), subtitle_path=Path(paths["reviewed_srt"]),
        )
    except (NestedMediaAcousticObserverError, OSError, TypeError, ValueError, KeyError):
        return blocked("MAPPING_INVALID")
    if not _same_digest(binding["independent_source_media"]["sha256"], hashes["source_media"]):
        return blocked("SOURCE_DRIFT")
    indexes = current_request.get("cue_indexes")
    target = descriptor.get("target_interval_ms")
    try:
        if (not isinstance(indexes, list) or len(indexes) != 1
                or indexes[0] not in descriptor["target_cue_indexes"]
                or not target[0] <= current_request["matched_start_ms"]
                < current_request["matched_end_ms"] <= target[1]):
            return blocked("OTHER_TARGET")
    except (KeyError, TypeError, IndexError):
        return blocked("TARGET_INVALID")

    envelope = _read_json(paths["native_envelope"])
    receipt = _read_json(paths["provider_receipt"])
    raw = _read_json(paths["raw_response"])
    if not all(isinstance(value, Mapping) for value in (envelope, receipt, raw)):
        return blocked("NATIVE_ARTIFACT_INVALID")
    try:
        broad = envelope["request"]
        prepared = _prepare_request(broad)
        # Measured mappings can land between milliseconds; the native request
        # contract accepts only integer milliseconds. Compare the unique nearest
        # integer window, keeping both the measured and actual crop clocks.
        native_source_window = [round(value) for value in geometry["source_window_ms"]]
        if (envelope.get("schema_version") != "native-context-witness-envelope.v1"
                or envelope.get("request_sha256") != prepared.request_sha256
                or envelope.get("source_media_sha256") != hashes["source_media"]
                or [prepared.physical_start_ms, prepared.physical_end_ms] != native_source_window
                or envelope.get("audio_start_ms") != prepared.physical_start_ms
                or envelope.get("audio_end_ms") != prepared.physical_end_ms):
            return blocked("NATIVE_GEOMETRY_DRIFT")
        metadata, receipt_binding = receipt["evidence"], receipt["binding"]
        native = envelope["native_observation"]
        unlocated = (metadata.get("provider") == "moss"
                     and metadata.get("status") == "TEXT_UNLOCATED"
                     and metadata.get("evidence_scope") == "exact_cue"
                     and metadata.get("native_segments") == []
                     and metadata.get("one_track_srt_eligible") is False
                     and native.get("transcript_basis") == "provider_full_crop_text"
                     and native.get("transcript_timeline_available") is False)
        if (receipt.get("evidence_sha256") != _digest(metadata)
                or metadata.get("raw_response") != raw
                or metadata.get("candidate_exposure") != "none"
                or (metadata.get("status") != "OK" and not unlocated)
                or receipt_binding.get("contract") != "candidate-free-native-secondary-v1"
                or receipt_binding.get("candidate_exposure") != "none"
                or any(receipt_binding.get(key) != metadata.get(key)
                       for key in ("provider", "model", "input_audio_sha256"))
                or receipt_binding.get("duration_ms") != prepared.physical_end_ms - prepared.physical_start_ms
                or metadata.get("input_audio_sha256") != hashes["audio_clip"]):
            return blocked("PROVIDER_ARTIFACT_DRIFT")
        rebuilt = exact_target_evidence(
            metadata, audio=Path(paths["audio_clip"]).read_bytes(),
            source_sha256=hashes["source_media"], start_ms=prepared.physical_start_ms,
            end_ms=prepared.physical_end_ms,
            prefer_provider_text=native.get("transcript_basis") == "provider_full_crop_text",
        )
        if any(native.get(key) != value for key, value in rebuilt.items() if key != "receipt_sha256"):
            return blocked("TRANSCRIPT_DRIFT")
        witness = _native_transcript_witness(
            request=broad, prepared=prepared, native_observation=native,
            source_sha256=hashes["source_media"], provider=metadata["provider"],
        )
        if witness.get("status") != "OBSERVED" or witness != envelope.get("witness"):
            return blocked("WITNESS_DRIFT")
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        return blocked("NATIVE_RECEIPT_INVALID")
    return {
        **dict(observation), "status": "OBSERVED", "evidence_scope": SCOPE,
        "same_cue_direct_transcript_authority": False, "speaker_role_authority": False,
        "current_cue_index": indexes[0], "source_window_ms": geometry["source_window_ms"],
        "native_source_window_ms": native_source_window,
        "source_window_quantization": "NEAREST_INTEGER_MILLISECOND",
        "outer_context_interval_ms": binding["interval_ms"],
        "provider": witness["provider"], "model": witness["model"],
        "source_transcript": native["transcript"],
        "source_native_segments": native.get("source_segments", []),
        "witness_request_sha256": prepared.request_sha256,
        "mapping_receipt": binding["mapping_receipt"],
    }, None
