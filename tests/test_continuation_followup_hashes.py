"""File-only hash readers share one stable digest, without relaxing guards."""
import hashlib
import os
from pathlib import Path

import pytest

from src.autoslice import evidence_identity as identity


def _read_counter(monkeypatch, target):
    opened = []
    original = Path.open

    def tracked(path, mode='r', *args, **kwargs):
        if path.resolve() == target.resolve() and mode == 'rb':
            opened.append(path)
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', tracked)
    return opened


def test_cold_read_hashes_argon_and_sibling_readers_once(tmp_path, monkeypatch):
    from src.autoslice.argon_escalation_contract import file_sha
    from src.autoslice.existing_subtitle_surface import _digest
    from src.autoslice.nested_source_cache import _sha
    from src.autoslice.continuation_subchecks import cold_read_session

    source = tmp_path / 'padded.wav'
    content = b'unchanged source' * 4096
    source.write_bytes(content)
    # Model a genuinely cold input, beyond the filesystem write-clock window.
    cold_now = max(source.stat().st_mtime_ns, source.stat().st_ctime_ns) + 1_000_000_000
    monkeypatch.setattr(identity, 'time_ns', lambda: cold_now)
    expected = hashlib.sha256(content).hexdigest()
    reads = _read_counter(monkeypatch, source)
    with cold_read_session():
        for _ in range(9):
            assert file_sha(source) == expected
            assert _digest(source) == 'sha256:' + expected
            assert _sha(source) == expected
    assert len(reads) == 1


@pytest.mark.parametrize('change', ['inode', 'size', 'mtime'])
def test_hash_cache_invalidates_each_placement_change(tmp_path, monkeypatch, change):
    from src.autoslice.argon_escalation_contract import file_sha
    source = tmp_path / 'source'
    source.write_bytes(b'abc')
    reads = _read_counter(monkeypatch, source)
    assert file_sha(source) == hashlib.sha256(b'abc').hexdigest()
    before = source.stat()
    if change == 'inode':
        replacement = tmp_path / 'replacement'
        replacement.write_bytes(b'xyz')
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, source)
        expected = b'xyz'
    elif change == 'size':
        source.write_bytes(b'abcd')
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        expected = b'abcd'
    else:
        source.write_bytes(b'xyz')
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        expected = b'xyz'
    assert file_sha(source) == hashlib.sha256(expected).hexdigest()
    assert len(reads) == 2


def test_guarded_argon_refuses_symlink_but_ordinary_hash_keeps_its_old_input_domain(tmp_path):
    from src.autoslice.argon_escalation_contract import file_sha
    source = tmp_path / 'source'
    source.write_bytes(b'abc')
    link = tmp_path / 'link'
    link.symlink_to(source)
    assert identity.regular_file_sha256(link) is None
    with pytest.raises(ValueError, match='SOURCE_FILE_UNSAFE'):
        file_sha(link)
    assert identity.file_sha256(link) == hashlib.sha256(b'abc').hexdigest()


def test_descriptor_attestations_keep_their_checks_without_rehashing(tmp_path, monkeypatch):
    from src.autoslice.native_audio_budget_receipt import _source_sha256_stable
    from src.autoslice.retained_reviewed_draft import _media_sha256

    source = tmp_path / 'source'
    source.write_bytes(b'abc')
    expected = hashlib.sha256(b'abc').hexdigest()
    cold_now = max(source.stat().st_mtime_ns, source.stat().st_ctime_ns) + 1_000_000_000
    monkeypatch.setattr(identity, 'time_ns', lambda: cold_now)
    original = identity.hashlib.sha256
    hashes = []

    def counted(*args, **kwargs):
        hashes.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(identity.hashlib, 'sha256', counted)
    for _ in range(5):
        assert _source_sha256_stable(source, label='offline') == expected
        assert _media_sha256(source, label='offline') == expected
    assert len(hashes) == 1


def test_changed_during_hash_is_rejected_and_not_memoized(tmp_path, monkeypatch):
    from src.autoslice.argon_escalation_contract import file_sha
    source = tmp_path / 'source'
    source.write_bytes(b'abc')
    original = Path.open
    changed = False

    class Reader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, size):
            nonlocal changed
            data = self.handle.read(size)
            if data and not changed:
                changed = True
                # A deterministic size change avoids timestamp tick races.
                with original(source, 'ab') as writer:
                    writer.write(b'd')
            return data

    def mutate(path, mode='r', *args, **kwargs):
        handle = original(path, mode, *args, **kwargs)
        return Reader(handle) if path == source and mode == 'rb' else handle

    monkeypatch.setattr(Path, 'open', mutate)
    with pytest.raises(ValueError, match='SOURCE_CHANGED_DURING_READ'):
        file_sha(source)
    monkeypatch.setattr(Path, 'open', original)
    assert file_sha(source) == hashlib.sha256(b'abcd').hexdigest()
