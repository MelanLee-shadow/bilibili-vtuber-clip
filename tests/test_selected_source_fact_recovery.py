from __future__ import annotations

import copy
from pathlib import Path

import pytest

import scripts.session_autoslice as runner
from src.autoslice import delivery_recovery
from src.autoslice.selected_source_fact_recovery import (
    BLOCKED,
    CONVERGED,
    OUTSTANDING,
    PICK_TO_QUEUE_TRANSITION,
    QUEUE_TO_PICK_TRANSITION,
    READY_TO_REQUEUE,
    RECOVERY_RECEIPT_FIELD,
    advance_selected_source_fact_recovery_receipt,
    build_selected_source_fact_recovery_receipt,
    inspect_selected_source_fact_recovery,
    is_selected_source_fact_rejection,
    validate_consumed_source_fact_recovery_receipt,
    validate_selected_source_fact_recovery_receipt,
)


CID = "auto_230114_221_307"
GRANT_ID = "2026-08-09-recover-221-source-fact"
OLD = "sha256:" + "1" * 64
NEW = "sha256:" + "2" * 64


def _rejection(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "candidate_id": CID,
        "status": "candidate_rejected",
        "rejected_status": "failed",
        "rc": 1,
        "selected_repair": True,
        "failure_kind": "story_contract",
        "failure_stage": "source_fact_repair",
        "failure_recoverable": False,
        "rejection_reason": "story_contract_unresolved_backfilled",
        "failure_recovery_fingerprint": OLD,
    }
    row.update(overrides)
    return row


def _state(row: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "picks": [_rejection() if row is None else row],
        "pending_talk": [],
        "talk_backlog": [],
        "talk_below_confidence_threshold": [],
        "pending_song": [],
        "song_backlog": [],
        "song_selection_backlog": [],
        "songs": [],
        "song_superseded_attempts": [],
    }


def _v6_grant(date: str) -> dict[str, object]:
    return {
        "schema_version": "operator-processing-scope-grant.v6",
        "grant_id": GRANT_ID,
        "recording_date": date,
        "reason": "retry exact selected source fact rejection",
        "candidate_ids": [CID],
        "user_authorization": {
            "quote": "继续，我连上网了",
            "timestamp": "2026-08-13T20:17:34Z",
        },
        "expires_at": "2099-08-14T04:00:00Z",
        "intent": "RECOVER_NAMED_SELECTED_SOURCE_FACT_REJECTION",
        "upload_allowed": False,
    }


def _queue_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "cid": CID,
        "segment_path": "/recordings/221.mp4",
        "start_ms": 221_000,
        "end_ms": 307_000,
        "selected_repair": True,
    }
    row.update(overrides)
    return row


def _receipt_at_pick(
    pick: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    queued = _queue_row()
    receipt = build_selected_source_fact_recovery_receipt(
        old_row=_rejection(),
        queued_row=queued,
        candidate_id=CID,
        grant_id=GRANT_ID,
        current_fingerprint=NEW,
    )
    receipt = advance_selected_source_fact_recovery_receipt(
        receipt,
        from_row={**queued, RECOVERY_RECEIPT_FIELD: receipt},
        to_row=pick,
        candidate_id=CID,
        grant_id=GRANT_ID,
        transition_kind=QUEUE_TO_PICK_TRANSITION,
    )
    return queued, receipt


def _configure_requeue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, dict[str, object]]:
    date = "2026-08-09"
    rec_root = tmp_path / "recordings"
    segment_dir = rec_root / date
    segment_dir.mkdir(parents=True)
    segment = segment_dir / "221.mp4"
    segment.write_bytes(b"media")
    base = tmp_path / "autoslice"
    cache = base / "cache" / date
    cache.mkdir(parents=True)
    (cache / "221.bcut.srt").write_text(
        "1\n00:00:00,000 --> 00:01:00,000\ntest\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(
        runner, "talk_pipeline_fingerprint", lambda _cid: "sha256:" + "3" * 64
    )
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda kind, candidate_id: (
            NEW
            if (kind, candidate_id) == ("story_contract", CID)
            else pytest.fail("unrelated recovery fingerprint requested")
        ),
    )
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(
        delivery_recovery.historical_recording_duration,
        "resolve",
        lambda *_args: 120_000,
    )
    monkeypatch.setattr(
        delivery_recovery,
        "_structured_chat_binding_for_record",
        lambda *_args, **_kwargs: {},
    )
    old = _rejection(
        segment=segment.name,
        start_ms=0,
        end_ms=60_000,
        hook="test",
        pipeline_fingerprint="sha256:" + "4" * 64,
        talk_repair_retry_count=0,
        talk_transient_retry_count=0,
    )
    state = _state(old)
    state["operator_processing_scope"] = _v6_grant(date)
    return date, state


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "failed"),
        ("rejected_status", "speaker_evidence_insufficient"),
        ("rc", True),
        ("rc", 0),
        ("selected_repair", False),
        ("failure_kind", "speaker_evidence"),
        ("failure_stage", "speaker_evidence"),
        ("failure_recoverable", True),
        ("rejection_reason", "speaker_identity_unresolved_backfilled"),
    ],
)
def test_strict_selected_source_fact_rejection_predicate(field, value):
    assert is_selected_source_fact_rejection(_rejection())
    assert not is_selected_source_fact_rejection(_rejection(**{field: value}))


def test_initial_rejection_is_ready_only_after_fingerprint_change(monkeypatch):
    current = {"value": NEW}
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda kind, candidate_id: (
            current["value"]
            if (kind, candidate_id) == ("story_contract", CID)
            else pytest.fail("inspector requested unrelated authority")
        ),
    )

    state = _state()
    state["talk_superseded_attempts"] = [
        {
            "candidate_id": CID,
            "status": "speaker_evidence_insufficient",
            "failure_kind": "speaker_evidence",
        }
    ]
    ready = inspect_selected_source_fact_recovery(
        state, candidate_id=CID, grant_id=GRANT_ID
    )
    assert (ready.outcome, ready.reason_code) == (
        READY_TO_REQUEUE,
        "SELECTED_SOURCE_FACT_RECOVERY_FINGERPRINT_CHANGED",
    )

    current["value"] = OLD
    converged = inspect_selected_source_fact_recovery(
        _state(), candidate_id=CID, grant_id=GRANT_ID
    )
    assert (converged.outcome, converged.reason_code) == (
        CONVERGED,
        "SELECTED_SOURCE_FACT_RECOVERY_FINGERPRINT_UNCHANGED",
    )


def test_receipt_binds_queue_grant_old_row_fingerprints_and_self_seal(monkeypatch):
    old_row = _rejection()
    queued_row: dict[str, object] = {
        "cid": CID,
        "segment_path": "/recordings/221.mp4",
        "start_ms": 221_000,
        "end_ms": 307_000,
        "selected_repair": True,
    }
    receipt = build_selected_source_fact_recovery_receipt(
        old_row=old_row,
        queued_row=queued_row,
        candidate_id=CID,
        grant_id=GRANT_ID,
        current_fingerprint=NEW,
    )
    queued_row[RECOVERY_RECEIPT_FIELD] = receipt

    assert "queued_row" not in receipt
    assert "queued_row_sha256" not in receipt
    assert receipt["initial_queue_row"] == _queue_row()

    assert validate_selected_source_fact_recovery_receipt(
        receipt,
        queued_row=queued_row,
        candidate_id=CID,
        grant_id=GRANT_ID,
        current_fingerprint=NEW,
    )

    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: NEW,
    )
    state = _state()
    state["picks"] = []
    state["pending_talk"] = [queued_row]
    inspection = inspect_selected_source_fact_recovery(
        state, candidate_id=CID, grant_id=GRANT_ID
    )
    assert (inspection.outcome, inspection.reason_code) == (
        OUTSTANDING,
        "SELECTED_SOURCE_FACT_RECOVERY_QUEUE_OUTSTANDING",
    )

    for drift in (
        "candidate",
        "grant",
        "old_row",
        "sealed_queue",
        "queued_row",
        "recorded",
        "current",
        "speaker_truth",
        "seal",
    ):
        drifted_row = copy.deepcopy(queued_row)
        drifted_receipt = drifted_row[RECOVERY_RECEIPT_FIELD]
        assert isinstance(drifted_receipt, dict)
        if drift == "candidate":
            drifted_receipt["candidate_id"] = "other"
        elif drift == "grant":
            drifted_receipt["operator_scope_grant_id"] = "other"
        elif drift == "old_row":
            drifted_receipt["old_row"]["failure_stage"] = "other"
        elif drift == "sealed_queue":
            drifted_receipt["initial_queue_row"]["segment_path"] = (
                "/recordings/other.mp4"
            )
        elif drift == "queued_row":
            drifted_row["segment_path"] = "/recordings/drifted.mp4"
        elif drift == "recorded":
            drifted_receipt["recorded_failure_recovery_fingerprint"] = NEW
        elif drift == "current":
            drifted_receipt["current_failure_recovery_fingerprint"] = OLD
        elif drift == "speaker_truth":
            drifted_receipt["speaker_truth_authority"] = {"speaker": "李豆沙"}
        else:
            drifted_receipt["receipt_sha256"] = OLD
        assert not validate_selected_source_fact_recovery_receipt(
            drifted_receipt,
            queued_row=drifted_row,
            candidate_id=CID,
            grant_id=GRANT_ID,
            current_fingerprint=NEW,
        )

    assert not validate_consumed_source_fact_recovery_receipt(
        receipt,
        candidate_id=CID,
        grant_id=GRANT_ID,
    )


def test_consumed_source_fact_retry_cannot_refresh_on_later_code_drift(
    monkeypatch,
):
    old_row = _rejection()
    queued_row: dict[str, object] = {
        "cid": CID,
        "segment_path": "/recordings/221.mp4",
        "start_ms": 221_000,
        "end_ms": 307_000,
        "selected_repair": True,
    }
    receipt = build_selected_source_fact_recovery_receipt(
        old_row=old_row,
        queued_row=queued_row,
        candidate_id=CID,
        grant_id=GRANT_ID,
        current_fingerprint=NEW,
    )
    produced = _rejection(failure_recovery_fingerprint=NEW)
    receipt = advance_selected_source_fact_recovery_receipt(
        receipt,
        from_row={**queued_row, RECOVERY_RECEIPT_FIELD: receipt},
        to_row=produced,
        candidate_id=CID,
        grant_id=GRANT_ID,
        transition_kind=QUEUE_TO_PICK_TRANSITION,
    )
    produced[RECOVERY_RECEIPT_FIELD] = receipt
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: "sha256:" + "3" * 64,
    )

    inspection = inspect_selected_source_fact_recovery(
        _state(produced), candidate_id=CID, grant_id=GRANT_ID
    )

    assert (inspection.outcome, inspection.reason_code) == (
        CONVERGED,
        "SELECTED_SOURCE_FACT_RECOVERY_ATTEMPT_CONSUMED",
    )


def test_ordered_lineage_is_rejection_queue_pick_queue_only() -> None:
    queued = _queue_row()
    receipt = build_selected_source_fact_recovery_receipt(
        old_row=_rejection(),
        queued_row=queued,
        candidate_id=CID,
        grant_id=GRANT_ID,
        current_fingerprint=NEW,
    )
    sealed_queue = {**queued, RECOVERY_RECEIPT_FIELD: receipt}

    with pytest.raises(ValueError):
        advance_selected_source_fact_recovery_receipt(
            receipt,
            from_row=sealed_queue,
            to_row=_queue_row(title_attempts=1),
            candidate_id=CID,
            grant_id=GRANT_ID,
            transition_kind=QUEUE_TO_PICK_TRANSITION,
        )

    pick = {
        "candidate_id": CID,
        "status": "failed",
        "failure_recoverable": True,
        "selected_repair": True,
    }
    receipt = advance_selected_source_fact_recovery_receipt(
        receipt,
        from_row=sealed_queue,
        to_row=pick,
        candidate_id=CID,
        grant_id=GRANT_ID,
        transition_kind=QUEUE_TO_PICK_TRANSITION,
    )
    sealed_pick = {**pick, RECOVERY_RECEIPT_FIELD: receipt}

    with pytest.raises(ValueError):
        advance_selected_source_fact_recovery_receipt(
            receipt,
            from_row=sealed_pick,
            to_row={"candidate_id": CID, "status": "failed", "selected_repair": True},
            candidate_id=CID,
            grant_id=GRANT_ID,
            transition_kind=QUEUE_TO_PICK_TRANSITION,
        )

    retry_queue = _queue_row(retry_reason="pipeline_fingerprint_changed")
    receipt = advance_selected_source_fact_recovery_receipt(
        receipt,
        from_row=sealed_pick,
        to_row=retry_queue,
        candidate_id=CID,
        grant_id=GRANT_ID,
        transition_kind=PICK_TO_QUEUE_TRANSITION,
    )
    assert [row["kind"] for row in receipt["transitions"]] == [
        "REJECTION_TO_QUEUE",
        "QUEUE_TO_PICK",
        "PICK_TO_QUEUE",
    ]
    sealed_retry_queue = {
        **retry_queue,
        RECOVERY_RECEIPT_FIELD: receipt,
    }
    assert validate_selected_source_fact_recovery_receipt(
        receipt,
        queued_row=sealed_retry_queue,
        candidate_id=CID,
        grant_id=GRANT_ID,
        current_fingerprint=NEW,
    )
    assert not validate_consumed_source_fact_recovery_receipt(
        receipt,
        candidate_id=CID,
        grant_id=GRANT_ID,
    )


def test_queue_rebound_is_whitelisted_but_cannot_masquerade_as_transition() -> None:
    queued = _queue_row()
    receipt = build_selected_source_fact_recovery_receipt(
        old_row=_rejection(),
        queued_row=queued,
        candidate_id=CID,
        grant_id=GRANT_ID,
        current_fingerprint=NEW,
    )
    rebound = {
        **queued,
        "title_attempts": 1,
        RECOVERY_RECEIPT_FIELD: receipt,
    }
    assert not validate_selected_source_fact_recovery_receipt(
        receipt,
        queued_row=rebound,
        candidate_id=CID,
        grant_id=GRANT_ID,
        current_fingerprint=NEW,
    )
    assert validate_selected_source_fact_recovery_receipt(
        receipt,
        queued_row=rebound,
        candidate_id=CID,
        grant_id=GRANT_ID,
        current_fingerprint=NEW,
        allow_queue_rebound=True,
    )
    with pytest.raises(ValueError):
        advance_selected_source_fact_recovery_receipt(
            receipt,
            from_row=rebound,
            to_row=_queue_row(title_attempts=2),
            candidate_id=CID,
            grant_id=GRANT_ID,
            transition_kind=QUEUE_TO_PICK_TRANSITION,
            allow_queue_rebound=True,
        )
    unauthorized = {**rebound, "arbitrary_authority": "speaker truth"}
    assert not validate_selected_source_fact_recovery_receipt(
        receipt,
        queued_row=unauthorized,
        candidate_id=CID,
        grant_id=GRANT_ID,
        current_fingerprint=NEW,
        allow_queue_rebound=True,
    )


def test_pick_head_requires_receipt_and_consumed_lineage_not_live_fingerprint(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: pytest.fail("consumed pick must not refresh runtime fingerprint"),
    )
    success = {
        "candidate_id": CID,
        "status": "review_ready",
        "selected_repair": True,
    }
    assert inspect_selected_source_fact_recovery(
        _state(success), candidate_id=CID, grant_id=GRANT_ID
    ).outcome == BLOCKED

    _, receipt = _receipt_at_pick(success)
    success[RECOVERY_RECEIPT_FIELD] = receipt
    assert inspect_selected_source_fact_recovery(
        _state(success), candidate_id=CID, grant_id=GRANT_ID
    ).outcome == CONVERGED

    retryable = {
        "candidate_id": CID,
        "status": "failed",
        "failure_recoverable": True,
        "selected_repair": True,
    }
    _, receipt = _receipt_at_pick(retryable)
    retryable[RECOVERY_RECEIPT_FIELD] = receipt
    assert inspect_selected_source_fact_recovery(
        _state(retryable), candidate_id=CID, grant_id=GRANT_ID
    ).outcome == OUTSTANDING

    retryable[RECOVERY_RECEIPT_FIELD]["receipt_sha256"] = OLD
    assert inspect_selected_source_fact_recovery(
        _state(retryable), candidate_id=CID, grant_id=GRANT_ID
    ).outcome == BLOCKED


def test_receipt_bound_media_ready_cover_pending_pick_is_outstanding(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: pytest.fail("consumed cover-pending pick must not refresh runtime code"),
    )
    cover_pending = {
        "candidate_id": CID,
        "status": "media_ready_cover_pending",
        "selected_repair": True,
    }
    _, receipt = _receipt_at_pick(cover_pending)
    cover_pending[RECOVERY_RECEIPT_FIELD] = receipt

    inspection = inspect_selected_source_fact_recovery(
        _state(cover_pending), candidate_id=CID, grant_id=GRANT_ID
    )

    assert (inspection.outcome, inspection.reason_code) == (
        OUTSTANDING,
        "SELECTED_SOURCE_FACT_RECOVERY_COVER_PENDING",
    )


def test_cover_pending_without_exact_receipt_remains_blocked(monkeypatch) -> None:
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: pytest.fail("invalid cover-pending pick must block without runtime I/O"),
    )
    cover_pending = {
        "candidate_id": CID,
        "status": "media_ready_cover_pending",
        "selected_repair": True,
    }
    assert inspect_selected_source_fact_recovery(
        _state(cover_pending), candidate_id=CID, grant_id=GRANT_ID
    ).outcome == BLOCKED

    _, receipt = _receipt_at_pick(cover_pending)
    receipt["receipt_sha256"] = OLD
    cover_pending[RECOVERY_RECEIPT_FIELD] = receipt
    assert inspect_selected_source_fact_recovery(
        _state(cover_pending), candidate_id=CID, grant_id=GRANT_ID
    ).outcome == BLOCKED


def test_v6_user_authorization_is_continue_not_speaker_truth() -> None:
    grant = _v6_grant("2026-08-09")

    assert grant["user_authorization"] == {
        "quote": "继续，我连上网了",
        "timestamp": "2026-08-13T20:17:34Z",
    }
    assert "89221说话人这个是李豆沙" not in str(grant)


def test_arbitrary_queue_or_speaker_truth_cannot_become_source_fact_authority(
    monkeypatch,
):
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: pytest.fail("unsealed queue must block before fingerprint I/O"),
    )
    state = _state()
    state["picks"] = []
    state["pending_talk"] = [{"cid": CID, "selected_repair": True}]
    assert inspect_selected_source_fact_recovery(
        state, candidate_id=CID, grant_id=GRANT_ID
    ).outcome == BLOCKED

    queued_row = {"cid": CID, "selected_repair": True}
    with pytest.raises(TypeError):
        build_selected_source_fact_recovery_receipt(
            old_row=_rejection(),
            queued_row=queued_row,
            candidate_id=CID,
            grant_id=GRANT_ID,
            current_fingerprint=NEW,
            speaker_truth_authority={"speaker": "李豆沙"},
        )


@pytest.mark.parametrize(
    "conflict",
    [
        "duplicate",
        "queued",
        "song",
        "malformed",
        "recorded_invalid",
        "current_invalid",
        "error",
    ],
)
def test_conflicts_malformed_shapes_and_fingerprint_errors_block(monkeypatch, conflict):
    state = _state()
    if conflict == "duplicate":
        state["picks"].append(copy.deepcopy(state["picks"][0]))
    elif conflict == "queued":
        state["pending_talk"] = [{"cid": CID, "selected_repair": True}]
    elif conflict == "song":
        state["pending_song"] = [{"candidate_id": CID}]
    elif conflict == "malformed":
        state["picks"][0]["failure_stage"] = "speaker_evidence"
    elif conflict == "recorded_invalid":
        state["picks"][0]["failure_recovery_fingerprint"] = "sha256:invalid"
    elif conflict == "current_invalid":
        monkeypatch.setattr(
            runner,
            "talk_failure_recovery_fingerprint",
            lambda *_a: "sha256:invalid",
        )
    else:
        monkeypatch.setattr(
            runner,
            "talk_failure_recovery_fingerprint",
            lambda *_a: (_ for _ in ()).throw(OSError("unavailable")),
        )
    if conflict not in {"error", "current_invalid"}:
        monkeypatch.setattr(
            runner,
            "talk_failure_recovery_fingerprint",
            lambda *_a: pytest.fail("conflict must block before fingerprint I/O"),
        )

    inspection = inspect_selected_source_fact_recovery(
        state, candidate_id=CID, grant_id=GRANT_ID
    )

    assert inspection.outcome == BLOCKED


def test_v6_requeue_writes_exact_receipt_and_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    date, state = _configure_requeue(tmp_path, monkeypatch)

    assert delivery_recovery.requeue_recoverable_talks(
        date, state, candidate_ids=(CID,)
    ) == 1

    assert state["picks"] == []
    queued = state["pending_talk"][0]
    receipt = queued[RECOVERY_RECEIPT_FIELD]
    assert queued["retry_reason"] == "selected_source_fact_contract_changed"
    assert validate_selected_source_fact_recovery_receipt(
        receipt,
        queued_row=queued,
        candidate_id=CID,
        grant_id=GRANT_ID,
        current_fingerprint=NEW,
    )
    archived_receipt = state["talk_superseded_attempts"][-1][
        RECOVERY_RECEIPT_FIELD
    ]
    assert archived_receipt == receipt
    assert archived_receipt is not receipt
    assert inspect_selected_source_fact_recovery(
        state, candidate_id=CID, grant_id=GRANT_ID
    ).outcome == OUTSTANDING


def test_selected_source_fact_rejection_never_requeues_without_exact_v6(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    date, state = _configure_requeue(tmp_path, monkeypatch)
    state.pop("operator_processing_scope")
    preimage = copy.deepcopy(state)

    assert delivery_recovery.requeue_recoverable_talks(
        date, state, candidate_ids=(CID,)
    ) == 0
    assert state == preimage
