from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops" / "recording" / "record_health_audit.py"
NOW = 1_788_393_600.0  # T00:00:00Z
RELATIVE = "2026-09-02/22966160_20260902-23-59-00.flv"


def _write_ledgers(
    tmp_path: Path,
    *,
    generated: float = NOW,
    state_epoch: float = NOW,
    recording: bool | None = False,
    webhook: dict[str, object] | None = None,
    finalized: dict[str, object] | None = None,
    dispositions: dict[str, object] | None = None,
) -> tuple[Path, Path]:
    status = tmp_path / "status.json"
    state = tmp_path / "adapter-state.json"
    status.write_text(
        json.dumps(
            {
                "schema_version": "recorder-neutral-status.v1",
                "room_id": "22966160",
                "generated_at_epoch": generated,
                "service_reachable": True,
                "streaming": recording,
                "recording": recording,
                "error": None,
                "finalize_errors": [],
            }
        ),
        encoding="utf-8",
    )
    state.write_text(
        json.dumps(
            {
                "schema_version": "bililive-recorder-adapter-state.v1",
                "last_room_status_epoch": state_epoch,
                "webhook_files": webhook or {},
                "finalized": finalized or {},
                "source_dispositions": dispositions or {},
                "source_disposition_identity_rebinds": {},
                "source_disposition_identity_rebind_tasks": {},
            }
        ),
        encoding="utf-8",
    )
    return status, state


def _run(status: Path, state: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--lookback-hours",
            "96",
            "--status-path",
            str(status),
            "--state-path",
            str(state),
            "--now-epoch",
            str(NOW),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_health_audit_happy_and_emits_one_stable_json_line(tmp_path: Path) -> None:
    status, state = _write_ledgers(
        tmp_path,
        recording=True,
        webhook={RELATIVE: {"status": "OPEN", "session_id": "active-session"}},
    )

    result = _run(status, state)

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.count("\n") == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "PASS"
    assert payload["healthy"] is True
    assert payload["inventory"]["open"] == [RELATIVE]
    assert payload["adapter_state_age_seconds"] == 0


def test_health_audit_fails_stale_status_or_adapter_state(tmp_path: Path) -> None:
    status, state = _write_ledgers(tmp_path, generated=NOW - 181)
    result = _run(status, state)
    assert result.returncode == 1
    assert "status stale: 181s" in json.loads(result.stdout)["unresolved"]

    status, state = _write_ledgers(tmp_path, state_epoch=NOW - 181)
    result = _run(status, state)
    assert result.returncode == 1
    assert "adapter-state stale: 181s" in json.loads(result.stdout)["unresolved"]


def test_health_audit_fails_unresolved_closed_and_open_recording_mismatch(
    tmp_path: Path,
) -> None:
    status, state = _write_ledgers(
        tmp_path,
        webhook={RELATIVE: {"status": "CLOSED"}},
    )
    result = _run(status, state)
    payload = json.loads(result.stdout)
    assert result.returncode == 1
    assert "CLOSED source lacks finalized or disposition binding" in " ".join(
        payload["unresolved"]
    )

    status, state = _write_ledgers(
        tmp_path,
        webhook={RELATIVE: {"status": "OPEN"}},
    )
    result = _run(status, state)
    payload = json.loads(result.stdout)
    assert result.returncode == 1
    assert "OPEN source is not currently recording" in " ".join(payload["unresolved"])

    second = "2026-09-02/22966160_20260902-23-58-00.flv"
    status, state = _write_ledgers(
        tmp_path,
        recording=True,
        webhook={RELATIVE: {"status": "OPEN"}, second: {"status": "OPEN"}},
    )
    result = _run(status, state)
    assert result.returncode == 1
    assert "active OPEN source count is 2" in " ".join(
        json.loads(result.stdout)["unresolved"]
    )


def test_health_audit_accepts_typed_connection_stub_disposition(tmp_path: Path) -> None:
    status, state = _write_ledgers(
        tmp_path,
        webhook={RELATIVE: {"status": "CLOSED"}},
        dispositions={
            RELATIVE: {
                "schema_version": "recording-connection-stub.v1",
                "status": "IGNORED_CONNECTION_STUB",
                "reason_code": "RECORDER_CONNECTION_STUB_NO_DECODABLE_VIDEO",
            }
        },
    )

    result = _run(status, state)

    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "PASS"


def test_health_audit_allows_only_same_session_closed_pending_while_recording(
    tmp_path: Path,
) -> None:
    open_relative = "2026-09-02/22966160_20260902-23-58-00.flv"
    session_id = "active-session"
    status, state = _write_ledgers(
        tmp_path,
        recording=True,
        webhook={
            RELATIVE: {"status": "CLOSED", "session_id": session_id},
            open_relative: {"status": "OPEN", "session_id": session_id},
        },
    )

    result = _run(status, state)

    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload["inventory"]["pending_active_session"] == [RELATIVE]
    assert payload["unresolved"] == []

    status, state = _write_ledgers(
        tmp_path,
        recording=True,
        webhook={
            RELATIVE: {"status": "CLOSED", "session_id": "different-session"},
            open_relative: {"status": "OPEN", "session_id": session_id},
        },
    )

    result = _run(status, state)

    payload = json.loads(result.stdout)
    assert result.returncode == 1
    assert RELATIVE in payload["inventory"]["closed_unresolved"]
    assert any("does not match active OPEN session" in item for item in payload["unresolved"])


def test_health_audit_uses_action_for_truncated_disposition(tmp_path: Path) -> None:
    status, state = _write_ledgers(
        tmp_path,
        webhook={RELATIVE: {"status": "CLOSED"}},
        dispositions={
            RELATIVE: {
                "schema_version": "recording-truncated-source-disposition.v1",
                "action": "IGNORED_TRUNCATED_RECONNECT_FRAGMENT",
            }
        },
    )

    result = _run(status, state)

    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "PASS"
