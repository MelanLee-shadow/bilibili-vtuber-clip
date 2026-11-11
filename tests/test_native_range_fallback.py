"""Ordinary native range -> existing Gemini writer -> cold/cache consumers."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice import gemini_range_context as gemini
from src.autoslice import native_context_witness as native
from src.autoslice import supplement_audio_budget
from src.autoslice.discovery_channels import (
    Channel, SourceIdentity, fullclip_prefetch_geometry, prefetch_fullclip,
    validated_observation_nominations,
)
from src.autoslice.entity_audio_verifier import _LocalAudioVerifier
from src.autoslice.review_errors import ProviderFailure
from tests.test_gemini_range_context import _verifier
from tests.test_native_context_witness import _install_range_writer


TEXT = '1\n00:00:01,000 --> 00:00:03,000\n整句范围听写\n\n'


@pytest.mark.parametrize('row,service_failure', [
    ({'provider': 'mai', 'status': 'UNCERTAIN', 'reason_code': 'MAI_API_KEY_MISSING'}, True),
    ({'provider': 'moss', 'status': 'UNCERTAIN', 'reason_code': 'NATIVE_RANGE_RECOVERY_CONFIG_NOT_READY',
      'original_failure_reason_code': 'MOSS_API_KEY_MISSING'}, True),
    ({'provider': 'moss', 'status': 'UNCERTAIN', 'reason_code': 'MOSS_HTTP_ERROR',
      'review_error': {'status': 'CALL_FAILURE', 'provider': 'moss', 'reason_code': 'MOSS_HTTP_ERROR'}}, True),
    ({'provider': 'moss', 'status': 'UNKNOWN', 'reason_code': 'MOSS_API_KEY_MISSING'}, False),
    ({'provider': 'agy', 'status': 'UNCERTAIN', 'reason_code': 'MOSS_API_KEY_MISSING'}, False),
    ({'provider': 'moss', 'status': 'UNCERTAIN', 'reason_code': 'MOSS_HTTP_ERROR', 'review_error': []}, False),
    ({'provider': 'moss', 'status': 'UNCERTAIN', 'reason_code': 'NATIVE_RANGE_RECOVERY_CONFIG_NOT_READY'}, False),
    ({'provider': 'moss', 'status': 'UNCERTAIN', 'reason_code': 42}, False),
    ({'provider': 42, 'status': 'UNCERTAIN', 'reason_code': 'MOSS_HTTP_ERROR'}, False),
    ({'provider': 'moss', 'status': [], 'reason_code': 'MOSS_HTTP_ERROR'}, False),
    ({'provider': 'moss', 'status': 'UNCERTAIN', 'reason_code': 'MAI_API_KEY_MISSING'}, False),
    ({'provider': 'moss', 'status': 'UNCERTAIN', 'reason_code': 'NATIVE_RANGE_RECOVERY_CONFIG_NOT_READY',
      'original_failure_reason_code': ['MOSS_API_KEY_MISSING']}, False),
    ({'provider': 'moss', 'status': 'UNCERTAIN', 'reason_code': 'MOSS_HTTP_ERROR',
      'review_error': {'status': 'CALL_FAILURE', 'provider': 'mai', 'reason_code': 'MOSS_HTTP_ERROR'}}, False),
    ({'provider': 'moss', 'status': 'UNCERTAIN', 'reason_code': 'MOSS_HTTP_ERROR',
      'review_error': {'status': 'CALL_FAILURE', 'provider': 'moss', 'reason_code': 42}}, False),
    ({'provider': 'moss', 'status': 'UNCERTAIN', 'reason_code': 'MOSS_HTTP_ERROR',
      'review_error': {'status': [], 'provider': 'moss', 'reason_code': 'MOSS_HTTP_ERROR'}}, False),
    ({'provider': 'mai', 'status': 'UNCERTAIN', 'reason_code': 'MAI_API_KEY_MISSING',
      'review_error': []}, True),
    ({}, False),
    (None, False),
])
def test_service_failure_fields_preserve_c0_filter(row, service_failure):
    from src.autoslice.discovery_channels import fullclip_scan_observations
    assert native.native_range_service_failure(row) is service_failure
    assert fullclip_scan_observations([row]) == (() if service_failure else (row,))


def setup_path(tmp_path, monkeypatch, *, failed='moss', reason='HTTP_ERROR', http_status=None,
               gemini_failure=False, source_bytes=b'unchanged recording', duration_ms=10_000):
    source = tmp_path/'source.mp4'
    source.write_bytes(source_bytes)
    output = tmp_path/'output'
    output.mkdir()
    source_sha = _install_range_writer(monkeypatch, source, tmp_path/'native-cache')
    ordinary_factory = native.build_native_foreign_witness
    counts = {'native_failed': 0, 'gemini': 0}

    def foreign_factory(**kwargs):
        if kwargs['provider'] != failed:
            return ordinary_factory(**kwargs)
        def fail(**_kwargs):
            counts['native_failed'] += 1
            raise ProviderFailure(f'{failed.upper()}_{reason}', provider=failed,
                metadata={'http_status': http_status} if http_status is not None else None)
        return fail

    def extract(_source, target, start, end):
        target.write_bytes(f'audio:{start}:{end}'.encode())

    def crop(**kwargs):
        kwargs['audio_path'].write_bytes(b'black-frame exact audio')
        return True, ''

    def observe(**kwargs):
        counts['gemini'] += 1
        kwargs['on_provider_dispatch']('agy', 'test-agy-model')
        if gemini_failure:
            raise ProviderFailure('AGY_SERVICE_FAILED', provider='agy')
        prompt = kwargs['job_dir']/'prompt.md'
        prompt.write_text(gemini.range_transcript_prompt(kwargs['request']))
        duration = kwargs['request']['target_audio_end_ms'] - kwargs['request']['target_audio_start_ms']
        raw = {'status': 'OBSERVED', 'target_audible': True,
            'full_transcript': '整句范围听写', 'segments': [
                {'segment': 'complete_block', 'start_ms': 0, 'end_ms': duration,
                 'transcript': '整句范围听写'}]}
        response = kwargs['job_dir']/'verdict.raw.json'
        response.write_text(json.dumps(raw))
        assert kwargs['candidates'] == []
        assert '整句范围听写' not in prompt.read_text()
        return SimpleNamespace(observed=raw, provider='agy', model='test-agy-model',
            prompt_path=prompt, response_path=response, provider_failures=[])

    monkeypatch.setattr(native, 'build_native_foreign_witness', foreign_factory)
    monkeypatch.setattr(gemini, '_extract_exact_mp3', extract)
    monkeypatch.setattr(gemini, '_crop_black_frame_audio', crop)
    monkeypatch.setattr(gemini, '_observe_entity_audio', observe)

    def build():
        fallback = _LocalAudioVerifier(**{**vars(_verifier(source, output)), 'source_duration_ms':duration_ms})
        return native.build_independent_context_witnesses(fallback=fallback,
            source_media=source, output_dir=output/'independent', providers=('mai', 'moss'),
            budget_receipt_path=output/'native-audio-budget.json',
            native_observation_cache_root=tmp_path/'native-cache')
    identity = SourceIdentity('clip', (source_sha,))
    return source, output, identity, counts, build


@pytest.mark.parametrize('failed,reason,http_status', [
    ('moss', 'HTTP_ERROR', 524), ('moss', 'HTTP_ERROR', 402), ('mai', 'HTTP_ERROR', 401),
    ('moss', 'TIMEOUT', None), ('mai', 'TRANSPORT_ERROR', None), ('moss', 'API_KEY_MISSING', None),
])
def test_first_failure_fallback_and_cold_resume_keep_originals(tmp_path, monkeypatch, failed, reason, http_status):
    from src.autoslice import discovery_channels as channels
    source, output, identity, counts, build = setup_path(tmp_path, monkeypatch,
        failed=failed, reason=reason, http_status=http_status)
    first = prefetch_fullclip(TEXT, verifier=build(), source=identity, source_offset_ms=100)
    failures = [r for r in first if r['status'] != 'OBSERVED']
    assert len(failures) == 1 and failures[0]['reason_code'] == f'{failed.upper()}_{reason}'
    assert failures[0]['provider'] == failed
    fallback, = [r for r in first if r.get('provider') == 'agy']
    assert fallback['model'] == 'test-agy-model' and fallback['candidate_blind'] is True
    assert fallback['authority'] == 'EVIDENCE_ONLY' and fallback['evidence_scope'] == 'RANGE_CONTEXT_ONLY'
    assert fallback['mutation_authorized'] is False
    assert channels.fullclip_scan_complete(first)
    context = {'candidate_id': 'clip', 'pieces': [{'source_media_sha256': identity.media_sha256s[0]}]}
    scan = validated_observation_nominations(TEXT, channels.fullclip_scan_observations(first),
        context=context, source_offset_ms=100, channel=Channel.FULL_TRANSCRIPT)
    assert not scan.rejection_reasons
    originals = {str(p): p.read_bytes() for p in output.glob('independent/*/native-context/*.json')}
    assert counts == {'native_failed': 1, 'gemini': 1}
    supplement_audio_budget._REGISTRY.pop(str(source.resolve()), None)
    cold = prefetch_fullclip(TEXT, verifier=build(), source=identity, source_offset_ms=100)
    assert channels.fullclip_scan_complete(cold)
    assert counts == {'native_failed': 1, 'gemini': 1}
    assert all(Path(p).read_bytes() == raw for p, raw in originals.items())
    assert [r for r in cold if r.get('provider') == 'agy'][0]['served_from_cache'] is True
    account = json.loads((output/'native-audio-budget.json').read_bytes())['budget']
    assert len([a for a in account['attempts'] if a['provider'] == 'agy']) == 1


@pytest.mark.parametrize('reason', [
    'NO_SPEECH', 'RESPONSE_INVALID', 'NATIVE_RANGE_ENVELOPE_INVALID',
    'NATIVE_ACQUISITION_OWNER_RECOVERY_REQUIRED',
])
def test_content_unknown_and_contract_failures_do_not_switch(tmp_path, monkeypatch, reason):
    _, _, identity, counts, build = setup_path(tmp_path, monkeypatch, reason=reason)
    rows = prefetch_fullclip(TEXT, verifier=build(), source=identity, source_offset_ms=100)
    assert any(r['status'] != 'OBSERVED' for r in rows)
    assert counts['gemini'] == 0


def test_bad_source_binding_never_reaches_fallback(tmp_path, monkeypatch):
    _, _, identity, counts, build = setup_path(tmp_path, monkeypatch)
    check, broad = fullclip_prefetch_geometry(TEXT, source=identity, source_offset_ms=100)
    check['clip_context_binding']['source_media_sha256s'] = ['f'*64]
    rows = build().observe_range_context(check_request=check, range_witness_request=broad, provider='moss')
    assert rows[0]['status'] != 'OBSERVED'
    assert counts == {'native_failed': 0, 'gemini': 0}


def test_backup_failure_stays_failed_and_keeps_both_provider_identities(tmp_path, monkeypatch):
    from src.autoslice import discovery_channels as channels
    _, _, identity, counts, build = setup_path(tmp_path, monkeypatch, gemini_failure=True)
    rows = prefetch_fullclip(TEXT, verifier=build(), source=identity, source_offset_ms=100)
    assert not channels.fullclip_scan_complete(rows)
    assert counts == {'native_failed': 1, 'gemini': 1}
    assert any(r.get('provider') == 'moss' and r['status'] != 'OBSERVED' for r in rows)
    assert any(r.get('provider') == 'agy' and r['status'] != 'OBSERVED' for r in rows)


def test_discovery_cold_call_reader_replays_fallback_without_a_live_owner(tmp_path, monkeypatch):
    from src.autoslice.discovery_channels import _ScanCalls, _scan_verifier
    _, _, identity, counts, build = setup_path(tmp_path, monkeypatch)
    calls = _ScanCalls()
    rows = prefetch_fullclip(TEXT, verifier=_scan_verifier(build(), calls, ('mai','moss')),
        source=identity, source_offset_ms=100)
    assert any(r.get('provider') == 'agy' and r['status'] == 'OBSERVED' for r in rows)
    retained = calls.retained()
    cold_calls = _ScanCalls(retained)
    # No provider methods exist on this delegate. The actual C0 call reader
    # consumes the saved original failure and backup answer from the manifest.
    cold = prefetch_fullclip(TEXT, verifier=_scan_verifier(object(), cold_calls, ('mai','moss')),
        source=identity, source_offset_ms=100)
    assert cold == rows and cold_calls.retained() == retained
    assert counts == {'native_failed': 1, 'gemini': 1}


@pytest.mark.parametrize('providers,complete', [
    ((('mai','mai'), ('mai','mai')), False),
    ((('agy','gemini-3.5-pro'), ('gemini_api','gemini-3.5-pro')), False),
    ((('mai','mai'), ('agy','gemini-3.5-pro')), True),
])
def test_fullclip_coverage_uses_actual_independent_models(providers, complete):
    from src.autoslice import discovery_channels as channels
    rows = [{'status': 'OBSERVED', 'provider': p, 'model': m} for p,m in providers]
    assert channels.fullclip_scan_complete(rows) is complete
