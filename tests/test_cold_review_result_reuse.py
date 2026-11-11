"""Ordinary persisted pending inputs reuse work, never a release or stale proof."""
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from src.autoslice import review_evidence_store as store
from src.autoslice import validation_memo
from src.autoslice.continuation_subchecks import (
    cold_read_session, end_cold_read, session_validated,
)


@pytest.mark.parametrize('successor', [False, True])
def test_interrupted_writer_pending_reuses_classification_not_release(tmp_path, monkeypatch, successor):
    from tests import test_continuation_authority_class as fixture
    from src.autoslice import finding_lifecycle as lifecycle, surface_canon
    from src.autoslice.final_review_contract import FinalReviewContractError, validate_final_review_release
    import dataclasses

    def newer_display_rule():
        template = surface_canon._CAPTION_DISPLAY_SURFACE_RULES[0]
        monkeypatch.setattr(surface_canon, '_CAPTION_DISPLAY_SURFACE_RULES', (
            *surface_canon._CAPTION_DISPLAY_SURFACE_RULES,
            dataclasses.replace(template, surface='ttt15', canonical='TTT15')))

    chat, text, context = fixture._interrupted_writer_state(
        tmp_path, restart=newer_display_rule if successor else None)
    path = tmp_path / 'candidate.chat-authority.json'
    original = path.read_bytes()
    calls = []
    classify = lifecycle.release_reason_codes

    def counted(rows, **kwargs):
        calls.append(deepcopy(rows))
        return classify(rows, **kwargs)

    monkeypatch.setattr(lifecycle, 'release_reason_codes', counted)
    with cold_read_session():
        first = fixture._cold_read(store.load_review(path), text, context)
        count = len(calls)
        assert count > 0
        second = fixture._cold_read(store.load_review(path), text, context)
        assert second == first and second['release_gate'] == 'BLOCK'
        assert len(calls) == count
        # The same entry is still rejected by the public release consumer.
        with pytest.raises(FinalReviewContractError):
            validate_final_review_release(second, expected_srt_sha256=first['reviewed_srt_sha256'])
        assert len(calls) == count
        # Active SRT and parent-scope decisions were never cached by this owner.
        with pytest.raises(fixture.pipeline.CurrentTextContinuationError):
            fixture._cold_read(chat, text.replace('完整收束', '完整收束了'), context)
        end_cold_read()
        assert fixture._cold_read(store.load_review(path), text, context) == first
        assert len(calls) > count
    assert path.read_bytes() == original


def test_new_cold_consumers_share_expansion_but_receive_detached_values(tmp_path, monkeypatch):
    original = {'status': 'UNKNOWN', 'request': {'new_member': '保留真实输入' * 3000}}
    packed = store.persist_document(tmp_path / 'new-owner.json', original)
    expansions = []
    expand = store.expand

    def counted(value, **kwargs):
        if kwargs.get('_active') is None:
            expansions.append(1)
        return expand(value, **kwargs)

    monkeypatch.setattr(store, 'expand', counted)

    @store.cold_input
    def first(value):
        return value

    @store.cold_input
    def second(value):
        return value['status'], value['request']

    with cold_read_session():
        current = first(deepcopy(packed))
        count = len(expansions)
        assert count == 1 and current == original
        current['request']['new_member'] = 'consumer-local edit'
        assert second(deepcopy(packed)) == ('UNKNOWN', original['request'])
        assert len(expansions) == count
        end_cold_read()
        assert first(packed) == original
        assert len(expansions) > count


@pytest.mark.parametrize('damage', ['same_inode', 'replace', 'delete'])
def test_cold_expansion_rechecks_nested_blob_and_recovers_missing_input(tmp_path, damage):
    path = tmp_path / 'nested.json'
    original = {'status': 'UNKNOWN', 'request': {'adjudication': {'new_member': '事实原件' * 1000}}}
    packed = store.persist_document(path, original)
    outer = json.loads(Path(packed['request']['path']).read_bytes())
    blob = Path(outer['adjudication']['path'])
    raw = blob.read_bytes()
    with cold_read_session():
        assert store.load_review(path) == original
        assert store.load_review(path) == original
        before = blob.stat()
        if damage == 'same_inode':
            blob.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
            os.utime(blob, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert blob.stat().st_ino == before.st_ino and blob.stat().st_size == before.st_size
        elif damage == 'replace':
            store.store.atomic_write(blob, raw)
            assert blob.stat().st_ino != before.st_ino
            assert store.load_review(path) == original
            blob.write_bytes(b'{}')
        else:
            blob.unlink()
        for _ in range(2):
            with pytest.raises(ValueError):
                store.load_review(path)
        store.store.blob(blob.parent.parent, raw)
        assert store.load_review(path) == original


def test_session_validated_replays_dependencies_into_its_parent(tmp_path):
    from src.autoslice.evidence_identity import read_file_bytes

    source = tmp_path / 'source'
    source.write_bytes(b'valid source')
    calls = []

    @session_validated('new-file-consumer')
    def validate():
        calls.append(1)
        if read_file_bytes(source) != b'valid source':
            raise ValueError('source binding rejected')

    @validation_memo.memoized_validation(lambda: str(source))
    def parent():
        return validate()

    with cold_read_session():
        validate()
        parent()  # Its dependency capture must inherit the session hit.
        assert len(calls) == 1
        before = source.stat()
        source.write_bytes(b'wrong source')  # Same inode, length and restored mtime.
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        for _ in range(2):
            with pytest.raises(ValueError, match='source binding rejected'):
                parent()
        assert len(calls) == 3  # Arbitrary failures never become cache entries.


def test_session_key_walks_shared_content_and_detects_mutation_without_tree_json(monkeypatch):
    calls, encoded = [], []
    dumps = json.dumps

    def scalar_only(value, **kwargs):
        assert not isinstance(value, (dict, list, tuple)), 'serialized the expanded argument tree'
        encoded.append(1)
        return dumps(value, **kwargs)

    monkeypatch.setattr(validation_memo.json, 'dumps', scalar_only)
    leaf = {'raw': 'one retained original' * 100}
    graph = {'history': [leaf] * 200}

    @session_validated('shared-input')
    def validate(value):
        calls.append(1)

    with cold_read_session():
        validate(graph)
        validate(deepcopy(graph))
        assert len(calls) == 1
        assert len(encoded) < 20  # Counts unique graph atoms, not 400 copies.
        leaf['raw'] += 'changed'
        validate(graph)
        assert len(calls) == 2
    assert validation_memo.validation_content_key([True]) != validation_memo.validation_content_key([1])
    assert validation_memo.validation_content_key([1]) != validation_memo.validation_content_key((1,))
    assert validation_memo.validation_content_key(b'original') != validation_memo.validation_content_key('original')


def test_session_result_budget_evicts_without_limiting_valid_inputs(monkeypatch):
    monkeypatch.setattr(validation_memo.SessionValidationMemo, 'MAX_BYTES', 1500)
    memo = validation_memo.SessionValidationMemo()
    for index in range(100):
        memo.remember(index, {'payload': str(index) * 100}, ())
        assert memo.size <= memo.MAX_BYTES and len(memo.entries) <= memo.MAX_ENTRIES
    memo.remember('oversize', {'payload': 'x' * 3000}, ())
    assert memo.recall('oversize') == (False, None)


def test_distinct_persisted_owners_reuse_immutable_blob_and_replay_parent_binding(tmp_path, monkeypatch):
    request = {'shared_original': 'the actual retained evidence ' * 1000}
    paths = [tmp_path / 'owner-a.json', tmp_path / 'owner-b.json']
    packed = [store.persist_document(path, {'owner': owner, 'request': request})
              for path, owner in zip(paths, ('a', 'b'))]
    owner_bytes = [path.read_bytes() for path in paths]
    assert packed[0]['request'] == packed[1]['request']
    blob = Path(packed[0]['request']['path'])
    before, expansions = blob.read_bytes(), []
    expand = store.expand

    def counted(value, **kwargs):
        if isinstance(value, dict) and 'shared_original' in value:
            expansions.append(1)
        return expand(value, **kwargs)

    monkeypatch.setattr(store, 'expand', counted)

    @validation_memo.memoized_validation(lambda: str(paths[1]))
    def parent():
        return store.load_review(paths[1])

    with cold_read_session():
        first = store.load_review(paths[0])
        first['request']['shared_original'] = 'local consumer edit'
        second = parent()
        assert second == {'owner': 'b', 'request': request}
        assert len(expansions) == 1  # The second owner reuses the blob, not owner a.
        info = blob.stat()
        blob.write_bytes(bytes([before[0] ^ 1]) + before[1:])
        os.utime(blob, ns=(info.st_atime_ns, info.st_mtime_ns))
        assert blob.stat().st_ino == info.st_ino and blob.stat().st_size == info.st_size
        for _ in range(2):
            with pytest.raises(ValueError):
                parent()  # Its first capture inherited the cross-owner blob hit.
        blob.write_bytes(before)
        assert parent() == {'owner': 'b', 'request': request}
        owner_before = paths[1].read_bytes()
        info = paths[1].stat()
        paths[1].write_bytes(owner_before.replace(b'"owner": "b"', b'"owner": "c"'))
        os.utime(paths[1], ns=(info.st_atime_ns, info.st_mtime_ns))
        assert parent() == {'owner': 'c', 'request': request}
        paths[1].write_bytes(owner_before)
        assert parent() == {'owner': 'b', 'request': request}
    assert [path.read_bytes() for path in paths] == owner_bytes


def test_shared_blob_cache_preserves_explicit_nested_store_binding(tmp_path):
    original_root = tmp_path / 'original'
    original_root.mkdir()
    packed = store.persist_document(original_root / 'owner.json', {
        'request': {'adjudication': {'retained_original': 'unchanged source ' * 1000}}})
    reference = dict(packed['request'])
    raw = Path(reference['path']).read_bytes()
    relocated = tmp_path / 'relocated' / 'evidence-store'
    assert store.store.blob(relocated, raw) == reference['sha256']
    reference['path'] = str(relocated / 'blobs' / (reference['sha256'] + '.bin'))
    with cold_read_session():
        assert store.expand({'request': reference})['request']['adjudication']['retained_original']
        # The same outer bytes now require their nested blob in the explicit
        # relocated store; a prior native-path hit cannot satisfy that binding.
        with pytest.raises(ValueError, match='REVIEW_EVIDENCE_BLOB_MISSING'):
            store.expand({'request': reference}, store_root=relocated)


def test_retained_blob_cache_survives_one_off_persisted_owner_scan(tmp_path, monkeypatch):
    paths = [tmp_path / ('owner-' + str(i) + '.json') for i in range(50)]
    for index, path in enumerate(paths):
        store.persist_document(path, {'owner': index,
            'request': {'retained_original': str(index) + ' evidence ' * 1000}})
    expand, first_expansions = store.expand, []

    def counted(value, **kwargs):
        if isinstance(value, str) and value.startswith('0 evidence '):
            first_expansions.append(1)
        return expand(value, **kwargs)

    monkeypatch.setattr(store, 'expand', counted)
    with cold_read_session():
        first = store.load_review(paths[0])
        for path in paths[1:]:
            store.load_review(path)
        assert store.load_review(paths[0]) == first
        assert len(first_expansions) == 1


def test_persisted_shared_history_uses_memory_budget_and_preserves_blob_guard(tmp_path):
    path = tmp_path / 'retained-history.json'
    shared = {'retained_original': 'same immutable evidence ' * 1000}
    packed = store.persist_document(path, {'history': [{'request': shared} for _ in range(500)]})
    owner_before = path.read_bytes()
    calls = []

    @validation_memo.memoized_validation(lambda: str(path))
    def parent():
        calls.append(1)
        return store.load_review(path)

    iterencode, python_containers = json.JSONEncoder.iterencode, []

    def measured(self, value, _one_shot=False):
        if isinstance(value, (dict, list, tuple)) and not _one_shot:
            python_containers.append(1)
        return iterencode(self, value, _one_shot=_one_shot)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(json.JSONEncoder, 'iterencode', measured)
        first = parent()
        assert len(store.encoded(first)) > 1_000_000
        second = parent()
        assert len(calls) == 2  # Original JSON budget still leaves this uncached.
        assert python_containers == []
    second['history'][0]['request']['retained_original'] = 'local edit'
    assert parent() == first
    history = json.loads(Path(packed['history']['path']).read_bytes())
    request = json.loads(Path(history[0]['request']['path']).read_bytes())
    blob = Path(request['retained_original']['path'])
    raw, info = blob.read_bytes(), blob.stat()
    blob.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
    os.utime(blob, ns=(info.st_atime_ns, info.st_mtime_ns))
    with pytest.raises(ValueError):
        parent()
    blob.write_bytes(raw)
    assert parent() == first
    assert path.read_bytes() == owner_before


def test_named_blob_reuse_survives_sibling_publish_but_preserves_directory_guards(tmp_path, monkeypatch):
    path = tmp_path / 'owner.json'
    original = {'request': {'retained_original': 'original named evidence ' * 1000}}
    packed = store.persist_document(path, original)
    expand, reads = store.expand, []

    def counted(value, **kwargs):
        if kwargs.get('_active') is None:
            reads.append(1)
        return expand(value, **kwargs)

    monkeypatch.setattr(store, 'expand', counted)

    @session_validated('directory-search-owner')
    def parent():
        assert store.load_review(path) == original
        if (tmp_path / 'new-relevant-receipt.json').exists():
            raise ValueError('directory search changed')

    root = store.evidence_store_root(tmp_path)
    with cold_read_session():
        assert store.load_review(path) == original
        before = len(reads)
        # An ordinary sibling publisher changes the same parent directories.
        store.persist_document(tmp_path / 'other-owner.json', {'request': 'different evidence ' * 1000})
        assert store.load_review(path) == original and len(reads) == before
        parent()  # Its capture inherits full current directory metadata.
        store.persist_document(tmp_path / 'new-relevant-receipt.json', {'request': 'new receipt'})
        for _ in range(2):
            with pytest.raises(ValueError, match='directory search changed'):
                parent()
        root.chmod(0o755)
        try:
            before = len(reads)
            assert store.load_review(path) == original
            assert len(reads) > before  # Recompute the original reader's decision.
        finally:
            root.chmod(0o700)
        blob = Path(packed['request']['path'])
        blob.chmod(0o644)
        try:
            with pytest.raises(ValueError, match='ARTIFACT_UNSAFE'):
                store.load_review(path)
        finally:
            blob.chmod(0o600)
        assert store.load_review(path) == original


def test_wide_persisted_input_bounds_key_work_without_skipping_validation(tmp_path, monkeypatch):
    # A large prefix exhausts the old key memo before a later shared history.
    # The normal publisher/reader introduces the same DAG shape as cold audits.
    shared = {'raw': ['original-' + str(i) for i in range(1400)]}
    original = {'audit_prefix': list(range(180_000)),
                'receipt_history': [{'request': shared} for _ in range(300)],
                'status': 'UNKNOWN'}
    path = tmp_path / 'new-wide-owner.json'
    store.persist_document(path, original)
    before = path.read_bytes()
    calls, encoded = [], []
    dumps = json.dumps

    def measured(value, *args, **kwargs):
        assert not isinstance(value, (dict, list, tuple)), 'serialized full cold arguments'
        encoded.append(1)
        return dumps(value, *args, **kwargs)

    @session_validated('wide-persisted-owner')
    def validate(value):
        calls.append(1)
        if value['status'] != 'UNKNOWN' or value['receipt_history'][0]['request']['raw'][0] != 'original-0':
            raise ValueError('source input rejected')

    with cold_read_session():
        current = store.load_review(path)
        monkeypatch.setattr(validation_memo.json, 'dumps', measured)
        assert validate(current) is None
        again = store.load_review(path)
        assert validate(again) is None
        assert len(calls) == 1
        again['receipt_history'][0]['request']['raw'][0] = 'changed input'
        with pytest.raises(ValueError, match='source input rejected'):
            validate(again)
        assert current == original
    assert calls and len(encoded) < 50_000
    assert path.read_bytes() == before


def test_key_work_budget_preserves_oversized_persisted_owner_validation(tmp_path):
    original = {'audit_prefix': [0] * 1_100_000, 'status': 'UNKNOWN'}
    path = tmp_path / 'oversized-owner.json'
    store.persist_document(path, original)
    before = path.read_bytes()
    calls = []

    @session_validated('oversized-persisted-owner')
    def validate(value):
        calls.append(1)
        if value['status'] != 'UNKNOWN':
            raise ValueError('source input rejected')

    with cold_read_session():
        current = store.load_review(path)
        assert validate(current) is None
        assert validate(current) is None
        current['status'] = 'CHANGED'
        with pytest.raises(ValueError, match='source input rejected'):
            validate(current)
    assert len(calls) >= 2
    assert path.read_bytes() == before


def test_release_and_binding_failures_are_not_session_results(tmp_path, monkeypatch):
    from tests import test_continuation_authority_class as fixture
    from src.autoslice import final_review_contract as contract

    text = '1\n00:00:00,000 --> 00:00:01,000\n完整收束\n'
    clean = fixture._review(text)
    packed = store.persist_document(tmp_path / 'clean.json', clean)
    calls = []
    integrity = contract._validate_review_integrity

    def counted(*args, **kwargs):
        calls.append(1)
        return integrity(*args, **kwargs)

    monkeypatch.setattr(contract, '_validate_review_integrity', counted)
    with cold_read_session():
        for _ in range(2):
            assert contract.validate_final_review_release(packed)['release_gate'] == 'PASS'
        assert len(calls) == 2  # No remembered release verdict.
        for _ in range(2):
            with pytest.raises(contract.FinalReviewContractError, match='SRT_BINDING_MISMATCH'):
                contract.validate_final_review_release(packed, expected_srt_sha256='sha256:' + '0' * 64)
        assert len(calls) == 4


def test_pending_result_does_not_survive_a_changed_contract(tmp_path, monkeypatch):
    from tests import test_continuation_authority_class as fixture
    from src.autoslice import final_review_contract as contract, final_review_meaning_policy as policy

    chat, text, context = fixture._interrupted_writer_state(tmp_path)
    with cold_read_session():
        fixture._cold_read(chat, text, context)
        monkeypatch.setattr(policy, 'POLICY_VERSION', policy.POLICY_VERSION + '-new')
        for _ in range(2):
            with pytest.raises(contract.FinalReviewContractError, match='MEANING_POLICY_REVIEW_REQUIRED'):
                contract.validate_final_review_release(chat['final_review_audit'])


def test_retained_loader_enters_and_preserves_one_existing_cold_session(tmp_path):
    from tests.test_retained_reviewed_draft import _fixture, _load
    from src.autoslice.continuation_subchecks import session_memo

    spec, out, padded, _ = _fixture(tmp_path)
    observed = []

    def parent(**kwargs):
        observed.append(session_memo('retained-test'))
        assert observed[-1] is not None
        return {'status': 'INPUT_VALIDATED'}

    assert session_memo('retained-test') is None
    assert _load(spec, out, padded, validator=parent)['proof']['fresh_review_required'] is True
    assert session_memo('retained-test') is None
    with cold_read_session():
        inherited = session_memo('retained-test')
        _load(spec, out, padded, validator=parent)
        assert observed[-1] is inherited and session_memo('retained-test') is inherited
