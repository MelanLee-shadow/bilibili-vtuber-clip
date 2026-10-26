"""Production-shaped offline calls; every witness crosses the ordinary wrapper."""
import hashlib
import json
import random
import threading
import time

import pytest

from src.autoslice import final_review_auditor as auditor
from src.autoslice import supplement_audio_budget as native
from src.autoslice.final_review_contract import FinalReviewContractError, validate_final_review_release
from src.autoslice.native_audio_budget_receipt import persist_native_audio_budget_receipt
from src.autoslice.exact_review_parallel import finish_native_admission
from tests import test_exact_review_parallel_integration as integration

CONTEXT = integration.CONTEXT


@pytest.fixture(scope='module')
def serial_oracle():
    return integration.serial_oracle.__wrapped__()


def normalized(value):
    """Remove runtime metadata, never status/choice/reason/text or budget counts."""
    ignored = {'attempt_id', 'cache_hit_id', 'refusal_id', 'revision', 'timestamp',
               'elapsed_seconds', 'created_at', 'updated_at', 'time_ns', 'source_media',
               'directory', 'attempt_path', 'receipt_sha256'}
    if isinstance(value, dict):
        return {key: normalized(item) for key, item in value.items() if key not in ignored}
    if isinstance(value, (list, tuple)):
        rows = [normalized(item) for item in value]
        return rows
    return value


def release(result, srt):
    from src.autoslice.final_review_meaning_policy import POLICY_VERSION
    digest = 'sha256:' + hashlib.sha256(srt.encode()).hexdigest()
    grid = 'sha256:' + 'd'*64
    audit = {'schema_version': 'final-review-audit.v2', 'reviewed_srt_sha256': digest,
             'meaning_policy': POLICY_VERSION, 'discovery': {'status': 'COMPLETE'},
             'correction_mutation_authority': {'schema_version': 'subtitle-correction-mutation-audit.v1',
                                               'status': 'PASS'},
             'status': 'FLAGGED' if result[0] else 'CLEAN',
             'release_gate': 'BLOCK' if result[0] else 'PASS',
             'findings': result[0], 'validated_finding_count': len(result[0]),
             'reason_codes': ['FINAL_REVIEW_UNRESOLVED_FINDINGS'] if result[0] else []}
    audit['boundary_semantic_review'] = {
        'status': 'PASS', 'review_scope': 'final_delivery',
        'request_sha256': 'sha256:'+'e'*64, 'cue_grid_sha256': grid,
        'final_endpoint_binding': {'schema_version': 'talk-boundary-final-endpoint-binding.v1',
            'status': 'PASS', 'recommended_end_cue_index': 20, 'recommended_end_ms': 41000,
            'final_closure_cue_index': 20, 'final_snapped_end_ms': 41000, 'reason_codes': [],
            'semantic_cue_grid_sha256': grid, 'final_cue_grid_sha256': grid},
        'source_separation_witness': {'schema_version': 'talk-boundary-source-separation-witness.v1',
            'status': 'PASS', 'source_review_sha256': 'sha256:'+'f'*64,
            'source_request_sha256': 'sha256:'+'0'*64, 'source_cue_grid_sha256': 'sha256:'+'1'*64,
            'source_final_start_ms': 0, 'source_final_end_ms': 41000, 'reason_codes': []}}
    try:
        validate_final_review_release(audit, expected_srt_sha256=digest)
    except FinalReviewContractError as exc:
        return 'BLOCK', str(exc)
    return 'PASS', None


def test_release_comparison_reaches_real_findings_gate():
    srt, _ = integration.fixture_agenda(interleaved=False)
    assert release(([], []), srt) == ('PASS', None)
    assert release(([{'cue_index': 1}], []), srt) == ('BLOCK', 'FINAL_REVIEW_FINDING_REVIEW_PENDING')


def shaped_run(monkeypatch, module, root, source, *, workers, seed, scale=.001,
               enforce_limits=False, max_windows=100, max_audio_ms=1000000,
               paired=False):
    srt, agenda = integration.fixture_agenda(interleaved=False)
    if paired:
        agenda = agenda[:10] + [
            {**row, 'suggestion': row['suggestion'] + '呢', 'why': row['why'] + '-second'}
            for row in agenda[:10]
        ]
    monkeypatch.setenv('AUTOSLICE_BASE', str(root))
    monkeypatch.setenv('AUTOSLICE_EXACT_REVIEW_PARALLELISM', str(workers))
    rng = random.Random(seed)
    # Stable answers/call counts; only timing is randomized across repetitions.
    shapes = [(1+i % 3, 1+i % 2) for i in range(20)]
    delays = [[rng.uniform(5, 25), *[rng.uniform(10, 60) for _ in range(a)],
               *[rng.uniform(5, 25) for _ in range(c)]] for a, c in shapes]
    budget = native.start_budget(source, enforce_limits=enforce_limits,
        max_windows=max_windows, max_audio_ms=max_audio_ms)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    receipt = root/'native.json'
    root.mkdir(parents=True, exist_ok=True)
    calls, admissions, active, peak = [], [], 0, 0
    active_audio = peak_audio = 0
    guard = threading.Lock()

    def provider(kind, cue, ordinal, duration):
        nonlocal active, peak, active_audio, peak_audio
        with guard:
            active += 1
            peak = max(peak, active)
            if kind == 'audio':
                active_audio += 1
                peak_audio = max(peak_audio, active_audio)
            calls.append((kind, cue, ordinal))
        try:
            time.sleep(duration*scale)
        finally:
            with guard:
                active -= 1
                if kind == 'audio':
                    active_audio -= 1

    def persist():
        persist_native_audio_budget_receipt(source_media=source,
            source_media_sha256=source_sha, receipt_path=receipt)

    def witness(request):
        cue, ordinal = request['cue_index'], request['ordinal']
        with budget.lock:
            attempt = budget.consume('mai', 'offline', cue*2000, cue*2000+1000)
            admissions.append((cue, ordinal))
            persist()
        if ordinal == shapes[cue-1][0]-1:
            finish_native_admission()
        provider('audio', cue, ordinal, delays[cue-1][1+ordinal])
        with budget.lock:
            budget.finish_attempt(attempt, status='OBSERVED')
            persist()
        return {'status': 'OBSERVED', 'attempt_id': attempt,
                'timestamp': time.time_ns(), 'path': str(root/f'attempt-{attempt}')}

    def judge(prompt):
        phase, cue, ordinal = prompt.split(':')[:3]
        cue, ordinal = int(cue), int(ordinal)
        a, _c = shapes[cue-1]
        duration = delays[cue-1][0 if phase == 'plan' else 1+a+ordinal]
        provider('cpa', cue, ordinal, duration)
        return '{"choice":"CURRENT","needs_audio":false}'
    judge.cpa_cache_identity = {'model': 'offline', 'effort': 'test'}

    def adjudicate(text, finding, *, entity_verifier, judge_llm_call, **kwargs):
        cue = finding['cue_index']
        a, c = shapes[cue-1]
        # Distinct proposals must produce distinct fake CPA inputs, just as
        # their real prompts do; do not benchmark accidental prompt-cache hits.
        suffix = ':' + finding['suggestion'] if paired else ''
        judge_llm_call(f'plan:{cue}:0{suffix}')
        observed, refusal = [], None
        for ordinal in range(a):
            try:
                observed.append(entity_verifier({'cue_index': cue, 'ordinal': ordinal}))
            except native.BudgetExceeded as exc:
                refusal = exc.reason_code
                finish_native_admission()
                break
        for ordinal in range(c):
            judge_llm_call(f'decide:{cue}:{ordinal}{suffix}')
        result = {'status': 'UNCERTAIN', 'repaired': False, 'choice': 'CURRENT',
                  'reason_code': refusal or 'OFFLINE_FIXED_UNRESOLVED',
                  'observations': [{'status': row['status'], 'attempt_id': row['attempt_id']}
                                   for row in observed]}
        if cue % 2 == 0 and refusal is None:
            result.update(status='OBSERVED', decision_authority='CPA_JUDGE',
                cpa_missing_proposal_convergence={'schema_version': 'subtitle-missing-proposal-cpa-convergence.v1',
                    'status': 'RESOLVED', 'decision': 'KEEP_EXISTING'})
        return text, result
    monkeypatch.setattr(module, 'adjudicate_context_finding', adjudicate)
    start = time.monotonic()
    result = module.adjudicate_exact_release_findings(srt, agenda,
        entity_verifier=witness, judge_llm_call=judge, clip_context=CONTEXT)
    wall = time.monotonic()-start
    snapshot = budget.snapshot()
    snapshot['attempts'] = sorted(normalized(snapshot['attempts']),
                                key=lambda row: json.dumps(row, sort_keys=True))
    snapshot['refusals'] = sorted(normalized(snapshot['refusals']),
                                key=lambda row: json.dumps(row, sort_keys=True))
    native._REGISTRY.pop(str(source.resolve()), None)
    return {'result': normalized(result), 'release': release(result, srt),
            'budget': normalized(snapshot), 'calls': sorted(calls), 'wall_s': wall,
            'peak': peak, 'peak_audio': peak_audio, 'admission_order': admissions}


def comparable(run):
    return {key: value for key, value in run.items()
            if key not in {'wall_s', 'peak', 'peak_audio', 'admission_order'}}


def test_production_shaped_benchmark(tmp_path, monkeypatch, serial_oracle):
    source = tmp_path/'source.mp4'
    source.write_bytes(b'fixed offline source')
    serial = shaped_run(monkeypatch, serial_oracle, tmp_path/'serial', source,
                        workers=1, seed=20261022)
    parallel = shaped_run(monkeypatch, auditor, tmp_path/'parallel', source,
                          workers=8, seed=20261022)
    assert comparable(serial) == comparable(parallel)
    speedup = serial['wall_s']/parallel['wall_s']
    print('REWORK2_SHAPED_BENCHMARK='+json.dumps({'items': 20, 'workers': 8,
        'scale': .001, 'serial_s': serial['wall_s'], 'parallel_s': parallel['wall_s'],
        'speedup': speedup, 'peak_provider_calls': parallel['peak'],
        'peak_audio_calls': parallel['peak_audio'],
        'audio_calls': sum(kind == 'audio' for kind, _, _ in serial['calls']),
        'cpa_calls': sum(kind == 'cpa' for kind, _, _ in serial['calls'])}))
    # The administrative contract requires disclosing the responsible gate
    # below 3x; timing is reported, not hidden by weakening decision checks.
    assert speedup > 0


def test_production_shaped_decisions_twenty_schedules(tmp_path, monkeypatch, serial_oracle):
    source = tmp_path/'source.mp4'
    source.write_bytes(b'fixed offline source')
    reference = comparable(shaped_run(monkeypatch, serial_oracle, tmp_path/'serial', source,
        workers=1, seed=0, scale=.00003))
    for seed in range(20):
        actual = shaped_run(monkeypatch, auditor, tmp_path/f'p{seed}', source,
                            workers=8, seed=seed, scale=.00003)
        assert comparable(actual) == reference
    print('REWORK2_DECISION_EQUIVALENCE='+json.dumps({'repetitions': 20,
        'identical': True, 'sha256': hashlib.sha256(json.dumps(reference, sort_keys=True).encode()).hexdigest()}))


@pytest.mark.parametrize('limits', [
    {'max_windows': 5, 'max_audio_ms': 1000000},
    {'max_windows': 100, 'max_audio_ms': 8500},
])
def test_production_shaped_caps_keep_serial_admission(tmp_path, monkeypatch, serial_oracle, limits):
    source = tmp_path/'source.mp4'
    source.write_bytes(b'fixed offline source')
    serial = shaped_run(monkeypatch, serial_oracle, tmp_path/'serial', source,
        workers=1, seed=0, scale=.00003, enforce_limits=True, **limits)
    reference = comparable(serial)
    assert reference['budget']['refusal_count'] > 0
    for seed in range(20):
        actual = shaped_run(monkeypatch, auditor, tmp_path/f'p{seed}', source,
            workers=8, seed=seed, scale=.00003, enforce_limits=True, **limits)
        assert comparable(actual) == reference
        assert actual['admission_order'] == serial['admission_order']
    print('REWORK2_CAPPED_EQUIVALENCE='+json.dumps({'repetitions': 20, 'limits': limits,
        'identical': True, 'attempt_count': reference['budget']['attempt_count'],
        'total_audio_ms': reference['budget']['total_audio_ms'],
        'refusal_count': reference['budget']['refusal_count']}))
