from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from src.autoslice import llm_client
from src.autoslice import qixi_post_correction_public_surface as qixi
from src.autoslice.provider_slots import ProviderSlotBusy, ProviderSlotError, provider_slot
from src.autoslice.qixi_transaction_core import exclusive_runner_commit


def _hold_slot(root: str, ready: object, release: object, result: object) -> None:
    try:
        with provider_slot(Path(root)) as lease:
            result.put(("held", lease.slot_index))
            ready.wait(10)
            release.wait(10)
    except BaseException as exc:
        result.put(("error", type(exc).__name__))


def _try_slot(root: str, result: object) -> None:
    try:
        with provider_slot(Path(root)):
            result.put("acquired")
    except ProviderSlotBusy:
        result.put("busy")
    except BaseException as exc:
        result.put(type(exc).__name__)


def test_process_pool_caps_at_two_and_does_not_block_runner_commit(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    results = context.Queue()
    workers = [context.Process(target=_hold_slot, args=(str(tmp_path), ready, release, results)) for _ in range(2)]
    for worker in workers:
        worker.start()
    held = [results.get(timeout=10), results.get(timeout=10)]
    assert {state for state, _slot in held} == {"held"}
    assert {slot for _state, slot in held} == {0, 1}

    # Provider work deliberately happens outside the short state/journal mutex.
    with exclusive_runner_commit(tmp_path):
        pass

    contender = context.Process(target=_try_slot, args=(str(tmp_path), results))
    contender.start()
    assert results.get(timeout=10) == "busy"
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


def test_command_adapter_and_qixi_default_share_one_runtime_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    monkeypatch.setenv("AUTOSLICE_PROVIDER_CONCURRENCY", "1")
    monkeypatch.setattr(llm_client, "_call_command", lambda _prompt, _config: "ok")
    config = llm_client.LlmConfig(transport="command", command_template="ignored")
    direct_command = llm_client.build_llm_call(config)
    qixi_default = qixi._default_source_fact_llm()

    with provider_slot(tmp_path):
        with pytest.raises(llm_client.LlmCallError, match="provider capacity is busy"):
            direct_command("one")
        with pytest.raises(llm_client.LlmCallError, match="provider capacity is busy"):
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
