from __future__ import annotations

import copy
from pathlib import Path

import pytest

import scripts.free_session_autoslice as runner
from src.autoslice import delivery_recovery, produce_dispatch, talk_lane
from src.autoslice.final_review_provider_budget_retry import (
    LEDGER_FIELD,
    LEDGER_SCHEMA,
)
from src.autoslice.selected_final_review_recovery import (
    BLOCKED,
    CONVERGED,
    OUTSTANDING,
    PICK_TO_QUEUE_TRANSITION,
    QUEUE_TO_PICK_TRANSITION,
    READY_TO_REQUEUE,
    RECOVERY_RECEIPT_FIELD,
    advance_selected_final_review_recovery_receipt,
    build_selected_final_review_recovery_receipt,
    inspect_selected_final_review_recovery,
    is_selected_final_review_rejection,
    validate_consumed_final_review_recovery_receipt,
    validate_selected_final_review_recovery_receipt,
)


CID = "auto_230114_1576_1666"
GRANT_ID = "2026-08-09-recover-1576-final-review"
OLD = "sha256:" + "1" * 64
NEW = "sha256:" + "2" * 64


def _rejection(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "candidate_id": CID,
        "status": "candidate_rejected",
        "rejected_status": "failed",
        "rc": 1,
        "selected_repair": True,
        "failure_kind": "subtitle_authority",
        "failure_stage": "final_review_findings",
        "failure_recoverable": False,
        "rejection_reason": "subtitle_authority_unresolved_backfilled",
        "failure_recovery_fingerprint": OLD,
    }
    row.update(overrides)
    return row


def _queue(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "cid": CID,
        "segment_path": "/recordings/1576.mp4",
        "start_ms": 1_576_000,
        "end_ms": 1_666_000,
        "selected_repair": True,
    }
    row.update(overrides)
    return row


def _state(row: dict[str, object], *, collection: str = "picks") -> dict[str, object]:
    state: dict[str, object] = {
        "picks": [],
        "pending_talk": [],
        "talk_backlog": [],
        "talk_below_confidence_threshold": [],
        "talk_superseded_attempts": [],
        "pending_song": [],
        "song_backlog": [],
        "song_selection_backlog": [],
        "songs": [],
        "song_superseded_attempts": [],
    }
    state[collection] = [row]
    return state


def _grant(date: str) -> dict[str, object]:
    return {
        "schema_version": "operator-processing-scope-grant.v7",
        "grant_id": GRANT_ID,
        "recording_date": date,
        "reason": "retry exact selected final review rejection and provider budget continuation",
        "candidate_ids": [CID],
        "user_authorization": {
            "quote": "806，1576内容没问题可以发。",
            "timestamp": "2026-08-13T20:17:34Z",
        },
        "expires_at": "2099-08-14T04:00:00Z",
        "intent": "RECOVER_NAMED_SELECTED_FINAL_REVIEW_REJECTION",
        "upload_allowed": False,
    }


def _configure_requeue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, dict[str, object]]:
    date = "2026-08-09"
    rec_root = tmp_path / "recordings"
    segment_dir = rec_root / date
    segment_dir.mkdir(parents=True)
    segment = segment_dir / "1576.mp4"
    segment.write_bytes(b"media")
    base = tmp_path / "autoslice"
    cache = base / "cache" / date
    cache.mkdir(parents=True)
    (cache / "1576.bcut.srt").write_text(
        "1\n00:00:00,000 --> 00:01:00,000\ntest\n", encoding="utf-8"
    )
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(
        runner, "talk_pipeline_fingerprint", lambda _cid: "sha256:" + "3" * 64
    )
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda kind, candidate_id: NEW
        if (kind, candidate_id) == ("subtitle_authority", CID)
        else pytest.fail("unrelated recovery fingerprint requested"),
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
        talk_repair_retry_count=5,
        talk_transient_retry_count=0,
    )
    state = _state(old)
    state["operator_processing_scope"] = _grant(date)
    return date, state


def _retry() -> dict[str, object]:
    basis: dict[str, object] = {
        "schema_version": "final-review-provider-budget-retry.v1",
        "status": "ELIGIBLE",
        "candidate_id": CID,
        "active_finding_count": 4,
        "raw_validated_finding_count": 16,
        "resolved_finding_count": 6,
        "disclosed_finding_count": 6,
        "provider_adjudication_count": 12,
        "provider_adjudication_budget": 12,
        "reviewed_srt_file": f"replacement_recuts/{CID}.recut.srt",
        "reviewed_srt_sha256": "sha256:" + "3" * 64,
        "reviewed_srt_size_bytes": 123,
        "prior_adjudication_sha256s": [
            "sha256:" + f"{index:064x}" for index in range(1, 13)
        ],
        "active_finding_sha256s": [
            "sha256:" + f"{index:064x}" for index in range(21, 25)
        ],
    }
    from src.autoslice.final_review_provider_budget_retry import _canonical_sha256

    basis["retry_fingerprint"] = _canonical_sha256(basis)
    return basis


def _receipt_at_pick(pick: dict[str, object]) -> dict[str, object]:
    queue = _queue()
    receipt = build_selected_final_review_recovery_receipt(
        old_row=_rejection(),
        queued_row=queue,
        candidate_id=CID,
        grant_id=GRANT_ID,
        current_fingerprint=NEW,
    )
    return advance_selected_final_review_recovery_receipt(
        receipt,
        from_row={**queue, RECOVERY_RECEIPT_FIELD: receipt},
        to_row=pick,
        candidate_id=CID,
        grant_id=GRANT_ID,
        transition_kind=QUEUE_TO_PICK_TRANSITION,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "failed"),
        ("rejected_status", "review_ready"),
        ("rc", True),
        ("rc", 0),
        ("selected_repair", False),
        ("failure_kind", "story_contract"),
        ("failure_stage", "final_review_provider_budget"),
        ("failure_recoverable", True),
        ("rejection_reason", "story_contract_unresolved_backfilled"),
    ],
)
def test_exact_selected_final_review_rejection_predicate(field, value):
    assert is_selected_final_review_rejection(_rejection())
    assert not is_selected_final_review_rejection(_rejection(**{field: value}))


def test_initial_rejection_changed_same_and_malformed_are_tri_state(monkeypatch):
    current = {"value": NEW}
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda kind, cid: current["value"]
        if (kind, cid) == ("subtitle_authority", CID)
        else pytest.fail("unrelated fingerprint requested"),
    )
    assert inspect_selected_final_review_recovery(
        _state(_rejection()), candidate_id=CID, grant_id=GRANT_ID
    ).outcome == READY_TO_REQUEUE
    current["value"] = OLD
    assert inspect_selected_final_review_recovery(
        _state(_rejection()), candidate_id=CID, grant_id=GRANT_ID
    ).outcome == CONVERGED
    malformed = _state(_rejection(failure_stage="source_fact_repair"))
    assert inspect_selected_final_review_recovery(
        malformed, candidate_id=CID, grant_id=GRANT_ID
    ).outcome == BLOCKED


def test_receipt_self_seals_queue_pick_queue_and_rejects_tamper():
    queue = _queue()
    receipt = build_selected_final_review_recovery_receipt(
        old_row=_rejection(),
        queued_row=queue,
        candidate_id=CID,
        grant_id=GRANT_ID,
        current_fingerprint=NEW,
    )
    queued = {**queue, RECOVERY_RECEIPT_FIELD: receipt}
    assert validate_selected_final_review_recovery_receipt(
        receipt,
        queued_row=queued,
        candidate_id=CID,
        grant_id=GRANT_ID,
        current_fingerprint=NEW,
    )
    provider = {
        "candidate_id": CID,
        "status": "failed",
        "selected_repair": True,
        "failure_kind": "subtitle_authority",
        "failure_stage": "final_review_provider_budget",
        "failure_recoverable": True,
        "rejection_reason": "subtitle_authority_unresolved_backfilled",
        "failure_fingerprint": "sha256:" + "4" * 64,
        "failure_evidence": {"provider_budget_retry": _retry()},
    }
    pick_receipt = advance_selected_final_review_recovery_receipt(
        receipt,
        from_row=queued,
        to_row=provider,
        candidate_id=CID,
        grant_id=GRANT_ID,
        transition_kind=QUEUE_TO_PICK_TRANSITION,
    )
    pick = {**provider, RECOVERY_RECEIPT_FIELD: pick_receipt}
    assert validate_consumed_final_review_recovery_receipt(
        pick_receipt,
        candidate_id=CID,
        grant_id=GRANT_ID,
        consumed_row=pick,
    )
    next_queue = _queue(retry_reason="final_review_provider_budget")
    queue_receipt = advance_selected_final_review_recovery_receipt(
        pick_receipt,
        from_row=pick,
        to_row=next_queue,
        candidate_id=CID,
        grant_id=GRANT_ID,
        transition_kind=PICK_TO_QUEUE_TRANSITION,
    )
    assert validate_selected_final_review_recovery_receipt(
        queue_receipt,
        queued_row={**next_queue, RECOVERY_RECEIPT_FIELD: queue_receipt},
        candidate_id=CID,
        grant_id=GRANT_ID,
        current_fingerprint=NEW,
    )
    tampered = copy.deepcopy(queue_receipt)
    tampered["current_row"]["start_ms"] = 0
    assert not validate_selected_final_review_recovery_receipt(
        tampered,
        queued_row={**next_queue, RECOVERY_RECEIPT_FIELD: tampered},
        candidate_id=CID,
        grant_id=GRANT_ID,
        current_fingerprint=NEW,
    )


def test_evolved_v7_receipt_reconstructs_one_immutable_initial_prefix():
    from src.autoslice.published_topic_final_review_handoff import (
        reconstruct_initial_final_review_receipt,
    )

    queue = _queue()
    initial = build_selected_final_review_recovery_receipt(
        old_row=_rejection(),
        queued_row=queue,
        candidate_id=CID,
        grant_id=GRANT_ID,
        current_fingerprint=NEW,
    )
    pick = {"candidate_id": CID, "status": "review_ready", "selected_repair": True}
    evolved = advance_selected_final_review_recovery_receipt(
        initial,
        from_row={**queue, RECOVERY_RECEIPT_FIELD: initial},
        to_row=pick,
        candidate_id=CID,
        grant_id=GRANT_ID,
        transition_kind=QUEUE_TO_PICK_TRANSITION,
    )

    assert reconstruct_initial_final_review_receipt(initial) == initial
    assert reconstruct_initial_final_review_receipt(evolved) == initial
    tampered = copy.deepcopy(evolved)
    tampered["transitions"][0]["to_row_sha256"] = "sha256:" + "f" * 64
    assert reconstruct_initial_final_review_receipt(tampered) is None


@pytest.mark.parametrize("status", ["failed", "candidate_rejected"])
def test_provider_budget_token_is_outstanding_then_consumed_terminal(status):
    provider = {
        "candidate_id": CID,
        "status": status,
        "selected_repair": True,
        "failure_kind": "subtitle_authority",
        "failure_stage": "final_review_provider_budget",
        "failure_recoverable": True,
        "failure_fingerprint": "sha256:" + "4" * 64,
        "failure_evidence": {"provider_budget_retry": _retry()},
    }
    receipt = _receipt_at_pick(provider)
    provider[RECOVERY_RECEIPT_FIELD] = receipt
    state = _state(provider)
    assert inspect_selected_final_review_recovery(
        state, candidate_id=CID, grant_id=GRANT_ID
    ).outcome == OUTSTANDING

    consumed = copy.deepcopy(provider)
    consumed[LEDGER_FIELD] = {
        "schema_version": LEDGER_SCHEMA,
        "entries": [
            {
                "candidate_id": CID,
                "retry_fingerprint": _retry()["retry_fingerprint"],
            }
        ],
    }
    receipt = _receipt_at_pick(
        {key: value for key, value in consumed.items() if key != RECOVERY_RECEIPT_FIELD}
    )
    consumed[RECOVERY_RECEIPT_FIELD] = receipt
    assert inspect_selected_final_review_recovery(
        _state(consumed), candidate_id=CID, grant_id=GRANT_ID
    ).outcome == CONVERGED


def test_provider_budget_malformed_or_conflicting_history_blocks():
    provider = {
        "candidate_id": CID,
        "status": "failed",
        "selected_repair": True,
        "failure_kind": "subtitle_authority",
        "failure_stage": "final_review_provider_budget",
        "failure_recoverable": True,
        "failure_fingerprint": "sha256:" + "4" * 64,
        "failure_evidence": {"provider_budget_retry": _retry()},
    }
    provider[RECOVERY_RECEIPT_FIELD] = _receipt_at_pick(provider)
    state = _state(provider)
    state["talk_superseded_attempts"] = [
        {"candidate_id": CID, LEDGER_FIELD: {"schema_version": "bad", "entries": []}}
    ]
    assert inspect_selected_final_review_recovery(
        state, candidate_id=CID, grant_id=GRANT_ID
    ).outcome == BLOCKED


@pytest.mark.parametrize("ledger_location", ["current", "history"])
def test_provider_budget_consumed_ledger_must_match_token(ledger_location):
    provider = {
        "candidate_id": CID,
        "status": "failed",
        "selected_repair": True,
        "failure_kind": "subtitle_authority",
        "failure_stage": "final_review_provider_budget",
        "failure_recoverable": True,
        "failure_fingerprint": "sha256:" + "4" * 64,
        "failure_evidence": {"provider_budget_retry": _retry()},
    }
    wrong_ledger = {
        "schema_version": LEDGER_SCHEMA,
        "entries": [
            {"candidate_id": CID, "retry_fingerprint": "sha256:" + "f" * 64}
        ],
    }
    if ledger_location == "current":
        provider[LEDGER_FIELD] = wrong_ledger
    provider[RECOVERY_RECEIPT_FIELD] = _receipt_at_pick(provider)
    state = _state(provider)
    if ledger_location == "history":
        state["talk_superseded_attempts"] = [
            {"candidate_id": CID, LEDGER_FIELD: wrong_ledger}
        ]
    assert inspect_selected_final_review_recovery(
        state, candidate_id=CID, grant_id=GRANT_ID
    ).outcome == BLOCKED


def test_success_with_provider_budget_claim_blocks():
    row = {
        "candidate_id": CID,
        "status": "review_ready",
        "selected_repair": True,
        "failure_evidence": {"provider_budget_retry": _retry()},
    }
    row[RECOVERY_RECEIPT_FIELD] = _receipt_at_pick(row)
    assert inspect_selected_final_review_recovery(
        _state(row), candidate_id=CID, grant_id=GRANT_ID
    ).outcome == BLOCKED


def test_non_provider_attempt_with_explicit_malformed_ledger_blocks():
    failed = {
        "candidate_id": CID,
        "status": "failed",
        "selected_repair": True,
        "failure_kind": "runtime_prerequisite",
        "failure_recoverable": True,
        LEDGER_FIELD: {"schema_version": "bad", "entries": []},
    }
    failed[RECOVERY_RECEIPT_FIELD] = _receipt_at_pick(
        {key: value for key, value in failed.items() if key != RECOVERY_RECEIPT_FIELD}
    )
    assert inspect_selected_final_review_recovery(
        _state(failed), candidate_id=CID, grant_id=GRANT_ID
    ).outcome == BLOCKED
    duplicate = _state(_rejection())
    duplicate["pending_talk"] = [_queue()]
    assert inspect_selected_final_review_recovery(
        duplicate, candidate_id=CID, grant_id=GRANT_ID
    ).outcome == BLOCKED
    assert inspect_selected_final_review_recovery(
        _state(_queue(), collection="pending_talk"),
        candidate_id=CID,
        grant_id=GRANT_ID,
    ).outcome == BLOCKED


def test_initial_rejection_with_explicit_bad_ledger_blocks_before_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_a: pytest.fail("bad initial ledger must block before fingerprint I/O"),
    )
    for ledger in (
        {"schema_version": LEDGER_SCHEMA, "entries": []},
        {"schema_version": "bad", "entries": []},
        None,
    ):
        state = _state(_rejection(**{LEDGER_FIELD: ledger}))
        assert inspect_selected_final_review_recovery(
            state, candidate_id=CID, grant_id=GRANT_ID
        ).outcome == BLOCKED


@pytest.mark.parametrize(
    "row",
    [
        {
            "candidate_id": CID,
            "selected_repair": True,
            "status": "candidate_rejected",
            "failure_kind": "mystery",
            "failure_recoverable": True,
        },
        {
            "candidate_id": CID,
            "selected_repair": True,
            "status": "failed",
            "failure_kind": "mystery",
            "failure_recoverable": False,
        },
        {
            "candidate_id": CID,
            "selected_repair": True,
            "status": "failed",
            "failure_kind": "subtitle_authority",
            "failure_stage": "future_unknown_stage",
            "failure_recoverable": False,
        },
    ],
)
def test_unknown_recoverable_or_deterministic_failure_shapes_block(row):
    row[RECOVERY_RECEIPT_FIELD] = _receipt_at_pick(row)
    assert inspect_selected_final_review_recovery(
        _state(row), candidate_id=CID, grant_id=GRANT_ID
    ).outcome == BLOCKED


@pytest.mark.parametrize(
    "pick",
    [
        {"status": "review_ready"},
        {
            "status": "candidate_rejected",
            "failure_kind": "subtitle_authority",
            "failure_stage": "final_review_findings",
            "failure_recoverable": False,
        },
    ],
)
def test_consumed_success_or_deterministic_terminal_converges(pick):
    row = {"candidate_id": CID, "selected_repair": True, **pick}
    row[RECOVERY_RECEIPT_FIELD] = _receipt_at_pick(row)
    assert inspect_selected_final_review_recovery(
        _state(row), candidate_id=CID, grant_id=GRANT_ID
    ).outcome == CONVERGED


def test_consumed_cover_and_infrastructure_failure_stay_outstanding():
    for row in (
        {
            "candidate_id": CID,
            "selected_repair": True,
            "status": "media_ready_cover_pending",
        },
        {
            "candidate_id": CID,
            "selected_repair": True,
            "status": "failed",
            "failure_kind": "runtime_prerequisite",
            "failure_stage": "source_media_binding",
            "failure_recoverable": True,
        },
    ):
        row[RECOVERY_RECEIPT_FIELD] = _receipt_at_pick(row)
        assert inspect_selected_final_review_recovery(
            _state(row), candidate_id=CID, grant_id=GRANT_ID
        ).outcome == OUTSTANDING


def test_song_conflict_duplicate_and_missing_receipt_block(monkeypatch):
    monkeypatch.setattr(
        runner, "talk_failure_recovery_fingerprint", lambda *_a: NEW
    )
    state = _state(_rejection())
    state["pending_song"] = [{"candidate_id": CID}]
    assert inspect_selected_final_review_recovery(
        state, candidate_id=CID, grant_id=GRANT_ID
    ).outcome == BLOCKED


def test_v7_initial_drift_requeue_writes_receipt_at_count_six(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    date, state = _configure_requeue(tmp_path, monkeypatch)
    assert delivery_recovery.requeue_recoverable_talks(
        date, state, candidate_ids=(CID,)
    ) == 1
    assert state["picks"] == []
    queued = state["pending_talk"][0]
    assert queued["retry_reason"] == "pipeline_fingerprint_changed"
    assert queued["talk_repair_retry_count"] == 6
    receipt = queued[RECOVERY_RECEIPT_FIELD]
    assert validate_selected_final_review_recovery_receipt(
        receipt,
        queued_row=queued,
        candidate_id=CID,
        grant_id=GRANT_ID,
        current_fingerprint=NEW,
    )
    archived = state["talk_superseded_attempts"][-1][RECOVERY_RECEIPT_FIELD]
    assert archived == receipt
    assert archived is not receipt


def test_v7_provider_budget_second_requeue_consumes_one_shot_without_count_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    date, state = _configure_requeue(tmp_path, monkeypatch)
    non_target = {
        "candidate_id": "auto_non_target",
        "status": "failed",
        "failure_recoverable": True,
        "opaque": {"order": [3, 1, 2]},
    }
    state["picks"].append(copy.deepcopy(non_target))
    state["talk_backlog"] = [{"cid": "auto_backlog", "opaque": [2, 1]}]
    state["pending_song"] = [{"cid": "song_pending", "opaque": {"p": 1}}]
    state["song_backlog"] = [{"cid": "song_backlog", "opaque": ["b", "a"]}]
    state["song_selection_backlog"] = [
        {"cid": "song_selection", "opaque": {"keep": True}}
    ]
    state["songs"] = [{"candidate_id": "song_done", "status": "blocked"}]
    state["song_superseded_attempts"] = [
        {"candidate_id": "song_old", "opaque": "same"}
    ]
    frozen_non_targets = {
        key: copy.deepcopy(state[key])
        for key in (
            "talk_backlog",
            "pending_song",
            "song_backlog",
            "song_selection_backlog",
            "songs",
            "song_superseded_attempts",
        )
    }
    assert delivery_recovery.requeue_recoverable_talks(
        date, state, candidate_ids=(CID,)
    ) == 1
    assert state["picks"] == [non_target]
    assert {key: state[key] for key in frozen_non_targets} == frozen_non_targets
    queued = state["pending_talk"].pop()
    provider = {
        "candidate_id": CID,
        "segment": "1576.mp4",
        "start_ms": 0,
        "end_ms": 60_000,
        "hook": "test",
        "status": "candidate_rejected",
        "rejected_status": "failed",
        "rc": 1,
        "selected_repair": True,
        "failure_kind": "subtitle_authority",
        "failure_stage": "final_review_provider_budget",
        "failure_recoverable": True,
        "rejection_reason": "subtitle_authority_unresolved_backfilled",
        "failure_fingerprint": "sha256:" + "4" * 64,
        "failure_recovery_fingerprint": NEW,
        "talk_repair_retry_count": 6,
        "talk_transient_retry_count": 0,
        "failure_evidence": {"provider_budget_retry": _retry()},
    }
    pick_receipt = advance_selected_final_review_recovery_receipt(
        queued[RECOVERY_RECEIPT_FIELD],
        from_row=queued,
        to_row=provider,
        candidate_id=CID,
        grant_id=GRANT_ID,
        transition_kind=QUEUE_TO_PICK_TRANSITION,
    )
    provider[RECOVERY_RECEIPT_FIELD] = pick_receipt
    state["picks"] = [copy.deepcopy(non_target), provider]

    assert inspect_selected_final_review_recovery(
        state, candidate_id=CID, grant_id=GRANT_ID
    ).outcome == OUTSTANDING
    assert delivery_recovery._active_selected_final_review_recovery_scope(
        date, state, (CID,)
    ) == (CID, GRANT_ID)
    assert delivery_recovery._talk_retry_decision(
        provider,
        cid=CID,
        existing_pending=set(),
        current_recovery=NEW,
        provider_budget_history=state["talk_superseded_attempts"],
    ) is not None
    from src.autoslice import talk_delivery_recovery

    assert talk_delivery_recovery._prepare_recoverable_talk(
        delivery_recovery,
        date=date,
        record=provider,
        cid=CID,
        existing_pending=set(),
        source_fact_scope=None,
        final_review_scope=(CID, GRANT_ID),
        selected_source_fact_retry=False,
        selected_source_fact_lineage_retry=False,
        selected_final_review_retry=False,
        selected_final_review_lineage_retry=True,
        provider_budget_history=state["talk_superseded_attempts"],
    ) is not None

    assert delivery_recovery.requeue_recoverable_talks(
        date, state, candidate_ids=(CID,)
    ) == 1
    assert state["picks"] == [non_target]
    assert {key: state[key] for key in frozen_non_targets} == frozen_non_targets
    second = state["pending_talk"][0]
    assert second["retry_reason"] == "final_review_provider_budget"
    assert second["talk_repair_retry_count"] == 6
    assert second[LEDGER_FIELD]["schema_version"] == LEDGER_SCHEMA
    assert second[RECOVERY_RECEIPT_FIELD]["transitions"][-1]["kind"] == (
        PICK_TO_QUEUE_TRANSITION
    )
    archived = state["talk_superseded_attempts"][-1]
    assert archived[RECOVERY_RECEIPT_FIELD] == second[RECOVERY_RECEIPT_FIELD]
    assert archived[RECOVERY_RECEIPT_FIELD] is not second[RECOVERY_RECEIPT_FIELD]
    assert archived[LEDGER_FIELD] == second[LEDGER_FIELD]
    assert archived[LEDGER_FIELD] is not second[LEDGER_FIELD]


def test_normal_and_synthetic_producer_results_carry_independent_v7_receipt(
    tmp_path: Path,
) -> None:
    item = _queue()
    receipt = build_selected_final_review_recovery_receipt(
        old_row=_rejection(),
        queued_row=item,
        candidate_id=CID,
        grant_id=GRANT_ID,
        current_fingerprint=NEW,
    )
    item[RECOVERY_RECEIPT_FIELD] = receipt
    normal = talk_lane._carry_talk_recovery_result(
        item, {"candidate_id": CID, "status": "failed"}
    )
    assert normal[RECOVERY_RECEIPT_FIELD] == receipt
    assert normal[RECOVERY_RECEIPT_FIELD] is not receipt

    def crash(_date: str, _item: dict, **_kwargs: object) -> dict:
        raise RuntimeError("synthetic producer crash")

    synthetic = produce_dispatch.produce_batch_windowed(
        "2026-08-08",
        [item],
        crash,
        produce_talk_fn=crash,
        produce_song_fn=lambda _date, _item: {},
        base=tmp_path,
        max_parallel=1,
        log=lambda _message: None,
        talk_pipeline_fingerprint=lambda _candidate_id: "sha256:" + "b" * 64,
        pipeline_fingerprint=lambda: "sha256:" + "c" * 64,
        song_pipeline_fingerprint=lambda: "sha256:" + "d" * 64,
        song_window_pre_ms=1_000,
        song_window_post_ms=1_000,
    )[0]
    assert synthetic[RECOVERY_RECEIPT_FIELD] == receipt
    assert synthetic[RECOVERY_RECEIPT_FIELD] is not receipt
