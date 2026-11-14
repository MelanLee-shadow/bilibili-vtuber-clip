"""Ordinary persistence and live change controls for legacy discovery reuse."""
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from threading import Lock

import pytest

import src.autoslice.completed_review_checkpoint as m


def question(root, member='target', *, reverse=False, provider='model-a', number=1):
    bag = [{'id': member + '-a'}, {'id': member + '-b'}, {'id': member + '-a'}]
    if reverse:
        bag.reverse()
        bag = bag[1:] + bag[:1]  # include duplicates without making a palindrome
    material = {'current_support_summary': {'transcript_matches': bag}, 'ordered': [1, 2]}
    prompt = 'Judge this question; preserve all evidence.\n' + json.dumps(material)
    binding = {'stage': member, 'inputs': material, 'number': number,
               'provider': {'routing': {'model': provider}}}
    return m.CompletedReviewCheckpoint(root, binding, prompt)


def persist(root, monkeypatch, member='target', *, raw='  UNKNOWN original answer\n', legacy=True):
    # Restore the actual old byte-key producer, then use its ordinary save.
    # Readers receive the producer artifact without a per-member migration.
    with monkeypatch.context() as patch:
        if legacy:
            patch.setattr(m, '_request_material_binding', lambda binding, prompt: m._semantic_binding(binding))
        writer = question(root, member)
        writer.raw = raw
        writer.save()
        path = next(writer.directory.glob('*.json'))
    return path


def test_ordinary_load_save_reuses_unrelated_discovery_work(tmp_path, monkeypatch):
    root = tmp_path / 'records'
    originals = [persist(root, monkeypatch, 'other-' + str(i)) for i in range(12)]
    target = persist(root, monkeypatch)
    before = {p: p.read_bytes() for p in [*originals, target]}
    original_normalizer = m._request_material_binding
    counts = {'unrelated': 0, 'reads': 0}
    def normalize(binding, prompt):
        if str(binding.get('stage')).startswith('other-'):
            counts['unrelated'] += 1
        return original_normalizer(binding, prompt)
    real_snapshot = m.stable_regular_snapshot
    def snapshot(*args, **kwargs):
        counts['reads'] += 1
        return real_snapshot(*args, **kwargs)
    monkeypatch.setattr(m, '_request_material_binding', normalize)
    monkeypatch.setattr(m, 'stable_regular_snapshot', snapshot)
    reader = question(root, reverse=True)
    assert reader.response(lambda _: pytest.fail('original answer redispatched')) == '  UNKNOWN original answer\n'
    assert counts['unrelated'] == 0
    counts.update(unrelated=0, reads=0)
    reader.save()
    assert question(root, reverse=True).load() == reader.raw
    assert counts['reads'] >= 24  # content freshness remains mandatory
    assert counts['unrelated'] == 0  # responsible old mechanism fails here
    assert all(p.read_bytes() == raw for p, raw in before.items())


def test_warm_miss_new_directory_and_same_record_rewrite(tmp_path, monkeypatch):
    root = tmp_path / 'records'
    path = persist(root, monkeypatch, 'otherx')
    reader = question(root, 'target')
    assert reader.load() is None
    before = path.stat()
    # Same inode/length, restored mtime; turn an unrelated record into target.
    payload = path.read_bytes().replace(b'otherx', b'target')
    assert len(payload) == before.st_size
    path.write_bytes(payload)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    # The changed request's raw SHA is stale: nomination must reach strict load.
    with pytest.raises(m.CompletedReviewCheckpointError, match='BYTES_DRIFT'):
        reader.load()
    path.unlink()
    assert reader.load() is None
    valid = persist(root, monkeypatch)
    original = valid.read_bytes()
    assert reader.load() == '  UNKNOWN original answer\n'
    valid.unlink()
    assert reader.load() is None
    valid.write_bytes(original)
    assert reader.load() == '  UNKNOWN original answer\n'


@pytest.mark.parametrize('sibling', ['complete', 'tmp', 'corrupt', 'symlink'])
def test_warm_selected_record_rechecks_every_new_sibling(tmp_path, monkeypatch, sibling):
    root = tmp_path / 'records'
    selected = persist(root, monkeypatch)
    reader = question(root, reverse=True)
    assert reader.load() == '  UNKNOWN original answer\n'
    if sibling in {'complete', 'tmp'}:
        value = json.loads(selected.read_bytes())
        value['response'] = 'different answer'
        value['response_sha256'] = hashlib.sha256(b'different answer').hexdigest()
        data = m._canonical(value)
        sha = hashlib.sha256(data).hexdigest()
        name = sha + '.json' if sibling == 'complete' else '.' + sha + '.tmp'
        (selected.parent / name).write_bytes(data)
        reason = 'AMBIGUOUS'
    elif sibling == 'corrupt':
        (selected.parent / ('0' * 64 + '.json')).write_bytes(b'{')
        reason = 'BYTES_DRIFT'
    else:
        (selected.parent / ('0' * 64 + '.json')).symlink_to(selected)
        reason = 'UNREADABLE'
    with pytest.raises(m.CompletedReviewCheckpointError, match=reason):
        reader.response(lambda _: pytest.fail('damaged record redispatched'))


def test_warm_direct_hit_still_rejects_new_legacy_directory(tmp_path, monkeypatch):
    root = tmp_path / 'records'
    writer = question(root)
    writer.raw = 'new ordinary answer'
    writer.save()
    assert question(root).load() == writer.raw
    with pytest.raises(m.CompletedReviewCheckpointError, match='AMBIGUOUS'):
        persist(root, monkeypatch, raw='old different answer')
    with pytest.raises(m.CompletedReviewCheckpointError, match='AMBIGUOUS'):
        question(root).load()


def test_provider_question_order_and_unseen_member_stay_distinct(tmp_path, monkeypatch):
    root = tmp_path / 'records'
    persist(root, monkeypatch)
    assert question(root, reverse=True).load() == '  UNKNOWN original answer\n'
    assert question(root, provider='model-b').load() is None
    assert question(root, 'new-member').load() is None
    newcomer = question(root, 'new-member', reverse=True)
    newcomer.raw = 'new member original bytes'
    newcomer.save()
    assert question(root, 'new-member').load() == newcomer.raw
    ordered = question(root)
    ordered.binding['inputs']['ordered'].reverse()
    changed = m.CompletedReviewCheckpoint(root, ordered.binding, ordered.prompt)
    assert changed.load() is None
    duplicate = question(root)
    duplicate.binding['inputs']['current_support_summary']['transcript_matches'].pop()
    assert m.CompletedReviewCheckpoint(root, duplicate.binding, duplicate.prompt).load() is None
    with pytest.raises(m.CompletedReviewCheckpointError, match='BINDING_DRIFT'):
        m.CompletedReviewCheckpoint(root, question(root).binding,
                                    question(root).prompt + '\nNew normative rule').load()


def test_python_equal_numeric_binding_does_not_false_exclude(tmp_path, monkeypatch):
    root = tmp_path / 'records'
    persist(root, monkeypatch)
    # Warm a discovery hint using a different question, then numerically equal
    # old bindings must still nominate the same original record.
    assert question(root, 'other').load() is None
    assert question(root, number=1.0, reverse=True).load() == '  UNKNOWN original answer\n'
    assert question(root, number=True, reverse=True).load() == '  UNKNOWN original answer\n'


def test_parallel_readers_and_writer_conflict_visible(tmp_path, monkeypatch):
    root = tmp_path / 'records'
    selected = persist(root, monkeypatch)
    for i in range(5):
        persist(root, monkeypatch, 'other-' + str(i))
    calls, lock = Counter(), Lock()
    normalizer = m._request_material_binding
    def counted(binding, prompt):
        if str(binding.get('stage')).startswith('other-'):
            with lock:
                calls[binding['stage']] += 1
        return normalizer(binding, prompt)
    monkeypatch.setattr(m, '_request_material_binding', counted)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: question(root, reverse=True).load(), range(8))) == ['  UNKNOWN original answer\n'] * 8
        assert calls == Counter()
        future = pool.submit(persist, root, monkeypatch, raw='parallel conflicting answer')
        with pytest.raises(m.CompletedReviewCheckpointError, match='AMBIGUOUS'):
            future.result()
    with pytest.raises(m.CompletedReviewCheckpointError, match='AMBIGUOUS'):
        question(root, reverse=True).load()
    assert selected.exists()


def test_evicted_hints_do_not_change_results_and_normalizer_errors_recur(tmp_path, monkeypatch):
    root = tmp_path / 'records'
    for i in range(5):
        persist(root, monkeypatch, 'other-' + str(i))
    persist(root, monkeypatch)
    monkeypatch.setattr(m, '_LEGACY_HINT_LIMIT', 2, raising=False)
    for _ in range(3):
        assert question(root, reverse=True).load() == '  UNKNOWN original answer\n'
    original = m._request_material_binding
    attempts = []
    reader = question(root, reverse=True)
    def reject(binding, prompt):
        if binding.get('stage') == 'target':
            attempts.append(1)
            raise TypeError('invalid old semantic binding')
        return original(binding, prompt)
    monkeypatch.setattr(m, '_request_material_binding', reject)
    for _ in range(2):
        with pytest.raises(m.CompletedReviewCheckpointError, match='UNREADABLE'):
            reader.load()
    assert len(attempts) == 2


@pytest.mark.parametrize('encoding', ['unicode_key', 'unicode_value', 'whitespace'])
def test_stage_prefilter_preserves_equivalent_json_spellings(tmp_path, monkeypatch, encoding):
    root = tmp_path / 'records'
    path = persist(root, monkeypatch)
    data = path.read_bytes()
    if encoding == 'unicode_key':
        data = data.replace(b'"stage":', b'"st\\u0061ge":')
    elif encoding == 'unicode_value':
        data = data.replace(b'"stage":"target"', b'"stage":"\\u0074arget"')
    else:
        data = data.replace(b'"stage":', b'"stage" \r\n :\t')
    assert data != path.read_bytes()
    replacement = path.parent / (hashlib.sha256(data).hexdigest() + '.json')
    path.unlink()
    replacement.write_bytes(data)
    assert question(root, reverse=True).load() == '  UNKNOWN original answer\n'


def test_unrelated_stage_with_matching_nested_key_stays_unrelated(tmp_path, monkeypatch):
    root = tmp_path / 'records'
    target = persist(root, monkeypatch)
    other = question(root, 'other')
    other.binding['inputs']['nested'] = {'stage': 'target'}
    other = m.CompletedReviewCheckpoint(root, other.binding, other.prompt)
    other.raw = 'not this question'
    other.save()
    assert question(root, reverse=True).load() == '  UNKNOWN original answer\n'
    assert target.exists()
