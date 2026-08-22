from __future__ import annotations

import multiprocessing
import os
import time
from pathlib import Path

import pytest

from src.autoslice import llm_client
from src.autoslice import qixi_post_correction_public_surface as qixi
from src.autoslice import provider_slots
from src.autoslice.provider_slots import ProviderSlotError, ProviderSlotTimeout, provider_slot, provider_wait_seconds
from src.autoslice.qixi_transaction_core import exclusive_runner_commit


def _hold_slot(root: str, release: object, result: object) -> None:
    try:
        with provider_slot(Path(root)) as lease:
            result.put(("held", lease.slot_index))
            release.wait(10)
    except BaseException as exc:
        result.put(("error", type(exc).__name__))


def _wait_for_slot(root: str, entered: object, result: object) -> None:
    try:
        entered.set()
        with provider_slot(Path(root), timeout_seconds=5, poll_seconds=0.01):
            result.put("acquired")
    except BaseException as exc:
        result.put(type(exc).__name__)


def test_process_pool_waits_at_capacity_then_acquires_without_blocking_runner_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOSLICE_PROVIDER_CONCURRENCY", "2")
    context = multiprocessing.get_context("spawn")
    release = context.Event()
    results = context.Queue()
    workers = [context.Process(target=_hold_slot, args=(str(tmp_path), release, results)) for _ in range(2)]
    for worker in workers:
        worker.start()
    held = [results.get(timeout=10), results.get(timeout=10)]
    assert {state for state, _slot in held} == {"held"}
    assert {slot for _state, slot in held} == {0, 1}

    # Provider work deliberately happens outside the short state/journal mutex.
    with exclusive_runner_commit(tmp_path):
        pass

    contender_entered = context.Event()
    contender = context.Process(target=_wait_for_slot, args=(str(tmp_path), contender_entered, results))
    contender.start()
    assert contender_entered.wait(5)
    time.sleep(0.15)
    assert contender.is_alive()
    release.set()
    assert results.get(timeout=10) == "acquired"
    contender.join(timeout=10)
    assert contender.exitcode == 0
    release.set()
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0


def test_provider_permit_releases_after_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOSLICE_PROVIDER_CONCURRENCY", "1")
    with pytest.raises(RuntimeError, match="boom"):
        with provider_slot(tmp_path):
            raise RuntimeError("boom")
    with provider_slot(tmp_path) as lease:
        assert lease.slot_index == 0


@pytest.mark.parametrize("value", ["0", "6", "not-an-int"])
def test_provider_capacity_env_is_strict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("AUTOSLICE_PROVIDER_CONCURRENCY", value)
    with pytest.raises(ProviderSlotError, match="1 to 5"):
        with provider_slot(tmp_path):
            pass


@pytest.mark.parametrize("value", ["0", "901", "not-an-int"])
def test_provider_wait_env_is_strict(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("AUTOSLICE_PROVIDER_WAIT_SECONDS", value)
    with pytest.raises(ProviderSlotError, match="1 to 900"):
        provider_wait_seconds()


def test_command_adapter_and_qixi_default_share_one_runtime_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    monkeypatch.setenv("AUTOSLICE_PROVIDER_CONCURRENCY", "1")
    monkeypatch.setenv("AUTOSLICE_PROVIDER_WAIT_SECONDS", "1")
    monkeypatch.setattr(llm_client, "_call_command", lambda _prompt, _config: "ok")
    config = llm_client.LlmConfig(transport="command", command_template="ignored")
    direct_command = llm_client.build_llm_call(config)
    qixi_default = qixi._default_source_fact_llm()

    with provider_slot(tmp_path):
        with pytest.raises(llm_client.LlmCallError, match="provider capacity wait timed out"):
            direct_command("one")
        with pytest.raises(llm_client.LlmCallError, match="provider capacity wait timed out"):
            qixi_default("two")
    assert direct_command("one") == "ok"
    assert qixi_default("two") == "ok"


def test_provider_slot_refuses_symlink_directory(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "provider-slots").symlink_to(target, target_is_directory=True)
    with pytest.raises(ProviderSlotError, match="unsafe"):
        with provider_slot(tmp_path):
            pass


def test_provider_slot_refuses_symlinked_runtime_parent(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    runtime = real_parent / "runtime"
    runtime.mkdir(parents=True)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ProviderSlotError, match="runtime root is unsafe"):
        with provider_slot(linked_parent / "runtime"):
            pass


def test_provider_slot_refuses_symlinked_slot_file(tmp_path: Path) -> None:
    slots = tmp_path / "provider-slots"
    slots.mkdir(mode=0o700)
    target = tmp_path / "foreign.lock"
    target.write_bytes(b"")
    os.chmod(target, 0o600)
    (slots / "slot-0.lock").symlink_to(target)
    with pytest.raises(ProviderSlotError, match="opened safely"):
        with provider_slot(tmp_path):
            pass


def test_provider_slot_rejects_post_flock_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot = tmp_path / "provider-slots" / "slot-0.lock"
    real_flock = provider_slots.fcntl.flock
    replaced = False

    def replace_after_lock(descriptor: int, operation: int) -> None:
        nonlocal replaced
        real_flock(descriptor, operation)
        if not replaced and operation & provider_slots.fcntl.LOCK_EX:
            replaced = True
            slot.unlink()
            slot.write_bytes(b"replacement")
            os.chmod(slot, 0o600)

    monkeypatch.setattr(provider_slots.fcntl, "flock", replace_after_lock)
    with pytest.raises(ProviderSlotError, match="ownership drifted"):
        with provider_slot(tmp_path):
            pass


def test_provider_slot_timeout_is_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOSLICE_PROVIDER_CONCURRENCY", "1")
    with provider_slot(tmp_path):
        with pytest.raises(ProviderSlotTimeout, match="wait timed out"):
            with provider_slot(tmp_path, timeout_seconds=0.01, poll_seconds=0.001):
                pass
