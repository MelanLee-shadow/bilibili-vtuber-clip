from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
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


def test_private_renderer_receives_verified_private_media_not_public_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_root, media = _package(tmp_path)
    plan = replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)
    monkeypatch.setattr(
        replay.subprocess, "run",
        lambda command, **_kwargs: (Path(command[-1]).write_bytes(media), type("Done", (), {"returncode": 0})())[1],
    )
    monkeypatch.setattr(replay, "apply_redelivery_subtitle_baseline", lambda text, **_kwargs: (text, {"status": "APPLIED"}))
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    stage = Path(replay.stage_replay(plan, stage_parent=parent)["stage"])
    seen: dict[str, object] = {}

    def renderer(**kwargs: object):
        seen.update(kwargs)
        directory = Path(kwargs["stage_parent"]) / "render"
        directory.mkdir()
        burned = directory / "burned.mp4"
        burned.write_bytes(b"burned")
        burned.chmod(0o600)
        return directory, directory / "srt", None, None, None, None, {"status": "BURNED", "path": str(burned)}

    rendered = replay.render_private_replay(
        plan, stage=stage, speaker_mode="uniform_host", speaker_overrides=None,
        speaker_python=Path("/usr/bin/python3"), branding_intro=None, renderer=renderer,
    )
    assert seen["media_source"] == stage / "recut.mp4"
    assert seen["stage_parent"] == stage
    assert rendered.burned_media.path.name == "burned.mp4"


def test_after_image_is_provider_gated_and_never_writes_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_root, media = _package(tmp_path)
    recut_root = out_root / DATE / CID / "replacement_recuts"
    live_media = recut_root / "live.mp4"
    live_subtitle = recut_root / "live.srt"
    live_burned = recut_root / "live.burned.mp4"
    live_media.write_bytes(b"old")
    live_subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nold\n")
    live_burned.write_bytes(b"old-burn")
    record_path = recut_root / f"{CID}.record.json"
    record = json.loads(record_path.read_text())
    record.update({
        "media_path": str(live_media), "subtitle_path": str(live_subtitle),
        "burned_preview": {"path": str(live_burned), "status": "BURNED"},
    })
    record_path.write_text(json.dumps(record))
    plan = replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)
    monkeypatch.setattr(replay.subprocess, "run", lambda command, **_kwargs: (Path(command[-1]).write_bytes(media), type("Done", (), {"returncode": 0})())[1])
    monkeypatch.setattr(replay, "apply_redelivery_subtitle_baseline", lambda text, **_kwargs: (text, {"status": "APPLIED"}))
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    staged = replay.stage_replay(plan, stage_parent=parent)
    stage = Path(staged["stage"])
    burn = stage / "render" / "burned.mp4"
    burn.parent.mkdir()
    burn.write_bytes(b"burned")
    burn.chmod(0o600)
    rendered = replay.PrivateRenderedReplay(
        stage=burn.parent, reviewed_srt=replay.regular_binding(stage / "reviewed.srt", label="TEST"),
        speaker_srt=None, speaker_ass=None, speaker_manifest=None,
        burned_media=replay.regular_binding(burn, label="TEST"),
        burned_preview={"status": "BURNED", "path": str(burn)}, branding_intro=None,
    )
    runtime = tmp_path / "runtime"
    (runtime / "repo").mkdir(parents=True)
    (runtime / "repo" / "DEPLOYED_COMMIT").write_text("commit\n")
    (runtime / "repo" / "DEPLOYED_AUTHORITY_MANIFEST").write_text("{}\n")
    state_path = runtime / "state" / f"{DATE}.json"
    state_path.parent.mkdir()
    before = {"picks": [{"candidate_id": CID, "status": "candidate_rejected"}]}
    state_path.write_text(json.dumps(before, ensure_ascii=False, indent=2))
    state_before = state_path.read_bytes()
    prepared = replay.prepare_replay_after_image(
        plan, staged=staged, rendered=rendered, runtime_root=runtime,
        state_path=state_path, source_fact_llm=None,
    )
    assert prepared.status == "NEEDS_PROVIDER"
    assert prepared.after_image is None
    assert state_path.read_bytes() == state_before
    assert live_media.read_bytes() == b"old"


def test_after_image_refuses_nonpass_source_fact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The source-fact adapter is injectable, but a non-PASS answer cannot be
    # transformed into a transaction handle or a delivery mutation.
    out_root, _ = _package(tmp_path)
    plan = replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)
    prepared = replay.prepare_replay_after_image(
        plan, staged={}, rendered=None, runtime_root=tmp_path,
        state_path=tmp_path / "missing.json", source_fact_llm=lambda _request: {"status": "FAIL"},
    )
    assert prepared.status == "NEEDS_PROVIDER"


def test_synthesized_private_finalizer_uses_prepare_only_and_private_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_root, _ = _package(tmp_path)
    recut = out_root / DATE / CID / "replacement_recuts"
    chat = recut / "chat.json"
    clip = recut / "clip.json"
    chat.write_text("{}\n")
    clip.write_text(json.dumps({"recording_date": DATE}))
    record_path = recut / f"{CID}.record.json"
    record = json.loads(record_path.read_text())
    record.update({
        "subtitle_timing_qa": {}, "chat_authority_audit_path": str(chat),
        "clip_context_path": str(clip), "publish_staging": {"title": "旧标题", "selection_hook": "钩子", "cover_generation": {}},
        "artifact_hashes": {**record["artifact_hashes"], "chat_authority_audit_sha256": _sha(chat.read_bytes())},
    })
    record_path.write_text(json.dumps(record))
    plan = replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)

    @dataclass(frozen=True)
    class Adapters:
        stage_publish_draft: object = None
        delivery_root: object = None

    calls: dict[str, object] = {}
    def fake_finalizer(**kwargs: object) -> int:
        calls.update(kwargs)
        runtime = Path(kwargs["out_root"]).parents[2]
        prepared = runtime / ".producer-prepared" / "talk" / CID / "seed"
        prepared.mkdir(parents=True)
        (prepared / "prepared.json").write_text("{}\n")
        (prepared / "prepared.json").chmod(0o600)
        return 0

    stage = tmp_path / "private"
    stage.mkdir(mode=0o700)
    result = replay.synthesize_replay_spec_and_finalize_private(
        plan, stage=stage, speaker_python=Path("/usr/bin/python3"),
        source_fact_llm=lambda *_args, **_kwargs: "", adapters=Adapters(), finalizer=fake_finalizer,
    )
    assert calls["options"].prepare_only is True
    assert calls["spec"]["given_title"] is None
    assert calls["spec"]["recovery_publication_authority"] is None
    assert result.prepared_manifest.is_relative_to(result.private_runtime_root)
    assert not list(recut.glob("*.prepared.json"))
