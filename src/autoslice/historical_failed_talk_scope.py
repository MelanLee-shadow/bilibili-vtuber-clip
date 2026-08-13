"""Runner orchestration for historical v2 failed-Talk recovery scopes."""

from __future__ import annotations

from collections.abc import Mapping

from src.autoslice.exact_talk_recovery_scope import maintain_delivery_recovery_scope
from src.autoslice.operator_processing_scope import operator_talk_scope
from src.autoslice.runner_proxy import RunnerProxy
from src.autoslice import semantic_evidence_scorecard_refresh as semantic_chat_refresh


_runner = RunnerProxy()


def freeze(state: Mapping[str, object], *, date: str) -> tuple[str, ...] | None:
    """Return the immutable Talk-only allowlist for this tick, if any."""

    return operator_talk_scope(state, date=date)


def maintain(
    date: str,
    state: dict,
    *,
    automatic_maintenance: bool,
    candidate_ids: tuple[str, ...] | None,
) -> tuple[int, int, int, int, bool]:
    return maintain_delivery_recovery_scope(
        date,
        state,
        automatic_maintenance=automatic_maintenance,
        talk_candidate_ids=candidate_ids,
    )


def work_flags(
    date: str,
    state: dict,
    *,
    automatic_maintenance: bool,
    candidate_ids: tuple[str, ...] | None,
) -> tuple[bool, bool, bool]:
    return semantic_chat_refresh.runner_date_work_flags(
        date,
        state,
        automatic_maintenance=automatic_maintenance,
        talk_candidate_ids=candidate_ids,
    )


def discover(date: str, state: dict, candidate_ids: tuple[str, ...] | None) -> None:
    if candidate_ids is None:
        _runner.discover_segments(date, state)


def prioritize_and_capture(
    state: dict,
    candidate_ids: tuple[str, ...] | None,
) -> list[dict]:
    allowed = set(candidate_ids) if candidate_ids is not None else None
    capture = [
        dict(item)
        for item in state.get("pending_talk", [])
        if isinstance(item, dict)
        and (
            allowed is None
            or str(item.get("cid") or item.get("candidate_id") or "") in allowed
        )
    ]
    reprioritize(state, candidate_ids)
    return capture


def reprioritize(state: dict, candidate_ids: tuple[str, ...] | None) -> None:
    _runner.prioritize(
        state,
        frozen_talk_candidate_ids=candidate_ids,
        allow_song_work=candidate_ids is None,
    )


def repair_covers(
    date: str,
    state: dict,
    *,
    automatic_maintenance: bool,
    candidate_ids: tuple[str, ...] | None,
) -> None:
    if not automatic_maintenance:
        return
    if candidate_ids is None:
        _runner.repair_covers(date, state)
        return
    allowed = set(candidate_ids)
    scoped = {
        str(record.get("candidate_id") or record.get("cid") or "")
        for record in state.get("picks", [])
        if isinstance(record, dict)
        and str(record.get("candidate_id") or record.get("cid") or "") in allowed
        and _runner.cover_repair_needed(date, record)
    }
    if scoped:
        _runner.repair_covers(date, state, candidate_ids=scoped)
