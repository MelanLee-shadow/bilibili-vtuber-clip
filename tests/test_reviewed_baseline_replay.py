from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.autoslice.reviewed_baseline_replay as replay
from src.autoslice.repository_asset_authority import _canonical_sha256


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
    reviewed = (stage / "reviewed.srt").read_text(encoding="utf-8")
    assert reviewed.strip()
    assert "00:00:" in reviewed
    assert "1592760" not in reviewed
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


def test_state_projection_restores_canonical_talk_fields_and_drops_rejection_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
        "cover_sha256": "sha256:" + "3" * 64, "cover_status": "AI_COVER_READY",
        "cover_generation": {"status": "READY"}, "title": "冻结标题",
    }
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
    plan = replay.ReplayPlan(
        DATE, CID, source_root.parent, source_root / f"{CID}.recut.record.json",
        source_root / "padded_0_1.mp4", 0, 1, "sha256:" + "0" * 64,
        object(), (),
    )
    package = replay.flatten_and_audit_private_replay(finalization, plan=plan)
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
    with pytest.raises(replay.ReviewedBaselineReplayError, match="FROZEN_TITLE_AUTHORITY_DRIFT"):
        adapter({}, cues=[], run_ffmpeg=False)


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
        "artifact_hashes": {**record["artifact_hashes"], "chat_authority_audit_sha256": _sha(chat.read_bytes())},
    })
    record_path.write_text(json.dumps(record))
    plan = replay.build_replay_plan(repo_root=ROOT, out_root=out_root, date=DATE, candidate_id=CID)

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
    assert calls["spec"]["story_contract"]["session_relation_authority"] == {"status": "BOUND"}
    assert calls["spec"]["pieces"] == [{
        "remote_media": "/recording/source.flv", "start_ms": 1592760,
        "end_ms": 1746900, "source_media_sha256": "sha256:" + "1" * 64,
    }]
    assert calls["sanitized"]
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
    tmp_path: Path,
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
