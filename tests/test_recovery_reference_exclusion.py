"""Actual private publishers participate in the cleanup exclusion window."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.autoslice import producer_batch_transaction as batch
from src.autoslice import qixi_transaction_core as core
from src.autoslice.producer_delivery_transaction import (
    DeliveryArtifact, ProducerDeliveryTransactionError, prepare_delivery,
)
from test_producer_batch_transaction import _seal


CHILD = r'''
import hashlib, json, sys
from pathlib import Path
from src.autoslice import qixi_transaction_core as core
from src.autoslice import producer_batch_transaction as batch
from src.autoslice.producer_delivery_transaction import (
    DeliveryArtifact, prepare_delivery, load_prepared_delivery, preflight_prepared_delivery,
)
root, operation = Path(sys.argv[1]), sys.argv[2]
try:
    if operation == 'shared':
        with core.recovery_reference_write_window(root):
            print('READY', flush=True)
            sys.stdin.readline()
    elif operation == 'commit':
        with core.exclusive_runner_commit(root):
            print('COMMIT-LEASE', flush=True)
    else:
        source = root / 'out' / 'source.txt'
        if operation == 'prepare':
            handle = prepare_delivery(
                runtime_root=root, lane='talk', candidate_id='fixture',
                artifacts=[DeliveryArtifact('record', source, root/'repo'/'delivery.txt',
                                            'sha256:'+hashlib.sha256(source.read_bytes()).hexdigest())],
            )
        else:
            handle = load_prepared_delivery(runtime_root=root, manifest_path=Path(sys.argv[3]))
            result = {'status':'delivery_prepared_no_target', 'prepared_summary':{
                'prepared_delivery':{'manifest_path':str(handle.manifest_path),
                'prepared_sha256':'sha256:'+handle.prepared_sha256, 'upload_enabled':False}}}
            binding = {'pipeline_fingerprint':'sha256:'+'c'*64}
            dependencies = batch.bind_prepared_result_inputs({source:False})
            batch.checkpoint_prepared_result(
                entry=batch.PreparedBatchEntry('talk','fixture',handle), binding=binding,
                result=result, source_dependencies=dependencies,
            )
            assert batch.load_prepared_result_checkpoint(runtime_root=root,lane='talk',
                candidate_id='fixture',binding=binding)==result
            with core.exclusive_runner_commit(root) as lease:
                preflight_prepared_delivery(handle=handle,lease=lease)
        print(json.dumps({'status':'PASS','manifest_path':str(handle.manifest_path)}), flush=True)
except (ValueError, RuntimeError) as exc:
    print(str(exc), flush=True)
    sys.exit(2)
'''


def _runtime(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "runtime"
    root.mkdir()
    _seal(root)
    source = root / "out" / "source.txt"
    source.parent.mkdir()
    source.write_bytes(b"unchanged private original")
    return root, source


def _child(root: Path, operation: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", CHILD, str(root), operation, *args],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
    )


def _prepare(root: Path, source: Path):
    return prepare_delivery(
        runtime_root=root, lane="talk", candidate_id="fixture",
        artifacts=[DeliveryArtifact("record", source, root / "repo" / "delivery.txt",
                                   "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest())],
    )


@pytest.mark.parametrize("operation", ["prepare", "checkpoint"])
def test_maintenance_excludes_actual_publishers_then_normal_replay_succeeds(
    tmp_path: Path, operation: str,
) -> None:
    root, source = _runtime(tmp_path)
    args = ()
    if operation == "checkpoint":
        args = (str(_prepare(root, source).manifest_path),)
    store = root / ".prepared-deliveries"
    before = {str(p): p.read_bytes() for p in store.rglob("*") if p.is_file()}
    with core.exclusive_recovery_maintenance(root):
        blocked = _child(root, operation, *args)
        assert blocked.returncode == 2 and "recovery reference lock busy" in blocked.stdout
        assert {str(p): p.read_bytes() for p in store.rglob("*") if p.is_file()} == before
        assert _child(root, "commit").returncode == 2
        assert source.read_bytes() == b"unchanged private original"
    resumed = _child(root, operation, *args)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    result = json.loads(resumed.stdout)
    if operation == "prepare":
        # The same bytes subsequently reach the actual checkpoint loader and
        # formal preflight; no provider or shared delivery target is created.
        resumed = _child(root, "checkpoint", result["manifest_path"])
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert not (root / "repo" / "delivery.txt").exists()


def test_private_writers_remain_parallel_and_do_not_hold_runner_commit(tmp_path: Path) -> None:
    root, _ = _runtime(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(root), "shared"],
        cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout.readline().strip() == "READY"
        with core.recovery_reference_write_window(root):
            with core.recovery_reference_write_window(root):
                with core.exclusive_runner_commit(root) as lease:
                    core.require_runner_commit_lease(lease, runtime_root=root)
        with pytest.raises(core.QixiTransactionCoreError, match="reference lock busy"):
            with core.exclusive_recovery_maintenance(root):
                pytest.fail("maintenance cannot overlap an existing private writer")
    finally:
        stdout, stderr = process.communicate("release\n", timeout=15)
    assert process.returncode == 0, stdout + stderr
    with core.exclusive_recovery_maintenance(root):
        pass


def test_existing_runner_lease_reused_without_granting_nested_writer(tmp_path: Path) -> None:
    root, source = _runtime(tmp_path)
    with core.exclusive_runner_commit(root) as lease:
        with core.exclusive_recovery_maintenance(root) as reused:
            assert reused is lease
            with pytest.raises(ProducerDeliveryTransactionError, match="nesting is forbidden"):
                _prepare(root, source)
            assert not (root / ".prepared-deliveries").exists()
        core.require_runner_commit_lease(lease, runtime_root=root)
        _prepare(root, source)


def test_checkpoint_rejects_dependency_changed_before_locked_publication(tmp_path: Path) -> None:
    root, source = _runtime(tmp_path)
    handle = _prepare(root, source)
    dependencies = batch.bind_prepared_result_inputs({source: False})
    source.write_bytes(b"changed since dispatch captured the dependency")
    with pytest.raises(batch.ProducerBatchTransactionError, match="before checkpoint"):
        batch.checkpoint_prepared_result(
            entry=batch.PreparedBatchEntry("talk", "fixture", handle),
            binding={}, result={}, source_dependencies=dependencies,
        )
    assert list(handle.manifest_path.parent.glob("dispatch-result-*.json")) == []


def test_fork_cannot_reuse_parent_lease_capability(tmp_path: Path) -> None:
    with core.exclusive_runner_commit(tmp_path) as lease:
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                core.require_runner_commit_lease(lease, runtime_root=tmp_path)
                os.write(write_fd, b"UNEXPECTED")
            except core.QixiTransactionCoreError:
                os.write(write_fd, b"REJECTED")
            finally:
                os._exit(0)
        os.close(write_fd)
        result = os.read(read_fd, 100)
        os.close(read_fd)
        os.waitpid(pid, 0)
        assert result == b"REJECTED"
        core.require_runner_commit_lease(lease, runtime_root=tmp_path)


def test_unsafe_reference_lock_fails_before_private_namespace_write(tmp_path: Path) -> None:
    root, source = _runtime(tmp_path)
    foreign = root / "foreign"
    foreign.write_bytes(b"retain foreign bytes")
    (root / "recovery-reference.lock").symlink_to(foreign)
    with pytest.raises(ProducerDeliveryTransactionError, match="opened safely"):
        _prepare(root, source)
    assert foreign.read_bytes() == b"retain foreign bytes"
    assert not (root / ".prepared-deliveries").exists()
