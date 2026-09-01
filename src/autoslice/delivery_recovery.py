"""Cross-tick delivery and failure recovery for the unattended runner.

The runner owns live paths, policy constants, and patchable integration points.
This module resolves those names lazily at call time so tests and manual repair
entry points keep the same runner-level seam in both module and cron script
execution modes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
import time
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import NamedTuple

from src.autoslice.candidate_selection import _exact_talk_contract_ids
from src.autoslice.published_cover_carry import queue_marker_is_valid, queue_plan_projection
from src.autoslice.final_review_carryover_retry import (
    unconsumed_final_review_carryover as _unconsumed_final_review_carryover,
)
from src.autoslice.final_review_provider_budget_retry import (
    LEDGER_FIELD as FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD,
    LEDGER_STATE_EMPTY as FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_EMPTY,
    LEDGER_STATE_INVALID as FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_INVALID,
    consumed_provider_budget_retry_ledger,
    preserved_provider_budget_retry_ledger,
    provider_budget_retry_claimed,
    resolve_provider_budget_retry_ledger_history,
    unconsumed_provider_budget_retry,
)
from src.autoslice.batch_terminal_state import (
    project_terminal_song_disposition,
    song_infra_transient_is_active,
)
from src.autoslice.runner_proxy import RunnerProxy
from src.autoslice.recovery_title_authority import (
    RecoveryTitleAuthorityError,
    expected_recovery_publish_title,
    validate_recovery_publication_authority,
)
from src.autoslice import historical_recording_duration, selection_rescore
from src.autoslice.semantic_scorecard_refresh_receipt import copied_refresh_receipt
from src.autoslice.selected_source_fact_recovery import (
    PICK_TO_QUEUE_TRANSITION,
    RECOVERY_RECEIPT_FIELD as SELECTED_SOURCE_FACT_RECOVERY_RECEIPT_FIELD,
    advance_selected_source_fact_recovery_receipt,
    build_selected_source_fact_recovery_receipt,
    is_selected_source_fact_rejection,
)
from src.autoslice.selected_final_review_recovery import (
    PICK_TO_QUEUE_TRANSITION as FINAL_REVIEW_PICK_TO_QUEUE_TRANSITION,
    RECOVERY_RECEIPT_FIELD as SELECTED_FINAL_REVIEW_RECOVERY_RECEIPT_FIELD,
    advance_selected_final_review_recovery_receipt,
    build_selected_final_review_recovery_receipt,
    is_selected_final_review_rejection,
    active_selected_final_review_recovery_scope,
)
from src.autoslice.selection_scorecard import apply_reviewed_selection_calibration
from src.autoslice.speaker_manual_review import (
    SPEAKER_MANUAL_REVIEW_STATUSES,
    park_for_manual_review,
    restore_fossilized_speaker_holds,
)
from src.autoslice import song_name_authority
from src.autoslice.talk_quota_freeze import carry_frozen_admission
from src.autoslice.talk_recovery_record_policy import (
    supplemental_recovery_candidate,
)

_runner = RunnerProxy()

# The focused Talk requeue leaf resolves these through this live module object
# so existing runner/test monkeypatch seams remain authoritative after extraction.
_TALK_DELIVERY_RECOVERY_RUNTIME_EXPORTS = (
    PICK_TO_QUEUE_TRANSITION,
    SELECTED_SOURCE_FACT_RECOVERY_RECEIPT_FIELD,
    advance_selected_source_fact_recovery_receipt,
    build_selected_source_fact_recovery_receipt,
    is_selected_source_fact_rejection,
    restore_fossilized_speaker_holds,
    carry_frozen_admission,
    supplemental_recovery_candidate,
    preserved_provider_budget_retry_ledger,
    FINAL_REVIEW_PICK_TO_QUEUE_TRANSITION,
    SELECTED_FINAL_REVIEW_RECOVERY_RECEIPT_FIELD,
    advance_selected_final_review_recovery_receipt,
    build_selected_final_review_recovery_receipt,
    is_selected_final_review_rejection,
)

_PIPELINE_FINGERPRINT_RX = re.compile(r"sha256:[0-9a-f]{64}")
_SAFE_CANDIDATE_ID_RX = re.compile(r"[A-Za-z0-9_-]{1,96}")
_SOURCE_SHA256_RX = re.compile(r"(?:sha256:)?([0-9a-f]{64})")
TALK_RECOVERY_FAILURE_STATUSES = frozenset(
    {
        "boundary_unrepairable",
        "speaker_review_required",
        "speaker_evidence_insufficient",
        "failed",
    }
)

# Only failures the classifier POSITIVELY identified as an infrastructure wait
# may retry on a timer for as long as the wait lasts (mount watchdog repairs
# the mount, a provider quota window reopens).  A generic/unknown producer
# failure must not inherit that unlimited loop: an unrecognized deterministic
# defect (for example a vanished source before classification existed) would
# then churn every tick forever and, because the runner treats a recoverable
# failure as an outage, stall the rest of the batch with it.
INFRASTRUCTURE_WAIT_FAILURE_KINDS = frozenset(
    {
        "runtime_prerequisite",
        "provider_transient",
    }
)

SANCTIONED_REVIVAL_RETRY_SCHEMA = "sanctioned-revival-retry.v1"
CONTENT_BOUNDARY_RECOVERY_RELATIVES = (
    "scripts/produce_slice_package.py",
    "src/autoslice/jingting_chunker.py",
    "src/autoslice/subtitle_timing_qa.py",
    "src/autoslice/boundary_endpoint_binding.py",
    "src/autoslice/boundary_resolver.py",
    "src/autoslice/boundary_semantic_review.py",
    "src/autoslice/boundary_semantic_projection.py",
    "src/autoslice/boundary_source_context_coverage.py",
    "src/autoslice/final_review_contract.py",
    "src/autoslice/frozen_boundary_receipt.py",
    "src/autoslice/frozen_source_boundary_receipt.py",
    "src/autoslice/piece_roles.py",
    "src/autoslice/producer_boundary.py",
    "src/autoslice/producer_boundary_owner_contract.py",
    "src/autoslice/producer_boundary_resolution.py",
    "src/autoslice/producer_boundary_review_stage.py",
    "src/autoslice/producer_source_boundary_review.py",
    "src/autoslice/producer_source_media.py",
    "src/autoslice/producer_request.py",
    "src/autoslice/producer_text_pipeline.py",
    "src/autoslice/redelivery_boundary_projection.py",
    "src/autoslice/redelivery_source_binding.py",
    "src/autoslice/redelivery_subtitle_baseline.py",
    "src/autoslice/reviewed_exact_source_interval.py",
    "src/autoslice/source_subtitle_truth.py",
    "src/autoslice/talk_delivery_recovery.py",
    "src/autoslice/talk_lane.py",
)


class _TalkRetryDecision(NamedTuple):
    retry_count: int
    transient_count: int
    changed: bool
    infrastructure_retry: bool
    transient: bool
    cover_route_retry: bool
    sanctioned_revival_retry: dict[str, object] | None
    carryover_fingerprint: str | None
    carryover_retry: bool
    provider_budget_ledger: dict[str, object] | None
    sanctioned_retry: bool
    rescore_fingerprint: str | None
    rescore_retry: bool


def _pending_sanctioned_revival_retry(
    record: Mapping[str, object],
) -> dict[str, object] | None:
    marker = record.get("sanctioned_revival_retry")
    revivals = record.get("revivals")
    if (
        not isinstance(marker, dict)
        or marker.get("schema_version") != SANCTIONED_REVIVAL_RETRY_SCHEMA
        or marker.get("status") != "PENDING"
        or not isinstance(revivals, list)
    ):
        return None
    revival_index = marker.get("revival_index")
    if (
        isinstance(revival_index, bool)
        or not isinstance(revival_index, int)
        or revival_index < 0
        or revival_index >= len(revivals)
    ):
        return None
    revival = revivals[revival_index]
    if (
        not isinstance(revival, dict)
        or revival.get("schema_version") != "candidate-revival.v1"
        or revival.get("revived_at") != marker.get("revived_at")
        or revival.get("expected_fix_commit")
        != marker.get("expected_fix_commit")
        or record.get("failure_recoverable") is not True
    ):
        return None
    return dict(marker)


def _talk_retry_decision(
    record: Mapping[str, object],
    *,
    cid: str,
    existing_pending: set[str],
    current_recovery: str,
    provider_budget_history: Collection[object] = (),
) -> _TalkRetryDecision | None:
    """Return the bounded retry route, or ``None`` when this tick must keep it."""

    retry_count = int(record.get("talk_repair_retry_count") or 0)
    transient_count = int(record.get("talk_transient_retry_count") or 0)
    recorded_recovery = record.get(
        "failure_recovery_fingerprint"
    ) or record.get("pipeline_fingerprint")
    changed = recorded_recovery != current_recovery
    next_retry_at = record.get("next_retry_at_epoch")
    infrastructure_waiting = bool(
        record.get("failure_recoverable") is True
        and record.get("failure_kind")
        in INFRASTRUCTURE_WAIT_FAILURE_KINDS
        and isinstance(next_retry_at, (int, float))
        and not isinstance(next_retry_at, bool)
        and time.time() < float(next_retry_at)
    )
    infrastructure_retry = bool(
        record.get("failure_recoverable") is True
        and record.get("failure_kind")
        in INFRASTRUCTURE_WAIT_FAILURE_KINDS
        and (
            not isinstance(next_retry_at, (int, float))
            or isinstance(next_retry_at, bool)
            or time.time() >= float(next_retry_at)
        )
    )
    transient = (
        record.get("status") == "failed"
        and record.get("failure_recoverable") is not False
        and transient_count < 1
    ) or infrastructure_retry
    # Screenshot-route maintenance owns an independent fingerprint-bound,
    # one-shot budget after a reviewable talk package already exists.
    cover_route_retry = bool(
        record.get("status") == "failed"
        and record.get("failure_recoverable") is True
        and record.get("failure_kind") == "cover_route_regeneration"
        and isinstance(
            record.get("cover_route_regeneration_fingerprint"), str
        )
        and int(record.get("cover_route_regeneration_attempts") or 0) > 0
    )
    sanctioned_revival_retry = _pending_sanctioned_revival_retry(record)
    carryover_fingerprint = _unconsumed_final_review_carryover(record)
    carryover_retry = carryover_fingerprint is not None
    provider_budget_retry = unconsumed_provider_budget_retry(
        record,
        candidate_id=cid,
        history_records=provider_budget_history,
    )
    provider_budget_ledger = (
        consumed_provider_budget_retry_ledger(
            record,
            candidate_id=cid,
            retry=provider_budget_retry,
            history_records=provider_budget_history,
        )
        if provider_budget_retry is not None
        else None
    )
    ledger_state, _ = resolve_provider_budget_retry_ledger_history(
        record,
        candidate_id=cid,
        history_records=provider_budget_history,
    )
    if (
        ledger_state
        in {
            FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_EMPTY,
            FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_INVALID,
        }
        and provider_budget_ledger is None
    ):
        # An explicit ledger is authority, not optional decoration.  A bad or
        # foreign-CID value must not disappear through an unrelated generic
        # changed/transient retry.  The only non-preserved form accepted here
        # is a structurally valid empty ledger being consumed by this route.
        return None
    if provider_budget_retry_claimed(record) and provider_budget_ledger is None:
        return None
    sanctioned_retry = sanctioned_revival_retry is not None
    # 狍哥案修复：selection_rescore 有自己的有界一次性预算
    # （sha256(failure_fingerprint + repaired_hook_sha256)，CAP=2），独立于
    # 通用 transient/lifetime 重试计数——同一 fingerprint 只吃一次，与
    # final_review_carryover 同款账本模式（rescore_consumed_fingerprints）。
    rescore_fingerprint = selection_rescore.unconsumed_rescore_fingerprint(record)
    rescore_retry = rescore_fingerprint is not None
    if (
        infrastructure_waiting
        and not sanctioned_retry
        and not carryover_retry
        and provider_budget_retry is None
    ):
        # Deploy fingerprint drift must not bypass an infrastructure cooldown.
        return None
    if (
        not cid
        or cid in existing_pending
        or not (
            changed
            or transient
            or cover_route_retry
            or sanctioned_retry
            or carryover_retry
            or provider_budget_retry is not None
            or rescore_retry
        )
        or (
            retry_count >= _runner.TALK_REPAIR_LIFETIME_RETRY_CAP
            and not infrastructure_retry
            and not changed
            and not cover_route_retry
            and not sanctioned_retry
            and not carryover_retry
            and provider_budget_retry is None
            and not rescore_retry
        )
    ):
        return None
    return _TalkRetryDecision(
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
    )

def _repair_budget_charge(
    decision: _TalkRetryDecision, *, provider_budget_retry: bool = False
) -> int:
    """这次 requeue 要不要吃掉一格终生修复预算。

    ``TALK_REPAIR_LIFETIME_RETRY_CAP`` 计的是"真修复尝试"，可此前 requeue 无条件
    ``+1``：于是纯基础设施抖动（CPA 挂了、挂载掉了、配额窗口没开）光靠定时唤醒就
    能把额度烧光，等真修复部署下来时预算已经被噪声吃完（A1，维护者 逐字
    「就按 A1 走吧」「A1 要做」）。

    判据照抄写回处 ``talk_transient_retry_count`` 已有的那串路线判据（同一组标志
    位），只把 ``transient`` 换成 ``infrastructure_retry``——等价于"当且仅当本次
    ``retry_reason`` 解析成 ``transient_infrastructure_failure`` 才不收费"。

    两个刻意保留的性质：``changed`` 在 ``retry_reason`` 阶梯上压着
    ``infrastructure_retry``，所以"infra 失败 + 相关部署落地"那一次仍算真修复、
    照常收费；``talk_transient_retry_count`` 一字未动，infra 退避曲线
    （``infra_retry_policy``）读的仍是它。存量计数不追溯重算，单调不回退。
    """
    if (
        decision.infrastructure_retry
        and not decision.changed
        and not decision.cover_route_retry
        and not decision.sanctioned_retry
        and not decision.carryover_retry
        and not decision.rescore_retry
    ) or provider_budget_retry:
        return 0
    return 1


def historical_source_recovery_in_progress(
    state: dict, cover_pending_status: str
) -> bool:
    """Keep an aged-out recovered date visible until every repair converges."""

    unresolved_rows = any(
        isinstance(row, dict)
        and (
            row.get("status") in TALK_RECOVERY_FAILURE_STATUSES
            or row.get("status") == cover_pending_status
            or row.get("failure_recoverable") is True
        )
        for row in (
            list(state.get("picks") or [])
            + list(state.get("songs") or [])
        )
    )
    return bool(state.get("source_recoveries")) and (
        state.get("status") in {"sealing", "processing"}
        or bool(state.get("pending_talk"))
        or bool(state.get("pending_song"))
        or unresolved_rows
    )


class RecoveryReviewRerunError(ValueError):
    """The explicit no-upload recovery rerun plan is not safely bound."""


def backfillable_talk_rejection(result: dict) -> tuple[str, str] | None:
    """Return the persisted rejection status/reason when a reserve may replace it.

    ``failure_kind == "selection_rescore"`` is deliberately its own kind, not
    a member of the ``{"subtitle_authority", "story_contract"}`` set below —
    that keeps the bounded rescore lane out of ``candidate_rejected +
    story_contract_unresolved_backfilled`` without touching either real
    terminal kind's semantics (狍哥案修复,).
    """

    status = result.get("status")
    if status in {
        "boundary_unrepairable",
        "speaker_review_required",
        "speaker_evidence_insufficient",
    }:
        reason = (
            "unsafe_boundary_backfilled"
            if status == "boundary_unrepairable"
            else "speaker_identity_unresolved_backfilled"
        )
        return str(status), reason
    if (
        status == "failed"
        and result.get("failure_kind") in {"subtitle_authority", "story_contract"}
        and result.get("failure_recoverable") is False
    ):
        return (
            "failed",
            "subtitle_authority_unresolved_backfilled"
            if result.get("failure_kind") == "subtitle_authority"
            else "story_contract_unresolved_backfilled",
        )
    return None


def apply_talk_backfill_rejection_policy(
    result: dict, *, exact_selected: bool
) -> bool:
    """Materialize a rejection only when this run is allowed to backfill it."""

    if exact_selected and result.get("status") == "candidate_rejected":
        # A direct gate rejection is still a failed selected delivery.  Leaving
        # it as candidate_rejected made the exact slot look intentionally
        # discarded and, because exact mode forbids a reserve, allowed an empty
        # queue to masquerade as review_ready.
        rejection_reason = str(
            result.get("rejection_reason")
            or result.get("failure_kind")
            or "exact_selected_candidate_rejected"
        )
        result["rejected_status"] = "candidate_rejected"
        result["status"] = "failed"
        result.setdefault("failure_kind", "candidate_gate")
        result.setdefault("failure_stage", "candidate_admission")
        result.setdefault("failure_recoverable", False)
        result["backfill_suppressed_by_exact_contract"] = {
            "schema_version": "exact-selection-backfill-suppression.v1",
            "status": "candidate_rejected",
            "reason": rejection_reason,
        }
        return False

    backfill_rejection = backfillable_talk_rejection(result)
    if backfill_rejection is None:
        return result.get("status") == "candidate_rejected"
    rejected_status, rejection_reason = backfill_rejection
    # 维护者：「说话人证据不足应该转人工审阅，不是判死」——说话人分离
    # 是刚开的功能（生产 8/7 才翻到 AUTOSLICE_SPEAKER_MODE=auto），不许拿它的
    # 不成熟去毙内容。处置与下面 exact 分支的既有范式同款：保留候选自己的说话
    # 人状态，不铸 candidate_rejected 化石；exact/普通两条路都盖同一份停泊回执。
    speaker_hold = rejected_status in SPEAKER_MANUAL_REVIEW_STATUSES
    if speaker_hold:
        park_for_manual_review(result, reason=rejection_reason)
    if exact_selected:
        result["backfill_suppressed_by_exact_contract"] = {
            "schema_version": "exact-selection-backfill-suppression.v1",
            "status": rejected_status,
            "reason": rejection_reason,
        }
        return False
    if speaker_hold:
        # 仍返回 True——席位照常让给候补（35fc448「Fix speaker evidence reserve
        # backfill」的既有裁定）。停泊只改"判死 vs 等人看"，不改配额。
        return True
    result["rejected_status"] = rejected_status
    result["status"] = "candidate_rejected"
    result["rejection_reason"] = rejection_reason
    return True


def _canonical_object_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _candidate_id(record: dict) -> str:
    return str(record.get("candidate_id") or record.get("cid") or "")


def _normalized_source_sha256(value: object) -> str | None:
    match = _SOURCE_SHA256_RX.fullmatch(str(value or "").strip().lower())
    if match is None:
        return None
    return "sha256:" + match.group(1)


def _record_source_sha256(record: dict) -> str | None:
    """Recover an already-bound official source hash without trusting labels."""

    for value in (record.get("source_media_sha256"), record.get("source_sha256")):
        normalized = _normalized_source_sha256(value)
        if normalized is not None:
            return normalized
    relation = record.get("session_relation_authority")
    if not isinstance(relation, dict):
        return None
    normalized = _normalized_source_sha256(relation.get("bound_source_sha256"))
    if normalized is not None:
        return normalized
    evidence = relation.get("evidence")
    if not isinstance(evidence, list):
        return None
    candidates = {
        normalized
        for row in evidence
        if isinstance(row, dict)
        and (
            row.get("source_alias_id")
            or row.get("evidence_class") == "HASH_BOUND_OFFICIAL_REPLAY"
        )
        if (normalized := _normalized_source_sha256(row.get("source_sha256")))
        is not None
    }
    return next(iter(candidates)) if len(candidates) == 1 else None


def _structured_chat_binding_for_record(
    segment: Path,
    record: dict,
    *,
    candidate_id: str,
) -> dict[str, object]:
    try:
        binding = _runner.resolve_structured_chat_binding(
            segment,
            source_sha256=_record_source_sha256(record),
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RecoveryReviewRerunError(
            f"RECOVERY_RERUN_CHAT_AUTHORITY_MISSING:{candidate_id}:{exc}"
        ) from exc
    if (
        not isinstance(binding, dict)
        or not isinstance(binding.get("structured_chat_required"), bool)
        or not str(binding.get("chat_binding_status") or "")
    ):
        raise RecoveryReviewRerunError(
            f"RECOVERY_RERUN_CHAT_BINDING_INVALID:{candidate_id}"
        )
    return binding


def _scorecard_rank_key(record: dict) -> tuple[int, float, str]:
    scorecard = record.get("selection_scorecard")
    tier = scorecard.get("tier") if isinstance(scorecard, dict) else None
    score = (
        scorecard.get("effective_score") if isinstance(scorecard, dict) else None
    )
    if (
        isinstance(tier, bool)
        or not isinstance(tier, int)
        or isinstance(score, bool)
        or not isinstance(score, (int, float))
    ):
        return (999, float("inf"), _candidate_id(record))
    return (tier, -float(score), _candidate_id(record))


def _validated_given_end_boundary(
    *,
    candidate_id: str,
    start_ms: int,
    end_ms: int,
    seg_dur_ms: int,
    given_end_ms: object,
    given_end_authority: object,
) -> tuple[int | None, str | None]:
    """Validate a reviewed source-timeline end before any recovery requeue.

    A selected record can keep a shorter semantic ``end_ms`` while carrying a
    reviewed ``given_end_ms`` that closes the final sentence.  Dropping that
    second boundary during automatic recovery silently reintroduces a cut in
    mid-thought, so the value and its authority travel as one fail-closed pair.
    """

    if given_end_ms is None:
        return None, None
    authority = str(given_end_authority or "").strip()
    if (
        isinstance(given_end_ms, bool)
        or not isinstance(given_end_ms, int)
        or given_end_ms <= start_ms
        or given_end_ms < end_ms
        or given_end_ms > seg_dur_ms
        or abs(given_end_ms - end_ms) > 30_000
        or not authority
    ):
        raise RecoveryReviewRerunError(
            f"RECOVERY_RERUN_GIVEN_END_INVALID:{candidate_id}"
        )
    return given_end_ms, authority


def _validated_recovery_publication(
    *,
    candidate_id: str,
    recovery_publication_authority: object,
) -> tuple[str | None, dict[str, object] | None]:
    """Replay the typed same-BV identity/title contract before requeue."""

    if recovery_publication_authority is None:
        return None, None
    try:
        authority = validate_recovery_publication_authority(
            recovery_publication_authority,
            candidate_id=candidate_id,
        )
    except RecoveryTitleAuthorityError as exc:
        raise RecoveryReviewRerunError(
            f"RECOVERY_RERUN_PUBLICATION_AUTHORITY_INVALID:"
            f"{candidate_id}:{exc}"
        ) from exc
    return expected_recovery_publish_title(authority), authority


def _cover_route_regeneration_receipt(record: dict) -> dict[str, object]:
    """Carry the per-build screenshot regeneration budget through requeue."""
    fingerprint = record.get("cover_route_regeneration_fingerprint")
    attempts = int(record.get("cover_route_regeneration_attempts") or 0)
    if fingerprint is None and attempts == 0:
        return {}
    return {
        "cover_route_regeneration_fingerprint": fingerprint,
        "cover_route_regeneration_attempts": attempts,
    }

def _recovery_queue_item(
    date: str,
    record: dict,
    *,
    candidate_id: str,
    retry_reason: str,
    selected_repair: bool,
    given_end_ms: int | None,
    given_end_authority: str | None,
    recovery_publication_authority: object,
    published_cover_carry: object = None,
    provider_budget_history: Collection[object] = (),
) -> dict:
    ledger_state, provider_budget_ledger = (
        resolve_provider_budget_retry_ledger_history(
            record,
            candidate_id=candidate_id,
            history_records=provider_budget_history,
        )
    )
    if published_cover_carry is not None and not queue_marker_is_valid(published_cover_carry, base=_runner.BASE, date=date, candidate_id=candidate_id):
        raise RecoveryReviewRerunError(f"RECOVERY_RERUN_PUBLISHED_COVER_CARRY_INVALID:{candidate_id}")
    if ledger_state in {
        FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_EMPTY,
        FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_INVALID,
    }:
        raise RecoveryReviewRerunError(
            "RECOVERY_RERUN_PROVIDER_BUDGET_LEDGER_INVALID:"
            f"{candidate_id}"
        )
    segment_name = Path(
        str(record.get("segment") or record.get("segment_path") or "")
    ).name
    segment = _runner.REC_ROOT / date / segment_name
    start_ms, end_ms = record.get("start_ms"), record.get("end_ms")
    if (
        not segment_name
        or not segment.is_file()
        or segment.is_symlink()
        or isinstance(start_ms, bool)
        or not isinstance(start_ms, int)
        or isinstance(end_ms, bool)
        or not isinstance(end_ms, int)
        or start_ms >= end_ms
    ):
        raise RecoveryReviewRerunError(
            f"RECOVERY_RERUN_SOURCE_INTERVAL_INVALID:{candidate_id}"
        )
    bcut_srt = _runner.BASE / "cache" / date / f"{segment.stem}.bcut.srt"
    if (
        not bcut_srt.is_file()
        or bcut_srt.is_symlink()
        or bcut_srt.stat().st_size <= 0
    ):
        raise RecoveryReviewRerunError(
            f"RECOVERY_RERUN_BCUT_AUTHORITY_MISSING:{candidate_id}"
        )
    seg_dur = historical_recording_duration.resolve(_runner, date, segment, record)
    if not isinstance(seg_dur, int) or seg_dur <= 0 or end_ms > seg_dur:
        raise RecoveryReviewRerunError(
            f"RECOVERY_RERUN_SOURCE_DURATION_INVALID:{candidate_id}"
        )
    given_end_ms, normalized_given_end_authority = _validated_given_end_boundary(
        candidate_id=candidate_id,
        start_ms=start_ms,
        end_ms=end_ms,
        seg_dur_ms=seg_dur,
        given_end_ms=given_end_ms,
        given_end_authority=given_end_authority,
    )
    given_title, normalized_publication_authority = (
        _validated_recovery_publication(
            candidate_id=candidate_id,
            recovery_publication_authority=recovery_publication_authority,
        )
    )
    chat_binding = _structured_chat_binding_for_record(
        segment,
        record,
        candidate_id=candidate_id,
    )
    retry_count = int(record.get("talk_repair_retry_count") or 0)
    item = {
        "cid": candidate_id,
        "segment_path": str(segment),
        "seg_dur_ms": seg_dur,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "xml": str(xml) if (xml := _runner.find_danmaku_xml(segment)) else None,
        **chat_binding,
        "hook": record.get("hook", ""),
        "confidence": record.get("confidence"),
        "selection_scorecard": apply_reviewed_selection_calibration(
            candidate_id,
            record.get("selection_scorecard"),
        ),
        "session_relation_authority": record.get("session_relation_authority"),
        "lane": record.get("lane", ""),
        "preview": record.get("preview", ""),
        "selected_repair": selected_repair,
        "talk_repair_retry_count": retry_count + (1 if selected_repair else 0),
        "talk_transient_retry_count": 0,
        "retry_reason": retry_reason,
        "bcut_srt_path": str(bcut_srt),
        "session_id": _recording_session_id(record),
        "filler_proposals": list(record.get("filler_proposals") or []),
        "filler_proposal_srt_sha256": record.get(
            "filler_proposal_srt_sha256"
        ),
        "merge_gap_removals": list(record.get("merge_gap_removals") or []),
        "cover_diversity_slot": record.get("cover_diversity_slot"),
        **_cover_route_regeneration_receipt(record),
        **copied_refresh_receipt(record),
        "recovery_source_record_sha256": _canonical_object_sha256(record),
    }
    if given_end_ms is not None:
        item["given_end_ms"] = given_end_ms
        item["given_end_authority"] = normalized_given_end_authority
    if given_title is not None:
        item["given_title"] = given_title
        item["recovery_publication_authority"] = (
            normalized_publication_authority
        )
    if provider_budget_ledger is not None:
        item[FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD] = (
            provider_budget_ledger
        )
    if published_cover_carry is not None:
        item["published_cover_carry"] = copy.deepcopy(published_cover_carry)
        item["published_cover_carry_required"] = True
        item["reuse_cover"] = True
    return item


def _carry_recovered_provider_budget_ledger_to_archive(
    queue_item: Mapping[str, object], archive: dict[str, object]
) -> None:
    """Archive the queue-normalized ledger, never the stale active preimage."""

    ledger = queue_item.get(FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD)
    if isinstance(ledger, Mapping):
        archive[FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD] = copy.deepcopy(
            dict(ledger)
        )
    else:
        archive.pop(FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD, None)


def _resolve_new_fingerprint_authority(
    queued_ids: set[str],
    *,
    expected_old_fingerprint: str,
    expected_new_fingerprint: str | None,
    expected_new_fingerprints_by_candidate: dict[str, str] | None,
) -> dict[str, str]:
    explicit = dict(expected_new_fingerprints_by_candidate or {})
    if expected_new_fingerprint is not None and explicit:
        raise RecoveryReviewRerunError(
            "RECOVERY_RERUN_NEW_FINGERPRINT_AUTHORITY_AMBIGUOUS"
        )
    if expected_new_fingerprint is not None:
        if not _PIPELINE_FINGERPRINT_RX.fullmatch(expected_new_fingerprint):
            raise RecoveryReviewRerunError(
                "RECOVERY_RERUN_NEW_FINGERPRINT_INVALID"
            )
        resolved = {cid: expected_new_fingerprint for cid in queued_ids}
    else:
        resolved = explicit
        if set(resolved) != queued_ids:
            raise RecoveryReviewRerunError(
                "RECOVERY_RERUN_NEW_FINGERPRINT_MAP_MUST_EQUAL_QUEUE"
            )
        if any(
            _PIPELINE_FINGERPRINT_RX.fullmatch(value) is None
            for value in resolved.values()
        ):
            raise RecoveryReviewRerunError(
                "RECOVERY_RERUN_NEW_FINGERPRINT_INVALID"
            )
    if any(value == expected_old_fingerprint for value in resolved.values()):
        raise RecoveryReviewRerunError("RECOVERY_RERUN_FINGERPRINT_UNCHANGED")
    return resolved


def _recovery_rerun_plan(
    *,
    date: str,
    requested: list[str],
    suppressed: list[str],
    replacements: list[str],
    expected_source_state_sha256: str,
    expected_old_fingerprint: str,
    new_fingerprint_by_candidate: dict[str, str],
    selection_authority: str,
    selection_override_events: list[dict[str, object]],
    selection_contract: dict,
    suppression_authority: str,
    boundary_overrides: dict[str, int],
    given_end_authority: str | None,
    recovery_publication_authorities: dict[str, dict[str, object]],
    queue: list[dict],
) -> dict:
    unique_new_fingerprints = set(new_fingerprint_by_candidate.values())
    return {
        "schema_version": "recovery-review-talk-rerun-plan.v7",
        "date": date,
        "upload_allowed": False,
        "source_state_sha256": expected_source_state_sha256,
        "old_pipeline_fingerprint": expected_old_fingerprint,
        "new_pipeline_fingerprint": (
            next(iter(unique_new_fingerprints))
            if len(unique_new_fingerprints) == 1
            else None
        ),
        "new_pipeline_fingerprints_by_candidate": dict(
            sorted(new_fingerprint_by_candidate.items())
        ),
        "candidate_ids": requested,
        "suppressed_candidate_ids": suppressed,
        "replacement_candidate_ids": replacements,
        "replacement_selection_authority": (
            selection_authority if replacements else None
        ),
        "selection_override_events": selection_override_events,
        "talk_selection_contract": copy.deepcopy(selection_contract),
        "user_suppression_authority": (
            suppression_authority if suppressed else None
        ),
        "given_end_ms_by_candidate": boundary_overrides,
        "given_end_authority": (
            str(given_end_authority).strip() if boundary_overrides else None
        ),
        "recovery_publication_authorities_by_candidate": dict(
            sorted(recovery_publication_authorities.items())
        ),
        "published_cover_carries_by_candidate": queue_plan_projection(queue),
        "queued_count": len(queue),
    }

def _validated_recovery_plan_overrides(
    *,
    queued_ids: set[str],
    given_end_ms_by_candidate: dict[str, int] | None,
    given_end_authority: str | None,
    recovery_publication_authorities_by_candidate: (
        dict[str, dict[str, object]] | None
    ),
) -> tuple[dict[str, int], dict[str, dict[str, object]]]:
    boundary_overrides = dict(given_end_ms_by_candidate or {})
    publication_authorities = dict(
        recovery_publication_authorities_by_candidate or {}
    )
    if set(boundary_overrides) != queued_ids:
        raise RecoveryReviewRerunError(
            "RECOVERY_RERUN_GIVEN_END_MUST_EQUAL_QUEUE"
        )
    normalized_given_end_authority = str(
        given_end_authority or ""
    ).strip()
    if not normalized_given_end_authority:
        raise RecoveryReviewRerunError(
            "RECOVERY_RERUN_GIVEN_END_AUTHORITY_REQUIRED"
        )
    if set(publication_authorities) != queued_ids:
        raise RecoveryReviewRerunError(
            "RECOVERY_RERUN_PUBLICATION_AUTHORITY_MUST_EQUAL_QUEUE"
        )
    for cid, authority in publication_authorities.items():
        _, validated = _validated_recovery_publication(
            candidate_id=cid,
            recovery_publication_authority=authority,
        )
        if (
            validated is None
            or boundary_overrides[cid]
            != validated.get("required_given_end_ms")
            or normalized_given_end_authority
            != str(validated.get("registry_authority") or "").strip()
        ):
            raise RecoveryReviewRerunError(
                f"RECOVERY_RERUN_GIVEN_END_AUTHORITY_MISMATCH:{cid}"
            )
    return boundary_overrides, publication_authorities


def _validate_recovery_plan_header(
    *,
    date: str,
    state: dict,
    source_state_sha256: str,
    old_fingerprint: str,
) -> None:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date or "")):
        raise RecoveryReviewRerunError("RECOVERY_RERUN_DATE_INVALID")
    if (
        state.get("run_mode") != "RECOVERY_REVIEW"
        or state.get("upload_allowed") is not False
    ):
        raise RecoveryReviewRerunError(
            "RECOVERY_RERUN_REQUIRES_NO_UPLOAD_REVIEW_STATE"
        )
    if state.get("pending_talk"):
        raise RecoveryReviewRerunError("RECOVERY_RERUN_PENDING_TALK_NOT_EMPTY")
    if not _PIPELINE_FINGERPRINT_RX.fullmatch(source_state_sha256):
        raise RecoveryReviewRerunError("RECOVERY_RERUN_SOURCE_STATE_SHA_INVALID")
    if not _PIPELINE_FINGERPRINT_RX.fullmatch(old_fingerprint):
        raise RecoveryReviewRerunError("RECOVERY_RERUN_OLD_FINGERPRINT_INVALID")


def plan_current_talk_recovery_rerun(
    date: str,
    state: dict,
    *,
    candidate_ids: list[str] | tuple[str, ...],
    expected_source_state_sha256: str,
    expected_old_fingerprint: str,
    expected_new_fingerprint: str | None = None,
    expected_new_fingerprints_by_candidate: dict[str, str] | None = None,
    suppressed_candidate_ids: list[str] | tuple[str, ...] = (),
    replacement_candidate_ids: list[str] | tuple[str, ...] = (),
    user_suppression_authority: str | None = None,
    replacement_selection_authority: str | None = None,
    given_end_ms_by_candidate: dict[str, int] | None = None,
    given_end_authority: str | None = None,
    recovery_publication_authorities_by_candidate: (
        dict[str, dict[str, object]] | None
    ) = None,
    published_cover_carries_by_candidate: dict[str, dict[str, object]] | None = None,
) -> dict:
    """Plan an explicit, hash-bound CURRENT-talk rerun outside cron.

    Old records remain SUPERSEDED; only the normal runner creates replacements.
    """

    _validate_recovery_plan_header(
        date=date,
        state=state,
        source_state_sha256=expected_source_state_sha256,
        old_fingerprint=expected_old_fingerprint,
    )
    requested = [str(candidate_id or "") for candidate_id in candidate_ids]
    suppressed = [
        str(candidate_id or "") for candidate_id in suppressed_candidate_ids
    ]
    replacements = [
        str(candidate_id or "") for candidate_id in replacement_candidate_ids
    ]
    all_requested = [*requested, *suppressed, *replacements]
    if (
        not requested and not replacements
        or len(all_requested) != len(set(all_requested))
        or any(
            _SAFE_CANDIDATE_ID_RX.fullmatch(value) is None
            for value in all_requested
        )
    ):
        raise RecoveryReviewRerunError("RECOVERY_RERUN_ALLOWLIST_INVALID")
    requested_set = set(requested)
    suppressed_set = set(suppressed)
    replacement_set = set(replacements)
    suppression_authority = str(user_suppression_authority or "").strip()
    if suppressed and not suppression_authority:
        raise RecoveryReviewRerunError(
            "RECOVERY_RERUN_SUPPRESSION_AUTHORITY_REQUIRED"
        )
    selection_authority = str(replacement_selection_authority or "").strip()
    if replacements and not selection_authority:
        raise RecoveryReviewRerunError(
            "RECOVERY_RERUN_REPLACEMENT_SELECTION_AUTHORITY_REQUIRED"
    )
    queued_ids = requested_set | replacement_set
    new_fingerprint_by_candidate = _resolve_new_fingerprint_authority(
        queued_ids,
        expected_old_fingerprint=expected_old_fingerprint,
        expected_new_fingerprint=expected_new_fingerprint,
        expected_new_fingerprints_by_candidate=(
            expected_new_fingerprints_by_candidate
        ),
    )
    boundary_overrides, recovery_publication_authorities = (
        _validated_recovery_plan_overrides(
            queued_ids=queued_ids,
            given_end_ms_by_candidate=given_end_ms_by_candidate,
            given_end_authority=given_end_authority,
            recovery_publication_authorities_by_candidate=(
                recovery_publication_authorities_by_candidate
            ),
        )
    )
    published_cover_carries = dict(published_cover_carries_by_candidate or {})
    if published_cover_carries and set(published_cover_carries) != requested_set:
        raise RecoveryReviewRerunError("RECOVERY_RERUN_PUBLISHED_COVER_CARRY_SCOPE_INVALID")
    provider_budget_history = state.get("talk_superseded_attempts") or ()
    picks = state.get("picks")
    if not isinstance(picks, list):
        raise RecoveryReviewRerunError("RECOVERY_RERUN_PICKS_INVALID")
    current_deliveries = [
        row
        for row in picks
        if isinstance(row, dict)
        and row.get("status") in _runner.DELIVERED_TALK_STATUSES
        and row.get("bundle_lifecycle") == "CURRENT"
        and row.get("bundle_compliance") == "COMPLIANT"
    ]
    current_ids = [_candidate_id(row) for row in current_deliveries]
    if (
        len(current_ids) != len(set(current_ids))
        or set(current_ids) != requested_set | suppressed_set
    ):
        raise RecoveryReviewRerunError(
            "RECOVERY_RERUN_ALLOWLIST_MUST_EQUAL_ALL_CURRENT_DELIVERIES"
        )
    by_id = {
        _candidate_id(row): row for row in current_deliveries
    }
    for cid in current_ids:
        if by_id[cid].get("pipeline_fingerprint") != expected_old_fingerprint:
            raise RecoveryReviewRerunError(
                f"RECOVERY_RERUN_OLD_FINGERPRINT_MISMATCH:{cid}"
            )
    backlog = state.get("talk_backlog") or []
    if not isinstance(backlog, list):
        raise RecoveryReviewRerunError("RECOVERY_RERUN_TALK_BACKLOG_INVALID")
    backlog_by_id = {
        _candidate_id(row): row
        for row in backlog
        if isinstance(row, dict) and _candidate_id(row)
    }
    if len(backlog_by_id) != len(
        [row for row in backlog if isinstance(row, dict) and _candidate_id(row)]
    ):
        raise RecoveryReviewRerunError(
            "RECOVERY_RERUN_TALK_BACKLOG_DUPLICATE_ID"
        )
    if any(cid not in backlog_by_id for cid in replacements):
        raise RecoveryReviewRerunError(
            "RECOVERY_RERUN_REPLACEMENT_NOT_IN_BACKLOG"
        )
    queue: list[dict] = []
    superseded: list[dict] = []
    for cid in requested:
        record = by_id[cid]
        expected_new = new_fingerprint_by_candidate[cid]
        try:
            current = _runner.talk_pipeline_fingerprint(cid)
        except ValueError as exc:
            raise RecoveryReviewRerunError(
                f"RECOVERY_RERUN_NEW_FINGERPRINT_UNAVAILABLE:{cid}"
            ) from exc
        if current != expected_new:
            raise RecoveryReviewRerunError(
                f"RECOVERY_RERUN_NEW_FINGERPRINT_MISMATCH:{cid}"
            )

        queue_item = _recovery_queue_item(
            date,
            record,
            candidate_id=cid,
            retry_reason="explicit_recovery_review_pipeline_rerun",
            selected_repair=True,
            given_end_ms=boundary_overrides.get(cid),
            given_end_authority=given_end_authority,
            recovery_publication_authority=(
                recovery_publication_authorities.get(cid)
            ),
            published_cover_carry=published_cover_carries.get(cid),
            provider_budget_history=provider_budget_history,
        )
        queue.append(queue_item)
        archived = copy.deepcopy(record)
        archived["bundle_lifecycle"] = "SUPERSEDED"
        archived["bundle_compliance"] = "STALE_PIPELINE"
        archived["superseded_by"] = expected_new
        archived["retry_reason"] = "explicit_recovery_review_pipeline_rerun"
        archived["source_state_sha256"] = expected_source_state_sha256
        _carry_recovered_provider_budget_ledger_to_archive(queue_item, archived)
        superseded.append(archived)
    ranked_backlog = sorted(
        (
            row
            for row in backlog_by_id.values()
            if isinstance(row.get("selection_scorecard"), dict)
            and row["selection_scorecard"].get("status") == "VALID"
        ),
        key=_scorecard_rank_key,
    )
    baseline_rank_by_id = {
        _candidate_id(row): index
        for index, row in enumerate(ranked_backlog, start=1)
    }
    displaced_pool = [
        row for row in ranked_backlog if _candidate_id(row) not in replacement_set
    ]
    selection_override_events: list[dict[str, object]] = []
    for replacement_ordinal, cid in enumerate(replacements, start=1):
        record = backlog_by_id[cid]
        expected_new = new_fingerprint_by_candidate[cid]
        try:
            current = _runner.talk_pipeline_fingerprint(cid)
        except ValueError as exc:
            raise RecoveryReviewRerunError(
                f"RECOVERY_RERUN_NEW_FINGERPRINT_UNAVAILABLE:{cid}"
            ) from exc
        if current != expected_new:
            raise RecoveryReviewRerunError(
                f"RECOVERY_RERUN_NEW_FINGERPRINT_MISMATCH:{cid}"
            )
        scorecard = record.get("selection_scorecard")
        if not isinstance(scorecard, dict) or scorecard.get("status") != "VALID":
            raise RecoveryReviewRerunError(
                f"RECOVERY_RERUN_REPLACEMENT_SCORECARD_INVALID:{cid}"
            )
        displaced = (
            displaced_pool[replacement_ordinal - 1]
            if replacement_ordinal <= len(displaced_pool)
            else None
        )
        selection_override = {
            "schema_version": "talk-selection-override.v1",
            "event_type": "USER_SELECTION_OVERRIDE",
            "candidate_id": cid,
            "baseline_rank": baseline_rank_by_id.get(cid),
            "selected_slot": len(requested) + replacement_ordinal,
            "displaced_baseline_candidate": (
                _candidate_id(displaced) if displaced is not None else None
            ),
            "authority": selection_authority,
            "scorecard_sha256": _canonical_object_sha256(scorecard),
        }
        queue_item = _recovery_queue_item(
            date,
            record,
            candidate_id=cid,
            retry_reason="explicit_user_selection_override",
            selected_repair=False,
            given_end_ms=boundary_overrides.get(cid),
            given_end_authority=given_end_authority,
            recovery_publication_authority=(
                recovery_publication_authorities.get(cid)
            ),
            provider_budget_history=provider_budget_history,
        )
        queue_item["selection_override"] = selection_override
        queue.append(queue_item)
        selection_override_events.append(selection_override)

    suppressed_records: list[dict] = []
    for cid in suppressed:
        archived = copy.deepcopy(by_id[cid])
        archived["bundle_lifecycle"] = "SUPERSEDED"
        archived["bundle_compliance"] = "USER_SUPPRESSED"
        archived["disposition"] = "EXCLUDE_FROM_DELIVERY"
        archived["reason_codes"] = [
            "REVIEWER_SUPPRESSED_ALREADY_UPLOADED_ELSEWHERE_UNVERIFIED"
        ]
        archived["suppression_authority"] = suppression_authority
        archived["source_state_sha256"] = expected_source_state_sha256
        suppressed_records.append(archived)

    state["picks"] = [
        row
        for row in picks
        if not (
            isinstance(row, dict)
            and _candidate_id(row) in requested_set | suppressed_set
        )
    ]
    state["talk_backlog"] = [
        row
        for row in backlog
        if not (isinstance(row, dict) and _candidate_id(row) in replacement_set)
    ]
    state["pending_talk"] = queue
    state.setdefault("talk_superseded_attempts", []).extend(superseded)
    state.setdefault("talk_user_suppressions", []).extend(suppressed_records)
    state.setdefault("talk_selection_overrides", []).extend(
        selection_override_events
    )
    state["talk_selection_contract"] = {
        "schema_version": "talk-selection-contract.v1",
        "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
        "candidate_ids": [_candidate_id(row) for row in queue],
        "source_state_sha256": expected_source_state_sha256,
        "authority": (
            selection_authority
            or given_end_authority
            or "explicit recovery review rerun allowlist"
        ),
    }
    state["status"] = "recovery_rerun_queued"
    plan = _recovery_rerun_plan(
        date=date, requested=requested, suppressed=suppressed,
        replacements=replacements,
        expected_source_state_sha256=expected_source_state_sha256,
        expected_old_fingerprint=expected_old_fingerprint,
        new_fingerprint_by_candidate=new_fingerprint_by_candidate,
        selection_authority=selection_authority,
        selection_override_events=selection_override_events,
        selection_contract=state["talk_selection_contract"],
        suppression_authority=suppression_authority,
        boundary_overrides=boundary_overrides,
        given_end_authority=given_end_authority,
        recovery_publication_authorities=(
            recovery_publication_authorities
        ),
        queue=queue,
    )
    state["delivery_rerun_plan"] = plan
    return plan


def _recording_session_id(record: dict) -> str:
    return str(record.get("session_id") or "legacy-date-session")


def requeue_recoverable_songs(date: str, state: dict) -> int:
    """Retry non-terminal song BLOCKs when the pipeline changes.

    `UNPROVEN`, missing proof, provider ambiguity, and runner failure mean the
    proof path did not finish; they are not evidence that someone else sang.
    A content fingerprint change earns one new attempt budget.  A transient
    provider failure additionally gets same-fingerprint retries with backoff.
    Completed negative audio/LRC proof and confirmed background playback /
    non-Li-Dousha singing remain terminal across routine fingerprint changes.
    """

    current = _runner.song_pipeline_fingerprint()
    existing_pending = {
        str(item.get("cid") or item.get("candidate_id") or "")
        for item in state.get("pending_song", [])
        if isinstance(item, dict)
    }
    kept: list[dict] = []
    requeued: list[dict] = []
    migrated_legacy_fingerprint = False
    for record in state.get("songs", []):
        if isinstance(record, dict):
            project_terminal_song_disposition(
                record,
                terminal_performer_rejection_codes=(
                    _runner.SONG_TERMINAL_PERFORMER_REJECTION_CODES
                ),
                infra_transient_reason_codes=(
                    _runner.SONG_INFRA_TRANSIENT_REASON_CODES
                ),
                song_infra_retry_cap=_runner.SONG_INFRA_RETRY_CAP,
            )
        if (
            not isinstance(record, dict)
            or record.get("delivered")
            or record.get("verified_delivery_pending_commit") is True
            or record.get("status") not in {"blocked", "failed"}
        ):
            kept.append(record)
            continue
        reasons = {str(code) for code in record.get("reason_codes") or []}
        # Pre-typed early song failures (before the selector ran) used to
        # carry only a free-form error.  Migrate them in place so a source
        # file or BCUT transcript that arrived after the first attempt can
        # recover even when unrelated candidates exhausted the session's
        # ordinary content-attempt cap.
        error = str(record.get("error") or "")
        if error.startswith("window cut failed:"):
            reasons.add("SONG_WINDOW_CUT_FAILED")
        elif error == "empty window srt":
            reasons.add("SONG_SOURCE_TRANSCRIPT_EMPTY")
        if reasons != {str(code) for code in record.get("reason_codes") or []}:
            record["reason_codes"] = sorted(reasons)
        recorded_song_fingerprint = record.get("song_pipeline_fingerprint")
        if not isinstance(recorded_song_fingerprint, str) or not recorded_song_fingerprint:
            # One-time migration from the historical global fingerprint.  Its
            # value cannot distinguish a song-proof change from a talk-only
            # entity change, so stamp the scoped baseline without retrying.
            record["song_pipeline_fingerprint"] = current
            recorded_song_fingerprint = current
            migrated_legacy_fingerprint = True
        changed = recorded_song_fingerprint != current
        retry_count = int(record.get("transient_retry_count") or 0)
        # Prefer the explicitly classified transient emitted by song_lane, but
        # past SONG_INFRA_RETRY_CAP stop letting it outrank a deterministic
        # content verdict — that unbounded veto is what kept 's
        # song_200130_1012 holding the session's only delivery slot forever.
        # Older records have no typed field and keep the reason-code fallback.
        infra_transient = song_infra_transient_is_active(
            record=record,
            reason_codes=reasons,
            infra_transient_reason_codes=_runner.SONG_INFRA_TRANSIENT_REASON_CODES,
            retry_cap=_runner.SONG_INFRA_RETRY_CAP,
            fallback_to_reason_codes=True,
        )
        next_retry_at = record.get("next_retry_at_epoch")
        infra_retry_due = (
            infra_transient
            and (
                not isinstance(next_retry_at, (int, float))
                or isinstance(next_retry_at, bool)
                or time.time() >= float(next_retry_at)
            )
        )
        legacy_transient = (
            bool(
                reasons
                & {
                    "AGY_SOURCE_CONTEXT_RUNNER_FAILED",
                    "PRODUCE_UNEXPECTED_EXCEPTION",
                    "SONG_AUDIO_LRC_ALIGNMENT_INVALID",
                    "SONG_DELIVERY_RECOVERY_AUTHORITY_MISSING",
                }
            )
            and retry_count < 1
        )
        transient = infra_retry_due or legacy_transient
        session_id = _recording_session_id(record)
        lifetime_attempts = sum(
            1
            for attempt in state.get("songs", []) + state.get("song_superseded_attempts", [])
            if isinstance(attempt, dict) and _recording_session_id(attempt) == session_id
        )
        content_change_retry = changed and lifetime_attempts < _runner.SONG_LIFETIME_ATTEMPT_CAP
        cid = str(record.get("candidate_id") or "")
        if not cid or cid in existing_pending or not (content_change_retry or transient):
            kept.append(record)
            continue

        segment_name = Path(str(record.get("segment") or record.get("segment_path") or "")).name
        segment = _runner.REC_ROOT / date / segment_name
        if not segment.is_file():
            kept.append(record)
            continue
        start_ms, end_ms = record.get("start_ms"), record.get("end_ms")
        if not isinstance(start_ms, int) or not isinstance(end_ms, int) or start_ms >= end_ms:
            kept.append(record)
            continue
        anchor_start = int(record.get("anchor_start_ms") or max(0, start_ms + _runner.SONG_WINDOW_PRE_MS))
        anchor_end = int(record.get("anchor_end_ms") or max(anchor_start + 1, end_ms - _runner.SONG_WINDOW_POST_MS))
        seg_dur = _runner.ffprobe_ms(segment)
        item = {
            "cid": cid,
            "segment_path": str(segment),
            "seg_dur_ms": seg_dur,
            "anchor_start_ms": anchor_start,
            "anchor_end_ms": min(seg_dur, anchor_end) if seg_dur else anchor_end,
            "xml": str(xml) if (xml := _runner.find_danmaku_xml(segment)) else None,
            "chat_jsonl": str(chat) if (chat := _runner.find_chat_jsonl(segment)) else None,
            "hook": record.get("hook", ""),
            "preview": record.get("preview", ""),
            "danmaku": int(record.get("danmaku") or 0),
            # Visual title evidence is a first-class song identity hint.  A
            # retry that drops it is weaker than the failed attempt and can
            # repeat the same LRC ambiguity forever (for example 群青 variants
            # or a wide frame window that attached the next song title).  维护者
            # 2026-08-10 起同理带走音频已证出的命名权威：不带＝每次重试都退回 BCUT 错名重检索。
            "lane": record.get("discovery_lane") or record.get("lane"),
            **song_name_authority.carry_song_identity_evidence(record),
            "transient_retry_count": retry_count + (1 if transient else 0),
            "selected_repair": True,
            "retry_reason": (
                "pipeline_fingerprint_changed"
                if content_change_retry
                else "transient_infrastructure_failure"
                if infra_retry_due
                else "transient_source_context_failure"
            ),
            "session_id": session_id,
            "resume_full_source": bool(
                "AGY_SOURCE_CONTEXT_RUNNER_FAILED" in reasons
                and isinstance(record.get("full_source_retry"), dict)
            ),
        }
        requeued.append(item)
        existing_pending.add(cid)
        state.setdefault("song_superseded_attempts", []).append(
            {
                "candidate_id": cid,
                "status": record.get("status"),
                "reason_codes": list(record.get("reason_codes") or []),
                "pipeline_fingerprint": record.get("pipeline_fingerprint"),
                "song_pipeline_fingerprint": recorded_song_fingerprint,
                "superseded_by": current,
                "retry_reason": item["retry_reason"],
                "session_id": session_id,
            }
        )
        _runner._remember_song_quarantine_interval(state, item)
    state["songs"] = kept
    state.setdefault("pending_song", []).extend(requeued)
    if migrated_legacy_fingerprint:
        state["song_pipeline_fingerprint_baseline"] = current
    return len(requeued)


def _song_delivery_recovery_authority(
    *,
    date: str,
    outer_candidate_id: str,
    summary_path: object,
    summary_sha256: object,
    source_candidate_id: object,
    title: object,
) -> dict | None:
    """Build the exact state envelope consumed by packaging-only recovery."""

    if not (
        re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date or ""))
        and re.fullmatch(r"[A-Za-z0-9_-]{1,96}", str(outer_candidate_id or ""))
        and isinstance(summary_path, str)
        and isinstance(summary_sha256, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", summary_sha256)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,96}", str(source_candidate_id or ""))
        and isinstance(title, str)
        and bool(title.strip())
    ):
        return None
    return {
        "schema_version": "song-delivery-recovery-authority.v1",
        "date": date,
        "outer_candidate_id": outer_candidate_id,
        "selector_summary_path": summary_path,
        "selector_summary_sha256": summary_sha256,
        "source_candidate_id": str(source_candidate_id),
        "title": title,
        "upload_enabled": False,
    }


def recover_bound_song_deliveries(date: str, state: dict) -> int:
    """Finish a verified song's packaging without recomputing its proof.

    A selector attempt records the exact summary path, hash and inner
    candidate id before delivery packaging begins.  If the process crashes or
    a later packaging-only bug is fixed, the next maintenance tick can replay
    only the deterministic manifest-last commit.  No directory glob or stale
    attempt selection is allowed.
    """

    recovered = 0
    delivered_by_session: dict[str, int] = {}
    for song in state.get("songs", []):
        if not isinstance(song, dict) or not song.get("delivered"):
            continue
        session_id = str(song.get("session_id") or "legacy-date-session")
        delivered_by_session[session_id] = delivered_by_session.get(session_id, 0) + 1
    for record in state.get("songs", []):
        if (
            not isinstance(record, dict)
            or record.get("delivered")
            or record.get("verified_delivery_pending_commit") is not True
            or record.get("status") not in {"blocked", "failed"}
            or "SONG_DELIVERY_ATOMIC_COPY_FAILED"
            not in {str(code) for code in record.get("reason_codes", [])}
        ):
            continue
        session_id = str(record.get("session_id") or "legacy-date-session")
        if delivered_by_session.get(session_id, 0) >= _runner.MAX_SONGS_PER_SESSION:
            continue
        cid = str(record.get("candidate_id") or "")
        summary_value = record.get("selector_summary_path")
        summary_sha256 = record.get("selector_summary_sha256")
        source_candidate_id = str(record.get("selector_record_candidate_id") or "")
        title = record.get("title")
        expected_authority = _runner._song_delivery_recovery_authority(
            date=date,
            outer_candidate_id=cid,
            summary_path=summary_value,
            summary_sha256=summary_sha256,
            source_candidate_id=source_candidate_id,
            title=title,
        )
        if (
            expected_authority is None
            or record.get("song_delivery_recovery_authority") != expected_authority
        ):
            _runner.log(f"song delivery recovery {cid}: state authority envelope missing or drifted")
            continue
        if not (
            re.fullmatch(r"[A-Za-z0-9_-]{1,96}", cid)
            and isinstance(summary_value, str)
            and isinstance(summary_sha256, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,96}", source_candidate_id)
            and isinstance(title, str)
            and title.strip()
            and record.get("rc") == 0
        ):
            continue
        summary_path = Path(summary_value)
        try:
            candidate_root = (_runner.BASE / "out" / date / cid).resolve(strict=True)
            summary_resolved = summary_path.resolve(strict=True)
        except OSError:
            continue
        if (
            summary_path.is_symlink()
            or not summary_resolved.is_relative_to(candidate_root)
            or summary_resolved.name != "summary.json"
            or not _runner._matches_sha256(summary_path, summary_sha256)
        ):
            _runner.log(f"song delivery recovery {cid}: selector summary authority drifted")
            continue
        try:
            summary = _runner._read_json_object(summary_resolved, label="bound selector summary")
        except ValueError as exc:
            _runner.log(f"song delivery recovery {cid}: {exc}")
            continue
        matches = [
            entry
            for entry in summary.get("records", [])
            if isinstance(entry, dict)
            and str(entry.get("candidate_id") or "") == source_candidate_id
        ]
        if len(matches) != 1:
            _runner.log(
                f"song delivery recovery {cid}: bound selector record is not unique "
                f"({len(matches)} match(es))"
            )
            continue
        try:
            delivery_update = _runner._commit_verified_song_package(
                date=date,
                delivery_candidate_id=cid,
                summary_record=matches[0],
                title=title,
                selector_rc=0,
                summary_authority_root=summary_resolved.parent,
            )
        except (OSError, _runner.SongDeliveryError, ValueError) as exc:
            _runner.log(
                f"song delivery recovery {cid}: deterministic packaging refused: "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        record.update(delivery_update)
        record["reason_codes"] = [
            str(code)
            for code in record.get("reason_codes", [])
            if str(code) != "SONG_DELIVERY_ATOMIC_COPY_FAILED"
        ]
        record.pop("delivery_error", None)
        record.pop("verified_delivery_pending_commit", None)
        record["status"] = "review_ready"
        record["delivery_recovered_without_selector_rerun"] = True
        record["delivery_recovered_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        recovered += 1
        delivered_by_session[session_id] = delivered_by_session.get(session_id, 0) + 1
        _runner.log(f"song delivery recovery {cid}: committed verified package without selector rerun")
    return recovered


def bind_song_delivery_recovery_authority(
    date: str,
    state: dict,
    *,
    candidate_id: str,
    summary_path: Path,
) -> bool:
    """Explicitly bind a pre-fix verified attempt for deterministic recovery.

    Older runner versions did not persist selector-summary authority before
    packaging.  This migration never searches attempt directories: an operator
    must supply the exact summary path.  The path, bytes, unique inner record,
    complete-song proof, host identity and canonical LRC title are all verified
    before state gains the three fields consumed by
    ``recover_bound_song_deliveries``.
    """

    matches = [
        record
        for record in state.get("songs", [])
        if isinstance(record, dict)
        and str(record.get("candidate_id") or "") == candidate_id
    ]
    if len(matches) != 1:
        raise _runner.SongDeliveryError(
            f"song recovery backfill requires one state record, found {len(matches)}"
        )
    state_record = matches[0]
    if (
        state_record.get("delivered")
        or state_record.get("status") not in {"blocked", "failed"}
        or state_record.get("rc") != 0
        or "SONG_DELIVERY_ATOMIC_COPY_FAILED"
        not in {str(code) for code in state_record.get("reason_codes", [])}
    ):
        raise _runner.SongDeliveryError("song recovery backfill state is not packaging-failure eligible")
    if summary_path.is_symlink():
        raise _runner.SongDeliveryError("song recovery backfill summary may not be a symlink")
    try:
        candidate_root = (_runner.BASE / "out" / date / candidate_id).resolve(strict=True)
        summary_resolved = summary_path.resolve(strict=True)
    except OSError as exc:
        raise _runner.SongDeliveryError(f"song recovery backfill path is missing: {exc}") from exc
    if (
        summary_resolved.name != "summary.json"
        or not summary_resolved.is_relative_to(candidate_root)
        or not summary_resolved.is_file()
    ):
        raise _runner.SongDeliveryError("song recovery backfill summary escapes the outer candidate")
    summary_sha256 = "sha256:" + _runner._sha256_regular_file(summary_resolved)
    summary = _runner._read_json_object(summary_resolved, label="song recovery backfill summary")
    records = [entry for entry in summary.get("records", []) if isinstance(entry, dict)]
    if len(records) != 1:
        raise _runner.SongDeliveryError(
            f"song recovery backfill requires one selector record, found {len(records)}"
        )
    summary_record = records[0]
    completion = _runner.song_completion_evidence(summary_record)
    if not _runner.song_delivery_ok(
        0,
        _runner.record_is_song(summary_record),
        summary_record.get("reason_codes", []),
        completion,
    ):
        raise _runner.SongDeliveryError("song recovery backfill proof chain is not delivery-ready")
    # 命名权威只认听音频那条链（维护者）；hook 是 BCUT 中文 ASR 的派生物，不是名字。
    verified_name = song_name_authority.extract_audio_song_name_authority(summary_record) or {}
    title = _runner.verified_song_fallback_title(verified_name.get("song_title"))
    if title is None:
        raise _runner.SongDeliveryError("song recovery backfill has no canonical LRC-bound title")
    source_candidate_id = str(summary_record.get("candidate_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", source_candidate_id):
        raise _runner.SongDeliveryError("song recovery backfill inner candidate id is unsafe")

    intended = {
        "selector_summary_path": str(summary_resolved),
        "selector_summary_sha256": summary_sha256,
        "selector_record_candidate_id": source_candidate_id,
    }
    existing = {
        key: state_record.get(key)
        for key in intended
        if state_record.get(key) is not None
    }
    if existing and existing != intended:
        raise _runner.SongDeliveryError("song recovery backfill conflicts with existing state authority")
    changed = (
        any(state_record.get(key) != value for key, value in intended.items())
        or state_record.get("verified_delivery_pending_commit") is not True
    )
    state_record.update(intended)
    state_record["verified_delivery_pending_commit"] = True
    state_record["title"] = title
    recovery_authority = _runner._song_delivery_recovery_authority(
        date=date,
        outer_candidate_id=candidate_id,
        summary_path=str(summary_resolved),
        summary_sha256=summary_sha256,
        source_candidate_id=source_candidate_id,
        title=title,
    )
    if recovery_authority is None:  # defensive: all fields were validated above
        raise _runner.SongDeliveryError("song recovery backfill authority envelope is invalid")
    state_record["song_delivery_recovery_authority"] = recovery_authority
    state_record["selector_summary_authority_backfill"] = {
        "schema_version": "song-selector-summary-authority-backfill.v1",
        "path": str(summary_resolved),
        "sha256": summary_sha256,
        "source_candidate_id": source_candidate_id,
        "title": title,
        "upload_enabled": False,
        "bound_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return changed


def _active_selected_source_fact_recovery_scope(
    date: str,
    state: Mapping[str, object],
    candidate_ids: Collection[str] | None,
) -> tuple[str, str] | None:
    """Return the exact v6 CID/grant only after canonical admission."""

    if candidate_ids is None or isinstance(candidate_ids, (str, bytes)):
        return None
    values = tuple(candidate_ids)
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    try:
        from src.autoslice.operator_processing_scope import (
            SOURCE_FACT_RECOVERY_GRANT_SCHEMA,
            SOURCE_FACT_RECOVERY_INTENT,
            STATE_KEY,
            operator_scope_admission,
            operator_talk_scope,
        )

        block = state.get(STATE_KEY)
        admission = operator_scope_admission(state, date=date)
        talk_scope = operator_talk_scope(state, date=date)
    except Exception:  # noqa: BLE001 - typed recovery must fail closed
        return None
    if not (
        isinstance(block, Mapping)
        and block.get("schema_version") == SOURCE_FACT_RECOVERY_GRANT_SCHEMA
        and block.get("intent") == SOURCE_FACT_RECOVERY_INTENT
        and block.get("upload_allowed") is False
        and block.get("candidate_ids") == list(values)
        and admission.admitted
        and admission.candidate_ids == values
        and admission.outstanding_candidate_ids == values
        and talk_scope == values
        and isinstance(admission.grant_id, str)
        and admission.grant_id
    ):
        return None
    return values[0], admission.grant_id


def _active_selected_final_review_recovery_scope(
    date: str,
    state: Mapping[str, object],
    candidate_ids: Collection[str] | None,
) -> tuple[str, str] | None:
    return active_selected_final_review_recovery_scope(
        date, state, candidate_ids
    )

def requeue_recoverable_talks(
    date: str,
    state: dict,
    *,
    candidate_ids: Collection[str] | None = None,
) -> int:
    """Retry undelivered selected talks through the focused leaf module."""

    from src.autoslice.talk_delivery_recovery import (
        requeue_recoverable_talks as _requeue_recoverable_talks,
    )

    return _requeue_recoverable_talks(
        sys.modules[__name__],
        date,
        state,
        candidate_ids=candidate_ids,
    )


def requeue_stale_current_recovery_talks(date: str, state: dict) -> int:
    """Refresh stale successful talks only inside an exact recovery contract.

    Ordinary production packages deliberately remain stable across broad code
    deploys.  A no-upload recovery review is different: its exact candidate set
    is an explicit request to regenerate those packages with the current,
    candidate-specific authorities.  Build the whole replacement transaction
    before mutating state so one missing source, BCUT, or chat binding cannot
    partially supersede the currently reviewable set.
    """

    try:
        contract_ids = _exact_talk_contract_ids(state)
    except ValueError as exc:
        raise RecoveryReviewRerunError(
            "INVALID_EXACT_TALK_SELECTION_CONTRACT"
        ) from exc
    if not contract_ids:
        return 0
    contract_set = set(contract_ids)
    rerun_plan = state.get("delivery_rerun_plan")
    if (
        not isinstance(rerun_plan, dict)
        or rerun_plan.get("schema_version")
        != "recovery-review-talk-rerun-plan.v7"
        or rerun_plan.get("talk_selection_contract")
        != state.get("talk_selection_contract")
    ):
        raise RecoveryReviewRerunError(
            "RECOVERY_REVIEW_V7_PLAN_REQUIRED"
        )
    plan_boundaries, plan_publication_authorities = (
        _validated_recovery_plan_overrides(
            queued_ids=contract_set,
            given_end_ms_by_candidate=rerun_plan.get(
                "given_end_ms_by_candidate"
            ),
            given_end_authority=rerun_plan.get("given_end_authority"),
            recovery_publication_authorities_by_candidate=rerun_plan.get(
                "recovery_publication_authorities_by_candidate"
            ),
        )
    )
    plan_end_authority = str(
        rerun_plan.get("given_end_authority") or ""
    ).strip()
    picks = state.get("picks")
    pending = state.get("pending_talk")
    if not isinstance(picks, list) or not isinstance(pending, list):
        raise RecoveryReviewRerunError(
            "RECOVERY_REVIEW_TALK_STATE_INVALID"
        )
    pending_ids = [
        _candidate_id(row)
        for row in pending
        if isinstance(row, dict) and _candidate_id(row)
    ]
    if len(pending_ids) != len(set(pending_ids)):
        raise RecoveryReviewRerunError(
            "RECOVERY_REVIEW_PENDING_TALK_DUPLICATE"
        )
    pending_set = set(pending_ids)
    for row in pending:
        if not isinstance(row, dict):
            continue
        cid = _candidate_id(row)
        if cid not in contract_set:
            continue
        if (
            row.get("given_end_ms") != plan_boundaries[cid]
            or row.get("given_end_authority") != plan_end_authority
            or row.get("recovery_publication_authority")
            != plan_publication_authorities[cid]
        ):
            raise RecoveryReviewRerunError(
                f"RECOVERY_REVIEW_PENDING_AUTHORITY_DRIFT:{cid}"
            )

    current_rows = [
        row
        for row in picks
        if isinstance(row, dict)
        and row.get("status") in _runner.DELIVERED_TALK_STATUSES
        and row.get("bundle_lifecycle") == "CURRENT"
        and row.get("bundle_compliance") == "COMPLIANT"
    ]
    current_ids = [_candidate_id(row) for row in current_rows]
    if (
        any(_SAFE_CANDIDATE_ID_RX.fullmatch(cid) is None for cid in current_ids)
        or len(current_ids) != len(set(current_ids))
    ):
        raise RecoveryReviewRerunError(
            "RECOVERY_REVIEW_CURRENT_TALK_INVALID"
        )
    if set(current_ids) - contract_set:
        raise RecoveryReviewRerunError(
            "RECOVERY_REVIEW_CURRENT_TALK_OUTSIDE_EXACT_CONTRACT"
        )

    queue: list[dict] = []
    archives: list[dict] = []
    stale_ids: set[str] = set()
    for record in current_rows:
        cid = _candidate_id(record)
        if cid in pending_set:
            continue
        if (
            record.get("given_end_ms") != plan_boundaries[cid]
            or record.get("given_end_authority") != plan_end_authority
            or record.get("recovery_publication_authority")
            != plan_publication_authorities[cid]
        ):
            raise RecoveryReviewRerunError(
                f"RECOVERY_REVIEW_CURRENT_AUTHORITY_DRIFT:{cid}"
            )
        try:
            current = _runner.talk_pipeline_fingerprint(cid)
        except ValueError as exc:
            raise RecoveryReviewRerunError(
                f"RECOVERY_REVIEW_TALK_FINGERPRINT_INVALID:{cid}"
            ) from exc
        recorded = str(record.get("pipeline_fingerprint") or "")
        if recorded == current:
            continue
        try:
            item = _recovery_queue_item(
                date,
                record,
                candidate_id=cid,
                retry_reason="current_delivery_pipeline_fingerprint_changed",
                selected_repair=True,
                given_end_ms=record.get("given_end_ms"),
                given_end_authority=record.get("given_end_authority"),
                recovery_publication_authority=record.get(
                    "recovery_publication_authority"
                ),
                provider_budget_history=(
                    state.get("talk_superseded_attempts") or ()
                ),
            )
        except RecoveryReviewRerunError as exc:
            if "CHAT_AUTHORITY_MISSING" not in str(exc):
                raise
            # This row is a delivered CURRENT+COMPLIANT package whose refresh
            # is optional (a broad fingerprint change), and its own evidence
            # is already frozen in the delivered sidecars.  A chat-record
            # infrastructure failure here must not freeze the whole exact
            # queue: keep the CURRENT package, disclose the deferral, and let
            # the candidates that genuinely need a rerun proceed.  The final
            # package audit still replays every contract against the current
            # validators, so a stale package cannot silently pass closure.
            state.setdefault("recovery_requeue_deferrals", []).append(
                {
                    "candidate_id": cid,
                    "reason_code": "STALE_REFRESH_DEFERRED_CHAT_UNREADABLE",
                    "detail": str(exc),
                    "kept_bundle_lifecycle": "CURRENT",
                    "recorded_pipeline_fingerprint": recorded,
                    "current_pipeline_fingerprint": current,
                }
            )
            continue
        archived = copy.deepcopy(record)
        archived["bundle_lifecycle"] = "SUPERSEDED"
        archived["bundle_compliance"] = "STALE_PIPELINE"
        archived["superseded_by"] = current
        archived["retry_reason"] = item["retry_reason"]
        archived["recovery_source_record_sha256"] = item[
            "recovery_source_record_sha256"
        ]
        _carry_recovered_provider_budget_ledger_to_archive(item, archived)
        queue.append(item)
        archives.append(archived)
        stale_ids.add(cid)

    if not queue:
        return 0
    state["picks"] = [
        row
        for row in picks
        if not (
            isinstance(row, dict)
            and _candidate_id(row) in stale_ids
            and row.get("status") in _runner.DELIVERED_TALK_STATUSES
            and row.get("bundle_lifecycle") == "CURRENT"
            and row.get("bundle_compliance") == "COMPLIANT"
        )
    ]
    pending.extend(queue)
    state.setdefault("talk_superseded_attempts", []).extend(archives)
    return len(queue)


def requeue_recoverable_deliveries(
    date: str, state: dict
) -> tuple[int, int, int]:
    """Run the ordered talk/song maintenance transaction for one date."""

    stale_talks = requeue_stale_current_recovery_talks(date, state)
    failed_talks = requeue_recoverable_talks(date, state)
    blocked_songs = requeue_recoverable_songs(date, state)
    return stale_talks, failed_talks, blocked_songs
