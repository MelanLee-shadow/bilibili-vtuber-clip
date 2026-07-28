"""Partition one ASR cue when adjacent operator truths meet inside it."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Callable, Mapping, Sequence

from src.autoslice.cue_split_hygiene import (
    _shift_boundary_punct,
    _snap_split_to_punct,
)
from src.autoslice.jingting_chunker import SrtCue
from src.autoslice.source_truth_governance import assertion_state
from src.autoslice.source_truth_target_selection import (
    MIN_CUE_OVERLAP_MS,
    _target_indexes,
)


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _input_grid(cues: Sequence[SrtCue]) -> list[dict[str, object]]:
    return [
        {
            "cue_index": index,
            "srt_index": cue.index,
            "start_ms": cue.start_ms,
            "end_ms": cue.end_ms,
            "text": cue.text,
        }
        for index, cue in enumerate(cues, start=1)
    ]


def _operator_replace(entry: Mapping[str, object]) -> bool:
    return bool(
        entry.get("knowledge_type") == "SOURCE_INTERVAL_TRUTH"
        and entry.get("action") == "replace_cue"
        and assertion_state(entry) == "VERIFIED_ACTIVE"
        and entry.get("decision_authority") == "IVAN_OPERATOR_TRUTH"
        and entry.get("required") is not False
        and str(entry.get("text") or "")
    )


def prepartition_adjacent_operator_truths(
    *,
    entries: Sequence[object],
    cues: Sequence[SrtCue],
    texts: Sequence[str],
    entry_windows: Callable[
        [Mapping[str, object]], Sequence[Mapping[str, object]]
    ],
    source_coverage_ms: Callable[[Sequence[Mapping[str, object]]], int],
) -> tuple[list[SrtCue], list[str], dict[str, object] | None]:
    """Split a cue at a shared truth boundary using only operator-owned text.

    The automatic lane is deliberately narrow: two required active operator
    ``replace_cue`` rows must be exactly adjacent on one recording, both source
    intervals must be fully retained, and the ASR cues selected for the two
    rows must exactly cover those intervals.  The later operator-owned text is
    then deterministically repartitioned across its complete interval; it need
    not already match a suffix produced by this particular ASR run.  Anything
    less exact fails closed instead of allowing the later row to overwrite the
    earlier row or text outside its owned interval.
    """

    active = [
        entry
        for entry in entries
        if isinstance(entry, Mapping) and _operator_replace(entry)
    ]
    output_cues = list(cues)
    output_texts = list(texts)
    input_origins = list(range(1, len(output_cues) + 1))
    partitions: list[dict[str, object]] = []
    for current in active:
        previous_rows = [
            prior
            for prior in active
            if prior.get("recording_basename")
            == current.get("recording_basename")
            and int(prior["source_end_ms"]) == int(current["source_start_ms"])
        ]
        if not previous_rows:
            continue
        if len(previous_rows) != 1:
            raise RuntimeError(
                "SOURCE_TRUTH_ADJACENT_PREDECESSOR_NOT_UNIQUE:"
                f"{current.get('truth_id')}"
            )
        previous = previous_rows[0]
        previous_windows = list(entry_windows(previous))
        current_windows = list(entry_windows(current))
        if not previous_windows or not current_windows:
            continue
        previous_duration = (
            int(previous["source_end_ms"]) - int(previous["source_start_ms"])
        )
        current_duration = (
            int(current["source_end_ms"]) - int(current["source_start_ms"])
        )
        if (
            len(previous_windows) != 1
            or len(current_windows) != 1
            or source_coverage_ms(previous_windows) != previous_duration
            or source_coverage_ms(current_windows) != current_duration
        ):
            continue
        boundary_ms = int(current_windows[0]["start_ms"])
        if int(previous_windows[0]["end_ms"]) != boundary_ms:
            continue
        crossing = [
            index
            for index, cue in enumerate(output_cues)
            if cue.start_ms < boundary_ms < cue.end_ms
        ]
        if not crossing:
            continue
        if len(crossing) != 1:
            raise RuntimeError(
                "SOURCE_TRUTH_ADJACENT_CROSSING_NOT_UNIQUE:"
                f"{current.get('truth_id')}"
            )
        crossing_index = crossing[0]
        previous_targets = _target_indexes(
            output_cues, previous_windows, grazing_exempt=True
        )
        current_targets = _target_indexes(
            output_cues, current_windows, grazing_exempt=True
        )
        if (
            previous_targets != [crossing_index]
            or not current_targets
            or current_targets[0] != crossing_index
            or current_targets != list(
                range(crossing_index, crossing_index + len(current_targets))
            )
            or boundary_ms - output_cues[crossing_index].start_ms
            < MIN_CUE_OVERLAP_MS
            or output_cues[crossing_index].end_ms - boundary_ms
            < MIN_CUE_OVERLAP_MS
            or output_cues[crossing_index].start_ms
            != int(previous_windows[0]["start_ms"])
            or output_cues[current_targets[-1]].end_ms
            != int(current_windows[0]["end_ms"])
        ):
            raise RuntimeError(
                "SOURCE_TRUTH_ADJACENT_OWNER_PARTITION_UNRESOLVED:"
                f"{current.get('truth_id')}"
            )
        from src.autoslice.chat_repair import _best_text_split

        parts = _snap_split_to_punct(
            _shift_boundary_punct(
                _best_text_split(
                    str(current["text"]),
                    [output_texts[index] for index in current_targets],
                )
            )
        )
        if (
            len(parts) != len(current_targets)
            or any(not part.strip() for part in parts)
        ):
            raise RuntimeError(
                "SOURCE_TRUTH_ADJACENT_TEXT_PARTITION_UNRESOLVED:"
                f"{current.get('truth_id')}"
            )
        original = output_cues[crossing_index]
        output_cues[crossing_index] = replace(original, end_ms=boundary_ms)
        output_cues.insert(
            crossing_index + 1,
            replace(original, start_ms=boundary_ms),
        )
        output_texts.insert(crossing_index + 1, parts[0])
        for target_index, part in zip(
            current_targets[1:],
            parts[1:],
            strict=True,
        ):
            # Inserting the new prefix cue shifted every pre-existing suffix
            # cue by one.  All of them are inside the later operator truth's
            # exact interval, so its text—not a coincidental ASR suffix—is
            # authoritative here.
            output_texts[target_index + 1] = part
        input_origins.insert(
            crossing_index + 1, input_origins[crossing_index]
        )
        partitions.append(
            {
                "previous_truth_id": previous.get("truth_id"),
                "current_truth_id": current.get("truth_id"),
                "source_boundary_ms": int(current["source_start_ms"]),
                "local_boundary_ms": boundary_ms,
                "input_cue_index": input_origins[crossing_index],
                "inserted_text": parts[0],
            }
        )
    if not partitions:
        return output_cues, output_texts, None
    projected_cues = [
        {
            "cue_index": index,
            "input_cue_index": input_origins[index - 1],
            "start_ms": cue.start_ms,
            "end_ms": cue.end_ms,
        }
        for index, cue in enumerate(output_cues, start=1)
    ]
    return output_cues, output_texts, {
        "schema_version": "source-truth-adjacent-cue-partition.v1",
        "status": "PASS",
        "input_cue_grid_sha256": _canonical_sha256(_input_grid(cues)),
        "partitions": partitions,
        "projected_cues": projected_cues,
    }


def partitioned_preview_grid(
    partition: object,
    input_cues: Sequence[SrtCue],
) -> tuple[list[SrtCue], dict[int, int]]:
    """Validate a partition receipt and return projected grid plus origin map."""

    identity = {
        index: index for index in range(1, len(input_cues) + 1)
    }
    if partition is None:
        return list(input_cues), identity
    if (
        not isinstance(partition, Mapping)
        or partition.get("schema_version")
        != "source-truth-adjacent-cue-partition.v1"
        or partition.get("status") != "PASS"
        or partition.get("input_cue_grid_sha256")
        != _canonical_sha256(_input_grid(input_cues))
        or not isinstance(partition.get("partitions"), list)
        or not partition.get("partitions")
        or not isinstance(partition.get("projected_cues"), list)
    ):
        raise RuntimeError("SOURCE_TRUTH_PREVIEW_PARTITION_INVALID")
    raw_projected = partition["projected_cues"]
    projected: list[SrtCue] = []
    origins: dict[int, int] = {}
    grouped: dict[int, list[SrtCue]] = {}
    for expected_index, raw in enumerate(raw_projected, start=1):
        if not isinstance(raw, Mapping):
            raise RuntimeError("SOURCE_TRUTH_PREVIEW_PARTITION_INVALID")
        cue_index = raw.get("cue_index")
        origin = raw.get("input_cue_index")
        start_ms = raw.get("start_ms")
        end_ms = raw.get("end_ms")
        if (
            cue_index != expected_index
            or isinstance(origin, bool)
            or not isinstance(origin, int)
            or not 1 <= origin <= len(input_cues)
            or isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or start_ms >= end_ms
        ):
            raise RuntimeError("SOURCE_TRUTH_PREVIEW_PARTITION_INVALID")
        source = input_cues[origin - 1]
        cue = SrtCue(
            index=source.index,
            start_ms=start_ms,
            end_ms=end_ms,
            text=source.text,
        )
        projected.append(cue)
        origins[cue_index] = origin
        grouped.setdefault(origin, []).append(cue)
    for origin, source in enumerate(input_cues, start=1):
        parts = grouped.get(origin, [])
        if (
            not parts
            or parts[0].start_ms != source.start_ms
            or parts[-1].end_ms != source.end_ms
            or any(left.end_ms != right.start_ms for left, right in zip(parts, parts[1:]))
        ):
            raise RuntimeError("SOURCE_TRUTH_PREVIEW_PARTITION_INVALID")
    return projected, origins


def map_projection_indexes(
    projection: Mapping[str, object],
    origins: Mapping[int, int],
) -> tuple[list[int], list[int]]:
    projected = [
        int(cue["cue_index"])
        for cue in projection.get("cues", [])
        if isinstance(cue, Mapping)
    ]
    mapped = sorted({origins[index] for index in projected})
    return projected, mapped


def partition_origin_map(partition: object) -> dict[int, int] | None:
    """Return projected-to-input cue ordinals from a formal partition audit."""

    if partition is None:
        return None
    if (
        not isinstance(partition, Mapping)
        or partition.get("schema_version")
        != "source-truth-adjacent-cue-partition.v1"
        or partition.get("status") != "PASS"
        or not isinstance(partition.get("projected_cues"), list)
    ):
        raise RuntimeError("SOURCE_TRUTH_FORMAL_PARTITION_INVALID")
    origins: dict[int, int] = {}
    for expected, raw in enumerate(partition["projected_cues"], start=1):
        if (
            not isinstance(raw, Mapping)
            or raw.get("cue_index") != expected
            or isinstance(raw.get("input_cue_index"), bool)
            or not isinstance(raw.get("input_cue_index"), int)
        ):
            raise RuntimeError("SOURCE_TRUTH_FORMAL_PARTITION_INVALID")
        origins[expected] = int(raw["input_cue_index"])
    return origins
