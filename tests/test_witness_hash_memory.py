import hashlib
import json
import tracemalloc
from copy import deepcopy

import pytest

from src.autoslice import exact_final_witness_authority as authority
from tests.test_identity_g6_audio_question import _ordinary_gate_adjudication, _request


def legacy(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize(
    "value",
    [
        {"汉🌊": ["\\", "\n", True, None, 1, -0.0]},
        {"floats": [float("nan"), float("inf"), -float("inf")]},
        {1: "integer", 2: "second"},
        {None: "null", "a": "string"},
        {"nested": [{"n": 1}] * 16},
    ],
)
def test_hash_matches_standard_json_or_same_error(value):
    try:
        expected = legacy(value)
    except Exception as error:
        with pytest.raises(type(error)):
            authority._sha_json(value)
    else:
        assert authority._sha_json(value) == expected


@pytest.mark.parametrize(
    "value", [{1: "integer", "x": "string"}, {"bad": object()}, {"bad": "\ud800"}]
)
def test_invalid_values_match_original_exception(value):
    with pytest.raises(Exception) as old:
        legacy(value)
    with pytest.raises(type(old.value)):
        authority._sha_json(value)


def test_cycle_matches_original_exception():
    value = []
    value.append(value)
    with pytest.raises(ValueError):
        legacy(value)
    with pytest.raises(ValueError):
        authority._sha_json(value)


def test_ordinary_audio_gate_preserves_request_rejections():
    request = _request(padded=True, mode=False)
    gate = _ordinary_gate_adjudication(request)["witness_judge"]["judge"]
    assert authority._valid_ordinary_judge_audio_gate(gate, request)
    forged = deepcopy(request)
    forged["request_sha256"] = "0" * 64
    assert not authority._valid_check_request(
        forged,
        current=request["current_cue"],
        proposed=request["proposed_cue"],
        window=(request["matched_start_ms"], request["matched_end_ms"]),
    )
    assert not authority._valid_check_request(
        request,
        current="changed",
        proposed=request["proposed_cue"],
        window=(request["matched_start_ms"], request["matched_end_ms"]),
    )
    assert not authority._valid_check_request(
        request,
        current=request["current_cue"],
        proposed="changed",
        window=(request["matched_start_ms"], request["matched_end_ms"]),
    )
    assert not authority._valid_check_request(
        request,
        current=request["current_cue"],
        proposed=request["proposed_cue"],
        window=(request["matched_start_ms"], request["matched_end_ms"] + 1),
    )
    changed = deepcopy(request)
    changed["whole_clip_current_srt"] += "mutated"
    assert not authority._valid_ordinary_judge_audio_gate(gate, changed)


def test_shared_history_hash_is_bounded_and_identical():
    shared = {"history": "证据文字" * 4096}
    value = {"records": [shared] * 128}
    expected = legacy(value)
    tracemalloc.start()
    try:
        actual = authority._sha_json(value)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert actual == expected
    assert peak < 2 * 1024 * 1024, peak
