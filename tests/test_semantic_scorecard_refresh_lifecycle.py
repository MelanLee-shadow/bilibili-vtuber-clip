"""Regression coverage for semantic scorecard refresh receipt provenance.

The published-topic validator owns receipt validation.  Production and
recovery lifecycle hops must only preserve an existing receipt byte-for-byte
(as an independent object); they must neither synthesize nor reinterpret it.
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.free_session_autoslice as runner  # noqa: F401 - binds RunnerProxy
from src.autoslice import delivery_recovery, produce_dispatch, talk_lane
from src.autoslice.final_review_provider_budget_retry import (
    LEDGER_FIELD as FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD,
    LEDGER_SCHEMA as FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_SCHEMA,
)
from src.autoslice.semantic_scorecard_refresh_receipt import ROW_RECEIPT_KEY
from src.autoslice.selected_source_fact_recovery import (
    RECOVERY_RECEIPT_FIELD as SELECTED_SOURCE_FACT_RECOVERY_RECEIPT_FIELD,
    build_selected_source_fact_recovery_receipt,
)


_SOURCE_FACT_GRANT_ID = "unit-source-fact-recovery"
_SOURCE_FACT_OLD = "sha256:" + "e" * 64
_SOURCE_FACT_CURRENT = "sha256:" + "f" * 64


def _receipt() -> dict[str, object]:
    return {
        "schema_version": "semantic-evidence-scorecard-refresh-receipt.v1",
        "status": "REFRESHED",
        "binding": {
            "candidate_id": "candidate-806",
            "evidence_sha256": ["sha256:" + "a" * 64],
        },
    }


def _assert_independent_receipt_copy(
    source: dict[str, object], target: dict[str, object]
) -> None:
    frozen = copy.deepcopy(source[ROW_RECEIPT_KEY])
    assert target[ROW_RECEIPT_KEY] == frozen
    assert target[ROW_RECEIPT_KEY] is not source[ROW_RECEIPT_KEY]
    target_receipt = target[ROW_RECEIPT_KEY]
    assert isinstance(target_receipt, dict)
    target_binding = target_receipt["binding"]
    assert isinstance(target_binding, dict)
    target_binding["candidate_id"] = "mutated-target"
    assert source[ROW_RECEIPT_KEY] == frozen


def _attach_source_fact_recovery_receipt(item: dict[str, object]) -> None:
    candidate_id = str(item["cid"])
    old_row = {
        "candidate_id": candidate_id,
        "status": "candidate_rejected",
        "rejected_status": "failed",
        "rc": 1,
        "selected_repair": True,
        "failure_kind": "story_contract",
        "failure_stage": "source_fact_repair",
        "failure_recoverable": False,
        "rejection_reason": "story_contract_unresolved_backfilled",
        "failure_recovery_fingerprint": _SOURCE_FACT_OLD,
    }
    item[SELECTED_SOURCE_FACT_RECOVERY_RECEIPT_FIELD] = (
        build_selected_source_fact_recovery_receipt(
            old_row=old_row,
            queued_row=item,
            candidate_id=candidate_id,
            grant_id=_SOURCE_FACT_GRANT_ID,
            current_fingerprint=_SOURCE_FACT_CURRENT,
        )
    )


def _assert_independent_source_fact_receipt(
    source: dict[str, object], target: dict[str, object]
) -> None:
    original = source[SELECTED_SOURCE_FACT_RECOVERY_RECEIPT_FIELD]
    carried = target[SELECTED_SOURCE_FACT_RECOVERY_RECEIPT_FIELD]
    assert isinstance(original, dict)
    assert carried == original
    assert carried is not original
    carried["old_row"]["candidate_id"] = "mutated-target"
    assert original["old_row"]["candidate_id"] == source["cid"]


def _provider_budget_ledger(candidate_id: str) -> dict[str, object]:
    return {
        "schema_version": FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_SCHEMA,
        "entries": [
            {
                "candidate_id": candidate_id,
                "retry_fingerprint": "sha256:" + "a" * 64,
            }
        ],
    }


@pytest.mark.parametrize("early_gate", ["scorecard", "filler"])
@pytest.mark.parametrize("valid_ledger", [True, False])
def test_produce_talk_early_rejection_only_carries_valid_provider_budget_ledger(
    monkeypatch: pytest.MonkeyPatch,
    early_gate: str,
    valid_ledger: bool,
) -> None:
    candidate_id = "candidate-1576"
    ledger = (
        _provider_budget_ledger(candidate_id)
        if valid_ledger
        else {
            "schema_version": FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_SCHEMA,
            "entries": [{"candidate_id": candidate_id}],
        }
    )
    rejection = {
        "candidate_id": candidate_id,
        "status": "candidate_rejected",
        "reason_codes": [f"UNIT_{early_gate.upper()}_REJECTION"],
    }
    monkeypatch.setattr(
        talk_lane,
        "_selection_scorecard_rejection",
        lambda _item: copy.deepcopy(rejection)
        if early_gate == "scorecard"
        else None,
    )
    monkeypatch.setattr(
        talk_lane,
        "_prepare_talk_filler_plan",
        lambda _item: {
            "effective_duration_ms": 60_000,
            "status": "contiguous",
            "removals": [],
        },
    )
    monkeypatch.setattr(
        talk_lane,
        "_talk_filler_rejection",
        lambda *_args, **_kwargs: (
            copy.deepcopy(rejection) if early_gate == "filler" else None
        ),
    )
    item = {
        "cid": candidate_id,
        "segment_path": "/recordings/segment.mp4",
        "start_ms": 0,
        "end_ms": 60_000,
        FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD: ledger,
    }

    result = talk_lane.produce_talk("2026-08-08", item)

    if valid_ledger:
        assert result[FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD] == ledger
        assert result[FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD] is not ledger
        assert (
            result[FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD]["entries"]
            is not ledger["entries"]
        )
    else:
        assert FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD not in result
        assert result["status"] == "candidate_rejected"
        assert result["failure_recoverable"] is False
        assert result["failure_kind"] == "pipeline_contract"
        assert result["failure_stage"] == "final_review_provider_budget_ledger"
        assert "FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_INVALID" in result["reason_codes"]


def test_produce_talk_blocks_malformed_provider_budget_ledger_before_filler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_id = "candidate-1576"
    monkeypatch.setattr(
        talk_lane,
        "_selection_scorecard_rejection",
        lambda _item: None,
    )
    monkeypatch.setattr(
        talk_lane,
        "_prepare_talk_filler_plan",
        lambda _item: pytest.fail("invalid ledger must block before filler work"),
    )
    item = {
        "cid": candidate_id,
        "segment_path": "/recordings/segment.mp4",
        "start_ms": 0,
        "end_ms": 60_000,
        FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD: {
            "schema_version": FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_SCHEMA,
            "entries": [{"candidate_id": candidate_id}],
        },
    }

    result = talk_lane.produce_talk("2026-08-08", item)

    assert result["status"] == "candidate_rejected"
    assert result["failure_recoverable"] is False
    assert result["failure_stage"] == "final_review_provider_budget_ledger"
    assert FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD not in result


@pytest.mark.parametrize("with_receipt", [False, True])
def test_produce_talk_preserves_existing_refresh_receipt_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_receipt: bool,
) -> None:
    base = tmp_path / "autoslice"
    (base / "logs").mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    runner_stub = SimpleNamespace(
        BASE=base,
        REPO_ROOT=repo,
        PIECE_PRE_MS=1_000,
        PIECE_POST_MS=1_000,
        BOUNDARY_REPAIR_INITIAL_CAP_MS=1_000,
        MIN_TALK_EFFECTIVE_DURATION_MS=45_000,
        SPEAKER_MODE="uniform_host",
        human_truth_mode=lambda: "withheld",
        safe_name=lambda _hook, candidate_id: candidate_id,
        candidate_text_override_path=lambda _candidate_id: None,
        candidate_subtitle_regression_path=lambda _candidate_id: None,
        candidate_reviewed_subtitle_baseline=lambda _candidate_id: None,
        candidate_speaker_override_path=lambda _candidate_id: None,
        log=lambda _message: None,
        talk_pipeline_fingerprint=lambda _candidate_id: "sha256:" + "b" * 64,
        last_json_block=lambda _tail: {},
        read_publish_meta=lambda _root: {},
        classify_talk_failure=lambda _tail: {
            "failure_kind": "producer_error",
            "failure_stage": "unit_test",
            "failure_message": "synthetic failure",
            "failure_fingerprint": "sha256:" + "c" * 64,
            "failure_recoverable": False,
        },
        talk_failure_recovery_fingerprint=lambda _kind, _candidate_id: (
            "sha256:" + "d" * 64
        ),
        _speaker_evidence_insufficient_failure=lambda _tail: False,
    )
    monkeypatch.setattr(talk_lane, "_runner", runner_stub)
    monkeypatch.setattr(
        talk_lane,
        "_prepare_talk_filler_plan",
        lambda _item: {
            "effective_duration_ms": 60_000,
            "status": "contiguous",
            "removals": [],
        },
    )
    monkeypatch.setattr(talk_lane, "_talk_filler_rejection", lambda *_a, **_k: None)
    monkeypatch.setattr(
        talk_lane,
        "build_piece_specs",
        lambda **_kwargs: [
            {
                "remote_media": "/recordings/segment.mp4",
                "start_ms": 0,
                "end_ms": 60_000,
            }
        ],
    )
    monkeypatch.setattr(
        talk_lane,
        "_persist_speaker_session_context",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        talk_lane,
        "_run_talk_producer_with_boundary_context_retry",
        lambda **_kwargs: (
            SimpleNamespace(returncode=1),
            "synthetic failure",
            None,
            0,
            None,
        ),
    )

    item: dict[str, object] = {
        "cid": "candidate-806",
        "segment_path": "/recordings/segment.mp4",
        "seg_dur_ms": 120_000,
        "start_ms": 0,
        "end_ms": 60_000,
        "hook": "test",
        "selected_repair": True,
        FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD: {
            "schema_version": "final-review-provider-budget-retry-ledger.v1",
            "entries": [
                {
                    "candidate_id": "candidate-806",
                    "retry_fingerprint": "sha256:" + "a" * 64,
                }
            ],
        },
    }
    if with_receipt:
        item[ROW_RECEIPT_KEY] = _receipt()
    _attach_source_fact_recovery_receipt(item)

    result = talk_lane.produce_talk("2026-08-08", item)

    _assert_independent_source_fact_receipt(item, result)
    if with_receipt:
        _assert_independent_receipt_copy(item, result)
    else:
        assert ROW_RECEIPT_KEY not in result
    assert (
        result[FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD]
        == item[FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD]
    )
    assert (
        result[FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD]
        is not item[FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD]
    )


@pytest.mark.parametrize("with_receipt", [False, True])
def test_unexpected_producer_exception_preserves_existing_refresh_receipt_only(
    tmp_path: Path,
    with_receipt: bool,
) -> None:
    def crash(_date: str, _item: dict, **_kwargs: object) -> dict:
        raise RuntimeError("synthetic producer crash")

    item: dict[str, object] = {
        "cid": "candidate-806",
        "segment_path": "/recordings/segment.mp4",
    }
    if with_receipt:
        item[ROW_RECEIPT_KEY] = _receipt()

    result = produce_dispatch.produce_batch_windowed(
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

    if with_receipt:
        _assert_independent_receipt_copy(item, result)
    else:
        assert ROW_RECEIPT_KEY not in result


def test_unexpected_talk_exception_preserves_provider_budget_ledger(
    tmp_path: Path,
) -> None:
    def crash(_date: str, _item: dict, **_kwargs: object) -> dict:
        raise RuntimeError("synthetic producer crash")

    ledger = {
        "schema_version": "final-review-provider-budget-retry-ledger.v1",
        "entries": [
            {
                "candidate_id": "candidate-1576",
                "retry_fingerprint": "sha256:" + "a" * 64,
            }
        ],
    }
    item: dict[str, object] = {
        "cid": "candidate-1576",
        "segment_path": "/recordings/segment.mp4",
        "selected_repair": True,
        FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD: ledger,
    }
    _attach_source_fact_recovery_receipt(item)

    result = produce_dispatch.produce_batch_windowed(
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

    assert result[FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD] == ledger
    assert result[FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD] is not ledger
    _assert_independent_source_fact_receipt(item, result)
    result[FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD]["entries"][0][
        "candidate_id"
    ] = "mutated-target"
    assert ledger["entries"][0]["candidate_id"] == "candidate-1576"


@pytest.mark.parametrize(
    "malformed_ledger",
    (
        {
            "schema_version": FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_SCHEMA,
            "entries": [{"candidate_id": "candidate-1576"}],
        },
        ["not-a-ledger"],
    ),
    ids=("mapping", "list"),
)
def test_unexpected_talk_exception_types_malformed_provider_budget_ledger(
    tmp_path: Path,
    malformed_ledger: object,
) -> None:
    def crash(_date: str, _item: dict, **_kwargs: object) -> dict:
        raise RuntimeError("synthetic producer crash")

    item = {
        "cid": "candidate-1576",
        "segment_path": "/recordings/segment.mp4",
        FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD: malformed_ledger,
    }

    result = produce_dispatch.produce_batch_windowed(
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

    assert FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD not in result
    assert result["status"] == "candidate_rejected"
    assert result["failure_recoverable"] is False
    assert result["failure_kind"] == "pipeline_contract"
    assert result["failure_stage"] == "final_review_provider_budget_ledger"
    assert "FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_INVALID" in result["reason_codes"]


def test_unexpected_talk_exception_omits_valid_empty_provider_budget_ledger(
    tmp_path: Path,
) -> None:
    def crash(_date: str, _item: dict, **_kwargs: object) -> dict:
        raise RuntimeError("synthetic producer crash")

    item = {
        "cid": "candidate-1576",
        "segment_path": "/recordings/segment.mp4",
        FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD: {
            "schema_version": FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_SCHEMA,
            "entries": [],
        },
    }

    result = produce_dispatch.produce_batch_windowed(
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

    assert FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD not in result
    assert result["status"] == "failed"
    assert result["failure_recoverable"] is True
    assert "FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_INVALID" not in result["reason_codes"]


def _recovery_fixture(
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
        find_danmaku_xml=lambda _segment: None,
        talk_pipeline_fingerprint=lambda _candidate_id: "sha256:" + "b" * 64,
        talk_failure_recovery_fingerprint=lambda _kind, _candidate_id: (
            "sha256:" + "c" * 64
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
        "candidate_id": "candidate-806",
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


@pytest.mark.parametrize("with_receipt", [False, True])
def test_explicit_recovery_queue_preserves_existing_refresh_receipt_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_receipt: bool,
) -> None:
    date, record = _recovery_fixture(tmp_path, monkeypatch)
    if with_receipt:
        record[ROW_RECEIPT_KEY] = _receipt()

    queued = delivery_recovery._recovery_queue_item(
        date,
        record,
        candidate_id="candidate-806",
        retry_reason="test",
        selected_repair=True,
        given_end_ms=None,
        given_end_authority=None,
        recovery_publication_authority=None,
    )

    if with_receipt:
        _assert_independent_receipt_copy(record, queued)
    else:
        assert ROW_RECEIPT_KEY not in queued


@pytest.mark.parametrize("with_receipt", [False, True])
def test_generic_failed_pick_requeue_preserves_existing_refresh_receipt_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_receipt: bool,
) -> None:
    date, record = _recovery_fixture(tmp_path, monkeypatch)
    if with_receipt:
        record[ROW_RECEIPT_KEY] = _receipt()
    decision = delivery_recovery._TalkRetryDecision(
        retry_count=0,
        transient_count=0,
        changed=True,
        infrastructure_retry=False,
        transient=False,
        cover_route_retry=False,
        sanctioned_revival_retry=None,
            carryover_fingerprint=None,
            carryover_retry=False,
            provider_budget_ledger=None,
            sanctioned_retry=False,
        rescore_fingerprint=None,
        rescore_retry=False,
    )
    monkeypatch.setattr(
        delivery_recovery,
        "_talk_retry_decision",
        lambda *_args, **_kwargs: decision,
    )
    monkeypatch.setattr(
        delivery_recovery.selection_rescore,
        "execute_pending_rescores",
        lambda *_args, **_kwargs: None,
    )

    state = {"picks": [record], "pending_talk": []}
    assert delivery_recovery.requeue_recoverable_talks(date, state) == 1
    queued = state["pending_talk"][0]

    if with_receipt:
        _assert_independent_receipt_copy(record, queued)
    else:
        assert ROW_RECEIPT_KEY not in queued
