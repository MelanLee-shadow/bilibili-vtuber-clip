"""Strict, speaker-only operator truth bound to one exact delivery grid.

This authority deliberately does not own subtitle wording or publication.  Cue
text and timing are present only to prevent a speaker decision from drifting to
different bytes or a different delivery.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Mapping, Sequence

from src.autoslice.speaker_common import HOST_SPEAKER, SpeakerFinalizationError


SCHEMA_VERSION = "operator-reviewed-speaker-truth.v1"
REVIEW_BINDING_SCHEMA_VERSION = "operator-reviewed-speaker-delivery-binding.v1"
DECISION_ALL_HOST = "ALL_DELIVERY_CUES_HOST"
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _required_text(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SpeakerFinalizationError(f"{label} must be non-empty")
    return text


def current_source_recording_binding(
    *,
    spec: Mapping[str, object],
    absolute_start_ms: int | None,
    absolute_end_ms: int | None,
) -> dict[str, object] | None:
    """Project the producer's already-attested one-piece source without I/O."""

    pieces = spec.get("pieces")
    if not isinstance(pieces, list) or len(pieces) != 1:
        return None
    piece = pieces[0]
    if not isinstance(piece, Mapping):
        return None
    raw_path = str(piece.get("remote_media") or "")
    basename = Path(raw_path).name
    source_sha256 = str(piece.get("source_media_sha256") or "").removeprefix("sha256:")
    piece_start_ms = piece.get("start_ms")
    piece_end_ms = piece.get("end_ms")
    values = (piece_start_ms, piece_end_ms, absolute_start_ms, absolute_end_ms)
    if (
        not raw_path
        or not basename
        or Path(basename).name != basename
        or SHA256_RE.fullmatch(source_sha256) is None
        or any(isinstance(value, bool) or not isinstance(value, int) for value in values)
        or not piece_start_ms <= absolute_start_ms < absolute_end_ms <= piece_end_ms
    ):
        return None
    return {
        "basename": basename,
        "sha256": source_sha256,
        "absolute_start_ms": absolute_start_ms,
        "absolute_end_ms": absolute_end_ms,
    }


def validate_operator_reviewed_speaker_truth(
    truth: Mapping[str, object],
    *,
    review_binding: Mapping[str, object],
    candidate_id: str,
    cues: Sequence[object],
    authority: str,
    expected_source_media_sha256: str,
    expected_text_final_srt_sha256: str,
    expected_automatic_srt_sha256: str,
    expected_source_recording: Mapping[str, object] | None,
) -> None:
    """Fail closed unless speaker-only truth matches every current cue exactly."""

    expected_truth_keys = {
        "schema_version",
        "candidate_id",
        "scope",
        "authority",
        "reviewed_at",
        "decision",
        "subtitle_text_authorized",
        "upload_authorized",
        "reviewed_delivery_video_sha256",
        "bindings",
        "cues",
    }
    if set(truth) != expected_truth_keys:
        raise SpeakerFinalizationError(
            "operator-reviewed speaker truth fields are incomplete or unsupported"
        )
    if truth.get("schema_version") != SCHEMA_VERSION:
        raise SpeakerFinalizationError("operator-reviewed speaker truth schema is invalid")
    if truth.get("candidate_id") != candidate_id:
        raise SpeakerFinalizationError("reviewed speaker truth candidate mismatch")
    if truth.get("scope") != "speaker_only":
        raise SpeakerFinalizationError("operator-reviewed speaker truth scope is invalid")
    if truth.get("authority") != authority:
        raise SpeakerFinalizationError("operator-reviewed speaker truth authority mismatch")
    _required_text(
        truth.get("reviewed_at"),
        label="operator-reviewed speaker truth reviewed_at",
    )
    if truth.get("decision") != DECISION_ALL_HOST:
        raise SpeakerFinalizationError("operator-reviewed speaker truth decision is invalid")
    if truth.get("subtitle_text_authorized") is not False:
        raise SpeakerFinalizationError(
            "operator-reviewed speaker truth must not authorize subtitle text"
        )
    if truth.get("upload_authorized") is not False:
        raise SpeakerFinalizationError("operator-reviewed speaker truth must not authorize upload")
    reviewed_delivery_sha = str(truth.get("reviewed_delivery_video_sha256") or "")
    if not SHA256_RE.fullmatch(reviewed_delivery_sha):
        raise SpeakerFinalizationError(
            "operator-reviewed speaker truth delivery video hash is invalid"
        )

    expected_review_binding_keys = {
        "schema_version",
        "candidate_id",
        "authority",
        "reviewed_at",
        "current_record_sha256",
        "reviewed_delivery_video",
        "source_media_sha256",
        "text_final_srt_sha256",
        "automatic_labelled_srt_sha256",
        "source_recording",
        "subtitle_text_authorized",
        "upload_authorized",
    }
    if set(review_binding) != expected_review_binding_keys:
        raise SpeakerFinalizationError(
            "operator-reviewed speaker delivery binding fields are invalid"
        )
    if review_binding.get("schema_version") != REVIEW_BINDING_SCHEMA_VERSION:
        raise SpeakerFinalizationError(
            "operator-reviewed speaker delivery binding schema is invalid"
        )
    if review_binding.get("candidate_id") != candidate_id:
        raise SpeakerFinalizationError(
            "operator-reviewed speaker delivery binding candidate mismatch"
        )
    if review_binding.get("authority") != authority:
        raise SpeakerFinalizationError(
            "operator-reviewed speaker delivery binding authority mismatch"
        )
    if review_binding.get("reviewed_at") != truth.get("reviewed_at"):
        raise SpeakerFinalizationError(
            "operator-reviewed speaker delivery binding review time drift"
        )
    if not SHA256_RE.fullmatch(str(review_binding.get("current_record_sha256") or "")):
        raise SpeakerFinalizationError(
            "operator-reviewed speaker delivery binding current record hash is invalid"
        )
    if review_binding.get("subtitle_text_authorized") is not False:
        raise SpeakerFinalizationError(
            "operator-reviewed speaker delivery binding must not authorize subtitle text"
        )
    if review_binding.get("upload_authorized") is not False:
        raise SpeakerFinalizationError(
            "operator-reviewed speaker delivery binding must not authorize upload"
        )
    reviewed_delivery = review_binding.get("reviewed_delivery_video")
    if not isinstance(reviewed_delivery, Mapping) or set(reviewed_delivery) != {
        "basename",
        "sha256",
    }:
        raise SpeakerFinalizationError(
            "operator-reviewed speaker delivery video binding is invalid"
        )
    reviewed_basename = _required_text(
        reviewed_delivery.get("basename"),
        label="operator-reviewed speaker delivery video basename",
    )
    if Path(reviewed_basename).name != reviewed_basename:
        raise SpeakerFinalizationError(
            "operator-reviewed speaker delivery video basename is invalid"
        )
    if reviewed_delivery.get("sha256") != reviewed_delivery_sha:
        raise SpeakerFinalizationError(
            "operator-reviewed speaker truth reviewed delivery hash drift"
        )

    bindings = truth.get("bindings")
    if not isinstance(bindings, Mapping) or set(bindings) != {
        "source_media_sha256",
        "text_final_srt_sha256",
        "automatic_labelled_srt_sha256",
        "cue_count",
        "source_recording",
    }:
        raise SpeakerFinalizationError("operator-reviewed speaker truth bindings are invalid")
    expected_bindings = {
        "source_media_sha256": expected_source_media_sha256,
        "text_final_srt_sha256": expected_text_final_srt_sha256,
        "automatic_labelled_srt_sha256": expected_automatic_srt_sha256,
    }
    for field, expected in expected_bindings.items():
        if (
            not SHA256_RE.fullmatch(expected)
            or bindings.get(field) != expected
            or review_binding.get(field) != expected
        ):
            raise SpeakerFinalizationError(f"operator-reviewed speaker truth {field} drift")
    cue_count = bindings.get("cue_count")
    if isinstance(cue_count, bool) or cue_count != len(cues) or cue_count <= 0:
        raise SpeakerFinalizationError("operator-reviewed speaker truth cue count drift")

    source_recording = bindings.get("source_recording")
    source_keys = {
        "basename",
        "sha256",
        "absolute_start_ms",
        "absolute_end_ms",
    }
    if not isinstance(source_recording, Mapping) or set(source_recording) != source_keys:
        raise SpeakerFinalizationError(
            "operator-reviewed speaker truth source recording binding is invalid"
        )
    basename = _required_text(
        source_recording.get("basename"),
        label="operator-reviewed speaker truth source recording basename",
    )
    if Path(basename).name != basename:
        raise SpeakerFinalizationError(
            "operator-reviewed speaker truth source recording basename is invalid"
        )
    if not SHA256_RE.fullmatch(str(source_recording.get("sha256") or "")):
        raise SpeakerFinalizationError(
            "operator-reviewed speaker truth source recording hash is invalid"
        )
    absolute_start_ms = source_recording.get("absolute_start_ms")
    absolute_end_ms = source_recording.get("absolute_end_ms")
    if (
        isinstance(absolute_start_ms, bool)
        or not isinstance(absolute_start_ms, int)
        or isinstance(absolute_end_ms, bool)
        or not isinstance(absolute_end_ms, int)
        or absolute_start_ms < 0
        or absolute_end_ms <= absolute_start_ms
    ):
        raise SpeakerFinalizationError("operator-reviewed speaker truth source interval is invalid")
    if expected_source_recording is None:
        raise SpeakerFinalizationError(
            "operator-reviewed speaker truth current source recording binding is unavailable"
        )
    if set(expected_source_recording) != source_keys:
        raise SpeakerFinalizationError(
            "operator-reviewed speaker truth current source recording binding is invalid"
        )
    expected_basename = str(expected_source_recording.get("basename") or "")
    expected_source_sha = str(expected_source_recording.get("sha256") or "")
    expected_start_ms = expected_source_recording.get("absolute_start_ms")
    expected_end_ms = expected_source_recording.get("absolute_end_ms")
    if (
        Path(expected_basename).name != expected_basename
        or not expected_basename
        or not SHA256_RE.fullmatch(expected_source_sha)
        or isinstance(expected_start_ms, bool)
        or not isinstance(expected_start_ms, int)
        or isinstance(expected_end_ms, bool)
        or not isinstance(expected_end_ms, int)
        or expected_start_ms < 0
        or expected_end_ms <= expected_start_ms
    ):
        raise SpeakerFinalizationError(
            "operator-reviewed speaker truth current source recording binding is invalid"
        )
    normalized_expected_source = {
        "basename": expected_basename,
        "sha256": expected_source_sha,
        "absolute_start_ms": expected_start_ms,
        "absolute_end_ms": expected_end_ms,
    }
    if dict(source_recording) != normalized_expected_source:
        raise SpeakerFinalizationError(
            "operator-reviewed speaker truth source recording binding drift"
        )
    review_source_recording = review_binding.get("source_recording")
    if (
        not isinstance(review_source_recording, Mapping)
        or set(review_source_recording) != source_keys
        or dict(review_source_recording) != normalized_expected_source
    ):
        raise SpeakerFinalizationError(
            "operator-reviewed speaker delivery source recording binding drift"
        )

    truth_cues = truth.get("cues")
    if not isinstance(truth_cues, list) or len(truth_cues) != len(cues):
        raise SpeakerFinalizationError("operator-reviewed speaker truth cue grid drift")
    for position, (truth_cue, current_cue) in enumerate(
        zip(truth_cues, cues, strict=True), start=1
    ):
        if not isinstance(truth_cue, Mapping) or set(truth_cue) != {
            "cue",
            "start",
            "end",
            "text",
            "speaker",
        }:
            raise SpeakerFinalizationError(
                f"operator-reviewed speaker truth cue {position} is invalid"
            )
        if truth_cue.get("cue") != position:
            raise SpeakerFinalizationError(
                "operator-reviewed speaker truth cue indices are not contiguous"
            )
        if truth_cue.get("speaker") != HOST_SPEAKER:
            raise SpeakerFinalizationError(
                f"operator-reviewed speaker truth cue {position} is not {HOST_SPEAKER}"
            )
        for field in ("start", "end", "text"):
            if truth_cue.get(field) != getattr(current_cue, field, None):
                raise SpeakerFinalizationError(
                    f"operator-reviewed speaker truth cue {position} {field} drift"
                )
