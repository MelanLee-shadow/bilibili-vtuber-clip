from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

import src.autoslice.redelivery_full_window_replay as full_window_replay
import src.autoslice.reviewed_baseline_replay as replay
from src.autoslice import producer_package_finalization as finalization
from src.autoslice.recut_materialization import _write_source_range_srt
from src.autoslice.redelivery_source_binding import V2RedeliverySourceBinding
from src.autoslice.reviewed_baseline_replay_c12_projection import (
    C12FinalDeliveryProjectionError,
    build_c12_final_delivery_projection,
    replay_c12_final_delivery_projection,
)


ROOT = Path(__file__).resolve().parents[1]
DATE = "2026-08-15"
CID = "auto_130012_435_574"
PADDED_START = 425_180
PADDED_END = 622_370
FINAL_START = 9_740
FINAL_END = 155_850


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _c12_plan_and_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    recut = tmp_path / "out" / DATE / CID / "replacement_recuts"
    recut.mkdir(parents=True)
    old_media = b"old-c12-record-media"
    padded = recut.parent / f"padded_{PADDED_START}_{PADDED_END}.mp4"
    padded.write_bytes(b"c12-padded-source")
    record = {
        "artifact_hashes": {"video_sha256": _sha(old_media)},
        "duration_ms": FINAL_END - FINAL_START,
        "boundary_audit": {"final_start_ms": FINAL_START, "final_end_ms": FINAL_END},
    }
    (recut / f"{CID}.record.json").write_text(json.dumps(record), encoding="utf-8")
    (recut / f"{CID}.recut.provenance.json").write_text(json.dumps({
        "source_piece": {
            "start_ms": PADDED_START,
            "end_ms": PADDED_END,
            "source_path": "/recording/22966160_20260815-13-00-12.mp4",
            "source_media_binding": "sha256:297cfb07b04b006ac76307ad0369af7160a62f64d04d315ddcfa7146d2625077",
        },
        "padded": {"output_path": str(padded), "start_ms": PADDED_START, "end_ms": PADDED_END},
        "final_recut": {
            "source_path": str(padded),
            "source_sha256": hashlib.sha256(padded.read_bytes()).hexdigest(),
            "start_ms": FINAL_START,
            "end_ms": FINAL_END,
        },
    }), encoding="utf-8")
    plan = replay.build_replay_plan(
        repo_root=ROOT, out_root=tmp_path / "out", date=DATE, candidate_id=CID,
    )

    def fake_run(command: list[str], **_kwargs: object) -> object:
        Path(command[-1]).write_bytes(old_media)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(replay.subprocess, "run", fake_run)
    stage_parent = tmp_path / "private"
    stage_parent.mkdir(mode=0o700)
    stage_result = replay.stage_replay(plan, stage_parent=stage_parent)
    stage = Path(stage_result["stage"])
    projection = build_c12_final_delivery_projection(
        candidate_id=CID,
        config=plan.baseline.config,
        record_sha256=replay.regular_binding(plan.record_path, label="RECORD").sha256,
        record_boundary=record["boundary_audit"],
        expected_video_sha256=plan.expected_video_sha256,
        padded_path=plan.padded_path,
        final_start_ms=FINAL_START,
        final_end_ms=FINAL_END,
        stage=stage,
    )
    assert projection is not None
    binding = V2RedeliverySourceBinding(
        absolute_source_start_ms=PADDED_START + FINAL_START,
        absolute_source_end_ms=PADDED_START + FINAL_END,
        source_recording_basename="22966160_20260815-13-00-12.mp4",
        source_sha256="297cfb07b04b006ac76307ad0369af7160a62f64d04d315ddcfa7146d2625077",
        content_absolute_start_ms=PADDED_START,
        content_absolute_end_ms=PADDED_END,
        padded_content_start_ms=0,
        padded_content_end_ms=PADDED_END - PADDED_START,
    )
    return plan, stage, projection, binding


def test_c12_generic_dispatch_proves_why_it_falls_to_ordinary_alignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, stage, _projection, binding = _c12_plan_and_stage(tmp_path, monkeypatch)
    # The generic gate compares the attested baseline with the content piece,
    # not the old record crop.  These are intentionally different grids.
    assert not full_window_replay.exact_full_window_replay_enabled(
        plan.baseline.config, binding
    )
    seen: dict[str, object] = {}

    def ordinary(text: str, **kwargs: object) -> tuple[str, dict[str, object]]:
        seen["text"] = text
        seen.update(kwargs)
        return text, {"status": "ALREADY_SATISFIED", "application_strategy": "ordinary"}

    monkeypatch.setattr(full_window_replay, "apply_redelivery_subtitle_baseline", ordinary)
    target = stage / "generic.srt"
    target.write_bytes((stage / "reviewed.srt").read_bytes())
    _text, audit = full_window_replay.replay_baseline_for_final_recut(
        truth_audit={}, recut_dir=stage, cid=CID, sanitized=[], binding=binding,
        config=plan.baseline.config, spec_parent=ROOT,
        final_start_ms=FINAL_START, final_end_ms=FINAL_END, subtitle_path=target,
        write_source_range_srt=_write_source_range_srt,
    )
    assert audit["application_strategy"] == "ordinary"
    assert seen["current_source_start_ms"] == 434_920
    assert seen["current_source_end_ms"] == 581_030
    assert plan.baseline.config["absolute_source_start_ms"] == 434_920
    assert plan.baseline.config["absolute_source_end_ms"] == 603_840
    assert (binding.content_absolute_start_ms, binding.content_absolute_end_ms) == (
        425_180, 622_370,
    )


def test_c12_private_projection_replays_the_sealed_truth_graph_to_old_record_crop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, stage, projection, binding = _c12_plan_and_stage(tmp_path, monkeypatch)
    target = stage / "finalizer-output.srt"
    target.write_text("untrusted current ASR\n", encoding="utf-8")

    output, audit = replay_c12_final_delivery_projection(
        projection=projection, config=plan.baseline.config, cid=CID,
        final_start_ms=FINAL_START, final_end_ms=FINAL_END, binding=binding,
        subtitle_path=target, write_source_range_srt=_write_source_range_srt,
    )

    assert target.read_text(encoding="utf-8") == output
    assert target.read_bytes() == (stage / "reviewed.srt").read_bytes()
    assert audit["application_strategy"] == "exact_reviewed_interval_replay"
    assert audit["exact_replay_then_final_crop"] is True
    assert audit["final_delivery_projection"] == {
        "absolute_source_start_ms": 434_920,
        "absolute_source_end_ms": 581_030,
    }
    assert audit["full_release_delivery_projection"]["receipt_sha256"] == projection.receipt_sha256
    assert audit["full_release_delivery_projection"]["full_release_cue_count"] == 59


def test_c12_projection_is_consumed_by_canonical_materializer_not_generic_alignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, stage, projection, _binding = _c12_plan_and_stage(tmp_path, monkeypatch)
    padded_provenance = plan.padded_path.with_suffix(".provenance.json")
    padded_provenance.write_text("{}\n", encoding="utf-8")

    def run_command(command: list[str], **_kwargs: object) -> None:
        Path(command[-1]).write_bytes(b"canonical-private-recut")

    def unexpected_generic(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("C12 must not re-enter generic alignment")

    monkeypatch.setattr(finalization, "replay_baseline_for_final_recut", unexpected_generic)
    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=lambda **kwargs: ["recut", str(kwargs["output_media"])],
        run_command=run_command,
        write_source_range_srt=_write_source_range_srt,
        apply_text_override_document=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
        run_speaker_finalization=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
        burn_preview_subtitles=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
        stage_publish_draft=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
        generate_upload_tags=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
        delivery_root=lambda: tmp_path / "delivery",
    )
    spec = {
        "pieces": [{
            "remote_media": "/recording/22966160_20260815-13-00-12.mp4",
            "start_ms": PADDED_START,
            "end_ms": PADDED_END,
        }],
        "subtitle_redelivery_baseline": plan.baseline.config,
    }
    canonical_out = tmp_path / "canonical-finalizer-out"
    canonical_out.mkdir()
    recut = finalization._materialize_final_recut(
        spec=spec, cid=CID, out_root=canonical_out,
        padded=plan.padded_path, padded_provenance_path=padded_provenance,
        piece_provenance_rows=[{
            "source_path": "/recording/22966160_20260815-13-00-12.mp4",
            "source_sha256": "297cfb07b04b006ac76307ad0369af7160a62f64d04d315ddcfa7146d2625077",
        }],
        final_start=FINAL_START, final_end=FINAL_END, sanitized=[], timing_qa={},
        text_override_path=None, adapters=adapters, spec_parent=ROOT,
        chat_authority_audit={},
        reviewed_baseline_replay_c12_projection=projection,
    )
    assert recut.redelivery_baseline_audit is not None
    assert recut.redelivery_baseline_audit["application_strategy"] == (
        "exact_reviewed_interval_replay"
    )
    assert recut.redelivery_baseline_audit["final_delivery_projection"] == {
        "absolute_source_start_ms": 434_920,
        "absolute_source_end_ms": 581_030,
    }
    assert recut.subtitle_path.read_bytes() == (stage / "reviewed.srt").read_bytes()


def test_c12_full_operator_ownership_blocks_old_source_truth_reapplication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, stage, projection, _binding = _c12_plan_and_stage(tmp_path, monkeypatch)
    padded_provenance = plan.padded_path.with_suffix(".provenance.json")
    padded_provenance.write_text("{}\n", encoding="utf-8")

    def run_command(command: list[str], **_kwargs: object) -> None:
        Path(command[-1]).write_bytes(b"canonical-private-recut")

    def source_truth_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("C12 source truth must not mutate the sealed delivery")

    monkeypatch.setattr(
        finalization, "apply_source_subtitle_truth", source_truth_must_not_run
    )
    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=lambda **kwargs: ["recut", str(kwargs["output_media"])],
        run_command=run_command,
        write_source_range_srt=_write_source_range_srt,
        apply_text_override_document=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
        run_speaker_finalization=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
        burn_preview_subtitles=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
        stage_publish_draft=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
        generate_upload_tags=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
        delivery_root=lambda: tmp_path / "delivery",
    )
    spec = {
        "pieces": [{
            "remote_media": "/recording/22966160_20260815-13-00-12.mp4",
            "start_ms": PADDED_START,
            "end_ms": PADDED_END,
        }],
        "subtitle_redelivery_baseline": plan.baseline.config,
    }
    truth_audit = {
        "schema_version": "source-subtitle-truth-audit.v1",
        "status": "DEFERRED_TO_REDELIVERY_BASELINE",
        "deferred_strategy": (
            "exact_reviewed_interval_replay_then_reapply_source_truth"
        ),
        "applied": [],
        "satisfied": [],
        "failures": [{
            "truth_id": "historical-source-truth-row",
            "required": True,
            "source_start_ms": 434_920,
            "source_end_ms": 435_100,
        }],
    }
    chat_authority = {"source_subtitle_truth_audit": truth_audit}
    canonical_out = tmp_path / "canonical-finalizer-out"
    canonical_out.mkdir()

    recut = finalization._materialize_final_recut(
        spec=spec, cid=CID, out_root=canonical_out,
        padded=plan.padded_path, padded_provenance_path=padded_provenance,
        piece_provenance_rows=[{
            "source_path": "/recording/22966160_20260815-13-00-12.mp4",
            "source_sha256": "297cfb07b04b006ac76307ad0369af7160a62f64d04d315ddcfa7146d2625077",
        }],
        final_start=FINAL_START, final_end=FINAL_END, sanitized=[], timing_qa={},
        text_override_path=None, adapters=adapters, spec_parent=ROOT,
        chat_authority_audit=chat_authority,
        reviewed_baseline_replay_c12_projection=projection,
    )

    assert recut.subtitle_path.read_bytes() == (stage / "reviewed.srt").read_bytes()
    assert recut.redelivery_baseline_audit is not None
    post_truth = recut.redelivery_baseline_audit["source_truth_reapplication"]
    assert post_truth["status"] == "SUPERSEDED_BY_OPERATOR_TEXT_FULL_OWNERSHIP"
    assert post_truth["reapplication_attempted"] is False
    assert post_truth["superseded_truth_ids"] == ["historical-source-truth-row"]
    assert (
        recut.redelivery_baseline_audit["deferred_exact_replay_reverification"]
        ["reason_code"]
        == "C12_OPERATOR_TEXT_FULL_OWNERSHIP_SUPERSEDES_SOURCE_TRUTH_REAPPLICATION"
    )
    assert chat_authority["source_subtitle_truth_audit"] == post_truth


def test_c12_private_projection_rejects_content_grid_confusion_before_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, stage, projection, binding = _c12_plan_and_stage(tmp_path, monkeypatch)
    target = stage / "finalizer-output.srt"
    sentinel = b"do-not-overwrite"
    target.write_bytes(sentinel)
    wrong_binding = replace(
        binding,
        content_absolute_start_ms=434_920,
        content_absolute_end_ms=603_840,
    )

    with pytest.raises(C12FinalDeliveryProjectionError, match="GRID_BINDING_DRIFT"):
        replay_c12_final_delivery_projection(
            projection=projection, config=plan.baseline.config, cid=CID,
            final_start_ms=FINAL_START, final_end_ms=FINAL_END,
            binding=wrong_binding, subtitle_path=target,
            write_source_range_srt=_write_source_range_srt,
        )
    assert target.read_bytes() == sentinel


def test_c12_private_projection_rejects_stage_srt_hash_drift_before_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, stage, projection, binding = _c12_plan_and_stage(tmp_path, monkeypatch)
    target = stage / "finalizer-output.srt"
    sentinel = b"do-not-overwrite"
    target.write_bytes(sentinel)
    (stage / "reviewed.srt").write_bytes(b"tampered")

    with pytest.raises(C12FinalDeliveryProjectionError, match="STAGE_ARTIFACT_DRIFT"):
        replay_c12_final_delivery_projection(
            projection=projection, config=plan.baseline.config, cid=CID,
            final_start_ms=FINAL_START, final_end_ms=FINAL_END, binding=binding,
            subtitle_path=target, write_source_range_srt=_write_source_range_srt,
        )
    assert target.read_bytes() == sentinel


def test_c12_projection_is_candidate_locked_and_refuses_freeze_count_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, stage, _projection, _binding = _c12_plan_and_stage(tmp_path, monkeypatch)
    assert build_c12_final_delivery_projection(
        candidate_id="auto_113028_1602_1698", config=plan.baseline.config,
        record_sha256=replay.regular_binding(plan.record_path, label="RECORD").sha256,
        record_boundary={"final_start_ms": FINAL_START, "final_end_ms": FINAL_END},
        expected_video_sha256=plan.expected_video_sha256, padded_path=plan.padded_path,
        final_start_ms=FINAL_START, final_end_ms=FINAL_END, stage=stage,
    ) is None
    config = copy.deepcopy(plan.baseline.config)
    config["operator_text_full_ownership"]["operator_unchanged_freeze_cue_count"] = 55
    with pytest.raises(C12FinalDeliveryProjectionError, match="OWNERSHIP_PIN_INVALID"):
        build_c12_final_delivery_projection(
            candidate_id=CID, config=config,
            record_sha256=replay.regular_binding(plan.record_path, label="RECORD").sha256,
            record_boundary={"final_start_ms": FINAL_START, "final_end_ms": FINAL_END},
            expected_video_sha256=plan.expected_video_sha256, padded_path=plan.padded_path,
            final_start_ms=FINAL_START, final_end_ms=FINAL_END, stage=stage,
        )
