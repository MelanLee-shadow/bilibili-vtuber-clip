"""Ordinary native producers, shared storage, and file guards define reuse."""
from copy import deepcopy

import pytest

from src.autoslice import span_phoneme_resolution as span
from src.autoslice.continuation_subchecks import cold_read_session
from src.autoslice.review_evidence_store import load_review, persist_document
from tests.test_span_range_route_persistence import exercise, offline  # noqa: F401
from tests.test_c_subtitle_route_contracts import hearing


@pytest.mark.parametrize('choice', ['CURRENT', 'PROPOSED'])
def test_ordinary_inline_hearing_cold_session_avoids_repeated_catalogue(tmp_path, monkeypatch, choice):
    (_, _, audit), _ = hearing(choice, False)
    path = tmp_path/'inline.json'
    persist_document(path, audit)
    saved = load_review(path)
    outcome = saved['judge']['span_compilation']
    calls = []
    original = span.range_observations
    def counted(value):
        calls.append(1)
        return original(value)
    monkeypatch.setattr(span, 'range_observations', counted)
    with cold_read_session():
        first = span.observation_catalogue(outcome['request'], outcome['witness'])
        assert first[0] and not first[1]
        assert span.observation_catalogue(outcome['request'], outcome['witness']) == first
        assert len(calls) == 1


@pytest.mark.parametrize('conflicting', [False, True])
def test_generated_range_catalogue_reuses_cold_work_and_detaches_result(tmp_path, monkeypatch, conflicting):
    _, audit, _ = exercise(tmp_path, monkeypatch, conflicting=conflicting)
    path = tmp_path/'history.json'
    persist_document(path, audit)
    saved = load_review(path)
    request, witness = saved['request'], saved['verdict']
    calls = []
    original = span.range_observations
    def counted(value):
        calls.append(1)
        return original(value)
    monkeypatch.setattr(span, 'range_observations', counted)
    with cold_read_session():
        first = span.observation_catalogue(request, witness)
        assert first[0] and first[1] == []
        expected = deepcopy(first)
        first[0][0]['text'] = 'consumer-only change'
        assert span.observation_catalogue(request, witness) == expected
        # A changing ancestor directory legitimately disables file-backed reuse.
        count = len(calls)
        changed = deepcopy(request)
        changed['current_cue'] += '新'
        span.observation_catalogue(changed, witness)
        assert len(calls) == count + 1
    span.observation_catalogue(request, witness)
    span.observation_catalogue(request, witness)
    assert len(calls) == count + 3  # Live stages do not inherit the cold cache.
    assert load_review(path) == saved


def test_cold_catalogue_reopens_tampered_file_and_does_not_cache_rejection(tmp_path, monkeypatch):
    _, audit, paths = exercise(tmp_path, monkeypatch)
    path = tmp_path/'history.json'
    persist_document(path, audit)
    saved = load_review(path)
    request, witness = saved['request'], saved['verdict']
    audio = paths[1]['audio_clip']
    original_bytes = audio.read_bytes()
    calls = []
    original = span.range_observations
    def counted(value):
        calls.append(1)
        return original(value)
    monkeypatch.setattr(span, 'range_observations', counted)
    with cold_read_session():
        before = span.observation_catalogue(request, witness)
        assert before[0] and before[1] == []
        assert span.observation_catalogue(request, witness) == before
        count = len(calls)
        audio.write_bytes(b'tampered native audio')
        damaged = span.observation_catalogue(request, witness)
        assert damaged[1] and len(calls) == count + 1
        assert span.observation_catalogue(request, witness) == damaged and len(calls) == count + 2
        audio.write_bytes(original_bytes)
        assert span.observation_catalogue(request, witness) == before and len(calls) == count + 3
        assert span.observation_catalogue(request, witness) == before
