"""Replay admission keeps the same identity without unfolding shared history."""
from collections import UserDict
import gc
import hashlib
import json
import tracemalloc

import pytest

from src.autoslice.canonical_json_hash import canonical_sha256
from src.autoslice.final_review_provider_budget import (
    _adjudication_input_fingerprint, _pending_audio_obligation, _public_identity,
)


def reference(text, finding, context=None, offset=0, judge=None):
    finding = dict(finding)
    if not finding.get('_prior_exact_source_transcript_handoffs'):
        finding.pop('_prior_exact_source_transcript_handoffs', None)
    value = {'srt_text': text, 'finding': _public_identity(finding),
        'clip_context': _public_identity(context), 'source_media_timeline_offset_ms': offset,
        'cpa_cache_identity': _public_identity(getattr(judge, 'cpa_cache_identity', None)),
        'pending_audio_obligation': _pending_audio_obligation(finding)}
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=repr)
    return 'sha256:' + hashlib.sha256(raw.encode()).hexdigest()


def fingerprint(finding, context=None, offset=0, judge=None):
    return _adjudication_input_fingerprint('原始字幕', finding, clip_context=context,
        source_media_timeline_offset_ms=offset, judge_llm_call=judge)


class UnknownValue:
    def __repr__(self):
        return 'stable fallback 汉字'


@pytest.mark.parametrize('value', [
    UserDict({7: ('字', [True, 1, -0.0, None]), 'items': {3, 1, 2}}),
    {'unknown': UnknownValue(), 'numbers': [float('nan'), float('inf'), -float('inf')]},
    {'exact_release_adjudication': {'excluded': 'original audit'}, 'why': '保留理由'},
])
def test_fingerprint_matches_original_bytes_and_preserves_material_changes(value):
    finding = {'cue_index': 1, 'history': [value, value], '_prior_exact_source_transcript_handoffs': []}
    def judge(_prompt):
        raise AssertionError('provider not allowed')
    judge.cpa_cache_identity = {'model': 'test', 'effort': 'medium'}
    assert fingerprint(finding, {'context': value}, 17, judge) == reference(
        '原始字幕', finding, {'context': value}, 17, judge)
    assert fingerprint(finding, offset=1) != fingerprint(finding)
    assert fingerprint({**finding, 'why': 'new evidence'}) != fingerprint(finding)
    assert fingerprint({**finding, 'provider_budget_replay': {'audit': 'new'}}) == fingerprint(finding)


@pytest.mark.parametrize('container', ['dict', 'list'])
def test_fingerprint_memory_depends_on_shared_graph_not_history_occurrences(container):
    value = {'record': ['recorded', 1], 'exact_release_adjudication': {'original': 'audit'}}
    for _ in range(17):
        value = {'first': value, 'later': value} if container == 'dict' else [value, value]
    finding = {'cue_index': 1, 'history': value}
    gc.collect()
    tracemalloc.start()
    try:
        digest = fingerprint(finding)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(digest) == 71 and digest.startswith('sha256:')
    assert peak < 12 * 1024**2, peak
    assert (value['first'] is value['later']) if container == 'dict' else (value[0] is value[1])


def test_bounded_canonical_hash_keeps_standard_default_and_nan_policy():
    value = {'large': ['保留原文' * 10000, UnknownValue()] * 3}
    expected = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), default=repr).encode()).hexdigest()
    assert canonical_sha256(value, default=repr) == expected
    with pytest.raises(TypeError):
        canonical_sha256(value)
    with pytest.raises(ValueError):
        canonical_sha256({'not_finite': float('nan')}, default=repr)
