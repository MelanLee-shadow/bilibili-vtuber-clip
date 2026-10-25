"""Server-side acceptance for local capture and verified CD2 archival.

Run on OCI3; the implementation worker only compiles this file on the Mac.
No live provider, credential, service control or model call is used.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import time

import pytest

from ops.recording import bililive_recorder_adapter as adapter
from ops.recording import recording_archive as identity
from ops.recording import recording_spool_mover as spool_module
from ops.recording.cd2_status import CD2Error, CD2Status, GrpcWeb, UPLOAD, decode, request_bytes
from src.autoslice.source_integrity import audit_finalized_recording_inventory


RELATIVE = "2026-10-24/22966160_20261024-20-00-00.flv"


class FakeCD2:
    def __init__(self, archive_root: Path):
        self.archive_root = archive_root
        self.mode = "complete"
        self.now = 0
        self.outage_until = 0
        self.refresh_calls = []

    def upload_list(self, filename):
        if self.now < self.outage_until:
            raise TimeoutError("simulated CD2 outage")
        if self.mode == "fuse_only":
            return []
        path = next(self.archive_root.rglob(filename))
        relative = path.relative_to(self.archive_root / identity.ROOM).as_posix()
        return [{"key": "upload:" + relative, "destPath": identity.cloud_path(relative),
                 "size": path.stat().st_size, "statusEnum": 7 if self.mode == "queued" else 5,
                 "errorMessage": "", "transferedBytes": path.stat().st_size}]

    def refresh(self, path):
        self.refresh_calls.append(path)
        relative = path.split("/22966160/", 1)[1]
        local_view = self.archive_root / identity.ROOM / relative
        return {"id": "provider-id:" + relative, "fullPathName": path,
                "size": local_view.stat().st_size + (1 if self.mode == "size_mismatch" else 0),
                "isDirectory": False, "isCloudFile": True}


@pytest.fixture
def recording(tmp_path, monkeypatch):
    spool = tmp_path / "spool" / identity.ROOM
    directory = spool / "2026-10-24"
    directory.mkdir(parents=True)
    for name in identity.artifact_paths(RELATIVE):
        (spool / name).write_bytes(("sealed bytes for " + name).encode())
    source = spool / RELATIVE
    state = {"schema_version": "bililive-recorder-adapter-state.v1",
             "webhook_files": {RELATIVE: {"status": "CLOSED", "file_size": source.stat().st_size}},
             "finalized": {RELATIVE: {"source_size": source.stat().st_size,
                                        "source_mtime_ns": source.stat().st_mtime_ns,
                                        "target_sha256": identity.sha256_file(source.with_suffix(".mp4")),
                                        "target": "/adapter/Videos/22966160/" + RELATIVE[:-4] + ".mp4",
                                        "finalized_at": "2026-10-24T21:00:00Z"}},
             "source_dispositions": {}}
    state_path = tmp_path / "state" / "adapter-state.json"
    identity.atomic_json(state_path, state)
    cloud = tmp_path / "mount" / "115open" / "李豆沙录播-oci3"
    cloud.mkdir(parents=True)
    client = FakeCD2(cloud)
    monkeypatch.setattr(spool_module, "require_cloud_mount", lambda *args: None)
    monkeypatch.setattr(spool_module, "open_files", lambda paths: set())
    observations = spool_module.UploadObservations(client, state_path.with_name("uploads.json"))
    mover = spool_module.Mover(spool=spool, archive_root=cloud,
                               mount=tmp_path / "mount", state_path=state_path,
                               archive_path=state_path.with_name("archive-state.json"),
                               client=client, observations=observations)
    return mover, client, state


@pytest.mark.parametrize("mode", ["queued", "fuse_only", "size_mismatch"])
def test_only_finished_and_refreshed_equal_size_allows_deletion(recording, mode):
    mover, client, _ = recording
    client.mode = mode
    mover.tick()
    assert all((mover.spool / name).exists() for name in identity.artifact_paths(RELATIVE))
    assert mover.archive["segments"][RELATIVE]["phase"] != "verified"
    assert not mover.events.exists()


def test_complete_group_tombstones_sha_proof_and_actual_df(recording):
    mover, client, state = recording
    original = {name: identity.sha256_file(mover.spool / name)
                for name in identity.artifact_paths(RELATIVE)}
    mover.tick()
    assert identity.verified_segment(state, mover.archive, RELATIVE)
    assert len(client.refresh_calls) == 5
    for name, sha in original.items():
        assert not (mover.spool / name).exists()
        assert identity.sha256_file(mover.archive_root / identity.ROOM / name) == sha
        assert mover.archive["segments"][RELATIVE]["files"][name]["sha256"] == sha
    events = [json.loads(line) for line in mover.events.read_text().splitlines()]
    assert len(events) == 10
    for authorized, deleted in zip(events[::2], events[1::2]):
        assert authorized["event"] == "DELETE_AUTHORIZED"
        assert identity.proof_valid(authorized["proof"],
                                    path=authorized["proof"]["object"]["fullPathName"],
                                    size=authorized["size"])
        assert deleted["event"] == "LOCAL_DELETED"
        assert deleted["df"]["available_bytes"] >= 0


def test_thirty_second_cd2_outage_keeps_recording_and_retries_with_bounded_backoff(recording):
    mover, client, state = recording
    client.outage_until = 30
    live = mover.spool / "2026-10-24/22966160_20261024-20-30-00.flv"
    live.write_bytes(b"live")
    state["webhook_files"][str(live.relative_to(mover.spool))] = {"status": "OPEN"}
    identity.atomic_json(mover.state_path, state)
    for second in (0, 5, 15, 30):
        with live.open("ab") as out:
            out.write(b"recording continues")
        client.now = second
        mover.tick(now=second)
        assert live.read_bytes().endswith(b"recording continues")
        assert (mover.spool / RELATIVE).exists()
    assert mover.archive["segments"][RELATIVE]["next_attempt_epoch"] == 35
    client.now = 35
    mover.tick(now=35)
    assert identity.verified_segment(state, mover.archive, RELATIVE)
    assert not (mover.spool / RELATIVE).exists()
    assert live.exists()
    # No path in the new lane can issue Docker stop/restart. Old watchdog
    # stopping behavior is retained in test_mount_watchdog.py.
    source = Path(spool_module.__file__).read_text()
    assert "docker" not in source.lower()


def test_open_or_unfinalized_segments_are_never_copied(recording, monkeypatch):
    mover, _, state = recording
    state["webhook_files"][RELATIVE]["status"] = "OPEN"
    identity.atomic_json(mover.state_path, state)
    mover.tick(now=0)
    assert not list(mover.archive_root.rglob("*.flv"))
    state["webhook_files"][RELATIVE]["status"] = "CLOSED"
    identity.atomic_json(mover.state_path, state)
    monkeypatch.setattr(spool_module, "open_files", lambda paths: {paths[0]})
    mover.tick(now=10)
    assert not list(mover.archive_root.rglob("*.flv"))


def test_no_clobber_unknown_existing_destination(recording):
    mover, _, _ = recording
    target = mover.archive_root / identity.ROOM / RELATIVE
    target.parent.mkdir(parents=True)
    target.write_bytes(b"other owner's data")
    mover.tick()
    assert target.read_bytes() == b"other owner's data"
    assert (mover.spool / RELATIVE).exists()


def test_interrupted_create_only_copy_resumes_only_matching_owned_prefix(recording):
    mover, _, state = recording
    row = mover.prepare(state, RELATIVE)
    item = row["files"][RELATIVE]
    target = mover.archive_root / identity.ROOM / RELATIVE
    target.parent.mkdir(parents=True)
    target.write_bytes((mover.spool / RELATIVE).read_bytes()[:7])
    item.update(destination_created=True, copy_started_at_epoch=time.time())
    mover.save()
    mover.copy_file(RELATIVE, item)
    assert target.read_bytes() == (mover.spool / RELATIVE).read_bytes()
    target.write_bytes(b"conflict")
    with pytest.raises(identity.ArchiveError, match="prefix conflicts"):
        mover.copy_file(RELATIVE, item)


def test_crash_mid_delete_resumes_and_adapter_accepts_archived_identity(recording, monkeypatch):
    mover, _, state = recording
    original_unlink = Path.unlink
    failed = False

    def interrupt(path, *args, **kwargs):
        nonlocal failed
        if str(path).endswith(".xml") and not failed:
            failed = True
            raise OSError("simulated unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupt)
    mover.tick(now=0)
    assert not (mover.spool / RELATIVE).exists()
    assert (mover.spool / RELATIVE[:-4]).with_suffix(".xml").exists()
    assert adapter.archived_recordings(state, mover.archive_path) == {RELATIVE}
    mover.tick(now=10)
    assert all(not (mover.spool / name).exists() for name in identity.artifact_paths(RELATIVE))


def test_downstream_blocks_partial_group_then_preserves_original_identity(recording):
    mover, client, _ = recording
    client.mode = "queued"
    mover.tick(now=0)
    date = mover.archive_root / identity.ROOM / "2026-10-24"
    blocked = audit_finalized_recording_inventory(date, room_id=identity.ROOM,
                                                 adapter_state_path=mover.state_path,
                                                 archive_state_path=mover.archive_path)
    assert blocked["can_select"] is False
    assert blocked["issues"][0]["code"] == "RECORDING_ARCHIVE_PENDING"
    client.mode = "complete"
    mover.tick(now=10)
    accepted = audit_finalized_recording_inventory(date, room_id=identity.ROOM,
                                                  adapter_state_path=mover.state_path,
                                                  archive_state_path=mover.archive_path)
    assert accepted["can_select"] is True
    binding = accepted["archive_bindings"][RELATIVE]
    for name in identity.artifact_paths(RELATIVE):
        assert binding[name]["sha256"] == identity.sha256_file(mover.archive_root / identity.ROOM / name)
    assert len(accepted["consumer_segments"]) == 1


def test_disk_guard_warns_on_existing_channel_without_stopping_capture(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(spool_module, "capacity", lambda path: {"available_bytes": 15 * 1024**3,
                                                              "total_bytes": 146 * 1024**3})
    monkeypatch.setattr(spool_module.subprocess, "run", lambda args, **kw: calls.append(args))
    result = spool_module.disk_guard(tmp_path)
    assert result["event"] == "RECORDING_SPOOL_LOW_SPACE"
    assert result["minimum_available_bytes"] == 16 * 1024**3
    assert calls[0][:5] == ["logger", "-p", "daemon.warning", "-t", "oci3-disk-guard"]


def test_cd2_status_requests_get_all_and_force_refresh():
    calls = []
    path = identity.cloud_path(RELATIVE)

    class Transport:
        def stream(self, method, fields, schema, **kwargs):
            calls.append((method, fields))
            if method == "GetUploadFileList":
                yield {"totalCount": 1, "uploadFiles": [{"destPath": path, "statusEnum": 5}]}
            else:
                yield {"subFiles": [{"fullPathName": path, "id": "id", "size": 1, "isCloudFile": True}]}

    client = CD2Status(Transport())
    assert client.upload_list("piece.flv")[0]["statusEnum"] == 5
    assert client.refresh(path)["isDirectory"] is False
    assert calls == [("GetUploadFileList", {1: True, 4: "piece.flv"}),
                     ("GetSubFiles", {1: str(Path(path).parent), 2: True})]


def test_cd2_wire_finished_is_explicit_not_queue_absence():
    decoded = decode(request_bytes({1: "key", 2: "/dest", 3: 42, 8: 5}), UPLOAD)
    assert decoded == {"key": "key", "destPath": "/dest", "size": 42, "statusEnum": 5}
    assert "statusEnum" not in decode(request_bytes({1: "queued"}), UPLOAD)
    with pytest.raises(CD2Error):
        decode(b"\x08\x80", UPLOAD)


def test_grpc_web_refuses_missing_success_trailers(tmp_path, monkeypatch):
    token = tmp_path / "synthetic-test-token"
    token.write_text("dummy-test-value")
    token.chmod(0o600)
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kw: io.BytesIO(b"\x00\x00\x00\x00\x00"))
    with pytest.raises(CD2Error, match="trailers"):
        list(GrpcWeb("http://127.0.0.1:19798", token).stream("GetUploadFileList", {}, {}))


def test_historical_115_ledger_is_read_only_and_conflicting_identity_is_rejected(tmp_path):
    current_path = tmp_path / "oci3-local-first/adapter-state.json"
    old_path = tmp_path / "oci3-115open/adapter-state.json"
    old = {"schema_version": "bililive-recorder-adapter-state.v1",
           "webhook_files": {RELATIVE: {"status": "CLOSED"}},
           "finalized": {RELATIVE: {"target_sha256": "a" * 64}}, "source_dispositions": {}}
    current = {"schema_version": old["schema_version"], "webhook_files": {},
               "finalized": {}, "source_dispositions": {}}
    identity.atomic_json(old_path, old)
    identity.atomic_json(current_path, current)
    before = old_path.read_bytes()
    merged = identity.read_consumer_state(current_path)
    assert merged["finalized"][RELATIVE] == old["finalized"][RELATIVE]
    assert old_path.read_bytes() == before
    assert identity.read_json(current_path)["finalized"] == {}
    current["finalized"][RELATIVE] = {"target_sha256": "b" * 64}
    identity.atomic_json(current_path, current)
    with pytest.raises(identity.ArchiveError, match="identity overlaps"):
        identity.read_consumer_state(current_path)


def test_disposition_bound_evidence_is_archived_but_kept_local(recording):
    mover, _, state = recording
    # The existing adapter validator remains the authority for this row. The
    # mover must never invalidate its physical evidence by deleting a successor.
    state["source_dispositions"] = {"2026-10-24/22966160_20261024-19-59-59.flv": {
        "session": {"successor_relative_path": RELATIVE}}}
    identity.atomic_json(mover.state_path, state)
    mover.tick()
    row = mover.archive["segments"][RELATIVE]
    assert identity.verified_segment(state, mover.archive, RELATIVE)
    assert row["retained_reason"] == "ADAPTER_DISPOSITION_PHYSICAL_EVIDENCE"
    assert all((mover.spool / name).exists() for name in identity.artifact_paths(RELATIVE))
    assert identity.retained_validation_source(RELATIVE, mover.archive, spool=mover.spool) == mover.spool / RELATIVE
    (mover.spool / RELATIVE).write_bytes(b"changed evidence")
    with pytest.raises(identity.ArchiveError, match="evidence changed"):
        identity.retained_validation_source(RELATIVE, mover.archive, spool=mover.spool)


def test_cloud_object_identity_change_defers_eviction(recording):
    mover, client, state = recording
    row = mover.prepare(state, RELATIVE)
    item = row["files"][RELATIVE]
    mover.copy_file(RELATIVE, item)
    mover.verify_file(item)
    refresh = client.refresh
    client.refresh = lambda path: {**refresh(path), "id": "replacement-object"}
    with pytest.raises(identity.ArchiveError, match="identity changed"):
        mover.verify_file(item)
    assert (mover.spool / RELATIVE).exists()


def test_empty_jsonl_uses_proto3_zero_size_without_relaxing_finished_gate():
    row = CD2Status._upload_defaults({"key": "zero-byte-upload"})
    assert row["size"] == 0
    assert row["statusEnum"] == 0
    assert not identity.proof_valid({"upload": row}, path="/x", size=0)
