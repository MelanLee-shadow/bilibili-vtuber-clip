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
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.autoslice.jingting_chunker import SrtCue, parse_srt_cues


SCHEMA_VERSION = "source-subtitle-truth-ledger.v1"
AUDIT_SCHEMA_VERSION = "source-subtitle-truth-audit.v1"
MIN_CUE_OVERLAP_MS = 80
DROP_CUE_BOUNDARY_EPSILON_MS = 120
SPOKEN_START_CUE_LAG_TOLERANCE_MS = 500
_SOURCE_SHA256_RX = re.compile(r"sha256:[0-9a-f]{64}")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _format_ms(value: int) -> str:
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _render(cues: Sequence[SrtCue], texts: Sequence[str]) -> str:
    blocks = []
    for cue, text in zip(cues, texts):
        if not text.strip():
            continue
        index = len(blocks) + 1
        blocks.append(
            f"{index}\n{_format_ms(cue.start_ms)} --> {_format_ms(cue.end_ms)}\n"
            f"{text}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _piece_media_basename(piece: Mapping[str, object]) -> str:
    for key in ("remote_media", "source_media", "media_path", "path"):
        value = piece.get(key)
        if isinstance(value, str) and value.strip():
            return Path(value).name
    return ""


def _load_source_aliases(
    document: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    """Validate evidence-bound alternate recordings of one source timeline.

    A basename alone is never enough to inherit reviewed subtitle truth.  An
    alias must be declared in the committed ledger and the candidate piece
    must carry the exact source-media SHA-256 from that declaration.  This is
    intentionally stricter than ordinary source matching because an official
    replay can have different leading/trailing bytes or a shifted timeline.
    """

    aliases_raw = document.get("source_aliases", [])
    if not isinstance(aliases_raw, list):
        raise RuntimeError("SOURCE_SUBTITLE_TRUTH_ALIASES_INVALID")
    aliases: list[Mapping[str, object]] = []
    identities: set[tuple[str, str]] = set()
    alias_ids: set[str] = set()
    for raw_alias in aliases_raw:
        if not isinstance(raw_alias, Mapping):
            raise RuntimeError("SOURCE_SUBTITLE_TRUTH_ALIAS_INVALID")
        alias_id = str(raw_alias.get("alias_id") or "")
        alias_name = str(raw_alias.get("alias_recording_basename") or "")
        canonical_name = str(
            raw_alias.get("canonical_recording_basename") or ""
        )
        source_sha256 = str(raw_alias.get("alias_source_sha256") or "")
        offset = raw_alias.get("alias_timeline_offset_ms")
        authority = str(raw_alias.get("authority") or "")
        if not alias_id or alias_id in alias_ids:
            raise RuntimeError("SOURCE_SUBTITLE_TRUTH_ALIAS_ID_INVALID")
        if (
            not alias_name
            or Path(alias_name).name != alias_name
            or not canonical_name
            or Path(canonical_name).name != canonical_name
            or alias_name == canonical_name
        ):
            raise RuntimeError("SOURCE_SUBTITLE_TRUTH_ALIAS_BASENAME_INVALID")
        if _SOURCE_SHA256_RX.fullmatch(source_sha256) is None:
            raise RuntimeError("SOURCE_SUBTITLE_TRUTH_ALIAS_SHA256_INVALID")
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise RuntimeError("SOURCE_SUBTITLE_TRUTH_ALIAS_OFFSET_INVALID")
        if not authority:
            raise RuntimeError("SOURCE_SUBTITLE_TRUTH_ALIAS_AUTHORITY_MISSING")
        identity = (alias_name, source_sha256)
        if identity in identities:
            raise RuntimeError("SOURCE_SUBTITLE_TRUTH_ALIAS_DUPLICATE")
        identities.add(identity)
        alias_ids.add(alias_id)
        aliases.append(raw_alias)
    return tuple(aliases)


def _piece_source_binding(
    piece: Mapping[str, object],
    *,
    canonical_name: str,
    source_aliases: Sequence[Mapping[str, object]],
) -> tuple[int, Mapping[str, object] | None] | None:
    """Return alias-minus-canonical offset plus its evidence row, if bound."""

    piece_name = _piece_media_basename(piece)
    if piece_name == canonical_name:
        return 0, None
    candidates = [
        alias
        for alias in source_aliases
        if alias.get("alias_recording_basename") == piece_name
        and alias.get("canonical_recording_basename") == canonical_name
    ]
    if not candidates:
        return None
    piece_sha256 = piece.get("source_media_sha256")
    if not isinstance(piece_sha256, str) or not piece_sha256:
        raise RuntimeError("SOURCE_SUBTITLE_TRUTH_ALIAS_PIECE_SHA256_MISSING")
    matches = [
        alias
        for alias in candidates
        if alias.get("alias_source_sha256") == piece_sha256
    ]
    if len(matches) != 1:
        raise RuntimeError("SOURCE_SUBTITLE_TRUTH_ALIAS_PIECE_SHA256_MISMATCH")
    alias = matches[0]
    return int(alias["alias_timeline_offset_ms"]), alias


def _entry_local_windows(
    entry: Mapping[str, object],
    *,
    pieces: Sequence[Mapping[str, object]],
    durations: Sequence[int],
    source_aliases: Sequence[Mapping[str, object]] = (),
) -> list[dict[str, object]]:
    source_name = str(entry.get("recording_basename") or "")
    source_start = int(entry["source_start_ms"])
    source_end = int(entry["source_end_ms"])
    windows: list[dict[str, int]] = []
    output_offset = 0
    for piece, duration in zip(pieces, durations):
        piece_start = int(piece["start_ms"])
        piece_end = int(piece["end_ms"])
        binding = _piece_source_binding(
            piece,
            canonical_name=source_name,
            source_aliases=source_aliases,
        )
        if binding is not None:
            # alias_timeline = canonical_timeline + offset
            alias_offset, alias = binding
            canonical_piece_start = piece_start - alias_offset
            canonical_piece_end = piece_end - alias_offset
            overlap_start = max(canonical_piece_start, source_start)
            overlap_end = min(canonical_piece_end, source_end)
            if overlap_start < overlap_end:
                window: dict[str, object] = {
                    "start_ms": (
                        output_offset + overlap_start - canonical_piece_start
                    ),
                    "end_ms": (
                        output_offset + overlap_end - canonical_piece_start
                    ),
                    "source_overlap_start_ms": overlap_start,
                    "source_overlap_end_ms": overlap_end,
                }
                if alias is not None:
                    window.update(
                        {
                            "source_alias_id": alias["alias_id"],
                            "alias_recording_basename": alias[
                                "alias_recording_basename"
                            ],
                            "alias_source_sha256": alias["alias_source_sha256"],
                            "alias_timeline_offset_ms": alias_offset,
                        }
                    )
                windows.append(window)
        output_offset += int(duration)
    return windows


def _source_coverage_ms(windows: Sequence[Mapping[str, object]]) -> int:
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


def _source_point_to_local_ms(
    windows: Sequence[Mapping[str, object]], source_ms: int
) -> int | None:
    """Project one absolute recording timestamp onto the retained timeline."""

    for window in windows:
        source_start = int(window["source_overlap_start_ms"])
        source_end = int(window["source_overlap_end_ms"])
        if source_start <= source_ms < source_end:
            return int(window["start_ms"]) + source_ms - source_start
    return None


_TRUTH_SURFACE_PUNCT_RX = re.compile(r"[\s，。！？；、：,.!?;:\"“”'‘’…~～|·（）()\[\]【】]+")


def _normalize_truth_surface(text: str) -> str:
    """Punctuation/whitespace-insensitive form for cross-cue truth comparison."""

    return _TRUTH_SURFACE_PUNCT_RX.sub("", text)


def _overlap_ms(cue: SrtCue, start_ms: int, end_ms: int) -> int:
    return max(0, min(cue.end_ms, end_ms) - max(cue.start_ms, start_ms))


def _target_indexes(
    cues: Sequence[SrtCue], windows: Sequence[Mapping[str, object]]
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


def _drop_cue_targets(
    cues: Sequence[SrtCue], windows: Sequence[Mapping[str, object]]
) -> tuple[list[int], list[dict[str, Any]]]:
    """Resolve deletions without allowing a truth window to eat real speech.

    A cue may be dropped only when one local truth window contains its complete
    timeline.  The small epsilon absorbs encoder/SRT rounding drift; a cue that
    materially straddles either boundary is a conflict and is left untouched.
    """

    targets: list[int] = []
    conflicts: list[dict[str, Any]] = []
    for index, cue in enumerate(cues):
        overlapping = [
            window
            for window in windows
            if _overlap_ms(cue, int(window["start_ms"]), int(window["end_ms"]))
            >= MIN_CUE_OVERLAP_MS
        ]
        if not overlapping:
            continue
        if any(
            cue.start_ms
            >= int(window["start_ms"]) - DROP_CUE_BOUNDARY_EPSILON_MS
            and cue.end_ms
            <= int(window["end_ms"]) + DROP_CUE_BOUNDARY_EPSILON_MS
            for window in overlapping
        ):
            targets.append(index)
            continue
        conflicts.append(
            {
                "cue_index": index + 1,
                "cue_start_ms": cue.start_ms,
                "cue_end_ms": cue.end_ms,
                "text": cue.text,
                "windows": [
                    {
                        "start_ms": int(window["start_ms"]),
                        "end_ms": int(window["end_ms"]),
                    }
                    for window in overlapping
                ],
            }
        )
    return targets, conflicts


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


def _audit_source_aliases(
    windows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    used = {
        str(window["source_alias_id"]): {
            "alias_id": window["source_alias_id"],
            "alias_recording_basename": window["alias_recording_basename"],
            "alias_source_sha256": window["alias_source_sha256"],
            "alias_timeline_offset_ms": window["alias_timeline_offset_ms"],
        }
        for window in windows
        if "source_alias_id" in window
    }
    return list(used.values())


def _resolve_spoken_start_target(
    cues: Sequence[SrtCue],
    target_indexes: Sequence[int],
    spoken_start_local: int,
) -> tuple[int | None, str | None]:
    """Select the spoken cue without consuming a real pre-onset neighbour.

    A broad reviewed truth window can graze the preceding sentence even though
    its absolute spoken-start pin belongs to the next cue.  Fresh ASR may also
    timestamp that next cue a few hundred milliseconds late.  Accept that
    bounded lag only when exactly one cue can own the onset, every earlier
    overlap ends before it, and no later overlap remains in the truth window.
    """

    eligible = [
        index
        for index in target_indexes
        if cues[index].start_ms
        <= spoken_start_local + SPOKEN_START_CUE_LAG_TOLERANCE_MS
        and spoken_start_local < cues[index].end_ms
    ]
    if len(eligible) != 1:
        return None, "SPOKEN_START_TARGET_NOT_UNIQUE"
    selected = eligible[0]
    if any(index > selected for index in target_indexes) or any(
        cues[index].end_ms > spoken_start_local
        for index in target_indexes
        if index < selected
    ):
        return None, "SPOKEN_START_TARGET_NOT_UNIQUE"
    if selected > 0 and cues[selected - 1].end_ms > spoken_start_local:
        return None, "SPOKEN_START_OVERLAPS_PREVIOUS_CUE"
    return selected, None


def _resolve_spoken_start_binding(
    windows: Sequence[Mapping[str, object]],
    cues: Sequence[SrtCue],
    target_indexes: Sequence[int],
    source_spoken_start_ms: int,
) -> tuple[int | None, int | None, str | None]:
    local_ms = _source_point_to_local_ms(windows, source_spoken_start_ms)
    if local_ms is None:
        return None, None, "SPOKEN_START_OUTSIDE_TARGET_CUE"
    target, error = _resolve_spoken_start_target(cues, target_indexes, local_ms)
    return local_ms, target, error


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
    source_aliases = _load_source_aliases(document)
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
            source_aliases=source_aliases,
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
            # 交付时间轴上的钉子辖区（2026-07-20 kmx r3 案）：终稿校验器的
            # 钉子豁免必须按时间比对——ledger 之后 layout 会重排 cue，序号
            # 映射到终稿会漂移。
            "local_windows": [
                {"start_ms": int(window["start_ms"]), "end_ms": int(window["end_ms"])}
                for window in windows
            ],
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
        used_aliases = _audit_source_aliases(windows)
        if used_aliases:
            row["source_aliases"] = used_aliases
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

        # A removed hallucinated prefix can leave the surviving words displayed
        # over the old prefix's silent/audio-only lead.  Text truth and timing
        # truth are therefore allowed to travel together, but only as an
        # absolute source-timeline speech onset on a unique cue.  Never infer
        # an onset from VAD absence here: this field is reviewed evidence.
        spoken_start_raw = raw_entry.get("spoken_start_ms")
        spoken_start_local: int | None = None
        timing_pin_error: str | None = None
        if spoken_start_raw is not None:
            if action != "replace_cue":
                timing_pin_error = "SPOKEN_START_REQUIRES_REPLACE_CUE"
            elif isinstance(spoken_start_raw, bool) or not isinstance(
                spoken_start_raw, int
            ):
                timing_pin_error = "SPOKEN_START_INVALID"
            elif not source_start_ms <= spoken_start_raw < source_end_ms:
                timing_pin_error = "SPOKEN_START_OUTSIDE_TRUTH_INTERVAL"
            else:
                spoken_start_local, spoken_target, timing_pin_error = (
                    _resolve_spoken_start_binding(
                        windows, cues, target_indexes, spoken_start_raw
                    )
                )
                if spoken_target is not None:
                    target_indexes = [spoken_target]
                    row["cue_indexes"] = [spoken_target + 1]
                    before = [texts[spoken_target]]

        if timing_pin_error is not None:
            row["reason_code"] = timing_pin_error
        elif action == "replace_cue":
            replacement = str(raw_entry.get("text") or "")
            if not replacement:
                row["reason_code"] = "REPLACE_CUE_TARGET_NOT_UNIQUE"
            elif len(target_indexes) != 1:
                # 2026-07-19 合并跳切实证：fresh 重转写会把同一源区间切成
                # 两条 cue（或 bleed 进相邻 cue），时间锚定的目标不再唯一。
                # 真值**内容**已在目标 cue 组里成立时按 satisfied 记账——
                # 比对做去标点归一（「…事情，kmx」跨 cue 时逗号由边界停顿
                # 表达，字符串级比对会被一个标点冤枉）。
                joined_norm = _normalize_truth_surface(
                    "".join(texts[index] for index in target_indexes)
                )
                replacement_norm = _normalize_truth_surface(replacement)
                if replacement_norm and replacement_norm in joined_norm:
                    satisfied = True
                else:
                    # 多 cue 辖区重分配（2026-07-20 kmx r5 案：每轮 fresh 的
                    # cue 切分方差让单目标 fail-closed 变成无限重掷）。钉子
                    # 文本按与现文本的相似度分布到覆盖的连续 cue 上（断点
                    # 吸附标点，词不跨 cue）——内容全部来自 Ivan 审定文本，
                    # 零发明；时间轴与 cue 数不动。
                    from src.autoslice.cue_split_hygiene import (
                        _shift_boundary_punct,
                        _snap_split_to_punct,
                    )
                    from src.autoslice.chat_repair import (
                        _best_text_split,
                        _match_metrics,
                    )

                    # 辖区收缩：与钉文毫无字符共通的 cue 是被窗口误圈的
                    # 邻句（真实内容不许被钉文覆盖），从两端剔除后必须仍
                    # 连续；收缩集与钉文整体相似 ≥0.55 才允许重分配落刀。
                    kept = [
                        index
                        for index in target_indexes
                        if _match_metrics(replacement, texts[index])[4] >= 2
                    ]
                    contiguous = bool(kept) and kept == list(
                        range(kept[0], kept[0] + len(kept))
                    )
                    joined_score = (
                        _match_metrics(
                            replacement,
                            "".join(texts[index] for index in kept),
                        )[0]
                        if kept
                        else 0.0
                    )
                    if contiguous and joined_score >= 0.55:
                        parts = _snap_split_to_punct(
                            _shift_boundary_punct(
                                _best_text_split(
                                    replacement,
                                    [texts[index] for index in kept],
                                )
                            )
                        )
                        if len(parts) == len(kept) and all(
                            part.strip() for part in parts
                        ):
                            for index, part in zip(kept, parts):
                                texts[index] = part
                            changed = True
                            satisfied = True
                            row["multi_cue_redistribution"] = parts
                            row["cue_indexes"] = [index + 1 for index in kept]
                        else:
                            row["reason_code"] = "REPLACE_CUE_TARGET_NOT_UNIQUE"
                    else:
                        row["reason_code"] = "REPLACE_CUE_TARGET_NOT_UNIQUE"
            else:
                index = target_indexes[0]
                satisfied = texts[index] == replacement
                if not satisfied:
                    texts[index] = replacement
                    changed = True
                    satisfied = True
                if spoken_start_local is not None:
                    before_start_ms = cues[index].start_ms
                    if before_start_ms != spoken_start_local:
                        cues[index] = replace(
                            cues[index], start_ms=spoken_start_local
                        )
                        changed = True
                    row["timing_pin"] = {
                        "source_spoken_start_ms": spoken_start_raw,
                        "before_start_ms": before_start_ms,
                        "after_start_ms": spoken_start_local,
                    }
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
        elif action == "drop_cue":
            drop_indexes, conflicts = _drop_cue_targets(cues, windows)
            target_indexes = drop_indexes
            row["cue_indexes"] = [index + 1 for index in target_indexes]
            before = [texts[index] for index in target_indexes]
            if conflicts:
                row["reason_code"] = "DROP_CUE_STRADDLES_TRUTH_INTERVAL"
                row["conflicts"] = conflicts
                row["drop_status"] = "CONFLICT"
            elif not target_indexes:
                # Desired absence is idempotent.  Source coverage above still
                # proves that this candidate actually retains the truth span.
                satisfied = True
                row["drop_status"] = "ALREADY_ABSENT"
            else:
                changed = any(texts[index].strip() for index in target_indexes)
                for index in target_indexes:
                    texts[index] = ""
                satisfied = True
                row["drop_status"] = "APPLIED" if changed else "ALREADY_ABSENT"
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


def ledger_local_windows(
    *,
    spec: Mapping[str, object],
    durations: Sequence[int],
    ledger_path: Path | None,
) -> list[tuple[int, int]]:
    """本候选交付时间轴上所有钉子的辖区窗口（只算不改）。

    2026-07-20 七星 r6 案：实体声学仲裁抢在钉子落刀前对「零三」烧完整条
    key ladder 再 fail-closed——ledger 已拥有的 span 不该进任何后置仲裁。
    加载失败返回空（豁免消失=门更严，安全方向）。"""

    try:
        if ledger_path is None or not ledger_path.is_file():
            return []
        document = json.loads(ledger_path.read_bytes().decode("utf-8"))
        entries = document.get("entries") or []
        source_aliases = _load_source_aliases(document)
        pieces = [p for p in (spec.get("pieces") or []) if isinstance(p, Mapping)]
        if len(pieces) != len(durations):
            return []
        out: list[tuple[int, int]] = []
        for raw_entry in entries:
            if (
                not isinstance(raw_entry, Mapping)
                or raw_entry.get("knowledge_type") != "SOURCE_INTERVAL_TRUTH"
            ):
                continue
            for window in _entry_local_windows(
                raw_entry,
                pieces=pieces,
                durations=durations,
                source_aliases=source_aliases,
            ):
                out.append((int(window["start_ms"]), int(window["end_ms"])))
        return out
    except Exception:
        return []
