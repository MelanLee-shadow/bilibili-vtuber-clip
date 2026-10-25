"""Offline contracts for the actual filesystem admission and transport seams."""
from contextlib import ExitStack
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor
import fcntl
import io
import json
import multiprocessing
import os
from pathlib import Path
import time
import urllib.error

import pytest

from src.autoslice import provider_admission as admission
from src.autoslice import provider_slots as slots


def snapshot(root, provider='cpa', modality='text'):
    bucket = slots._route_root(Path(root), provider, modality)
    return json.loads((bucket / 'window.json').read_text())


def assert_route_held(root, provider, modality):
    bucket = slots._route_root(Path(root), provider, modality)
    active = snapshot(root, provider, modality)['active']
    assert active
    # Observe the kernel lease, rather than assuming an unchanging capacity=1.
    with (bucket / f'slot-{active[-1]}.lock').open('rb') as lease:
        with pytest.raises(BlockingIOError):
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)


def saturate(root, provider='gemini', modality='audio'):
    held = ExitStack()
    try:
        slots._safe_runtime_root(Path(root))
        bucket = slots._route_root(Path(root), provider, modality)
        limit = snapshot(root, provider, modality)['limit'] if (bucket / 'window.json').exists() else slots.provider_capacity()
        for _ in range(limit):
            lease = held.enter_context(slots.provider_slot(Path(root), provider=provider, modality=modality))
            lease.report(admission.RateFeedback(successful=False))
        return held
    except BaseException:
        held.close()
        raise


def _feedback_worker(root, result):
    with slots.runtime_provider_slot(runtime_root=root, provider='cpa', modality='text') as lease:
        lease.report({'http_status': 429, 'retry_after_seconds': 5})
    result.put(snapshot(root))


def _observer_worker(root, result):
    before = snapshot(root)
    try:
        with slots.provider_slot(Path(root), timeout_seconds=.05):
            outcome = 'unexpected'
    except slots.ProviderSlotTimeout:
        outcome = 'cooldown'
    # Neither a different provider nor a different modality shares that pause.
    with slots.provider_slot(Path(root), provider='gemini', modality='text', timeout_seconds=.1):
        pass
    with slots.provider_slot(Path(root), provider='cpa', modality='audio', timeout_seconds=.1):
        pass
    result.put((before, outcome))


def _hold_worker(root, entered, release, result, crash=False):
    with slots.provider_slot(Path(root), timeout_seconds=5) as lease:
        result.put(lease.slot_index)
        entered.set()
        release.wait(10)
        if crash:
            os._exit(0)


def _join(process):
    process.join(10)
    assert not process.is_alive()
    assert process.exitcode == 0


def test_cross_process_rate_feedback_halves_window_and_shares_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_PROVIDER_CONCURRENCY', '8')
    context = multiprocessing.get_context('spawn')
    results = context.Queue()
    first = context.Process(target=_feedback_worker, args=(str(tmp_path), results))
    first.start()
    first_state = results.get(timeout=10)
    _join(first)
    second = context.Process(target=_observer_worker, args=(str(tmp_path), results))
    second.start()
    observed, outcome = results.get(timeout=10)
    _join(second)
    assert first_state['limit'] == observed['limit'] == 4
    assert first_state['retry_at'] == observed['retry_at']
    assert outcome == 'cooldown'
    assert snapshot(tmp_path, 'gemini')['limit'] == 9
    time.sleep(max(0, first_state['retry_at'] - time.time()) + .01)
    with slots.provider_slot(tmp_path, timeout_seconds=.1):
        pass
    assert snapshot(tmp_path)['limit'] == 5


def test_initial_window_above_old_64_ceiling_is_not_clamped(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_PROVIDER_CONCURRENCY', '65')
    with slots.provider_slot(tmp_path):
        pass
    assert snapshot(tmp_path)['limit'] == 66


def test_growth_and_shared_inflight_exceed_initial_window(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_PROVIDER_CONCURRENCY', '1')
    for _ in range(70):
        with slots.provider_slot(tmp_path):
            pass
    context = multiprocessing.get_context('spawn')
    entered, release, results = context.Event(), context.Event(), context.Queue()
    child = context.Process(target=_hold_worker, args=(str(tmp_path), entered, release, results))
    child.start()
    try:
        assert entered.wait(10)
        assert results.get(timeout=10) == 0
        with ExitStack() as calls:
            for _ in range(64):
                calls.enter_context(slots.provider_slot(tmp_path, timeout_seconds=1))
            assert len(snapshot(tmp_path)['active']) == 65
    finally:
        release.set()
        _join(child)
    assert not snapshot(tmp_path)['active']
    assert snapshot(tmp_path)['limit'] == 136


def test_shrink_counts_all_live_indices_not_just_low_slots(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_PROVIDER_CONCURRENCY', '4')
    with ExitStack() as active:
        leases = [active.enter_context(slots.provider_slot(tmp_path)) for _ in range(3)]
        with slots.provider_slot(tmp_path) as limited:
            limited.report({'quota_exhausted': True})
        state = snapshot(tmp_path)
        assert state['limit'] == 2 and len(state['active']) == 3
        # Let the cooldown elapse; excess live calls still prevent admission.
        time.sleep(max(0, state['retry_at'] - time.time()) + .01)
        with pytest.raises(slots.ProviderSlotTimeout):
            with slots.provider_slot(tmp_path, timeout_seconds=.02):
                pass
        for lease in leases:
            lease.report(admission.RateFeedback(successful=False))
    assert snapshot(tmp_path)['limit'] == 2


def test_success_adds_one_without_erasing_a_shared_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_PROVIDER_CONCURRENCY', '4')
    with slots.provider_slot(tmp_path):
        with slots.provider_slot(tmp_path) as limited:
            limited.report({'http_status': 429, 'retry_after_seconds': 60})
        cooling = snapshot(tmp_path)
        assert cooling['limit'] == 2
    finished = snapshot(tmp_path)
    assert finished['limit'] == 3
    assert finished['retry_at'] == cooling['retry_at']


def test_crashed_process_lease_is_reclaimed_without_increasing_window(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_PROVIDER_CONCURRENCY', '1')
    context = multiprocessing.get_context('spawn')
    entered, release, results = context.Event(), context.Event(), context.Queue()
    child = context.Process(target=_hold_worker, args=(str(tmp_path), entered, release, results, True))
    child.start()
    assert entered.wait(10)
    assert results.get(timeout=10) == 0
    release.set()
    _join(child)
    assert snapshot(tmp_path)['active'] == [0]
    with slots.provider_slot(tmp_path, timeout_seconds=.1) as lease:
        assert lease.slot_index == 0
        assert snapshot(tmp_path)['limit'] == 1
    assert snapshot(tmp_path)['limit'] == 2


def test_nested_admission_uses_one_physical_lease_and_one_feedback(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    monkeypatch.setenv('AUTOSLICE_PROVIDER_CONCURRENCY', '2')
    with admission.provider_admission('canonical-runtime-cpa:local', 'text') as report:
        with slots.runtime_provider_slot(provider='cpa', modality='text') as lease:
            assert snapshot(tmp_path)['active'] == [lease.slot_index]
            report({'http_status': 429})
    assert snapshot(tmp_path)['limit'] == 1


def test_copied_context_in_another_thread_must_acquire_its_own_lease(tmp_path, monkeypatch):
    from threading import Event
    monkeypatch.setenv('AUTOSLICE_PROVIDER_CONCURRENCY', '1')
    entered = Event()
    def call():
        with slots.runtime_provider_slot(runtime_root=tmp_path, timeout_seconds=2):
            entered.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with slots.runtime_provider_slot(runtime_root=tmp_path) as parent:
            future = pool.submit(copy_context().run, call)
            try:
                assert not entered.wait(.05)
            finally:
                parent.report(admission.RateFeedback(successful=False))
        future.result(timeout=5)
    assert entered.is_set()
    assert snapshot(tmp_path)['limit'] == 2


def test_canonical_wrapper_does_not_charge_a_cached_transport_result(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    def forbidden(**_kwargs):
        pytest.fail('cached result must not enter provider admission')
    monkeypatch.setattr(slots, 'runtime_provider_slot', forbidden)
    primary = lambda prompt: 'already cached'
    wrapped = admission.FinalReviewCall(primary, provider='canonical-runtime-cpa:local')
    assert wrapped('same request') == 'already cached'


@pytest.mark.parametrize('damage', ['json', 'symlink', 'permissions'])
def test_corrupted_shared_state_never_falls_back_to_private_window(tmp_path, damage):
    with slots.provider_slot(tmp_path):
        pass
    state = tmp_path / 'provider-slots/window.json'
    if damage == 'json':
        state.write_text('{')
    elif damage == 'symlink':
        state.unlink()
        state.symlink_to(tmp_path / 'foreign')
    else:
        state.chmod(0o644)
    with pytest.raises(slots.ProviderSlotError):
        with slots.provider_slot(tmp_path, timeout_seconds=.1):
            pytest.fail('unsafe admission')


def test_http_429_feedback_survives_caught_image_transport_error(tmp_path, monkeypatch):
    from src.autoslice import cover_generation
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    reference = tmp_path / 'reference.png'
    reference.write_bytes(b'fixture')
    failure = urllib.error.HTTPError('https://cpa.invalid', 429, 'limited', {'Retry-After': '3'}, io.BytesIO(b'{}'))
    def fail(*_args, **_kwargs):
        raise failure
    monkeypatch.setattr('urllib.request.urlopen', fail)
    result = cover_generation._call_cpa_image_edit(base_url='https://cpa.invalid',
        api_key='fictional', reference_path=reference, output_path=tmp_path / 'output.png',
        prompt='fixture', request_path=tmp_path / 'request.json', response_path=tmp_path / 'response.json')
    assert result['reason_code'] == 'CPA_IMAGE_EDIT_HTTP_ERROR'
    state = snapshot(tmp_path, 'cpa', 'image')
    assert state['limit'] == 10
    assert state['retry_at'] > time.time() + 2


@pytest.mark.parametrize('value', [{'retry_after_seconds': 2}, {'error': {'code': 429, 'status': 'RESOURCE_EXHAUSTED'}}])
def test_structured_provider_feedback_is_not_lost(value):
    assert admission.observed_rate_feedback(value).rate_limited


def test_command_429_prior_to_success_updates_shared_window(tmp_path, monkeypatch):
    import subprocess
    from src.autoslice import llm_client as llm
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    diagnostics = llm.PROVIDER_DIAGNOSTIC_MARKER + json.dumps({
        'provider_http_status': 429, 'provider_retry_after_seconds': 5})
    def run(argv, **_kwargs):
        Path(argv[2]).write_text('same saved answer')
        return subprocess.CompletedProcess(argv, 0, '', diagnostics + '\n' +
            '[cpa] model=fixture attempt=2 http=200 curl_exit=0 body_bytes=1 result=ok')
    monkeypatch.setattr(llm.subprocess, 'run', run)
    call = llm.build_llm_call(llm.LlmConfig(transport='command',
        command_template='fictional {prompt_file} {completion_file}'))
    before = time.time()
    assert call('unchanged prompt') == 'same saved answer'
    state = snapshot(tmp_path)
    assert state['limit'] == 10
    assert state['retry_at'] >= before + 5


@pytest.mark.parametrize('diagnostic', ['quota exhausted', 'HTTP 429 rate limit'])
def test_agy_canonical_cli_failure_classification_reaches_shared_window(tmp_path, monkeypatch, diagnostic):
    import subprocess
    from src.autoslice import agy_gemini_client as agy
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    result = agy.run_local_agy(['fictional-agy'], cwd=tmp_path, timeout=2,
        command_runner=lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, '', diagnostic))
    assert result.completed.returncode == 1
    state = snapshot(tmp_path, 'agy', 'audio_video')
    assert state['limit'] == 10 and state['retry_at'] > time.time()


def test_decorated_checkpoint_identity_keeps_timeout_callback_identity():
    from src.autoslice.normal_cover_generation_checkpoint import callable_identity
    def send():
        return 'unused'
    first = slots.provider_transport(timeout_result=lambda: 'first')(send)
    second = slots.provider_transport(timeout_result=lambda: 'second')(send)
    assert callable_identity(first) != callable_identity(second)


def test_decorated_checkpoint_still_refuses_unbound_captured_state():
    from src.autoslice.normal_cover_generation_checkpoint import callable_identity
    def factory(value):
        def send():
            return value
        return slots.provider_transport()(send)
    with pytest.raises(ValueError, match='COVER_CHECKPOINT_CALLABLE_BINDING_REQUIRED'):
        callable_identity(factory('fixture-only'))


@pytest.mark.parametrize('phase', ['window', 'slot'])
def test_runtime_root_replacement_cannot_keep_authority_via_hardlinked_lock(tmp_path, monkeypatch, phase):
    root, old = tmp_path / 'runtime', tmp_path / 'old-runtime'
    root.mkdir()
    real_flock = slots.fcntl.flock
    replaced = False
    def replace(fd, operation):
        nonlocal replaced
        real_flock(fd, operation)
        target = root / 'provider-slots' / ('window.lock' if phase == 'window' else 'slot-0.lock')
        if (not replaced and operation & fcntl.LOCK_EX and target.exists()
                and os.fstat(fd).st_ino == target.stat().st_ino):
            replaced = True
            root.rename(old)
            (root / 'provider-slots').mkdir(mode=0o700, parents=True)
            for name in ('window.lock', 'slot-0.lock'):
                if (old / 'provider-slots' / name).exists():
                    os.link(old / 'provider-slots' / name, root / 'provider-slots' / name)
    monkeypatch.setattr(slots.fcntl, 'flock', replace)
    with pytest.raises(slots.ProviderSlotError, match='runtime root ownership drifted'):
        with slots.provider_slot(root):
            pytest.fail('replacement root must not inherit authority')
