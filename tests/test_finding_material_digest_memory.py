"""A material fingerprint keeps its exact JSON identity without a full encoding."""
import hashlib
import json
import tracemalloc

import pytest

from src.autoslice.finding_context import digest


def legacy_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def test_repeated_history_digest_does_not_allocate_the_expanded_json():
    history = {'raw': '字幕原始证据\\\n' * 2048, 'round': 3}
    value = {'materials': [history] * 128}
    expected = legacy_digest(value)
    tracemalloc.start()
    try:
        actual = digest(value)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert actual == expected
    assert peak < 2 * 1024**2, peak


@pytest.mark.parametrize('value', [
    {'empty': [], 'x': [True, 1, 1.0, 0.0, -0.0, None]},
    {'emoji': '海边🌊', 'escape': '\\"\n\t', 'tuple': ('甲', {'乙': [1]})},
    {1: 'legacy integer key', 2: 'second'},
])
def test_material_digest_preserves_the_original_codec(value):
    assert digest(value) == legacy_digest(value)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), {1: 1, 'x': 2}, object(), '\ud800'])
def test_material_digest_preserves_invalid_value_rejections(value):
    with pytest.raises(Exception) as old:
        legacy_digest(value)
    with pytest.raises(type(old.value)):
        digest(value)


def test_material_digest_keeps_cycle_rejection():
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match='Circular reference'):
        digest(cyclic)
