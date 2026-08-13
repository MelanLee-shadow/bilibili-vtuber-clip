from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.autoslice import isolated_source_read as isolated


def test_isolated_read_returns_strict_hash_bound_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source.xml"
    payload = b"<i><d p='1'>hello</d></i>"
    source.write_bytes(payload)

    result = isolated.read_source_bytes_isolated(
        source,
        spool_root=tmp_path / "spool",
        timeout_seconds=2.0,
    )

    assert result.payload == payload
    assert result.source_binding["path"] == str(source.absolute())
    assert result.source_binding["size_bytes"] == len(payload)
    assert result.source_binding["sha256"] == "sha256:" + hashlib.sha256(payload).hexdigest()


def test_timeout_does_not_wait_and_same_key_stays_deduped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.xml"
    source.write_bytes(b"source")
    spool = tmp_path / "spool"
    original_child_argv = isolated._child_argv

    def detached_lock_holder(**kwargs: object) -> list[str]:
        lock_fd = int(kwargs["lock_fd"])
        script = (
            "import os,time;"
            "pid=os.fork();"
            "(os.setsid(),time.sleep(0.8),os._exit(0)) if pid==0 else time.sleep(60)"
        )
        return [sys.executable, "-c", script, str(lock_fd)]

    monkeypatch.setattr(isolated, "_child_argv", detached_lock_holder)
    started = time.monotonic()
    with pytest.raises(isolated.IsolatedSourceReadError) as timed_out:
        isolated.read_source_bytes_isolated(
            source,
            spool_root=spool,
            timeout_seconds=0.2,
            poll_interval_seconds=0.01,
        )
    elapsed = time.monotonic() - started

    assert timed_out.value.reason_code == "SOURCE_READ_TIMEOUT"
    assert elapsed < 0.6
    with pytest.raises(isolated.IsolatedSourceReadError) as active:
        isolated.read_source_bytes_isolated(
            source,
            spool_root=spool,
            timeout_seconds=0.2,
        )
    assert active.value.reason_code == "SOURCE_READ_ALREADY_ACTIVE"

    monkeypatch.setattr(isolated, "_child_argv", original_child_argv)
    time.sleep(0.9)
    assert (
        isolated.read_source_bytes_isolated(
            source,
            spool_root=spool,
            timeout_seconds=2.0,
        ).payload
        == b"source"
    )
    assert list((spool / "keys").iterdir()) == []
    assert list((spool / "runs").iterdir()) == []


def test_timeout_path_never_calls_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.xml"
    source.write_bytes(b"source")

    class NeverWaitProcess:
        pid = 43210

        def poll(self):
            return None

        def wait(self, *_args: object, **_kwargs: object):
            pytest.fail("timeout path must never call wait")

    process = NeverWaitProcess()
    monkeypatch.setattr(isolated.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(isolated.os, "killpg", lambda *_args: None)
    try:
        with pytest.raises(isolated.IsolatedSourceReadError) as timed_out:
            isolated.read_source_bytes_isolated(
                source,
                spool_root=tmp_path / "spool",
                timeout_seconds=0.01,
                poll_interval_seconds=0.001,
            )
        assert timed_out.value.reason_code == "SOURCE_READ_TIMEOUT"
    finally:
        isolated._TIMED_OUT_CHILDREN.remove(process)


def test_fuse_spool_and_oversized_source_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.xml"
    source.write_bytes(b"source")
    monkeypatch.setattr(isolated, "_linux_filesystem_type", lambda _path: "fuse.CloudFS")
    with pytest.raises(isolated.IsolatedSourceReadError) as fuse_spool:
        isolated.read_source_bytes_isolated(source, spool_root=tmp_path / "spool")
    assert fuse_spool.value.reason_code == "LOCAL_SPOOL_ON_FUSE"

    monkeypatch.undo()
    source.open("wb").truncate(isolated.MAX_SOURCE_BYTES + 1)
    with pytest.raises(isolated.IsolatedSourceReadError) as too_large:
        isolated.read_source_bytes_isolated(
            source,
            spool_root=tmp_path / "spool",
            timeout_seconds=2.0,
        )
    assert too_large.value.reason_code == "SOURCE_TOO_LARGE"

    target = tmp_path / "target.xml"
    target.write_bytes(b"target")
    symlink = tmp_path / "symlink.xml"
    symlink.symlink_to(target)
    with pytest.raises(isolated.IsolatedSourceReadError) as symlinked:
        isolated.read_source_bytes_isolated(
            symlink,
            spool_root=tmp_path / "spool",
            timeout_seconds=2.0,
        )
    assert symlinked.value.reason_code == "SOURCE_NOT_REGULAR"


def test_active_orphan_limit_refuses_another_source(tmp_path: Path) -> None:
    source = tmp_path / "source.xml"
    source.write_bytes(b"source")
    spool = tmp_path / "spool"
    keys = spool / "keys"
    spool.mkdir(mode=0o700)
    keys.mkdir(mode=0o700)
    lock_paths = [keys / f"{index:064x}.lock" for index in range(isolated.MAX_ACTIVE_READS)]
    ready = tmp_path / "ready"
    holder_script = (
        "import fcntl,pathlib,sys,time;"
        "fds=[open(p,'a+b') for p in sys.argv[2:]];"
        "[fcntl.flock(f.fileno(),fcntl.LOCK_EX) for f in fds];"
        "pathlib.Path(sys.argv[1]).write_text('ready');"
        "time.sleep(60)"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_script, str(ready), *(str(path) for path in lock_paths)]
    )
    try:
        deadline = time.monotonic() + 2.0
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()

        with pytest.raises(isolated.IsolatedSourceReadError) as saturated:
            isolated.read_source_bytes_isolated(
                source,
                spool_root=spool,
                timeout_seconds=1.0,
            )
        assert saturated.value.reason_code == "SOURCE_READ_ORPHAN_LIMIT"
    finally:
        holder.terminate()
        holder.wait(timeout=2.0)


def test_oversized_registry_fails_closed_without_scanning(tmp_path: Path) -> None:
    source = tmp_path / "source.xml"
    source.write_bytes(b"source")
    spool = tmp_path / "spool"
    keys = spool / "keys"
    spool.mkdir(mode=0o700)
    keys.mkdir(mode=0o700)
    for index in range(isolated.MAX_REGISTRY_LOCKS + 1):
        (keys / f"{index:064x}.lock").touch()

    with pytest.raises(isolated.IsolatedSourceReadError) as oversized:
        isolated.read_source_bytes_isolated(
            source,
            spool_root=spool,
            timeout_seconds=1.0,
        )
    assert oversized.value.reason_code == "SOURCE_READ_REGISTRY_LIMIT"
    assert len(list(keys.iterdir())) == isolated.MAX_REGISTRY_LOCKS + 1


def test_unknown_spool_filesystem_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.xml"
    source.write_bytes(b"source")
    monkeypatch.setattr(isolated, "_linux_filesystem_type", lambda _path: None)

    with pytest.raises(isolated.IsolatedSourceReadError) as unknown:
        isolated.read_source_bytes_isolated(source, spool_root=tmp_path / "spool")
    assert unknown.value.reason_code == "LOCAL_SPOOL_FILESYSTEM_UNKNOWN"


def test_parent_rejects_local_payload_hash_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "source.xml"
    source.write_bytes(b"source")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "payload.bin").write_bytes(b"tampered")
    (run_dir / "result.json").write_text(
        json.dumps(
            {
                "schema_version": isolated.RESULT_SCHEMA,
                "status": "OK",
                "token": "token",
                "source_path": str(source.absolute()),
                "source_binding": {
                    "path": str(source.absolute()),
                    "size_bytes": 6,
                    "mtime_ns": 1,
                    "ctime_ns": 1,
                    "device": 1,
                    "inode": 1,
                    "mode": 0o600,
                    "sha256": "sha256:" + hashlib.sha256(b"source").hexdigest(),
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(isolated.IsolatedSourceReadError) as invalid:
        isolated._load_local_result(
            run_dir,
            source_path=str(source.absolute()),
            token="token",
        )
    assert invalid.value.reason_code == "LOCAL_READ_RESULT_INVALID"


def test_parent_rejects_symlinked_local_result(tmp_path: Path) -> None:
    source = tmp_path / "source.xml"
    source.write_bytes(b"source")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (run_dir / "result.json").symlink_to(outside)

    with pytest.raises(isolated.IsolatedSourceReadError) as invalid:
        isolated._load_local_result(
            run_dir,
            source_path=str(source.absolute()),
            token="token",
        )
    assert invalid.value.reason_code == "SOURCE_READ_CHILD_FAILED"
