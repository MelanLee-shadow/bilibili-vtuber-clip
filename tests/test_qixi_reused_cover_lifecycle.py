"""Regression gates for the Qixi strict published-cover carry lifecycle."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import scripts.free_session_autoslice as runner
from src.autoslice import cover_maintenance, cover_repair, talk_lane
import src.autoslice.published_cover_carry as published_cover_carry
from src.autoslice.produce_dispatch import produce_batch_windowed


DATE = "2026-08-17"
CID = "auto_113022_354_496"


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _strict_record(tmp_path: Path) -> tuple[dict[str, object], dict[str, object], Path, Path]:
    media = tmp_path / "delivery.mp4"
    cover = tmp_path / "delivery.cover.png"
    media.write_bytes(b"video")
    cover.write_bytes(b"published-8c-cover")
    marker = {
        "cover_path": str(cover),
        "cover_sha256": _sha(cover),
        "generation_path": str(tmp_path / "published-cover-generation.json"),
    }
    generation = {
        "title": "七夕修复",
        "fallback_used": False,
        "method": "images.edit",
        "published_cover_carry_strict": True,
        "final_cover": str(cover),
        "final_cover_sha256": _sha(cover),
        "reused_cover_path": str(cover),
    }
    record = {
        "candidate_id": CID,
        "status": "review_ready",
        "title": "七夕修复",
        "cover_status": "REUSED_COVER",
        "published_cover_carry_required": True,
        "published_cover_carry": marker,
        "cover_path": str(cover),
        "cover_sha256": _sha(cover),
        "video_sha256": _sha(media),
        "cover_generation": generation,
    }
    return record, marker, media, cover


def _wire_strict_proof(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tmp_path: Path,
    record: dict[str, object],
    marker: dict[str, object],
    media: Path,
    cover: Path,
    publish_cover_status: str = "REUSED_COVER",
) -> None:
    monkeypatch.setattr(runner, "BASE", tmp_path)
    monkeypatch.setattr(runner, "delivered_paths", lambda _date, _record: (media, cover))
    monkeypatch.setattr(
        published_cover_carry,
        "validate_materialized_marker",
        lambda observed, **_kwargs: observed == marker,
    )
    monkeypatch.setattr(cover_repair, "cover_maintenance_block_reason", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        cover_repair,
        "_active_cover_documents",
        lambda **_kwargs: [
            (
                tmp_path / "publish.json",
                {
                    "artifact_hashes": {"cover_sha256": record["cover_sha256"]},
                    "publish_staging": {
                        "cover_status": publish_cover_status,
                        "upload_enabled": False,
                        "cover_path": record["cover_path"],
                        "cover_generation": record["cover_generation"],
                    },
                },
            )
        ],
    )
    monkeypatch.setattr(cover_repair, "_active_song_delivery_manifest", lambda *_a, **_kw: None)


def test_valid_strict_reused_cover_is_delivery_ready_and_not_maintenance_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, marker, media, cover = _strict_record(tmp_path)
    _wire_strict_proof(
        monkeypatch, tmp_path=tmp_path, record=record, marker=marker, media=media, cover=cover
    )

    assert published_cover_carry.result_cover_delivery_ready(
        result=record,
        marker=marker,
        base=tmp_path,
        date=DATE,
        candidate_id=CID,
        delivered_paths=runner.delivered_paths,
        initial_cover_proof_valid=runner._initial_cover_proof_valid,
    )
    assert cover_repair._initial_cover_proof_valid(DATE, record, media, cover)
    assert not cover_repair.cover_repair_needed(DATE, record)


def test_cover_maintenance_does_not_queue_regeneration_for_valid_strict_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, marker, media, cover = _strict_record(tmp_path)
    _wire_strict_proof(
        monkeypatch, tmp_path=tmp_path, record=record, marker=marker, media=media, cover=cover
    )
    monkeypatch.setattr(cover_maintenance, "cover_maintenance_block_reason", lambda *_a, **_kw: None)
    monkeypatch.setattr(runner, "_roll_forward_prepared_cover_transactions", lambda *_a: False)
    monkeypatch.setattr(runner, "_recover_committed_cover_binding", lambda *_a: False)
    monkeypatch.setattr(runner, "_refresh_cover_repair_budget", lambda *_a: False)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:new-policy")
    monkeypatch.setattr(
        runner,
        "_cover_authority_preflight",
        lambda *_a: pytest.fail("valid strict carry must not reach cover maintenance"),
    )

    cover_maintenance.repair_covers(DATE, {"picks": [record], "songs": []})

    assert record["status"] == "review_ready"
    assert record.get("failure_kind") is None
    assert record.get("cover_route_regeneration_attempts") is None


@pytest.mark.parametrize("fault", ["missing_marker", "tampered_marker", "result_marker", "bare_reuse", "result_hash", "publish_status"])
def test_reused_cover_stays_fail_closed_when_strict_proof_drifts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    record, marker, media, cover = _strict_record(tmp_path)
    publish_status = "REUSED_COVER"
    if fault == "missing_marker":
        record.pop("published_cover_carry")
    elif fault == "tampered_marker":
        record["published_cover_carry"] = {**marker, "cover_sha256": "sha256:" + "0" * 64}
    elif fault == "result_marker":
        record["published_cover_carry"] = {**marker, "unexpected": "not-the-queue-marker"}
    elif fault == "bare_reuse":
        record["published_cover_carry_required"] = False
    elif fault == "result_hash":
        record["cover_sha256"] = "sha256:" + "0" * 64
    elif fault == "publish_status":
        publish_status = "AI_COVER_READY"
    _wire_strict_proof(
        monkeypatch,
        tmp_path=tmp_path,
        record=record,
        marker=marker,
        media=media,
        cover=cover,
        publish_cover_status=publish_status,
    )

    assert not published_cover_carry.result_cover_delivery_ready(
        result=record,
        marker=(marker if fault == "result_marker" else record.get("published_cover_carry")),
        base=tmp_path,
        date=DATE,
        candidate_id=CID,
        delivered_paths=runner.delivered_paths,
        initial_cover_proof_valid=runner._initial_cover_proof_valid,
    )
    assert not cover_repair._initial_cover_proof_valid(DATE, record, media, cover)
    assert cover_repair.cover_repair_needed(DATE, record)


def test_required_carry_rejects_before_producer_without_provider_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner, "BASE", tmp_path)
    monkeypatch.setattr(
        published_cover_carry, "validate_materialized_marker", lambda *_a, **_kw: False
    )
    called: list[object] = []
    monkeypatch.setattr(talk_lane, "_run_talk_producer_with_boundary_context_retry", lambda **_kw: called.append(1))

    result = talk_lane.produce_talk(
        DATE,
        {
            "cid": CID,
            "segment_path": "/source.mp4",
            "start_ms": 0,
            "end_ms": 60_000,
            "hook": "七夕",
            "published_cover_carry_required": True,
            "published_cover_carry": {},
        },
        reuse_cover=True,
    )

    assert called == []
    assert result["reason_codes"] == ["PUBLISHED_COVER_CARRY_MARKER_INVALID"]


def test_dispatch_to_real_talk_result_carries_marker_into_postproduction_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, marker, media, cover = _strict_record(tmp_path)
    _wire_strict_proof(
        monkeypatch, tmp_path=tmp_path, record=record, marker=marker, media=media, cover=cover
    )
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner, "SPEAKER_MODE", "off")
    monkeypatch.setattr(runner, "PIECE_PRE_MS", 0)
    monkeypatch.setattr(runner, "PIECE_POST_MS", 0)
    monkeypatch.setattr(runner, "MIN_TALK_EFFECTIVE_DURATION_MS", 1)
    monkeypatch.setattr(runner, "BOUNDARY_REPAIR_INITIAL_CAP_MS", 1)
    monkeypatch.setattr(runner, "human_truth_mode", lambda: False)
    monkeypatch.setattr(runner, "talk_pipeline_fingerprint", lambda _cid: "sha256:pipeline")
    monkeypatch.setattr(runner, "log", lambda _message: None)
    monkeypatch.setattr(runner, "read_publish_meta", lambda _root: {
        key: value
        for key, value in record.items()
        if key in {"title", "cover_status", "cover_path", "cover_sha256", "cover_generation", "video_sha256"}
    })
    monkeypatch.setattr(runner, "_initial_cover_proof_valid", lambda *_args: True)
    monkeypatch.setattr(talk_lane, "_provider_budget_ledger_preflight_rejection", lambda *_a, **_kw: None)
    monkeypatch.setattr(talk_lane, "_selection_scorecard_rejection", lambda _item: None)
    monkeypatch.setattr(
        talk_lane,
        "_prepare_talk_filler_plan",
        lambda _item: {"effective_duration_ms": 60_000, "status": "OK", "removals": []},
    )
    monkeypatch.setattr(talk_lane, "_talk_filler_rejection", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        talk_lane,
        "build_piece_specs",
        lambda **_kwargs: [{"remote_media": "/source.mp4", "start_ms": 0, "end_ms": 60_000}],
    )
    monkeypatch.setattr(talk_lane, "_apply_recovery_authorities_to_talk_spec", lambda *_a, **_kw: None)
    monkeypatch.setattr(talk_lane, "_apply_optional_talk_spec_fields", lambda *_a, **_kw: None)
    monkeypatch.setattr(talk_lane, "_persist_speaker_session_context", lambda **_kwargs: None)
    monkeypatch.setattr(
        talk_lane,
        "_run_talk_producer_with_boundary_context_retry",
        lambda **_kwargs: (__import__("subprocess").CompletedProcess([], 0), "{}", {}, 0, None),
    )

    item = {
        "cid": CID,
        "segment_path": "/source.mp4",
        "start_ms": 0,
        "end_ms": 60_000,
        "hook": "七夕",
        "published_cover_carry_required": True,
        "published_cover_carry": marker,
    }
    rows = produce_batch_windowed(
        DATE,
        [item],
        talk_lane.produce_talk,
        produce_talk_fn=talk_lane.produce_talk,
        produce_song_fn=lambda *_a, **_kw: pytest.fail("song lane must not run"),
        base=tmp_path,
        max_parallel=1,
        log=lambda _message: None,
        talk_pipeline_fingerprint=lambda _cid: "sha256:pipeline",
        pipeline_fingerprint=lambda: "sha256:all",
        song_pipeline_fingerprint=lambda: "sha256:song",
        song_window_pre_ms=0,
        song_window_post_ms=0,
    )

    assert rows[0]["status"] == "review_ready", rows[0]
    assert rows[0]["published_cover_carry_required"] is True
    assert rows[0]["published_cover_carry"] == marker
