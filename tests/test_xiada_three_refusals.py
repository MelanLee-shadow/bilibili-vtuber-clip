"""Ordinary range/recut provenance survives GC only through original bound bytes."""
import json
from pathlib import Path
import shutil

import pytest

from src.autoslice import targeted_relisten as relisten
from tests import test_targeted_relisten_source_identity as fixture
from tests.test_final_recut_range_source_binding import package


def retire_pieces(root):
    copies = root / 'retained-copy'
    copies.mkdir()
    for path in root.glob('piece-*.mp4'):
        shutil.copyfile(path, copies / path.name)
        path.unlink()


@pytest.mark.parametrize('provider', ['gemini', 'mai', 'moss'])
@pytest.mark.parametrize('kind', ['padded', 'padded_chain', 'final', 'final_chain'])
def test_ordinary_producer_persistence_gc_and_cold_consumer(tmp_path, monkeypatch, kind, provider):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    media, _, check, _, raw, _, calls, _ = fixture.make_case(tmp_path, monkeypatch, kind, provider)
    path = tmp_path / 'saved-observation.json'
    path.write_text(json.dumps(raw))
    original = relisten.evidence_projection(check, ranges=[raw])
    assert original['effective_distinct_observations'] == 1
    immutable = {p: p.read_bytes() for p in tmp_path.rglob('*.json')}
    retire_pieces(tmp_path)
    # Also remove the delivered medium and final-recut's padded parent. The
    # immutable sidecars stay at their original paths; only bytes relocate.
    retained = tmp_path / 'retained-copy'
    for artifact in dict.fromkeys([media, tmp_path / 'padded.mp4']):
        if artifact.exists():
            shutil.copyfile(artifact, retained / artifact.name)
            artifact.unlink()
    current = relisten.evidence_projection(check, ranges=[json.loads(path.read_text())])
    assert current == original, current['rejected_observations']
    assert len(calls) == 1 and not media.exists()
    assert all(p.read_bytes() == before for p, before in immutable.items())
    from src.autoslice import bound_media_recovery
    resolve = bound_media_recovery.resolve_bound_media
    with monkeypatch.context() as old:
        old.setattr(bound_media_recovery, 'resolve_bound_media',
            lambda p, *a, **kw: Path(p) if Path(p).name.startswith('piece-') else resolve(p, *a, **kw))
        rejected = relisten.evidence_projection(check, ranges=[raw])
        assert rejected['effective_distinct_observations'] == 0


def test_distinct_equal_byte_pieces_can_share_one_recovered_copy(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    write = Path.write_bytes
    def identical(path, data):
        if path.is_relative_to(tmp_path) and path.name in {'piece-0.mp4', 'piece-1.mp4'}:
            data = b'same synthetic encoded silence for distinct recording windows'
        return write(path, data)
    with monkeypatch.context() as producer:
        producer.setattr(Path, 'write_bytes', identical)
        _, _, check, _, raw, _, calls, _ = fixture.make_case(tmp_path, monkeypatch, 'final_chain')
        donor = tmp_path / 'ordinary-single-piece-run'
        donor.mkdir()
        package(donor, 1)
    expected = relisten.evidence_projection(check, ranges=[raw])
    assert expected['effective_distinct_observations'] == 1
    # The other ordinary single-piece run advertises these same encoded bytes.
    # Both old logical windows may resolve to its one physical copy.
    for path in tmp_path.glob('piece-*.mp4'):
        path.unlink()
    assert relisten.evidence_projection(check, ranges=[raw]) == expected
    assert len(calls) == 1


@pytest.mark.parametrize('missing_index', [0, 1])
@pytest.mark.parametrize('location', ['original', 'hardlink'])
def test_recovered_piece_cannot_alias_an_existing_logical_piece(tmp_path, monkeypatch, missing_index, location):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    run = tmp_path / 'ordinary-run'
    run.mkdir()
    write = Path.write_bytes
    def identical(path, data):
        if path.parent == run and path.name in {'piece-0.mp4', 'piece-1.mp4'}:
            data = b'same encoded bytes for different recording windows'
        return write(path, data)
    with monkeypatch.context() as producer:
        producer.setattr(Path, 'write_bytes', identical)
        _, _, check, _, raw, _, calls, _ = fixture.make_case(run, monkeypatch, 'final_chain')
    assert relisten.evidence_projection(check, ranges=[raw])['effective_distinct_observations'] == 1
    missing = run / f'piece-{missing_index}.mp4'
    existing = run / f'piece-{1 - missing_index}.mp4'
    missing_proof = json.loads(missing.with_suffix('.provenance.json').read_text())
    existing_proof = json.loads(existing.with_suffix('.provenance.json').read_text())
    assert missing_proof['start_ms'] != existing_proof['start_ms']
    missing.unlink()
    recovered = existing
    if location == 'hardlink':
        retained = tmp_path / 'aaa-retained-copy'
        retained.mkdir()
        recovered = retained / missing.name
        recovered.hardlink_to(existing)
    from src.autoslice import bound_media_recovery
    # Use the real resolver, including its normal copy discovery and SHA check.
    assert bound_media_recovery.resolve_bound_media(missing, missing_proof['output_sha256']) == recovered
    assert recovered.samefile(existing)
    immutable = {p: p.read_bytes() for p in tmp_path.rglob('*.json')}
    current = relisten.evidence_projection(check, ranges=[raw])
    assert current['effective_distinct_observations'] == 0, current
    assert current['rejected_observations']
    assert len(calls) == 1
    assert all(p.read_bytes() == before for p, before in immutable.items())


@pytest.mark.parametrize('damage', ['bytes', 'symlink', 'source', 'sidecar', 'unrecoverable'])
def test_recovery_keeps_bad_data_and_unprovable_ancestry_rejected(tmp_path, monkeypatch, damage):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    _, recording, check, _, raw, _, calls, _ = fixture.make_case(tmp_path, monkeypatch, 'final')
    piece = tmp_path / 'piece-0.mp4'
    copy = tmp_path / 'retained-copy' / piece.name
    copy.parent.mkdir()
    shutil.copyfile(piece, copy)
    if damage == 'bytes':
        piece.write_bytes(b'changed original bytes')
    elif damage == 'symlink':
        piece.unlink()
        piece.symlink_to(copy)
    elif damage == 'source':
        recording.unlink()
    elif damage == 'sidecar':
        p = piece.with_suffix('.provenance.json')
        value = json.loads(p.read_text())
        value['source_sha256'] = '0' * 64
        p.write_text(json.dumps(value))
    else:
        piece.unlink()
        copy.unlink()
        (tmp_path / 'padded.mp4').unlink()
        from src.autoslice import bound_media_recovery
        monkeypatch.setattr(bound_media_recovery, '_rebuild', lambda *_: False)
    current = relisten.evidence_projection(check, ranges=[raw])
    assert current['effective_distinct_observations'] == 0 and current['rejected_observations']
    assert len(calls) == 1


@pytest.mark.parametrize('timing', ['existing', 'appears_during_resolution'])
def test_existing_identical_piece_hardlink_alias_still_rejected(tmp_path, monkeypatch, timing):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    write = Path.write_bytes
    def identical(path, data):
        if path.parent == tmp_path and path.name in {'piece-0.mp4', 'piece-1.mp4'}:
            data = b'same synthetic encoded bytes, independent original inodes'
        return write(path, data)
    with monkeypatch.context() as producer:
        producer.setattr(Path, 'write_bytes', identical)
        _, _, check, _, raw, _, calls, _ = fixture.make_case(tmp_path, monkeypatch, 'final_chain')
    assert relisten.evidence_projection(check, ranges=[raw])['effective_distinct_observations'] == 1
    second = tmp_path / 'piece-1.mp4'
    second.unlink()
    if timing == 'existing':
        second.hardlink_to(tmp_path / 'piece-0.mp4')
    else:
        from src.autoslice import bound_media_recovery
        resolve = bound_media_recovery.resolve_bound_media
        def appears(path, *args, **kwargs):
            if Path(path) == second:
                second.hardlink_to(tmp_path / 'piece-0.mp4')
                return second
            return resolve(path, *args, **kwargs)
        monkeypatch.setattr(bound_media_recovery, 'resolve_bound_media', appears)
    assert relisten.evidence_projection(check, ranges=[raw])['effective_distinct_observations'] == 0
    assert len(calls) == 1
