"""Inventory-only special-file regressions; no real package or provider work."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.autoslice import b2_v7_recovery_successor as b2


REPO = Path(__file__).resolve().parents[1]
CHILD = r'''
import json, os, socket, subprocess, sys
from pathlib import Path
from src.autoslice import b2_v7_recovery_successor as b2

def forbidden(*args, **kwargs):
    raise AssertionError("inventory-only test cannot invoke preparation, media or network")
for name in ("preflight", "prepare", "project_documents", "load_native", "new_file", "new_json"):
    setattr(b2, name, forbidden)
socket.create_connection = forbidden
socket.socket.connect = forbidden
subprocess.run = forbidden
subprocess.Popen = forbidden

root, mode = Path(sys.argv[1]), sys.argv[2]
path = root / "attachment.txt"
open_impl, close_impl = os.open, os.close
opened, closed, raced = [], [], []

def observed_open(value, flags, *args, **kwargs):
    if Path(value) == path:
        if mode == "swap-to-fifo" and not raced:
            path.rename(root / "preserved-original.txt")
            os.mkfifo(path)
            raced.append(True)
        fd = open_impl(value, flags, *args, **kwargs)
        opened.append({"fd": fd, "flags": flags})
        return fd
    return open_impl(value, flags, *args, **kwargs)

def observed_close(fd):
    closed.append(fd)
    return close_impl(fd)

os.open, os.close = observed_open, observed_close
try:
    result = b2._source_inventory(root)
except Exception as exc:
    result = {"error_type": type(exc).__name__, "error": str(exc)}
print(json.dumps({"result": result, "opened": opened, "closed": closed, "raced": bool(raced)}))
'''


@pytest.fixture(autouse=True)
def forbid_full_preparation(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("this regression only calls the new inventory seam")
    for name in ("preflight", "prepare", "project_documents", "load_native", "new_file", "new_json"):
        monkeypatch.setattr(b2, name, forbidden)


def _run_child(root: Path, mode: str) -> dict:
    # Only this short, isolated Python child is allowed. A regression cannot hang
    # the test process: a blocked reader is killed and reaped before failing.
    proc = subprocess.Popen(
        [sys.executable, "-B", "-c", CHILD, str(root), mode],
        cwd=REPO, env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        stdout, stderr = proc.communicate(timeout=2)
    diagnostic = {"mode": mode, "timed_out": timed_out, "child_returncode": proc.returncode,
                  "child_reaped": proc.poll() is not None, "stdout": stdout.decode(),
                  "stderr": stderr.decode(), "scope": "SYNTHETIC_INVENTORY_ONLY"}
    (root.parent / (mode + ".diagnostic.json")).write_text(json.dumps(diagnostic, indent=2) + "\n")
    assert not timed_out, "inventory blocked on FIFO; isolated child was killed and reaped"
    assert proc.returncode == 0, diagnostic
    return json.loads(stdout)


@pytest.mark.parametrize("mode", ["existing-fifo", "swap-to-fifo"])
def test_fifo_is_rejected_without_blocking_or_descriptor_leak(tmp_path, mode):
    root = tmp_path / "synthetic-source"
    root.mkdir()
    path = root / "attachment.txt"
    if mode == "existing-fifo":
        os.mkfifo(path)
    else:
        path.write_bytes(b"preserved synthetic preimage")
    observed = _run_child(root, mode)
    assert observed["result"]["error_type"] == "ValueError"
    assert observed["result"]["error"] == "NOT_REGULAR:" + str(path)
    if mode == "existing-fifo":
        assert observed["opened"] == []
    else:
        assert observed["raced"] is True
        assert len(observed["opened"]) == 1
        assert observed["opened"][0]["flags"] & os.O_NONBLOCK
        assert observed["opened"][0]["fd"] in observed["closed"]
        assert (root / "preserved-original.txt").read_bytes() == b"preserved synthetic preimage"


def test_regular_inventory_keeps_exact_multichunk_bytes(tmp_path):
    root = tmp_path / "synthetic-source"
    root.mkdir()
    raw = b"\x00\xff\r\nsynthetic-bytes" * 66000
    path = root / "attachment.txt"
    path.write_bytes(raw)
    assert b2._source_inventory(root) == {"attachment.txt": {
        "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
    }}
    assert path.read_bytes() == raw


def test_changed_regular_file_still_trips_existing_drift_check(tmp_path, monkeypatch):
    root = tmp_path / "synthetic-source"
    root.mkdir()
    path = root / "attachment.txt"
    path.write_bytes(b"first synthetic bytes")
    original_read = os.read
    changed = []

    def drifting_read(fd, length):
        data = original_read(fd, length)
        if data and not changed:
            with path.open("ab") as stream:
                stream.write(b" changed during descriptor read")
            changed.append(True)
        return data

    with monkeypatch.context() as local:
        local.setattr(os, "read", drifting_read)
        with pytest.raises(ValueError, match="^INPUT_CHANGED_DURING_READ:"):
            b2._source_inventory(root)
    assert changed == [True]
