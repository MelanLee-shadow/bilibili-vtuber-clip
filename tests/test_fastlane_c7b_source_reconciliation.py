from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from src.autoslice.fastlane_c7b_source_reconciliation import (
    C7bSourceReconciliationError,
    resolve_c7b_delivery_end_clamp,
    resolve_c7b_delivery_start_clamp,
    resolve_c7b_source_reconciliation,
)
from src.autoslice import redelivery_full_window_replay as full_window
from src.autoslice.recut_materialization import _write_source_range_srt
from src.autoslice.redelivery_full_window_replay import replay_full_window_text_and_crop
from src.autoslice.redelivery_subtitle_baseline import apply_redelivery_subtitle_baseline


ROOT = Path(__file__).resolve().parents[1]
CID = "auto_130040_201_255"
DATE = "2026-08-14"


def _inputs() -> tuple[object, dict[str, object], object, dict[str, object], object]:
    record = {
        "artifact_hashes": {"video_sha256": "sha256:19c4bfa64f0f3bed347b992b4059f48b6d92917308cb189b16d99362caab9068"},
        "duration_ms": 54170,
        "boundary_audit": {"final_start_ms": 9750, "final_end_ms": 63920},
    }
    provenance = {"final_recut": {
        "output_sha256": "09c42e6cab8b35891f7b307dcfd4e23323073f30915ffc36bcbe2f33acdb1798",
        "source_sha256": "5b06a7bb19e22c0a8c368c83ee83170ff068b04d32fd07cf246859e8f09014f0",
        "absolute_source_start_ms": 200940, "absolute_source_end_ms": 281830,
        "start_ms": 9750, "end_ms": 90640,
    }}
    return (
        SimpleNamespace(sha256="sha256:b875d8ddedaa47971249e3af057b218f45e62fb002ddcf6c9733cd38d5bfd8f5"), record,
        SimpleNamespace(sha256="sha256:7b13b5fa7a7f862e90c6e07bbef51a1809b0b9fb2792aeef7dc42d45a866d626"), provenance,
        SimpleNamespace(sha256="sha256:5b06a7bb19e22c0a8c368c83ee83170ff068b04d32fd07cf246859e8f09014f0"),
        SimpleNamespace(path=Path("/private/auto_130040_201_255.recut.mp4"), sha256="sha256:09c42e6cab8b35891f7b307dcfd4e23323073f30915ffc36bcbe2f33acdb1798"),
    )


def test_c7b_reconciliation_only_replaces_the_exact_stale_technical_hash() -> None:
    record_binding, record, provenance_binding, provenance, padded_binding, actual_binding = _inputs()
    resolved = resolve_c7b_source_reconciliation(
        repo_root=ROOT, date=DATE, candidate_id=CID, record_binding=record_binding,
        record=record, provenance_binding=provenance_binding, provenance=provenance,
        padded_binding=padded_binding, actual_binding=actual_binding,
    )
    assert resolved is not None
    assert resolved.expected_video_sha256 == "sha256:09c42e6cab8b35891f7b307dcfd4e23323073f30915ffc36bcbe2f33acdb1798"
    assert (resolved.materialization_start_ms, resolved.materialization_end_ms) == (9750, 90640)
    assert resolve_c7b_source_reconciliation(
        repo_root=ROOT, date=DATE, candidate_id="ordinary", record_binding=record_binding,
        record=record, provenance_binding=provenance_binding, provenance=provenance,
        padded_binding=padded_binding, actual_binding=actual_binding,
    ) is None


def test_c7b_reconciliation_fails_closed_on_record_or_provenance_drift() -> None:
    record_binding, record, provenance_binding, provenance, padded_binding, actual_binding = _inputs()
    record["boundary_audit"] = {"final_start_ms": 9750, "final_end_ms": 90640}
    with pytest.raises(C7bSourceReconciliationError, match="C7B_SOURCE_PROVENANCE_DRIFT"):
        resolve_c7b_source_reconciliation(
            repo_root=ROOT, date=DATE, candidate_id=CID, record_binding=record_binding,
            record=record, provenance_binding=provenance_binding, provenance=provenance,
            padded_binding=padded_binding, actual_binding=actual_binding,
        )


def test_c7b_baseline_resolves_its_sealed_relative_lanes_from_manifest_parent() -> None:
    manifest = ROOT / "assets/lidousha/reviewed_subtitle_baselines" / f"{CID}.subtitle-baseline.v1.json"
    import json
    config = json.loads(manifest.read_text(encoding="utf-8"))
    source = (manifest.parent / config["operator_truth_lanes"]["pipeline_diagnostic"]["path"]).read_text(encoding="utf-8")
    _output, audit = apply_redelivery_subtitle_baseline(
        source, config=config, spec_parent=manifest.parent,
        current_source_start_ms=191190, current_source_end_ms=303140,
        current_source_recording_basename="22966160_20260814-13-00-40.mp4",
        current_source_sha256="660f609ca46cf9b6a5d7618df290ffd8cf32e54677813be343067719d8616b54",
    )
    assert audit["status"] in {"APPLIED", "ALREADY_SATISFIED"}


def test_c7b_full_window_replay_forwards_candidate_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    manifest = ROOT / "assets/lidousha/reviewed_subtitle_baselines" / f"{CID}.subtitle-baseline.v1.json"
    config = json.loads(manifest.read_text(encoding="utf-8"))
    text = (manifest.parent / config["operator_truth_lanes"]["pipeline_diagnostic"]["path"]).read_text(encoding="utf-8")
    seen: dict[str, object] = {}

    def fake_apply(current: str, **kwargs: object) -> tuple[str, dict[str, object]]:
        seen.update(kwargs)
        return current, {"status": "APPLIED"}

    monkeypatch.setattr(full_window, "apply_redelivery_subtitle_baseline", fake_apply)
    monkeypatch.setattr(full_window, "_build_full_release_delivery_projection_receipt", lambda **_kwargs: None)
    _cropped, audit = replay_full_window_text_and_crop(
        text=text,
        config=config,
        spec_parent=manifest.parent,
        padded_start_ms=191190,
        padded_end_ms=303140,
        final_start_ms=9750,
        final_end_ms=63920,
        write_source_range_srt=_write_source_range_srt,
        crop_path=tmp_path / "reviewed.srt",
        read_crop=lambda path: path.read_bytes(),
        projection_receipt_path=tmp_path / "receipt.json",
        projection_candidate_id=CID,
        projection_record_sha256="sha256:" + "a" * 64,
        projection_record_boundary={"final_start_ms": 9750, "final_end_ms": 63920},
        projection_staged_media_sha256="sha256:" + "b" * 64,
        projection_lane_bytes={},
        recording_date=DATE,
    )
    assert audit["status"] == "APPLIED"
    assert seen["candidate_id"] == CID
    assert seen["recording_date"] == DATE


def test_non_c7b_mapping_operator_authority_is_rejected() -> None:
    import json
    manifest = ROOT / "assets/lidousha/reviewed_subtitle_baselines" / f"{CID}.subtitle-baseline.v1.json"
    config = json.loads(manifest.read_text(encoding="utf-8"))
    pipeline = manifest.parent / config["operator_truth_lanes"]["pipeline_diagnostic"]["path"]
    config["candidate_id"] = "ordinary_candidate"
    source = pipeline.read_text(encoding="utf-8")
    output, audit = apply_redelivery_subtitle_baseline(
        source, config=config, spec_parent=manifest.parent,
        current_source_start_ms=191190, current_source_end_ms=303140,
        current_source_recording_basename="22966160_20260814-13-00-40.mp4",
        current_source_sha256="660f609ca46cf9b6a5d7618df290ffd8cf32e54677813be343067719d8616b54",
        candidate_id="ordinary_candidate", recording_date=DATE,
    )
    assert output == source
    assert audit["status"] == "FAILED"
    assert audit["failures"][0]["reason_code"] == "REDELIVERY_OPERATOR_DROP_RELEASE_MAPPING_INVALID"


def test_c7b_boundary_clamps_are_exact_and_fail_closed() -> None:
    common = dict(repo_root=ROOT, candidate_id=CID, recording_date=DATE,
                  record_sha256="sha256:b875d8ddedaa47971249e3af057b218f45e62fb002ddcf6c9733cd38d5bfd8f5",
                  staged_media_sha256="sha256:09c42e6cab8b35891f7b307dcfd4e23323073f30915ffc36bcbe2f33acdb1798",
                  final_start_ms=9750, final_end_ms=63920)
    assert resolve_c7b_delivery_start_clamp(**common, source_ordinal=5, text="呵呵，李豆沙还是太好了", source_start_ms=8630, source_end_ms=12230) == (0, 2480)
    assert resolve_c7b_delivery_end_clamp(**common, source_ordinal=27, text="泡泡机，泡泡机、泡泡机", source_start_ms=62380, source_end_ms=64180) == (52630, 54170)
    with pytest.raises(C7bSourceReconciliationError, match="END_CLAMP_DRIFT"):
        resolve_c7b_delivery_end_clamp(**common, source_ordinal=27, text="漂移", source_start_ms=62380, source_end_ms=64180)
