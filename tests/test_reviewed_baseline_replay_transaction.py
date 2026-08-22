from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.autoslice.reviewed_baseline_replay_transaction import (
    ReviewedBaselineReplayTransactionError,
    commit_replay,
    _prepare_artifacts,
    resume_replay,
    stream_binding,
)


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(0o600)


def _runtime(tmp_path: Path) -> tuple[Path, Path]:
    runtime = tmp_path / "runtime"
    (runtime / "repo").mkdir(parents=True)
    _write(runtime / "repo" / "DEPLOYED_COMMIT", b"abc\n")
    _write(runtime / "repo" / "DEPLOYED_AUTHORITY_MANIFEST", b"{}\n")
    state = runtime / "state" / "2026-08-14.json"
    _write(state, b'{"picks":[]}\n')
    return runtime, state


def _deployed(runtime: Path) -> dict[str, str]:
    return {
        "commit": stream_binding(runtime / "repo" / "DEPLOYED_COMMIT", label="TEST").sha256,  # type: ignore[union-attr]
        "authority_manifest_sha256": stream_binding(runtime / "repo" / "DEPLOYED_AUTHORITY_MANIFEST", label="TEST").sha256,  # type: ignore[union-attr]
    }


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
    _write(runtime / "repo" / ("DEPLOYED_COMMIT" if alter == "commit" else "DEPLOYED_AUTHORITY_MANIFEST"), b"drift\n")
    with pytest.raises(ReviewedBaselineReplayTransactionError, match="DEPLOYED_DRIFT"):
        commit_replay(runtime_root=runtime, date="2026-08-14", candidate_id="cid", deployed=deployed,
                      state_path=state, state_before=state.read_bytes(), state_after=b'{}\n', artifacts=artifacts)
