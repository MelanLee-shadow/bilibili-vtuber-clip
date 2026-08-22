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

import scripts.free_session_autoslice as runner
from src.autoslice.historical_fastlane_authority import (
    HistoricalFastlaneAuthorityError,
    commit_scope_renewal,
    create_historical_run_authority,
    finish_historical_run,
    load_and_start_historical_run,
    prepare_historical_run_authority,
    prepare_scope_renewal,
)
from src.autoslice import historical_fastlane_authority as historical_module
from src.autoslice.producer_delivery_transaction import deployment_authority_binding
from src.autoslice.repository_asset_authority import DEPLOYED_AUTHORITY_MANIFEST_SCHEMA, _canonical_sha256
from src.autoslice.runner_state_writeback import state_bytes


DATE = "2026-08-14"
IDS = ("auto_113028_1602_1698", "auto_113028_1271_1328", "auto_120032_753_816", "auto_123036_727_785")
NOW = datetime(2026, 8, 22, tzinfo=timezone.utc)


def _sha(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, mode)


def _runtime(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
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
    _write(adapter, json.dumps({
        "schema_version": "recorder-neutral-status.v1", "room_id": "123",
        "generated_at_epoch": NOW.timestamp(), "service_reachable": True,
        "live_status": 0, "streaming": False, "recording": False,
        "finalizing": False, "error": None,
    }).encode())
    adapter_state = tmp_path / "adapter-state.json"
    _write(adapter_state, json.dumps({"schema_version": "bililive-recorder-adapter-state.v1"}).encode())
    recorder_env = tmp_path / "recorder.env"
    _write(recorder_env, b"", 0o600)
    return root, rec, adapter, adapter_state


@pytest.fixture(autouse=True)
def direct_recorder_idle(monkeypatch):
    monkeypatch.setattr(
        historical_module,
        "query_room_status",
        lambda *_args, **_kwargs: {"streaming": False, "recording": False},
    )


def test_scope_renewal_only_changes_grant_and_expiry_and_recovers(tmp_path: Path):
    root, _, _, _ = _runtime(tmp_path)
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


def test_scope_renewal_preserves_canonical_terminal_newline(tmp_path: Path):
    root, _, _, _ = _runtime(tmp_path)
    state_path = root / "state" / f"{DATE}.json"
    before = state_path.read_bytes() + b"\n"
    _write(state_path, before)
    prepared = prepare_scope_renewal(
        runtime_root=root, date=DATE, candidate_ids=IDS, new_grant_id="renewed-newline",
        expires_at="2026-08-26T00:00:00Z", expected_state_sha256=_sha(before),
        expected_authority=deployment_authority_binding(root), now=NOW,
    )
    expected = json.loads(before)
    expected["operator_processing_scope"]["grant_id"] = "renewed-newline"
    expected["operator_processing_scope"]["expires_at"] = "2026-08-26T00:00:00Z"
    assert prepared.after == state_bytes(expected) + b"\n"
    commit_scope_renewal(prepared, runtime_root=root)
    assert state_path.read_bytes() == prepared.after


def test_scope_renewal_refuses_noncanonical_state_whitespace(tmp_path: Path):
    root, _, _, _ = _runtime(tmp_path)
    state_path = root / "state" / f"{DATE}.json"
    malformed_rendering = state_path.read_bytes().replace(b'\n  "pending_talk"', b'\n    "pending_talk"')
    _write(state_path, malformed_rendering)
    with pytest.raises(HistoricalFastlaneAuthorityError, match="STATE_FORMAT_INVALID"):
        prepare_scope_renewal(
            runtime_root=root, date=DATE, candidate_ids=IDS, new_grant_id="renewed-whitespace",
            expires_at="2026-08-26T00:00:00Z", expected_state_sha256=_sha(malformed_rendering),
            expected_authority=deployment_authority_binding(root), now=NOW,
        )


def test_scope_renewal_refuses_other_v2_shape_and_state_drift(tmp_path: Path):
    root, _, _, _ = _runtime(tmp_path)
    state_path = root / "state" / f"{DATE}.json"
    before = state_path.read_bytes()
    state = json.loads(before)
    state["operator_processing_scope"]["upload_allowed"] = False
    _write(state_path, state_bytes(state))
    with pytest.raises(HistoricalFastlaneAuthorityError, match="SCOPE_(MISMATCH|INVALID)"):
        prepare_scope_renewal(runtime_root=root, date=DATE, candidate_ids=IDS, new_grant_id="renewed-grant", expires_at="2026-08-26T00:00:00Z", expected_state_sha256=_sha(state_path.read_bytes()), expected_authority=deployment_authority_binding(root), now=NOW)


def test_scope_renewal_refuses_wrong_candidate_order_or_authorization_shape(tmp_path: Path):
    root, _, _, _ = _runtime(tmp_path)
    state_path = root / "state" / f"{DATE}.json"
    with pytest.raises(HistoricalFastlaneAuthorityError, match="SCOPE_MISMATCH"):
        prepare_scope_renewal(
            runtime_root=root, date=DATE, candidate_ids=tuple(reversed(IDS)), new_grant_id="renewed-grant",
            expires_at="2026-08-26T00:00:00Z", expected_state_sha256=_sha(state_path.read_bytes()),
            expected_authority=deployment_authority_binding(root), now=NOW,
        )
    state = json.loads(state_path.read_text())
    state["operator_processing_scope"]["user_authorization"] = {"quote": "missing timestamp"}
    _write(state_path, state_bytes(state))
    with pytest.raises(HistoricalFastlaneAuthorityError, match="SCOPE_INVALID"):
        prepare_scope_renewal(
            runtime_root=root, date=DATE, candidate_ids=IDS, new_grant_id="renewed-grant",
            expires_at="2026-08-26T00:00:00Z", expected_state_sha256=_sha(state_path.read_bytes()),
            expected_authority=deployment_authority_binding(root), now=NOW,
        )


def test_historical_authority_requires_disabled_clean_idle_source_and_is_single_use(tmp_path: Path):
    root, rec, adapter, adapter_state = _runtime(tmp_path)
    (root / "DISABLED").write_text("paused\n")
    state = (root / "state" / f"{DATE}.json").read_bytes()
    path, document = prepare_historical_run_authority(
        runtime_root=root, recording_root=rec, adapter_status_path=adapter, date=DATE,
        candidate_ids=IDS, nonce="historical-20260814-a", expires_at="2026-08-22T00:30:00Z",
        expected_state_sha256=_sha(state), expected_authority=deployment_authority_binding(root),
        now=NOW, room_id=123, adapter_state_path=adapter_state,
        recorder_endpoint="http://test/graphql", recorder_env=tmp_path / "recorder.env",
    )
    assert not path.parent.exists()
    create_historical_run_authority(path, document)
    started = load_and_start_historical_run(authority_path=path, runtime_root=root, recording_root=rec, adapter_status_path=adapter, adapter_state_path=adapter_state, recorder_endpoint="http://test/graphql", recorder_env=tmp_path / "recorder.env", now=NOW, room_id=123)
    assert started["status"] == "STARTED" and started["upload_allowed"] is False
    with pytest.raises(HistoricalFastlaneAuthorityError, match="RUN_REPLAY_REFUSED"):
        load_and_start_historical_run(authority_path=path, runtime_root=root, recording_root=rec, adapter_status_path=adapter, adapter_state_path=adapter_state, recorder_endpoint="http://test/graphql", recorder_env=tmp_path / "recorder.env", now=NOW, room_id=123)
    finish_historical_run(authority_path=path, runtime_root=root)


def test_historical_authority_refuses_missing_disabled_or_source_drift(tmp_path: Path):
    root, rec, adapter, adapter_state = _runtime(tmp_path)
    state = (root / "state" / f"{DATE}.json").read_bytes()
    with pytest.raises(HistoricalFastlaneAuthorityError, match="DISABLED_REQUIRED"):
        prepare_historical_run_authority(runtime_root=root, recording_root=rec, adapter_status_path=adapter, date=DATE, candidate_ids=IDS, nonce="historical-20260814-b", expires_at="2026-08-22T00:30:00Z", expected_state_sha256=_sha(state), expected_authority=deployment_authority_binding(root), now=NOW, room_id=123, adapter_state_path=adapter_state, recorder_endpoint="http://test/graphql", recorder_env=tmp_path / "recorder.env")
    (root / "DISABLED").write_text("paused\n")
    path, document = prepare_historical_run_authority(runtime_root=root, recording_root=rec, adapter_status_path=adapter, date=DATE, candidate_ids=IDS, nonce="historical-20260814-b", expires_at="2026-08-22T00:30:00Z", expected_state_sha256=_sha(state), expected_authority=deployment_authority_binding(root), now=NOW, room_id=123, adapter_state_path=adapter_state, recorder_endpoint="http://test/graphql", recorder_env=tmp_path / "recorder.env")
    create_historical_run_authority(path, document)
    _write(rec / DATE / "extra.flv", b"drift", 0o644)
    with pytest.raises(HistoricalFastlaneAuthorityError, match="SOURCE_PREIMAGE_DRIFT"):
        load_and_start_historical_run(authority_path=path, runtime_root=root, recording_root=rec, adapter_status_path=adapter, adapter_state_path=adapter_state, recorder_endpoint="http://test/graphql", recorder_env=tmp_path / "recorder.env", now=NOW, room_id=123)


def test_historical_authority_refuses_bad_flv_and_active_adapter_before_any_receipt(tmp_path: Path):
    root, rec, adapter, adapter_state = _runtime(tmp_path)
    (root / "DISABLED").write_text("paused\n")
    state = (root / "state" / f"{DATE}.json").read_bytes()
    (rec / DATE / "123_20260814-11-30-25.mp4").unlink()
    _write(rec / DATE / "123_20260814-11-30-25.flv", b"raw", 0o644)
    with pytest.raises(HistoricalFastlaneAuthorityError, match="SOURCE_INVENTORY_BLOCKED"):
        prepare_historical_run_authority(runtime_root=root, recording_root=rec, adapter_status_path=adapter, date=DATE, candidate_ids=IDS, nonce="historical-20260814-c", expires_at="2026-08-22T00:30:00Z", expected_state_sha256=_sha(state), expected_authority=deployment_authority_binding(root), now=NOW, room_id=123, adapter_state_path=adapter_state, recorder_endpoint="http://test/graphql", recorder_env=tmp_path / "recorder.env")
    (rec / DATE / "123_20260814-11-30-25.flv").unlink()
    _write(rec / DATE / "123_20260814-11-30-25.mp4", b"mp4", 0o644)
    _write(adapter, json.dumps({"schema_version": "recorder-neutral-status.v1", "room_id": "123", "generated_at_epoch": NOW.timestamp(), "service_reachable": True, "live_status": 1, "streaming": True, "recording": True, "finalizing": False, "error": None}).encode())
    with pytest.raises(HistoricalFastlaneAuthorityError, match="ADAPTER_NOT_CLEAN_IDLE"):
        prepare_historical_run_authority(runtime_root=root, recording_root=rec, adapter_status_path=adapter, date=DATE, candidate_ids=IDS, nonce="historical-20260814-c", expires_at="2026-08-22T00:30:00Z", expected_state_sha256=_sha(state), expected_authority=deployment_authority_binding(root), now=NOW, room_id=123, adapter_state_path=adapter_state, recorder_endpoint="http://test/graphql", recorder_env=tmp_path / "recorder.env")


def test_historical_authority_refuses_empty_target_date_even_with_staging_directory(tmp_path: Path):
    root, rec, adapter, adapter_state = _runtime(tmp_path)
    (root / "DISABLED").write_text("paused\n")
    (rec / DATE / "123_20260814-11-30-25.mp4").unlink()
    (rec / DATE / ".brec-adapter-staging").mkdir()
    state = (root / "state" / f"{DATE}.json").read_bytes()
    with pytest.raises(HistoricalFastlaneAuthorityError, match="SOURCE_EMPTY"):
        prepare_historical_run_authority(
            runtime_root=root, recording_root=rec, adapter_status_path=adapter, date=DATE,
            candidate_ids=IDS, nonce="historical-empty-date", expires_at="2026-08-22T00:30:00Z",
            expected_state_sha256=_sha(state), expected_authority=deployment_authority_binding(root),
            now=NOW, room_id=123, adapter_state_path=adapter_state,
            recorder_endpoint="http://test/graphql", recorder_env=tmp_path / "recorder.env",
        )


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


def test_scope_renewal_can_replace_an_expired_v2_scope_but_runtime_cannot_consume_it(tmp_path: Path):
    root, rec, adapter, adapter_state = _runtime(tmp_path)
    state_path = root / "state" / f"{DATE}.json"
    state = json.loads(state_path.read_text())
    state["operator_processing_scope"]["expires_at"] = "2026-08-22T00:00:00Z"
    _write(state_path, state_bytes(state))
    prepared = prepare_scope_renewal(
        runtime_root=root, date=DATE, candidate_ids=IDS, new_grant_id="renewed-expired-v2",
        expires_at="2026-08-26T00:00:00Z", expected_state_sha256=_sha(state_path.read_bytes()),
        expected_authority=deployment_authority_binding(root), now=NOW,
    )
    (root / "DISABLED").write_text("paused\n")
    with pytest.raises(HistoricalFastlaneAuthorityError, match="SCOPE_EXPIRED"):
        prepare_historical_run_authority(
            runtime_root=root, recording_root=rec, adapter_status_path=adapter, date=DATE,
            candidate_ids=IDS, nonce="historical-expired-scope", expires_at="2026-08-22T00:30:00Z",
            expected_state_sha256=_sha(state_path.read_bytes()), expected_authority=deployment_authority_binding(root),
            now=NOW, room_id=123, adapter_state_path=adapter_state,
            recorder_endpoint="http://test/graphql", recorder_env=tmp_path / "recorder.env",
        )
    commit_scope_renewal(prepared, runtime_root=root)


def test_source_tree_accepts_other_dates_and_seals_nested_staging_without_payload(tmp_path: Path):
    root, rec, adapter, adapter_state = _runtime(tmp_path)
    (root / "DISABLED").write_text("paused\n")
    _write(rec / "2026-08-13" / "unrelated.mp4", b"other", 0o644)
    _write(rec / DATE / ".brec-adapter-staging" / "nested.json", b"nested", 0o600)
    state = (root / "state" / f"{DATE}.json").read_bytes()
    _path, document = prepare_historical_run_authority(
        runtime_root=root, recording_root=rec, adapter_status_path=adapter, date=DATE,
        candidate_ids=IDS, nonce="historical-multi-date", expires_at="2026-08-22T00:30:00Z",
        expected_state_sha256=_sha(state), expected_authority=deployment_authority_binding(root),
        now=NOW, room_id=123, adapter_state_path=adapter_state,
        recorder_endpoint="http://test/graphql", recorder_env=tmp_path / "recorder.env",
    )
    entries = document["source_binding"]["entries"]
    assert {row["relative_path"] for row in entries} >= {".", ".brec-adapter-staging", ".brec-adapter-staging/nested.json"}
    large = rec / DATE / "large.bin"
    _write(large, b"x" * (2 * 1024 * 1024), 0o600)
    fingerprint = historical_module._streaming_regular_fingerprint(large, label="source")
    assert fingerprint is not None and not hasattr(fingerprint, "payload")


def test_start_rechecks_fresh_adapter_and_direct_recorder_without_raw_heartbeat_equality(tmp_path: Path, monkeypatch):
    root, rec, adapter, adapter_state = _runtime(tmp_path)
    (root / "DISABLED").write_text("paused\n")
    state = (root / "state" / f"{DATE}.json").read_bytes()
    path, document = prepare_historical_run_authority(
        runtime_root=root, recording_root=rec, adapter_status_path=adapter, date=DATE,
        candidate_ids=IDS, nonce="historical-status-refresh", expires_at="2026-08-22T00:30:00Z",
        expected_state_sha256=_sha(state), expected_authority=deployment_authority_binding(root),
        now=NOW, room_id=123, adapter_state_path=adapter_state,
        recorder_endpoint="http://test/graphql", recorder_env=tmp_path / "recorder.env",
    )
    create_historical_run_authority(path, document)
    refreshed = json.loads(adapter.read_text())
    refreshed["generated_at_epoch"] = NOW.timestamp() + 1
    _write(adapter, json.dumps(refreshed).encode())
    started = load_and_start_historical_run(
        authority_path=path, runtime_root=root, recording_root=rec, adapter_status_path=adapter,
        adapter_state_path=adapter_state, recorder_endpoint="http://test/graphql",
        recorder_env=tmp_path / "recorder.env", now=NOW, room_id=123,
    )
    assert started["started_adapter_status_sha256"] != started["authorization_adapter_status_sha256"]
    finish_historical_run(authority_path=path, runtime_root=root)
    stale = dict(refreshed)
    stale["generated_at_epoch"] = NOW.timestamp() - 91
    _write(adapter, json.dumps(stale).encode())
    with pytest.raises(HistoricalFastlaneAuthorityError, match="ADAPTER_STATUS_STALE"):
        prepare_historical_run_authority(
            runtime_root=root, recording_root=rec, adapter_status_path=adapter, date=DATE,
            candidate_ids=IDS, nonce="historical-status-stale", expires_at="2026-08-22T00:30:00Z",
            expected_state_sha256=_sha(state), expected_authority=deployment_authority_binding(root),
            now=NOW, room_id=123, adapter_state_path=adapter_state,
            recorder_endpoint="http://test/graphql", recorder_env=tmp_path / "recorder.env",
        )


def test_direct_recorder_turning_live_after_source_hash_refuses_authority(tmp_path: Path, monkeypatch):
    root, rec, adapter, adapter_state = _runtime(tmp_path)
    (root / "DISABLED").write_text("paused\n")
    state = (root / "state" / f"{DATE}.json").read_bytes()
    source_complete = {"value": False}
    original = historical_module._source_binding

    def bind_then_mark_live(*args, **kwargs):
        result = original(*args, **kwargs)
        source_complete["value"] = True
        return result

    monkeypatch.setattr(historical_module, "_source_binding", bind_then_mark_live)
    monkeypatch.setattr(
        historical_module,
        "query_room_status",
        lambda *_args, **_kwargs: {
            "streaming": source_complete["value"], "recording": source_complete["value"],
        },
    )
    with pytest.raises(HistoricalFastlaneAuthorityError, match="DIRECT_RECORDER_NOT_IDLE"):
        prepare_historical_run_authority(
            runtime_root=root, recording_root=rec, adapter_status_path=adapter, date=DATE,
            candidate_ids=IDS, nonce="historical-live-after-hash", expires_at="2026-08-22T00:30:00Z",
            expected_state_sha256=_sha(state), expected_authority=deployment_authority_binding(root),
            now=NOW, room_id=123, adapter_state_path=adapter_state,
            recorder_endpoint="http://test/graphql", recorder_env=tmp_path / "recorder.env",
        )


@pytest.mark.parametrize("direct", [{"streaming": True, "recording": False}, {"streaming": None, "recording": None}])
def test_start_refuses_direct_live_or_unknown_after_authorization(tmp_path: Path, monkeypatch, direct):
    root, rec, adapter, adapter_state = _runtime(tmp_path)
    (root / "DISABLED").write_text("paused\n")
    state = (root / "state" / f"{DATE}.json").read_bytes()
    path, document = prepare_historical_run_authority(
        runtime_root=root, recording_root=rec, adapter_status_path=adapter, date=DATE,
        candidate_ids=IDS, nonce="historical-start-direct-recheck", expires_at="2026-08-22T00:30:00Z",
        expected_state_sha256=_sha(state), expected_authority=deployment_authority_binding(root),
        now=NOW, room_id=123, adapter_state_path=adapter_state,
        recorder_endpoint="http://test/graphql", recorder_env=tmp_path / "recorder.env",
    )
    create_historical_run_authority(path, document)
    monkeypatch.setattr(historical_module, "query_room_status", lambda *_args, **_kwargs: direct)
    with pytest.raises(HistoricalFastlaneAuthorityError, match="DIRECT_RECORDER_NOT_IDLE"):
        load_and_start_historical_run(
            authority_path=path, runtime_root=root, recording_root=rec, adapter_status_path=adapter,
            adapter_state_path=adapter_state, recorder_endpoint="http://test/graphql",
            recorder_env=tmp_path / "recorder.env", now=NOW, room_id=123,
        )


def test_authority_refuses_today_before_any_state_or_source_probe(tmp_path: Path):
    root, rec, adapter, adapter_state = _runtime(tmp_path)
    (root / "DISABLED").write_text("paused\n")
    with pytest.raises(HistoricalFastlaneAuthorityError, match="DATE_NOT_CLOSED"):
        prepare_historical_run_authority(
            runtime_root=root, recording_root=rec, adapter_status_path=adapter, date="2026-08-22",
            candidate_ids=IDS, nonce="historical-today-refused", expires_at="2026-08-22T00:30:00Z",
            expected_state_sha256="sha256:" + "0" * 64, expected_authority=deployment_authority_binding(root),
            now=NOW, room_id=123, adapter_state_path=adapter_state,
            recorder_endpoint="http://test/graphql", recorder_env=tmp_path / "recorder.env",
        )


def test_long_source_validation_rechecks_expiry_before_prepared_receipt(tmp_path: Path):
    root, rec, adapter, adapter_state = _runtime(tmp_path)
    (root / "DISABLED").write_text("paused\n")
    state = (root / "state" / f"{DATE}.json").read_bytes()
    with pytest.raises(HistoricalFastlaneAuthorityError, match="RUN_EXPIRED"):
        prepare_historical_run_authority(
            runtime_root=root, recording_root=rec, adapter_status_path=adapter, date=DATE,
            candidate_ids=IDS, nonce="historical-expired-during-hash", expires_at="2026-08-22T00:00:30Z",
            expected_state_sha256=_sha(state), expected_authority=deployment_authority_binding(root),
            now=NOW, clock=lambda: NOW.replace(second=31), room_id=123,
            adapter_state_path=adapter_state, recorder_endpoint="http://test/graphql",
            recorder_env=tmp_path / "recorder.env",
        )


@pytest.mark.parametrize("live", [True, None])
def test_historical_tick_keeps_live_and_unknown_holds(monkeypatch, live):
    called: list[str] = []
    monkeypatch.setattr(runner, "cjk_font_present", lambda: True)
    monkeypatch.setattr(runner, "source_health_error", lambda: None)
    monkeypatch.setattr(runner, "recorder_live_status", lambda: live)
    monkeypatch.setattr(runner, "live_determination_basis", lambda _live: {})
    monkeypatch.setattr(runner, "write_alert", lambda *_args: None)
    monkeypatch.setattr(runner, "write_heartbeat", lambda *_args: None)
    monkeypatch.setattr(runner, "log", lambda *_args: None)
    monkeypatch.setattr(runner, "list_dates", lambda: [DATE])
    monkeypatch.setattr(runner, "process_date", lambda date: called.append(date))
    assert runner.tick(historical_date=DATE) == 0
    assert called == []


def test_historical_tick_live_false_reaches_mid_tick_recheck(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(runner, "cjk_font_present", lambda: True)
    monkeypatch.setattr(runner, "source_health_error", lambda: None)
    monkeypatch.setattr(runner, "recorder_live_status", lambda: False)
    monkeypatch.setattr(runner, "live_determination_basis", lambda _live: {})
    monkeypatch.setattr(runner, "live_hold_recheck", lambda **_kwargs: False)
    monkeypatch.setattr(runner, "write_heartbeat", lambda *_args: None)
    monkeypatch.setattr(runner, "log", lambda *_args: None)
    monkeypatch.setattr(runner, "read_state", lambda _date: {})
    monkeypatch.setattr(runner, "process_date", lambda date: called.append(date))
    assert runner.tick(historical_date=DATE) == 0
    assert called == [DATE]
