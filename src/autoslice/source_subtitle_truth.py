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

from src.autoslice.source_truth_target_selection import (  # noqa: E402
    MIN_CUE_OVERLAP_MS,
    _drop_cue_targets,
    _target_indexes,
)
from src.autoslice.jingting_chunker import SrtCue, parse_srt_cues


SCHEMA_VERSION = "source-subtitle-truth-ledger.v1"
AUDIT_SCHEMA_VERSION = "source-subtitle-truth-audit.v1"
PREVIEW_SCHEMA_VERSION = "source-truth-deterministic-preview.v1"
SPOKEN_START_CUE_LAG_TOLERANCE_MS = 500
# Candidate recall timestamps and subtitle cue onsets may differ by a few
# frames.  Boundary ownership may absorb only this bounded lead jitter; larger
# lead/story or any trailing story/context straddle remains fail-closed.
BOUNDARY_OWNER_LEAD_TOLERANCE_MS = 500
_SOURCE_SHA256_RX = re.compile(r"sha256:[0-9a-f]{64}")
_ASSERTION_STATES = frozenset(
    {"PROPOSED", "VERIFIED_ACTIVE", "REJECTED", "CONFLICTED", "SUPERSEDED"}
)
_BOUNDARY_ROLES = frozenset({"story_content", "next_topic_witness"})


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


def _assertion_state(entry: Mapping[str, object]) -> str:
    """Legacy rows are active; revision-aware rows must name a valid state."""

    state = entry.get("assertion_state")
    if state is None:
        return "VERIFIED_ACTIVE"
    value = str(state)
    if value not in _ASSERTION_STATES:
        raise RuntimeError("SOURCE_SUBTITLE_TRUTH_ASSERTION_STATE_INVALID")
    return value


def _boundary_role(entry: Mapping[str, object]) -> str:
    """Classify whether a required truth owns the delivery endpoint."""

    value = str(entry.get("boundary_role") or "story_content")
    if value not in _BOUNDARY_ROLES:
        raise RuntimeError("SOURCE_SUBTITLE_TRUTH_BOUNDARY_ROLE_INVALID")
    return value


def _skip_inactive_assertion(
    *,
    audit: dict[str, Any],
    entry: Mapping[str, object],
    truth_id: str,
    assertion_state: str,
) -> bool:
    if assertion_state in {"PROPOSED", "REJECTED", "SUPERSEDED"}:
        audit.setdefault("inactive", []).append(
            {
                "truth_id": truth_id,
                "assertion_state": assertion_state,
                "superseded_by": entry.get("superseded_by"),
            }
        )
        return True
    if assertion_state == "CONFLICTED":
        audit["failures"].append(
            {
                "truth_id": truth_id,
                "assertion_state": assertion_state,
                "reason_code": "SOURCE_TRUTH_ASSERTION_CONFLICTED",
                "recording_basename": entry.get("recording_basename"),
                "source_start_ms": int(entry["source_start_ms"]),
                "source_end_ms": int(entry["source_end_ms"]),
            }
        )
        return True
    return False


def _forbidden_token_failure(
    *,
    entry: Mapping[str, object],
    texts: Sequence[str],
    target_indexes: Sequence[int],
    row: dict[str, Any],
) -> dict[str, Any] | None:
    forbidden = entry.get("forbidden_tokens", [])
    if not isinstance(forbidden, list) or any(
        not isinstance(token, str) or not token for token in forbidden
    ):
        return {**row, "reason_code": "FORBIDDEN_TOKENS_INVALID"}
    hits = [
        token
        for token in forbidden
        if any(token.casefold() in texts[index].casefold() for index in target_indexes)
    ]
    if not hits:
        return None
    return {
        **row,
        "reason_code": "FORBIDDEN_TOKEN_SURVIVED_SOURCE_TRUTH",
        "forbidden_token_hits": hits,
    }


def _resolve_mention_postcondition_targets(
    *,
    entry: Mapping[str, object],
    pieces: Sequence[Mapping[str, object]],
    durations: Sequence[int],
    source_aliases: Sequence[Mapping[str, object]],
    cues: Sequence[SrtCue],
    row: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve independently reviewed mention intervals to isolated cues.

    A broad repair interval may contain the same name several times.  Merely
    finding the canonical spelling once in that interval allowed one correct
    mention to hide another wrong one (the 2026-07-22 毁神/绘声 incident).
    ``mention_postconditions`` bind every required occurrence to its own
    absolute source interval.  This resolver is shared by mutation ownership
    and postcondition verification so a correct pre-existing spelling cannot
    become an ambiguous broad-window owner, while merged mentions still fail
    closed.
    """

    raw_conditions = entry.get("mention_postconditions")
    if raw_conditions is None:
        return [], []
    if not isinstance(raw_conditions, list) or not raw_conditions:
        return [], [
            {**row, "reason_code": "MENTION_POSTCONDITIONS_INVALID"}
        ]

    parent_start = int(entry["source_start_ms"])
    parent_end = int(entry["source_end_ms"])
    resolved: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for ordinal, condition in enumerate(raw_conditions, start=1):
        if not isinstance(condition, Mapping):
            failures.append(
                {
                    **row,
                    "reason_code": "MENTION_POSTCONDITION_INVALID",
                    "mention_ordinal": ordinal,
                }
            )
            continue
        start = condition.get("source_start_ms")
        end = condition.get("source_end_ms")
        required_text = str(condition.get("required_text") or "")
        forbidden = condition.get("forbidden_tokens", [])
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or not parent_start <= start < end <= parent_end
            or not required_text
            or not isinstance(forbidden, list)
            or any(not isinstance(token, str) or not token for token in forbidden)
        ):
            failures.append(
                {
                    **row,
                    "reason_code": "MENTION_POSTCONDITION_INVALID",
                    "mention_ordinal": ordinal,
                }
            )
            continue
        mention_entry = {
            **entry,
            "source_start_ms": start,
            "source_end_ms": end,
        }
        mention_windows = _entry_local_windows(
            mention_entry,
            pieces=pieces,
            durations=durations,
            source_aliases=source_aliases,
        )
        mention_targets = _target_indexes(cues, mention_windows)
        if (
            _source_coverage_ms(mention_windows) != end - start
            or not mention_targets
        ):
            failures.append(
                {
                    **row,
                    "reason_code": "MENTION_POSTCONDITION_TARGET_MISSING",
                    "mention_ordinal": ordinal,
                    "source_start_ms": start,
                    "source_end_ms": end,
                }
            )
            continue
        resolved.append(
            {
                "ordinal": ordinal,
                "condition": condition,
                "target_indexes": mention_targets,
                "forbidden_tokens": forbidden,
            }
        )

    cue_owners: dict[int, set[int]] = {}
    for resolved_row in resolved:
        for cue_index in resolved_row["target_indexes"]:
            cue_owners.setdefault(cue_index, set()).add(
                int(resolved_row["ordinal"])
            )
    overlapping_ordinals = {
        ordinal
        for owners in cue_owners.values()
        if len(owners) > 1
        for ordinal in owners
    }

    isolated: list[dict[str, Any]] = []
    for resolved_row in resolved:
        ordinal = int(resolved_row["ordinal"])
        condition = resolved_row["condition"]
        mention_targets = resolved_row["target_indexes"]
        audit_row = {
            **row,
            "mention_ordinal": ordinal,
            "mention_source_start_ms": int(condition["source_start_ms"]),
            "mention_source_end_ms": int(condition["source_end_ms"]),
            "mention_cue_indexes": [index + 1 for index in mention_targets],
        }
        if ordinal in overlapping_ordinals:
            failures.append(
                {
                    **audit_row,
                    "reason_code": "MENTION_POSTCONDITION_TARGET_NOT_ISOLATED",
                }
            )
            continue
        isolated.append(resolved_row)
    return isolated, failures


def _mention_postcondition_failures(
    *,
    entry: Mapping[str, object],
    pieces: Sequence[Mapping[str, object]],
    durations: Sequence[int],
    source_aliases: Sequence[Mapping[str, object]],
    cues: Sequence[SrtCue],
    texts: Sequence[str],
    row: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Verify the canonical and forbidden surfaces at every exact mention."""

    resolved, failures = _resolve_mention_postcondition_targets(
        entry=entry,
        pieces=pieces,
        durations=durations,
        source_aliases=source_aliases,
        cues=cues,
        row=row,
    )
    for resolved_row in resolved:
        ordinal = int(resolved_row["ordinal"])
        condition = resolved_row["condition"]
        mention_targets = resolved_row["target_indexes"]
        forbidden = resolved_row["forbidden_tokens"]
        audit_row = {
            **row,
            "mention_ordinal": ordinal,
            "mention_source_start_ms": int(condition["source_start_ms"]),
            "mention_source_end_ms": int(condition["source_end_ms"]),
            "mention_cue_indexes": [
                index + 1 for index in mention_targets
            ],
        }
        joined = "".join(texts[index] for index in mention_targets)
        required_text = str(condition["required_text"])
        if _normalize_truth_surface(required_text) not in _normalize_truth_surface(joined):
            failures.append(
                {
                    **audit_row,
                    "reason_code": "MENTION_REQUIRED_TEXT_MISSING",
                    "required_text": required_text,
                    "actual_text": joined,
                }
            )
            continue
        hits = [token for token in forbidden if token.casefold() in joined.casefold()]
        if hits:
            failures.append(
                {
                    **audit_row,
                    "reason_code": "MENTION_FORBIDDEN_TOKEN_SURVIVED",
                    "forbidden_token_hits": hits,
                }
            )
    return failures


def _source_truth_postcondition_failures(
    entry: Mapping[str, object],
    pieces: Sequence[Mapping[str, object]],
    durations: Sequence[int],
    source_aliases: Sequence[Mapping[str, object]],
    cues: Sequence[SrtCue],
    texts: Sequence[str],
    target_indexes: Sequence[int],
    row: Mapping[str, Any],
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    forbidden = _forbidden_token_failure(
        entry=entry,
        texts=texts,
        target_indexes=target_indexes,
        row=dict(row),
    )
    if forbidden is not None:
        failures.append(forbidden)
    failures.extend(
        _mention_postcondition_failures(
            entry=entry,
            pieces=pieces,
            durations=durations,
            source_aliases=source_aliases,
            cues=cues,
            texts=texts,
            row=row,
        )
    )
    return failures


def _source_truth_audit_row(
    *,
    entry: Mapping[str, object],
    truth_id: str,
    assertion_state: str,
    action: str,
    target_indexes: Sequence[int],
    windows: Sequence[Mapping[str, object]],
) -> dict[str, Any]:
    entry_bytes = json.dumps(
        entry,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    canonical_texts: list[str] = []
    required_text = ""
    if action == "replace_cue":
        replacement = str(entry.get("text") or "")
        if replacement:
            canonical_texts.append(replacement)
    elif action == "replace_substring":
        required_text = str(entry.get("required_text") or "")

    return {
        "truth_id": truth_id,
        "revision_id": entry.get("revision_id"),
        "assertion_state": assertion_state,
        "supersedes": entry.get("supersedes"),
        "evidence_class": entry.get("evidence_class"),
        "before_span": entry.get("before_span"),
        "authority": entry.get("authority"),
        "recording_basename": entry.get("recording_basename"),
        "source_start_ms": int(entry["source_start_ms"]),
        "source_end_ms": int(entry["source_end_ms"]),
        "required": entry.get("required") is not False,
        "boundary_role": _boundary_role(entry),
        "action": action,
        # Typed, ledger-derived positive output authority.  Downstream guards
        # may use these exact canonical surfaces as witnesses (for example a
        # structured-chat username containing kana).  They must never infer a
        # witness from the whole post-edit ``after`` cue, which can include
        # unrelated text merely sharing this interval.
        "declared_output_contract": {
            "schema_version": "source-truth-declared-output.v1",
            "action": action,
            "canonical_texts": canonical_texts,
            "required_text": required_text,
        },
        "cue_indexes": [index + 1 for index in target_indexes],
        "local_windows": [
            {"start_ms": int(window["start_ms"]), "end_ms": int(window["end_ms"])}
            for window in windows
        ],
        "entry_sha256": "sha256:" + _sha256_bytes(entry_bytes),
    }


def validated_source_truth_projection(
    row: Mapping[str, object],
) -> dict[str, Any] | None:
    """Validate and return one post-apply cue projection, if present."""

    projection = row.get("resolved_target_projection")
    if not isinstance(projection, Mapping):
        return None
    action = str(row.get("action") or "")
    status = projection.get("status")
    raw_cues = projection.get("cues")
    raw_indexes = row.get("cue_indexes")
    raw_windows = row.get("local_windows")
    if (
        projection.get("schema_version")
        != "source-truth-resolved-target-projection.v1"
        or projection.get("selector")
        != "half-open-overlap-gte-min-then-action-resolution"
        or projection.get("min_overlap_ms") != MIN_CUE_OVERLAP_MS
        or projection.get("action") != action
        or status not in {"RESOLVED", "ALREADY_ABSENT"}
        or not isinstance(raw_cues, list)
        or not isinstance(raw_indexes, list)
        or not isinstance(raw_windows, list)
        or (status == "ALREADY_ABSENT" and (action != "drop_cue" or raw_cues))
        or (status == "RESOLVED" and not raw_cues)
    ):
        return None
    cue_indexes = [
        value
        for value in raw_indexes
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    if len(cue_indexes) != len(raw_indexes):
        return None
    local_windows: list[tuple[int, int]] = []
    for raw_window in raw_windows:
        if not isinstance(raw_window, Mapping):
            return None
        window_start = raw_window.get("start_ms")
        window_end = raw_window.get("end_ms")
        if (
            isinstance(window_start, bool)
            or not isinstance(window_start, int)
            or isinstance(window_end, bool)
            or not isinstance(window_end, int)
            or window_start >= window_end
        ):
            return None
        local_windows.append((window_start, window_end))
    if not local_windows:
        return None
    cues: list[dict[str, Any]] = []
    previous_index = 0
    previous_start = -1
    for raw in raw_cues:
        if not isinstance(raw, Mapping):
            return None
        cue_index = raw.get("cue_index")
        start_ms = raw.get("start_ms")
        end_ms = raw.get("end_ms")
        before_text = raw.get("before_text")
        after_text = raw.get("after_text")
        if (
            isinstance(cue_index, bool)
            or not isinstance(cue_index, int)
            or cue_index <= previous_index
            or isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or start_ms >= end_ms
            or start_ms < previous_start
            or not isinstance(before_text, str)
            or not isinstance(after_text, str)
        ):
            return None
        cues.append(
            {
                "cue_index": cue_index,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "before_text": before_text,
                "after_text": after_text,
            }
        )
        previous_index = cue_index
        previous_start = start_ms
    if [cue["cue_index"] for cue in cues] != cue_indexes:
        return None
    if status == "ALREADY_ABSENT" and cue_indexes:
        return None
    timing_pin = row.get("timing_pin")
    timing_pin_index = cue_indexes[0] if len(cue_indexes) == 1 else None
    if timing_pin is not None:
        if (
            not isinstance(timing_pin, Mapping)
            or action != "replace_cue"
            or timing_pin_index is None
            or isinstance(timing_pin.get("before_start_ms"), bool)
            or not isinstance(timing_pin.get("before_start_ms"), int)
            or isinstance(timing_pin.get("after_start_ms"), bool)
            or not isinstance(timing_pin.get("after_start_ms"), int)
        ):
            return None
    for cue in cues:
        selection_start = int(cue["start_ms"])
        if timing_pin is not None and cue["cue_index"] == timing_pin_index:
            if int(timing_pin["after_start_ms"]) != selection_start:
                return None
            if not any(
                window_start <= selection_start < window_end
                for window_start, window_end in local_windows
            ):
                return None
            selection_start = int(timing_pin["before_start_ms"])
        selection_end = int(cue["end_ms"])
        if selection_start >= selection_end or not any(
            min(selection_end, window_end)
            - max(selection_start, window_start)
            >= MIN_CUE_OVERLAP_MS
            for window_start, window_end in local_windows
        ):
            return None
    return {
        "schema_version": projection["schema_version"],
        "selector": projection["selector"],
        "min_overlap_ms": MIN_CUE_OVERLAP_MS,
        "action": action,
        "status": status,
        "cues": cues,
    }


def source_truth_owner_windows(
    row: Mapping[str, object],
) -> list[tuple[int, int]]:
    """Return effective owner windows, preferring post-apply projection."""

    if "resolved_target_projection" in row:
        projection = validated_source_truth_projection(row)
        if projection is None:
            return []
        if projection["status"] == "RESOLVED":
            return [
                (int(cue["start_ms"]), int(cue["end_ms"]))
                for cue in projection["cues"]
            ]
        # An already-absent drop has no cue projection.  Its reviewed source
        # span remains the only meaningful absence window.
    raw_windows = row.get("local_windows")
    if not isinstance(raw_windows, list):
        return []
    windows: list[tuple[int, int]] = []
    for window in raw_windows:
        if not isinstance(window, Mapping):
            return []
        start = window.get("start_ms")
        end = window.get("end_ms")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start >= end
        ):
            return []
        windows.append((start, end))
    return windows


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + _sha256_bytes(encoded)


def _preview_input_grid(cues: Sequence[SrtCue]) -> list[dict[str, object]]:
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


def _preview_raw_windows(
    row: Mapping[str, object],
    *,
    truth_id: str,
) -> list[tuple[int, int]]:
    raw_windows = row.get("local_windows")
    if not isinstance(raw_windows, list) or not raw_windows:
        raise RuntimeError(
            f"SOURCE_TRUTH_PREVIEW_FALLBACK_WINDOWS_INVALID: {truth_id}"
        )
    windows: list[tuple[int, int]] = []
    for raw_window in raw_windows:
        if not isinstance(raw_window, Mapping):
            raise RuntimeError(
                f"SOURCE_TRUTH_PREVIEW_FALLBACK_WINDOWS_INVALID: {truth_id}"
            )
        start_ms = raw_window.get("start_ms")
        end_ms = raw_window.get("end_ms")
        if (
            isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or start_ms >= end_ms
        ):
            raise RuntimeError(
                f"SOURCE_TRUTH_PREVIEW_FALLBACK_WINDOWS_INVALID: {truth_id}"
            )
        windows.append((start_ms, end_ms))
    return windows


def _projection_matches_preview_grid(
    row: Mapping[str, object],
    projection: Mapping[str, object],
    cues: Sequence[SrtCue],
) -> bool:
    """Bind a validated post-apply projection back to its pre-apply cue grid.

    Text may legitimately reflect an earlier truth row on the same cue, so the
    binding uses cue ordinal and timing.  A reviewed ``spoken_start_ms`` pin is
    the only permitted timing change.
    """

    projected_cues = projection.get("cues")
    if not isinstance(projected_cues, list):
        return False
    timing_pin = row.get("timing_pin")
    timing_pin_index: int | None = None
    if isinstance(timing_pin, Mapping):
        raw_indexes = row.get("cue_indexes")
        if not isinstance(raw_indexes, list) or len(raw_indexes) != 1:
            return False
        timing_pin_index = int(raw_indexes[0])
    for projected in projected_cues:
        if not isinstance(projected, Mapping):
            return False
        cue_index = int(projected["cue_index"])
        if cue_index < 1 or cue_index > len(cues):
            return False
        source_cue = cues[cue_index - 1]
        if int(projected["end_ms"]) != source_cue.end_ms:
            return False
        if timing_pin_index == cue_index:
            assert isinstance(timing_pin, Mapping)
            if (
                int(timing_pin["before_start_ms"]) != source_cue.start_ms
                or int(timing_pin["after_start_ms"])
                != int(projected["start_ms"])
            ):
                return False
        elif int(projected["start_ms"]) != source_cue.start_ms:
            return False
    return True


def build_source_truth_preview_receipt(
    *,
    input_srt_text: str,
    source_truth_audit: Mapping[str, object],
    stage: str,
) -> dict[str, Any]:
    """Build protection evidence without returning projected subtitle text.

    The caller may run :func:`apply_source_subtitle_truth` on a draft solely to
    discover deterministic targets, then discard that function's rendered SRT
    and pass its audit here.  Required successful rows protect only exact,
    validated post-apply cue projections.  Required failures deliberately
    ignore any attached projection and fall back to their raw discovery
    windows at the same 80 ms half-open overlap threshold.  Optional truths
    never grant protection or ownership.
    """

    if not isinstance(stage, str) or not stage.strip():
        raise RuntimeError("SOURCE_TRUTH_PREVIEW_STAGE_INVALID")
    if (
        source_truth_audit.get("schema_version") != AUDIT_SCHEMA_VERSION
        or not isinstance(source_truth_audit.get("applied"), list)
        or not isinstance(source_truth_audit.get("satisfied"), list)
        or not isinstance(source_truth_audit.get("failures"), list)
    ):
        raise RuntimeError("SOURCE_TRUTH_PREVIEW_AUDIT_INVALID")

    cues = parse_srt_cues(input_srt_text)
    if any(cue.start_ms >= cue.end_ms for cue in cues):
        raise RuntimeError("SOURCE_TRUTH_PREVIEW_INPUT_GRID_INVALID")
    grid = _preview_input_grid(cues)
    ledger_sha256 = source_truth_audit.get("ledger_sha256")
    has_rows = any(
        source_truth_audit.get(key)
        for key in ("applied", "satisfied", "failures")
    )
    if (
        ledger_sha256 is not None
        and (
            not isinstance(ledger_sha256, str)
            or _SOURCE_SHA256_RX.fullmatch(ledger_sha256) is None
        )
    ) or (
        has_rows and ledger_sha256 is None
    ):
        raise RuntimeError("SOURCE_TRUTH_PREVIEW_LEDGER_BINDING_INVALID")

    exact_owners: list[dict[str, object]] = []
    unresolved_fallbacks: list[dict[str, object]] = []
    ignored_optional_truth_ids: set[str] = set()
    exact_indexes: set[int] = set()
    fallback_indexes: set[int] = set()
    fallback_windows: set[tuple[int, int]] = set()

    for bucket in ("applied", "satisfied"):
        rows = source_truth_audit[bucket]
        assert isinstance(rows, list)
        for raw_row in rows:
            if not isinstance(raw_row, Mapping):
                raise RuntimeError("SOURCE_TRUTH_PREVIEW_AUDIT_ROW_INVALID")
            truth_id = str(raw_row.get("truth_id") or "")
            if not truth_id:
                raise RuntimeError("SOURCE_TRUTH_PREVIEW_TRUTH_ID_MISSING")
            if raw_row.get("required") is False:
                ignored_optional_truth_ids.add(truth_id)
                continue
            projection = validated_source_truth_projection(raw_row)
            if projection is None or not _projection_matches_preview_grid(
                raw_row, projection, cues
            ):
                raise RuntimeError(
                    f"SOURCE_TRUTH_PREVIEW_PROJECTION_INVALID: {truth_id}"
                )
            cue_indexes = [
                int(cue["cue_index"])
                for cue in projection["cues"]
            ]
            exact_indexes.update(cue_indexes)
            exact_owners.append(
                {
                    "truth_id": truth_id,
                    "entry_sha256": raw_row.get("entry_sha256"),
                    "source_bucket": bucket,
                    "projection_status": projection["status"],
                    "cue_indexes": cue_indexes,
                    # Bind the full validated projection without exposing its
                    # before/after subtitle text as preview output.
                    "projection_sha256": _canonical_sha256(projection),
                }
            )

    for raw_row in source_truth_audit["failures"]:
        if not isinstance(raw_row, Mapping):
            raise RuntimeError("SOURCE_TRUTH_PREVIEW_AUDIT_ROW_INVALID")
        truth_id = str(raw_row.get("truth_id") or "")
        if not truth_id:
            raise RuntimeError("SOURCE_TRUTH_PREVIEW_TRUTH_ID_MISSING")
        if raw_row.get("required") is False:
            ignored_optional_truth_ids.add(truth_id)
            continue
        windows = _preview_raw_windows(raw_row, truth_id=truth_id)
        # Never inspect ``resolved_target_projection`` here.  A failed truth
        # has no exact owner, even if a stale/tampered projection is attached.
        window_rows = [
            {"start_ms": start_ms, "end_ms": end_ms}
            for start_ms, end_ms in windows
        ]
        cue_indexes = [
            index + 1 for index in _target_indexes(cues, window_rows)
        ]
        fallback_indexes.update(cue_indexes)
        fallback_windows.update(windows)
        unresolved_fallbacks.append(
            {
                "truth_id": truth_id,
                "entry_sha256": raw_row.get("entry_sha256"),
                "reason_code": raw_row.get("reason_code"),
                "local_windows": window_rows,
                "cue_indexes": cue_indexes,
            }
        )

    receipt: dict[str, Any] = {
        "schema_version": PREVIEW_SCHEMA_VERSION,
        "status": (
            "UNRESOLVED_REQUIRED"
            if unresolved_fallbacks
            else "PASS"
        ),
        "stage": stage.strip(),
        "min_overlap_ms": MIN_CUE_OVERLAP_MS,
        "input_srt_sha256": (
            "sha256:" + _sha256_bytes(input_srt_text.encode("utf-8"))
        ),
        "input_cue_grid_sha256": _canonical_sha256(
            {
                "schema_version": "source-truth-preview-input-grid.v1",
                "cues": grid,
            }
        ),
        "ledger_sha256": ledger_sha256,
        "source_truth_audit_sha256": _canonical_sha256(
            source_truth_audit
        ),
        "source_truth_audit_status": source_truth_audit.get("status"),
        "exact_projection_owners": exact_owners,
        "unresolved_fallbacks": unresolved_fallbacks,
        "ignored_optional_truth_ids": sorted(
            ignored_optional_truth_ids
        ),
        "exact_protected_cue_indexes": sorted(exact_indexes),
        "fallback_protected_cue_indexes": sorted(fallback_indexes),
        "protected_cue_indexes": sorted(
            exact_indexes | fallback_indexes
        ),
        "fallback_local_windows": [
            {"start_ms": start_ms, "end_ms": end_ms}
            for start_ms, end_ms in sorted(fallback_windows)
        ],
    }
    receipt["receipt_sha256"] = _canonical_sha256(receipt)
    return receipt


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


def _truth_pinyin_ratio(a: str, b: str) -> float:
    """Toneless-pinyin similarity for truth-window dominion checks.

    672 案（2026-07-27）：转录把「南町nightin」误听成「难听难听」——字符级
    零共通但语音同一。真值窗辖区判定必须能看见语音等价，否则误听 cue 被
    辖区收缩踢出、replace_cue 永远 NOT_UNIQUE。独立小实现，避免与审片
    模块耦合；pypinyin 缺失时返回 0（退回纯字符行为）。
    """

    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return 0.0
    if not a or not b:
        return 0.0
    from difflib import SequenceMatcher

    return SequenceMatcher(
        None,
        " ".join(str(t).lower() for t in lazy_pinyin(a)),
        " ".join(str(t).lower() for t in lazy_pinyin(b)),
    ).ratio()


def _cue_window_containment(
    cue: SrtCue,
    windows: Sequence[Mapping[str, object]],
) -> float:
    """Fraction of the cue's duration owned by truth windows (0.0-1.0)."""

    duration = max(1, cue.end_ms - cue.start_ms)
    covered = 0
    for window in windows:
        try:
            start_ms = int(window["start_ms"])  # type: ignore[index]
            end_ms = int(window["end_ms"])  # type: ignore[index]
        except (KeyError, TypeError, ValueError):
            continue
        covered += max(0, min(cue.end_ms, end_ms) - max(cue.start_ms, start_ms))
    return min(1.0, covered / duration)


def _apply_replace_cue_action(
    *,
    entry: Mapping[str, object],
    cues: list[SrtCue],
    texts: list[str],
    target_indexes: list[int],
    before_by_index: Mapping[int, str],
    before: list[str],
    row: dict[str, Any],
    spoken_start_raw: object,
    spoken_start_local: int | None,
    windows: Sequence[tuple[int, int]] = (),
) -> tuple[bool, bool, list[int], list[str]]:
    """Apply one ``replace_cue`` row without changing orchestration order."""

    changed = False
    satisfied = False
    replacement = str(entry.get("text") or "")
    if not replacement:
        row["reason_code"] = "REPLACE_CUE_TARGET_NOT_UNIQUE"
    elif len(target_indexes) != 1:
        from src.autoslice.chat_repair import (
            _best_text_split,
            _match_metrics,
        )
        from src.autoslice.cue_split_hygiene import (
            _shift_boundary_punct,
            _snap_split_to_punct,
        )

        # 2026-07-19 合并跳切实证：fresh 重转写会把同一源区间切成
        # 两条 cue（或 bleed 进相邻 cue），时间锚定的目标不再唯一。
        # 真值**内容**已在目标 cue 组里成立时按 satisfied 记账——
        # 比对做去标点归一（「…事情，kmx」跨 cue 时逗号由边界停顿
        # 表达，字符串级比对会被一个标点冤枉）。
        joined_norm = _normalize_truth_surface(
            "".join(texts[index] for index in target_indexes)
        )
        replacement_norm = _normalize_truth_surface(replacement)
        kept = [
            index
            for index in target_indexes
            # 字符共通 ≥2 或语音相似 ≥0.30：误听 cue（难听难听≈南町）不许
            # 被辖区收缩当邻句踢出——语音等价就是窗内成员资格。
            if _match_metrics(replacement, texts[index])[4] >= 2
            or _truth_pinyin_ratio(replacement, texts[index]) >= 0.30
        ]
        contiguous = bool(kept) and kept == list(
            range(kept[0], kept[0] + len(kept))
        )
        kept_joined_norm = _normalize_truth_surface(
            "".join(texts[index] for index in kept)
        )
        if replacement_norm and replacement_norm in joined_norm:
            satisfied = True
            if contiguous and replacement_norm in kept_joined_norm:
                target_indexes = kept
                row["cue_indexes"] = [
                    index + 1 for index in target_indexes
                ]
                before = [
                    before_by_index[index] for index in target_indexes
                ]
        else:
            # 多 cue 辖区重分配（2026-07-20 kmx r5 案：每轮 fresh 的
            # cue 切分方差让单目标 fail-closed 变成无限重掷）。钉子
            # 文本按与现文本的相似度分布到覆盖的连续 cue 上（断点
            # 吸附标点，词不跨 cue）——内容全部来自 Ivan 审定文本，
            # 零发明；时间轴与 cue 数不动。
            # 辖区收缩：与钉文毫无字符共通的 cue 是被窗口误圈的
            # 邻句（真实内容不许被钉文覆盖），从两端剔除后必须仍
            # 连续；收缩集与钉文整体相似 ≥0.55 才允许重分配落刀。
            kept_joined_text = "".join(texts[index] for index in kept)
            joined_char_score = (
                _match_metrics(replacement, kept_joined_text)[0]
                if kept
                else 0.0
            )
            # 语音相似与字符相似取 max：误听窗（南町nightin→难听难听）字符
            # 相似 ~0.50 不过线，拼音相似过线。
            joined_score = max(
                joined_char_score,
                _truth_pinyin_ratio(replacement, kept_joined_text)
                if kept
                else 0.0,
            )
            # 时间包含度准入（672 重复句窗）：全部 kept cue ≥90% 时长在
            # 真值窗内 → 窗口时间即 Ivan 裁定的辖区，文本相似只留 0.30
            # 底线防「错窗错配」；任何骑缘 cue 使该准入失效（邻句保护）。
            window_containment_admission = bool(
                kept
                and windows
                and joined_score >= 0.30
                and all(
                    _cue_window_containment(cues[index], windows) >= 0.90
                    for index in kept
                )
            )
            if window_containment_admission:
                row["window_containment_admission"] = True
            if contiguous and (
                joined_score >= 0.55 or window_containment_admission
            ):
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
                    target_indexes = kept
                    row["cue_indexes"] = [
                        index + 1 for index in target_indexes
                    ]
                    before = [
                        before_by_index[index] for index in target_indexes
                    ]
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
    return changed, satisfied, target_indexes, before


def _apply_replace_substring_action(
    *,
    entry: Mapping[str, object],
    pieces: Sequence[Mapping[str, object]],
    durations: Sequence[int],
    source_aliases: Sequence[Mapping[str, object]],
    cues: Sequence[SrtCue],
    texts: list[str],
    target_indexes: list[int],
    before_by_index: Mapping[int, str],
    before: list[str],
    row: dict[str, Any],
) -> tuple[bool, bool, list[int], list[str]]:
    """Apply one substring truth with mention-scoped mutation ownership."""

    replacements_raw = entry.get("replacements")
    if not isinstance(replacements_raw, list) or not target_indexes:
        row["reason_code"] = "SUBSTRING_TARGET_OR_RULE_MISSING"
        return False, False, target_indexes, before

    mention_owned_indexes: list[int] = []
    mutation_indexes = list(target_indexes)
    if entry.get("mention_postconditions") is not None:
        resolved_mentions, mention_resolution_failures = (
            _resolve_mention_postcondition_targets(
                entry=entry,
                pieces=pieces,
                durations=durations,
                source_aliases=source_aliases,
                cues=cues,
                row=row,
            )
        )
        if not mention_resolution_failures:
            mention_owned_indexes = sorted(
                {
                    index
                    for mention in resolved_mentions
                    for index in mention["target_indexes"]
                }
            )
        row["mention_owner_resolution"] = {
            "status": "PASS" if mention_owned_indexes else "BLOCK",
            "cue_indexes": [
                index + 1 for index in mention_owned_indexes
            ],
            "failure_reason_codes": sorted(
                {
                    str(
                        failure.get("reason_code")
                        or "MENTION_OWNER_RESOLUTION_FAILED"
                    )
                    for failure in mention_resolution_failures
                }
            ),
        }
        if mention_owned_indexes:
            mutation_indexes = mention_owned_indexes
            target_indexes = mention_owned_indexes
            row["cue_indexes"] = [
                index + 1 for index in target_indexes
            ]
            before = [
                before_by_index.get(index, texts[index])
                for index in target_indexes
            ]
        else:
            # A mention resolution failure owns neither mutation nor audit
            # projection. The shared postcondition check records the exact
            # typed failure after this no-op.
            mutation_indexes = []
            target_indexes = []
            before = []
            row["cue_indexes"] = []
            row["reason_code"] = "MENTION_OWNER_RESOLUTION_FAILED"

    changed = False
    for index in mutation_indexes:
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
    required_text = str(entry.get("required_text") or "")
    satisfied = bool(
        mutation_indexes
        and (
            not required_text
            or any(
                required_text in texts[index]
                for index in mutation_indexes
            )
        )
    )
    replacement_indexes = sorted(
        {
            int(replacement["cue_index"]) - 1
            for replacement in row.get("replacements") or []
        }
    )
    required_indexes = [
        index
        for index in mutation_indexes
        if required_text and required_text in texts[index]
    ]
    owned_indexes = (
        mention_owned_indexes
        if mention_owned_indexes
        else replacement_indexes
        if replacement_indexes
        else required_indexes
        if len(required_indexes) == 1
        else []
    )
    if (
        not replacement_indexes
        and not mention_owned_indexes
        and len(required_indexes) > 1
        and entry.get("required") is not False
    ):
        satisfied = False
        row["reason_code"] = "REPLACE_SUBSTRING_OWNER_AMBIGUOUS"
    if owned_indexes:
        target_indexes = owned_indexes
        row["cue_indexes"] = [index + 1 for index in target_indexes]
        before = [
            before_by_index.get(index, texts[index])
            for index in target_indexes
        ]
    return changed, satisfied, target_indexes, before


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
        assertion_state = _assertion_state(raw_entry)
        windows = _entry_local_windows(
            raw_entry,
            pieces=pieces,
            durations=durations,
            source_aliases=source_aliases,
        )
        if not windows:
            continue
        if _skip_inactive_assertion(
            audit=audit,
            entry=raw_entry,
            truth_id=truth_id,
            assertion_state=assertion_state,
        ):
            continue
        target_indexes = _target_indexes(cues, windows, grazing_exempt=True)
        action = str(raw_entry.get("action") or "")
        source_start_ms = int(raw_entry["source_start_ms"])
        source_end_ms = int(raw_entry["source_end_ms"])
        # local_windows bind truth authority to delivery time; cue indexes may
        # drift after layout and are kept only as operator evidence.
        row = _source_truth_audit_row(
            entry=raw_entry,
            truth_id=truth_id,
            assertion_state=assertion_state,
            action=action,
            target_indexes=target_indexes,
            windows=windows,
        )
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
        before_by_index = {index: texts[index] for index in target_indexes}
        before = [before_by_index[index] for index in target_indexes]
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
            changed, satisfied, target_indexes, before = (
                _apply_replace_cue_action(
                    entry=raw_entry,
                    cues=cues,
                    texts=texts,
                    target_indexes=target_indexes,
                    before_by_index=before_by_index,
                    before=before,
                    row=row,
                    spoken_start_raw=spoken_start_raw,
                    spoken_start_local=spoken_start_local,
                    windows=windows,
                )
            )
        elif action == "replace_substring":
            changed, satisfied, target_indexes, before = (
                _apply_replace_substring_action(
                    entry=raw_entry,
                    pieces=pieces,
                    durations=durations,
                    source_aliases=source_aliases,
                    cues=cues,
                    texts=texts,
                    target_indexes=target_indexes,
                    before_by_index=before_by_index,
                    before=before,
                    row=row,
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

        owner_indexes = [
            int(index) - 1 for index in row.get("cue_indexes") or []
        ]
        projection_cues = [
            {
                "cue_index": index + 1,
                "start_ms": cues[index].start_ms,
                "end_ms": cues[index].end_ms,
                "before_text": before_by_index.get(index, ""),
                "after_text": texts[index],
            }
            for index in owner_indexes
            if 0 <= index < len(cues)
        ]
        projection_status = (
            "ALREADY_ABSENT"
            if action == "drop_cue" and not projection_cues
            else "RESOLVED"
        )
        row["resolved_target_projection"] = {
            "schema_version": (
                "source-truth-resolved-target-projection.v1"
            ),
            "selector": (
                "half-open-overlap-gte-min-then-action-resolution"
            ),
            "min_overlap_ms": MIN_CUE_OVERLAP_MS,
            "action": action,
            "status": projection_status,
            "cues": projection_cues,
        }
        row["before"] = before
        row["after"] = [texts[index] for index in target_indexes]
        failures = _source_truth_postcondition_failures(
            raw_entry, pieces, durations, source_aliases, cues, texts,
            target_indexes, row,
        )
        if failures:
            audit["failures"].extend(failures)
            continue
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
    """本候选交付时间轴上所有 required 钉子的辖区窗口（只算不改）。

    2026-07-20 七星 r6 案：实体声学仲裁抢在钉子落刀前对「零三」烧完整条
    key ladder 再 fail-closed——ledger 已拥有的 span 不该进任何后置仲裁。
    ``required:false`` 只作 best-effort，不能取得 defer/owner 权限。加载失败返回
    空（豁免消失=门更严，安全方向）。"""

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
                or _assertion_state(raw_entry) != "VERIFIED_ACTIVE"
                or raw_entry.get("required") is False
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


def candidate_boundary_owner_scope(
    *,
    spec: Mapping[str, object],
    durations: Sequence[int],
) -> dict[str, object]:
    """Return the immutable candidate-local story scope for boundary owners.

    Source truth remains applicable to every retained padded-context interval.
    Boundary ownership is narrower: widening only the final witness reserve
    must not turn a later candidate's truth into part of this candidate's
    story.  The scope deliberately excludes piece ``end_ms`` and the final
    piece duration, because those are the values a bounded context retry is
    allowed to widen.
    """

    pieces = [
        piece
        for piece in (spec.get("pieces") or [])
        if isinstance(piece, Mapping)
    ]
    if not pieces or len(pieces) != len(durations):
        raise RuntimeError("SOURCE_TRUTH_BOUNDARY_OWNER_SCOPE_MAPPING_INVALID")
    if any(
        isinstance(duration, bool)
        or not isinstance(duration, int)
        or duration <= 0
        for duration in durations
    ):
        raise RuntimeError("SOURCE_TRUTH_BOUNDARY_OWNER_SCOPE_DURATION_INVALID")

    first_piece_start_ms = pieces[0].get("start_ms")
    last_piece_start_ms = pieces[-1].get("start_ms")
    semantic_start_ms = spec.get("semantic_start_ms", first_piece_start_ms)
    semantic_end_ms = spec.get("semantic_end_ms")
    given_end_ms = spec.get("given_end_ms")
    for value in (
        first_piece_start_ms,
        last_piece_start_ms,
        semantic_start_ms,
        semantic_end_ms,
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError(
                "SOURCE_TRUTH_BOUNDARY_OWNER_SCOPE_TIMESTAMP_INVALID"
            )
    if given_end_ms is not None and (
        isinstance(given_end_ms, bool) or not isinstance(given_end_ms, int)
    ):
        raise RuntimeError(
            "SOURCE_TRUTH_BOUNDARY_OWNER_SCOPE_TIMESTAMP_INVALID"
        )

    prior_piece_duration_ms = sum(int(value) for value in durations[:-1])
    semantic_story_start_ms = (
        int(semantic_start_ms) - int(first_piece_start_ms)
    )
    story_start_ms = max(
        0,
        semantic_story_start_ms - BOUNDARY_OWNER_LEAD_TOLERANCE_MS,
    )
    story_source_end_ms = max(
        int(semantic_end_ms),
        int(given_end_ms)
        if given_end_ms is not None
        else int(semantic_end_ms),
    )
    story_end_ms = (
        prior_piece_duration_ms
        + story_source_end_ms
        - int(last_piece_start_ms)
    )
    padded_duration_ms = sum(int(value) for value in durations)
    if not 0 <= story_start_ms < story_end_ms <= padded_duration_ms:
        raise RuntimeError("SOURCE_TRUTH_BOUNDARY_OWNER_SCOPE_INVALID")

    core: dict[str, object] = {
        "schema_version": "candidate-boundary-owner-scope.v1",
        "candidate_id": str(spec.get("candidate_id") or ""),
        "story_start_ms": story_start_ms,
        "story_end_ms": story_end_ms,
        "semantic_story_start_ms": semantic_story_start_ms,
        "lead_tolerance_ms": BOUNDARY_OWNER_LEAD_TOLERANCE_MS,
        "semantic_source_start_ms": int(semantic_start_ms),
        "semantic_source_end_ms": int(semantic_end_ms),
        "given_source_end_ms": (
            int(given_end_ms) if given_end_ms is not None else None
        ),
        "first_piece_source_start_ms": int(first_piece_start_ms),
        "last_piece_source_start_ms": int(last_piece_start_ms),
        "prior_piece_duration_ms": prior_piece_duration_ms,
    }
    return {
        **core,
        "scope_sha256": _canonical_sha256(core),
    }


def ledger_required_owner_contracts(
    *,
    spec: Mapping[str, object],
    durations: Sequence[int],
    ledger_path: Path | None,
) -> list[dict[str, object]]:
    """Freeze every required reviewed truth covered by the padded candidate.

    Unlike :func:`ledger_local_windows`, this is a release/boundary authority
    and therefore never degrades to an empty set on malformed input.  A truth
    that intersects the padded source must be fully covered; otherwise the
    candidate cannot silently turn it into ``NOT_REQUIRED`` by trimming.
    """

    if ledger_path is None:
        return []
    if not ledger_path.is_file() or ledger_path.is_symlink():
        raise RuntimeError("SOURCE_SUBTITLE_TRUTH_LEDGER_INVALID")
    document = json.loads(ledger_path.read_bytes().decode("utf-8"))
    if (
        not isinstance(document, Mapping)
        or document.get("schema_version") != SCHEMA_VERSION
        or not isinstance(document.get("entries"), list)
    ):
        raise RuntimeError("SOURCE_SUBTITLE_TRUTH_LEDGER_SCHEMA_INVALID")
    pieces = [
        piece
        for piece in (spec.get("pieces") or [])
        if isinstance(piece, Mapping)
    ]
    if len(pieces) != len(durations):
        raise RuntimeError("SOURCE_SUBTITLE_TRUTH_PIECE_MAPPING_INVALID")
    source_aliases = _load_source_aliases(document)
    owner_scope = candidate_boundary_owner_scope(
        spec=spec,
        durations=durations,
    )
    story_start_ms = int(owner_scope["story_start_ms"])
    story_end_ms = int(owner_scope["story_end_ms"])
    contracts: list[dict[str, object]] = []
    seen: set[str] = set()
    for entry in document["entries"]:
        if (
            not isinstance(entry, Mapping)
            or entry.get("knowledge_type") != "SOURCE_INTERVAL_TRUTH"
            or _assertion_state(entry) != "VERIFIED_ACTIVE"
            or entry.get("required") is False
        ):
            continue
        truth_id = str(entry.get("truth_id") or "")
        if not truth_id:
            raise RuntimeError("SOURCE_SUBTITLE_TRUTH_ID_MISSING")
        windows = _entry_local_windows(
            entry,
            pieces=pieces,
            durations=durations,
            source_aliases=source_aliases,
        )
        if not windows:
            continue
        source_start = int(entry["source_start_ms"])
        source_end = int(entry["source_end_ms"])
        if (
            source_end <= source_start
            or _source_coverage_ms(windows) != source_end - source_start
        ):
            raise RuntimeError(
                f"SOURCE_TRUTH_REQUIRED_OWNER_PARTIAL: {truth_id}"
            )
        boundary_role = _boundary_role(entry)
        if boundary_role == "next_topic_witness":
            if any(
                min(int(window["end_ms"]), story_end_ms)
                - max(int(window["start_ms"]), story_start_ms)
                > 0
                for window in windows
            ):
                raise RuntimeError(
                    "SOURCE_TRUTH_NEXT_TOPIC_WITNESS_OVERLAPS_STORY:"
                    f"{truth_id}"
                )
            # It remains required subtitle truth in the padded source context
            # and may prove topic separation, but it must not move the story
            # endpoint forward into the next topic.
            continue
        fully_inside_story = all(
            story_start_ms <= int(window["start_ms"])
            and int(window["end_ms"]) <= story_end_ms
            for window in windows
        )
        overlaps_story = any(
            min(int(window["end_ms"]), story_end_ms)
            - max(int(window["start_ms"]), story_start_ms)
            > 0
            for window in windows
        )
        if not fully_inside_story:
            if overlaps_story:
                raise RuntimeError(
                    "SOURCE_TRUTH_BOUNDARY_OWNER_SCOPE_STRADDLE:"
                    f"{truth_id}"
                )
            # Lead/post context is still corrected and verified by
            # apply_source_subtitle_truth; it simply cannot move this
            # candidate's delivery boundary.
            continue
        if truth_id in seen:
            raise RuntimeError(
                f"SOURCE_TRUTH_REQUIRED_OWNER_AMBIGUOUS: {truth_id}"
            )
        seen.add(truth_id)
        contracts.append(
            {
                "owner_kind": "source_subtitle_truth",
                "owner_id": truth_id,
                "required": True,
                "source_start_ms": source_start,
                "source_end_ms": source_end,
                "owner_scope_sha256": owner_scope["scope_sha256"],
                "local_windows": [
                    {
                        "start_ms": int(window["start_ms"]),
                        "end_ms": int(window["end_ms"]),
                    }
                    for window in windows
                ],
            }
        )
    return contracts
