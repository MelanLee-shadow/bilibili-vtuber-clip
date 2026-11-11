"""The encoder optimization must preserve existing seals and bounded batches."""
from enum import IntEnum
import hashlib
import json
import random

import pytest

from src.autoslice.canonical_json_hash import canonical_sha256


def reference(value, allow_nan=False):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=allow_nan).encode('utf-8')).hexdigest()


@pytest.mark.parametrize('allow_nan', [False, True])
def test_existing_canonical_bytes_across_json_shapes_and_mutation(allow_nan):
    rng = random.Random(81024)

    def member(depth):
        scalars = [None, True, False, -0.0, 0, -123456789123456789,
                   1.5, 1e-120, '中文\n\t\\\"', '𝄞\u2028', '']
        if depth == 0:
            return rng.choice(scalars)
        if rng.random() < .5:
            return [member(depth - 1) for _ in range(rng.randrange(6))]
        return {str(i) + rng.choice(['', '中文']): member(depth - 1)
                for i in range(rng.randrange(6))}

    class Number(IntEnum):
        ONE = 1

    values = [member(4) for _ in range(250)] + [
        {1: 'integer', 2: 'keys'}, {None: 1, False: 2}, (1, 'tuple'),
        {'enum': Number.ONE}, [float('nan'), float('inf'), -float('inf')],
        {'control': '\x00\b\f\r' * 20000}, list(range(60000)),
        {str(i): [i, 'escaped\n中文'] for i in range(5000)},
    ]
    for value in values:
        try:
            expected = reference(value, allow_nan)
        except (ValueError, TypeError) as exc:
            with pytest.raises(type(exc)):
                canonical_sha256(value, allow_nan=allow_nan)
        else:
            assert canonical_sha256(value, allow_nan=allow_nan) == expected
    leaf = {'text': 'retained original ' * 1000}
    value = {'history': [leaf] * 600}
    before = canonical_sha256(value, allow_nan=allow_nan)
    assert before == reference(value, allow_nan)
    leaf['text'] += 'current change'
    assert canonical_sha256(value, allow_nan=allow_nan) == reference(value, allow_nan) != before


def test_bounded_c_encoder_batches_for_shared_persisted_evidence(tmp_path, monkeypatch):
    from src.autoslice import review_evidence_store as store

    leaf = {'text': '真实保留证据' * 1000, 'scores': list(range(100))}
    path = tmp_path / 'ordinary-history.json'
    store.persist_document(path, {'history': [{'request': leaf} for _ in range(600)]})
    value = store.load_review(path)
    expected = reference(value)
    encode, iterencode = json.JSONEncoder.encode, json.JSONEncoder.iterencode
    containers, python_containers = [], []

    def measured(self, node):
        raw = encode(self, node)
        if isinstance(node, (dict, list, tuple)):
            containers.append(len(raw.encode('utf-8')))
        return raw

    def measured_iter(self, node, _one_shot=False):
        if isinstance(node, (dict, list, tuple)) and not _one_shot:
            python_containers.append(1)
        return iterencode(self, node, _one_shot=_one_shot)

    monkeypatch.setattr(json.JSONEncoder, 'encode', measured)
    monkeypatch.setattr(json.JSONEncoder, 'iterencode', measured_iter)
    assert canonical_sha256(value) == expected
    assert containers and max(containers) <= 65536
    assert python_containers == []


def test_circular_and_unsupported_values_keep_original_rejection():
    circular = list(range(40000))
    circular.append(circular)  # Cycle lies beyond the first bounded batch.
    for value in (circular, {'not-json': object()}, {'mixed': {1: 'a', '2': 'b'}}):
        with pytest.raises((ValueError, TypeError)) as original:
            reference(value)
        with pytest.raises(type(original.value)):
            canonical_sha256(value)
