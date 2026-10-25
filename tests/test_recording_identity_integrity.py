"""OCI3 regression coverage; compile locally, execute only in server acceptance."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ops.recording import bililive_recorder_adapter as adapter
from ops.recording import recording_archive as identity
from src.autoslice import source_integrity
from tests.test_recording_spool_mover import RELATIVE, recording  # noqa: F401 (fixture)
from tests.test_source_integrity import (
    _typed_connection_stub,
    _typed_truncated_ignored,
    _typed_truncated_recovered,
)


def _local_first_ledger(state_path: Path, tmp_path: Path) -> Path:
    current = tmp_path / "oci3-local-first" / "adapter-state.json"
    identity.atomic_json(current, json.loads(state_path.read_text()))
    return current


def _mutate(path: Path, kind: str, monkeypatch) -> None:
    before = path.stat()
    if kind == "same_size_restored_mtime":
        archive_stat = identity.fingerprint(path)
        evidence_stat = source_integrity._regular_file_fingerprint(path)
        adapter_stat = adapter._regular_file_fingerprint(path)
        original = path.read_bytes()
        path.write_bytes(bytes(value ^ 1 for value in original))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        # Model a coarse/stale stat response explicitly. Neither the test nor
        # the production decision may depend on a lucky ctime tick.
        archive_fingerprint = identity.fingerprint
        evidence_fingerprint = source_integrity._regular_file_fingerprint
        adapter_fingerprint = adapter._regular_file_fingerprint
        monkeypatch.setattr(identity, "fingerprint", lambda target: (
            archive_stat if target == path else archive_fingerprint(target)
        ))
        monkeypatch.setattr(source_integrity, "_regular_file_fingerprint", lambda target: (
            evidence_stat if target == path else evidence_fingerprint(target)
        ))
        monkeypatch.setattr(adapter, "_regular_file_fingerprint", lambda target: (
            adapter_stat if target == path else adapter_fingerprint(target)
        ))
    elif kind == "mtime_rollback":
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns - 1_000_000_000))
        assert path.stat().st_mtime_ns < before.st_mtime_ns
    elif kind == "inode_replace":
        replacement = path.with_name(path.name + ".replacement")
        replacement.write_bytes(path.read_bytes())
        os.chmod(replacement, before.st_mode & 0o777)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, path)
        assert path.stat().st_ino != before.st_ino
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize("ledger", ["legacy", "local_first"])
@pytest.mark.parametrize("role", ["source", "xml", "successor_mp4"])
def test_inventory_rehashes_disposition_evidence_with_unchanged_stat(
    tmp_path, monkeypatch, ledger, role
):
    date, stub, successor_mp4, state_path = _typed_connection_stub(tmp_path, monkeypatch)
    if ledger == "local_first":
        state_path = _local_first_ledger(state_path, tmp_path)
    paths = {"source": stub, "xml": stub.with_suffix(".xml"),
             "successor_mp4": successor_mp4}
    # Prime the ordinary reader before rewriting; a stat-keyed digest cache
    # would otherwise make this important control pass only on the first read.
    assert source_integrity.audit_finalized_recording_inventory(
        date, room_id="123456", adapter_state_path=state_path
    )["status"] == "PASS"
    _mutate(paths[role], "same_size_restored_mtime", monkeypatch)
    audit = source_integrity.audit_finalized_recording_inventory(
        date, room_id="123456", adapter_state_path=state_path
    )
    assert audit["status"] == "BLOCKED"
    assert audit["can_select"] is False
    assert f"{role} SHA-256 drifted" in audit["issues"][0]["source_disposition_error"]


@pytest.mark.parametrize("role", ["source", "xml", "successor_source", "successor_mp4"])
@pytest.mark.parametrize("field", ["size_bytes", "mtime_ns", "ctime_ns", "device", "inode", "mode"])
def test_inventory_preserves_each_unreceipted_disposition_fingerprint_guard(
    tmp_path, monkeypatch, role, field
):
    date, stub, successor_mp4, old_state = _typed_connection_stub(tmp_path, monkeypatch)
    state_path = _local_first_ledger(old_state, tmp_path)
    target = {"source": stub, "xml": stub.with_suffix(".xml"),
              "successor_source": successor_mp4.with_suffix(".flv"),
              "successor_mp4": successor_mp4}[role]
    original_fingerprint = source_integrity._regular_file_fingerprint

    def drifted(path):
        current = original_fingerprint(path)
        if path == target:
            current[field] += -1 if field == "mtime_ns" else 1
        return current

    monkeypatch.setattr(source_integrity, "_regular_file_fingerprint", drifted)
    audit = source_integrity.audit_finalized_recording_inventory(
        date, room_id="123456", adapter_state_path=state_path
    )
    assert audit["status"] == "BLOCKED"
    assert audit["can_select"] is False
    assert "drifted" in audit["issues"][0]["source_disposition_error"]


@pytest.mark.parametrize("builder", [_typed_truncated_recovered, _typed_truncated_ignored])
@pytest.mark.parametrize("ledger", ["legacy", "local_first"])
@pytest.mark.parametrize("suffix", [".flv", ".xml"])
@pytest.mark.parametrize("kind", ["same_size_restored_mtime", "mtime_rollback", "inode_replace"])
def test_inventory_preserves_truncated_source_identity_guards(
    tmp_path, monkeypatch, builder, ledger, suffix, kind
):
    built = builder(tmp_path)
    date, source, state_path = built[0], built[1], built[-1]
    if ledger == "local_first":
        state_path = _local_first_ledger(state_path, tmp_path)
    assert source_integrity.audit_finalized_recording_inventory(
        date, room_id="123456", adapter_state_path=state_path
    )["status"] == "PASS"
    _mutate(source.with_suffix(suffix), kind, monkeypatch)
    audit = source_integrity.audit_finalized_recording_inventory(
        date, room_id="123456", adapter_state_path=state_path
    )
    assert audit["status"] == "BLOCKED"
    assert audit["issues"][0]["code"] == "TRUNCATED_SOURCE_DISPOSITION_DRIFT"
    assert "drifted" in audit["issues"][0]["message"]


@pytest.mark.parametrize("suffix", [".mp4", ".jsonl", ".meta.json"])
def test_inventory_rehashes_truncated_outputs_with_unchanged_stat(tmp_path, monkeypatch, suffix):
    date, source, _mp4, old_state = _typed_truncated_recovered(tmp_path)
    state_path = _local_first_ledger(old_state, tmp_path)
    _mutate(source.with_suffix(suffix), "same_size_restored_mtime", monkeypatch)
    audit = source_integrity.audit_finalized_recording_inventory(
        date, room_id="123456", adapter_state_path=state_path
    )
    assert audit["status"] == "BLOCKED"
    assert "SHA-256 drifted" in audit["issues"][0]["message"]


@pytest.fixture
def local_first_disposition(recording, tmp_path, monkeypatch):
    mover, client, state = recording
    successor = mover.spool / RELATIVE
    stub = successor.parent / f"{identity.ROOM}_20261024-19-59-57.flv"
    stub.write_bytes(b"stub")
    stub.with_suffix(".xml").write_text(
        '<i><BililiveRecorder version="2.18.0"/>'
        f'<BililiveRecorderRecordInfo roomid="{identity.ROOM}" name="host" '
        'title="test" start_time="2026-10-24T19:59:57+08:00"/></i>'
    )
    relative = f"{stub.parent.name}/{stub.name}"
    state["webhook_files"][relative] = {
        "status": "CLOSED", "session_id": "local-first-session",
        "opening_event_id": "stub-open", "closing_event_id": "stub-close",
        "file_open_time": "2026-10-24T19:59:57+08:00",
        "file_close_time": "2026-10-24T19:59:58+08:00",
        "file_size": stub.stat().st_size, "duration": 1.0,
    }
    state["webhook_files"][RELATIVE].update({
        "session_id": "local-first-session", "opening_event_id": "successor-open",
        "closing_event_id": "successor-close",
        "file_open_time": "2026-10-24T20:00:00+08:00",
        "file_close_time": "2026-10-24T20:30:00+08:00", "duration": 1800.0,
    })
    state["finalized"][RELATIVE]["target"] = str(successor.with_suffix(".mp4"))
    monkeypatch.setattr(adapter, "probe_connection_stub_video", lambda *args, **kwargs: {
        "duration_seconds": 1.0, "size_bytes": stub.stat().st_size,
        "video_codec": "h264", "width": 0, "height": 0,
        "decoded_video_frames": 0, "video_packets": 0,
    })
    monkeypatch.setattr(adapter, "probe_media", lambda *args, **kwargs: {
        "duration_seconds": 1800.0, "size_bytes": successor.with_suffix(".mp4").stat().st_size,
        "video_codec": "h264", "audio_codec": "aac", "width": 1920, "height": 1080,
    })
    row = adapter.build_connection_stub_disposition(
        stub, record_root=mover.spool, webhook_files=state["webhook_files"],
        finalized=state["finalized"],
    )
    assert row is not None
    state["source_dispositions"][relative] = row
    mover.state_path = tmp_path / "oci3-local-first" / "adapter-state.json"
    mover.archive_path = mover.state_path.with_name("archive-state.json")
    mover.events = mover.state_path.with_name("archive-tombstones.jsonl")
    identity.atomic_json(mover.state_path, state)
    monkeypatch.setattr(identity, "SPOOL_ROOT", mover.spool.parent)
    monkeypatch.setattr(identity, "ARCHIVE_ROOT", mover.archive_root)
    monkeypatch.setattr(identity, "CLOUD_MOUNT", mover.mount)
    monkeypatch.setattr(identity, "STATE_DIR", mover.state_path.parent)
    monkeypatch.setattr(identity, "ARCHIVE_STATE", mover.archive_path)
    # Enter through the actual adapter producer, mover persistence and inventory
    # consumer. No fabricated verified archive or disposition is used.
    assert _audit(mover, "spool")["status"] == "PASS"
    mover.tick(now=0)
    assert identity.verified_segment(state, mover.archive, RELATIVE)
    assert not (mover.archive_root / identity.ROOM / relative).exists()
    return mover, client, state, stub


def _audit(mover, location):
    root = mover.spool if location == "spool" else mover.archive_root / identity.ROOM
    return source_integrity.audit_finalized_recording_inventory(
        root / "2026-10-24", room_id=identity.ROOM, adapter_state_path=mover.state_path,
    )


@pytest.mark.parametrize("location", ["spool", "115"])
def test_local_first_inventory_accepts_same_identity_and_keeps_stub_guard(
    local_first_disposition, location
):
    mover, _, state, stub = local_first_disposition
    audit = _audit(mover, location)
    assert audit["status"] == "PASS"
    assert audit["can_select"] is True
    assert len(audit["consumer_segments"]) == 1
    assert audit["archive_bindings"][RELATIVE][RELATIVE[:-4] + ".mp4"]["sha256"] == (
        state["finalized"][RELATIVE]["target_sha256"]
    )
    assert any(issue["code"] == adapter.SOURCE_DISPOSITION_REASON for issue in audit["issues"])
    assert stub.exists()


@pytest.mark.parametrize("location", ["spool", "115"])
@pytest.mark.parametrize("suffix", identity.SUFFIXES)
@pytest.mark.parametrize("kind", ["same_size_restored_mtime", "mtime_rollback", "inode_replace"])
def test_local_first_inventory_checks_actual_artifact_at_each_location(
    local_first_disposition, monkeypatch, location, suffix, kind
):
    mover, _, _, _ = local_first_disposition
    assert _audit(mover, location)["status"] == "PASS"
    root = mover.spool if location == "spool" else mover.archive_root / identity.ROOM
    _mutate(root / (RELATIVE[:-4] + suffix), kind, monkeypatch)
    audit = _audit(mover, location)
    assert audit["status"] == "BLOCKED"
    assert audit["can_select"] is False
    assert audit["consumer_segments"] == []


@pytest.mark.parametrize("role", ["source", "xml", "successor_source", "successor_mp4"])
@pytest.mark.parametrize("kind", ["same_size_restored_mtime", "mtime_rollback", "inode_replace"])
def test_cloud_inventory_rechecks_unmoved_stub_and_successor(
    local_first_disposition, monkeypatch, role, kind
):
    mover, _, _, stub = local_first_disposition
    target = {"source": stub, "xml": stub.with_suffix(".xml"),
              "successor_source": mover.spool / RELATIVE,
              "successor_mp4": (mover.spool / RELATIVE).with_suffix(".mp4")}[role]
    assert _audit(mover, "115")["status"] == "PASS"
    _mutate(target, kind, monkeypatch)
    assert _audit(mover, "115")["status"] == "BLOCKED"


@pytest.mark.parametrize("location", ["spool", "115"])
@pytest.mark.parametrize("suffix", [".mp4", ".jsonl"])
def test_local_first_inventory_rejects_missing_bound_artifact(
    local_first_disposition, location, suffix
):
    mover, _, _, _ = local_first_disposition
    root = mover.spool if location == "spool" else mover.archive_root / identity.ROOM
    (root / (RELATIVE[:-4] + suffix)).unlink()
    audit = _audit(mover, location)
    assert audit["status"] == "BLOCKED"
    assert audit["can_select"] is False


def test_cloud_inventory_rejects_missing_unmoved_stub(local_first_disposition):
    mover, _, _, stub = local_first_disposition
    stub.unlink()
    audit = _audit(mover, "115")
    assert audit["status"] == "BLOCKED"
    assert audit["can_select"] is False


@pytest.mark.parametrize("location", ["spool", "115"])
def test_local_first_inventory_rejects_ignored_stub_in_mp4_lane(local_first_disposition, location):
    mover, _, _, stub = local_first_disposition
    root = mover.spool if location == "spool" else mover.archive_root / identity.ROOM
    unexpected = root / stub.parent.name / stub.with_suffix(".mp4").name
    unexpected.write_bytes(b"unapproved stub mp4")
    audit = _audit(mover, location)
    assert audit["status"] == "BLOCKED"
    assert audit["can_select"] is False


@pytest.mark.parametrize("kind", ["same_size_restored_mtime", "mtime_rollback", "inode_replace"])
def test_mover_rejects_archive_drift_before_local_eviction(recording, monkeypatch, kind):
    mover, _, state = recording
    row = mover.prepare(state, RELATIVE)
    for name, item in row["files"].items():
        mover.copy_file(name, item)
        mover.verify_file(item)
    row["phase"] = "verified"
    mover.save()
    _mutate((mover.archive_root / identity.ROOM / RELATIVE).with_suffix(".mp4"), kind, monkeypatch)
    mover.tick(now=10)
    assert row["error"] == "ArchiveError"
    assert "drifted" in row["reason"]
    assert all((mover.spool / name).exists() for name in identity.artifact_paths(RELATIVE))
    assert not mover.events.exists()


@pytest.mark.parametrize("stage", ["copy", "evict"])
@pytest.mark.parametrize("suffix", [".flv", ".mp4"])
@pytest.mark.parametrize("kind", ["same_size_restored_mtime", "mtime_rollback", "inode_replace"])
def test_mover_preserves_local_preimage_guards(recording, monkeypatch, stage, suffix, kind):
    mover, _, state = recording
    row = mover.prepare(state, RELATIVE)
    name = RELATIVE[:-4] + suffix
    if stage == "evict":
        for artifact, item in row["files"].items():
            mover.copy_file(artifact, item)
            mover.verify_file(item)
        row["phase"] = "verified"
        mover.save()
    _mutate(mover.spool / name, kind, monkeypatch)
    with pytest.raises(identity.ArchiveError):
        if stage == "copy":
            mover.copy_file(name, row["files"][name])
        else:
            mover.evict(RELATIVE, row)
    # Eviction may already have removed other intact, cloud-verified siblings;
    # the changed preimage itself must never receive deletion authorization.
    assert (mover.spool / name).exists()
    assert not row["files"][name].get("delete_authorized")


@pytest.mark.parametrize("binding", ["source_size", "source_mtime_ns", "target_sha256"])
def test_mover_prepare_preserves_adapter_content_bindings(recording, monkeypatch, binding):
    mover, _, state = recording
    source = mover.spool / RELATIVE
    if binding == "source_size":
        source.write_bytes(source.read_bytes() + b"extra")
    else:
        _mutate(source if binding == "source_mtime_ns" else source.with_suffix(".mp4"),
                "mtime_rollback" if binding == "source_mtime_ns" else "same_size_restored_mtime",
                monkeypatch)
    with pytest.raises(identity.ArchiveError):
        mover.prepare(state, RELATIVE)
    assert not list(mover.archive_root.rglob("*.flv"))
    assert not mover.events.exists()


@pytest.mark.parametrize("location", ["spool", "115"])
def test_inventory_blocks_artifact_change_during_hashing(local_first_disposition, monkeypatch, location):
    mover, _, _, _ = local_first_disposition
    root = mover.spool if location == "spool" else mover.archive_root / identity.ROOM
    target = (root / RELATIVE).with_suffix(".mp4")
    original_hash = identity.sha256_file

    def unstable(path):
        result = original_hash(path)
        if path == target:
            current = path.stat()
            os.utime(path, ns=(current.st_atime_ns, current.st_mtime_ns - 1_000_000_000))
        return result

    monkeypatch.setattr(identity, "sha256_file", unstable)
    audit = _audit(mover, location)
    assert audit["status"] == "BLOCKED"
    assert "drifted while hashing" in audit["issues"][0]["message"]


def test_inventory_blocks_disposition_change_during_hashing(tmp_path, monkeypatch):
    date, stub, successor_mp4, state_path = _typed_connection_stub(tmp_path, monkeypatch)
    original_hash = identity.sha256_file

    def unstable(path):
        result = original_hash(path)
        if path == successor_mp4:
            current = path.stat()
            os.utime(path, ns=(current.st_atime_ns, current.st_mtime_ns - 1_000_000_000))
        return result

    monkeypatch.setattr(identity, "sha256_file", unstable)
    audit = source_integrity.audit_finalized_recording_inventory(
        date, room_id="123456", adapter_state_path=state_path
    )
    assert audit["status"] == "BLOCKED"
    assert "drifted while hashing" in audit["issues"][0]["source_disposition_error"]
