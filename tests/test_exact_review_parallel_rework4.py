"""Hard admission order, early stream closure and the soft-mode bypass."""
import hashlib
import json
import random
import threading
import time

import pytest

from src.autoslice import diarized_transcription
from src.autoslice import final_review_auditor as auditor
from src.autoslice import native_foreign_witness as native
from src.autoslice import supplement_audio_budget as budgets
from src.autoslice.exact_review_parallel import (
    execute_exact_review_groups, finish_native_admission,
)
from tests import test_exact_review_parallel_integration as integration
from tests import test_exact_review_parallel_rework2 as shaped


@pytest.fixture(scope='module')
def serial_oracle():
    return integration.serial_oracle.__wrapped__()


@pytest.mark.parametrize('limits,reason', [
    ({'max_windows': 2, 'max_audio_ms': 100000}, 'LOCAL_AUDIO_WINDOW_CAP'),
    ({'max_windows': 100, 'max_audio_ms': 2000}, 'LOCAL_AUDIO_TOTAL_CAP'),
])
@pytest.mark.parametrize('extra_audio', [False, True])
def test_dynamic_answers_preserve_original_cap_recipient(
        tmp_path, monkeypatch, serial_oracle, limits, reason, extra_audio):
    """A's second request is discovered after an answer, never pre-reserved."""
    source = tmp_path/'source.bin'
    source.write_bytes(b'fixed offline source')
    srt, findings = integration.fixture_agenda(2, interleaved=False)

    def run(module, seed):
        budget = budgets.start_budget(source, enforce_limits=True, **limits)
        rng = random.Random(seed)
        delays = [rng.uniform(.0001, .003) for _ in range(3)]
        admissions = []
        monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path/f'{module.__name__}-{seed}'))
        monkeypatch.setenv('AUTOSLICE_EXACT_REVIEW_PARALLELISM', '8')

        def adjudicate(text, finding, **kwargs):
            cue = finding['cue_index']
            statuses = []
            def listen(start):
                try:
                    attempt = budget.consume('mai', 'offline', start, start+1000)
                except budgets.BudgetExceeded as exc:
                    statuses.append(exc.reason_code)
                    return False
                admissions.append((cue, start))
                time.sleep(delays[len(statuses)])
                budget.finish_attempt(attempt, status='OBSERVED')
                statuses.append('OBSERVED')
                return True
            if cue == 1:
                listen(0)
                # The first provider/CPA answer reveals whether more is needed.
                if extra_audio:
                    listen(1000)
            else:
                listen(4000)
                if not extra_audio:
                    listen(5000)
            # Deliberately no early-close hint: production's ordinary callback
            # closure is required for a dynamic path that cannot declare sooner.
            return text, {'status': 'UNCERTAIN', 'repaired': False,
                          'reason_code': statuses[-1], 'observations': statuses}
        monkeypatch.setattr(module, 'adjudicate_context_finding', adjudicate)
        result = module.adjudicate_exact_release_findings(
            srt, findings, entity_verifier=lambda request: {}, clip_context=integration.CONTEXT)
        snapshot = shaped.normalized(budget.snapshot())
        return shaped.normalized(result), snapshot, admissions

    expected = run(serial_oracle, 0)
    assert expected[1]['refusals'][0]['reason_code'] == reason
    assert [cue for cue, _ in expected[2]] == ([1, 1] if extra_audio else [1, 2])
    for seed in range(20):
        assert run(auditor, seed) == expected


@pytest.mark.parametrize('provider', ['mai', 'moss'])
def test_fixed_last_native_request_releases_before_actual_transport_returns(
        tmp_path, monkeypatch, provider):
    source = tmp_path/'source.bin'
    source.write_bytes(b'fixed offline source')
    budget = budgets.start_budget(source, enforce_limits=True, max_windows=2)
    observer = native.build_native_foreign_witness(
        source_media=source, output_dir=tmp_path/'out', provider=provider)
    overlap = threading.Barrier(2)
    calls = []

    def transcribe(audio, **kwargs):
        kwargs['before_request']()
        assert not budget._lock._is_owned()
        calls.append(kwargs['provider'])
        overlap.wait(timeout=5)
        from src.autoslice.mai_transcription import MAI_MODEL
        from src.autoslice.moss_transcription import MOSS_MODEL
        return {'provider': provider, 'model': {'mai': MAI_MODEL, 'moss': MOSS_MODEL}[provider],
                'input_audio_sha256': hashlib.sha256(audio).hexdigest(),
                'response_sha256': 'c'*64,
                'native_segments': [{'start_ms': 0, 'end_ms': 500, 'text': '原话'}]}
    monkeypatch.setattr(diarized_transcription, 'transcribe_evidence', transcribe)
    monkeypatch.setattr(native, '_extract_exact_mp3', lambda _s, out, a, b: out.write_bytes(b'audio'))

    def prepare(index):
        return lambda: observer(start_ms=index*2000, end_ms=index*2000+1000,
                                last_native_admission=True)
    result = execute_exact_review_groups([[0], [1]], prepare=prepare, parallelism=2)
    assert len(result) == len(calls) == 2
    snapshot = budget.snapshot()
    assert snapshot['sealed_attempt_count'] == 2
    assert snapshot['pending_attempt_count'] == 0
    assert json.loads((tmp_path/'out/native-audio-budget.json').read_bytes())['budget'] == snapshot


def test_soft_budget_never_enters_wait_even_when_previous_item_is_open(tmp_path, monkeypatch):
    import src.autoslice.exact_review_parallel as scheduler
    budget = budgets.SupplementAudioBudget(tmp_path/'source', max_windows=1, max_audio_ms=1)
    overlap = threading.Barrier(2)
    def forbidden(**kwargs):
        raise AssertionError('soft budget entered the hard admission gate')
    monkeypatch.setattr(scheduler, 'wait_for_native_turn', forbidden)
    def prepare(index):
        def run():
            attempt = budget.consume('mai', 'offline', index*2000, index*2000+1000)
            overlap.wait(timeout=5)
            budget.finish_attempt(attempt, status='OBSERVED')
            return index
        return run
    assert execute_exact_review_groups([[0], [1]], prepare=prepare, parallelism=2) == [0, 1]
    assert budget.snapshot()['attempt_count'] == 2


@pytest.mark.parametrize('refused', [False, True])
def test_last_reservation_closes_on_admission_or_refusal_and_cannot_reopen(tmp_path, refused):
    budget = budgets.SupplementAudioBudget(tmp_path/'source', enforce_limits=True,
                                          max_audio_ms=1 if refused else 10000)
    overlap = threading.Barrier(2)
    def prepare(index):
        def run():
            try:
                budget.consume('mai', 'offline', index*2000, index*2000+1000,
                               last_native_admission=True)
            except budgets.BudgetExceeded:
                assert refused
            overlap.wait(timeout=5)
            with pytest.raises(RuntimeError, match='already finished'):
                budget.consume('mai', 'offline', 6000, 7000)
            return index
        return run
    assert execute_exact_review_groups([[0], [1]], prepare=prepare, parallelism=2) == [0, 1]


def test_explicit_dynamic_close_releases_before_terminal_cpa(tmp_path):
    budget = budgets.SupplementAudioBudget(tmp_path/'source', enforce_limits=True)
    overlap = threading.Barrier(2)
    def prepare(index):
        def run():
            budget.consume('mai', 'offline', index*2000, index*2000+1000)
            # Only now does the answer establish no more native audio is needed.
            finish_native_admission()
            overlap.wait(timeout=5)
            return index
        return run
    assert execute_exact_review_groups([[0], [1]], prepare=prepare, parallelism=2) == [0, 1]


def test_production_cache_only_terminal_closes_before_its_cpa_work(tmp_path, monkeypatch):
    from src.autoslice import semantic_audio_recovery, targeted_relisten
    source = tmp_path/'source'
    budget = budgets.SupplementAudioBudget(source, enforce_limits=True)
    srt, findings = integration.fixture_agenda(2, interleaved=False)
    overlap = threading.Barrier(2)
    monkeypatch.setattr(auditor, '_adjudicate_context_finding',
                        lambda text, *args, **kwargs: (text, {'repaired': False}))
    monkeypatch.setattr(auditor, 'history_audio_recovery_from_adjudication',
                        lambda *args, **kwargs: {'status': 'BLOCK'})
    monkeypatch.setattr(semantic_audio_recovery, 'recover_semantic_audio_range',
                        lambda **kwargs: ({}, {'status': 'CONTEXT_REVIEW'}))
    def terminal(**kwargs):
        overlap.wait(timeout=5)
        return {'repaired': False}
    monkeypatch.setattr(targeted_relisten, 'close_with_existing_editorial', terminal)
    def verifier(request):
        return {}
    verifier.observe_range_context = lambda **kwargs: []
    def prepare(index):
        def run():
            if index == 0:
                return auditor._adjudicate_context_finding_with_range(
                    srt, findings[0], entity_verifier=verifier,
                    clip_context=integration.CONTEXT, require_complete_utterance_support=True)
            budget.consume('mai', 'offline', 0, 1000, last_native_admission=True)
            overlap.wait(timeout=5)
            return None
        return run
    execute_exact_review_groups([[0], [1]], prepare=prepare, parallelism=2)


def test_hard_dynamic_exception_closes_stream_and_preserves_other_group(tmp_path):
    budget = budgets.SupplementAudioBudget(tmp_path/'source', enforce_limits=True)
    finished = []
    def prepare(index):
        def run():
            budget.consume('mai', 'offline', index*2000, index*2000+1000)
            if index == 0:
                raise LookupError('original dynamic failure')
            finished.append(index)
        return run
    with pytest.raises(LookupError, match='original dynamic failure'):
        execute_exact_review_groups([[0], [1]], prepare=prepare, parallelism=2)
    assert finished == [1]
    assert budget.snapshot()['attempt_count'] == 2


@pytest.mark.parametrize('enforce_limits', [False, True], ids=['soft', 'hard'])
def test_same_rework3_parameters_benchmark(
        tmp_path, monkeypatch, serial_oracle, enforce_limits):
    source = tmp_path/'source.bin'
    source.write_bytes(b'fixed offline source')
    serial = shaped.shaped_run(monkeypatch, serial_oracle, tmp_path/'serial', source,
        workers=1, seed=20261022, enforce_limits=enforce_limits)
    parallel = shaped.shaped_run(monkeypatch, auditor, tmp_path/'parallel', source,
        workers=8, seed=20261022, enforce_limits=enforce_limits)
    assert shaped.comparable(serial) == shaped.comparable(parallel)
    print('REWORK4_BENCHMARK='+json.dumps({'mode': 'hard' if enforce_limits else 'soft',
        'items': 20, 'workers': 8, 'seed': 20261022, 'scale': .001,
        'cpa_delay_s': [5, 25], 'audio_delay_s': [10, 60],
        'serial_s': serial['wall_s'], 'parallel_s': parallel['wall_s'],
        'speedup': serial['wall_s']/parallel['wall_s'], 'peak_audio': parallel['peak_audio'],
        'audio_calls': sum(kind == 'audio' for kind, _, _ in parallel['calls']),
        'cpa_calls': sum(kind == 'cpa' for kind, _, _ in parallel['calls'])}))
