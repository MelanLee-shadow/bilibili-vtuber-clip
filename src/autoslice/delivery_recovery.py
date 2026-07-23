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
import time
from pathlib import Path

from src.autoslice.runner_proxy import RunnerProxy


_runner = RunnerProxy()

_PIPELINE_FINGERPRINT_RX = re.compile(r"sha256:[0-9a-f]{64}")
_SAFE_CANDIDATE_ID_RX = re.compile(r"[A-Za-z0-9_-]{1,96}")


class RecoveryReviewRerunError(ValueError):
    """The explicit no-upload recovery rerun plan is not safely bound."""


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


def _recovery_queue_item(
    date: str,
    record: dict,
    *,
    candidate_id: str,
    retry_reason: str,
    selected_repair: bool,
    given_end_ms: int | None,
    given_end_authority: str | None,
) -> dict:
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
    seg_dur = _runner.ffprobe_ms(segment)
    if not isinstance(seg_dur, int) or seg_dur <= 0 or end_ms > seg_dur:
        raise RecoveryReviewRerunError(
            f"RECOVERY_RERUN_SOURCE_DURATION_INVALID:{candidate_id}"
        )
    if given_end_ms is not None and (
        isinstance(given_end_ms, bool)
        or not isinstance(given_end_ms, int)
        or given_end_ms <= start_ms
        or given_end_ms > seg_dur
        or abs(given_end_ms - end_ms) > 30_000
        or not str(given_end_authority or "").strip()
    ):
        raise RecoveryReviewRerunError(
            f"RECOVERY_RERUN_GIVEN_END_INVALID:{candidate_id}"
        )
    retry_count = int(record.get("talk_repair_retry_count") or 0)
    item = {
        "cid": candidate_id,
        "segment_path": str(segment),
        "seg_dur_ms": seg_dur,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "xml": str(xml) if (xml := _runner.find_danmaku_xml(segment)) else None,
        "chat_jsonl": (
            str(chat) if (chat := _runner.find_chat_jsonl(segment)) else None
        ),
        "hook": record.get("hook", ""),
        "confidence": record.get("confidence"),
        "selection_scorecard": record.get("selection_scorecard"),
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
        "recovery_source_record_sha256": _canonical_object_sha256(record),
    }
    if given_end_ms is not None:
        item["given_end_ms"] = given_end_ms
        item["given_end_authority"] = str(given_end_authority).strip()
    return item


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
) -> dict:
    """Plan an explicit, hash-bound CURRENT-talk rerun outside cron.

    Old records remain SUPERSEDED; only the normal runner creates replacements.
    """

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
    if not _PIPELINE_FINGERPRINT_RX.fullmatch(
        expected_source_state_sha256
    ):
        raise RecoveryReviewRerunError("RECOVERY_RERUN_SOURCE_STATE_SHA_INVALID")
    if not _PIPELINE_FINGERPRINT_RX.fullmatch(expected_old_fingerprint):
        raise RecoveryReviewRerunError("RECOVERY_RERUN_OLD_FINGERPRINT_INVALID")
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
    boundary_overrides = dict(given_end_ms_by_candidate or {})
    queued_ids = requested_set | replacement_set
    new_fingerprint_by_candidate = _resolve_new_fingerprint_authority(
        queued_ids,
        expected_old_fingerprint=expected_old_fingerprint,
        expected_new_fingerprint=expected_new_fingerprint,
        expected_new_fingerprints_by_candidate=(
            expected_new_fingerprints_by_candidate
        ),
    )
    if set(boundary_overrides) - queued_ids:
        raise RecoveryReviewRerunError(
            "RECOVERY_RERUN_GIVEN_END_CANDIDATE_NOT_QUEUED"
        )
    if boundary_overrides and not str(given_end_authority or "").strip():
        raise RecoveryReviewRerunError(
            "RECOVERY_RERUN_GIVEN_END_AUTHORITY_REQUIRED"
        )
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

        queue.append(
            _recovery_queue_item(
                date,
                record,
                candidate_id=cid,
                retry_reason="explicit_recovery_review_pipeline_rerun",
                selected_repair=True,
                given_end_ms=boundary_overrides.get(cid),
                given_end_authority=given_end_authority,
            )
        )
        archived = copy.deepcopy(record)
        archived["bundle_lifecycle"] = "SUPERSEDED"
        archived["bundle_compliance"] = "STALE_PIPELINE"
        archived["superseded_by"] = expected_new
        archived["retry_reason"] = "explicit_recovery_review_pipeline_rerun"
        archived["source_state_sha256"] = expected_source_state_sha256
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
            "IVAN_SUPPRESSED_ALREADY_UPLOADED_ELSEWHERE_UNVERIFIED"
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
    state["status"] = "recovery_rerun_queued"
    unique_new_fingerprints = set(new_fingerprint_by_candidate.values())
    plan = {
        "schema_version": "recovery-review-talk-rerun-plan.v4",
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
        "user_suppression_authority": (
            suppression_authority if suppressed else None
        ),
        "given_end_ms_by_candidate": boundary_overrides,
        "given_end_authority": (
            str(given_end_authority).strip() if boundary_overrides else None
        ),
        "queued_count": len(queue),
    }
    state["delivery_rerun_plan"] = plan
    return plan


def _recording_session_id(record: dict) -> str:
    return str(record.get("session_id") or "legacy-date-session")


def requeue_recoverable_songs(date: str, state: dict) -> int:
    """Retry non-terminal song BLOCKs when the pipeline changes.

    `UNPROVEN`, missing proof, provider ambiguity, and runner failure mean the
    proof path did not finish; they are not evidence that someone else sang.
    A content fingerprint change earns one new attempt budget.  A transient
    AGY source-context failure additionally gets one same-fingerprint retry.
    Confirmed background playback / non-Li-Dousha singing remains terminal.
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
        if (
            not isinstance(record, dict)
            or record.get("delivered")
            or record.get("verified_delivery_pending_commit") is True
            or record.get("status") not in {"blocked", "failed"}
        ):
            kept.append(record)
            continue
        reasons = {str(code) for code in record.get("reason_codes") or []}
        if reasons & _runner.SONG_TERMINAL_PERFORMER_REJECTION_CODES:
            kept.append(record)
            continue
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
        infra_transient = bool(reasons & _runner.SONG_INFRA_TRANSIENT_REASON_CODES)
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
            # or a wide frame window that attached the next song title).
            "lane": record.get("discovery_lane") or record.get("lane"),
            "title_hint": record.get("title_hint"),
            "visual_song_evidence": record.get("visual_song_evidence"),
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
    job = summary_record.get("source_context_job")
    boundary = job.get("song_boundary") if isinstance(job, dict) else None
    canonical_song_title = boundary.get("song_title") if isinstance(boundary, dict) else None
    title = _runner.verified_song_fallback_title(canonical_song_title, state_record.get("hook"))
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


def requeue_recoverable_talks(date: str, state: dict) -> int:
    """Retry undelivered selected talks with bounded generation semantics.

    Boundary failures wake on a relevant pipeline change.  A generic producer
    failure additionally gets one same-fingerprint retry so a transient CPA or
    worker crash cannot permanently lose an already-selected candidate.  All
    retries share one per-candidate lifetime cap.
    """

    existing_pending = {
        str(item.get("cid") or item.get("candidate_id") or "")
        for item in state.get("pending_talk", [])
        if isinstance(item, dict)
    }
    kept: list[dict] = []
    requeued: list[dict] = []
    for record in state.get("picks", []):
        if not isinstance(record, dict) or record.get("status") not in {
            "boundary_unrepairable",
            "speaker_review_required",
            "failed",
        }:
            kept.append(record)
            continue
        cid = str(record.get("candidate_id") or record.get("cid") or "")
        try:
            current = _runner.talk_pipeline_fingerprint(cid)
            current_recovery = _runner.talk_failure_recovery_fingerprint(
                record.get("failure_kind"), cid
            )
        except ValueError:
            kept.append(record)
            continue
        retry_count = int(record.get("talk_repair_retry_count") or 0)
        transient_count = int(record.get("talk_transient_retry_count") or 0)
        recorded_recovery = record.get("failure_recovery_fingerprint") or record.get(
            "pipeline_fingerprint"
        )
        changed = recorded_recovery != current_recovery
        next_retry_at = record.get("next_retry_at_epoch")
        infrastructure_retry = bool(
            record.get("failure_recoverable") is True
            and (
                not isinstance(next_retry_at, (int, float))
                or isinstance(next_retry_at, bool)
                or time.time() >= float(next_retry_at)
            )
        )
        transient = (
            record.get("status") == "failed" and transient_count < 1
        ) or infrastructure_retry
        if (
            not cid
            or cid in existing_pending
            or not (changed or transient)
            or (
                retry_count >= _runner.TALK_REPAIR_LIFETIME_RETRY_CAP
                and not infrastructure_retry
                and not changed
            )
        ):
            kept.append(record)
            continue
        segment_name = Path(str(record.get("segment") or record.get("segment_path") or "")).name
        segment = _runner.REC_ROOT / date / segment_name
        start_ms, end_ms = record.get("start_ms"), record.get("end_ms")
        if (
            not segment_name
            or not segment.is_file()
            or isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or start_ms >= end_ms
        ):
            kept.append(record)
            continue
        seg_dur = _runner.ffprobe_ms(segment)
        item = {
            "cid": cid,
            "segment_path": str(segment),
            "seg_dur_ms": seg_dur,
            "start_ms": start_ms,
            "end_ms": min(seg_dur, end_ms) if seg_dur else end_ms,
            "xml": str(xml) if (xml := _runner.find_danmaku_xml(segment)) else None,
            "chat_jsonl": str(chat) if (chat := _runner.find_chat_jsonl(segment)) else None,
            "hook": record.get("hook", ""),
            "confidence": record.get("confidence"),
            "selection_scorecard": record.get("selection_scorecard"),
            "session_relation_authority": record.get("session_relation_authority"),
            "lane": record.get("lane", ""),
            "preview": record.get("preview", ""),
            "selected_repair": True,
            "talk_repair_retry_count": retry_count + 1,
            "talk_transient_retry_count": transient_count + (1 if transient and not changed else 0),
            "retry_reason": (
                "pipeline_fingerprint_changed"
                if changed
                else "transient_infrastructure_failure"
                if infrastructure_retry
                else "transient_produce_failure"
            ),
            "bcut_srt_path": str(_runner.BASE / "cache" / date / f"{segment.stem}.bcut.srt"),
            "session_id": _recording_session_id(record),
            # 恢复跳切/微剪计划（2026-07-19）：requeue 丢失 merge_gap_removals
            # 会让合并候选退化成整窗 sweep 被 fail-closed 守卫拒绝。
            "filler_proposals": list(record.get("filler_proposals") or []),
            "filler_proposal_srt_sha256": record.get("filler_proposal_srt_sha256"),
            "merge_gap_removals": list(record.get("merge_gap_removals") or []),
        }
        requeued.append(item)
        existing_pending.add(cid)
        state.setdefault("talk_superseded_attempts", []).append(
            {
                "candidate_id": cid,
                "status": record.get("status"),
                "pipeline_fingerprint": record.get("pipeline_fingerprint"),
                "superseded_by": current,
                "failure_recovery_fingerprint": record.get(
                    "failure_recovery_fingerprint"
                ),
                "superseded_recovery_fingerprint": current_recovery,
                "talk_repair_retry_count": retry_count,
                "talk_transient_retry_count": transient_count,
                "retry_reason": item["retry_reason"],
                "failure_kind": record.get("failure_kind"),
                "failure_stage": record.get("failure_stage"),
                "failure_fingerprint": record.get("failure_fingerprint"),
                "session_id": _recording_session_id(record),
            }
        )
    state["picks"] = kept
    state.setdefault("pending_talk", []).extend(requeued)
    return len(requeued)
