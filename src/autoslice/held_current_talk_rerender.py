"""Strict no-upload rerender for one named held CURRENT Talk package.

This is deliberately narrower than the general recovery-review planner.  It
does not create a publication authority, an upload grant, a replacement pick,
or a broad historical refresh.  A caller must already hold an admitted
operator scope for exactly one candidate.  The candidate remains protected by
one exact, commit/deploy-sealed ``hold_pending_review`` registry row while the
ordinary Talk recovery queue rebuilds the review package.

The expensive inspection is also the transaction preflight.  Missing source,
BCUT, structured chat, a changed registry, duplicate active rows, or a second
pipeline change all block before candidate collections are mutated.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from src.autoslice import delivery_recovery, publication_registry
from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthorityError,
    require_repository_asset_authority,
)


OUTSTANDING_CURRENT = "OUTSTANDING_CURRENT"
OUTSTANDING_QUEUED = "OUTSTANDING_QUEUED"
CONVERGED = "CONVERGED"
BLOCKED = "BLOCKED"
RETRY_REASON = "held_current_review_pipeline_rerender"
HOLD_BINDING_SCHEMA = "held-current-publication-hold-binding.v1"
RECEIPT_SCHEMA = "held-current-talk-rerender-receipt.v1"
_FINGERPRINT_RX = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DATE_RX = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_CANDIDATE_RX = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_ACTIVE_COLLECTIONS = (
    "pending_talk",
    "talk_backlog",
    "picks",
    "talk_below_confidence_threshold",
    "pending_song",
    "song_backlog",
    "song_selection_backlog",
    "songs",
)
_TALK_QUEUE_COLLECTIONS = frozenset({"pending_talk", "talk_backlog"})


class HeldCurrentTalkRerenderError(ValueError):
    """The named rerender ceased to be safe after tick-entry admission."""


@dataclass(frozen=True, slots=True)
class HeldCurrentTalkRerenderInspection:
    outcome: str
    reason_code: str
    candidate_id: str
    recorded_fingerprint: str | None = None
    current_fingerprint: str | None = None
    queue_item: dict[str, object] | None = None
    archived_record: dict[str, object] | None = None
    hold_binding: dict[str, object] | None = None


def _blocked(candidate_id: str, reason_code: str) -> HeldCurrentTalkRerenderInspection:
    return HeldCurrentTalkRerenderInspection(BLOCKED, reason_code, candidate_id)


def _candidate_id(row: object) -> str:
    if not isinstance(row, Mapping):
        return ""
    return str(row.get("candidate_id") or row.get("cid") or "").strip()


def _committed_hold_binding(*, date: str, candidate_id: str) -> dict[str, object]:
    """Bind one exact hold row to HEAD or the deployed authority manifest."""

    root = Path(delivery_recovery._runner.REPO_ROOT).resolve(strict=True)
    path = Path(publication_registry.DEFAULT_REGISTRY_PATH)
    if path.is_symlink() or not path.is_file():
        raise HeldCurrentTalkRerenderError("HELD_CURRENT_REGISTRY_UNAVAILABLE")
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise HeldCurrentTalkRerenderError(
            "HELD_CURRENT_REGISTRY_OUTSIDE_REPOSITORY"
        ) from exc
    payload = resolved.read_bytes()
    try:
        authority = require_repository_asset_authority(
            repo_root=root,
            relative_path=relative,
            observed_bytes=payload,
        )
    except RepositoryAssetAuthorityError as exc:
        raise HeldCurrentTalkRerenderError(
            f"HELD_CURRENT_REGISTRY_UNSEALED:{exc}"
        ) from exc
    try:
        registry = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HeldCurrentTalkRerenderError(
            "HELD_CURRENT_REGISTRY_INVALID_JSON"
        ) from exc
    if (
        not isinstance(registry, Mapping)
        or registry.get("schema_version") != publication_registry.REGISTRY_SCHEMA
        or not isinstance(registry.get("entries"), list)
    ):
        raise HeldCurrentTalkRerenderError("HELD_CURRENT_REGISTRY_INVALID_SCHEMA")
    matches: list[Mapping[str, object]] = []
    for row in registry["entries"]:
        if (
            not isinstance(row, Mapping)
            or not str(row.get("candidate_id") or "").strip()
            or row.get("status")
            not in {"published", "hold_pending_review", "released_for_upload"}
            or (
                row.get("status") == "published"
                and not str(row.get("bvid") or "").strip()
            )
        ):
            raise HeldCurrentTalkRerenderError("HELD_CURRENT_REGISTRY_INVALID_ROW")
        if (
            str(row.get("candidate_id") or "") == candidate_id
            and str(row.get("recording_date") or "") == date
        ):
            matches.append(row)
    if len(matches) != 1:
        raise HeldCurrentTalkRerenderError(
            f"HELD_CURRENT_REGISTRY_ROW_NOT_UNIQUE:{len(matches)}"
        )
    if matches[0].get("status") != "hold_pending_review":
        raise HeldCurrentTalkRerenderError(
            f"HELD_CURRENT_REGISTRY_NOT_HELD:{matches[0].get('status')}"
        )
    return {
        "schema_version": HOLD_BINDING_SCHEMA,
        "candidate_id": candidate_id,
        "recording_date": date,
        "status": "hold_pending_review",
        "registry_repo_path": relative.as_posix(),
        "registry_sha256": authority.file_sha256,
        "repository_authority_mode": authority.mode,
        "repository_commit": authority.commit,
    }


def _active_match(
    state: Mapping[str, object], candidate_id: str
) -> tuple[str, dict[str, object]]:
    matches: list[tuple[str, dict[str, object]]] = []
    for key in _ACTIVE_COLLECTIONS:
        rows = state.get(key)
        if rows is None:
            continue
        if not isinstance(rows, list):
            raise HeldCurrentTalkRerenderError(
                f"HELD_CURRENT_STATE_COLLECTION_INVALID:{key}"
            )
        matches.extend(
            (key, row)
            for row in rows
            if isinstance(row, dict) and _candidate_id(row) == candidate_id
        )
    if len(matches) != 1:
        raise HeldCurrentTalkRerenderError(
            f"HELD_CURRENT_ACTIVE_ROW_NOT_UNIQUE:{len(matches)}"
        )
    return matches[0]


def _current_fingerprint(candidate_id: str) -> str:
    try:
        current = delivery_recovery._runner.talk_pipeline_fingerprint(candidate_id)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise HeldCurrentTalkRerenderError(
            f"HELD_CURRENT_FINGERPRINT_UNAVAILABLE:{exc}"
        ) from exc
    if not isinstance(current, str) or _FINGERPRINT_RX.fullmatch(current) is None:
        raise HeldCurrentTalkRerenderError("HELD_CURRENT_FINGERPRINT_INVALID")
    return current


def _validate_queued_evidence(
    *, date: str, candidate_id: str, row: dict[str, object]
) -> None:
    segment_path = Path(str(row.get("segment_path") or ""))
    segment = delivery_recovery._runner.REC_ROOT / date / segment_path.name
    bcut = delivery_recovery._runner.BASE / "cache" / date / f"{segment.stem}.bcut.srt"
    start_ms, end_ms = row.get("start_ms"), row.get("end_ms")
    if (
        segment_path != segment
        or segment.is_symlink()
        or not segment.is_file()
        or Path(str(row.get("bcut_srt_path") or "")) != bcut
        or bcut.is_symlink()
        or not bcut.is_file()
        or bcut.stat().st_size <= 0
        or isinstance(start_ms, bool)
        or not isinstance(start_ms, int)
        or isinstance(end_ms, bool)
        or not isinstance(end_ms, int)
        or start_ms >= end_ms
    ):
        raise HeldCurrentTalkRerenderError("HELD_CURRENT_QUEUED_SOURCE_INVALID")
    duration = delivery_recovery._runner.ffprobe_ms(segment)
    if (
        isinstance(duration, bool)
        or not isinstance(duration, int)
        or duration <= 0
        or end_ms > duration
    ):
        raise HeldCurrentTalkRerenderError("HELD_CURRENT_QUEUED_DURATION_INVALID")
    expected_chat = delivery_recovery._structured_chat_binding_for_record(
        segment, row, candidate_id=candidate_id
    )
    _require_exact_chat_binding(expected_chat)
    if any(row.get(key) != value for key, value in expected_chat.items()):
        raise HeldCurrentTalkRerenderError("HELD_CURRENT_QUEUED_CHAT_BINDING_DRIFT")


def _require_exact_chat_binding(binding: Mapping[str, object]) -> None:
    origin = binding.get("chat_origin_epoch_ms")
    offset = binding.get("chat_timeline_offset_ms")
    if (
        binding.get("structured_chat_required") is not True
        or not str(binding.get("chat_binding_status") or "").startswith("BOUND_")
        or not str(binding.get("chat_binding_authority") or "").strip()
        or not str(binding.get("chat_jsonl") or "").strip()
        or _FINGERPRINT_RX.fullmatch(str(binding.get("chat_jsonl_sha256") or ""))
        is None
        or isinstance(origin, bool)
        or not isinstance(origin, int)
        or origin <= 0
        or isinstance(offset, bool)
        or not isinstance(offset, int)
    ):
        raise HeldCurrentTalkRerenderError(
            "HELD_CURRENT_EXACT_CHAT_AUTHORITY_REQUIRED"
        )


def _queued_archive(
    state: Mapping[str, object], *, candidate_id: str, grant_id: str
) -> dict[str, object]:
    rows = state.get("talk_superseded_attempts")
    if not isinstance(rows, list):
        raise HeldCurrentTalkRerenderError("HELD_CURRENT_ARCHIVE_INVALID")
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and _candidate_id(row) == candidate_id
        and row.get("retry_reason") == RETRY_REASON
        and row.get("operator_scope_grant_id") == grant_id
    ]
    if len(matches) != 1:
        raise HeldCurrentTalkRerenderError(
            f"HELD_CURRENT_ARCHIVE_NOT_UNIQUE:{len(matches)}"
        )
    archive = matches[0]
    if (
        archive.get("bundle_lifecycle") != "SUPERSEDED"
        or archive.get("bundle_compliance") != "STALE_PIPELINE"
    ):
        raise HeldCurrentTalkRerenderError("HELD_CURRENT_ARCHIVE_INVALID")
    return archive


def inspect_named_held_current_talk_rerender(
    date: str,
    state: Mapping[str, object],
    *,
    candidate_id: str,
    grant_id: str,
) -> HeldCurrentTalkRerenderInspection:
    """Preflight or revalidate one held-current rerender without mutation."""

    if (
        _DATE_RX.fullmatch(date) is None
        or _CANDIDATE_RX.fullmatch(candidate_id) is None
        or not grant_id.strip()
    ):
        return _blocked(candidate_id, "HELD_CURRENT_REQUEST_INVALID")
    if state.get("upload_allowed") is not False:
        return _blocked(candidate_id, "HELD_CURRENT_NO_UPLOAD_STATE_REQUIRED")
    if state.get("talk_selection_contract") is not None:
        return _blocked(candidate_id, "HELD_CURRENT_EXACT_CONTRACT_CONFLICT")
    try:
        hold_binding = _committed_hold_binding(date=date, candidate_id=candidate_id)
        collection, row = _active_match(state, candidate_id)
        current = _current_fingerprint(candidate_id)
        if collection in _TALK_QUEUE_COLLECTIONS:
            if (
                row.get("selected_repair") is not True
                or row.get("retry_reason") != RETRY_REASON
                or row.get("operator_scope_grant_id") != grant_id
            ):
                raise HeldCurrentTalkRerenderError("HELD_CURRENT_QUEUE_AUTHORITY_INVALID")
            _validate_queued_evidence(date=date, candidate_id=candidate_id, row=row)
            archive = _queued_archive(
                state, candidate_id=candidate_id, grant_id=grant_id
            )
            recorded = str(archive.get("pipeline_fingerprint") or "")
            target = str(archive.get("superseded_by") or "")
            if (
                _FINGERPRINT_RX.fullmatch(recorded) is None
                or _FINGERPRINT_RX.fullmatch(target) is None
                or recorded == target
                or current != target
            ):
                raise HeldCurrentTalkRerenderError(
                    "HELD_CURRENT_QUEUED_FINGERPRINT_DRIFT"
                )
            return HeldCurrentTalkRerenderInspection(
                OUTSTANDING_QUEUED,
                "HELD_CURRENT_RERENDER_QUEUED",
                candidate_id,
                recorded,
                current,
                hold_binding=hold_binding,
            )
        if collection != "picks":
            raise HeldCurrentTalkRerenderError(
                f"HELD_CURRENT_NOT_ACTIVE_TALK:{collection}"
            )
        if (
            row.get("status") != "review_ready"
            or row.get("bundle_lifecycle") != "CURRENT"
            or row.get("bundle_compliance") != "COMPLIANT"
        ):
            raise HeldCurrentTalkRerenderError("HELD_CURRENT_PICK_NOT_REVIEW_READY")
        if (
            row.get("given_title") is not None
            or row.get("recovery_publication_authority") is not None
        ):
            # A hold is an upload prohibition, not a same-BV/title authority.
            # This narrow review path must let the ordinary current title lane
            # run from evidence; it cannot inherit a publication title bypass.
            raise HeldCurrentTalkRerenderError(
                "HELD_CURRENT_PUBLICATION_AUTHORITY_FORBIDDEN"
            )
        recorded = str(row.get("pipeline_fingerprint") or "")
        if _FINGERPRINT_RX.fullmatch(recorded) is None:
            raise HeldCurrentTalkRerenderError(
                "HELD_CURRENT_RECORDED_FINGERPRINT_INVALID"
            )
        if recorded == current:
            return HeldCurrentTalkRerenderInspection(
                CONVERGED,
                "HELD_CURRENT_PIPELINE_ALREADY_CURRENT",
                candidate_id,
                recorded,
                current,
                hold_binding=hold_binding,
            )
        queue_item = delivery_recovery._recovery_queue_item(
            date,
            row,
            candidate_id=candidate_id,
            retry_reason=RETRY_REASON,
            selected_repair=True,
            given_end_ms=row.get("given_end_ms"),
            given_end_authority=row.get("given_end_authority"),
            recovery_publication_authority=None,
            provider_budget_history=(
                state.get("talk_superseded_attempts") or ()
            ),
        )
        _require_exact_chat_binding(queue_item)
        queue_item["operator_scope_grant_id"] = grant_id
        queue_item["publication_hold_binding"] = copy.deepcopy(hold_binding)
        archive = copy.deepcopy(row)
        archive["bundle_lifecycle"] = "SUPERSEDED"
        archive["bundle_compliance"] = "STALE_PIPELINE"
        archive["superseded_by"] = current
        archive["retry_reason"] = RETRY_REASON
        archive["operator_scope_grant_id"] = grant_id
        archive["publication_hold_binding"] = copy.deepcopy(hold_binding)
        archive["recovery_source_record_sha256"] = queue_item[
            "recovery_source_record_sha256"
        ]
        delivery_recovery._carry_recovered_provider_budget_ledger_to_archive(
            queue_item, archive
        )
        return HeldCurrentTalkRerenderInspection(
            OUTSTANDING_CURRENT,
            "HELD_CURRENT_RERENDER_REQUIRED",
            candidate_id,
            recorded,
            current,
            queue_item,
            archive,
            hold_binding,
        )
    except (HeldCurrentTalkRerenderError, delivery_recovery.RecoveryReviewRerunError) as exc:
        return _blocked(candidate_id, str(exc))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _blocked(
            candidate_id,
            f"HELD_CURRENT_PREFLIGHT_ERROR:{type(exc).__name__}:{exc}",
        )


def requeue_named_held_current_talk_for_review(
    date: str,
    state: dict,
    *,
    candidate_ids: Sequence[str],
    grant_id: str,
) -> int:
    """Atomically supersede and queue exactly one eligible held CURRENT Talk."""

    ids = tuple(candidate_ids)
    if len(ids) != 1 or len(set(ids)) != 1:
        raise HeldCurrentTalkRerenderError("HELD_CURRENT_ALLOWLIST_MUST_BE_ONE")
    inspection = inspect_named_held_current_talk_rerender(
        date, state, candidate_id=ids[0], grant_id=grant_id
    )
    if inspection.outcome in {CONVERGED, OUTSTANDING_QUEUED}:
        return 0
    if (
        inspection.outcome != OUTSTANDING_CURRENT
        or inspection.queue_item is None
        or inspection.archived_record is None
        or inspection.hold_binding is None
    ):
        raise HeldCurrentTalkRerenderError(inspection.reason_code)
    picks = state.get("picks")
    pending = state.get("pending_talk")
    archives = state.get("talk_superseded_attempts")
    receipts = state.get("held_current_talk_rerender_receipts")
    if (
        not isinstance(picks, list)
        or (pending is not None and not isinstance(pending, list))
        or (archives is not None and not isinstance(archives, list))
        or (receipts is not None and not isinstance(receipts, list))
    ):
        raise HeldCurrentTalkRerenderError("HELD_CURRENT_TRANSACTION_STATE_INVALID")
    candidate_id = ids[0]
    next_picks = [
        row
        for row in picks
        if not (isinstance(row, dict) and _candidate_id(row) == candidate_id)
    ]
    if len(next_picks) != len(picks) - 1:
        raise HeldCurrentTalkRerenderError("HELD_CURRENT_TRANSACTION_PICK_DRIFT")
    next_pending = list(pending or []) + [copy.deepcopy(inspection.queue_item)]
    next_archives = list(archives or []) + [
        copy.deepcopy(inspection.archived_record)
    ]
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "recording_date": date,
        "candidate_id": candidate_id,
        "operator_scope_grant_id": grant_id,
        "upload_allowed": False,
        "recorded_pipeline_fingerprint": inspection.recorded_fingerprint,
        "target_pipeline_fingerprint": inspection.current_fingerprint,
        "publication_hold_binding": copy.deepcopy(inspection.hold_binding),
        "status": "QUEUED_SELECTED_REPAIR",
    }
    # Candidate collections change only after every source/registry/fingerprint
    # check and every replacement document has succeeded.
    state["picks"] = next_picks
    state["pending_talk"] = next_pending
    state["talk_superseded_attempts"] = next_archives
    state["held_current_talk_rerender_receipts"] = list(receipts or []) + [
        receipt
    ]
    return 1
