from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.autoslice.fastlane_c3_private_stage as c3_stage
import src.autoslice.reviewed_baseline_replay as replay
import src.autoslice.reviewed_baseline_replay_stage_projection as projection
from src.autoslice.fastlane_c3_private_stage import (
    C3PrivateStageAuthorityError,
    resolve_c3_private_stage_geometry,
)
from src.autoslice.recut_materialization import _fresh_srt_to_source_cues, _write_source_range_srt
from src.autoslice.reviewed_subtitle_baseline_registry import load_candidate_reviewed_subtitle_baseline


ROOT = Path(__file__).resolve().parents[1]
CID = "auto_220021_561_670"
DATE = "2026-08-13"


def _plan(
    tmp_path: Path, *, padded: str = "padded_551960_718620.mp4", start: int = 9_690,
    end: int = 118_730, expected_video: object = "sha256:2e94ba7ae18e64903baca4cadb647b9545915778b19082e3cd0582d32e20c84e",
):
    baseline = load_candidate_reviewed_subtitle_baseline(
        ROOT / "assets/lidousha/reviewed_subtitle_baselines", CID, repo_root=ROOT
    )
    assert baseline is not None
    record = tmp_path / f"{CID}.record.json"
    record.write_text(json.dumps({"boundary_audit": {"final_start_ms": start, "final_end_ms": end}}))
    return SimpleNamespace(
        candidate_id=CID, date=DATE, baseline=baseline, record_path=record,
        padded_path=tmp_path / padded, local_start_ms=start, local_end_ms=end,
        expected_video_sha256=expected_video,
    )


def test_c3_stage_geometry_is_accepted_final_not_broad_padded_window(tmp_path: Path) -> None:
    geometry = resolve_c3_private_stage_geometry(_plan(tmp_path))
    assert geometry is not None
    assert geometry.binding.absolute_source_start_ms == 561_650
    assert geometry.binding.absolute_source_end_ms == 670_690
    assert (geometry.source_local_start_ms, geometry.source_local_end_ms) == (0, 109_040)
    assert (geometry.record_local_start_ms, geometry.record_local_end_ms) == (9_690, 118_730)
    # The broad media pad remains a provenance input, but is not subtitle authority.
    assert (geometry.binding.content_absolute_start_ms, geometry.binding.content_absolute_end_ms) == (551_960, 718_620)


def test_c3_stage_rejects_broad_padded_geometry_as_final_source(tmp_path: Path) -> None:
    with pytest.raises(C3PrivateStageAuthorityError, match="BROAD_PADDED_GEOMETRY"):
        resolve_c3_private_stage_geometry(_plan(tmp_path, start=0, end=109_040))


def test_c3_stage_helper_does_not_claim_other_candidates(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    plan.candidate_id = "auto_other"
    assert resolve_c3_private_stage_geometry(plan) is None


@pytest.mark.parametrize("expected_video", (None, "sha256:" + "0" * 64))
def test_c3_stage_requires_exact_expected_video(tmp_path: Path, expected_video: object) -> None:
    with pytest.raises(C3PrivateStageAuthorityError, match="FINAL_INTERVAL_DRIFT"):
        resolve_c3_private_stage_geometry(_plan(tmp_path, expected_video=expected_video))


def test_c3_stage_rejects_coordinated_proposal_reseal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = c3_stage._document

    def resealed(path: Path, **kwargs: object):
        document, digest = original(path, **kwargs)
        if path.name == f"{CID}.v1.json":
            document = deepcopy(document)
            document["permissions"]["provider_allowed"] = True
            document["authority_sha256"] = c3_stage._canonical_sha256(
                {key: value for key, value in document.items() if key != "authority_sha256"}
            )
            return document, "sha256:" + "f" * 64
        return document, digest

    monkeypatch.setattr(c3_stage, "_document", resealed)
    with pytest.raises(C3PrivateStageAuthorityError, match="PROPOSAL_FILE_DRIFT"):
        resolve_c3_private_stage_geometry(_plan(tmp_path))


def test_c3_stage_rejects_acceptance_byte_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = c3_stage._document

    def drifted(path: Path, **kwargs: object):
        document, digest = original(path, **kwargs)
        if path.name.endswith("accepted.v1.json"):
            return document, "sha256:" + "e" * 64
        return document, digest

    monkeypatch.setattr(c3_stage, "_document", drifted)
    with pytest.raises(C3PrivateStageAuthorityError, match="ACCEPTANCE_FILE_DRIFT"):
        resolve_c3_private_stage_geometry(_plan(tmp_path))


@pytest.mark.parametrize("field", ("speaker_srt_sha256", "ass_sha256", "burned_video_sha256"))
def test_c3_stage_rejects_nontext_final_grid_hash_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    original = c3_stage._document
    documents: dict[str, dict] = {}

    def resealed(path: Path, **kwargs: object):
        document, digest = original(path, **kwargs)
        documents[path.name] = deepcopy(document)
        if path.name == f"{CID}.v1.json":
            document = documents[path.name]
            document["final_grid"][field] = "sha256:" + "0" * 64
            document["authority_sha256"] = c3_stage._canonical_sha256(
                {key: value for key, value in document.items() if key != "authority_sha256"}
            )
            monkeypatch.setattr(c3_stage, "_PROPOSAL_SEAL", document["authority_sha256"])
            return document, c3_stage._PROPOSAL_FILE_SHA
        if path.name.endswith("accepted.v1.json"):
            document = documents[path.name]
            proposal = documents[f"{CID}.v1.json"]
            document["proposal"]["authority_sha256"] = proposal["authority_sha256"]
            document["acceptance_sha256"] = c3_stage._canonical_sha256(
                {key: value for key, value in document.items() if key != "acceptance_sha256"}
            )
            monkeypatch.setattr(c3_stage, "_ACCEPTANCE_SEAL", document["acceptance_sha256"])
            return document, c3_stage._ACCEPTANCE_FILE_SHA
        return document, digest

    monkeypatch.setattr(c3_stage, "_document", resealed)
    with pytest.raises(C3PrivateStageAuthorityError, match="FINAL_INTERVAL_DRIFT"):
        resolve_c3_private_stage_geometry(_plan(tmp_path))


def test_c3_stage_projection_uses_bound_final_geometry_not_broad_pad(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    geometry = resolve_c3_private_stage_geometry(plan)
    assert geometry is not None
    observed: dict[str, object] = {}

    def fake_apply(text: str, **kwargs: object) -> tuple[str, dict[str, object]]:
        observed.update(kwargs)
        return text, {"status": "ALREADY_SATISFIED"}

    monkeypatch.setattr(projection, "apply_redelivery_subtitle_baseline", fake_apply)
    stage = tmp_path / "stage"
    stage.mkdir()
    output, audit, descriptor = projection.prepare_stage_delivery_projection(
        plan, stage, SimpleNamespace(sha256="sha256:" + "1" * 64),
        regular_binding=replay.regular_binding, load_json=replay._load_json,
        read_small_bytes=replay._read_small_bytes,
        fresh_srt_to_source_cues=_fresh_srt_to_source_cues,
        write_source_range_srt=_write_source_range_srt,
        error=replay.ReviewedBaselineReplayError, c3_geometry=geometry,
    )
    assert output
    assert audit["status"] == "ALREADY_SATISFIED"
    assert descriptor is None
    assert observed["current_source_start_ms"] == 561_650
    assert observed["current_source_end_ms"] == 670_690
    assert observed["current_source_recording_basename"] == "22966160_20260813-22-00-21.mp4"
