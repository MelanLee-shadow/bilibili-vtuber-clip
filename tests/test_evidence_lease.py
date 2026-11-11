from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

import pytest


def api():
    from src.autoslice import evidence_lease
    return evidence_lease


def issue(base, lease_id="input", **changes):
    fields = {"path": base / "out/old/source.mp4", "sha256": hashlib.sha256(b"media").hexdigest(),
              "holder": "four-repair", "purpose": "native-audio input", "until_utc": "2099-01-01T00:00:00Z"}
    fields.update(changes)
    return api().write_lease(base, lease_id, **fields)


def test_atomic_lease_write_read_and_expiry_boundary(tmp_path, monkeypatch):
    lease = api()
    destination = issue(tmp_path, until_utc="2026-10-24T00:00:00Z")
    before = destination.read_bytes()
    real_replace = os.replace
    observed = []

    def replace(source, target):
        assert Path(target).read_bytes() == before
        assert json.loads(Path(source).read_text())["holder"] == "administration"
        observed.append(Path(source))
        real_replace(source, target)

    monkeypatch.setattr(lease.os, "replace", replace)
    issue(tmp_path, holder="administration", until_utc="2026-10-24T00:00:00+00:00")
    expiry = datetime(2026, 10, 24, tzinfo=timezone.utc).timestamp()
    assert lease.read_leases(tmp_path, now=expiry - 1)["input"]["holder"] == "administration"
    assert lease.read_leases(tmp_path, now=expiry) == {}
    assert observed and not observed[0].exists()
    assert list(destination.parent.iterdir()) == [destination]


def test_failed_atomic_replace_preserves_previous_lease(tmp_path, monkeypatch):
    destination = issue(tmp_path)
    before = destination.read_bytes()

    def fail(*_):
        raise OSError("interrupted")

    monkeypatch.setattr(api().os, "replace", fail)
    with pytest.raises(OSError, match="interrupted"):
        issue(tmp_path, purpose="renewal")
    assert destination.read_bytes() == before
    assert list(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize("changes", [
    {"schema": "unknown"}, {"sha256": "bad"}, {"path": "relative.mp4"},
    {"holder": ""}, {"until_utc": "2026-10-24"}, {"until_utc": "2026-10-24T00:00:00-04:00"},
    {"until_utc": "not a time"}, {"extra": True},
])
def test_reader_rejects_invalid_lease(tmp_path, changes):
    path = issue(tmp_path)
    document = json.loads(path.read_text())
    document.update(changes)
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        api().read_leases(tmp_path)


def test_writer_rejects_path_escape_id_and_reader_rejects_symlink(tmp_path):
    with pytest.raises(ValueError, match="id"):
        issue(tmp_path, "../escape")
    path = issue(tmp_path)
    target = path.with_suffix(".saved")
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(ValueError, match="regular"):
        api().read_leases(tmp_path)


@pytest.mark.parametrize('shape', ['spec', 'commands'])
def test_ordinary_binding_issue_persist_collect_apply_and_expiry(tmp_path, shape, monkeypatch):
    from src.autoslice import runtime_gc as gc
    from scripts.scan_state_dangling_media_refs import main
    base = tmp_path / 'runtime'
    media = base / 'private-runs/old/input.mp4'
    media.parent.mkdir(parents=True)
    media.write_bytes(b'media')
    other = media.with_name('unbound.wav')
    other.write_bytes(b'media')
    source = base / 'state' / ('SPEC.json' if shape == 'spec' else 'COMMANDS.md')
    source.parent.mkdir()
    parent = source.with_name('parent.json')
    parent.write_text(json.dumps({'source_media': '../private-runs/old/input.mp4',
                                  'source_media_sha256': hashlib.sha256(b'media').hexdigest()}))
    document = {'inputs': [{'path': str(parent)}], 'runtime_root': str(base),
                'output_root': str(media.parent), 'note': str(other)}
    source.write_text(json.dumps(document) if shape == 'spec' else
                      'Prose ' + str(other) + '\n```json\n' + json.dumps(document) + '\n```\n')
    old = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
    for path in (media, other, media.parent, media.parent.parent):
        os.utime(path, (old, old))
    options = dict(home=tmp_path / 'none', live=gc.Activity(), free_bytes=30 * gc.GIB)
    assert {r['path'] for r in gc.collect(base, **options)['targets']} == {str(media), str(other)}
    assert main(['--base', str(base), '--issue-source', str(source), '--holder', 'new-owner',
                 '--purpose', 'new work input', '--until-utc', '2099-01-01T00:00:00Z']) == 0
    leases = api().read_leases(base)
    assert any(row['path'] == str(media) for row in leases.values())
    assert not any(row['path'] in {str(base), str(media.parent), str(other)} for row in leases.values())
    approved = gc.collect(base, preserve={str(media), str(other)}, **options)
    assert {row['classification'] for row in approved['preserve_classification']} == {'covered-by-lease', 'no-inflight-binding'}
    approved = gc.collect(base, **options)
    assert {r['path'] for r in approved['targets']} == {str(other)}
    monkeypatch.setattr(gc, 'activity', lambda *_: gc.Activity())
    assert gc.apply_plan(approved)['removed'] == [str(other)]
    assert media.exists()
    expired = gc.collect(base, now=datetime(2100, 1, 1, tzinfo=timezone.utc).timestamp(), **options)
    assert {r['path'] for r in expired['targets']} == {str(media)}


def test_lease_lookup_does_not_resolve_each_media_or_cross_sibling_prefix(tmp_path, monkeypatch):
    lease = {'path': str(tmp_path / 'c1')}
    index = api().LeaseIndex({'owner': lease})
    def forbidden(*_args, **_kwargs):
        raise AssertionError('media lookup must not stat/resolve')
    monkeypatch.setattr(Path, 'resolve', forbidden)
    assert index.covering(tmp_path / 'c1/source.mp4') == ['owner']
    assert index.covering(tmp_path / 'c10/source.mp4') == []


def test_shared_declaration_is_hashed_once_for_many_scalar_inputs(tmp_path, monkeypatch):
    source = tmp_path / 'SPEC.json'
    source.write_text(json.dumps({'inputs': [f'./media-{index}.mp4' for index in range(50)]}))
    read = Path.read_bytes
    reads = []
    def observed(path):
        reads.append(path)
        return read(path)
    monkeypatch.setattr(Path, 'read_bytes', observed)
    leases = api().derive(tmp_path, [source], holder='owner', purpose='inputs',
                          until_utc='2099-01-01T00:00:00Z')
    assert len(leases) == 51
    assert reads == [source]


def test_digest_cache_binds_bytes_seen_by_each_selected_source(tmp_path, monkeypatch):
    shared = tmp_path / 'parent.json'
    first, second = tmp_path / 'first.json', tmp_path / 'second.json'
    for source in (first, second):
        source.write_text(json.dumps({'inputs': [{'path': str(shared)}]}))
    old = json.dumps({'inputs': ['./old.mp4']}).encode()
    new = json.dumps({'inputs': ['./new.mp4']}).encode()
    shared.write_bytes(old)
    read = Path.read_bytes
    def rewrite(path):
        if path == second:
            shared.write_bytes(new)
        return read(path)
    monkeypatch.setattr(Path, 'read_bytes', rewrite)
    leases = api().derive(tmp_path, [first, second], holder='owner', purpose='inputs', until_utc='2099-01-01T00:00:00Z')
    observed = {row['sha256'] for row in leases.values() if row['path'] == str(shared)}
    assert observed == {hashlib.sha256(old).hexdigest(), hashlib.sha256(new).hexdigest()}


def test_media_cannot_be_used_as_a_declaration_to_trigger_hashing(tmp_path):
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'media')
    with pytest.raises(ValueError, match='structured declaration'):
        api().derive(tmp_path, [media], holder='owner', purpose='inputs', until_utc='2099-01-01T00:00:00Z')


def test_external_stage_declaration_protects_external_old_clone_without_reading_it(tmp_path, monkeypatch):
    from src.autoslice import runtime_gc as gc
    base = tmp_path / 'runtime'
    base.mkdir()
    media = tmp_path / 'home/vtuber-old/input.mp4'
    media.parent.mkdir(parents=True)
    media.write_bytes(b'media')
    stamp = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
    for path in (media, media.parent):
        os.utime(path, (stamp, stamp))
    source = tmp_path / 'administrative/request.json'
    source.parent.mkdir()
    source.write_text(json.dumps({'schema_version': 'same-bv-stage-request.v1',
                                  'inputs': [{'path': str(media)}]}))
    options = dict(home=tmp_path / 'home', live=gc.Activity(), free_bytes=30 * gc.GIB)
    assert {row['path'] for row in gc.collect(base, **options)['targets']} == {str(media)}
    original = Path.read_bytes
    def no_external_content(path):
        assert path != media
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', no_external_content)
    api().issue_from_sources(base, [source], holder='stage-owner', purpose='inputs', until_utc='2099-01-01T00:00:00Z')
    assert not gc.collect(base, **options)['targets']


@pytest.mark.parametrize('mode', ['inplace-invalid', 'alias-invalid', 'atomic-renew', 'expiry', 'fallback'])
def test_snapshot_guard_detects_changes_and_preserves_fallback(tmp_path, monkeypatch, mode):
    lease = api()
    path = issue(tmp_path)
    if mode == 'alias-invalid':
        alias = tmp_path / 'alias.json'
        os.link(path, alias)
        path = alias
    expected = lease.read_leases(tmp_path)
    guard = lease.SnapshotGuard(tmp_path, expected)
    try:
        assert guard.unchanged()
        if mode == 'fallback':
            guard.close()
            issue(tmp_path, holder='new owner')
            assert not guard.unchanged()
        elif mode == 'atomic-renew':
            issue(tmp_path, until_utc='2099-02-01T00:00:00Z')
            assert not guard.unchanged()
        elif mode == 'expiry':
            monkeypatch.setattr(lease.time, 'time', lambda: datetime(2100, 1, 1, tzinfo=timezone.utc).timestamp())
            assert not guard.unchanged()
        else:
            path.write_text('{}')
            with pytest.raises(ValueError, match='schema'):
                guard.unchanged()
    finally:
        guard.close()


@pytest.mark.skipif(not __import__('sys').platform.startswith('linux'), reason='Linux filesystem events')
def test_apply_does_not_reopen_all_leases_for_each_target(tmp_path, monkeypatch):
    from src.autoslice import runtime_gc as gc
    lease = api()
    issue(tmp_path)
    files = []
    stamp = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
    for index in range(20):
        path = tmp_path / 'private-runs/idle' / f'{index}.mp4'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'media')
        os.utime(path, (stamp, stamp))
        files.append(path)
    os.utime(files[0].parent, (stamp, stamp))
    monkeypatch.setattr(gc, 'activity', lambda *_: gc.Activity())
    approved = gc.collect(tmp_path, home=tmp_path / 'none', free_bytes=30 * gc.GIB)
    reader, reads = lease.read_leases, []
    def observed(*args, **kwargs):
        reads.append(True)
        return reader(*args, **kwargs)
    monkeypatch.setattr(lease, 'read_leases', observed)
    result = gc.apply_plan(approved)
    assert result['status'] == 'APPLIED' and len(result['removed']) == 20
    assert len(reads) <= 3
