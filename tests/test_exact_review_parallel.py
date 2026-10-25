"""Scheduling primitives only; production shared-state integration is separate."""
from collections import defaultdict
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import random
import threading
import time

import pytest

from src.autoslice import exact_review_parallel as scheduling
from src.autoslice.final_review_auditor import build_context_adjudication_request


SRT = "1\n00:00:01,000 --> 00:00:02,000\n甲句\n\n2\n00:00:03,000 --> 00:00:04,000\n乙句\n"
CONTEXT = {"candidate_id": "offline", "source_media_sha256": "a" * 64,
           "pieces": [{"start_ms": 0, "end_ms": 5000, "source_media_sha256": "a" * 64}]}


def test_grouping_uses_ordinary_requests_and_only_g6(monkeypatch):
    findings = [
        {"cue_index": 1, "suspect": "甲句", "suggestion": "甲词", "repair_class": "phonetic"},
        {"cue_index": 2, "suspect": "乙句", "suggestion": "乙词", "repair_class": "phonetic"},
        {"cue_index": 1, "suspect": "甲句", "suggestion": "丙词", "repair_class": "phonetic"},
    ]
    before = deepcopy(findings)
    original = scheduling.audio_question_identity.same_audio_question
    calls = []

    def g6(old, new, **kwargs):
        calls.append(kwargs)
        return original(old, new, **kwargs)

    monkeypatch.setattr(scheduling.audio_question_identity, "same_audio_question", g6)
    groups = scheduling.group_exact_review_agenda(findings,
        request_for_finding=lambda f: build_context_adjudication_request(SRT, f, clip_context=CONTEXT))
    assert groups == [[0, 2], [1]]
    assert findings == before
    assert calls and all(call == {"compare_proposed": False, "strict_source": True} for call in calls)


def test_g6_source_clock_representation_and_controls():
    request = build_context_adjudication_request(SRT, {
        "cue_index": 1, "suspect": "甲句", "suggestion": "甲词", "repair_class": "phonetic",
    }, clip_context=CONTEXT)
    represented = deepcopy(request)
    represented.update(matched_start_ms=0, matched_end_ms=1000,
                       source_media_timeline_offset_ms=1000, proposed_cue="another candidate")
    other_source = deepcopy(request)
    other_source["clip_context_binding"]["source_media_sha256s"] = ["b" * 64]
    other_text = {**request, "current_cue": "different", "base_text_sha256": "f" * 64}
    other_time = {**request, "matched_end_ms": 2001}
    values = [request, represented, other_source, other_text, other_time, None, None]
    assert scheduling.group_exact_review_agenda(values, request_for_finding=lambda r: r) == [
        [0, 1], [2], [3], [4], [5], [6]]


@pytest.mark.parametrize("raw,expected", [(None, 8), ("1", 1), ("8", 8), ("64", 64)])
def test_parallelism_config(raw, expected):
    env = {} if raw is None else {scheduling.PARALLELISM_ENV: raw}
    assert scheduling.exact_review_parallelism(env) == expected


@pytest.mark.parametrize("raw", ["0", "65", "-1", "", "eight", "8.0"])
def test_invalid_parallelism_is_not_silently_accepted(raw):
    with pytest.raises(ValueError, match=scheduling.PARALLELISM_ENV):
        scheduling.exact_review_parallelism({scheduling.PARALLELISM_ENV: raw})


def test_groups_overlap_but_each_group_keeps_order_and_context():
    context = ContextVar("offline-review-context", default="lost")
    token = context.set("bound")
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = defaultdict(int)
    visits = defaultdict(list)
    groups = [[0, 2], [1, 3]]

    def process(index):
        group = index % 2
        assert context.get() == "bound"
        with lock:
            active[group] += 1
            assert active[group] == 1
            visits[group].append(index)
        if index < 2:
            barrier.wait(timeout=3)
        with lock:
            active[group] -= 1
        return index

    try:
        assert scheduling.execute_exact_review_groups(groups, process, parallelism=8) == [0, 1, 2, 3]
    finally:
        context.reset(token)
    assert dict(visits) == {0: [0, 2], 1: [1, 3]}


def test_one_worker_preserves_original_agenda_not_group_flattening():
    visits = []
    assert scheduling.execute_exact_review_groups([[0, 2], [1, 3]],
        lambda i: visits.append(i) or i, parallelism=1) == [0, 1, 2, 3]
    assert visits == [0, 1, 2, 3]


def test_exception_waits_for_other_groups_and_raises_original_earliest_error():
    first = threading.Event()
    finished = threading.Event()
    error = RuntimeError("first agenda failure")

    def process(index):
        if index == 0:
            assert first.wait(timeout=3)
            raise error
        if index == 1:
            first.set()
            raise ValueError("earlier completion but later agenda failure")
        if index == 2:
            pytest.fail("failed group continued")
        assert first.wait(timeout=3)
        finished.set()
        return index

    with pytest.raises(RuntimeError) as caught:
        scheduling.execute_exact_review_groups([[0, 2], [1], [3]], process, parallelism=8)
    assert caught.value is error
    assert finished.is_set()


@pytest.mark.parametrize("groups", [[[0, 0]], [[1]], [[1, 0]], [[], [0]]])
def test_invalid_partitions_are_rejected(groups):
    with pytest.raises(ValueError, match="partition"):
        scheduling.execute_exact_review_groups(groups, lambda i: i)


def test_deterministic_callback_hashes_repeat_with_shuffled_completion():
    groups = [[0, 4, 8], [1, 5, 9], [2, 6, 10], [3, 7, 11]]

    def run(workers, seed):
        rng = random.Random(seed)
        delays = [rng.uniform(0.0001, 0.004) for _ in range(12)]

        def process(index):
            time.sleep(delays[index])
            return {"agenda_index": index, "exact_release_adjudication": {
                "status": "UNCERTAIN", "fake_answer": f"answer:{index}"}}

        result = scheduling.execute_exact_review_groups(groups, process, parallelism=workers)
        return hashlib.sha256(json.dumps(result, ensure_ascii=False).encode()).hexdigest()

    serial = run(1, 0)
    assert all(run(8, seed) == serial for seed in range(20))
    print("EXACT_REVIEW_EQUIVALENCE=" + json.dumps({"repetitions": 20, "hash": serial,
        "identical": True, "scope": "pure deterministic callback outputs; no production records"}))


def test_simulated_provider_benchmark():
    rng = random.Random(20261022)
    delays = [rng.uniform(2, 25) * 0.001 for _ in range(20)]

    def process(index):
        time.sleep(delays[index])
        return {"index": index, "fake_answer": f"answer:{index}"}

    measurements = {}
    hashes = []
    for workers in (1, 8):
        started = time.monotonic()
        result = scheduling.execute_exact_review_groups([[i] for i in range(20)],
            process, parallelism=workers)
        measurements[str(workers)] = time.monotonic() - started
        hashes.append(hashlib.sha256(json.dumps(result).encode()).hexdigest())
    assert hashes[0] == hashes[1]
    print("EXACT_REVIEW_BENCHMARK=" + json.dumps({"items": 20, "scale": 0.001,
        "seed": 20261022, "wall_s": measurements, "speedup": measurements["1"] / measurements["8"],
        "hash": hashes[0], "scope": "scheduling primitive with pure fake provider; not production integration"}))
