"""Adversarial canaries for the disabled-only historical fastlane authority."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.autoslice.historical_fastlane_authority import (
    HistoricalFastlaneAuthorityError,
    commit_scope_renewal,
    create_historical_run_authority,
    finish_historical_run,
    load_and_start_historical_run,
    prepare_historical_run_authority,
    prepare_scope_renewal,
)
from src.autoslice.producer_delivery_transaction import deployment_authority_binding
from src.autoslice.repository_asset_authority import DEPLOYED_AUTHORITY_MANIFEST_SCHEMA, _canonical_sha256


DATE = "2026-08-14"
IDS = ("auto_113028_1602_1698", "auto_113028_1271_1328", "auto_120032_753_816", "auto_123036_727_785")
NOW = datetime(2026, 8, 22, tzinfo=timezone.utc)


def _sha(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, mode)


def _runtime(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "runtime"
    (root / "state").mkdir(parents=True)
    (root / "repo").mkdir()
    _write(root / "repo" / "DEPLOYED_COMMIT", b"1" * 40 + b"\n")
    body = {"schema_version": DEPLOYED_AUTHORITY_MANIFEST_SCHEMA, "deployed_commit": "1" * 40, "entries": {"assets/x": {"bytes": 1, "sha256": "sha256:" + "2" * 64}}}
    manifest = dict(body)
    manifest["manifest_sha256"] = _canonical_sha256(body)
    _write(root / "repo" / "DEPLOYED_AUTHORITY_MANIFEST.json", json.dumps(manifest, sort_keys=True).encode())
    state = {
        "operator_processing_scope": {
            "schema_version": "operator-processing-scope-grant.v2",
            "grant_id": "old-grant", "recording_date": DATE, "reason": "historical named failed picks",
            "candidate_ids": list(IDS),
            "user_authorization": {"quote": "Ivan explicitly approved these historical candidates", "timestamp": "2026-08-19T00:08:52Z"},
            "expires_at": "2026-08-25T00:00:00Z", "intent": "RECOVER_NAMED_FAILED_PICKS",
        },
        "pending_talk": [{"cid": cid} for cid in IDS],
    }
    state_path = root / "state" / f"{DATE}.json"
    _write(state_path, json.dumps(state, ensure_ascii=False, indent=2).encode())
    rec = tmp_path / "recordings"
    date_dir = rec / DATE
    date_dir.mkdir(parents=True)
    _write(date_dir / "123_20260814-11-30-25.mp4", b"mp4", 0o644)
    adapter = tmp_path / "adapter.json"
    _write(adapter, json.dumps({"service_reachable": True, "streaming": False, "recording": False, "finalizing": False, "error": None}).encode())
    return root, rec


def test_scope_renewal_only_changes_grant_and_expiry_and_recovers(tmp_path: Path):
    root, _ = _runtime(tmp_path)
    state_path = root / "state" / f"{DATE}.json"
    before = state_path.read_bytes()
    prepared = prepare_scope_renewal(
        runtime_root=root, date=DATE, candidate_ids=IDS, new_grant_id="renewed-grant",
        expires_at="2026-08-26T00:00:00Z", expected_state_sha256=_sha(before),
        expected_authority=deployment_authority_binding(root), now=NOW,
    )
    assert not (root / ".operator-scope-renewals").exists()
    assert prepared.receipt["pointer_diff"] and "user_authorization" not in json.dumps(prepared.receipt)
    commit_scope_renewal(prepared, runtime_root=root)
    after = json.loads(state_path.read_text())
    assert after["operator_processing_scope"]["grant_id"] == "renewed-grant"
    assert after["operator_processing_scope"]["expires_at"] == "2026-08-26T00:00:00Z"
    assert commit_scope_renewal(prepared, runtime_root=root) == prepared.receipt_path


def test_scope_renewal_refuses_other_v2_shape_and_state_drift(tmp_path: Path):
    root, _ = _runtime(tmp_path)
    state_path = root / "state" / f"{DATE}.json"
    before = state_path.read_bytes()
    state = json.loads(before)
    state["operator_processing_scope"]["upload_allowed"] = False
    _write(state_path, json.dumps(state).encode())
    with pytest.raises(HistoricalFastlaneAuthorityError, match="SCOPE_(MISMATCH|INVALID)"):
        prepare_scope_renewal(runtime_root=root, date=DATE, candidate_ids=IDS, new_grant_id="renewed-grant", expires_at="2026-08-26T00:00:00Z", expected_state_sha256=_sha(state_path.read_bytes()), expected_authority=deployment_authority_binding(root), now=NOW)


def test_historical_authority_requires_disabled_clean_idle_source_and_is_single_use(tmp_path: Path):
    root, rec = _runtime(tmp_path)
    (root / "DISABLED").write_text("paused\n")
    state = (root / "state" / f"{DATE}.json").read_bytes()
    adapter = tmp_path / "adapter.json"
    path, document = prepare_historical_run_authority(
        runtime_root=root, recording_root=rec, adapter_status_path=adapter, date=DATE,
        candidate_ids=IDS, nonce="historical-20260814-a", expires_at="2026-08-22T00:30:00Z",
        expected_state_sha256=_sha(state), expected_authority=deployment_authority_binding(root),
        direct_recorder_idle=True, now=NOW, room_id=123,
    )
    assert not path.parent.exists()
    create_historical_run_authority(path, document)
    started = load_and_start_historical_run(authority_path=path, runtime_root=root, recording_root=rec, adapter_status_path=adapter, now=NOW, room_id=123)
    assert started["status"] == "STARTED" and started["upload_allowed"] is False
    with pytest.raises(HistoricalFastlaneAuthorityError, match="RUN_REPLAY_REFUSED"):
        load_and_start_historical_run(authority_path=path, runtime_root=root, recording_root=rec, adapter_status_path=adapter, now=NOW, room_id=123)
    finish_historical_run(authority_path=path, runtime_root=root)


def test_historical_authority_refuses_missing_disabled_or_source_drift(tmp_path: Path):
    root, rec = _runtime(tmp_path)
    state = (root / "state" / f"{DATE}.json").read_bytes()
    with pytest.raises(HistoricalFastlaneAuthorityError, match="DISABLED_REQUIRED"):
        prepare_historical_run_authority(runtime_root=root, recording_root=rec, adapter_status_path=tmp_path / "adapter.json", date=DATE, candidate_ids=IDS, nonce="historical-20260814-b", expires_at="2026-08-22T00:30:00Z", expected_state_sha256=_sha(state), expected_authority=deployment_authority_binding(root), direct_recorder_idle=True, now=NOW, room_id=123)
    (root / "DISABLED").write_text("paused\n")
    path, document = prepare_historical_run_authority(runtime_root=root, recording_root=rec, adapter_status_path=tmp_path / "adapter.json", date=DATE, candidate_ids=IDS, nonce="historical-20260814-b", expires_at="2026-08-22T00:30:00Z", expected_state_sha256=_sha(state), expected_authority=deployment_authority_binding(root), direct_recorder_idle=True, now=NOW, room_id=123)
    create_historical_run_authority(path, document)
    _write(rec / DATE / "extra.flv", b"drift", 0o644)
    with pytest.raises(HistoricalFastlaneAuthorityError, match="SOURCE_PREIMAGE_DRIFT"):
        load_and_start_historical_run(authority_path=path, runtime_root=root, recording_root=rec, adapter_status_path=tmp_path / "adapter.json", now=NOW, room_id=123)


def test_historical_authority_refuses_bad_flv_and_active_adapter_before_any_receipt(tmp_path: Path):
    root, rec = _runtime(tmp_path)
    (root / "DISABLED").write_text("paused\n")
    state = (root / "state" / f"{DATE}.json").read_bytes()
    (rec / DATE / "123_20260814-11-30-25.mp4").unlink()
    _write(rec / DATE / "123_20260814-11-30-25.flv", b"raw", 0o644)
    with pytest.raises(HistoricalFastlaneAuthorityError, match="SOURCE_INVENTORY_BLOCKED"):
        prepare_historical_run_authority(runtime_root=root, recording_root=rec, adapter_status_path=tmp_path / "adapter.json", date=DATE, candidate_ids=IDS, nonce="historical-20260814-c", expires_at="2026-08-22T00:30:00Z", expected_state_sha256=_sha(state), expected_authority=deployment_authority_binding(root), direct_recorder_idle=True, now=NOW, room_id=123)
    (rec / DATE / "123_20260814-11-30-25.flv").unlink()
    _write(rec / DATE / "123_20260814-11-30-25.mp4", b"mp4", 0o644)
    _write(tmp_path / "adapter.json", json.dumps({"service_reachable": True, "streaming": True, "recording": True, "finalizing": False, "error": None}).encode())
    with pytest.raises(HistoricalFastlaneAuthorityError, match="ADAPTER_NOT_CLEAN_IDLE"):
        prepare_historical_run_authority(runtime_root=root, recording_root=rec, adapter_status_path=tmp_path / "adapter.json", date=DATE, candidate_ids=IDS, nonce="historical-20260814-c", expires_at="2026-08-22T00:30:00Z", expected_state_sha256=_sha(state), expected_authority=deployment_authority_binding(root), direct_recorder_idle=True, now=NOW, room_id=123)


def test_cron_shaped_runner_invocation_cannot_activate_historical_authority(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    runtime = tmp_path / "runtime"
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "free_session_autoslice.py"), "--historical-authority", str(tmp_path / "authority.json")],
        cwd=root, env={**os.environ, "AUTOSLICE_BASE": str(runtime)}, text=True,
        capture_output=True, check=False,
    )
    assert result.returncode == 2
    assert "requires --once" in result.stderr
    assert not (runtime / ".historical-autoslice-once").exists()
