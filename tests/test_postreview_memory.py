"""Receipt consumers must not allocate a serialized copy just to inspect it."""
from copy import deepcopy
import json
import tracemalloc

import pytest

from src.autoslice import review_evidence_store as evidence
from src.autoslice.validation_memo import memoized_validation


def _peak(call):
    tracemalloc.start()
    try:
        result = call()
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_cold_ordinary_inline_history_is_bounded_and_rescans_mutation(tmp_path):
    # A real publisher creates the reference; the reader must discover it when
    # inserted after an earlier inline call, and keep its actual hash guard.
    packed = evidence.persist_document(tmp_path / 'review.json',
        {'request': {'raw_completion': 'saved answer', 'status': 'UNKNOWN'}})
    shared = {'raw_completion': '原始观察' * 64_000}
    history = {'passes': [shared] * 32}
    consume = evidence.cold_input(lambda value: value)
    result, peak = _peak(lambda: consume(history))
    assert result is history
    assert peak < 2_000_000, peak
    history['new_receipt'] = packed['request']
    assert consume(history)['new_receipt'] == {'raw_completion': 'saved answer', 'status': 'UNKNOWN'}
    from pathlib import Path
    Path(packed['request']['path']).write_bytes(b'{}')
    with pytest.raises(ValueError, match='ARTIFACT_SHA_MISMATCH'):
        consume(history)


def test_validation_cache_budget_does_not_serialize_oversize_history():
    shared = {'raw_completion': 'saved observation' * 32_000}
    original = {'passes': [shared] * 32}
    calls = []
    @memoized_validation(lambda: 'large-receipt')
    def owner():
        calls.append(1)
        return original
    result, peak = _peak(owner)
    assert result is original
    assert peak < 2_000_000, peak
    assert owner() is original
    assert len(calls) == 2  # Oversize results remain uncached, not rejected.


def test_inline_tuples_non_json_and_malformed_references_keep_consumer_contract():
    consume = evidence.cold_input(lambda value: value)
    opaque = object()
    value = ({'unknown': None}, {'legacy': opaque})
    assert consume(value) is value
    malformed = {'schema_version': evidence.REF_SCHEMA, 'sha256': 'broken', 'path': '/invalid'}
    with pytest.raises(ValueError, match='REVIEW_EVIDENCE_REFERENCE_INVALID'):
        consume({'request': malformed})


def test_cached_small_receipts_remain_copy_isolated_and_same_json_budget():
    from src.autoslice.validation_memo import _small_json_result
    for value in (None, {}, {'raw': '中' * 999_980}, [True, False, 3.5, ('a', None)]):
        assert _small_json_result(value) == (len(json.dumps(value, ensure_ascii=False)) <= 1_000_000)
    assert not _small_json_result({'raw': 'x' * 1_000_000})
    assert not _small_json_result(object())
    source = {'history': [{'status': 'UNKNOWN'}]}
    calls = []
    @memoized_validation(lambda: 'small-receipt')
    def owner():
        calls.append(1)
        return deepcopy(source)
    owner()['history'][0]['status'] = 'invented'
    assert owner() == source and len(calls) == 1


def test_ordinary_gate_logs_postreview_work_and_keeps_release_rejection(tmp_path, capsys):
    from tests.test_producer_package_finalization import (
        test_text_recovery_never_grants_release_and_still_validates_current_bytes,
    )
    test_text_recovery_never_grants_release_and_still_validates_current_bytes(tmp_path)
    lines = capsys.readouterr().err.splitlines()
    for phase in ('span_obligations', 'convergence', 'persist', 'repairs', 'pass_receipt'):
        stage = 'exact_final_review.postprocess.' + phase
        assert any(' START ' + stage + ' ' in line for line in lines), stage
        assert any(' END ' + stage + ' ' in line for line in lines), stage
    assert not any('{' in line or '}' in line or '保留正文' in line for line in lines)


def test_postreview_span_failure_keeps_original_exception_and_open_stage(tmp_path, monkeypatch, capsys):
    from src.autoslice import producer_package_finalization as finalization
    from src.autoslice import span_finding_obligations as span
    subtitle = tmp_path / 'current.srt'
    subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\n正文\n')
    error = RuntimeError('original owner failure')
    def fail(*_args):
        raise error
    monkeypatch.setattr(span, 'enforce_review', fail)
    with pytest.raises(RuntimeError) as caught:
        finalization._run_exact_final_review_gate(
            cid='current', out_root=tmp_path, final_start=0, final_end=1000,
            recut=finalization.CurrentTextReviewArtifacts(subtitle),
            chat_authority_audit={}, chat_authority_path=tmp_path/'authority.json',
            run_exact_final_review=lambda *_: {})
    assert caught.value is error
    log = capsys.readouterr().err
    assert ' START exact_final_review.postprocess.span_obligations ' in log
    assert ' END exact_final_review.postprocess.span_obligations ' not in log


def test_persisted_immutable_history_expands_as_a_graph_and_roundtrips_exactly(tmp_path):
    history = {'status': 'UNKNOWN', 'raw_completion': '原观察' * 300}
    for _ in range(12):
        history = {'status': 'UNKNOWN', 'request': {'previous': [history, history]}}
    document = {'findings': [history, history], 'release_gate': 'BLOCK'}
    source = tmp_path/'source.json'
    evidence.persist_document(source, document)
    current = evidence.load_review(source)
    seen = set()
    pending = [current]
    while pending:
        node = pending.pop()
        if not isinstance(node, (dict, list)) or id(node) in seen:
            continue
        seen.add(id(node))
        pending.extend(node.values() if isinstance(node, dict) else node)
    assert len(seen) < 150, len(seen)
    output = tmp_path/'roundtrip.json'
    evidence.persist_document(output, current)
    assert output.read_bytes() == source.read_bytes()
    # Only the immutable request history shares storage; live finding rows
    # remain independently editable, and another read owns its own graph.
    assert current['findings'][0] is not current['findings'][1]
    current['findings'][0]['routed'] = 'local annotation'
    assert 'routed' not in current['findings'][1]
    assert 'routed' not in evidence.load_review(source)['findings'][0]


def test_unclassified_large_payloads_are_not_made_shared(tmp_path):
    value = {'rows': [{'text': 'x'*9000}]}
    path = tmp_path/'source.json'
    evidence.persist_document(path, {'left': value, 'right': deepcopy(value)})
    current = evidence.load_review(path)
    current['left']['rows'][0]['text'] = 'changed'
    assert current['right']['rows'][0]['text'] == 'x'*9000


@pytest.mark.parametrize('value', [None, {'原文': ['a\n"\\', None, False, -0.0, 1e23]},
    {2: 'two', 1: 'one'}, ('tuple', {'large': '原文'*40_000})])
def test_streamed_receipt_hash_is_byte_identical_to_original(value):
    import hashlib
    from src.autoslice.canonical_json_hash import canonical_sha256
    from src.autoslice.context_assisted_audio import digest as context_digest
    from src.autoslice.span_phoneme_resolution import digest as span_digest
    expected = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    assert canonical_sha256(value) == context_digest(value) == span_digest(value) == expected


def test_streamed_receipt_hash_keeps_json_rejections_and_bounded_workspace():
    from src.autoslice.canonical_json_hash import canonical_sha256
    for value in (float('nan'), {'mixed': object()}):
        with pytest.raises((TypeError, ValueError)):
            canonical_sha256(value)
    leaf = {'text': 'original observation'*10_000}
    _result, peak = _peak(lambda: canonical_sha256([leaf]*40))
    assert peak < 2_000_000, peak


def test_graph_reuse_revalidates_blob_replaced_during_the_same_document_read(tmp_path, monkeypatch):
    from pathlib import Path
    from src.autoslice import argon_escalation_storage as store
    path = tmp_path/'source.json'
    packed = evidence.persist_document(path, {
        'first': {'request': {'raw': 'original'}},
        'second': {'request': {'raw': 'original'}},
    })
    reference = packed['first']['request']
    original = store.get_blob
    replaced = []
    def replace_after_read(root, sha):
        raw = original(root, sha)
        if sha == reference['sha256'] and not replaced:
            replaced.append(sha)
            Path(reference['path']).write_bytes(b'{}')
        return raw
    monkeypatch.setattr(store, 'get_blob', replace_after_read)
    with pytest.raises(ValueError, match='ARTIFACT_SHA_MISMATCH'):
        evidence.load_review(path)
