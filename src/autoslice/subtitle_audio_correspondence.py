"""Check subtitle timing against a timed witness from the same media.

This module is deliberately a small, provider-free correspondence check.  It
does not transcribe audio and it never edits subtitle text.  A witness SRT is
treated as timing evidence only after its provenance binds the witness bytes
and the actual media bytes, and declares both timebases in integer
milliseconds.

The supported mapping is explicit and intentionally narrow::

    witness_media_ms = final_delivery_ms + intro_offset_ms

The offset is applied exactly once.  The checker finds long, unique textual
anchors with standard-library normalization and :mod:`difflib`, then compares
their observed witness starts with the transformed final starts.  It requires
three distinct anchors covering the beginning, middle, and end of the clip.
Proper-name or other wording errors remain outside this check: the text status
is reported as ``UNASSESSED`` rather than becoming a repair request.

Provenance schema (``subtitle-audio-correspondence-provenance.v1``)::

    {
      "schema_version": "subtitle-audio-correspondence-provenance.v1",
      "actual_media_sha256": "<hex or sha256:<hex>>",
      "witness_srt_sha256": "<hex or sha256:<hex>>",
      "timebase": {
        "unit": "ms",
        "final_srt": "delivery_local_ms",
        "witness_srt": "actual_media_local_ms",
        "intro_offset_application": "add_once_to_final_srt"
      }
    }

An optional ``timebase.declared_intro_offset_ms`` is accepted only when it is
an integer and equals the offset supplied to :func:`check_subtitle_audio_correspondence`.
The checker computes both file hashes itself; a self-reported ``PASS`` field
does not substitute for those checks.

The existing ``subtitle-audio-witness-provenance.v1`` sidecar is also accepted
when it contains ``media_sha256``, ``witness_srt_sha256``,
``time_domain=FINAL_MEDIA_LOCAL``, and integer ``time_origin_ms=0``.  That
legacy shape is normalized to the mapping above; its provider field is never
used as evidence.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import re
import statistics
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "subtitle-audio-correspondence-receipt.v1"
PROVENANCE_SCHEMA_VERSION = "subtitle-audio-correspondence-provenance.v1"
LEGACY_PROVENANCE_SCHEMA_VERSION = "subtitle-audio-witness-provenance.v1"

# These limits are intentionally conservative.  Ordinary cue-boundary
# variation is expected to be below roughly a second; a 1.5 s common shift or
# a 3 s front/middle/end spread is treated as a timing failure.
ANCHOR_MIN_CHARS = 6
ANCHOR_MIN_UNIQUE_CHARS = 3
ANCHOR_MIN_SIMILARITY = 0.78
ANCHOR_TIE_MARGIN = 0.05
FUZZY_START_ANCHOR_RULE = "first_nonempty_matching_block_starts_at_zero_on_both_sides"
GLOBAL_SHIFT_THRESHOLD_MS = 1_500
INCONSISTENT_SPREAD_THRESHOLD_MS = 3_000
INCONSISTENT_MAD_THRESHOLD_MS = 1_000
MIN_ANCHORS = 3

_SRT_TIME_RE = re.compile(
    r"^(?P<hours>\d+):(?P<minutes>[0-5]\d):(?P<seconds>[0-5]\d)[,.](?P<millis>\d{1,3})$"
)
_SHA256_RE = re.compile(r"^(?:sha256:)?(?P<hex>[0-9a-fA-F]{64})$")

# Short conversational words are common enough to create false matches even
# when they are unique in a tiny witness file.  Longer phrases containing one
# of these words remain eligible.
_COMMON_SHORT_TEXT = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "good",
        "hello",
        "hey",
        "i",
        "it",
        "me",
        "no",
        "okay",
        "ok",
        "the",
        "this",
        "we",
        "yes",
        "you",
        "啊",
        "吧",
        "不",
        "的",
        "对",
        "好",
        "好吧",
        "嗯",
        "哦",
        "了",
        "吗",
        "那",
        "你",
        "你们",
        "我",
        "我们",
        "是",
        "他",
        "她",
        "这",
        "然后",
        "现在",
        "可以",
        "看看",
        "怎么",
    }
)


class CorrespondenceInputError(ValueError):
    """Raised when media, witness, SRT, or provenance cannot be trusted."""

    def __init__(self, reason_code: str, detail: str, context: Mapping[str, Any] | None = None):
        self.reason_code = reason_code
        self.detail = detail
        self.context = dict(context or {})
        super().__init__(f"{reason_code}: {detail}")


@dataclass(frozen=True)
class TimedCue:
    index: str
    start_ms: int
    end_ms: int
    text: str

    @property
    def normalized_text(self) -> str:
        return normalize_text(self.text)


@dataclass(frozen=True)
class _Anchor:
    final: TimedCue
    witness: TimedCue
    similarity: float
    final_start_aligned_ms: int
    start_delta_ms: int
    end_delta_ms: int


def _regular_file(path: str | Path, *, label: str) -> Path:
    candidate = Path(path)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CorrespondenceInputError("INPUT_FILE_UNAVAILABLE", f"{label}: {candidate}: {exc}") from exc
    if candidate.is_symlink() or not resolved.is_file():
        raise CorrespondenceInputError("INPUT_NOT_REGULAR_FILE", f"{label}: {candidate}")
    return resolved


def sha256_file(path: str | Path) -> str:
    """Return a canonical ``sha256:<hex>`` digest for a regular file."""

    file_path = _regular_file(path, label="hash input")
    digest = hashlib.sha256()
    try:
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise CorrespondenceInputError("INPUT_READ_FAILED", f"{file_path}: {exc}") from exc
    return f"sha256:{digest.hexdigest()}"


def normalize_text(text: str) -> str:
    """Normalize text for correspondence matching without changing its text."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(
        character
        for character in normalized
        if unicodedata.category(character)[0] in {"L", "N"}
    )


def _parse_srt_time(value: str, *, block_number: int) -> int:
    match = _SRT_TIME_RE.fullmatch(value.strip())
    if match is None:
        raise CorrespondenceInputError(
            "SRT_TIME_NOT_INTEGER_MS",
            f"block {block_number}: invalid time {value!r}; expected HH:MM:SS,mmm",
        )
    millis_text = match.group("millis")
    millis = int(millis_text.ljust(3, "0"))
    hours = int(match.group("hours"))
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    result = ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis
    if not isinstance(result, int) or not math.isfinite(result):  # defensive contract guard
        raise CorrespondenceInputError("SRT_TIME_NOT_INTEGER_MS", f"block {block_number}: {value!r}")
    return result


def parse_timed_srt(srt_text: str, *, label: str = "SRT") -> list[TimedCue]:
    """Parse a strict, finite integer-millisecond SRT cue list."""

    if not isinstance(srt_text, str):
        raise CorrespondenceInputError("SRT_NOT_TEXT", f"{label} is not text")
    raw = srt_text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[TimedCue] = []
    seen_indexes: set[str] = set()
    previous_start_ms = -1
    blocks = [block for block in re.split(r"\n{2,}", raw) if block.strip()]
    if not blocks:
        raise CorrespondenceInputError("SRT_EMPTY", f"{label} has no cues")

    for block_number, block in enumerate(blocks, start=1):
        lines = block.splitlines()
        if len(lines) < 3:
            raise CorrespondenceInputError("SRT_BLOCK_INVALID", f"{label} block {block_number} is incomplete")
        index = lines[0].strip()
        timing_line = lines[1].strip()
        if not index or "-->" not in timing_line:
            raise CorrespondenceInputError("SRT_BLOCK_INVALID", f"{label} block {block_number} has no timing line")
        if index in seen_indexes:
            raise CorrespondenceInputError("SRT_DUPLICATE_INDEX", f"{label} index {index!r} repeats")
        start_raw, end_raw = (part.strip() for part in timing_line.split("-->", 1))
        start_ms = _parse_srt_time(start_raw, block_number=block_number)
        end_ms = _parse_srt_time(end_raw, block_number=block_number)
        if end_ms <= start_ms:
            raise CorrespondenceInputError(
                "SRT_INTERVAL_INVALID",
                f"{label} block {block_number}: end must be after start",
            )
        if start_ms < previous_start_ms:
            raise CorrespondenceInputError(
                "SRT_NOT_TIME_ORDERED",
                f"{label} block {block_number}: start time moves backwards",
            )
        text = "\n".join(lines[2:]).strip()
        if not text:
            raise CorrespondenceInputError("SRT_TEXT_EMPTY", f"{label} block {block_number} has empty text")
        cues.append(TimedCue(index=index, start_ms=start_ms, end_ms=end_ms, text=text))
        seen_indexes.add(index)
        previous_start_ms = start_ms
    return cues


def _canonical_digest(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise CorrespondenceInputError("PROVENANCE_HASH_INVALID", f"{field} must be a SHA-256 string")
    match = _SHA256_RE.fullmatch(value.strip())
    if match is None:
        raise CorrespondenceInputError("PROVENANCE_HASH_INVALID", f"{field} is not a SHA-256 digest")
    return f"sha256:{match.group('hex').lower()}"


def _load_provenance(path: str | Path) -> tuple[dict[str, Any], Path, str]:
    provenance_path = _regular_file(path, label="provenance JSON")
    provenance_sha256 = sha256_file(provenance_path)
    try:
        value = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CorrespondenceInputError("PROVENANCE_JSON_INVALID", f"{provenance_path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CorrespondenceInputError("PROVENANCE_JSON_INVALID", "root must be an object")
    return value, provenance_path, provenance_sha256


def _validate_provenance(
    provenance: Mapping[str, Any],
    *,
    observed_media_sha256: str,
    observed_witness_sha256: str,
    intro_offset_ms: int,
) -> dict[str, Any]:
    source_schema_version = provenance.get("schema_version")
    if source_schema_version == LEGACY_PROVENANCE_SCHEMA_VERSION:
        expected_media_sha256 = _canonical_digest(
            provenance.get("media_sha256"), field="media_sha256"
        )
        expected_witness_sha256 = _canonical_digest(
            provenance.get("witness_srt_sha256"), field="witness_srt_sha256"
        )
        if provenance.get("time_domain") != "FINAL_MEDIA_LOCAL":
            raise CorrespondenceInputError(
                "PROVENANCE_TIMEBASE_INVALID",
                "time_domain must be 'FINAL_MEDIA_LOCAL'",
            )
        time_origin_ms = provenance.get("time_origin_ms")
        if isinstance(time_origin_ms, bool) or not isinstance(time_origin_ms, int):
            raise CorrespondenceInputError(
                "PROVENANCE_TIMEBASE_NOT_INTEGER_MS",
                "time_origin_ms must be an integer",
            )
        if time_origin_ms != 0:
            raise CorrespondenceInputError(
                "PROVENANCE_TIMEBASE_INVALID",
                "time_origin_ms must be 0 for FINAL_MEDIA_LOCAL",
            )
        normalized_timebase = {
            "unit": "ms",
            "final_srt": "delivery_local_ms",
            "witness_srt": "actual_media_local_ms",
            "intro_offset_application": "add_once_to_final_srt",
            "provenance_time_domain": provenance["time_domain"],
            "provenance_time_origin_ms": time_origin_ms,
        }
        declared_offset = None
    elif source_schema_version == PROVENANCE_SCHEMA_VERSION:
        expected_media_sha256 = _canonical_digest(
            provenance.get("actual_media_sha256"), field="actual_media_sha256"
        )
        expected_witness_sha256 = _canonical_digest(
            provenance.get("witness_srt_sha256"), field="witness_srt_sha256"
        )
        timebase = provenance.get("timebase")
        if not isinstance(timebase, Mapping):
            raise CorrespondenceInputError("PROVENANCE_TIMEBASE_MISSING", "timebase object is required")
        expected = {
            "unit": "ms",
            "final_srt": "delivery_local_ms",
            "witness_srt": "actual_media_local_ms",
            "intro_offset_application": "add_once_to_final_srt",
        }
        for key, expected_value in expected.items():
            if timebase.get(key) != expected_value:
                raise CorrespondenceInputError(
                    "PROVENANCE_TIMEBASE_INVALID",
                    f"timebase.{key} must be {expected_value!r}",
                )
        normalized_timebase = dict(timebase)
        declared_offset = timebase.get("declared_intro_offset_ms")
    else:
        raise CorrespondenceInputError(
            "PROVENANCE_SCHEMA_UNSUPPORTED",
            f"expected {PROVENANCE_SCHEMA_VERSION!r} or {LEGACY_PROVENANCE_SCHEMA_VERSION!r}",
        )
    if expected_media_sha256 != observed_media_sha256:
        raise CorrespondenceInputError(
            "PROVENANCE_MEDIA_HASH_MISMATCH",
            f"declared {expected_media_sha256}, observed {observed_media_sha256}",
            {"declared_media_sha256": expected_media_sha256, "observed_media_sha256": observed_media_sha256},
        )
    if expected_witness_sha256 != observed_witness_sha256:
        raise CorrespondenceInputError(
            "PROVENANCE_WITNESS_SRT_HASH_MISMATCH",
            f"declared {expected_witness_sha256}, observed {observed_witness_sha256}",
            {
                "declared_witness_srt_sha256": expected_witness_sha256,
                "observed_witness_srt_sha256": observed_witness_sha256,
            },
        )

    if declared_offset is not None:
        if isinstance(declared_offset, bool) or not isinstance(declared_offset, int):
            raise CorrespondenceInputError(
                "PROVENANCE_TIMEBASE_NOT_INTEGER_MS",
                "timebase.declared_intro_offset_ms must be an integer",
            )
        if declared_offset != intro_offset_ms:
            raise CorrespondenceInputError(
                "PROVENANCE_INTRO_OFFSET_MISMATCH",
                f"declared {declared_offset}, supplied {intro_offset_ms}",
            )
    return {
        "schema_version": source_schema_version,
        "actual_media_sha256": expected_media_sha256,
        "witness_srt_sha256": expected_witness_sha256,
        "timebase": normalized_timebase,
        "status_field_ignored": "PASS" in provenance and provenance.get("status") == "PASS",
    }


def _is_eligible_anchor(cue: TimedCue, occurrences: Mapping[str, int]) -> bool:
    normalized = cue.normalized_text
    if len(normalized) < ANCHOR_MIN_CHARS:
        return False
    if len(set(normalized)) < ANCHOR_MIN_UNIQUE_CHARS:
        return False
    if normalized in _COMMON_SHORT_TEXT:
        return False
    return occurrences.get(normalized, 0) == 1


def _similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    ratio = difflib.SequenceMatcher(None, left, right, autojunk=False).ratio()
    if left in right or right in left:
        ratio = max(ratio, min(len(left), len(right)) / max(len(left), len(right)))
    return ratio


def _has_shared_text_start(left: str, right: str) -> bool:
    """Return whether the first non-empty match starts each text.

    A fuzzy anchor must share its actual sentence start on both sides.  This
    prevents a later shared phrase from becoming a cue-start timing anchor;
    exact normalized matches remain eligible without this fuzzy-only rule.
    """

    first_match = next(
        (
            block
            for block in difflib.SequenceMatcher(None, left, right, autojunk=False).get_matching_blocks()
            if block.size
        ),
        None,
    )
    return first_match is not None and first_match.a == 0 and first_match.b == 0


def _find_anchors(
    final_cues: Sequence[TimedCue],
    witness_cues: Sequence[TimedCue],
    *,
    intro_offset_ms: int,
) -> list[_Anchor]:
    final_occurrences = Counter(cue.normalized_text for cue in final_cues)
    witness_occurrences = Counter(cue.normalized_text for cue in witness_cues)
    eligible_final = [cue for cue in final_cues if _is_eligible_anchor(cue, final_occurrences)]
    eligible_witness = [cue for cue in witness_cues if _is_eligible_anchor(cue, witness_occurrences)]

    candidates: list[tuple[float, TimedCue, TimedCue]] = []
    for final_cue in eligible_final:
        scored = sorted(
            [
                (
                    _similarity(final_cue.normalized_text, witness_cue.normalized_text),
                    witness_cue,
                )
                for witness_cue in eligible_witness
            ],
            key=lambda item: item[0],
            reverse=True,
        )
        scored = [
            (similarity, witness_cue)
            for similarity, witness_cue in scored
            if similarity == 1.0
            or _has_shared_text_start(final_cue.normalized_text, witness_cue.normalized_text)
        ]
        if not scored or scored[0][0] < ANCHOR_MIN_SIMILARITY:
            continue
        if (
            len(scored) > 1
            and scored[0][0] < 1.0
            and scored[0][0] - scored[1][0] < ANCHOR_TIE_MARGIN
        ):
            continue
        candidates.append((scored[0][0], final_cue, scored[0][1]))

    anchors: list[_Anchor] = []
    used_final: set[str] = set()
    used_witness: set[str] = set()
    for similarity, final_cue, witness_cue in sorted(
        candidates,
        key=lambda item: (-item[0], item[1].start_ms, item[2].start_ms),
    ):
        if final_cue.index in used_final or witness_cue.index in used_witness:
            continue
        aligned_start_ms = final_cue.start_ms + intro_offset_ms
        anchors.append(
            _Anchor(
                final=final_cue,
                witness=witness_cue,
                similarity=similarity,
                final_start_aligned_ms=aligned_start_ms,
                start_delta_ms=witness_cue.start_ms - aligned_start_ms,
                end_delta_ms=witness_cue.end_ms - (final_cue.end_ms + intro_offset_ms),
            )
        )
        used_final.add(final_cue.index)
        used_witness.add(witness_cue.index)
    return sorted(anchors, key=lambda anchor: anchor.witness.start_ms)


def _coverage(anchors: Sequence[_Anchor], timeline_end_ms: int) -> dict[str, Any]:
    if timeline_end_ms <= 0:
        return {"status": "INSUFFICIENT", "start": [], "middle": [], "end": [], "required": ["start", "middle", "end"]}
    start_cut = timeline_end_ms / 3
    end_cut = timeline_end_ms * 2 / 3
    bins: dict[str, list[str]] = {"start": [], "middle": [], "end": []}
    for anchor in anchors:
        # Coverage belongs to the main clip, excluding branding. Including a
        # long intro could move every anchor out of a short clip's first third.
        # Witness positions are also unsuitable because drift moves the bins.
        position = anchor.final.start_ms
        bucket = "start" if position < start_cut else "middle" if position < end_cut else "end"
        bins[bucket].append(anchor.final.index)
    status = "PASS" if len(anchors) >= MIN_ANCHORS and all(bins.values()) else "INSUFFICIENT"
    return {"status": status, **bins, "required": ["start", "middle", "end"]}


def _validate_intro_offset(intro_offset_ms: int) -> None:
    if isinstance(intro_offset_ms, bool) or not isinstance(intro_offset_ms, int) or intro_offset_ms < 0:
        raise CorrespondenceInputError(
            "INTRO_OFFSET_NOT_INTEGER_MS",
            "intro_offset_ms must be a non-negative integer",
        )
    if not math.isfinite(intro_offset_ms):
        raise CorrespondenceInputError("INTRO_OFFSET_NOT_INTEGER_MS", "intro_offset_ms is not finite")


def _input_error_receipt(
    error: CorrespondenceInputError,
    *,
    final_srt: str | Path,
    actual_media: str | Path,
    witness_srt: str | Path,
    provenance: str | Path,
    intro_offset_ms: object,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "BLOCK",
        "timing_status": "BLOCK",
        "text_correctness_status": "UNASSESSED",
        "reason_codes": [error.reason_code],
        "error": error.detail,
        "validation_context": error.context,
        "inputs": {
            "final_srt": str(final_srt),
            "actual_media": str(actual_media),
            "witness_srt": str(witness_srt),
            "provenance": str(provenance),
        },
        "timebase": {
            "intro_offset_ms": intro_offset_ms,
            "intro_offset_application_count": 0,
            "mapping": "not_applied_due_to_input_error",
        },
    }


def check_subtitle_audio_correspondence(
    final_srt: str | Path,
    actual_media: str | Path,
    witness_srt: str | Path,
    provenance: str | Path,
    intro_offset_ms: int = 0,
) -> dict[str, Any]:
    """Return an evidence receipt for subtitle/witness timing correspondence.

    Input integrity failures raise :class:`CorrespondenceInputError`; timing
    insufficiency or drift is represented in the returned receipt as
    ``status=BLOCK``.  No input file is written or modified.
    """

    _validate_intro_offset(intro_offset_ms)
    final_path = _regular_file(final_srt, label="final SRT")
    media_path = _regular_file(actual_media, label="actual media")
    witness_path = _regular_file(witness_srt, label="witness SRT")
    provenance_value, provenance_path, provenance_sha256 = _load_provenance(provenance)

    try:
        final_text = final_path.read_text(encoding="utf-8")
        witness_text = witness_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CorrespondenceInputError("SRT_READ_FAILED", str(exc)) from exc
    final_cues = parse_timed_srt(final_text, label="final SRT")
    witness_cues = parse_timed_srt(witness_text, label="witness SRT")
    final_sha256 = sha256_file(final_path)
    witness_sha256 = sha256_file(witness_path)
    media_sha256 = sha256_file(media_path)
    provenance_binding = _validate_provenance(
        provenance_value,
        observed_media_sha256=media_sha256,
        observed_witness_sha256=witness_sha256,
        intro_offset_ms=intro_offset_ms,
    )

    anchors = _find_anchors(final_cues, witness_cues, intro_offset_ms=intro_offset_ms)
    timeline_end_ms = max(
        max(cue.end_ms for cue in witness_cues),
        max(cue.end_ms for cue in final_cues) + intro_offset_ms,
    )
    coverage = _coverage(anchors, max(cue.end_ms for cue in final_cues))
    deltas = [anchor.start_delta_ms for anchor in anchors]
    reasons: list[str] = []
    timing_status = "INSUFFICIENT"
    timing_summary: dict[str, Any] = {
        "anchor_count": len(anchors),
        "timeline_end_ms": timeline_end_ms,
        "median_start_delta_ms": None,
        "minimum_start_delta_ms": None,
        "maximum_start_delta_ms": None,
        "start_delta_spread_ms": None,
        "median_absolute_deviation_ms": None,
    }
    if coverage["status"] != "PASS":
        reasons.append("INSUFFICIENT_UNIQUE_ANCHORS_START_MIDDLE_END")
    else:
        median_delta = int(statistics.median(deltas))
        deviations = [abs(delta - median_delta) for delta in deltas]
        spread = max(deltas) - min(deltas)
        mad = int(statistics.median(deviations))
        timing_summary.update(
            {
                "median_start_delta_ms": median_delta,
                "minimum_start_delta_ms": min(deltas),
                "maximum_start_delta_ms": max(deltas),
                "start_delta_spread_ms": spread,
                "median_absolute_deviation_ms": mad,
            }
        )
        if abs(median_delta) > GLOBAL_SHIFT_THRESHOLD_MS:
            reasons.append("GLOBAL_TIMING_SHIFT")
            if intro_offset_ms > 0 and median_delta < 0 and abs(abs(median_delta) - intro_offset_ms) <= 500:
                reasons.append("INTRO_OFFSET_MAY_ALREADY_BE_INCLUDED")
            timing_status = "BLOCK"
        elif spread > INCONSISTENT_SPREAD_THRESHOLD_MS or mad > INCONSISTENT_MAD_THRESHOLD_MS:
            reasons.append("INCONSISTENT_TIMING_DRIFT")
            timing_status = "BLOCK"
        elif max(abs(delta) for delta in deltas) > INCONSISTENT_SPREAD_THRESHOLD_MS:
            reasons.append("INCONSISTENT_TIMING_DRIFT")
            timing_status = "BLOCK"
        else:
            timing_status = "PASS"

    if timing_status == "PASS" and coverage["status"] == "PASS":
        status = "PASS"
    else:
        status = "BLOCK"
    anchor_rows = [
        {
            "final_cue_index": anchor.final.index,
            "witness_cue_index": anchor.witness.index,
            "final_text": anchor.final.text,
            "witness_text": anchor.witness.text,
            "similarity": round(anchor.similarity, 6),
            "final_start_ms": anchor.final.start_ms,
            "final_start_aligned_ms": anchor.final_start_aligned_ms,
            "witness_start_ms": anchor.witness.start_ms,
            "start_delta_ms": anchor.start_delta_ms,
            "end_delta_ms": anchor.end_delta_ms,
        }
        for anchor in anchors
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "timing_status": timing_status,
        "text_correctness_status": "UNASSESSED",
        "text_correctness_note": "This receipt aligns existing text only; it authorizes no wording or proper-name repair.",
        "reason_codes": reasons or ["TIMING_CORRESPONDENCE_PASS"],
        "inputs": {
            "final_srt": {"path": str(final_path), "sha256": final_sha256, "cue_count": len(final_cues)},
            "actual_media": {
                "path": str(media_path),
                "sha256": media_sha256,
                "size_bytes": media_path.stat().st_size,
            },
            "witness_srt": {"path": str(witness_path), "sha256": witness_sha256, "cue_count": len(witness_cues)},
            "provenance": {"path": str(provenance_path), "sha256": provenance_sha256},
        },
        "provenance_validation": {
            "status": "PASS",
            "actual_media_sha256_bound": provenance_binding["actual_media_sha256"],
            "witness_srt_sha256_bound": provenance_binding["witness_srt_sha256"],
            "timebase": provenance_binding["timebase"],
            "self_reported_status_not_used": True,
        },
        "timebase": {
            "unit": "ms",
            "final_srt": "delivery_local_ms",
            "witness_srt": "actual_media_local_ms",
            "mapping": "witness_ms = final_delivery_ms + intro_offset_ms",
            "intro_offset_ms": intro_offset_ms,
            "intro_offset_application_count": 1,
        },
        "anchor_policy": {
            "minimum_anchor_count": MIN_ANCHORS,
            "required_coverage": ["start", "middle", "end"],
            "minimum_normalized_chars": ANCHOR_MIN_CHARS,
            "minimum_unique_chars": ANCHOR_MIN_UNIQUE_CHARS,
            "minimum_similarity": ANCHOR_MIN_SIMILARITY,
            "fuzzy_start_anchor_rule": FUZZY_START_ANCHOR_RULE,
            "global_shift_threshold_ms": GLOBAL_SHIFT_THRESHOLD_MS,
            "inconsistent_spread_threshold_ms": INCONSISTENT_SPREAD_THRESHOLD_MS,
            "inconsistent_mad_threshold_ms": INCONSISTENT_MAD_THRESHOLD_MS,
        },
        "anchor_coverage": coverage,
        "anchors": anchor_rows,
        "timing_summary": timing_summary,
    }


def _write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check SRT timing against a hash-bound timed witness SRT")
    parser.add_argument("--final-srt", type=Path, required=True)
    parser.add_argument("--actual-media", type=Path, required=True)
    parser.add_argument("--witness-srt", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--intro-offset-ms", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        receipt = check_subtitle_audio_correspondence(
            args.final_srt,
            args.actual_media,
            args.witness_srt,
            args.provenance,
            intro_offset_ms=args.intro_offset_ms,
        )
    except CorrespondenceInputError as exc:
        receipt = _input_error_receipt(
            exc,
            final_srt=args.final_srt,
            actual_media=args.actual_media,
            witness_srt=args.witness_srt,
            provenance=args.provenance,
            intro_offset_ms=args.intro_offset_ms,
        )
        _write_receipt(args.output, receipt)
        print(json.dumps({"status": receipt["status"], "output": str(args.output)}, ensure_ascii=False))
        return 2
    _write_receipt(args.output, receipt)
    print(json.dumps({"status": receipt["status"], "timing_status": receipt["timing_status"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if receipt["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
