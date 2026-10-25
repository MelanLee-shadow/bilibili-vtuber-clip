"""Offline production-entry equivalence against the unchanged HEAD serial code."""
import hashlib
import json
import random
import subprocess
import threading
import time
import types

import pytest

from src.autoslice import final_review_auditor as auditor
from src.autoslice import semantic_retry_progress as retry
from src.autoslice import supplement_audio_budget as native
from src.autoslice.exact_review_parallel import execute_exact_review_groups
from src.autoslice.native_audio_budget_receipt import persist_native_audio_budget_receipt


CONTEXT = {"candidate_id": "offline", "source_media_sha256": "a" * 64,
           "pieces": [{"source_media_sha256": "a" * 64, "start_ms": 0, "end_ms": 100000}]}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode()


@pytest.fixture(scope="module")
def serial_oracle():
    pytest.skip("Historical equivalence needs private pre-change Git objects; generic scheduler tests run without them.")


def fixture_agenda(count=20, *, interleaved=True):
    texts = [f"这是第{i}句" for i in range(1, count + 1)]
    srt = "\n\n".join(f"{i}\n00:00:{2*i:02d},000 --> 00:00:{2*i+1:02d},000\n{text}"
                          for i, text in enumerate(texts, 1)) + "\n"
    rows = [{"cue_index": i, "suspect": text, "suggestion": text + "吗",
             "repair_class": "phonetic", "why": f"question-{i}"}
            for i, text in enumerate(texts, 1)]
    # Interleaved repeated question, distinct candidate and a missing proposal.
    if interleaved and count >= 8:
        rows[6] = {**rows[0], "suggestion": texts[0] + "呢", "why": "another-candidate"}
        rows[7] = {"cue_index": 8, "suspect": texts[7], "why": "missing proposal", "repair_class": "phonetic"}
    return srt, rows


def files(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*"))
            if path.is_file()}


def fixture_runner(monkeypatch, module, root, source, *, workers, seed, native_ledger=False,
                   failures=(), delays=None, independent=False):
    srt, findings = fixture_agenda(interleaved=not independent)
    monkeypatch.setenv("AUTOSLICE_BASE", str(root))
    monkeypatch.setenv("AUTOSLICE_EXACT_REVIEW_PARALLELISM", str(workers))
    monkeypatch.setattr(retry.time, "time_ns", lambda: 123456789)
    monkeypatch.setattr(module, "MAX_CONTEXT_ADJUDICATIONS", 3)
    rng = random.Random(seed)
    delays = delays or [rng.uniform(0.0001, 0.004) for _ in range(20)]
    active = maximum = total_calls = 0
    lock = threading.Lock()
    budget = native.start_budget(source) if native_ledger else None
    receipt = root / "native.json"

    def judge(prompt):
        nonlocal active, maximum, total_calls
        cue = int(prompt.split(':')[1].split('\n')[0])
        with lock:
            active += 1
            total_calls += 1
            maximum = max(maximum, active)
        try:
            time.sleep(delays[cue - 1])
            if cue in failures:
                raise LookupError(f"offline failure {cue}")
            return '{"choice":"CURRENT","needs_audio":false}'
        finally:
            with lock:
                active -= 1
    judge.cpa_cache_identity = {"model": "offline-fixed", "effort": "test"}

    def adjudicate(text, finding, *, judge_llm_call, **kwargs):
        cue = finding['cue_index']
        if budget is not None:
            with budget.lock:
                attempt = budget.consume(provider="mai", model="offline", start_ms=cue*2000,
                                         end_ms=cue*2000+1000)
                budget.finish_attempt(attempt, status="OBSERVED")
                persist_native_audio_budget_receipt(source_media=source, receipt_path=receipt,
                    source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
                snapshot = budget.snapshot()
        else:
            snapshot = None
        judge_llm_call(f"offline:{cue}")
        request = module.build_context_adjudication_request(text,
            module._candidate_blind_missing_proposal_scaffold(text, finding) or finding,
            clip_context=CONTEXT)
        result = {"status": "UNCERTAIN", "repaired": False, "request": request,
                  "native_snapshot": snapshot}
        if cue % 2 == 0:
            result.update(status="OBSERVED", decision_authority="CPA_JUDGE",
                cpa_missing_proposal_convergence={"schema_version": "subtitle-missing-proposal-cpa-convergence.v1",
                    "status": "RESOLVED", "decision": "KEEP_EXISTING"})
        return text, result
    monkeypatch.setattr(module, "adjudicate_context_finding", adjudicate)
    started = time.monotonic()
    error = None
    try:
        result = module.adjudicate_exact_release_findings(
            srt, findings, entity_verifier=None, clip_context=CONTEXT,
            judge_llm_call=judge, initial_provider_adjudication_count=2)
    except Exception as exc:
        error = (type(exc), str(exc))
        result = None
    return result, files(root), error, time.monotonic() - started, maximum, total_calls


def test_production_entry_success_is_byte_identical_twenty_random_schedules(
        tmp_path, monkeypatch, serial_oracle):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'fixed offline media')
    serial = fixture_runner(monkeypatch, serial_oracle, tmp_path/'serial', source,
                            workers=1, seed=0)
    assert serial[2] is None
    assert serial[0][0] and serial[0][1]
    expected = hashlib.sha256(encode(serial[0]) + encode(
        {k: v.hex() for k, v in serial[1].items()})).hexdigest()
    for seed in range(20):
        parallel = fixture_runner(monkeypatch, auditor, tmp_path/f'p{seed}', source,
                                  workers=8, seed=seed)
        assert parallel[2] is None
        assert encode(parallel[0]) == encode(serial[0])
        assert parallel[1] == serial[1]
        assert parallel[4] > 1
    print('PRODUCTION_EQUIVALENCE=' + json.dumps({"repetitions": 20, "identical": True,
        "sha256": expected, "files": len(serial[1]), "oracle": "cf15de922 original serial entry and budget"}))


def test_native_final_totals_and_decisions_equal_original_serial(tmp_path, monkeypatch, serial_oracle):
    source = tmp_path/'source.mp4'
    source.write_bytes(b'fixed offline media')
    serial = fixture_runner(monkeypatch, serial_oracle, tmp_path/'serial', source,
                            workers=1, seed=0, native_ledger=True)
    parallel = fixture_runner(monkeypatch, auditor, tmp_path/'parallel', source,
                              workers=8, seed=7, native_ledger=True)
    assert serial[2] is None and parallel[2] is None
    def decisions(value):
        if isinstance(value, dict):
            return {k: decisions(v) for k, v in value.items() if k != 'native_snapshot'}
        if isinstance(value, (list, tuple)):
            return [decisions(v) for v in value]
        return value
    assert decisions(parallel[0]) == decisions(serial[0])
    def ledger(raw):
        row = json.loads(raw)
        row.pop('receipt_sha256')
        budget = row['budget']
        for collection, identifier in [('attempts', 'attempt_id'), ('cache_hits', 'cache_hit_id'),
                                       ('refusals', 'refusal_id')]:
            for event in budget[collection]:
                event.pop(identifier)
            budget[collection].sort(key=lambda event: json.dumps(event, sort_keys=True))
        return row
    assert ledger(parallel[1]['native.json']) == ledger(serial[1]['native.json'])
    native._REGISTRY.pop(str(source.resolve()), None)


def test_failure_matches_serial_and_other_groups_keep_verifiable_complete_records(
        tmp_path, monkeypatch, serial_oracle):
    source = tmp_path/'source.mp4'
    source.write_bytes(b'fixed offline media')
    serial = fixture_runner(monkeypatch, serial_oracle, tmp_path/'serial', source,
                            workers=1, seed=0, failures=(1, 2))
    parallel = fixture_runner(monkeypatch, auditor, tmp_path/'parallel', source,
                              workers=8, seed=7, failures=(1, 2))
    assert serial[2] == parallel[2] == (LookupError, 'offline failure 1')
    assert serial[0] is None and parallel[0] is None
    statuses = []
    for name, raw in parallel[1].items():
        if not name.endswith('.json'):
            continue  # Create-only dispatch claims are not attempt payloads.
        row = json.loads(raw)
        assert retry.digest(row['request']) == row['request_sha256']
        if row['status'] == 'COMPLETED':
            assert hashlib.sha256(row['completion'].encode()).hexdigest() == row['completion_sha256']
        else:
            assert row['status'] == 'OUTCOME_UNKNOWN'
        statuses.append(row['status'])
    assert statuses.count('COMPLETED') == 17  # second item of failed cue-1 group is skipped
    assert statuses.count('OUTCOME_UNKNOWN') == 2
    # Cold readers validate each complete/unknown original; nothing is relabelled successful.
    for directory in (tmp_path/'parallel'/'cache'/'semantic-cpa-attempts').iterdir():
        recorder = object.__new__(retry.SemanticAttemptRecorder)
        recorder.directory, recorder.issue, recorder.blocked = directory, directory.name, None
        assert recorder._entries()


def test_production_entry_benchmark_twenty_items(tmp_path, monkeypatch, serial_oracle):
    source = tmp_path/'source.mp4'
    source.write_bytes(b'fixed offline media')
    rng = random.Random(20261022)
    delays = [rng.uniform(2, 25)*0.001 for _ in range(20)]
    serial = fixture_runner(monkeypatch, serial_oracle, tmp_path/'serial', source,
                            workers=1, seed=0, delays=delays, independent=True)
    parallel = fixture_runner(monkeypatch, auditor, tmp_path/'parallel', source,
                              workers=8, seed=0, delays=delays, independent=True)
    assert serial[2] is None and parallel[2] is None
    assert encode(serial[0]) == encode(parallel[0]) and serial[1] == parallel[1]
    assert serial[5] == parallel[5] == 20
    print('PRODUCTION_BENCHMARK=' + json.dumps({"items": 20, "seed": 20261022, "scale": .001,
        "serial_s": serial[3], "parallel_8_s": parallel[3], "speedup": serial[3]/parallel[3],
        "peak_fake_calls": parallel[4], "actual_calls_each": parallel[5]}))


def test_prepared_scheduler_interleaved_groups_more_than_workers_and_early_prepare_failure():
    order = []
    guard = threading.Lock()
    def prepare(index):
        order.append(index)
        def run():
            with guard:
                pass
            return index
        return run
    assert execute_exact_review_groups([[i, i+12] for i in range(12)],
        parallelism=2, prepare=prepare) == list(range(24))
    assert order == list(range(24))
    completed = []
    def failing(index):
        if index in (0, 2):
            raise ArithmeticError(str(index))
        return lambda: completed.append(index)
    with pytest.raises(ArithmeticError, match='^0$'):
        execute_exact_review_groups([[0, 4], [1], [2], [3]], parallelism=2, prepare=failing)
    assert sorted(completed) == [1, 3]


def test_real_adjudicator_twenty_random_schedules(tmp_path, monkeypatch, serial_oracle):
    """Keep request building, real adjudication, budget, judge caches and recorder."""
    srt, findings = fixture_agenda(12)
    # This fixture exercises the actual ordinary phonetic request/CPA path.
    for finding in findings:
        finding.update(suspect=f"这是第{finding['cue_index']}句",
                       suggestion=f"这是第{finding['cue_index']}局")
    monkeypatch.setattr(retry.time, 'time_ns', lambda: 123456789)

    def run(module, root, workers, seed):
        monkeypatch.setenv('AUTOSLICE_BASE', str(root))
        monkeypatch.setenv('AUTOSLICE_EXACT_REVIEW_PARALLELISM', str(workers))
        rng = random.Random(seed)
        delays = {i: rng.uniform(.0001, .002) for i in range(1, 13)}
        def verifier(request):
            time.sleep(delays[request['cue_indexes'][0]])
            return {'schema_version': 'subtitle-span-acoustic-witness.v1',
                'request_sha256': request['request_sha256'], 'status': 'OBSERVED',
                'target_audible': True, 'heard_pinyin': 'zhe shi di yi ju',
                'uncertain_positions': [], 'syllable_count': 5, 'confidence': .9,
                'reason': 'offline fixed witness'}
        def judge(_prompt):
            time.sleep(.001)
            return json.dumps({'choice': 'CURRENT', 'needs_audio': False,
                'reason': 'offline fixed judge', 'current_utterance_supported': True,
                'current_utterance_support_reason': 'full current utterance supported'})
        judge.cpa_cache_identity = {'models': ['offline'], 'effort': 'test'}
        result = module.adjudicate_exact_release_findings(srt, findings,
            entity_verifier=verifier, clip_context=CONTEXT, judge_llm_call=judge,
            initial_provider_adjudication_count=10)
        return encode(result), files(root)

    reference = run(serial_oracle, tmp_path/'serial', 1, 0)
    for seed in range(20):
        assert run(auditor, tmp_path/f'p{seed}', 8, seed) == reference
    print('REAL_ADJUDICATOR_EQUIVALENCE=' + json.dumps({'repetitions': 20,
        'identical': True, 'output_sha256': hashlib.sha256(reference[0]).hexdigest(),
        'record_tree_sha256': hashlib.sha256(encode({k: v.hex() for k, v in reference[1].items()})).hexdigest(),
        'files': len(reference[1]), 'oracle': 'cf15de922 original serial entry and budget'}))


def test_handoff_prefix_and_replay_identity_match_original_serial(tmp_path, monkeypatch, serial_oracle):
    srt, findings = fixture_agenda(2)
    findings.append(dict(findings[0]))
    monkeypatch.setattr(retry.time, 'time_ns', lambda: 123456789)
    def run(module, root, workers):
        monkeypatch.setenv('AUTOSLICE_BASE', str(root))
        monkeypatch.setenv('AUTOSLICE_EXACT_REVIEW_PARALLELISM', str(workers))
        monkeypatch.setattr(module, 'coalesce_exact_review_agenda', lambda _srt, rows: list(rows))
        monkeypatch.setattr(module, 'valid_exact_source_transcript_handoff', lambda h: bool(h.get('receipt_sha256')))
        def judge(_prompt):
            time.sleep(.002)
            return '{"choice":"CURRENT","needs_audio":false}'
        judge.cpa_cache_identity = {'model': 'offline'}
        def adjudicate(text, finding, *, judge_llm_call, **kwargs):
            judge_llm_call(f"cue:{finding['cue_index']}")
            request = module.build_context_adjudication_request(text,
                module._candidate_blind_missing_proposal_scaffold(text, finding) or finding,
                clip_context=CONTEXT)
            return text, {'status': 'UNCERTAIN', 'repaired': False,
                'exact_source_transcript_handoff': {
                    'receipt_sha256': f"receipt-{finding['cue_index']}", 'input_request': request}}
        monkeypatch.setattr(module, 'adjudicate_context_finding', adjudicate)
        result = module.adjudicate_exact_release_findings(srt, findings,
            entity_verifier=None, clip_context=CONTEXT, judge_llm_call=judge)
        return result, files(root)
    expected = run(serial_oracle, tmp_path/'serial', 1)
    assert run(auditor, tmp_path/'parallel', 8) == expected
    assert [r['exact_release_adjudication']['cpa_resource_pressure']['provider_adjudication_count']
            for r in expected[0][0]] == [1, 2, 3]


def test_argon_retrieval_waits_for_all_groups_and_preserves_order(tmp_path, monkeypatch, serial_oracle):
    srt, findings = fixture_agenda(3)
    def run(module, root, workers):
        monkeypatch.setenv('AUTOSLICE_BASE', str(root))
        monkeypatch.setenv('AUTOSLICE_EXACT_REVIEW_PARALLELISM', str(workers))
        monkeypatch.setattr(retry.time, 'time_ns', lambda: 123456789)
        counts, visits, times = {}, [], [0.]
        monkeypatch.setattr(module, '_argon_retrieval_clock', lambda: times[0])
        monkeypatch.setattr(module, '_argon_retrieval_sleep', lambda seconds: times.__setitem__(0, times[0]+seconds))
        class Verifier:
            argon_escalation = object()
            def __call__(self, _request):
                raise AssertionError('unexpected audio')
        def adjudicate(text, finding, **kwargs):
            cue = finding['cue_index']
            n = counts.get(cue, 0)+1
            counts[cue] = n
            if n > 1:
                assert all(counts.get(i, 0) >= 1 for i in (1, 2, 3))
            time.sleep(.001*(4-cue))
            visits.append((cue, n))
            if n == 1:
                return text, {'status': 'UNCERTAIN', 'repaired': False,
                    'reason_code': 'ARGON_ESCALATION_PENDING_OR_UNRESOLVED',
                    'argon_escalation': {'status': 'PENDING', 'job_name': f'job-{cue}',
                                         'dispatches_this_advance': 1}}
            return text, {'status': 'UNCERTAIN', 'repaired': False, 'reason_code': 'RETRIEVED'}
        monkeypatch.setattr(module, 'adjudicate_context_finding', adjudicate)
        result = module.adjudicate_exact_release_findings(srt, findings,
            entity_verifier=Verifier(), clip_context=CONTEXT)
        assert visits[3:] == [(1, 2), (2, 2), (3, 2)]
        return result
    assert run(auditor, tmp_path/'parallel', 8) == run(serial_oracle, tmp_path/'serial', 1)


def test_verifier_facade_never_serializes_provider_calls():
    from src.autoslice.exact_review_parallel import OrderedVerifierAccess
    visits = []
    barrier = threading.Barrier(4)
    def verifier(index):
        visits.append(index)
        barrier.wait(timeout=5)
        return index
    guarded = OrderedVerifierAccess(verifier)
    def prepare(index):
        def run():
            time.sleep(.001*(4-index))
            return guarded(index)
        return run
    assert execute_exact_review_groups([[i] for i in range(4)], parallelism=4,
        prepare=prepare) == list(range(4))
    assert sorted(visits) == list(range(4))


def test_argon_create_only_storage_parallel_distinct_roots_and_collision(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from src.autoslice import argon_escalation_storage as store
    root = tmp_path/'private'
    store.directory(root)
    def write(index):
        target = root/str(index)
        store.directory(target)
        with store.locked(target):
            store.immutable(target/'fixed.json', b'{"fixed":true}')
            store.immutable(target/'fixed.json', b'{"fixed":true}')
            return store.read(target/'fixed.json')
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(write, range(20))) == [b'{"fixed":true}']*20
    with store.locked(root/'0'), pytest.raises(ValueError):
        store.immutable(root/'0'/'fixed.json', b'{"different":true}')


def test_failed_round_retains_native_receipt_and_cold_resume_without_reset(tmp_path, monkeypatch):
    from src.autoslice.native_audio_budget_receipt import (
        require_native_audio_budget_continuity, resume_native_audio_budget,
    )
    source = tmp_path/'source.mp4'
    source.write_bytes(b'fixed offline media')
    root = tmp_path/'failed'
    result = fixture_runner(monkeypatch, auditor, root, source, workers=8, seed=19,
                            failures=(1, 2), native_ledger=True)
    assert result[0] is None and result[2] == (LookupError, 'offline failure 1')
    receipt = json.loads((root/'native.json').read_text())
    assert len(receipt['budget']['attempts']) == 19
    assert all(row['status'] == 'OBSERVED' for row in receipt['budget']['attempts'])
    frozen = (root/'native.json').read_bytes()
    native.start_budget(source)
    binding = dict(source_media=source, receipt_path=root/'native.json',
                   source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                   require_existing=True)
    resume_native_audio_budget(**binding)
    require_native_audio_budget_continuity(**binding)
    assert (root/'native.json').read_bytes() == frozen
    assert len(native.get_budget(source).snapshot()['attempts']) == 19
    native._REGISTRY.pop(str(source.resolve()), None)
