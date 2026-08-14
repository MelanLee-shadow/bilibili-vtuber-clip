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
from src.autoslice.semantic_scorecard_refresh_receipt import ROW_RECEIPT_KEY


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
    }
    if with_receipt:
        item[ROW_RECEIPT_KEY] = _receipt()

    result = talk_lane.produce_talk("2026-08-08", item)

    if with_receipt:
        _assert_independent_receipt_copy(item, result)
    else:
        assert ROW_RECEIPT_KEY not in result


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
