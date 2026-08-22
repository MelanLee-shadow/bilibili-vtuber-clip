from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import src.autoslice.reviewed_baseline_replay as replay


ROOT = Path(__file__).resolve().parents[1]
CID = "auto_113028_1602_1698"
DATE = "2026-08-14"


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _package(tmp_path: Path, *, source_sha: str | None = None) -> tuple[Path, bytes]:
    package = tmp_path / "out" / DATE / CID / "replacement_recuts"
    package.mkdir(parents=True)
    media = b"old-record-media"
    padded = package.parent / "padded_1592760_1746900.mp4"
    padded.write_bytes(b"padded-bytes")
    (package / f"{CID}.record.json").write_text(json.dumps({
        "artifact_hashes": {"video_sha256": _sha(media)},
        "duration_ms": 96790,
        "boundary_audit": {"final_start_ms": 9750, "final_end_ms": 106540},
    }))
    (package / f"{CID}.recut.provenance.json").write_text(json.dumps({
        "final_recut": {
            "source_path": str(padded),
            "source_sha256": source_sha or hashlib.sha256(b"padded-bytes").hexdigest(),
        },
    }))
    return tmp_path / "out", media


def test_plan_is_read_only_and_binds_canonical_v2_v3_assets(tmp_path: Path) -> None:
    out_root, _media = _package(tmp_path)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    plan = replay.build_replay_plan(
        repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID
    )

    assert plan.local_start_ms == 9750
    assert plan.local_end_ms == 106540
    assert plan.baseline.config["schema_version"] == "subtitle-redelivery-baseline.v2"
    assert plan.baseline.config["operator_text_full_ownership"]["schema_version"] == "operator-reviewed-text-full-ownership-pin.v3"
    assert before == sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert all(row["predicate"] != "UPLOAD_ALLOWED" or row["status"] == "PASS_FALSE" for row in plan.matrix)


def test_plan_refuses_current_padded_source_hash_drift(tmp_path: Path) -> None:
    out_root, _media = _package(tmp_path, source_sha="0" * 64)
    with pytest.raises(replay.ReviewedBaselineReplayError, match="PADDED_SOURCE_DRIFT"):
        replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)


def test_stage_rebuilds_only_private_artifacts_and_preserves_expected_video_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_root, media = _package(tmp_path)
    plan = replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)

    def fake_run(command: list[str], **_kwargs: object) -> object:
        Path(command[-1]).write_bytes(media)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(replay.subprocess, "run", fake_run)
    monkeypatch.setattr(
        replay, "apply_redelivery_subtitle_baseline",
        lambda text, **_kwargs: (text, {"status": "APPLIED"}),
    )
    stage_parent = tmp_path / "private"
    stage_parent.mkdir(mode=0o700)
    result = replay.stage_replay(plan, stage_parent=stage_parent)

    stage = Path(result["stage"])
    assert {path.name for path in stage.iterdir()} == {
        "recut.mp4", "reviewed.srt", "redelivery-baseline.json", "stage.json",
    }
    assert (stage / "recut.mp4").is_file()
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in stage.iterdir() if path.name != "recut.mp4")
    assert _sha((stage / "recut.mp4").read_bytes()) == plan.expected_video_sha256
    assert not list((out_root / DATE / CID / "replacement_recuts").glob("*.stage.json"))


def test_stage_rejects_rebuilt_video_that_is_not_the_old_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_root, _media = _package(tmp_path)
    plan = replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)

    def fake_run(command: list[str], **_kwargs: object) -> object:
        Path(command[-1]).write_bytes(b"wrong-window")
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(replay.subprocess, "run", fake_run)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    with pytest.raises(replay.ReviewedBaselineReplayError, match="OLD_RECORD_VIDEO_SHA256_MISMATCH"):
        replay.stage_replay(plan, stage_parent=private)
