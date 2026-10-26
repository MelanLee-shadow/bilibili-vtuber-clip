"""Timestamp-colliding writes must be safe without per-caller fresh flags."""
import hashlib
import os
from pathlib import Path

import pytest

from src.autoslice import evidence_identity as identity


@pytest.fixture(autouse=True)
def empty_memo():
    identity._MEMO.clear()
    yield
    identity._MEMO.clear()


def _clock(monkeypatch, path, age_ns):
    info = path.stat()
    now = [max(info.st_mtime_ns, info.st_ctime_ns) + age_ns]
    monkeypatch.setattr(identity, "time_ns", lambda: now[0], raising=False)
    return now


def _reads(monkeypatch, target):
    opened = []
    original = Path.open

    def counted(path, mode="r", *args, **kwargs):
        if path == target and mode == "rb":
            opened.append(path)
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted)
    return opened


@pytest.mark.parametrize("reader", [identity.regular_file_sha256, identity.file_sha256])
@pytest.mark.parametrize("delay_ns", [0, 1_000_000_000])
def test_same_tick_rewrite_cannot_reuse_or_age_a_racy_digest(
    tmp_path, monkeypatch, reader, delay_ns,
):
    path = tmp_path / "mutable"
    path.write_bytes(b"before")
    before = path.stat()
    placement = identity._placement(before)
    monkeypatch.setattr(identity, "_placement", lambda _info: placement)
    now = _clock(monkeypatch, path, 0)
    assert reader(path) == hashlib.sha256(b"before").hexdigest()

    path.write_bytes(b"after!")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    now[0] += delay_ns
    assert reader(path) == hashlib.sha256(b"after!").hexdigest()


@pytest.mark.parametrize("age_ns,expected_reads", [
    (9_999_999, 2), (10_000_000, 2), (10_000_001, 1),
])
def test_cache_reuse_requires_an_entire_safe_interval(
    tmp_path, monkeypatch, age_ns, expected_reads,
):
    path = tmp_path / "boundary"
    path.write_bytes(b"bytes")
    _clock(monkeypatch, path, age_ns)
    reads = _reads(monkeypatch, path)
    expected = hashlib.sha256(b"bytes").hexdigest()
    assert identity.file_sha256(path) == expected
    assert identity.file_sha256(path) == expected
    assert len(reads) == expected_reads


def test_cold_input_hits_cache_and_fresh_still_bypasses_it(tmp_path, monkeypatch):
    path = tmp_path / "cold"
    path.write_bytes(b"stable")
    _clock(monkeypatch, path, 1_000_000_000)
    reads = _reads(monkeypatch, path)
    expected = hashlib.sha256(b"stable").hexdigest()
    for _ in range(20):
        assert identity.file_sha256(path) == expected
    assert len(reads) == 1
    assert identity.file_sha256(path, fresh=True) == expected
    assert len(reads) == 2
    assert identity.file_sha256(path) == expected
    assert len(reads) == 2


def test_backdated_mtime_does_not_hide_a_recent_ctime(tmp_path, monkeypatch):
    path = tmp_path / "restored-mtime"
    path.write_bytes(b"old")
    info = path.stat()
    old_mtime = info.st_mtime_ns - 10_000_000_000
    os.utime(path, ns=(info.st_atime_ns, old_mtime))
    placement = identity._placement(path.stat())
    monkeypatch.setattr(identity, "_placement", lambda _info: placement)
    _clock(monkeypatch, path, 0)
    assert identity.file_sha256(path) == hashlib.sha256(b"old").hexdigest()
    path.write_bytes(b"new")
    os.utime(path, ns=(info.st_atime_ns, old_mtime))
    assert identity.file_sha256(path) == hashlib.sha256(b"new").hexdigest()


def test_slow_hash_cannot_turn_a_racy_snapshot_into_a_cold_entry(tmp_path, monkeypatch):
    path = tmp_path / "slow"
    path.write_bytes(b"old")
    before = path.stat()
    placement = identity._placement(before)
    monkeypatch.setattr(identity, "_placement", lambda _info: placement)
    now = _clock(monkeypatch, path, 0)
    original = Path.open

    class SlowReader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, size):
            data = self.handle.read(size)
            if data:
                now[0] += 1_000_000_000
            return data

    def slow_open(target, mode="r", *args, **kwargs):
        handle = original(target, mode, *args, **kwargs)
        return SlowReader(handle) if target == path and mode == "rb" else handle

    monkeypatch.setattr(Path, "open", slow_open)
    assert identity.file_sha256(path) == hashlib.sha256(b"old").hexdigest()
    path.write_bytes(b"new")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert identity.file_sha256(path) == hashlib.sha256(b"new").hexdigest()


def test_clock_rollback_forces_a_read(tmp_path, monkeypatch):
    path = tmp_path / "rollback"
    path.write_bytes(b"unchanged")
    now = _clock(monkeypatch, path, 1_000_000_000)
    reads = _reads(monkeypatch, path)
    first = identity.file_sha256(path)
    now[0] -= 100_000_000
    assert identity.file_sha256(path) == first
    assert len(reads) == 2


def test_future_timestamp_is_never_memo_reused(tmp_path, monkeypatch):
    path = tmp_path / "future"
    path.write_bytes(b"unchanged")
    now = _clock(monkeypatch, path, 0)
    future = now[0] + 1_000_000_000
    os.utime(path, ns=(future, future))
    reads = _reads(monkeypatch, path)
    first = identity.file_sha256(path)
    assert identity.file_sha256(path) == first
    assert len(reads) == 2


def test_same_evidence_consumer_rejects_same_tick_content_drift(tmp_path, monkeypatch):
    path = tmp_path / "evidence"
    path.write_bytes(b"accepted")
    info = path.stat()
    placement = identity._placement(info)
    monkeypatch.setattr(identity, "_placement", lambda _info: placement)
    _clock(monkeypatch, path, 0)
    expected = hashlib.sha256(b"accepted").hexdigest()
    assert identity.same_evidence_file(path, expected, recorded_size=8,
                                       recorded_mtime_ns=info.st_mtime_ns)
    path.write_bytes(b"rejected")
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert not identity.same_evidence_file(path, expected, recorded_size=8,
                                           recorded_mtime_ns=info.st_mtime_ns)


def test_cold_aliases_reuse_digest_without_relaxing_regular_file_guard(tmp_path, monkeypatch):
    path = tmp_path / "regular"
    path.write_bytes(b"stable")
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    _clock(monkeypatch, path, 1_000_000_000)
    reads = _reads(monkeypatch, path)
    expected = identity.file_sha256(path)
    assert identity.file_sha256(alias) == expected
    assert len(reads) == 1
    assert identity.regular_file_sha256(alias) is None
    assert identity.regular_file_sha256(tmp_path) is None
    assert identity.regular_file_sha256(tmp_path / "missing") is None
