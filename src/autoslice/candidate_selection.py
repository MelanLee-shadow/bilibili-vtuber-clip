"""Session sealing, candidate quarantine, delivery budgets, and prioritization.

These are deterministic state policies. Runner constants and patchable helpers
are resolved lazily so the existing runner-level test and repair API remains
stable in module and cron script execution modes.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from src.autoslice.runner_proxy import RunnerProxy


_runner = RunnerProxy()


_LEGACY_SESSION_ID = "legacy-date-session"
_SESSION_ID_RX = re.compile(r"^live-(\d{8})T(\d{6})(?:[+-]\d{4})?$")
_SEGMENT_TIME_RX = re.compile(r"_(\d{8})-(\d{2})-(\d{2})-(\d{2})$")


def _item_session_id(item: dict) -> str:
    return str(item.get("session_id") or _LEGACY_SESSION_ID)


def session_sealed(date: str, state: dict) -> bool:
    """The date's recordings are STABLE: same segment inventory (names+sizes)
    as the previous tick, with at least one segment.  Selecting before seal
    hands the early segments the whole quota (2026-07-09 audit: a conf=0.94
    late-arriving candidate lost to five earlier 0.85-0.90 ones).  Costs one
    extra tick (~10 min) of latency after stream end; also absorbs the
    recorder's final flush.  Mutates state['seg_snapshot'] for the next tick."""
    snapshot: dict[str, int] = {}
    for segment in _runner.list_segments(date):
        try:
            snapshot[segment.stem] = segment.stat().st_size
        except OSError:
            return False  # flaky source read — never seal on a lie
    prev = state.get("seg_snapshot")
    state["seg_snapshot"] = snapshot
    return bool(snapshot) and prev == snapshot


def song_delivery_budget(state: dict, session_id: str | None = None) -> int:
    """Remaining song DELIVERY slots, including verified commit reservations.

    An ordinary gate-BLOCKED attempt must not eat a slot (2026-07-09 audit:
    two BLOCKs consumed both slots and the date still read ``done``).  Once a
    song has passed the positive full-song/host proof and only its atomic
    packaging failed, however, that exact hash-bound attempt owns a slot until
    deterministic recovery either commits it or an operator revokes it.  This
    prevents two later songs from filling the quota and a delayed recovery
    silently exposing a third delivery.
    """

    consumed = sum(
        1
        for song in state.get("songs", [])
        if isinstance(song, dict)
        and (session_id is None or _item_session_id(song) == session_id)
        and (
            bool(song.get("delivered"))
            or song.get("verified_delivery_pending_commit") is True
        )
    )
    return max(0, _runner.MAX_SONGS_PER_SESSION - consumed)


def _talk_slots_for_session(state: dict, session_id: str) -> int:
    records = [
        item
        for item in state.get("picks", [])
        if isinstance(item, dict) and _item_session_id(item) == session_id
    ]
    produced = sum(
        1 for item in records if item.get("status") in _runner.DELIVERED_TALK_STATUSES
    )
    reserved_for_revival = sum(
        1
        for item in records
        if item.get("status") == "failed"
        and item.get("failure_recoverable") is True
        and int(item.get("talk_transient_retry_count") or 0)
        + int(item.get("talk_repair_retry_count") or 0)
        < _runner.TALK_REPAIR_LIFETIME_RETRY_CAP
    )
    attempts_left = max(0, _runner.TALK_ATTEMPT_CAP - len(records))
    return min(
        max(0, _runner.MAX_TALK_PICKS - produced - reserved_for_revival),
        attempts_left,
    )


def _assign_cover_diversity_slots(state: dict) -> None:
    """Allocate a stable, collision-free cover family within each live session.

    Candidate-id hashing gives good long-run variety but can still draw the
    same two backgrounds four times in one review batch.  Selection is the one
    place that sees the full batch, so it assigns slots 0..N here.  Delivered
    siblings reserve their old slots; rejected candidates do not, allowing a
    backfill candidate to reuse the missing visual family.
    """

    pending = [
        item for item in state.get("pending_talk", []) if isinstance(item, dict)
    ]
    sessions = list(dict.fromkeys(_item_session_id(item) for item in pending))
    for session_id in sessions:
        used = {
            int(record["cover_diversity_slot"])
            for record in state.get("picks", [])
            if isinstance(record, dict)
            and _item_session_id(record) == session_id
            and record.get("status") in _runner.DELIVERED_TALK_STATUSES
            and isinstance(record.get("cover_diversity_slot"), int)
            and not isinstance(record.get("cover_diversity_slot"), bool)
            and int(record["cover_diversity_slot"]) >= 0
        }
        session_pending = [
            item for item in pending if _item_session_id(item) == session_id
        ]
        for item in session_pending:
            existing = item.get("cover_diversity_slot")
            if (
                isinstance(existing, int)
                and not isinstance(existing, bool)
                and existing >= 0
                and existing not in used
            ):
                slot = existing
            else:
                slot = next(value for value in range(len(used) + len(session_pending) + 1) if value not in used)
            item["cover_diversity_slot"] = slot
            used.add(slot)


def backlog_has_eligible_session_work(state: dict) -> bool:
    """Whether a backlog contains work for a session with quota remaining."""

    if any(
        _talk_slots_for_session(state, _item_session_id(item)) > 0
        for item in state.get("talk_backlog", [])
        if isinstance(item, dict)
    ):
        return True
    song_sessions = {
        _item_session_id(item)
        for item in state.get("song_backlog", [])
        if isinstance(item, dict)
    }
    for session_id in song_sessions:
        generation_attempts = sum(
            1
            for item in state.get("songs", [])
            if isinstance(item, dict) and _item_session_id(item) == session_id
        )
        lifetime_attempts = generation_attempts + sum(
            1
            for item in state.get("song_superseded_attempts", [])
            if isinstance(item, dict) and _item_session_id(item) == session_id
        )
        if (
            _runner.song_delivery_budget(state, session_id) > 0
            and generation_attempts < _runner.SONG_ATTEMPT_CAP
            and lifetime_attempts < _runner.SONG_LIFETIME_ATTEMPT_CAP
        ):
            return True
    return False


def _remember_song_quarantine_interval(state: dict, item: dict) -> None:
    """Persist source intervals that may contain a song before any rendering.

    Candidate-local BLOCK was insufficient: an overlapping semantic talk
    candidate could otherwise be produced first and launder background music,
    a guest song, or an unverified performance through the talk lane.  The
    interval taint survives song backlog moves, retries, and later state ticks.
    """

    segment = str(item.get("segment_path") or item.get("segment") or "").strip()
    anchor_start_ms = item.get("anchor_start_ms")
    anchor_end_ms = item.get("anchor_end_ms")
    if (
        not segment
        or isinstance(anchor_start_ms, bool)
        or not isinstance(anchor_start_ms, int)
        or isinstance(anchor_end_ms, bool)
        or not isinstance(anchor_end_ms, int)
        or not 0 <= anchor_start_ms < anchor_end_ms
    ):
        return
    # A proof retry window is evidence-search context, not music occupancy.
    # Treating the 2m-pre/6m-post search envelope as a song interval blocked
    # unrelated talks hundreds of seconds away on 2026-07-18.  The recall
    # anchor is the only content-local evidence available before song repair;
    # retain a small boundary guard without laundering the search window into
    # a content boundary.
    guard_ms = _runner.SONG_TALK_QUARANTINE_GUARD_MS
    start_ms = max(0, anchor_start_ms - guard_ms)
    end_ms = anchor_end_ms + guard_ms
    segment_duration_ms = item.get("seg_dur_ms")
    if (
        isinstance(segment_duration_ms, int)
        and not isinstance(segment_duration_ms, bool)
        and segment_duration_ms > 0
    ):
        end_ms = min(segment_duration_ms, end_ms)
    interval = {
        "segment_path": segment,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "original_anchor_start_ms": anchor_start_ms,
        "original_anchor_end_ms": anchor_end_ms,
        "candidate_id": str(item.get("cid") or item.get("candidate_id") or ""),
        "reason_code": "SONG_INTERVAL_REQUIRES_JOINT_SINGING_PROOF",
    }
    intervals = state.setdefault("song_quarantine_intervals", [])
    identity = (Path(segment).name, anchor_start_ms, anchor_end_ms)
    for index, existing in enumerate(intervals):
        if not isinstance(existing, dict):
            continue
        existing_identity = (
            Path(str(existing.get("segment_path") or "")).name,
            existing.get("original_anchor_start_ms", existing.get("start_ms")),
            existing.get("original_anchor_end_ms", existing.get("end_ms")),
        )
        if existing_identity == identity:
            # Canonicalize old persisted states too.  Returning early here used
            # to make an obsolete broad interval survive every deployment.
            intervals[index] = interval
            return
    intervals.append(interval)


def _canonicalize_persisted_song_quarantine_intervals(state: dict) -> None:
    """Migrate proof-window-era intervals without needing a queued song item."""

    intervals = state.get("song_quarantine_intervals")
    if not isinstance(intervals, list):
        return
    segment_durations = state.get("segment_durations_ms")
    for index, existing in enumerate(list(intervals)):
        if not isinstance(existing, dict):
            continue
        anchor_start_ms = existing.get(
            "original_anchor_start_ms", existing.get("start_ms")
        )
        anchor_end_ms = existing.get(
            "original_anchor_end_ms", existing.get("end_ms")
        )
        if (
            not isinstance(anchor_start_ms, int)
            or isinstance(anchor_start_ms, bool)
            or not isinstance(anchor_end_ms, int)
            or isinstance(anchor_end_ms, bool)
            or not 0 <= anchor_start_ms < anchor_end_ms
        ):
            continue
        end_ms = anchor_end_ms + _runner.SONG_TALK_QUARANTINE_GUARD_MS
        stem = Path(str(existing.get("segment_path") or "")).stem
        if isinstance(segment_durations, dict):
            duration_ms = segment_durations.get(stem)
            if (
                isinstance(duration_ms, int)
                and not isinstance(duration_ms, bool)
                and duration_ms > 0
            ):
                end_ms = min(end_ms, duration_ms)
        intervals[index] = {
            **existing,
            "start_ms": max(
                0,
                anchor_start_ms - _runner.SONG_TALK_QUARANTINE_GUARD_MS,
            ),
            "end_ms": end_ms,
            "original_anchor_start_ms": anchor_start_ms,
            "original_anchor_end_ms": anchor_end_ms,
            "reason_code": "SONG_INTERVAL_REQUIRES_JOINT_SINGING_PROOF",
        }


def _session_relative_ms(item: dict, local_ms: int) -> int | None:
    session_match = _SESSION_ID_RX.match(_item_session_id(item))
    segment_value = str(item.get("segment_path") or item.get("segment") or "")
    segment_match = _SEGMENT_TIME_RX.search(Path(segment_value).stem)
    if session_match is None or segment_match is None:
        return None
    try:
        session_start = datetime.strptime(
            "".join(session_match.groups()), "%Y%m%d%H%M%S"
        )
        segment_start = datetime.strptime(
            "".join(segment_match.groups()), "%Y%m%d%H%M%S"
        )
    except ValueError:
        return None
    return int((segment_start - session_start).total_seconds() * 1000) + local_ms


def exclude_session_edge_bgm_candidates(state: dict) -> None:
    """Exclude positional opening/ending BGM before song proof or talk taint.

    This is deliberately session-relative rather than segment-relative: a real
    host performance can begin at 00:00 after a recorder rotation.  Missing or
    malformed recorder timestamps fail closed and leave the candidate alone.
    """

    queue_names = ("pending_song", "song_backlog")
    queued = [
        item
        for name in queue_names
        for item in state.get(name, [])
        if isinstance(item, dict)
    ]
    if not queued:
        return

    session_ends: dict[str, int] = {}
    segment_sessions = state.get("segment_sessions")
    segment_durations = state.get("segment_durations_ms")
    if isinstance(segment_sessions, dict) and isinstance(segment_durations, dict):
        for stem, duration_ms in segment_durations.items():
            session_id = segment_sessions.get(stem)
            if (
                not isinstance(session_id, str)
                or not isinstance(duration_ms, int)
                or isinstance(duration_ms, bool)
                or duration_ms <= 0
            ):
                continue
            relative_end = _session_relative_ms(
                {"session_id": session_id, "segment_path": str(stem)},
                duration_ms,
            )
            if relative_end is not None:
                session_ends[session_id] = max(
                    session_ends.get(session_id, relative_end), relative_end
                )
    for item in queued:
        duration_ms = item.get("seg_dur_ms")
        if (
            not isinstance(duration_ms, int)
            or isinstance(duration_ms, bool)
            or duration_ms <= 0
        ):
            continue
        relative_end = _session_relative_ms(item, duration_ms)
        if relative_end is not None:
            session_id = _item_session_id(item)
            session_ends[session_id] = max(
                session_ends.get(session_id, relative_end), relative_end
            )

    excluded_ids: set[str] = set()
    excluded_rows = state.setdefault("song_edge_bgm_excluded", [])
    known_ids = {
        str(row.get("candidate_id") or "")
        for row in excluded_rows
        if isinstance(row, dict)
    }
    for item in queued:
        candidate_id = str(item.get("cid") or item.get("candidate_id") or "")
        anchor_start_ms = item.get("anchor_start_ms")
        anchor_end_ms = item.get("anchor_end_ms")
        if (
            not candidate_id
            or not isinstance(anchor_start_ms, int)
            or isinstance(anchor_start_ms, bool)
            or not isinstance(anchor_end_ms, int)
            or isinstance(anchor_end_ms, bool)
        ):
            continue
        relative_start = _session_relative_ms(item, anchor_start_ms)
        relative_end = _session_relative_ms(item, anchor_end_ms)
        session_id = _item_session_id(item)
        session_end = session_ends.get(session_id)
        reason_code = None
        if (
            relative_start is not None
            and relative_start <= _runner.SESSION_INTRO_BGM_MAX_OFFSET_MS
        ):
            reason_code = "SESSION_INTRO_BGM_BY_POSITION"
        elif (
            relative_end is not None
            and session_end is not None
            and 0 <= session_end - relative_end
            <= _runner.SESSION_OUTRO_BGM_MAX_REMAINING_MS
        ):
            reason_code = "SESSION_OUTRO_BGM_BY_POSITION"
        if reason_code is None:
            continue

        excluded_ids.add(candidate_id)
        if candidate_id not in known_ids:
            excluded_rows.append(
                {
                    "candidate_id": candidate_id,
                    "session_id": session_id,
                    "segment_path": str(
                        item.get("segment_path") or item.get("segment") or ""
                    ),
                    "anchor_start_ms": anchor_start_ms,
                    "anchor_end_ms": anchor_end_ms,
                    "session_relative_anchor_start_ms": relative_start,
                    "session_relative_anchor_end_ms": relative_end,
                    "session_end_ms": session_end,
                    "reason_code": reason_code,
                    "decision": "EXCLUDE_POSITIONAL_BGM",
                }
            )
            known_ids.add(candidate_id)
        _note_not_selected(
            state,
            f"{Path(str(item.get('segment_path') or item.get('segment') or '')).name} "
            f"{anchor_start_ms // 1000}-{anchor_end_ms // 1000}s "
            f"(排除:{reason_code},整场首尾背景曲默认不视为主播演唱)",
        )

    if not excluded_ids:
        return
    for name in queue_names:
        state[name] = [
            item
            for item in state.get(name, [])
            if not (
                isinstance(item, dict)
                and str(item.get("cid") or item.get("candidate_id") or "")
                in excluded_ids
            )
        ]
    state["song_quarantine_intervals"] = [
        interval
        for interval in state.get("song_quarantine_intervals", [])
        if not (
            isinstance(interval, dict)
            and str(interval.get("candidate_id") or "") in excluded_ids
        )
    ]


def _note_not_selected(state: dict, entry: str) -> None:
    """Record a not-selected line once; selection reruns every tick and must stay idempotent."""
    notes = state.setdefault("not_selected", [])
    if entry not in notes:
        notes.append(entry)


def quarantine_overlapping_talk_candidates(state: dict) -> None:
    """Remove every talk candidate overlapping a known song-like interval.

    Songs have their own proof-bearing lane.  A talk-shaped sibling, parent,
    child, merge, or retry may never materialize the same audio while the song
    interval is unresolved or blocked.  We deliberately keep the quarantine
    even after a valid song delivery: the same interval must not also escape as
    an ordinary talk artifact that bypasses the song gate.
    """

    _canonicalize_persisted_song_quarantine_intervals(state)
    for source in (state.get("pending_song", []), state.get("song_backlog", [])):
        for item in (source if isinstance(source, list) else []):
            if isinstance(item, dict):
                _runner._remember_song_quarantine_interval(state, item)

    intervals = [item for item in state.get("song_quarantine_intervals", []) if isinstance(item, dict)]
    blocked = state.setdefault("song_overlap_blocked_talk", [])
    reconsidered = list(state.get("pending_talk", []))
    pending_ids = {
        str(item.get("cid") or item.get("candidate_id") or "")
        for item in reconsidered
        if isinstance(item, dict)
    }
    for tombstone in blocked:
        candidate = tombstone.get("candidate") if isinstance(tombstone, dict) else None
        candidate_id = (
            str(candidate.get("cid") or candidate.get("candidate_id") or "")
            if isinstance(candidate, dict)
            else ""
        )
        if (
            tombstone.get("status") == "blocked"
            and candidate_id
            and candidate_id not in pending_ids
        ):
            reconsidered.append(candidate)
            pending_ids.add(candidate_id)

    kept: list[dict] = []
    for talk in reconsidered:
        talk_segment = Path(str(talk.get("segment_path") or talk.get("segment") or "")).name
        talk_start = talk.get("start_ms")
        talk_end = talk.get("end_ms")
        overlap = next(
            (
                interval
                for interval in intervals
                if talk_segment
                and talk_segment == Path(str(interval.get("segment_path") or "")).name
                and isinstance(talk_start, int)
                and not isinstance(talk_start, bool)
                and isinstance(talk_end, int)
                and not isinstance(talk_end, bool)
                and isinstance(interval.get("start_ms"), int)
                and not isinstance(interval.get("start_ms"), bool)
                and isinstance(interval.get("end_ms"), int)
                and not isinstance(interval.get("end_ms"), bool)
                and max(talk_start, int(interval["start_ms"])) < min(talk_end, int(interval["end_ms"]))
            ),
            None,
        )
        if overlap is None:
            kept.append(talk)
            talk_id = str(talk.get("cid") or talk.get("candidate_id") or "")
            for tombstone in blocked:
                if (
                    isinstance(tombstone, dict)
                    and tombstone.get("status") == "blocked"
                    and str(tombstone.get("candidate_id") or "") == talk_id
                ):
                    tombstone["status"] = "released"
                    tombstone["release_reason_code"] = (
                        "SONG_QUARANTINE_INTERVAL_REEVALUATED"
                    )
            continue
        tombstone = {
            "candidate_id": str(talk.get("cid") or talk.get("candidate_id") or ""),
            "segment_path": str(talk.get("segment_path") or talk.get("segment") or ""),
            "start_ms": talk_start,
            "end_ms": talk_end,
            "status": "blocked",
            "reason_code": "TALK_OVERLAPS_UNVERIFIED_SONG_INTERVAL",
            "song_candidate_id": str(overlap.get("candidate_id") or ""),
            "song_start_ms": overlap.get("start_ms"),
            "song_end_ms": overlap.get("end_ms"),
            "candidate": dict(talk),
        }
        identity = (
            tombstone["candidate_id"],
            Path(tombstone["segment_path"]).name,
            tombstone["start_ms"],
            tombstone["end_ms"],
        )
        if not any(
            isinstance(existing, dict)
            and (
                str(existing.get("candidate_id") or ""),
                Path(str(existing.get("segment_path") or "")).name,
                existing.get("start_ms"),
                existing.get("end_ms"),
            )
            == identity
            and existing.get("status") == "blocked"
            for existing in blocked
        ):
            blocked.append(tombstone)
        _runner._note_not_selected(
            state,
            f"{talk_segment} {int(talk_start or 0) // 1000}-{int(talk_end or 0) // 1000}s "
            "(门拦:与未验证/已阻断歌切区间重叠,不得走 talk 旁路)",
        )
    state["pending_talk"] = kept




def refill_songs(state: dict) -> None:
    """Top up pending_song from the structured backlog, danmaku-desc, honoring
    both the delivery budget and the hard per-session attempt cap. Legacy string
    backlog entries (pre-v4 states) stay for the report but cannot backfill."""
    backlog = state.setdefault("song_backlog", [])
    pending = state.get("pending_song", [])
    structured = [item for item in pending if isinstance(item, dict)] + [
        item for item in backlog if isinstance(item, dict)
    ]
    selected_repairs = [item for item in structured if item.get("selected_repair")]
    pool = [item for item in structured if not item.get("selected_repair")]
    legacy = [b for b in backlog if not isinstance(b, dict)]
    sessions = list(dict.fromkeys(_item_session_id(item) for item in structured))
    selected: list[dict] = []
    deferred: list[dict] = []
    for session_id in sessions:
        delivery_slots = _runner.song_delivery_budget(state, session_id)
        session_repairs = [
            item for item in selected_repairs if _item_session_id(item) == session_id
        ]
        selected.extend(session_repairs[:delivery_slots])
        deferred.extend(session_repairs[delivery_slots:])
        ordinary_slots = max(0, delivery_slots - min(len(session_repairs), delivery_slots))
        session_pool = [item for item in pool if _item_session_id(item) == session_id]
        session_pool.sort(
            key=lambda x: (
                -(x.get("danmaku") or 0),
                -(x["anchor_end_ms"] - x["anchor_start_ms"]),
            )
        )
        attempts = sum(
            1
            for item in state.get("songs", [])
            if isinstance(item, dict) and _item_session_id(item) == session_id
        )
        lifetime_attempts = attempts + sum(
            1
            for item in state.get("song_superseded_attempts", [])
            if isinstance(item, dict) and _item_session_id(item) == session_id
        )
        allowed = min(
            ordinary_slots,
            max(0, _runner.SONG_ATTEMPT_CAP - attempts),
            max(0, _runner.SONG_LIFETIME_ATTEMPT_CAP - lifetime_attempts),
        )
        selected.extend(session_pool[:allowed])
        deferred.extend(session_pool[allowed:])
    # Infrastructure retries bypass discovery attempt caps, but never run more
    # than the remaining delivery slots concurrently; otherwise several old
    # attempts could all recover at once and over-deliver one live session.
    state["pending_song"] = selected
    state["song_backlog"] = deferred + legacy


def prioritize(state: dict) -> None:
    """Phase B: GLOBAL talk ranking by recall confidence (the metric asset is
    embedded in the recall prompt, so confidence carries its hard tiers), with
    a soft per-segment diversity cap that yields when slots would go unfilled.
    Replaces the segment round-robin that let five early candidates claim the
    whole quota regardless of score.  Songs: top danmaku, budget = deliveries."""
    # Preserve confidence-ranked reserve candidates so a deterministic
    # boundary/speaker rejection can automatically free its slot.  They used
    # to survive only as report strings, making top-5 mean "try exactly five
    # and accept fewer on any content-level refusal".
    prior_backlog = [
        item for item in state.pop("talk_backlog", []) if isinstance(item, dict)
    ]
    state.setdefault("pending_talk", []).extend(prior_backlog)
    _runner.exclude_session_edge_bgm_candidates(state)
    _runner.quarantine_overlapping_talk_candidates(state)
    pending_talk = state.get("pending_talk", [])
    selected_repairs = [item for item in pending_talk if item.get("selected_repair")]
    pending_talk = [item for item in pending_talk if not item.get("selected_repair")]
    below_threshold = [
        item
        for item in pending_talk
        if isinstance(item.get("confidence"), (int, float))
        and not isinstance(item.get("confidence"), bool)
        and float(item["confidence"]) < _runner.MIN_TALK_CONFIDENCE
    ]
    if below_threshold:
        below_ids = {
            str(item.get("cid") or item.get("candidate_id") or "")
            for item in state.setdefault("talk_below_confidence_threshold", [])
            if isinstance(item, dict)
        }
        for item in below_threshold:
            cid = str(item.get("cid") or item.get("candidate_id") or "")
            if cid not in below_ids:
                state["talk_below_confidence_threshold"].append(item)
                below_ids.add(cid)
            _runner._note_not_selected(
                state,
                f"{Path(item['segment_path']).name} {item['start_ms'] // 1000}-{item['end_ms'] // 1000}s "
                f"conf={item.get('confidence')} hook={item.get('hook', '')[:40]} "
                f"(落选:低于最低信心分{_runner.MIN_TALK_CONFIDENCE:.2f},top-{_runner.MAX_TALK_PICKS}是上限不是凑数目标)",
            )
    below_object_ids = {id(item) for item in below_threshold}
    pending_talk = [item for item in pending_talk if id(item) not in below_object_ids]
    keep: list[dict] = []
    deferred: list[dict] = []
    sessions = list(dict.fromkeys(_item_session_id(item) for item in pending_talk))
    for session_id in sessions:
        slots = _talk_slots_for_session(state, session_id)
        ranked = sorted(
            (item for item in pending_talk if _item_session_id(item) == session_id),
            key=lambda x: -(x.get("confidence") or 0.0),
        )
        session_keep: list[dict] = []
        session_deferred: list[dict] = []
        per_seg: dict[str, int] = {}
        for item in ranked:
            seg = item["segment_path"]
            if len(session_keep) < slots and per_seg.get(seg, 0) < _runner.TALK_PER_SEGMENT_CAP:
                session_keep.append(item)
                per_seg[seg] = per_seg.get(seg, 0) + 1
            else:
                session_deferred.append(item)
        # The diversity cap is SOFT inside each live session.
        for item in list(session_deferred):
            if len(session_keep) >= slots:
                break
            session_keep.append(item)
            session_deferred.remove(item)
        keep.extend(session_keep)
        deferred.extend(session_deferred)
    # These candidates already won selection in an earlier generation and
    # failed without delivery.  Do not discard the retry merely because
    # successful siblings now fill the ordinary delivery quota.
    state["pending_talk"] = selected_repairs + keep
    state["talk_backlog"] = deferred
    _assign_cover_diversity_slots(state)
    for item in deferred:
        _runner._note_not_selected(
            state,
            f"{Path(item['segment_path']).name} {item['start_ms'] // 1000}-{item['end_ms'] // 1000}s "
            f"conf={item.get('confidence')} hook={item.get('hook', '')[:40]} "
            f"(候补:全场按信心分全局排序取{_runner.MAX_TALK_PICKS}席,同段软上限{_runner.TALK_PER_SEGMENT_CAP})",
        )
    _runner.refill_songs(state)
