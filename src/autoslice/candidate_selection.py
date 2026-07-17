"""Session sealing, candidate quarantine, delivery budgets, and prioritization.

These are deterministic state policies. Runner constants and patchable helpers
are resolved lazily so the existing runner-level test and repair API remains
stable in module and cron script execution modes.
"""

from __future__ import annotations

from pathlib import Path

from src.autoslice.runner_proxy import RunnerProxy


_runner = RunnerProxy()


_LEGACY_SESSION_ID = "legacy-date-session"


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
    # Quarantining only the recall anchor still lets a talk sibling escape with
    # the song's intro or tail.  Cover the same conservative source range that
    # the authoritative full-proof retry is allowed to inspect.
    start_ms = max(0, anchor_start_ms - _runner.SONG_PROOF_RETRY_PRE_MS)
    end_ms = anchor_end_ms + _runner.SONG_PROOF_RETRY_POST_MS
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
    if any(
        isinstance(existing, dict)
        and (
            Path(str(existing.get("segment_path") or "")).name,
            existing.get("original_anchor_start_ms", existing.get("start_ms")),
            existing.get("original_anchor_end_ms", existing.get("end_ms")),
        )
        == identity
        for existing in intervals
    ):
        return
    intervals.append(interval)


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

    for source in (state.get("pending_song", []), state.get("song_backlog", [])):
        for item in (source if isinstance(source, list) else []):
            if isinstance(item, dict):
                _runner._remember_song_quarantine_interval(state, item)

    intervals = [item for item in state.get("song_quarantine_intervals", []) if isinstance(item, dict)]
    kept: list[dict] = []
    blocked = state.setdefault("song_overlap_blocked_talk", [])
    for talk in state.get("pending_talk", []):
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
        }
        if tombstone not in blocked:
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
    for item in deferred:
        _runner._note_not_selected(
            state,
            f"{Path(item['segment_path']).name} {item['start_ms'] // 1000}-{item['end_ms'] // 1000}s "
            f"conf={item.get('confidence')} hook={item.get('hook', '')[:40]} "
            f"(候补:全场按信心分全局排序取{_runner.MAX_TALK_PICKS}席,同段软上限{_runner.TALK_PER_SEGMENT_CAP})",
        )
    _runner.refill_songs(state)
