"""Separate product memo cleanup from the standard encoder's error lifetime."""
import gc
import hashlib
import json
import sys
import weakref

import pytest

from src.autoslice import canonical_json_hash as codec


class _TrackedText(str):
    pass


class _TrackedUnsupported:
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


def _canonical_bytes(value, *, allow_nan=False):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=allow_nan).encode("utf-8")


def _stdlib_digest(value):
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256()
    for fragment in encoder.iterencode(value):
        digest.update(fragment.encode("utf-8"))
    return digest.hexdigest()


def _active_memo_lengths():
    frame = sys._getframe(1)
    try:
        for _ in range(12):
            if frame is None:
                break
            if frame.f_code is codec.canonical_sha256.__code__:
                return len(frame.f_locals["sizes"]), len(frame.f_locals["encoded"])
            frame = frame.f_back
        raise AssertionError("active canonical hash frame missing")
    finally:
        del frame


def _lifetime_probe(case, hash_call=None):
    leaf = _TrackedUnsupported() if case == "unsupported" else _TrackedText("original graph")
    reference = weakref.ref(leaf)
    value = {"a_payload": "x" * (70_000 if case == "large" else 8), "z_leaf": leaf}
    if case == "prefilled":
        value = {name: {"payload": "x" * 4096} for name in ("a", "b", "c")}
        value["z_leaf"] = leaf
    if case == "cycle":
        value["cycle"] = value
    exits = []
    target_code = codec.canonical_sha256.__code__
    previous_profile = sys.getprofile()

    def observe_return(frame, event, _arg):
        if event == "return" and frame.f_code is target_code:
            # Read only two counts. Do not retain frames, locals dictionaries,
            # memo objects, arguments, returned values, or exceptions.
            exits.append((len(frame.f_locals["sizes"]), len(frame.f_locals["encoded"])))

    sys.setprofile(observe_return)
    try:
        digest = (hash_call or codec.canonical_sha256)(value)
    except (TypeError, ValueError) as exc:
        outcome = (type(exc).__name__, str(exc))
    else:
        expected = hashlib.sha256(_canonical_bytes(value)).hexdigest()
        outcome = ("digest", digest, expected)
    finally:
        sys.setprofile(previous_profile)
        if case == "cycle":
            value.pop("cycle")  # Release the cycle deliberately owned by this fixture.
    return outcome, reference, tuple(exits)


@pytest.mark.skipif(sys.implementation.name != "cpython", reason="checks prompt reference-count release")
@pytest.mark.parametrize("case", ["small", "large"])
def test_success_releases_source_graph_before_cyclic_gc(cyclic_gc_disabled, case):
    outcome, reference, exits = _lifetime_probe(case)
    assert outcome[0] == "digest" and outcome[1] == outcome[2]
    assert exits == ((0, 0),)
    released = reference() is None
    assert released


@pytest.mark.skipif(sys.implementation.name != "cpython", reason="checks prompt reference-count release")
@pytest.mark.parametrize("case", ["small", "prefilled"])
def test_controlled_encoder_error_clears_own_memos_without_stdlib_markers(
        cyclic_gc_disabled, monkeypatch, case):
    before = []
    original_iterencode = json.JSONEncoder.iterencode

    def fail_without_markers(_encoder, _value, _one_shot=False):
        if _one_shot:
            return original_iterencode(_encoder, _value, _one_shot=True)
        # This ordinary function creates neither recursive encoder functions
        # nor a markers dictionary. Preserve scalar evidence that the product
        # caches were populated before the injected error.
        before.append(_active_memo_lengths())
        raise TypeError("controlled encoder failure before markers")

    monkeypatch.setattr(json.JSONEncoder, "iterencode", fail_without_markers)
    outcome, reference, exits = _lifetime_probe(case)
    assert outcome == ("TypeError", "controlled encoder failure before markers")
    assert len(before) == 1 and before[0][0] > 0
    if case == "prefilled":
        assert before[0][1] > 0
    assert exits == ((0, 0),)
    released = reference() is None
    assert released


@pytest.mark.skipif(sys.implementation.name != "cpython", reason="compares actual CPython encoder lifetime")
@pytest.mark.parametrize("case", ["unsupported", "cycle"])
def test_real_json_error_lifetime_matches_stdlib_and_releases_after_gc(cyclic_gc_disabled, case):
    standard, standard_reference, standard_exits = _lifetime_probe(case, _stdlib_digest)
    actual, reference, exits = _lifetime_probe(case)
    assert standard_exits == ()
    assert actual == standard
    assert actual[0] == ("TypeError" if case == "unsupported" else "ValueError")
    assert exits == ((0, 0),)
    # On the observed CPython 3.12, _make_iterencode keeps markers alive on
    # these two real errors. Preserve this fact instead of claiming immediate
    # all-error release; also allow a future stdlib to repair its own lifetime.
    standard_released = standard_reference() is None
    actual_released = reference() is None
    assert actual_released == standard_released
    gc.collect()
    standard_released_after_gc = standard_reference() is None
    actual_released_after_gc = reference() is None
    assert standard_released_after_gc and actual_released_after_gc



def test_hash_matches_original_canonical_bytes_and_is_deterministic():
    value = {"z": [None, True, False, -17, 1.25, -0.0],
             "a": {"unicode": "汉字🙂", "escapes": "\\\"\n\t\u0000"}}
    expected = hashlib.sha256(_canonical_bytes(value)).hexdigest()
    reordered = dict(reversed(tuple(value.items())))
    assert codec.canonical_sha256(value) == expected
    assert codec.canonical_sha256(reordered) == expected
    assert codec.canonical_sha256(value) == expected


def test_shared_dag_keeps_every_canonical_occurrence():
    leaf = {"payload": "x" * 4_096, "values": [1, 2, 3]}
    branch = [leaf, leaf]
    value = {"left": [branch] * 12, "right": branch}
    expected = hashlib.sha256(_canonical_bytes(value)).hexdigest()
    assert codec.canonical_sha256(value) == expected
    assert value["left"][0] is branch and branch[0] is branch[1]


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_actual_encoded_byte_budget_boundary_preserves_digest(offset):
    # Each distinct child is encoded separately: its upper bound exceeds
    # _BATCH_BYTES // 4, while its actual canonical bytes are exactly 4096.
    # The original 8 MiB memo is therefore tested just below, at, and above
    # capacity. No production budget constant is patched by this test.
    assert codec._MEMO_BYTES == 8 * 1024 * 1024
    payload = "x" * (4096 - len(_canonical_bytes({"payload": ""})))
    sample = {"payload": payload}
    assert len(_canonical_bytes(sample)) == 4096
    count = codec._MEMO_BYTES // 4096 + offset
    value = [{"payload": payload} for _ in range(count)]
    expected = hashlib.sha256(_canonical_bytes(value)).hexdigest()
    assert codec.canonical_sha256(value) == expected


def test_nonfinite_policy_and_legacy_json_fallback_are_unchanged():
    for value in (float("nan"), float("inf"), -float("inf")):
        expected = hashlib.sha256(_canonical_bytes({"value": value}, allow_nan=True)).hexdigest()
        assert codec.canonical_sha256({"value": value}, allow_nan=True) == expected
        with pytest.raises(ValueError, match="Out of range float values are not JSON compliant"):
            codec.canonical_sha256({"value": value})
    value = {7: _TrackedText("legacy key and scalar subclass")}
    assert codec.canonical_sha256(value) == hashlib.sha256(_canonical_bytes(value)).hexdigest()


def test_unexpected_encoder_exception_keeps_original_identity():
    error = RuntimeError("original encoder failure")

    class BrokenDictionary(dict):
        def items(self):
            raise error

    try:
        with pytest.raises(RuntimeError) as caught:
            codec.canonical_sha256({"nested": BrokenDictionary(value=1)})
        assert caught.value is error
    finally:
        error.__traceback__ = None
