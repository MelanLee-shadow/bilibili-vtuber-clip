from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice import c5_start_clamp as c5
from src.autoslice.jingting_chunker import parse_srt_cues
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


def test_c5_start_clamp_is_revoked_for_every_runtime() -> None:
    assert c5.finalizer_authority_kwargs(
        candidate_id=c5.CANDIDATE_ID,
        recording_date=c5.RECORDING_DATE,
        runtime_root=Path("/runtime"),
    ) == {}


@pytest.mark.parametrize("include_historical_authority", [False, True])
def test_delivery_local_c5_can_never_enter_full_window_crop(
    tmp_path: Path, include_historical_authority: bool,
) -> None:
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
    kwargs: dict[str, object] = {
        "text": lane_bytes["pipeline_diagnostic"].decode("utf-8"),
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
    if include_historical_authority:
        kwargs.update(_c5_inputs(tmp_path))
    with pytest.raises(
        FullWindowReplayError,
        match="DELIVERY_LOCAL_BASELINE_CANNOT_REPLAY_FULL_WINDOW",
    ):
        replay_full_window_text_and_crop(**kwargs)


@pytest.mark.parametrize(
    ("candidate_id", "padded_start", "padded_end", "local_start", "local_end", "cue_count", "anchor_index", "anchor_start", "anchor_text"),
    [
        ("auto_113028_1271_1328", 1_261_170, 1_376_550, 9_750, 67_524, 21, 5, 9_560, "呃"),
        ("auto_113028_1602_1698", 1_592_760, 1_746_900, 9_750, 106_540, 20, 1, 250, "《线上直播间"),
    ],
)
def test_delivery_local_stage_projection_is_identity_without_cue_loss(
    tmp_path: Path,
    candidate_id: str,
    padded_start: int,
    padded_end: int,
    local_start: int,
    local_end: int,
    cue_count: int,
    anchor_index: int,
    anchor_start: int,
    anchor_text: str,
) -> None:
    baseline = load_candidate_reviewed_subtitle_baseline(
        ASSETS, candidate_id, repo_root=ROOT,
    )
    assert baseline is not None
    record = tmp_path / "record.json"
    record.write_text(json.dumps({
        "boundary_audit": {
            "final_start_ms": local_start,
            "final_end_ms": local_end,
        }
    }), encoding="utf-8")
    padded = tmp_path / f"padded_{padded_start}_{padded_end}.mp4"
    plan = SimpleNamespace(
        date="2026-08-14",
        candidate_id=candidate_id,
        record_path=record,
        padded_path=padded,
        local_start_ms=local_start,
        local_end_ms=local_end,
        baseline=baseline,
    )
    stage = tmp_path / "stage"
    stage.mkdir()
    projected, audit, descriptor = replay.prepare_stage_delivery_projection(
        plan,
        stage,
        SimpleNamespace(sha256="sha256:" + "1" * 64),
        regular_binding=replay.regular_binding,
        load_json=replay._load_json,
        read_small_bytes=replay._read_small_bytes,
        fresh_srt_to_source_cues=replay._fresh_srt_to_source_cues,
        write_source_range_srt=replay._write_source_range_srt,
        error=replay.ReviewedBaselineReplayError,
    )
    assert projected == baseline.baseline_path.read_bytes()
    assert audit["status"] in {"APPLIED", "ALREADY_SATISFIED"}
    assert descriptor is None
    assert not (stage / "full-release-delivery-projection.json").exists()
    cues = parse_srt_cues(projected.decode("utf-8"))
    assert len(cues) == cue_count
    assert cues[0].start_ms == 250
    anchor = cues[anchor_index - 1]
    assert (anchor.start_ms, anchor.text) == (anchor_start, anchor_text)


def test_stage_replay_never_passes_revoked_c5_authority(
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
        return b"1\n00:00:00,250 --> 00:00:01,000\ncorrect\n", {"status": "APPLIED"}, None

    monkeypatch.setattr(replay.subprocess, "run", fake_run)
    monkeypatch.setattr(replay, "prepare_stage_delivery_projection", fake_projection)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    replay.stage_replay(
        plan,
        stage_parent=parent,
        runtime_authority_root=tmp_path / "runtime",
    )
    assert not {
        "c5_start_clamp_proposal_path",
        "c5_start_clamp_acceptance_path",
        "recording_date",
    } & set(seen)
