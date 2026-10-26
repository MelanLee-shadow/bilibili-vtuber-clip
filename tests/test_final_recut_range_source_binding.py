"""The normal final-recut writer must feed both ordinary range observers."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from src.autoslice import independent_context_adjudication as independent
from src.autoslice import native_context_witness as native
from src.autoslice import gemini_range_context as gemini
from src.autoslice.producer_recut_provenance import write_final_recut_provenance
from src.autoslice.producer_final_recut_source_binding import resolve_final_recut_source
from tests.test_native_context_witness import _install_range_writer, _range_requests
from tests.test_gemini_range_context import _verifier


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True))


def package(tmp_path, count=1, cache_status=None):
    recording = tmp_path / 'recording.flv'
    recording.write_bytes(b'synthetic immutable recording')
    rows, inputs = [], []
    duration = 40_000 if count == 1 else 20_000
    for index in range(count):
        piece = tmp_path / f'piece-{index}.mp4'
        piece.write_bytes(f'piece {index}'.encode())
        start = 100_000 + index * 40_000
        row = {'source_path': str(recording), 'source_sha256': sha(recording),
               'source_media_binding': 'sha256:' + sha(recording),
               'start_ms': start, 'end_ms': start + duration,
               'output_path': str(piece), 'output_sha256': sha(piece)}
        write(piece.with_suffix('.provenance.json'), row)
        rows.append(row)
        inputs.append({'path': str(piece), 'sha256': sha(piece)})
    padded = tmp_path / 'padded.mp4'
    padded.write_bytes(b''.join((tmp_path / f'piece-{i}.mp4').read_bytes() for i in range(count)))
    proof = padded.with_suffix('.provenance.json')
    write(proof, {'output_path': str(padded), 'output_sha256': sha(padded), 'inputs': inputs})
    media = tmp_path / 'final.recut.mp4'
    media.write_bytes(b'real writer, synthetic recut bytes')
    spec = {'pieces': [{'start_ms': row['start_ms'], 'end_ms': row['end_ms']} for row in rows]}
    _, absolute_start, absolute_end = resolve_final_recut_source(
        spec=spec, piece_provenance_rows=rows, final_start=5_000, final_end=35_000)
    if cache_status is not None:
        # The real cached-piece path annotates its in-memory row only; the
        # immutable piece sidecar stays byte-for-byte unchanged.
        for row in rows:
            row['source_revalidation_status'] = cache_status
    final_proof = write_final_recut_provenance(
        media_path=media, padded=padded, padded_provenance_path=proof,
        piece_provenance_rows=rows, final_start=5_000, final_end=35_000,
        absolute_source_start_ms=absolute_start, absolute_source_end_ms=absolute_end)
    return media, recording, padded, final_proof


@pytest.mark.parametrize('count', [1, 2])
def test_final_recut_writer_enters_shared_source_binding(tmp_path, count):
    media, recording, padded, proof = package(tmp_path, count)
    check, _ = _range_requests(sha(recording))
    before = {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    result = independent._range_media_source_binding(check, str(media), sha(media))
    assert result is not None
    assert result['role'] == 'PRODUCER_FINAL_RECUT_FROM_RECORDING'
    assert result['media_sha256'] == sha(media)
    assert result['parent_media_sha256'] == sha(padded)
    assert result['final_recut_interval_ms'] == [5_000, 35_000]
    assert all(p.read_bytes() == raw for p, raw in before.items())


def install_gemini(monkeypatch, source, output, dispatches):
    verifier = _verifier(source, output)
    verifier.source_duration_ms = 30_000
    def extract(_source, output_path, start_ms, end_ms):
        output_path.write_bytes(f'fixture:{start_ms}:{end_ms}'.encode())
    def crop(**kw):
        kw['audio_path'].write_bytes(b'fake black-frame mp4')
        return True, ''
    def observe(**kw):
        dispatches.append('gemini')
        kw['on_provider_dispatch']('agy', 'test-agy-model')
        prompt = kw['job_dir'] / 'prompt.md'
        prompt.write_text(gemini.range_transcript_prompt(kw['request']))
        duration = kw['request']['target_audio_end_ms'] - kw['request']['target_audio_start_ms']
        raw = {'status': 'OBSERVED', 'target_audible': True, 'full_transcript': '独立范围听写',
               'segments': [{'segment': 'complete_block', 'start_ms': 0,
                             'end_ms': duration, 'transcript': '独立范围听写'}]}
        response = kw['job_dir'] / 'verdict.raw.json'
        write(response, raw)
        return SimpleNamespace(observed=raw, provider='agy', model='test-agy-model',
                               prompt_path=prompt, response_path=response, provider_failures=[])
    monkeypatch.setattr(gemini, '_extract_exact_mp3', extract)
    monkeypatch.setattr(gemini, '_crop_black_frame_audio', crop)
    monkeypatch.setattr(gemini, '_observe_entity_audio', observe)
    return lambda check, broad: gemini.observe_gemini_range_context(
        verifier=verifier, check_request=check, range_witness_request=broad,
        budget_receipt_path=output / 'native-audio-budget.json')


@pytest.mark.parametrize('provider', ['mai', 'moss', 'gemini'])
@pytest.mark.parametrize('count', [1, 2])
def test_normal_range_observer_and_consumer_accept_ordinary_final_recut(tmp_path, monkeypatch, provider, count):
    media, recording, _, _ = package(tmp_path, count)
    check, broad = _range_requests(sha(recording))
    output = tmp_path / 'out'
    output.mkdir()
    dispatches = []
    if provider == 'gemini':
        observe = install_gemini(monkeypatch, media, output, dispatches)
    else:
        _install_range_writer(monkeypatch, media, tmp_path / 'cache', dispatches=dispatches)
        wrapper = native.build_independent_context_witnesses(
            fallback=lambda req: pytest.fail('range must not fallback to a narrow witness'),
            source_media=media, output_dir=output, providers=(provider,),
            native_observation_cache_root=tmp_path / 'cache')
        def observe(request, window):
            return wrapper.observe_range_context(
                check_request=request, range_witness_request=window)
    first = observe(check, broad)
    assert first[0]['status'] == 'OBSERVED', first[0].get('reason_code')
    assert first[0]['evidence_scope'] == 'RANGE_CONTEXT_ONLY'
    assert first[0]['mutation_authorized'] is False
    validated, reason = independent.validate_range_acoustic_observation(
        check['whole_clip_current_srt'], check, first[0])
    assert validated is not None, reason
    before = len(dispatches)
    second = observe(check, broad)
    assert second[0]['status'] == 'OBSERVED'
    assert len(dispatches) == before == 1


@pytest.mark.parametrize('damage', ['media', 'parent', 'recording', 'output_path', 'parent_hash',
    'piece_copy', 'absolute_clock', 'negative_cut', 'outside_cut', 'source_hash_list',
    'missing_parent_proof', 'parent_proof_content', 'cycle', 'source_link'])
def test_recut_provenance_drift_is_still_rejected(tmp_path, damage):
    media, recording, padded, proof = package(tmp_path)
    check, _ = _range_requests(sha(recording))
    value = json.loads(proof.read_text())
    if damage == 'media':
        media.write_bytes(b'changed output')
    elif damage == 'parent':
        padded.write_bytes(b'changed padded')
    elif damage == 'recording':
        recording.write_bytes(b'changed recording')
    elif damage == 'output_path':
        value['final_recut']['output_path'] = str(tmp_path / 'not-the-media.mp4')
    elif damage == 'parent_hash':
        value['final_recut']['source_sha256'] = 'f' * 64
    elif damage == 'piece_copy':
        value['source_piece']['start_ms'] += 1
    elif damage == 'absolute_clock':
        value['final_recut']['absolute_source_start_ms'] += 1
    elif damage == 'negative_cut':
        value['final_recut']['start_ms'] = -1
    elif damage == 'outside_cut':
        value['final_recut']['end_ms'] = 99_000
    elif damage == 'source_hash_list':
        check['clip_context_binding']['source_media_sha256s'] = ['f' * 64]
    elif damage == 'missing_parent_proof':
        padded.with_suffix('.provenance.json').unlink()
    elif damage == 'parent_proof_content':
        parent = json.loads(padded.with_suffix('.provenance.json').read_text())
        parent['inputs'] = []
        write(padded.with_suffix('.provenance.json'), parent)
    elif damage == 'cycle':
        value['final_recut']['source_path'] = str(media)
    else:
        renamed = tmp_path / 'actual-recording.flv'
        recording.rename(renamed)
        recording.symlink_to(renamed)
    write(proof, value)
    assert independent._range_media_source_binding(check, str(media), sha(media)) is None


@pytest.mark.parametrize('provider', ['mai', 'moss', 'gemini'])
@pytest.mark.parametrize('damage', ['recording', 'absolute_clock'])
def test_corrupt_recut_never_reaches_provider(tmp_path, monkeypatch, provider, damage):
    media, recording, _, proof = package(tmp_path)
    check, broad = _range_requests(sha(recording))
    dispatches = []
    output = tmp_path / 'out'
    output.mkdir()
    if provider == 'gemini':
        observe = install_gemini(monkeypatch, media, output, dispatches)
    else:
        _install_range_writer(monkeypatch, media, tmp_path / 'cache', dispatches=dispatches)
        wrapper = native.build_independent_context_witnesses(
            fallback=lambda req: pytest.fail('no narrow fallback for bad recut'),
            source_media=media, output_dir=output, providers=(provider,),
            native_observation_cache_root=tmp_path / 'cache')
        def observe(request, window):
            return wrapper.observe_range_context(
                check_request=request, range_witness_request=window)
    if damage == 'recording':
        recording.write_bytes(b'drift after construction')
    else:
        value = json.loads(proof.read_text())
        value['final_recut']['absolute_source_start_ms'] += 1
        write(proof, value)
    result = observe(check, broad)
    assert result[0]['status'] == 'UNCERTAIN'
    assert dispatches == []


def test_explicit_delivered_source_must_match_before_parent_projection(tmp_path):
    media, recording, _, _ = package(tmp_path)
    check, _ = _range_requests(sha(recording))
    check['source_media_sha256'] = sha(media)
    before = json.loads(json.dumps(check))
    assert independent._range_media_source_binding(check, str(media), sha(media)) is not None
    assert check == before
    check['source_media_sha256'] = 'f' * 64
    assert independent._range_media_source_binding(check, str(media), sha(media)) is None


@pytest.mark.parametrize('status', ['HASH_BOUND_CACHE_REUSED_SOURCE_PATH_PRESENT',
                                    'HASH_BOUND_CACHE_SOURCE_ROOT_UNAVAILABLE'])
def test_cached_piece_writer_annotation_is_not_a_source_mismatch(tmp_path, status):
    media, recording, _, proof = package(tmp_path, cache_status=status)
    check, _ = _range_requests(sha(recording))
    before = {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    assert independent._range_media_source_binding(check, str(media), sha(media)) is not None
    assert all(p.read_bytes() == raw for p, raw in before.items())
    # Historical source availability metadata cannot bypass today's bytes.
    recording.write_bytes(b'not the bound recording')
    assert independent._range_media_source_binding(check, str(media), sha(media)) is None
