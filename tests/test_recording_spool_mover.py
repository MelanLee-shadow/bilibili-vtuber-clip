"""Server-side acceptance for local capture and verified CD2 archival.

Run on OCI3; the implementation worker only compiles this file on the Mac.
No live provider, credential, service control or model call is used.
"""
from __future__ import annotations

import io
import json
from functools import partial
from pathlib import Path
import sys
import time

import pytest

from ops.recording import bililive_recorder_adapter as adapter
from ops.recording import cd2_status as cd2_module
from ops.recording import recording_archive as identity
from ops.recording import recording_spool_mover as spool_module
from ops.recording.cd2_status import CD2Error, CD2Status, GrpcWeb, UPLOAD, decode, request_bytes
from src.autoslice.source_integrity import audit_finalized_recording_inventory


RELATIVE = f"2026-10-24/{identity.ROOM}_20261024-20-00-00.flv"


class FakeCD2:
    def __init__(self, archive_root: Path):
        self.archive_root = archive_root
        self.mode = "complete"
        self.now = 0
        self.outage_until = 0
        self.refresh_calls = []

    def check_auth(self):
        pass

    def push_uploads(self, *, on_open=None):
        if on_open is not None:
            on_open()
        # Finished may happen before subscription; only upload_list can then
        # supply evidence. Stream readiness is deliberately cleared on EOF.
        yield from ()

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
        relative = path.split(f"/{identity.ROOM}/", 1)[1]
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
                                        "target": f"/adapter/Videos/{identity.ROOM}/" + RELATIVE[:-4] + ".mp4",
                                        "finalized_at": "2026-10-24T21:00:00Z"}},
             "source_dispositions": {}}
    state_path = tmp_path / "state" / "adapter-state.json"
    identity.atomic_json(state_path, state)
    cloud = tmp_path / "mount" / identity.ARCHIVE_ROOT.relative_to(identity.CLOUD_MOUNT)
    cloud.mkdir(parents=True)
    client = FakeCD2(cloud)
    monkeypatch.setattr(spool_module, "require_cloud_mount", lambda *args: None)
    monkeypatch.setattr(spool_module, "open_files", lambda paths: set())
    monkeypatch.setattr(spool_module, "capacity", lambda path: {
        "available_bytes": 64 * 1024**3, "total_bytes": 146 * 1024**3})
    observations = spool_module.UploadObservations(client, state_path.with_name("uploads.json"))
    mover = spool_module.Mover(spool=spool, archive_root=cloud,
                               mount=tmp_path / "mount", state_path=state_path,
                               archive_path=state_path.with_name("archive-state.json"),
                               client=client, observations=observations)
    return mover, client, state


@pytest.fixture
def main_once(recording, monkeypatch, tmp_path):
    mover, _, _ = recording
    token = tmp_path / "synthetic-token"
    monkeypatch.setattr(sys, "argv", ["recording_spool_mover.py", "--spool", str(mover.spool),
                                     "--state-path", str(mover.state_path),
                                     "--archive-state", str(mover.archive_path),
                                     "--cd2-token-file", str(token)])
    monkeypatch.setattr(spool_module, "require_local_spool", lambda path: None)
    monkeypatch.setattr(identity, "ARCHIVE_ROOT", mover.archive_root)
    monkeypatch.setattr(identity, "CLOUD_MOUNT", mover.mount)
    monkeypatch.setattr(spool_module, "Mover", partial(spool_module.Mover, mount=mover.mount))

    class EndIteration(BaseException):
        pass

    def stop_sleep(seconds):
        raise EndIteration

    class OneWatch:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            with pytest.raises(EndIteration):
                self.target()

    monkeypatch.setattr(spool_module.threading, "Thread", OneWatch)
    monkeypatch.setattr(spool_module.time, "sleep", stop_sleep)

    def run():
        with pytest.raises(EndIteration):
            spool_module.main()
        return identity.read_json(mover.archive_path)

    return run, token


@pytest.mark.parametrize("auth", ["missing", "prefixed", "rejected"])
def test_main_copies_entire_group_without_authenticated_status(recording, main_once, monkeypatch,
                                                               capsys, auth):
    mover, _, _ = recording
    run, token = main_once
    synthetic_secret = "SYNTHETIC-NOT-A-REAL-TOKEN"
    requests = []

    def reject(request, **kwargs):
        requests.append(request.full_url.rsplit("/", 1)[-1])
        trailer = b"grpc-status: 16\r\ngrpc-message: Invalid auth token " + synthetic_secret.encode()
        return io.BytesIO(b"\x80" + len(trailer).to_bytes(4, "big") + trailer)

    monkeypatch.setattr("urllib.request.urlopen", reject)
    if auth != "missing":
        token.write_text(("name1234=" if auth == "prefixed" else "") + synthetic_secret)
        token.chmod(0o600)
    archive = run()
    row = archive["segments"][RELATIVE]
    assert row["phase"] == "pending"
    assert row["error"] == "CD2Error"
    for name in identity.artifact_paths(RELATIVE):
        assert row["files"][name]["copy_complete"]
        assert (mover.spool / name).read_bytes() == (mover.archive_root / identity.ROOM / name).read_bytes()
    assert not mover.events.exists()
    assert requests == (["GetUploadFileList", "PushMessage", "GetUploadFileList"] if auth == "rejected" else [])
    persisted = "".join(path.read_text() for path in mover.archive_path.parent.glob("*.json*"))
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "CD2_AUTH_CHECK_FAILED" in output
    expected_code = {"missing": "CD2_TOKEN_FILE_MISSING", "prefixed": "CD2_TOKEN_NAME_PREFIX",
                     "rejected": "CD2_AUTH_REJECTED"}[auth]
    assert expected_code in output
    assert row["reason"] == expected_code
    assert synthetic_secret not in output + persisted


@pytest.mark.parametrize("mode", ["complete", "queued", "fuse_only", "size_mismatch"])
def test_main_does_not_wait_for_stream_and_preserves_delete_gate(recording, main_once,
                                                                monkeypatch, mode):
    mover, client, state = recording
    client.mode = mode
    monkeypatch.setattr(spool_module, "CD2Status", lambda transport: client)
    archive = main_once[0]()
    row = archive["segments"][RELATIVE]
    assert all(item["copy_complete"] for item in row["files"].values())
    assert identity.verified_segment(state, archive, RELATIVE) == (mode == "complete")
    assert all((mover.spool / name).exists() == (mode != "complete")
               for name in identity.artifact_paths(RELATIVE))
    if mode == "fuse_only":
        assert not client.refresh_calls  # Missed Finished cannot be guessed from FUSE/queue absence.


def test_startup_auth_check_stays_in_background_while_main_copies(recording, main_once, monkeypatch):
    mover, client, _ = recording
    client.mode = "fuse_only"
    callbacks = []

    class DeferredThread:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            callbacks.append(self.target)

    monkeypatch.setattr(spool_module.threading, "Thread", DeferredThread)
    monkeypatch.setattr(spool_module, "CD2Status", lambda transport: client)
    monkeypatch.setattr(client, "check_auth", lambda: pytest.fail("auth check must remain in background"))
    archive = main_once[0]()
    assert len(callbacks) == 1
    assert all(item["copy_complete"] for item in archive["segments"][RELATIVE]["files"].values())
    assert all((mover.spool / name).exists() for name in identity.artifact_paths(RELATIVE))


def test_tick_copies_later_group_members_during_cd2_outage(recording, monkeypatch):
    mover, client, _ = recording
    client.outage_until = 30
    mover.tick(now=0)
    persisted = identity.read_json(mover.archive_path)
    assert all(item["copy_complete"] for item in persisted["segments"][RELATIVE]["files"].values())
    assert all((mover.spool / name).exists() for name in identity.artifact_paths(RELATIVE))
    assert not mover.events.exists()
    # A restart consumes the receipts without recopying, then obtains fresh
    # authenticated Finished + forceRefresh evidence before eviction.
    restarted = spool_module.Mover(spool=mover.spool, archive_root=mover.archive_root,
                                  state_path=mover.state_path, archive_path=mover.archive_path,
                                  client=client, observations=mover.observations, mount=mover.mount)
    monkeypatch.setattr(restarted, "copy_file", lambda *args: pytest.fail("already copied"))
    client.now = 35
    restarted.tick(now=35)
    assert all(not (mover.spool / name).exists() for name in identity.artifact_paths(RELATIVE))


def test_queue_disappearing_without_finished_retains_local(recording):
    mover, client, _ = recording
    client.mode = "queued"
    mover.tick(now=0)
    client.mode = "fuse_only"
    mover.tick(now=5)
    assert all((mover.spool / name).exists() for name in identity.artifact_paths(RELATIVE))
    assert not mover.events.exists()


def test_copy_capacity_floor_defers_without_create_and_retries(recording, monkeypatch, capsys):
    mover, client, _ = recording
    client.mode = "fuse_only"
    size = (mover.spool / RELATIVE).stat().st_size
    available = 8 * 1024**3 + size - 1
    calls = []
    monkeypatch.setattr(spool_module, "capacity", lambda path: {
        "available_bytes": available, "total_bytes": 146 * 1024**3})
    monkeypatch.setattr(spool_module.subprocess, "run", lambda args, **kw: calls.append(args))
    mover.tick(now=0)
    assert not list(mover.archive_root.rglob("*.flv"))
    assert all((mover.spool / name).exists() for name in identity.artifact_paths(RELATIVE))
    assert calls[0][:5] == ["logger", "-p", "daemon.warning", "-t", "oci3-disk-guard"]
    assert "RECORDING_SPOOL_LOW_SPACE" in capsys.readouterr().out
    # Equality is permitted; no extra margin may exclude an authorized copy.
    available += 1
    mover.tick(now=5)
    assert (mover.archive_root / identity.ROOM / RELATIVE).exists()
    assert (mover.spool / RELATIVE).exists()


def test_resumed_copy_reserves_only_remaining_bytes(recording, monkeypatch):
    mover, _, state = recording
    row = mover.prepare(state, RELATIVE)
    item = row["files"][RELATIVE]
    target = mover.archive_root / identity.ROOM / RELATIVE
    target.parent.mkdir(parents=True)
    prefix = (mover.spool / RELATIVE).read_bytes()[:-1]
    target.write_bytes(prefix)
    item.update(destination_created=True, copy_started_at_epoch=time.time())
    monkeypatch.setattr(spool_module, "capacity", lambda path: {
        "available_bytes": 8 * 1024**3 + 1, "total_bytes": 146 * 1024**3})
    mover.copy_file(RELATIVE, item)
    assert item["copy_complete"]
    assert target.read_bytes() == (mover.spool / RELATIVE).read_bytes()


def test_zero_size_create_obeys_floor_but_completed_copy_can_be_recovered(recording, monkeypatch):
    mover, _, state = recording
    name = RELATIVE[:-4] + ".jsonl"
    (mover.spool / name).write_bytes(b"")
    row = mover.prepare(state, RELATIVE)
    item = row["files"][name]
    available = 8 * 1024**3 - 1
    monkeypatch.setattr(spool_module, "capacity", lambda path: {
        "available_bytes": available, "total_bytes": 146 * 1024**3})
    monkeypatch.setattr(spool_module.subprocess, "run", lambda *args, **kwargs: None)
    with pytest.raises(identity.ArchiveError, match="8 GiB"):
        mover.copy_file(name, item)
    target = mover.archive_root / identity.ROOM / name
    assert not target.exists()
    available += 1
    mover.copy_file(name, item)
    # A crash after writing all bytes but before marking copy_complete requires
    # no new copy space; low disk must not prevent reaching verification.
    item.pop("copy_complete")
    available = 0
    mover.copy_file(name, item)
    assert item["copy_complete"]
    assert target.read_bytes() == b""


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


def _success_frames(payload=b""):
    trailer = b"grpc-status: 0\r\n"
    return (b"\x00" + len(payload).to_bytes(4, "big") + payload
            + b"\x80" + len(trailer).to_bytes(4, "big") + trailer)


class IdleResponse(io.BytesIO):
    def read(self, size=-1):
        if self.tell() == len(self.getvalue()):
            raise TimeoutError("SYNTHETIC-REMOTE-DETAIL-MUST-NOT-BE-LOGGED")
        return super().read(size)


@pytest.mark.parametrize("value", ["a-token-123", "jwt.header.signature", "QmFzZTY0Lw=="])
def test_raw_bearer_values_including_padding_are_sent_unchanged(tmp_path, monkeypatch, value):
    token = tmp_path / "synthetic-token"
    token.write_text(value + "\n")
    token.chmod(0o600)
    requests = []

    def reply(request, **kwargs):
        requests.append(request)
        return io.BytesIO(_success_frames())

    monkeypatch.setattr("urllib.request.urlopen", reply)
    assert list(GrpcWeb("http://127.0.0.1:19798", token).stream(
        "GetUploadFileList", {1: True, 4: ".recording-spool-auth-check"}, {})) == [{}]
    assert len(requests) == 1
    assert requests[0].get_header("Authorization") == "Bearer " + value
    assert requests[0].full_url.endswith("/GetUploadFileList")
    assert requests[0].data[5:] == request_bytes({1: True, 4: ".recording-spool-auth-check"})


@pytest.mark.parametrize("contents,code", [
    ("name1234=synthetic-value", "CD2_TOKEN_NAME_PREFIX"),
    ("", "CD2_TOKEN_FILE_FORMAT"),
    ("one\ntwo", "CD2_TOKEN_FILE_FORMAT"),
    ("Bearer synthetic-value", "CD2_TOKEN_FILE_FORMAT"),
])
def test_invalid_token_representations_have_safe_local_codes_before_rpc(tmp_path, monkeypatch, contents, code):
    token = tmp_path / "synthetic-token"
    token.write_text(contents)
    token.chmod(0o600)
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: pytest.fail("must not send malformed token"))
    with pytest.raises(CD2Error) as caught:
        list(GrpcWeb("http://127.0.0.1:19798", token).stream("GetUploadFileList", {}, {}))
    assert caught.value.code == code
    assert "synthetic-value" not in str(caught.value)


def test_grpc_auth_rejection_code_does_not_include_server_details(tmp_path, monkeypatch):
    token = tmp_path / "synthetic-token"
    token.write_text("synthetic-value")
    token.chmod(0o600)
    trailer = b"grpc-status: 16\r\ngrpc-message: SYNTHETIC-SECRET\r\n"
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: io.BytesIO(
        b"\x80" + len(trailer).to_bytes(4, "big") + trailer))
    with pytest.raises(CD2Error) as caught:
        list(GrpcWeb("http://127.0.0.1:19798", token).stream("GetUploadFileList", {}, {}))
    assert caught.value.code == "CD2_AUTH_REJECTED"
    assert "SYNTHETIC-SECRET" not in str(caught.value)


@pytest.mark.parametrize("stage", ["idle", "connect", "partial_header", "partial_body", "unary"])
def test_only_push_timeout_between_frames_is_idle(tmp_path, monkeypatch, stage):
    token = tmp_path / "synthetic-token"
    token.write_text("synthetic-value")
    token.chmod(0o600)

    def reply(*args, **kwargs):
        if stage == "connect":
            raise TimeoutError("synthetic connect failure")
        prefix = {"partial_header": b"\x00\x00", "partial_body": b"\x00\x00\x00\x00\x02\x08"}.get(stage, b"")
        return IdleResponse(prefix)

    monkeypatch.setattr("urllib.request.urlopen", reply)
    client = GrpcWeb("http://127.0.0.1:19798", token)
    expected = CD2Error if stage == "idle" else TimeoutError
    with pytest.raises(expected) as caught:
        list(client.stream("GetUploadFileList" if stage == "unary" else "PushMessage", {}, {}))
    if stage == "idle":
        assert caught.value.code == "CD2_PUSH_IDLE_TIMEOUT"


def test_watch_checks_auth_once_and_reconnects_idle_stream_without_clearing_ready(tmp_path, monkeypatch, capsys):
    token = tmp_path / "synthetic-token"
    token.write_text("synthetic-value")
    token.chmod(0o600)
    client = CD2Status(GrpcWeb("http://127.0.0.1:19798", token))
    observations = spool_module.UploadObservations(client, tmp_path / "uploads.json")
    methods = []
    canary_payloads = []
    ready_at_connect = []

    class Stop(BaseException):
        pass

    def reply(request, **kwargs):
        method = request.full_url.rsplit("/", 1)[-1]
        methods.append(method)
        if method == "GetUploadFileList":
            canary_payloads.append(request.data[5:])
            return io.BytesIO(_success_frames())
        ready_at_connect.append(observations.ready.is_set())
        if len(ready_at_connect) == 3:
            raise Stop
        return IdleResponse()

    monkeypatch.setattr("urllib.request.urlopen", reply)
    monkeypatch.setattr(spool_module.time, "sleep", lambda seconds: pytest.fail("idle must reconnect immediately"))
    with pytest.raises(Stop):
        observations.watch()
    assert methods == ["GetUploadFileList", "PushMessage", "PushMessage", "PushMessage"]
    assert canary_payloads == [request_bytes({1: True, 4: ".recording-spool-auth-check"})]
    assert ready_at_connect == [False, True, True]
    assert observations.ready.is_set()
    assert observations.rows == {}
    assert not observations.path.exists()
    output = capsys.readouterr()
    assert "CD2_AUTH_CHECK_OK" in output.out
    assert output.out.count("CD2_PUSH_IDLE code=CD2_PUSH_IDLE_TIMEOUT") == 2
    assert "CD2_PUSH_RETRY" not in output.out
    assert "SYNTHETIC-REMOTE-DETAIL" not in output.out + output.err


def test_watch_real_errors_back_off_and_idle_resets_delay_without_new_uploads(tmp_path, monkeypatch, capsys):
    class Stop(BaseException):
        pass

    class Client:
        checks = 0

        def check_auth(self):
            self.checks += 1

        def push_uploads(self, *, on_open):
            on_open()
            outcome = outcomes.pop(0)
            raise outcome
            yield  # retain the ordinary generator interface

    rejected = CD2Error("SYNTHETIC-SECRET")
    rejected.code = "CD2_AUTH_REJECTED"
    # The baseline producer represented idle as plain TimeoutError. Keeping
    # that fallback lets this same ordinary-path check run on the old tree.
    idle = getattr(cd2_module, "CD2IdleTimeout", TimeoutError)()
    outcomes = [TimeoutError("SYNTHETIC-SECRET"), rejected,
                idle, TimeoutError("SYNTHETIC-SECRET"), Stop()]
    client = Client()
    observations = spool_module.UploadObservations(client, tmp_path / "uploads.json")
    sleeps = []
    monkeypatch.setattr(spool_module.time, "sleep", lambda seconds: sleeps.append((seconds, observations.ready.is_set())))
    with pytest.raises(Stop):
        observations.watch()
    assert client.checks == 1
    assert sleeps == [(5, False), (10, False), (5, False)]
    assert observations.rows == {}
    output = capsys.readouterr()
    assert "CD2_AUTH_REJECTED" in output.out
    assert "CD2_TIMEOUT" in output.out
    assert "SYNTHETIC-SECRET" not in output.out + output.err


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
