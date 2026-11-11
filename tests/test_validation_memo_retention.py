"""Temporary size-walk references must not wait for cyclic GC to disappear."""
import gc
import json
import sys
import weakref

import pytest

from src.autoslice.validation_memo import _small_json_result, memoized_validation


class _TrackedText(str):
    pass


@pytest.fixture
def cyclic_gc_disabled():
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()
        gc.collect()


def _size_probe(large):
    leaf = _TrackedText('original retained value')
    reference = weakref.ref(leaf)
    # The small case reaches the real legacy JSON fallback. The large case
    # exceeds the native size budget before visiting the weak-reference leaf.
    value = {'raw': 'x' * 1_000_000, 'tail': leaf} if large else {'raw': leaf}
    return _small_json_result(value), reference


@pytest.mark.skipif(sys.implementation.name != 'cpython', reason='checks prompt reference-count release')
@pytest.mark.parametrize('large,expected', [(False, True), (True, False)])
def test_size_walk_releases_input_before_cyclic_gc(cyclic_gc_disabled, large, expected):
    accepted, reference = _size_probe(large)
    assert accepted is expected
    # Only a bool and weakref escape the probe; assertion reporting cannot
    # become an accidental strong owner of the original input.
    released = reference() is None
    assert released


@pytest.mark.skipif(sys.implementation.name != 'cpython', reason='checks prompt reference-count release')
def test_public_oversize_result_is_uncached_and_released_with_validator_alive(cyclic_gc_disabled):
    calls = []

    @memoized_validation(lambda: 'oversize-retention-check')
    def validate():
        calls.append(1)
        return {'raw': 'x' * 1_000_000, 'tail': _TrackedText('original')}

    def consume_once():
        value = validate()
        assert len(value['raw']) == 1_000_000
        return weakref.ref(value['tail'])

    first = consume_once()
    first_released = first() is None
    second = consume_once()
    second_released = second() is None
    assert len(calls) == 2
    assert callable(validate)  # The decorated validator remains a live owner.
    assert first_released and second_released


def test_json_character_budget_is_unchanged(cyclic_gc_disabled):
    for size in (999_998, 999_999):
        value = 'x' * size
        assert _small_json_result(value) is (len(json.dumps(value, ensure_ascii=False)) <= 1_000_000)


def test_non_json_result_still_returns_false(cyclic_gc_disabled):
    assert _small_json_result({'unsupported': object()}) is False


def test_cyclic_result_still_returns_false(cyclic_gc_disabled):
    value = []
    value.append(value)
    try:
        assert _small_json_result(value) is False
    finally:
        value.clear()  # Release the cycle deliberately owned by this fixture.


def test_unexpected_encoder_exception_keeps_original_identity(cyclic_gc_disabled):
    error = RuntimeError('original JSON iterator failure')

    class BrokenDictionary(dict):
        def items(self):
            raise error

    try:
        with pytest.raises(RuntimeError) as caught:
            _small_json_result(BrokenDictionary(value=1))
        assert caught.value is error
    finally:
        error.__traceback__ = None
