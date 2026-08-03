"""Fail-closed discovery of a boundary witness in the next recording segment."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from src.autoslice.piece_roles import BOUNDARY_WITNESS_RESERVE_ROLE
from src.autoslice.runner_proxy import RunnerProxy
from src.autoslice.structured_chat_binding import StructuredChatBindingError


_runner = RunnerProxy()
_SEGMENT_WALLCLOCK_RX = re.compile(
    r"_(\d{8})-(\d{2})-(\d{2})-(\d{2})$"
)
CROSS_SEGMENT_CONTINUITY_TOLERANCE_MS = 5_000
BOUNDARY_WITNESS_RESERVE_MARGIN_MS = 2_000


def _segment_wallclock_start(path: Path) -> datetime | None:
    """Parse a recording segment's wall-clock start from its basename."""

    match = _SEGMENT_WALLCLOCK_RX.search(path.stem)
    if match is None:
        return None
    try:
        return datetime.strptime(
            "".join(match.groups()), "%Y%m%d%H%M%S"
        )
    except ValueError:
        return None


def discover_cross_segment_witness_reserve(
    *,
    date: str,
    item: dict,
    deficit_ms: int,
) -> dict[str, object] | None:
    """Build a next-segment reserve piece, or ``None`` if proof is missing.

    Every failure mode (no next segment, unparseable timestamps, a wall-clock
    gap beyond tolerance) returns ``None`` so the caller retains the existing
    fail-closed boundary result.
    """

    current_path = Path(str(item.get("segment_path") or ""))
    if not current_path.name:
        return None
    seg_dur_ms = item.get("seg_dur_ms")
    if (
        isinstance(seg_dur_ms, bool)
        or not isinstance(seg_dur_ms, int)
        or seg_dur_ms <= 0
    ):
        return None
    try:
        segments = _runner.list_segments(date)
    except Exception:  # noqa: BLE001 — discovery is best-effort
        return None
    try:
        current_index = next(
            index
            for index, segment in enumerate(segments)
            if segment.name == current_path.name
        )
    except StopIteration:
        return None
    if current_index + 1 >= len(segments):
        return None
    next_segment = segments[current_index + 1]

    current_wallclock = _segment_wallclock_start(current_path)
    next_wallclock = _segment_wallclock_start(next_segment)
    if current_wallclock is None or next_wallclock is None:
        return None
    expected_next_wallclock = current_wallclock + timedelta(
        milliseconds=seg_dur_ms
    )
    continuity_delta_ms = int(
        (next_wallclock - expected_next_wallclock).total_seconds() * 1000
    )
    if abs(continuity_delta_ms) > CROSS_SEGMENT_CONTINUITY_TOLERANCE_MS:
        return None

    reserve_piece: dict[str, object] = {
        "remote_media": str(next_segment),
        "start_ms": 0,
        "end_ms": deficit_ms + BOUNDARY_WITNESS_RESERVE_MARGIN_MS,
        "piece_role": BOUNDARY_WITNESS_RESERVE_ROLE,
    }
    try:
        xml_path = _runner.find_danmaku_xml(next_segment)
    except Exception:  # noqa: BLE001 — optional enrichment
        xml_path = None
    if xml_path:
        reserve_piece["danmaku_xml_local"] = str(xml_path)

    chat_binding_absent = True
    try:
        chat_binding = _runner.resolve_structured_chat_binding(
            next_segment
        )
    except StructuredChatBindingError:
        chat_binding = None
    except Exception:  # noqa: BLE001 — reserve context, never delivery truth
        chat_binding = None
    if isinstance(chat_binding, dict) and chat_binding.get("chat_jsonl"):
        reserve_piece["chat_jsonl_local"] = str(
            chat_binding["chat_jsonl"]
        )
        for field in (
            "chat_jsonl_sha256",
            "chat_origin_epoch_ms",
            "chat_timeline_offset_ms",
            "structured_chat_required",
            "chat_source_alias_id",
            "chat_canonical_recording_basename",
            "chat_binding_status",
            "chat_binding_authority",
        ):
            if field in chat_binding:
                reserve_piece[field] = chat_binding[field]
        chat_binding_absent = False

    return {
        "piece": reserve_piece,
        "next_segment_name": next_segment.name,
        "gap_ms": deficit_ms,
        "continuity_delta_ms": continuity_delta_ms,
        "chat_binding_absent": chat_binding_absent,
    }
