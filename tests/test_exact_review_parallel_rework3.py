"""Exercise actual cache/native owners with offline transport replacements."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading

import pytest

from src.autoslice import argon_escalation_storage as storage
from src.autoslice import audio_cache_io
from src.autoslice import diarized_transcription
from src.autoslice import entity_audio_verifier as entity
from src.autoslice import native_foreign_witness as native
from src.autoslice import supplement_audio_budget as budgets
from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder, RetryNeedsPlan
from tests import test_entity_audio_verifier as entity_tests


def test_audio_publisher_is_the_same_implementation_as_argon():
    assert audio_cache_io.atomic_write is storage.atomic_write
    assert audio_cache_io.immutable_directory is storage.immutable_directory


def test_bundle_readers_never_see_staging_and_collisions_verify_every_byte(tmp_path, monkeypatch):
    final = tmp_path/'cache-key'
    files = {'manifest.json': b'{"complete":true}', 'prompt.json': b'prompt',
             'response.json': b'full response'}
    ready, release = threading.Event(), threading.Event()
    rename = storage.os.rename

    def held_rename(source, target):
        if target == final:
            ready.set()
            assert release.wait(5)
        return rename(source, target)
    monkeypatch.setattr(storage.os, 'rename', held_rename)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(storage.immutable_directory, final, files)
        assert ready.wait(5)
        assert not final.exists()
        assert len(list(tmp_path.glob('.cache-key-*'))) == 1
        release.set()
        future.result()
    assert {p.name: p.read_bytes() for p in final.iterdir()} == files
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: storage.immutable_directory(final, files), range(24)))
    with pytest.raises(ValueError, match='IMMUTABLE_ARTIFACT_CONFLICT'):
        storage.immutable_directory(final, {**files, 'response.json': b'changed'})
    assert {p.name: p.read_bytes() for p in final.iterdir()} == files
    assert not list(tmp_path.glob('.cache-key-*'))
    empty = tmp_path/'occupied-empty'
    empty.mkdir()
    with pytest.raises(ValueError, match='IMMUTABLE_ARTIFACT_CONFLICT'):
        storage.immutable_directory(empty, files)
    assert not list(empty.iterdir())


def test_interrupted_file_publish_keeps_previous_record_and_removes_temp(tmp_path, monkeypatch):
    path = tmp_path/'record.json'
    storage.atomic_write(path, b'previous complete record')
    def fail(*_args):
        raise OSError('synthetic pre-replace failure')
    monkeypatch.setattr(storage.os, 'replace', fail)
    with pytest.raises(OSError, match='pre-replace'):
        storage.atomic_write(path, b'new complete record')
    assert path.read_bytes() == b'previous complete record'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['record.json']


@pytest.mark.parametrize('provider', ['mai', 'moss'])
def test_native_actual_provider_overlaps_without_source_or_receipt_lock(tmp_path, monkeypatch, provider):
    source = tmp_path/'source.bin'
    source.write_bytes(b'fixed source')
    output = tmp_path/'out'
    budget = budgets.start_budget(source)
    observer = native.build_native_foreign_witness(
        source_media=source, output_dir=output, provider=provider)
    barrier = threading.Barrier(8)
    calls = []
    def transcribe(audio, **kwargs):
        kwargs['before_request']()
        assert not budget._lock._is_owned()
        calls.append(kwargs['provider'])
        # A lock across transport makes reaching this barrier impossible.
        barrier.wait(timeout=10)
        from src.autoslice.mai_transcription import MAI_MODEL
        from src.autoslice.moss_transcription import MOSS_MODEL
        return {'provider': provider, 'model': {'mai': MAI_MODEL, 'moss': MOSS_MODEL}[provider],
                'input_audio_sha256': hashlib.sha256(audio).hexdigest(),
                'response_sha256': 'c'*64,
                'native_segments': [{'start_ms': 0, 'end_ms': 500, 'text': '原话'}]}
    monkeypatch.setattr(diarized_transcription, 'transcribe_evidence', transcribe)
    monkeypatch.setattr(native, '_extract_exact_mp3', lambda _s, out, a, b: out.write_bytes(b'audio'))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: observer(start_ms=i*2000, end_ms=i*2000+1000), range(8)))
    assert len(calls) == len(results) == 8
    snapshot = budget.snapshot()
    assert snapshot['attempt_count'] == snapshot['sealed_attempt_count'] == 8
    assert snapshot['pending_attempt_count'] == 0
    receipt = json.loads((output/'native-audio-budget.json').read_bytes())
    assert receipt['budget'] == snapshot
    assert observer(start_ms=0, end_ms=1000)['served_from_cache'] is True
    assert len(calls) == 8
    final_dirs = [p for p in (output/'native-exact'/provider).iterdir() if not p.name.startswith('.')]
    assert len(final_dirs) == 8
    assert all(len(list(p.iterdir())) == 2 for p in final_dirs)
    print(f'REWORK3_NATIVE_OWNER={provider}:peak_audio=8,dispatches=8,pending=0,cache_replay=0_dispatch')


def test_entity_actual_provider_and_same_content_cache_publish_in_parallel(tmp_path, monkeypatch):
    source = tmp_path/'base'/'source.mp4'
    source.parent.mkdir()
    source.write_bytes(b'fixed source')
    output = source.parent/'out'/'date'/'candidate'
    barrier = threading.Barrier(8)
    calls = []
    def run(command, **kwargs):
        if command[0] == 'ffmpeg':
            from pathlib import Path
            Path(command[-1]).write_bytes(b'identical audio bytes')
        else:
            from pathlib import Path
            calls.append(command)
            barrier.wait(timeout=10)
            (Path(kwargs['cwd'])/'verdict.json').write_text(entity_tests._observed_blind_witness())
        return entity_tests._Completed()
    monkeypatch.setattr(entity.subprocess, 'run', run)
    monkeypatch.setattr(entity, '_validate_cropped_audio_media', lambda *a, **k: (True, ''))
    monkeypatch.setenv('ENTITY_AUDIO_GEMINI_WEB_ENABLED', '0')
    monkeypatch.delenv('ENTITY_AUDIO_DISABLE_AGY', raising=False)
    verifier = entity.build_local_audio_entity_verifier(source_media=source, output_dir=output,
        recording_date='2026-10-22', source_duration_ms=60000, agy_bin='offline-agy')
    requests = [entity_tests._blind_witness_request(evidence_id=f'{i:064x}', start_ms=i*4000+2000)
                for i in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(verifier, requests))
    assert len(calls) == 8
    assert all(row['status'] == 'OBSERVED' for row in results)
    entries = list((source.parent/'cache'/'witness-acoustic').glob('*/*/*.json'))
    assert len(entries) == 3  # one complete content key, three mutually bound files
    assert all(not p.name.startswith('.') for p in entries)
    # Different request identity, same bytes/prompt/model: a true cache hit.
    replay = verifier(entity_tests._blind_witness_request(evidence_id='e'*64, start_ms=40000))
    assert replay['status'] == 'OBSERVED' and len(calls) == 8
    # Import a complete old flat cache through the same validator and atomic
    # bundle publisher. Old cache bytes remain intact and never cause re-buy.
    bundle = entries[0].parent
    for path in entries:
        path.rename(bundle.parent/path.name)
    bundle.rmdir()
    replay = verifier(entity_tests._blind_witness_request(evidence_id='f'*64, start_ms=44000))
    assert replay['status'] == 'OBSERVED' and len(calls) == 8
    assert bundle.is_dir()
    print('REWORK3_ENTITY_OWNER=peak_audio=8,same_content_bundle=1,cache_replay=0_dispatch')


def test_recorder_claim_preserves_unknown_and_cold_replay_without_call_lock(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    entered, release = threading.Event(), threading.Event()
    calls = []
    def call(prompt):
        calls.append(prompt)
        entered.set()
        assert release.wait(5)
        return '{"choice":"CURRENT","needs_audio":false}'
    call.cpa_cache_identity = {'model': 'offline'}
    def recorder():
        return SemanticAttemptRecorder(srt_text='1\n00:00:01,000 --> 00:00:02,000\n原话\n',
            finding={'cue_index': 1, 'why': 'same question'},
            clip_context={'source_media_sha256': 'a'*64}, offset_ms=0)
    first = recorder()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first.wrap(call), 'same question')
        assert entered.wait(5)
        try:
            with pytest.raises(RetryNeedsPlan, match='PRIOR_CALL_OUTCOME_UNKNOWN'):
                recorder().wrap(call)('same question')
            assert not list(tmp_path.rglob('*.lock'))
        finally:
            release.set()
        raw = future.result()
    assert recorder().wrap(call)('same question') == raw
    assert len(calls) == 1
