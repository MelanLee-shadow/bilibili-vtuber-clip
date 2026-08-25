from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice import c5_start_clamp as c5
from src.autoslice.recut_materialization import _write_source_range_srt
from src.autoslice.redelivery_full_window_replay import (
    FullWindowReplayError,
    replay_full_window_text_and_crop,
)
import src.autoslice.reviewed_baseline_replay as replay
from src.autoslice.reviewed_subtitle_baseline_registry import (
    load_candidate_reviewed_subtitle_baseline,
)


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets" / "lidousha" / "reviewed_subtitle_baselines"


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _c5_inputs(tmp_path: Path) -> dict[str, object]:
    runtime = tmp_path / "runtime"
    authority = runtime / ".private-c5-start-clamp-authority"
    authority.mkdir(parents=True, mode=0o700)
    proposal_path, acceptance_path = c5.runtime_authority_paths(runtime)
    proposal_path.write_bytes(
        (ROOT / "docs" / "reviews" / "auto_113028_1271_1328-c5-start-clamp-proposal.v1.json").read_bytes()
    )
    proposal, proposal_sha = c5.load_proposal(proposal_path)
    acceptance = c5.build_accepted_authority(
        proposal_path=proposal_path,
        proposal_file_sha256=proposal_sha,
        proposal_self_sha256=str(proposal["self_sha256"]),
        expectations=c5.C5_ACCEPTANCE_EXPECTATIONS,
    )
    c5.materialize_accepted_authority(
        acceptance_path,
        proposal_path=proposal_path,
        proposal=proposal,
        proposal_file_sha256=proposal_sha,
        acceptance=acceptance,
        expectations=c5.C5_ACCEPTANCE_EXPECTATIONS,
    )
    return {
        "c5_start_clamp_proposal_path": proposal_path,
        "c5_start_clamp_acceptance_path": acceptance_path,
        "recording_date": c5.RECORDING_DATE,
    }


def _replay_kwargs(tmp_path: Path) -> dict[str, object]:
    baseline = load_candidate_reviewed_subtitle_baseline(
        ASSETS, c5.CANDIDATE_ID, repo_root=ROOT,
    )
    assert baseline is not None
    config = baseline.config
    lanes = config["operator_truth_lanes"]
    assert isinstance(lanes, dict)
    lane_bytes = {
        name: (ASSETS / str(value["path"])).read_bytes()
        for name, value in lanes.items()
        if name in {"pipeline_diagnostic", "decision_ledger", "diff_receipt"}
    }
    diagnostic = lane_bytes["pipeline_diagnostic"].decode("utf-8")
    return {
        "text": diagnostic,
        "config": config,
        "spec_parent": ASSETS,
        "padded_start_ms": 1_261_170,
        "padded_end_ms": 1_376_550,
        "final_start_ms": 9_750,
        "final_end_ms": 67_524,
        "write_source_range_srt": _write_source_range_srt,
        "crop_path": tmp_path / "reviewed.srt",
        "read_crop": lambda path: path.read_bytes(),
        "projection_receipt_path": tmp_path / "receipt.json",
        "projection_candidate_id": c5.CANDIDATE_ID,
        "projection_record_sha256": "sha256:" + "1" * 64,
        "projection_record_boundary": {"final_start_ms": 9_750, "final_end_ms": 67_524},
        "projection_staged_media_sha256": str(c5.BOUNDARY["media_sha256"]),
        "projection_lane_bytes": lane_bytes,
    }


def test_c5_stage_projection_requires_all_authority_inputs(tmp_path: Path) -> None:
    kwargs = _replay_kwargs(tmp_path)
    with pytest.raises(FullWindowReplayError, match="DELIVERY_PROJECTION_STRADDLER"):
        replay_full_window_text_and_crop(**kwargs)


def test_c5_stage_projection_uses_accepted_clamp_at_first_projection(tmp_path: Path) -> None:
    kwargs = _replay_kwargs(tmp_path)
    kwargs.update(_c5_inputs(tmp_path))
    cropped, audit = replay_full_window_text_and_crop(**kwargs)
    assert cropped.startswith(b"1\n00:00:00,000 --> 00:00:00,330\n\xe5\x91\x83\n")
    receipt = kwargs["projection_receipt_path"]
    assert isinstance(receipt, Path)
    assert _sha(receipt) == audit["full_release_delivery_projection"]["receipt_sha256"]


def test_stage_replay_passes_only_c5_runtime_authority_to_early_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = b"old-record-media"
    record = tmp_path / "record.json"
    record.write_text("{}", encoding="utf-8")
    plan = SimpleNamespace(
        date=c5.RECORDING_DATE,
        candidate_id=c5.CANDIDATE_ID,
        record_path=record,
        padded_path=tmp_path / "padded_0_67524.mp4",
        local_start_ms=9_750,
        local_end_ms=67_524,
        expected_video_sha256="sha256:" + hashlib.sha256(media).hexdigest(),
        baseline=SimpleNamespace(
            config={"sha256": "0" * 64}, manifest_path=tmp_path / "baseline.json",
        ),
        matrix=(),
    )
    seen: dict[str, object] = {}

    def fake_run(command: list[str], **_kwargs: object) -> object:
        Path(command[-1]).write_bytes(media)
        return type("Completed", (), {"returncode": 0})()

    def fake_projection(*_args: object, **kwargs: object) -> tuple[bytes, dict[str, str], None]:
        seen.update(kwargs)
        return b"1\n00:00:00,000 --> 00:00:00,330\n\xe5\x91\x83\n", {"status": "APPLIED"}, None

    monkeypatch.setattr(replay.subprocess, "run", fake_run)
    monkeypatch.setattr(replay, "prepare_stage_delivery_projection", fake_projection)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    runtime = tmp_path / "runtime"
    replay.stage_replay(plan, stage_parent=parent, runtime_authority_root=runtime)
    proposal_path, acceptance_path = c5.runtime_authority_paths(runtime)
    assert seen["c5_start_clamp_proposal_path"] == proposal_path
    assert seen["c5_start_clamp_acceptance_path"] == acceptance_path
    assert seen["recording_date"] == c5.RECORDING_DATE
