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
When single-cue anchors leave one of those regions uncovered, one exact anchor
may span two adjacent cues on either side; this is a timing correspondence
fallback, not a cue edit or a fuzzy semantic match.
Proper-name or other wording errors remain outside this check: the text status
is reported as ``UNASSESSED`` rather than becoming a repair request.

Anchor uniqueness (``anchor_policy.anchor_uniqueness``).  A pair ``(f, w)`` is
timing evidence only if the sentence it carries can be placed at one speech
location on each side.  An *occurrence* of a sentence ``t`` is any single cue
or adjacent cue pair whose normalized text reaches ``ANCHOR_MIN_SIMILARITY``
against ``t`` (ASR variants included), or any contiguous cue span whose
normalized text contains ``t`` exactly.  For both anchor texts, the number of
mutually disjoint occurrences strictly before and strictly after the anchor's
own cues must be equal on the final and witness sides: zero/zero is a unique
anchor; otherwise the k-th occurrence is paired with the k-th occurrence
(``ordered_repeat``).  Ordered repeats are kept only when at least one unique
anchor exists and every ordered repeat keeps the start order of every unique
anchor and of every other ordered repeat on both sides; otherwise all ordered
repeats are dropped.  Receipts without the policy key were produced by the
legacy whole-cue exact-count rule and are replayed under that rule.

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

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import argparse
import difflib
import json
import math
import re
import statistics
import unicodedata
from collections import Counter
from dataclasses import dataclass
from itertools import combinations
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

# Current uniqueness rule, written into every new receipt's anchor_policy.
ANCHOR_UNIQUENESS_POLICY = "sentence-occurrence-order.v1"
# Replay-only identity of the rule used before the key existed (whole-cue
# exact-text Counter); it is never written, so legacy receipts stay unchanged.
LEGACY_ANCHOR_UNIQUENESS_POLICY = "legacy-exact-whole-cue-count"
_ANCHOR_UNIQUENESS_POLICIES = frozenset({ANCHOR_UNIQUENESS_POLICY, LEGACY_ANCHOR_UNIQUENESS_POLICY})
ANCHOR_OCCURRENCE_MATCH = (
    "single_or_adjacent_pair_similarity_at_least_minimum_or_exact_containment_in_contiguous_span"
)
ANCHOR_REPEAT_RESOLUTION = (
    "equal_disjoint_occurrences_before_and_after_on_both_sides_for_both_texts_"
    "and_start_order_consistent_with_every_unique_anchor_else_drop_all_ordered_repeats"
)

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
    final_cues: tuple[TimedCue, ...]
    witness_cues: tuple[TimedCue, ...]
    similarity: float
    final_start_aligned_ms: int
    start_delta_ms: int
    end_delta_ms: int
    # ``None`` under the legacy rule (receipt shape unchanged); otherwise
    # ``"unique"`` or ``"ordered_repeat"`` with the occurrence counts proving it.
    occurrence_resolution: str | None = None
    occurrences_before: int = 0
    occurrences_after: int = 0

    @property
    def final_indices(self) -> tuple[str, ...]:
        return tuple(cue.index for cue in self.final_cues)

    @property
    def witness_indices(self) -> tuple[str, ...]:
        return tuple(cue.index for cue in self.witness_cues)

    @property
    def final_index(self) -> str:
        return "+".join(self.final_indices)

    @property
    def witness_index(self) -> str:
        return "+".join(self.witness_indices)

    @property
    def final_start_ms(self) -> int:
        return self.final_cues[0].start_ms

    @property
    def final_end_ms(self) -> int:
        return self.final_cues[-1].end_ms

    @property
    def witness_start_ms(self) -> int:
        return self.witness_cues[0].start_ms

    @property
    def witness_end_ms(self) -> int:
        return self.witness_cues[-1].end_ms

    @property
    def final_text(self) -> str:
        return " ".join(cue.text for cue in self.final_cues)

    @property
    def witness_text(self) -> str:
        return " ".join(cue.text for cue in self.witness_cues)

    @property
    def kind(self) -> str:
        return "composite" if len(self.final_cues) == 2 or len(self.witness_cues) == 2 else "single"


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
    try:
        digest = _evidence_file_sha256(file_path)
    except OSError as exc:
        raise CorrespondenceInputError("INPUT_READ_FAILED", f"{file_path}: {exc}") from exc
    return f"sha256:{digest}"


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


def anchor_uniqueness_policy_of(receipt: Mapping[str, Any]) -> str:
    """Return the uniqueness rule a stored receipt was produced under.

    Receipts written before ``anchor_policy.anchor_uniqueness`` existed are
    replayed under the legacy rule; an unknown declared rule is refused rather
    than replayed under a different one.
    """

    policy = receipt.get("anchor_policy") if isinstance(receipt, Mapping) else None
    if not isinstance(policy, Mapping) or "anchor_uniqueness" not in policy:
        return LEGACY_ANCHOR_UNIQUENESS_POLICY
    declared = policy.get("anchor_uniqueness")
    if declared != ANCHOR_UNIQUENESS_POLICY:
        raise CorrespondenceInputError(
            "ANCHOR_UNIQUENESS_POLICY_UNSUPPORTED",
            f"receipt declares anchor_uniqueness={declared!r}",
        )
    return ANCHOR_UNIQUENESS_POLICY


def _is_eligible_anchor(cue: TimedCue, occurrences: Mapping[str, int] | None) -> bool:
    """Length/common-word eligibility; ``occurrences=None`` skips the legacy count."""

    normalized = cue.normalized_text
    if len(normalized) < ANCHOR_MIN_CHARS:
        return False
    if len(set(normalized)) < ANCHOR_MIN_UNIQUE_CHARS:
        return False
    if normalized in _COMMON_SHORT_TEXT:
        return False
    if occurrences is None:
        return True
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


def _group_normalized_text(cues: Sequence[TimedCue]) -> str:
    return "".join(cue.normalized_text for cue in cues)


def _pair_gap_is_eligible(cues: Sequence[TimedCue]) -> bool:
    if len(cues) != 2:
        return False
    gap_ms = cues[1].start_ms - cues[0].end_ms
    return 0 <= gap_ms <= 250


def _composite_text_occurrences(cues: Sequence[TimedCue]) -> Counter[str]:
    """Count every singleton and adjacent-pair normalized text occurrence."""

    occurrences = Counter(cue.normalized_text for cue in cues)
    occurrences.update(
        _group_normalized_text(group)
        for group in zip(cues, cues[1:])
    )
    return occurrences


def _eligible_composite_groups(
    cues: Sequence[TimedCue], *, require_unique_text: bool = True
) -> list[tuple[TimedCue, TimedCue]]:
    """Return exact two-cue groups without widening the time contract.

    ``require_unique_text`` applies the legacy exact-count uniqueness; the
    current rule decides uniqueness per candidate from sentence occurrences.
    """

    text_occurrences = _composite_text_occurrences(cues)
    pairs: list[tuple[TimedCue, TimedCue]] = []
    for first, second in zip(cues, cues[1:]):
        group = (first, second)
        normalized = _group_normalized_text(group)
        if not _pair_gap_is_eligible(group):
            continue
        if (
            len(normalized) < ANCHOR_MIN_CHARS
            or len(set(normalized)) < ANCHOR_MIN_UNIQUE_CHARS
            or normalized in _COMMON_SHORT_TEXT
            or all(cue.normalized_text in _COMMON_SHORT_TEXT for cue in group)
            or (require_unique_text and text_occurrences[normalized] != 1)
        ):
            continue
        pairs.append(group)
    return pairs


def _disjoint_count(spans: Any) -> int:
    """Maximum number of mutually disjoint cue spans (earliest-end greedy)."""

    count = 0
    last_end = -1
    for start, end in sorted(spans, key=lambda span: (span[1], span[0])):
        if start > last_end:
            count += 1
            last_end = end
    return count


class _OccurrenceIndex:
    """Where a sentence is spoken on one side, over every cue (eligible or not).

    An occurrence is a single cue or adjacent cue pair whose normalized text
    reaches ``ANCHOR_MIN_SIMILARITY`` against the sentence (ASR variants), or
    any contiguous span whose normalized text contains the sentence exactly.
    A shorter cue merely contained in the sentence is a different, shorter
    sentence unless it reaches the similarity floor on its own.
    """

    def __init__(self, cues: Sequence[TimedCue]):
        self._texts = [cue.normalized_text for cue in cues]
        self._pairs = [left + right for left, right in zip(self._texts, self._texts[1:])]
        owners: list[int] = []
        for position, text in enumerate(self._texts):
            owners.extend([position] * len(text))
        self._stream = "".join(self._texts)
        self._owners = owners
        self._cache: dict[str, tuple[tuple[int, int], ...]] = {}

    def spans(self, text: str) -> tuple[tuple[int, int], ...]:
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        found: set[tuple[int, int]] = set()
        if text:
            for position, cue_text in enumerate(self._texts):
                if _similarity(text, cue_text) >= ANCHOR_MIN_SIMILARITY:
                    found.add((position, position))
            for position, pair_text in enumerate(self._pairs):
                if _similarity(text, pair_text) >= ANCHOR_MIN_SIMILARITY:
                    found.add((position, position + 1))
            start = self._stream.find(text)
            while start != -1:
                found.add((self._owners[start], self._owners[start + len(text) - 1]))
                start = self._stream.find(text, start + 1)
        result = tuple(sorted(found))
        self._cache[text] = result
        return result

    def beside(self, texts: Sequence[str], own: tuple[int, int]) -> tuple[int, int]:
        """Disjoint occurrences of any spelling strictly before/after ``own``."""

        spans = {span for text in texts for span in self.spans(text)}
        before = _disjoint_count(span for span in spans if span[1] < own[0])
        after = _disjoint_count(span for span in spans if span[0] > own[1])
        return before, after


def _occurrence_resolution(
    texts: Sequence[str],
    final_index: _OccurrenceIndex,
    final_span: tuple[int, int],
    witness_index: _OccurrenceIndex,
    witness_span: tuple[int, int],
) -> tuple[str, int, int] | None:
    """Return ``(resolution, before, after)`` or ``None`` when unresolvable.

    The sentence is either anchor spelling (final or witness text).  It must
    have the same number of other disjoint occurrences before and after the
    anchor on the final side as on the witness side.
    """

    spellings = tuple(dict.fromkeys(texts))
    final_counts = final_index.beside(spellings, final_span)
    if final_counts != witness_index.beside(spellings, witness_span):
        return None
    before, after = final_counts
    return ("unique" if before == after == 0 else "ordered_repeat", before, after)


def _start_order_agrees(left: _Anchor, right: _Anchor) -> bool:
    final_order = (left.final_start_ms > right.final_start_ms) - (left.final_start_ms < right.final_start_ms)
    witness_order = (left.witness_start_ms > right.witness_start_ms) - (
        left.witness_start_ms < right.witness_start_ms
    )
    return final_order == witness_order


def _ordered_repeats_consistent(anchors: Sequence[_Anchor]) -> bool:
    unique = [anchor for anchor in anchors if anchor.occurrence_resolution == "unique"]
    ordered = [anchor for anchor in anchors if anchor.occurrence_resolution == "ordered_repeat"]
    if not ordered:
        return True
    if not unique:
        return False
    return all(_start_order_agrees(item, other) for item in ordered for other in unique) and all(
        _start_order_agrees(item, other) for item, other in combinations(ordered, 2)
    )


def _without_inconsistent_ordered_repeats(anchors: Sequence[_Anchor]) -> list[_Anchor]:
    if _ordered_repeats_consistent(anchors):
        return list(anchors)
    return [anchor for anchor in anchors if anchor.occurrence_resolution != "ordered_repeat"]


def _make_anchor(
    final_cues: Sequence[TimedCue],
    witness_cues: Sequence[TimedCue],
    *,
    similarity: float,
    intro_offset_ms: int,
    resolution: tuple[str, int, int] | None = None,
) -> _Anchor:
    final_group = tuple(final_cues)
    witness_group = tuple(witness_cues)
    aligned_start_ms = final_group[0].start_ms + intro_offset_ms
    return _Anchor(
        final_cues=final_group,
        witness_cues=witness_group,
        similarity=similarity,
        final_start_aligned_ms=aligned_start_ms,
        start_delta_ms=witness_group[0].start_ms - aligned_start_ms,
        end_delta_ms=witness_group[-1].end_ms - (final_group[-1].end_ms + intro_offset_ms),
        occurrence_resolution=resolution[0] if resolution is not None else None,
        occurrences_before=resolution[1] if resolution is not None else 0,
        occurrences_after=resolution[2] if resolution is not None else 0,
    )


def _repeat_last(resolution: tuple[str, int, int] | None) -> bool:
    """Sort key: unique (and legacy) anchors are selected before ordered repeats."""

    return resolution is not None and resolution[0] != "unique"


def _find_anchors(
    final_cues: Sequence[TimedCue],
    witness_cues: Sequence[TimedCue],
    *,
    intro_offset_ms: int,
    anchor_uniqueness: str = ANCHOR_UNIQUENESS_POLICY,
) -> list[_Anchor]:
    legacy = anchor_uniqueness == LEGACY_ANCHOR_UNIQUENESS_POLICY
    final_occurrences = Counter(cue.normalized_text for cue in final_cues) if legacy else None
    witness_occurrences = Counter(cue.normalized_text for cue in witness_cues) if legacy else None
    eligible_final = [cue for cue in final_cues if _is_eligible_anchor(cue, final_occurrences)]
    eligible_witness = [cue for cue in witness_cues if _is_eligible_anchor(cue, witness_occurrences)]
    final_index = _OccurrenceIndex(final_cues)
    witness_index = _OccurrenceIndex(witness_cues)
    final_position = {cue.index: position for position, cue in enumerate(final_cues)}
    witness_position = {cue.index: position for position, cue in enumerate(witness_cues)}

    def group_span(group: Sequence[TimedCue], positions: Mapping[str, int]) -> tuple[int, int]:
        return positions[group[0].index], positions[group[-1].index]

    candidates: list[tuple[float, TimedCue, TimedCue, tuple[str, int, int] | None]] = []
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
        if legacy:
            if not scored or scored[0][0] < ANCHOR_MIN_SIMILARITY:
                continue
            if (
                len(scored) > 1
                and scored[0][0] < 1.0
                and scored[0][0] - scored[1][0] < ANCHOR_TIE_MARGIN
            ):
                continue
            candidates.append((scored[0][0], final_cue, scored[0][1], None))
            continue

        # Every witness cue reaching the floor is an occurrence of this
        # sentence; the partner is the one at the same occurrence rank on
        # both sides.  More than one rank-consistent partner is ambiguous.
        final_span = group_span((final_cue,), final_position)
        resolvable = [
            (similarity, witness_cue, resolution)
            for similarity, witness_cue in scored
            if similarity >= ANCHOR_MIN_SIMILARITY
            for resolution in [
                _occurrence_resolution(
                    (final_cue.normalized_text, witness_cue.normalized_text),
                    final_index,
                    final_span,
                    witness_index,
                    group_span((witness_cue,), witness_position),
                )
            ]
            if resolution is not None
        ]
        if len(resolvable) != 1:
            continue
        similarity, witness_cue, resolution = resolvable[0]
        # Other occurrences are resolved by rank; a below-floor near miss
        # still has to leave the existing tie margin.
        near_misses = [score for score, _cue in scored if score < ANCHOR_MIN_SIMILARITY]
        if similarity < 1.0 and near_misses and similarity - near_misses[0] < ANCHOR_TIE_MARGIN:
            continue
        candidates.append((similarity, final_cue, witness_cue, resolution))

    anchors: list[_Anchor] = []
    used_final: set[str] = set()
    used_witness: set[str] = set()
    for similarity, final_cue, witness_cue, resolution in sorted(
        candidates,
        key=lambda item: (_repeat_last(item[3]), -item[0], item[1].start_ms, item[2].start_ms),
    ):
        if final_cue.index in used_final or witness_cue.index in used_witness:
            continue
        anchors.append(
            _make_anchor(
                (final_cue,),
                (witness_cue,),
                similarity=similarity,
                intro_offset_ms=intro_offset_ms,
                resolution=resolution,
            )
        )
        used_final.add(final_cue.index)
        used_witness.add(witness_cue.index)
    if not legacy:
        anchors = _without_inconsistent_ordered_repeats(anchors)
    used_final = {index for anchor in anchors for index in anchor.final_indices}
    used_witness = {index for anchor in anchors for index in anchor.witness_indices}

    timeline_end_ms = max((cue.end_ms for cue in final_cues), default=0)
    if _coverage(anchors, timeline_end_ms)["status"] == "PASS":
        return sorted(anchors, key=lambda anchor: anchor.witness_start_ms)

    final_pairs = [
        group
        for group in _eligible_composite_groups(final_cues, require_unique_text=legacy)
        if not any(cue.index in used_final for cue in group)
    ]
    witness_pairs = [
        group
        for group in _eligible_composite_groups(witness_cues, require_unique_text=legacy)
        if not any(cue.index in used_witness for cue in group)
    ]
    final_singles = [
        (cue,)
        for cue in eligible_final
        if cue.index not in used_final
    ]
    witness_singles = [
        (cue,)
        for cue in eligible_witness
        if cue.index not in used_witness
    ]
    final_groups = final_singles + final_pairs
    witness_groups = witness_singles + witness_pairs
    composite_candidates: list[_Anchor] = []
    for final_group in final_groups:
        final_normalized = _group_normalized_text(final_group)
        for witness_group in witness_groups:
            if len(final_group) == 1 and len(witness_group) == 1:
                continue
            if final_normalized != _group_normalized_text(witness_group):
                continue
            resolution = None
            if not legacy:
                resolution = _occurrence_resolution(
                    (final_normalized,),
                    final_index,
                    group_span(final_group, final_position),
                    witness_index,
                    group_span(witness_group, witness_position),
                )
                if resolution is None:
                    continue
            composite_candidates.append(
                _make_anchor(
                    final_group,
                    witness_group,
                    similarity=1.0,
                    intro_offset_ms=intro_offset_ms,
                    resolution=resolution,
                )
            )

    selected: list[_Anchor] = []
    selected_final = set(used_final)
    selected_witness = set(used_witness)
    for candidate in sorted(
        composite_candidates,
        key=lambda anchor: (
            anchor.occurrence_resolution == "ordered_repeat",
            anchor.final_start_ms,
            anchor.witness_start_ms,
            anchor.final_index,
            anchor.witness_index,
        ),
    ):
        if any(index in selected_final for index in candidate.final_indices):
            continue
        if any(index in selected_witness for index in candidate.witness_indices):
            continue
        if candidate.occurrence_resolution == "ordered_repeat" and not _ordered_repeats_consistent(
            anchors + selected + [candidate]
        ):
            continue
        trial = anchors + selected + [candidate]
        trial_coverage = _coverage(trial, timeline_end_ms)
        bucket = "start" if candidate.final_start_ms < timeline_end_ms / 3 else (
            "middle" if candidate.final_start_ms < timeline_end_ms * 2 / 3 else "end"
        )
        covered_before = _coverage(anchors + selected, timeline_end_ms)
        if covered_before[bucket] and len(anchors) + len(selected) >= MIN_ANCHORS:
            continue
        selected.append(candidate)
        selected_final.update(candidate.final_indices)
        selected_witness.update(candidate.witness_indices)
        if trial_coverage["status"] == "PASS":
            break

    result = anchors + selected
    if not legacy:
        result = _without_inconsistent_ordered_repeats(result)
    return sorted(result, key=lambda anchor: anchor.witness_start_ms)


def _coverage(anchors: Sequence[_Anchor], timeline_end_ms: int) -> dict[str, Any]:
    if timeline_end_ms <= 0:
        return {"status": "INSUFFICIENT", "start": [], "middle": [], "end": [], "required": ["start", "middle", "end"]}
    start_cut = timeline_end_ms / 3
    end_cut = timeline_end_ms * 2 / 3
    bins: dict[str, list[str]] = {"start": [], "middle": [], "end": []}
    cue_ids: dict[str, list[list[str]]] = {"start": [], "middle": [], "end": []}
    for anchor in anchors:
        # Coverage belongs to the main clip, excluding branding. Including a
        # long intro could move every anchor out of a short clip's first third.
        # Witness positions are also unsuitable because drift moves the bins.
        position = anchor.final_start_ms
        bucket = "start" if position < start_cut else "middle" if position < end_cut else "end"
        bins[bucket].append(anchor.final_index)
        cue_ids[bucket].append(list(anchor.final_indices))
    status = "PASS" if len(anchors) >= MIN_ANCHORS and all(bins.values()) else "INSUFFICIENT"
    return {"status": status, **bins, "cue_ids": cue_ids, "required": ["start", "middle", "end"]}


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
    assess_text: bool = False,
    anchor_uniqueness: str = ANCHOR_UNIQUENESS_POLICY,
) -> dict[str, Any]:
    """Return an evidence receipt for subtitle/witness timing correspondence.

    ``assess_text=True`` (new captures only) also records the deterministic
    wording-disagreement nominations (``witness_disagreement``) and reports
    ``text_correctness_status=ASSESSED``.  Frozen historical receipts and their
    successors keep the default ``UNASSESSED`` shape byte for byte.

    ``anchor_uniqueness`` defaults to the current rule.  Validators replaying
    a stored receipt pass :func:`anchor_uniqueness_policy_of` of that receipt,
    so a receipt without the policy key is recomputed under the legacy rule in
    its original shape.

    Input integrity failures raise :class:`CorrespondenceInputError`; timing
    insufficiency or drift is represented in the returned receipt as
    ``status=BLOCK``.  No input file is written or modified.
    """

    _validate_intro_offset(intro_offset_ms)
    if anchor_uniqueness not in _ANCHOR_UNIQUENESS_POLICIES:
        raise CorrespondenceInputError(
            "ANCHOR_UNIQUENESS_POLICY_UNSUPPORTED",
            f"anchor_uniqueness={anchor_uniqueness!r}",
        )
    legacy_uniqueness = anchor_uniqueness == LEGACY_ANCHOR_UNIQUENESS_POLICY
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

    anchors = _find_anchors(
        final_cues,
        witness_cues,
        intro_offset_ms=intro_offset_ms,
        anchor_uniqueness=anchor_uniqueness,
    )
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
            "anchor_kind": anchor.kind,
            "final_cue_index": anchor.final_index,
            "witness_cue_index": anchor.witness_index,
            "final_cue_indices": list(anchor.final_indices),
            "witness_cue_indices": list(anchor.witness_indices),
            "final_text": anchor.final_text,
            "witness_text": anchor.witness_text,
            "similarity": round(anchor.similarity, 6),
            "final_start_ms": anchor.final_start_ms,
            "final_start_aligned_ms": anchor.final_start_aligned_ms,
            "witness_start_ms": anchor.witness_start_ms,
            "start_delta_ms": anchor.start_delta_ms,
            "end_delta_ms": anchor.end_delta_ms,
            **(
                {}
                if legacy_uniqueness
                else {
                    "occurrence_resolution": anchor.occurrence_resolution,
                    "occurrences_before": anchor.occurrences_before,
                    "occurrences_after": anchor.occurrences_after,
                }
            ),
        }
        for anchor in anchors
    ]
    text_block: dict[str, Any] = {
        "text_correctness_status": "UNASSESSED",
        "text_correctness_note": "This receipt aligns existing text only; it authorizes no wording or proper-name repair.",
    }
    if assess_text:
        from src.autoslice.witness_disagreement import assess_correspondence_text

        assessment = assess_correspondence_text(
            final_text, witness_text, intro_offset_ms=intro_offset_ms
        )
        text_block = {
            "text_correctness_status": "ASSESSED",
            "text_correctness_verdict": (
                "NOMINATIONS_PENDING_ADJUDICATION"
                if assessment["nomination_count"] else "NO_DISAGREEMENT_NOMINATED"
            ),
            "text_correctness_note": (
                "Wording disagreements with the independent witness are nominated, not decided; "
                "the exact-final review must hear the audio and rule on each. This receipt repairs nothing."
            ),
            "witness_disagreement": assessment,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "timing_status": timing_status,
        **text_block,
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
            "composite_enabled_when_single_coverage_insufficient": True,
            "composite_max_cues_per_side": 2,
            "composite_gap_min_ms": 0,
            "composite_gap_max_ms": 250,
            "composite_match": "exact_normalized_only",
            "composite_counts_as_one_anchor": True,
            "composite_requires_unique_text": True,
            "global_shift_threshold_ms": GLOBAL_SHIFT_THRESHOLD_MS,
            "inconsistent_spread_threshold_ms": INCONSISTENT_SPREAD_THRESHOLD_MS,
            "inconsistent_mad_threshold_ms": INCONSISTENT_MAD_THRESHOLD_MS,
            **(
                {}
                if legacy_uniqueness
                else {
                    "anchor_uniqueness": ANCHOR_UNIQUENESS_POLICY,
                    "anchor_occurrence_match": ANCHOR_OCCURRENCE_MATCH,
                    "anchor_repeat_resolution": ANCHOR_REPEAT_RESOLUTION,
                }
            ),
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
