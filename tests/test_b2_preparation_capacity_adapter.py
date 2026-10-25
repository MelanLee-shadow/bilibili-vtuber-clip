"""New allocation adapter only: never run E428 preflight/prepare or media tests."""
from __future__ import annotations

import ast
from collections import namedtuple
import errno
import os
from pathlib import Path
import socket

import pytest

from src.autoslice import b2_v7_recovery_successor as successor
from src.autoslice import package_import_capacity as native
from src.autoslice.failed_pick_import import PackageImportError

GIB = 1024 ** 3
SCRATCH = 128 * 1024 ** 2
DiskUsage = namedtuple("DiskUsage", "total used free")


@pytest.fixture(autouse=True)
def no_preparation_or_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object):
        raise AssertionError("capacity adapter tests cannot prepare packages or call providers")

    for name in ("preflight", "prepare", "load_native", "new_file", "new_json"):
        monkeypatch.setattr(successor, name, forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _frozen() -> dict:
    # Only allocation metadata. No real record, video, subtitle or verdict exists.
    item = {key: key + ".json" for key in
            ("record", "publish_json", "chat_authority", "clip_context")}
    inventory = {name: {"bytes": size} for name, size in
                 zip(item.values(), (100, 200, 300, 400))}
    inventory["synthetic-media-size-only"] = {"bytes": GIB}
    return {"item": item, "inventory": inventory}


def _free(monkeypatch: pytest.MonkeyPatch, free: int) -> None:
    monkeypatch.setattr(native.shutil, "disk_usage",
                        lambda _path: DiskUsage(100 * GIB, 100 * GIB - free, free))


def test_configured_reserve_blocks_even_when_old_scratch_budget_would_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSLICE_MIN_FREE_BYTES", str(16 * GIB))
    _free(monkeypatch, 4 * GIB)
    target = tmp_path / "release-v7-e428-a1"
    with pytest.raises(PackageImportError) as failure:
        successor.preparation_capacity(_frozen(), target)
    assert failure.value.code == "INSUFFICIENT_DISK_SPACE"
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []
    assert os.environ["AUTOSLICE_MIN_FREE_BYTES"] == str(16 * GIB)


def test_budget_counts_history_scratch_and_configured_reserve_at_exact_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSLICE_MIN_FREE_BYTES", str(16 * GIB))
    budget = GIB + 2000 + SCRATCH
    _free(monkeypatch, budget + 16 * GIB)
    receipt = successor.preparation_capacity(_frozen(), tmp_path / "release-v7-e428-a1")
    assert receipt["source_copy_bytes"] == GIB + 1000
    assert receipt["additional_history_copy_bytes"] == 1000
    assert receipt["scratch_allowance_bytes"] == SCRATCH
    assert receipt["allocation_budget_bytes"] == budget
    assert len(receipt["volumes"]) == 1
    volume = receipt["volumes"][0]
    assert volume["planned_copy_bytes"] == budget
    assert volume["reserve_bytes"] == 16 * GIB
    assert volume["required_bytes"] == volume["available_bytes"] == budget + 16 * GIB
    assert "NOT_RESERVED_SPACE_OR_ACTUAL_WRITTEN_BYTES" in receipt["scope"]
    assert list(tmp_path.iterdir()) == []


def test_one_byte_below_combined_budget_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSLICE_MIN_FREE_BYTES", str(16 * GIB))
    _free(monkeypatch, GIB + 2000 + SCRATCH + 16 * GIB - 1)
    with pytest.raises(PackageImportError) as failure:
        successor.preparation_capacity(_frozen(), tmp_path / "release-v7-e428-a1")
    assert failure.value.code == "INSUFFICIENT_DISK_SPACE"
    assert list(tmp_path.iterdir()) == []


def test_native_unset_default_is_not_silently_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Synthetic environment only, not an alteration of the production reserve.
    monkeypatch.delenv("AUTOSLICE_MIN_FREE_BYTES", raising=False)
    _free(monkeypatch, GIB + 2000 + SCRATCH)
    receipt = successor.preparation_capacity(_frozen(), tmp_path / "release-v7-e428-a1")
    assert receipt["volumes"][0]["reserve_bytes"] == 0
    assert receipt["scratch_allowance_bytes"] == SCRATCH
    assert "AUTOSLICE_MIN_FREE_BYTES" not in os.environ


@pytest.mark.parametrize("value", ["-1", "invalid", "1.5"])
def test_invalid_reserve_uses_shared_error_without_disk_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    monkeypatch.setenv("AUTOSLICE_MIN_FREE_BYTES", value)

    def forbidden(_path: object):
        raise AssertionError("invalid policy must fail before probing capacity")

    monkeypatch.setattr(native.shutil, "disk_usage", forbidden)
    with pytest.raises(PackageImportError) as failure:
        successor.preparation_capacity(_frozen(), tmp_path / "release-v7-e428-a1")
    assert failure.value.code == "DISK_CAPACITY_POLICY_INVALID"
    assert os.environ["AUTOSLICE_MIN_FREE_BYTES"] == value
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("number", [errno.EACCES, errno.EIO])
def test_disk_failure_propagates_shared_typed_error_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, number: int,
) -> None:
    monkeypatch.setenv("AUTOSLICE_MIN_FREE_BYTES", str(16 * GIB))

    def unavailable(_path: object):
        raise OSError(number, "synthetic capacity probe failure")

    monkeypatch.setattr(native.shutil, "disk_usage", unavailable)
    with pytest.raises(PackageImportError) as failure:
        successor.preparation_capacity(_frozen(), tmp_path / "release-v7-e428-a1")
    assert failure.value.code == "DISK_CAPACITY_UNAVAILABLE"
    assert list(tmp_path.iterdir()) == []


def test_adapter_call_precedes_native_runtime_and_first_receipt_write() -> None:
    # Inspect placement; never invoke the original preparation body or its tests.
    tree = ast.parse(Path(successor.__file__).read_bytes())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "prepare")
    calls = [n for n in ast.walk(function) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)]
    lines = {name: min(n.lineno for n in calls if n.func.id == name)
             for name in ("preparation_capacity", "load_native", "new_json")}
    assert lines["preparation_capacity"] < lines["load_native"] < lines["new_json"]
