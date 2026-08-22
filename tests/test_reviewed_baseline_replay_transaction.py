from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from src.autoslice.producer_delivery_transaction import deployment_authority_binding
from src.autoslice.repository_asset_authority import _canonical_sha256
from src.autoslice.reviewed_baseline_replay_transaction import (
    ReplayAfterImage,
    ReviewedBaselineReplayTransactionError,
    cleanup_prepared_after_image_stage,
    commit_replay,
    _prepare_artifacts,
    resume_replay,
    stage_lane_after_image_artifacts,
)


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(0o600)


def _runtime(tmp_path: Path) -> tuple[Path, Path]:
    runtime = tmp_path / "runtime"
    (runtime / "repo").mkdir(parents=True)
    commit = "a" * 40
    manifest_body = {
        "schema_version": "deployed-authority-manifest.v1",
        "deployed_commit": commit,
        "entries": {"docs/pipeline/80-package-delivery.md": {
            "bytes": 1, "sha256": "sha256:" + "b" * 64,
        }},
    }
    _write(runtime / "repo" / "DEPLOYED_COMMIT", (commit + "\n").encode())
    _write(
        runtime / "repo" / "DEPLOYED_AUTHORITY_MANIFEST.json",
        json.dumps({**manifest_body, "manifest_sha256": _canonical_sha256(manifest_body)}).encode(),
    )
    state = runtime / "state" / "2026-08-14.json"
    _write(state, b'{"picks":[]}\n')
    return runtime, state


def _deployed(runtime: Path) -> dict[str, str]:
    return deployment_authority_binding(runtime)


def _prepared(tmp_path: Path, runtime: Path) -> tuple[tuple, Path]:
    stage = tmp_path / "stage"
    stage.mkdir()
    _write(stage / "recut.mp4", b"new-video")
    _write(stage / "record.json", b'{"new":true}\n')
    (runtime / "out").mkdir()
    target = runtime / "out" / "record.json"
    _write(target, b'{"old":true}\n')
    return _prepare_artifacts(stage_root=stage, targets={"recut.mp4": runtime / "out" / "recut.mp4", "record.json": target}), target


def test_commit_is_state_last_and_upload_free(tmp_path: Path) -> None:
    runtime, state = _runtime(tmp_path)
    artifacts, target = _prepared(tmp_path, runtime)
    before = state.read_bytes()
    journal = commit_replay(runtime_root=runtime, date="2026-08-14", candidate_id="cid",
                            deployed=_deployed(runtime), state_path=state, state_before=before,
                            state_after=b'{"picks":["cid"]}\n', artifacts=artifacts)
    assert json.loads(journal.read_text())["status"] == "COMMITTED"
    assert target.read_bytes() == b'{"new":true}\n'
    assert state.read_bytes() == b'{"picks":["cid"]}\n'


def test_target_drift_creates_no_journal_or_target_write(tmp_path: Path) -> None:
    runtime, state = _runtime(tmp_path)
    artifacts, target = _prepared(tmp_path, runtime)
    target.write_bytes(b"foreign")
    with pytest.raises(ReviewedBaselineReplayTransactionError, match="TARGET_DRIFT"):
        commit_replay(runtime_root=runtime, date="2026-08-14", candidate_id="cid",
                      deployed=_deployed(runtime), state_path=state, state_before=state.read_bytes(),
                      state_after=b'{"picks":["cid"]}\n', artifacts=artifacts)
    assert target.read_bytes() == b"foreign"
    assert not (runtime / ".reviewed-baseline-replay-journal").exists()


def test_state_drift_creates_no_journal(tmp_path: Path) -> None:
    runtime, state = _runtime(tmp_path)
    artifacts, _ = _prepared(tmp_path, runtime)
    stale = state.read_bytes()
    _write(state, b'{"external":true}\n')
    with pytest.raises(ReviewedBaselineReplayTransactionError, match="STATE_DRIFT"):
        commit_replay(runtime_root=runtime, date="2026-08-14", candidate_id="cid",
                      deployed=_deployed(runtime), state_path=state, state_before=stale,
                      state_after=b'{}\n', artifacts=artifacts)
    assert not (runtime / ".reviewed-baseline-replay-journal").exists()


def test_crash_after_artifact_is_provider_free_resume(tmp_path: Path) -> None:
    runtime, state = _runtime(tmp_path)
    artifacts, _ = _prepared(tmp_path, runtime)
    with pytest.raises(RuntimeError, match="INJECTED_CRASH"):
        commit_replay(runtime_root=runtime, date="2026-08-14", candidate_id="cid",
                      deployed=_deployed(runtime), state_path=state, state_before=state.read_bytes(),
                      state_after=b'{"picks":["cid"]}\n', artifacts=artifacts,
                      fail_after_role="recut.mp4")
    journal = next((runtime / ".reviewed-baseline-replay-journal").glob("*.json"))
    resume_replay(runtime_root=runtime, journal=journal)
    assert json.loads(journal.read_text())["status"] == "COMMITTED"
    assert state.read_bytes() == b'{"picks":["cid"]}\n'


def test_resume_marks_committed_when_state_was_already_written(tmp_path: Path) -> None:
    runtime, state = _runtime(tmp_path)
    artifacts, _ = _prepared(tmp_path, runtime)
    after = b'{"picks":["cid"]}\n'
    with pytest.raises(RuntimeError, match="INJECTED_CRASH"):
        commit_replay(runtime_root=runtime, date="2026-08-14", candidate_id="cid",
                      deployed=_deployed(runtime), state_path=state, state_before=state.read_bytes(),
                      state_after=after, artifacts=artifacts, fail_after_role="recut.mp4")
    journal = next((runtime / ".reviewed-baseline-replay-journal").glob("*.json"))
    _write(state, after)
    resume_replay(runtime_root=runtime, journal=journal)
    assert json.loads(journal.read_text())["status"] == "COMMITTED"
    assert state.read_bytes() == after


def test_lane_stage_copies_private_sources_without_touching_targets(tmp_path: Path) -> None:
    runtime, state = _runtime(tmp_path)
    source_root = tmp_path / "finalizer-private"
    source_root.mkdir()
    _write(source_root / "video", b"new-video")
    _write(source_root / "record", b'{"new":true}\n')
    (runtime / "out").mkdir()
    old_record = runtime / "out" / "record.json"
    _write(old_record, b'{"old":true}\n')
    artifacts = stage_lane_after_image_artifacts(
        runtime_root=runtime, date="2026-08-14", candidate_id="cid",
        sources={"video": source_root / "video", "delivery-record": source_root / "record"},
        targets={"video": runtime / "out" / "video.mp4", "delivery-record": old_record},
    )
    assert not (runtime / "out" / "video.mp4").exists()
    assert old_record.read_bytes() == b'{"old":true}\n'
    assert all(artifact.staged.path.is_relative_to(runtime / ".reviewed-baseline-replay-private") for artifact in artifacts)
    commit_replay(
        runtime_root=runtime, date="2026-08-14", candidate_id="cid", deployed=_deployed(runtime),
        state_path=state, state_before=state.read_bytes(), state_after=b'{"picks":["cid"]}\n',
        artifacts=artifacts,
    )
    assert (runtime / "out" / "video.mp4").read_bytes() == b"new-video"
    assert old_record.read_bytes() == b'{"new":true}\n'


def test_private_stage_cleanup_refuses_a_pending_same_candidate_journal(tmp_path: Path) -> None:
    runtime, state = _runtime(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    _write(source / "video", b"new-video")
    artifacts = stage_lane_after_image_artifacts(
        runtime_root=runtime, date="2026-08-14", candidate_id="cid",
        sources={"video": source / "video"}, targets={"video": runtime / "out" / "video.mp4"},
    )
    after = ReplayAfterImage(
        date="2026-08-14", candidate_id="cid", deployed=_deployed(runtime), state_path=state,
        state_before=state.read_bytes(), state_after=b'{"picks":["cid"]}\n',
        record_before_sha256="sha256:" + "a" * 64, stage_sha256="sha256:" + "b" * 64,
        artifacts=artifacts,
    )
    with pytest.raises(RuntimeError, match="INJECTED_CRASH"):
        commit_replay(runtime_root=runtime, date=after.date, candidate_id=after.candidate_id,
                      deployed=after.deployed, state_path=state, state_before=after.state_before,
                      state_after=after.state_after, artifacts=artifacts, fail_after_role="video")
    with pytest.raises(ReviewedBaselineReplayTransactionError, match="CLEANUP_PENDING"):
        cleanup_prepared_after_image_stage(runtime_root=runtime, after=after)


def test_lane_stage_allows_new_delivery_date_without_creating_it_until_commit(tmp_path: Path) -> None:
    runtime, state = _runtime(tmp_path)
    source_root = runtime / "candidate-private"
    source_root.mkdir()
    _write(source_root / "video", b"new-video")
    target = runtime / "repo" / "lidousha" / "2026-08-14" / "cid__cid.mp4"
    artifacts = stage_lane_after_image_artifacts(
        runtime_root=runtime, date="2026-08-14", candidate_id="cid",
        sources={"video": source_root / "video"}, targets={"video": target},
    )
    assert not target.parent.exists()
    commit_replay(
        runtime_root=runtime, date="2026-08-14", candidate_id="cid", deployed=_deployed(runtime),
        state_path=state, state_before=state.read_bytes(), state_after=b'{"picks":["cid"]}\n',
        artifacts=artifacts,
    )
    assert target.read_bytes() == b"new-video"


def test_stage_copy_failure_removes_its_private_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _ = _runtime(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    _write(source / "one", b"one")
    _write(source / "two", b"two")
    import src.autoslice.reviewed_baseline_replay_transaction as txn
    original = txn._copy_stage_regular
    calls = 0
    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_INJECTED_COPY_FAILURE")
        return original(*args, **kwargs)
    monkeypatch.setattr(txn, "_copy_stage_regular", fail_second)
    with pytest.raises(ReviewedBaselineReplayTransactionError, match="INJECTED_COPY_FAILURE"):
        stage_lane_after_image_artifacts(
            runtime_root=runtime, date="2026-08-14", candidate_id="cid",
            sources={"one": source / "one", "two": source / "two"},
            targets={"one": runtime / "out" / "one", "two": runtime / "out" / "two"},
        )
    assert not list((runtime / ".reviewed-baseline-replay-private").rglob("one"))


def test_post_copy_pre_return_failure_removes_private_transaction_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _ = _runtime(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    _write(source / "video", b"new-video")
    import src.autoslice.reviewed_baseline_replay_transaction as txn
    monkeypatch.setattr(
        txn, "_prepare_artifacts",
        lambda **_kwargs: (_ for _ in ()).throw(ReviewedBaselineReplayTransactionError("REPLAY_TXN_POST_STAGE_FAILURE")),
    )
    with pytest.raises(ReviewedBaselineReplayTransactionError, match="POST_STAGE_FAILURE"):
        stage_lane_after_image_artifacts(
            runtime_root=runtime, date="2026-08-14", candidate_id="cid",
            sources={"video": source / "video"}, targets={"video": runtime / "out" / "video.mp4"},
        )
    private_root = runtime / ".reviewed-baseline-replay-private"
    assert not list(private_root.rglob("video"))


def test_same_seed_collision_cannot_remove_the_winner_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _ = _runtime(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    _write(source / "video", b"new-video")
    import src.autoslice.reviewed_baseline_replay_transaction as txn
    entered = threading.Event()
    release = threading.Event()
    original = txn._copy_stage_regular

    def pause_copy(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return original(*args, **kwargs)

    monkeypatch.setattr(txn, "_copy_stage_regular", pause_copy)
    result: list[tuple] = []

    def winner() -> None:
        result.append(stage_lane_after_image_artifacts(
            runtime_root=runtime, date="2026-08-14", candidate_id="cid",
            sources={"video": source / "video"}, targets={"video": runtime / "out" / "video.mp4"},
        ))

    thread = threading.Thread(target=winner)
    thread.start()
    assert entered.wait(timeout=2)
    with pytest.raises(ReviewedBaselineReplayTransactionError, match="STAGE_COLLISION"):
        stage_lane_after_image_artifacts(
            runtime_root=runtime, date="2026-08-14", candidate_id="cid",
            sources={"video": source / "video"}, targets={"video": runtime / "out" / "video.mp4"},
        )
    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert result and result[0][0].staged.path.read_bytes() == b"new-video"


def test_target_with_dotdot_is_rejected_before_private_stage(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    _write(source / "video", b"new-video")
    with pytest.raises(ReviewedBaselineReplayTransactionError, match="TARGET_INVALID"):
        stage_lane_after_image_artifacts(
            runtime_root=runtime, date="2026-08-14", candidate_id="cid",
            sources={"video": source / "video"},
            targets={"video": runtime / "out" / "x" / ".." / ".." / "escape.mp4"},
        )


@pytest.mark.parametrize("name", ["link", "fifo", "directory"])
def test_stage_must_be_regular(tmp_path: Path, name: str) -> None:
    runtime, _ = _runtime(tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()
    if name == "link":
        (stage / "recut.mp4").symlink_to(runtime / "repo" / "DEPLOYED_COMMIT")
    elif name == "fifo":
        import os
        os.mkfifo(stage / "recut.mp4")
    else:
        (stage / "recut.mp4").mkdir()
    with pytest.raises(ReviewedBaselineReplayTransactionError, match="STAGED_UNSAFE"):
        _prepare_artifacts(stage_root=stage, targets={"recut.mp4": runtime / "out" / "recut.mp4"})


@pytest.mark.parametrize("alter", ["commit", "manifest"])
def test_deployed_drift_blocks_before_journal(tmp_path: Path, alter: str) -> None:
    runtime, state = _runtime(tmp_path)
    artifacts, _ = _prepared(tmp_path, runtime)
    deployed = _deployed(runtime)
    _write(runtime / "repo" / ("DEPLOYED_COMMIT" if alter == "commit" else "DEPLOYED_AUTHORITY_MANIFEST.json"), b"drift\n")
    with pytest.raises(ReviewedBaselineReplayTransactionError, match="DEPLOYED_DRIFT"):
        commit_replay(runtime_root=runtime, date="2026-08-14", candidate_id="cid", deployed=deployed,
                      state_path=state, state_before=state.read_bytes(), state_after=b'{}\n', artifacts=artifacts)
