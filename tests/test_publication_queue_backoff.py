from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.autoslice.publication_queue_backoff import (
    GENERIC_BASE_SECONDS,
    QUOTA_SECONDS,
    STATE_FILENAME,
    evaluate_upload_backoff,
    record_queue_result,
)

NOW = 2_000_000_000.0


def _manifest(runtime: Path, name: str = "candidate") -> tuple[Path, str]:
    video_sha = hashlib.sha256(name.encode()).hexdigest()
    path = runtime / "out" / name / f"{name}.upload_manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "manifest_version": 3,
                "video": {"sha256": video_sha},
            }
        ),
        encoding="utf-8",
    )
    return path.resolve(), video_sha


def _at(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def _failed_rows(
    manifest: Path,
    video_sha: str,
    *,
    count: int,
    latest_at: float,
    quota: bool = False,
) -> list[dict]:
    manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    rows: list[dict] = []
    for index in range(count):
        attempt = f"attempt-{index}"
        stable = {
            "attempt_id": attempt,
            "manifest": str(manifest),
            "manifest_sha256": manifest_sha,
            "video_sha256": video_sha,
            "cover_sha256": "c" * 64,
            "uploader": "fixture",
        }
        rows.extend(
            [
                {
                    "event": "UPLOAD_ATTEMPT_STARTED",
                    "at": _at(latest_at - (count - index) * 60),
                    **stable,
                },
                {
                    "event": "UPLOAD_ATTEMPT_FINISHED",
                    "at": _at(latest_at - (count - index - 1) * 60),
                    **stable,
                    "uploader_rc": 1,
                    "rc": 1,
                    "bvid": None,
                    **({"quota_frequency_code": 21566} if quota and index == count - 1 else {}),
                },
            ]
        )
    return rows


def test_ledger_failure_backoff_survives_without_sidecar(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    manifest, video_sha = _manifest(runtime)
    entries = _failed_rows(
        manifest,
        video_sha,
        count=2,
        latest_at=NOW - 30,
    )

    gate = evaluate_upload_backoff(
        runtime_root=runtime,
        manifest_path=manifest,
        ledger_entries=entries,
        now_epoch=NOW,
    )

    assert gate["status"] == "BACKOFF"
    assert gate["source"] == "UPLOAD_LEDGER"
    assert gate["failure_count"] == 2
    assert gate["retry_after_seconds"] == GENERIC_BASE_SECONDS * 2 - 30


def test_manifest_change_resets_candidate_backoff(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    manifest, video_sha = _manifest(runtime)
    entries = _failed_rows(manifest, video_sha, count=1, latest_at=NOW - 10)
    manifest.write_text(
        json.dumps(
            {
                "manifest_version": 3,
                "video": {"sha256": hashlib.sha256(b"changed").hexdigest()},
            }
        ),
        encoding="utf-8",
    )

    gate = evaluate_upload_backoff(
        runtime_root=runtime,
        manifest_path=manifest,
        ledger_entries=entries,
        now_epoch=NOW,
    )

    assert gate["status"] == "CLEAR"


def test_quota_failure_is_global_and_uses_24_hour_window(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    failed_manifest, failed_video = _manifest(runtime, "failed")
    current_manifest, _current_video = _manifest(runtime, "current")
    entries = _failed_rows(
        failed_manifest,
        failed_video,
        count=1,
        latest_at=NOW - 60,
        quota=True,
    )

    gate = evaluate_upload_backoff(
        runtime_root=runtime,
        manifest_path=current_manifest,
        ledger_entries=entries,
        now_epoch=NOW,
    )

    assert gate["status"] == "BACKOFF"
    assert gate["scope"] == "GLOBAL_QUOTA"
    assert gate["retry_after_seconds"] == QUOTA_SECONDS - 60


def test_quota_result_sidecar_is_atomic_durable_and_self_hashed(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    manifest, _video_sha = _manifest(runtime)

    recorded = record_queue_result(
        runtime_root=runtime,
        manifest_path=manifest,
        rc=8,
        status="QUOTA_BLOCKED",
        ledger_entries=[],
        now_epoch=NOW,
    )
    path = runtime / "reports" / STATE_FILENAME

    assert recorded["status"] == "RECORDED"
    assert path.is_file()
    assert path.stat().st_mode & 0o077 == 0
    gate = evaluate_upload_backoff(
        runtime_root=runtime,
        manifest_path=manifest,
        ledger_entries=[],
        now_epoch=NOW + 1,
    )
    assert gate["status"] == "BACKOFF"
    assert gate["scope"] == "GLOBAL_QUOTA"
    assert gate["source"] == "QUEUE_RESULT"


def test_sidecar_tamper_fails_closed(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    manifest, _video_sha = _manifest(runtime)
    record_queue_result(
        runtime_root=runtime,
        manifest_path=manifest,
        rc=8,
        status="QUOTA_BLOCKED",
        ledger_entries=[],
        now_epoch=NOW,
    )
    path = runtime / "reports" / STATE_FILENAME
    document = json.loads(path.read_text())
    document["next_attempt_epoch"] = NOW - 1
    path.write_text(json.dumps(document), encoding="utf-8")

    gate = evaluate_upload_backoff(
        runtime_root=runtime,
        manifest_path=manifest,
        ledger_entries=[],
        now_epoch=NOW,
    )

    assert gate["status"] == "INVALID"
    assert gate["reason_code"] == "PUBLICATION_QUEUE_BACKOFF_STATE_INVALID"


def test_success_clears_matching_sidecar(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    manifest, _video_sha = _manifest(runtime)
    record_queue_result(
        runtime_root=runtime,
        manifest_path=manifest,
        rc=8,
        status="QUOTA_BLOCKED",
        ledger_entries=[],
        now_epoch=NOW,
    )

    cleared = record_queue_result(
        runtime_root=runtime,
        manifest_path=manifest,
        rc=0,
        status="ACTION_COMPLETED",
        ledger_entries=[],
        now_epoch=NOW + 10,
    )

    assert cleared["status"] == "CLEARED"
    assert not (runtime / "reports" / STATE_FILENAME).exists()


def test_success_for_other_manifest_preserves_local_backoff(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    blocked, _blocked_video = _manifest(runtime, "blocked")
    other, _other_video = _manifest(runtime, "other")
    recorded = record_queue_result(
        runtime_root=runtime,
        manifest_path=blocked,
        rc=17,
        status="ACTION_FAILED",
        ledger_entries=[],
        now_epoch=NOW,
    )
    assert recorded["status"] == "RECORDED"
    state_path = runtime / "reports" / STATE_FILENAME
    before = state_path.read_bytes()

    result = record_queue_result(
        runtime_root=runtime,
        manifest_path=other,
        rc=0,
        status="ACTION_COMPLETED",
        ledger_entries=[],
        now_epoch=NOW + 1,
    )

    assert result["status"] == "PRESERVED"
    assert result["scope"] == "MANIFEST"
    assert result["manifest_path"] == str(blocked)
    assert state_path.read_bytes() == before
    gate = evaluate_upload_backoff(
        runtime_root=runtime,
        manifest_path=blocked,
        ledger_entries=[],
        now_epoch=NOW + 2,
    )
    assert gate["status"] == "BACKOFF"
    assert gate["scope"] == "MANIFEST"
