"""Candidate audio alignment for an already identified nested source.

This module only measures whether two immutable media files have a stable
linear audio offset.  A successful measurement is still ``EVIDENCE_ONLY``:
it does not establish who is speaking, authorize subtitle deletion, or grant
publication authority.  The caller supplies the source identity; discovery
and acoustic role adjudication remain separate stages.
"""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import hashlib
import json
import math
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from src.autoslice.package_import import atomic_write_json, exclusive_lock

SCHEMA_VERSION = "nested-media-source-mapping.v1"
RESULT_SCHEMA = "nested-media-source-audio-alignment-result.v1"
ALGORITHM_VERSION = "numpy-fft-ncc-preemphasis-v3"
SAMPLE_RATE_HZ = 8000
CHUNK_MS = 8000
CHUNK_STEP_MS = 4000
LOCAL_HALF_WIDTH_MS = 3000
MAX_LAG_RANGE_MS = 20.0
MIN_MEDIAN_ABS_NCC = 0.15
LOCAL_SCOPE_SCHEMA = "nested-source-audio-local-scope.v1"
CURRENT_CUE_SCOPE_SCHEMA = "nested-source-audio-current-cue-scope.v1"
CURRENT_CUE_SCOPE_KIND = "CURRENT_WATCHED_MEDIA_CUE"
CURRENT_CUE_MAPPING_MODE = "CURRENT_CUE_REVERSE_NCC"
OUTER_CONTEXT_REQUEST_SCHEMA = "nested-source-audio-outer-context-request.v1"
OUTER_CONTEXT_RESULT_SCHEMA = "nested-source-audio-outer-context-result.v1"
OUTER_CONTEXT_PROJECTION_SCHEMA = "nested-source-audio-outer-context-projection.v1"
_SAFE_IDENTITY = re.compile(r"[A-Za-z0-9_.:-]{1,240}\Z")
_SHA256 = re.compile(r"(?:sha256:)?[0-9a-fA-F]{64}\Z")


class NestedSourceAudioAlignmentError(ValueError):
    """Input or evidence cannot be safely bound to an alignment request."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_JSON_UNSERIALIZABLE") from exc


def _canonical_sha(value: object) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _file_sha(path: Path) -> str:
    try:
        digest = _evidence_file_sha256(path)
    except OSError as exc:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_MEDIA_UNREADABLE") from exc
    return "sha256:" + digest


def _regular_file(value: str | Path, *, code: str) -> Path:
    candidate = Path(value)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise NestedSourceAudioAlignmentError(code) from exc
    if candidate.is_symlink() or not resolved.is_file():
        raise NestedSourceAudioAlignmentError(code)
    return resolved


def _safe_root(value: str | Path) -> Path:
    root = Path(value).expanduser()
    if root.exists() and root.is_symlink():
        raise NestedSourceAudioAlignmentError("ALIGNMENT_EVIDENCE_ROOT_UNSAFE")
    root.mkdir(parents=True, exist_ok=True)
    resolved = root.resolve()
    if resolved.is_symlink():
        raise NestedSourceAudioAlignmentError("ALIGNMENT_EVIDENCE_ROOT_UNSAFE")
    return resolved


def _normal_interval(value: object) -> list[int | float]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 2:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_INTERVAL_INVALID")
    values: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise NestedSourceAudioAlignmentError("ALIGNMENT_INTERVAL_INVALID")
        number = float(item)
        if not math.isfinite(number):
            raise NestedSourceAudioAlignmentError("ALIGNMENT_INTERVAL_INVALID")
        values.append(number)
    if values[0] < 0 or values[1] <= values[0]:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_INTERVAL_INVALID")
    return [int(number) if number.is_integer() else round(number, 3) for number in values]


def _same_interval(left: Sequence[int | float], right: Sequence[int | float]) -> bool:
    return len(left) == len(right) == 2 and all(
        abs(float(a) - float(b)) <= 1e-6 for a, b in zip(left, right)
    )


def _normal_hash(value: object, *, code: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value.strip()):
        raise NestedSourceAudioAlignmentError(code)
    return "sha256:" + value.strip().removeprefix("sha256:").lower()


def _normal_local_scope(value: object, interval: list[int | float]) -> dict[str, object]:
    """Validate an explicit, descriptor-bound local alignment search scope.

    The scope is deliberately typed by the caller.  The mapper never chooses a
    local region after a failed whole-source scan, and the descriptor binding
    fields make a cached local result unusable for another cue or SRT/frame.
    """
    if not isinstance(value, Mapping) or value.get("schema_version") != LOCAL_SCOPE_SCHEMA:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_LOCAL_SCOPE_INVALID")
    try:
        outer_window = _normal_interval(value.get("outer_window_ms"))
        source_window = _normal_interval(value.get("source_window_ms"))
        context_interval = _normal_interval(value.get("descriptor_context_interval_ms"))
        target_interval = _normal_interval(value.get("target_interval_ms"))
    except NestedSourceAudioAlignmentError as exc:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_LOCAL_SCOPE_INVALID") from exc
    if not _same_interval(context_interval, interval):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_LOCAL_SCOPE_CONTEXT_MISMATCH")
    if not (
        outer_window[0] <= context_interval[0]
        and context_interval[1] <= outer_window[1]
        and context_interval[0] <= target_interval[0] < target_interval[1] <= context_interval[1]
    ):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_LOCAL_SCOPE_TARGET_INVALID")
    if float(outer_window[1]) - float(outer_window[0]) < float(source_window[1]) - float(source_window[0]):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_LOCAL_SCOPE_DURATION_INVALID")

    raw_chunks = value.get("chunk_windows_ms")
    if isinstance(raw_chunks, (str, bytes)) or not isinstance(raw_chunks, Sequence) or len(raw_chunks) < 2:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_LOCAL_SCOPE_CHUNKS_INVALID")
    chunk_windows: list[list[int | float]] = []
    previous_end: float | None = None
    for raw_chunk in raw_chunks:
        try:
            chunk = _normal_interval(raw_chunk)
        except NestedSourceAudioAlignmentError as exc:
            raise NestedSourceAudioAlignmentError("ALIGNMENT_LOCAL_SCOPE_CHUNKS_INVALID") from exc
        if abs(float(chunk[1]) - float(chunk[0]) - CHUNK_MS) > 1e-6:
            raise NestedSourceAudioAlignmentError("ALIGNMENT_LOCAL_SCOPE_CHUNKS_INVALID")
        if not (source_window[0] <= chunk[0] and chunk[1] <= source_window[1]):
            raise NestedSourceAudioAlignmentError("ALIGNMENT_LOCAL_SCOPE_CHUNKS_INVALID")
        if previous_end is not None and float(chunk[0]) < previous_end:
            raise NestedSourceAudioAlignmentError("ALIGNMENT_LOCAL_SCOPE_CHUNKS_OVERLAP")
        previous_end = float(chunk[1])
        chunk_windows.append(chunk)

    observation_id = value.get("observation_id")
    if not isinstance(observation_id, str) or not _SAFE_IDENTITY.fullmatch(observation_id):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_LOCAL_SCOPE_DESCRIPTOR_INVALID")
    frame_path = value.get("caption_frame_path")
    if not isinstance(frame_path, str) or not frame_path or "\n" in frame_path or "\r" in frame_path:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_LOCAL_SCOPE_DESCRIPTOR_INVALID")
    frame_ms = value.get("caption_frame_ms")
    if isinstance(frame_ms, bool) or not isinstance(frame_ms, int) or not target_interval[0] <= frame_ms < target_interval[1]:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_LOCAL_SCOPE_DESCRIPTOR_INVALID")
    target_indexes = value.get("target_cue_indexes")
    if (
        isinstance(target_indexes, (str, bytes))
        or not isinstance(target_indexes, Sequence)
        or not target_indexes
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in target_indexes)
    ):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_LOCAL_SCOPE_DESCRIPTOR_INVALID")
    try:
        final_srt_sha = _normal_hash(value.get("final_srt_sha256"), code="ALIGNMENT_LOCAL_SCOPE_DESCRIPTOR_INVALID")
        frame_sha = _normal_hash(value.get("caption_frame_sha256"), code="ALIGNMENT_LOCAL_SCOPE_DESCRIPTOR_INVALID")
    except NestedSourceAudioAlignmentError as exc:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_LOCAL_SCOPE_DESCRIPTOR_INVALID") from exc

    normalized: dict[str, object] = {
        "schema_version": LOCAL_SCOPE_SCHEMA,
        "outer_window_ms": outer_window,
        "source_window_ms": source_window,
        "chunk_windows_ms": chunk_windows,
        "descriptor_context_interval_ms": context_interval,
        "target_interval_ms": target_interval,
        "target_cue_indexes": [int(item) for item in target_indexes],
        "observation_id": observation_id,
        "caption_frame_path": frame_path,
        "caption_frame_sha256": frame_sha,
        "caption_frame_ms": frame_ms,
        "final_srt_sha256": final_srt_sha,
    }
    if "candidate_id" in value:
        candidate_id = value.get("candidate_id")
        if not isinstance(candidate_id, str) or not _SAFE_IDENTITY.fullmatch(candidate_id):
            raise NestedSourceAudioAlignmentError("ALIGNMENT_LOCAL_SCOPE_DESCRIPTOR_INVALID")
        normalized["candidate_id"] = candidate_id
    return normalized


def _normal_current_cue_scope(
    value: object,
    interval: list[int | float],
    *,
    candidate_id: str | None = None,
    outer_media_sha256: str | None = None,
) -> dict[str, object]:
    """Validate the current, hash-bound cue descriptor for automatic mapping.

    Unlike ``local_scope``, this descriptor does not contain a source window or
    chunk choices.  The mapper derives exactly one fixed outer window and two
    endpoint chunks after probing the current media.  Requiring the current
    frame, SRT, candidate, observation and media hashes here keeps that choice
    bound to the descriptor which authorized it.
    """
    if not isinstance(value, Mapping):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_CURRENT_CUE_SCOPE_INVALID")
    if value.get("schema_version") != CURRENT_CUE_SCOPE_SCHEMA:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_CURRENT_CUE_SCOPE_INVALID")
    if value.get("kind") != CURRENT_CUE_SCOPE_KIND:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_CURRENT_CUE_SCOPE_KIND_INVALID")
    try:
        context_interval = _normal_interval(value.get("descriptor_context_interval_ms"))
        target_interval = _normal_interval(value.get("target_interval_ms"))
    except NestedSourceAudioAlignmentError as exc:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_CURRENT_CUE_SCOPE_INVALID") from exc
    if not _same_interval(context_interval, interval):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_CURRENT_CUE_SCOPE_CONTEXT_MISMATCH")
    if not (
        context_interval[0] <= target_interval[0]
        < target_interval[1] <= context_interval[1]
    ):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_CURRENT_CUE_SCOPE_TARGET_INVALID")

    observation_id = value.get("observation_id")
    if not isinstance(observation_id, str) or not _SAFE_IDENTITY.fullmatch(observation_id):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_CURRENT_CUE_SCOPE_DESCRIPTOR_INVALID")
    scope_candidate = value.get("candidate_id")
    if not isinstance(scope_candidate, str) or not _SAFE_IDENTITY.fullmatch(scope_candidate):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_CURRENT_CUE_SCOPE_DESCRIPTOR_INVALID")
    if candidate_id is not None:
        if not isinstance(candidate_id, str) or not _SAFE_IDENTITY.fullmatch(candidate_id):
            raise NestedSourceAudioAlignmentError("ALIGNMENT_CURRENT_CUE_SCOPE_DESCRIPTOR_INVALID")
        if scope_candidate != candidate_id:
            raise NestedSourceAudioAlignmentError("ALIGNMENT_CURRENT_CUE_SCOPE_CANDIDATE_MISMATCH")

    frame_path = value.get("caption_frame_path")
    if not isinstance(frame_path, str) or not frame_path or "\n" in frame_path or "\r" in frame_path:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_CURRENT_CUE_SCOPE_DESCRIPTOR_INVALID")
    frame_ms = value.get("caption_frame_ms")
    if (
        isinstance(frame_ms, bool)
        or not isinstance(frame_ms, int)
        or not target_interval[0] <= frame_ms < target_interval[1]
    ):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_CURRENT_CUE_SCOPE_FRAME_INVALID")
    target_indexes = value.get("target_cue_indexes")
    if (
        isinstance(target_indexes, (str, bytes))
        or not isinstance(target_indexes, Sequence)
        or not target_indexes
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in target_indexes)
    ):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_CURRENT_CUE_SCOPE_TARGET_INVALID")
    try:
        frame_sha = _normal_hash(
            value.get("caption_frame_sha256"), code="ALIGNMENT_CURRENT_CUE_SCOPE_DESCRIPTOR_INVALID",
        )
        final_srt_sha = _normal_hash(
            value.get("final_srt_sha256"), code="ALIGNMENT_CURRENT_CUE_SCOPE_DESCRIPTOR_INVALID",
        )
        outer_sha = _normal_hash(
            value.get("outer_media_sha256"), code="ALIGNMENT_CURRENT_CUE_SCOPE_DESCRIPTOR_INVALID",
        )
    except NestedSourceAudioAlignmentError as exc:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_CURRENT_CUE_SCOPE_DESCRIPTOR_INVALID") from exc
    if outer_media_sha256 is not None:
        try:
            expected_outer_sha = _normal_hash(
                outer_media_sha256, code="ALIGNMENT_CURRENT_CUE_SCOPE_MEDIA_MISMATCH",
            )
        except NestedSourceAudioAlignmentError as exc:
            raise NestedSourceAudioAlignmentError("ALIGNMENT_CURRENT_CUE_SCOPE_MEDIA_MISMATCH") from exc
        if outer_sha != expected_outer_sha:
            raise NestedSourceAudioAlignmentError("ALIGNMENT_CURRENT_CUE_SCOPE_MEDIA_MISMATCH")

    normalized: dict[str, object] = {
        "schema_version": CURRENT_CUE_SCOPE_SCHEMA,
        "kind": CURRENT_CUE_SCOPE_KIND,
        "candidate_id": scope_candidate,
        "observation_id": observation_id,
        "descriptor_context_interval_ms": context_interval,
        "target_interval_ms": target_interval,
        "target_cue_indexes": [int(item) for item in target_indexes],
        "caption_frame_path": frame_path,
        "caption_frame_sha256": frame_sha,
        "caption_frame_ms": frame_ms,
        "final_srt_sha256": final_srt_sha,
        "outer_media_sha256": outer_sha,
    }
    return normalized


def _validate_local_scope_bounds(
    scope: Mapping[str, object], *, outer_duration_ms: float, source_duration_ms: float,
) -> None:
    outer_window = scope["outer_window_ms"]
    source_window = scope["source_window_ms"]
    if (
        float(outer_window[1]) > outer_duration_ms + 1e-3
        or float(source_window[1]) > source_duration_ms + 1e-3
    ):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_LOCAL_SCOPE_OUT_OF_BOUNDS")


def _source_identity(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_SOURCE_IDENTITY_INVALID")
    bvid, cid = value.get("bvid"), value.get("cid")
    if not isinstance(bvid, str) or not bvid.strip() or not _SAFE_IDENTITY.fullmatch(bvid.strip()):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_SOURCE_IDENTITY_INVALID")
    if isinstance(cid, bool) or not isinstance(cid, (str, int)):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_SOURCE_IDENTITY_INVALID")
    cid_text = str(cid).strip()
    if not cid_text or not _SAFE_IDENTITY.fullmatch(cid_text):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_SOURCE_IDENTITY_INVALID")
    return {"bvid": bvid.strip(), "cid": cid_text}


def _write_json_once(path: Path, value: Mapping[str, object]) -> str:
    """Write one evidence document without silently replacing an old one."""

    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise NestedSourceAudioAlignmentError("ALIGNMENT_EVIDENCE_PATH_DRIFT")
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise NestedSourceAudioAlignmentError("ALIGNMENT_EVIDENCE_PATH_DRIFT") from exc
        if existing != dict(value):
            raise NestedSourceAudioAlignmentError("ALIGNMENT_EVIDENCE_PATH_DRIFT")
        return _file_sha(path)
    atomic_write_json(path, value)
    return _file_sha(path)


def _read_json(path: Path, *, code: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NestedSourceAudioAlignmentError(code) from exc
    if not isinstance(value, Mapping):
        raise NestedSourceAudioAlignmentError(code)
    return dict(value)


def _outer_context_entry(
    value: Mapping[str, object],
    name: str,
    *,
    path_code: str,
    hash_code: str,
) -> tuple[Path, str]:
    """Read one path/hash binding from the explicit outer-context contract."""

    raw = value.get(name)
    if raw is None:
        path_value = value.get(f"{name}_path")
        hash_value = value.get(f"{name}_sha256")
    elif isinstance(raw, Mapping):
        path_value = raw.get("path")
        hash_value = raw.get("sha256")
    else:
        raise NestedSourceAudioAlignmentError(path_code)
    if not isinstance(path_value, str) or not path_value or "\n" in path_value or "\r" in path_value:
        raise NestedSourceAudioAlignmentError(path_code)
    try:
        path = _regular_file(path_value, code=path_code)
        expected = _normal_hash(hash_value, code=hash_code)
    except NestedSourceAudioAlignmentError:
        raise
    except (TypeError, ValueError) as exc:
        raise NestedSourceAudioAlignmentError(hash_code) from exc
    if _file_sha(path) != expected:
        raise NestedSourceAudioAlignmentError(hash_code)
    return path, expected


def _probe_duration_ms_without_artifact(media: Path) -> float:
    """Probe one media duration without creating a second cache artifact."""

    argv = [
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(media),
    ]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_FFPROBE_UNAVAILABLE") from exc
    if completed.returncode != 0:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_FFPROBE_FAILED")
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, ValueError) as exc:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_FFPROBE_INVALID") from exc
    if not isinstance(payload, Mapping):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_FFPROBE_INVALID")
    duration = _probe_duration_ms({"payload": payload})
    if duration is None:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_FFPROBE_DURATION_INVALID")
    return duration


def _extract_outer_context_frame(media: Path, frame_ms: int) -> bytes:
    """Extract a context frame with the existing native frame helper."""

    try:
        from src.autoslice.cpa_frame_witness import _extract_frame_jpeg

        frame = _extract_frame_jpeg(media, frame_ms)
    except Exception as exc:  # pragma: no cover - native ffmpeg errors are environment-specific
        raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_FRAME_EXTRACT_FAILED") from exc
    if not isinstance(frame, bytes) or not frame:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_FRAME_INVALID")
    return frame


def _project_outer_context_srt(
    current_srt: Path,
    output: Path,
    *,
    offset_ms: int,
) -> tuple[str, dict[str, object]]:
    """Render the current SRT in context coordinates and prove its inverse."""

    try:
        from src.autoslice.source_context_executor import _parse_srt, _write_relative_srt

        cues = _parse_srt(current_srt)
        shifted = [
            replace(
                cue,
                source_start_ms=int(cue.source_start_ms + offset_ms),
                source_end_ms=int(cue.source_end_ms + offset_ms),
            )
            for cue in cues
        ]
        _write_relative_srt(shifted, 0, output)
        rendered = _parse_srt(output)
    except NestedSourceAudioAlignmentError:
        raise
    except Exception as exc:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_SRT_RENDER_FAILED") from exc
    if len(rendered) != len(cues):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_SRT_ROUNDTRIP_MISMATCH")
    for original, projected in zip(cues, rendered):
        if (
            original.text != projected.text
            or projected.source_start_ms != original.source_start_ms + offset_ms
            or projected.source_end_ms != original.source_end_ms + offset_ms
        ):
            raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_SRT_ROUNDTRIP_MISMATCH")
    proof = {
        "schema_version": OUTER_CONTEXT_PROJECTION_SCHEMA,
        "offset_ms": offset_ms,
        "cue_count": len(cues),
        "text_preserved": True,
        "timing_inverse": "projected_time_minus_offset_equals_current_time",
        "current_srt_path": str(current_srt),
        "current_srt_sha256": _file_sha(current_srt),
        "context_srt_path": str(output),
        "context_srt_sha256": _file_sha(output),
    }
    return _file_sha(output), proof


def _validate_outer_context_inputs(
    *,
    outer: Path,
    outer_sha: str,
    interval: list[int | float],
    scope: Mapping[str, object],
    outer_context: Mapping[str, object],
) -> dict[str, object]:
    """Validate the hash-bound recut/context/current-SRT contract."""

    if not isinstance(outer_context, Mapping):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_INVALID")
    context_media, context_sha = _outer_context_entry(
        outer_context, "context_media",
        path_code="ALIGNMENT_OUTER_CONTEXT_MEDIA_INVALID",
        hash_code="ALIGNMENT_OUTER_CONTEXT_MEDIA_SHA256_MISMATCH",
    )
    current_srt, current_srt_sha = _outer_context_entry(
        outer_context, "current_srt",
        path_code="ALIGNMENT_OUTER_CONTEXT_SRT_INVALID",
        hash_code="ALIGNMENT_OUTER_CONTEXT_SRT_SHA256_MISMATCH",
    )
    provenance_path, provenance_sha = _outer_context_entry(
        outer_context, "recut_provenance",
        path_code="ALIGNMENT_OUTER_CONTEXT_PROVENANCE_INVALID",
        hash_code="ALIGNMENT_OUTER_CONTEXT_PROVENANCE_SHA256_MISMATCH",
    )
    if _file_sha(outer) != outer_sha:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_MEDIA_CHANGED_BEFORE_MAPPING")

    provenance = _read_json(
        provenance_path, code="ALIGNMENT_OUTER_CONTEXT_PROVENANCE_INVALID",
    )
    if provenance.get("schema_version") != "lidousha-speaker-recut-provenance.v1":
        raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_PROVENANCE_SCHEMA_INVALID")
    final = provenance.get("final_recut")
    if not isinstance(final, Mapping):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_PROVENANCE_FINAL_INVALID")
    try:
        final_source = _regular_file(
            str(final.get("source_path") or ""),
            code="ALIGNMENT_OUTER_CONTEXT_PROVENANCE_SOURCE_INVALID",
        )
        final_output = _regular_file(
            str(final.get("output_path") or ""),
            code="ALIGNMENT_OUTER_CONTEXT_PROVENANCE_OUTPUT_INVALID",
        )
        final_source_sha = _normal_hash(
            final.get("source_sha256"), code="ALIGNMENT_OUTER_CONTEXT_PROVENANCE_SOURCE_SHA_INVALID",
        )
        final_output_sha = _normal_hash(
            final.get("output_sha256"), code="ALIGNMENT_OUTER_CONTEXT_PROVENANCE_OUTPUT_SHA_INVALID",
        )
    except NestedSourceAudioAlignmentError:
        raise
    if final_source != context_media or final_output != outer:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_PROVENANCE_PATH_MISMATCH")
    if final_source_sha != context_sha or final_output_sha != outer_sha:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_PROVENANCE_MEDIA_MISMATCH")
    start_ms = final.get("start_ms")
    end_ms = final.get("end_ms")
    if (
        isinstance(start_ms, bool) or not isinstance(start_ms, int)
        or isinstance(end_ms, bool) or not isinstance(end_ms, int)
        or start_ms < 0 or end_ms <= start_ms
    ):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_PROVENANCE_INTERVAL_INVALID")
    recut_duration = end_ms - start_ms
    if float(interval[1]) > recut_duration + 1e-6:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_INTERVAL_OUT_OF_RECUT")

    frame_path = _regular_file(
        str(scope.get("caption_frame_path") or ""),
        code="ALIGNMENT_OUTER_CONTEXT_FRAME_INVALID",
    )
    if _file_sha(frame_path) != scope.get("caption_frame_sha256"):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_FRAME_SHA256_MISMATCH")
    if scope.get("final_srt_sha256") != current_srt_sha:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_SCOPE_SRT_MISMATCH")

    try:
        from src.autoslice.source_context_executor import _parse_srt

        cues = _parse_srt(current_srt)
    except Exception as exc:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_SRT_INVALID") from exc
    if not cues:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_SRT_EMPTY")
    for cue in cues:
        if cue.source_start_ms < 0 or cue.source_end_ms > recut_duration:
            raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_SRT_OUT_OF_RECUT")
    target_indexes = scope.get("target_cue_indexes")
    if not isinstance(target_indexes, Sequence) or isinstance(target_indexes, (str, bytes)):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_TARGET_INVALID")
    cue_by_index = {
        int(cue.cue_id): cue
        for cue in cues
        if str(cue.cue_id).isdigit()
    }
    selected = [cue_by_index.get(int(index)) for index in target_indexes]
    if any(cue is None for cue in selected):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_TARGET_NOT_IN_SRT")
    selected_cues = [cue for cue in selected if cue is not None]
    target_interval = scope.get("target_interval_ms")
    if not isinstance(target_interval, Sequence) or isinstance(target_interval, (str, bytes)):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_TARGET_INVALID")
    expected_target = [
        min(cue.source_start_ms for cue in selected_cues),
        max(cue.source_end_ms for cue in selected_cues),
    ]
    if not _same_interval(target_interval, expected_target):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_TARGET_SRT_MISMATCH")
    if _file_sha(current_srt) != current_srt_sha:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_SRT_CHANGED")
    return {
        "context_media": {"path": str(context_media), "sha256": context_sha, "bytes": context_media.stat().st_size},
        "current_outer": {"path": str(outer), "sha256": outer_sha, "bytes": outer.stat().st_size},
        "current_srt": {"path": str(current_srt), "sha256": current_srt_sha, "bytes": current_srt.stat().st_size},
        "recut_provenance": {
            "path": str(provenance_path), "sha256": provenance_sha, "bytes": provenance_path.stat().st_size,
        },
        "final_recut": {
            "source_path": str(final_source), "source_sha256": final_source_sha,
            "start_ms": start_ms, "end_ms": end_ms,
            "output_path": str(final_output), "output_sha256": final_output_sha,
        },
        "scope_frame": {"path": str(frame_path), "sha256": scope["caption_frame_sha256"], "frame_ms": scope["caption_frame_ms"]},
        "cue_count": len(cues),
    }


def _run_probe(media: Path, output: Path) -> dict[str, object]:
    argv = [
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(media),
    ]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_FFPROBE_UNAVAILABLE") from exc
    if completed.returncode != 0:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_FFPROBE_FAILED")
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, ValueError) as exc:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_FFPROBE_INVALID") from exc
    if not isinstance(payload, Mapping):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_FFPROBE_INVALID")
    _write_json_once(output, dict(payload))
    return {
        "argv": argv,
        "returncode": int(completed.returncode),
        "path": str(output),
        "sha256": _file_sha(output),
        "payload": dict(payload),
    }


def _run_extract(media: Path, output: Path, *, temp: Path) -> dict[str, object]:
    if output.exists() or output.is_symlink() or temp.exists() or temp.is_symlink():
        raise NestedSourceAudioAlignmentError("ALIGNMENT_AUDIO_OUTPUT_ALREADY_EXISTS")
    argv = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(media),
        "-map", "0:a:0", "-ar", str(SAMPLE_RATE_HZ), "-ac", "2", "-c:a", "pcm_f32le",
        "-f", "f32le", str(temp),
    ]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_FFMPEG_UNAVAILABLE") from exc
    if completed.returncode != 0:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_FFMPEG_FAILED")
    if not temp.is_file() or temp.is_symlink():
        raise NestedSourceAudioAlignmentError("ALIGNMENT_AUDIO_OUTPUT_MISSING")
    try:
        temp.replace(output)
    except OSError as exc:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_AUDIO_OUTPUT_COMMIT_FAILED") from exc
    raw_sha = _file_sha(output)
    size = output.stat().st_size
    if size <= 0 or size % (2 * 4) != 0:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_AUDIO_PCM_INVALID")
    return {
        "argv": argv,
        "returncode": int(completed.returncode),
        "path": str(output),
        "sha256": raw_sha,
        "bytes": size,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "channels": 2,
        "sample_count_per_channel": size // (2 * 4),
    }


def _probe_duration_ms(probe: Mapping[str, object]) -> float | None:
    fmt = probe.get("payload")
    if not isinstance(fmt, Mapping):
        return None
    format_row = fmt.get("format")
    if not isinstance(format_row, Mapping):
        return None
    try:
        seconds = float(format_row.get("duration"))
    except (TypeError, ValueError):
        return None
    return seconds * 1000.0 if math.isfinite(seconds) and seconds > 0 else None


def _load_pcm(path: Path) -> dict[str, np.ndarray]:
    try:
        raw = np.fromfile(path, dtype="<f4")
    except (OSError, ValueError) as exc:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_AUDIO_PCM_INVALID") from exc
    if raw.size < SAMPLE_RATE_HZ or raw.size % 2:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_AUDIO_PCM_INVALID")
    stereo = raw.reshape((-1, 2)).astype(np.float64)
    left, right = stereo[:, 0], stereo[:, 1]
    return {"left": left, "right": right, "mid": 0.5 * (left + right), "side": 0.5 * (left - right)}


def _whiten(values: np.ndarray) -> np.ndarray:
    if values.size == 0:
        return values.astype(np.float64, copy=False)
    result = np.empty_like(values, dtype=np.float64)
    result[0] = 0.0
    result[1:] = values[1:] - 0.97 * values[:-1]
    result -= float(np.mean(result))
    scale = float(np.std(result))
    return result / scale if scale > 1e-12 else np.zeros_like(result)


def _valid_ncc(long: np.ndarray, short: np.ndarray) -> np.ndarray:
    """Return normalized dot products for every valid long-window offset."""

    if short.size > long.size or short.size == 0:
        return np.empty(0, dtype=np.float64)
    long_w = _whiten(long)
    short_w = _whiten(short)
    n, m = long_w.size, short_w.size
    nfft = 1 << (n + m - 1).bit_length()
    correlation = np.fft.irfft(
        np.fft.rfft(long_w, nfft) * np.conj(np.fft.rfft(short_w, nfft)), nfft,
    )[: n - m + 1]
    prefix = np.concatenate(([0.0], np.cumsum(long_w * long_w)))
    energy = prefix[m:] - prefix[:-m]
    denominator = np.sqrt(np.maximum(energy, 1e-30) * float(np.dot(short_w, short_w)))
    return correlation / denominator


def _best_peak(scores: np.ndarray, *, separation_samples: int = SAMPLE_RATE_HZ // 2) -> dict[str, object]:
    if scores.size == 0 or not np.all(np.isfinite(scores)):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_NCC_INVALID")
    # Search the whole curve after excluding each chosen peak's neighbourhood.
    # Taking only the largest samples first can hide a second strong match when
    # all those samples cluster around the first peak.
    remaining = np.abs(scores).copy()
    peaks: list[dict[str, object]] = []
    for _ in range(5):
        index = int(np.argmax(remaining))
        if remaining[index] < 0:
            break
        peaks.append({
            "lag_samples": index,
            "lag_ms": round(index * 1000.0 / SAMPLE_RATE_HZ, 3),
            "ncc": round(float(scores[index]), 9),
            "abs_ncc": round(abs(float(scores[index])), 9),
        })
        radius = max(1, separation_samples)
        remaining[max(0, index - radius + 1):index + radius] = -1
    if not peaks:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_NCC_INVALID")
    return {"best": peaks[0], "peaks": peaks, "valid_lag_count": int(scores.size)}


def _peak_ambiguous(summary: Mapping[str, object]) -> bool:
    peaks = summary.get("peaks")
    if not isinstance(peaks, list) or len(peaks) < 2 or not isinstance(peaks[0], Mapping):
        return False
    top = float(peaks[0].get("abs_ncc", 0.0))
    if top < MIN_MEDIAN_ABS_NCC:
        return False
    return any(
        isinstance(peak, Mapping)
        and float(peak.get("abs_ncc", 0.0)) >= max(MIN_MEDIAN_ABS_NCC, top - 0.05)
        for peak in peaks[1:]
    )


def _local_search(
    long: np.ndarray, short: np.ndarray, expected_sample: float,
) -> dict[str, object]:
    half_width = round(LOCAL_HALF_WIDTH_MS * SAMPLE_RATE_HZ / 1000.0)
    expected = int(round(expected_sample))
    lower = max(0, expected - half_width)
    upper = min(long.size - short.size, expected + half_width)
    if upper < lower:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_LOCAL_WINDOW_INVALID")
    region = long[lower : upper + short.size]
    scores = _valid_ncc(region, short)
    summary = _best_peak(scores, separation_samples=max(1, SAMPLE_RATE_HZ // 4))
    peak = summary["best"]
    peak = dict(peak)
    peak["lag_samples"] = int(peak["lag_samples"]) + lower
    peak["lag_ms"] = round(float(peak["lag_samples"]) * 1000.0 / SAMPLE_RATE_HZ, 3)
    peak["expected_samples"] = expected
    peak["residual_ms"] = round(
        (int(peak["lag_samples"]) - expected) * 1000.0 / SAMPLE_RATE_HZ, 3,
    )
    peak["ambiguous_peak"] = _peak_ambiguous(summary)
    return peak


def _high_energy_chunks(side: np.ndarray) -> list[tuple[int, float]]:
    chunk = round(CHUNK_MS * SAMPLE_RATE_HZ / 1000.0)
    step = round(CHUNK_STEP_MS * SAMPLE_RATE_HZ / 1000.0)
    if side.size < chunk:
        return []
    candidates = [
        (start, float(np.sqrt(np.mean(side[start : start + chunk] ** 2))))
        for start in range(0, side.size - chunk + 1, step)
    ]
    selected: list[tuple[int, float]] = []
    for start, energy in sorted(candidates, key=lambda row: row[1], reverse=True):
        if all(abs(start - prior) > chunk for prior, _ in selected):
            selected.append((start, energy))
        if len(selected) >= 10:
            break
    return sorted(selected)


def _round_ms(value: float) -> int | float:
    rounded = round(float(value), 3)
    return int(rounded) if rounded.is_integer() else rounded


def _analyze(
    *, outer: Mapping[str, np.ndarray], source: Mapping[str, np.ndarray], interval: list[int | float],
    extraction: Mapping[str, object], source_identity: Mapping[str, str],
    chunk_starts: Sequence[int] | None = None,
    target_interval: list[int | float] | None = None,
) -> tuple[bool, str, dict[str, object], dict[str, object] | None]:
    outer_count = outer["side"].size
    source_count = source["side"].size
    outer_duration_ms = outer_count * 1000.0 / SAMPLE_RATE_HZ
    source_duration_ms = source_count * 1000.0 / SAMPLE_RATE_HZ
    interval_start, interval_end = (float(item) for item in interval)
    if interval_end > outer_duration_ms + 1e-3:
        return False, "ALIGNMENT_TARGET_OUTSIDE_OUTER", {
            "outer_duration_ms": _round_ms(outer_duration_ms),
            "source_duration_ms": _round_ms(source_duration_ms),
        }, None
    if source_duration_ms > outer_duration_ms + 1e-3:
        return False, "ALIGNMENT_SOURCE_LONGER_THAN_OUTER", {
            "outer_duration_ms": _round_ms(outer_duration_ms),
            "source_duration_ms": _round_ms(source_duration_ms),
        }, None
    fit_interval = interval if target_interval is None else _normal_interval(target_interval)
    if not (
        interval_start <= fit_interval[0] < fit_interval[1] <= interval_end
    ):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_TARGET_INTERVAL_INVALID")
    fit_interval_start, fit_interval_end = (float(item) for item in fit_interval)

    global_rows: dict[str, object] = {}
    for representation in ("side", "mid"):
        scores = _valid_ncc(outer[representation], source[representation])
        global_rows[representation] = _best_peak(scores)
    global_side = global_rows["side"]["best"]
    global_mid = global_rows["mid"]["best"]
    global_lag_diff = abs(float(global_side["lag_ms"]) - float(global_mid["lag_ms"]))
    global_ok = (
        global_lag_diff <= MAX_LAG_RANGE_MS
        and float(global_side["abs_ncc"]) >= MIN_MEDIAN_ABS_NCC
        and float(global_mid["abs_ncc"]) >= MIN_MEDIAN_ABS_NCC
        and not _peak_ambiguous(global_rows["side"])
        and not _peak_ambiguous(global_rows["mid"])
    )

    if chunk_starts is None:
        chunks = _high_energy_chunks(source["side"])
    else:
        duration_samples = round(CHUNK_MS * SAMPLE_RATE_HZ / 1000.0)
        chunks = []
        for source_start in chunk_starts:
            if (
                isinstance(source_start, bool)
                or not isinstance(source_start, int)
                or source_start < 0
                or source_start + duration_samples > source_count
            ):
                raise NestedSourceAudioAlignmentError("ALIGNMENT_LOCAL_SCOPE_CHUNKS_INVALID")
            chunks.append((source_start, float(np.sqrt(np.mean(
                source["side"][source_start : source_start + duration_samples] ** 2
            )))))
    if len(chunks) < 2:
        return False, "ALIGNMENT_NOT_ENOUGH_INDEPENDENT_CHUNKS", {
            "global": global_rows, "outer_duration_ms": _round_ms(outer_duration_ms),
            "source_duration_ms": _round_ms(source_duration_ms),
        }, None
    local_rows: list[dict[str, object]] = []
    for source_start, energy in chunks:
        duration_samples = round(CHUNK_MS * SAMPLE_RATE_HZ / 1000.0)
        expected = float(global_side["lag_samples"]) + source_start
        side_peak = _local_search(
            outer["side"], source["side"][source_start : source_start + duration_samples], expected,
        )
        mid_peak = _local_search(
            outer["mid"], source["mid"][source_start : source_start + duration_samples], expected,
        )
        side_lag_ms = float(side_peak["lag_ms"]) - source_start * 1000.0 / SAMPLE_RATE_HZ
        mid_lag_ms = float(mid_peak["lag_ms"]) - source_start * 1000.0 / SAMPLE_RATE_HZ
        local_rows.append({
            "source_start_ms": _round_ms(source_start * 1000.0 / SAMPLE_RATE_HZ),
            "duration_ms": CHUNK_MS,
            "source_side_rms": round(energy, 12),
            "side": side_peak,
            "mid": mid_peak,
            "side_lag_ms": _round_ms(side_lag_ms),
            "mid_lag_ms": _round_ms(mid_lag_ms),
            "representation_lag_delta_ms": round(side_lag_ms - mid_lag_ms, 3),
            "observed_lag_ms": _round_ms(side_lag_ms),
            "abs_ncc": side_peak["abs_ncc"],
        })
    local_lags = np.asarray([float(row["observed_lag_ms"]) for row in local_rows])
    local_scores = np.asarray([float(row["abs_ncc"]) for row in local_rows])
    local_mid_lags = np.asarray([float(row["mid_lag_ms"]) for row in local_rows])
    median_lag = float(np.median(local_lags))
    median_mid_lag = float(np.median(local_mid_lags))
    local_range = float(np.max(local_lags) - np.min(local_lags))
    local_mid_range = float(np.max(local_mid_lags) - np.min(local_mid_lags))
    local_ok = (
        local_range <= MAX_LAG_RANGE_MS
        and local_mid_range <= MAX_LAG_RANGE_MS
        and abs(median_lag - median_mid_lag) <= MAX_LAG_RANGE_MS
        and float(np.median(local_scores)) >= MIN_MEDIAN_ABS_NCC
        and all(not bool(row["side"].get("ambiguous_peak")) for row in local_rows)
        and all(not bool(row["mid"].get("ambiguous_peak")) for row in local_rows)
        and all(abs(float(row["representation_lag_delta_ms"])) <= MAX_LAG_RANGE_MS for row in local_rows)
    )

    source_start_sample = round((fit_interval_start - median_lag) * SAMPLE_RATE_HZ / 1000.0)
    target_samples = round((fit_interval_end - fit_interval_start) * SAMPLE_RATE_HZ / 1000.0)
    target_valid = source_start_sample >= 0 and source_start_sample + target_samples <= source_count
    target_fit: dict[str, object] = {"source_window_candidate_ms": [
        _round_ms(source_start_sample * 1000.0 / SAMPLE_RATE_HZ),
        _round_ms((source_start_sample + target_samples) * 1000.0 / SAMPLE_RATE_HZ),
    ]}
    target_ok = False
    target_lag = median_lag
    if target_valid and target_samples >= SAMPLE_RATE_HZ:
        source_segment = source["side"][source_start_sample : source_start_sample + target_samples]
        target_peak = _local_search(
            outer["side"], source_segment,
            fit_interval_start * SAMPLE_RATE_HZ / 1000.0,
        )
        target_mid_peak = _local_search(
            outer["mid"], source["mid"][source_start_sample : source_start_sample + target_samples],
            fit_interval_start * SAMPLE_RATE_HZ / 1000.0,
        )
        target_lag = float(target_peak["lag_ms"]) - source_start_sample * 1000.0 / SAMPLE_RATE_HZ
        target_fit.update(
            side=target_peak, mid=target_mid_peak,
            observed_lag_ms=_round_ms(target_lag),
            target_outer_interval_ms=fit_interval,
            target_source_interval_ms=[
                _round_ms(fit_interval_start - target_lag),
                _round_ms(fit_interval_end - target_lag),
            ],
        )
        target_ok = (
            float(target_peak["abs_ncc"]) >= MIN_MEDIAN_ABS_NCC
            and float(target_mid_peak["abs_ncc"]) >= MIN_MEDIAN_ABS_NCC
            and not bool(target_peak.get("ambiguous_peak"))
            and not bool(target_mid_peak.get("ambiguous_peak"))
            and abs(float(target_peak["lag_ms"]) - fit_interval_start) <= MAX_LAG_RANGE_MS
            and abs(float(target_mid_peak["lag_ms"]) - fit_interval_start) <= MAX_LAG_RANGE_MS
            and abs(target_lag - median_lag) <= MAX_LAG_RANGE_MS
        )
    else:
        target_fit["reason_code"] = "ALIGNMENT_TARGET_SOURCE_WINDOW_INVALID"

    evidence: dict[str, object] = {
        "algorithm_version": ALGORITHM_VERSION,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "representations": ["side", "mid"],
        "extraction": dict(extraction),
        "global": global_rows,
        "local_chunks": local_rows,
        "target_fit": target_fit,
        "decision_metrics": {
            "global_lag_delta_ms": round(global_lag_diff, 3),
            "global_supported": global_ok,
            "local_lag_median_ms": round(median_lag, 3),
            "local_lag_range_ms": [round(float(np.min(local_lags)), 3), round(float(np.max(local_lags)), 3)],
            "local_mid_lag_range_ms": [
                round(float(np.min(local_mid_lags)), 3), round(float(np.max(local_mid_lags)), 3),
            ],
            "local_abs_ncc_median": round(float(np.median(local_scores)), 9),
            "target_supported": target_ok,
            "source_duration_ms": _round_ms(source_duration_ms),
            "outer_duration_ms": _round_ms(outer_duration_ms),
        },
        "chunk_selection": {
            "mode": "EXPLICIT" if chunk_starts is not None else "HIGH_ENERGY",
            "source_starts_ms": [
                _round_ms(start * 1000.0 / SAMPLE_RATE_HZ) for start, _ in chunks
            ],
        },
        "authority": "EVIDENCE_ONLY",
        "source_role_authority": False,
        "mutation_authorized": False,
    }
    if not global_ok:
        return False, "ALIGNMENT_GLOBAL_WEAK_OR_INCONSISTENT", evidence, None
    if not local_ok:
        return False, "ALIGNMENT_LOCAL_WEAK_NONLINEAR_OR_INCONSISTENT", evidence, None
    if not target_ok:
        return False, "ALIGNMENT_TARGET_WINDOW_UNSUPPORTED", evidence, None
    mapping_lag = float(target_lag)
    source_window = [
        _round_ms(interval_start - mapping_lag),
        _round_ms(interval_end - mapping_lag),
    ]
    if source_window[0] < 0 or float(source_window[1]) > source_duration_ms + 1e-3:
        return False, "ALIGNMENT_TARGET_SOURCE_WINDOW_INVALID", evidence, None
    # Derive the serialized lag from the serialized source start so the
    # existing consumer's exact window/lag invariant survives decimal rounding.
    mapping_lag = _round_ms(interval_start - float(source_window[0]))
    mapping = {
        "schema_version": SCHEMA_VERSION,
        "status": "EVIDENCE_ONLY",
        "media": {
            "outer": {"path": extraction["outer_media"]["path"], "sha256": extraction["outer_media"]["sha256"]},
            "inner": {
                "path": extraction["source_media"]["path"], "sha256": extraction["source_media"]["sha256"],
                "bvid": source_identity["bvid"], "cid": source_identity["cid"],
            },
        },
        "lineage_mapping": {
            "outer_window_ms": list(interval),
            "source_window_ms": source_window,
            "lag_ms": _round_ms(mapping_lag),
        },
        "alignment_evidence": evidence,
        "alignment_candidate": "LINEAR_AUDIO_OFFSET_CANDIDATE",
        "source_role_authority": False,
        "mutation_authorized": False,
    }
    return True, "ALIGNMENT_CANDIDATE_BOUND_EVIDENCE_ONLY", evidence, mapping


def _slice_pcm(
    pcm: Mapping[str, np.ndarray], window_ms: Sequence[int | float],
) -> dict[str, np.ndarray]:
    start = round(float(window_ms[0]) * SAMPLE_RATE_HZ / 1000.0)
    end = round(float(window_ms[1]) * SAMPLE_RATE_HZ / 1000.0)
    return {key: value[start:end] for key, value in pcm.items()}


def _analyze_local_scope(
    *, outer: Mapping[str, np.ndarray], source: Mapping[str, np.ndarray],
    interval: list[int | float], extraction: Mapping[str, object],
    source_identity: Mapping[str, str], scope: Mapping[str, object],
) -> tuple[bool, str, dict[str, object], dict[str, object] | None]:
    """Measure one caller-selected local scope and project its map to file time."""
    outer_window = scope["outer_window_ms"]
    source_window = scope["source_window_ms"]
    target_interval = scope["target_interval_ms"]
    outer_start_sample = round(float(outer_window[0]) * SAMPLE_RATE_HZ / 1000.0)
    outer_end_sample = round(float(outer_window[1]) * SAMPLE_RATE_HZ / 1000.0)
    source_start_sample = round(float(source_window[0]) * SAMPLE_RATE_HZ / 1000.0)
    source_end_sample = round(float(source_window[1]) * SAMPLE_RATE_HZ / 1000.0)
    local_outer = _slice_pcm(outer, outer_window)
    local_source = _slice_pcm(source, source_window)
    local_interval = [
        _round_ms(float(interval[0]) - float(outer_window[0])),
        _round_ms(float(interval[1]) - float(outer_window[0])),
    ]
    local_target = [
        _round_ms(float(target_interval[0]) - float(outer_window[0])),
        _round_ms(float(target_interval[1]) - float(outer_window[0])),
    ]
    local_chunk_starts = [
        round((float(chunk[0]) - float(source_window[0])) * SAMPLE_RATE_HZ / 1000.0)
        for chunk in scope["chunk_windows_ms"]
    ]
    local_extraction = {
        **dict(extraction),
        "scope": {
            "outer_window_ms": list(outer_window),
            "source_window_ms": list(source_window),
            "chunk_windows_ms": [list(chunk) for chunk in scope["chunk_windows_ms"]],
            "target_outer_interval_ms": list(target_interval),
            "descriptor_context_interval_ms": list(interval),
            "outer_start_sample": outer_start_sample,
            "outer_end_sample": outer_end_sample,
            "source_start_sample": source_start_sample,
            "source_end_sample": source_end_sample,
        },
    }
    accepted, reason, relative_evidence, relative_mapping = _analyze(
        outer=local_outer,
        source=local_source,
        interval=local_interval,
        target_interval=local_target,
        chunk_starts=local_chunk_starts,
        extraction=local_extraction,
        source_identity=source_identity,
    )
    evidence = {
        "scope_schema_version": LOCAL_SCOPE_SCHEMA,
        "scope": dict(scope),
        "clock_projection": {
            "outer_base_ms": outer_window[0],
            "source_base_ms": source_window[0],
            "relative_lag_policy": "outer_base_plus_relative_lag_minus_source_base",
            "global_alignment_supported": False,
        },
        "relative_alignment_evidence": relative_evidence,
        "decision": "MAPPING_CANDIDATE_BOUND" if accepted else "UNKNOWN_KEEP",
        "reason_code": reason,
        "authority": "EVIDENCE_ONLY",
        "source_role_authority": False,
        "mutation_authorized": False,
    }
    if not accepted or relative_mapping is None:
        return False, reason, evidence, None

    relative_lineage = relative_mapping["lineage_mapping"]
    relative_source_window = relative_lineage["source_window_ms"]
    absolute_source_window = [
        _round_ms(float(source_window[0]) + float(relative_source_window[0])),
        _round_ms(float(source_window[0]) + float(relative_source_window[1])),
    ]
    absolute_outer_window = list(interval)
    absolute_lag = _round_ms(
        float(absolute_outer_window[0]) - float(absolute_source_window[0])
    )
    projected_mapping = dict(relative_mapping)
    projected_mapping["lineage_mapping"] = {
        "outer_window_ms": absolute_outer_window,
        "source_window_ms": absolute_source_window,
        "lag_ms": absolute_lag,
    }
    projected_mapping["alignment_scope"] = dict(scope)
    projected_mapping["alignment_candidate"] = "LOCAL_SCOPE_LINEAR_AUDIO_OFFSET_CANDIDATE"
    projected_mapping["scope_authority"] = "EVIDENCE_ONLY"
    projected_mapping["alignment_evidence"] = evidence
    return True, "ALIGNMENT_LOCAL_SCOPE_CANDIDATE_BOUND", evidence, projected_mapping


def _current_cue_outer_window(
    scope: Mapping[str, object], *, outer_duration_ms: float,
) -> tuple[list[int | float], list[list[int | float]]]:
    """Choose the one deterministic outer window for a current cue.

    The target midpoint is the anchor.  The feasible start interval shifts
    that anchor only as much as necessary to keep the complete descriptor
    context and target inside the window.  No correlation score participates in
    this choice, so a failed mapping cannot search for a more favorable scene.
    """
    context = scope["descriptor_context_interval_ms"]
    target = scope["target_interval_ms"]
    context_width = float(context[1]) - float(context[0])
    width = max(float(2 * CHUNK_MS + CHUNK_MS // 2), context_width)
    if outer_duration_ms + 1e-3 < width:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_CURRENT_CUE_OUTER_TOO_SHORT")

    desired_start = ((float(target[0]) + float(target[1])) / 2.0) - width / 2.0
    lower = max(0.0, float(context[1]) - width, float(target[1]) - width)
    upper = min(
        outer_duration_ms - width,
        float(context[0]),
        float(target[0]),
    )
    if upper + 1e-3 < lower:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_CURRENT_CUE_OUTER_CONTEXT_INVALID")
    start = min(max(desired_start, lower), upper)
    start_ms = _round_ms(start)
    end_ms = _round_ms(float(start_ms) + width)
    if float(end_ms) > outer_duration_ms + 1e-3:
        start_ms = _round_ms(outer_duration_ms - width)
        end_ms = _round_ms(float(start_ms) + width)
    outer_window = [start_ms, end_ms]
    if not (
        float(outer_window[0]) <= float(context[0])
        and float(context[1]) <= float(outer_window[1]) + 1e-3
        and float(outer_window[0]) <= float(target[0])
        and float(target[1]) <= float(outer_window[1]) + 1e-3
    ):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_CURRENT_CUE_OUTER_CONTEXT_INVALID")

    first = [outer_window[0], _round_ms(float(outer_window[0]) + CHUNK_MS)]
    second = [_round_ms(float(outer_window[1]) - CHUNK_MS), outer_window[1]]
    if float(first[1]) > float(second[0]) + 1e-6:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_CURRENT_CUE_CHUNKS_INVALID")
    return outer_window, [first, second]


def _inverse_measurement(
    *, source: Mapping[str, np.ndarray], outer: Mapping[str, np.ndarray],
) -> tuple[bool, str, dict[str, object], int | None]:
    """Measure the unique source-long/outer-short offset before forward gates."""
    rows: dict[str, object] = {}
    for representation in ("side", "mid"):
        scores = _valid_ncc(source[representation], outer[representation])
        if scores.size == 0:
            return False, "ALIGNMENT_CURRENT_CUE_INVERSE_WINDOW_INVALID", {
                "measurement_orientation": "independent_source_long_to_current_outer_short",
                "representations": ["side", "mid"],
                "reason_code": "ALIGNMENT_CURRENT_CUE_INVERSE_WINDOW_INVALID",
            }, None
        summary = _best_peak(scores)
        best = dict(summary["best"])
        best["ambiguous_peak"] = _peak_ambiguous(summary)
        rows[representation] = {**summary, "best": best}

    side = rows["side"]["best"]
    mid = rows["mid"]["best"]
    lag_delta = abs(float(side["lag_ms"]) - float(mid["lag_ms"]))
    supported = (
        lag_delta <= MAX_LAG_RANGE_MS
        and float(side["abs_ncc"]) >= MIN_MEDIAN_ABS_NCC
        and float(mid["abs_ncc"]) >= MIN_MEDIAN_ABS_NCC
        and not bool(side.get("ambiguous_peak"))
        and not bool(mid.get("ambiguous_peak"))
    )
    source_start_sample = int(round(
        (int(side["lag_samples"]) + int(mid["lag_samples"])) / 2.0
    ))
    evidence: dict[str, object] = {
        "measurement_orientation": "independent_source_long_to_current_outer_short",
        "long_role": "independent_source",
        "short_role": "current_outer_window",
        "representations": ["side", "mid"],
        "side": rows["side"],
        "mid": rows["mid"],
        "decision_metrics": {
            "lag_delta_ms": round(lag_delta, 3),
            "side_abs_ncc": side["abs_ncc"],
            "mid_abs_ncc": mid["abs_ncc"],
            "unique_offset": supported,
            "source_start_ms": _round_ms(source_start_sample * 1000.0 / SAMPLE_RATE_HZ),
        },
    }
    if not supported:
        return False, "ALIGNMENT_CURRENT_CUE_INVERSE_WEAK_OR_AMBIGUOUS", evidence, None
    return True, "ALIGNMENT_CURRENT_CUE_INVERSE_BOUND", evidence, source_start_sample


def _analyze_current_cue_scope(
    *, outer: Mapping[str, np.ndarray], source: Mapping[str, np.ndarray],
    interval: list[int | float], extraction: Mapping[str, object],
    source_identity: Mapping[str, str], scope: Mapping[str, object],
) -> tuple[bool, str, dict[str, object], dict[str, object] | None]:
    """Align a current cue by one deterministic reverse-NCC local scope.

    The reverse search only locates the source slice.  Acceptance still goes
    through the unchanged ``_analyze`` implementation on that slice, so the
    existing global, endpoint-chunk, ambiguity, lag and target-fit gates remain
    the acceptance gates for the projected mapping.
    """
    outer_duration_ms = outer["side"].size * 1000.0 / SAMPLE_RATE_HZ
    source_duration_ms = source["side"].size * 1000.0 / SAMPLE_RATE_HZ
    outer_window, chunk_windows = _current_cue_outer_window(
        scope, outer_duration_ms=outer_duration_ms,
    )
    fixed_outer = _slice_pcm(outer, outer_window)
    inverse_ok, inverse_reason, inverse_evidence, source_start_sample = _inverse_measurement(
        source=source, outer=fixed_outer,
    )
    evidence: dict[str, object] = {
        "mapping_mode": CURRENT_CUE_MAPPING_MODE,
        "scope_schema_version": CURRENT_CUE_SCOPE_SCHEMA,
        "current_cue_scope": dict(scope),
        "fixed_outer_window_ms": list(outer_window),
        "fixed_chunk_windows_ms": [list(chunk) for chunk in chunk_windows],
        "source_duration_ms": _round_ms(source_duration_ms),
        "outer_duration_ms": _round_ms(outer_duration_ms),
        "inverse_measurement": inverse_evidence,
        "measurement_orientation": "independent_source_long_to_current_outer_short",
    }
    if not inverse_ok or source_start_sample is None:
        evidence["decision"] = "UNKNOWN_KEEP"
        evidence["reason_code"] = inverse_reason
        return False, inverse_reason, evidence, None

    width_samples = round((float(outer_window[1]) - float(outer_window[0])) * SAMPLE_RATE_HZ / 1000.0)
    source_end_sample = source_start_sample + width_samples
    if source_start_sample < 0 or source_end_sample > source["side"].size:
        reason = "ALIGNMENT_CURRENT_CUE_SOURCE_WINDOW_INVALID"
        evidence["decision"] = "UNKNOWN_KEEP"
        evidence["reason_code"] = reason
        return False, reason, evidence, None
    source_window = [
        _round_ms(source_start_sample * 1000.0 / SAMPLE_RATE_HZ),
        _round_ms(source_end_sample * 1000.0 / SAMPLE_RATE_HZ),
    ]
    local_source = _slice_pcm(source, source_window)
    local_interval = [
        _round_ms(float(interval[0]) - float(outer_window[0])),
        _round_ms(float(interval[1]) - float(outer_window[0])),
    ]
    target_interval = scope["target_interval_ms"]
    local_target = [
        _round_ms(float(target_interval[0]) - float(outer_window[0])),
        _round_ms(float(target_interval[1]) - float(outer_window[0])),
    ]
    chunk_samples = round(CHUNK_MS * SAMPLE_RATE_HZ / 1000.0)
    local_chunk_starts = [0, width_samples - chunk_samples]
    local_extraction = {
        **dict(extraction),
        "scope": {
            "mode": CURRENT_CUE_MAPPING_MODE,
            "outer_window_ms": list(outer_window),
            "source_window_ms": list(source_window),
            "chunk_windows_ms": [list(chunk) for chunk in chunk_windows],
            "descriptor_context_interval_ms": list(interval),
            "target_interval_ms": list(target_interval),
            "inverse_source_start_ms": source_window[0],
        },
    }
    accepted, reason, relative_evidence, relative_mapping = _analyze(
        outer=fixed_outer,
        source=local_source,
        interval=local_interval,
        target_interval=local_target,
        chunk_starts=local_chunk_starts,
        extraction=local_extraction,
        source_identity=source_identity,
    )
    evidence["forward_validation"] = relative_evidence
    evidence["decision"] = "MAPPING_CANDIDATE_BOUND" if accepted else "UNKNOWN_KEEP"
    evidence["reason_code"] = reason
    if not accepted or relative_mapping is None:
        return False, reason, evidence, None

    relative_lineage = relative_mapping["lineage_mapping"]
    relative_source_window = relative_lineage["source_window_ms"]
    absolute_source_window = [
        _round_ms(float(source_window[0]) + float(relative_source_window[0])),
        _round_ms(float(source_window[0]) + float(relative_source_window[1])),
    ]
    absolute_outer_window = list(interval)
    absolute_lag = _round_ms(
        float(absolute_outer_window[0]) - float(absolute_source_window[0])
    )
    projected_mapping = dict(relative_mapping)
    projected_mapping["lineage_mapping"] = {
        "outer_window_ms": absolute_outer_window,
        "source_window_ms": absolute_source_window,
        "lag_ms": absolute_lag,
    }
    projected_mapping["current_cue_scope"] = dict(scope)
    projected_mapping["mapping_scope"] = CURRENT_CUE_MAPPING_MODE
    projected_mapping["alignment_candidate"] = "CURRENT_CUE_REVERSE_NCC_CANDIDATE"
    projected_mapping["scope_authority"] = "EVIDENCE_ONLY"
    projected_mapping["measurement_orientation"] = (
        "independent_source_long_to_current_outer_short_then_outer_to_source_projection"
    )
    projected_mapping["alignment_evidence"] = evidence
    return True, "ALIGNMENT_CURRENT_CUE_CANDIDATE_BOUND", evidence, projected_mapping


def _result_base(
    *, request_path: Path, request_dir: Path, request_sha: str, reason: str, replayed: bool,
) -> dict[str, object]:
    return {
        "schema_version": RESULT_SCHEMA,
        "status": "EVIDENCE_ONLY",
        "action": "KEEP",
        "reason_code": reason,
        "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False,
        "request_sha256": request_sha,
        "request_path": str(request_path),
        "request_dir": str(request_dir),
        "replayed": replayed,
        "provider_calls": 0,
        "download_calls": 0,
        "asr_calls": 0,
    }


def _replay_cached(request: Mapping[str, object], request_path: Path, request_dir: Path) -> dict[str, object] | None:
    request_sha = _canonical_sha(request)
    if not request_path.is_file():
        return None
    saved_request = _read_json(request_path, code="ALIGNMENT_CACHE_REQUEST_INVALID")
    if saved_request != dict(request):
        raise NestedSourceAudioAlignmentError("ALIGNMENT_CACHE_BINDING_DRIFT")
    result_path = request_dir / "RESULT.json"
    if not result_path.is_file():
        return _result_base(
            request_path=request_path, request_dir=request_dir, request_sha=request_sha,
            reason="ALIGNMENT_AMBIGUOUS_OR_INCOMPLETE", replayed=True,
        )
    result = _read_json(result_path, code="ALIGNMENT_CACHE_RESULT_INVALID")
    if result.get("request_sha256") != request_sha:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_CACHE_RESULT_BINDING_DRIFT")
    result["replayed"] = True
    mapping_ref = result.get("mapping_receipt")
    if isinstance(mapping_ref, Mapping):
        mapping_path = Path(str(mapping_ref.get("path", "")))
        if not mapping_path.is_file() or _file_sha(mapping_path) != mapping_ref.get("sha256"):
            raise NestedSourceAudioAlignmentError("ALIGNMENT_CACHE_MAPPING_DRIFT")
    return result


def _prepare_outer_context_mapping(
    *,
    outer_media: str | Path,
    source_media: str | Path,
    source_identity: Mapping[str, object],
    interval_ms: Sequence[int | float],
    evidence_root: str | Path,
    current_cue_scope: Mapping[str, object],
    candidate_id: str,
    outer_context: Mapping[str, object],
) -> dict[str, object]:
    """Map a current recut through its immutable padded context media.

    The context mapper is deliberately a recursive call with the existing
    current-cue algorithm.  This wrapper only validates and records the recut
    projection; it never changes the NCC thresholds or chooses a source window.
    The recursive request and receipt remain separate immutable evidence.
    """

    outer = _regular_file(outer_media, code="ALIGNMENT_OUTER_MEDIA_UNAVAILABLE")
    source = _regular_file(source_media, code="ALIGNMENT_SOURCE_MEDIA_UNAVAILABLE")
    identity = _source_identity(source_identity)
    interval = _normal_interval(interval_ms)
    outer_sha, source_sha = _file_sha(outer), _file_sha(source)
    normalized_scope = _normal_current_cue_scope(
        current_cue_scope,
        interval,
        candidate_id=candidate_id,
        outer_media_sha256=outer_sha,
    )
    binding = _validate_outer_context_inputs(
        outer=outer,
        outer_sha=outer_sha,
        interval=interval,
        scope=normalized_scope,
        outer_context=outer_context,
    )
    root = _safe_root(evidence_root)
    context_media = Path(str(binding["context_media"]["path"]))
    context_sha = str(binding["context_media"]["sha256"])
    current_srt = Path(str(binding["current_srt"]["path"]))
    current_srt_sha = str(binding["current_srt"]["sha256"])
    provenance_path = Path(str(binding["recut_provenance"]["path"]))
    provenance_sha = str(binding["recut_provenance"]["sha256"])
    final_recut = dict(binding["final_recut"])
    offset_ms = int(final_recut["start_ms"])

    request = {
        "schema_version": OUTER_CONTEXT_REQUEST_SCHEMA,
        "algorithm_version": ALGORITHM_VERSION,
        "projection_schema": OUTER_CONTEXT_PROJECTION_SCHEMA,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "chunk_ms": CHUNK_MS,
        "chunk_step_ms": CHUNK_STEP_MS,
        "local_half_width_ms": LOCAL_HALF_WIDTH_MS,
        "outer_media": {
            "path": str(outer), "sha256": outer_sha, "bytes": outer.stat().st_size,
        },
        "context_media": {
            "path": str(context_media), "sha256": context_sha, "bytes": context_media.stat().st_size,
        },
        "source_media": {
            "path": str(source), "sha256": source_sha, "bytes": source.stat().st_size,
        },
        "source_identity": identity,
        "interval_ms": interval,
        "candidate_id": candidate_id,
        "current_cue_scope": normalized_scope,
        "current_srt": {
            "path": str(current_srt), "sha256": current_srt_sha, "bytes": current_srt.stat().st_size,
        },
        "recut_provenance": {
            "path": str(provenance_path), "sha256": provenance_sha, "bytes": provenance_path.stat().st_size,
        },
        "final_recut": final_recut,
        "mapping_mode": CURRENT_CUE_MAPPING_MODE,
    }
    request_sha = _canonical_sha(request)
    context_root = root / "outer-context"
    request_dir = context_root / request_sha.removeprefix("sha256:")
    request_path = request_dir / "INPUT.json"
    result_path = request_dir / "RESULT.json"
    process_path = request_dir / "PROCESS.json"
    mapping_path = request_dir / "mapping-receipt.json"

    with exclusive_lock(context_root / "alignment.lock", label="nested source outer context alignment"):
        if request_dir.exists():
            if request_dir.is_symlink() or not request_dir.is_dir():
                raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_EVIDENCE_PATH_DRIFT")
            if not request_path.is_file() or not result_path.is_file():
                raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_INCOMPLETE")
            saved = _read_json(request_path, code="ALIGNMENT_OUTER_CONTEXT_INPUT_INVALID")
            if saved.get("request") != request or saved.get("request_sha256") != request_sha:
                raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_CACHE_BINDING_DRIFT")
            result = _read_json(result_path, code="ALIGNMENT_OUTER_CONTEXT_RESULT_INVALID")
            if result.get("request_sha256") != request_sha:
                raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_RESULT_BINDING_DRIFT")
            mapping_ref = result.get("mapping_receipt")
            if isinstance(mapping_ref, Mapping):
                cached_mapping_path = Path(str(mapping_ref.get("path") or ""))
                if (
                    not cached_mapping_path.is_file()
                    or _file_sha(cached_mapping_path) != mapping_ref.get("sha256")
                ):
                    raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_MAPPING_DRIFT")
            result["replayed"] = True
            return result

        request_dir.mkdir(parents=True, exist_ok=False)
        process = {
            "schema_version": "nested-source-audio-outer-context-process.v1",
            "status": "RUNNING",
            "request_sha256": request_sha,
            "started_at": _now(),
            "provider_calls": 0,
            "download_calls": 0,
            "asr_calls": 0,
        }
        atomic_write_json(process_path, process)
        try:
            current_duration_ms = _probe_duration_ms_without_artifact(outer)
            context_duration_ms = _probe_duration_ms_without_artifact(context_media)
            recut_end_ms = int(final_recut["end_ms"])
            if float(interval[1]) > current_duration_ms + 1e-3:
                raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_CURRENT_MEDIA_TOO_SHORT")
            if recut_end_ms > context_duration_ms + 1e-3:
                raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_MEDIA_TOO_SHORT")
            context_frame_ms = int(normalized_scope["caption_frame_ms"]) + offset_ms
            if context_frame_ms < 0 or context_frame_ms >= context_duration_ms:
                raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_FRAME_OUT_OF_BOUNDS")
            context_frame = _extract_outer_context_frame(context_media, context_frame_ms)
            context_frame_path = request_dir / "context-caption-frame.jpg"
            context_frame_path.write_bytes(context_frame)
            context_frame_sha = _file_sha(context_frame_path)
            projected_srt_path = request_dir / "context-current.srt"
            projected_srt_sha, srt_proof = _project_outer_context_srt(
                current_srt, projected_srt_path, offset_ms=offset_ms,
            )

            shifted_interval = [
                int(float(interval[0]) + offset_ms), int(float(interval[1]) + offset_ms),
            ]
            shifted_scope = dict(normalized_scope)
            shifted_scope.update(
                descriptor_context_interval_ms=[
                    int(float(normalized_scope["descriptor_context_interval_ms"][0]) + offset_ms),
                    int(float(normalized_scope["descriptor_context_interval_ms"][1]) + offset_ms),
                ],
                target_interval_ms=[
                    int(float(normalized_scope["target_interval_ms"][0]) + offset_ms),
                    int(float(normalized_scope["target_interval_ms"][1]) + offset_ms),
                ],
                caption_frame_path=str(context_frame_path),
                caption_frame_sha256=context_frame_sha,
                caption_frame_ms=context_frame_ms,
                final_srt_sha256=projected_srt_sha,
                outer_media_sha256=context_sha,
            )
            shifted_scope = _normal_current_cue_scope(
                shifted_scope,
                shifted_interval,
                candidate_id=candidate_id,
                outer_media_sha256=context_sha,
            )
            derived = {
                "current_outer_duration_ms": round(current_duration_ms, 3),
                "context_media_duration_ms": round(context_duration_ms, 3),
                "context_frame": {
                    "path": str(context_frame_path), "sha256": context_frame_sha,
                    "frame_ms": context_frame_ms,
                },
                "projected_srt": srt_proof,
                "shifted_interval_ms": shifted_interval,
                "shifted_current_cue_scope": shifted_scope,
            }
            _write_json_once(
                request_path,
                {"schema_version": OUTER_CONTEXT_REQUEST_SCHEMA, "request_sha256": request_sha, "request": request, "derived": derived},
            )

            context_result = prepare_nested_source_mapping(
                context_media,
                source,
                identity,
                shifted_interval,
                request_dir / "context-mapping",
                current_cue_scope=shifted_scope,
                candidate_id=candidate_id,
            )
            if _file_sha(outer) != outer_sha or _file_sha(current_srt) != current_srt_sha:
                raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_INPUT_CHANGED")
            if _file_sha(provenance_path) != provenance_sha:
                raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_PROVENANCE_CHANGED")
            if _file_sha(context_media) != context_sha or _file_sha(source) != source_sha:
                raise NestedSourceAudioAlignmentError("ALIGNMENT_MEDIA_CHANGED_DURING_EXTRACTION")
            if _file_sha(Path(str(normalized_scope["caption_frame_path"]))) != normalized_scope["caption_frame_sha256"]:
                raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_FRAME_CHANGED")

            projection = {
                "schema_version": OUTER_CONTEXT_PROJECTION_SCHEMA,
                "offset_ms": offset_ms,
                "current_outer": binding["current_outer"],
                "context_media": binding["context_media"],
                "current_srt": binding["current_srt"],
                "context_srt": {
                    "path": str(projected_srt_path), "sha256": projected_srt_sha,
                    "bytes": projected_srt_path.stat().st_size,
                },
                "recut_provenance": binding["recut_provenance"],
                "final_recut": final_recut,
                "current_frame": binding["scope_frame"],
                "context_frame": {
                    "path": str(context_frame_path), "sha256": context_frame_sha,
                    "frame_ms": context_frame_ms,
                },
                "srt_roundtrip": srt_proof,
                "current_scope": normalized_scope,
                "context_scope": shifted_scope,
            }
            result = {
                "schema_version": OUTER_CONTEXT_RESULT_SCHEMA,
                "status": "EVIDENCE_ONLY",
                "action": "KEEP",
                "authority": "EVIDENCE_ONLY",
                "mutation_authorized": False,
                "request_sha256": request_sha,
                "request_path": str(request_path),
                "request_dir": str(request_dir),
                "replayed": False,
                "provider_calls": 0,
                "download_calls": 0,
                "asr_calls": 0,
                "mapping_scope": CURRENT_CUE_MAPPING_MODE,
                "outer_context_projection": projection,
                "context_mapping_result": {
                    "decision": context_result.get("decision"),
                    "reason_code": context_result.get("reason_code"),
                    "request_path": context_result.get("request_path"),
                    "request_sha256": context_result.get("request_sha256"),
                    "result_path": str(Path(str(context_result.get("request_dir"))) / "RESULT.json"),
                },
            }
            context_ref = context_result.get("mapping_receipt")
            if context_result.get("decision") == "MAPPING_CANDIDATE_BOUND" and isinstance(context_ref, Mapping):
                context_mapping_path = Path(str(context_ref.get("path") or ""))
                context_root_resolved = (request_dir / "context-mapping").resolve()
                try:
                    context_mapping_path.resolve().relative_to(context_root_resolved)
                except ValueError as exc:
                    raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_MAPPING_PATH_INVALID") from exc
                if not context_mapping_path.is_file() or _file_sha(context_mapping_path) != context_ref.get("sha256"):
                    raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_MAPPING_DRIFT")
                context_mapping = _read_json(
                    context_mapping_path, code="ALIGNMENT_OUTER_CONTEXT_MAPPING_INVALID",
                )
                lineage = context_mapping.get("lineage_mapping")
                if not isinstance(lineage, Mapping) or not _same_interval(lineage.get("outer_window_ms", []), shifted_interval):
                    raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_MAPPING_SCOPE_MISMATCH")
                source_window = lineage.get("source_window_ms")
                if not isinstance(source_window, Sequence) or isinstance(source_window, (str, bytes)):
                    raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_MAPPING_LINEAGE_INVALID")
                mapping = dict(context_mapping)
                media = context_mapping.get("media")
                if not isinstance(media, Mapping) or not isinstance(media.get("inner"), Mapping):
                    raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_MAPPING_MEDIA_INVALID")
                mapping["media"] = {
                    "outer": binding["current_outer"],
                    "inner": dict(media["inner"]),
                }
                mapping["lineage_mapping"] = {
                    "outer_window_ms": list(interval),
                    "source_window_ms": list(source_window),
                    "lag_ms": _round_ms(float(interval[0]) - float(source_window[0])),
                }
                mapping["current_cue_scope"] = dict(normalized_scope)
                mapping["mapping_scope"] = CURRENT_CUE_MAPPING_MODE
                mapping["alignment_candidate"] = "CURRENT_CUE_REVERSE_NCC_CONTEXT_PROJECTED_CANDIDATE"
                mapping["outer_context_projection"] = projection
                mapping["context_mapping_receipt"] = {
                    "path": str(context_mapping_path), "sha256": context_ref.get("sha256"),
                }
                mapping_sha = _write_json_once(mapping_path, mapping)
                result.update(
                    decision="MAPPING_CANDIDATE_BOUND",
                    reason_code="ALIGNMENT_CURRENT_CUE_CONTEXT_CANDIDATE_BOUND",
                    mapping_receipt={"path": str(mapping_path), "sha256": mapping_sha},
                )
            else:
                result.update(
                    decision="UNKNOWN_KEEP",
                    reason_code=str(context_result.get("reason_code") or "ALIGNMENT_CURRENT_CUE_CONTEXT_UNKNOWN"),
                )
            result_sha = _write_json_once(result_path, result)
            process.update(
                status="COMPLETE", completed_at=_now(), result_path=str(result_path), result_sha256=result_sha,
            )
            atomic_write_json(process_path, process)
            return result
        except NestedSourceAudioAlignmentError as exc:
            if not request_path.exists():
                _write_json_once(
                    request_path,
                    {
                        "schema_version": OUTER_CONTEXT_REQUEST_SCHEMA,
                        "request_sha256": request_sha,
                        "request": request,
                        "derived": {"status": "NOT_CREATED", "reason_code": str(exc)},
                    },
                )
            result = {
                "schema_version": OUTER_CONTEXT_RESULT_SCHEMA,
                "status": "EVIDENCE_ONLY",
                "action": "KEEP",
                "decision": "UNKNOWN_KEEP",
                "reason_code": str(exc),
                "authority": "EVIDENCE_ONLY",
                "mutation_authorized": False,
                "request_sha256": request_sha,
                "request_path": str(request_path),
                "request_dir": str(request_dir),
                "replayed": False,
                "provider_calls": 0,
                "download_calls": 0,
                "asr_calls": 0,
                "mapping_scope": CURRENT_CUE_MAPPING_MODE,
            }
            result_sha = _write_json_once(result_path, result)
            process.update(
                status="COMPLETE", completed_at=_now(), result_path=str(result_path), result_sha256=result_sha,
                reason_code=str(exc),
            )
            atomic_write_json(process_path, process)
            return result


def prepare_nested_source_mapping(
    outer_media: str | Path,
    source_media: str | Path,
    source_identity: Mapping[str, object],
    interval_ms: Sequence[int | float],
    evidence_root: str | Path,
    local_scope: Mapping[str, object] | None = None,
    current_cue_scope: Mapping[str, object] | None = None,
    candidate_id: str | None = None,
    outer_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Create or replay one bounded, evidence-only source time mapping.

    The function never calls a model, downloads media, or edits subtitle
    inputs.  Weak or inconsistent correlations are persisted as UNKNOWN/KEEP
    and cannot be retried under the same request hash.
    """

    if outer_context is not None:
        if local_scope is not None or current_cue_scope is None or candidate_id is None:
            raise NestedSourceAudioAlignmentError("ALIGNMENT_OUTER_CONTEXT_SCOPE_INVALID")
        return _prepare_outer_context_mapping(
            outer_media=outer_media,
            source_media=source_media,
            source_identity=source_identity,
            interval_ms=interval_ms,
            evidence_root=evidence_root,
            current_cue_scope=current_cue_scope,
            candidate_id=candidate_id,
            outer_context=outer_context,
        )

    outer = _regular_file(outer_media, code="ALIGNMENT_OUTER_MEDIA_UNAVAILABLE")
    source = _regular_file(source_media, code="ALIGNMENT_SOURCE_MEDIA_UNAVAILABLE")
    identity = _source_identity(source_identity)
    interval = _normal_interval(interval_ms)
    if local_scope is not None and current_cue_scope is not None:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_SCOPE_MODES_EXCLUSIVE")
    normalized_local_scope = (
        _normal_local_scope(local_scope, interval) if local_scope is not None else None
    )
    root = _safe_root(evidence_root)
    outer_sha, source_sha = _file_sha(outer), _file_sha(source)
    if current_cue_scope is not None and candidate_id is None:
        raise NestedSourceAudioAlignmentError("ALIGNMENT_CURRENT_CUE_CANDIDATE_REQUIRED")
    normalized_current_cue_scope = (
        _normal_current_cue_scope(
            current_cue_scope, interval, candidate_id=candidate_id,
            outer_media_sha256=outer_sha,
        )
        if current_cue_scope is not None
        else None
    )
    request = {
        "schema_version": "nested-source-audio-alignment-request.v1",
        "algorithm_version": ALGORITHM_VERSION,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "chunk_ms": CHUNK_MS,
        "chunk_step_ms": CHUNK_STEP_MS,
        "local_half_width_ms": LOCAL_HALF_WIDTH_MS,
        "outer_media": {"path": str(outer), "sha256": outer_sha, "bytes": outer.stat().st_size},
        "source_media": {"path": str(source), "sha256": source_sha, "bytes": source.stat().st_size},
        "source_identity": identity,
        "interval_ms": interval,
        "thresholds": {
            "max_lag_range_ms": MAX_LAG_RANGE_MS,
            "min_median_abs_ncc": MIN_MEDIAN_ABS_NCC,
        },
    }
    if normalized_local_scope is not None:
        request["local_scope"] = normalized_local_scope
    if normalized_current_cue_scope is not None:
        request["mapping_mode"] = CURRENT_CUE_MAPPING_MODE
        request["candidate_id"] = candidate_id
        request["current_cue_scope"] = normalized_current_cue_scope
    request_sha = _canonical_sha(request)
    request_dir = root / request_sha.removeprefix("sha256:")
    request_path, result_path = request_dir / "INPUT.json", request_dir / "RESULT.json"
    with exclusive_lock(root / "alignment.lock", label="nested source audio alignment"):
        request_dir.mkdir(parents=True, exist_ok=True)
        if request_path.exists():
            cached = _replay_cached(request, request_path, request_dir)
            if cached is not None:
                return cached
        _write_json_once(request_path, request)
        process_path = request_dir / "PROCESS.json"
        process = {
            "schema_version": "nested-source-audio-alignment-process.v1",
            "status": "RUNNING", "request_sha256": request_sha, "started_at": _now(),
            "provider_calls": 0, "download_calls": 0, "asr_calls": 0,
        }
        atomic_write_json(process_path, process)
        try:
            outer_probe_path = request_dir / "outer.ffprobe.json"
            source_probe_path = request_dir / "source.ffprobe.json"
            outer_raw = request_dir / "outer-8k-stereo.f32le"
            source_raw = request_dir / "source-8k-stereo.f32le"
            outer_probe = _run_probe(outer, outer_probe_path)
            source_probe = _run_probe(source, source_probe_path)
            outer_duration = _probe_duration_ms(outer_probe)
            source_duration = _probe_duration_ms(source_probe)
            # The historical whole-source matcher fits the source inside the
            # outer file.  The current-cue reverse matcher deliberately treats
            # the independent source as the long signal, so it must bypass that
            # old precondition while retaining it for the default/local paths.
            if (
                normalized_current_cue_scope is None
                and outer_duration is not None
                and source_duration is not None
                and source_duration > outer_duration + 100
            ):
                raise NestedSourceAudioAlignmentError("ALIGNMENT_SOURCE_LONGER_THAN_OUTER")
            if normalized_local_scope is not None and outer_duration is not None and source_duration is not None:
                _validate_local_scope_bounds(
                    normalized_local_scope,
                    outer_duration_ms=outer_duration,
                    source_duration_ms=source_duration,
                )
            outer_extract = _run_extract(outer, outer_raw, temp=request_dir / "outer-8k-stereo.f32le.tmp")
            source_extract = _run_extract(source, source_raw, temp=request_dir / "source-8k-stereo.f32le.tmp")
            extraction = {
                "outer_media": {"path": str(outer), "sha256": outer_sha, "bytes": outer.stat().st_size},
                "source_media": {"path": str(source), "sha256": source_sha, "bytes": source.stat().st_size},
                "outer_probe": outer_probe, "source_probe": source_probe,
                "outer_pcm": outer_extract, "source_pcm": source_extract,
                "probe_duration_ms": {
                    "outer": _probe_duration_ms(outer_probe), "source": _probe_duration_ms(source_probe),
                },
            }
            outer_pcm, source_pcm = _load_pcm(outer_raw), _load_pcm(source_raw)
            if normalized_current_cue_scope is not None:
                accepted, reason, evidence, mapping = _analyze_current_cue_scope(
                    outer=outer_pcm, source=source_pcm, interval=interval,
                    extraction=extraction, source_identity=identity,
                    scope=normalized_current_cue_scope,
                )
                global_accepted, global_reason, global_evidence = accepted, reason, evidence
                local_reason = None
            else:
                accepted, reason, evidence, mapping = _analyze(
                    outer=outer_pcm, source=source_pcm, interval=interval,
                    extraction=extraction, source_identity=identity,
                )
                global_accepted, global_reason, global_evidence = accepted, reason, evidence
                local_reason = None
            if normalized_local_scope is not None:
                local_accepted, local_reason, local_evidence, local_mapping = _analyze_local_scope(
                    outer=outer_pcm, source=source_pcm, interval=interval,
                    extraction=extraction, source_identity=identity,
                    scope=normalized_local_scope,
                )
                evidence = {
                    **dict(global_evidence),
                    "whole_source_alignment": {
                        "accepted": global_accepted,
                        "reason_code": global_reason,
                        "alignment_evidence": global_evidence,
                    },
                    "local_scope_alignment": local_evidence,
                }
                if local_accepted and local_mapping is not None:
                    accepted, reason, mapping = True, local_reason, local_mapping
                else:
                    accepted, reason, mapping = False, local_reason or global_reason, None
            if _file_sha(outer) != outer_sha or _file_sha(source) != source_sha:
                raise NestedSourceAudioAlignmentError("ALIGNMENT_MEDIA_CHANGED_DURING_EXTRACTION")
            result = _result_base(
                request_path=request_path, request_dir=request_dir, request_sha=request_sha,
                reason=reason, replayed=False,
            )
            result["alignment_evidence"] = evidence
            if normalized_local_scope is not None:
                result["mapping_scope"] = "LOCAL_BOUNDED"
                result["global_reason_code"] = global_reason
                result["global_decision"] = "MAPPING_CANDIDATE_BOUND" if global_accepted else "UNKNOWN_KEEP"
            elif normalized_current_cue_scope is not None:
                result["mapping_scope"] = CURRENT_CUE_MAPPING_MODE
                result["inverse_reason_code"] = evidence.get("inverse_measurement", {}).get("reason_code") if isinstance(evidence.get("inverse_measurement"), Mapping) else None
                result["forward_decision"] = evidence.get("decision")
            if accepted and mapping is not None:
                mapping_path = request_dir / "mapping-receipt.json"
                mapping_sha = _write_json_once(mapping_path, mapping)
                result.update(
                    action="KEEP", decision="MAPPING_CANDIDATE_BOUND",
                    mapping_receipt={"path": str(mapping_path), "sha256": mapping_sha},
                )
            else:
                result.update(decision="UNKNOWN_KEEP")
            result_sha = _write_json_once(result_path, result)
            process.update(status="COMPLETE", completed_at=_now(), result_path=str(result_path), result_sha256=result_sha)
            atomic_write_json(process_path, process)
            return result
        except NestedSourceAudioAlignmentError as exc:
            result = _result_base(
                request_path=request_path, request_dir=request_dir, request_sha=request_sha,
                reason=str(exc), replayed=False,
            )
            if normalized_current_cue_scope is not None:
                result["mapping_scope"] = CURRENT_CUE_MAPPING_MODE
            result["decision"] = "UNKNOWN_KEEP"
            result["error_type"] = type(exc).__name__
            result_sha = _write_json_once(result_path, result)
            process.update(
                status="COMPLETE", completed_at=_now(), result_path=str(result_path),
                result_sha256=result_sha, reason_code=str(exc),
            )
            atomic_write_json(process_path, process)
            return result


__all__ = [
    "CURRENT_CUE_SCOPE_KIND", "CURRENT_CUE_SCOPE_SCHEMA", "LOCAL_SCOPE_SCHEMA",
    "NestedSourceAudioAlignmentError", "prepare_nested_source_mapping",
]
