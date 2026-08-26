"""Fail-closed subtitle text preservation for reviewed redeliveries.

A subtitle-only rerun still invokes nondeterministic ASR and reviewer stages.
Without an explicit baseline, unrelated cues can silently change while the
operator is fixing one known incident or merely re-burning media.  This module
binds a previous delivered SRT by hash and projects only its text onto the new
cue timing outside higher-authority source-truth windows.

Version 1 aligns two SRTs on the same local timeline.  Version 2 also binds the
source recording identity and projects the reviewed baseline through absolute
source time, so a new recut may trim or extend only at clean reviewed-coverage
boundaries.  Timing normally remains current.  A manifest may explicitly make
the whole reviewed SRT authoritative when both the source identity and exact
source interval match; this prevents a fresh ASR pass from deleting already
reviewed cues.  Other missing, split, merged, ambiguous, drifted, or
identity-mismatched cues still fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.autoslice.jingting_chunker import SrtCue, parse_srt_cues
from src.autoslice.redelivery_boundary_projection import (
    AUTHORITY_CONFIG_KEY,
    RedeliveryBoundaryProjectionError,
    projection_materialization_receipt,
)
from src.autoslice.text_baseline_guard import (
    BASELINE_CONTAINS_SPEAKER_LABEL_PREFIX,
    contains_speaker_label_prefix,
)


SCHEMA_VERSION = "subtitle-redelivery-baseline.v1"
SCHEMA_VERSION_V2 = "subtitle-redelivery-baseline.v2"
AUDIT_SCHEMA_VERSION = "subtitle-redelivery-baseline-audit.v1"
AUDIT_SCHEMA_VERSION_V2 = "subtitle-redelivery-baseline-audit.v2"
MODE = "preserve_text_outside_source_truth"
MIN_ALIGNMENT_OVERLAP_MS = 80
MIN_ALIGNMENT_RATIO = 0.80
MAX_ALIGNMENT_BOUNDARY_DRIFT_MS = 250
# Release-grade cue merging preserves exact normalized text but a fresh ASR
# grid can move the absorbed one-character cue's outer edge slightly farther
# than an ordinary one-to-one alignment.  The 2026-07-22 hotpot redelivery
# differed by 320ms after 「行」 was absorbed into 「嘻，晓得吧」.  Keep this a
# separate, narrow allowance: adjacency and exact joined-text equality remain
# mandatory, and ordinary alignment still uses the stricter 250ms bound.
MAX_RELEASE_GRADE_MERGE_BOUNDARY_DRIFT_MS = 400
# A same-BV exact-source repair may deliberately move the video pin a single
# renderer tail pad past the reviewed subtitle interval.  The reviewed SRT is
# still the complete text authority: replay it byte-for-byte and leave the
# bounded video-only tail without inventing a fresh ASR cue.
MAX_EXACT_REPLAY_VIDEO_TAIL_MS = 400
_SHA256_RX = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")
_OPERATOR_DROP_LEDGER_SCHEMA = "operator-reviewed-subtitle-decisions.v3"
_OPERATOR_DROP_PIN_SCHEMA = "operator-reviewed-text-full-ownership-pin.v3"


@dataclass(frozen=True)
class _V2Timeline:
    baseline_start_ms: int
    baseline_end_ms: int
    current_start_ms: int
    current_end_ms: int
    current_duration_ms: int
    effective_start_ms: int
    effective_end_ms: int
    protected_windows: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class _V2CueRows:
    baseline: dict[int, tuple[int, int]]
    current: dict[int, tuple[int, int]]
    protected_current_count: int


@dataclass(frozen=True)
class _V2Alignment:
    strong_by_current: dict[int, list[tuple[int, int, float, int, int]]]
    raw_by_current: dict[int, list[int]]
    strong_by_baseline: dict[int, list[int]]
    raw_by_baseline: dict[int, list[int]]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _format_ms(value: int) -> str:
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _render(cues: Sequence[SrtCue], texts: Sequence[str]) -> str:
    return "\n\n".join(
        f"{index}\n{_format_ms(cue.start_ms)} --> {_format_ms(cue.end_ms)}\n{text}"
        for index, (cue, text) in enumerate(zip(cues, texts), start=1)
    ) + ("\n" if cues else "")


def _overlap_ms(left: SrtCue, right: SrtCue) -> int:
    return max(0, min(left.end_ms, right.end_ms) - max(left.start_ms, right.start_ms))


def _protected(cue: SrtCue, windows: Sequence[tuple[int, int]]) -> bool:
    return any(
        max(0, min(cue.end_ms, end_ms) - max(cue.start_ms, start_ms))
        >= MIN_ALIGNMENT_OVERLAP_MS
        for start_ms, end_ms in windows
    )


def _valid_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _read_sha256(value: object, *, reason_code: str) -> str:
    match = _SHA256_RX.fullmatch(str(value or ""))
    if match is None:
        raise ValueError(reason_code)
    return match.group(1)


def _read_recording_basename(value: object, *, reason_code: str) -> str:
    basename = str(value or "").strip()
    if (
        not basename
        or basename in {".", ".."}
        or Path(basename).name != basename
    ):
        raise ValueError(reason_code)
    return basename


def _sealed_operator_drop_release(
    *, config: Mapping[str, Any], baseline: Sequence[SrtCue], spec_parent: Path,
) -> str | None:
    """Prove a v3 release was reconstructed from every sealed source cue.

    The reviewed SRT is deliberately shorter only where an explicit DROP row
    consumes one source cue.  We never infer those gaps from timing overlap.
    """

    pin = config.get("operator_text_full_ownership")
    if not isinstance(pin, Mapping) or pin.get("schema_version") != _OPERATOR_DROP_PIN_SCHEMA:
        return None
    lanes = config.get("operator_truth_lanes")
    if not isinstance(lanes, Mapping):
        raise ValueError("REDELIVERY_OPERATOR_DROP_LANES_INVALID")
    try:
        ledger_lane = lanes["decision_ledger"]
        pipeline_lane = lanes["pipeline_diagnostic"]
        if not isinstance(ledger_lane, Mapping) or not isinstance(pipeline_lane, Mapping):
            raise ValueError
        ledger_path = spec_parent / Path(str(ledger_lane["path"]))
        pipeline_path = spec_parent / Path(str(pipeline_lane["path"]))
        if (
            ledger_path.parent != spec_parent or pipeline_path.parent != spec_parent
            or ledger_path.is_symlink() or pipeline_path.is_symlink()
            or not ledger_path.is_file() or not pipeline_path.is_file()
        ):
            raise ValueError
        ledger_raw = ledger_path.read_bytes()
        pipeline_raw = pipeline_path.read_bytes()
        if _sha256_bytes(ledger_raw) != _read_sha256(ledger_lane["sha256"], reason_code="REDELIVERY_OPERATOR_DROP_LEDGER_SHA_INVALID") or _sha256_bytes(pipeline_raw) != _read_sha256(pipeline_lane["sha256"], reason_code="REDELIVERY_OPERATOR_DROP_PIPELINE_SHA_INVALID"):
            raise ValueError
        ledger = json.loads(ledger_raw.decode("utf-8"))
        source = parse_srt_cues(pipeline_raw.decode("utf-8"))
    except (KeyError, OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise ValueError("REDELIVERY_OPERATOR_DROP_LANES_INVALID") from exc
    rows = ledger.get("cue_decisions") if isinstance(ledger, Mapping) else None
    if not isinstance(rows, list) or ledger.get("schema_version") != _OPERATOR_DROP_LEDGER_SCHEMA or len(rows) != len(source):
        raise ValueError("REDELIVERY_OPERATOR_DROP_LEDGER_INVALID")
    release_index = 0
    for ordinal, (source_cue, row) in enumerate(zip(source, rows, strict=True), start=1):
        common = {"cue", "disposition"}
        before = str(source_cue.text).strip()
        if (
            not isinstance(row, Mapping) or not common.issubset(row)
            or row.get("cue") != ordinal
        ):
            raise ValueError("REDELIVERY_OPERATOR_DROP_SOURCE_MAPPING_INVALID")
        if row.get("disposition") == "OPERATOR_DROP":
            if set(row) != common | {"decision_authority", "drop_reason"} or row.get("decision_authority") != "LEDGER_OPERATOR_AUTHORITY" or not isinstance(row.get("drop_reason"), str) or not row["drop_reason"].strip():
                raise ValueError("REDELIVERY_OPERATOR_DROP_ROW_INVALID")
            continue
        if release_index >= len(baseline):
            raise ValueError("REDELIVERY_OPERATOR_DROP_RELEASE_MAPPING_INVALID")
        release = baseline[release_index]
        if (release.start_ms, release.end_ms) != (source_cue.start_ms, source_cue.end_ms) or not str(release.text).strip():
            raise ValueError("REDELIVERY_OPERATOR_DROP_RELEASE_MAPPING_INVALID")
        if row.get("disposition") == "OPERATOR_UNCHANGED_FREEZE":
            if set(row) != common or str(release.text).strip() != before:
                raise ValueError("REDELIVERY_OPERATOR_DROP_RELEASE_MAPPING_INVALID")
        elif row.get("disposition") == "OPERATOR_EXACT_TEXT":
            authority = row.get("decision_authority")
            pinned_operator = pin.get("operator_authority")
            authority_valid = authority == "LEDGER_OPERATOR_AUTHORITY" or (
                isinstance(pinned_operator, Mapping) and authority == pinned_operator
            )
            if set(row) != common | {"release_text", "decision_authority"} or not authority_valid or row.get("release_text") != str(release.text).strip():
                raise ValueError("REDELIVERY_OPERATOR_DROP_RELEASE_MAPPING_INVALID")
        else:
            raise ValueError("REDELIVERY_OPERATOR_DROP_ROW_INVALID")
        release_index += 1
    if release_index != len(baseline):
        raise ValueError("REDELIVERY_OPERATOR_DROP_RELEASE_MAPPING_INVALID")
    return _render(baseline, [cue.text for cue in baseline])


def _read_config(
    config: Mapping[str, Any], *, spec_parent: Path
) -> tuple[str, Path, str, str]:
    schema_version = config.get("schema_version")
    if schema_version not in {SCHEMA_VERSION, SCHEMA_VERSION_V2}:
        raise ValueError("REDELIVERY_BASELINE_SCHEMA_INVALID")
    if config.get("mode") != MODE:
        raise ValueError("REDELIVERY_BASELINE_MODE_INVALID")
    raw_path = config.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("REDELIVERY_BASELINE_PATH_MISSING")
    path = Path(raw_path)
    if not path.is_absolute():
        path = spec_parent / path
    if not path.is_file() or path.is_symlink():
        raise ValueError("REDELIVERY_BASELINE_PATH_INVALID")
    path = path.resolve()
    expected_sha256 = _read_sha256(
        config.get("sha256"),
        reason_code="REDELIVERY_BASELINE_SHA256_INVALID",
    )
    authority = str(config.get("authority") or "").strip()
    if not authority:
        raise ValueError("REDELIVERY_BASELINE_AUTHORITY_MISSING")
    return str(schema_version), path, expected_sha256, authority


def _fail(
    current_srt: str,
    audit: dict[str, Any],
    reason_code: str,
    **details: Any,
) -> tuple[str, dict[str, Any]]:
    audit["status"] = "FAILED"
    audit["failures"].append({"reason_code": reason_code, **details})
    audit["output_sha256"] = audit["current_input_sha256"]
    return current_srt, audit


def _absolute_overlap_ms(
    left_start_ms: int,
    left_end_ms: int,
    right_start_ms: int,
    right_end_ms: int,
) -> int:
    return max(
        0,
        min(left_end_ms, right_end_ms) - max(left_start_ms, right_start_ms),
    )


def _fully_protected_absolute(
    start_ms: int,
    end_ms: int,
    windows: Sequence[tuple[int, int]],
) -> bool:
    return any(
        window_start_ms <= start_ms and end_ms <= window_end_ms
        for window_start_ms, window_end_ms in windows
    )


def _partially_protected_absolute(
    start_ms: int,
    end_ms: int,
    windows: Sequence[tuple[int, int]],
) -> bool:
    return any(
        _absolute_overlap_ms(start_ms, end_ms, window_start_ms, window_end_ms)
        >= MIN_ALIGNMENT_OVERLAP_MS
        and not (window_start_ms <= start_ms and end_ms <= window_end_ms)
        for window_start_ms, window_end_ms in windows
    )


def _uncovered_boundary_straddle(
    *,
    absolute_start_ms: int,
    absolute_end_ms: int,
    timeline: _V2Timeline,
    reviewed_cue_support: Sequence[tuple[int, int]],
) -> tuple[str, int] | None:
    """Classify a cue crossing only reviewed leading or trailing air.

    The v2 coverage envelope binds the source interval, but an SRT may leave
    silence before its first cue or after its last cue.  A newly extended cue
    may cross that envelope while remaining half-open disjoint from every
    reviewed cue.  Preserve only that edge case; even one millisecond of actual
    reviewed-cue overlap remains fail-closed.
    """

    if not reviewed_cue_support or any(
        _absolute_overlap_ms(
            absolute_start_ms,
            absolute_end_ms,
            reviewed_start_ms,
            reviewed_end_ms,
        )
        > 0
        for reviewed_start_ms, reviewed_end_ms in reviewed_cue_support
    ):
        return None
    first_reviewed_start_ms = min(
        start_ms for start_ms, _end_ms in reviewed_cue_support
    )
    last_reviewed_end_ms = max(
        end_ms for _start_ms, end_ms in reviewed_cue_support
    )
    if (
        absolute_start_ms < timeline.effective_start_ms
        and absolute_end_ms <= first_reviewed_start_ms
    ):
        return "prefix", first_reviewed_start_ms
    if (
        absolute_end_ms > timeline.effective_end_ms
        and absolute_start_ms >= last_reviewed_end_ms
    ):
        return "tail", last_reviewed_end_ms
    return None


def _read_v2_timeline(
    current_srt: str,
    *,
    config: Mapping[str, Any],
    audit: dict[str, Any],
    protected_windows: Sequence[tuple[int, int]],
    current_source_start_ms: int | None,
    current_source_end_ms: int | None,
    current_source_recording_basename: str | None,
    current_source_sha256: str | None,
) -> _V2Timeline | None:
    baseline_start_raw = config.get("absolute_source_start_ms")
    baseline_end_raw = config.get("absolute_source_end_ms")
    if not _valid_int(baseline_start_raw) or baseline_start_raw < 0:
        _fail(
            current_srt,
            audit,
            "REDELIVERY_BASELINE_ABSOLUTE_SOURCE_START_INVALID",
        )
        return None
    if (
        not _valid_int(baseline_end_raw)
        or baseline_end_raw <= baseline_start_raw
    ):
        _fail(
            current_srt,
            audit,
            "REDELIVERY_BASELINE_ABSOLUTE_SOURCE_END_INVALID",
        )
        return None
    try:
        expected_recording_basename = _read_recording_basename(
            config.get("source_recording_basename"),
            reason_code="REDELIVERY_BASELINE_SOURCE_RECORDING_BASENAME_INVALID",
        )
        expected_source_sha256 = _read_sha256(
            config.get("source_sha256"),
            reason_code="REDELIVERY_BASELINE_SOURCE_SHA256_INVALID",
        )
        actual_recording_basename = _read_recording_basename(
            current_source_recording_basename,
            reason_code="REDELIVERY_CURRENT_SOURCE_RECORDING_BASENAME_INVALID",
        )
        actual_source_sha256 = _read_sha256(
            current_source_sha256,
            reason_code="REDELIVERY_CURRENT_SOURCE_SHA256_INVALID",
        )
    except ValueError as exc:
        _fail(current_srt, audit, str(exc))
        return None

    if not _valid_int(current_source_start_ms) or current_source_start_ms < 0:
        _fail(
            current_srt,
            audit,
            "REDELIVERY_CURRENT_ABSOLUTE_SOURCE_START_INVALID",
        )
        return None
    if (
        not _valid_int(current_source_end_ms)
        or current_source_end_ms <= current_source_start_ms
    ):
        _fail(
            current_srt,
            audit,
            "REDELIVERY_CURRENT_ABSOLUTE_SOURCE_END_INVALID",
        )
        return None

    baseline_start_ms = int(baseline_start_raw)
    baseline_end_ms = int(baseline_end_raw)
    current_start_ms = int(current_source_start_ms)
    current_end_ms = int(current_source_end_ms)
    audit.update(
        {
            "reviewed_coverage": {
                "absolute_source_start_ms": baseline_start_ms,
                "absolute_source_end_ms": baseline_end_ms,
            },
            "current_source_interval": {
                "absolute_source_start_ms": current_start_ms,
                "absolute_source_end_ms": current_end_ms,
            },
            "source_recording_identity": {
                "expected_basename": expected_recording_basename,
                "current_basename": actual_recording_basename,
                "expected_sha256": expected_source_sha256,
                "current_sha256": actual_source_sha256,
            },
            "uncovered_current_intervals": [],
            "uncovered_current_cues": [],
            "omitted_by_new_boundary": [],
            "protected_absolute_intervals": [],
        }
    )
    if actual_recording_basename != expected_recording_basename:
        _fail(
            current_srt,
            audit,
            "REDELIVERY_SOURCE_RECORDING_BASENAME_MISMATCH",
            expected=expected_recording_basename,
            current=actual_recording_basename,
        )
        return None
    if actual_source_sha256 != expected_source_sha256:
        _fail(
            current_srt,
            audit,
            "REDELIVERY_SOURCE_RECORDING_SHA256_MISMATCH",
            expected=expected_source_sha256,
            current=actual_source_sha256,
        )
        return None

    current_duration_ms = current_end_ms - current_start_ms
    absolute_protected_windows: list[tuple[int, int]] = []
    for start_ms, end_ms in protected_windows:
        if (
            not _valid_int(start_ms)
            or not _valid_int(end_ms)
            or start_ms < 0
            or end_ms <= start_ms
            or end_ms > current_duration_ms
        ):
            _fail(
                current_srt,
                audit,
                "REDELIVERY_PROTECTED_INTERVAL_INVALID",
                start_ms=start_ms,
                end_ms=end_ms,
            )
            return None
        absolute_start_ms = current_start_ms + start_ms
        absolute_end_ms = current_start_ms + end_ms
        absolute_protected_windows.append((absolute_start_ms, absolute_end_ms))
        audit["protected_absolute_intervals"].append(
            {
                "absolute_source_start_ms": absolute_start_ms,
                "absolute_source_end_ms": absolute_end_ms,
            }
        )

    effective_start_ms = max(baseline_start_ms, current_start_ms)
    effective_end_ms = min(baseline_end_ms, current_end_ms)
    audit["effective_reviewed_overlap"] = {
        "absolute_source_start_ms": effective_start_ms,
        "absolute_source_end_ms": effective_end_ms,
    }
    if current_start_ms < min(current_end_ms, baseline_start_ms):
        absolute_end_ms = min(current_end_ms, baseline_start_ms)
        audit["uncovered_current_intervals"].append(
            {
                "position": "prefix",
                "absolute_source_start_ms": current_start_ms,
                "absolute_source_end_ms": absolute_end_ms,
                "local_start_ms": 0,
                "local_end_ms": absolute_end_ms - current_start_ms,
            }
        )
    if max(current_start_ms, baseline_end_ms) < current_end_ms:
        absolute_start_ms = max(current_start_ms, baseline_end_ms)
        audit["uncovered_current_intervals"].append(
            {
                "position": "tail",
                "absolute_source_start_ms": absolute_start_ms,
                "absolute_source_end_ms": current_end_ms,
                "local_start_ms": absolute_start_ms - current_start_ms,
                "local_end_ms": current_end_ms - current_start_ms,
            }
        )
    if effective_start_ms >= effective_end_ms:
        _fail(
            current_srt,
            audit,
            "REDELIVERY_REVIEWED_COVERAGE_DISJOINT",
        )
        return None
    return _V2Timeline(
        baseline_start_ms=baseline_start_ms,
        baseline_end_ms=baseline_end_ms,
        current_start_ms=current_start_ms,
        current_end_ms=current_end_ms,
        current_duration_ms=current_duration_ms,
        effective_start_ms=effective_start_ms,
        effective_end_ms=effective_end_ms,
        protected_windows=tuple(absolute_protected_windows),
    )


def _classify_v2_cues(
    *,
    current: Sequence[SrtCue],
    baseline: Sequence[SrtCue],
    timeline: _V2Timeline,
    audit: dict[str, Any],
) -> _V2CueRows:
    baseline_rows: dict[int, tuple[int, int]] = {}
    reviewed_cue_support: list[tuple[int, int]] = []
    baseline_duration_ms = (
        timeline.baseline_end_ms - timeline.baseline_start_ms
    )
    for baseline_index, cue in enumerate(baseline):
        absolute_start_ms = timeline.baseline_start_ms + cue.start_ms
        absolute_end_ms = timeline.baseline_start_ms + cue.end_ms
        if (
            cue.start_ms < 0
            or cue.end_ms <= cue.start_ms
            or cue.end_ms > baseline_duration_ms
        ):
            audit["failures"].append(
                {
                    "reason_code": "REDELIVERY_BASELINE_CUE_OUTSIDE_REVIEWED_COVERAGE",
                    "baseline_cue_index": baseline_index + 1,
                    "start_ms": cue.start_ms,
                    "end_ms": cue.end_ms,
                }
            )
            continue
        if (
            absolute_end_ms <= timeline.effective_start_ms
            or absolute_start_ms >= timeline.effective_end_ms
        ):
            audit["omitted_by_new_boundary"].append(
                {
                    "baseline_cue_index": baseline_index + 1,
                    "absolute_source_start_ms": absolute_start_ms,
                    "absolute_source_end_ms": absolute_end_ms,
                    "text": cue.text,
                }
            )
            continue
        if (
            absolute_start_ms < timeline.effective_start_ms
            or absolute_end_ms > timeline.effective_end_ms
        ):
            audit["failures"].append(
                {
                    "reason_code": "REDELIVERY_BASELINE_CUE_CUT_BY_NEW_BOUNDARY",
                    "baseline_cue_index": baseline_index + 1,
                    "absolute_source_start_ms": absolute_start_ms,
                    "absolute_source_end_ms": absolute_end_ms,
                }
            )
            continue
        reviewed_cue_support.append((absolute_start_ms, absolute_end_ms))
        if _partially_protected_absolute(
            absolute_start_ms,
            absolute_end_ms,
            timeline.protected_windows,
        ):
            audit["failures"].append(
                {
                    "reason_code": "REDELIVERY_BASELINE_CUE_PARTIALLY_PROTECTED",
                    "baseline_cue_index": baseline_index + 1,
                }
            )
            continue
        if _fully_protected_absolute(
            absolute_start_ms,
            absolute_end_ms,
            timeline.protected_windows,
        ):
            continue
        baseline_rows[baseline_index] = (absolute_start_ms, absolute_end_ms)

    current_rows: dict[int, tuple[int, int]] = {}
    protected_current_count = 0
    for current_index, cue in enumerate(current):
        absolute_start_ms = timeline.current_start_ms + cue.start_ms
        absolute_end_ms = timeline.current_start_ms + cue.end_ms
        if (
            cue.start_ms < 0
            or cue.end_ms <= cue.start_ms
            or cue.end_ms > timeline.current_duration_ms
        ):
            audit["failures"].append(
                {
                    "reason_code": "REDELIVERY_CURRENT_CUE_OUTSIDE_SOURCE_INTERVAL",
                    "current_cue_index": current_index + 1,
                    "start_ms": cue.start_ms,
                    "end_ms": cue.end_ms,
                }
            )
            continue
        if (
            absolute_end_ms <= timeline.effective_start_ms
            or absolute_start_ms >= timeline.effective_end_ms
        ):
            audit["uncovered_current_cues"].append(
                {
                    "current_cue_index": current_index + 1,
                    "absolute_source_start_ms": absolute_start_ms,
                    "absolute_source_end_ms": absolute_end_ms,
                    "text": cue.text,
                }
            )
            continue
        if (
            absolute_start_ms < timeline.effective_start_ms
            or absolute_end_ms > timeline.effective_end_ms
        ):
            uncovered_boundary = _uncovered_boundary_straddle(
                absolute_start_ms=absolute_start_ms,
                absolute_end_ms=absolute_end_ms,
                timeline=timeline,
                reviewed_cue_support=reviewed_cue_support,
            )
            if uncovered_boundary is not None:
                boundary_position, support_boundary_ms = uncovered_boundary
                audit["uncovered_current_cues"].append(
                    {
                        "current_cue_index": current_index + 1,
                        "absolute_source_start_ms": absolute_start_ms,
                        "absolute_source_end_ms": absolute_end_ms,
                        "text": cue.text,
                        "boundary_position": boundary_position,
                        "classification": (
                            "BOUNDARY_STRADDLE_WITHOUT_REVIEWED_CUE_OVERLAP"
                        ),
                        "reviewed_cue_support_boundary_ms": (
                            support_boundary_ms
                        ),
                    }
                )
                continue
            audit["failures"].append(
                {
                    "reason_code": "REDELIVERY_CURRENT_CUE_STRADDLES_REVIEWED_COVERAGE",
                    "current_cue_index": current_index + 1,
                    "absolute_source_start_ms": absolute_start_ms,
                    "absolute_source_end_ms": absolute_end_ms,
                }
            )
            continue
        if _partially_protected_absolute(
            absolute_start_ms,
            absolute_end_ms,
            timeline.protected_windows,
        ):
            audit["failures"].append(
                {
                    "reason_code": "REDELIVERY_CURRENT_CUE_PARTIALLY_PROTECTED",
                    "current_cue_index": current_index + 1,
                }
            )
            continue
        if _fully_protected_absolute(
            absolute_start_ms,
            absolute_end_ms,
            timeline.protected_windows,
        ):
            protected_current_count += 1
            continue
        current_rows[current_index] = (absolute_start_ms, absolute_end_ms)
    return _V2CueRows(
        baseline=baseline_rows,
        current=current_rows,
        protected_current_count=protected_current_count,
    )


def _build_v2_alignment(rows: _V2CueRows) -> _V2Alignment:
    strong_by_current: dict[int, list[tuple[int, int, float, int, int]]] = {}
    raw_by_current: dict[int, list[int]] = {}
    strong_by_baseline: dict[int, list[int]] = {
        baseline_index: [] for baseline_index in rows.baseline
    }
    raw_by_baseline: dict[int, list[int]] = {
        baseline_index: [] for baseline_index in rows.baseline
    }
    for current_index, (current_abs_start, current_abs_end) in rows.current.items():
        strong_candidates: list[tuple[int, int, float, int, int]] = []
        raw_candidates: list[int] = []
        current_duration = current_abs_end - current_abs_start
        for baseline_index, (
            baseline_abs_start,
            baseline_abs_end,
        ) in rows.baseline.items():
            overlap = _absolute_overlap_ms(
                current_abs_start,
                current_abs_end,
                baseline_abs_start,
                baseline_abs_end,
            )
            if overlap < MIN_ALIGNMENT_OVERLAP_MS:
                continue
            raw_candidates.append(baseline_index)
            raw_by_baseline[baseline_index].append(current_index)
            baseline_duration = baseline_abs_end - baseline_abs_start
            ratio = overlap / max(1, current_duration, baseline_duration)
            start_drift = abs(current_abs_start - baseline_abs_start)
            end_drift = abs(current_abs_end - baseline_abs_end)
            if (
                ratio >= MIN_ALIGNMENT_RATIO
                and start_drift <= MAX_ALIGNMENT_BOUNDARY_DRIFT_MS
                and end_drift <= MAX_ALIGNMENT_BOUNDARY_DRIFT_MS
            ):
                strong_candidates.append(
                    (
                        baseline_index,
                        overlap,
                        ratio,
                        start_drift,
                        end_drift,
                    )
                )
                strong_by_baseline[baseline_index].append(current_index)
        strong_by_current[current_index] = strong_candidates
        raw_by_current[current_index] = raw_candidates
    return _V2Alignment(
        strong_by_current=strong_by_current,
        raw_by_current=raw_by_current,
        strong_by_baseline=strong_by_baseline,
        raw_by_baseline=raw_by_baseline,
    )


def _release_grade_merge_equivalent(
    current_cue: SrtCue,
    baseline_indexes: Sequence[int],
    baseline: Sequence[SrtCue],
) -> bool:
    """当前 cue 是否为基线相邻 cue 的发布级合并（1863 案，2026-07-27）。

    生产端贴邻合并（merge_release_grade_cues）把 <300ms 残片/单字并入邻居，
    文本=拼接（归一化后「，」消失）、时窗=并集。恒等比较必须认这个形状，
    否则合并版 redelivery 永久失败。任何文本或时窗越界仍拒。
    """

    indexes = sorted(int(i) for i in baseline_indexes)
    if len(indexes) < 2 or indexes != list(range(indexes[0], indexes[-1] + 1)):
        return False
    from src.autoslice.chat_evidence import normalize_chat_text

    joined = normalize_chat_text(
        "".join(baseline[i].text for i in indexes)
    )
    if not joined or normalize_chat_text(current_cue.text) != joined:
        return False
    union_start = min(baseline[i].start_ms for i in indexes)
    union_end = max(baseline[i].end_ms for i in indexes)
    return (
        abs(current_cue.start_ms - union_start)
        <= MAX_RELEASE_GRADE_MERGE_BOUNDARY_DRIFT_MS
        and abs(current_cue.end_ms - union_end)
        <= MAX_RELEASE_GRADE_MERGE_BOUNDARY_DRIFT_MS
    )


def _append_v2_alignment_failures(
    *,
    current: Sequence[SrtCue],
    baseline: Sequence[SrtCue],
    timeline: _V2Timeline,
    alignment: _V2Alignment,
    audit: dict[str, Any],
) -> None:
    merge_consumed_baseline: set[int] = set()
    for current_index, candidates in alignment.strong_by_current.items():
        cue = current[current_index]
        if not candidates:
            raw_candidates = alignment.raw_by_current[current_index]
            if len(raw_candidates) > 1 and _release_grade_merge_equivalent(
                cue, raw_candidates, baseline
            ):
                merge_consumed_baseline.update(
                    int(i) for i in raw_candidates
                )
                audit.setdefault("accepted_release_grade_merges", []).append(
                    {
                        "current_cue_index": current_index + 1,
                        "baseline_cue_indexes": [
                            int(i) + 1 for i in raw_candidates
                        ],
                    }
                )
                continue
        if len(candidates) > 1:
            audit["failures"].append(
                {
                    "reason_code": "REDELIVERY_CURRENT_CUE_ALIGNMENT_AMBIGUOUS",
                    "current_cue_index": current_index + 1,
                    "baseline_cue_indexes": [
                        candidate[0] + 1 for candidate in candidates
                    ],
                }
            )
        elif not candidates:
            raw_candidates = alignment.raw_by_current[current_index]
            if len(raw_candidates) > 1:
                reason_code = "REDELIVERY_CURRENT_CUE_MERGES_BASELINE_CUES"
            elif raw_candidates:
                reason_code = "REDELIVERY_CURRENT_CUE_ALIGNMENT_DRIFT"
            else:
                reason_code = "REDELIVERY_CURRENT_CUE_UNALIGNED"
            audit["failures"].append(
                {
                    "reason_code": reason_code,
                    "current_cue_index": current_index + 1,
                    "baseline_cue_indexes": [
                        baseline_index + 1
                        for baseline_index in raw_candidates
                    ],
                    "absolute_source_start_ms": (
                        timeline.current_start_ms + cue.start_ms
                    ),
                    "absolute_source_end_ms": (
                        timeline.current_start_ms + cue.end_ms
                    ),
                }
            )

    for baseline_index, candidates in alignment.strong_by_baseline.items():
        if baseline_index in merge_consumed_baseline:
            continue  # 已被发布级合并等价消费
        cue = baseline[baseline_index]
        if len(candidates) > 1:
            audit["failures"].append(
                {
                    "reason_code": "REDELIVERY_BASELINE_CUE_SPLIT_ACROSS_CURRENT_CUES",
                    "baseline_cue_index": baseline_index + 1,
                    "current_cue_indexes": [
                        current_index + 1 for current_index in candidates
                    ],
                }
            )
        elif not candidates:
            raw_candidates = alignment.raw_by_baseline[baseline_index]
            if len(raw_candidates) > 1:
                reason_code = "REDELIVERY_BASELINE_CUE_SPLIT_ACROSS_CURRENT_CUES"
            elif raw_candidates:
                reason_code = "REDELIVERY_BASELINE_CUE_ALIGNMENT_DRIFT"
            else:
                reason_code = "REDELIVERY_BASELINE_CUE_UNCONSUMED"
            audit["failures"].append(
                {
                    "reason_code": reason_code,
                    "baseline_cue_index": baseline_index + 1,
                    "current_cue_indexes": [
                        current_index + 1 for current_index in raw_candidates
                    ],
                    "absolute_source_start_ms": (
                        timeline.baseline_start_ms + cue.start_ms
                    ),
                    "absolute_source_end_ms": (
                        timeline.baseline_start_ms + cue.end_ms
                    ),
                }
            )


def _render_v2_output(
    current_srt: str,
    *,
    current: Sequence[SrtCue],
    baseline: Sequence[SrtCue],
    rows: _V2CueRows,
    alignment: _V2Alignment,
    audit: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    texts = [cue.text for cue in current]
    changed_count = 0
    accepted_merges = {
        int(row["current_cue_index"]) - 1: [
            int(index) - 1 for index in row["baseline_cue_indexes"]
        ]
        for row in audit.get("accepted_release_grade_merges") or []
        if isinstance(row, Mapping)
        and _valid_int(row.get("current_cue_index"))
        and isinstance(row.get("baseline_cue_indexes"), list)
        and all(_valid_int(index) for index in row["baseline_cue_indexes"])
    }
    for current_index in sorted(rows.current):
        current_cue = current[current_index]
        current_abs_start, current_abs_end = rows.current[current_index]
        merged_baseline_indexes = accepted_merges.get(current_index)
        if merged_baseline_indexes is not None:
            # _append_v2_alignment_failures already proved adjacency, exact
            # normalized joined text, and the bounded union timing.  Preserve
            # the production merge's punctuation while recording one explicit
            # many-to-one authority mapping; it has no strong one-to-one row.
            baseline_abs_start = min(
                rows.baseline[index][0]
                for index in merged_baseline_indexes
            )
            baseline_abs_end = max(
                rows.baseline[index][1]
                for index in merged_baseline_indexes
            )
            overlap = _absolute_overlap_ms(
                current_abs_start,
                current_abs_end,
                baseline_abs_start,
                baseline_abs_end,
            )
            audit["mappings"].append(
                {
                    "mapping_kind": "release_grade_merge_equivalent",
                    "current_cue_index": current_index + 1,
                    "baseline_cue_indexes": [
                        index + 1 for index in merged_baseline_indexes
                    ],
                    "start_ms": current_cue.start_ms,
                    "end_ms": current_cue.end_ms,
                    "current_absolute_source_start_ms": current_abs_start,
                    "current_absolute_source_end_ms": current_abs_end,
                    "baseline_absolute_source_start_ms": (
                        baseline_abs_start
                    ),
                    "baseline_absolute_source_end_ms": baseline_abs_end,
                    "overlap_ms": overlap,
                    "start_drift_ms": abs(
                        current_abs_start - baseline_abs_start
                    ),
                    "end_drift_ms": abs(
                        current_abs_end - baseline_abs_end
                    ),
                    "changed": False,
                    "before": texts[current_index],
                    "after": texts[current_index],
                }
            )
            audit["owned_intervals"].append(
                {
                    "start_ms": current_cue.start_ms,
                    "end_ms": current_cue.end_ms,
                }
            )
            continue
        (
            baseline_index,
            overlap,
            ratio,
            start_drift,
            end_drift,
        ) = alignment.strong_by_current[current_index][0]
        baseline_cue = baseline[baseline_index]
        before = texts[current_index]
        after = baseline_cue.text
        if before != after:
            texts[current_index] = after
            changed_count += 1
        baseline_abs_start, baseline_abs_end = rows.baseline[baseline_index]
        audit["mappings"].append(
            {
                "current_cue_index": current_index + 1,
                "baseline_cue_index": baseline_index + 1,
                "start_ms": current_cue.start_ms,
                "end_ms": current_cue.end_ms,
                "current_absolute_source_start_ms": current_abs_start,
                "current_absolute_source_end_ms": current_abs_end,
                "baseline_absolute_source_start_ms": baseline_abs_start,
                "baseline_absolute_source_end_ms": baseline_abs_end,
                "overlap_ms": overlap,
                "overlap_ratio": round(ratio, 6),
                "start_drift_ms": start_drift,
                "end_drift_ms": end_drift,
                "changed": before != after,
                "before": before,
                "after": after,
            }
        )
        audit["owned_intervals"].append(
            {"start_ms": current_cue.start_ms, "end_ms": current_cue.end_ms}
        )

    output = _render(current, texts)
    audit.update(
        {
            "status": "APPLIED" if changed_count else "ALREADY_SATISFIED",
            "mapped_cue_count": len(audit["mappings"]),
            "changed_cue_count": changed_count,
            "protected_cue_count": rows.protected_current_count,
            "omitted_baseline_cue_count": len(
                audit["omitted_by_new_boundary"]
            ),
            "uncovered_current_cue_count": len(audit["uncovered_current_cues"]),
            "output_sha256": _sha256_bytes(output.encode("utf-8")),
        }
    )
    return output, audit


def _replay_exact_v2_interval(
    current_srt: str,
    *,
    current: Sequence[SrtCue],
    baseline: Sequence[SrtCue],
    timeline: _V2Timeline,
    config: Mapping[str, Any],
    audit: dict[str, Any],
    spec_parent: Path,
) -> tuple[str, dict[str, Any]] | None:
    """Replay reviewed cue timing only under an explicit exact-source grant."""

    replay = config.get("exact_interval_replay", False)
    if not isinstance(replay, bool):
        return _fail(
            current_srt,
            audit,
            "REDELIVERY_BASELINE_EXACT_INTERVAL_REPLAY_INVALID",
        )
    video_tail_extension_ms = (
        timeline.current_end_ms - timeline.baseline_end_ms
    )
    projection_receipt: dict[str, object] | None = None
    if config.get(AUTHORITY_CONFIG_KEY) is not None:
        try:
            projection_receipt = projection_materialization_receipt(
                config=config,
                baseline_cues=baseline,
                current_source_start_ms=timeline.current_start_ms,
                current_source_end_ms=timeline.current_end_ms,
                application_strategy="exact_reviewed_interval_replay",
                video_tail_extension_ms=video_tail_extension_ms,
            )
        except RedeliveryBoundaryProjectionError as exc:
            return _fail(current_srt, audit, str(exc))
    if not replay or (
        timeline.current_start_ms != timeline.baseline_start_ms
        or not (
            0
            <= video_tail_extension_ms
            <= MAX_EXACT_REPLAY_VIDEO_TAIL_MS
        )
    ):
        return None
    duration_ms = timeline.baseline_end_ms - timeline.baseline_start_ms
    invalid = [
        index + 1
        for index, cue in enumerate(baseline)
        if cue.start_ms < 0
        or cue.end_ms <= cue.start_ms
        or cue.end_ms > duration_ms
    ]
    if invalid:
        return _fail(
            current_srt,
            audit,
            "REDELIVERY_BASELINE_EXACT_REPLAY_CUE_OUTSIDE_INTERVAL",
            baseline_cue_indexes=invalid,
        )

    try:
        output = _sealed_operator_drop_release(
            config=config, baseline=baseline, spec_parent=spec_parent,
        )
    except ValueError as exc:
        return _fail(current_srt, audit, str(exc))
    if output is None:
        output = _render(baseline, [cue.text for cue in baseline])
    mismatches = sum(
        left.start_ms != right.start_ms
        or left.end_ms != right.end_ms
        or left.text != right.text
        for left, right in zip(current, baseline)
    ) + abs(len(current) - len(baseline))
    for index, cue in enumerate(baseline, start=1):
        absolute_start_ms = timeline.baseline_start_ms + cue.start_ms
        absolute_end_ms = timeline.baseline_start_ms + cue.end_ms
        pre_replay_cues = [
            {
                "current_cue_index": current_index,
                "start_ms": current_cue.start_ms,
                "end_ms": current_cue.end_ms,
                "text": current_cue.text,
            }
            for current_index, current_cue in enumerate(current, start=1)
            if _absolute_overlap_ms(
                cue.start_ms,
                cue.end_ms,
                current_cue.start_ms,
                current_cue.end_ms,
            )
            > 0
        ]
        audit["mappings"].append(
            {
                "mapping_kind": "exact_reviewed_interval_replay",
                "baseline_cue_index": index,
                "output_cue_index": index,
                "start_ms": cue.start_ms,
                "end_ms": cue.end_ms,
                "baseline_absolute_source_start_ms": absolute_start_ms,
                "baseline_absolute_source_end_ms": absolute_end_ms,
                "text": cue.text,
                # Exact replay can replace a differently segmented cue grid.
                # Retain the hash-bound input cue evidence so downstream
                # authority reconciliation can prove that a reviewed baseline
                # actually reverted a proposed surface instead of granting a
                # geometry-only exemption.
                "pre_replay_cues": pre_replay_cues,
            }
        )
        audit["owned_intervals"].append(
            {"start_ms": cue.start_ms, "end_ms": cue.end_ms}
        )
    audit.update(
        {
            "status": "APPLIED" if output != current_srt else "ALREADY_SATISFIED",
            "application_strategy": "exact_reviewed_interval_replay",
            "timing_authority": "hash_bound_reviewed_srt_exact_source_interval",
            "video_tail_extension_ms": video_tail_extension_ms,
            "current_cue_count": len(current),
            "replayed_cue_count": len(baseline),
            "mapped_cue_count": len(baseline),
            "changed_cue_count": mismatches,
            "protected_cue_count": sum(
                _fully_protected_absolute(
                    timeline.baseline_start_ms + cue.start_ms,
                    timeline.baseline_start_ms + cue.end_ms,
                    timeline.protected_windows,
                )
                for cue in baseline
            ),
            "protected_windows_replayed_for_later_source_truth": bool(
                timeline.protected_windows
            ),
            "omitted_baseline_cue_count": 0,
            "uncovered_current_cue_count": 0,
            "output_sha256": _sha256_bytes(output.encode("utf-8")),
        }
    )
    if projection_receipt is not None:
        audit["terminal_projection_materialization"] = projection_receipt
    return output, audit


def _apply_v2(
    current_srt: str,
    *,
    current: Sequence[SrtCue],
    baseline: Sequence[SrtCue],
    config: Mapping[str, Any],
    audit: dict[str, Any],
    protected_windows: Sequence[tuple[int, int]],
    current_source_start_ms: int | None,
    current_source_end_ms: int | None,
    current_source_recording_basename: str | None,
    current_source_sha256: str | None,
    spec_parent: Path,
) -> tuple[str, dict[str, Any]]:
    timeline = _read_v2_timeline(
        current_srt,
        config=config,
        audit=audit,
        protected_windows=protected_windows,
        current_source_start_ms=current_source_start_ms,
        current_source_end_ms=current_source_end_ms,
        current_source_recording_basename=current_source_recording_basename,
        current_source_sha256=current_source_sha256,
    )
    if timeline is None:
        return current_srt, audit

    exact_replay = _replay_exact_v2_interval(
        current_srt,
        current=current,
        baseline=baseline,
        timeline=timeline,
        config=config,
        audit=audit,
        spec_parent=spec_parent,
    )
    if exact_replay is not None:
        return exact_replay

    rows = _classify_v2_cues(
        current=current,
        baseline=baseline,
        timeline=timeline,
        audit=audit,
    )
    if audit["failures"]:
        audit["status"] = "FAILED"
        audit["output_sha256"] = audit["current_input_sha256"]
        return current_srt, audit

    alignment = _build_v2_alignment(rows)
    _append_v2_alignment_failures(
        current=current,
        baseline=baseline,
        timeline=timeline,
        alignment=alignment,
        audit=audit,
    )
    if audit["failures"]:
        audit["status"] = "FAILED"
        audit["output_sha256"] = audit["current_input_sha256"]
        return current_srt, audit
    return _render_v2_output(
        current_srt,
        current=current,
        baseline=baseline,
        rows=rows,
        alignment=alignment,
        audit=audit,
    )

def apply_redelivery_subtitle_baseline(
    current_srt: str,
    *,
    config: Mapping[str, Any] | None,
    spec_parent: Path,
    protected_windows: Sequence[tuple[int, int]] = (),
    current_source_start_ms: int | None = None,
    current_source_end_ms: int | None = None,
    current_source_recording_basename: str | None = None,
    current_source_sha256: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Preserve hash-bound baseline text while retaining current cue timing."""

    audit: dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "status": "SKIPPED_NOT_CONFIGURED",
        "mode": MODE,
        "baseline_path": None,
        "baseline_sha256": None,
        "current_input_sha256": _sha256_bytes(current_srt.encode("utf-8")),
        "output_sha256": None,
        "protected_intervals": [
            {"start_ms": int(start_ms), "end_ms": int(end_ms)}
            for start_ms, end_ms in protected_windows
        ],
        "owned_intervals": [],
        "mappings": [],
        "failures": [],
    }
    if config is None:
        audit["output_sha256"] = audit["current_input_sha256"]
        return current_srt, audit
    if not isinstance(config, Mapping):
        audit["status"] = "FAILED"
        audit["failures"].append({"reason_code": "REDELIVERY_BASELINE_CONFIG_INVALID"})
        audit["output_sha256"] = audit["current_input_sha256"]
        return current_srt, audit
    if config.get("schema_version") == SCHEMA_VERSION_V2:
        audit["schema_version"] = AUDIT_SCHEMA_VERSION_V2

    try:
        baseline_schema_version, path, expected_sha256, authority = _read_config(
            config, spec_parent=spec_parent
        )
        raw = path.read_bytes()
    except (OSError, ValueError) as exc:
        audit["status"] = "FAILED"
        audit["failures"].append({"reason_code": str(exc)})
        audit["output_sha256"] = audit["current_input_sha256"]
        return current_srt, audit

    actual_sha256 = _sha256_bytes(raw)
    audit.update(
        {
            "baseline_path": str(path),
            "baseline_sha256": actual_sha256,
            "expected_baseline_sha256": expected_sha256,
            "authority": authority,
            "baseline_schema_version": baseline_schema_version,
        }
    )
    if actual_sha256 != expected_sha256:
        audit["status"] = "FAILED"
        audit["failures"].append({"reason_code": "REDELIVERY_BASELINE_SHA256_MISMATCH"})
        audit["output_sha256"] = audit["current_input_sha256"]
        return current_srt, audit

    try:
        baseline_srt = raw.decode("utf-8")
    except UnicodeDecodeError:
        audit["status"] = "FAILED"
        audit["failures"].append({"reason_code": "REDELIVERY_BASELINE_UTF8_INVALID"})
        audit["output_sha256"] = audit["current_input_sha256"]
        return current_srt, audit
    if contains_speaker_label_prefix(baseline_srt):
        return _fail(
            current_srt,
            audit,
            BASELINE_CONTAINS_SPEAKER_LABEL_PREFIX,
        )
    current = parse_srt_cues(current_srt)
    baseline = parse_srt_cues(baseline_srt)
    if not current or not baseline:
        audit["status"] = "FAILED"
        audit["failures"].append({"reason_code": "REDELIVERY_BASELINE_SRT_EMPTY"})
        audit["output_sha256"] = audit["current_input_sha256"]
        return current_srt, audit
    if baseline_schema_version == SCHEMA_VERSION_V2:
        return _apply_v2(
            current_srt,
            current=current,
            baseline=baseline,
            config=config,
            audit=audit,
            protected_windows=protected_windows,
            current_source_start_ms=current_source_start_ms,
            current_source_end_ms=current_source_end_ms,
            current_source_recording_basename=current_source_recording_basename,
            current_source_sha256=current_source_sha256,
            spec_parent=spec_parent,
        )

    current_indexes = [
        index for index, cue in enumerate(current) if not _protected(cue, protected_windows)
    ]
    baseline_indexes = [
        index for index, cue in enumerate(baseline) if not _protected(cue, protected_windows)
    ]
    mappings: list[tuple[int, int, int, float]] = []
    used_baseline: set[int] = set()
    for current_index in current_indexes:
        current_cue = current[current_index]
        candidates: list[tuple[int, int, float]] = []
        for baseline_index in baseline_indexes:
            if baseline_index in used_baseline:
                continue
            baseline_cue = baseline[baseline_index]
            overlap = _overlap_ms(current_cue, baseline_cue)
            denominator = max(
                1,
                min(
                    current_cue.end_ms - current_cue.start_ms,
                    baseline_cue.end_ms - baseline_cue.start_ms,
                ),
            )
            ratio = overlap / denominator
            if overlap >= MIN_ALIGNMENT_OVERLAP_MS and ratio >= MIN_ALIGNMENT_RATIO:
                candidates.append((baseline_index, overlap, ratio))
        candidates.sort(key=lambda row: (row[1], row[2]), reverse=True)
        if not candidates:
            audit["failures"].append(
                {
                    "reason_code": "REDELIVERY_CURRENT_CUE_UNALIGNED",
                    "current_cue_index": current_index + 1,
                    "start_ms": current_cue.start_ms,
                    "end_ms": current_cue.end_ms,
                    "text": current_cue.text,
                }
            )
            continue
        if len(candidates) > 1 and candidates[0][1:] == candidates[1][1:]:
            audit["failures"].append(
                {
                    "reason_code": "REDELIVERY_CURRENT_CUE_ALIGNMENT_AMBIGUOUS",
                    "current_cue_index": current_index + 1,
                    "baseline_cue_indexes": [row[0] + 1 for row in candidates[:2]],
                }
            )
            continue
        baseline_index, overlap, ratio = candidates[0]
        used_baseline.add(baseline_index)
        mappings.append((current_index, baseline_index, overlap, ratio))

    for baseline_index in baseline_indexes:
        if baseline_index not in used_baseline:
            baseline_cue = baseline[baseline_index]
            audit["failures"].append(
                {
                    "reason_code": "REDELIVERY_BASELINE_CUE_UNCONSUMED",
                    "baseline_cue_index": baseline_index + 1,
                    "start_ms": baseline_cue.start_ms,
                    "end_ms": baseline_cue.end_ms,
                    "text": baseline_cue.text,
                }
            )

    if audit["failures"]:
        audit["status"] = "FAILED"
        audit["output_sha256"] = audit["current_input_sha256"]
        return current_srt, audit

    texts = [cue.text for cue in current]
    changed_count = 0
    for current_index, baseline_index, overlap, ratio in mappings:
        current_cue = current[current_index]
        baseline_cue = baseline[baseline_index]
        before = texts[current_index]
        after = baseline_cue.text
        if before != after:
            changed_count += 1
            texts[current_index] = after
        audit["mappings"].append(
            {
                "current_cue_index": current_index + 1,
                "baseline_cue_index": baseline_index + 1,
                "start_ms": current_cue.start_ms,
                "end_ms": current_cue.end_ms,
                "overlap_ms": overlap,
                "overlap_ratio": round(ratio, 6),
                "changed": before != after,
                "before": before,
                "after": after,
            }
        )
        audit["owned_intervals"].append(
            {"start_ms": current_cue.start_ms, "end_ms": current_cue.end_ms}
        )

    output = _render(current, texts)
    audit.update(
        {
            "status": "APPLIED" if changed_count else "ALREADY_SATISFIED",
            "mapped_cue_count": len(mappings),
            "changed_cue_count": changed_count,
            "protected_cue_count": len(current) - len(current_indexes),
            "output_sha256": _sha256_bytes(output.encode("utf-8")),
        }
    )
    return output, audit
