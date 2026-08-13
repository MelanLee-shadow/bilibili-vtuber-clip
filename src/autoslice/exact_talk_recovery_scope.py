"""Fail-closed song-work isolation for exact talk recovery runs."""

from __future__ import annotations

from collections.abc import Collection

from src.autoslice.candidate_selection import _exact_talk_contract_ids
from src.autoslice.runner_proxy import RunnerProxy


_runner = RunnerProxy()


def suppress_exact_talk_recovery_song_work(
    state: dict, *, phase: str
) -> bool:
    """Clear active song queues that an exact talk contract cannot authorize."""

    if not _exact_talk_contract_ids(state):
        return False
    cleared: dict[str, dict[str, object]] = {}
    for key in ("pending_song", "song_backlog", "song_selection_backlog"):
        rows = state.get(key)
        if not isinstance(rows, list) or not rows:
            continue
        candidate_ids: list[str] = []
        legacy_row_count = 0
        for row in rows:
            if not isinstance(row, dict):
                legacy_row_count += 1
                continue
            candidate_id = str(
                row.get("candidate_id")
                or row.get("cid")
                or row.get("song_candidate_id")
                or ""
            ).strip()
            if candidate_id:
                candidate_ids.append(candidate_id)
        cleared[key] = {
            "row_count": len(rows),
            "candidate_ids": sorted(set(candidate_ids)),
            "legacy_row_count": legacy_row_count,
        }
        state[key] = []
    if not cleared:
        return False
    state.setdefault(
        "exact_talk_recovery_song_scope_suppressions", []
    ).append(
        {
            "schema_version": "exact-talk-recovery-song-scope-suppression.v1",
            "reason": "EXACT_TALK_RECOVERY_FORBIDS_SONG_WORK",
            "phase": phase,
            "cleared": cleared,
        }
    )
    return True


def maintain_delivery_recovery_scope(
    date: str,
    state: dict,
    *,
    automatic_maintenance: bool,
    talk_candidate_ids: Collection[str] | None = None,
) -> tuple[int, int, int, int, bool]:
    """Run talk/song maintenance without crossing an exact talk-only scope."""

    exact_talk_recovery = bool(_exact_talk_contract_ids(state))
    suppress_exact_talk_recovery_song_work(
        state, phase="before_delivery_recovery"
    )
    song_baseline = state.get("song_pipeline_fingerprint_baseline")
    if not automatic_maintenance:
        return 0, 0, 0, 0, False
    talk_only_recovery = talk_candidate_ids is not None
    if exact_talk_recovery or talk_only_recovery:
        recovered_songs = 0
        stale_talks = (
            _runner.requeue_stale_current_recovery_talks(date, state)
            if exact_talk_recovery
            else 0
        )
        failed_talks = (
            _runner.requeue_recoverable_talks(
                date,
                state,
                candidate_ids=set(talk_candidate_ids or ()),
            )
            if talk_only_recovery
            else _runner.requeue_recoverable_talks(date, state)
        )
        blocked_songs = 0
    else:
        recovered_songs = _runner.recover_bound_song_deliveries(date, state)
        (
            stale_talks,
            failed_talks,
            blocked_songs,
        ) = _runner.requeue_recoverable_deliveries(date, state)
    song_baseline_changed = (
        state.get("song_pipeline_fingerprint_baseline") != song_baseline
    )
    return (
        recovered_songs,
        stale_talks,
        failed_talks,
        blocked_songs,
        song_baseline_changed,
    )
