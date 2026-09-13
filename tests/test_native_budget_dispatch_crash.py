"""Hard process-exit regression; synthetic source and no network/provider access."""
from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import sys

import pytest


def _crash_worker(root: str, provider: str, run: int, before_send: bool) -> None:
    def no_network(event, _args):
        if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo"}:
            raise AssertionError("network forbidden in synthetic crash regression")

    sys.addaudithook(no_network)
    from src.autoslice import diarized_transcription as transcriber
    from src.autoslice import native_foreign_witness as native
    from src.autoslice.supplement_audio_budget import start_budget

    folder = Path(root)
    source = folder / "synthetic-source.bin"
    start_budget(source)

    def extract(_source, target, _start, _end):
        target.write_bytes(b"synthetic audio, not decodable media")

    def fake_dispatch(_audio, **kwargs):
        # Use the real secondary observer's actual before-HTTP callback.
        kwargs["before_request"]()
        if before_send:
            os._exit(73)
        with (folder / "simulated-dispatches.jsonl").open("a") as handle:
            handle.write(json.dumps({"run": run, "provider": provider}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        # No finally blocks execute, as with a killed/crashed provider worker.
        os._exit(73)

    native._extract_exact_mp3 = extract
    transcriber.transcribe_evidence = fake_dispatch
    try:
        observe = native.build_native_foreign_witness(
            source_media=source, output_dir=folder / "evidence", provider=provider,
        )
        observe(start_ms=1_000, end_ms=2_000)
    except BaseException as exc:
        (folder / f"worker-{run}.json").write_text(json.dumps({
            "outcome": "refused", "type": type(exc).__name__,
            "reason_code": getattr(exc, "reason_code", None), "detail": str(exc),
        }, indent=2) + "\n")


@pytest.mark.parametrize("provider", ["moss", "mai"])
@pytest.mark.parametrize("before_send", [False, True])
def test_crash_after_dispatch_does_not_erase_attempt_or_allow_restart(
    tmp_path: Path, provider: str, before_send: bool,
) -> None:
    (tmp_path / "synthetic-source.bin").write_bytes(b"immutable synthetic input")
    ctx = multiprocessing.get_context("spawn")
    snapshots = []
    for run in (1, 2):
        worker = ctx.Process(target=_crash_worker, args=(str(tmp_path), provider, run, before_send))
        try:
            worker.start()
            worker.join(20)
            assert not worker.is_alive(), "owned synthetic worker exceeded bound"
            receipt = json.loads((tmp_path / "evidence/native-audio-budget.json").read_bytes())
            outcome = tmp_path / f"worker-{run}.json"
            snapshots.append({
                "run": run, "exitcode": worker.exitcode,
                "recorded_attempts": receipt["budget"]["attempt_count"],
                "pending_attempts": receipt["budget"]["pending_attempt_count"],
                "revision": receipt["revision"],
                "outcome": json.loads(outcome.read_bytes()) if outcome.exists() else None,
            })
        finally:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
    log = tmp_path / "simulated-dispatches.jsonl"
    dispatches = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    observed = {"provider": provider, "before_send": before_send, "snapshots": snapshots,
                "simulated_dispatches": len(dispatches), "events": dispatches,
                "real_provider_calls": 0, "workers_exited": True}
    (tmp_path / "OBSERVED.json").write_text(json.dumps(observed, indent=2) + "\n")
    assert snapshots[0]["exitcode"] == 73, observed
    assert snapshots[0]["recorded_attempts"] == 1, observed
    assert snapshots[0]["pending_attempts"] == 1, observed
    assert snapshots[1]["exitcode"] == 0, observed
    assert snapshots[1]["outcome"]["reason_code"] == "LOCAL_AUDIO_BUDGET_RECEIPT_PERSIST_FAILED"
    assert snapshots[1]["recorded_attempts"] == 1, observed
    assert len(dispatches) == (0 if before_send else 1), observed


def _native_fixture(tmp_path, monkeypatch, provider, *, max_audio_ms=1_000):
    import hashlib
    from src.autoslice import diarized_transcription as transcriber
    from src.autoslice import native_foreign_witness as native
    from src.autoslice.mai_transcription import MAI_MODEL
    from src.autoslice.moss_transcription import MOSS_MODEL
    from src.autoslice.supplement_audio_budget import start_budget

    source = tmp_path / "synthetic-source.bin"
    source.write_bytes(b"immutable synthetic input")
    receipt = tmp_path / "evidence/native-audio-budget.json"
    budget = start_budget(source, max_windows=1, max_audio_ms=max_audio_ms)
    calls = []
    extracted = []

    def extract(_source, output, start, end):
        extracted.append((start, end))
        output.write_bytes(b"synthetic audio")

    def fake_dispatch(audio, **kwargs):
        kwargs["before_request"]()
        pending = json.loads(receipt.read_bytes())
        assert pending["budget"]["pending_attempt_count"] == 1
        assert pending["budget"]["attempts"][-1]["status"] == "DISPATCHED"
        calls.append(kwargs["provider"])
        return {
            "provider": kwargs["provider"],
            "model": {"mai": MAI_MODEL, "moss": MOSS_MODEL}[kwargs["provider"]],
            "status": "OBSERVED", "input_audio_sha256": hashlib.sha256(audio).hexdigest(),
            "response_sha256": "a" * 64, "native_segments": [],
            "native_timeline": {"valid": True}, "raw_response": {"text": "synthetic"},
        }

    monkeypatch.setattr(native, "_extract_exact_mp3", extract)
    monkeypatch.setattr(native, "exact_target_evidence", lambda *a, **k: {"native_segments": []})
    monkeypatch.setattr(transcriber, "transcribe_evidence", fake_dispatch)
    observer = native.build_native_foreign_witness(
        source_media=source, output_dir=receipt.parent, provider=provider,
    )
    return observer, budget, receipt, calls, extracted


@pytest.mark.parametrize("provider", ["mai", "moss"])
def test_committed_reservation_precedes_dispatch_and_cache_does_not_reserve_again(
    tmp_path, monkeypatch, provider,
):
    from src.autoslice.supplement_audio_budget import BudgetExceeded
    observer, budget, receipt, calls, _ = _native_fixture(tmp_path, monkeypatch, provider)
    assert observer(start_ms=1_000, end_ms=2_000)["served_from_cache"] is False
    assert observer(start_ms=1_000, end_ms=2_000)["served_from_cache"] is True
    with pytest.raises(BudgetExceeded):
        observer(start_ms=3_000, end_ms=4_000)
    packet = json.loads(receipt.read_bytes())
    assert packet["budget"] == budget.snapshot()
    assert calls == [provider]
    assert packet["budget"]["attempt_count"] == 1
    assert packet["budget"]["total_audio_ms"] == 1_000
    assert packet["budget"]["cache_hit_count"] == 1
    assert packet["budget"]["refusal_count"] == 1
    assert packet["budget"]["attempts"][0]["status"] == "OBSERVED"


@pytest.mark.parametrize("provider", ["mai", "moss"])
@pytest.mark.parametrize("point", ["file_fsync", "replace", "directory_fsync"])
def test_persistence_failure_stops_before_http_and_keeps_outcome_unresolved(
    tmp_path, monkeypatch, provider, point,
):
    import stat
    from src.autoslice import native_audio_budget_receipt as receipts
    observer, budget, receipt, calls, extracted = _native_fixture(tmp_path, monkeypatch, provider)
    previous = receipt.read_bytes()
    real_fsync, real_replace = os.fsync, os.replace

    def fail_sync(fd):
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if ((point == "directory_fsync" and is_directory)
                or (point == "file_fsync" and not is_directory)):
            raise OSError("synthetic durability failure")
        real_fsync(fd)

    def fail_replace(source, target):
        if point == "replace" and Path(target) == receipt:
            raise OSError("synthetic replacement failure")
        real_replace(source, target)

    with monkeypatch.context() as faults:
        faults.setattr(receipts.os, "fsync", fail_sync)
        faults.setattr(receipts.os, "replace", fail_replace)
        with pytest.raises(receipts.BudgetReceiptPersistenceError):
            observer(start_ms=1_000, end_ms=2_000)
    assert calls == []
    assert extracted == [(1_000, 2_000)]
    assert budget.snapshot()["pending_attempt_count"] == 1
    assert budget.snapshot()["sealed_attempt_count"] == 0
    assert budget.snapshot()["failed_attempt_count"] == 0
    if point != "directory_fsync":
        assert receipt.read_bytes() == previous
    else:
        assert json.loads(receipt.read_bytes())["budget"]["pending_attempt_count"] == 1
    assert not list(receipt.parent.glob(".native-audio-budget-*.json"))
    # The same process cannot pretend the unresolved request freed its budget.
    with pytest.raises(receipts.BudgetReceiptPersistenceError):
        observer(start_ms=1_000, end_ms=2_000)
    assert calls == [] and extracted == [(1_000, 2_000)]


def test_receipt_fsyncs_file_before_replace_and_directory_after(tmp_path, monkeypatch):
    import hashlib
    import stat
    from src.autoslice import native_audio_budget_receipt as receipts
    from src.autoslice.supplement_audio_budget import start_budget
    source = tmp_path / "source.bin"
    source.write_bytes(b"synthetic source")
    budget = start_budget(source)
    budget.consume("moss", "synthetic-model", 0, 1_000)
    path = tmp_path / "out/native-audio-budget.json"
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def sync(fd):
        events.append("directory_fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file_fsync")
        real_fsync(fd)

    def replace(source, target):
        events.append("replace")
        real_replace(source, target)

    with monkeypatch.context() as spies:
        spies.setattr(receipts.os, "fsync", sync)
        spies.setattr(receipts.os, "replace", replace)
        result = receipts.persist_native_audio_budget_receipt(
            source_media=source, source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            receipt_path=path,
        )
    assert events == ["file_fsync", "replace", "directory_fsync"]
    assert result["receipt_sha256"] == receipts.receipt_sha256(result)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("provider", ["mai", "moss"])
def test_terminal_write_failure_preserves_pending_dispatch_and_blocks_replay(
    tmp_path, monkeypatch, provider,
):
    from src.autoslice import native_audio_budget_receipt as receipts
    observer, budget, receipt, calls, extracted = _native_fixture(tmp_path, monkeypatch, provider)
    original = receipts._write_atomic

    def fail_terminal(path, payload):
        if payload["budget"]["sealed_attempt_count"]:
            raise receipts.BudgetReceiptPersistenceError("synthetic terminal write failure")
        original(path, payload)

    with monkeypatch.context() as fault:
        fault.setattr(receipts, "_write_atomic", fail_terminal)
        with pytest.raises(receipts.BudgetReceiptPersistenceError):
            observer(start_ms=1_000, end_ms=2_000)
    assert calls == [provider]
    assert budget.snapshot()["sealed_attempt_count"] == 1
    assert json.loads(receipt.read_bytes())["budget"]["pending_attempt_count"] == 1
    with pytest.raises(receipts.BudgetReceiptPersistenceError):
        observer(start_ms=1_000, end_ms=2_000)
    assert calls == [provider] and extracted == [(1_000, 2_000)]


def test_persistence_hook_without_budget_is_rejected_before_provider(tmp_path, monkeypatch):
    from src.autoslice import diarized_transcription as transcriber
    from src.autoslice import subtitle_audio_evidence as secondary
    monkeypatch.setattr(transcriber, "transcribe_evidence", lambda *a, **k: pytest.fail("provider"))
    with pytest.raises(ValueError, match="active source budget"):
        secondary.observe_secondary(
            b"synthetic", media_path=tmp_path / "unused.mp3", provider="mai",
            duration_ms=1_000, persist_before_dispatch=lambda: pytest.fail("persistence"),
        )
    assert not list(tmp_path.iterdir())


def test_different_native_consumers_share_one_durable_source_budget(tmp_path, monkeypatch):
    from src.autoslice import native_foreign_witness as native
    first, budget, receipt, calls, _ = _native_fixture(
        tmp_path, monkeypatch, "moss", max_audio_ms=2_000,
    )
    second = native.build_native_foreign_witness(
        source_media=tmp_path / "synthetic-source.bin", output_dir=tmp_path / "other-consumer",
        provider="mai", budget_receipt_path=receipt,
    )
    first(start_ms=1_000, end_ms=2_000)
    second(start_ms=1_000, end_ms=2_000)
    packet = json.loads(receipt.read_bytes())
    assert calls == ["moss", "mai"]
    assert packet["budget"] == budget.snapshot()
    assert packet["budget"]["attempt_count"] == 2
    assert packet["budget"]["total_audio_ms"] == 2_000
    assert packet["budget"]["distinct_window_count"] == 1
    assert packet["providers"] == ["mai", "moss"]
    assert not (tmp_path / "other-consumer/native-audio-budget.json").exists()
