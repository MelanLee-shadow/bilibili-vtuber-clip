from __future__ import annotations

import subprocess
from pathlib import Path

import scripts.session_autoslice  # noqa: F401 — bind the runner proxy for retry checks
from src.autoslice import delivery_recovery, produce_dispatch
from src.autoslice.final_review_carryover import (
    carryover_path,
    checkpoint_final_review_carryover,
)


DATE = "2099-01-02"
CID = "fixture_talk_timeout"
PIPELINE_FINGERPRINT = "sha256:" + "a" * 64


def _timeout(_date: str, _item: dict, **_kwargs: object) -> dict:
    raise subprocess.TimeoutExpired(["produce_slice_package.py"], 5400)


def _dispatch(base: Path, item: dict) -> dict:
    return produce_dispatch.produce_batch_windowed(
        DATE,
        [item],
        _timeout,
        produce_talk_fn=_timeout,
        produce_song_fn=lambda _date, _item: {},
        base=base,
        max_parallel=1,
        log=lambda _message: None,
        talk_pipeline_fingerprint=lambda _candidate_id: PIPELINE_FINGERPRINT,
        pipeline_fingerprint=lambda: "sha256:" + "b" * 64,
        song_pipeline_fingerprint=lambda: "sha256:" + "c" * 64,
        song_window_pre_ms=1_000,
        song_window_post_ms=1_000,
    )[0]


def test_timeout_with_replayable_carryover_uses_typed_bounded_route(tmp_path: Path) -> None:
    out_root = tmp_path / "out" / DATE
    (out_root / CID).mkdir(parents=True)
    audit = {
        "findings": [
            {
                "cue_index": 14,
                "base_text_sha256": "b" * 64,
                "kind": "context",
                "suspect": "错误",
                "replacement": "正确",
                "proposed_full_cue": "正确",
                "why": "test",
                "exact_release_adjudication": {"repaired": True},
            }
        ]
    }
    checkpoint_final_review_carryover(carryover_path(out_root, CID), audit)
    item = {
        "cid": CID,
        "segment_path": "/recordings/source.mp4",
        "session_id": "session-1",
    }

    sibling_result = _dispatch(tmp_path, item)

    assert sibling_result["reason_codes"] == ["PRODUCE_TIMEOUT"]
    assert "failure_stage" not in sibling_result

    checkpoint_final_review_carryover(carryover_path(out_root / CID, CID), audit)
    result = _dispatch(tmp_path, item)

    assert result["status"] == "failed"
    assert result["rc"] == -1
    assert result["failure_recoverable"] is True
    assert result["failure_kind"] == "subtitle_authority"
    assert result["failure_stage"] == "final_review_carryover"
    assert result["reason_codes"] == ["PRODUCE_TIMEOUT_FINAL_REVIEW_CARRYOVER"]
    assert result["failure_fingerprint"].startswith("sha256:")
    assert result["failure_evidence"]["carryover"]["observed_count"] == 1
    assert "findings" not in result["failure_evidence"]
    assert result["segment"] == "source.mp4"
    assert result["session_id"] == "session-1"
    assert result["pipeline_fingerprint"] == PIPELINE_FINGERPRINT

    decision = delivery_recovery._talk_retry_decision(
        result,
        cid=CID,
        existing_pending=set(),
        current_recovery=PIPELINE_FINGERPRINT,
    )
    assert decision is not None
    assert decision.carryover_retry is True
    assert decision.carryover_fingerprint == result["failure_fingerprint"]

    consumed = _dispatch(
        tmp_path,
        {
            **item,
            "final_review_carryover_consumed_fingerprints": [
                result["failure_fingerprint"]
            ],
        },
    )
    assert consumed["final_review_carryover_consumed_fingerprints"] == [
        result["failure_fingerprint"]
    ]
    consumed_decision = delivery_recovery._talk_retry_decision(
        consumed,
        cid=CID,
        existing_pending=set(),
        current_recovery=PIPELINE_FINGERPRINT,
    )
    assert consumed_decision is not None
    assert consumed_decision.carryover_retry is False


def test_timeout_without_carryover_keeps_one_generic_retry(tmp_path: Path) -> None:
    (tmp_path / "out" / DATE / CID).mkdir(parents=True)
    item = {
        "cid": CID,
        "segment_path": "/recordings/source.mp4",
        "talk_transient_retry_count": 0,
    }

    result = _dispatch(tmp_path, item)

    assert result["status"] == "failed"
    assert result["rc"] == -1
    assert result["failure_recoverable"] is True
    assert result["reason_codes"] == ["PRODUCE_TIMEOUT"]
    assert "failure_kind" not in result
    assert "failure_stage" not in result
    assert "failure_fingerprint" not in result
    assert "failure_evidence" not in result

    decision = delivery_recovery._talk_retry_decision(
        result,
        cid=CID,
        existing_pending=set(),
        current_recovery=PIPELINE_FINGERPRINT,
    )
    assert decision is not None
    assert decision.carryover_retry is False
    assert delivery_recovery._talk_retry_decision(
        {**result, "talk_transient_retry_count": 1},
        cid=CID,
        existing_pending=set(),
        current_recovery=PIPELINE_FINGERPRINT,
    ) is None


def _dispatch_with_disk_floor(
    tmp_path: Path,
    items: list[dict],
    produce_fn,
    *,
    min_free_bytes: int,
    free_bytes_fn,
    log,
) -> list[dict]:
    return produce_dispatch.produce_batch_windowed(
        DATE,
        items,
        produce_fn,
        produce_talk_fn=produce_fn,
        produce_song_fn=lambda _date, _item: {},
        base=tmp_path,
        max_parallel=1,
        min_free_bytes=min_free_bytes,
        free_bytes_fn=free_bytes_fn,
        log=log,
        talk_pipeline_fingerprint=lambda _candidate_id: PIPELINE_FINGERPRINT,
        pipeline_fingerprint=lambda: "sha256:" + "b" * 64,
        song_pipeline_fingerprint=lambda: "sha256:" + "c" * 64,
        song_window_pre_ms=1_000,
        song_window_post_ms=1_000,
        on_result=lambda _index, _item, _result: True,
    )


def test_disk_floor_under_limit_launches_nothing_and_keeps_queue(
    tmp_path: Path,
) -> None:
    items = [{"cid": "first"}, {"cid": "tail"}]
    started: list[str] = []
    logs: list[str] = []

    def produce(_date: str, item: dict) -> dict:
        started.append(item["cid"])
        return {"candidate_id": item["cid"], "status": "ok"}

    results = _dispatch_with_disk_floor(
        tmp_path,
        items,
        produce,
        min_free_bytes=100,
        free_bytes_fn=lambda _path: 99,
        log=logs.append,
    )

    assert started == []
    assert results == []
    assert items == [{"cid": "first"}, {"cid": "tail"}]
    assert any("disk free-space floor reached" in message for message in logs)


def test_disk_floor_crossing_stops_the_next_candidate_and_keeps_tail(
    tmp_path: Path,
) -> None:
    items = [{"cid": "first"}, {"cid": "tail"}]
    started: list[str] = []
    logs: list[str] = []
    free_bytes = iter((101, 99))

    def produce(_date: str, item: dict) -> dict:
        started.append(item["cid"])
        return {"candidate_id": item["cid"], "status": "ok"}

    results = _dispatch_with_disk_floor(
        tmp_path,
        items,
        produce,
        min_free_bytes=100,
        free_bytes_fn=lambda _path: next(free_bytes),
        log=logs.append,
    )

    assert started == ["first"]
    assert [row["candidate_id"] for row in results] == ["first"]
    assert items == [{"cid": "first"}, {"cid": "tail"}]
    assert any("free bytes 99 < floor 100" in message for message in logs)
