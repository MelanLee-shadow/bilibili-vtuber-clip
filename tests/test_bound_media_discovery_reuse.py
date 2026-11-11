"""Reuse positive search observations, without caching absence or authority."""
import hashlib
import os
from pathlib import Path

import pytest

from src.autoslice import bound_media_recovery as media


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def test_ordinary_native_parent_reuses_other_verified_search_members(tmp_path, monkeypatch):
    from tests.test_prepared_native_audio_recovery import _seed
    from src.autoslice.prepared_native_audio_recovery import inherit_prepared_native_audio
    from src.autoslice.supplement_audio_budget import start_budget, get_budget
    parent, source, _ = _seed(tmp_path)
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    state = inherit_prepared_native_audio(cid='new-member', parent=parent,
                                         source=source, runtime_root=tmp_path)
    before = {p: p.read_bytes() for p in state.cache_root.rglob('*') if p.is_file()}
    for p in (parent/'native-exact').rglob('input.mp3'):
        p.unlink()
    start_budget(source)
    scans = []
    copies = media.copies
    def searched(*args, **kwargs):
        scans.append(args[0])
        return copies(*args, **kwargs)
    monkeypatch.setattr(media, 'copies', searched)
    again = inherit_prepared_native_audio(cid='new-member', parent=parent,
                                         source=source, runtime_root=tmp_path)
    assert again == state
    assert get_budget(source).snapshot()['attempt_count'] == 2
    assert all(p.read_bytes() == raw for p, raw in before.items())
    # First missing MAI input discovers and verifies the distinct MOSS bytes.
    # The next ordinary caller must not repeat the same whole-tree search.
    assert len(scans) == 1


@pytest.mark.parametrize('damage', ['bytes', 'missing', 'symlink'])
def test_reused_positive_copy_is_revalidated_and_absence_is_not_cached(tmp_path, damage):
    copy = tmp_path/'other'/'input.mp3';copy.parent.mkdir();copy.write_bytes(b'good')
    expected = sha(b'good')
    assert media.resolve_bound_media(tmp_path/'missing-a'/'input.mp3', expected, root=tmp_path) == copy
    original = copy.stat()
    if damage == 'bytes':
        copy.write_bytes(b'evil');os.utime(copy,ns=(original.st_atime_ns,original.st_mtime_ns))
    elif damage == 'missing':copy.unlink()
    else:
        target=tmp_path/'untrusted.mp3';target.write_bytes(b'good')
        copy.unlink();copy.symlink_to(target)
    path=tmp_path/'missing-b'/'input.mp3'
    with pytest.raises(media.BoundMediaUnavailable):
        media.resolve_bound_media(path,expected,root=tmp_path)
    if copy.is_symlink():copy.unlink()
    copy.write_bytes(b'good')
    assert media.resolve_bound_media(path,expected,root=tmp_path)==copy


def test_positive_discovery_does_not_cross_search_roots(tmp_path):
    first,second=tmp_path/'first',tmp_path/'second'
    copy=first/'saved'/'input.mp3';copy.parent.mkdir(parents=True);copy.write_bytes(b'root bound')
    second.mkdir()
    expected=sha(copy.read_bytes())
    assert media.resolve_bound_media(first/'missing-a'/'input.mp3',expected,root=first)==copy
    with pytest.raises(media.BoundMediaUnavailable):
        media.resolve_bound_media(second/'missing-b'/'input.mp3',expected,root=second)


def test_positive_hit_replays_actual_file_dependency_into_outer_validation(tmp_path):
    from src.autoslice.evidence_identity import read_file_bytes
    from src.autoslice.validation_memo import memoized_validation
    copy=tmp_path/'saved'/'input.mp3';copy.parent.mkdir();copy.write_bytes(b'good')
    expected=sha(b'good')
    media.resolve_bound_media(tmp_path/'warm'/'input.mp3',expected,root=tmp_path)
    calls=[]
    @memoized_validation(lambda:'consumer')
    def consume():
        calls.append(True)
        return read_file_bytes(media.resolve_bound_media(tmp_path/'cold'/'input.mp3',expected,root=tmp_path)).decode()
    assert consume()=='good' and consume()=='good' and len(calls)==1
    info=copy.stat();copy.write_bytes(b'evil');os.utime(copy,ns=(info.st_atime_ns,info.st_mtime_ns))
    with pytest.raises(media.BoundMediaUnavailable):consume()
    assert len(calls)==2


@pytest.mark.skipif(os.geteuid() == 0, reason='root can read chmod000 directories')
@pytest.mark.parametrize('same_logical_path', [False, True])
def test_unreadable_cached_copy_falls_back_to_other_valid_bytes(tmp_path, same_logical_path):
    private=tmp_path/'private';private.mkdir()
    copy=private/'input.mp3';copy.write_bytes(b'good')
    expected=sha(b'good');first=tmp_path/'missing-a'/'input.mp3'
    assert media.resolve_bound_media(first,expected,root=tmp_path)==copy
    fallback=tmp_path/'visible'/'input.mp3';fallback.parent.mkdir();fallback.write_bytes(b'good')
    private.chmod(0)
    try:
        path=first if same_logical_path else tmp_path/'missing-b'/'input.mp3'
        assert media.resolve_bound_media(path,expected,root=tmp_path)==fallback
    finally:private.chmod(0o700)
