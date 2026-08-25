from __future__ import annotations

import hashlib
import json
import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.autoslice.reviewed_baseline_replay as replay
import src.autoslice.reviewed_baseline_replay_authority as replay_authority
import src.autoslice.reviewed_baseline_replay_publish as replay_publish
from src.autoslice import llm_client
from src.autoslice.published_recovery_package_contract import (
    REGISTRY_REPO_PATH,
    REGISTRY_SHA256,
)
from src.autoslice.recovery_title_authority import (
    build_recovery_publication_authorities,
)
from src.autoslice.repository_asset_authority import _canonical_sha256
from src.autoslice.reviewed_baseline_replay_setup import resolve_replay_exact_final_reviewer


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
    padded.with_suffix(".provenance.json").write_text(json.dumps({
        "output_path": str(padded.resolve()),
        "output_sha256": _sha(padded.read_bytes()),
        "inputs": [{"source": "/recording/source.flv"}],
    }))
    (package / f"{CID}.record.json").write_text(json.dumps({
        "artifact_hashes": {"video_sha256": _sha(media)},
        "duration_ms": 96790,
        "boundary_audit": {"final_start_ms": 9750, "final_end_ms": 106540},
    }))
    (package / f"{CID}.recut.provenance.json").write_text(json.dumps({
        "source_piece": {
            "start_ms": 1592760, "end_ms": 1746900,
            "source_path": "/recording/source.flv",
            "source_media_binding": "sha256:" + "1" * 64,
        },
        "padded": {"output_path": str(padded), "start_ms": 1592760, "end_ms": 1746900},
        "final_recut": {
            "source_path": str(padded),
            "source_sha256": source_sha or hashlib.sha256(b"padded-bytes").hexdigest(),
            # This is the current R5-overwritten observation, not old record
            # authority.  It deliberately differs from 9750..106540.
            "start_ms": 0, "end_ms": 106540,
        },
    }))
    return tmp_path / "out", media


def _runtime_authority(root: Path) -> Path:
    runtime = root / "runtime-authority"
    repo = runtime / "repo"
    repo.mkdir(parents=True)
    commit = "a" * 40
    body = {
        "schema_version": "deployed-authority-manifest.v1",
        "deployed_commit": commit,
        "entries": {"docs/pipeline/80-package-delivery.md": {
            "bytes": 1, "sha256": "sha256:" + "b" * 64,
        }},
    }
    manifest = {**body, "manifest_sha256": _canonical_sha256(body)}
    (repo / "DEPLOYED_COMMIT").write_text(commit + "\n")
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text(json.dumps(manifest))
    return runtime


def _runtime_cpa_env(runtime: Path, payload: str) -> Path:
    runtime.mkdir(parents=True, exist_ok=True)
    path = runtime / "cpa.env"
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_runtime_cpa_command_environment_is_private_and_child_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    _runtime_cpa_env(runtime, "\n".join((
        "# ordinary config",
        "GEMINI_PAID_BACKUP_DAILY_CAP",  # ignored before parsing
        "export CPA_BASE_URL='https://runtime.example.test/v1'",
        "CPA_API_KEY='runtime-secret'",
        "",
    )))
    monkeypatch.setenv("CPA_BASE_URL", "https://ambient.invalid/v1")
    monkeypatch.setenv("CPA_API_KEY", "ambient-secret")
    monkeypatch.setenv("CPA_AMBIENT_EXTRA", "preserved-cpa-value")
    monkeypatch.setenv("UNRELATED", "preserved")
    child_env = llm_client.runtime_cpa_command_environment(runtime, _owner_uid=os.getuid())
    assert child_env["CPA_BASE_URL"] == "https://runtime.example.test/v1"
    assert child_env["CPA_API_KEY"] == "runtime-secret"
    assert child_env["CPA_AMBIENT_EXTRA"] == "preserved-cpa-value"
    assert child_env["UNRELATED"] == "preserved"
    assert os.environ["CPA_BASE_URL"] == "https://ambient.invalid/v1"
    assert os.environ["CPA_API_KEY"] == "ambient-secret"

    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> object:
        observed["env"] = kwargs["env"]
        completion = next(Path(part) for part in command if part.endswith("completion.txt"))
        completion.write_text("ok", encoding="utf-8")
        return type("Completed", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(llm_client.subprocess, "run", fake_run)
    config = llm_client.LlmConfig(
        transport="command", command_template="echo {prompt_file} {completion_file}",
        command_child_env=child_env,
    )
    assert "runtime-secret" not in repr(config)
    assert llm_client._call_command("fixture", config) == "ok"
    assert observed["env"] is child_env


def test_runtime_cpa_command_environment_accepts_private_symlink_and_refuses_bad_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    authority = tmp_path / "authority" / "cpa.env"
    authority.parent.mkdir()
    authority.write_text("CPA_BASE_URL=https://runtime.example/v1\nCPA_API_KEY=secret\n", encoding="utf-8")
    authority.chmod(0o600)
    (runtime / "cpa.env").symlink_to(authority)
    env = llm_client.runtime_cpa_command_environment(runtime, _owner_uid=os.getuid())
    assert env["CPA_BASE_URL"] == "https://runtime.example/v1"

    (runtime / "cpa.env").unlink()
    cpa = _runtime_cpa_env(runtime, "CPA_BASE_URL=https://runtime.example/v1\nCPA_API_KEY=secret\n")
    cpa.chmod(0o644)
    with pytest.raises(llm_client.LlmRuntimeEnvironmentError, match="UNSAFE"):
        llm_client.runtime_cpa_command_environment(runtime, _owner_uid=os.getuid())
    cpa.chmod(0o600)
    cpa.write_text("not even assignment\nCPA_BROKEN\n", encoding="utf-8")
    with pytest.raises(llm_client.LlmRuntimeEnvironmentError, match="INVALID"):
        llm_client.runtime_cpa_command_environment(runtime, _owner_uid=os.getuid())

    cpa.write_text("CPA_BASE_URL=https://runtime.example/v1\nCPA_API_KEY=secret\n", encoding="utf-8")
    real_read = llm_client.os.read
    changed = False

    def drift_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        payload = real_read(descriptor, size)
        if not changed:
            changed = True
            cpa.write_text("CPA_BASE_URL=https://runtime.example/v1\nCPA_API_KEY=secret\n", encoding="utf-8")
        return payload

    monkeypatch.setattr(llm_client.os, "read", drift_read)
    with pytest.raises(llm_client.LlmRuntimeEnvironmentError, match="UNSAFE"):
        llm_client.runtime_cpa_command_environment(runtime, _owner_uid=os.getuid())


def test_production_replay_llm_binds_runtime_cpa_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    _runtime_cpa_env(runtime, "CPA_BASE_URL=https://runtime.example/v1\nCPA_API_KEY=runtime-secret\n")
    captured: dict[str, object] = {}

    def fake_build(config: llm_client.LlmConfig):
        captured["config"] = config
        return lambda _prompt: "{}"

    monkeypatch.setattr(llm_client, "build_llm_call", fake_build)
    real_environment = llm_client.runtime_cpa_command_environment
    monkeypatch.setattr(
        llm_client, "runtime_cpa_command_environment",
        lambda path: real_environment(path, _owner_uid=os.getuid()),
    )
    monkeypatch.setenv("CPA_BASE_URL", "https://ambient.invalid/v1")
    call = replay._production_llm_call(runtime_root=runtime, effort="medium")
    assert call("fixture") == "{}"
    config = captured["config"]
    assert isinstance(config, llm_client.LlmConfig)
    assert config.command_child_env is not None
    assert config.command_child_env["CPA_BASE_URL"] == "https://runtime.example/v1"
    assert os.environ["CPA_BASE_URL"] == "https://ambient.invalid/v1"
    assert shlex.split(config.command_template)[1] == str(
        runtime / "repo" / "scripts" / "llm_via_cpa.sh"
    )


def test_production_replay_llm_uses_runtime_bridge_outside_repo_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The replay CLI can run by absolute path from a non-repository CWD."""

    runtime = tmp_path / "runtime"
    bridge = runtime / "repo" / "scripts" / "llm_via_cpa.sh"
    _runtime_cpa_env(
        runtime,
        "CPA_BASE_URL=https://runtime.example/v1\nCPA_API_KEY=runtime-secret\n",
    )
    real_environment = llm_client.runtime_cpa_command_environment
    monkeypatch.setattr(
        llm_client,
        "runtime_cpa_command_environment",
        lambda path: real_environment(path, _owner_uid=os.getuid()),
    )
    observed: dict[str, list[str]] = {}

    def fake_call_command(_prompt: str, config: llm_client.LlmConfig) -> str:
        observed["command"] = shlex.split(config.command_template)
        return "{}"

    monkeypatch.setattr(llm_client, "_call_command", fake_call_command)
    outside_repo = tmp_path / "outside-repo"
    outside_repo.mkdir()
    monkeypatch.chdir(outside_repo)

    call = replay._production_llm_call(runtime_root=runtime, effort="low")
    assert call("fixture") == "{}"
    assert observed["command"][:2] == ["bash", str(bridge)]
    assert Path(observed["command"][1]).is_absolute()


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


def test_plan_rejects_the_old_full_piece_label_for_delivery_local_srt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_root, _media = _package(tmp_path)
    baseline = replay._baseline(repo_root=ROOT, candidate_id=CID)
    bad_config = dict(baseline.config)
    bad_config.update(
        absolute_source_start_ms=1_592_760,
        absolute_source_end_ms=1_746_900,
    )
    bad_baseline = type(baseline)(
        config=bad_config,
        manifest_path=baseline.manifest_path,
        baseline_path=baseline.baseline_path,
        operator_truth_lane_paths=baseline.operator_truth_lane_paths,
        exact_interval_authority_path=baseline.exact_interval_authority_path,
        exact_interval_authority=baseline.exact_interval_authority,
    )
    monkeypatch.setattr(replay, "_baseline", lambda **_kwargs: bad_baseline)
    with pytest.raises(
        replay.ReviewedBaselineReplayError,
        match="DELIVERY_INTERVAL_MISMATCH",
    ):
        replay.build_replay_plan(
            repo_root=ROOT,
            out_root=out_root,
            date=DATE,
            candidate_id=CID,
        )


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
    stage_parent = tmp_path / "private"
    stage_parent.mkdir(mode=0o700)
    result = replay.stage_replay(plan, stage_parent=stage_parent)

    stage = Path(result["stage"])
    assert {path.name for path in stage.iterdir()} == {
        "recut.mp4", "reviewed.srt", "redelivery-baseline.json", "stage.json",
    }
    assert (stage / "recut.mp4").is_file()
    reviewed = (stage / "reviewed.srt").read_text(encoding="utf-8")
    assert reviewed == plan.baseline.baseline_path.read_text(encoding="utf-8")
    assert len(replay._fresh_srt_to_source_cues(reviewed, window_start_ms=0, duration_ms=96_790)) == 20
    assert "00:00:00,250" in reviewed
    assert "《线上直播间" in reviewed
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in stage.iterdir() if path.name != "recut.mp4")
    assert _sha((stage / "recut.mp4").read_bytes()) == plan.expected_video_sha256
    stage_document = json.loads((stage / "stage.json").read_text(encoding="utf-8"))
    assert "delivery_projection_receipt" not in stage_document
    assert not list((out_root / DATE / CID / "replacement_recuts").glob("*.stage.json"))


def test_stage_replays_delivery_local_baseline_without_second_crop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delivery-local reviewed grid is identity-projected onto final media."""

    out_root, media = _package(tmp_path)
    plan = replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)

    def fake_run(command: list[str], **_kwargs: object) -> object:
        Path(command[-1]).write_bytes(media)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(replay.subprocess, "run", fake_run)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    result = replay.stage_replay(plan, stage_parent=parent)

    audit = json.loads((Path(result["stage"]) / "redelivery-baseline.json").read_text())
    assert audit["status"] in {"APPLIED", "ALREADY_SATISFIED"}
    assert audit["application_strategy"] == "exact_reviewed_interval_replay"
    assert audit["current_source_interval"] == {
        "absolute_source_start_ms": 1_602_510,
        "absolute_source_end_ms": 1_699_300,
    }
    reviewed = (Path(result["stage"]) / "reviewed.srt").read_text(encoding="utf-8")
    assert reviewed == plan.baseline.baseline_path.read_text(encoding="utf-8")


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


def test_stage_projection_keeps_non_c5_candidates_on_the_generic_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_root, media = _package(tmp_path)
    plan = replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)

    def fake_run(command: list[str], **_kwargs: object) -> object:
        Path(command[-1]).write_bytes(media)
        return type("Completed", (), {"returncode": 0})()

    seen: dict[str, object] = {}

    def fake_projection(*_args: object, **kwargs: object) -> tuple[bytes, dict[str, str], None]:
        seen.update(kwargs)
        return b"1\n00:00:00,000 --> 00:00:01,000\nfrozen\n", {"status": "APPLIED"}, None

    monkeypatch.setattr(replay.subprocess, "run", fake_run)
    monkeypatch.setattr(replay, "prepare_stage_delivery_projection", fake_projection)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    replay.stage_replay(plan, stage_parent=parent, runtime_authority_root=tmp_path / "runtime")
    assert not {"c5_start_clamp_proposal_path", "c5_start_clamp_acceptance_path", "recording_date"} & set(seen)


def test_locator_projection_rewrites_only_mutable_private_paths(tmp_path: Path) -> None:
    private = tmp_path / "private-runtime"
    private_package = private / DATE / CID / "replacement_recuts"
    private_candidate = private / DATE / CID
    private_repo = private / "repo"
    target_package = tmp_path / "out" / DATE / CID / "replacement_recuts"
    target_candidate = target_package.parent
    target_repo = tmp_path / "runtime" / "repo"
    document = {
        "media_path": str(private_package / "burned.mp4"),
        # A command/provenance leaf is frozen evidence, not a runtime
        # locator.  It must remain byte-identical even if it names staging.
        "burned_preview": {"command": ["ffmpeg", str(private_package / "burned.mp4")]},
        "story_contract": {"candidate_id": CID},
    }
    projected = replay._project_document_locators(
        document, kind="record",
        mappings=(
            (str(private_package), str(target_package)),
            (str(private_candidate), str(target_candidate)),
            (str(private_repo), str(target_repo)),
        ),
        private_runtime_root=private,
    )
    assert projected["media_path"] == str(target_package / "burned.mp4")
    assert projected["burned_preview"] == document["burned_preview"]

    with pytest.raises(replay.ReviewedBaselineReplayError, match="PRIVATE_LOCATOR_UNPROJECTABLE"):
        replay._project_document_locators(
            {"media_path": str(private / "not-a-package-root" / "burned.mp4")},
            kind="record",
            mappings=(
                (str(private_package), str(target_package)),
                (str(private_candidate), str(target_candidate)),
                (str(private_repo), str(target_repo)),
            ),
            private_runtime_root=private,
        )


@pytest.mark.parametrize(
    ("negative_case", "expected_error"),
    (
        (None, None),
        ("burned_hash", "REPLAY_PROJECTED_BURNED_VIDEO_HASH_INVALID"),
        ("cover_hash", "REPLAY_PROJECTED_COVER_HASH_STATUS_INVALID"),
        ("cover_status", "REPLAY_PROJECTED_COVER_HASH_STATUS_INVALID"),
        ("locator", "REPLAY_PROJECTED_LOCATOR_INVALID"),
        ("story_candidate", "REPLAY_PROJECTED_STORY_CANDIDATE_INVALID"),
    ),
)
def test_state_projection_restores_canonical_talk_fields_and_drops_rejection_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, negative_case: str | None,
    expected_error: str | None,
) -> None:
    out_root, _ = _package(tmp_path)
    plan = replay.build_replay_plan(
        repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID,
    )
    runtime = _runtime_authority(tmp_path)
    state_path = runtime / "state" / f"{DATE}.json"
    state_path.parent.mkdir()
    state_path.write_bytes(json.dumps({"picks": [{
        "cid": CID, "hook": "钩子", "status": "candidate_rejected", "rc": 1,
        "failure_kind": "terminal", "failure_stage": "final_review",
        "failure_message": "old", "failure_recoverable": True,
        "rejected_status": "BLOCKED", "rejection_reason": "old",
        "retry_reason": "retry", "sanctioned_revival_retry": {"keep": True},
        "subtitle_sha256": "sha256:" + "f" * 64,
    }]}).encode())
    projection_root = tmp_path / "projection"
    projection_root.mkdir()
    recut_root = plan.package_root / "replacement_recuts"
    video = recut_root / "burned.mp4"
    subtitle = recut_root / "burned.srt"
    cover = recut_root / "burned.cover.png"
    record = {
        "duration_ms": 96790, "media_path": str(video), "subtitle_path": str(subtitle),
        "story_contract": {"candidate_id": CID, "selection_hook": "钩子"},
        "boundary_audit": {"verdict": "CLEAN", "closure_sentence": "收束"},
        "subtitle_timing_qa": {"counts": {"cues": 2}},
        "artifact_hashes": {"burned_video_sha256": "sha256:" + "1" * 64},
    }
    publish = {
        "video_path": str(video), "cover_path": str(cover),
        "artifact_hashes": {"cover_sha256": "sha256:" + "3" * 64},
        "cover_status": "AI_COVER_READY",
        "cover_generation": {"status": "READY"}, "title": "冻结标题",
    }
    if negative_case == "burned_hash":
        record["artifact_hashes"]["burned_video_sha256"] = "sha256:" + "9" * 64
    elif negative_case == "cover_hash":
        publish["artifact_hashes"]["cover_sha256"] = "sha256:" + "9" * 64
    elif negative_case == "cover_status":
        publish["cover_status"] = "BLOCKED"
    elif negative_case == "locator":
        publish["cover_path"] = str(tmp_path / "outside.cover.png")
    elif negative_case == "story_candidate":
        record["story_contract"]["candidate_id"] = "different-candidate"
    for name, document in (("record", record), ("publish", publish), ("chat", {}), ("speaker", {"status": "READY"})):
        (projection_root / f"{name}.json").write_bytes(json.dumps(document).encode())
    live = replay.ReplayLiveProjection(
        record=replay.regular_binding(projection_root / "record.json", label="TEST_RECORD"),
        publish=replay.regular_binding(projection_root / "publish.json", label="TEST_PUBLISH"),
        chat=replay.regular_binding(projection_root / "chat.json", label="TEST_CHAT"),
        speaker_manifest=replay.regular_binding(projection_root / "speaker.json", label="TEST_SPEAKER"),
        private_delivery_root=tmp_path / "private-delivery",
        live_delivery_root=runtime / "repo" / "lidousha" / DATE,
    )
    delivery = {
        "video": {"source": "ignored", "target": str(live.live_delivery_root / "钩子__auto_113028_1602_1698.mp4"), "sha256": "sha256:" + "1" * 64},
        "subtitle": {"source": "ignored", "target": str(live.live_delivery_root / "钩子__auto_113028_1602_1698.srt"), "sha256": "sha256:" + "2" * 64},
        "cover": {"source": "ignored", "target": str(live.live_delivery_root / "钩子__auto_113028_1602_1698.cover.png"), "sha256": "sha256:" + "3" * 64},
        "speaker_srt": {"source": "ignored", "target": str(live.live_delivery_root / "钩子__auto_113028_1602_1698.speaker.srt"), "sha256": "sha256:" + "4" * 64},
        "speaker_ass": {"source": "ignored", "target": str(live.live_delivery_root / "钩子__auto_113028_1602_1698.speaker.ass"), "sha256": "sha256:" + "5" * 64},
        "record": {"source": "ignored", "target": str(live.live_delivery_root / "钩子__auto_113028_1602_1698.record.json"), "sha256": "sha256:" + "6" * 64},
        "publish": {"source": "ignored", "target": str(live.live_delivery_root / "钩子__auto_113028_1602_1698.publish.json"), "sha256": "sha256:" + "7" * 64},
        "chat_authority": {"source": "ignored", "target": str(live.live_delivery_root / "钩子__auto_113028_1602_1698.chat-authority.json"), "sha256": "sha256:" + "8" * 64},
        "speaker_manifest": {"source": "ignored", "target": str(live.live_delivery_root / "钩子__auto_113028_1602_1698.speaker.json"), "sha256": "sha256:" + "9" * 64},
    }
    monkeypatch.setattr(replay, "_delivery_artifacts_from_prepared", lambda *_args, **_kwargs: delivery)
    if expected_error is not None:
        with pytest.raises(replay.ReviewedBaselineReplayError, match=expected_error):
            replay.project_replay_state_after(
                plan, runtime_root=runtime, state_path=state_path,
                finalization=object(), projection=live,
            )
        return
    result = replay.project_replay_state_after(
        plan, runtime_root=runtime, state_path=state_path,
        finalization=object(), projection=live,
    )
    row = json.loads(result.after)["picks"][0]
    assert row["status"] == "review_ready" and row["rc"] == 0
    assert row["delivered"] == delivery["video"]["target"]
    assert row["summary"]["delivery"] == delivery["video"]["target"]
    assert row["video_sha256"] == delivery["video"]["sha256"]
    assert row["sanctioned_revival_retry"] == {"keep": True}
    assert row["retry_reason"] == "retry"
    assert row["cover_path"] == str(cover)
    assert not {"failure_kind", "rejected_status", "rejection_reason", "subtitle_sha256"} & set(row)


def test_rebind_reuses_sealed_live_projection_after_other_state_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serial state rebases must not recreate the create-only projection root."""

    from src.autoslice.reviewed_baseline_replay_transaction import ReplayAfterImage

    out_root, _ = _package(tmp_path)
    plan = replay.build_replay_plan(
        repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID,
    )
    runtime = _runtime_authority(tmp_path)
    state_path = runtime / "state" / f"{DATE}.json"
    state_path.parent.mkdir()
    state_path.write_bytes(b'{"generation":0}\n')
    private = tmp_path / "private-runtime"
    projection_root = private / "live-projection"
    projection_root.mkdir(parents=True)
    bindings = {}
    for name in ("record", "publish", "chat", "speaker"):
        path = projection_root / f"{CID}.{name}.json"
        path.write_text("{}\n")
        bindings[name] = replay.regular_binding(path, label=f"TEST_{name.upper()}")
    finalization = replay.PrivateReplayFinalization(
        spec_path=private / "replay-spec.json", private_runtime_root=private,
        prepared_manifest=private / "prepared.json", prepared_sha256="sha256:" + "a" * 64,
    )
    projection = replay.ReplayLiveProjection(
        record=bindings["record"], publish=bindings["publish"], chat=bindings["chat"],
        speaker_manifest=bindings["speaker"], private_delivery_root=private / "delivery" / DATE,
        live_delivery_root=runtime / "repo" / "lidousha" / DATE,
    )
    after = ReplayAfterImage(
        date=DATE, candidate_id=CID, deployed={}, state_path=state_path,
        state_before=b"before", state_after=b"after", record_before_sha256="sha256:" + "b" * 64,
        stage_sha256="sha256:" + "c" * 64, artifacts=(), upload_allowed=False,
    )
    monkeypatch.setattr(
        "src.autoslice.producer_delivery_transaction.deployment_authority_binding",
        lambda _root: {"deployed_commit": "same"},
    )
    monkeypatch.setattr(
        replay, "project_private_finalization_to_live",
        lambda *_args, **_kwargs: pytest.fail("rebind must reuse the retained projection"),
    )

    def state_projection(*_args, **_kwargs):
        current = state_path.read_bytes()
        return replay.ReplayStateProjection(before=current, after=current + b"rebased\n", delivered={})

    monkeypatch.setattr(replay, "project_replay_state_after", state_projection)
    # Simulate an earlier candidate's serial commit after the concurrent
    # prepare, then rebase this candidate against that new exact preimage.
    state_path.write_bytes(b'{"generation":1,"other_candidate":true}\n')
    rebound = replay.rebind_replay_after_image_state(
        plan, runtime_root=runtime, state_path=state_path,
        finalization=finalization, after=after, projection=projection,
    )
    assert rebound.state_before == b'{"generation":1,"other_candidate":true}\n'
    assert rebound.state_after.endswith(b"rebased\n")

    bindings["record"].path.write_text('{"tampered":true}\n')
    with pytest.raises(replay.ReviewedBaselineReplayError, match="LIVE_PROJECTION_DRIFT"):
        replay.rebind_replay_after_image_state(
            plan, runtime_root=runtime, state_path=state_path,
            finalization=finalization, after=after, projection=projection,
        )


def test_flatten_preserves_prepared_recut_basename_not_role_suffixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.autoslice.producer_delivery_transaction import DeliveryArtifact, prepare_delivery

    runtime = _runtime_authority(tmp_path)
    private = runtime / "private-runtime"
    (private / "repo").mkdir(parents=True)
    for name in ("DEPLOYED_COMMIT", "DEPLOYED_AUTHORITY_MANIFEST.json"):
        (private / "repo" / name).write_bytes((runtime / "repo" / name).read_bytes())
    source_root = private / DATE / CID / "replacement_recuts"
    source_root.mkdir(parents=True)
    sources = {}
    for role, suffix, payload in (
        ("video", ".recut.mp4", b"video"),
        ("subtitle", ".recut.srt", b"srt"),
        ("publish", ".recut.publish.json", b"{}"),
        ("record", ".recut.record.json", b"{}"),
        ("chat_authority", ".recut.chat-authority.json", b"{}"),
        ("speaker_manifest", ".recut.speaker-final.json", b"{}"),
    ):
        path = source_root / f"{CID}{suffix}"
        path.write_bytes(payload)
        sources[role] = path
    prepared = prepare_delivery(
        runtime_root=private, lane="talk", candidate_id=CID,
        artifacts=[
            DeliveryArtifact(role, source, private / "delivery" / DATE / source.name, _sha(source.read_bytes()))
            for role, source in sources.items()
        ],
    )
    spec = private / "replay-spec.json"
    spec.write_text(json.dumps({"output_root": str(private / DATE / CID)}))
    finalization = replay.PrivateReplayFinalization(
        spec, private, prepared.manifest_path, "sha256:" + prepared.prepared_sha256,
    )
    import scripts.audit_lidousha_review_package as package_audit
    import scripts.build_manual_review_manifest as manual

    monkeypatch.setattr(manual, "build_manual", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(package_audit, "audit_package", lambda _root: {
        "schema_version": "lidousha-review-package-audit.v2",
        "policy_epoch": package_audit.AUDIT_POLICY_EPOCH,
        "passed": True, "issue_count": 0, "blocking_issue_count": 0,
    })
    (source_root / "carried-from-old-package.txt").write_text("carry\n")
    plan = replay.ReplayPlan(
        DATE, CID, tmp_path / "recovery-target" / CID,
        source_root / f"{CID}.recut.record.json",
        source_root / "padded_0_1.mp4", 0, 1, "sha256:" + "0" * 64,
        object(), (),
    )
    package = replay.flatten_and_audit_private_replay(
        finalization, plan=plan, base_package_root=source_root,
    )
    assert (package.root / "carried-from-old-package.txt").read_text() == "carry\n"
    assert (package.root / f"{CID}.recut.publish.json").is_file()
    assert (package.root / f"{CID}.recut.speaker-final.json").is_file()
    assert (package.root / f"{CID}.publish.json").is_file()
    assert (package.root / f"{CID}.speaker.json").is_file()


def test_private_audit_receipt_binds_the_simulated_live_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The frozen audit has the actual target root and no private-only issue."""

    from src.autoslice.package_audit_binding import audit_content_binding
    from src.autoslice.producer_delivery_transaction import DeliveryArtifact, prepare_delivery
    import scripts.audit_lidousha_review_package as package_audit
    import scripts.build_manual_review_manifest as manual

    runtime = _runtime_authority(tmp_path)
    private = runtime / "private-runtime"
    (private / "repo").mkdir(parents=True)
    for name in ("DEPLOYED_COMMIT", "DEPLOYED_AUTHORITY_MANIFEST.json"):
        (private / "repo" / name).write_bytes((runtime / "repo" / name).read_bytes())
    source_root = private / DATE / CID / "replacement_recuts"
    (source_root / "covers").mkdir(parents=True)
    (source_root / "covers" / "carried.cover.png").write_bytes(b"cover")
    sources: dict[str, Path] = {}
    for role, suffix, payload in (
        ("video", ".recut.mp4", b"video"),
        ("subtitle", ".recut.srt", b"srt"),
        ("publish", ".recut.publish.json", b"{}"),
        ("record", ".recut.record.json", b"{}"),
        ("chat_authority", ".recut.chat-authority.json", b"{}"),
        ("speaker_manifest", ".recut.speaker-final.json", b"{}"),
    ):
        path = source_root / f"{CID}{suffix}"
        path.write_bytes(payload)
        sources[role] = path
    prepared = prepare_delivery(
        runtime_root=private, lane="talk", candidate_id=CID,
        artifacts=[
            DeliveryArtifact(role, source, private / "delivery" / DATE / source.name,
                             _sha(source.read_bytes()))
            for role, source in sources.items()
        ],
    )
    spec = private / "replay-spec.json"
    spec.write_text(json.dumps({"output_root": str(private / DATE / CID)}))
    finalization = replay.PrivateReplayFinalization(
        spec, private, prepared.manifest_path, "sha256:" + prepared.prepared_sha256,
    )
    plan = replay.ReplayPlan(
        DATE, CID, source_root.parent, source_root / f"{CID}.recut.record.json",
        source_root / "padded_0_1.mp4", 0, 1, "sha256:" + "0" * 64,
        object(), (),
    )
    monkeypatch.setattr(manual, "build_manual", lambda *_args, **_kwargs: {"ok": True})

    def fake_audit(root: Path) -> dict[str, object]:
        inputs = [
            [path.relative_to(root).as_posix(), _sha(path.read_bytes())]
            for path in sorted(root.rglob("*")) if path.is_file()
            and path.name != "package-audit.json"
        ]
        return {
            "schema_version": "lidousha-review-package-audit.v2",
            "policy_epoch": package_audit.AUDIT_POLICY_EPOCH,
            "passed": True, "root": str(root.resolve()),
            "audited_inputs": inputs, "issues": [],
            "issue_count": 0, "blocking_issue_count": 0,
        }

    monkeypatch.setattr(package_audit, "audit_package", fake_audit)
    package = replay.flatten_and_audit_private_replay(finalization, plan=plan)
    stored = json.loads(package.package_audit.path.read_text())
    simulated = fake_audit(package.root)
    simulated["root"] = str((plan.package_root / "replacement_recuts").resolve())
    assert audit_content_binding(stored) == audit_content_binding(simulated)


def test_private_audit_rejects_informational_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An INFO issue is content-bound by authorized upload and cannot freeze."""

    from src.autoslice.producer_delivery_transaction import DeliveryArtifact, prepare_delivery
    import scripts.audit_lidousha_review_package as package_audit
    import scripts.build_manual_review_manifest as manual

    runtime = _runtime_authority(tmp_path)
    private = runtime / "private-runtime"
    (private / "repo").mkdir(parents=True)
    for name in ("DEPLOYED_COMMIT", "DEPLOYED_AUTHORITY_MANIFEST.json"):
        (private / "repo" / name).write_bytes((runtime / "repo" / name).read_bytes())
    source_root = private / DATE / CID / "replacement_recuts"
    source_root.mkdir(parents=True)
    sources = {}
    for role, suffix in (("video", ".recut.mp4"), ("subtitle", ".recut.srt"),
                         ("publish", ".recut.publish.json"), ("record", ".recut.record.json"),
                         ("chat_authority", ".recut.chat-authority.json"),
                         ("speaker_manifest", ".recut.speaker-final.json")):
        path = source_root / f"{CID}{suffix}"
        path.write_bytes(b"x")
        sources[role] = path
    prepared = prepare_delivery(
        runtime_root=private, lane="talk", candidate_id=CID,
        artifacts=[DeliveryArtifact(role, source, private / "delivery" / DATE / source.name,
                                    _sha(source.read_bytes())) for role, source in sources.items()],
    )
    spec = private / "replay-spec.json"
    spec.write_text(json.dumps({"output_root": str(private / DATE / CID)}))
    finalization = replay.PrivateReplayFinalization(
        spec, private, prepared.manifest_path, "sha256:" + prepared.prepared_sha256,
    )
    plan = replay.ReplayPlan(DATE, CID, source_root.parent, source_root / f"{CID}.recut.record.json",
                             source_root / "padded_0_1.mp4", 0, 1,
                             "sha256:" + "0" * 64, object(), ())
    monkeypatch.setattr(manual, "build_manual", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(package_audit, "audit_package", lambda _root: {
        "schema_version": "lidousha-review-package-audit.v2",
        "policy_epoch": package_audit.AUDIT_POLICY_EPOCH,
        "passed": True, "issue_count": 1, "blocking_issue_count": 0,
    })
    with pytest.raises(replay.ReviewedBaselineReplayError, match="PACKAGE_AUDIT_BLOCKED"):
        replay.flatten_and_audit_private_replay(finalization, plan=plan)


def test_carried_cover_duplicate_schema_paths_copy_once_but_validate_each(
    tmp_path: Path,
) -> None:
    out_root, _ = _package(tmp_path)
    package = out_root / DATE / CID / "replacement_recuts"
    cover = package / "covers" / "one.png"
    cover.parent.mkdir()
    cover.write_bytes(b"one-sealed-cover")
    plan = replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)
    digest = _sha(cover.read_bytes())
    generation = {
        "final_cover": str(cover), "final_cover_sha256": digest,
        "pre_overlay_path": str(cover), "pre_overlay_sha256": digest,
        "ai_background": str(cover), "ai_background_sha256": digest,
        "reference_image": str(cover), "cover_reference_sha256": digest,
        "rendered_text_pixels": {
            "mask_path": str(cover), "mask_sha256": digest,
            "pre_overlay_path": str(cover), "pre_overlay_sha256": digest,
        },
    }
    private_package = tmp_path / "private" / "replacement_recuts"
    private_package.mkdir(parents=True)
    private_cover, carried = replay._private_carried_cover_generation(
        plan, private_package=private_package, cover_path=cover,
        cover_sha256=digest, generation=generation,
    )
    assert private_cover.read_bytes() == cover.read_bytes()
    assert [path for path in private_package.rglob("*") if path.is_file()] == [
        private_package / "covers" / "one.png"
    ]
    assert carried["final_cover"] == str(private_cover)
    assert carried["rendered_text_pixels"]["mask_path"] == str(private_cover)


def test_carried_cover_allows_only_hash_bound_runtime_asset_reference(
    tmp_path: Path,
) -> None:
    out_root, _ = _package(tmp_path)
    package = out_root / DATE / CID / "replacement_recuts"
    cover = package / "covers" / "cover.png"
    cover.parent.mkdir()
    cover.write_bytes(b"cover")
    runtime = _runtime_authority(tmp_path)
    reference = runtime / "assets" / "emote" / "reference.png"
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"trusted-runtime-reference")
    plan = replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)
    cover_sha, reference_sha = _sha(cover.read_bytes()), _sha(reference.read_bytes())
    generation = {
        "final_cover": str(cover), "final_cover_sha256": cover_sha,
        "pre_overlay_path": str(cover), "pre_overlay_sha256": cover_sha,
        "ai_background": str(cover), "ai_background_sha256": cover_sha,
        "reference_image": str(reference), "cover_reference_sha256": reference_sha,
        "rendered_text_pixels": {"mask_path": str(cover), "mask_sha256": cover_sha,
                                 "pre_overlay_path": str(cover), "pre_overlay_sha256": cover_sha},
    }
    private_package = tmp_path / "private" / "replacement_recuts"
    private_package.mkdir(parents=True)
    _private_cover, carried = replay._private_carried_cover_generation(
        plan, private_package=private_package, cover_path=cover, cover_sha256=cover_sha,
        generation=generation, runtime_authority_root=runtime,
    )
    expected = private_package / ".replay-carried-runtime-assets" / "emote" / "reference.png"
    assert carried["reference_image"] == str(expected)
    assert expected.read_bytes() == reference.read_bytes()

    generation["reference_image"] = str(tmp_path / "unrelated.png")
    (tmp_path / "unrelated.png").write_bytes(b"unrelated")
    generation["cover_reference_sha256"] = _sha((tmp_path / "unrelated.png").read_bytes())
    (tmp_path / "private2").mkdir()
    with pytest.raises(replay.ReviewedBaselineReplayError, match="COVER_CARRY_PATH_INVALID"):
        replay._private_carried_cover_generation(
            plan, private_package=tmp_path / "private2", cover_path=cover,
            cover_sha256=cover_sha, generation=generation,
            runtime_authority_root=runtime,
        )


def test_candidate_sidecars_cannot_escape_the_exact_candidate_root(tmp_path: Path) -> None:
    from src.autoslice.reviewed_baseline_replay_projection import exact_candidate_sidecar_target

    out_root, _ = _package(tmp_path)
    plan = replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)
    assert exact_candidate_sidecar_target(
        plan.candidate_id, plan.package_root, role="candidate-chat-authority",
        target=plan.package_root / f"{CID}.chat-authority.json",
        error=replay.ReviewedBaselineReplayError,
    ).name == f"{CID}.chat-authority.json"
    with pytest.raises(replay.ReviewedBaselineReplayError, match="RECORD_MIRROR_BINDING_INVALID"):
        exact_candidate_sidecar_target(
            plan.candidate_id, plan.package_root, role="candidate-chat-authority",
            target=plan.package_root.parent / "other" / f"{CID}.chat-authority.json",
            error=replay.ReviewedBaselineReplayError,
        )


def test_after_image_refuses_deployment_drift_since_private_prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    finalization = replay.PrivateReplayFinalization(
        spec_path=tmp_path / "spec.json", private_runtime_root=tmp_path / "private-runtime",
        prepared_manifest=tmp_path / "prepared.json", prepared_sha256="sha256:" + "a" * 64,
    )
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(
        "src.autoslice.producer_delivery_transaction.deployment_authority_binding",
        lambda root: {"commit": "old" if Path(root) == finalization.private_runtime_root else "new"},
    )
    with pytest.raises(replay.ReviewedBaselineReplayError, match="DEPLOYED_AUTHORITY_DRIFT"):
        replay.build_replay_after_image(
            SimpleNamespace(), runtime_root=tmp_path / "runtime", state_path=tmp_path / "state.json",
            finalization=finalization, package=SimpleNamespace(package_audit=SimpleNamespace(path=tmp_path / "audit")),
            projection=SimpleNamespace(),
        )


@pytest.mark.parametrize(
    "authority_candidate",
    [None, "auto_113028_1271_1328", "auto_113028_1602_1698"],
)
def test_replay_publish_adapter_forwards_only_explicit_recovery_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority_candidate: str | None,
) -> None:
    out_root, _ = _package(tmp_path)
    plan = replay.build_replay_plan(
        repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID
    )
    package = plan.package_root / "replacement_recuts"
    cover = package / "cover.png"
    cover.write_bytes(b"cover")
    plan.record_path.write_text(json.dumps({
        "story_contract": {"candidate_id": CID, "selection_hook": "冻结钩子"},
        "publish_staging": {
            "title": "【李豆沙】冻结标题", "selection_hook": "冻结钩子",
            "cover_path": str(cover),
            "cover_generation": {"final_cover_sha256": _sha(b"cover")},
        },
        "artifact_hashes": {"cover_sha256": _sha(b"cover")},
        # The wrapper must never infer from record content.
        "recovery_publication_authority": {"forbidden": "record fallback"},
    }))
    private_package = tmp_path / "private-package"
    private_package.mkdir()
    monkeypatch.setattr(
        replay, "_private_carried_cover_generation",
        lambda *_args, **_kwargs: (
            private_package / "cover.png",
            {"final_cover_sha256": _sha(b"cover")},
        ),
    )
    monkeypatch.setattr(
        "src.autoslice.source_fact_review.source_fact_review_passes",
        lambda _review: True,
    )
    authorities = build_recovery_publication_authorities(
        candidate_ids={"auto_113028_1271_1328", "auto_113028_1602_1698"},
        registry_path=ROOT / REGISTRY_REPO_PATH,
        expected_registry_sha256=REGISTRY_SHA256,
        repo_root=ROOT,
    )
    expected = authorities.get(authority_candidate) if authority_candidate else None
    seen: list[object] = []

    def stage_publish(_record: object, **kwargs: object) -> dict[str, object]:
        observed = kwargs.get("recovery_publication_authority")
        seen.append(observed)
        return {
            "story_contract": {"selection_hook": "冻结钩子"},
            "publish_staging": {
                "title": "【李豆沙】冻结标题",
                "title_authority_error": None,
                "source_fact_review": {"status": "PASS"},
                "recovery_publication_authority": observed,
            },
        }

    monkeypatch.setattr(
        "src.autoslice.publish_staging._stage_publish_draft", stage_publish
    )
    adapter = replay.replay_publish_adapter(
        plan,
        source_fact_llm=lambda *_args, **_kwargs: "",
        private_package=private_package,
        runtime_authority_root=_runtime_authority(tmp_path),
    )
    kwargs: dict[str, object] = {"cues": [], "run_ffmpeg": False}
    if authority_candidate is not None:
        kwargs["recovery_publication_authority"] = expected
    staged = adapter(
        {"story_contract": {"selection_hook": "冻结钩子"}}, **kwargs
    )
    assert seen == [expected]
    assert staged["publish_staging"]["recovery_publication_authority"] == expected


def test_replay_publish_adapter_explicit_none_remains_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The parameterized test covers omission. This explicit-None call guards
    # compatibility with the producer finalizer's normal non-recovery path.
    out_root, _ = _package(tmp_path)
    plan = replay.build_replay_plan(
        repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID
    )
    package = plan.package_root / "replacement_recuts"
    cover = package / "cover.png"
    cover.write_bytes(b"cover")
    plan.record_path.write_text(json.dumps({
        "story_contract": {"candidate_id": CID, "selection_hook": "冻结钩子"},
        "publish_staging": {
            "title": "冻结标题", "selection_hook": "冻结钩子",
            "cover_path": str(cover),
            "cover_generation": {"final_cover_sha256": _sha(b"cover")},
        },
        "artifact_hashes": {"cover_sha256": _sha(b"cover")},
        "recovery_publication_authority": {"forbidden": "record fallback"},
    }))
    private_package = tmp_path / "private-package"
    private_package.mkdir()
    monkeypatch.setattr(
        replay, "_private_carried_cover_generation",
        lambda *_args, **_kwargs: (
            private_package / "cover.png",
            {"final_cover_sha256": _sha(b"cover")},
        ),
    )
    monkeypatch.setattr(
        "src.autoslice.source_fact_review.source_fact_review_passes",
        lambda _review: True,
    )
    seen: list[object] = []

    def stage_publish(_record: object, **kwargs: object) -> dict[str, object]:
        seen.append(kwargs.get("recovery_publication_authority"))
        return {
            "story_contract": {"selection_hook": "冻结钩子"},
            "publish_staging": {
                "title": "冻结标题", "title_authority_error": None,
                "source_fact_review": {"status": "PASS"},
            },
        }

    monkeypatch.setattr(
        "src.autoslice.publish_staging._stage_publish_draft", stage_publish
    )
    adapter = replay.replay_publish_adapter(
        plan,
        source_fact_llm=lambda *_args, **_kwargs: "",
        private_package=private_package,
        runtime_authority_root=_runtime_authority(tmp_path),
    )
    adapter(
        {"story_contract": {"selection_hook": "冻结钩子"}},
        cues=[], run_ffmpeg=False, recovery_publication_authority=None,
    )
    assert seen == [None]


def test_replay_publish_adapter_refuses_source_fact_title_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_root, _ = _package(tmp_path)
    plan = replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)
    package = plan.package_root / "replacement_recuts"
    cover = package / "cover.png"
    cover.write_bytes(b"cover")
    record = {
        "story_contract": {"candidate_id": CID, "selection_hook": "冻结钩子"},
        "publish_staging": {
            "title": "【李豆沙】冻结标题", "selection_hook": "冻结钩子",
            "cover_path": str(cover), "cover_generation": {"final_cover_sha256": _sha(b"cover")},
        },
        "artifact_hashes": {"cover_sha256": _sha(b"cover")},
    }
    plan.record_path.write_text(json.dumps(record))
    private_package = tmp_path / "private-package"
    private_package.mkdir()
    monkeypatch.setattr(
        replay, "_private_carried_cover_generation",
        lambda *_args, **_kwargs: (private_package / "cover.png", {"final_cover_sha256": _sha(b"cover")}),
    )
    monkeypatch.setattr(
        "src.autoslice.publish_staging._stage_publish_draft",
        lambda *_args, **_kwargs: {
            "story_contract": {"selection_hook": "冻结钩子"},
            "publish_staging": {
                "title": "【李豆沙】被改写的标题", "title_authority_error": None,
                "source_fact_review": {"receipt_sha256": "sha256:" + "a" * 64},
            },
        },
    )
    monkeypatch.setattr(
        "src.autoslice.source_fact_review.source_fact_review_passes", lambda _review: True,
    )
    adapter = replay.replay_publish_adapter(
        plan, source_fact_llm=lambda *_args, **_kwargs: "", private_package=private_package,
        runtime_authority_root=_runtime_authority(tmp_path),
    )
    with pytest.raises(
        replay.ReviewedBaselineReplayError,
        match="REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_STAGED_TITLE_MISMATCH",
    ):
        adapter({}, cues=[], run_ffmpeg=False)


def test_replay_exact_review_prefers_production_over_adapter_fallback(tmp_path: Path) -> None:
    calls: list[str] = []

    def fallback(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {"reviewer": "fallback"}

    def production_reviewer(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {"reviewer": "production"}

    def build_production(*_args: object, **_kwargs: object) -> object:
        calls.append("production")
        return production_reviewer

    reviewer, _ = resolve_replay_exact_final_reviewer(
        explicit_reviewer=None, fallback_reviewer=fallback, use_production=True,
        entity_verifier=lambda *_args, **_kwargs: None, text_adapters=None,
        plan=object(), candidate_id=CID, spec={"clip_context": {}},
        spec_piece={"start_ms": 0, "end_ms": 1, "source_media_sha256": "sha256:" + "a" * 64},
        padded=tmp_path / "padded.mp4", out_root=tmp_path / "out",
        runtime_authority_root=tmp_path / "runtime", release_text_path=tmp_path / "release.srt",
        read_text=lambda _path: "", reconstruct_chat=lambda *_args, **_kwargs: [],
        replay_reviewer=build_production, provider_invocation=None,
        error_factory=ValueError,
    )
    assert reviewer is production_reviewer
    assert calls == ["production"]


def test_replay_publish_adapter_uses_story_contract_hook_when_staging_hook_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_root, _ = _package(tmp_path)
    plan = replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)
    package = plan.package_root / "replacement_recuts"
    cover = package / "cover.png"
    cover.write_bytes(b"cover")
    hook = "故事闭环后明确切换话题"
    record = {
        "story_contract": {"candidate_id": CID, "selection_hook": hook},
        "publish_staging": {
            "title": "冻结标题", "cover_path": str(cover),
            "cover_generation": {"final_cover_sha256": _sha(b"cover")},
        },
        "artifact_hashes": {"cover_sha256": _sha(b"cover")},
    }
    plan.record_path.write_text(json.dumps(record))
    private_package = tmp_path / "private-package"
    private_package.mkdir()
    monkeypatch.setattr(
        replay, "_private_carried_cover_generation",
        lambda *_args, **_kwargs: (private_package / "cover.png", {"final_cover_sha256": _sha(b"cover")}),
    )
    seen: dict[str, object] = {}
    def stage_publish(record, **kwargs):
        seen["selection_hook"] = kwargs["selection_hook"]
        return {
            "story_contract": {"selection_hook": hook},
            "publish_staging": {
                "title": "冻结标题", "title_authority_error": None,
                "source_fact_review": {"receipt_sha256": "sha256:" + "a" * 64},
            },
        }
    monkeypatch.setattr("src.autoslice.publish_staging._stage_publish_draft", stage_publish)
    monkeypatch.setattr("src.autoslice.source_fact_review.source_fact_review_passes", lambda _review: True)
    adapter = replay.replay_publish_adapter(
        plan, source_fact_llm=lambda *_args, **_kwargs: "", private_package=private_package,
        runtime_authority_root=_runtime_authority(tmp_path),
    )
    result = adapter({}, cues=[], run_ffmpeg=False)
    assert isinstance(result, dict)
    story = result.get("story_contract")
    assert isinstance(story, dict)
    assert story.get("selection_hook") == hook
    assert seen["selection_hook"] == hook


def test_replay_publish_adapter_accepts_unchanged_manual_surface_without_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_root, _ = _package(tmp_path)
    plan = replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)
    package = plan.package_root / "replacement_recuts"
    cover = package / "cover.png"
    cover.write_bytes(b"cover")
    record = {
        "story_contract": {"candidate_id": CID, "selection_hook": "冻结钩子"},
        "publish_staging": {
            "title": "【李豆沙】冻结标题", "selection_hook": "冻结钩子",
            "source_fact_review": {"status": "PASS", "receipt_sha256": "sha256:" + "a" * 64},
            "cover_path": str(cover), "cover_generation": {"final_cover_sha256": _sha(b"cover")},
        },
        "artifact_hashes": {"cover_sha256": _sha(b"cover")},
    }
    plan.record_path.write_text(json.dumps(record))
    private_package = tmp_path / "private-package"
    private_package.mkdir()
    monkeypatch.setattr(
        replay, "_private_carried_cover_generation",
        lambda *_args, **_kwargs: (private_package / "cover.png", {"final_cover_sha256": _sha(b"cover")}),
    )
    seen: dict[str, object] = {}

    def stage_publish(stage_record, **_kwargs):
        story = stage_record["story_contract"]
        seen["cover_story_source_fact_review"] = story.get("source_fact_review")
        return {
            "story_contract": dict(story),
            "publish_staging": {
                "title": "【李豆沙】冻结标题", "title_authority_error": None,
                "title_authority_status": "RESOLVED_MANUAL", "source_fact_review": None,
            },
        }

    monkeypatch.setattr("src.autoslice.publish_staging._stage_publish_draft", stage_publish)
    monkeypatch.setattr(
        "src.autoslice.source_fact_review.source_fact_review_passes",
        lambda review: isinstance(review, Mapping) and review.get("status") == "PASS",
    )
    adapter = replay.replay_publish_adapter(
        plan, source_fact_llm=lambda *_args, **_kwargs: "", private_package=private_package,
        runtime_authority_root=_runtime_authority(tmp_path),
    )
    result = adapter({"story_contract": {"selection_hook": "冻结钩子"}}, cues=[], run_ffmpeg=False)
    staging = result["publish_staging"]
    story = result["story_contract"]
    assert isinstance(staging, Mapping)
    assert isinstance(story, Mapping)
    assert staging["title"] == "【李豆沙】冻结标题"
    assert staging["source_fact_review"] == record["publish_staging"]["source_fact_review"]
    assert story["source_fact_review"] == record["publish_staging"]["source_fact_review"]
    assert seen["cover_story_source_fact_review"] == record["publish_staging"]["source_fact_review"]


def test_replay_publish_adapter_refuses_fallback_when_story_receipt_drifts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_root, _ = _package(tmp_path)
    plan = replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)
    package = plan.package_root / "replacement_recuts"
    cover = package / "cover.png"
    cover.write_bytes(b"cover")
    old_review = {"status": "PASS", "receipt_sha256": "sha256:" + "a" * 64}
    plan.record_path.write_text(json.dumps({
        "story_contract": {"candidate_id": CID, "selection_hook": "冻结钩子"},
        "publish_staging": {
            "title": "冻结标题", "selection_hook": "冻结钩子", "source_fact_review": old_review,
            "cover_path": str(cover), "cover_generation": {"final_cover_sha256": _sha(b"cover")},
        },
        "artifact_hashes": {"cover_sha256": _sha(b"cover")},
    }))
    private_package = tmp_path / "private-package"
    private_package.mkdir()
    monkeypatch.setattr(
        replay, "_private_carried_cover_generation",
        lambda *_args, **_kwargs: (private_package / "cover.png", {"final_cover_sha256": _sha(b"cover")}),
    )
    monkeypatch.setattr(
        "src.autoslice.publish_staging._stage_publish_draft",
        lambda *_args, **_kwargs: {
            "story_contract": {"selection_hook": "冻结钩子", "source_fact_review": {"status": "PASS", "receipt_sha256": "sha256:" + "b" * 64}},
            "publish_staging": {"title": "冻结标题", "title_authority_error": None, "source_fact_review": None},
        },
    )
    monkeypatch.setattr(
        "src.autoslice.source_fact_review.source_fact_review_passes",
        lambda review: isinstance(review, Mapping) and review.get("status") == "PASS",
    )
    adapter = replay.replay_publish_adapter(
        plan, source_fact_llm=lambda *_args, **_kwargs: "", private_package=private_package,
        runtime_authority_root=_runtime_authority(tmp_path),
    )
    with pytest.raises(
        replay.ReviewedBaselineReplayError,
        match="REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_SOURCE_FACT_REVIEW_MISSING",
    ):
        adapter({"story_contract": {"selection_hook": "冻结钩子"}}, cues=[], run_ffmpeg=False)


def test_replay_publish_adapter_never_seeds_old_receipt_for_root_reviewed_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_root, _ = _package(tmp_path)
    plan = replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)
    package = plan.package_root / "replacement_recuts"
    cover = package / "cover.png"
    cover.write_bytes(b"cover")
    old_review = {"status": "PASS", "receipt_sha256": "sha256:" + "a" * 64}
    plan.record_path.write_text(json.dumps({
        "story_contract": {"candidate_id": CID, "selection_hook": "冻结钩子"},
        "publish_staging": {
            "title": "冻结标题", "selection_hook": "冻结钩子", "source_fact_review": old_review,
            "cover_path": str(cover), "cover_generation": {"final_cover_sha256": _sha(b"cover")},
        },
        "artifact_hashes": {"cover_sha256": _sha(b"cover")},
    }))
    private_package = tmp_path / "private-package"
    private_package.mkdir()
    monkeypatch.setattr(
        replay, "_private_carried_cover_generation",
        lambda *_args, **_kwargs: (private_package / "cover.png", {"final_cover_sha256": _sha(b"cover")}),
    )
    monkeypatch.setattr(
        "src.autoslice.candidate_public_text_surface_authority.load_candidate_public_text_surface_authority",
        lambda _candidate_id: SimpleNamespace(
            is_root_reviewed_resolution=True,
            resolved_title="新的冻结标题",
            resolved_selection_hook="新的冻结钩子",
        ),
    )
    seen: dict[str, object] = {}

    def stage_publish(stage_record, **_kwargs):
        story = stage_record["story_contract"]
        seen["source_fact_review"] = story.get("source_fact_review")
        return {
            "story_contract": {"selection_hook": "新的冻结钩子"},
            "publish_staging": {"title": "新的冻结标题", "title_authority_error": None, "source_fact_review": None},
        }

    monkeypatch.setattr("src.autoslice.publish_staging._stage_publish_draft", stage_publish)
    monkeypatch.setattr(
        "src.autoslice.source_fact_review.source_fact_review_passes",
        lambda review: isinstance(review, Mapping) and review.get("status") == "PASS",
    )
    adapter = replay.replay_publish_adapter(
        plan, source_fact_llm=lambda *_args, **_kwargs: "", private_package=private_package,
        runtime_authority_root=_runtime_authority(tmp_path),
    )
    with pytest.raises(
        replay.ReviewedBaselineReplayError,
        match="REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_SOURCE_FACT_REVIEW_MISSING",
    ):
        adapter({"story_contract": {"selection_hook": "新的冻结钩子"}}, cues=[], run_ffmpeg=False)
    assert seen["source_fact_review"] is None


def test_replay_publish_adapter_refuses_unverified_missing_source_fact_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_root, _ = _package(tmp_path)
    plan = replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)
    package = plan.package_root / "replacement_recuts"
    cover = package / "cover.png"
    cover.write_bytes(b"cover")
    record = {
        "story_contract": {"candidate_id": CID, "selection_hook": "冻结钩子"},
        "publish_staging": {
            "title": "【李豆沙】冻结标题", "selection_hook": "冻结钩子",
            "cover_path": str(cover), "cover_generation": {"final_cover_sha256": _sha(b"cover")},
        },
        "artifact_hashes": {"cover_sha256": _sha(b"cover")},
    }
    plan.record_path.write_text(json.dumps(record))
    private_package = tmp_path / "private-package"
    private_package.mkdir()
    monkeypatch.setattr(
        replay, "_private_carried_cover_generation",
        lambda *_args, **_kwargs: (private_package / "cover.png", {"final_cover_sha256": _sha(b"cover")}),
    )
    monkeypatch.setattr(
        "src.autoslice.publish_staging._stage_publish_draft",
        lambda *_args, **_kwargs: {
            "story_contract": {"selection_hook": "冻结钩子"},
            "publish_staging": {
                "title": "【李豆沙】冻结标题", "title_authority_error": None,
                "title_authority_status": "JOB_TITLE", "source_fact_review": None,
            },
        },
    )
    adapter = replay.replay_publish_adapter(
        plan, source_fact_llm=lambda *_args, **_kwargs: "", private_package=private_package,
        runtime_authority_root=_runtime_authority(tmp_path),
    )
    with pytest.raises(
        replay.ReviewedBaselineReplayError,
        match="REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_SOURCE_FACT_REVIEW",
    ):
        adapter({}, cues=[], run_ffmpeg=False)


def test_replay_publish_adapter_classifies_missing_staged_story_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_root, _ = _package(tmp_path)
    plan = replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)
    package = plan.package_root / "replacement_recuts"
    cover = package / "cover.png"
    cover.write_bytes(b"cover")
    record = {
        "story_contract": {"candidate_id": CID, "selection_hook": "冻结钩子"},
        "publish_staging": {
            "title": "冻结标题", "selection_hook": "冻结钩子",
            "source_fact_review": {"status": "PASS", "receipt_sha256": "sha256:" + "a" * 64},
            "cover_path": str(cover), "cover_generation": {"final_cover_sha256": _sha(b"cover")},
        },
        "artifact_hashes": {"cover_sha256": _sha(b"cover")},
    }
    plan.record_path.write_text(json.dumps(record))
    private_package = tmp_path / "private-package"
    private_package.mkdir()
    monkeypatch.setattr(
        replay, "_private_carried_cover_generation",
        lambda *_args, **_kwargs: (private_package / "cover.png", {"final_cover_sha256": _sha(b"cover")}),
    )
    monkeypatch.setattr(
        "src.autoslice.publish_staging._stage_publish_draft",
        lambda *_args, **_kwargs: {
            "publish_staging": {
                "title": "冻结标题", "title_authority_error": None,
                "source_fact_review": None,
            },
        },
    )
    monkeypatch.setattr(
        "src.autoslice.source_fact_review.source_fact_review_passes",
        lambda review: isinstance(review, Mapping) and review.get("status") == "PASS",
    )
    adapter = replay.replay_publish_adapter(
        plan, source_fact_llm=lambda *_args, **_kwargs: "", private_package=private_package,
        runtime_authority_root=_runtime_authority(tmp_path),
    )
    with pytest.raises(replay.ReviewedBaselineReplayError, match="REPLAY_STORY_CONTRACT_MISSING"):
        adapter({}, cues=[], run_ffmpeg=False)


@pytest.mark.parametrize(
    ("review", "title", "story_hook", "title_error", "expected"),
    [
        (None, "冻结标题", "冻结钩子", "provider_failed", replay_publish.REPLAY_FROZEN_SOURCE_FACT_REVIEW_MISSING),
        ({"reason_code": "CPA_TEXT_REVIEW_CALL_FAILED"}, "冻结标题", "冻结钩子", None, "REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_SOURCE_FACT_REVIEW_CPA_TEXT_REVIEW_CALL_FAILED"),
        ({"reason_code": "CPA_ENTITY_SURFACE_RESPONSE_INVALID"}, "冻结标题", "冻结钩子", None, "REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_SOURCE_FACT_REVIEW_CPA_ENTITY_SURFACE_RESPONSE_INVALID"),
        ({"reason_code": "CPA_TEXT_REVIEW_ADDRESSEE_ATTRIBUTION_UNRESOLVED"}, "冻结标题", "冻结钩子", None, "REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_SOURCE_FACT_REVIEW_CPA_TEXT_REVIEW_ADDRESSEE_ATTRIBUTION_UNRESOLVED"),
        ({"reason_code": "UNLISTED_PROVIDER_DETAIL"}, "冻结标题", "冻结钩子", None, replay_publish.REPLAY_FROZEN_SOURCE_FACT_REVIEW),
        ({"status": "PASS"}, "错误标题", "冻结钩子", None, replay_publish.REPLAY_FROZEN_STAGED_TITLE_MISMATCH),
        ({"status": "PASS"}, "冻结标题", "错误钩子", None, replay_publish.REPLAY_FROZEN_STORY_RESOLVED_HOOK_MISMATCH),
        ({"status": "PASS"}, "冻结标题", "冻结钩子", "title_failed", replay_publish.REPLAY_FROZEN_TITLE_AUTHORITY_ERROR),
    ],
)
def test_replay_publish_adapter_emits_typed_title_surface_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    review: dict[str, object] | None,
    title: str,
    story_hook: str,
    title_error: str | None,
    expected: str,
) -> None:
    out_root, _ = _package(tmp_path)
    plan = replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)
    package = plan.package_root / "replacement_recuts"
    cover = package / "cover.png"
    cover.write_bytes(b"cover")
    plan.record_path.write_text(json.dumps({
        "story_contract": {"candidate_id": CID, "selection_hook": "冻结钩子"},
        "publish_staging": {
            "title": "冻结标题", "selection_hook": "冻结钩子", "cover_path": str(cover),
            "cover_generation": {"final_cover_sha256": _sha(b"cover")},
        },
        "artifact_hashes": {"cover_sha256": _sha(b"cover")},
    }))
    private_package = tmp_path / "private-package"
    private_package.mkdir()
    monkeypatch.setattr(
        replay, "_private_carried_cover_generation",
        lambda *_args, **_kwargs: (private_package / "cover.png", {"final_cover_sha256": _sha(b"cover")}),
    )
    monkeypatch.setattr(
        "src.autoslice.publish_staging._stage_publish_draft",
        lambda *_args, **_kwargs: {
            "story_contract": {"selection_hook": story_hook},
            "publish_staging": {
                "title": title, "title_authority_error": title_error,
                "source_fact_review": review,
            },
        },
    )
    monkeypatch.setattr(
        "src.autoslice.source_fact_review.source_fact_review_passes",
        lambda value: isinstance(value, Mapping) and value.get("status") == "PASS",
    )
    adapter = replay.replay_publish_adapter(
        plan, source_fact_llm=lambda *_args, **_kwargs: "", private_package=private_package,
        runtime_authority_root=_runtime_authority(tmp_path),
    )
    with pytest.raises(replay.ReviewedBaselineReplayError, match=expected):
        adapter({}, cues=[], run_ffmpeg=False)



def _record_bound_authority_fixture(tmp_path: Path) -> tuple[SimpleNamespace, Path, dict[str, object], Path, Path]:
    """Build a drifted candidate pair plus one exact portable record mirror."""

    candidate = tmp_path / "out" / DATE / CID
    package = candidate / "replacement_recuts"
    package.mkdir(parents=True)
    direct_chat = candidate / f"{CID}.chat-authority.json"
    direct_clip = candidate / f"{CID}.clip-context.json"
    direct_chat.write_bytes(b"drifted-chat")
    direct_clip.write_bytes(b"drifted-clip")
    source_sha = "sha256:" + "1" * 64
    subtitle_sha = "sha256:" + "2" * 64
    speaker_sha = "sha256:" + "3" * 64
    ass_sha = "sha256:" + "4" * 64
    draft = "1\n00:00:00,000 --> 00:00:01,000\ntext\n"
    clip = {
        "schema_version": "lidousha-clip-context.v1", "candidate_id": CID,
        "recording_date": DATE, "mutation_authorized": False,
        "whole_clip_draft_srt": draft, "whole_clip_draft_srt_sha256": _sha(draft.encode()),
        "pieces": [{"source_media_sha256": source_sha}],
        "retrieval_budget": {"whole_clip_transcript_truncated": False},
    }
    clip["context_sha256"] = _canonical_sha256(clip)
    chat = {
        "schema_version": "chat-authority-audit.v2", "status": "APPLIED_AND_VERIFIED",
        "final_status": "FINAL_ARTIFACTS_VERIFIED",
        "final_output_srt_sha256": subtitle_sha.removeprefix("sha256:"),
        "final_text_srt_sha256": subtitle_sha.removeprefix("sha256:"),
        "final_speaker_srt_sha256": speaker_sha.removeprefix("sha256:"),
        "speaker_ass_sha256": ass_sha.removeprefix("sha256:"),
        "final_review_audit": {"schema_version": "final-review-audit.v2", "status": "CLEAN", "reviewed_srt_sha256": subtitle_sha},
        "structured_chat_binding_audit": {"schema_version": "structured-chat-binding-audit.v1", "status": "PASS"},
    }
    portable = tmp_path / "runtime" / "repo" / "lidousha" / DATE
    portable.mkdir(parents=True)
    stem = "portable-name"
    portable_chat = portable / f"{stem}.chat-authority.json"
    portable_clip = portable / f"{stem}.clip-context.json"
    portable_chat.write_text(json.dumps(chat))
    portable_clip.write_text(json.dumps(clip))
    publish = {"artifact_hashes": {
        "chat_authority_audit_sha256": _sha(portable_chat.read_bytes()),
        "clip_context_file_sha256": _sha(portable_clip.read_bytes()),
    }}
    portable_publish = portable / f"{stem}.publish.json"
    portable_publish.write_text(json.dumps(publish))
    record = {
        "chat_authority_audit_path": str(direct_chat), "clip_context_path": str(direct_clip),
        "clip_context_payload_sha256": clip["context_sha256"],
        "artifact_hashes": {
            "chat_authority_audit_sha256": _sha(portable_chat.read_bytes()),
            "clip_context_file_sha256": _sha(portable_clip.read_bytes()),
            "publish_draft_sha256": _sha(portable_publish.read_bytes()),
            "subtitle_sha256": subtitle_sha, "speaker_review_srt_sha256": speaker_sha,
            "ass_sha256": ass_sha,
        },
        "story_contract": {
            "candidate_id": CID,
            "clip_context_binding": {"context_sha256": clip["context_sha256"]},
        },
    }
    record_path = package / f"{CID}.record.json"
    record_path.write_text(json.dumps(record))
    portable_record = portable / f"{stem}.record.json"
    portable_record.write_bytes(record_path.read_bytes())
    plan = SimpleNamespace(candidate_id=CID, date=DATE, package_root=candidate)
    return plan, record_path, record, portable, portable_record


def _rebind_portable_authority(
    *, record_path: Path, record: dict[str, object], portable: Path, portable_record: Path,
) -> None:
    """Refresh the fixture's record/publish byte closure after a sidecar edit."""

    stem = portable_record.name.removesuffix(".record.json")
    chat = portable / f"{stem}.chat-authority.json"
    clip = portable / f"{stem}.clip-context.json"
    publish_path = portable / f"{stem}.publish.json"
    hashes = record["artifact_hashes"]
    assert isinstance(hashes, dict)
    hashes["chat_authority_audit_sha256"] = _sha(chat.read_bytes())
    hashes["clip_context_file_sha256"] = _sha(clip.read_bytes())
    publish = json.loads(publish_path.read_text())
    publish["artifact_hashes"] = {
        "chat_authority_audit_sha256": hashes["chat_authority_audit_sha256"],
        "clip_context_file_sha256": hashes["clip_context_file_sha256"],
    }
    publish_path.write_text(json.dumps(publish))
    hashes["publish_draft_sha256"] = _sha(publish_path.read_bytes())
    record_path.write_text(json.dumps(record))
    portable_record.write_bytes(record_path.read_bytes())


def _resolve_fixture_authority(
    *, plan: SimpleNamespace, record_path: Path, record: dict[str, object], portable: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> replay_authority.RecordBoundFinalizerAuthority:
    monkeypatch.setattr(replay_authority, "_portable_date_root", lambda **_kwargs: portable)
    return replay_authority.resolve_record_bound_finalizer_authority(
        plan=plan, record_binding=replay.regular_binding(record_path, label="RECORD"), record=record,
        runtime_authority_root=portable.parents[3], source_media_sha256="sha256:" + "1" * 64,
        regular_binding=replay.regular_binding, safe_directory=replay._safe_directory,
        load_json=replay._load_json, error=replay.ReviewedBaselineReplayError,
    )


def test_record_bound_portable_mirror_recovers_only_exact_sidecar_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, record_path, record, portable, _portable_record = _record_bound_authority_fixture(tmp_path)
    authority = _resolve_fixture_authority(
        plan=plan, record_path=record_path, record=record, portable=portable, monkeypatch=monkeypatch,
    )

    assert authority.source == "portable-record-mirror"
    assert authority.chat.path.parent == portable
    assert authority.clip_context.path.parent == portable
    assert authority.chat.sha256 == record["artifact_hashes"]["chat_authority_audit_sha256"]
    assert authority.clip_context.sha256 == record["artifact_hashes"]["clip_context_file_sha256"]


def test_record_bound_authority_uses_current_exact_pair_without_portable_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, record_path, record, portable, portable_record = _record_bound_authority_fixture(tmp_path)
    stem = portable_record.name.removesuffix(".record.json")
    candidate = plan.package_root
    (candidate / f"{CID}.chat-authority.json").write_bytes(
        (portable / f"{stem}.chat-authority.json").read_bytes()
    )
    (candidate / f"{CID}.clip-context.json").write_bytes(
        (portable / f"{stem}.clip-context.json").read_bytes()
    )
    monkeypatch.setattr(
        replay_authority, "_portable_date_root",
        lambda **_kwargs: pytest.fail("exact current authority must not scan portable mirrors"),
    )
    authority = replay_authority.resolve_record_bound_finalizer_authority(
        plan=plan, record_binding=replay.regular_binding(record_path, label="RECORD"), record=record,
        runtime_authority_root=tmp_path / "runtime", source_media_sha256="sha256:" + "1" * 64,
        regular_binding=replay.regular_binding, safe_directory=replay._safe_directory,
        load_json=replay._load_json, error=replay.ReviewedBaselineReplayError,
    )
    assert authority.source == "current-candidate"


def test_record_bound_portable_mirror_refuses_missing_or_symlink_companion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, record_path, record, portable, portable_record = _record_bound_authority_fixture(tmp_path)
    portable_record.unlink()
    with pytest.raises(replay.ReviewedBaselineReplayError, match="PORTABLE_AUTHORITY_RECORD_AMBIGUOUS"):
        _resolve_fixture_authority(
            plan=plan, record_path=record_path, record=record, portable=portable, monkeypatch=monkeypatch,
        )

    _plan, _record_path, _record, portable, portable_record = _record_bound_authority_fixture(tmp_path / "symlink")
    (portable / "portable-name.clip-context.json").unlink()
    (portable / "portable-name.clip-context.json").symlink_to(portable / "portable-name.chat-authority.json")
    with pytest.raises(replay.ReviewedBaselineReplayError, match="PORTABLE_CLIP_CONTEXT_UNSAFE"):
        _resolve_fixture_authority(
            plan=_plan, record_path=_record_path, record=_record, portable=portable, monkeypatch=monkeypatch,
        )


@pytest.mark.parametrize(
    ("kind", "mutate", "reason"),
    [
        ("chat", lambda value: value.update({"final_status": "STALE"}), "CHAT_AUTHORITY_RECORD_CLOSURE_DRIFT"),
        ("clip-candidate", lambda value: value.update({"candidate_id": "other"}), "CLIP_CONTEXT_INVALID"),
        ("clip-date", lambda value: value.update({"recording_date": "2026-08-15"}), "CLIP_CONTEXT_INVALID"),
        ("clip-source", lambda value: value["pieces"][0].update({"source_media_sha256": "sha256:" + "e" * 64}), "CLIP_CONTEXT_INVALID"),
        ("clip-payload", lambda _value: None, "CLIP_CONTEXT_PAYLOAD_DRIFT"),
    ],
)
def test_record_bound_portable_authority_rechecks_chat_and_clip_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, mutate, reason: str,
) -> None:
    plan, record_path, record, portable, portable_record = _record_bound_authority_fixture(tmp_path)
    path = portable / f"portable-name.{ 'chat-authority' if kind == 'chat' else 'clip-context' }.json"
    document = json.loads(path.read_text())
    mutate(document)
    if kind == "clip-payload":
        document["whole_clip_draft_srt"] = "1\n00:00:00,000 --> 00:00:01,000\nother\n"
        document["whole_clip_draft_srt_sha256"] = _sha(document["whole_clip_draft_srt"].encode())
    if kind.startswith("clip-"):
        document["context_sha256"] = _canonical_sha256({
            key: value for key, value in document.items() if key != "context_sha256"
        })
    path.write_text(json.dumps(document))
    _rebind_portable_authority(
        record_path=record_path, record=record, portable=portable, portable_record=portable_record,
    )
    with pytest.raises(replay.ReviewedBaselineReplayError, match=reason):
        _resolve_fixture_authority(
            plan=plan, record_path=record_path, record=record, portable=portable, monkeypatch=monkeypatch,
        )


def test_record_bound_authority_requires_real_hashes() -> None:
    assert replay_authority._same_sha(None, None) is False
    assert replay_authority._same_sha("", "") is False


def test_record_bound_authority_accepts_canonical_no_match_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, record_path, record, portable, portable_record = _record_bound_authority_fixture(tmp_path)
    chat_path = portable / "portable-name.chat-authority.json"
    chat = json.loads(chat_path.read_text())
    chat["status"] = "NO_MATCH"
    chat_path.write_text(json.dumps(chat))
    _rebind_portable_authority(
        record_path=record_path, record=record, portable=portable, portable_record=portable_record,
    )

    assert _resolve_fixture_authority(
        plan=plan, record_path=record_path, record=record, portable=portable, monkeypatch=monkeypatch,
    ).source == "portable-record-mirror"


@pytest.mark.parametrize("status", ["FAILED", "PENDING_TEXT_OVERRIDE", "UNRESOLVED"])
def test_record_bound_authority_refuses_nonterminal_chat_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    plan, record_path, record, portable, portable_record = _record_bound_authority_fixture(tmp_path)
    chat_path = portable / "portable-name.chat-authority.json"
    chat = json.loads(chat_path.read_text())
    chat["status"] = status
    chat_path.write_text(json.dumps(chat))
    _rebind_portable_authority(
        record_path=record_path, record=record, portable=portable, portable_record=portable_record,
    )

    with pytest.raises(replay.ReviewedBaselineReplayError, match="CHAT_AUTHORITY_RECORD_CLOSURE_DRIFT"):
        _resolve_fixture_authority(
            plan=plan, record_path=record_path, record=record, portable=portable, monkeypatch=monkeypatch,
        )


def _sealed_structured_chat_context(rows: list[dict[str, object]]) -> tuple[dict[str, object], str]:
    source_sha = "sha256:" + "a" * 64
    draft = "1\n00:00:00,000 --> 00:00:01,000\ntext\n"
    context = {
        "schema_version": "lidousha-clip-context.v1", "candidate_id": CID,
        "recording_date": DATE, "mutation_authorized": False,
        "whole_clip_draft_srt": draft, "whole_clip_draft_srt_sha256": _sha(draft.encode()),
        "pieces": [{"source_media_sha256": source_sha}], "structured_chat": rows,
        "retrieval_budget": {
            "whole_clip_transcript_truncated": False,
            "structured_chat_truncated": False,
            "structured_chat_selected_rows": len(rows),
            "structured_chat_total_rows": len(rows),
            "structured_chat_row_cap": 240,
            "structured_chat_selection_policy": "all_sc_gift_guard_then_temporal_danmaku_sampling",
        },
    }
    context["context_sha256"] = _canonical_sha256(context)
    return context, source_sha


def _canonical_chat_row(*, kind: str = "danmaku", offset: int = -5000, event_id: str = "") -> dict[str, object]:
    return {
        "kind": kind, "offset_ms": offset, "text": "弹幕", "sender": "观众",
        "source": "/recording/source.xml", "source_sha256": "sha256:" + "a" * 64,
        "source_event_id": event_id,
    }


def test_reconstruct_structured_chat_accepts_negative_xml_danmaku_without_event_id() -> None:
    context, source_sha = _sealed_structured_chat_context([_canonical_chat_row()])
    chat = replay._reconstruct_structured_chat(
        context, plan=SimpleNamespace(candidate_id=CID, date=DATE), source_media_sha256=source_sha,
    )

    assert len(chat) == 1
    assert chat[0].offset_ms == -5000
    assert chat[0].source_event_id == ""


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda context: context["retrieval_budget"].update({"structured_chat_truncated": True}), "BUDGET_INVALID"),
        (lambda context: context["structured_chat"][0].pop("sender"), "ROW_SHAPE_INVALID"),
        (lambda context: context["structured_chat"][0].update({"kind": "other"}), "ROW_VALUE_INVALID"),
        (lambda context: context["structured_chat"][0].update({"source_sha256": "not-a-hash"}), "ROW_VALUE_INVALID"),
        (lambda context: context["structured_chat"][0].update({"kind": "superchat", "source_event_id": ""}), "EVENT_ID_INVALID"),
    ],
)
def test_reconstruct_structured_chat_refuses_budget_row_shape_value_and_event_drift(mutate, reason: str) -> None:
    context, source_sha = _sealed_structured_chat_context([_canonical_chat_row()])
    mutate(context)
    context["context_sha256"] = _canonical_sha256({
        key: value for key, value in context.items() if key != "context_sha256"
    })

    with pytest.raises(replay.ReviewedBaselineReplayError, match=f"REPLAY_STRUCTURED_CHAT_{reason}"):
        replay._reconstruct_structured_chat(
            context, plan=SimpleNamespace(candidate_id=CID, date=DATE), source_media_sha256=source_sha,
        )


def test_exact_final_reviewer_returns_callable_and_tracks_every_provider_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tracker observes each provider-backed callback only at invocation."""

    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"padded")
    plan = SimpleNamespace(candidate_id=CID, date=DATE)
    monkeypatch.setattr(replay, "_reconstruct_structured_chat", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        "src.autoslice.delivery_fast_path.resolve_operator_text_full_ownership",
        lambda _spec: {"schema_version": "operator-text-full-ownership.v1"},
    )
    monkeypatch.setattr(
        "src.autoslice.delivery_fast_path.skipped_final_review_audit", lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "src.autoslice.review_package_boundary_validators.semantic_boundary_review_is_valid",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "src.autoslice.producer_boundary_review_stage.exact_delivery_correction_audit",
        lambda **kwargs: kwargs["llm_call"]("boundary") or {"status": "PASS"},
    )
    monkeypatch.setattr(
        "src.autoslice.delivery_fast_path.discover_priority_findings", lambda *_args, **_kwargs: ([], {}),
    )
    monkeypatch.setattr(
        "src.autoslice.producer_text_pipeline._final_review_structured_context", lambda **_kwargs: {},
    )
    monkeypatch.setattr(
        "src.autoslice.clip_context.clip_context_prompt_text", lambda _context: "context",
    )
    monkeypatch.setattr(
        "src.autoslice.producer_text_pipeline._run_exact_final_release_review",
        lambda **kwargs: (
            kwargs["verify_confusable_entity"]("entity"),
            kwargs["screen_read_probe"](0, 1),
            kwargs["final_review_llm"]("final"),
            kwargs["pronoun_audit_llm"]("pronoun"),
            {"status": "CLEAN"},
        )[-1],
    )
    attempts: list[None] = []
    reviewer = replay.replay_exact_final_reviewer(
        plan, spec={
            "pieces": [{"source_media_sha256": "sha256:" + "1" * 64}],
            "selection_hook": "hook", "selection_scorecard": {},
            "boundary_semantic_review": {"candidate_id": CID},
        }, clip_context={}, runtime_root=tmp_path, out_root=tmp_path, padded=padded,
        verify_confusable_entity=lambda _request: {}, screen_read_probe=lambda *_args: {},
        boundary_llm=lambda _prompt: "{}", final_llm=lambda _prompt: "{}",
        pronoun_llm=lambda _prompt: "{}", provider_invocation=lambda: attempts.append(None),
    )
    assert callable(reviewer)
    assert reviewer("1\n00:00:00,000 --> 00:00:01,000\ntext\n", {}, 0, 1) == {"status": "CLEAN"}
    assert len(attempts) == 5


def test_record_bound_portable_mirror_rejects_ambiguous_or_mismatched_companions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, record_path, record, portable, portable_record = _record_bound_authority_fixture(tmp_path)
    monkeypatch.setattr(replay_authority, "_portable_date_root", lambda **_kwargs: portable)
    # A second byte-identical record has no authority to choose between stems.
    (portable / "second.record.json").write_bytes(portable_record.read_bytes())
    with pytest.raises(replay.ReviewedBaselineReplayError, match="PORTABLE_AUTHORITY_RECORD_AMBIGUOUS"):
        replay_authority.resolve_record_bound_finalizer_authority(
            plan=plan, record_binding=replay.regular_binding(record_path, label="RECORD"), record=record,
            runtime_authority_root=tmp_path / "runtime", source_media_sha256="sha256:" + "1" * 64,
            regular_binding=replay.regular_binding, safe_directory=replay._safe_directory,
            load_json=replay._load_json, error=replay.ReviewedBaselineReplayError,
        )

    (portable / "second.record.json").unlink()
    (portable / "portable-name.clip-context.json").write_bytes(b"wrong")
    with pytest.raises(replay.ReviewedBaselineReplayError, match="FINALIZER_CLIP_CONTEXT_DRIFT"):
        replay_authority.resolve_record_bound_finalizer_authority(
            plan=plan, record_binding=replay.regular_binding(record_path, label="RECORD"), record=record,
            runtime_authority_root=tmp_path / "runtime", source_media_sha256="sha256:" + "1" * 64,
            regular_binding=replay.regular_binding, safe_directory=replay._safe_directory,
            load_json=replay._load_json, error=replay.ReviewedBaselineReplayError,
        )


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
        "speaker_mode": "auto", "subtitle_style": "lidousha-speaker-sapphire-host-white-guest-v2",
        "story_contract": {
            "candidate_id": CID, "selection_hook": "钩子", "selection_scorecard": {"x": 1},
            "session_relation_authority": {"status": "BOUND"},
        },
        "burned_preview": {"branding_intro": {
            "status": "PREPENDED", "intro_id": "intro-a",
            "intro_media_sha256": "a" * 64, "intro_offset_ms": 1234,
        }},
        "artifact_hashes": {**record["artifact_hashes"], "chat_authority_audit_sha256": _sha(chat.read_bytes())},
    })
    record_path.write_text(json.dumps(record))
    plan = replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)
    monkeypatch.setattr(
        replay, "resolve_record_bound_finalizer_authority",
        lambda **_kwargs: SimpleNamespace(
            chat=replay.regular_binding(chat, label="CHAT"),
            clip_context=replay.regular_binding(clip, label="CLIP"),
            source="fixture",
        ),
    )

    @dataclass(frozen=True)
    class Adapters:
        stage_publish_draft: object = None
        delivery_root: object = None
        run_exact_final_review: object = lambda *_args: {}

    calls: dict[str, object] = {}
    def fake_finalizer(**kwargs: object) -> int:
        from src.autoslice.producer_delivery_prepare import prepare_talk_delivery

        calls.update(kwargs)
        output = Path(kwargs["out_root"])
        runtime = output.parents[1]
        recut_dir = output / "replacement_recuts"
        recut_dir.mkdir()
        burned = recut_dir / f"{CID}.recut.mp4"
        subtitle = recut_dir / f"{CID}.recut.srt"
        speaker_ass = recut_dir / f"{CID}.recut.speaker.ass"
        record_path = recut_dir / f"{CID}.recut.record.json"
        publish_path = recut_dir / f"{CID}.recut.publish.json"
        speaker_manifest_path = recut_dir / f"{CID}.recut.speaker-final.json"
        chat_path = Path(kwargs["chat_authority_path"])
        burned.write_bytes(b"private-burn")
        subtitle.write_text("1\n00:00:00,250 --> 00:00:01,000\n文本\n")
        speaker_ass.write_text("[Script Info]\n")
        profile_path = runtime / "repo" / "speaker-profile.json"
        profile_path.write_text("{}\n")
        speaker_manifest = {
            "status": "READY", "production_ready": True,
            "source_media": str(burned), "text_final_srt": str(subtitle),
            "profile": str(profile_path), "output_review_srt": str(subtitle),
            "output_ass": str(speaker_ass),
        }
        speaker_manifest_path.write_text(json.dumps(speaker_manifest))
        chat = {
            "final_text_srt_path": str(subtitle),
            "speaker_manifest_sha256": _sha(speaker_manifest_path.read_bytes()).removeprefix("sha256:"),
            "burn_binding": {"burned_media_path": str(burned), "ass_path": str(speaker_ass)},
        }
        chat_path.write_text(json.dumps(chat))
        publish = {
            "video_path": str(burned),
            "artifact_hashes": {
                "burned_video_sha256": _sha(burned.read_bytes()),
                "chat_authority_audit_sha256": _sha(chat_path.read_bytes()),
            },
        }
        publish_path.write_text(json.dumps(publish))
        finalized_record = {
            "media_path": str(burned), "subtitle_path": str(subtitle),
            "chat_authority_audit_path": str(chat_path),
            "clip_context_path": str(output / f"{CID}.clip-context.json"),
            "story_contract": {
                "candidate_id": CID, "selection_hook": "钩子",
                # Frozen evidence must never be rewritten merely because it
                # happens to name the private finalizer workspace.
                "private_evidence_path": str(output / "frozen-evidence.json"),
            },
            "burned_preview": {
                "status": "BURNED", "path": str(burned),
                "burned_sha256": _sha(burned.read_bytes()),
            },
            "artifact_hashes": {
                "burned_video_sha256": _sha(burned.read_bytes()),
                "chat_authority_audit_sha256": _sha(chat_path.read_bytes()),
                "publish_draft_sha256": _sha(publish_path.read_bytes()),
            },
            "publish_staging": {"publish_json_path": str(publish_path)},
            "speaker_finalization_manifest_path": str(speaker_manifest_path),
            "speaker_finalization_manifest_sha256": _sha(speaker_manifest_path.read_bytes()),
            "speaker_finalization": speaker_manifest,
        }
        record_path.write_text(json.dumps(finalized_record))
        handle = prepare_talk_delivery(
            spec=kwargs["spec"], candidate_id=CID, record=finalized_record,
            staging={"publish_json_path": str(publish_path)}, record_path=record_path, subtitle_path=subtitle,
            speaker_review_srt=subtitle, speaker_ass=speaker_ass, speaker_manifest_path=speaker_manifest_path,
            redelivery_baseline_audit_path=None, subtitle_regression_audit_path=None,
            chat_authority_path=Path(kwargs["chat_authority_path"]),
            talk_filler_audit_path=None, text_manifest_path=None,
            delivery_root=kwargs["adapters"].delivery_root(),
        )
        assert handle.runtime_root == runtime
        calls["prepared"] = handle
        return 0

    stage = tmp_path / "private"
    stage.mkdir(mode=0o700)
    authority_root = _runtime_authority(tmp_path)
    pinned_intro = {"intro_id": "intro-a", "recorded_delivery_binding": {}}
    monkeypatch.setattr(replay, "_replay_branding_intro", lambda **_kwargs: pinned_intro)
    result = replay.synthesize_replay_spec_and_finalize_private(
        plan, stage=stage, runtime_authority_root=authority_root,
        speaker_python=Path("/usr/bin/python3"),
        source_fact_llm=lambda *_args, **_kwargs: "", adapters=Adapters(), finalizer=fake_finalizer,
    )
    assert calls["options"].prepare_only is True
    assert calls["spec"]["given_title"] is None
    assert calls["spec"]["recovery_publication_authority"] is None
    assert calls["spec"]["delivery_name"] == f"钩子__{CID}"
    assert calls["options"].speaker_mode == "auto"
    assert calls["speaker_subtitle_style_id"] == "lidousha-speaker-sapphire-host-white-guest-v2"
    assert calls["branding_intro"] is pinned_intro
    assert calls["spec"]["story_contract"]["session_relation_authority"] == {"status": "BOUND"}
    assert calls["spec"]["pieces"] == [{
        "remote_media": "/recording/source.flv", "start_ms": 1592760,
        "end_ms": 1746900, "source_media_sha256": "sha256:" + "1" * 64,
    }]
    assert calls["sanitized"]
    # C4 has 24 diagnostic cues but only 20 sealed release cues (four
    # operator drops). The finalizer/addressee-facing input must be release
    # truth, never the diagnostic source grid.
    diagnostic = plan.baseline.config["operator_truth_lanes"]["pipeline_diagnostic"]
    diagnostic_path = plan.baseline.manifest_path.parent / str(diagnostic["path"])
    assert len(calls["sanitized"]) == 20
    assert len(calls["sanitized"]) < diagnostic_path.read_text(encoding="utf-8").count("\n\n") + 1
    assert calls["sanitized"][2].text == "见面发现，あれ？"
    assert max(cue.source_end_ms for cue in calls["sanitized"]) < 200000
    assert calls["padded_provenance_path"] == plan.padded_path.with_suffix(".provenance.json")
    assert result.prepared_manifest.is_relative_to(result.private_runtime_root)
    assert result.prepared_manifest == calls["prepared"].manifest_path
    assert (result.private_runtime_root / "repo" / "DEPLOYED_COMMIT").is_file()
    assert (result.private_runtime_root / "repo" / "DEPLOYED_AUTHORITY_MANIFEST.json").read_bytes() == (
        authority_root / "repo" / "DEPLOYED_AUTHORITY_MANIFEST.json"
    ).read_bytes()
    assert not list(recut.glob("*.prepared.json"))

    class FakeProfile:
        def delivery_root_for(self, _repo: Path) -> Path:
            root = tmp_path / "profile-delivery"
            root.mkdir(exist_ok=True)
            return root

    import src.autoslice.channel_profile as channel_profile
    monkeypatch.setattr(channel_profile, "load_channel_profile", lambda _repo: FakeProfile())
    projection = replay.project_private_finalization_to_live(
        plan, finalization=result, runtime_authority_root=authority_root,
    )
    projected_record = json.loads(projection.record.path.read_text())
    projected_publish = json.loads(projection.publish.path.read_text())
    projected_chat = json.loads(projection.chat.path.read_text())
    projected_speaker = json.loads(projection.speaker_manifest.path.read_text())
    assert projection.live_delivery_root == tmp_path / "profile-delivery" / DATE
    recut_root = plan.package_root / "replacement_recuts"
    assert projected_record["media_path"] == str(recut_root / f"{CID}.recut.mp4")
    assert projected_publish["video_path"] == str(recut_root / f"{CID}.recut.mp4")
    assert projected_chat["final_text_srt_path"] == str(recut_root / f"{CID}.recut.srt")
    assert projected_speaker["source_media"] == str(recut_root / f"{CID}.recut.mp4")
    assert projected_speaker["profile"] == str(authority_root / "repo" / "speaker-profile.json")
    assert projected_record["speaker_finalization"] == projected_speaker
    assert projected_record["speaker_finalization_manifest_sha256"] == projection.speaker_manifest.sha256
    assert projected_record["artifact_hashes"]["chat_authority_audit_sha256"] == projection.chat.sha256
    assert projected_record["artifact_hashes"]["publish_draft_sha256"] == projection.publish.sha256
    assert projected_record["publish_staging"]["publish_json_path"] == str(
        recut_root / f"{CID}.recut.publish.json"
    )
    assert projected_record["chat_authority_audit_path"] == str(
        plan.package_root / f"{CID}.chat-authority.json"
    )
    assert projected_record["clip_context_path"] == str(
        plan.package_root / f"{CID}.clip-context.json"
    )
    assert projected_chat["speaker_manifest_sha256"] == projection.speaker_manifest.sha256.removeprefix("sha256:")
    assert projected_publish["artifact_hashes"]["chat_authority_audit_sha256"] == projection.chat.sha256
    assert projected_record["story_contract"]["private_evidence_path"].startswith(
        str(result.private_runtime_root)
    )
    assert str(result.private_runtime_root) not in projected_record["media_path"]


def test_private_finalizer_refuses_to_reuse_an_old_clean_review(
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
        "clip_context_path": str(clip), "speaker_mode": "auto",
        "subtitle_style": "lidousha-speaker-sapphire-host-white-guest-v2",
        "story_contract": {"candidate_id": CID, "selection_hook": "钩子"},
        "artifact_hashes": {**record["artifact_hashes"],
                            "chat_authority_audit_sha256": _sha(chat.read_bytes())},
    })
    record_path.write_text(json.dumps(record))
    plan = replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)
    monkeypatch.setattr(
        replay, "resolve_record_bound_finalizer_authority",
        lambda **_kwargs: SimpleNamespace(
            chat=replay.regular_binding(chat, label="CHAT"),
            clip_context=replay.regular_binding(clip, label="CLIP"), source="fixture",
        ),
    )

    @dataclass(frozen=True)
    class Adapters:
        stage_publish_draft: object = None
        delivery_root: object = None

    stage = tmp_path / "private"
    stage.mkdir(mode=0o700)
    with pytest.raises(replay.ReviewedBaselineReplayError, match="EXACT_FINAL_REVIEW_ADAPTER_MISSING"):
        replay.synthesize_replay_spec_and_finalize_private(
            plan, stage=stage, runtime_authority_root=_runtime_authority(tmp_path),
            speaker_python=Path("/usr/bin/python3"), source_fact_llm=lambda *_args: "",
            adapters=Adapters(), finalizer=lambda **_kwargs: 0,
        )


def test_private_finalizer_pins_existing_delivery_branding_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = {
        "status": "PREPENDED", "intro_id": "intro-a",
        "intro_media_sha256": "a" * 64, "intro_offset_ms": 1234,
    }
    current = {"candidates": [{"intro_id": "intro-a"}]}
    pinned = {"intro_id": "intro-a", "recorded_delivery_binding": recorded}
    monkeypatch.setattr(replay, "require_branding_intro", lambda _root: current)
    monkeypatch.setattr(
        replay, "pin_existing_delivery_intro",
        lambda observed, binding: pinned if observed is current and binding is recorded else {},
    )
    assert replay._replay_branding_intro(
        runtime_authority_root=Path("/runtime"), burned={"branding_intro": recorded}
    ) is pinned


def test_private_finalizer_refuses_missing_or_drifted_branding_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(replay.ReviewedBaselineReplayError, match="BRANDING_AUTHORITY_MISSING"):
        replay._replay_branding_intro(runtime_authority_root=Path("/runtime"), burned={})
    monkeypatch.setattr(replay, "require_branding_intro", lambda _root: {})
    monkeypatch.setattr(
        replay, "pin_existing_delivery_intro",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("drift")),
    )
    with pytest.raises(replay.ReviewedBaselineReplayError, match="BRANDING_AUTHORITY_DRIFT"):
        replay._replay_branding_intro(
            runtime_authority_root=Path("/runtime"),
            burned={"branding_intro": {
                "status": "PREPENDED", "intro_id": "intro-a",
                "intro_media_sha256": "a" * 64, "intro_offset_ms": 1234,
            }},
        )
