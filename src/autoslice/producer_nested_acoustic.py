"""Ordinary finalizer handoff for prepared, immutable watched-source inputs.

This collects role evidence and attribution only.  It does not edit subtitles,
change their display style, or turn an acoustic receipt into release authority.
"""
from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import copy
import hashlib
import json
import math
import os
import re
import subprocess
import wave
from collections.abc import Callable, Mapping
from pathlib import Path

from src.autoslice.nested_media_acoustic_observer import (
    NestedMediaAcousticObserverError,
    _canonical_hash,
    _interval,
    _json_mapping,
    _mapping_info,
    observer_target_scope,
    _verify_file,
    _validate_cached_pass,
    _validate_voice_reference_binding,
    VOICE_REFERENCE_MODE,
    observe_nested_media_acoustics,
)
from src.autoslice.package_import import atomic_write_json, exclusive_lock
from src.autoslice.nested_media_caption_attribution import (
    OBSERVATION_SCHEMA,
    build_caption_source_attribution,
)
from src.autoslice.nested_media_caption_dedup import POLICY as CAPTION_DEDUP_POLICY
from src.autoslice.nested_media_caption_dedup import SCHEMA as CAPTION_DEDUP_SCHEMA
from src.autoslice.producer_nested_external_audio import (
    CATALOG_ENV,
    EXTERNAL_AUDIO_SCENE_SOURCE_ROLE,
    normalize_source_controls,
    resolve_external_audio_reference,
)
from src.autoslice.producer_external_audio_acquisition import (
    prepare_external_audio_reference,
)
from src.autoslice.short_reference_playback_locator import (
    ALGORITHM_VERSION as SHORT_REFERENCE_LOCATOR_ALGORITHM,
    MAX_REFERENCE_DURATION_MS as SHORT_REFERENCE_LOCATOR_MAX_MS,
    ShortReferencePlaybackLocatorError,
    locate_short_reference,
    validate_locator,
)
from src.autoslice.subtitle_audio_correspondence import parse_timed_srt

INPUT_SCHEMA = "nested-media-acoustic-inputs.v1"
AUDIT_SCHEMA = "producer-nested-media-acoustic.v1"
PREPARE_SCHEMA = "nested-media-acoustic-audio-preparation.v1"
LOCAL_MAPPING_SCOPE_FIELD = "local_source_mapping_scope"
LOCAL_MAPPING_SCOPE_SCHEMA = "nested-source-audio-local-scope.v1"
CURRENT_CUE_SCOPE_FIELD = "current_cue_scope"
CURRENT_CUE_SCOPE_SCHEMA = "nested-source-audio-current-cue-scope.v1"
CURRENT_CUE_SCOPE_KIND = "CURRENT_WATCHED_MEDIA_CUE"
_NO_CAPTION_SCOPE_KIND = "CURRENT_WATCHED_MEDIA_CUE_NO_CAPTION"
CURRENT_CUE_MAPPING_MODE = "CURRENT_CUE_REVERSE_NCC"
_INPUT_FIELDS = (
    "observation_id", "outer_media_sha256", "final_srt_sha256",
    "caption_frame_path", "caption_frame_sha256", "caption_frame_ms", "interval_ms",
    "independent_source_media_path", "independent_source_media_sha256",
    "mapping_receipt_path", "mapping_receipt_sha256",
    "host_enrollment_path", "host_enrollment_sha256",
    "source_audio_path", "source_audio_sha256", "target_audio_path", "target_audio_sha256",
)
_AUDIO_FIELDS = (
    "source_audio_path", "source_audio_sha256",
    "target_audio_path", "target_audio_sha256",
)
_BASE_INPUT_FIELDS = tuple(field for field in _INPUT_FIELDS if field not in _AUDIO_FIELDS)
_AUDIO_FORMAT = {
    "container": "wav", "codec_name": "pcm_s16le", "sample_fmt": "s16",
    "sample_rate": 16000, "channels": 2,
}
_PREPARE_METHOD = {
    "name": "FFMPEG_T_WINDOW_PCM16_BOUNDED_TRAILING_OVERSHOOT",
    "start_precision_digits": 6,
    "duration_precision_digits": 6,
    "trailing_timestamp_normalization_version": 2,
    "trailing_overshoot_request_ms": 10,
    "max_trailing_overshoot_ms": 20,
}
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9_.-]{1,180}\Z")
_ACOUSTIC_ROLES = frozenset({"source_media", "host", "overlap", "unknown"})
_VOICE_REFERENCE_BINDING_FIELDS = (
    "voice_reference_binding_path", "voice_reference_binding_sha256",
)
_VOICE_REFERENCE_WINDOW_MAX_MS = 6500
_CAPTION_CANDIDATE_HANDOFF_SCHEMA = "nested-media-caption-candidate-handoff.v1"


class _CaptionCandidateHandoffError(ValueError):
    """A sealed handoff could not be replayed without changing its evidence."""

    def __init__(self, attempts: list[dict[str, object]]) -> None:
        super().__init__("CAPTION_CANDIDATE_HANDOFF_UNAVAILABLE")
        self.attempts = attempts


def _sha(path: Path) -> str:
    digest = _evidence_file_sha256(path)
    return "sha256:" + digest


def _safe_segment(value: object, *, code: str) -> str:
    if not isinstance(value, str) or not _SAFE_SEGMENT.fullmatch(value):
        raise NestedMediaAcousticObserverError(code)
    return value


def _same_interval(left: object, right: object) -> bool:
    if (
        isinstance(left, (str, bytes)) or isinstance(right, (str, bytes))
        or not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple))
        or len(left) != 2 or len(right) != 2
    ):
        return False
    try:
        return all(abs(float(a) - float(b)) <= 1e-6 for a, b in zip(left, right))
    except (TypeError, ValueError):
        return False


def _same_hash(left: object, right: object) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return left.removeprefix("sha256:").lower() == right.removeprefix("sha256:").lower()


def _external_voice_reference_seed_status(descriptor: Mapping[str, object]) -> str:
    """Classify a source-bound, but not yet prepared, external voice seed.

    A source URI and full-file hash are provenance for an independent audio
    reference.  They do not create a role decision by themselves.  Keep this
    classification deliberately narrow so partially prepared descriptors do
    not fall through to the legacy BVID/CID mapping path.
    """
    if descriptor.get("observer_mode") == VOICE_REFERENCE_MODE:
        return "ABSENT"
    binding = descriptor.get("source_cache_binding")
    has_external_marker = isinstance(binding, Mapping) and (
        binding.get("kind") == "OFFICIAL_EXTERNAL_AUDIO"
        or any(key in binding for key in ("source_uri", "source_sha256"))
    )
    if not has_external_marker:
        return "ABSENT"
    if not isinstance(binding, Mapping) or binding.get("kind") != "OFFICIAL_EXTERNAL_AUDIO":
        return "INVALID"
    # An external reference must never be mixed with a legacy BVID/CID or a
    # previous mapping/audio/binding receipt.  The prepared helper creates
    # those files only after the current cue and source bytes are validated.
    if any(binding.get(key) not in (None, "") for key in ("bvid", "cid")):
        return "INVALID"
    forbidden = (
        "mapping_receipt_path", "mapping_receipt_sha256", "mapping_receipt",
        *_AUDIO_FIELDS, *_VOICE_REFERENCE_BINDING_FIELDS,
    )
    if any(descriptor.get(field) not in (None, "") for field in forbidden):
        return "INVALID"
    source_path = descriptor.get("independent_source_media_path")
    source_sha = descriptor.get("independent_source_media_sha256")
    source_uri = binding.get("source_uri")
    binding_sha = binding.get("source_sha256")
    if (
        not isinstance(source_path, (str, Path)) or not str(source_path).strip()
        or not isinstance(source_sha, str) or not source_sha.strip()
        or not isinstance(source_uri, str) or not source_uri.strip()
        or not isinstance(binding_sha, str) or not binding_sha.strip()
        or not _same_hash(source_sha, binding_sha)
    ):
        return "INVALID"
    return "VALID"


def _validate_descriptor_local_mapping_scope(
    *, descriptor: Mapping[str, object], scope: object,
    context_interval: list[int], target_interval: list[int], target_cues: list[int],
) -> dict[str, object]:
    """Require a local mapper request to name the current descriptor exactly."""
    if not isinstance(scope, Mapping) or scope.get("schema_version") != LOCAL_MAPPING_SCOPE_SCHEMA:
        raise NestedMediaAcousticObserverError("LOCAL_MAPPING_SCOPE_INVALID")
    try:
        from src.autoslice.nested_source_audio_alignment import _normal_local_scope

        normalized_scope = _normal_local_scope(scope, context_interval)
    except Exception as exc:
        raise NestedMediaAcousticObserverError("LOCAL_MAPPING_SCOPE_INVALID") from exc
    if normalized_scope != dict(scope):
        raise NestedMediaAcousticObserverError("LOCAL_MAPPING_SCOPE_INVALID")
    expected = {
        "observation_id": descriptor.get("observation_id"),
        "descriptor_context_interval_ms": context_interval,
        "target_interval_ms": target_interval,
        "target_cue_indexes": target_cues,
        "caption_frame_path": str(descriptor.get("caption_frame_path")),
        "caption_frame_ms": descriptor.get("caption_frame_ms"),
        "final_srt_sha256": descriptor.get("final_srt_sha256"),
        "caption_frame_sha256": descriptor.get("caption_frame_sha256"),
    }
    for field, value in expected.items():
        actual = scope.get(field)
        if field.endswith("_interval_ms"):
            if not _same_interval(actual, value):
                raise NestedMediaAcousticObserverError("LOCAL_MAPPING_DESCRIPTOR_SCOPE_MISMATCH")
        elif field.endswith("_sha256"):
            if not _same_hash(actual, value):
                raise NestedMediaAcousticObserverError("LOCAL_MAPPING_DESCRIPTOR_SCOPE_MISMATCH")
        elif field == "caption_frame_path":
            try:
                if Path(str(actual)).resolve() != Path(str(value)).resolve():
                    raise NestedMediaAcousticObserverError("LOCAL_MAPPING_DESCRIPTOR_SCOPE_MISMATCH")
            except (OSError, RuntimeError) as exc:
                raise NestedMediaAcousticObserverError("LOCAL_MAPPING_DESCRIPTOR_SCOPE_MISMATCH") from exc
        elif actual != value:
            raise NestedMediaAcousticObserverError("LOCAL_MAPPING_DESCRIPTOR_SCOPE_MISMATCH")
    return dict(scope)


def _validate_local_mapping_receipt_scope(
    *, mapping: Mapping[str, object], descriptor: Mapping[str, object],
) -> None:
    scope = mapping.get("alignment_scope")
    requested = descriptor.get(LOCAL_MAPPING_SCOPE_FIELD)
    if scope is None:
        if requested is not None:
            raise NestedMediaAcousticObserverError("LOCAL_MAPPING_SCOPE_RECEIPT_REQUIRED")
        return
    if requested is None:
        raise NestedMediaAcousticObserverError("LOCAL_MAPPING_SCOPE_NOT_REQUESTED")
    target_interval = descriptor.get("target_interval_ms")
    target_cues = descriptor.get("target_cue_indexes")
    context_interval = descriptor.get("interval_ms")
    if not (
        isinstance(context_interval, list)
        and isinstance(target_interval, list)
        and isinstance(target_cues, list)
    ):
        raise NestedMediaAcousticObserverError("LOCAL_MAPPING_DESCRIPTOR_SCOPE_MISMATCH")
    requested_scope = _validate_descriptor_local_mapping_scope(
        descriptor=descriptor, scope=requested,
        context_interval=[int(item) for item in context_interval],
        target_interval=[int(item) for item in target_interval],
        target_cues=[int(item) for item in target_cues],
    )
    receipt_scope = _validate_descriptor_local_mapping_scope(
        descriptor=descriptor, scope=scope,
        context_interval=[int(item) for item in context_interval],
        target_interval=[int(item) for item in target_interval],
        target_cues=[int(item) for item in target_cues],
    )
    if receipt_scope != requested_scope:
        raise NestedMediaAcousticObserverError("LOCAL_MAPPING_SCOPE_RECEIPT_MISMATCH")


def _validate_descriptor_current_cue_scope(
    *, descriptor: Mapping[str, object], scope: object,
    context_interval: list[int], target_interval: list[int], target_cues: list[int],
    candidate_id: str, media_sha: str, srt_sha: str,
) -> dict[str, object]:
    """Require an automatic mapper request to name the current descriptor."""
    if not isinstance(scope, Mapping) or scope.get("schema_version") != CURRENT_CUE_SCOPE_SCHEMA:
        raise NestedMediaAcousticObserverError("CURRENT_CUE_SCOPE_INVALID")
    try:
        from src.autoslice.nested_source_audio_alignment import _normal_current_cue_scope

        normalized_scope = _normal_current_cue_scope(
            scope, context_interval, candidate_id=candidate_id, outer_media_sha256=media_sha,
        )
    except Exception as exc:
        raise NestedMediaAcousticObserverError("CURRENT_CUE_SCOPE_INVALID") from exc
    if normalized_scope != dict(scope):
        raise NestedMediaAcousticObserverError("CURRENT_CUE_SCOPE_INVALID")
    expected = {
        "kind": CURRENT_CUE_SCOPE_KIND,
        "candidate_id": candidate_id,
        "observation_id": descriptor.get("observation_id"),
        "descriptor_context_interval_ms": context_interval,
        "target_interval_ms": target_interval,
        "target_cue_indexes": target_cues,
        "caption_frame_path": str(descriptor.get("caption_frame_path")),
        "caption_frame_ms": descriptor.get("caption_frame_ms"),
        "final_srt_sha256": srt_sha,
        "outer_media_sha256": media_sha,
        "caption_frame_sha256": descriptor.get("caption_frame_sha256"),
    }
    for field, value in expected.items():
        actual = scope.get(field)
        if field.endswith("_interval_ms"):
            if not _same_interval(actual, value):
                raise NestedMediaAcousticObserverError("CURRENT_CUE_SCOPE_DESCRIPTOR_MISMATCH")
        elif field.endswith("_sha256"):
            if not _same_hash(actual, value):
                raise NestedMediaAcousticObserverError("CURRENT_CUE_SCOPE_DESCRIPTOR_MISMATCH")
        elif field == "caption_frame_path":
            try:
                if Path(str(actual)).resolve() != Path(str(value)).resolve():
                    raise NestedMediaAcousticObserverError("CURRENT_CUE_SCOPE_DESCRIPTOR_MISMATCH")
            except (OSError, RuntimeError) as exc:
                raise NestedMediaAcousticObserverError("CURRENT_CUE_SCOPE_DESCRIPTOR_MISMATCH") from exc
        elif actual != value:
            raise NestedMediaAcousticObserverError("CURRENT_CUE_SCOPE_DESCRIPTOR_MISMATCH")
    return dict(normalized_scope)


def _current_cue_scope_from_descriptor(
    *, descriptor: Mapping[str, object], candidate_id: str,
    context_interval: list[int], target_interval: list[int], target_cues: list[int],
    media_sha: str, srt_sha: str, validated_acoustic_only: bool = False,
) -> dict[str, object] | None:
    """Build automatic scope only for a validated current watched-media frame.

    A matched caption may use the ordinary visual row directly.  An unmatched
    row is eligible only after ``_validate_acoustic_only_timing_scope`` has
    reproduced its current WATCHED_MEDIA scene/frame/cue binding.  The raw
    visual label alone is never enough to authorize the automatic scope.
    """
    visual = descriptor.get("visual_observation")
    if not isinstance(visual, Mapping):
        return None
    acoustic_only_scope = descriptor.get("acoustic_only_timing_scope")
    if isinstance(acoustic_only_scope, Mapping):
        if not validated_acoustic_only:
            return None
    elif visual.get("role") != "WATCHED_MEDIA_CAPTION" or visual.get("caption_present") is not True:
        return None
    if (
        not _same_interval(descriptor.get("interval_ms"), context_interval)
        or not _same_interval(descriptor.get("target_interval_ms"), target_interval)
        or descriptor.get("target_cue_indexes") != list(target_cues)
        or not _same_hash(descriptor.get("outer_media_sha256"), media_sha)
        or not _same_hash(descriptor.get("final_srt_sha256"), srt_sha)
        or visual.get("frame_ms") != descriptor.get("caption_frame_ms")
        or not _same_hash(visual.get("frame_sha256"), descriptor.get("caption_frame_sha256"))
    ):
        raise NestedMediaAcousticObserverError("CURRENT_CUE_SCOPE_DESCRIPTOR_MISMATCH")
    try:
        if Path(str(visual.get("frame_path"))).resolve() != Path(
            str(descriptor.get("caption_frame_path"))
        ).resolve():
            raise NestedMediaAcousticObserverError("CURRENT_CUE_SCOPE_DESCRIPTOR_MISMATCH")
    except (OSError, RuntimeError) as exc:
        raise NestedMediaAcousticObserverError("CURRENT_CUE_SCOPE_DESCRIPTOR_MISMATCH") from exc
    required = (
        "observation_id", "caption_frame_path", "caption_frame_sha256", "caption_frame_ms",
    )
    if any(descriptor.get(field) in (None, "") for field in required):
        return None
    scope = {
        "schema_version": CURRENT_CUE_SCOPE_SCHEMA,
        "kind": CURRENT_CUE_SCOPE_KIND,
        "candidate_id": candidate_id,
        "observation_id": descriptor["observation_id"],
        "descriptor_context_interval_ms": list(context_interval),
        "target_interval_ms": list(target_interval),
        "target_cue_indexes": list(target_cues),
        "caption_frame_path": str(Path(str(descriptor["caption_frame_path"])).resolve()),
        "caption_frame_sha256": descriptor["caption_frame_sha256"],
        "caption_frame_ms": descriptor["caption_frame_ms"],
        "final_srt_sha256": srt_sha,
        "outer_media_sha256": media_sha,
    }
    return _validate_descriptor_current_cue_scope(
        descriptor=descriptor, scope=scope, context_interval=context_interval,
        target_interval=target_interval, target_cues=target_cues,
        candidate_id=candidate_id, media_sha=media_sha, srt_sha=srt_sha,
    )


def _validate_current_mapping_receipt_scope(
    *, mapping: Mapping[str, object], descriptor: Mapping[str, object],
    candidate_id: str, media_sha: str, srt_sha: str,
) -> None:
    receipt_scope = mapping.get(CURRENT_CUE_SCOPE_FIELD)
    requested = descriptor.get(CURRENT_CUE_SCOPE_FIELD)
    if receipt_scope is None:
        if requested is not None:
            raise NestedMediaAcousticObserverError("CURRENT_CUE_SCOPE_RECEIPT_REQUIRED")
        return
    if requested is None:
        raise NestedMediaAcousticObserverError("CURRENT_CUE_SCOPE_NOT_REQUESTED")
    context = descriptor.get("interval_ms")
    target = descriptor.get("target_interval_ms")
    cues = descriptor.get("target_cue_indexes")
    if not (
        isinstance(context, list) and isinstance(target, list) and isinstance(cues, list)
    ):
        raise NestedMediaAcousticObserverError("CURRENT_CUE_SCOPE_DESCRIPTOR_MISMATCH")
    requested_scope = _validate_descriptor_current_cue_scope(
        descriptor=descriptor, scope=requested,
        context_interval=[int(item) for item in context],
        target_interval=[int(item) for item in target],
        target_cues=[int(item) for item in cues], candidate_id=candidate_id,
        media_sha=media_sha, srt_sha=srt_sha,
    )
    receipt_scope = _validate_descriptor_current_cue_scope(
        descriptor=descriptor, scope=receipt_scope,
        context_interval=[int(item) for item in context],
        target_interval=[int(item) for item in target],
        target_cues=[int(item) for item in cues], candidate_id=candidate_id,
        media_sha=media_sha, srt_sha=srt_sha,
    )
    if receipt_scope != requested_scope:
        raise NestedMediaAcousticObserverError("CURRENT_CUE_SCOPE_RECEIPT_MISMATCH")
    if mapping.get("mapping_scope") != CURRENT_CUE_MAPPING_MODE:
        raise NestedMediaAcousticObserverError("CURRENT_CUE_SCOPE_MAPPING_MODE_MISMATCH")


def _path_hash(path: object, expected: object, *, unavailable: str, mismatch: str) -> tuple[Path, str]:
    return _verify_file(path, expected, unavailable=unavailable, mismatch=mismatch)


def _reject_voice_reference_outer_reuse(
    *, source_path: Path, source_sha: str, outer_path: Path, outer_sha: str,
    outer_context: object | None,
    source_identity: Mapping[str, object],
) -> None:
    """Keep a cached source from becoming the current outer mix by relabeling."""
    try:
        same_path = source_path.resolve() == outer_path.resolve()
    except (OSError, RuntimeError) as exc:
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_SOURCE_IDENTITY_UNAVAILABLE") from exc
    if same_path:
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_SOURCE_IS_OUTER_MEDIA")
    if _same_hash(source_sha, outer_sha):
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_SOURCE_BYTES_ARE_OUTER_MEDIA")
    if outer_context is None:
        return
    if not isinstance(outer_context, Mapping):
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_OUTER_CONTEXT_INVALID")

    # The current outer-context contract exposes these exact entries.  Validate
    # their actual files before using them as an identity witness; never infer
    # a BVID/CID from a candidate id, basename, or cache directory.
    for entry_name in ("context_media", "current_outer"):
        entry = outer_context.get(entry_name)
        if entry is None:
            continue
        if not isinstance(entry, Mapping):
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_OUTER_CONTEXT_INVALID")
        context_path, context_sha = _path_hash(
            entry.get("path"), entry.get("sha256"),
            unavailable="VOICE_REFERENCE_OUTER_CONTEXT_UNAVAILABLE",
            mismatch="VOICE_REFERENCE_OUTER_CONTEXT_HASH_MISMATCH",
        )
        try:
            context_same_path = source_path.resolve() == context_path.resolve()
        except (OSError, RuntimeError) as exc:
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_OUTER_CONTEXT_INVALID") from exc
        if context_same_path:
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_SOURCE_IS_OUTER_CONTEXT")
        if _same_hash(source_sha, context_sha):
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_SOURCE_BYTES_ARE_OUTER_CONTEXT")
        # Some callers may carry the already-identified outer BVID/CID in this
        # existing context entry.  Compare only that explicit identity; absent
        # fields remain absent and are never synthesized here.
        outer_bvid, outer_cid = entry.get("bvid"), entry.get("cid")
        if outer_bvid is None and outer_cid is None:
            continue
        if (
            not isinstance(outer_bvid, str) or not outer_bvid.strip()
            or isinstance(outer_cid, bool) or outer_cid in (None, "")
        ):
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_OUTER_CONTEXT_IDENTITY_INVALID")
        if (
            str(source_identity.get("bvid")) == outer_bvid.strip()
            and str(source_identity.get("cid")) == str(outer_cid)
        ):
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_SOURCE_IDENTITY_IS_OUTER")


def _read_caption_candidate_handoffs(
    *, spec: Mapping[str, object], candidate_id: str, source_media: Path,
    subtitle_path: Path, source_cache_root: Path, media_sha: str, srt_sha: str,
) -> tuple[dict[str, object], dict[str, object]]:
    """Replay one exact caption discovery receipt for a current consumer.

    The handoff names the original locator file and its hash only.  The old
    locator is read as-is, so its discovery frame/request cannot be rewritten
    to the current descriptor's frame.  The returned candidate remains an
    unverified byte/cache fact for the current mapping and relevance gates.
    """
    raw_handoffs = spec.get("nested_media_caption_candidate_handoffs")
    if not isinstance(raw_handoffs, list) or not raw_handoffs:
        raise _CaptionCandidateHandoffError([{
            "index": None, "reason_code": "CAPTION_CANDIDATE_HANDOFFS_INVALID",
        }])
    from src.autoslice.nested_source_cache import read_cached_caption_source_candidate

    attempts: list[dict[str, object]] = []
    for index, raw in enumerate(raw_handoffs):
        if (
            not isinstance(raw, Mapping)
            or not {"locator_path", "locator_sha256"} <= set(raw)
            or not set(raw) <= {"locator_path", "locator_sha256", "locator_field"}
        ):
            attempts.append({"index": index, "reason_code": "CAPTION_CANDIDATE_HANDOFF_ITEM_INVALID"})
            continue
        try:
            locator_path, locator_sha = _path_hash(
                raw.get("locator_path"), raw.get("locator_sha256"),
                unavailable="CAPTION_CANDIDATE_LOCATOR_UNAVAILABLE",
                mismatch="CAPTION_CANDIDATE_LOCATOR_HASH_MISMATCH",
            )
            envelope = _json_mapping(locator_path, code="CAPTION_CANDIDATE_LOCATOR_INVALID")
            locator_field = raw.get("locator_field")
            if locator_field is not None and locator_field not in {"locator", "fresh_source_locator"}:
                raise ValueError("CAPTION_CANDIDATE_LOCATOR_FIELD_INVALID")
            locator = (
                envelope.get(locator_field)
                if isinstance(locator_field, str) else envelope
            )
            if not isinstance(locator, Mapping):
                raise ValueError("CAPTION_CANDIDATE_LOCATOR_INVALID")
            if any(
                key in value and value.get(key) is not False
                for value in (envelope, locator)
                for key in ("source_role_authority", "mutation_authorized")
            ):
                raise ValueError("CAPTION_CANDIDATE_LOCATOR_AUTHORITY_CLAIM")
            request_sha = locator.get("request_sha256")
            frame = locator.get("frame")
            if not isinstance(request_sha, str) or not request_sha or not isinstance(frame, Mapping):
                raise ValueError("CAPTION_CANDIDATE_LOCATOR_BINDING_REQUIRED")
            cached = read_cached_caption_source_candidate(
                cache_root=source_cache_root, locator=locator, candidate_id=candidate_id,
                source_media=source_media, subtitle_path=subtitle_path,
            )
            if cached.get("status") != "CANDIDATE_CACHE_AVAILABLE":
                raise ValueError(str(cached.get("reason_code") or "SOURCE_CACHE_NO_AVAILABLE_PUBLIC_CANDIDATE"))
            if (
                cached.get("source_role_authority") is not False
                or cached.get("mutation_authorized") is not False
                or cached.get("uniqueness_claim") is not False
                or not _hashes_match(cached.get("source_media_sha256"), media_sha)
                or not _hashes_match(cached.get("final_srt_sha256"), srt_sha)
            ):
                raise ValueError("CAPTION_CANDIDATE_HANDOFF_BINDING_MISMATCH")
            provenance = {
                "schema_version": _CAPTION_CANDIDATE_HANDOFF_SCHEMA,
                "status": "REPLAYED",
                "action": "KEEP",
                "mutation_authorized": False,
                "source_role_authority": False,
                "uniqueness_claim": False,
                "locator_path": str(locator_path),
                "locator_sha256": locator_sha,
                "locator_field": locator_field,
                "envelope_path": str(locator_path),
                "envelope_sha256": locator_sha,
                "locator_candidate_id": locator.get("candidate_id"),
                "locator_request_sha256": request_sha,
                "locator_frame": copy.deepcopy(dict(frame)),
                "current_candidate_id": candidate_id,
                "current_outer_media": {
                    "path": str(Path(source_media).resolve()), "sha256": media_sha,
                },
                "current_final_srt": {
                    "path": str(Path(subtitle_path).resolve()), "sha256": srt_sha,
                },
                "candidate_cache": {
                    "status": cached.get("status"),
                    "independent_source_media_path": cached.get("independent_source_media_path"),
                    "independent_source_media_sha256": cached.get("independent_source_media_sha256"),
                    "source_cache_binding": copy.deepcopy(cached.get("source_cache_binding")),
                    "locator_candidate_id": cached.get("locator_candidate_id"),
                    "consumer_candidate_id": cached.get("consumer_candidate_id"),
                    "caption_candidate_discovery_request_sha256": cached.get(
                        "caption_candidate_discovery_request_sha256"
                    ),
                },
                "attempts": attempts,
            }
            return cached, provenance
        except Exception as exc:
            reason = str(exc)
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", reason):
                reason = type(exc).__name__
            attempts.append({"index": index, "reason_code": reason})
    raise _CaptionCandidateHandoffError(attempts)


def _verified_receipt_role(attribution: Mapping[str, object]) -> str | None:
    """Return a role only from the receipt reread by attribution validation.

    ``observe_nested_media_acoustics`` already checks the provider result before
    writing its receipt.  The extra state check here makes the handoff consume
    the current hash-bound receipt rather than trusting the in-memory
    ``final_class`` returned by that call.  Attribution may still force the
    visual row to ``unknown``/``KEEP`` when the caption is unmatched; that is a
    separate display decision.
    """
    rows = attribution.get("rows")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        return None
    row = rows[0]
    receipt = row.get("acoustic_receipt")
    if not isinstance(receipt, Mapping) or receipt.get("status") != "PASS":
        return None
    source = receipt.get("source_media_speech")
    host = receipt.get("host_speech")
    overlap = receipt.get("overlap_speech")
    if source == "PRESENT" and host == "ABSENT" and overlap == "ABSENT":
        derived = "source_media"
    elif source == "PRESENT" and overlap == "PRESENT":
        derived = "overlap"
    elif source == "ABSENT" and host == "PRESENT" and overlap == "ABSENT":
        derived = "host"
    else:
        derived = "unknown"
    role = receipt.get("final_class")
    if role not in _ACOUSTIC_ROLES or role != derived:
        return None
    return str(role)


_PROVIDER_ATTEMPT_FIELDS = (
    "free_key_ordinal", "status", "error_type", "category", "http_status",
    "provider", "transport", "job_invocations", "historical_job_invocations",
    "replayed_completion", "underlying_api_call_count",
)


def _observer_provider_trace(
    observer_result: Mapping[str, object], evidence_root: Path,
) -> dict[str, object] | None:
    """Read the typed provider trace written by the observer protocol.

    ``observe_nested_media_acoustics`` intentionally returns an evidence-only
    envelope after a provider error.  The envelope's reason is useful for
    attribution, but the non-secret HTTP/category/attempt details live in its
    hash-bound ``observer-response.json``.  Read that file only beneath this
    run's evidence root and copy the small typed subset needed by the producer
    audit; never copy provider completions or credential-bearing values.
    """
    request_value = observer_result.get("request_path")
    request_sha = observer_result.get("request_sha256")
    response_value = observer_result.get("observer_response_path")
    if (
        not isinstance(request_value, str) or not request_value
        or not isinstance(request_sha, str) or not request_sha
        or not isinstance(response_value, str) or not response_value
    ):
        return None
    try:
        root = evidence_root.expanduser().absolute().resolve(strict=True)
        request = Path(request_value).expanduser()
        resolved_request = request.resolve(strict=True)
        resolved_request.relative_to(root)
        if request.is_symlink() or not resolved_request.is_file():
            return None
        response = Path(response_value).expanduser()
        resolved = response.resolve(strict=True)
        resolved.relative_to(root)
        if response.is_symlink() or not resolved.is_file():
            return None
        request_bytes = resolved_request.read_bytes()
        request_payload = json.loads(request_bytes.decode("utf-8"))
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(request_payload, Mapping) or not isinstance(payload, Mapping):
        return None
    request_without_sha = dict(request_payload)
    request_without_sha.pop("request_sha256", None)
    expected_request_bytes = (
        json.dumps(request_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if (
        request_payload.get("request_sha256") != request_sha
        or _canonical_hash(request_without_sha) != request_sha
        or request_bytes != expected_request_bytes
        or payload.get("request_sha256") != request_sha
    ):
        return None
    evidence = payload.get("evidence")
    if not isinstance(evidence, Mapping):
        return None
    raw_attempts = evidence.get("provider_attempts")
    attempts: list[dict[str, object]] = []
    if isinstance(raw_attempts, list):
        for raw in raw_attempts:
            if not isinstance(raw, Mapping):
                continue
            attempt: dict[str, object] = {}
            for field in _PROVIDER_ATTEMPT_FIELDS:
                value = raw.get(field)
                if field in {"http_status", "free_key_ordinal", "job_invocations",
                             "historical_job_invocations"}:
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        attempt[field] = value
                elif field == "replayed_completion":
                    if isinstance(value, bool):
                        attempt[field] = value
                elif field == "provider":
                    if isinstance(value, str) and value in {"agy", "gemini"}:
                        attempt[field] = value
                elif field == "transport":
                    if value == "agy_three_part":
                        attempt[field] = value
                elif field == "underlying_api_call_count":
                    if value == "UNKNOWN_UNEXPOSED":
                        attempt[field] = value
                elif isinstance(value, str) and value:
                    attempt[field] = value
            if attempt:
                attempts.append(attempt)
    trace: dict[str, object] = {
        "replayed": observer_result.get("replayed") is True,
        "request_path": str(resolved_request),
        "request_file_sha256": _sha(resolved_request),
        "request_sha256": request_sha,
        "observer_response_path": str(resolved),
        "observer_response_sha256": _sha(resolved),
        "reason_code": evidence.get("reason_code")
        if isinstance(evidence.get("reason_code"), str)
        else None,
        "provider_attempts": attempts,
    }
    for field in ("error_type", "http_status"):
        value = evidence.get(field)
        if field == "http_status":
            if isinstance(value, int) and not isinstance(value, bool):
                trace[field] = value
        elif isinstance(value, str) and value:
            trace[field] = value
    for field in ("provider_request_sha256", "provider_response_sha256"):
        value = evidence.get(field)
        if isinstance(value, str) and value:
            trace[field] = value
    return trace


def _probe_pcm16_wav(path: Path, *, expected_duration_ms: float) -> dict[str, object]:
    argv = [
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_name,sample_fmt,sample_rate,channels:format=duration",
        "-of", "json", str(path),
    ]
    try:
        completed = subprocess.run(
            argv, check=True, capture_output=True, text=True,
        )
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError) as exc:
        raise NestedMediaAcousticObserverError("PCM16_AUDIO_PROBE_FAILED") from exc
    if not isinstance(payload, Mapping):
        raise NestedMediaAcousticObserverError("PCM16_AUDIO_PROBE_INVALID")
    streams = payload.get("streams")
    formats = payload.get("format")
    if not isinstance(streams, list) or not streams or not isinstance(streams[0], Mapping):
        raise NestedMediaAcousticObserverError("PCM16_AUDIO_PROBE_INVALID")
    stream = streams[0]
    if (
        stream.get("codec_name") != _AUDIO_FORMAT["codec_name"]
        or stream.get("sample_fmt") != _AUDIO_FORMAT["sample_fmt"]
        or str(stream.get("sample_rate")) != str(_AUDIO_FORMAT["sample_rate"])
        or stream.get("channels") != _AUDIO_FORMAT["channels"]
    ):
        raise NestedMediaAcousticObserverError("PCM16_AUDIO_FORMAT_INVALID")
    if not isinstance(formats, Mapping):
        raise NestedMediaAcousticObserverError("PCM16_AUDIO_PROBE_INVALID")
    try:
        duration = float(formats["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise NestedMediaAcousticObserverError("PCM16_AUDIO_DURATION_INVALID") from exc
    expected_duration = float(expected_duration_ms) / 1000.0
    if not math.isfinite(duration) or abs(duration - expected_duration) > 0.02:
        raise NestedMediaAcousticObserverError("PCM16_AUDIO_DURATION_MISMATCH")
    expected_frames = round(expected_duration * _AUDIO_FORMAT["sample_rate"])
    try:
        with wave.open(str(path), "rb") as handle:
            wave_info = {
                "channels": handle.getnchannels(),
                "sample_width": handle.getsampwidth(),
                "sample_rate": handle.getframerate(),
                "frames": handle.getnframes(),
            }
    except (OSError, wave.Error) as exc:
        raise NestedMediaAcousticObserverError("PCM16_AUDIO_WAVE_INVALID") from exc
    if (
        wave_info["channels"] != _AUDIO_FORMAT["channels"]
        or wave_info["sample_width"] != 2
        or wave_info["sample_rate"] != _AUDIO_FORMAT["sample_rate"]
        or wave_info["frames"] != expected_frames
    ):
        raise NestedMediaAcousticObserverError("PCM16_AUDIO_FRAME_COUNT_MISMATCH")
    return {
        "argv": argv,
        "format": dict(formats),
        "streams": [dict(stream)],
        "wave": wave_info,
        "duration_ms": round(duration * 1000.0, 3),
    }


def _normalize_trailing_pcm_frames(path: Path, *, expected_duration_ms: float) -> dict[str, object] | None:
    """Keep the requested start and remove only bounded trailing timestamp overshoot.

    ffmpeg output seeking/resampling can leave a fractional packet at the end
    of an otherwise exact window.  Never pad short audio or accept a wider
    window: retain the unmodified extraction and probe the derived WAV strictly.
    """
    expected_frames = round(expected_duration_ms * _AUDIO_FORMAT["sample_rate"] / 1000)
    try:
        with wave.open(str(path), "rb") as handle:
            params = handle.getparams()
            if (params.nchannels, params.sampwidth, params.framerate, params.comptype) != (2, 2, 16000, "NONE"):
                raise NestedMediaAcousticObserverError("PCM16_AUDIO_FORMAT_INVALID")
            extra_frames = params.nframes - expected_frames
            if extra_frames == 0:
                return None
            max_extra = round(_PREPARE_METHOD["max_trailing_overshoot_ms"] * 16000 / 1000)
            if not 0 < extra_frames <= max_extra:
                raise NestedMediaAcousticObserverError("PCM16_AUDIO_FRAME_COUNT_MISMATCH")
            frames = handle.readframes(params.nframes)
            if len(frames) != params.nframes * 4:
                raise NestedMediaAcousticObserverError("PCM16_AUDIO_WAVE_INVALID")
        raw = path.with_name(path.name + ".timestamp-overshoot.wav")
        if raw.exists() or raw.is_symlink():
            raise NestedMediaAcousticObserverError("PCM16_AUDIO_OUTPUT_ALREADY_EXISTS")
        raw_sha = _sha(path)
        path.rename(raw)
        with path.open("xb") as output, wave.open(output, "wb") as handle:
            handle.setparams(params)
            handle.writeframes(frames[:expected_frames * 4])
    except (OSError, wave.Error) as exc:
        raise NestedMediaAcousticObserverError("PCM16_AUDIO_NORMALIZATION_FAILED") from exc
    return {
        "method": "PRESERVE_START_TRIM_TRAILING_FRAMES",
        "raw_path": str(raw), "raw_sha256": raw_sha,
        "raw_frames": params.nframes, "expected_frames": expected_frames,
        "trimmed_frames": extra_frames, "trimmed_ms": extra_frames / 16,
    }


def _extract_pcm16_wav(
    *, source: Path, output: Path, window_ms: list[int | float],
) -> tuple[list[str], dict[str, object]]:
    if output.exists() or output.is_symlink():
        raise NestedMediaAcousticObserverError("PCM16_AUDIO_OUTPUT_ALREADY_EXISTS")
    start_ms, end_ms = (float(item) for item in window_ms)
    duration_ms = end_ms - start_ms
    # Codec packet boundaries can make an exact -t crop a few PCM frames
    # short.  Request a bounded tail and let the existing strict normalizer
    # remove it; the normalizer still rejects every short result.
    requested_duration_ms = duration_ms + float(_PREPARE_METHOD["trailing_overshoot_request_ms"])
    argv = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
        "-ss", f"{start_ms / 1000.0:.6f}",
        "-t", f"{requested_duration_ms / 1000.0:.6f}",
        "-vn", "-ar", "16000", "-ac", "2",
        "-c:a", "pcm_s16le", "-f", "wav",
        str(output),
    ]
    try:
        subprocess.run(argv, check=True, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise NestedMediaAcousticObserverError("PCM16_AUDIO_EXTRACTION_FAILED") from exc
    if not output.is_file() or output.is_symlink():
        raise NestedMediaAcousticObserverError("PCM16_AUDIO_OUTPUT_MISSING")
    normalization = _normalize_trailing_pcm_frames(output, expected_duration_ms=duration_ms)
    probe = _probe_pcm16_wav(output, expected_duration_ms=duration_ms)
    if normalization is not None:
        probe["trailing_timestamp_normalization"] = normalization
    return argv, probe


def _probe_media_duration_ms(source: Path) -> int | float:
    """Read the independent source duration for the bounded voice excerpt."""
    argv = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(source),
    ]
    try:
        completed = subprocess.run(argv, check=True, capture_output=True, text=True)
        payload = json.loads(completed.stdout)
        duration = float(payload["format"]["duration"])
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError,
            UnicodeError, json.JSONDecodeError) as exc:
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_SOURCE_DURATION_FAILED") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_SOURCE_DURATION_INVALID")
    # Preserve the complete fractional-millisecond tail of short recordings.
    # Flooring before PCM extraction silently discarded source samples.
    duration_ms = round(duration * 1000.0, 3)
    if duration_ms <= 0:
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_SOURCE_DURATION_INVALID")
    bounded = min(_VOICE_REFERENCE_WINDOW_MAX_MS, duration_ms)
    return int(bounded) if float(bounded).is_integer() else bounded


def _automatic_voice_descriptor(
    *, descriptor: Mapping[str, object], source_path: Path, source_sha: str,
    source_cache_binding: Mapping[str, object], target_interval: list[int],
    target_cues: list[object], context_interval: list[int], binding_path: Path,
    binding_sha: str,
    audio_fields: Mapping[str, object] | None = None,
    playback_locator: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Make a private voice descriptor without carrying alignment receipts."""
    target_scope = observer_target_scope(
        interval_ms=context_interval, target_interval_ms=target_interval,
        target_cue_indexes=target_cues, target_audio_window_ms=context_interval,
    )
    dropped = set(_AUDIO_FIELDS) | set(_VOICE_REFERENCE_BINDING_FIELDS) | {
        "mapping_receipt_path", "mapping_receipt_sha256", CURRENT_CUE_SCOPE_FIELD,
        LOCAL_MAPPING_SCOPE_FIELD, "mapping_receipt", "observer_mode", "source_cache_binding",
        "playback_locator",
    }
    result = {key: value for key, value in descriptor.items() if key not in dropped}
    result.update(
        observer_mode=VOICE_REFERENCE_MODE,
        independent_source_media_path=str(source_path),
        independent_source_media_sha256=source_sha,
        source_cache_binding=copy.deepcopy(dict(source_cache_binding)),
        target_interval_ms=list(target_interval),
        target_audio_window_ms=list(target_scope["target_audio_window_ms"]),
        target_audio_offset_ms=list(target_scope["target_audio_offset_ms"]),
        target_cue_indexes=list(target_cues),
        voice_reference_binding_path=str(binding_path),
        voice_reference_binding_sha256=binding_sha,
    )
    if audio_fields is not None:
        result.update(dict(audio_fields))
    if playback_locator is not None:
        result["playback_locator"] = copy.deepcopy(dict(playback_locator))
    return result


def _voice_reference_binding_payload(
    *, candidate_id: str, observation_id: str, outer: tuple[Path, str],
    srt: tuple[Path, str], frame: tuple[Path, str, int], source: tuple[Path, str],
    host: tuple[Path, str], source_identity: Mapping[str, object],
    context_interval: list[int], reference_window: list[int],
    target_interval: list[int], target_cues: list[object],
    source_audio: tuple[Path, str] | None = None,
    target_audio: tuple[Path, str] | None = None,
    playback_locator: Mapping[str, object] | None = None,
) -> dict[str, object]:
    target_scope = observer_target_scope(
        interval_ms=context_interval, target_interval_ms=target_interval,
        target_cue_indexes=target_cues, target_audio_window_ms=context_interval,
    )
    external = source_identity.get("kind") == "OFFICIAL_EXTERNAL_AUDIO"
    if external:
        source_uri = source_identity.get("source_uri")
        source_full_sha = source_identity.get("source_sha256")
        if (
            not isinstance(source_uri, str) or not source_uri.strip()
            or not isinstance(source_full_sha, str)
            or not _same_hash(source_full_sha, source[1])
        ):
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_SOURCE_IDENTITY_INVALID")
        source_identity_payload = {
            "kind": "OFFICIAL_EXTERNAL_AUDIO",
            "source_uri": source_uri.strip(),
            "source_sha256": source_full_sha,
        }
    else:
        if any(key in source_identity for key in ("kind", "source_uri", "source_sha256")):
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_SOURCE_IDENTITY_INVALID")
        bvid = source_identity.get("bvid")
        cid = source_identity.get("cid")
        if not isinstance(bvid, str) or not bvid or cid in (None, "") or isinstance(cid, bool):
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_SOURCE_IDENTITY_INVALID")
        cid_text = str(cid)
    payload: dict[str, object] = {
        "schema_version": "nested-media-voice-reference-binding.v1",
        "status": "REFERENCE_BOUND", "mode": VOICE_REFERENCE_MODE,
        "candidate_id": candidate_id, "observation_id": observation_id,
        "outer_media": {"path": str(outer[0]), "sha256": outer[1]},
        "final_srt": {"path": str(srt[0]), "sha256": srt[1]},
        "caption_frame": {
            "path": str(frame[0]), "sha256": frame[1], "frame_ms": frame[2],
        },
        "source_media": {
            "path": str(source[0]), "sha256": source[1],
        },
        "host_enrollment": {"path": str(host[0]), "sha256": host[1]},
        "outer_context_interval_ms": list(context_interval),
        "reference_window_ms": list(reference_window),
        "target_interval_ms": list(target_interval),
        "target_cue_indexes": list(target_cues),
        "target_audio_window_ms": list(target_scope["target_audio_window_ms"]),
        "target_audio_offset_ms": list(target_scope["target_audio_offset_ms"]),
        "time_alignment_performed": False,
        "source_subtraction_authorized": False,
        "caption_dedup_authority": False,
        "source_role_authority": False,
        "mutation_authorized": False,
    }
    if external:
        payload["source_identity"] = source_identity_payload
        payload["source_media"].update({
            "source_uri": source_identity_payload["source_uri"],
            "source_sha256": source_identity_payload["source_sha256"],
        })
    else:
        payload["bvid"] = bvid
        payload["cid"] = cid_text
        payload["source_media"].update({"bvid": bvid, "cid": cid_text})
    if source_audio is not None:
        payload["source_audio"] = {"path": str(source_audio[0]), "sha256": source_audio[1]}
    if target_audio is not None:
        payload["target_audio"] = {"path": str(target_audio[0]), "sha256": target_audio[1]}
    if playback_locator is not None:
        payload["playback_locator"] = copy.deepcopy(dict(playback_locator))
    return payload


def _voice_reference_locator_context(
    *, candidate_id: str, observation_id: str,
    outer: tuple[Path, str], srt: tuple[Path, str], frame: tuple[Path, str, int],
    target_interval: list[int], target_cues: list[object],
) -> dict[str, object]:
    """Return the immutable current-input context sealed into a locator hint."""
    return {
        "candidate_id": candidate_id,
        "observation_id": observation_id,
        "outer_media": {"path": str(outer[0]), "sha256": outer[1]},
        "final_srt": {"path": str(srt[0]), "sha256": srt[1]},
        "caption_frame": {
            "path": str(frame[0]), "sha256": frame[1], "frame_ms": frame[2],
        },
        "target_interval_ms": list(target_interval),
        "target_cue_indexes": list(target_cues),
    }


def _voice_reference_prepare_request(
    *, binding: Mapping[str, object], reference_window: list[int],
    target_interval: list[int], context_interval: list[int],
) -> dict[str, object]:
    target_scope = observer_target_scope(
        interval_ms=context_interval, target_interval_ms=target_interval,
        target_cue_indexes=binding.get("target_cue_indexes"),
        target_audio_window_ms=context_interval,
    )
    return {
        "schema_version": PREPARE_SCHEMA,
        "method": dict(_PREPARE_METHOD),
        **dict(binding),
        "source_window_ms": list(reference_window),
        "target_window_ms": list(context_interval),
        "target_audio_window_ms": list(target_scope["target_audio_window_ms"]),
        "target_audio_offset_ms": list(target_scope["target_audio_offset_ms"]),
    }


def _voice_reference_audio_fields(payload: Mapping[str, object]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for prefix in ("source", "target"):
        item = payload.get(f"{prefix}_audio")
        if not isinstance(item, Mapping):
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_PREPARE_INCOMPLETE")
        path, value = item.get("path"), item.get("sha256")
        if not isinstance(path, str) or not path or not isinstance(value, str) or not value:
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_PREPARE_INVALID")
        fields[f"{prefix}_audio_path"] = path
        fields[f"{prefix}_audio_sha256"] = value
    return fields


def _prepare_automatic_voice_reference(
    *, descriptor: Mapping[str, object], candidate_id: str,
    source_media: Path, subtitle_path: Path, evidence_root: Path,
    media_sha: str, srt_sha: str, source_cache: Mapping[str, object],
    context_interval: list[int], target_interval: list[int],
    target_cues: list[object], outer_context: object | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Create or replay one bounded single-cue unaligned voice request."""
    if descriptor.get("observer_mode") not in (None, ""):
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_DESCRIPTOR_PREEXISTING")
    if any(descriptor.get(field) not in (None, "") for field in _AUDIO_FIELDS):
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_AUDIO_PREEXISTING")
    if any(descriptor.get(field) not in (None, "") for field in _VOICE_REFERENCE_BINDING_FIELDS):
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_BINDING_PREEXISTING")
    if len(target_cues) != 1:
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_TARGET_SINGLE_CUE_REQUIRED")
    source_value = source_cache.get("independent_source_media_path")
    source_hash = source_cache.get("independent_source_media_sha256")
    cache_binding = source_cache.get("source_cache_binding")
    if not isinstance(cache_binding, Mapping):
        raise NestedMediaAcousticObserverError("SOURCE_CACHE_BINDING_REQUIRED")
    source_path, source_sha = _path_hash(
        source_value, source_hash,
        unavailable="INDEPENDENT_SOURCE_UNAVAILABLE",
        mismatch="INDEPENDENT_SOURCE_HASH_MISMATCH",
    )
    if not _same_hash(source_sha, source_hash):
        raise NestedMediaAcousticObserverError("SOURCE_CACHE_MEDIA_HASH_MISMATCH")
    if cache_binding.get("kind") == "OFFICIAL_EXTERNAL_AUDIO":
        # An external recording is a byte-bound voice candidate, never a
        # fabricated Bilibili source or an aligned playback identity.
        source_identity = {
            "kind": "OFFICIAL_EXTERNAL_AUDIO",
            "source_uri": cache_binding.get("source_uri"),
            "source_sha256": cache_binding.get("source_sha256"),
        }
        if (not isinstance(source_identity["source_uri"], str)
                or not source_identity["source_uri"].strip()
                or not _same_hash(source_identity["source_sha256"], source_sha)):
            raise NestedMediaAcousticObserverError("SOURCE_CACHE_IDENTITY_INVALID")
    else:
        source_identity = {
            "bvid": cache_binding.get("bvid"), "cid": cache_binding.get("cid"),
        }
    if source_identity.get("kind") != "OFFICIAL_EXTERNAL_AUDIO" and (
        not isinstance(source_identity["bvid"], str)
        or not source_identity["bvid"]
        or isinstance(source_identity["cid"], bool)
        or source_identity["cid"] in (None, "")
    ):
        raise NestedMediaAcousticObserverError("SOURCE_CACHE_IDENTITY_INVALID")
    outer_path, outer_actual_sha = _path_hash(
        source_media, media_sha,
        unavailable="OUTER_MEDIA_UNAVAILABLE", mismatch="OUTER_MEDIA_HASH_MISMATCH",
    )
    _reject_voice_reference_outer_reuse(
        source_path=source_path, source_sha=source_sha,
        outer_path=outer_path, outer_sha=outer_actual_sha,
        outer_context=outer_context, source_identity=source_identity,
    )
    srt_path, srt_actual_sha = _path_hash(
        subtitle_path, srt_sha,
        unavailable="FINAL_SRT_UNAVAILABLE", mismatch="FINAL_SRT_HASH_MISMATCH",
    )
    frame_path, frame_sha = _path_hash(
        descriptor.get("caption_frame_path"), descriptor.get("caption_frame_sha256"),
        unavailable="CAPTION_FRAME_UNAVAILABLE", mismatch="CAPTION_FRAME_HASH_MISMATCH",
    )
    frame_ms = descriptor.get("caption_frame_ms")
    if isinstance(frame_ms, bool) or not isinstance(frame_ms, int):
        raise NestedMediaAcousticObserverError("CAPTION_FRAME_INTERVAL_MISMATCH")
    host_path, host_sha = _path_hash(
        descriptor.get("host_enrollment_path"), descriptor.get("host_enrollment_sha256"),
        unavailable="HOST_ENROLLMENT_UNAVAILABLE", mismatch="HOST_ENROLLMENT_HASH_MISMATCH",
    )
    observation_id = _safe_segment(descriptor.get("observation_id"), code="OBSERVATION_ID_INVALID")
    _safe_segment(candidate_id, code="CANDIDATE_ID_INVALID")
    source_duration_ms = _probe_media_duration_ms(source_path)
    reference_window = [0, min(_VOICE_REFERENCE_WINDOW_MAX_MS, source_duration_ms)]
    locator_context = _voice_reference_locator_context(
        candidate_id=candidate_id, observation_id=observation_id,
        outer=(outer_path, outer_actual_sha), srt=(srt_path, srt_actual_sha),
        frame=(frame_path, frame_sha, frame_ms), target_interval=target_interval,
        target_cues=target_cues,
    )
    base_payload = _voice_reference_binding_payload(
        candidate_id=candidate_id, observation_id=observation_id,
        outer=(outer_path, outer_actual_sha), srt=(srt_path, srt_actual_sha),
        frame=(frame_path, frame_sha, frame_ms), source=(source_path, source_sha),
        host=(host_path, host_sha), source_identity=source_identity,
        context_interval=context_interval, reference_window=reference_window,
        target_interval=target_interval, target_cues=target_cues,
    )
    root = Path(evidence_root).expanduser().absolute()
    if root.exists() and root.is_symlink():
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_PREPARE_ROOT_UNSAFE")
    candidate_dir = root / _safe_segment(candidate_id, code="CANDIDATE_ID_INVALID")
    observation_dir = candidate_dir / _safe_segment(observation_id, code="OBSERVATION_ID_INVALID")
    for directory in (candidate_dir, observation_dir):
        if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_PREPARE_ROOT_UNSAFE")
    preparation_identity = dict(base_payload)
    if (cache_binding.get("kind") == "OFFICIAL_EXTERNAL_AUDIO"
            and source_duration_ms < SHORT_REFERENCE_LOCATOR_MAX_MS):
        # Preserve the old PCM/binding/cache intact. A pre-locator preparation
        # must not hide newly available deterministic evidence from this run.
        preparation_identity["short_reference_locator_algorithm"] = SHORT_REFERENCE_LOCATOR_ALGORITHM
    request_dir = observation_dir / (
        "voice-reference-" + _canonical_hash(preparation_identity).removeprefix("sha256:")
    )
    binding_path = request_dir / "VOICE-REFERENCE.json"
    source_output = request_dir / "independent-source.pcm16.wav"
    target_output = request_dir / "current-target.pcm16.wav"
    manifest_path = request_dir / "PREPARE.json"
    with exclusive_lock(observation_dir / "voice-reference.lock", label="voice reference prepare"):
        request_dir.mkdir(parents=True, exist_ok=True)
        existing = [path.exists() or path.is_symlink() for path in
                    (binding_path, source_output, target_output, manifest_path)]
        if any(existing):
            if not all(existing):
                raise NestedMediaAcousticObserverError("VOICE_REFERENCE_PREPARE_INCOMPLETE")
            binding_file, binding_sha = _current_file_hash(
                binding_path, unavailable="VOICE_REFERENCE_BINDING_UNAVAILABLE"
            )
            payload = _json_mapping(binding_file, code="VOICE_REFERENCE_BINDING_INVALID")
            audio_fields = _voice_reference_audio_fields(payload)
            persisted_locator = payload.get("playback_locator")
            validated_locator: Mapping[str, object] | None = None
            if persisted_locator is not None:
                if not isinstance(persisted_locator, Mapping):
                    raise NestedMediaAcousticObserverError("VOICE_REFERENCE_PLAYBACK_LOCATOR_INVALID")
                if cache_binding.get("kind") != "OFFICIAL_EXTERNAL_AUDIO":
                    raise NestedMediaAcousticObserverError("VOICE_REFERENCE_PLAYBACK_LOCATOR_INVALID")
                source_uri = source_identity.get("source_uri")
                if not isinstance(source_uri, str) or not source_uri.strip():
                    raise NestedMediaAcousticObserverError("VOICE_REFERENCE_PLAYBACK_LOCATOR_INVALID")
                try:
                    validated_locator = validate_locator(
                        persisted_locator,
                        reference_audio_path=audio_fields["source_audio_path"],
                        reference_audio_sha256=audio_fields["source_audio_sha256"],
                        target_audio_path=audio_fields["target_audio_path"],
                        target_audio_sha256=audio_fields["target_audio_sha256"],
                        target_audio_window_ms=context_interval,
                        target_interval_ms=target_interval,
                        reference_source_path=source_path,
                        reference_source_sha256=source_sha,
                        source_uri=source_uri,
                        binding_context=locator_context,
                    )
                except ShortReferencePlaybackLocatorError as exc:
                    raise NestedMediaAcousticObserverError(
                        "VOICE_REFERENCE_PLAYBACK_LOCATOR_DRIFT"
                    ) from exc
            expected_payload = {
                **base_payload,
                "source_audio": {
                    "path": audio_fields["source_audio_path"],
                    "sha256": audio_fields["source_audio_sha256"],
                },
                "target_audio": {
                    "path": audio_fields["target_audio_path"],
                    "sha256": audio_fields["target_audio_sha256"],
                },
            }
            if validated_locator is not None:
                expected_payload["playback_locator"] = copy.deepcopy(dict(validated_locator))
            if payload != expected_payload:
                raise NestedMediaAcousticObserverError("VOICE_REFERENCE_BINDING_DRIFT")
            for field, expected in {
                "source_audio_path": source_output,
                "target_audio_path": target_output,
            }.items():
                if Path(audio_fields[field]).resolve() != expected.resolve():
                    raise NestedMediaAcousticObserverError("VOICE_REFERENCE_PREPARE_OUTPUT_DRIFT")
            voice_descriptor = _automatic_voice_descriptor(
                descriptor=descriptor, source_path=source_path, source_sha=source_sha,
                source_cache_binding=cache_binding, target_interval=target_interval,
                target_cues=target_cues, context_interval=context_interval,
                binding_path=binding_file, binding_sha=binding_sha,
                audio_fields=audio_fields,
                playback_locator=validated_locator,
            )
            binding, _state = _voice_descriptor_binding(
                descriptor=voice_descriptor, candidate_id=candidate_id,
                source_media=source_media, subtitle_path=subtitle_path,
            )
            request = _voice_reference_prepare_request(
                binding=binding, reference_window=reference_window,
                target_interval=target_interval, context_interval=context_interval,
            )
            request_sha = _canonical_hash(request)
            fields, manifest_sha = _validate_prepared_manifest(
                manifest_path=manifest_path, expected_binding=request,
                request_sha=request_sha,
                expected_outputs={
                    "source_audio_path": source_output,
                    "target_audio_path": target_output,
                },
            )
            if fields != audio_fields:
                raise NestedMediaAcousticObserverError("VOICE_REFERENCE_PREPARE_OUTPUT_DRIFT")
            return voice_descriptor, {
                "status": "REPLAYED", "preparation_path": str(manifest_path),
                "preparation_sha256": manifest_sha,
                "binding_path": str(binding_file), "binding_sha256": binding_sha,
                "reference_window_ms": reference_window,
            }
        source_argv, source_probe = _extract_pcm16_wav(
            source=source_path, output=source_output, window_ms=reference_window,
        )
        target_argv, target_probe = _extract_pcm16_wav(
            source=outer_path, output=target_output, window_ms=context_interval,
        )
        source_audio = (source_output, _sha(source_output))
        target_audio = (target_output, _sha(target_output))
        playback_locator: Mapping[str, object] | None = None
        if cache_binding.get("kind") == "OFFICIAL_EXTERNAL_AUDIO":
            try:
                playback_locator = locate_short_reference(
                    reference_audio_path=source_audio[0], reference_audio_sha256=source_audio[1],
                    target_audio_path=target_audio[0], target_audio_sha256=target_audio[1],
                    target_audio_window_ms=context_interval, target_interval_ms=target_interval,
                    reference_source_path=source_path, reference_source_sha256=source_sha,
                    source_uri=str(source_identity["source_uri"]),
                    binding_context=locator_context,
                )
            except ShortReferencePlaybackLocatorError:
                # A missing, weak, repeated, or unsupported match preserves
                # the ordinary unaligned voice-reference path.
                playback_locator = None
        payload = _voice_reference_binding_payload(
            candidate_id=candidate_id, observation_id=observation_id,
            outer=(outer_path, outer_actual_sha), srt=(srt_path, srt_actual_sha),
            frame=(frame_path, frame_sha, frame_ms), source=(source_path, source_sha),
            host=(host_path, host_sha), source_identity=source_identity,
            context_interval=context_interval, reference_window=reference_window,
            target_interval=target_interval, target_cues=target_cues,
            source_audio=source_audio, target_audio=target_audio,
            playback_locator=playback_locator,
        )
        if binding_path.exists() or binding_path.is_symlink():
            raise NestedMediaAcousticObserverError("VOICE_REFERENCE_BINDING_DRIFT")
        binding_sha = "sha256:" + atomic_write_json(binding_path, payload)
        voice_descriptor = _automatic_voice_descriptor(
            descriptor=descriptor, source_path=source_path, source_sha=source_sha,
            source_cache_binding=cache_binding, target_interval=target_interval,
            target_cues=target_cues, context_interval=context_interval,
            binding_path=binding_path, binding_sha=binding_sha,
            audio_fields={
                "source_audio_path": str(source_output), "source_audio_sha256": source_audio[1],
                "target_audio_path": str(target_output), "target_audio_sha256": target_audio[1],
            },
            playback_locator=playback_locator,
        )
        binding, _state = _voice_descriptor_binding(
            descriptor=voice_descriptor, candidate_id=candidate_id,
            source_media=source_media, subtitle_path=subtitle_path,
        )
        request = _voice_reference_prepare_request(
            binding=binding, reference_window=reference_window,
            target_interval=target_interval, context_interval=context_interval,
        )
        request_sha = _canonical_hash(request)
        manifest: dict[str, object] = {
            "schema_version": PREPARE_SCHEMA, "status": "PREPARED",
            "request_sha256": request_sha, "input_binding": request,
            "source_audio": {
                "path": str(source_output), "sha256": source_audio[1],
                "window_ms": reference_window, "argv": source_argv, "probe": source_probe,
            },
            "target_audio": {
                "path": str(target_output), "sha256": target_audio[1],
                "window_ms": context_interval, "target_interval_ms": target_interval,
                "target_audio_offset_ms": list(
                    observer_target_scope(
                        interval_ms=context_interval, target_interval_ms=target_interval,
                        target_cue_indexes=target_cues,
                        target_audio_window_ms=context_interval,
                    )["target_audio_offset_ms"]
                ),
                "argv": target_argv, "probe": target_probe,
            },
        }
        manifest_sha = "sha256:" + atomic_write_json(manifest_path, manifest)
    return voice_descriptor, {
        "status": "PREPARED", "preparation_path": str(manifest_path),
        "preparation_sha256": manifest_sha,
        "binding_path": str(binding_path), "binding_sha256": binding_sha,
        "reference_window_ms": reference_window,
    }


def _audio_fields_from_manifest(manifest: Mapping[str, object]) -> dict[str, str]:
    source = manifest.get("source_audio")
    target = manifest.get("target_audio")
    if not isinstance(source, Mapping) or not isinstance(target, Mapping):
        raise NestedMediaAcousticObserverError("PCM16_AUDIO_PREPARE_INVALID")
    fields = {
        "source_audio_path": source.get("path"),
        "source_audio_sha256": source.get("sha256"),
        "target_audio_path": target.get("path"),
        "target_audio_sha256": target.get("sha256"),
    }
    if any(not isinstance(value, str) or not value for value in fields.values()):
        raise NestedMediaAcousticObserverError("PCM16_AUDIO_PREPARE_INVALID")
    return {key: str(value) for key, value in fields.items()}


def _validate_prepared_manifest(
    *, manifest_path: Path, expected_binding: Mapping[str, object],
    request_sha: str, expected_outputs: Mapping[str, Path],
) -> tuple[dict[str, str], str]:
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise NestedMediaAcousticObserverError("PCM16_AUDIO_PREPARE_INCOMPLETE")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NestedMediaAcousticObserverError("PCM16_AUDIO_PREPARE_INVALID") from exc
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("schema_version") != PREPARE_SCHEMA
        or manifest.get("status") != "PREPARED"
        or manifest.get("request_sha256") != request_sha
        or manifest.get("input_binding") != dict(expected_binding)
    ):
        raise NestedMediaAcousticObserverError("PCM16_AUDIO_PREPARE_INVALID")
    fields = _audio_fields_from_manifest(manifest)
    for field, expected_output in expected_outputs.items():
        path = Path(fields[field])
        try:
            if path.resolve() != expected_output.resolve():
                raise NestedMediaAcousticObserverError("PCM16_AUDIO_PREPARE_OUTPUT_DRIFT")
        except (OSError, RuntimeError) as exc:
            raise NestedMediaAcousticObserverError("PCM16_AUDIO_PREPARE_OUTPUT_DRIFT") from exc
        expected = fields[field.replace("_path", "_sha256")]
        _path_hash(path, expected, unavailable="PCM16_AUDIO_PREPARE_OUTPUT_MISSING",
                   mismatch="PCM16_AUDIO_PREPARE_OUTPUT_DRIFT")
    return fields, _sha(manifest_path)


def _descriptor_binding(
    *, descriptor: Mapping[str, object], candidate_id: str, source_media: Path,
    subtitle_path: Path,
) -> tuple[dict[str, object], dict[str, object]]:
    if descriptor.get("observer_mode") == VOICE_REFERENCE_MODE:
        return _voice_descriptor_binding(
            descriptor=descriptor, candidate_id=candidate_id,
            source_media=source_media, subtitle_path=subtitle_path,
        )
    required = (
        "observation_id", "outer_media_sha256", "final_srt_sha256",
        "caption_frame_path", "caption_frame_sha256", "caption_frame_ms", "interval_ms",
        "independent_source_media_path", "independent_source_media_sha256",
        "mapping_receipt_path", "mapping_receipt_sha256",
        "host_enrollment_path", "host_enrollment_sha256",
    )
    if any(field not in descriptor for field in required):
        raise NestedMediaAcousticObserverError("PREPARED_ACOUSTIC_INPUT_INCOMPLETE")
    observation_id = _safe_segment(descriptor["observation_id"], code="OBSERVATION_ID_INVALID")
    _safe_segment(candidate_id, code="CANDIDATE_ID_INVALID")
    interval_value = _interval(descriptor["interval_ms"], code="INTERVAL_INVALID", integer=True)
    interval = [int(item) for item in interval_value]
    frame_ms = descriptor["caption_frame_ms"]
    if isinstance(frame_ms, bool) or not isinstance(frame_ms, int) or not interval[0] <= frame_ms < interval[1]:
        raise NestedMediaAcousticObserverError("CAPTION_FRAME_INTERVAL_MISMATCH")
    outer_path, outer_sha = _path_hash(
        source_media, descriptor["outer_media_sha256"],
        unavailable="OUTER_MEDIA_UNAVAILABLE", mismatch="OUTER_MEDIA_HASH_MISMATCH",
    )
    srt_path, srt_sha = _path_hash(
        subtitle_path, descriptor["final_srt_sha256"],
        unavailable="FINAL_SRT_UNAVAILABLE", mismatch="FINAL_SRT_HASH_MISMATCH",
    )
    frame_path, frame_sha = _path_hash(
        descriptor["caption_frame_path"], descriptor["caption_frame_sha256"],
        unavailable="CAPTION_FRAME_UNAVAILABLE", mismatch="CAPTION_FRAME_HASH_MISMATCH",
    )
    source_path, source_sha = _path_hash(
        descriptor["independent_source_media_path"], descriptor["independent_source_media_sha256"],
        unavailable="INDEPENDENT_SOURCE_UNAVAILABLE", mismatch="INDEPENDENT_SOURCE_HASH_MISMATCH",
    )
    mapping_path, mapping_sha = _path_hash(
        descriptor["mapping_receipt_path"], descriptor["mapping_receipt_sha256"],
        unavailable="MAPPING_RECEIPT_UNAVAILABLE", mismatch="MAPPING_RECEIPT_HASH_MISMATCH",
    )
    host_path, host_sha = _path_hash(
        descriptor["host_enrollment_path"], descriptor["host_enrollment_sha256"],
        unavailable="HOST_ENROLLMENT_UNAVAILABLE", mismatch="HOST_ENROLLMENT_HASH_MISMATCH",
    )
    mapping = _json_mapping(mapping_path, code="MAPPING_RECEIPT_INVALID")
    mapping_info = _mapping_info(mapping, outer_sha=outer_sha, source_sha=source_sha, interval=interval)
    _validate_local_mapping_receipt_scope(mapping=mapping, descriptor=descriptor)
    _validate_current_mapping_receipt_scope(
        mapping=mapping, descriptor=descriptor, candidate_id=candidate_id,
        media_sha=outer_sha, srt_sha=srt_sha,
    )
    cache_binding = descriptor.get("source_cache_binding")
    if isinstance(cache_binding, Mapping):
        source_identity = mapping_info.get("source_identity")
        if (
            not isinstance(source_identity, Mapping)
            or source_identity.get("bvid") != cache_binding.get("bvid")
            or str(source_identity.get("cid")) != str(cache_binding.get("cid"))
        ):
            raise NestedMediaAcousticObserverError("SOURCE_CACHE_MAPPING_IDENTITY_MISMATCH")
    visual = descriptor.get("visual_observation")
    if not isinstance(visual, Mapping):
        raise NestedMediaAcousticObserverError("CURRENT_VISUAL_OBSERVATION_REQUIRED")
    if (
        visual.get("observation_id") != observation_id
        or visual.get("frame_ms") != frame_ms
        or Path(str(visual.get("frame_path"))).resolve() != frame_path
        or str(visual.get("frame_sha256")).removeprefix("sha256:")
        != frame_sha.removeprefix("sha256:")
    ):
        raise NestedMediaAcousticObserverError("CURRENT_VISUAL_BINDING_MISMATCH")
    binding: dict[str, object] = {
        "candidate_id": candidate_id,
        "observation_id": observation_id,
        "outer_media": {"path": str(outer_path), "sha256": outer_sha},
        "final_srt": {"path": str(srt_path), "sha256": srt_sha},
        "caption_frame": {"path": str(frame_path), "sha256": frame_sha, "frame_ms": frame_ms},
        "interval_ms": interval,
        "independent_source_media": {"path": str(source_path), "sha256": source_sha},
        "mapping_receipt": {"path": str(mapping_path), "sha256": mapping_sha, **mapping_info},
        "host_enrollment": {"path": str(host_path), "sha256": host_sha},
        "format": dict(_AUDIO_FORMAT),
    }
    return binding, {"source_window_ms": list(mapping_info["source_window_ms"]), "outer_path": outer_path}


def _voice_consumer_original_paths(
    *, descriptor: Mapping[str, object], source_media: Path, subtitle_path: Path,
) -> tuple[Path, Path]:
    """Resolve the immutable voice binding paths for a relocated consumer.

    The consumer may be handed byte-identical copies of the current outer
    media or SRT.  The receipt request still names the original paths, so
    replay the existing descriptor binding against those recorded paths after
    checking that the caller's current bytes match the descriptor.
    """
    _path_hash(
        source_media, descriptor["outer_media_sha256"],
        unavailable="OUTER_MEDIA_UNAVAILABLE", mismatch="OUTER_MEDIA_HASH_MISMATCH",
    )
    _path_hash(
        subtitle_path, descriptor["final_srt_sha256"],
        unavailable="FINAL_SRT_UNAVAILABLE", mismatch="FINAL_SRT_HASH_MISMATCH",
    )
    binding_path, _ = _path_hash(
        descriptor["voice_reference_binding_path"], descriptor["voice_reference_binding_sha256"],
        unavailable="VOICE_REFERENCE_BINDING_UNAVAILABLE",
        mismatch="VOICE_REFERENCE_BINDING_HASH_MISMATCH",
    )
    binding = _json_mapping(binding_path, code="VOICE_REFERENCE_BINDING_INVALID")
    original_paths: list[Path] = []
    for field, unavailable, mismatch in (
        ("outer_media", "OUTER_MEDIA_UNAVAILABLE", "VOICE_REFERENCE_OUTER_BINDING_MISMATCH"),
        ("final_srt", "FINAL_SRT_UNAVAILABLE", "VOICE_REFERENCE_SRT_BINDING_MISMATCH"),
    ):
        recorded = binding.get(field)
        if not isinstance(recorded, Mapping):
            raise NestedMediaAcousticObserverError(mismatch)
        recorded_path = recorded.get("path")
        recorded_sha = recorded.get("sha256")
        if not isinstance(recorded_path, str) or not recorded_path:
            raise NestedMediaAcousticObserverError(mismatch)
        original_path, _ = _path_hash(
            recorded_path, recorded_sha, unavailable=unavailable, mismatch=mismatch,
        )
        original_paths.append(original_path)
    return original_paths[0], original_paths[1]


def _voice_descriptor_binding(
    *, descriptor: Mapping[str, object], candidate_id: str, source_media: Path,
    subtitle_path: Path,
) -> tuple[dict[str, object], dict[str, object]]:
    """Bind a single current cue to an explicitly unaligned voice reference."""

    required = (
        "observation_id", "outer_media_sha256", "final_srt_sha256",
        "caption_frame_path", "caption_frame_sha256", "caption_frame_ms", "interval_ms",
        "independent_source_media_path", "independent_source_media_sha256",
        "host_enrollment_path", "host_enrollment_sha256",
        "voice_reference_binding_path", "voice_reference_binding_sha256",
        "target_interval_ms", "target_cue_indexes",
    )
    if any(field not in descriptor for field in required):
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_INPUT_INCOMPLETE")
    observation_id = _safe_segment(descriptor["observation_id"], code="OBSERVATION_ID_INVALID")
    _safe_segment(candidate_id, code="CANDIDATE_ID_INVALID")
    interval = [int(item) for item in _interval(descriptor["interval_ms"], code="INTERVAL_INVALID", integer=True)]
    frame_ms = descriptor["caption_frame_ms"]
    if isinstance(frame_ms, bool) or not isinstance(frame_ms, int) or not interval[0] <= frame_ms < interval[1]:
        raise NestedMediaAcousticObserverError("CAPTION_FRAME_INTERVAL_MISMATCH")
    target_interval = [int(item) for item in _interval(
        descriptor["target_interval_ms"], code="VOICE_REFERENCE_TARGET_INTERVAL_INVALID", integer=True,
    )]
    target_cues = descriptor["target_cue_indexes"]
    if not isinstance(target_cues, list) or len(target_cues) != 1:
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_TARGET_SINGLE_CUE_REQUIRED")
    if not interval[0] <= target_interval[0] < target_interval[1] <= interval[1]:
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_TARGET_INTERVAL_OUTSIDE_CONTEXT")
    if not target_interval[0] <= frame_ms < target_interval[1]:
        raise NestedMediaAcousticObserverError("VOICE_REFERENCE_TARGET_FRAME_MISMATCH")
    outer_path, outer_sha = _path_hash(
        source_media, descriptor["outer_media_sha256"],
        unavailable="OUTER_MEDIA_UNAVAILABLE", mismatch="OUTER_MEDIA_HASH_MISMATCH",
    )
    srt_path, srt_sha = _path_hash(
        subtitle_path, descriptor["final_srt_sha256"],
        unavailable="FINAL_SRT_UNAVAILABLE", mismatch="FINAL_SRT_HASH_MISMATCH",
    )
    frame_path, frame_sha = _path_hash(
        descriptor["caption_frame_path"], descriptor["caption_frame_sha256"],
        unavailable="CAPTION_FRAME_UNAVAILABLE", mismatch="CAPTION_FRAME_HASH_MISMATCH",
    )
    source_path, source_sha = _path_hash(
        descriptor["independent_source_media_path"], descriptor["independent_source_media_sha256"],
        unavailable="INDEPENDENT_SOURCE_UNAVAILABLE", mismatch="INDEPENDENT_SOURCE_HASH_MISMATCH",
    )
    host_path, host_sha = _path_hash(
        descriptor["host_enrollment_path"], descriptor["host_enrollment_sha256"],
        unavailable="HOST_ENROLLMENT_UNAVAILABLE", mismatch="HOST_ENROLLMENT_HASH_MISMATCH",
    )
    source_audio = target_audio = None
    if all(descriptor.get(field) not in (None, "") for field in _AUDIO_FIELDS):
        source_audio = _path_hash(
            descriptor["source_audio_path"], descriptor["source_audio_sha256"],
            unavailable="SOURCE_AUDIO_UNAVAILABLE", mismatch="SOURCE_AUDIO_HASH_MISMATCH",
        )
        target_audio = _path_hash(
            descriptor["target_audio_path"], descriptor["target_audio_sha256"],
            unavailable="TARGET_AUDIO_UNAVAILABLE", mismatch="TARGET_AUDIO_HASH_MISMATCH",
        )
    binding_path, binding_sha, voice_info = _validate_voice_reference_binding(
        binding_path=descriptor["voice_reference_binding_path"],
        binding_sha256=descriptor["voice_reference_binding_sha256"],
        candidate_id=candidate_id, observation_id=observation_id,
        outer=(outer_path, outer_sha), srt=(srt_path, srt_sha), frame=(frame_path, frame_sha),
        source_media=(source_path, source_sha), host=(host_path, host_sha),
        source_audio=source_audio, target_audio=target_audio,
        interval=interval, frame_ms=frame_ms, target_interval=target_interval,
        target_cues=list(target_cues),
        target_audio_window_ms=descriptor.get("target_audio_window_ms"),
    )
    cache_binding = descriptor.get("source_cache_binding")
    if isinstance(cache_binding, Mapping):
        bound_source = voice_info.get("source_media")
        identity = voice_info.get("source_identity")
        if (cache_binding.get("kind") == "OFFICIAL_EXTERNAL_AUDIO"
                or isinstance(identity, Mapping) and identity.get("kind") == "OFFICIAL_EXTERNAL_AUDIO"):
            if (not isinstance(identity, Mapping)
                    or cache_binding.get("kind") != "OFFICIAL_EXTERNAL_AUDIO"
                    or identity.get("kind") != "OFFICIAL_EXTERNAL_AUDIO"
                    or cache_binding.get("source_uri") != identity.get("source_uri")
                    or not _same_hash(cache_binding.get("source_sha256"), identity.get("source_sha256"))):
                raise NestedMediaAcousticObserverError("SOURCE_CACHE_VOICE_IDENTITY_MISMATCH")
        elif (
            not isinstance(bound_source, Mapping)
            or cache_binding.get("bvid") != bound_source.get("bvid")
            or str(cache_binding.get("cid")) != str(bound_source.get("cid"))
        ):
            raise NestedMediaAcousticObserverError("SOURCE_CACHE_VOICE_IDENTITY_MISMATCH")
    visual = descriptor.get("visual_observation")
    if not isinstance(visual, Mapping):
        raise NestedMediaAcousticObserverError("CURRENT_VISUAL_OBSERVATION_REQUIRED")
    if (
        visual.get("observation_id") != observation_id
        or visual.get("frame_ms") != frame_ms
        or Path(str(visual.get("frame_path"))).resolve() != frame_path
        or str(visual.get("frame_sha256")).removeprefix("sha256:")
        != frame_sha.removeprefix("sha256:")
    ):
        raise NestedMediaAcousticObserverError("CURRENT_VISUAL_BINDING_MISMATCH")
    binding: dict[str, object] = {
        "candidate_id": candidate_id, "observation_id": observation_id,
        "outer_media": {"path": str(outer_path), "sha256": outer_sha},
        "final_srt": {"path": str(srt_path), "sha256": srt_sha},
        "caption_frame": {"path": str(frame_path), "sha256": frame_sha, "frame_ms": frame_ms},
        "interval_ms": interval, "target_interval_ms": target_interval,
        "target_cue_indexes": list(target_cues),
        "independent_source_media": {"path": str(source_path), "sha256": source_sha},
        "voice_reference_binding": {"path": str(binding_path), "sha256": binding_sha, **voice_info},
        "host_enrollment": {"path": str(host_path), "sha256": host_sha},
        "format": dict(_AUDIO_FORMAT),
    }
    if source_audio is not None and target_audio is not None:
        binding["source_audio"] = {"path": str(source_audio[0]), "sha256": source_audio[1]}
        binding["target_audio"] = {"path": str(target_audio[0]), "sha256": target_audio[1]}
    return binding, {
        "reference_window_ms": list(voice_info["reference_window_ms"]),
        "outer_path": outer_path,
        "mode": VOICE_REFERENCE_MODE,
    }


def _bare_hash(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.removeprefix("sha256:").lower()
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        return None
    return value


def _hashes_match(left: object, right: object) -> bool:
    left_hash = _bare_hash(left)
    right_hash = _bare_hash(right)
    return left_hash is not None and left_hash == right_hash


def _current_file_hash(value: object, *, unavailable: str) -> tuple[Path, str]:
    candidate = Path(value)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise NestedMediaAcousticObserverError(unavailable) from exc
    if candidate.is_symlink() or not resolved.is_file():
        raise NestedMediaAcousticObserverError(unavailable)
    try:
        return resolved, _sha(resolved)
    except OSError as exc:
        raise NestedMediaAcousticObserverError(unavailable) from exc


def _scene_observation_id(candidate_id: str, cue_index: int, frame_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{candidate_id}-scene-{cue_index}-{frame_id}")
    value = value.strip("-")[:180]
    return value or f"{candidate_id}-scene-{cue_index}"


_ACOUSTIC_ONLY_SCOPE_SCHEMA = "nested-media-acoustic-timing-scope.v1"


def _validate_acoustic_only_timing_scope(
    *,
    descriptor: Mapping[str, object],
    spec: Mapping[str, object],
    candidate_id: str,
    subtitle_path: Path,
    media_sha: str,
    srt_sha: str,
    visual: Mapping[str, object],
    no_caption: bool = False,
) -> tuple[list[int], list[int]]:
    """Revalidate a timing-only row against current automatic scene evidence.

    The producer may prepare an acoustic request from a frame that sits inside
    one current subtitle cue even when the embedded caption text does not
    fuzzy-match that cue. The separate no-caption kind requires an explicit
    WATCHED_MEDIA scene with an empty caption list, never a raw blank frame.
    These paths are deliberately narrower than ordinary
    attribution: the timing scope must be reproduced from the current,
    hash-bound scene-evidence file and the current SRT before any audio call.
    """

    raw_scope = descriptor.get("acoustic_only_timing_scope")
    if not isinstance(raw_scope, Mapping):
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_REQUIRED")
    if raw_scope.get("schema_version") != _ACOUSTIC_ONLY_SCOPE_SCHEMA:
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_SCHEMA_INVALID")
    expected_kind = _NO_CAPTION_SCOPE_KIND if no_caption else "CURRENT_WATCHED_MEDIA_CUE"
    if raw_scope.get("kind") != expected_kind:
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_KIND_INVALID")
    external_button = (
        visual.get("role") == "NO_EMBEDDED_CAPTION_OBSERVED"
        and isinstance(visual.get("external_audio_candidate"), Mapping)
        and isinstance(visual.get("source_controls"), Mapping)
    )
    if no_caption:
        expected_attribution = (
            "NO_CAPTION_EXTERNAL_AUDIO_BUTTON" if external_button
            else "NO_CAPTION_CURRENT_SCENE"
        )
        expected_role = "NO_EMBEDDED_CAPTION_OBSERVED"
        if (
            visual.get("caption_present") is not False
            or visual.get("role") != expected_role
            or visual.get("visible_text") != ""
            or raw_scope.get("visual_attribution") != expected_attribution
        ):
            raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_NO_CAPTION_REQUIRED")
    elif visual.get("caption_present") is not True:
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_CAPTION_REQUIRED")

    cue_index = raw_scope.get("cue_index")
    frame_ms = raw_scope.get("frame_ms")
    frame_id = raw_scope.get("frame_id")
    observation_id = raw_scope.get("observation_id")
    if (
        isinstance(cue_index, bool) or not isinstance(cue_index, int)
        or isinstance(frame_ms, bool) or not isinstance(frame_ms, int)
        or not isinstance(frame_id, str) or not frame_id
        or not isinstance(observation_id, str) or observation_id != descriptor.get("observation_id")
    ):
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_BINDING_INVALID")
    if visual.get("frame_ms") != frame_ms:
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_FRAME_MISMATCH")

    config = spec.get("nested_media_caption_dedup")
    if not isinstance(config, Mapping) or config.get("policy") != CAPTION_DEDUP_POLICY:
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_CONFIG_INVALID")
    configured_value = config.get("evidence_path")
    declared_value = raw_scope.get("evidence_path")
    if not isinstance(configured_value, str) or not configured_value:
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_EVIDENCE_REQUIRED")
    if not isinstance(declared_value, str) or not declared_value:
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_EVIDENCE_REQUIRED")
    configured_path = Path(configured_value)
    if not configured_path.is_absolute():
        configured_path = subtitle_path.parent / configured_path
    declared_path = Path(declared_value)
    if not declared_path.is_absolute():
        declared_path = subtitle_path.parent / declared_path
    try:
        if configured_path.resolve() != declared_path.resolve():
            raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_EVIDENCE_PATH_MISMATCH")
    except (OSError, RuntimeError) as exc:
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_EVIDENCE_PATH_MISMATCH") from exc
    if not _hashes_match(config.get("evidence_sha256"), raw_scope.get("evidence_sha256")):
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_EVIDENCE_HASH_MISMATCH")
    evidence_file, evidence_sha = _path_hash(
        declared_path, raw_scope.get("evidence_sha256"),
        unavailable="CURRENT_ACOUSTIC_SCOPE_EVIDENCE_UNAVAILABLE",
        mismatch="CURRENT_ACOUSTIC_SCOPE_EVIDENCE_HASH_MISMATCH",
    )
    try:
        evidence = json.loads(evidence_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_EVIDENCE_INVALID") from exc
    if not isinstance(evidence, Mapping):
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_EVIDENCE_INVALID")
    external_scene = spec.get("nested_media_external_audio_scene") is True
    expected_source_role = (
        EXTERNAL_AUDIO_SCENE_SOURCE_ROLE if external_scene else "WATCHED_MEDIA"
    )
    if (
        evidence.get("schema_version") != CAPTION_DEDUP_SCHEMA
        or evidence.get("candidate_id") != candidate_id
        or evidence.get("source_role") != expected_source_role
        or evidence.get("policy") != CAPTION_DEDUP_POLICY
        or not _hashes_match(evidence.get("source_media_sha256"), media_sha)
        or not _hashes_match(evidence.get("input_transcript_sha256"), srt_sha)
        or not isinstance(evidence.get("scene_observations"), list)
    ):
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_BINDING_MISMATCH")

    try:
        cues = parse_timed_srt(
            subtitle_path.read_text(encoding="utf-8-sig"), label="current final scene"
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_SRT_INVALID") from exc
    cue_by_index: dict[int | str, object] = {}
    for cue in cues:
        key: int | str = int(cue.index) if cue.index.isdigit() else cue.index
        if key in cue_by_index:
            raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_CUE_DUPLICATE")
        cue_by_index[key] = cue
    cue = cue_by_index.get(cue_index)
    if cue is None:
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_CUE_NOT_CURRENT")
    expected_interval = [int(cue.start_ms), int(cue.end_ms)]
    if raw_scope.get("cue_interval_ms") != expected_interval:
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_CUE_INTERVAL_MISMATCH")
    if descriptor.get("target_interval_ms") != expected_interval:
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_TARGET_MISMATCH")
    if descriptor.get("target_cue_indexes") != [cue_index]:
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_TARGET_MISMATCH")
    if not expected_interval[0] <= frame_ms < expected_interval[1]:
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_FRAME_INTERVAL_MISMATCH")
    expected_scene = "NOT_WATCHED_MEDIA" if external_button else "WATCHED_MEDIA"
    if raw_scope.get("scene") != expected_scene:
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_SCENE_INVALID")
    if raw_scope.get("frame_ms") != raw_scope.get("caption_frame_ms", frame_ms):
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_FRAME_MISMATCH")

    matching_rows = [
        row for row in evidence["scene_observations"]
        if isinstance(row, Mapping)
        and row.get("frame_id") == frame_id
        and row.get("cue_index") == cue_index
        and row.get("frame_ms") == frame_ms
    ]
    if len(matching_rows) != 1:
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_OBSERVATION_INVALID")
    scene_row = matching_rows[0]
    if scene_row.get("scene") != expected_scene or not isinstance(scene_row.get("captions"), list):
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_SCENE_INVALID")
    if no_caption and scene_row["captions"] != []:
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_NO_CAPTION_REQUIRED")
    if external_button:
        if scene_row.get("source_controls") != visual.get("source_controls"):
            raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_SOURCE_CONTROLS_MISMATCH")
    if not no_caption and (
        not isinstance(visual.get("visible_text"), str)
        or visual["visible_text"] not in scene_row["captions"]
    ):
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_CAPTION_BINDING_MISMATCH")
    scene_frame_value = scene_row.get("frame_path")
    if isinstance(scene_frame_value, str) and not Path(scene_frame_value).is_absolute():
        scene_frame_value = str(evidence_file.parent / scene_frame_value)
    scene_frame_path, scene_frame_sha = _path_hash(
        scene_frame_value, scene_row.get("frame_sha256"),
        unavailable="CURRENT_ACOUSTIC_SCOPE_FRAME_UNAVAILABLE",
        mismatch="CURRENT_ACOUSTIC_SCOPE_FRAME_HASH_MISMATCH",
    )
    descriptor_frame_path, descriptor_frame_sha = _path_hash(
        descriptor.get("caption_frame_path"), descriptor.get("caption_frame_sha256"),
        unavailable="CURRENT_ACOUSTIC_SCOPE_FRAME_UNAVAILABLE",
        mismatch="CURRENT_ACOUSTIC_SCOPE_FRAME_HASH_MISMATCH",
    )
    if (
        scene_frame_path != descriptor_frame_path
        or not _hashes_match(scene_frame_sha, descriptor_frame_sha)
        or Path(str(visual.get("frame_path"))).resolve() != scene_frame_path
        or not _hashes_match(visual.get("frame_sha256"), scene_frame_sha)
    ):
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_FRAME_BINDING_MISMATCH")
    if raw_scope.get("frame_path") != str(scene_frame_path) or not _hashes_match(
        raw_scope.get("frame_sha256"), scene_frame_sha
    ):
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_FRAME_BINDING_MISMATCH")
    expected_observation_id = _scene_observation_id(candidate_id, cue_index, frame_id)
    if observation_id != expected_observation_id:
        raise NestedMediaAcousticObserverError("CURRENT_ACOUSTIC_SCOPE_OBSERVATION_INVALID")
    return expected_interval, [cue_index]


def _first_matching_host_enrollment(
    *, profile_path: Path, reference_dir: Path,
) -> tuple[Path, str, str, str]:
    """Validate the declared profile without loading its speaker model."""

    from src.autoslice.host_vocal_proof import _validate_profile

    profile_file, profile_sha = _current_file_hash(
        profile_path, unavailable="HOST_PROFILE_UNAVAILABLE"
    )
    try:
        profile = json.loads(profile_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NestedMediaAcousticObserverError("HOST_PROFILE_INVALID") from exc
    if not isinstance(profile, Mapping):
        raise NestedMediaAcousticObserverError("HOST_PROFILE_INVALID")
    try:
        _model_info, references = _validate_profile(profile)
    except Exception as exc:
        raise NestedMediaAcousticObserverError("HOST_PROFILE_INVALID") from exc
    try:
        reference_root = Path(reference_dir).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise NestedMediaAcousticObserverError("HOST_REFERENCE_DIR_UNAVAILABLE") from exc
    if not reference_root.is_dir() or Path(reference_dir).is_symlink():
        raise NestedMediaAcousticObserverError("HOST_REFERENCE_DIR_UNAVAILABLE")
    for expected in references:
        filename = expected["filename"]
        candidate = reference_root / filename
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        try:
            resolved.relative_to(reference_root)
        except ValueError:
            continue
        if candidate.is_symlink() or not resolved.is_file():
            continue
        actual_sha = _sha(resolved)
        if _hashes_match(actual_sha, expected["sha256"]):
            return resolved, actual_sha, str(expected["id"]), profile_sha
    raise NestedMediaAcousticObserverError("HOST_ENROLLMENT_UNAVAILABLE")


def prepare_final_scene_acoustic_spec(
    *, spec: Mapping[str, object], candidate_id: str, source_media: Path,
    subtitle_path: Path, duration_ms: int, profile_path: Path,
    reference_dir: Path, external_audio_source_map: Path | None = None,
    external_audio_cache_root: Path | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Build evidence-only acoustic inputs from current watched-scene rows.

    This is deliberately separate from ``_descriptor_binding``.  It creates
    only the visual/current-media portion consumed by the normal source-cache
    preparation path; source mapping and audio fields must be supplied by that
    later path.  An already declared descriptor is immutable, including a
    malformed or partial one.
    """

    effective = copy.deepcopy(dict(spec))
    audit: dict[str, object] = {
        "schema_version": AUDIT_SCHEMA,
        "candidate_id": candidate_id,
        "status": "EVIDENCE_ONLY",
        "observations": [],
        "subtitle_mutations": 0,
        "display_mutation_authorized": False,
        "mutation_authorized": False,
        "publication_authorized": False,
        "provider_calls": 0,
        "source_role_authority": False,
    }

    def blocked(reason_code: str, **details: object) -> tuple[dict[str, object], dict[str, object]]:
        audit["reason_code"] = reason_code
        audit.update(details)
        audit["status"] = "EVIDENCE_ONLY"
        return effective, audit

    # The existing handoff, even if incomplete, belongs to the earlier
    # producer path.  Never replace it with an automatically invented one.
    if "nested_media_acoustic" in spec:
        audit.update(
            status="PRESERVED",
            reason_code="EXISTING_NESTED_ACOUSTIC_PRESERVED",
            descriptor_count=(
                len(spec["nested_media_acoustic"].get("observations", []))
                if isinstance(spec["nested_media_acoustic"], Mapping)
                and isinstance(spec["nested_media_acoustic"].get("observations"), list)
                else None
            ),
        )
        return effective, audit
    external_scene = spec.get("nested_media_external_audio_scene") is True
    if spec.get("watched_media") is not True and not external_scene:
        return blocked("WATCHED_MEDIA_DECLARATION_REQUIRED")
    if (
        isinstance(duration_ms, bool)
        or not isinstance(duration_ms, int)
        or duration_ms <= 0
    ):
        return blocked("DURATION_INVALID")
    try:
        _safe_segment(candidate_id, code="CANDIDATE_ID_INVALID")
    except NestedMediaAcousticObserverError as exc:
        return blocked(str(exc))

    try:
        media_path, media_sha = _current_file_hash(
            source_media, unavailable="OUTER_MEDIA_UNAVAILABLE"
        )
        srt_path, srt_sha = _current_file_hash(
            subtitle_path, unavailable="FINAL_SRT_UNAVAILABLE"
        )
    except NestedMediaAcousticObserverError as exc:
        return blocked(str(exc))
    audit.update(source_media_sha256=media_sha, final_srt_sha256=srt_sha,
                 duration_ms=duration_ms)
    try:
        cues = parse_timed_srt(srt_path.read_text(encoding="utf-8-sig"), label="final scene")
    except (OSError, UnicodeError, ValueError) as exc:
        return blocked("FINAL_SRT_INVALID", error_type=type(exc).__name__)
    cue_by_index: dict[int | str, object] = {}
    for cue in cues:
        key: int | str = int(cue.index) if cue.index.isdigit() else cue.index
        if key in cue_by_index:
            return blocked("FINAL_SRT_CUE_INDEX_DUPLICATE")
        cue_by_index[key] = cue
        if cue.start_ms < 0 or cue.end_ms <= cue.start_ms or cue.end_ms > duration_ms:
            return blocked("FINAL_SRT_TIMELINE_INVALID")

    config = spec.get("nested_media_caption_dedup")
    if not isinstance(config, Mapping) or config.get("policy") != CAPTION_DEDUP_POLICY:
        return blocked("CURRENT_CAPTION_DEDUP_CONFIG_INVALID")
    evidence_value = config.get("evidence_path")
    if not isinstance(evidence_value, str) or not evidence_value:
        return blocked("CURRENT_CAPTION_DEDUP_EVIDENCE_REQUIRED")
    evidence_path = Path(evidence_value)
    if not evidence_path.is_absolute():
        evidence_path = subtitle_path.parent / evidence_path
    try:
        evidence_file, evidence_sha = _path_hash(
            evidence_path, config.get("evidence_sha256"),
            unavailable="CURRENT_CAPTION_DEDUP_EVIDENCE_UNAVAILABLE",
            mismatch="CURRENT_CAPTION_DEDUP_EVIDENCE_HASH_MISMATCH",
        )
        evidence = json.loads(evidence_file.read_text(encoding="utf-8"))
    except (NestedMediaAcousticObserverError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        return blocked(str(exc) if isinstance(exc, NestedMediaAcousticObserverError) else "CURRENT_CAPTION_DEDUP_EVIDENCE_INVALID")
    if not isinstance(evidence, Mapping):
        return blocked("CURRENT_CAPTION_DEDUP_EVIDENCE_INVALID")
    expected_source_role = (
        EXTERNAL_AUDIO_SCENE_SOURCE_ROLE if external_scene else "WATCHED_MEDIA"
    )
    if (
        evidence.get("schema_version") != CAPTION_DEDUP_SCHEMA
        or evidence.get("candidate_id") != candidate_id
        or evidence.get("source_role") != expected_source_role
        or evidence.get("policy") != CAPTION_DEDUP_POLICY
        or evidence.get("source_interval_ms") != [0, duration_ms]
        or not _hashes_match(evidence.get("source_media_sha256"), media_sha)
        or not _hashes_match(evidence.get("input_transcript_sha256"), srt_sha)
        or not isinstance(evidence.get("observations"), list)
        or not isinstance(evidence.get("scene_observations"), list)
    ):
        return blocked("CURRENT_CAPTION_DEDUP_BINDING_MISMATCH",
                       evidence_path=str(evidence_file), evidence_sha256=evidence_sha)
    try:
        enrollment_path, enrollment_sha, enrollment_id, profile_sha = _first_matching_host_enrollment(
            profile_path=Path(profile_path), reference_dir=Path(reference_dir)
        )
    except NestedMediaAcousticObserverError as exc:
        return blocked(str(exc), evidence_path=str(evidence_file), evidence_sha256=evidence_sha)
    audit.update(
        evidence_path=str(evidence_file), evidence_sha256=evidence_sha,
        host_profile_path=str(Path(profile_path).resolve()),
        host_profile_sha256=profile_sha,
        host_enrollment_id=enrollment_id,
        host_enrollment_path=str(enrollment_path),
        host_enrollment_sha256=enrollment_sha,
    )

    # A recovered official audio file is a voice reference, not a mapping or
    # role receipt. Bind it once to this outer media, then let the ordinary
    # observer prepare fresh frame/SRT/target bindings for each watched cue.
    watched_reference: dict[str, object] | None = None
    reference = spec.get("nested_media_external_audio_reference")
    if reference is not None:
        reference_audit: dict[str, object] = {
            "status": "UNKNOWN", "action": "KEEP", "source_role_authority": False,
        }
        audit["external_audio_reference"] = reference_audit
        try:
            if (
                not isinstance(reference, Mapping)
                or reference.get("candidate_id") != candidate_id
                or not _hashes_match(reference.get("outer_media_sha256"), media_sha)
                or _external_voice_reference_seed_status(reference) != "VALID"
            ):
                raise NestedMediaAcousticObserverError("EXTERNAL_AUDIO_REFERENCE_BINDING_INVALID")
            source_path, source_sha = _path_hash(
                reference.get("independent_source_media_path"),
                reference.get("independent_source_media_sha256"),
                unavailable="INDEPENDENT_SOURCE_UNAVAILABLE",
                mismatch="INDEPENDENT_SOURCE_HASH_MISMATCH",
            )
            if source_path.resolve() == media_path.resolve() or _same_hash(source_sha, media_sha):
                raise NestedMediaAcousticObserverError("INDEPENDENT_SOURCE_IS_OUTER_MEDIA")
            watched_reference = {
                "independent_source_media_path": str(source_path),
                "independent_source_media_sha256": source_sha,
                "source_cache_binding": copy.deepcopy(reference["source_cache_binding"]),
            }
            reference_audit.update(status="EVIDENCE_ONLY", **watched_reference)
        except (NestedMediaAcousticObserverError, OSError, ValueError) as exc:
            reference_audit["reason_code"] = str(exc)

    descriptors: list[dict[str, object]] = []
    seen_cues: set[int | str] = set()
    for raw in evidence["scene_observations"]:
        row_audit: dict[str, object] = {"status": "UNKNOWN", "action": "KEEP"}
        audit["observations"].append(row_audit)
        if not isinstance(raw, Mapping):
            row_audit["reason_code"] = "SCENE_OBSERVATION_INVALID"
            continue
        for field in ("frame_id", "frame_path", "frame_sha256", "scene", "captions"):
            row_audit[field] = raw.get(field)
        cue_index = raw.get("cue_index")
        frame_ms = raw.get("frame_ms")
        frame_id = raw.get("frame_id")
        scene = raw.get("scene")
        captions = raw.get("captions")
        if (
            not isinstance(frame_id, str) or not frame_id
            or isinstance(cue_index, bool) or not isinstance(cue_index, int)
            or isinstance(frame_ms, bool) or not isinstance(frame_ms, int)
            or scene not in {"WATCHED_MEDIA", "NOT_WATCHED_MEDIA", "UNKNOWN"}
            or not isinstance(captions, list)
            or len(captions) > 12
            or any(not isinstance(caption, str) or len(caption) > 500 for caption in captions)
            or (scene != "WATCHED_MEDIA" and captions)
        ):
            row_audit["reason_code"] = "SCENE_OBSERVATION_INVALID"
            continue
        external_button_scene = (
            scene == "NOT_WATCHED_MEDIA"
            and external_scene
            and isinstance(raw.get("source_controls"), Mapping)
        )
        if scene != "WATCHED_MEDIA" and not external_button_scene:
            row_audit["reason_code"] = (
                "SCENE_UNKNOWN_KEEP" if scene == "UNKNOWN" else "SCENE_NOT_WATCHED_MEDIA_KEEP"
            )
            continue
        cue = cue_by_index.get(cue_index)
        if cue is None:
            row_audit["reason_code"] = "SCENE_CUE_NOT_CURRENT"
            continue
        if frame_ms < cue.start_ms or frame_ms >= cue.end_ms or frame_ms >= duration_ms:
            row_audit["reason_code"] = "SCENE_FRAME_CUE_MISMATCH"
            continue
        frame_value = raw.get("frame_path")
        try:
            frame_path_input = Path(frame_value)
            if not frame_path_input.is_absolute():
                frame_path_input = evidence_file.parent / frame_path_input
            frame_path, frame_sha = _path_hash(
                frame_path_input, raw.get("frame_sha256"),
                unavailable="CAPTION_FRAME_UNAVAILABLE",
                mismatch="CAPTION_FRAME_HASH_MISMATCH",
            )
        except (NestedMediaAcousticObserverError, TypeError, ValueError) as exc:
            row_audit["reason_code"] = str(exc)
            continue
        if cue_index in seen_cues:
            row_audit["reason_code"] = "SCENE_CUE_DUPLICATE_KEEP"
            continue
        if captions and any(not caption.strip() for caption in captions):
            row_audit["reason_code"] = "SCENE_CAPTIONS_INVALID"
            continue
        observation_id = _scene_observation_id(candidate_id, cue_index, frame_id)
        if external_button_scene:
            try:
                controls = normalize_source_controls(raw.get("source_controls"))
                if controls is None:
                    raise ValueError("SOURCE_CONTROLS_MISSING")
                # An explicit catalog remains the strict, caller-owned path.
                # The profile map is only an additive cache-acquisition seam;
                # it must never conceal a configured catalog drift/failure.
                if CATALOG_ENV in os.environ:
                    external_reference = resolve_external_audio_reference(
                        controls, cue_text=cue.text, unaligned_visible_reference=True,
                    )
                else:
                    acquired = prepare_external_audio_reference(
                        controls,
                        source_map_path=external_audio_source_map,
                        cache_root=external_audio_cache_root,
                        cue_text=cue.text,
                    )
                    if acquired.get("status") != "READY":
                        external_reference = acquired
                    else:
                        external_reference = resolve_external_audio_reference(
                            controls,
                            catalog_value=acquired.get("catalog_value"),
                            cue_text=cue.text,
                            unaligned_visible_reference=True,
                        )
                        external_reference = {
                            **external_reference,
                            "acquisition": acquired.get("acquisition"),
                        }
            except (TypeError, ValueError) as exc:
                row_audit["reason_code"] = str(exc)
                continue
            row_audit["source_controls"] = controls
            row_audit["external_audio_reference"] = {
                key: value for key, value in external_reference.items()
                if key not in {"source_media_path", "source_media_sha256"}
            }
            if external_reference.get("status") != "READY":
                row_audit["reason_code"] = str(
                    external_reference.get("reason_code", "EXTERNAL_AUDIO_REFERENCE_UNAVAILABLE")
                )
                continue
            context_interval = [
                max(0, int(cue.start_ms) - 4000),
                min(duration_ms, int(cue.end_ms) + 4000),
            ]
            visual = {
                "schema_version": OBSERVATION_SCHEMA,
                "candidate_id": candidate_id,
                "observation_id": observation_id,
                "frame_ms": frame_ms,
                "caption_present": False,
                "visible_text": "",
                "role": "NO_EMBEDDED_CAPTION_OBSERVED",
                "pipeline_burned_subtitle": False,
                "frame_path": str(frame_path),
                "frame_sha256": frame_sha,
                "source_controls": controls,
                "external_audio_candidate": {
                    key: value for key, value in external_reference.items()
                    if key not in {"source_media_path", "source_media_sha256"}
                },
            }
            scope = {
                "schema_version": _ACOUSTIC_ONLY_SCOPE_SCHEMA,
                "kind": _NO_CAPTION_SCOPE_KIND,
                "observation_id": observation_id,
                "scene": "NOT_WATCHED_MEDIA",
                "cue_index": cue_index,
                "cue_interval_ms": [int(cue.start_ms), int(cue.end_ms)],
                "frame_id": frame_id,
                "frame_ms": frame_ms,
                "caption_frame_ms": frame_ms,
                "frame_path": str(frame_path),
                "frame_sha256": frame_sha,
                "evidence_path": str(evidence_file),
                "evidence_sha256": evidence_sha,
                "visual_attribution": "NO_CAPTION_EXTERNAL_AUDIO_BUTTON",
            }
            descriptors.append({
                "observation_id": observation_id,
                "cue_index": cue_index,
                "outer_media_sha256": media_sha,
                "final_srt_sha256": srt_sha,
                "caption_frame_path": str(frame_path),
                "caption_frame_sha256": frame_sha,
                "caption_frame_ms": frame_ms,
                "interval_ms": context_interval,
                "target_interval_ms": [int(cue.start_ms), int(cue.end_ms)],
                "target_cue_indexes": [cue_index],
                "acoustic_only_timing_scope": scope,
                "independent_source_media_path": external_reference["source_media_path"],
                "independent_source_media_sha256": external_reference["source_media_sha256"],
                "source_cache_binding": copy.deepcopy(external_reference["source_cache_binding"]),
                "host_enrollment_path": str(enrollment_path),
                "host_enrollment_sha256": enrollment_sha,
                "visual_observation": visual,
            })
            seen_cues.add(cue_index)
            row_audit.update(
                status="ELIGIBLE", action="PREPARE_EXTERNAL_AUDIO_REFERENCE",
                observation_id=observation_id, context_interval_ms=context_interval,
                target_interval_ms=[int(cue.start_ms), int(cue.end_ms)],
                target_cue_indexes=[cue_index], acoustic_only_timing_scope=scope,
            )
            continue
        candidate_captions = list(captions) if captions else [""]
        visual: dict[str, object] | None = None
        attributed: Mapping[str, object] | None = None
        unmatched_visual: dict[str, object] | None = None
        attempted_attributions: list[object] = []
        for caption in candidate_captions:
            visual_candidate: dict[str, object] = {
                "schema_version": OBSERVATION_SCHEMA,
                "candidate_id": candidate_id,
                "observation_id": observation_id,
                "frame_ms": frame_ms,
                "caption_present": bool(caption.strip()),
                "visible_text": caption.strip(),
                "role": (
                    "WATCHED_MEDIA_CAPTION"
                    if caption.strip()
                    else "NO_EMBEDDED_CAPTION_OBSERVED"
                ),
                "pipeline_burned_subtitle": False,
                "frame_path": str(frame_path),
                "frame_sha256": frame_sha,
            }
            try:
                attribution = build_caption_source_attribution(
                    candidate_id=candidate_id, source_media_path=media_path,
                    source_media_sha256=media_sha, final_srt_path=srt_path,
                    final_srt_sha256=srt_sha, observations=[visual_candidate],
                )
            except (OSError, TypeError, ValueError) as exc:
                row_audit["reason_code"] = "SCENE_CAPTION_ATTRIBUTION_UNAVAILABLE"
                row_audit["error_type"] = type(exc).__name__
                attempted_attributions.append(None)
                continue
            attributed_rows = attribution.get("rows", [])
            candidate_attributed = (
                attributed_rows[0]
                if attributed_rows and isinstance(attributed_rows[0], Mapping)
                else None
            )
            attempted_attributions.append(candidate_attributed)
            if unmatched_visual is None:
                unmatched_visual = visual_candidate
            if (
                isinstance(candidate_attributed, Mapping)
                and cue_index in candidate_attributed.get("cue_indexes", [])
            ):
                visual = visual_candidate
                attributed = candidate_attributed
                break
        if not captions or visual is None or not isinstance(attributed, Mapping) or cue_index not in attributed.get("cue_indexes", []):
            row_audit["reason_code"] = (
                "SCENE_NO_CAPTION_CURRENT_CUE" if not captions
                else "SCENE_CAPTION_UNMATCHED_CURRENT_CUE"
            )
            row_audit["attribution"] = attempted_attributions
            if unmatched_visual is None:
                continue
            # An unmatched or absent caption grants no visual deletion or
            # voice identity. The current WATCHED_MEDIA frame is an acoustic
            # timing anchor because the automatic evidence placed it inside
            # exactly one current SRT cue.  Preserve the evidence binding so
            # the consumer can revalidate it after the prepare handoff.
            visual = unmatched_visual
            context_interval = [max(0, int(cue.start_ms) - 4000),
                                min(duration_ms, int(cue.end_ms) + 4000)]
            scope = {
                "schema_version": _ACOUSTIC_ONLY_SCOPE_SCHEMA,
                "kind": _NO_CAPTION_SCOPE_KIND if not captions else "CURRENT_WATCHED_MEDIA_CUE",
                "observation_id": observation_id,
                "scene": "WATCHED_MEDIA",
                "cue_index": cue_index,
                "cue_interval_ms": [int(cue.start_ms), int(cue.end_ms)],
                "frame_id": frame_id,
                "frame_ms": frame_ms,
                "caption_frame_ms": frame_ms,
                "frame_path": str(frame_path),
                "frame_sha256": frame_sha,
                "evidence_path": str(evidence_file),
                "evidence_sha256": evidence_sha,
                "visual_attribution": (
                    "NO_CAPTION_CURRENT_SCENE" if not captions else "UNMATCHED_CURRENT_CUE"
                ),
            }
            descriptors.append({
                "observation_id": observation_id,
                "cue_index": cue_index,
                "outer_media_sha256": media_sha,
                "final_srt_sha256": srt_sha,
                "caption_frame_path": str(frame_path),
                "caption_frame_sha256": frame_sha,
                "caption_frame_ms": frame_ms,
                "interval_ms": context_interval,
                "target_interval_ms": [int(cue.start_ms), int(cue.end_ms)],
                "target_cue_indexes": [cue_index],
                "acoustic_only_timing_scope": scope,
                "host_enrollment_path": str(enrollment_path),
                "host_enrollment_sha256": enrollment_sha,
                "visual_observation": visual,
            })
            seen_cues.add(cue_index)
            row_audit.update(
                acoustic_only_timing_scope=scope,
                observation_id=observation_id,
                context_interval_ms=context_interval,
                target_interval_ms=[int(cue.start_ms), int(cue.end_ms)],
                target_cue_indexes=[cue_index],
            )
            continue
        # A visible sentence can span several subtitle cues. The context must
        # cover that whole matched span, rather than only this frame's cue.
        matched_interval = attributed["cue_interval_ms"]
        context_interval = [max(0, int(matched_interval[0]) - 4000),
                            min(duration_ms, int(matched_interval[1]) + 4000)]
        descriptors.append({
            "observation_id": observation_id,
            "cue_index": cue_index,
            "outer_media_sha256": media_sha,
            "final_srt_sha256": srt_sha,
            "caption_frame_path": str(frame_path),
            "caption_frame_sha256": frame_sha,
            "caption_frame_ms": frame_ms,
            "interval_ms": context_interval,
            "target_interval_ms": list(matched_interval),
            "target_cue_indexes": list(attributed["cue_indexes"]),
            "host_enrollment_path": str(enrollment_path),
            "host_enrollment_sha256": enrollment_sha,
            "visual_observation": visual,
        })
        seen_cues.add(cue_index)
        row_audit.update(status="ELIGIBLE", action="PREPARE_DESCRIPTOR",
                         observation_id=observation_id, context_interval_ms=context_interval,
                         attribution=dict(attributed))
    audit["descriptor_count"] = len(descriptors)
    audit["eligible_observation_count"] = len(descriptors)
    audit["input_schema"] = INPUT_SCHEMA if descriptors else None
    if descriptors:
        if watched_reference is not None:
            reference_cues: list[int | str] = []
            for descriptor in descriptors:
                visual = descriptor.get("visual_observation")
                acoustic_scope = descriptor.get("acoustic_only_timing_scope")
                if (
                    isinstance(visual, Mapping)
                    and (
                        visual.get("role") == "WATCHED_MEDIA_CAPTION"
                        or (isinstance(acoustic_scope, Mapping)
                            and acoustic_scope.get("scene") == "WATCHED_MEDIA")
                    )
                    and descriptor.get("source_cache_binding") is None
                ):
                    descriptor.update(copy.deepcopy(watched_reference))
                    reference_cues.append(descriptor["cue_index"])
            audit["external_audio_reference"]["prepared_cue_indexes"] = reference_cues
        effective["nested_media_acoustic"] = {
            "schema_version": INPUT_SCHEMA,
            "observations": descriptors,
        }
        audit["status"] = "EVIDENCE_ONLY"
        audit["descriptor_status"] = "AUTO_PREPARED"
    else:
        audit["reason_code"] = "NO_ELIGIBLE_CURRENT_WATCHED_SCENE_ROWS"
    return effective, audit


def prepare_missing_nested_acoustic_audio(
    *, spec: Mapping[str, object], candidate_id: str, source_media: Path,
    subtitle_path: Path, evidence_root: Path,
) -> dict[str, object]:
    """Materialize missing source/target PCM16 crops for existing descriptors.

    This helper is deliberately create-only.  It fills the four source/target
    audio fields only when all four are absent.  A partial or complete audio
    handoff is preserved and left to the existing strict observer validation;
    no old audio path is replaced and no provider is called here.
    """
    result: dict[str, object] = {
        "schema_version": PREPARE_SCHEMA, "candidate_id": candidate_id,
        "status": "NOT_REQUESTED", "observations": [],
    }
    prepared = spec.get("nested_media_acoustic")
    if prepared is None:
        return result
    if (
        not isinstance(prepared, Mapping)
        or prepared.get("schema_version") != INPUT_SCHEMA
        or not isinstance(prepared.get("observations"), list)
    ):
        result.update(status="EVIDENCE_ONLY", reason_code="PREPARED_ACOUSTIC_INPUT_INVALID")
        return result
    result["status"] = "EVIDENCE_ONLY"
    rows: list[dict[str, object]] = []
    for descriptor in prepared["observations"]:
        row: dict[str, object] = {"status": "EVIDENCE_ONLY", "action": "KEEP"}
        rows.append(row)
        if not isinstance(descriptor, Mapping):
            row["reason_code"] = "PREPARED_ACOUSTIC_INPUT_INVALID"
            continue
        row["observation_id"] = descriptor.get("observation_id")
        present = [field for field in _AUDIO_FIELDS if descriptor.get(field) not in (None, "")]
        if present and len(present) != len(_AUDIO_FIELDS):
            row["reason_code"] = "AUDIO_FIELDS_INCOMPLETE"
            continue
        if len(present) == len(_AUDIO_FIELDS):
            try:
                for path_field, hash_field in (
                    ("source_audio_path", "source_audio_sha256"),
                    ("target_audio_path", "target_audio_sha256"),
                ):
                    _path_hash(
                        descriptor[path_field], descriptor[hash_field],
                        unavailable="EXISTING_AUDIO_UNAVAILABLE", mismatch="EXISTING_AUDIO_DRIFT",
                    )
            except NestedMediaAcousticObserverError as exc:
                row["reason_code"] = str(exc)
                continue
            row.update(status="AUDIO_COMPLETE", audio_fields={field: descriptor[field] for field in _AUDIO_FIELDS})
            continue
        try:
            binding, mapping_state = _descriptor_binding(
                descriptor=descriptor, candidate_id=candidate_id,
                source_media=Path(source_media), subtitle_path=Path(subtitle_path),
            )
            source_window = mapping_state["source_window_ms"]
            outer_path = mapping_state["outer_path"]
            request = {
                "schema_version": PREPARE_SCHEMA, "method": dict(_PREPARE_METHOD),
                **binding, "source_window_ms": source_window,
            }
            request_sha = _canonical_hash(request)
            root = Path(evidence_root).expanduser().absolute()
            if root.exists() and root.is_symlink():
                raise NestedMediaAcousticObserverError("AUDIO_PREPARE_ROOT_UNSAFE")
            candidate_dir = root / _safe_segment(candidate_id, code="CANDIDATE_ID_INVALID")
            observation_dir = candidate_dir / _safe_segment(
                str(descriptor["observation_id"]), code="OBSERVATION_ID_INVALID"
            )
            request_dir = observation_dir / request_sha.removeprefix("sha256:")
            source_output = request_dir / "independent-source.pcm16.wav"
            target_output = request_dir / "current-target.pcm16.wav"
            manifest_path = request_dir / "PREPARE.json"
            with exclusive_lock(observation_dir / "prepare.lock", label="nested acoustic audio prepare"):
                if manifest_path.exists() or source_output.exists() or target_output.exists():
                    fields, manifest_sha = _validate_prepared_manifest(
                        manifest_path=manifest_path,
                        expected_binding=request,
                        request_sha=request_sha,
                        expected_outputs={
                            "source_audio_path": source_output,
                            "target_audio_path": target_output,
                        },
                    )
                    row.update(status="REPLAYED", audio_fields=fields,
                               preparation_path=str(manifest_path),
                               preparation_sha256=manifest_sha)
                    continue
                request_dir.mkdir(parents=True, exist_ok=True)
                source_argv, source_probe = _extract_pcm16_wav(
                    source=Path(binding["independent_source_media"]["path"]),
                    output=source_output, window_ms=source_window,
                )
                target_argv, target_probe = _extract_pcm16_wav(
                    source=outer_path, output=target_output, window_ms=binding["interval_ms"],
                )
                manifest: dict[str, object] = {
                    "schema_version": PREPARE_SCHEMA, "status": "PREPARED",
                    "request_sha256": request_sha,
                    "input_binding": request,
                    "source_audio": {
                        "path": str(source_output), "sha256": _sha(source_output),
                        "window_ms": source_window, "argv": source_argv, "probe": source_probe,
                    },
                    "target_audio": {
                        "path": str(target_output), "sha256": _sha(target_output),
                        "window_ms": binding["interval_ms"], "argv": target_argv, "probe": target_probe,
                    },
                }
                manifest_sha = "sha256:" + atomic_write_json(manifest_path, manifest)
                fields = _audio_fields_from_manifest(manifest)
                row.update(status="PREPARED", audio_fields=fields,
                           preparation_path=str(manifest_path),
                           preparation_sha256=manifest_sha)
        except NestedMediaAcousticObserverError as exc:
            row["reason_code"] = str(exc)
        except (OSError, TypeError, ValueError) as exc:
            row["reason_code"] = type(exc).__name__
    result["observations"] = rows
    statuses = {str(row.get("status")) for row in rows}
    if statuses and statuses <= {"PREPARED", "REPLAYED", "AUDIO_COMPLETE"}:
        result["status"] = "PREPARED" if "PREPARED" in statuses else "AUDIO_COMPLETE"
    return result


def _prepared_acoustic_declaration_error(
    spec: Mapping[str, object], prepared: object,
) -> str | None:
    """Validate the ordinary schema and watched-scene declaration contract."""
    if (
        not isinstance(prepared, Mapping)
        or prepared.get("schema_version") != INPUT_SCHEMA
        or not isinstance(prepared.get("observations"), list)
    ):
        return "PREPARED_ACOUSTIC_INPUT_INVALID"
    observations = prepared["observations"]
    external_seed_statuses = [
        _external_voice_reference_seed_status(row)
        if isinstance(row, Mapping) else "INVALID"
        for row in observations
    ]
    voice_or_external_seed_batch = bool(observations) and all(
        isinstance(row, Mapping)
        and (
            row.get("observer_mode") == VOICE_REFERENCE_MODE
            or status == "VALID"
        )
        for row, status in zip(observations, external_seed_statuses)
    )
    external_scene_seed_batch = (
        spec.get("nested_media_external_audio_scene") is True
        and bool(observations)
        and all(status == "VALID" for status in external_seed_statuses)
    )
    if not spec.get("watched_media") and not (
        (spec.get("watched_media") is False and voice_or_external_seed_batch)
        or external_scene_seed_batch
    ):
        return (
            "VOICE_REFERENCE_SOURCE_IDENTITY_INVALID"
            if any(status == "INVALID" for status in external_seed_statuses)
            else "WATCHED_MEDIA_DECLARATION_REQUIRED"
        )
    return None


def observe_prepared_nested_acoustics(
    *, spec: Mapping[str, object], candidate_id: str, source_media: Path,
    subtitle_path: Path, evidence_root: Path, callbacks: object | None = None,
    callbacks_factory: Callable[..., object] | None = None,
    source_cache_root: Path | None = None,
    source_relevance_probe: Callable[..., Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Consume a prepared source handoff against the actual finalizer bytes.

    Missing or stale evidence remains unknown/KEEP.  Never rebind an earlier
    prepared SRT or receipt to the current text to make it pass.
    """
    audit: dict[str, object] = {
        "schema_version": AUDIT_SCHEMA, "candidate_id": candidate_id,
        "status": "NOT_REQUESTED", "observations": [],
        "subtitle_mutations": 0, "display_mutation_authorized": False,
        "publication_authorized": False,
        "provider_calls": 0, "cpa_calls": 0, "provider_attempts": [],
    }
    prepared = spec.get("nested_media_acoustic")
    if prepared is None:
        return audit
    audit["status"] = "EVIDENCE_ONLY"
    declaration_error = _prepared_acoustic_declaration_error(spec, prepared)
    if declaration_error is not None:
        audit["reason_code"] = declaration_error
        return audit
    try:
        media_sha, srt_sha = _sha(source_media), _sha(subtitle_path)
    except OSError:
        audit["reason_code"] = "CURRENT_FINALIZER_INPUT_UNAVAILABLE"
        return audit
    audit.update(source_media_sha256=media_sha, final_srt_sha256=srt_sha)
    rows: list[dict[str, object]] = []
    for descriptor in prepared["observations"]:
        row: dict[str, object] = {"label": "unknown", "action": "KEEP"}
        rows.append(row)
        if not isinstance(descriptor, Mapping):
            row["reason_code"] = "PREPARED_ACOUSTIC_INPUT_INVALID"
            continue
        row["observation_id"] = descriptor.get("observation_id")
        voice_mode = descriptor.get("observer_mode") == VOICE_REFERENCE_MODE
        external_seed_status = _external_voice_reference_seed_status(descriptor)
        if not voice_mode and external_seed_status == "INVALID":
            row["reason_code"] = "VOICE_REFERENCE_SOURCE_IDENTITY_INVALID"
            continue
        external_source_seed = not voice_mode and external_seed_status == "VALID"
        source_fields = ("independent_source_media_path", "independent_source_media_sha256")
        mapping_fields = ("mapping_receipt_path", "mapping_receipt_sha256")
        missing_mapping = all(descriptor.get(field) in (None, "") for field in mapping_fields)
        missing_source = all(descriptor.get(field) in (None, "") for field in source_fields)
        source_cache_verified = False
        cached_source: Mapping[str, object] | None = None
        if voice_mode and any(descriptor.get(field) not in (None, "") for field in mapping_fields):
            row["reason_code"] = "VOICE_REFERENCE_MAPPING_FORBIDDEN"
            continue
        if voice_mode and missing_source:
            row["reason_code"] = "VOICE_REFERENCE_SOURCE_REQUIRED"
            continue
        if voice_mode and any(descriptor.get(field) in (None, "") for field in _VOICE_REFERENCE_BINDING_FIELDS):
            row["reason_code"] = "VOICE_REFERENCE_BINDING_REQUIRED"
            continue
        if not voice_mode and missing_mapping and any(
            descriptor.get(field) not in (None, "") for field in _AUDIO_FIELDS
        ):
            row["reason_code"] = "MAPPING_REQUIRED_FOR_EXISTING_AUDIO"
            continue  # Never attach a newly measured mapping to old audio.
        if not voice_mode and (missing_source or missing_mapping) and not external_source_seed:
            try:
                from src.autoslice.nested_source_cache import (
                    read_cached_caption_source_candidate,
                    read_cached_nested_source,
                )

                locator = spec.get("nested_media_source_locator")
                if source_cache_root is None:
                    raise ValueError("SOURCE_CACHE_CURRENT_LOCATOR_REQUIRED")
                cached: Mapping[str, object] | None = None
                current_locator_failure: str | None = None
                handoff_allowed = locator is None
                if isinstance(locator, Mapping):
                    try:
                        caption_discovery = locator.get("caption_candidate_discovery")
                        if (locator.get("status") == "CANDIDATE_DISCOVERY_ONLY"
                                or (isinstance(caption_discovery, Mapping)
                                    and caption_discovery.get("status") == "CANDIDATE_DISCOVERY_ONLY")):
                            current_cached = read_cached_caption_source_candidate(
                                cache_root=source_cache_root, locator=locator, candidate_id=candidate_id,
                                source_media=source_media, subtitle_path=subtitle_path,
                            )
                            if current_cached.get("status") != "CANDIDATE_CACHE_AVAILABLE":
                                row["current_locator_source_candidate"] = copy.deepcopy(dict(current_cached))
                                current_locator_failure = str(
                                    current_cached.get("reason_code")
                                    or "SOURCE_CACHE_NO_AVAILABLE_PUBLIC_CANDIDATE"
                                )
                                row["current_locator_failure"] = current_locator_failure
                                if current_cached.get("status") != "CANDIDATE_CACHE_UNAVAILABLE":
                                    raise ValueError("SOURCE_CACHE_CURRENT_CANDIDATE_INVALID")
                                handoff_allowed = True
                            else:
                                cached = current_cached
                            # A search/cache hit supplies candidate bytes only.
                            # The unchanged mapping and three-part acoustic gates
                            # below must still establish timing and target role.
                        else:
                            cached = read_cached_nested_source(
                                cache_root=source_cache_root, locator=locator, candidate_id=candidate_id,
                                source_media=source_media, subtitle_path=subtitle_path,
                            )
                            source_cache_verified = True
                    except Exception as exc:
                        current_locator_failure = str(exc)
                        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", current_locator_failure):
                            current_locator_failure = type(exc).__name__
                        row["current_locator_failure"] = current_locator_failure
                        raise
                else:
                    current_locator_failure = "SOURCE_CACHE_CURRENT_LOCATOR_REQUIRED"
                    row["current_locator_failure"] = current_locator_failure

                if cached is None and handoff_allowed and spec.get("nested_media_caption_candidate_handoffs") is not None:
                    if current_locator_failure is not None:
                        row["current_locator_failure"] = current_locator_failure
                    try:
                        cached, handoff = _read_caption_candidate_handoffs(
                            spec=spec, candidate_id=candidate_id, source_media=Path(source_media),
                            subtitle_path=Path(subtitle_path), source_cache_root=source_cache_root,
                            media_sha=media_sha, srt_sha=srt_sha,
                        )
                    except _CaptionCandidateHandoffError as exc:
                        row["caption_candidate_handoff"] = {
                            "schema_version": _CAPTION_CANDIDATE_HANDOFF_SCHEMA,
                            "status": "UNKNOWN_KEEP", "action": "KEEP",
                            "mutation_authorized": False, "source_role_authority": False,
                            "uniqueness_claim": False, "current_candidate_id": candidate_id,
                            "current_outer_media": {
                                "path": str(Path(source_media).resolve()), "sha256": media_sha,
                            },
                            "current_final_srt": {
                                "path": str(Path(subtitle_path).resolve()), "sha256": srt_sha,
                            },
                            "attempts": copy.deepcopy(exc.attempts),
                        }
                        raise
                    row["caption_candidate_handoff"] = handoff
                if cached is None:
                    raise ValueError(current_locator_failure or "SOURCE_CACHE_CURRENT_LOCATOR_REQUIRED")
                if cached.get("status") == "CANDIDATE_CACHE_AVAILABLE":
                    row["caption_source_candidate"] = cached
                cached_source = cached
                if not missing_source:
                    # Preserve explicit source paths, including relocated files;
                    # their actual bytes must match the verified download.
                    _path_hash(
                        descriptor.get(source_fields[0]), descriptor.get(source_fields[1]),
                        unavailable="INDEPENDENT_SOURCE_UNAVAILABLE",
                        mismatch="INDEPENDENT_SOURCE_HASH_MISMATCH",
                    )
                    if str(descriptor[source_fields[1]]).removeprefix("sha256:") != str(cached[source_fields[1]]).removeprefix("sha256:"):
                        raise ValueError("SOURCE_CACHE_EXPLICIT_MEDIA_MISMATCH")
                    descriptor = {**dict(descriptor), "source_cache_binding": cached["source_cache_binding"]}
                else:
                    # Fill a private descriptor copy, preserving the caller's spec.
                    descriptor = {**dict(descriptor), **cached}
                if not missing_mapping:
                    _descriptor_binding(
                        descriptor=descriptor, candidate_id=candidate_id,
                        source_media=source_media, subtitle_path=subtitle_path,
                    )
                row["source_cache_binding"] = cached["source_cache_binding"]
            except Exception as exc:
                row["reason_code"] = "NESTED_SOURCE_CACHE_UNAVAILABLE"
                row["error_type"] = type(exc).__name__
                if re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", str(exc)):
                    row["cache_reason_code"] = str(exc)
                continue
        required = tuple(
            field for field in _BASE_INPUT_FIELDS
            if not missing_mapping or field not in mapping_fields
        )
        if voice_mode:
            required = tuple(
                field for field in required
                if field not in mapping_fields
            ) + _VOICE_REFERENCE_BINDING_FIELDS
        if any(descriptor.get(field) in (None, "") for field in required):
            row["reason_code"] = "PREPARED_ACOUSTIC_INPUT_INCOMPLETE"
            continue
        present_audio = [
            field for field in _AUDIO_FIELDS if descriptor.get(field) not in (None, "")
        ]
        if present_audio and len(present_audio) != len(_AUDIO_FIELDS):
            row["reason_code"] = "AUDIO_FIELDS_INCOMPLETE"
            continue
        audio_complete = len(present_audio) == len(_AUDIO_FIELDS)
        expected_media = "sha256:" + str(descriptor["outer_media_sha256"]).removeprefix("sha256:")
        expected_srt = "sha256:" + str(descriptor["final_srt_sha256"]).removeprefix("sha256:")
        if expected_media != media_sha or expected_srt != srt_sha:
            row["reason_code"] = "PREPARED_ACOUSTIC_INPUT_STALE"
            continue
        visual = descriptor.get("visual_observation")
        if not isinstance(visual, Mapping):
            row["reason_code"] = "CURRENT_VISUAL_OBSERVATION_REQUIRED"
            continue
        try:
            # Validate source/frame/SRT and the visual row before spending a call.
            baseline = build_caption_source_attribution(
                candidate_id=candidate_id, source_media_path=source_media,
                source_media_sha256=media_sha, final_srt_path=subtitle_path,
                final_srt_sha256=srt_sha, observations=[visual],
            )
            if (visual.get("observation_id") != descriptor["observation_id"]
                    or visual.get("frame_ms") != descriptor["caption_frame_ms"]
                    or Path(str(visual.get("frame_path"))).resolve() != Path(str(descriptor["caption_frame_path"])).resolve()
                    or str(visual.get("frame_sha256")).removeprefix("sha256:") != str(descriptor["caption_frame_sha256"]).removeprefix("sha256:")):
                row["reason_code"] = "CURRENT_VISUAL_BINDING_MISMATCH"
                continue
            descriptor_interval = [
                int(item) for item in _interval(
                    descriptor["interval_ms"], code="CURRENT_CUE_INTERVAL_INVALID", integer=True
                )
            ]
            baseline_rows = baseline.get("rows")
            baseline_interval = (
                baseline_rows[0].get("cue_interval_ms")
                if isinstance(baseline_rows, list)
                and baseline_rows
                and isinstance(baseline_rows[0], Mapping)
                else None
            )
            # A semantic context window may surround the current cue.  It
            # must cover the whole cue; the source mapping/frame checks still
            # bind the wider window before extraction or provider dispatch.
            acoustic_only_scope = descriptor.get("acoustic_only_timing_scope")
            acoustic_only_scope_validated = False
            if acoustic_only_scope is not None:
                no_caption = (
                    isinstance(acoustic_only_scope, Mapping)
                    and acoustic_only_scope.get("kind") == _NO_CAPTION_SCOPE_KIND
                )
                if baseline_interval is not None and not no_caption:
                    row["reason_code"] = "CURRENT_ACOUSTIC_SCOPE_REQUIRES_UNMATCHED_CAPTION"
                    continue
                try:
                    target_interval, target_cues = _validate_acoustic_only_timing_scope(
                        descriptor=descriptor, spec=spec, candidate_id=candidate_id,
                        subtitle_path=Path(subtitle_path), media_sha=media_sha,
                        srt_sha=srt_sha, visual=visual,
                        no_caption=no_caption,
                    )
                except NestedMediaAcousticObserverError as exc:
                    row["reason_code"] = str(exc)
                    continue
                acoustic_only_scope_validated = True
                if not (
                    descriptor_interval[0] <= target_interval[0]
                    < target_interval[1] <= descriptor_interval[1]
                ):
                    row["reason_code"] = "CURRENT_ACOUSTIC_SCOPE_CONTEXT_MISMATCH"
                    continue
                # Keep visual attribution unmatched.  This target is only an
                # independently validated audio timing scope.
                row["cue_interval_ms"] = None
                row["context_interval_ms"] = descriptor_interval
                row["acoustic_only_timing_scope"] = dict(acoustic_only_scope)
            else:
                if not (
                    isinstance(baseline_interval, list)
                    and len(baseline_interval) == 2
                    and descriptor_interval[0] <= baseline_interval[0]
                    < baseline_interval[1] <= descriptor_interval[1]
                ):
                    row["reason_code"] = "CURRENT_CUE_INTERVAL_MISMATCH"
                    continue
                target_interval = baseline_interval
                row["cue_interval_ms"] = baseline_interval
                row["context_interval_ms"] = descriptor_interval
                target_cues = baseline_rows[0]["cue_indexes"]
                focus_fields = ("target_interval_ms", "target_cue_indexes")
                if any(descriptor.get(field) is not None for field in focus_fields):
                    if (_canonical_hash(descriptor.get("target_interval_ms")) != _canonical_hash(baseline_interval)
                            or _canonical_hash(descriptor.get("target_cue_indexes")) != _canonical_hash(target_cues)):
                        row["reason_code"] = "CURRENT_TARGET_CUE_BINDING_MISMATCH"
                        continue
                if not baseline_interval[0] <= descriptor["caption_frame_ms"] < baseline_interval[1]:
                    row["reason_code"] = "CURRENT_TARGET_FRAME_INTERVAL_MISMATCH"
                    continue
            if voice_mode:
                if not isinstance(target_cues, list) or len(target_cues) != 1:
                    row["reason_code"] = "VOICE_REFERENCE_TARGET_SINGLE_CUE_REQUIRED"
                    continue
                # A voice-reference request is always one current cue.  A
                # prior joint/overlap receipt can never be projected here.
                declared_target = descriptor.get("target_interval_ms")
                declared_cues = descriptor.get("target_cue_indexes")
                if declared_target is not None and not _same_interval(declared_target, target_interval):
                    row["reason_code"] = "VOICE_REFERENCE_TARGET_SCOPE_MISMATCH"
                    continue
                if declared_cues is not None and declared_cues != target_cues:
                    row["reason_code"] = "VOICE_REFERENCE_TARGET_SCOPE_MISMATCH"
                    continue
            if external_source_seed:
                # This is the ordinary source-discovery handoff for an
                # independent public recording.  The helper verifies the
                # complete source bytes and current outer/SRT/frame/host
                # bindings, then materializes the two PCM inputs.  It does
                # not infer a role or perform source-time alignment.
                try:
                    descriptor, voice_preparation = _prepare_automatic_voice_reference(
                        descriptor=descriptor, candidate_id=candidate_id,
                        source_media=Path(source_media), subtitle_path=Path(subtitle_path),
                        evidence_root=Path(evidence_root) / "voice-reference",
                        media_sha=media_sha, srt_sha=srt_sha,
                        source_cache={
                            "independent_source_media_path": descriptor[source_fields[0]],
                            "independent_source_media_sha256": descriptor[source_fields[1]],
                            "source_cache_binding": descriptor["source_cache_binding"],
                        }, context_interval=descriptor_interval,
                        target_interval=target_interval, target_cues=list(target_cues),
                        outer_context=spec.get("nested_media_outer_context"),
                    )
                except (NestedMediaAcousticObserverError, OSError, TypeError, ValueError) as exc:
                    row["voice_reference_fallback"] = {
                        "status": "UNKNOWN_KEEP", "reason_code": str(exc),
                    }
                    row["reason_code"] = str(exc)
                    continue
                voice_mode = True
                audio_complete = True
                row["audio_preparation"] = voice_preparation
                row["voice_reference_fallback"] = voice_preparation
                row["source_cache_binding"] = descriptor.get("source_cache_binding")
                row["voice_reference_mode"] = VOICE_REFERENCE_MODE
            # This is a new private request, not a rebinding of an old role
            # receipt. Keep context audio for voice recognition while binding
            # every role decision to the current matched cue span separately.
            descriptor = {**dict(descriptor), "target_interval_ms": target_interval,
                          "target_cue_indexes": target_cues}
            row["target_interval_ms"] = target_interval
            row["target_cue_indexes"] = target_cues
            local_scope = descriptor.get(LOCAL_MAPPING_SCOPE_FIELD)
            if voice_mode and (local_scope is not None or descriptor.get(CURRENT_CUE_SCOPE_FIELD) is not None):
                row["reason_code"] = "VOICE_REFERENCE_MAPPING_FORBIDDEN"
                continue
            if local_scope is not None:
                try:
                    local_scope = _validate_descriptor_local_mapping_scope(
                        descriptor=descriptor, scope=local_scope,
                        context_interval=descriptor_interval,
                        target_interval=target_interval,
                        target_cues=target_cues,
                    )
                except NestedMediaAcousticObserverError as exc:
                    row["reason_code"] = str(exc)
                    continue
            current_scope = descriptor.get(CURRENT_CUE_SCOPE_FIELD)
            if current_scope is not None:
                try:
                    current_scope = _validate_descriptor_current_cue_scope(
                        descriptor=descriptor, scope=current_scope,
                        context_interval=descriptor_interval,
                        target_interval=target_interval,
                        target_cues=target_cues,
                        candidate_id=candidate_id, media_sha=media_sha, srt_sha=srt_sha,
                    )
                except NestedMediaAcousticObserverError as exc:
                    row["reason_code"] = str(exc)
                    continue
            elif missing_mapping and local_scope is None and not voice_mode:
                try:
                    current_scope = _current_cue_scope_from_descriptor(
                        descriptor=descriptor, candidate_id=candidate_id,
                        context_interval=descriptor_interval,
                        target_interval=target_interval, target_cues=target_cues,
                        media_sha=media_sha, srt_sha=srt_sha,
                        validated_acoustic_only=acoustic_only_scope_validated,
                    )
                except NestedMediaAcousticObserverError as exc:
                    row["reason_code"] = str(exc)
                    continue
                if current_scope is not None:
                    descriptor = {**dict(descriptor), CURRENT_CUE_SCOPE_FIELD: current_scope}
            if missing_mapping and not voice_mode:
                # Only a real current source cache supplies the identity. Check
                # immutable source/host bytes before measuring a new time map.
                from src.autoslice.nested_source_audio_alignment import prepare_nested_source_mapping

                independent_source, _ = _path_hash(
                    descriptor[source_fields[0]], descriptor[source_fields[1]],
                    unavailable="INDEPENDENT_SOURCE_UNAVAILABLE",
                    mismatch="INDEPENDENT_SOURCE_HASH_MISMATCH",
                )
                _path_hash(
                    descriptor["host_enrollment_path"], descriptor["host_enrollment_sha256"],
                    unavailable="HOST_ENROLLMENT_UNAVAILABLE", mismatch="HOST_ENROLLMENT_HASH_MISMATCH",
                )
                source_identity = descriptor["source_cache_binding"]
                mapping = prepare_nested_source_mapping(
                    outer_media=source_media, source_media=independent_source,
                    source_identity={"bvid": source_identity["bvid"], "cid": source_identity["cid"]},
                    interval_ms=descriptor_interval, evidence_root=Path(evidence_root) / "source-alignment",
                    local_scope=local_scope, current_cue_scope=current_scope,
                    candidate_id=candidate_id,
                    outer_context=(spec.get("nested_media_outer_context")
                                   if current_scope is not None else None),
                )
                row["source_mapping_preparation"] = mapping
                receipt = mapping.get("mapping_receipt")
                if mapping.get("decision") != "MAPPING_CANDIDATE_BOUND" or not isinstance(receipt, Mapping):
                    # Keep the failed time map. Candidate bytes may supply a
                    # voice reference only after a separate frame comparison;
                    # that comparison does not promote source identity.
                    row["source_mapping_failure"] = copy.deepcopy(dict(mapping))
                    if not isinstance(cached_source, Mapping):
                        row["reason_code"] = "SOURCE_MAPPING_UNSUPPORTED"
                        continue
                    if not source_cache_verified:
                        if source_relevance_probe is None or len(target_cues) != 1:
                            row["reason_code"] = "SOURCE_MAPPING_UNSUPPORTED"
                            continue
                        try:
                            _reject_voice_reference_outer_reuse(
                                source_path=independent_source,
                                source_sha=str(descriptor[source_fields[1]]),
                                outer_path=Path(source_media), outer_sha=media_sha,
                                outer_context=spec.get("nested_media_outer_context"),
                                source_identity={key: source_identity.get(key)
                                                 for key in ("bvid", "cid")},
                            )
                            relevance = source_relevance_probe(
                                candidate_id=candidate_id,
                                descriptor={**dict(descriptor), **{
                                    field: spec[field] for field in (
                                        "nested_media_caption_dedup", "nested_media_outer_context",
                                    ) if field in spec
                                }},
                                cached_source=cached_source, source_media=Path(source_media),
                                subtitle_path=Path(subtitle_path),
                                evidence_root=Path(evidence_root) / "source-frame-relevance",
                            )
                        except Exception as exc:
                            row["reason_code"] = "SOURCE_FRAME_RELEVANCE_UNAVAILABLE"
                            row["error_type"] = type(exc).__name__
                            continue
                        row["source_frame_relevance"] = copy.deepcopy(dict(relevance))
                        if (relevance.get("decision") != "MATCH"
                                or relevance.get("authority") != "EVIDENCE_ONLY"
                                or relevance.get("action") != "KEEP"):
                            row["reason_code"] = "SOURCE_FRAME_RELEVANCE_UNCONFIRMED"
                            continue
                    try:
                        if len(target_cues) != 1:
                            raise NestedMediaAcousticObserverError(
                                "VOICE_REFERENCE_TARGET_SINGLE_CUE_REQUIRED"
                            )
                        descriptor, voice_preparation = _prepare_automatic_voice_reference(
                            descriptor=descriptor, candidate_id=candidate_id,
                            source_media=Path(source_media), subtitle_path=Path(subtitle_path),
                            evidence_root=Path(evidence_root) / "voice-reference",
                            media_sha=media_sha, srt_sha=srt_sha,
                            source_cache=cached_source, context_interval=descriptor_interval,
                            target_interval=target_interval, target_cues=list(target_cues),
                            outer_context=spec.get("nested_media_outer_context"),
                        )
                    except (NestedMediaAcousticObserverError, OSError, TypeError, ValueError) as exc:
                        row["voice_reference_fallback"] = {
                            "status": "UNKNOWN_KEEP", "reason_code": str(exc),
                        }
                        row["reason_code"] = "SOURCE_MAPPING_UNSUPPORTED"
                        continue
                    voice_mode = True
                    audio_complete = True
                    row["audio_preparation"] = voice_preparation
                    row["voice_reference_fallback"] = voice_preparation
                    row["source_cache_binding"] = cached_source.get("source_cache_binding")
                    row["target_interval_ms"] = target_interval
                    row["target_cue_indexes"] = target_cues
                else:
                    descriptor = {**dict(descriptor), "mapping_receipt_path": receipt["path"],
                                  "mapping_receipt_sha256": receipt["sha256"]}
                    # A time map is evidence only. It must pass the unchanged full
                    # source/frame/current-SRT mapping consumer before audio/calls.
                    _descriptor_binding(
                        descriptor=descriptor, candidate_id=candidate_id,
                        source_media=source_media, subtitle_path=subtitle_path,
                    )
            dispatch_descriptor: Mapping[str, object] = descriptor
            if voice_mode:
                try:
                    voice_binding, _voice_state = _voice_descriptor_binding(
                        descriptor=descriptor, candidate_id=candidate_id,
                        source_media=source_media, subtitle_path=subtitle_path,
                    )
                    nested_binding = voice_binding.get("voice_reference_binding")
                    if isinstance(nested_binding, Mapping) and isinstance(
                        nested_binding.get("playback_locator"), Mapping
                    ):
                        dispatch_descriptor = {
                            **dict(descriptor),
                            "playback_locator": copy.deepcopy(
                                dict(nested_binding["playback_locator"])
                            ),
                        }
                except NestedMediaAcousticObserverError as exc:
                    row["reason_code"] = str(exc)
                    continue
            if not audio_complete:
                # The preparation helper receives a private same-schema copy;
                # the caller's spec/descriptor stays immutable for this run.
                preparation_spec = {
                    "nested_media_acoustic": {
                        "schema_version": INPUT_SCHEMA,
                        "observations": [dict(descriptor)],
                    }
                }
                if voice_mode:
                    row["reason_code"] = "VOICE_REFERENCE_AUDIO_REQUIRED"
                    continue
                preparation = prepare_missing_nested_acoustic_audio(
                    spec=preparation_spec, candidate_id=candidate_id,
                    source_media=Path(source_media), subtitle_path=Path(subtitle_path),
                    evidence_root=Path(evidence_root) / "audio-preparation",
                )
                preparation_rows = preparation.get("observations")
                preparation_row = (
                    preparation_rows[0]
                    if isinstance(preparation_rows, list)
                    and preparation_rows
                    and isinstance(preparation_rows[0], Mapping)
                    else None
                )
                row["audio_preparation"] = preparation_row or preparation
                if (
                    not isinstance(preparation_row, Mapping)
                    or preparation_row.get("status") not in {"PREPARED", "REPLAYED"}
                    or not isinstance(preparation_row.get("audio_fields"), Mapping)
                ):
                    row["reason_code"] = (
                        preparation_row.get("reason_code", "AUDIO_PREPARATION_UNAVAILABLE")
                        if isinstance(preparation_row, Mapping)
                        else "AUDIO_PREPARATION_UNAVAILABLE"
                    )
                    continue
                dispatch_descriptor = {
                    **dict(descriptor), **dict(preparation_row["audio_fields"])
                }
            dispatch_callbacks = callbacks
            if callbacks_factory is not None:
                try:
                    dispatch_callbacks = callbacks_factory(
                        descriptor=dispatch_descriptor,
                        candidate_id=candidate_id,
                        observation_id=str(dispatch_descriptor["observation_id"]),
                        evidence_root=evidence_root,
                    )
                except Exception as exc:
                    row["reason_code"] = "NESTED_ACOUSTIC_CALLBACK_FACTORY_UNAVAILABLE"
                    row["error_type"] = type(exc).__name__
                    continue
            if dispatch_callbacks is None:
                row["reason_code"] = "NESTED_ACOUSTIC_CALLBACKS_UNAVAILABLE"
                continue
            try:
                observe_callback = dispatch_callbacks.observe_audio
                cpa_callback = dispatch_callbacks.cpa_judge
                runtime_identity = dispatch_callbacks.runtime_identity
            except AttributeError:
                row["reason_code"] = "NESTED_ACOUSTIC_CALLBACKS_INVALID"
                continue
            observer_kwargs = {
                field: dispatch_descriptor[field]
                for field in _INPUT_FIELDS
                if field in dispatch_descriptor
            }
            if voice_mode:
                observer_kwargs["mapping_receipt_path"] = None
                observer_kwargs["mapping_receipt_sha256"] = None
            result = observe_nested_media_acoustics(
                **observer_kwargs,
                target_interval_ms=dispatch_descriptor["target_interval_ms"],
                target_cue_indexes=dispatch_descriptor["target_cue_indexes"],
                target_audio_window_ms=dispatch_descriptor.get("target_audio_window_ms"),
                candidate_id=candidate_id, outer_media_path=source_media,
                final_srt_path=subtitle_path, evidence_root=evidence_root,
                observe_audio=observe_callback, cpa_judge=cpa_callback,
                runtime_identity=runtime_identity,
                observer_mode=(VOICE_REFERENCE_MODE if voice_mode else None),
                voice_reference_binding_path=(
                    dispatch_descriptor.get("voice_reference_binding_path") if voice_mode else None
                ),
                voice_reference_binding_sha256=(
                    dispatch_descriptor.get("voice_reference_binding_sha256") if voice_mode else None
                ),
            )
            row["observer_result"] = result
            # Keep the exact dispatched inputs for current downstream receipt
            # rereads. Labels and raw model prose are not role authority.
            row["validated_inputs"] = copy.deepcopy(dict(dispatch_descriptor))
            provider_trace = _observer_provider_trace(result, Path(evidence_root))
            if provider_trace is not None:
                attempts = provider_trace.get("provider_attempts")
                if not isinstance(attempts, list):
                    attempts = []
                row["provider_trace"] = provider_trace
                replayed = provider_trace.get("replayed") is True
                if replayed:
                    row["historical_provider_attempts"] = copy.deepcopy(attempts)
                    historical = audit.setdefault("historical_provider_attempts", [])
                    if isinstance(historical, list):
                        historical.extend(copy.deepcopy(attempts))
                else:
                    # An AGY attempt may reuse an existing native completion.
                    # Count actual jobs, retaining its unknown underlying API
                    # count and historical jobs separately in the typed trace.
                    dispatches = sum(
                        attempt.get("job_invocations", 1)
                        if attempt.get("transport") == "agy_three_part" else 1
                        for attempt in attempts
                    )
                    audit["provider_calls"] = int(audit.get("provider_calls", 0)) + dispatches
                    audit_attempts = audit.get("provider_attempts")
                    if isinstance(audit_attempts, list):
                        audit_attempts.extend(copy.deepcopy(attempts))
                    row["provider_attempts"] = copy.deepcopy(attempts)
                reason_code = provider_trace.get("reason_code")
                if isinstance(reason_code, str) and reason_code:
                    if replayed:
                        row["historical_observer_reason_code"] = reason_code
                    else:
                        row["observer_reason_code"] = reason_code
                    if reason_code == "PROVIDER_ERROR" and not replayed:
                        row["reason_code"] = "PROVIDER_ERROR"
                        audit["reason_code"] = "PROVIDER_ERROR"
                        failures = audit.setdefault("provider_failures", [])
                        if isinstance(failures, list):
                            failures.append(copy.deepcopy(provider_trace))
                    elif reason_code == "PROVIDER_ERROR" and replayed:
                        failures = audit.setdefault("historical_provider_failures", [])
                        if isinstance(failures, list):
                            failures.append(copy.deepcopy(provider_trace))
            if result.get("cpa_response_path") and result.get("replayed") is not True:
                audit["cpa_calls"] = int(audit.get("cpa_calls", 0)) + 1
            elif result.get("cpa_response_path"):
                audit["historical_cpa_calls"] = int(audit.get("historical_cpa_calls", 0)) + 1
            binding = result.get("receipt_binding") if result.get("status") == "PASS" else None
            attributed = (build_caption_source_attribution(
                candidate_id=candidate_id, source_media_path=source_media,
                source_media_sha256=media_sha, final_srt_path=subtitle_path,
                final_srt_sha256=srt_sha, observations=[visual],
                acoustic_receipt_bindings=[binding],
            ) if isinstance(binding, Mapping) and not voice_mode else baseline)
            row["attribution"] = attributed
            if voice_mode:
                if isinstance(binding, Mapping) and result.get("status") == "PASS":
                    row["label"] = str(result.get("final_class", "unknown"))
                row["action"] = "KEEP"
                row["voice_reference_mode"] = VOICE_REFERENCE_MODE
                row["voice_reference_authority"] = "EVIDENCE_ONLY"
                continue
            actual_rows = attributed.get("rows", [])
            if actual_rows and isinstance(actual_rows[0], Mapping):
                visual_label = actual_rows[0].get("label", "unknown")
                if acoustic_only_scope is not None:
                    if acoustic_only_scope.get("kind") == _NO_CAPTION_SCOPE_KIND:
                        # Voice receipts do not turn an absent visual caption
                        # into a display-deletion proposal. Keep the original
                        # no-caption control and consume only its logical role.
                        row["attribution"] = baseline
                        row["visual_caption_label"] = baseline["rows"][0].get("label", "unknown")
                        role = _verified_receipt_role(attributed)
                        if role is not None:
                            row["label"] = role
                        continue
                    # The visual caption remains unmatched/KEEP.  A role is
                    # promoted only after attribution rereads and validates the
                    # current PASS receipt; never trust the in-memory result.
                    row["visual_caption_label"] = visual_label
                    row["action"] = "KEEP"
                    if (
                        actual_rows[0].get("visual_match") == "unmatched"
                        and visual_label == "unknown"
                        and actual_rows[0].get("action") == "KEEP"
                        and attributed.get("safe_to_drop_cue_indexes") == []
                    ):
                        role = _verified_receipt_role(attributed)
                        if role is not None:
                            row["label"] = role
                else:
                    row["label"] = visual_label
            # Attribution's possible display action stays a proposal.  This
            # finalizer handoff preserves the current transcript and style.
        except Exception as exc:
            row["reason_code"] = "NESTED_ACOUSTIC_EVIDENCE_UNAVAILABLE"
            row["error_type"] = type(exc).__name__
    audit["observations"] = rows
    return audit


def current_caption_audio_roles(
    *, candidate_id: str, source_media: Path, subtitle_path: Path,
    acoustic_audit: Mapping[str, object],
) -> dict[str, object]:
    """Reread existing normal receipts; never dispatch or mutate text.

    Logical stream ownership is separate from subtitle display/deletion. A
    stale full SRT, PCM, mapping, frame or typed receipt withdraws the role.
    """
    media_sha, srt_sha = _sha(source_media), _sha(subtitle_path)
    cues = parse_timed_srt(subtitle_path.read_text(encoding="utf-8-sig"), label="audio roles")
    rows = [{"cue_index": int(c.index) if c.index.isdigit() else c.index,
             "interval_ms": [c.start_ms, c.end_ms], "role": "unknown", "action": "KEEP",
             "receipts": []} for c in cues]
    by_index = {r["cue_index"]: r for r in rows}
    votes: dict[object, list[str]] = {}
    failures = []
    for observed in acoustic_audit.get("observations", []):
        if not isinstance(observed, Mapping):
            continue
        descriptor, result = observed.get("validated_inputs"), observed.get("observer_result")
        if not isinstance(descriptor, Mapping) or not isinstance(result, Mapping):
            continue
        receipt_binding = result.get("receipt_binding")
        if not isinstance(receipt_binding, Mapping):
            continue
        try:
            voice_mode = descriptor.get("observer_mode") == VOICE_REFERENCE_MODE
            binding_source_media, binding_subtitle_path = source_media, subtitle_path
            if voice_mode:
                binding_source_media, binding_subtitle_path = _voice_consumer_original_paths(
                    descriptor=descriptor, source_media=source_media, subtitle_path=subtitle_path,
                )
            binding, _ = _descriptor_binding(
                descriptor=descriptor, candidate_id=candidate_id,
                source_media=binding_source_media, subtitle_path=binding_subtitle_path,
            )
            visual = descriptor["visual_observation"]
            request_path = Path(str(result["request_path"]))
            request = _json_mapping(request_path, code="ACOUSTIC_REQUEST_INVALID")
            request_sha = request["request_sha256"]
            receipt_path, _ = _path_hash(receipt_binding["path"], receipt_binding["sha256"],
                                         unavailable="ACOUSTIC_RECEIPT_UNAVAILABLE",
                                         mismatch="ACOUSTIC_RECEIPT_HASH_MISMATCH")
            receipt = _json_mapping(receipt_path, code="ACOUSTIC_RECEIPT_INVALID")
            file_fields = (("outer", "outer_media"), ("srt", "final_srt"),
                           ("frame", "caption_frame"), ("source_media", "independent_source_media"),
                           ("host", "host_enrollment"))
            if not voice_mode:
                file_fields = (*file_fields, ("mapping", "mapping_receipt"))
            files = {
                key: (Path(str(binding[field]["path"])), str(binding[field]["sha256"]))
                for key, field in file_fields
            }
            for key, field in (("source_audio", "source_audio"), ("target_audio", "target_audio")):
                files[key] = _path_hash(descriptor[field + "_path"], descriptor[field + "_sha256"],
                                        unavailable="ACOUSTIC_PCM_UNAVAILABLE", mismatch="ACOUSTIC_PCM_HASH_MISMATCH")
            mapping_info = None
            voice_reference_info = None
            if voice_mode:
                voice_binding = binding.get("voice_reference_binding")
                if not isinstance(voice_binding, Mapping):
                    raise ValueError("VOICE_REFERENCE_BINDING_INVALID")
                voice_reference_info = {
                    key: value for key, value in voice_binding.items()
                    if key not in {"path", "sha256"}
                }
            else:
                mapping_info = {key: value for key, value in binding["mapping_receipt"].items()
                                if key not in {"path", "sha256"}}
            target_interval = descriptor["target_interval_ms"]
            target_indexes = descriptor["target_cue_indexes"]
            # Reconstruct the current request surface, preserving only the
            # originally typed runtime identity and policy from normal input.
            for key in ("candidate_id", "observation_id", "interval_ms", "target_interval_ms", "target_cue_indexes"):
                expected = candidate_id if key == "candidate_id" else descriptor[key]
                if request.get(key) != expected:
                    raise ValueError("ACOUSTIC_REQUEST_SCOPE_DRIFT")
            if voice_mode:
                if (
                    request.get("observer_mode") != VOICE_REFERENCE_MODE
                    or "mapping_receipt" in request
                    or request.get("voice_reference_binding") != binding["voice_reference_binding"]
                ):
                    raise ValueError("VOICE_REFERENCE_REQUEST_INPUT_DRIFT")
            else:
                for key in ("outer_media", "final_srt", "caption_frame", "independent_source_media", "mapping_receipt"):
                    if request.get(key) != binding[key]:
                        raise ValueError("ACOUSTIC_REQUEST_INPUT_DRIFT")
            expected_parts = [{"role": role, "path": str(files[key][0]), "sha256": files[key][1]}
                              for role, key in (("host_enrollment", "host"), ("independent_source", "source_audio"),
                                                ("current_target", "target_audio"))]
            if request.get("audio_parts") != expected_parts:
                raise ValueError("ACOUSTIC_REQUEST_PCM_DRIFT")
            _validate_cached_pass(
                receipt=receipt, receipt_path=receipt_path, request_path=request_path,
                observer_path=Path(str(result["observer_response_path"])),
                cpa_path=Path(str(result["cpa_response_path"])), request=request, request_sha=request_sha,
                candidate_id=candidate_id, observation_id=str(descriptor["observation_id"]),
                interval=list(descriptor["interval_ms"]), target_interval=list(target_interval),
                target_cue_indexes=list(target_indexes), files=files, mapping_info=mapping_info,
                observer_mode=(VOICE_REFERENCE_MODE if voice_mode else None),
                voice_reference_info=voice_reference_info,
            )
            if voice_mode:
                if len(target_indexes) != 1 or receipt.get("observer_mode") != VOICE_REFERENCE_MODE:
                    raise ValueError("VOICE_REFERENCE_TARGET_SINGLE_CUE_REQUIRED")
                role = receipt.get("final_class", "unknown")
                if role not in _ACOUSTIC_ROLES:
                    role = "unknown"
            else:
                attribution = build_caption_source_attribution(
                    candidate_id=candidate_id, source_media_path=source_media, source_media_sha256=media_sha,
                    final_srt_path=subtitle_path, final_srt_sha256=srt_sha,
                    observations=[visual], acoustic_receipt_bindings=[receipt_binding],
                )
                if descriptor.get("acoustic_only_timing_scope") is not None:
                    scope = descriptor["acoustic_only_timing_scope"]
                    _validate_acoustic_only_timing_scope(
                        descriptor=descriptor,
                        spec={"nested_media_caption_dedup": {
                            "policy": CAPTION_DEDUP_POLICY,
                            "evidence_path": scope["evidence_path"],
                            "evidence_sha256": scope["evidence_sha256"],
                        }},
                        candidate_id=candidate_id,
                        subtitle_path=subtitle_path,
                        media_sha=media_sha,
                        srt_sha=srt_sha,
                        visual=visual,
                        no_caption=scope.get("kind") == _NO_CAPTION_SCOPE_KIND,
                    )
                    role = _verified_receipt_role(attribution)
                else:
                    aligned = attribution["rows"][0]
                    if aligned["cue_indexes"] != target_indexes or aligned["cue_interval_ms"] != target_interval:
                        raise ValueError("ACOUSTIC_TARGET_ALIGNMENT_DRIFT")
                    role = _verified_receipt_role(attribution)
            if role not in _ACOUSTIC_ROLES or any(i not in by_index for i in target_indexes):
                raise ValueError("ACOUSTIC_ROLE_UNAVAILABLE")
            selected = [by_index[i] for i in target_indexes]
            if [selected[0]["interval_ms"][0], selected[-1]["interval_ms"][1]] != target_interval:
                raise ValueError("ACOUSTIC_TARGET_INTERVAL_DRIFT")
            for row in selected:
                votes.setdefault(row["cue_index"], []).append(role)
                row["receipts"].append(dict(receipt_binding))
        except Exception as exc:
            failures.append({"observation_id": descriptor.get("observation_id"),
                             "reason_code": str(exc) if re.fullmatch(r"[A-Z][A-Z0-9_]+", str(exc)) else type(exc).__name__})
    for row in rows:
        roles = set(votes.get(row["cue_index"], []))
        row["role"] = roles.pop() if len(roles) == 1 else "unknown"
    return {"candidate_id": candidate_id, "source_media_sha256": media_sha,
            "final_srt_sha256": srt_sha, "status": "EVIDENCE_ONLY", "mutation_authorized": False,
            "uniform_host_display_only": True, "rows": rows, "unavailable_observations": failures}


def split_current_acoustic_observations_for_reuse(
    *, candidate_id: str, source_media: Path, subtitle_path: Path,
    previous_audit: Mapping[str, object] | None,
    current_observations: object,
    current_spec: Mapping[str, object] | None = None,
    current_prepared: object = None,
) -> dict[str, object]:
    """Partition current descriptors by strict, already validated observations.

    The normal finalizer may receive a new output directory for a later pass.
    This helper only reuses observations already present in the supplied audit;
    it never searches other run directories or copies a receipt into a new
    cache.  ``current_caption_audio_roles`` rereads the complete old request,
    receipt, frame, PCM and source/voice bindings against the current outer
    media and SRT before any descriptor can be reused.  A canonical descriptor
    comparison requires identical current declared inputs.  An ordinary
    external-audio seed has not yet materialized its PCM/binding fields; it
    may match the same seed of a fully revalidated voice request without
    recreating those derived files or changing the original receipt.
    """
    observations = current_observations if isinstance(current_observations, list) else []
    result: dict[str, object] = {
        "reused_observations": [],
        "unmatched_observations": [
            copy.deepcopy(item) for item in observations
        ],
        "reused_count": 0,
        "validated_previous_receipt_count": 0,
    }
    if not isinstance(previous_audit, Mapping):
        return result
    if current_spec is not None:
        declaration_error = _prepared_acoustic_declaration_error(
            current_spec, current_prepared,
        )
        if declaration_error is not None:
            return result

    roles = current_caption_audio_roles(
        candidate_id=candidate_id, source_media=source_media,
        subtitle_path=subtitle_path, acoustic_audit=previous_audit,
    )
    valid_receipts = {
        (receipt.get("path"), receipt.get("sha256"))
        for row in roles.get("rows", [])
        if isinstance(row, Mapping)
        for receipt in row.get("receipts", [])
        if isinstance(receipt, Mapping)
        and isinstance(receipt.get("path"), str)
        and isinstance(receipt.get("sha256"), str)
    }
    result["validated_previous_receipt_count"] = len(valid_receipts)
    if not valid_receipts:
        return result

    def key_for_descriptor(descriptor: Mapping[str, object]) -> str:
        def json_value(value: object) -> object:
            if isinstance(value, Path):
                return str(value)
            if isinstance(value, Mapping):
                return {str(key): json_value(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [json_value(item) for item in value]
            return value

        return _canonical_hash(json_value(descriptor))

    def request_policy_for_descriptor(descriptor: Mapping[str, object]) -> dict[str, object]:
        policy: dict[str, object] = {
            "correlation_alone_is_insufficient": True,
            "unknown_action": "KEEP",
            "mutation_authorized": False,
        }
        if descriptor.get("observer_mode") == VOICE_REFERENCE_MODE:
            policy.update(
                time_alignment_performed=False,
                source_subtraction_authorized=False,
                caption_dedup_authority=False,
                source_role_authority=False,
            )
        return policy

    def current_transport(prepared: object) -> str | None:
        if not isinstance(prepared, Mapping):
            return None
        value = prepared.get("observer_transport", "gemini_api")
        if value is None:
            return "gemini_api"
        return value if isinstance(value, str) and value else None

    declared_transport = current_transport(current_prepared)
    if declared_transport is None:
        return result
    by_descriptor: dict[str, list[tuple[tuple[str, str], dict[str, object]]]] = {}
    by_external_seed: dict[str, list[tuple[tuple[str, str], dict[str, object]]]] = {}
    previous_rows = previous_audit.get("observations", [])
    if not isinstance(previous_rows, list):
        return result
    for observed in previous_rows:
        if not isinstance(observed, Mapping):
            continue
        descriptor = observed.get("validated_inputs")
        observer_result = observed.get("observer_result")
        if not isinstance(descriptor, Mapping) or not isinstance(observer_result, Mapping):
            continue
        receipt_binding = observer_result.get("receipt_binding")
        if not isinstance(receipt_binding, Mapping):
            continue
        receipt_key = (receipt_binding.get("path"), receipt_binding.get("sha256"))
        if receipt_key not in valid_receipts:
            continue
        request_path = observer_result.get("request_path")
        if not isinstance(request_path, str) or not request_path:
            continue
        try:
            request = _json_mapping(Path(request_path), code="ACOUSTIC_REQUEST_INVALID")
        except (NestedMediaAcousticObserverError, OSError, UnicodeError, json.JSONDecodeError):
            continue
        if request.get("policy") != request_policy_for_descriptor(descriptor):
            continue
        runtime_identity = request.get("runtime_identity")
        if not isinstance(runtime_identity, Mapping):
            continue
        previous_transport = runtime_identity.get("transport")
        if not isinstance(previous_transport, str) or not previous_transport:
            continue
        if declared_transport == "agy_three_part":
            if previous_transport != "agy_three_part":
                continue
        elif declared_transport == "gemini_api":
            if previous_transport == "agy_three_part":
                continue
        elif previous_transport != declared_transport:
            continue
        descriptor_key = key_for_descriptor(descriptor)
        by_descriptor.setdefault(descriptor_key, []).append(
            (receipt_key, copy.deepcopy(dict(observed)))
        )
        if descriptor.get("observer_mode") == VOICE_REFERENCE_MODE:
            # These are the only fields added by automatic voice preparation.
            # Every declared source/frame/cue/host field remains in the key.
            # The complete old PCM and binding were already revalidated above.
            derived_fields = set(_AUDIO_FIELDS) | set(_VOICE_REFERENCE_BINDING_FIELDS) | {
                "observer_mode", "target_audio_window_ms", "target_audio_offset_ms",
            }
            seed = {key: value for key, value in descriptor.items() if key not in derived_fields}
            if _external_voice_reference_seed_status(seed) == "VALID":
                by_external_seed.setdefault(key_for_descriptor(seed), []).append(
                    (receipt_key, copy.deepcopy(dict(observed)))
                )

    reused: list[dict[str, object]] = []
    unmatched: list[object] = []
    consumed: set[tuple[str, str]] = set()
    for current in observations:
        if not isinstance(current, Mapping):
            unmatched.append(copy.deepcopy(current))
            continue
        descriptor_key = key_for_descriptor(current)
        candidates = by_descriptor.get(descriptor_key, [])
        if not candidates and _external_voice_reference_seed_status(current) == "VALID":
            candidates = by_external_seed.get(descriptor_key, [])
        selected: tuple[str, str] | None = None
        selected_row: dict[str, object] | None = None
        for receipt_key, observed in candidates:
            if receipt_key not in consumed:
                selected = receipt_key
                selected_row = observed
                break
        if selected is None or selected_row is None:
            unmatched.append(copy.deepcopy(dict(current)))
            continue
        consumed.add(selected)
        reused.append(selected_row)

    result["reused_observations"] = reused
    result["unmatched_observations"] = unmatched
    result["reused_count"] = len(reused)
    return result


def retain_current_acoustic_observations(
    *, candidate_id: str, source_media: Path, subtitle_path: Path,
    previous_audit: Mapping[str, object], current_audit: Mapping[str, object],
) -> dict[str, object]:
    """Continue a normal audit using only receipts still valid for current bytes.

    Do not import a role table or search unrelated run directories. The normal
    consumer rereads every prior request, frame, PCM and receipt before keeping
    its observation. New-pass counters remain counters for that pass alone.
    """
    roles = current_caption_audio_roles(
        candidate_id=candidate_id, source_media=source_media,
        subtitle_path=subtitle_path, acoustic_audit=previous_audit,
    )
    valid = {
        (receipt["path"], receipt["sha256"])
        for row in roles["rows"] for receipt in row["receipts"]
    }
    combined = copy.deepcopy(dict(current_audit))
    observations = combined.get("observations", [])
    if not isinstance(observations, list):
        observations = []

    def receipt_key(observation):
        if not isinstance(observation, Mapping):
            return None
        result = observation.get("observer_result")
        binding = result.get("receipt_binding") if isinstance(result, Mapping) else None
        if not isinstance(binding, Mapping):
            return None
        path, sha = binding.get("path"), binding.get("sha256")
        return (path, sha) if isinstance(path, str) and isinstance(sha, str) else None

    seen = {receipt_key(row) for row in observations}
    retained = 0
    previous_rows = previous_audit.get("observations", [])
    for row in previous_rows if isinstance(previous_rows, list) else []:
        key = receipt_key(row)
        if key in valid and key not in seen:
            observations.append(copy.deepcopy(row))
            seen.add(key)
            retained += 1
    combined["observations"] = observations
    combined["retained_current_observation_count"] = retained
    combined["retained_unavailable_observations"] = roles["unavailable_observations"]
    return combined


def caption_audio_roles_after_typed_text(
    *, candidate_id: str, source_media: Path, srt_text: str,
    acoustic_audit: Mapping[str, object], text_authority: Mapping[str, object],
) -> dict[str, object]:
    """Project hash-bound audio roles through a replayed typed-text suffix.

    Original requests and receipts remain bound to their original SRT. A
    validated text-only repair may change cue text without changing the
    acoustic role, but cue index/timing, source bytes or the continuation
    chain still withdraw it. This is context only: aligned
    caption/deletion evidence is never carried forward.
    """
    from src.autoslice.producer_text_pipeline import _continuation_replay_clock_text_chain

    current_sha = hashlib.sha256(srt_text.encode()).hexdigest()
    cues = parse_timed_srt(srt_text, label="typed audio role context")
    rows = [{"cue_index": int(c.index) if c.index.isdigit() else c.index,
             "interval_ms": [c.start_ms, c.end_ms], "role": "unknown",
             "action": "KEEP", "receipts": []} for c in cues]
    failures, votes = [], {}
    reference_srt_hashes: set[str] = set()
    chain_states: set[str] = set()
    for observed in acoustic_audit.get("observations", []):
        if not isinstance(observed, Mapping):
            continue
        descriptor = observed.get("validated_inputs")
        if not isinstance(descriptor, Mapping):
            continue
        observer_mode = descriptor.get("observer_mode")
        if observer_mode not in (None, VOICE_REFERENCE_MODE):
            continue
        voice_mode = observer_mode == VOICE_REFERENCE_MODE
        try:
            if voice_mode:
                binding_path, _ = _path_hash(
                    descriptor["voice_reference_binding_path"], descriptor["voice_reference_binding_sha256"],
                    unavailable="VOICE_REFERENCE_BINDING_UNAVAILABLE", mismatch="VOICE_REFERENCE_BINDING_HASH_MISMATCH",
                )
                binding = _json_mapping(binding_path, code="VOICE_REFERENCE_BINDING_INVALID")
                reference = binding["final_srt"]
            else:
                result = observed.get("observer_result")
                if not isinstance(result, Mapping):
                    raise ValueError("ACOUSTIC_OBSERVER_RESULT_INVALID")
                request_path = Path(str(result["request_path"]))
                request = _json_mapping(request_path, code="ACOUSTIC_REQUEST_INVALID")
                reference = request["final_srt"]
            if not isinstance(reference, Mapping):
                raise ValueError("FINAL_SRT_BINDING_INVALID")
            reference_path, reference_sha = _path_hash(
                reference["path"], reference["sha256"],
                unavailable="FINAL_SRT_UNAVAILABLE", mismatch="FINAL_SRT_HASH_MISMATCH",
            )
            before = reference_path.read_text(encoding="utf-8")
            reference_sha = "sha256:" + str(reference_sha).removeprefix("sha256:")
            reference_srt_hashes.add(reference_sha)
            if reference_sha.removeprefix("sha256:") != current_sha:
                _continuation_replay_clock_text_chain(
                    text_authority, reference_text=before,
                    reference_sha=reference_sha.removeprefix("sha256:"),
                    current_text=srt_text, current_sha=current_sha,
                )
                chain_states.add("CONTINUATION_REPLAY_VALIDATED")
            else:
                chain_states.add("BYTE_IDENTICAL")
            original = current_caption_audio_roles(
                candidate_id=candidate_id, source_media=source_media,
                subtitle_path=reference_path, acoustic_audit={"observations": [observed]},
            )
            failures.extend(original["unavailable_observations"])
            old_cues = parse_timed_srt(before, label="original audio role context")
            for old_cue, current_cue, role in zip(old_cues, cues, original["rows"]):
                if (old_cue.index, old_cue.start_ms, old_cue.end_ms) != (
                    current_cue.index, current_cue.start_ms, current_cue.end_ms
                ) or role["role"] == "unknown":
                    continue
                votes.setdefault(role["cue_index"], []).append(role)
        except Exception as exc:
            failures.append({"observation_id": descriptor.get("observation_id"),
                             "reason_code": str(exc) if re.fullmatch(r"[A-Z][A-Z0-9_]+", str(exc)) else type(exc).__name__})
    for row in rows:
        selected = votes.get(row["cue_index"], [])
        if selected and len({r["role"] for r in selected}) == 1:
            row["role"] = selected[0]["role"]
            row["receipts"] = [r for vote in selected for r in vote["receipts"]]
    return {"candidate_id": candidate_id, "source_media_sha256": _sha(source_media),
            "final_srt_sha256": "sha256:" + current_sha, "status": "EVIDENCE_ONLY",
            "mutation_authorized": False, "uniform_host_display_only": True,
            "rows": rows, "unavailable_observations": failures,
            "context_projection": "HASH_BOUND_AUDIO_ROLES_REPLAYED_NORMAL_TYPED_TEXT_ONLY",
            "typed_text_binding": {
                "reference_final_srt_sha256": sorted(reference_srt_hashes),
                "current_final_srt_sha256": "sha256:" + current_sha,
                "reference_receipts_immutable": True,
                "chain_validation": (
                    "CONTINUATION_REPLAY_VALIDATED"
                    if "CONTINUATION_REPLAY_VALIDATED" in chain_states
                    else "BYTE_IDENTICAL"
                    if "BYTE_IDENTICAL" in chain_states
                    else "UNAVAILABLE"
                ),
            }}


def caption_audio_roles_context(srt_text: str, roles: object, *,
                                scope_cue_indexes: tuple[int, ...] | None = None,
                                source_offset_ms: int = 0) -> str:
    """Render only normalized current roles, excluding model prose."""
    if (not isinstance(roles, Mapping) or roles.get("status") != "EVIDENCE_ONLY"
            or roles.get("mutation_authorized") is not False
            or roles.get("uniform_host_display_only") is not True):
        return ""
    if roles.get("final_srt_sha256") != "sha256:" + hashlib.sha256(srt_text.encode()).hexdigest():
        return ""
    cues = parse_timed_srt(srt_text, label="audio role context")
    expected = [(int(c.index) if c.index.isdigit() else c.index, [c.start_ms, c.end_ms]) for c in cues]
    raw_rows = roles.get("rows")
    if not isinstance(raw_rows, list) or len(raw_rows) != len(expected):
        return ""
    normalized = []
    for row, (index, interval) in zip(raw_rows, expected):
        if not isinstance(row, Mapping) or row.get("cue_index") != index or row.get("interval_ms") != interval:
            return ""
        if row.get("role") not in _ACOUSTIC_ROLES or row.get("action") != "KEEP":
            return ""
        normalized.append({"cue_index": index, "interval_ms": interval, "role": row["role"]})
    if scope_cue_indexes is not None:
        scope=set(scope_cue_indexes)
        if (not scope or any(type(index) is not int or not 1 <= index <= len(expected) for index in scope)
                or type(source_offset_ms) is not int):
            raise ValueError('CAPTION_AUDIO_ROLE_SCOPE_INVALID')
        # Validate the original full receipt above before projecting C(P).
        # The whole-SRT hash remains in that receipt; physical role rows are
        # the only semantic input to this local view.
        material={"source_media_sha256": roles.get("source_media_sha256"), "clock": "SOURCE",
            "rows": [{**row, "cue_index": ordinal,
                      "interval_ms": [value + source_offset_ms for value in row['interval_ms']]}
                     for ordinal,row in enumerate(normalized,1) if ordinal in scope]}
    else:
        material={"final_srt_sha256": roles["final_srt_sha256"],
                  "source_media_sha256": roles.get("source_media_sha256"), "rows": normalized}
    return ("当前字幕的声源证据（不是人物身份）：source_media=内嵌原片，host=主播，"
            "overlap=两者重叠、不可推断唯一说话人，unknown=缺证据、不可推断。"
            "这些角色只帮助理解语境，不能单独授权删字幕、换字或删音频；所有字幕仍走原有CPA裁决。\n"
            + json.dumps(material, ensure_ascii=False, sort_keys=True))


def build_runtime_nested_acoustic_observer(
    runtime_root: Path | None = None, *, source_cache_root: Path | None = None,
) -> Callable:
    """Lazily assemble the selected acoustic transport and canonical CPA."""
    def source_relevance_probe(**arguments):
        from src.autoslice.cpa_frame_witness import batch_jpeg_vision_probe
        from src.autoslice.llm_client import runtime_cpa_command_environment
        from src.autoslice.nested_source_frame_relevance import (
            observe_candidate_source_frame_relevance,
        )
        from src.autoslice.producer_final_review_transport import _resolved_runtime_root

        runtime = _resolved_runtime_root(runtime_root)
        model = (getattr(batch_jpeg_vision_probe, "__kwdefaults__", None) or {}).get("model")
        if runtime is None or not isinstance(model, str) or not model:
            return {"decision": "UNKNOWN", "authority": "EVIDENCE_ONLY",
                    "reason_code": "SOURCE_RELEVANCE_RUNTIME_UNBOUND"}

        def vision_probe(images, question):
            env = runtime_cpa_command_environment(runtime)
            return batch_jpeg_vision_probe(
                images, question, api_base=env.get("CPA_BASE_URL", ""),
                api_key=env.get("CPA_API_KEY", ""), model=model,
            )

        return observe_candidate_source_frame_relevance(
            **arguments, vision_probe=vision_probe, model=model,
        )

    def observe(**kwargs):
        spec = kwargs.get("spec")
        prepared = spec.get("nested_media_acoustic") if isinstance(spec, Mapping) else None
        if prepared is None:
            return {"schema_version": AUDIT_SCHEMA, "status": "NOT_REQUESTED",
                    "subtitle_mutations": 0, "display_mutation_authorized": False,
                    "publication_authorized": False}
        observer_transport = (
            prepared.get("observer_transport")
            if isinstance(prepared, Mapping)
            else None
        )
        cache_root = source_cache_root
        if cache_root is None and runtime_root is not None:
            cache_root = Path(runtime_root) / "cache" / "nested-sources"
        if observer_transport == "agy_three_part":
            try:
                from src.autoslice.agy_three_part_acoustic import (
                    ATTACHMENT_INSTRUCTIONS,
                    VOICE_REFERENCE_ATTACHMENT_INSTRUCTIONS,
                    build_three_part_callbacks,
                )
                from src.autoslice.nested_media_acoustic_runtime import (
                    CPA_PROMPT,
                    OBSERVER_PROMPT,
                    VOICE_REFERENCE_CPA_PROMPT,
                    VOICE_REFERENCE_OBSERVER_PROMPT,
                )
                from src.autoslice.producer_final_review_transport import build_final_review_llm_call

                cpa = build_final_review_llm_call(runtime_root=runtime_root)
            except Exception as exc:
                return {"schema_version": AUDIT_SCHEMA, "status": "EVIDENCE_ONLY",
                        "reason_code": "NESTED_ACOUSTIC_RUNTIME_CONFIG_UNAVAILABLE",
                        "error_type": type(exc).__name__, "label": "unknown",
                        "action": "KEEP", "subtitle_mutations": 0,
                        "display_mutation_authorized": False, "publication_authorized": False}
            if not isinstance(getattr(cpa, "cpa_cache_identity", None), Mapping):
                return {"schema_version": AUDIT_SCHEMA, "status": "EVIDENCE_ONLY",
                        "reason_code": "CANONICAL_CPA_RUNTIME_BINDING_REQUIRED",
                        "label": "unknown", "action": "KEEP", "subtitle_mutations": 0,
                        "display_mutation_authorized": False, "publication_authorized": False}

            def callbacks_factory(**factory_kwargs):
                descriptor = factory_kwargs.get("descriptor")
                voice_mode = (
                    isinstance(descriptor, Mapping)
                    and descriptor.get("observer_mode") == VOICE_REFERENCE_MODE
                )
                playback_locator = (
                    descriptor.get("playback_locator")
                    if voice_mode
                    and isinstance(descriptor, Mapping)
                    and isinstance(descriptor.get("playback_locator"), Mapping)
                    else None
                )
                prompt_kwargs = (
                    {"observer_prompt": VOICE_REFERENCE_OBSERVER_PROMPT,
                     "cpa_prompt": VOICE_REFERENCE_CPA_PROMPT,
                     "attachment_instructions": VOICE_REFERENCE_ATTACHMENT_INSTRUCTIONS}
                    if voice_mode else
                    {"observer_prompt": OBSERVER_PROMPT,
                     "cpa_prompt": CPA_PROMPT,
                     "attachment_instructions": ATTACHMENT_INSTRUCTIONS}
                )
                return build_three_part_callbacks(
                    cpa_call=cpa, playback_locator=playback_locator,
                    **prompt_kwargs, **factory_kwargs,
                )

            return observe_prepared_nested_acoustics(
                **kwargs, callbacks_factory=callbacks_factory, source_cache_root=cache_root,
                source_relevance_probe=source_relevance_probe,
            )
        if observer_transport not in (None, "gemini_api"):
            return {"schema_version": AUDIT_SCHEMA, "status": "EVIDENCE_ONLY",
                    "reason_code": "NESTED_ACOUSTIC_TRANSPORT_UNSUPPORTED",
                    "label": "unknown", "action": "KEEP", "subtitle_mutations": 0,
                    "display_mutation_authorized": False, "publication_authorized": False}
        try:
            from scripts.session_autoslice import load_gemini_credentials
            from src.autoslice import agy_gemini_client
            from src.autoslice.entity_audio_verifier import _entity_api_model
            from src.autoslice.nested_media_acoustic_runtime import (
                CPA_PROMPT,
                OBSERVER_PROMPT,
                VOICE_REFERENCE_CPA_PROMPT,
                VOICE_REFERENCE_OBSERVER_PROMPT,
                build_runtime_acoustic_callbacks,
            )
            from src.autoslice.producer_final_review_transport import build_final_review_llm_call

            credentials = load_gemini_credentials()
            free_keys = list(dict.fromkeys(credentials[name] for name in
                agy_gemini_client.FREE_KEY_ENV_NAMES if credentials.get(name)))
            cpa = build_final_review_llm_call(runtime_root=runtime_root)
            model = _entity_api_model()
        except Exception as exc:
            return {"schema_version": AUDIT_SCHEMA, "status": "EVIDENCE_ONLY",
                    "reason_code": "NESTED_ACOUSTIC_RUNTIME_CONFIG_UNAVAILABLE",
                    "error_type": type(exc).__name__, "label": "unknown",
                    "action": "KEEP", "subtitle_mutations": 0,
                    "display_mutation_authorized": False, "publication_authorized": False}
        if not free_keys:
            return {"schema_version": AUDIT_SCHEMA, "status": "EVIDENCE_ONLY",
                    "reason_code": "GEMINI_FREE_CREDENTIAL_MISSING", "label": "unknown",
                    "action": "KEEP", "subtitle_mutations": 0,
                    "display_mutation_authorized": False, "publication_authorized": False}
        if not isinstance(getattr(cpa, "cpa_cache_identity", None), Mapping):
            return {"schema_version": AUDIT_SCHEMA, "status": "EVIDENCE_ONLY",
                    "reason_code": "CANONICAL_CPA_RUNTIME_BINDING_REQUIRED",
                    "label": "unknown", "action": "KEEP", "subtitle_mutations": 0,
                    "display_mutation_authorized": False, "publication_authorized": False}
        from src.autoslice import gemini_backup_policy

        paid_key = credentials.get(gemini_backup_policy.PAID_KEY_ENV) or None
        exhausted_slots = set()
        paid_quota_exhausted = []
        attempts = []

        def audio_generate(**arguments):
            attempts.clear()
            if agy_gemini_client.cpa_gemini_client.enabled():
                completion = agy_gemini_client.generate_content(key='CPA_MANAGED', **arguments)
                attempts.append({'transport': 'cpa_gemini_native', 'key_tier': 'UNKNOWN',
                                 'status': 'RESPONDED'})
                return completion
            last_quota_error = None
            for slot, key in enumerate(free_keys, start=1):
                if slot in exhausted_slots:
                    continue
                try:
                    completion = agy_gemini_client.generate_content(key=key, **arguments)
                except Exception as exc:
                    status = getattr(exc, "code", None)
                    attempts.append({"free_key_ordinal": slot, "status": "FAILED",
                        "error_type": type(exc).__name__,
                        "category": agy_gemini_client.classify_gemini_failure(exc),
                        "http_status": status if isinstance(status, int) else None})
                    if status != 429:
                        raise
                    exhausted_slots.add(slot)
                    last_quota_error = exc
                    continue
                attempts.append({"free_key_ordinal": slot, "status": "RESPONDED"})
                return completion




            if paid_quota_exhausted:
                raise last_quota_error or RuntimeError("GEMINI_PAID_BACKUP_QUOTA_EXHAUSTED")
            item_key = hashlib.sha256(
                str(arguments.get("prompt") or "").encode("utf-8")
                + bytes(arguments.get("inline_data") or b"")).hexdigest()
            gate = agy_gemini_client.quota_fastpath_paid_gate(
                item_key, [True] * len(free_keys), key_configured=paid_key is not None)
            allowed, gate_reason = gate if gate is not None else (False, "FREE_CHAIN_NOT_QUOTA")
            paid_ordinal = len(free_keys) + 1
            if not allowed:
                attempts.append({"key_tier": gemini_backup_policy.PAID_KEY_TIER,
                                 "key_ordinal": paid_ordinal, "status": "SKIPPED",
                                 "gate_reason": gate_reason})
                raise last_quota_error or RuntimeError("GEMINI_FREE_CHAIN_EXHAUSTED")
            stamp = gemini_backup_policy.record_paid_use(
                item_key, purpose="nested_media_acoustic", admission=gate_reason)
            try:
                completion = agy_gemini_client.generate_content(key=paid_key, **arguments)
            except Exception as exc:
                status = getattr(exc, "code", None)
                attempts.append({"key_tier": gemini_backup_policy.PAID_KEY_TIER,
                    "key_ordinal": paid_ordinal, "status": "FAILED",
                    "error_type": type(exc).__name__,
                    "category": agy_gemini_client.classify_gemini_failure(exc),
                    "http_status": status if isinstance(status, int) else None,
                    "gate_reason": gate_reason, "paid_policy_stamp": dict(stamp)})
                if status == 429:
                    # The paid route is exhausted too: later windows keep
                    # unknown/KEEP without re-dispatching the same failure.
                    paid_quota_exhausted.append(True)
                raise
            attempts.append({"key_tier": gemini_backup_policy.PAID_KEY_TIER,
                             "key_ordinal": paid_ordinal, "status": "RESPONDED",
                             "gate_reason": gate_reason, "paid_policy_stamp": dict(stamp)})
            return completion

        def callbacks_factory(**factory_kwargs):
            descriptor = factory_kwargs.get("descriptor")
            voice_mode = (
                isinstance(descriptor, Mapping)
                and descriptor.get("observer_mode") == VOICE_REFERENCE_MODE
            )
            playback_locator = (
                descriptor.get("playback_locator")
                if voice_mode
                and isinstance(descriptor, Mapping)
                and isinstance(descriptor.get("playback_locator"), Mapping)
                else None
            )
            prompt_kwargs = (
                {"observer_prompt": VOICE_REFERENCE_OBSERVER_PROMPT,
                 "cpa_prompt": VOICE_REFERENCE_CPA_PROMPT}
                if voice_mode else
                {"observer_prompt": OBSERVER_PROMPT, "cpa_prompt": CPA_PROMPT}
            )
            return build_runtime_acoustic_callbacks(
                audio_generate=audio_generate, cpa_call=cpa,
                audio_attempts=lambda: copy.deepcopy(attempts),
                provider="gemini_api", model=model,
                transport="src.autoslice.agy_gemini_client.generate_content",
                transport_module="src.autoslice.agy_gemini_client",
                playback_locator=playback_locator,
                **prompt_kwargs,
            )
        return observe_prepared_nested_acoustics(
            **kwargs, callbacks_factory=callbacks_factory,
            source_cache_root=cache_root,
            source_relevance_probe=source_relevance_probe,
        )
    return observe
