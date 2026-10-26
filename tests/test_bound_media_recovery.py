"""GC → cold consumer and normal recollection, with immutable-history controls."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import runpy
import shutil
import wave

import pytest

from src.autoslice import bound_media_recovery as recovery
from src.autoslice import gemini_range_context as gemini
from src.autoslice import independent_context_adjudication as adjudication
from src.autoslice.argon_escalation_contract import AudioSpec, Finding, prepare_audio, ContractError


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def generated(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    module = runpy.run_path(str(Path(__file__).with_name('test_gemini_range_context.py')))
    module['test_range_observer_uses_exact_block_cache_and_keeps_provider_blind'](
        tmp_path, monkeypatch, None)
    srt = '1\n00:00:01,000 --> 00:00:03,000\nspoken words\n\n'
    check = module['_check_request'](tmp_path/'source.mp4', srt)
    verifier = module['_verifier'](tmp_path/'source.mp4', tmp_path/'out')
    request = module['_range_request'](check)
    def observe():
        return gemini.observe_gemini_range_context(verifier=verifier,
            check_request=check, range_witness_request=request,
            budget_receipt_path=tmp_path/'out/native-audio-budget.json')[0]
    row = observe()
    return row, check, observe


@pytest.mark.parametrize('route', ['copy', 'rebuild'])
def test_generated_range_cold_recovery_does_not_rewrite_or_redispatch(generated, tmp_path, monkeypatch, route):
    row, check, observe = generated
    bindings = row['artifact_bindings']
    immutable = {p: p.read_bytes() for p in (tmp_path/'out').rglob('*.json')}
    original = Path(bindings['audio_clip_path'])
    if route == 'copy':
        target = tmp_path/'other-run'/original.name
        target.parent.mkdir()
        shutil.copyfile(original, target)
    else:
        from src.autoslice import entity_audio_verifier
        def crop(**kw):
            kw['audio_path'].write_bytes(b'black-frame-mp4')
            return True, ''
        monkeypatch.setattr(entity_audio_verifier, '_crop_black_frame_audio', crop)
    for p in (tmp_path/'out').rglob('input.mp4'):
        p.unlink()
    valid, reason = adjudication.validate_segmented_range_context_observation(
        check['whole_clip_current_srt'], check, row)
    assert reason is None and valid['authority'] == 'EVIDENCE_ONLY'
    assert valid['mutation_authorized'] is False
    again = observe()
    assert again['status'] == 'OBSERVED' and again['served_from_cache']
    assert again['artifact_bindings']['audio_clip_path'] == str(original)
    assert not original.exists()
    for p, raw in immutable.items():
        if p.name != 'native-audio-budget.json':
            assert p.read_bytes() == raw


def test_unrecoverable_range_is_unprovable_and_ordinary_collection_runs(generated, tmp_path, monkeypatch):
    row, check, observe = generated
    original = Path(row['artifact_bindings']['audio_clip_path'])
    old_acquisition = original.parent/'acquisition.json'
    before = old_acquisition.read_bytes()
    for p in (tmp_path/'out').rglob('input.mp4'):
        p.unlink()
    from src.autoslice import entity_audio_verifier
    def different(**kw):
        kw['audio_path'].write_bytes(b'not-the-bound-bytes')
        return True, ''
    monkeypatch.setattr(entity_audio_verifier, '_crop_black_frame_audio', different)
    valid, reason = adjudication.validate_segmented_range_context_observation(
        check['whole_clip_current_srt'], check, row)
    assert valid is None and reason == 'BOUND_MEDIA_REACQUISITION_REQUIRED'
    metadata = json.loads(before)
    from types import SimpleNamespace
    budget = SimpleNamespace(snapshot=lambda: {'attempts': [
        {'attempt_id':n, 'provider':metadata['provider'], 'model':metadata['model'],
         'start_ms':metadata['source_target_ms'][0], 'end_ms':metadata['source_target_ms'][1],
         'status':'OBSERVED'} for n in range(1,4)]})
    read_claims = gemini._read_dispatch_claims
    monkeypatch.setattr(gemini, '_read_dispatch_claims', lambda *a, **kw: {
        n:{'same-request':{'run_dir':str(original.parent)}} for n in range(1,4)})
    basis = gemini._resend_basis(budget, directories=[], source_sha256=metadata['source_media_sha256'],
        provider=metadata['provider'],model=metadata['model'],
        start_ms=metadata['source_target_ms'][0],end_ms=metadata['source_target_ms'][1],
        identity='same-request')
    assert basis['reason'] == 'BOUND_MEDIA_REACQUISITION_REQUIRED'
    assert basis['prior_attempt_ids'] == [1,2,3] and basis['soft_pressure_exceeded']
    monkeypatch.setattr(gemini, '_read_dispatch_claims', read_claims)
    later = observe()
    assert later['status'] == 'OBSERVED' and not later.get('served_from_cache')
    assert later['artifact_bindings']['audio_clip_path'] != str(original)
    assert old_acquisition.read_bytes() == before and not original.exists()
    ledger = json.loads((tmp_path/'out/native-audio-budget.json').read_text())
    assert ledger['budget']['attempt_count'] == 3  # two old calls, one new call


def test_missing_media_cannot_mask_a_changed_prompt(generated, tmp_path, monkeypatch):
    row, check, observe = generated
    for p in (tmp_path/'out').rglob('input.mp4'):
        p.unlink()
    prompt = Path(row['artifact_bindings']['prompt_path'])
    prompt.write_text('changed prompt')
    later = observe()
    assert later['status'] == 'UNCERTAIN'
    assert later['reason_code'] == 'GEMINI_RANGE_CACHE_INVALID'
    ledger = json.loads((tmp_path/'out/native-audio-budget.json').read_text())
    assert ledger['budget']['attempt_count'] == 2


@pytest.mark.parametrize('damage', ['bytes', 'symlink', 'read_change'])
def test_existing_damage_keeps_original_rejection(generated, tmp_path, monkeypatch, damage):
    row, check, observe = generated
    path = Path(row['artifact_bindings']['audio_clip_path'])
    copy = tmp_path/'input.mp4'
    shutil.copyfile(path, copy)
    if damage == 'bytes':
        path.write_bytes(b'bad bytes')
    elif damage == 'symlink':
        path.unlink()
        path.symlink_to(copy)
    else:
        original_hash = adjudication._file_sha256
        monkeypatch.setattr(adjudication, '_file_sha256',
            lambda value: None if value == str(path) else original_hash(value))
    valid, reason = adjudication.validate_segmented_range_context_observation(
        check['whole_clip_current_srt'], check, row)
    assert valid is None and reason == 'SEGMENTED_RANGE_ARTIFACT_BINDING_INVALID'


def test_argon_decode_resolves_same_bytes_without_source_binding_rewrite(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    source = tmp_path/'source.wav'
    with wave.open(str(source), 'wb') as handle:
        handle.setparams((1, 2, 16000, 0, 'NONE', ''))
        handle.writeframes(b'\x01\x00'*32000)
    spec = AudioSpec(source, Finding(digest(source), (500, 1000), 'finding'), (0, 1500))
    binding, audio = prepare_audio(spec)
    copy = tmp_path/'other-run'/source.name
    copy.parent.mkdir()
    shutil.copyfile(source, copy)
    source.unlink()
    assert prepare_audio(spec) == (binding, audio)
    assert spec.source_path == source and not source.exists()
    from src.autoslice.argon_escalation_integration import spec_from_envelope
    saved = {'source_binding':{'source_path':str(source),
        'source_media_sha256':spec.finding.source_media_sha256,
        'target_source_ms':[500,1000], 'finding_id':'finding', 'context_source_ms':[0,1500]}}
    physical = spec_from_envelope(saved)
    assert physical.source_path == copy and prepare_audio(physical) == (binding, audio)
    assert saved['source_binding']['source_path'] == str(source)
    source.write_bytes(b'corrupt')
    with pytest.raises(ContractError, match='SOURCE_SHA_MISMATCH'):
        prepare_audio(spec)
    source.unlink()
    source.symlink_to(copy)
    with pytest.raises(ContractError, match='SOURCE_FILE_UNSAFE'):
        prepare_audio(spec)


def test_native_range_writer_to_cold_consumer_recovers_exact_input(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    module = runpy.run_path(str(Path(__file__).with_name('test_native_context_witness.py')))
    source = tmp_path/'source.mp4'
    source.write_bytes(b'range-source')
    sha = module['_install_range_writer'](monkeypatch, source, tmp_path/'cache')
    check, request = module['_range_requests'](sha)
    from src.autoslice import native_context_witness as native
    from src.autoslice import native_foreign_witness as foreign
    wrapper = native.build_independent_context_witnesses(fallback=lambda r: {},
        source_media=source, output_dir=tmp_path/'out', providers=('mai',),
        native_observation_cache_root=tmp_path/'cache')
    row, = wrapper.observe_range_context(check_request=check, range_witness_request=request)
    path = Path(row['artifact_bindings']['audio_clip_path'])
    raw = path.read_bytes()
    path.unlink()
    monkeypatch.setattr(foreign, '_extract_exact_mp3', lambda src, out, a, b: out.write_bytes(raw))
    valid, reason = adjudication.validate_native_range_context_observation(
        check['whole_clip_current_srt'], check, row)
    assert valid is not None and reason is None and not path.exists()


def test_padded_writer_recipe_rebuilds_piece_then_copy(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    source, piece, padded = (tmp_path/n for n in ('raw.mp4','piece.mp4','padded.mp4'))
    source.write_bytes(b'raw recording')
    expected = hashlib.sha256(b'piece bytes').hexdigest()
    piece.with_suffix('.provenance.json').write_text(json.dumps({
        'source_path':str(source), 'source_sha256':digest(source), 'start_ms':10,
        'end_ms':1000, 'output_path':str(piece), 'output_sha256':expected}))
    padded.with_suffix('.provenance.json').write_text(json.dumps({
        'inputs':[{'path':str(piece),'sha256':expected}],
        'output_path':str(padded),'output_sha256':expected}))
    from src.autoslice import recut_materialization
    writer = tmp_path/'synthetic_writer.py'
    writer.write_text('from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_bytes(b"piece bytes")\n')
    monkeypatch.setattr(recut_materialization, '_accurate_reencode_recut_command',
        lambda **kw: ['python3', str(writer), str(kw['output_media'])])
    resolved = recovery.resolve_bound_media(padded, expected)
    assert digest(resolved) == expected and not piece.exists() and not padded.exists()


@pytest.mark.parametrize('exists', [False, True])
def test_invalid_digest_keeps_original_argon_binding_error(tmp_path, exists):
    from src.autoslice.argon_escalation_integration import spec_from_envelope
    path = tmp_path/'input.mp4'
    if exists:
        path.write_bytes(b'unchanged')
    spec = spec_from_envelope({'source_binding':{'source_path':str(path),
        'source_media_sha256':'invalid', 'target_source_ms':[500,1000],
        'finding_id':'finding','context_source_ms':[0,1500]}})
    with pytest.raises(ContractError, match='SOURCE_SHA_INVALID'):
        spec.finding.binding()
