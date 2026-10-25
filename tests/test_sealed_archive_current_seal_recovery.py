from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops.recording import bililive_recorder_adapter as adapter
from ops.recording import current_seal_recovery
from ops.recording.sealed_archive_intake import (
    SealedArchiveIntakeError,
    _validate_receipt_row,
    intake_sealed_archive,
)


ROOM_ID = "22966160"
DATE = "2026-10-03"
FILENAME = "22966160_20261003-22-39-34.flv"
RELATIVE = f"{DATE}/{FILENAME}"
XML_START = "2026-10-03T22:39:34+08:00"
STAT = {
    "size_bytes": 11,
    "mtime_ns": 100,
    "ctime_ns": 100,
    "inode": 7,
    "device": 8,
    "mode": 0,
}
PROBE_SUMMARY = {
    "proc_dirs": 1,
    "fd_dirs": 1,
    "fd_entries": 0,
    "readlink_ok": 0,
    "readlink_fail": 0,
    "matched": 0,
    "nonfinal": 0,
    "proc_missing": 0,
    "fd_dir_errors": 0,
    "fd_snapshot_changes": 0,
    "proc_snapshot_changed": 0,
    "complete": 1,
}


def _write_json(path: Path, value: object) -> str:
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _bundle(tmp_path: Path) -> dict[str, object]:
    source_dir = tmp_path / "115open" / "李豆沙录播" / ROOM_ID / DATE
    source_dir.mkdir(parents=True)
    source = source_dir / FILENAME
    xml = source.with_suffix(".xml")
    source.write_bytes(b"flv-current")
    xml.write_text("<i />", encoding="utf-8")
    source_sha = adapter.sha256_file(source)
    xml_sha = adapter.sha256_file(xml)
    source_size = source.stat().st_size
    xml_size = xml.stat().st_size
    row = {
        "status": "fuse_copy_complete",
        "attempts": 1,
        "source": str(source),
        "xml": str(xml),
        "target_flv": str(tmp_path / "115open" / "李豆沙录播" / ROOM_ID / DATE / FILENAME),
        "target_xml": str(tmp_path / "115open" / "李豆沙录播" / ROOM_ID / DATE / xml.name),
        "source_size_bytes": source_size,
        "source_sha256": source_sha,
        "xml_sha256": xml_sha,
        "flv_copy": {
            "status": "copied",
            "target_size_bytes": source_size,
            "target_sha256": source_sha,
        },
        "xml_copy": {
            "status": "copied",
            "target_size_bytes": xml_size,
            "target_sha256": xml_sha,
        },
        "media_verification": {
            "ffprobe_rc": 0,
            "full_av_decode_rc": 0,
            "video_stream_count": 1,
            "valid_video_geometry": True,
            "ffprobe_stderr_present": False,
            "decode_stderr_present": False,
        },
        "xml_parse": {
            "root_tag": "i",
            "event_counts": {"d": 1, "gift": 0, "guard": 0, "sc": 0},
        },
        "stable_pair": {
            phase: {
                "flv": {"size_bytes": source_size},
                "xml": {"size_bytes": xml_size},
            }
            for phase in ("before", "after")
        },
    }
    receipt = {
        "state_source": "unique existing native archive worker",
        "state_path": "/home/维护者/.local/share/vtuber-slice/115-archive-switch-20261002/archive-state.json",
        "rows": {RELATIVE: row},
    }
    receipt_path = tmp_path / "native-receipt.json"
    receipt_sha = _write_json(receipt_path, receipt)
    phases = {}
    phase_offsets = (0, 11, 12, 13, 14)
    for offset, phase in zip(phase_offsets, current_seal_recovery.CURRENT_OPEN_PHASES):
        phases[phase] = {
            "phase": phase,
            "source_relative": RELATIVE,
            "observed_at_utc": f"2026-10-04T22:00:{offset:02d}+00:00",
            "probe": {
                "schema": "container-open-fd-probe-v2",
                "summary": dict(PROBE_SUMMARY),
                "matched_records": 0,
                "open_relatives": [],
            },
        }
    recovery = {
        "schema": "sealed-archive-current-seal-recovery.v1",
        "authority": "N100_115_ARCHIVE_STATE_CURRENT_SEAL_RECOVERY",
        "evidence_scope": "current_seal_recovery",
        "historical_receipt_sha256": receipt_sha,
        "room_id": ROOM_ID,
        "date": DATE,
        "source_relative": RELATIVE,
        "admission_mode": "idle",
        "native_source": {
            "path": row["source"],
            "xml_path": row["xml"],
            "state_path": receipt["state_path"],
            "state_sha256": "a" * 64,
            "worker_path": "/home/维护者/.local/share/vtuber-slice/115-archive-switch-20261002/archive_115_worker.py",
            "worker_sha256": "b" * 64,
        },
        "historical_row": {
            "row_sha256": current_seal_recovery.canonical_json_sha256(row),
            "status": row["status"],
            "attempts": row["attempts"],
            "source_size_bytes": source_size,
            "source_sha256": source_sha,
            "xml_sha256": xml_sha,
        },
        "current_stability": {
            "wait_seconds": 10.0,
            "before": {"flv": {**STAT, "size_bytes": source_size}, "xml": {**STAT, "size_bytes": xml_size}},
            "after": {"flv": {**STAT, "size_bytes": source_size}, "xml": {**STAT, "size_bytes": xml_size}},
        },
        "current_hash": {
            "observed_at_utc": "2026-10-04T22:00:10+00:00",
            "before": {"flv": {**STAT, "size_bytes": source_size}, "xml": {**STAT, "size_bytes": xml_size}},
            "after": {"flv": {**STAT, "size_bytes": source_size}, "xml": {**STAT, "size_bytes": xml_size}},
            "flv_sha256": source_sha,
            "xml_sha256": xml_sha,
            "flv_size_bytes": source_size,
            "xml_size_bytes": xml_size,
        },
        "current_xml_parse": {
            "observed_at_utc": "2026-10-04T22:00:10+00:00",
            "root_tag": "i",
            "event_counts": {"d": 1, "gift": 0, "guard": 0, "sc": 0},
        },
        "current_open_exclusions": phases,
    }
    recovery_path = tmp_path / "current-recovery.json"
    recovery_sha = _write_json(recovery_path, recovery)
    return {
        "source": source,
        "xml": xml,
        "receipt": receipt,
        "receipt_path": receipt_path,
        "receipt_sha": receipt_sha,
        "recovery": recovery,
        "recovery_path": recovery_path,
        "recovery_sha": recovery_sha,
        "row": row,
        "relative": RELATIVE,
    }


def _validate(bundle: dict[str, object]) -> dict[str, object]:
    return current_seal_recovery.validate_current_seal_recovery(
        bundle["recovery"],
        receipt=bundle["receipt"],
        receipt_sha256=bundle["receipt_sha"],
        room_id=ROOM_ID,
        date=DATE,
        relative=RELATIVE,
        row=bundle["row"],
        source_flv=bundle["source"],
        source_xml=bundle["xml"],
    )


def test_current_recovery_accepts_complete_current_proof(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    validated = _validate(bundle)
    assert validated["schema"] == "sealed-archive-current-seal-recovery.v1"
    assert set(validated["current_open_exclusions"]) == set(
        current_seal_recovery.CURRENT_OPEN_PHASES
    )


@pytest.mark.parametrize("value", [1.0, "1"])
def test_current_recovery_rejects_non_integer_probe_counters(
    tmp_path: Path, value: object
) -> None:
    bundle = _bundle(tmp_path)
    recovery = deepcopy(bundle["recovery"])
    recovery["current_open_exclusions"]["current_before_hash"]["probe"]["summary"][
        "complete"
    ] = value
    with pytest.raises(current_seal_recovery.CurrentSealRecoveryError, match="must be an integer"):
        _validate({**bundle, "recovery": recovery})


def test_current_recovery_rejects_nonfinite_wait_seconds(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    recovery = deepcopy(bundle["recovery"])
    recovery["current_stability"]["wait_seconds"] = float("inf")
    with pytest.raises(current_seal_recovery.CurrentSealRecoveryError, match="wait_seconds is invalid"):
        _validate({**bundle, "recovery": recovery})


def test_current_recovery_uses_observed_stability_phase_window(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    recovery = deepcopy(bundle["recovery"])
    recovery["current_open_exclusions"]["current_after_stability"][
        "observed_at_utc"
    ] = "2026-10-04T22:00:09+00:00"
    with pytest.raises(
        current_seal_recovery.CurrentSealRecoveryError,
        match="current stability phase window is shorter than 10 seconds",
    ):
        _validate({**bundle, "recovery": recovery})


def _write_reader_proof(tmp_path: Path) -> tuple[Path, str, bytes]:
    path = tmp_path / "current-recovery.json"
    raw = b'{"schema":"sealed-archive-current-seal-recovery.v1"}\n'
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest(), raw


def test_current_recovery_reader_accepts_read_only_atime_change(
    tmp_path: Path, monkeypatch
) -> None:
    path, expected_sha, raw = _write_reader_proof(tmp_path)
    original_stat = Path.stat
    stat_calls = 0

    class AtimeChangedStat:
        def __init__(self, original):
            self._original = original

        def __getattr__(self, name):
            return getattr(self._original, name)

        @property
        def st_atime_ns(self):
            return self._original.st_atime_ns + 1

        @property
        def st_atime(self):
            return self._original.st_atime + 1e-9

    def fake_stat(self, *args, **kwargs):
        nonlocal stat_calls
        result = original_stat(self, *args, **kwargs)
        if self == path:
            stat_calls += 1
            if stat_calls >= 2:
                return AtimeChangedStat(result)
        return result

    monkeypatch.setattr(Path, "stat", fake_stat)
    payload, actual_sha = current_seal_recovery.read_current_seal_recovery(
        path, expected_sha256=expected_sha
    )
    assert payload["schema"] == "sealed-archive-current-seal-recovery.v1"
    assert actual_sha == expected_sha
    assert raw == path.read_bytes()


@pytest.mark.parametrize("field", ["st_ino", "st_mtime_ns"])
def test_current_recovery_reader_rejects_identity_or_write_timestamp_drift(
    tmp_path: Path, monkeypatch, field: str
) -> None:
    path, expected_sha, _ = _write_reader_proof(tmp_path)
    original_stat = Path.stat
    stat_calls = 0

    def fake_stat(self, *args, **kwargs):
        nonlocal stat_calls
        result = original_stat(self, *args, **kwargs)
        if self == path:
            stat_calls += 1
            if stat_calls >= 2:
                values = {name: getattr(result, name) for name in (
                    "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode"
                )}
                values[field] += 1
                return SimpleNamespace(
                    **values,
                    st_atime_ns=result.st_atime_ns,
                    st_atime=result.st_atime,
                )
        return result

    monkeypatch.setattr(Path, "stat", fake_stat)
    with pytest.raises(
        current_seal_recovery.CurrentSealRecoveryError,
        match="current recovery changed or SHA-256 mismatched",
    ):
        current_seal_recovery.read_current_seal_recovery(
            path, expected_sha256=expected_sha
        )


def test_current_recovery_reader_rejects_content_drift(tmp_path: Path, monkeypatch) -> None:
    path, expected_sha, raw = _write_reader_proof(tmp_path)
    original_read_bytes = Path.read_bytes

    def fake_read_bytes(self):
        value = original_read_bytes(self)
        if self == path:
            return value + b"drift"
        return value

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)
    with pytest.raises(
        current_seal_recovery.CurrentSealRecoveryError,
        match="current recovery changed or SHA-256 mismatched",
    ):
        current_seal_recovery.read_current_seal_recovery(
            path, expected_sha256=hashlib.sha256(raw).hexdigest()
        )


def test_default_intake_contract_still_rejects_missing_historical_fields(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    with pytest.raises(SealedArchiveIntakeError, match="receipt row open_exclusion"):
        _validate_receipt_row(
            bundle["receipt"],
            room_id=ROOM_ID,
            date=DATE,
            source_flv=bundle["source"],
            source_xml=bundle["xml"],
        )


@pytest.mark.parametrize(
    ("label", "mutate", "message"),
    [
        (
            "incomplete_probe",
            lambda d: d["current_open_exclusions"]["current_before_hash"]["probe"]["summary"].update(complete=0),
            "enumeration is incomplete",
        ),
        (
            "target_open",
            lambda d: (
                d["current_open_exclusions"]["current_before_hash"]["probe"].update(
                    open_relatives=["2026-10-03/22966160_20261003-22-39-34"],
                    matched_records=1,
                ),
                d["current_open_exclusions"]["current_before_hash"]["probe"]["summary"].update(
                    matched=1
                ),
            ),
            "still lists the target",
        ),
        (
            "stat_drift",
            lambda d: d["current_hash"]["after"]["flv"].update(mtime_ns=101),
            "changed during current hash window",
        ),
        (
            "receipt_drift",
            lambda d: d.update(historical_receipt_sha256="c" * 64),
            "bound to another receipt",
        ),
    ],
)
def test_current_recovery_rejects_material_drift(
    tmp_path: Path, label: str, mutate, message: str
) -> None:
    del label
    bundle = _bundle(tmp_path)
    recovery = deepcopy(bundle["recovery"])
    mutate(recovery)
    bundle["recovery"] = recovery
    with pytest.raises(current_seal_recovery.CurrentSealRecoveryError, match=message):
        _validate(bundle)


def test_current_recovery_binds_finalized_ledger_and_result(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle(tmp_path)
    state_path = tmp_path / "adapter-state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "bililive-recorder-adapter-state.v1",
                "managed_since_epoch": 0,
                "finalized": {},
                "source_dispositions": {},
                "source_disposition_identity_rebinds": {},
                "source_disposition_identity_rebind_tasks": {},
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "oci3-local" / DATE
    def fake_xml_to_jsonl(path: Path):
        del path
        return (
            b'{"cmd":"DANMU_MSG"}\n',
            {"roomid": ROOM_ID, "start_time": XML_START},
            1,
        )

    def fake_finalize(source: Path, *, existing_ledger, output_dir: Path, **kwargs):
        del source, existing_ledger, kwargs
        target = output_dir / FILENAME.replace(".flv", ".mp4")
        target.write_bytes(b"finalized-mp4")
        return {"status": "finalized", "target_sha256": adapter.sha256_file(target)}

    monkeypatch.setattr(adapter, "xml_to_jsonl", fake_xml_to_jsonl)
    monkeypatch.setattr(adapter, "finalize_recording", fake_finalize)
    # The intake now probes the piece duration for the source-selection rule
    # (OCI3 self-recording priority) before writing outputs; fake bytes here.
    monkeypatch.setattr(adapter, "probe_media", lambda *_a, **_k: {"duration_seconds": 1800.0})
    result = intake_sealed_archive(
        bundle["receipt_path"],
        expected_receipt_sha256=bundle["receipt_sha"],
        room_id=ROOM_ID,
        date=DATE,
        source_flv=bundle["source"],
        source_xml=bundle["xml"],
        output_dir=output_dir,
        state_path=state_path,
        current_seal_recovery_path=bundle["recovery_path"],
        expected_current_seal_recovery_sha256=bundle["recovery_sha"],
    )
    assert result["current_seal_recovery_path"] == str(bundle["recovery_path"].absolute())
    assert result["current_seal_recovery_sha256"] == bundle["recovery_sha"]
    ledger = json.loads(state_path.read_text(encoding="utf-8"))["finalized"][RELATIVE]
    assert ledger["current_seal_recovery_path"] == str(bundle["recovery_path"].absolute())
    assert ledger["current_seal_recovery_sha256"] == bundle["recovery_sha"]
    before = state_path.read_bytes()
    second = intake_sealed_archive(
        bundle["receipt_path"],
        expected_receipt_sha256=bundle["receipt_sha"],
        room_id=ROOM_ID,
        date=DATE,
        source_flv=bundle["source"],
        source_xml=bundle["xml"],
        output_dir=output_dir,
        state_path=state_path,
        current_seal_recovery_path=bundle["recovery_path"],
        expected_current_seal_recovery_sha256=bundle["recovery_sha"],
    )
    assert second["state_updated"] is False
    assert state_path.read_bytes() == before
