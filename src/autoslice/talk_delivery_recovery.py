"""Focused implementation of selected Talk delivery requeue.

The legacy :mod:`src.autoslice.delivery_recovery` module remains the public
and monkeypatchable integration surface.  It passes its live module object to
this leaf at call time so tests and runtime overrides keep resolving the same
symbols without an import cycle.
"""

from __future__ import annotations

import copy
import subprocess
import time
from collections.abc import Collection, Mapping
from pathlib import Path
from types import ModuleType
from typing import NamedTuple


class _PreparedTalkRequeue(NamedTuple):
    item: dict[str, object]
    current: str
    current_recovery: str
    retry_count: int
    transient_count: int


def _retry_reason(
    sanctioned_retry: bool,
    selected_source_fact_retry: bool,
    provider_budget_retry: bool,
    carryover_retry: bool,
    rescore_retry: bool,
    cover_route_retry: bool,
    changed: bool,
    infrastructure_retry: bool,
) -> str:
    if sanctioned_retry:
        return "sanctioned_candidate_revival"
    if selected_source_fact_retry:
        return "selected_source_fact_contract_changed"
    if provider_budget_retry:
        return "final_review_provider_budget"
    if carryover_retry:
        return "final_review_carryover"
    if rescore_retry:
        return "source_fact_rescore"
    if cover_route_retry:
        return "cover_route_regeneration"
    if changed:
        return "pipeline_fingerprint_changed"
    if infrastructure_retry:
        return "transient_infrastructure_failure"
    return "transient_produce_failure"


def _apply_recovery_lineage_receipt(
    *,
    record: Mapping[str, object],
    item: dict[str, object],
    candidate_id: str,
    scope: tuple[str, str] | None,
    initial_retry: bool,
    lineage_retry: bool,
    receipt_field: str,
    builder: object,
    advancer: object,
    pick_to_queue_transition: str,
    current_fingerprint: str,
) -> bool:
    """Build or advance one typed receipt without broadening its scope."""

    if not initial_retry and not lineage_retry:
        return True
    if scope is None:
        return False
    try:
        if initial_retry:
            item[receipt_field] = builder(
                old_row=record,
                queued_row=item,
                candidate_id=candidate_id,
                grant_id=scope[1],
                current_fingerprint=current_fingerprint,
            )
        else:
            item[receipt_field] = advancer(
                record[receipt_field],
                from_row=record,
                to_row=item,
                candidate_id=candidate_id,
                grant_id=scope[1],
                transition_kind=pick_to_queue_transition,
            )
    except (KeyError, TypeError, ValueError):
        return False
    return True


def _prepare_recoverable_talk(
    runtime: ModuleType,
    *,
    date: str,
    record: dict[str, object],
    cid: str,
    existing_pending: set[str],
    source_fact_scope: tuple[str, str] | None,
    final_review_scope: tuple[str, str] | None,
    selected_source_fact_retry: bool,
    selected_source_fact_lineage_retry: bool,
    selected_final_review_retry: bool,
    selected_final_review_lineage_retry: bool,
    provider_budget_history: Collection[object],
) -> _PreparedTalkRequeue | None:
    try:
        current = runtime._runner.talk_pipeline_fingerprint(cid)
        current_recovery = runtime._runner.talk_failure_recovery_fingerprint(
            record.get("failure_kind"), cid
        )
    except ValueError:
        return None
    decision = runtime._talk_retry_decision(
        record,
        cid=cid,
        existing_pending=existing_pending,
        current_recovery=current_recovery,
        provider_budget_history=provider_budget_history,
    )
    if decision is None:
        return None
    (
        retry_count,
        transient_count,
        changed,
        infrastructure_retry,
        transient,
        cover_route_retry,
        sanctioned_revival_retry,
        carryover_fingerprint,
        carryover_retry,
        provider_budget_ledger,
        sanctioned_retry,
        rescore_fingerprint,
        rescore_retry,
    ) = decision
    carried_provider_budget_ledger = (
        provider_budget_ledger
        or runtime.preserved_provider_budget_retry_ledger(
            record,
            candidate_id=cid,
            history_records=provider_budget_history,
        )
    )
    segment_name = Path(
        str(record.get("segment") or record.get("segment_path") or "")
    ).name
    segment = runtime._runner.REC_ROOT / date / segment_name
    start_ms, end_ms = record.get("start_ms"), record.get("end_ms")
    try:
        segment_is_file = segment.is_file()
    except OSError as exc:
        record["recovery_source_status"] = "SOURCE_RECORDING_ROOT_UNAVAILABLE"
        record["recovery_source_error"] = f"{type(exc).__name__}: {exc}"
        return None
    if (
        not segment_name
        or not segment_is_file
        or isinstance(start_ms, bool)
        or not isinstance(start_ms, int)
        or isinstance(end_ms, bool)
        or not isinstance(end_ms, int)
        or start_ms >= end_ms
    ):
        return None
    try:
        seg_dur = runtime.historical_recording_duration.resolve(
            runtime._runner, date, segment, record
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        record["recovery_source_status"] = "SOURCE_RECORDING_ROOT_UNAVAILABLE"
        record["recovery_source_error"] = f"{type(exc).__name__}: {exc}"
        return None
    if (
        isinstance(seg_dur, bool)
        or not isinstance(seg_dur, int)
        or seg_dur <= 0
        or end_ms > seg_dur
    ):
        return None
    try:
        given_end_ms, given_end_authority = (
            runtime._validated_given_end_boundary(
                candidate_id=cid,
                start_ms=start_ms,
                end_ms=end_ms,
                seg_dur_ms=seg_dur,
                given_end_ms=record.get("given_end_ms"),
                given_end_authority=record.get("given_end_authority"),
            )
        )
    except runtime.RecoveryReviewRerunError:
        return None
    try:
        given_title, publication_authority = (
            runtime._validated_recovery_publication(
                candidate_id=cid,
                recovery_publication_authority=record.get(
                    "recovery_publication_authority"
                ),
            )
        )
    except runtime.RecoveryReviewRerunError:
        return None
    if (
        record.get("given_title") is not None
        and given_title != record.get("given_title")
    ):
        return None
    try:
        chat_binding = runtime._structured_chat_binding_for_record(
            segment,
            record,
            candidate_id=cid,
        )
    except runtime.RecoveryReviewRerunError as exc:
        record["recovery_chat_binding_status"] = "BLOCKED"
        record["recovery_chat_binding_error"] = str(exc)
        return None
    item = {
        "cid": cid,
        "segment_path": str(segment),
        "segment_scene_context": copy.deepcopy(
            record.get("segment_scene_context")
        ),
        "seg_dur_ms": seg_dur,
        "start_ms": start_ms,
        "end_ms": min(seg_dur, end_ms) if seg_dur else end_ms,
        "xml": (
            str(xml)
            if (xml := runtime._runner.find_danmaku_xml(segment))
            else None
        ),
        **chat_binding,
        "hook": (
            str(
                (record.get("source_fact_rescore") or {}).get(
                    "repaired_hook"
                )
                or record.get("hook", "")
            )
            if rescore_retry
            else record.get("hook", "")
        ),
        "confidence": record.get("confidence"),
        "selection_scorecard": (
            None
            if rescore_retry
            else runtime.apply_reviewed_selection_calibration(
                cid,
                record.get("selection_scorecard"),
            )
        ),
        "session_relation_authority": record.get(
            "session_relation_authority"
        ),
        "lane": record.get("lane", ""),
        "preview": record.get("preview", ""),
        "selected_repair": True,
        "talk_repair_retry_count": retry_count
        + runtime._repair_budget_charge(
            decision,
            provider_budget_retry=provider_budget_ledger is not None,
        ),
        "talk_transient_retry_count": transient_count
        + (
            1
            if transient
            and not changed
            and not cover_route_retry
            and not sanctioned_retry
            and not carryover_retry
            and provider_budget_ledger is None
            and not rescore_retry
            else 0
        ),
        "retry_reason": _retry_reason(
            sanctioned_retry,
            selected_source_fact_retry,
            provider_budget_ledger is not None,
            carryover_retry,
            rescore_retry,
            cover_route_retry,
            changed,
            infrastructure_retry,
        ),
        "bcut_srt_path": str(
            runtime._runner.BASE / "cache" / date / f"{segment.stem}.bcut.srt"
        ),
        "session_id": runtime._recording_session_id(record),
        "filler_proposals": list(record.get("filler_proposals") or []),
        "filler_proposal_srt_sha256": record.get(
            "filler_proposal_srt_sha256"
        ),
        "merge_gap_removals": list(record.get("merge_gap_removals") or []),
        "cover_diversity_slot": record.get("cover_diversity_slot"),
        **runtime.carry_frozen_admission(record),
        **runtime._cover_route_regeneration_receipt(record),
        **runtime.copied_refresh_receipt(record),
        "recovery_source_record_sha256": runtime._canonical_object_sha256(
            record
        ),
    }
    # sanctioned-revival 审计块必须跨 requeue 存活（复活是治理事件，
    # 丢块等于抹掉“谁在何据下解冻化石态”的证据链）。
    if record.get("revivals"):
        item["revivals"] = list(record["revivals"])
    if sanctioned_revival_retry is not None:
        item["sanctioned_revival_retry"] = {
            **sanctioned_revival_retry,
            "status": "QUEUED",
            "queued_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
        }
    consumed_carryovers = [
        value
        for value in (
            record.get("final_review_carryover_consumed_fingerprints") or []
        )
        if isinstance(value, str)
    ]
    if carryover_fingerprint is not None:
        consumed_carryovers.append(carryover_fingerprint)
    if consumed_carryovers:
        item["final_review_carryover_consumed_fingerprints"] = list(
            dict.fromkeys(consumed_carryovers)
        )
    if carried_provider_budget_ledger is not None:
        item[runtime.FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD] = (
            carried_provider_budget_ledger
        )
    if given_end_ms is not None:
        item["given_end_ms"] = given_end_ms
        item["given_end_authority"] = given_end_authority
    if given_title is not None:
        item["given_title"] = given_title
        item["recovery_publication_authority"] = publication_authority
    if rescore_retry:
        item["rescore_pending"] = True
        item["source_fact_rescore"] = dict(
            record.get("source_fact_rescore") or {}
        )
        consumed_rescores = [
            value
            for value in (record.get("rescore_consumed_fingerprints") or [])
            if isinstance(value, str)
        ]
        consumed_rescores.append(rescore_fingerprint)
        item["rescore_consumed_fingerprints"] = list(
            dict.fromkeys(consumed_rescores)
        )
    if not _apply_recovery_lineage_receipt(
        record=record,
        item=item,
        candidate_id=cid,
        scope=source_fact_scope,
        initial_retry=selected_source_fact_retry,
        lineage_retry=selected_source_fact_lineage_retry,
        receipt_field=runtime.SELECTED_SOURCE_FACT_RECOVERY_RECEIPT_FIELD,
        builder=runtime.build_selected_source_fact_recovery_receipt,
        advancer=runtime.advance_selected_source_fact_recovery_receipt,
        pick_to_queue_transition=runtime.PICK_TO_QUEUE_TRANSITION,
        current_fingerprint=current_recovery,
    ):
        return None
    if not _apply_recovery_lineage_receipt(
        record=record,
        item=item,
        candidate_id=cid,
        scope=final_review_scope,
        initial_retry=selected_final_review_retry,
        lineage_retry=selected_final_review_lineage_retry,
        receipt_field=runtime.SELECTED_FINAL_REVIEW_RECOVERY_RECEIPT_FIELD,
        builder=runtime.build_selected_final_review_recovery_receipt,
        advancer=runtime.advance_selected_final_review_recovery_receipt,
        pick_to_queue_transition=(
            runtime.FINAL_REVIEW_PICK_TO_QUEUE_TRANSITION
        ),
        current_fingerprint=current_recovery,
    ):
        return None
    return _PreparedTalkRequeue(
        item,
        current,
        current_recovery,
        retry_count,
        transient_count,
    )


def requeue_recoverable_talks(
    runtime: ModuleType,
    date: str,
    state: dict,
    *,
    candidate_ids: Collection[str] | None = None,
) -> int:
    """Implement selected Talk requeue through the legacy runtime seam."""

    # 维护者 裁定的迁移面：裁定之前化石化的说话人拒绝行先迁回停泊态，
    # 再进下面的常规恢复判定（本体在 src/autoslice/speaker_manual_review.py）。
    allowed = set(candidate_ids) if candidate_ids is not None else None
    source_fact_scope = runtime._active_selected_source_fact_recovery_scope(
        date, state, candidate_ids
    )
    final_review_scope = runtime._active_selected_final_review_recovery_scope(
        date, state, candidate_ids
    )
    from src.autoslice.selected_final_review_terminal_regrant import (
        RECOVERY_RECEIPT_FIELD as TERMINAL_REGRANT_RECEIPT_FIELD,
        active_selected_final_review_terminal_regrant_scope,
        build_selected_final_review_terminal_regrant_receipt,
        is_selected_final_review_terminal_rejection,
    )

    terminal_regrant_scope = active_selected_final_review_terminal_regrant_scope(
        date, state, candidate_ids
    )
    runtime.restore_fossilized_speaker_holds(state, candidate_ids=allowed)
    existing_pending = {
        str(item.get("cid") or item.get("candidate_id") or "")
        for item in state.get("pending_talk", [])
        if isinstance(item, dict)
    }
    exact_contract_ids = set(runtime._exact_talk_contract_ids(state))
    provider_budget_history = state.get("talk_superseded_attempts") or ()
    kept: list[dict] = []
    requeued: list[dict] = []
    for record in state.get("picks", []):
        if not isinstance(record, dict):
            kept.append(record)
            continue
        cid = str(record.get("candidate_id") or record.get("cid") or "")
        if allowed is not None and cid not in allowed:
            kept.append(record)
            continue
        recoverable_status = (
            record.get("status") in runtime.TALK_RECOVERY_FAILURE_STATUSES
        )
        selected_source_fact_retry = bool(
            source_fact_scope is not None
            and cid == source_fact_scope[0]
            and runtime.is_selected_source_fact_rejection(record)
        )
        selected_source_fact_lineage_retry = bool(
            source_fact_scope is not None
            and cid == source_fact_scope[0]
            and isinstance(
                record.get(
                    runtime.SELECTED_SOURCE_FACT_RECOVERY_RECEIPT_FIELD
                ),
                Mapping,
            )
        )
        selected_final_review_retry = bool(
            final_review_scope is not None
            and cid == final_review_scope[0]
            and runtime.is_selected_final_review_rejection(record)
        )
        selected_final_review_lineage_retry = bool(
            final_review_scope is not None
            and cid == final_review_scope[0]
            and isinstance(
                record.get(
                    runtime.SELECTED_FINAL_REVIEW_RECOVERY_RECEIPT_FIELD
                ),
                Mapping,
            )
        )
        selected_terminal_regrant_retry = bool(
            terminal_regrant_scope is not None
            and cid == terminal_regrant_scope[0]
            and is_selected_final_review_terminal_rejection(record)
        )
        if not (
            recoverable_status
            or selected_source_fact_retry
            or selected_final_review_retry
            or selected_terminal_regrant_retry
            or runtime.supplemental_recovery_candidate(
                record,
                candidate_id=cid,
                exact_contract_ids=exact_contract_ids,
            )
        ):
            kept.append(record)
            continue
        prepared = _prepare_recoverable_talk(
            runtime,
            date=date,
            record=record,
            cid=cid,
            existing_pending=existing_pending,
            source_fact_scope=source_fact_scope,
            final_review_scope=final_review_scope,
            selected_source_fact_retry=selected_source_fact_retry,
            selected_source_fact_lineage_retry=(
                selected_source_fact_lineage_retry
            ),
            selected_final_review_retry=selected_final_review_retry,
            selected_final_review_lineage_retry=(
                selected_final_review_lineage_retry
            ),
            provider_budget_history=provider_budget_history,
        )
        if prepared is None:
            kept.append(record)
            continue
        item = prepared.item
        if selected_terminal_regrant_retry:
            parent_receipt = record.get(
                runtime.SELECTED_FINAL_REVIEW_RECOVERY_RECEIPT_FIELD
            )
            grant = state.get("operator_processing_scope")
            try:
                if not isinstance(parent_receipt, Mapping) or not isinstance(
                    grant, Mapping
                ):
                    raise ValueError("terminal regrant parent authority is missing")
                item[runtime.SELECTED_FINAL_REVIEW_RECOVERY_RECEIPT_FIELD] = (
                    copy.deepcopy(parent_receipt)
                )
                item["retry_reason"] = "selected_final_review_terminal_regrant"
                item[TERMINAL_REGRANT_RECEIPT_FIELD] = (
                    build_selected_final_review_terminal_regrant_receipt(
                        state=state,
                        old_row=record,
                        queued_row=item,
                        candidate_id=cid,
                        grant=grant,
                    )
                )
            except (TypeError, ValueError):
                kept.append(record)
                continue
        requeued.append(item)
        existing_pending.add(cid)
        archived = {
            "candidate_id": cid,
            "status": record.get("status"),
            "pipeline_fingerprint": record.get("pipeline_fingerprint"),
            "superseded_by": prepared.current,
            "failure_recovery_fingerprint": record.get(
                "failure_recovery_fingerprint"
            ),
            "superseded_recovery_fingerprint": prepared.current_recovery,
            "talk_repair_retry_count": prepared.retry_count,
            "talk_transient_retry_count": prepared.transient_count,
            "retry_reason": item["retry_reason"],
            "failure_kind": record.get("failure_kind"),
            "failure_stage": record.get("failure_stage"),
            "failure_fingerprint": record.get("failure_fingerprint"),
            "sanctioned_revival_retry": item.get(
                "sanctioned_revival_retry"
            ),
            "final_review_carryover_consumed_fingerprints": item.get(
                "final_review_carryover_consumed_fingerprints"
            ),
            runtime.SELECTED_SOURCE_FACT_RECOVERY_RECEIPT_FIELD: (
                copy.deepcopy(
                    item.get(
                        runtime.SELECTED_SOURCE_FACT_RECOVERY_RECEIPT_FIELD
                    )
                )
            ),
            runtime.SELECTED_FINAL_REVIEW_RECOVERY_RECEIPT_FIELD: (
                copy.deepcopy(
                    item.get(
                        runtime.SELECTED_FINAL_REVIEW_RECOVERY_RECEIPT_FIELD
                    )
                )
            ),
            "session_id": runtime._recording_session_id(record),
        }
        if isinstance(item.get(TERMINAL_REGRANT_RECEIPT_FIELD), Mapping):
            archived[TERMINAL_REGRANT_RECEIPT_FIELD] = copy.deepcopy(
                item[TERMINAL_REGRANT_RECEIPT_FIELD]
            )
        if (
            item.get(runtime.FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD)
            is not None
        ):
            archived[runtime.FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD] = (
                copy.deepcopy(
                    item[runtime.FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD]
                )
            )
        state.setdefault("talk_superseded_attempts", []).append(archived)
    state["picks"] = kept
    state.setdefault("pending_talk", []).extend(requeued)
    # 闭环接线（维护者 狍哥案实施指令）：这是 exact-contract 和
    # 普通两条 requeue 路径共同经过的唯一收口——一次调用覆盖两条分支，
    # 不新增第二个调用点。重活在 selection_rescore.py。
    runtime.selection_rescore.execute_pending_rescores(
        date, state, candidate_ids=allowed
    )
    return len(requeued)
