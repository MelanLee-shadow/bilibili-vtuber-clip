"""Runner-level persistence contract for the one v6 source-fact recovery."""

from __future__ import annotations

import copy
import json

import pytest

import scripts.session_autoslice as runner
from src.autoslice import runner_state_writeback
from src.autoslice import (
    historical_failed_talk_scope,
    historical_recording_duration,
    selection_rescore,
)
from src.autoslice.selected_source_fact_recovery_persistence import (
    SelectedSourceFactRecoveryPersistenceError,
    capture_active_scope,
    validate_head_for_test,
)
from src.autoslice.operator_processing_scope import (
    SOURCE_FACT_RECOVERY_GRANT_SCHEMA,
    SOURCE_FACT_RECOVERY_INTENT,
)
from src.autoslice.selected_source_fact_recovery import (
    RECOVERY_RECEIPT_FIELD,
    build_selected_source_fact_recovery_receipt,
)


DATE = "2026-08-17"
TARGET = "auto_123655_1613_1676"
OTHER = "auto_unrelated"
CURRENT_FINGERPRINT = "sha256:" + "2" * 64


def _grant() -> dict:
    return {
        "schema_version": SOURCE_FACT_RECOVERY_GRANT_SCHEMA,
        "grant_id": "retry-one-selected-source-fact-rejection",
        "recording_date": DATE,
        "reason": "只重试点名的出处事实失败候选，不处理或发布其他候选。",
        "candidate_ids": [TARGET],
        "user_authorization": {
            "quote": "继续，我连上网了",
            "timestamp": "2026-08-13T22:00:00Z",
        },
        "expires_at": "2099-08-14T06:00:00Z",
        "intent": SOURCE_FACT_RECOVERY_INTENT,
        "upload_allowed": False,
    }


def _rejection(candidate_id: str = TARGET) -> dict:
    return {
        "candidate_id": candidate_id,
        "status": "candidate_rejected",
        "rejected_status": "failed",
        "selected_repair": True,
        "rc": 1,
        "failure_kind": "story_contract",
        "failure_stage": "source_fact_repair",
        "failure_recoverable": False,
        "failure_recovery_fingerprint": "sha256:" + "1" * 64,
        "rejection_reason": "story_contract_unresolved_backfilled",
        "segment": f"{candidate_id}.mp4",
        "start_ms": 10_000,
        "end_ms": 20_000,
        "hook": candidate_id,
    }


def _state() -> dict:
    return {
        "status": "no_delivery",
        "upload_allowed": False,
        "operator_processing_scope": _grant(),
        "picks": [_rejection(), _rejection(OTHER)],
        "pending_talk": [],
        "talk_backlog": [],
        "talk_below_confidence_threshold": [],
        "pending_song": [{"candidate_id": "song_unrelated", "status": "pending"}],
        "song_backlog": [],
        "song_selection_backlog": [],
        "songs": [],
        "song_superseded_attempts": [],
        "unrelated_v3_marker": {"preserve": True},
    }


def _queue_from_rejection(state: dict) -> None:
    old = next(row for row in state["picks"] if row["candidate_id"] == TARGET)
    queue = {
        "cid": TARGET,
        "segment_path": f"/recordings/{TARGET}.mp4",
        "start_ms": 10_000,
        "end_ms": 20_000,
        "hook": TARGET,
        "selected_repair": True,
        "talk_repair_retry_count": 1,
    }
    queue[RECOVERY_RECEIPT_FIELD] = build_selected_source_fact_recovery_receipt(
        old_row=old,
        queued_row=queue,
        candidate_id=TARGET,
        grant_id=_grant()["grant_id"],
        current_fingerprint=CURRENT_FINGERPRINT,
    )
    state["picks"] = [row for row in state["picks"] if row is not old]
    state["pending_talk"].append(queue)


def _install_runner_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path, *, maintain):
    base = tmp_path / "autoslice"
    (base / "state").mkdir(parents=True)
    runner.state_path(DATE)  # preserve the production path function in coverage
    monkeypatch.setattr(runner, "BASE", base)
    (base / "state" / f"{DATE}.json").write_text(
        json.dumps(_state(), ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda kind, candidate_id: CURRENT_FINGERPRINT
        if (kind, candidate_id) == ("story_contract", TARGET)
        else None,
    )
    monkeypatch.setattr(runner, "runtime_health_error", lambda: None)
    monkeypatch.setattr(runner, "recover_finalized_legacy_hls", lambda *_a, **_k: [])
    monkeypatch.setattr(
        runner,
        "audit_finalized_recording_inventory",
        lambda *_a, **_k: {"can_select": True, "issues": []},
    )
    monkeypatch.setattr(
        runner.historical_failed_talk_scope,
        "annotate_sessions",
        lambda *_a, **_k: 0,
    )
    monkeypatch.setattr(runner.historical_failed_talk_scope, "maintain", maintain)
    monkeypatch.setattr(
        runner.historical_failed_talk_scope,
        "work_flags",
        lambda *_a, **_k: (False, False, False),
    )
    monkeypatch.setattr(
        runner,
        "_project_terminal_batch_state",
        lambda *_a, **_k: {"retry_epoch": None},
    )
    monkeypatch.setattr(runner, "write_reports", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "write_alert", lambda *_a, **_k: None)
    return base


def _persisted(base) -> dict:
    return json.loads((base / "state" / f"{DATE}.json").read_text(encoding="utf-8"))


def test_process_date_v6_requeues_then_proves_typed_transition_after_writeback(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A requeue count is insufficient until it has a receipt-sealed disk head."""

    calls = 0

    def maintain(_date, value, **_kwargs):
        nonlocal calls
        calls += 1
        _queue_from_rejection(value)
        return 0, 0, 1, 0, False

    base = _install_runner_fixture(monkeypatch, tmp_path, maintain=maintain)

    runner.process_date(DATE)

    persisted = _persisted(base)
    assert calls == 1
    assert [row["cid"] for row in persisted["pending_talk"]] == [TARGET]
    assert RECOVERY_RECEIPT_FIELD in persisted["pending_talk"][0]
    assert all(row["candidate_id"] != TARGET for row in persisted["picks"])
    assert persisted["unrelated_v3_marker"] == {"preserve": True}
    assert persisted["pending_song"] == [{"candidate_id": "song_unrelated", "status": "pending"}]


def test_real_v6_maintain_builds_the_receipt_that_runner_commits(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Do not merely mock the leaf: exercise the deployed v6 requeue path."""

    state = _state()
    segment = tmp_path / DATE / f"{TARGET}.mp4"
    segment.parent.mkdir(parents=True)
    segment.write_bytes(b"frozen source")
    monkeypatch.setattr(runner, "REC_ROOT", tmp_path)
    monkeypatch.setattr(runner, "talk_pipeline_fingerprint", lambda *_a: "sha256:" + "3" * 64)
    monkeypatch.setattr(
        runner, "historical_recording_duration", historical_recording_duration, raising=False
    )
    monkeypatch.setattr(historical_recording_duration, "resolve", lambda *_a, **_k: 30_000)
    monkeypatch.setattr(
        runner, "_validated_given_end_boundary", lambda **_k: (None, None), raising=False
    )
    monkeypatch.setattr(
        runner, "_validated_recovery_publication", lambda **_k: (None, None), raising=False
    )
    monkeypatch.setattr(runner, "_structured_chat_binding_for_record", lambda *_a, **_k: {}, raising=False)
    monkeypatch.setattr(runner, "apply_reviewed_selection_calibration", lambda *_a: None, raising=False)
    monkeypatch.setattr(runner, "selection_rescore", selection_rescore, raising=False)
    monkeypatch.setattr(selection_rescore, "execute_pending_rescores", lambda *_a, **_k: None)
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda kind, candidate_id: CURRENT_FINGERPRINT
        if (kind, candidate_id) == ("story_contract", TARGET)
        else None,
    )

    assert historical_failed_talk_scope.maintain(
        DATE,
        state,
        automatic_maintenance=True,
        candidate_ids=(TARGET,),
    ) == (0, 0, 1, 0, False)
    assert len(state["pending_talk"]) == 1
    assert RECOVERY_RECEIPT_FIELD in state["pending_talk"][0]


def test_process_date_v6_detects_silent_writeback_before_rc0(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A no-op write seam cannot turn the actual v6 requeue into success."""

    def maintain(_date, value, **_kwargs):
        _queue_from_rejection(value)
        return 0, 0, 1, 0, False

    base = _install_runner_fixture(monkeypatch, tmp_path, maintain=maintain)
    monkeypatch.setattr(runner, "write_state", lambda *_a, **_k: None)

    with pytest.raises(
        SelectedSourceFactRecoveryPersistenceError,
        match="PERSISTENCE_QUEUE_HEAD_INVALID",
    ):
        runner.process_date(DATE)

    persisted = _persisted(base)
    assert persisted["pending_talk"] == []
    assert persisted["picks"][0]["candidate_id"] == TARGET


def test_process_date_v6_rejects_same_candidate_disk_wins_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A three-way candidate conflict cannot be acknowledged as a v6 requeue."""

    captured: dict[str, object] = {}

    def maintain(_date, value, **_kwargs):
        _queue_from_rejection(value)
        foreign = _persisted(base)
        target = next(row for row in foreign["picks"] if row["candidate_id"] == TARGET)
        target["foreign_writer_marker"] = "disk-wins"
        (base / "state" / f"{DATE}.json").write_text(
            json.dumps(foreign, ensure_ascii=False), encoding="utf-8"
        )
        captured["state"] = value
        return 0, 0, 1, 0, False

    base = _install_runner_fixture(monkeypatch, tmp_path, maintain=maintain)
    messages: list[str] = []
    monkeypatch.setattr(runner, "log", messages.append)

    with pytest.raises(
        SelectedSourceFactRecoveryPersistenceError,
        match="PERSISTENCE_QUEUE_HEAD_INVALID",
    ):
        runner.process_date(DATE)

    live = captured["state"]
    assert isinstance(live, runner_state_writeback.TrackedState)
    assert live["pending_talk"] == []
    assert next(
        row for row in live["picks"] if row["candidate_id"] == TARGET
    )["foreign_writer_marker"] == "disk-wins"
    persisted = _persisted(base)
    assert persisted["pending_talk"] == []
    assert next(
        row for row in persisted["picks"] if row["candidate_id"] == TARGET
    )["foreign_writer_marker"] == "disk-wins"
    assert any("RUNNER_STATE_WRITEBACK_CONFLICT" in message for message in messages)


def test_process_date_v6_tracked_state_detachment_and_crash_reentry_are_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A reproduced detached-reference risk and post-write crash both reenter safely."""

    first_run = True
    stale_nested: dict[str, object] = {}

    def maintain(_date, value, **_kwargs):
        nonlocal first_run
        if not value["pending_talk"]:
            assert stale_nested["picks"] is not value["picks"]
            _queue_from_rejection(value)
            assert isinstance(value, runner_state_writeback.TrackedState)
            return 0, 0, 1, 0, False
        first_run = False
        return 0, 0, 0, 0, False

    base = _install_runner_fixture(monkeypatch, tmp_path, maintain=maintain)

    def annotate_sessions(_date, value, **_kwargs):
        stale_nested["picks"] = value["picks"]
        value["session_annotation_canary"] = True
        return 1

    monkeypatch.setattr(
        runner.historical_failed_talk_scope, "annotate_sessions", annotate_sessions
    )
    original_write = runner.write_state
    writes = 0

    def crash_after_first_write(date, value):
        nonlocal writes
        writes += 1
        original_write(date, value)
        if writes == 2:
            raise RuntimeError("simulated crash after accepted writeback")

    monkeypatch.setattr(runner, "write_state", crash_after_first_write)
    with pytest.raises(RuntimeError, match="simulated crash"):
        runner.process_date(DATE)
    after_crash = _persisted(base)
    assert len(after_crash["pending_talk"]) == 1
    assert RECOVERY_RECEIPT_FIELD in after_crash["pending_talk"][0]

    monkeypatch.setattr(runner, "write_state", original_write)
    runner.process_date(DATE)
    resumed = _persisted(base)
    assert first_run is False
    assert len(resumed["pending_talk"]) == 1
    assert resumed["pending_talk"] == after_crash["pending_talk"]


@pytest.mark.parametrize("failure", ["receipt", "duplicate", "wrong_candidate", "foreign_drift"])
def test_v6_persistence_validator_rejects_nonunique_or_untrusted_heads(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    state = _state()
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: CURRENT_FINGERPRINT,
    )
    _queue_from_rejection(state)
    if failure == "receipt":
        state["pending_talk"][0][RECOVERY_RECEIPT_FIELD]["grant_id"] = "tampered"
    elif failure == "duplicate":
        state["talk_backlog"] = [copy.deepcopy(state["pending_talk"][0])]
    elif failure == "wrong_candidate":
        state["pending_talk"][0]["cid"] = OTHER
    else:
        state["operator_processing_scope"]["candidate_ids"] = [OTHER]
    with pytest.raises(SelectedSourceFactRecoveryPersistenceError):
        validate_head_for_test(
            state,
            date=DATE,
            candidate_id=TARGET,
            grant_id=_grant()["grant_id"],
        )


def test_v6_persistence_hook_does_not_claim_or_mutate_v3_or_unrelated_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    state["operator_processing_scope"].update(
        {
            "schema_version": "operator-processing-scope-grant.v3",
            "intent": "RERENDER_NAMED_HELD_CURRENT_FOR_REVIEW",
        }
    )
    before = copy.deepcopy(state)
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: CURRENT_FINGERPRINT,
    )

    assert capture_active_scope(
        state, date=DATE, candidate_ids=(TARGET,)
    ) is None
    assert state == before
