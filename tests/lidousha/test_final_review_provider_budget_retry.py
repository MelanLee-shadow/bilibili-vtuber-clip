from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice import delivery_recovery
from src.autoslice.delivery_recovery import _talk_retry_decision
from src.autoslice.final_review_provider_budget_retry import (
    LEDGER_FIELD,
    LEDGER_SCHEMA,
    LEDGER_STATE_INVALID,
    build_provider_budget_retry_evidence,
    consumed_provider_budget_retry_ledger,
    resolve_provider_budget_retry_ledger_history,
    unconsumed_provider_budget_retry,
)


CANDIDATE_ID = "auto_210131_1576_1802"


def _consumed_ledger(
    candidate_id: str = CANDIDATE_ID,
    fingerprint_char: str = "b",
) -> dict[str, object]:
    return {
        "schema_version": LEDGER_SCHEMA,
        "entries": [
            {
                "candidate_id": candidate_id,
                "retry_fingerprint": "sha256:" + fingerprint_char * 64,
            }
        ],
    }


def _recovery_queue_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, dict[str, object]]:
    date = "2026-08-08"
    rec_root = tmp_path / "recordings"
    segment_dir = rec_root / date
    segment_dir.mkdir(parents=True)
    segment = segment_dir / "segment.mp4"
    segment.write_bytes(b"media")
    base = tmp_path / "autoslice"
    cache = base / "cache" / date
    cache.mkdir(parents=True)
    (cache / "segment.bcut.srt").write_text(
        "1\n00:00:00,000 --> 00:01:00,000\ntest\n",
        encoding="utf-8",
    )
    runner_stub = SimpleNamespace(
        REC_ROOT=rec_root,
        BASE=base,
        TALK_REPAIR_LIFETIME_RETRY_CAP=3,
        find_danmaku_xml=lambda _segment: None,
        talk_pipeline_fingerprint=lambda _candidate_id: (
            "sha256:" + "c" * 64
        ),
        talk_failure_recovery_fingerprint=lambda _kind, _candidate_id: (
            "sha256:" + "d" * 64
        ),
    )
    monkeypatch.setattr(delivery_recovery, "_runner", runner_stub)
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
    record: dict[str, object] = {
        "candidate_id": CANDIDATE_ID,
        "segment": segment.name,
        "start_ms": 0,
        "end_ms": 60_000,
        "hook": "test",
        "status": "failed",
        "failure_kind": "producer_error",
        "failure_recoverable": True,
        "pipeline_fingerprint": "sha256:" + "a" * 64,
    }
    return date, record


def _audit_and_srt(tmp_path: Path) -> tuple[dict[str, object], Path]:
    srt_path = (
        tmp_path
        / "replacement_recuts"
        / f"{CANDIDATE_ID}.recut.srt"
    )
    srt_path.parent.mkdir(parents=True)
    srt_bytes = b"1\n00:00:00,000 --> 00:00:02,000\nretry\n"
    srt_path.write_bytes(srt_bytes)
    adjudication = {
        "schema_version": "subtitle-span-adjudication.v1",
        "status": "SKIPPED_BUDGET",
        "repaired": False,
        "provider_adjudication_count": 12,
        "provider_adjudication_budget": 12,
    }
    findings = [
        {
            "cue_index": cue_index,
            "suspect": f"old-{cue_index}",
            "suggestion": f"new-{cue_index}",
            "exact_release_adjudication": dict(adjudication),
        }
        for cue_index in (65, 67)
    ]
    return (
        {
            "schema_version": "final-review-audit.v2",
            "status": "FLAGGED",
            "release_gate": "BLOCK",
            "reason_codes": ["FINAL_REVIEW_UNRESOLVED_FINDINGS"],
            "reviewed_srt_sha256": "sha256:"
            + hashlib.sha256(srt_bytes).hexdigest(),
            "discovery": {
                "status": "COMPLETE",
                "explicit_empty_findings": False,
                "raw_validated_finding_count": 14,
                "resolved_finding_count": 6,
            },
            "validated_finding_count": len(findings),
            "findings": findings,
            "resolved_findings": [
                {
                    "cue_index": cue_index,
                    "status": "RESOLVED",
                    "exact_release_adjudication": {
                        "schema_version": "subtitle-span-adjudication.v1",
                        "status": "OBSERVED",
                        "repaired": False,
                    },
                }
                for cue_index in range(1, 7)
            ],
            "unresolved_findings_disclosed": [
                {
                    "cue_index": cue_index,
                    "status": "DISCLOSED",
                    "exact_release_adjudication": {
                        "schema_version": "subtitle-span-adjudication.v1",
                        "status": "OBSERVED",
                        "repaired": False,
                    },
                }
                for cue_index in range(7, 13)
            ],
            "boundary_semantic_review": {
                "schema_version": "talk-boundary-semantic-review.v1",
                "status": "PASS",
            },
            "correction_mutation_authority": {
                "schema_version": "subtitle-correction-mutation-audit.v1",
                "status": "PASS",
                "failures": [],
            },
        },
        srt_path,
    )


def _build(audit: dict[str, object], srt_path: Path):
    return build_provider_budget_retry_evidence(
        audit,
        candidate_id=CANDIDATE_ID,
        contract_reason_code="FINAL_REVIEW_UNRESOLVED_FINDINGS",
        reviewed_srt_path=srt_path,
    )


def test_budget_retry_evidence_allows_zero_disclosed_rows(
    tmp_path: Path,
) -> None:
    audit, srt_path = _audit_and_srt(tmp_path)
    audit["unresolved_findings_disclosed"] = []
    audit["resolved_findings"] = [
        *audit["resolved_findings"],
        *[
            {
                "cue_index": cue_index,
                "status": "RESOLVED",
                "exact_release_adjudication": {
                    "schema_version": "subtitle-span-adjudication.v1",
                    "status": "OBSERVED",
                    "repaired": False,
                },
            }
            for cue_index in range(7, 13)
        ],
    ]
    audit["discovery"]["resolved_finding_count"] = 12

    retry = _build(audit, srt_path)

    assert retry is not None
    assert retry["disclosed_finding_count"] == 0
    assert retry["resolved_finding_count"] == 12


@pytest.mark.parametrize(
    "malformation",
    (
        "mixed_status",
        "disclosed_status",
        "extra_adjudication_field",
        "provider_count",
        "raw_count",
        "mixed_reason",
        "boundary",
        "correction_authority",
        "srt_tamper",
    ),
)
def test_budget_retry_evidence_rejects_mixed_malformed_or_tampered_inputs(
    tmp_path: Path, malformation: str
) -> None:
    audit, srt_path = _audit_and_srt(tmp_path)
    findings = audit["findings"]
    assert isinstance(findings, list)
    adjudication = findings[0]["exact_release_adjudication"]
    assert isinstance(adjudication, dict)
    if malformation == "mixed_status":
        adjudication["status"] = "OBSERVED"
    elif malformation == "disclosed_status":
        audit["unresolved_findings_disclosed"][0][
            "exact_release_adjudication"
        ]["status"] = "SKIPPED_BUDGET"
    elif malformation == "extra_adjudication_field":
        adjudication["reason"] = "budget exhausted"
    elif malformation == "provider_count":
        adjudication["provider_adjudication_count"] = 11
    elif malformation == "raw_count":
        audit["discovery"]["raw_validated_finding_count"] = 15
    elif malformation == "mixed_reason":
        audit["reason_codes"].append("ANOTHER_UNRESOLVED_REASON")
    elif malformation == "boundary":
        audit["boundary_semantic_review"]["status"] = "BLOCK"
    elif malformation == "correction_authority":
        audit["correction_mutation_authority"]["failures"] = [
            {"reason_code": "UNROUTED"}
        ]
    else:
        srt_path.write_text("tampered\n", encoding="utf-8")

    assert _build(audit, srt_path) is None


def test_budget_retry_ledger_is_candidate_level_single_use_and_tamper_closed(
    tmp_path: Path,
) -> None:
    audit, srt_path = _audit_and_srt(tmp_path)
    retry = _build(audit, srt_path)
    assert retry is not None
    record = {
        "candidate_id": CANDIDATE_ID,
        "status": "failed",
        "failure_kind": "subtitle_authority",
        "failure_stage": "final_review_provider_budget",
        "failure_recoverable": True,
        "failure_fingerprint": "sha256:" + "a" * 64,
        "failure_evidence": {"provider_budget_retry": retry},
    }
    assert unconsumed_provider_budget_retry(
        record, candidate_id=CANDIDATE_ID
    ) == retry

    ledger = consumed_provider_budget_retry_ledger(
        record,
        candidate_id=CANDIDATE_ID,
        retry=retry,
    )
    assert ledger is not None
    record[LEDGER_FIELD] = ledger
    assert unconsumed_provider_budget_retry(
        record, candidate_id=CANDIDATE_ID
    ) is None

    # Even a newly hash-bound finding set does not refresh the candidate-level
    # one-shot allowance.
    audit["findings"][0]["suspect"] = "different-current-blocker"
    second_retry = _build(audit, srt_path)
    assert second_retry is not None
    assert second_retry["retry_fingerprint"] != retry["retry_fingerprint"]
    record["failure_evidence"] = {"provider_budget_retry": second_retry}
    assert unconsumed_provider_budget_retry(
        record, candidate_id=CANDIDATE_ID
    ) is None

    tampered = copy.deepcopy(record)
    tampered[LEDGER_FIELD]["entries"].append(
        copy.deepcopy(tampered[LEDGER_FIELD]["entries"][0])
    )
    assert unconsumed_provider_budget_retry(
        tampered, candidate_id=CANDIDATE_ID
    ) is None


def test_invalid_budget_route_cannot_fall_through_changed_or_transient_retry(
    tmp_path: Path,
) -> None:
    audit, srt_path = _audit_and_srt(tmp_path)
    retry = _build(audit, srt_path)
    assert retry is not None
    retry["provider_adjudication_count"] = 11
    record = {
        "status": "failed",
        "failure_kind": "subtitle_authority",
        "failure_stage": "final_review_provider_budget",
        "failure_recoverable": True,
        "failure_fingerprint": "sha256:" + "a" * 64,
        "failure_recovery_fingerprint": "sha256:old",
        "talk_repair_retry_count": 0,
        "talk_transient_retry_count": 0,
        "failure_evidence": {"provider_budget_retry": retry},
    }

    assert (
        _talk_retry_decision(
            record,
            cid=CANDIDATE_ID,
            existing_pending=set(),
            current_recovery="sha256:new",
        )
        is None
    )


@pytest.mark.parametrize(
    "ledger",
    (
        {
            "schema_version": LEDGER_SCHEMA,
            "entries": [
                {
                    "candidate_id": CANDIDATE_ID,
                    # Missing retry_fingerprint makes this explicit ledger
                    # malformed rather than absent.
                }
            ],
        },
        _consumed_ledger("auto_foreign_candidate"),
    ),
    ids=("malformed", "foreign-candidate"),
)
@pytest.mark.parametrize(
    ("recorded_recovery", "current_recovery", "transient_count"),
    (
        ("sha256:" + "a" * 64, "sha256:" + "b" * 64, 1),
        ("sha256:" + "a" * 64, "sha256:" + "a" * 64, 0),
    ),
    ids=("changed-only", "transient-only"),
)
def test_explicit_invalid_ledger_blocks_unrelated_generic_retry(
    monkeypatch: pytest.MonkeyPatch,
    ledger: dict[str, object],
    recorded_recovery: str,
    current_recovery: str,
    transient_count: int,
) -> None:
    monkeypatch.setattr(
        delivery_recovery,
        "_runner",
        SimpleNamespace(TALK_REPAIR_LIFETIME_RETRY_CAP=3),
    )
    record = {
        "status": "failed",
        "failure_kind": "producer_error",
        "failure_stage": "unrelated_stage",
        "failure_recoverable": True,
        "failure_recovery_fingerprint": recorded_recovery,
        "talk_repair_retry_count": 0,
        "talk_transient_retry_count": transient_count,
        LEDGER_FIELD: ledger,
    }

    assert (
        _talk_retry_decision(
            record,
            cid=CANDIDATE_ID,
            existing_pending=set(),
            current_recovery=current_recovery,
        )
        is None
    )


@pytest.mark.parametrize(
    ("retry_reason", "selected_repair"),
    (
        ("explicit_recovery_review_pipeline_rerun", True),
        ("explicit_user_selection_override", False),
        ("current_delivery_pipeline_fingerprint_changed", True),
    ),
)
def test_recovery_queue_item_preserves_valid_ledger_as_deep_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retry_reason: str,
    selected_repair: bool,
) -> None:
    date, record = _recovery_queue_fixture(tmp_path, monkeypatch)
    ledger = _consumed_ledger()
    record[LEDGER_FIELD] = ledger

    queued = delivery_recovery._recovery_queue_item(
        date,
        record,
        candidate_id=CANDIDATE_ID,
        retry_reason=retry_reason,
        selected_repair=selected_repair,
        given_end_ms=None,
        given_end_authority=None,
        recovery_publication_authority=None,
    )

    assert queued[LEDGER_FIELD] == ledger
    assert queued[LEDGER_FIELD] is not ledger
    queued_entries = queued[LEDGER_FIELD]["entries"]
    source_entries = ledger["entries"]
    assert queued_entries is not source_entries
    queued_entries[0]["candidate_id"] = "mutated_candidate"
    assert source_entries[0]["candidate_id"] == CANDIDATE_ID


def test_unrelated_generic_retry_preserves_valid_consumed_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    date, record = _recovery_queue_fixture(tmp_path, monkeypatch)
    ledger = _consumed_ledger()
    record.update(
        {
            "failure_recovery_fingerprint": "sha256:" + "a" * 64,
            "talk_repair_retry_count": 0,
            "talk_transient_retry_count": 1,
            LEDGER_FIELD: ledger,
        }
    )
    monkeypatch.setattr(
        delivery_recovery.selection_rescore,
        "execute_pending_rescores",
        lambda *_args, **_kwargs: None,
    )
    state = {"picks": [record], "pending_talk": []}

    assert delivery_recovery.requeue_recoverable_talks(date, state) == 1

    queued = state["pending_talk"][0]
    assert queued["retry_reason"] == "pipeline_fingerprint_changed"
    assert queued["talk_repair_retry_count"] == 1
    assert queued[LEDGER_FIELD] == ledger
    assert queued[LEDGER_FIELD] is not ledger
    queued[LEDGER_FIELD]["entries"][0]["candidate_id"] = "mutated"
    assert ledger["entries"][0]["candidate_id"] == CANDIDATE_ID


@pytest.mark.parametrize(
    "ledger",
    (
        {
            "schema_version": LEDGER_SCHEMA,
            "entries": [{"candidate_id": CANDIDATE_ID}],
        },
        _consumed_ledger("auto_foreign_candidate"),
    ),
    ids=("malformed", "foreign-candidate"),
)
@pytest.mark.parametrize(
    "retry_reason",
    (
        "explicit_recovery_review_pipeline_rerun",
        "explicit_user_selection_override",
        "current_delivery_pipeline_fingerprint_changed",
    ),
)
def test_recovery_queue_item_rejects_invalid_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger: dict[str, object],
    retry_reason: str,
) -> None:
    date, record = _recovery_queue_fixture(tmp_path, monkeypatch)
    record[LEDGER_FIELD] = ledger

    with pytest.raises(
        delivery_recovery.RecoveryReviewRerunError,
        match=f"RECOVERY_RERUN_PROVIDER_BUDGET_LEDGER_INVALID:{CANDIDATE_ID}",
    ):
        delivery_recovery._recovery_queue_item(
            date,
            record,
            candidate_id=CANDIDATE_ID,
            retry_reason=retry_reason,
            selected_repair=True,
            given_end_ms=None,
            given_end_authority=None,
            recovery_publication_authority=None,
        )


def test_historical_consumed_ledger_blocks_missing_current_one_shot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit, srt_path = _audit_and_srt(tmp_path)
    retry = _build(audit, srt_path)
    assert retry is not None
    record = {
        "candidate_id": CANDIDATE_ID,
        "status": "candidate_rejected",
        "failure_kind": "subtitle_authority",
        "failure_stage": "final_review_provider_budget",
        "failure_recoverable": True,
        "failure_fingerprint": "sha256:" + "a" * 64,
        "failure_recovery_fingerprint": "sha256:" + "c" * 64,
        "failure_evidence": {"provider_budget_retry": retry},
    }
    history = [
        {"candidate_id": CANDIDATE_ID, LEDGER_FIELD: _consumed_ledger()}
    ]
    monkeypatch.setattr(
        delivery_recovery,
        "_runner",
        SimpleNamespace(TALK_REPAIR_LIFETIME_RETRY_CAP=3),
    )

    assert (
        unconsumed_provider_budget_retry(
            record,
            candidate_id=CANDIDATE_ID,
            history_records=history,
        )
        is None
    )
    assert (
        consumed_provider_budget_retry_ledger(
            record,
            candidate_id=CANDIDATE_ID,
            retry=retry,
            history_records=history,
        )
        is None
    )
    assert (
        _talk_retry_decision(
            record,
            cid=CANDIDATE_ID,
            existing_pending=set(),
            current_recovery="sha256:" + "d" * 64,
            provider_budget_history=history,
        )
        is None
    )


def test_generic_retry_recovers_historical_consumed_ledger_as_deep_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    date, record = _recovery_queue_fixture(tmp_path, monkeypatch)
    record.update(
        failure_recovery_fingerprint="sha256:" + "a" * 64,
        talk_repair_retry_count=0,
        talk_transient_retry_count=1,
    )
    ledger = _consumed_ledger()
    history_row = {"candidate_id": CANDIDATE_ID, LEDGER_FIELD: ledger}
    monkeypatch.setattr(
        delivery_recovery.selection_rescore,
        "execute_pending_rescores",
        lambda *_args, **_kwargs: None,
    )
    state = {
        "picks": [record],
        "pending_talk": [],
        "talk_superseded_attempts": [history_row],
    }

    assert delivery_recovery.requeue_recoverable_talks(date, state) == 1

    queued = state["pending_talk"][0]
    archived = state["talk_superseded_attempts"][-1]
    assert queued[LEDGER_FIELD] == ledger
    assert queued[LEDGER_FIELD] is not ledger
    assert archived[LEDGER_FIELD] == ledger
    assert archived[LEDGER_FIELD] is not queued[LEDGER_FIELD]
    queued[LEDGER_FIELD]["entries"][0]["candidate_id"] = "mutated"
    assert ledger["entries"][0]["candidate_id"] == CANDIDATE_ID
    assert archived[LEDGER_FIELD]["entries"][0]["candidate_id"] == CANDIDATE_ID


def test_generic_retry_does_not_archive_an_absent_ledger_as_null(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    date, record = _recovery_queue_fixture(tmp_path, monkeypatch)
    record.update(
        failure_recovery_fingerprint="sha256:" + "a" * 64,
        talk_repair_retry_count=0,
        talk_transient_retry_count=1,
    )
    monkeypatch.setattr(
        delivery_recovery.selection_rescore,
        "execute_pending_rescores",
        lambda *_args, **_kwargs: None,
    )
    state = {"picks": [record], "pending_talk": []}

    assert delivery_recovery.requeue_recoverable_talks(date, state) == 1
    assert LEDGER_FIELD not in state["talk_superseded_attempts"][0]


@pytest.mark.parametrize(
    ("current_ledger", "history_ledger"),
    (
        (
            {"schema_version": LEDGER_SCHEMA, "entries": [{}]},
            _consumed_ledger(),
        ),
        (_consumed_ledger("auto_foreign_candidate"), _consumed_ledger()),
        (_consumed_ledger(), _consumed_ledger(fingerprint_char="c")),
        (None, {"schema_version": LEDGER_SCHEMA, "entries": [{}]}),
    ),
    ids=(
        "current-malformed",
        "current-foreign",
        "current-history-conflict",
        "history-malformed",
    ),
)
def test_invalid_current_or_historical_ledger_blocks_generic_retry(
    monkeypatch: pytest.MonkeyPatch,
    current_ledger: dict[str, object] | None,
    history_ledger: dict[str, object],
) -> None:
    monkeypatch.setattr(
        delivery_recovery,
        "_runner",
        SimpleNamespace(TALK_REPAIR_LIFETIME_RETRY_CAP=3),
    )
    record = {
        "status": "failed",
        "failure_kind": "producer_error",
        "failure_recoverable": True,
        "failure_recovery_fingerprint": "sha256:" + "a" * 64,
        "talk_repair_retry_count": 0,
        "talk_transient_retry_count": 1,
    }
    if current_ledger is not None:
        record[LEDGER_FIELD] = current_ledger
    history = [
        {"candidate_id": CANDIDATE_ID, LEDGER_FIELD: history_ledger}
    ]

    assert (
        _talk_retry_decision(
            record,
            cid=CANDIDATE_ID,
            existing_pending=set(),
            current_recovery="sha256:" + "d" * 64,
            provider_budget_history=history,
        )
        is None
    )


def test_special_recovery_queue_recovers_historical_ledger_as_deep_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    date, record = _recovery_queue_fixture(tmp_path, monkeypatch)
    ledger = _consumed_ledger()
    history = [{"candidate_id": CANDIDATE_ID, LEDGER_FIELD: ledger}]

    queued = delivery_recovery._recovery_queue_item(
        date,
        record,
        candidate_id=CANDIDATE_ID,
        retry_reason="explicit_recovery_review_pipeline_rerun",
        selected_repair=True,
        given_end_ms=None,
        given_end_authority=None,
        recovery_publication_authority=None,
        provider_budget_history=history,
    )

    assert queued[LEDGER_FIELD] == ledger
    assert queued[LEDGER_FIELD] is not ledger
    queued[LEDGER_FIELD]["entries"][0]["candidate_id"] = "mutated"
    assert ledger["entries"][0]["candidate_id"] == CANDIDATE_ID


@pytest.mark.parametrize(
    ("current_ledger", "history_ledger"),
    (
        (None, {"schema_version": LEDGER_SCHEMA, "entries": [{}]}),
        (_consumed_ledger(), _consumed_ledger(fingerprint_char="c")),
    ),
    ids=("malformed-history", "conflicting-history"),
)
def test_special_recovery_queue_rejects_invalid_historical_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    current_ledger: dict[str, object] | None,
    history_ledger: dict[str, object],
) -> None:
    date, record = _recovery_queue_fixture(tmp_path, monkeypatch)
    if current_ledger is not None:
        record[LEDGER_FIELD] = current_ledger
    history = [
        {"candidate_id": CANDIDATE_ID, LEDGER_FIELD: history_ledger}
    ]

    with pytest.raises(
        delivery_recovery.RecoveryReviewRerunError,
        match=f"RECOVERY_RERUN_PROVIDER_BUDGET_LEDGER_INVALID:{CANDIDATE_ID}",
    ):
        delivery_recovery._recovery_queue_item(
            date,
            record,
            candidate_id=CANDIDATE_ID,
            retry_reason="explicit_recovery_review_pipeline_rerun",
            selected_repair=True,
            given_end_ms=None,
            given_end_authority=None,
            recovery_publication_authority=None,
            provider_budget_history=history,
        )


@pytest.mark.parametrize("outer_candidate_id", ["auto_foreign_candidate", None])
def test_history_ledger_reverse_candidate_mismatch_is_invalid(
    outer_candidate_id: str | None,
) -> None:
    history_row: dict[str, object] = {LEDGER_FIELD: _consumed_ledger()}
    if outer_candidate_id is not None:
        history_row["candidate_id"] = outer_candidate_id

    state, ledger = resolve_provider_budget_retry_ledger_history(
        {"candidate_id": CANDIDATE_ID},
        candidate_id=CANDIDATE_ID,
        history_records=[history_row],
    )

    assert state == LEDGER_STATE_INVALID
    assert ledger is None
