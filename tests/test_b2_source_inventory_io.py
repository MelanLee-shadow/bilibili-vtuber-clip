"""Inventory-only regressions on synthetic files, never B2 preparation or media."""

import errno
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import urllib.request

import pytest

from src.autoslice import b2_v7_recovery_successor as b2


@pytest.fixture(autouse=True)
def inventory_only(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("inventory cases must not prepare packages, write outputs, or call providers")

    # These are explicitly outside this test's scope, including the old refused suite.
    for name in ("preflight", "prepare", "project_documents", "load_native",
                 "new_file", "new_json", "replace_owned"):
        monkeypatch.setattr(b2, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.fixture
def source_tree(tmp_path):
    root = tmp_path / "synthetic-input"
    contents = {
        "plain.txt": b"synthetic root file\n",
        "evidence/other.txt": b"synthetic attachment\n",
        "evidence/deep/receipt.txt": b"synthetic nested receipt\n",
    }
    for relative, raw in contents.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    return root, contents


@pytest.mark.parametrize("location", ["root", "nested"])
@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO, errno.ENOENT],
                         ids=["permission", "io-error", "vanished-directory"])
def test_incomplete_enumeration_raises_original_error(source_tree, monkeypatch,
                                                       location, error_number):
    root, contents = source_tree
    unreadable = root if location == "root" else root / "evidence" / "deep"
    problem = OSError(error_number, "synthetic enumeration failure", str(unreadable))
    scandir = os.scandir
    encountered = []

    def injected(path):
        if Path(path) == unreadable:
            encountered.append(str(path))
            raise problem
        return scandir(path)

    with monkeypatch.context() as local:
        local.setattr(os, "scandir", injected)
        with pytest.raises(OSError) as raised:
            b2._source_inventory(root)
    assert raised.value is problem
    assert raised.value.errno == error_number
    assert raised.value.filename == str(unreadable)
    assert encountered == [str(unreadable)]
    assert all((root / rel).read_bytes() == raw for rel, raw in contents.items())


def test_complete_inventory_preserves_nested_raw_bindings(source_tree):
    root, contents = source_tree
    result = b2._source_inventory(root)
    assert result == {
        rel: {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        for rel, raw in contents.items()
    }
    assert all((root / rel).read_bytes() == raw for rel, raw in contents.items())


def test_existing_directory_symlink_rejection_is_preserved(source_tree):
    root, contents = source_tree
    (root / "alias").symlink_to(root / "evidence", target_is_directory=True)
    with pytest.raises(ValueError, match="^SOURCE_DIRECTORY_SYMLINK$"):
        b2._source_inventory(root)
    assert (root / "alias").is_symlink()
    assert all((root / rel).read_bytes() == raw for rel, raw in contents.items())
