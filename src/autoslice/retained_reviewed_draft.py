"""Hash-bound ingress for a normally verified retained-text draft.

This module is deliberately a small adapter around the existing producer
contracts.  It validates an existing current SRT and parent evidence, replays
one already-recorded filler verifier response without calling a provider, and
then delegates the clock/text projection to
:mod:`retained_current_text_view`.  Its output is a projection witness only;
it is never a new authority, review result, or release decision.  When the
ordinary parent validator returns a source-boundary context, the loader also
projects its immutable padding onto the measured concat clock and leaves the
latest reviewed body text as the only body spelling.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src.autoslice.boundary_semantic_review import cue_grid_sha256
from src.autoslice.clip_context import validate_clip_context
from src.autoslice.jingting_chunker import SrtCue, parse_srt_cues
from src.autoslice.retained_current_text_view import (
    RetainedCurrentTextViewError,
    _is_zero_deletion_identity_shape,
    build_retained_current_text_view,
    is_zero_deletion_identity_plan,
)
from src.autoslice.talk_filler import verify_automatic_filler_plan


_SCHEMA = "retained-reviewed-draft.v1"
_SOURCE_BOUNDARY_PROJECTION_SCHEMA = "retained-source-boundary-context-projection.v1"
_SHA256_RE = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")


class RetainedReviewedDraftError(ValueError):
    """Raised when the retained draft cannot pass normal input validation."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_prefixed(value: bytes) -> str:
    return "sha256:" + _sha256_bytes(value)


def _normalise_sha(value: object, *, label: str) -> str:
    match = _SHA256_RE.fullmatch(str(value or "").strip())
    if match is None:
        raise RetainedReviewedDraftError(f"{label}_HASH_INVALID")
    return match.group(1).lower()


def _strict_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RetainedReviewedDraftError(f"{label}_INTEGER_REQUIRED")
    return value


def _read_frozen(path_value: object, expected: object, *, label: str) -> tuple[Path, bytes, str]:
    if not isinstance(path_value, str) or not path_value:
        raise RetainedReviewedDraftError(f"{label}_PATH_INVALID")
    path = Path(path_value)
    try:
        if path.is_symlink() or not path.is_file():
            raise RetainedReviewedDraftError(f"{label}_FILE_INVALID")
        raw = path.read_bytes()
    except OSError as exc:
        raise RetainedReviewedDraftError(f"{label}_FILE_UNREADABLE") from exc
    actual = _sha256_bytes(raw)
    if actual != _normalise_sha(expected, label=label):
        raise RetainedReviewedDraftError(f"{label}_HASH_MISMATCH")
    return path, raw, actual


def _read_json(
    path_value: object,
    expected: object,
    *,
    label: str,
    review: bool = False,
) -> tuple[Path, bytes, Any, str]:
    path, raw, digest = _read_frozen(path_value, expected, label=label)
    try:
        if review:
            from src.autoslice.review_evidence_store import review_from_bytes
            parsed = review_from_bytes(raw, directory=path.parent)
        else:
            parsed = json.loads(raw.decode("utf-8"))
    except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetainedReviewedDraftError(f"{label}_JSON_INVALID") from exc
    return path, raw, parsed, digest


def _read_local_json(path: Path, *, label: str) -> tuple[bytes, Any]:
    """Read one normal-preparation sidecar and reject a read-time drift."""

    try:
        before = path.lstat()
        if path.is_symlink() or not path.is_file():
            raise RetainedReviewedDraftError(f"{label}_FILE_INVALID")
        with path.open("rb") as handle:
            opened = handle.fileno()
            before_open = path.stat()
            raw = handle.read()
            handle_stat = os.fstat(opened)
        after = path.lstat()
    except OSError as exc:
        raise RetainedReviewedDraftError(f"{label}_FILE_UNREADABLE") from exc
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    identity_open = (before_open.st_dev, before_open.st_ino, before_open.st_size, before_open.st_mtime_ns, before_open.st_ctime_ns)
    identity_handle = (handle_stat.st_dev, handle_stat.st_ino, handle_stat.st_size, handle_stat.st_mtime_ns, handle_stat.st_ctime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if not (identity_before == identity_open == identity_handle == identity_after):
        raise RetainedReviewedDraftError(f"{label}_FILE_DRIFT")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetainedReviewedDraftError(f"{label}_JSON_INVALID") from exc
    return raw, parsed


def _require_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RetainedReviewedDraftError(f"{label}_MAPPING_REQUIRED")
    return value


def _write_history(path: Path, raw: bytes, *, label: str) -> Path:
    """Persist an immutable copy of an input without replacing prior evidence."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise RetainedReviewedDraftError(f"{label}_HISTORY_PATH_INVALID")
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise RetainedReviewedDraftError(f"{label}_HISTORY_UNREADABLE") from exc
        if existing != raw:
            if label == "PENDING_ENTITY_INPUT":
                from src.autoslice.retained_preparation_checkpoint import replay_retained_history_copy
                if replay_retained_history_copy(path, existing, raw):
                    return path
            raise RetainedReviewedDraftError(f"{label}_HISTORY_DRIFT")
        return path
    try:
        with path.open("xb") as handle:
            handle.write(raw)
    except FileExistsError:
        # A concurrent normal retry may have won the race.  Re-read and apply
        # the same byte-identity rule rather than overwriting it.
        return _write_history(path, raw, label=label)
    except OSError as exc:
        raise RetainedReviewedDraftError(f"{label}_HISTORY_WRITE_FAILED") from exc
    return path


def _media_sha256(path: Path, *, label: str) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            raise RetainedReviewedDraftError(f"{label}_FILE_INVALID")
        before = path.stat()
        with path.open("rb") as handle:
            from .evidence_identity import EvidenceFileChangedError, file_sha256
            try:
                digest = file_sha256(path)
            except EvidenceFileChangedError as exc:
                raise RetainedReviewedDraftError(f"{label}_FILE_DRIFT") from exc
            handle_stat = os.fstat(handle.fileno())
        after = path.stat()
    except OSError as exc:
        raise RetainedReviewedDraftError(f"{label}_FILE_UNREADABLE") from exc
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    identity_handle = (handle_stat.st_dev, handle_stat.st_ino, handle_stat.st_size, handle_stat.st_mtime_ns, handle_stat.st_ctime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if not (identity_before == identity_handle == identity_after):
        raise RetainedReviewedDraftError(f"{label}_FILE_DRIFT")
    return digest


def _piece_rows_from_sidecars(
    *,
    spec: Mapping[str, object],
    plan: Mapping[str, object],
    out_root: Path,
) -> tuple[list[dict[str, object]], bytes, str, list[str]]:
    pieces = spec.get("pieces")
    retained = plan.get("retained_intervals")
    if not isinstance(pieces, list) or not pieces:
        raise RetainedReviewedDraftError("SOURCE_PIECES_INVALID")
    if not isinstance(retained, list) or len(pieces) != len(retained):
        raise RetainedReviewedDraftError("SOURCE_PIECES_PLAN_COUNT_MISMATCH")

    rows: list[dict[str, object]] = []
    output_hashes: list[str] = []
    for index, (piece, interval) in enumerate(zip(pieces, retained, strict=True)):
        if not isinstance(piece, Mapping) or not isinstance(interval, Mapping):
            raise RetainedReviewedDraftError("SOURCE_PIECE_MAPPING_INVALID")
        piece_start = _strict_int(piece.get("start_ms"), label=f"PIECE_{index}_START_MS")
        piece_end = _strict_int(piece.get("end_ms"), label=f"PIECE_{index}_END_MS")
        if piece_end <= piece_start:
            raise RetainedReviewedDraftError("SOURCE_PIECE_RANGE_INVALID")
        path = out_root / f"piece_{index}_{piece_start}_{piece_end}.provenance.json"
        raw, parsed = _read_local_json(path, label=f"PIECE_{index}_PROVENANCE")
        # The sidecar itself is the normal prepare output.  Its bytes are
        # checked above; parsed rows are then checked against the live spec.
        row = dict(_require_mapping(parsed, label=f"PIECE_{index}_PROVENANCE"))
        row_start = _strict_int(row.get("start_ms"), label=f"PIECE_{index}_PROVENANCE_START_MS")
        row_end = _strict_int(row.get("end_ms"), label=f"PIECE_{index}_PROVENANCE_END_MS")
        if (row_start, row_end) != (piece_start, piece_end):
            raise RetainedReviewedDraftError("PIECE_PROVENANCE_SPEC_RANGE_MISMATCH")
        output_path_value = row.get("output_path")
        if not isinstance(output_path_value, str) or not output_path_value:
            raise RetainedReviewedDraftError(f"PIECE_{index}_OUTPUT_PATH_INVALID")
        output_path = Path(output_path_value)
        if output_path.is_symlink() or not output_path.is_file():
            raise RetainedReviewedDraftError(f"PIECE_{index}_OUTPUT_FILE_INVALID")
        actual_output_sha = _media_sha256(output_path, label=f"PIECE_{index}_OUTPUT")
        declared_output_sha = _normalise_sha(row.get("output_sha256"), label=f"PIECE_{index}_OUTPUT")
        if declared_output_sha != actual_output_sha:
            raise RetainedReviewedDraftError(f"PIECE_{index}_OUTPUT_HASH_MISMATCH")
        # Keep the normal sidecar's raw digest spelling in the frozen envelope;
        # the projection helper accepts the producer's raw or prefixed form
        # and normalises only for comparisons.
        row["output_sha256"] = actual_output_sha
        output_hashes.append(actual_output_sha)
        rows.append(row)

    # The envelope is a deterministic view of the exact normal sidecar rows;
    # it is not a new semantic receipt and is hash-bound before projection.
    provenance_document = {"piece_provenance": rows}
    provenance_bytes = json.dumps(
        provenance_document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return rows, provenance_bytes, _sha256_bytes(provenance_bytes), output_hashes


def _validate_padded_provenance(
    *,
    padded: Path,
    padded_sha256: object,
    piece_rows: Sequence[Mapping[str, object]],
) -> str:
    if padded.is_symlink() or not padded.is_file():
        raise RetainedReviewedDraftError("PADDED_MEDIA_FILE_INVALID")
    actual_padded_sha = _media_sha256(padded, label="PADDED_MEDIA")
    if actual_padded_sha != _normalise_sha(padded_sha256, label="PADDED_MEDIA"):
        raise RetainedReviewedDraftError("PADDED_MEDIA_HASH_MISMATCH")
    provenance_path = padded.with_suffix(".provenance.json")
    if provenance_path.is_symlink() or not provenance_path.is_file():
        raise RetainedReviewedDraftError("PADDED_PROVENANCE_FILE_INVALID")
    _raw, document = _read_local_json(provenance_path, label="PADDED_PROVENANCE")
    if not isinstance(document, Mapping):
        raise RetainedReviewedDraftError("PADDED_PROVENANCE_INVALID")
    if document.get("output_path") != str(padded.resolve()):
        raise RetainedReviewedDraftError("PADDED_PROVENANCE_OUTPUT_PATH_MISMATCH")
    if _normalise_sha(document.get("output_sha256"), label="PADDED_PROVENANCE_OUTPUT") != actual_padded_sha:
        raise RetainedReviewedDraftError("PADDED_PROVENANCE_OUTPUT_HASH_MISMATCH")
    inputs = document.get("inputs")
    if not isinstance(inputs, list) or len(inputs) != len(piece_rows):
        raise RetainedReviewedDraftError("PADDED_PROVENANCE_INPUT_COUNT_MISMATCH")
    for index, (raw_input, row) in enumerate(zip(inputs, piece_rows, strict=True)):
        if not isinstance(raw_input, Mapping):
            raise RetainedReviewedDraftError("PADDED_PROVENANCE_INPUT_INVALID")
        if raw_input.get("path") != str(Path(str(row["output_path"])).resolve()):
            raise RetainedReviewedDraftError(f"PADDED_PROVENANCE_INPUT_{index}_PATH_MISMATCH")
        if _normalise_sha(raw_input.get("sha256"), label=f"PADDED_PROVENANCE_INPUT_{index}") != _normalise_sha(row.get("output_sha256"), label=f"PIECE_{index}_OUTPUT"):
            raise RetainedReviewedDraftError(f"PADDED_PROVENANCE_INPUT_{index}_HASH_MISMATCH")
    return actual_padded_sha


def _validate_parent_materialization(
    *,
    config: Mapping[str, object],
    clip_context: Mapping[str, object],
    source_hashes: Sequence[str],
) -> dict[str, object]:
    """Validate the immutable materialization that owns the parent clock.

    The parent boundary audit belongs to the old, finite parent media.  A
    retained draft may have a different concatenated padded output, so this
    check keeps the two media identities and durations separate.  The old
    output and every input sidecar are checked from their actual bytes and
    source ranges; the caller then passes this result to the ordinary parent
    validator as a frozen binding.
    """

    path_value = config.get("parent_materialization_path")
    expected_sha = config.get("parent_materialization_sha256")
    duration_value = config.get("parent_materialization_duration_ms")
    provenance_path_value = config.get("parent_materialization_provenance_path")
    provenance_sha = config.get("parent_materialization_provenance_sha256")
    if not isinstance(path_value, str) or not path_value:
        raise RetainedReviewedDraftError("PARENT_MATERIALIZATION_PATH_REQUIRED")
    if expected_sha is None:
        raise RetainedReviewedDraftError("PARENT_MATERIALIZATION_HASH_REQUIRED")
    if not isinstance(provenance_path_value, str) or not provenance_path_value:
        raise RetainedReviewedDraftError("PARENT_MATERIALIZATION_PROVENANCE_PATH_REQUIRED")
    if provenance_sha is None:
        raise RetainedReviewedDraftError("PARENT_MATERIALIZATION_PROVENANCE_HASH_REQUIRED")
    try:
        duration = _strict_int(duration_value, label="PARENT_MATERIALIZATION_DURATION_MS")
    except RetainedReviewedDraftError:
        raise
    if duration <= 0:
        raise RetainedReviewedDraftError("PARENT_MATERIALIZATION_DURATION_INVALID")

    parent_path = Path(path_value)
    actual_parent_sha = _media_sha256(parent_path, label="PARENT_MATERIALIZATION")
    expected_parent_sha = _normalise_sha(expected_sha, label="PARENT_MATERIALIZATION")
    if actual_parent_sha != expected_parent_sha:
        raise RetainedReviewedDraftError("PARENT_MATERIALIZATION_HASH_MISMATCH")

    provenance_path = Path(provenance_path_value)
    provenance_raw, provenance = _read_local_json(
        provenance_path,
        label="PARENT_MATERIALIZATION_PROVENANCE",
    )
    provenance_digest = _sha256_bytes(provenance_raw)
    if provenance_digest != _normalise_sha(
        provenance_sha, label="PARENT_MATERIALIZATION_PROVENANCE"
    ):
        raise RetainedReviewedDraftError("PARENT_MATERIALIZATION_PROVENANCE_HASH_MISMATCH")
    if not isinstance(provenance, Mapping):
        raise RetainedReviewedDraftError("PARENT_MATERIALIZATION_PROVENANCE_INVALID")
    if provenance.get("output_path") != str(parent_path.resolve()):
        raise RetainedReviewedDraftError("PARENT_MATERIALIZATION_OUTPUT_PATH_MISMATCH")
    if _normalise_sha(
        provenance.get("output_sha256"), label="PARENT_MATERIALIZATION_OUTPUT"
    ) != actual_parent_sha:
        raise RetainedReviewedDraftError("PARENT_MATERIALIZATION_OUTPUT_HASH_MISMATCH")

    inputs = provenance.get("inputs")
    context_pieces = clip_context.get("pieces")
    if not isinstance(inputs, list) or not inputs:
        raise RetainedReviewedDraftError("PARENT_MATERIALIZATION_INPUTS_INVALID")
    if not isinstance(context_pieces, list) or len(inputs) != len(context_pieces):
        raise RetainedReviewedDraftError("PARENT_MATERIALIZATION_INPUT_COUNT_MISMATCH")
    if len(source_hashes) != len(context_pieces):
        raise RetainedReviewedDraftError("PARENT_MATERIALIZATION_SOURCE_COUNT_MISMATCH")

    total_duration = 0
    for index, (raw_input, context_piece, expected_source_sha) in enumerate(
        zip(inputs, context_pieces, source_hashes, strict=True)
    ):
        if not isinstance(raw_input, Mapping) or not isinstance(context_piece, Mapping):
            raise RetainedReviewedDraftError("PARENT_MATERIALIZATION_INPUT_INVALID")
        input_path_value = raw_input.get("path")
        if not isinstance(input_path_value, str) or not input_path_value:
            raise RetainedReviewedDraftError(f"PARENT_MATERIALIZATION_INPUT_{index}_PATH_INVALID")
        input_path = Path(input_path_value)
        actual_input_sha = _media_sha256(
            input_path, label=f"PARENT_MATERIALIZATION_INPUT_{index}"
        )
        if _normalise_sha(raw_input.get("sha256"), label=f"PARENT_MATERIALIZATION_INPUT_{index}") != actual_input_sha:
            raise RetainedReviewedDraftError(
                f"PARENT_MATERIALIZATION_INPUT_{index}_HASH_MISMATCH"
            )

        input_provenance_path = input_path.with_suffix(".provenance.json")
        if input_provenance_path.is_symlink() or not input_provenance_path.is_file():
            raise RetainedReviewedDraftError(
                f"PARENT_MATERIALIZATION_INPUT_{index}_PROVENANCE_MISSING"
            )
        _input_raw, input_provenance = _read_local_json(
            input_provenance_path,
            label=f"PARENT_MATERIALIZATION_INPUT_{index}_PROVENANCE",
        )
        if not isinstance(input_provenance, Mapping):
            raise RetainedReviewedDraftError(
                f"PARENT_MATERIALIZATION_INPUT_{index}_PROVENANCE_INVALID"
            )
        if input_provenance.get("output_path") != str(input_path.resolve()):
            raise RetainedReviewedDraftError(
                f"PARENT_MATERIALIZATION_INPUT_{index}_OUTPUT_PATH_MISMATCH"
            )
        if _normalise_sha(
            input_provenance.get("output_sha256"),
            label=f"PARENT_MATERIALIZATION_INPUT_{index}_OUTPUT",
        ) != actual_input_sha:
            raise RetainedReviewedDraftError(
                f"PARENT_MATERIALIZATION_INPUT_{index}_OUTPUT_HASH_MISMATCH"
            )
        input_start = _strict_int(
            input_provenance.get("start_ms"),
            label=f"PARENT_MATERIALIZATION_INPUT_{index}_START_MS",
        )
        input_end = _strict_int(
            input_provenance.get("end_ms"),
            label=f"PARENT_MATERIALIZATION_INPUT_{index}_END_MS",
        )
        if input_end <= input_start:
            raise RetainedReviewedDraftError(
                f"PARENT_MATERIALIZATION_INPUT_{index}_RANGE_INVALID"
            )
        context_start = _strict_int(
            context_piece.get("start_ms"),
            label=f"PARENT_CONTEXT_PIECE_{index}_START_MS",
        )
        context_end = _strict_int(
            context_piece.get("end_ms"),
            label=f"PARENT_CONTEXT_PIECE_{index}_END_MS",
        )
        if (input_start, input_end) != (context_start, context_end):
            raise RetainedReviewedDraftError(
                f"PARENT_MATERIALIZATION_INPUT_{index}_RANGE_MISMATCH"
            )
        input_source_sha = _normalise_sha(
            input_provenance.get("source_media_binding")
            or input_provenance.get("source_sha256"),
            label=f"PARENT_MATERIALIZATION_INPUT_{index}_SOURCE",
        )
        if input_source_sha != _normalise_sha(
            expected_source_sha, label=f"PARENT_CONTEXT_PIECE_{index}_SOURCE"
        ):
            raise RetainedReviewedDraftError(
                f"PARENT_MATERIALIZATION_INPUT_{index}_SOURCE_MISMATCH"
            )
        context_source_sha = _normalise_sha(
            context_piece.get("source_media_sha256"),
            label=f"PARENT_CONTEXT_PIECE_{index}_SOURCE",
        )
        if input_source_sha != context_source_sha:
            raise RetainedReviewedDraftError(
                f"PARENT_MATERIALIZATION_INPUT_{index}_CONTEXT_SOURCE_MISMATCH"
            )
        total_duration += input_end - input_start

    if provenance.get("duration_ms") is not None and _strict_int(
        provenance.get("duration_ms"), label="PARENT_MATERIALIZATION_PROVENANCE_DURATION_MS"
    ) != duration:
        raise RetainedReviewedDraftError("PARENT_MATERIALIZATION_DURATION_PROVENANCE_MISMATCH")
    if total_duration != duration:
        raise RetainedReviewedDraftError("PARENT_MATERIALIZATION_DURATION_MISMATCH")
    return {
        "path": str(parent_path.resolve()),
        "sha256": actual_parent_sha,
        "duration_ms": duration,
        "provenance_path": str(provenance_path.resolve()),
        "provenance_sha256": provenance_digest,
    }


def _boundary_offset(
    *,
    authority: Mapping[str, object],
    declared_offset: object,
) -> tuple[int, Mapping[str, object], Mapping[str, object]]:
    final_review = _require_mapping(authority.get("final_review_audit"), label="PARENT_FINAL_REVIEW")
    boundary = _require_mapping(final_review.get("boundary_semantic_review"), label="PARENT_BOUNDARY_REVIEW")
    endpoint = _require_mapping(boundary.get("final_endpoint_binding"), label="PARENT_ENDPOINT")
    witness = _require_mapping(boundary.get("source_separation_witness"), label="PARENT_SOURCE_WITNESS")
    if endpoint.get("status") != "PASS" or witness.get("status") != "PASS":
        raise RetainedReviewedDraftError("PARENT_SOURCE_BOUNDARY_NOT_PASS")
    final_start = _strict_int(endpoint.get("final_start_ms"), label="PARENT_FINAL_START_MS")
    final_end = _strict_int(endpoint.get("final_end_ms"), label="PARENT_FINAL_END_MS")
    source_start = _strict_int(witness.get("source_final_start_ms"), label="PARENT_SOURCE_START_MS")
    source_end = _strict_int(witness.get("source_final_end_ms"), label="PARENT_SOURCE_END_MS")
    if final_end <= final_start or source_end <= source_start:
        raise RetainedReviewedDraftError("PARENT_SOURCE_BOUNDARY_INTERVAL_INVALID")
    if source_end - source_start != final_end - final_start:
        raise RetainedReviewedDraftError("PARENT_SOURCE_BOUNDARY_DURATION_MISMATCH")
    offset = source_start - final_start
    declared = _strict_int(declared_offset, label="SOURCE_TIMELINE_OFFSET_MS")
    if declared < 0 or offset != declared or source_end - final_end != declared:
        raise RetainedReviewedDraftError("SOURCE_TIMELINE_OFFSET_PARENT_MISMATCH")
    return offset, endpoint, witness


def _validated_source_boundary_context(
    *,
    config: Mapping[str, object],
    authority: Mapping[str, object],
    clip_context: Mapping[str, object],
    parent_validation: Mapping[str, object],
) -> dict[str, object] | None:
    """Read the source bytes/grid that the ordinary parent already validated."""

    raw_validated = parent_validation.get("source_boundary_context")
    if raw_validated is None:
        if config.get("source_boundary_srt_path") is not None or config.get("source_boundary_srt_sha256") is not None:
            raise RetainedReviewedDraftError("PARENT_SOURCE_BOUNDARY_CONTEXT_MISSING")
        return None
    validated = _require_mapping(raw_validated, label="PARENT_SOURCE_BOUNDARY_CONTEXT")
    path_value = config.get("source_boundary_srt_path")
    expected_value = config.get("source_boundary_srt_sha256")
    explicit = path_value is not None or expected_value is not None
    if explicit:
        if not isinstance(path_value, str) or not path_value or expected_value is None:
            raise RetainedReviewedDraftError("SOURCE_BOUNDARY_SRT_ARTIFACT_PAIR_INVALID")
        source_path, source_raw, source_digest = _read_frozen(
            path_value, expected_value, label="SOURCE_BOUNDARY_SRT"
        )
        source_path_value: str | None = str(source_path)
        source_role = "EXPLICIT_SOURCE_BOUNDARY_ARTIFACT"
    else:
        source_value = authority.get("post_transcript_entity_output_srt")
        declared = authority.get("post_transcript_entity_output_srt_sha256")
        if not isinstance(source_value, str) or not source_value or declared is None:
            raise RetainedReviewedDraftError("SOURCE_BOUNDARY_SRT_MISSING")
        source_raw = source_value.encode("utf-8")
        source_digest = _sha256_bytes(source_raw)
        if _normalise_sha(declared, label="SOURCE_BOUNDARY_SRT") != source_digest:
            raise RetainedReviewedDraftError("SOURCE_BOUNDARY_SRT_HASH_MISMATCH")
        source_path_value = None
        source_role = "CHAT_AUTHORITY_POST_TRANSCRIPT_ENTITY_OUTPUT"
    try:
        source_text = source_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RetainedReviewedDraftError("SOURCE_BOUNDARY_SRT_UTF8_INVALID") from exc
    if _normalise_sha(validated.get("source_srt_sha256"), label="SOURCE_BOUNDARY_CONTEXT_SRT") != source_digest:
        raise RetainedReviewedDraftError("SOURCE_BOUNDARY_CONTEXT_SOURCE_SRT_HASH_MISMATCH")
    if validated.get("source_srt_path") != source_path_value:
        raise RetainedReviewedDraftError("SOURCE_BOUNDARY_CONTEXT_PATH_MISMATCH")
    pieces = clip_context.get("pieces")
    if not isinstance(pieces, list) or len(pieces) != 1 or not isinstance(pieces[0], Mapping):
        raise RetainedReviewedDraftError("SOURCE_BOUNDARY_CONTEXT_PIECE_COUNT_INVALID")
    piece_start = _strict_int(
        pieces[0].get("start_ms"), label="SOURCE_BOUNDARY_CONTEXT_PIECE_START_MS"
    )
    if piece_start < 0:
        raise RetainedReviewedDraftError("SOURCE_BOUNDARY_CONTEXT_PIECE_START_INVALID")
    return {
        "source_srt_role": source_role,
        "source_srt_path": source_path_value,
        "source_srt_text": source_text,
        "source_srt_sha256": "sha256:" + source_digest,
        "source_cue_grid_sha256": validated.get("source_cue_grid_sha256"),
        "parent_context_piece_start_ms": piece_start,
    }


def _clock_interval(
    start: int, end: int, pieces: Sequence[Mapping[str, int]], *, source: bool
) -> dict[str, int] | None:
    left_start, left_end = ("source_start_ms", "source_end_ms") if source else ("concat_start_ms", "concat_end_ms")
    right_start, right_end = ("concat_start_ms", "concat_end_ms") if source else ("source_start_ms", "source_end_ms")
    for piece in pieces:
        if piece[left_start] <= start and end <= piece[left_end]:
            mapped_start = piece[right_start] + start - piece[left_start]
            mapped_end = piece[right_start] + end - piece[left_start]
            if mapped_start < piece[right_start] or mapped_end > piece[right_end]:
                raise RetainedReviewedDraftError(
                    "SOURCE_BOUNDARY_CUE_EXCEEDS_MEASURED_PIECE"
                    if source else "SOURCE_BOUNDARY_CURRENT_CUE_EXCEEDS_PIECE"
                )
            return {"start_ms": mapped_start, "end_ms": mapped_end}
    return None


def build_retained_source_boundary_context(
    retained: Mapping[str, object], reviewed_srt_text: str
) -> tuple[list[SrtCue], dict[str, object]]:
    """Project source padding and the latest reviewed body onto concat time."""

    context = _require_mapping(retained.get("source_boundary_context"), label="SOURCE_BOUNDARY_CONTEXT")
    projection = _require_mapping(retained.get("projection"), label="SOURCE_BOUNDARY_PROJECTION")
    source_text = context.get("source_srt_text")
    if not isinstance(source_text, str) or not source_text:
        raise RetainedReviewedDraftError("SOURCE_BOUNDARY_SRT_MISSING")
    source_raw = source_text.encode("utf-8")
    source_digest = _sha256_bytes(source_raw)
    if _normalise_sha(context.get("source_srt_sha256"), label="SOURCE_BOUNDARY_SRT") != source_digest:
        raise RetainedReviewedDraftError("SOURCE_BOUNDARY_SRT_HASH_MISMATCH")
    source_cues = parse_srt_cues(source_text)
    if not source_cues:
        raise RetainedReviewedDraftError("SOURCE_BOUNDARY_SRT_MISSING")
    source_grid = cue_grid_sha256(source_cues)
    if _normalise_sha(context.get("source_cue_grid_sha256"), label="SOURCE_BOUNDARY_GRID") != _normalise_sha(source_grid, label="SOURCE_BOUNDARY_GRID"):
        raise RetainedReviewedDraftError("SOURCE_BOUNDARY_GRID_HASH_MISMATCH")
    raw_pieces = projection.get("piece_clock")
    if not isinstance(raw_pieces, list) or not raw_pieces:
        raise RetainedReviewedDraftError("SOURCE_BOUNDARY_PIECE_CLOCK_MISSING")
    pieces = [_require_mapping(value, label=f"SOURCE_BOUNDARY_PIECE_{index}") for index, value in enumerate(raw_pieces)]
    physical = _require_mapping(projection.get("physical_source_range_ms"), label="SOURCE_BOUNDARY_PHYSICAL_RANGE")
    body_start = _strict_int(physical.get("start_ms"), label="SOURCE_BOUNDARY_BODY_START_MS")
    body_end = _strict_int(physical.get("end_ms"), label="SOURCE_BOUNDARY_BODY_END_MS")
    offset = _strict_int(projection.get("source_timeline_offset_ms"), label="SOURCE_BOUNDARY_OFFSET_MS")
    source_range = _require_mapping(projection.get("source_range_ms"), label="SOURCE_BOUNDARY_SOURCE_RANGE")
    if body_end <= body_start or body_start != _strict_int(source_range.get("start_ms"), label="SOURCE_BOUNDARY_SOURCE_START_MS") + offset or body_end != _strict_int(source_range.get("end_ms"), label="SOURCE_BOUNDARY_SOURCE_END_MS") + offset:
        raise RetainedReviewedDraftError("SOURCE_BOUNDARY_CLOCK_BINDING_MISMATCH")
    piece_start = _strict_int(context.get("parent_context_piece_start_ms"), label="SOURCE_BOUNDARY_CONTEXT_PIECE_START_MS")
    body_start_map = _clock_interval(body_start, body_start, pieces, source=True)
    body_end_map = _clock_interval(body_end, body_end, pieces, source=True)
    if body_start_map is None or body_end_map is None:
        raise RetainedReviewedDraftError("SOURCE_BOUNDARY_BODY_RANGE_CROSSES_PIECE_BOUNDARY")
    concat_start, concat_end = body_start_map["start_ms"], body_end_map["end_ms"]

    reviewed_cues = parse_srt_cues(reviewed_srt_text)
    if not reviewed_cues:
        raise RetainedReviewedDraftError("SOURCE_BOUNDARY_REVIEWED_SRT_MISSING")
    body_rows: list[SrtCue] = []
    delivery_rows: list[dict[str, object]] = []
    caption_source_ranges: list[tuple[int, int]] = []
    for cue in reviewed_cues:
        if cue.start_ms < concat_start or cue.end_ms > concat_end:
            raise RetainedReviewedDraftError("SOURCE_BOUNDARY_CURRENT_CUE_OUTSIDE_BODY")
        mapped = _clock_interval(cue.start_ms, cue.end_ms, pieces, source=False)
        if mapped is None:
            raise RetainedReviewedDraftError("SOURCE_BOUNDARY_CURRENT_CUE_CROSSES_PIECE_BOUNDARY")
        if mapped["start_ms"] < body_start or mapped["end_ms"] > body_end:
            raise RetainedReviewedDraftError("SOURCE_BOUNDARY_CURRENT_CUE_OUTSIDE_BODY")
        caption_source_ranges.append((mapped["start_ms"], mapped["end_ms"]))
        body_rows.append(cue)
        delivery_rows.append({
            "cue_index": str(cue.index),
            "delivery_start_ms": cue.start_ms,
            "delivery_end_ms": cue.end_ms,
            "source_start_ms": cue.start_ms,
            "source_end_ms": cue.end_ms,
            "text_sha256": _sha256_prefixed(cue.text.encode("utf-8")),
        })

    padding_rows: list[SrtCue] = []
    dropped_count = replaced_count = 0
    caption_start = min(start for start, _end in caption_source_ranges)
    caption_end = max(end for _start, end in caption_source_ranges)
    for cue in source_cues:
        start = cue.start_ms + piece_start
        end = cue.end_ms + piece_start
        if (start < body_start < end) or (start < body_end < end):
            # A physical cut may lie in the captionless leading/trailing gap.
            # Keep the complete original cue as source-context evidence only
            # when it is disjoint from the current caption span. Never clip
            # it or merge a crossing utterance into the reviewed body.
            if not (end <= caption_start or start >= caption_end):
                raise RetainedReviewedDraftError("SOURCE_BOUNDARY_CUE_CROSSES_BODY_RANGE")
        mapped = _clock_interval(start, end, pieces, source=True)
        if mapped is None and (
            end <= pieces[0]["source_start_ms"]
            or start >= pieces[-1]["source_end_ms"]
            or any(
                left["source_end_ms"] <= start and end <= right["source_start_ms"]
                for left, right in zip(pieces[:-1], pieces[1:], strict=True)
            )
        ):
            dropped_count += 1
            continue
        if mapped is None:
            raise RetainedReviewedDraftError("SOURCE_BOUNDARY_CUE_CROSSES_PIECE_BOUNDARY")
        if body_start <= start and end <= body_end:
            replaced_count += 1
            continue
        padding_rows.append(SrtCue("", mapped["start_ms"], mapped["end_ms"], cue.text))

    rows = sorted(
        [*padding_rows, *body_rows],
        key=lambda cue: (cue.start_ms, cue.end_ms, cue.text),
    )
    output = [SrtCue(str(index), cue.start_ms, cue.end_ms, cue.text) for index, cue in enumerate(rows, 1)]
    proof = {
        "schema_version": _SOURCE_BOUNDARY_PROJECTION_SCHEMA,
        "status": "EVIDENCE_ONLY",
        "fresh_review_required": True,
        "prior_boundary_pass_inherited": False,
        "source_srt_sha256": "sha256:" + source_digest,
        "source_cue_grid_sha256": source_grid,
        "reviewed_srt_sha256": _sha256_prefixed(reviewed_srt_text.encode("utf-8")),
        "output_cue_grid_sha256": cue_grid_sha256(output),
        "parent_context_piece_start_ms": piece_start,
        "padding_cue_count": len(padding_rows),
        "replaced_source_cue_count": replaced_count,
        "dropped_source_cue_count": dropped_count,
        # Retained text is already on the measured padded concat clock.  The
        # boundary consumer still needs an explicit current-body contract;
        # the wider source context contains padding and has different indices.
        # PASS here attests geometry only, never a semantic or text release.
        "current_delivery_source_projection": {
            "schema_version": "delivery-to-source-timeline-projection.v1",
            "status": "PASS",
            "role": "MEASURED_CONCAT_GEOMETRY_ONLY",
            "delivery_srt_role": "CURRENT_RETAINED_DRAFT_TEXT",
            "source_srt_role": "PADDED_SOURCE_DOMAIN_COORDINATES",
            "delivery_final_start_ms": concat_start,
            "delivery_final_end_ms": concat_end,
            "source_final_start_ms": concat_start,
            "source_final_end_ms": concat_end,
            "offset_ms": 0,
            "delivery_srt_cue_grid_sha256": cue_grid_sha256(reviewed_cues),
            "projected_source_cue_grid_sha256": cue_grid_sha256(reviewed_cues),
            "reviewed_srt_sha256": _sha256_prefixed(reviewed_srt_text.encode("utf-8")),
            "cue_count": len(delivery_rows),
            "delivery_first_start_ms": delivery_rows[0]["delivery_start_ms"],
            "delivery_last_end_ms": delivery_rows[-1]["delivery_end_ms"],
            "source_first_start_ms": delivery_rows[0]["source_start_ms"],
            "source_last_end_ms": delivery_rows[-1]["source_end_ms"],
            "rows": delivery_rows,
            "semantic_authority": False,
            "fresh_review_required": True,
            "prior_boundary_pass_inherited": False,
        },
    }
    return output, proof


def _default_parent_validator(**_kwargs: object) -> Mapping[str, object]:
    raise RetainedReviewedDraftError("PARENT_VALIDATOR_REQUIRED")


def load_retained_reviewed_draft(
    *,
    spec: Mapping[str, object],
    durations: Sequence[int],
    padded: Path,
    padded_dur: int,
    out_root: Path,
    cid: str,
    validate_parent: Callable[..., Mapping[str, object]] = _default_parent_validator,
) -> dict[str, object]:
    """Load, validate and project a retained current-text draft.

    ``validate_parent`` is the ordinary producer continuation validator.  The
    loader never substitutes a weaker validator when it rejects a historical
    BLOCK.  The returned ``proof`` is deliberately ``PROJECTED`` only and must
    be followed by a fresh entity/text/boundary review.
    """

    config = _require_mapping(spec.get("retained_current_text"), label="RETAINED_CURRENT_TEXT")
    if config.get("schema_version") != _SCHEMA:
        raise RetainedReviewedDraftError("RETAINED_CURRENT_TEXT_SCHEMA_INVALID")
    if not isinstance(durations, Sequence) or isinstance(durations, (str, bytes)):
        raise RetainedReviewedDraftError("SOURCE_DURATIONS_INVALID")
    measured_durations = [_strict_int(value, label=f"PIECE_{i}_DURATION_MS") for i, value in enumerate(durations)]
    if any(value <= 0 for value in measured_durations):
        raise RetainedReviewedDraftError("SOURCE_DURATIONS_INVALID")
    padded_duration = _strict_int(padded_dur, label="PADDED_DURATION_MS")
    if padded_duration <= 0:
        raise RetainedReviewedDraftError("PADDED_DURATION_INVALID")

    _current_path, current_raw, _current_digest = _read_frozen(
        config.get("current_srt_path"), config.get("current_srt_sha256"), label="CURRENT_SRT"
    )
    try:
        current_text = current_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RetainedReviewedDraftError("CURRENT_SRT_UTF8_INVALID") from exc
    if not parse_srt_cues(current_text):
        raise RetainedReviewedDraftError("CURRENT_SRT_EMPTY")

    parent_auth_path, parent_auth_raw, parent_auth, parent_auth_digest = _read_json(
        config.get("parent_authority_path"), config.get("parent_authority_sha256"),
        label="PARENT_AUTHORITY", review=True,
    )
    parent_ctx_path, parent_ctx_raw, parent_ctx, parent_ctx_digest = _read_json(
        config.get("parent_clip_context_path"), config.get("parent_clip_context_sha256"), label="PARENT_CLIP_CONTEXT"
    )
    plan_path, plan_raw, plan, plan_digest = _read_json(
        config.get("filler_plan_path"), config.get("filler_plan_sha256"), label="FILLER_PLAN"
    )
    plan = _require_mapping(plan, label="FILLER_PLAN")
    # A strict no-op identity plan has no semantic verifier request or
    # response to replay.  Keep the structural candidate check separate from
    # the complete predicate so malformed identity candidates still reach the
    # normal projection validation and fail closed on their missing/invalid
    # duration or verification fields.
    identity_candidate = _is_zero_deletion_identity_shape(plan)
    identity_plan = is_zero_deletion_identity_plan(plan)
    request_path: Path | None = None
    response_path: Path | None = None
    request_raw = b""
    response_raw = b""
    response_digest: str | None = None
    verifier_request: str | None = None
    verifier_response: str | None = None
    if not identity_candidate:
        request_path, request_raw, _request_digest = _read_frozen(
            config.get("verifier_request_path"),
            config.get("verifier_request_sha256"),
            label="VERIFIER_REQUEST",
        )
        response_path, response_raw, response_digest = _read_frozen(
            config.get("verifier_response_path"),
            config.get("verifier_response_sha256"),
            label="VERIFIER_RESPONSE",
        )
        try:
            verifier_request = request_raw.decode("utf-8")
            verifier_response = response_raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RetainedReviewedDraftError("VERIFIER_BYTES_UTF8_INVALID") from exc

    source_binding = _require_mapping(config.get("source_binding"), label="SOURCE_BINDING")
    # Let the existing projection helper enforce the exact raw source/binding
    # shape.  We still require the declared SRT binding here for early errors.
    if _normalise_sha(source_binding.get("source_srt_sha256"), label="SOURCE_BINDING_SOURCE_SRT") != _sha256_bytes(current_raw):
        raise RetainedReviewedDraftError("SOURCE_BINDING_SRT_HASH_MISMATCH")
    padded_sha = config.get("padded_sha256")
    if padded_sha is None:
        raise RetainedReviewedDraftError("PADDED_MEDIA_HASH_REQUIRED")

    pieces = spec.get("pieces")
    if not isinstance(pieces, list):
        raise RetainedReviewedDraftError("SOURCE_PIECES_INVALID")
    rows, provenance_raw, provenance_digest, output_hashes = _piece_rows_from_sidecars(
        spec=spec, plan=plan, out_root=out_root
    )
    if len(rows) != len(measured_durations):
        raise RetainedReviewedDraftError("SOURCE_DURATIONS_PIECE_COUNT_MISMATCH")
    padded_digest = _validate_padded_provenance(
        padded=padded, padded_sha256=padded_sha, piece_rows=rows
    )

    source_hashes = config.get("source_media_sha256s")
    if source_hashes is None:
        source_hashes = [source_binding.get("source_media_binding")]
    if not isinstance(source_hashes, list) or not source_hashes:
        raise RetainedReviewedDraftError("PARENT_SOURCE_HASHES_INVALID")
    normalised_source_hashes = [_normalise_sha(value, label="SOURCE_MEDIA") for value in source_hashes]
    try:
        parent_ctx = validate_clip_context(
            parent_ctx,
            candidate_id=cid,
            recording_date=str(spec.get("date") or "") or None,
            source_media_sha256s=["sha256:" + value for value in normalised_source_hashes],
        )
    except Exception as exc:  # ordinary validator errors remain a hard input failure
        raise RetainedReviewedDraftError(f"PARENT_CLIP_CONTEXT_INVALID:{exc}") from exc

    parent_materialization = _validate_parent_materialization(
        config=config,
        clip_context=parent_ctx,
        source_hashes=["sha256:" + value for value in normalised_source_hashes],
    )

    try:
        parent_validation = validate_parent(
            authority=parent_auth,
            srt_text=current_text,
            srt_sha256=_sha256_bytes(current_raw),
            clip_context=parent_ctx,
            candidate_id=cid,
            boundary_semantic_review=parent_auth["final_review_audit"]["boundary_semantic_review"],
            retained_config=config,
            padded_sha256="sha256:" + padded_digest,
            source_media_sha256s=["sha256:" + value for value in normalised_source_hashes],
            padded_duration_ms=padded_duration,
            parent_materialization_path=parent_materialization["path"],
            parent_materialization_sha256="sha256:" + str(parent_materialization["sha256"]),
            parent_materialization_duration_ms=parent_materialization["duration_ms"],
            parent_materialization_provenance_path=parent_materialization["provenance_path"],
            parent_materialization_provenance_sha256="sha256:" + str(parent_materialization["provenance_sha256"]),
        )
    except RetainedReviewedDraftError:
        raise
    except Exception as exc:
        raise RetainedReviewedDraftError(f"PARENT_VALIDATION_FAILED:{exc}") from exc
    if not isinstance(parent_validation, Mapping):
        raise RetainedReviewedDraftError("PARENT_VALIDATION_RESULT_INVALID")
    validated_offset = parent_validation.get("source_timeline_offset_ms")
    validated_endpoint = parent_validation.get("source_endpoint")
    if validated_offset is not None:
        offset = _strict_int(validated_offset, label="SOURCE_TIMELINE_OFFSET_MS")
        declared_offset = _strict_int(
            config.get("source_timeline_offset_ms"),
            label="SOURCE_TIMELINE_OFFSET_MS",
        )
        if offset != declared_offset:
            raise RetainedReviewedDraftError("SOURCE_TIMELINE_OFFSET_PARENT_MISMATCH")
        endpoint = _require_mapping(validated_endpoint, label="PARENT_SOURCE_ENDPOINT")
        witness = parent_validation.get("source_witness")
        if witness is not None:
            witness = _require_mapping(witness, label="PARENT_SOURCE_WITNESS")
    else:
        offset, endpoint, witness = _boundary_offset(
            authority=parent_auth,
            declared_offset=config.get("source_timeline_offset_ms"),
        )

    cues = parse_srt_cues(current_text)
    verifier_cues = [
        SimpleNamespace(
            index=cue.index,
            source_start_ms=cue.start_ms,
            source_end_ms=cue.end_ms,
            text=cue.text,
        )
        for cue in cues
    ]
    callback_calls = 0

    def replay_verifier(prompt: str) -> str:
        nonlocal callback_calls
        callback_calls += 1
        if identity_candidate:
            # An identity plan must be accepted only by the verifier's
            # deterministic NO_AUTOMATIC_REMOVAL result.  If a malformed plan
            # unexpectedly reaches the provider callback path, fail closed
            # rather than fabricating a request/response binding.
            raise RetainedReviewedDraftError("VERIFIER_REQUEST_UNEXPECTED")
        if prompt != verifier_request:
            raise RetainedReviewedDraftError("VERIFIER_REQUEST_MISMATCH")
        if verifier_response is None:
            raise RetainedReviewedDraftError("VERIFIER_RESPONSE_MISSING")
        return verifier_response

    try:
        verification = verify_automatic_filler_plan(
            cues=verifier_cues,
            plan=plan,
            llm_call=replay_verifier,
        )
    except RetainedReviewedDraftError:
        raise
    except Exception as exc:
        raise RetainedReviewedDraftError(f"VERIFIER_REPLAY_FAILED:{exc}") from exc
    if identity_candidate:
        if callback_calls != 0:
            raise RetainedReviewedDraftError("VERIFIER_REQUEST_UNEXPECTED")
        if verification != plan.get("global_verification"):
            raise RetainedReviewedDraftError("VERIFIER_RESULT_PLAN_MISMATCH")
        # ``identity_plan`` includes the explicit duration and verifier
        # contract.  Keep the projection as the final validator so malformed
        # candidates report its precise immutable-plan error rather than being
        # promoted by this ingress branch.
        if identity_plan:
            if verification.get("status") != "NOT_REQUIRED" or verification.get(
                "reason_code"
            ) != "NO_AUTOMATIC_REMOVAL":
                raise RetainedReviewedDraftError("VERIFIER_REPLAY_NOT_NOOP")
    else:
        if callback_calls == 0:
            raise RetainedReviewedDraftError("VERIFIER_REQUEST_NOT_REPLAYED")
        if verification.get("status") != "PASS":
            error = str(verification.get("error") or verification.get("reason_code") or "UNKNOWN")
            if "VERIFIER_REQUEST_MISMATCH" in error:
                raise RetainedReviewedDraftError("VERIFIER_REQUEST_MISMATCH")
            raise RetainedReviewedDraftError(f"VERIFIER_REPLAY_NOT_PASS:{error}")
        if verification != plan.get("global_verification"):
            raise RetainedReviewedDraftError("VERIFIER_RESULT_PLAN_MISMATCH")

    try:
        projection = build_retained_current_text_view(
            source_srt_bytes=current_raw,
            authority_bytes=parent_auth_raw,
            clip_context_bytes=parent_ctx_raw,
            plan=plan,
            plan_bytes=plan_raw,
            plan_sha256=plan_digest,
            piece_provenance=rows,
            provenance_bytes=provenance_raw,
            provenance_sha256=provenance_digest,
            piece_durations_ms=measured_durations,
            source_binding=source_binding,
            authority_sha256=parent_auth_digest,
            clip_context_sha256=parent_ctx_digest,
            source_timeline_offset_ms=offset,
        )
    except (RetainedCurrentTextViewError, OSError) as exc:
        raise RetainedReviewedDraftError(f"RETAINED_PROJECTION_FAILED:{exc}") from exc

    source_boundary_input = _validated_source_boundary_context(
        config=config,
        authority=parent_auth,
        clip_context=parent_ctx,
        parent_validation=parent_validation,
    )
    source_boundary_cues: list[SrtCue] | None = None
    source_boundary_projection: dict[str, object] | None = None
    if source_boundary_input is not None:
        source_boundary_cues, source_boundary_projection = build_retained_source_boundary_context(
            {
                "source_boundary_context": source_boundary_input,
                "projection": projection,
            },
            str(projection["srt_text"]),
        )

    history = {
        "parent_srt": _write_history(out_root / f"{cid}.retained-parent-current.srt", current_raw, label="PARENT_SRT"),
        "parent_authority": _write_history(out_root / f"{cid}.retained-parent-authority.json", parent_auth_raw, label="PARENT_AUTHORITY"),
        "parent_clip_context": _write_history(out_root / f"{cid}.retained-parent-clip-context.json", parent_ctx_raw, label="PARENT_CLIP_CONTEXT"),
        "filler_plan": _write_history(out_root / f"{cid}.retained-parent-filler-plan.json", plan_raw, label="FILLER_PLAN"),
        "piece_provenance": _write_history(
            out_root / f"{cid}.retained-parent-piece-provenance.json", provenance_raw, label="PIECE_PROVENANCE"
        ),
    }
    if not identity_candidate:
        history["verifier_request"] = _write_history(
            out_root / f"{cid}.retained-parent-verifier-request.txt",
            request_raw,
            label="VERIFIER_REQUEST",
        )
        history["verifier_response"] = _write_history(
            out_root / f"{cid}.retained-parent-verifier-response.txt",
            response_raw,
            label="VERIFIER_RESPONSE",
        )
    if source_boundary_input is not None:
        source_boundary_raw = str(source_boundary_input["source_srt_text"]).encode("utf-8")
        history["source_boundary_srt"] = _write_history(
            out_root / f"{cid}.retained-source-boundary.srt",
            source_boundary_raw,
            label="SOURCE_BOUNDARY_SRT",
        )
    source_endpoint = {
        "final_start_ms": endpoint["final_start_ms"],
        "final_end_ms": endpoint["final_end_ms"],
    }
    if witness is not None:
        source_endpoint.update(
            {
                "source_final_start_ms": witness["source_final_start_ms"],
                "source_final_end_ms": witness["source_final_end_ms"],
            }
        )
    proof = {
        "schema_version": _SCHEMA,
        "status": "PROJECTED",
        "fresh_review_required": True,
        "candidate_id": cid,
        "source_srt_sha256": _sha256_prefixed(current_raw),
        "projected_srt_sha256": projection["projected_srt_sha256"],
        "parent_authority_sha256": "sha256:" + parent_auth_digest,
        "parent_clip_context_sha256": "sha256:" + parent_ctx_digest,
        "filler_plan_sha256": "sha256:" + plan_digest,
        "piece_provenance_sha256": "sha256:" + provenance_digest,
        "padded_sha256": "sha256:" + padded_digest,
        "padded_duration_ms": padded_duration,
        "source_timeline_offset_ms": offset,
        "source_endpoint": source_endpoint,
        "piece_output_sha256s": ["sha256:" + value for value in output_hashes],
        "history_paths": {key: str(path) for key, path in history.items()},
        "parent_validation": {
            "status": parent_validation.get("status", "INPUT_VALIDATED"),
            "fresh_review_required": parent_validation.get("fresh_review_required", True),
            "historical_release_gate": parent_validation.get("historical_release_gate"),
            "unresolved_finding_count": parent_validation.get("unresolved_finding_count"),
            "pending_entity_reviews": list(parent_validation.get("pending_entity_reviews") or []),
            "parent_materialization_path": parent_validation.get("parent_materialization_path"),
            "parent_materialization_sha256": parent_validation.get("parent_materialization_sha256"),
            "parent_materialization_duration_ms": parent_validation.get("parent_materialization_duration_ms"),
            "parent_materialization_provenance_path": parent_validation.get(
                "parent_materialization_provenance_path"
            ),
            "parent_materialization_provenance_sha256": parent_validation.get(
                "parent_materialization_provenance_sha256"
            ),
        },
        "projection_status": projection["status"],
    }
    if response_digest is not None:
        proof["verifier_response_sha256"] = "sha256:" + response_digest
    if source_boundary_projection is not None:
        proof["source_boundary_projection"] = source_boundary_projection
    pending_entities = []
    for pending in parent_validation.get("pending_entity_reviews") or []:
        matches = [row for row in projection["cue_map"]
            if str(row["old_cue_index"]) == str(pending["parent_delivery_cue_index"])
            and row["old_start_ms"] == pending["parent_delivery_start_ms"]
            and row["old_end_ms"] == pending["parent_delivery_end_ms"]
            and row["text_sha256"] == pending["text_sha256"]]
        if len(matches) != 1:
            raise RetainedReviewedDraftError("PENDING_ENTITY_PROJECTION_DRIFT")
        pending_entities.append({**pending, "projection": dict(matches[0])})
    if pending_entities:
        proof["pending_entity_reviews"] = pending_entities
        proof["pending_entity_review_required"] = True
        _write_history(out_root / f"{cid}.retained-entity-review-input.json",
            json.dumps(proof, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"),
            label="PENDING_ENTITY_INPUT")

    result = {
        "schema_version": _SCHEMA,
        "srt_text": projection["srt_text"],
        "proof": proof,
        "projection": projection,
        "parent_authority": dict(parent_auth),
        "parent_clip_context": dict(parent_ctx),
        "parent_srt_text": current_text,
        "plan": dict(plan),
        "source_media_sha256s": tuple("sha256:" + value for value in normalised_source_hashes),
        "padded_sha256": "sha256:" + padded_digest,
        "piece_output_sha256s": tuple("sha256:" + value for value in output_hashes),
        "source_boundary_context": source_boundary_input,
        "source_boundary_cues": tuple(source_boundary_cues or ()),
        "source_boundary_projection": source_boundary_projection,
    }
    if response_digest is not None:
        result["verifier_response_sha256"] = "sha256:" + response_digest
    from src.autoslice.semantic_request_history import freeze_parent_exchanges, sources_from_audit
    original_contexts = [{"path": str(parent_ctx_path), "sha256": "sha256:" + parent_ctx_digest}]
    checkpoint = parent_auth_path.parent / (cid + ".source-text-checkpoint.json")
    if checkpoint.is_file() and not checkpoint.is_symlink():
        checkpoint_data = json.loads(checkpoint.read_bytes())
        if isinstance(checkpoint_data.get("context"), Mapping):
            original_contexts.append(dict(checkpoint_data["context"]))
    exchange_bytes = freeze_parent_exchanges(output_root=parent_auth_path.parent,
        source_contexts=original_contexts, source_media_sha256=padded_digest)
    inherited_sources = sources_from_audit(parent_auth)
    if exchange_bytes is not None:
        exchange_path = _write_history(out_root / f"{cid}.retained-parent-cpa-exchanges.json",
                                      exchange_bytes, label="PARENT_CPA_EXCHANGES")
        inherited_sources.append({"kind": "exchanges", "path": str(exchange_path),
                                  "sha256": _sha256_prefixed(exchange_bytes)})
    if inherited_sources:
        proof["semantic_history_sources"] = inherited_sources
    from src.autoslice.semantic_request_history import partial_proof_from_audit
    partial_reference = spec.get("retained_partial_evidence")
    partial_parent_sha = parent_auth_digest
    if partial_reference is None:
        inherited_partial = partial_proof_from_audit(parent_auth)
        partial_reference = inherited_partial.get("manifest")
        if partial_reference is not None:
            original_parent = inherited_partial.get("parent_authority") or {}
            partial_parent_sha = str(original_parent.get("sha256") or "").removeprefix("sha256:")
            # Follow a byte-bound ancestor, never retag the stopped run as a
            # child of a newer successful review.
            if not partial_parent_sha or not any(
                    ref.get("kind") == "authority"
                    and str(ref.get("sha256") or "").removeprefix("sha256:") == partial_parent_sha
                    for ref in inherited_sources):
                raise RetainedReviewedDraftError("RETAINED_PARTIAL_ANCESTOR_BINDING_INVALID")
    runtime_root = Path(str(spec.get("runtime_root") or out_root))
    if partial_reference is not None:
        from src.autoslice.retained_partial_evidence import load_partial_evidence
        partial = load_partial_evidence(reference=partial_reference,
            candidate_id=cid, source=padded, parent_authority_sha256=partial_parent_sha,
            runtime_root=runtime_root)
        result["partial_evidence"] = partial
        proof["partial_evidence"] = partial["proof"]
    else:
        # A completed (non-partial) retained parent carries the same physical
        # source account. 41:1540-1541: its native occupation, failures and
        # cache are never refunded or reset, and its usable observations are
        # reused first. Join that ledger through the ordinary inheritance,
        # exactly as retained_source_input does, instead of a hand-made seed.
        from src.autoslice.prepared_native_audio_recovery import (
            inherit_prepared_native_audio, join_own_interrupted_account,
        )
        native = inherit_prepared_native_audio(
            cid=cid, parent=parent_auth_path.parent, source=padded, runtime_root=runtime_root)
        # An interrupted earlier attempt in this same output root keeps its own
        # real observations (exact-prefix + complete originals, or nothing).
        native, own_account = join_own_interrupted_account(
            native, cid=cid, out_root=Path(out_root), source=padded, runtime_root=runtime_root)
        if own_account is not None:
            result["retained_native_own_attempt"] = own_account
        # Runtime inputs only. The retained proof stays byte-identical to the
        # saved retained-entity-review input (its identity is re-checked).
        result["retained_native"] = native
        result["retained_native_parent_output_root"] = str(parent_auth_path.parent)
    return result


def pending_entity_review_findings(
    retained: Mapping[str, object] | None, *, srt_text: str,
) -> list[dict[str, object]]:
    """Nominate each validated old question on the unchanged current grid.

    The old candidate and old verdict are deliberately absent. A changed
    target requires an explicit upstream rebind, not text-only fuzzy matching.
    """
    proof = (retained or {}).get("proof") or {}
    pending = proof.get("pending_entity_reviews") or []
    if not pending:
        return []
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    findings = []
    for item in pending:
        projected = item["projection"]
        matches = [(index, cue) for index, cue in enumerate(cues, 1)
            if cue.start_ms == projected["new_start_ms"]
            and cue.end_ms == projected["new_end_ms"]
            and _sha256_prefixed(cue.text.encode()) == item["text_sha256"]]
        if len(matches) != 1:
            raise RetainedReviewedDraftError("PENDING_ENTITY_CURRENT_TARGET_DRIFT")
        index, cue = matches[0]
        findings.append({
            "cue": index, "cue_index": index, "kind": "context",
            "suspect": cue.text[item["entity_start_codepoint"]:item["entity_end_codepoint"]],
            "span_start_codepoint": item["entity_start_codepoint"],
            "span_end_codepoint": item["entity_end_codepoint"],
            "proposed_full_cue": None, "repair_class": "phonetic",
            "base_text_sha256": _sha256_bytes(cue.text.encode()),
            "why": "A hash-bound source entity receipt still requests audio; "
                   "re-adjudicate this surviving utterance on current evidence.",
        })
    return findings


def validate_pending_entity_checkpoint(retained, snapshot) -> None:
    """An older CLEAN checkpoint cannot erase a newly recovered obligation."""
    pending = (retained.get("proof") or {}).get("pending_entity_reviews") or []
    if not pending or snapshot is None:
        return
    authority = snapshot.get("chat_authority") or {}
    prior = authority.get("retained_reviewed_draft") or {}
    completion = authority.get("retained_entity_review") or {}
    if (prior.get("pending_entity_reviews") != pending
            or completion.get("status") != "CURRENT_REVIEW_COMPLETED"
            or completion.get("parent_authority_sha256")
            != retained["proof"]["parent_authority_sha256"]):
        raise RetainedReviewedDraftError("PENDING_ENTITY_CHECKPOINT_REVIEW_REQUIRED")
    _path, _raw, saved, _digest = _read_json(
        completion.get("path"), completion.get("sha256"), label="PENDING_ENTITY_COMPLETION")
    if (saved.get("schema_version") != "retained-entity-current-review.v1"
            or saved.get("status") != "CURRENT_REVIEW_COMPLETED"
            or saved.get("parent_authority_sha256") != prior.get("parent_authority_sha256")
            or saved.get("pending_entity_reviews") != pending
            or saved.get("candidate_id") != retained["proof"]["candidate_id"]):
        raise RetainedReviewedDraftError("PENDING_ENTITY_COMPLETION_BINDING_DRIFT")
    before, after = saved.get("input_srt_text"), saved.get("output_srt_text")
    if (not isinstance(before, str) or not isinstance(after, str)
            or _sha256_prefixed(before.encode()) != saved.get("input_srt_sha256")
            or _sha256_prefixed(after.encode()) != saved.get("output_srt_sha256")):
        raise RetainedReviewedDraftError("PENDING_ENTITY_COMPLETION_TEXT_DRIFT")
    completed, carried, still_pending = _pending_entity_review_decisions(
        retained, input_srt_text=before, output_srt_text=after,
        findings=pending_entity_review_findings(retained, srt_text=before),
        review=saved.get("normal_correction_review") or {},
        cid=saved["candidate_id"],
    )
    if (still_pending or completed != saved.get("completed") or saved.get("pending") != []
            or carried != saved.get("carried_to_exact_final", [])):
        raise RetainedReviewedDraftError("PENDING_ENTITY_COMPLETION_DECISION_INVALID")


def _request_targets_pending_entity(request, nominee) -> bool:
    """Bind an actual changed occurrence, never just a repeated word."""
    from difflib import SequenceMatcher

    current, proposed = request.get("current_cue"), request.get("proposed_cue")
    if not isinstance(current, str):
        return False
    start, end = nominee["span_start_codepoint"], nominee["span_end_codepoint"]
    if "edit_span_binding" in request:
        from src.autoslice.span_phoneme_resolution import edit_binding
        try:
            binding = edit_binding(request)
        except (KeyError, TypeError, ValueError):
            return False
        if binding is None:
            return False
        left, right = binding["codepoint_span"]
        if max(left, start) >= min(right, end):
            return False
    if not isinstance(proposed, str) or current == proposed:
        suspect = request.get("suspect")
        return (isinstance(suspect, str) and bool(suspect)
                and suspect == nominee["suspect"] and current.count(suspect) == 1
                and current.find(suspect) == start and start + len(suspect) == end)
    # Actual edit blocks also disambiguate requests that predate the native
    # span binding, and avoid
    # treating unchanged text between unrelated edits as a reviewed target.
    return any(
        tag != "equal" and (
            max(left, start) < min(right, end)
            or (left == right and start < left < end)
        )
        for tag, left, right, _new_left, _new_right in
        SequenceMatcher(None, current, proposed, autojunk=False).get_opcodes()
    )


# 行政 依 F7 按类裁定：当前未决裁决交接 G，未审仍拦。
# 以下常量只保留历史原因码，不再决定哪些中间状态可以交接。
_K04_STOPS = frozenset({"CONTINUATION_VALUE_PLAN_REQUIRED", "CONTINUATION_PLAN_ALREADY_UNRESOLVED"})
_EDITORIAL_HANDOFF = "EDITORIAL_EXACT_FINAL_REVIEW_REQUIRED"
_ARGON_HANDOFF = "ARGON_ESCALATION_PENDING_OR_UNRESOLVED"
# admin (stomach cue11/15/26): an OBSERVED Argon hearing refused by
# the trigger identity check before G6 (identity misrejection, no judge yet).
_ARGON_REJECTED_HANDOFF = "ARGON_OBSERVED_IDENTITY_REJECTED"
_ARGON_IDENTITY_REJECTIONS = frozenset({"ARGON_TARGETED_CURRENT_CONTEXT_DRIFT"})


def _argon_observed_identity_rejected(adjudication: Mapping) -> bool:
    argon = adjudication.get("argon_escalation")
    return bool(adjudication.get("reason_code") == "ARGON_INTEGRATION_REJECTED"
                and adjudication.get("detail") in _ARGON_IDENTITY_REJECTIONS
                and isinstance(argon, Mapping) and argon.get("status") == "OBSERVED")


def _exact_final_handoff_reasons(adjudication: object) -> list[str]:
    """Carry a current unresolved adjudication, independent of its stop branch.

    Callers also verify the review row's current-text and exact request binding.
    Argon pending/observed identity rejection retains its missing-authority
    exception. Legacy reason strings remain stable for saved handoffs.
    """

    if (not isinstance(adjudication, Mapping)
            or adjudication.get("schema_version") != "subtitle-span-adjudication.v1"):
        return []
    recovery = adjudication.get("semantic_audio_recovery") or {}
    argon_rejected = _argon_observed_identity_rejected(adjudication)
    argon_pending = (adjudication.get("policy_branch") == _ARGON_HANDOFF
                     or recovery.get("status") == "ARGON_PENDING" or argon_rejected)
    if argon_pending:
        if (adjudication.get("decision_authority") not in (None, "CPA_JUDGE")
                or adjudication.get("witness_authority") not in (None, "EVIDENCE_ONLY")):
            return []
    elif (adjudication.get("decision_authority") != "CPA_JUDGE"
            or adjudication.get("witness_authority") != "EVIDENCE_ONLY"):
        return []
    mutation = adjudication.get("mutation_authority")
    if (adjudication.get("repaired") is not False
            or not isinstance(mutation, Mapping) or mutation.get("status") != "NOT_APPLIED"
            or adjudication.get("timing_immutable") is not True
            or not isinstance(adjudication.get("status"), str) or not adjudication["status"]
            or adjudication["status"] in {"RESOLVED", "APPLIED"}
            # A claimed decided keep belongs to the completed-decision validator
            # below. An invalid completion cannot bypass it via a handoff.
            or adjudication.get("policy_branch") == "JUDGE_KEEPS_CURRENT"):
        return []
    argon = adjudication.get("argon_escalation") or recovery.get("argon_escalation") or {}
    stops = {adjudication.get("reason_code"),
             ((adjudication.get("witness_judge") or {}).get("judge") or {}).get("retry_reason_code"),
             (adjudication.get("text_first_judge") or {}).get("retry_reason_code"),
             (adjudication.get("proposal_bootstrap") or {}).get("reason_code"),
             (adjudication.get("cpa_missing_proposal_convergence") or {}).get("reason_code")}
    reasons = sorted(stops & _K04_STOPS)
    if _EDITORIAL_HANDOFF in {adjudication.get("reason_code"), recovery.get("reason_code"),
                              recovery.get("terminal_reason_code")}:
        reasons.append(_EDITORIAL_HANDOFF)
    if (_ARGON_HANDOFF in {adjudication.get("reason_code"), adjudication.get("policy_branch")}
            or recovery.get("status") == "ARGON_PENDING"
            or (isinstance(argon, Mapping) and argon.get("status") == "PENDING")):
        reasons.append(_ARGON_HANDOFF)
    if argon_rejected:
        reasons.append(_ARGON_REJECTED_HANDOFF)
    if reasons:
        return reasons
    return list(dict.fromkeys([
        "CURRENT_REVIEW_UNRESOLVED_CARRIED",
        *[value for value in (adjudication.get("policy_branch"), recovery.get("reason_code"))
          if isinstance(value, str) and value],
    ]))


def _pending_entity_review_decisions(
    retained, *, input_srt_text, output_srt_text, findings, review, cid,
):
    from src.autoslice.final_review_auditor import audit_correction_mutation_authority
    from src.autoslice.final_review_contract import is_audio_decided_keep_current
    from src.autoslice.acoustic_witness_adjudication import (
        ADJUDICATION_SCHEMA, build_witness_request, parse_judge_choice, valid_witness_evidence,
    )
    from src.autoslice.llm_client import extract_json_object

    input_cues = [cue for cue in parse_srt_cues(input_srt_text) if cue.text.strip()]
    output_cues = [cue for cue in parse_srt_cues(output_srt_text) if cue.text.strip()]
    mutation_audit = audit_correction_mutation_authority(review)
    completed, carried, pending = [], [], []
    for nominee in findings:
        index = nominee["cue_index"]
        cue = input_cues[index - 1]
        decisions = []
        handoff = None
        for row_index, row in enumerate(review.get("findings") or []):
            if not isinstance(row, Mapping) or row.get("cue_index") != index:
                continue
            adjudication = row.get("context_audio_adjudication") or {}
            request = adjudication.get("request") or {}
            request_payload = {k: v for k, v in request.items() if k != "request_sha256"}
            request_sha = _sha256_bytes(json.dumps(request_payload, ensure_ascii=False,
                sort_keys=True, separators=(",", ":")).encode())
            binding = request.get("clip_context_binding") or {}
            expected_sources = sorted(retained["source_media_sha256s"])
            if (row.get("base_text_sha256") != _sha256_bytes(cue.text.encode())
                    or request.get("schema_version") != "subtitle-span-acoustic-check-request.v1"
                    or request.get("request_sha256") != request_sha
                    or request.get("cue_indexes") != [index]
                    or request.get("current_cue") != cue.text
                    or not _request_targets_pending_entity(request, nominee)
                    or request.get("base_text_sha256") != nominee["base_text_sha256"]
                    or request.get("matched_start_ms") != cue.start_ms
                    or request.get("matched_end_ms") != cue.end_ms
                    or request.get("source_media_timeline_offset_ms") != 0
                    or binding.get("candidate_id") != cid
                    or sorted(binding.get("source_media_sha256s") or []) != expected_sources):
                continue
            reasons = _exact_final_handoff_reasons(adjudication)
            if reasons:
                handoff = {
                    "cue_index": index, "request_sha256": request_sha,
                    "review_finding_index": row_index,
                    "physical_target_ms": [cue.start_ms, cue.end_ms],
                    "base_text_sha256": nominee["base_text_sha256"],
                    "suspect": nominee["suspect"],
                    "span_start_codepoint": nominee["span_start_codepoint"],
                    "span_end_codepoint": nominee["span_end_codepoint"],
                    "why": nominee["why"], "handoff_reason_codes": reasons,
                    "mutation_authorized": False, "release_authorized": False,
                }
            judge = (adjudication.get("witness_judge") or {}).get("judge") or {}
            raw_completion = judge.get("span_review_raw_completion")
            if (not isinstance(raw_completion, str)
                    or _sha256_bytes(raw_completion.encode()) != judge.get("completion_sha256")):
                continue
            try:
                raw_judge = extract_json_object(raw_completion)
            except (TypeError, ValueError):
                continue
            if not isinstance(raw_judge, Mapping):
                continue
            # Reuse the exact native decoder, including ranking precedence.
            # Hash-correct raw text cannot authorize a different typed choice.
            raw_choice, raw_ranking = parse_judge_choice(
                raw_judge, set(judge.get("choice_set") or []))
            if (raw_choice != judge.get("choice")
                    or raw_ranking != (judge.get("ranking") or [])
                    or (judge.get("candidate_id") is not None
                        and raw_judge.get("candidate_id", judge["candidate_id"])
                        != judge["candidate_id"])
                    or judge.get("selected_candidate_text", request.get("proposed_cue"))
                    != request.get("proposed_cue")):
                continue
            witness = adjudication.get("verdict") or {}
            try:
                witness_request = build_witness_request(request)
            except (KeyError, TypeError, ValueError):
                continue
            if (not isinstance(witness, Mapping)
                    or not valid_witness_evidence(witness,
                        request_sha256=witness_request["request_sha256"])
                    or witness.get("status") != "OBSERVED"
                    or witness.get("target_audible") is not True):
                continue
            # Reuse current predicates; do not interpret a missing/default
            # historical needs_audio flag as a new explicit negative decision.
            explicit_done = (
                raw_judge.get("needs_audio") is False
                and judge.get("schema_version") == ADJUDICATION_SCHEMA
                and judge.get("status") == "JUDGED"
                and judge.get("check_request_sha256") == request_sha
                and judge.get("needs_audio") is False
                and judge.get("needs_audio_requested") is False
                and (judge.get("needs_audio_request_unresolved") is None
                     or judge.get("needs_audio_request_unresolved") is False)
                and adjudication.get("status") == "OBSERVED"
                and adjudication.get("decision_authority") == "CPA_JUDGE"
                and adjudication.get("witness_authority") == "EVIDENCE_ONLY"
            )
            same = [out for out in output_cues
                    if (out.start_ms, out.end_ms) == (cue.start_ms, cue.end_ms)]
            repaired = (adjudication.get("repaired") is True
                and (adjudication.get("mutation_authority") or {}).get("status") == "PASS"
                and mutation_audit.get("status") == "PASS"
                and judge.get("choice") == "PROPOSED"
                and len(same) == 1 and same[0].text != cue.text
                and same[0].text == request.get("proposed_cue"))
            kept = (is_audio_decided_keep_current(row) and judge.get("choice") == "CURRENT"
                and len(same) == 1 and same[0].text == cue.text)
            if explicit_done and (repaired or kept):
                decisions.append(request_sha)
        if decisions:
            completed.append({"cue_index": index, "request_sha256s": sorted(set(decisions))})
        elif handoff is not None:
            carried.append(handoff)
        else:
            pending.append(dict(nominee))
    return completed, carried, pending


def require_pending_entity_review(
    retained, *, input_srt_text, output_srt_text, findings, review,
    out_root: Path, cid: str, authority: dict,
) -> None:
    """Stop before materialization unless normal CPA resolves every question.

    This records a consumption of a fresh normal correction review. It never
    changes or upgrades the original receipt, nor grants final release. In
    particular an empty/CLEAN scan and noncritical disclosure are insufficient.
    """
    if not findings:
        return
    proof = retained["proof"]
    completed, carried, pending = _pending_entity_review_decisions(
        retained, input_srt_text=input_srt_text, output_srt_text=output_srt_text,
        findings=findings, review=review, cid=cid,
    )
    payload = {
        "schema_version": "retained-entity-current-review.v1",
        "status": "PENDING_CURRENT_REVIEW" if pending else "CURRENT_REVIEW_COMPLETED",
        "candidate_id": cid,
        "parent_authority_sha256": proof["parent_authority_sha256"],
        "input_srt_sha256": _sha256_prefixed(input_srt_text.encode()),
        "output_srt_sha256": _sha256_prefixed(output_srt_text.encode()),
        "input_srt_text": input_srt_text, "output_srt_text": output_srt_text,
        "pending_entity_reviews": proof["pending_entity_reviews"],
        "completed": completed, "pending": pending,
        # Re-adjudicated but undecided by rule: an exact-final question, not a
        # decision. It stays unresolved until exact-final resolves it.
        **({"carried_to_exact_final": carried} if carried else {}),
        "normal_correction_review": dict(review),
        "mutation_authorized": False, "release_authorized": False,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode()
    path = out_root / f"{cid}.retained-entity-review.{_sha256_bytes(raw)}.json"
    _write_history(path, raw, label="PENDING_ENTITY_REVIEW")
    authority["retained_entity_review"] = {
        "status": payload["status"], "path": str(path),
        "sha256": _sha256_prefixed(raw),
        "parent_authority_sha256": proof["parent_authority_sha256"],
        **({"carried_to_exact_final_count": len(carried)} if carried else {}),
        "mutation_authorized": False, "release_authorized": False,
    }
    if pending:
        raise RetainedReviewedDraftError(
            "PENDING_ENTITY_CURRENT_REVIEW_REQUIRED:" + str(path))


__all__ = [
    "RetainedReviewedDraftError",
    "build_retained_source_boundary_context",
    "load_retained_reviewed_draft",
]


def retained_entity_handoff_findings(
    srt_text: str, authority: Mapping[str, object] | None, *, timeline_offset_ms: int = 0,
) -> list[dict[str, object]]:
    """Exact-final priority findings for re-adjudicated, handed-off obligations.

    K14 accepted them only as "re-adjudicated on current evidence and handed
    to exact-final by rule"; this is that hand-off's receiving side. The saved
    completion is re-read by hash, the question is re-bound to the same
    physical utterance on this clock, and its current-review evidence and
    request travel with it. It carries no mutation or release authority: an
    unresolved question keeps exact-final BLOCKED.
    """
    pointer = (authority or {}).get("retained_entity_review")
    if (not isinstance(pointer, Mapping) or pointer.get("status") != "CURRENT_REVIEW_COMPLETED"
            or not pointer.get("carried_to_exact_final_count")):
        return []
    _path, _raw, saved, digest = _read_json(
        pointer.get("path"), pointer.get("sha256"), label="PENDING_ENTITY_COMPLETION")
    carried = saved.get("carried_to_exact_final") or []
    rows = (saved.get("normal_correction_review") or {}).get("findings") or []
    if (saved.get("status") != "CURRENT_REVIEW_COMPLETED"
            or len(carried) != pointer["carried_to_exact_final_count"]):
        raise RetainedReviewedDraftError("RETAINED_ENTITY_HANDOFF_BINDING_DRIFT")
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    findings: list[dict[str, object]] = []
    for entry in carried:
        position = entry.get("review_finding_index")
        row = rows[position] if type(position) is int and 0 <= position < len(rows) else None
        adjudication = (row or {}).get("context_audio_adjudication") or {}
        request = adjudication.get("request") or {}
        if (not isinstance(row, Mapping) or row.get("cue_index") != entry.get("cue_index")
                or row.get("base_text_sha256") != entry.get("base_text_sha256")
                or request.get("request_sha256") != entry.get("request_sha256")
                or _exact_final_handoff_reasons(adjudication) != entry.get("handoff_reason_codes")):
            raise RetainedReviewedDraftError("RETAINED_ENTITY_HANDOFF_BINDING_DRIFT")
        start, end = entry["physical_target_ms"]
        matches = [(index, cue) for index, cue in enumerate(cues, 1)
                   if (cue.start_ms + timeline_offset_ms, cue.end_ms + timeline_offset_ms) == (start, end)]
        if len(matches) != 1:
            # The obligation cannot silently disappear (SUB-18).
            raise RetainedReviewedDraftError("RETAINED_ENTITY_HANDOFF_TARGET_DRIFT")
        index, cue = matches[0]
        same_text = _sha256_bytes(cue.text.encode()) == entry["base_text_sha256"]
        span = (entry["span_start_codepoint"], entry["span_end_codepoint"])
        finding: dict[str, object] = {
            "cue": index, "cue_index": index, "kind": "context",
            "suspect": cue.text[span[0]:span[1]] if same_text else cue.text,
            **({"span_start_codepoint": span[0], "span_end_codepoint": span[1]} if same_text else {}),
            "proposed_full_cue": None, "repair_class": "phonetic",
            "base_text_sha256": _sha256_bytes(cue.text.encode()),
            "why": entry["why"] + " The current review re-adjudicated it and handed it to "
                   "exact-final review (" + ", ".join(entry["handoff_reason_codes"]) + ").",
            "candidate_provenance": {
                "kind": "retained_entity_current_review_handoff",
                "retained_entity_review_sha256": digest,
                "handoff_reason_codes": list(entry["handoff_reason_codes"]),
                "mutation_authorized": False, "release_authorized": False,
            },
        }
        if same_text:
            # The same physical words: keep the current-review evidence and request.
            finding["exact_release_adjudication"] = json.loads(json.dumps(adjudication))
            if isinstance(row.get("_prior_range_acoustic_observations"), list):
                finding["_prior_range_acoustic_observations"] = json.loads(
                    json.dumps(row["_prior_range_acoustic_observations"]))
        findings.append(finding)
    return findings
