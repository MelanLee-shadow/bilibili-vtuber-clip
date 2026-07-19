"""Apply reviewed subtitle truth on the original recording timeline.

Candidate-local overrides are useful for one delivery, but they do not protect
another candidate cut from the same source interval.  This module binds a
reviewed correction to ``recording basename + absolute source milliseconds``
and projects it onto any retained piece that contains that interval.

The ledger is deliberately a late text authority.  It never changes timing or
piece boundaries, and required entries fail closed when an included source
interval cannot be found on the final SRT timeline.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.autoslice.jingting_chunker import SrtCue, parse_srt_cues


SCHEMA_VERSION = "source-subtitle-truth-ledger.v1"
AUDIT_SCHEMA_VERSION = "source-subtitle-truth-audit.v1"
MIN_CUE_OVERLAP_MS = 80


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _format_ms(value: int) -> str:
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _render(cues: Sequence[SrtCue], texts: Sequence[str]) -> str:
    blocks = [
        (
            f"{index}\n{_format_ms(cue.start_ms)} --> {_format_ms(cue.end_ms)}\n"
            f"{text}"
        )
        for index, (cue, text) in enumerate(zip(cues, texts), start=1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _piece_media_basename(piece: Mapping[str, object]) -> str:
    for key in ("remote_media", "source_media", "media_path", "path"):
        value = piece.get(key)
        if isinstance(value, str) and value.strip():
            return Path(value).name
    return ""


def _entry_local_windows(
    entry: Mapping[str, object],
    *,
    pieces: Sequence[Mapping[str, object]],
    durations: Sequence[int],
) -> list[dict[str, int]]:
    source_name = str(entry.get("recording_basename") or "")
    source_start = int(entry["source_start_ms"])
    source_end = int(entry["source_end_ms"])
    windows: list[dict[str, int]] = []
    output_offset = 0
    for piece, duration in zip(pieces, durations):
        piece_start = int(piece["start_ms"])
        piece_end = int(piece["end_ms"])
        overlap_start = max(piece_start, source_start)
        overlap_end = min(piece_end, source_end)
        if (
            _piece_media_basename(piece) == source_name
            and overlap_start < overlap_end
        ):
            windows.append(
                {
                    "start_ms": output_offset + overlap_start - piece_start,
                    "end_ms": output_offset + overlap_end - piece_start,
                    "source_overlap_start_ms": overlap_start,
                    "source_overlap_end_ms": overlap_end,
                }
            )
        output_offset += int(duration)
    return windows


def _source_coverage_ms(windows: Sequence[Mapping[str, int]]) -> int:
    intervals = sorted(
        (
            int(window["source_overlap_start_ms"]),
            int(window["source_overlap_end_ms"]),
        )
        for window in windows
    )
    if not intervals:
        return 0
    covered = 0
    current_start, current_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
            continue
        covered += current_end - current_start
        current_start, current_end = start, end
    return covered + current_end - current_start


def _overlap_ms(cue: SrtCue, start_ms: int, end_ms: int) -> int:
    return max(0, min(cue.end_ms, end_ms) - max(cue.start_ms, start_ms))


def _target_indexes(
    cues: Sequence[SrtCue], windows: Sequence[Mapping[str, int]]
) -> list[int]:
    matches: list[tuple[int, int]] = []
    for index, cue in enumerate(cues):
        overlap = max(
            (
                _overlap_ms(cue, int(window["start_ms"]), int(window["end_ms"]))
                for window in windows
            ),
            default=0,
        )
        if overlap >= MIN_CUE_OVERLAP_MS:
            matches.append((index, overlap))
    matches.sort(key=lambda row: row[0])
    return [index for index, _overlap in matches]


def _replace_substrings(
    text: str, replacements: Sequence[Mapping[str, object]]
) -> tuple[str, list[dict[str, str]]]:
    output = text
    applied: list[dict[str, str]] = []
    for row in replacements:
        surface = str(row.get("surface") or "")
        canonical = str(row.get("canonical") or "")
        if not surface or not canonical or surface not in output:
            continue
        output = output.replace(surface, canonical)
        applied.append({"surface": surface, "canonical": canonical})
    return output, applied


def apply_source_subtitle_truth(
    srt_text: str,
    *,
    spec: Mapping[str, object],
    durations: Sequence[int],
    ledger_path: Path | None,
) -> tuple[str, dict[str, Any]]:
    """Apply every reviewed source interval included by this candidate."""

    audit: dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "status": "SKIPPED_NO_LEDGER",
        "ledger_path": str(ledger_path) if ledger_path is not None else None,
        "ledger_sha256": None,
        "applied": [],
        "satisfied": [],
        "failures": [],
    }
    if ledger_path is None:
        return srt_text, audit
    if not ledger_path.is_file() or ledger_path.is_symlink():
        raise RuntimeError(f"SOURCE_SUBTITLE_TRUTH_LEDGER_INVALID: {ledger_path}")

    raw = ledger_path.read_bytes()
    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("SOURCE_SUBTITLE_TRUTH_LEDGER_SCHEMA_INVALID")
    entries = document.get("entries")
    if not isinstance(entries, list):
        raise RuntimeError("SOURCE_SUBTITLE_TRUTH_LEDGER_ENTRIES_INVALID")
    pieces_raw = spec.get("pieces")
    if not isinstance(pieces_raw, list) or len(pieces_raw) != len(durations):
        raise RuntimeError("SOURCE_SUBTITLE_TRUTH_PIECE_MAPPING_INVALID")
    pieces: list[Mapping[str, object]] = []
    for piece in pieces_raw:
        if not isinstance(piece, Mapping):
            raise RuntimeError("SOURCE_SUBTITLE_TRUTH_PIECE_INVALID")
        pieces.append(piece)

    cues = parse_srt_cues(srt_text)
    texts = [cue.text for cue in cues]
    audit["ledger_sha256"] = "sha256:" + _sha256_bytes(raw)
    audit["status"] = "NO_RELEVANT_INTERVAL"

    for raw_entry in entries:
        if not isinstance(raw_entry, Mapping):
            raise RuntimeError("SOURCE_SUBTITLE_TRUTH_ENTRY_INVALID")
        if raw_entry.get("knowledge_type") != "SOURCE_INTERVAL_TRUTH":
            continue
        truth_id = str(raw_entry.get("truth_id") or "")
        if not truth_id:
            raise RuntimeError("SOURCE_SUBTITLE_TRUTH_ID_MISSING")
        windows = _entry_local_windows(
            raw_entry,
            pieces=pieces,
            durations=durations,
        )
        if not windows:
            continue
        target_indexes = _target_indexes(cues, windows)
        action = str(raw_entry.get("action") or "")
        source_start_ms = int(raw_entry["source_start_ms"])
        source_end_ms = int(raw_entry["source_end_ms"])
        row: dict[str, Any] = {
            "truth_id": truth_id,
            "authority": raw_entry.get("authority"),
            "recording_basename": raw_entry.get("recording_basename"),
            "source_start_ms": source_start_ms,
            "source_end_ms": source_end_ms,
            "action": action,
            "cue_indexes": [index + 1 for index in target_indexes],
            "entry_sha256": "sha256:"
            + _sha256_bytes(
                json.dumps(
                    raw_entry,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ),
        }
        source_duration_ms = source_end_ms - source_start_ms
        source_coverage_ms = _source_coverage_ms(windows)
        row["source_coverage_ms"] = source_coverage_ms
        if source_duration_ms <= 0:
            raise RuntimeError("SOURCE_SUBTITLE_TRUTH_INTERVAL_INVALID")
        if source_coverage_ms != source_duration_ms:
            row["reason_code"] = "SOURCE_INTERVAL_PARTIALLY_RETAINED"
            if raw_entry.get("required") is not False:
                audit["failures"].append(row)
            continue
        before = [texts[index] for index in target_indexes]
        changed = False
        satisfied = False

        if action == "replace_cue":
            replacement = str(raw_entry.get("text") or "")
            if not replacement:
                row["reason_code"] = "REPLACE_CUE_TARGET_NOT_UNIQUE"
            elif len(target_indexes) != 1:
                # 2026-07-19 合并跳切实证：fresh 重转写会把同一源区间切成
                # 两条 cue（或 bleed 进相邻 cue），时间锚定的目标不再唯一。
                # 真值文本已在目标 cue 组里逐字成立（单条等于或跨界拼接包含）
                # 时按 satisfied 记账——no-op 不落刀；未成立才是真失败：
                # 多 cue 替换无法安全落刀，保持 fail-closed。
                joined = "".join(texts[index] for index in target_indexes)
                if replacement in joined or any(
                    texts[index] == replacement for index in target_indexes
                ):
                    satisfied = True
                else:
                    row["reason_code"] = "REPLACE_CUE_TARGET_NOT_UNIQUE"
            else:
                index = target_indexes[0]
                satisfied = texts[index] == replacement
                if not satisfied:
                    texts[index] = replacement
                    changed = True
                    satisfied = True
        elif action == "replace_substring":
            replacements_raw = raw_entry.get("replacements")
            if not isinstance(replacements_raw, list) or not target_indexes:
                row["reason_code"] = "SUBSTRING_TARGET_OR_RULE_MISSING"
            else:
                for index in target_indexes:
                    updated, replacements = _replace_substrings(
                        texts[index],
                        [
                            item
                            for item in replacements_raw
                            if isinstance(item, Mapping)
                        ],
                    )
                    if replacements:
                        texts[index] = updated
                        changed = True
                        row.setdefault("replacements", []).extend(
                            {"cue_index": index + 1, **item}
                            for item in replacements
                        )
                required_text = str(raw_entry.get("required_text") or "")
                satisfied = bool(
                    target_indexes
                    and (
                        not required_text
                        or any(required_text in texts[index] for index in target_indexes)
                    )
                )
        else:
            row["reason_code"] = "ACTION_UNSUPPORTED"

        row["before"] = before
        row["after"] = [texts[index] for index in target_indexes]
        if changed:
            audit["applied"].append(row)
        elif satisfied:
            audit["satisfied"].append(row)
        elif raw_entry.get("required") is not False:
            row.setdefault("reason_code", "REQUIRED_SOURCE_TRUTH_NOT_SATISFIED")
            audit["failures"].append(row)

    if audit["failures"]:
        audit["status"] = "FAILED"
    elif audit["applied"]:
        audit["status"] = "APPLIED"
    elif audit["satisfied"]:
        audit["status"] = "ALREADY_SATISFIED"
    return _render(cues, texts), audit
