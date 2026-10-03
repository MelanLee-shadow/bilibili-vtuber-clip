"""Only shared capacity ancestor I/O: no real package, pipeline run or network."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from src.autoslice import package_import_capacity as capacity
from src.autoslice.failed_pick_import import PackageImportError


@pytest.fixture(autouse=True)
def no_network_or_preparation(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_a: object, **_k: object):
        raise AssertionError("ancestor tests cannot access network or prepare a package")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setenv("AUTOSLICE_MIN_FREE_BYTES", str(16 * 1024**3))


def _entry(destination: Path, status: str = "WOULD_COPY") -> dict:
    return {"destination": str(destination), "bytes": 1024, "status": status}


def _probe_log(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls = []
    def probe(path):
        calls.append(Path(path))
        return SimpleNamespace(free=32 * 1024**3)
    monkeypatch.setattr(capacity.shutil, "disk_usage", probe)
    return calls


def _stat_error(monkeypatch: pytest.MonkeyPatch, denied: Path, number: int) -> list[Path]:
    original = os.stat
    calls = []
    def stat_path(path, *args, **kwargs):
        if not isinstance(path, int) and Path(os.fsdecode(os.fspath(path))) == denied:
            calls.append(denied)
            raise OSError(number, "synthetic ancestor metadata failure", str(denied))
        return original(path, *args, **kwargs)
    # Patch the OS seam used by both Path.exists and Path.stat across Python versions.
    monkeypatch.setattr(os, "stat", stat_path)
    return calls


@pytest.mark.parametrize("number", [errno.EACCES, errno.EIO])
@pytest.mark.parametrize("nested_missing", [False, True])
def test_unreadable_ancestor_never_falls_back_to_a_different_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    number: int, nested_missing: bool,
) -> None:
    target_volume = tmp_path / "target-volume"
    target_volume.mkdir()
    destination = target_volume / "missing" / "item.bin" if nested_missing else target_volume / "item.bin"
    probes = _probe_log(monkeypatch)
    seen = _stat_error(monkeypatch, target_volume, number)
    with pytest.raises(PackageImportError) as error:
        capacity.copy_capacity([_entry(destination)])
    assert error.value.code == "DISK_CAPACITY_UNAVAILABLE"
    assert isinstance(error.value.__cause__, OSError)
    assert error.value.__cause__.errno == number
    assert error.value.__cause__.filename == str(target_volume)
    assert seen == [target_volume]
    assert probes == []


def test_missing_parents_still_find_the_actual_existing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes = _probe_log(monkeypatch)
    result = capacity.copy_capacity([_entry(tmp_path / "not-yet" / "created" / "item.bin")])
    assert probes == [tmp_path]
    assert result[0]["path"] == str(tmp_path)
    assert result[0]["required_bytes"] == 16 * 1024**3 + 1024
    assert not (tmp_path / "not-yet").exists()


def test_existing_regular_file_is_not_a_destination_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_bytes(b"unchanged")
    probes = _probe_log(monkeypatch)
    with pytest.raises(PackageImportError) as error:
        capacity.copy_capacity([_entry(parent_file / "item.bin")])
    assert error.value.code == "DISK_CAPACITY_UNAVAILABLE"
    assert error.value.__cause__.errno == errno.ENOTDIR
    assert probes == []
    assert parent_file.read_bytes() == b"unchanged"


def test_later_unreadable_parent_aborts_before_any_volume_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    good, denied = tmp_path / "good", tmp_path / "denied"
    good.mkdir()
    denied.mkdir()
    probes = _probe_log(monkeypatch)
    _stat_error(monkeypatch, denied, errno.EACCES)
    with pytest.raises(PackageImportError) as error:
        capacity.copy_capacity([_entry(good / "a"), _entry(denied / "b")])
    assert error.value.code == "DISK_CAPACITY_UNAVAILABLE"
    assert probes == []


def test_no_planned_write_does_not_inspect_its_unreadable_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    denied = tmp_path / "denied"
    denied.mkdir()
    probes = _probe_log(monkeypatch)
    seen = _stat_error(monkeypatch, denied, errno.EIO)
    assert capacity.copy_capacity([_entry(denied / "item", "ALREADY_IDENTICAL")]) == []
    assert probes == [] and seen == []


def test_missing_root_cannot_loop_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    original = os.stat
    count = 0
    def stat_path(path, *args, **kwargs):
        nonlocal count
        if not isinstance(path, int) and Path(os.fsdecode(os.fspath(path))) == Path("/"):
            count += 1
            if count > 3:
                raise RuntimeError("synthetic iteration bound: would loop at filesystem root")
            raise FileNotFoundError(errno.ENOENT, "synthetic unavailable root", "/")
        return original(path, *args, **kwargs)
    probes = _probe_log(monkeypatch)
    monkeypatch.setattr(os, "stat", stat_path)
    with pytest.raises(PackageImportError) as error:
        capacity.copy_capacity([_entry(Path("/synthetic-capacity-item"))])
    assert error.value.code == "DISK_CAPACITY_UNAVAILABLE"
    assert error.value.__cause__.errno == errno.ENOENT
    assert count == 1 and probes == []
