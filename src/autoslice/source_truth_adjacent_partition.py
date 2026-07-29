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


def _rebalance_partition_parts(parts: Sequence[str]) -> list[str]:
    """Keep an exact operator string while avoiding empty/one-char slivers."""

    out = list(parts)
    for index, value in enumerate(out):
        if value.strip():
            continue
        if index + 1 < len(out) and len(out[index + 1]) > 1:
            take = 2 if len(out[index + 1]) > 2 else 1
            out[index] = out[index + 1][:take]
            out[index + 1] = out[index + 1][take:]
            continue
        if index > 0 and len(out[index - 1]) > 1:
            take = 2 if len(out[index - 1]) > 2 else 1
            out[index] = out[index - 1][-take:]
            out[index - 1] = out[index - 1][:-take]
    for index, value in enumerate(out):
        if len(value.strip()) != 1:
            continue
        if index + 1 < len(out) and len(out[index + 1]) > 2:
            out[index] += out[index + 1][0]
            out[index + 1] = out[index + 1][1:]
            continue
        if index > 0 and len(out[index - 1]) > 2:
            out[index] = out[index - 1][-1] + out[index]
            out[index - 1] = out[index - 1][:-1]
    return out


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
    processed: set[str] = set()
    from src.autoslice.chat_repair import _best_text_split

    for seed in active:
        seed_id = str(seed.get("truth_id") or "")
        if seed_id in processed:
            continue
        recording = seed.get("recording_basename")
        same_recording = [
            row
            for row in active
            if row.get("recording_basename") == recording
        ]

        head = seed
        while True:
            predecessors = [
                row
                for row in same_recording
                if int(row["source_end_ms"])
                == int(head["source_start_ms"])
            ]
            if len(predecessors) > 1:
                raise RuntimeError(
                    "SOURCE_TRUTH_ADJACENT_PREDECESSOR_NOT_UNIQUE:"
                    f"{head.get('truth_id')}"
                )
            if not predecessors:
                break
            head = predecessors[0]

        chain = [head]
        while True:
            successors = [
                row
                for row in same_recording
                if int(row["source_start_ms"])
                == int(chain[-1]["source_end_ms"])
            ]
            if len(successors) > 1:
                raise RuntimeError(
                    "SOURCE_TRUTH_ADJACENT_SUCCESSOR_NOT_UNIQUE:"
                    f"{chain[-1].get('truth_id')}"
                )
            if not successors:
                break
            successor = successors[0]
            if successor in chain:
                raise RuntimeError(
                    "SOURCE_TRUTH_ADJACENT_CHAIN_CYCLE:"
                    f"{successor.get('truth_id')}"
                )
            chain.append(successor)
        processed.update(str(row.get("truth_id") or "") for row in chain)
        if len(chain) < 2:
            continue

        windows_by_id: dict[str, list[Mapping[str, object]]] = {}
        chain_eligible = True
        for row in chain:
            row_id = str(row.get("truth_id") or "")
            windows = list(entry_windows(row))
            duration = int(row["source_end_ms"]) - int(
                row["source_start_ms"]
            )
            if (
                len(windows) != 1
                or source_coverage_ms(windows) != duration
            ):
                chain_eligible = False
                break
            windows_by_id[row_id] = windows
        if not chain_eligible:
            continue
        if any(
            int(windows_by_id[str(left.get("truth_id") or "")][0]["end_ms"])
            != int(
                windows_by_id[
                    str(right.get("truth_id") or "")
                ][0]["start_ms"]
            )
            for left, right in zip(chain, chain[1:])
        ):
            continue

        split_receipts: list[dict[str, object]] = []
        for previous, current in zip(chain, chain[1:]):
            current_id = str(current.get("truth_id") or "")
            boundary_ms = int(windows_by_id[current_id][0]["start_ms"])
            crossing = [
                index
                for index, cue in enumerate(output_cues)
                if cue.start_ms < boundary_ms < cue.end_ms
            ]
            if len(crossing) > 1:
                raise RuntimeError(
                    "SOURCE_TRUTH_ADJACENT_CROSSING_NOT_UNIQUE:"
                    f"{current.get('truth_id')}"
                )
            if not crossing:
                continue
            crossing_index = crossing[0]
            original = output_cues[crossing_index]
            left_ms = boundary_ms - original.start_ms
            right_ms = original.end_ms - boundary_ms
            if (
                left_ms < MIN_CUE_OVERLAP_MS
                or right_ms < MIN_CUE_OVERLAP_MS
            ):
                # The formal selector already excludes <80ms grazing overlap.
                # Preserve the existing readable cue instead of creating a
                # 50ms subtitle sliver (909 three-part SC); bind the choice to
                # the nearest existing edge and let the opposite owner start
                # at the next qualifying cue.
                aligned_edge_ms = (
                    original.start_ms
                    if left_ms < right_ms
                    else original.end_ms
                )
                split_receipts.append(
                    {
                        "previous_truth_id": previous.get("truth_id"),
                        "current_truth_id": current.get("truth_id"),
                        "source_boundary_ms": int(
                            current["source_start_ms"]
                        ),
                        "local_boundary_ms": boundary_ms,
                        "input_cue_index": input_origins[crossing_index],
                        "alignment_kind": (
                            "BOUNDARY_SLIVER_ALIGNED_TO_EXISTING_CUE_EDGE"
                        ),
                        "aligned_cue_edge_ms": aligned_edge_ms,
                        "alignment_drift_ms": abs(
                            aligned_edge_ms - boundary_ms
                        ),
                    }
                )
                continue
            output_cues[crossing_index] = replace(
                original, end_ms=boundary_ms
            )
            output_cues.insert(
                crossing_index + 1,
                replace(original, start_ms=boundary_ms),
            )
            output_texts.insert(
                crossing_index + 1, output_texts[crossing_index]
            )
            input_origins.insert(
                crossing_index + 1, input_origins[crossing_index]
            )
            split_receipts.append(
                {
                    "previous_truth_id": previous.get("truth_id"),
                    "current_truth_id": current.get("truth_id"),
                    "source_boundary_ms": int(current["source_start_ms"]),
                    "local_boundary_ms": boundary_ms,
                    "input_cue_index": input_origins[crossing_index],
                }
            )
        if not split_receipts:
            continue

        targets_by_id: dict[str, list[int]] = {}
        parts_by_id: dict[str, list[str]] = {}
        for row in chain:
            row_id = str(row.get("truth_id") or "")
            window = windows_by_id[row_id][0]
            targets = _target_indexes(
                output_cues,
                [window],
                grazing_exempt=True,
            )
            if (
                not targets
                or targets
                != list(range(targets[0], targets[-1] + 1))
                or output_cues[targets[0]].start_ms
                < int(window["start_ms"]) - (MIN_CUE_OVERLAP_MS - 1)
                or output_cues[targets[-1]].end_ms
                > int(window["end_ms"]) + (MIN_CUE_OVERLAP_MS - 1)
            ):
                raise RuntimeError(
                    "SOURCE_TRUTH_ADJACENT_OWNER_PARTITION_UNRESOLVED:"
                    f"{row.get('truth_id')}"
                )
            parts = _rebalance_partition_parts(
                _snap_split_to_punct(
                    _shift_boundary_punct(
                        _best_text_split(
                            str(row["text"]),
                            [output_texts[index] for index in targets],
                        )
                    )
                )
            )
            if (
                len(parts) != len(targets)
                or any(not part.strip() for part in parts)
            ):
                raise RuntimeError(
                    "SOURCE_TRUTH_ADJACENT_TEXT_PARTITION_UNRESOLVED:"
                    f"{row.get('truth_id')}"
                )
            targets_by_id[row_id] = targets
            parts_by_id[row_id] = parts
        for row in chain:
            row_id = str(row.get("truth_id") or "")
            for target_index, part in zip(
                targets_by_id[row_id],
                parts_by_id[row_id],
                strict=True,
            ):
                output_texts[target_index] = part
        for receipt in split_receipts:
            if receipt.get("alignment_kind"):
                continue
            current_id = str(receipt["current_truth_id"])
            receipt["inserted_text"] = parts_by_id[current_id][0]
        partitions.extend(split_receipts)
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
