"""Two-process accounting regression using synthetic source and provider seams only."""
from __future__ import annotations

import hashlib
import json
import multiprocessing
from pathlib import Path
import sys


def _observer_worker(source: str, output: str, pipe, hold: bool) -> None:
    from src.autoslice import native_foreign_witness as native
    from src.autoslice.moss_transcription import MOSS_MODEL
    from src.autoslice.supplement_audio_budget import get_budget, start_budget

    # No model/API may be reached from a synthetic race reproduction.
    def no_network(event, _args):
        if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo"}:
            raise AssertionError("network forbidden in synthetic regression")

    sys.addaudithook(no_network)
    path = Path(source)
    start_budget(path)

    def extract(_source, target, _start, _end):
        pipe.send({"event": "extraction"})
        if hold:
            assert pipe.poll(20), "parent did not release the owned test process"
            assert pipe.recv() == "release"
        target.write_bytes(b"synthetic audio, not decodable media")

    def observe_secondary(_audio, **kwargs):
        budget = get_budget(path)
        attempt = budget.consume("moss", MOSS_MODEL, kwargs["crop_start_ms"], kwargs["crop_end_ms"])
        pipe.send({"event": "simulated_dispatch"})
        budget.finish_attempt(attempt, status="OBSERVED")
        return {"served_from_cache": False}

    native._extract_exact_mp3 = extract
    native.observe_secondary = observe_secondary
    # Lexical/ASR qualification is unrelated to the accounting race under test.
    native.exact_target_evidence = lambda *args, **kwargs: {"native_segments": []}
    try:
        observer = native.build_native_foreign_witness(
            source_media=path, output_dir=Path(output), provider="moss",
        )
        pipe.send({"event": "ready"})
        assert pipe.poll(20)
        assert pipe.recv() == "observe"
        observer(start_ms=1_000, end_ms=2_000)
        pipe.send({"event": "done", "status": "success"})
    except BaseException as exc:
        pipe.send({"event": "done", "status": "refused", "type": type(exc).__name__,
                   "reason_code": getattr(exc, "reason_code", None), "detail": str(exc)})
    finally:
        pipe.close()


def _receive(pipe) -> dict:
    assert pipe.poll(25), "owned worker did not provide its bounded result"
    return pipe.recv()


def _until_done(pipe) -> list[dict]:
    result = []
    while True:
        row = _receive(pipe)
        result.append(row)
        if row["event"] == "done":
            return result


def test_parallel_observers_do_not_dispatch_from_one_receipt_twice(tmp_path: Path) -> None:
    """Hold one real native observer at extraction, then admit a second process."""
    source = tmp_path / "synthetic-source.bin"
    source.write_bytes(b"shared synthetic immutable source")
    output = tmp_path / "evidence"
    ctx = multiprocessing.get_context("spawn")
    parent_a, child_a = ctx.Pipe()
    parent_b, child_b = ctx.Pipe()
    a = ctx.Process(target=_observer_worker, args=(str(source), str(output), child_a, True))
    b = ctx.Process(target=_observer_worker, args=(str(source), str(output), child_b, False))
    traces = {"A": [], "B": []}
    try:
        a.start()
        assert _receive(parent_a) == {"event": "ready"}
        b.start()
        assert _receive(parent_b) == {"event": "ready"}
        parent_a.send("observe")
        traces["A"].append(_receive(parent_a))
        assert traces["A"][-1] == {"event": "extraction"}
        parent_b.send("observe")
        traces["B"] = _until_done(parent_b)
        parent_a.send("release")
        traces["A"].extend(_until_done(parent_a))
        a.join(10)
        b.join(10)
        assert not a.is_alive() and not b.is_alive()
        assert a.exitcode == b.exitcode == 0
        receipt = json.loads((output / "native-audio-budget.json").read_bytes())
        observed_dispatches = sum(
            event["event"] == "simulated_dispatch" for rows in traces.values() for event in rows
        )
        evidence = {"traces": traces, "pids": [a.pid, b.pid],
                    "simulated_dispatches": observed_dispatches,
                    "recorded_attempts": receipt["budget"]["attempt_count"],
                    "receipt_revision": receipt["revision"], "workers_exited": True,
                    "real_provider_calls": 0,
                    "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
        (tmp_path / "OBSERVED.json").write_text(json.dumps(evidence, indent=2) + "\n")
        assert traces["A"][-1]["status"] == "success"
        assert traces["B"][-1]["status"] == "refused", evidence
        assert traces["B"][-1]["reason_code"] == "LOCAL_AUDIO_BUDGET_RECEIPT_PERSIST_FAILED"
        assert not any(e["event"] in {"extraction", "simulated_dispatch"} for e in traces["B"])
        assert observed_dispatches == receipt["budget"]["attempt_count"] == 1
    finally:
        # Only these new synthetic child processes belong to this test.
        for process in (a, b):
            if process.pid and process.is_alive():
                process.terminate()
                process.join(5)
        for pipe in (parent_a, child_a, parent_b, child_b):
            pipe.close()


def _hold_lock(path: str, pipe) -> None:
    from src.autoslice.native_audio_budget_receipt import exclusive_native_audio_budget
    with exclusive_native_audio_budget(Path(path)):
        pipe.send("locked")
        assert pipe.poll(20)
        assert pipe.recv() == "release"
    pipe.send("released")
    pipe.close()


def test_busy_receipt_blocks_constructor_and_direct_writer(tmp_path: Path) -> None:
    import pytest
    from src.autoslice import native_audio_budget_receipt as receipts
    from src.autoslice import native_foreign_witness as native
    from src.autoslice.supplement_audio_budget import start_budget

    source = tmp_path / "source.bin"
    source.write_bytes(b"synthetic source")
    receipt = tmp_path / "out/native-audio-budget.json"
    start_budget(source)
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    worker = ctx.Process(target=_hold_lock, args=(str(receipt), child))
    worker.start()
    try:
        assert _receive(parent) == "locked"
        lock = receipt.with_name(receipt.name + ".lock")
        identity = lock.stat().st_ino
        with pytest.raises(receipts.BudgetReceiptPersistenceError):
            receipts.persist_native_audio_budget_receipt(
                source_media=source, source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                receipt_path=receipt,
            )
        with pytest.raises(receipts.BudgetReceiptPersistenceError):
            native.build_native_foreign_witness(
                source_media=source, output_dir=receipt.parent, provider="moss",
            )
        assert not receipt.exists()
        parent.send("release")
        assert _receive(parent) == "released"
        worker.join(10)
        assert worker.exitcode == 0 and not worker.is_alive()
        with receipts.exclusive_native_audio_budget(receipt):
            # Nested persistence must share the owning descriptor, not self-deadlock.
            packet = receipts.persist_native_audio_budget_receipt(
                source_media=source, source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                receipt_path=receipt,
            )
        assert packet["revision"] == 0 and packet["budget"]["attempt_count"] == 0
        assert lock.stat().st_ino == identity
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        parent.close()
        child.close()


def test_lock_release_on_body_error_preserves_inode(tmp_path: Path) -> None:
    import pytest
    from src.autoslice.native_audio_budget_receipt import exclusive_native_audio_budget
    path = tmp_path / "receipt.json"
    lock = tmp_path / "receipt.json.lock"
    with pytest.raises(ValueError, match="synthetic body error"):
        with exclusive_native_audio_budget(path):
            original = lock.stat().st_ino
            raise ValueError("synthetic body error")
    with exclusive_native_audio_budget(path):
        assert lock.stat().st_ino == original and lock.stat().st_nlink == 1
    assert not path.exists()


def test_unsafe_existing_lock_never_overwritten(tmp_path: Path) -> None:
    import os
    import pytest
    from src.autoslice.native_audio_budget_receipt import (
        BudgetReceiptPersistenceError, exclusive_native_audio_budget,
    )
    for kind in ("symlink", "dangling", "hardlink", "contents", "directory", "mode"):
        folder = tmp_path / kind
        folder.mkdir()
        path, lock = folder / "receipt.json", folder / "receipt.json.lock"
        other = folder / "preserved.bin"
        other.write_bytes(b"")
        other.chmod(0o600)
        if kind in {"symlink", "dangling"}:
            lock.symlink_to(other if kind == "symlink" else folder / "missing")
        elif kind == "hardlink":
            os.link(other, lock)
        elif kind == "directory":
            lock.mkdir()
        else:
            lock.write_bytes(b"do not erase" if kind == "contents" else b"")
            lock.chmod(0o644 if kind == "mode" else 0o600)
        before = lock.lstat()
        with pytest.raises(BudgetReceiptPersistenceError):
            with exclusive_native_audio_budget(path):
                pytest.fail("unsafe lock admitted")
        after = lock.lstat()
        assert (after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns) == (
            before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns,
        )
        assert not path.exists() and other.read_bytes() == b""


def _try_inherited_lock(path: str, pipe) -> None:
    from src.autoslice.native_audio_budget_receipt import (
        BudgetReceiptPersistenceError, exclusive_native_audio_budget,
    )
    try:
        with exclusive_native_audio_budget(Path(path)):
            pipe.send("wrongly inherited ownership")
    except BudgetReceiptPersistenceError:
        pipe.send("refused")
    pipe.close()


def test_fork_child_does_not_inherit_receipt_ownership(tmp_path: Path) -> None:
    from src.autoslice.native_audio_budget_receipt import exclusive_native_audio_budget
    path = tmp_path / "receipt.json"
    ctx = multiprocessing.get_context("fork")
    parent, child = ctx.Pipe()
    worker = ctx.Process(target=_try_inherited_lock, args=(str(path), child))
    try:
        with exclusive_native_audio_budget(path):
            worker.start()
            assert _receive(parent) == "refused"
            worker.join(10)
            assert worker.exitcode == 0 and not worker.is_alive()
        with exclusive_native_audio_budget(path):
            assert not path.exists()
    finally:
        if worker.pid and worker.is_alive():
            worker.terminate()
            worker.join(5)
        parent.close()
        child.close()
