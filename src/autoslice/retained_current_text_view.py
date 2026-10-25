"""Deterministic current-text projection for an authorized filler splice.

This module only projects an already verified talk-filler plan onto the
existing current SRT.  It does not decide whether a removal is semantic,
write an authority receipt, or claim that the projected text passed review.
The source SRT, parent authority/context, plan, and source-media provenance
are all hash-bound before any cue is projected.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from src.autoslice.jingting_chunker import _format_srt_time, parse_srt_cues


_SHA256_RX = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")
_SCHEMA = "retained-current-text-view.v1"


class RetainedCurrentTextViewError(ValueError):
    """Raised when a hash-bound deterministic projection cannot be built."""


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _normalise_sha(value: object, *, label: str) -> str:
    match = _SHA256_RX.fullmatch(str(value or "").strip())
    if not match:
        raise RetainedCurrentTextViewError(f"{label}_HASH_INVALID")
    return "sha256:" + match.group(1).lower()


def _strict_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RetainedCurrentTextViewError(f"{label}_INTEGER_REQUIRED")
    return value


def _required_int(row: Mapping[str, object], key: str, *, label: str) -> int:
    try:
        value = row[key]
    except KeyError as exc:
        raise RetainedCurrentTextViewError(f"{label}_{key.upper()}_INVALID") from exc
    return _strict_int(value, label=f"{label}_{key.upper()}")


def _require_bytes(value: bytes, *, label: str) -> bytes:
    if not isinstance(value, bytes) or not value:
        raise RetainedCurrentTextViewError(f"{label}_BYTES_INVALID")
    return value


def _source_media_hash(row: Mapping[str, object], *, label: str) -> str:
    """Validate the exact producer_source_media provenance shape.

    ``prepare_source_media`` stores the source digest without a prefix in
    ``source_sha256`` and the same digest with a ``sha256:`` prefix in
    ``source_media_binding``.  Treating either field as an arbitrary nested
    object would make the binding ambiguous, so both fields are required.
    """

    source_sha = row.get("source_sha256")
    binding = row.get("source_media_binding")
    if not isinstance(source_sha, str) or re.fullmatch(r"[0-9a-fA-F]{64}", source_sha) is None:
        raise RetainedCurrentTextViewError(f"{label}_SOURCE_SHA256_INVALID")
    if not isinstance(binding, str):
        raise RetainedCurrentTextViewError(f"{label}_SOURCE_MEDIA_BINDING_INVALID")
    expected = "sha256:" + source_sha.lower()
    if binding != expected:
        raise RetainedCurrentTextViewError(f"{label}_SOURCE_MEDIA_BINDING_MISMATCH")
    return expected


def _is_zero_deletion_identity_shape(plan: Mapping[str, object]) -> bool:
    """Return whether *plan* has the only shape allowed for identity replay.

    This is intentionally a structural predicate rather than an acceptance
    result.  The duration fields and verifier result are checked by the normal
    plan normaliser; keeping the shape check separate lets the retained-draft
    loader avoid requiring verifier request/response files for a legitimate
    no-op plan, while malformed identity candidates still fail closed in the
    normal projection path.
    """

    if not isinstance(plan, Mapping):
        return False
    source_start = plan.get("source_start_ms")
    source_end = plan.get("source_end_ms")
    if (
        isinstance(source_start, bool)
        or not isinstance(source_start, int)
        or isinstance(source_end, bool)
        or not isinstance(source_end, int)
        or source_start < 0
        or source_end <= source_start
    ):
        return False
    if plan.get("removals") != []:
        return False
    retained = plan.get("retained_intervals")
    if not isinstance(retained, list) or len(retained) != 1:
        return False
    interval = retained[0]
    if not isinstance(interval, Mapping):
        return False
    start = interval.get("start_ms")
    end = interval.get("end_ms")
    return (
        not isinstance(start, bool)
        and isinstance(start, int)
        and not isinstance(end, bool)
        and isinstance(end, int)
        and start == source_start
        and end == source_end
    )


def is_zero_deletion_identity_plan(plan: Mapping[str, object]) -> bool:
    """Return whether a plan is a complete, strict no-deletion identity plan.

    This predicate does not replace :func:`_normalise_plan`: source-SRT and
    immutable byte bindings are still validated there.  It only describes the
    identity contract needed by callers that decide whether verifier
    request/response artifacts are applicable.
    """

    if not _is_zero_deletion_identity_shape(plan):
        return False
    source_start = plan["source_start_ms"]
    source_end = plan["source_end_ms"]
    effective = plan.get("effective_duration_ms")
    removed = plan.get("removed_duration_ms")
    verification = plan.get("global_verification")
    return (
        isinstance(effective, int)
        and not isinstance(effective, bool)
        and effective == source_end - source_start
        and isinstance(removed, int)
        and not isinstance(removed, bool)
        and removed == 0
        and isinstance(verification, Mapping)
        and verification.get("status") == "NOT_REQUIRED"
        and verification.get("reason_code") == "NO_AUTOMATIC_REMOVAL"
    )


def _normalise_plan(
    plan: Mapping[str, object],
    *,
    source_srt_hash: str,
) -> tuple[int, int, list[dict[str, int]], list[dict[str, int]]]:
    if not isinstance(plan, Mapping):
        raise RetainedCurrentTextViewError("FILLER_PLAN_INVALID")

    plan_hash = _normalise_sha(plan.get("source_srt_sha256"), label="PLAN_SOURCE_SRT")
    if plan_hash != source_srt_hash:
        raise RetainedCurrentTextViewError("SOURCE_SRT_PLAN_HASH_MISMATCH")

    try:
        source_start = plan["source_start_ms"]
        source_end = plan["source_end_ms"]
    except KeyError as exc:
        raise RetainedCurrentTextViewError("FILLER_PLAN_SOURCE_RANGE_INVALID") from exc
    source_start = _strict_int(source_start, label="FILLER_PLAN_SOURCE_START_MS")
    source_end = _strict_int(source_end, label="FILLER_PLAN_SOURCE_END_MS")
    if source_start < 0 or source_end <= source_start:
        raise RetainedCurrentTextViewError("FILLER_PLAN_SOURCE_RANGE_INVALID")

    raw_retained = plan.get("retained_intervals")
    raw_removals = plan.get("removals")
    if not isinstance(raw_retained, list) or not raw_retained:
        raise RetainedCurrentTextViewError("FILLER_PLAN_RETAINED_INTERVALS_INVALID")
    if not isinstance(raw_removals, list):
        raise RetainedCurrentTextViewError("FILLER_PLAN_REMOVALS_INVALID")

    retained: list[dict[str, int]] = []
    cursor = source_start
    for raw in raw_retained:
        if not isinstance(raw, Mapping):
            raise RetainedCurrentTextViewError("FILLER_PLAN_RETAINED_INTERVAL_INVALID")
        start = _required_int(raw, "start_ms", label="RETAINED_INTERVAL")
        end = _required_int(raw, "end_ms", label="RETAINED_INTERVAL")
        if (
            start < cursor
            or end <= start
            or end > source_end
        ):
            raise RetainedCurrentTextViewError("FILLER_PLAN_RETAINED_INTERVALS_NOT_COVERING_SOURCE")
        retained.append({"start_ms": start, "end_ms": end})
        cursor = end
    removals: list[dict[str, int]] = []
    cursor = source_start
    for interval in retained:
        if interval["start_ms"] > cursor:
            removals.append({"start_ms": cursor, "end_ms": interval["start_ms"]})
        cursor = interval["end_ms"]
    if cursor < source_end:
        removals.append({"start_ms": cursor, "end_ms": source_end})

    supplied_removals: list[dict[str, int]] = []
    for raw in raw_removals:
        if not isinstance(raw, Mapping):
            raise RetainedCurrentTextViewError("FILLER_PLAN_REMOVAL_INVALID")
        start = _required_int(raw, "source_start_ms", label="REMOVAL")
        end = _required_int(raw, "source_end_ms", label="REMOVAL")
        if end <= start:
            raise RetainedCurrentTextViewError("FILLER_PLAN_REMOVAL_INVALID")
        supplied_removals.append({"start_ms": start, "end_ms": end})
    if supplied_removals != removals:
        raise RetainedCurrentTextViewError("FILLER_PLAN_REMOVALS_DO_NOT_MATCH_RETAINED_INTERVALS")

    expected_effective = sum(row["end_ms"] - row["start_ms"] for row in retained)
    expected_removed = sum(row["end_ms"] - row["start_ms"] for row in removals)
    identity_shape = _is_zero_deletion_identity_shape(plan)
    if identity_shape:
        # A no-op identity must carry explicit zero-deletion durations.  An
        # omitted field must not silently turn a malformed plan into a
        # verifier-free identity.
        for key, expected in (
            ("effective_duration_ms", expected_effective),
            ("removed_duration_ms", expected_removed),
        ):
            if key not in plan:
                raise RetainedCurrentTextViewError(
                    f"FILLER_PLAN_{key.upper()}_REQUIRED"
                )
            actual = _strict_int(plan[key], label=f"FILLER_PLAN_{key.upper()}")
            if actual != expected:
                raise RetainedCurrentTextViewError(f"FILLER_PLAN_{key.upper()}_MISMATCH")
        verification = plan.get("global_verification")
        if (
            not isinstance(verification, Mapping)
            or verification.get("status") != "NOT_REQUIRED"
            or verification.get("reason_code") != "NO_AUTOMATIC_REMOVAL"
        ):
            raise RetainedCurrentTextViewError("FILLER_PLAN_IDENTITY_VERIFICATION_INVALID")
    else:
        verification = plan.get("global_verification")
        if not isinstance(verification, Mapping) or verification.get("status") != "PASS":
            raise RetainedCurrentTextViewError("FILLER_PLAN_GLOBAL_VERIFICATION_NOT_PASS")
        for key, expected in (
            ("effective_duration_ms", expected_effective),
            ("removed_duration_ms", expected_removed),
        ):
            if key in plan:
                actual = _strict_int(plan[key], label=f"FILLER_PLAN_{key.upper()}")
                if actual != expected:
                    raise RetainedCurrentTextViewError(f"FILLER_PLAN_{key.upper()}_MISMATCH")
    return source_start, source_end, retained, removals


def _normalise_provenance(
    piece_provenance: Sequence[Mapping[str, object]],
    piece_durations_ms: Sequence[int],
    retained: Sequence[Mapping[str, int]],
    *,
    source_binding: Mapping[str, object],
    source_timeline_offset_ms: int,
) -> list[dict[str, Any]]:
    if not isinstance(piece_provenance, Sequence) or isinstance(piece_provenance, (str, bytes)):
        raise RetainedCurrentTextViewError("PIECE_PROVENANCE_INVALID")
    if len(piece_provenance) != len(retained) or len(piece_durations_ms) != len(retained):
        raise RetainedCurrentTextViewError("PIECE_PROVENANCE_INTERVAL_COUNT_MISMATCH")
    if not isinstance(source_binding, Mapping):
        raise RetainedCurrentTextViewError("SOURCE_BINDING_INVALID")
    bound_media_hash = _source_media_hash(source_binding, label="SOURCE_BINDING")

    normalised: list[dict[str, Any]] = []
    previous_end: int | None = None
    concat_start = 0
    for index, (raw, interval, raw_duration) in enumerate(
        zip(piece_provenance, retained, piece_durations_ms, strict=True)
    ):
        if not isinstance(raw, Mapping):
            raise RetainedCurrentTextViewError("PIECE_PROVENANCE_ROW_INVALID")
        start = _required_int(raw, "start_ms", label=f"PIECE_{index}")
        end = _required_int(raw, "end_ms", label=f"PIECE_{index}")
        if start < 0 or end <= start:
            raise RetainedCurrentTextViewError("PIECE_PROVENANCE_RANGE_INVALID")
        if previous_end is not None and start < previous_end:
            raise RetainedCurrentTextViewError("PIECE_PROVENANCE_SOURCE_RANGES_OVERLAP")
        physical_interval_start = interval["start_ms"] + source_timeline_offset_ms
        physical_interval_end = interval["end_ms"] + source_timeline_offset_ms
        if index != 0 and start != physical_interval_start:
            raise RetainedCurrentTextViewError("PIECE_PROVENANCE_INNER_PADDING")
        if index != len(retained) - 1 and end != physical_interval_end:
            raise RetainedCurrentTextViewError("PIECE_PROVENANCE_INNER_PADDING")
        if index == 0 and start > physical_interval_start:
            raise RetainedCurrentTextViewError("PIECE_PROVENANCE_DOES_NOT_COVER_RETAINED_INTERVAL")
        if index == len(retained) - 1 and end < physical_interval_end:
            raise RetainedCurrentTextViewError("PIECE_PROVENANCE_DOES_NOT_COVER_RETAINED_INTERVAL")
        duration = _strict_int(raw_duration, label="PIECE_DURATION")
        if duration <= 0:
            raise RetainedCurrentTextViewError("PIECE_DURATION_INVALID")
        source_hash = _source_media_hash(raw, label=f"PIECE_{index}")
        if source_hash != bound_media_hash:
            raise RetainedCurrentTextViewError("PIECE_SOURCE_BINDING_MISMATCH")
        if not raw.get("output_sha256"):
            raise RetainedCurrentTextViewError(f"PIECE_{index}_OUTPUT_SHA256_MISSING")
        output_hash = _normalise_sha(raw["output_sha256"], label=f"PIECE_{index}_OUTPUT")
        normalised.append(
            {
                "piece_index": index,
                "source_start_ms": start,
                "source_end_ms": end,
                "plan_start_ms": interval["start_ms"],
                "plan_end_ms": interval["end_ms"],
                "actual_duration_ms": duration,
                "concat_start_ms": concat_start,
                "concat_end_ms": concat_start + duration,
                "source_media_sha256": source_hash,
                "output_sha256": output_hash,
            }
        )
        concat_start += duration
        previous_end = end
    return normalised


def _verify_frozen_json(
    value: bytes,
    expected_sha256: str,
    *,
    label: str,
) -> tuple[str, object]:
    value = _require_bytes(value, label=label)
    digest = _sha256_bytes(value)
    expected = _normalise_sha(expected_sha256, label=label)
    if digest != expected:
        raise RetainedCurrentTextViewError(f"{label}_HASH_MISMATCH")
    try:
        parsed = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetainedCurrentTextViewError(f"{label}_JSON_INVALID") from exc
    if not isinstance(parsed, (Mapping, list)):
        raise RetainedCurrentTextViewError(f"{label}_JSON_INVALID")
    return digest, parsed


def build_retained_current_text_view(
    *,
    source_srt_bytes: bytes,
    authority_bytes: bytes,
    clip_context_bytes: bytes,
    plan: Mapping[str, object],
    plan_bytes: bytes,
    plan_sha256: str,
    piece_provenance: Sequence[Mapping[str, object]],
    provenance_bytes: bytes,
    provenance_sha256: str,
    piece_durations_ms: Sequence[int],
    source_binding: Mapping[str, object],
    authority_sha256: str,
    clip_context_sha256: str,
    source_timeline_offset_ms: int,
) -> dict[str, object]:
    """Project a verified retained-piece plan onto the current SRT.

    The returned object is a projection witness only.  It has no review,
    release, or authority status and does not write any input artifact.  The
    plan's global ``PASS`` is required as an input binding for an actual
    deletion; the only exception is a strict zero-deletion identity plan whose
    verifier result is ``NOT_REQUIRED/NO_AUTOMATIC_REMOVAL``.  This function
    does not replay the verifier or endorse either result.  Likewise, the
    parent authority/context bytes are hash checked only; parent semantic
    acceptance and the next normal review remain downstream responsibilities.
    ``piece_durations_ms`` are the measured output durations, so each later
    piece starts at the accumulated measured duration rather than at a
    source-time subtraction.
    """

    source_srt_bytes = _require_bytes(source_srt_bytes, label="SOURCE_SRT")
    authority_bytes = _require_bytes(authority_bytes, label="PARENT_AUTHORITY")
    clip_context_bytes = _require_bytes(clip_context_bytes, label="PARENT_CLIP_CONTEXT")
    source_timeline_offset_ms = _strict_int(
        source_timeline_offset_ms,
        label="SOURCE_TIMELINE_OFFSET_MS",
    )
    if source_timeline_offset_ms < 0:
        raise RetainedCurrentTextViewError("SOURCE_TIMELINE_OFFSET_MS_INVALID")
    source_srt_hash = _sha256_bytes(source_srt_bytes)
    authority_hash = _normalise_sha(authority_sha256, label="PARENT_AUTHORITY")
    clip_context_hash = _normalise_sha(clip_context_sha256, label="PARENT_CLIP_CONTEXT")
    if authority_hash != _sha256_bytes(authority_bytes):
        raise RetainedCurrentTextViewError("PARENT_AUTHORITY_HASH_MISMATCH")
    if clip_context_hash != _sha256_bytes(clip_context_bytes):
        raise RetainedCurrentTextViewError("PARENT_CLIP_CONTEXT_HASH_MISMATCH")
    frozen_plan_hash, frozen_plan = _verify_frozen_json(
        plan_bytes,
        plan_sha256,
        label="FILLER_PLAN",
    )
    if not isinstance(frozen_plan, Mapping) or dict(frozen_plan) != dict(plan):
        raise RetainedCurrentTextViewError("FILLER_PLAN_BYTES_MAPPING_MISMATCH")
    frozen_provenance_hash, frozen_provenance = _verify_frozen_json(
        provenance_bytes,
        provenance_sha256,
        label="PIECE_PROVENANCE",
    )
    frozen_rows = frozen_provenance
    if isinstance(frozen_provenance, Mapping) and set(frozen_provenance) == {"piece_provenance"}:
        frozen_rows = frozen_provenance["piece_provenance"]
    if not isinstance(frozen_rows, list) or frozen_rows != list(piece_provenance):
        raise RetainedCurrentTextViewError("PIECE_PROVENANCE_BYTES_MAPPING_MISMATCH")

    try:
        source_srt_text = source_srt_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RetainedCurrentTextViewError("SOURCE_SRT_UTF8_INVALID") from exc
    cues = parse_srt_cues(source_srt_text)
    if not cues:
        raise RetainedCurrentTextViewError("SOURCE_SRT_HAS_NO_CUES")

    source_start, source_end, retained, removals = _normalise_plan(
        frozen_plan,
        source_srt_hash=source_srt_hash,
    )
    identity_plan = is_zero_deletion_identity_plan(frozen_plan)
    if identity_plan:
        # A zero-deletion identity is a complete current body, so silently
        # dropping a cue outside the declared source interval would make the
        # identity claim lossy.  Ordinary deletion plans retain their existing
        # OUTSIDE_PLAN handling below because those plans intentionally scope a
        # current SRT to a selected source interval.
        for cue in cues:
            if int(cue.start_ms) < source_start or int(cue.end_ms) > source_end:
                raise RetainedCurrentTextViewError(
                    "IDENTITY_CURRENT_CUE_OUTSIDE_SOURCE_RANGE"
                )
    pieces = _normalise_provenance(
        piece_provenance,
        piece_durations_ms,
        retained,
        source_binding=source_binding,
        source_timeline_offset_ms=source_timeline_offset_ms,
    )
    bound_srt_hash = _normalise_sha(
        source_binding.get("source_srt_sha256"),
        label="SOURCE_BINDING_SOURCE_SRT",
    )
    if bound_srt_hash != source_srt_hash:
        raise RetainedCurrentTextViewError("SOURCE_BINDING_SRT_HASH_MISMATCH")

    output_blocks: list[str] = []
    cue_map: list[dict[str, object]] = []
    dropped: list[dict[str, str]] = []
    seen_cue_indexes: set[str] = set()
    for cue in cues:
        if str(cue.index) in seen_cue_indexes:
            raise RetainedCurrentTextViewError("SOURCE_SRT_DUPLICATE_CUE_INDEX")
        seen_cue_indexes.add(str(cue.index))
        cue_start, cue_end = int(cue.start_ms), int(cue.end_ms)
        if cue_end <= source_start or cue_start >= source_end:
            dropped.append({"old_cue_index": str(cue.index), "reason": "OUTSIDE_PLAN"})
            continue
        if cue_start < source_start or cue_end > source_end:
            raise RetainedCurrentTextViewError("CUE_CROSSES_PLAN_BOUNDARY")
        piece_index: int | None = None
        for index, interval in enumerate(retained):
            if interval["start_ms"] <= cue_start and cue_end <= interval["end_ms"]:
                piece_index = index
                break
        if piece_index is None:
            containing_gap = next(
                (
                    gap
                    for gap in removals
                    if gap["start_ms"] <= cue_start and cue_end <= gap["end_ms"]
                ),
                None,
            )
            if containing_gap is not None:
                dropped.append(
                    {"old_cue_index": str(cue.index), "reason": "INSIDE_REMOVED_INTERVAL"}
                )
                continue
            if any(cue_start < gap["end_ms"] and cue_end > gap["start_ms"] for gap in removals):
                raise RetainedCurrentTextViewError("CUE_CROSSES_REMOVED_INTERVAL")
            raise RetainedCurrentTextViewError("CUE_NOT_CONTAINED_BY_RETAINED_INTERVAL")

        piece = pieces[piece_index]
        physical_cue_start = cue_start + source_timeline_offset_ms
        physical_cue_end = cue_end + source_timeline_offset_ms
        new_start = piece["concat_start_ms"] + physical_cue_start - piece["source_start_ms"]
        new_end = piece["concat_start_ms"] + physical_cue_end - piece["source_start_ms"]
        if new_start < piece["concat_start_ms"] or new_end > piece["concat_end_ms"]:
            raise RetainedCurrentTextViewError("CUE_EXCEEDS_MEASURED_PIECE_DURATION")
        old_cue_index = str(cue.index)
        new_cue_index = str(len(cue_map) + 1)
        text = str(cue.text)
        output_blocks.append(
            f"{new_cue_index}\n{_format_srt_time(new_start)} --> {_format_srt_time(new_end)}\n{text}"
        )
        cue_map.append(
            {
                "old_cue_index": old_cue_index,
                "new_cue_index": new_cue_index,
                "piece_index": piece_index,
                "old_start_ms": cue_start,
                "old_end_ms": cue_end,
                "physical_old_start_ms": cue_start + source_timeline_offset_ms,
                "physical_old_end_ms": cue_end + source_timeline_offset_ms,
                "new_start_ms": new_start,
                "new_end_ms": new_end,
                "text_sha256": _sha256_text(text),
                "source_media_sha256": piece["source_media_sha256"],
                "source_piece_output_sha256": piece["output_sha256"],
            }
        )

    srt_text = "\n\n".join(output_blocks) + ("\n" if output_blocks else "")
    return {
        "schema_version": _SCHEMA,
        "status": "PROJECTED",
        "source_srt_sha256": source_srt_hash,
        "plan_sha256": frozen_plan_hash,
        "provenance_sha256": frozen_provenance_hash,
        "projected_srt_sha256": _sha256_text(srt_text),
        "parent_authority_sha256": authority_hash,
        "parent_clip_context_sha256": clip_context_hash,
        "source_media_sha256": pieces[0]["source_media_sha256"] if pieces else None,
        "source_range_ms": {"start_ms": source_start, "end_ms": source_end},
        "physical_source_range_ms": {
            "start_ms": source_start + source_timeline_offset_ms,
            "end_ms": source_end + source_timeline_offset_ms,
        },
        "source_timeline_offset_ms": source_timeline_offset_ms,
        "piece_clock": pieces,
        "cue_map": cue_map,
        "dropped_cues": dropped,
        "srt_text": srt_text,
    }


__all__ = [
    "RetainedCurrentTextViewError",
    "build_retained_current_text_view",
    "is_zero_deletion_identity_plan",
]
