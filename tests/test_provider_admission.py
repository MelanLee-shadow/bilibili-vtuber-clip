"""Offline admission contracts, including original transport/checkpoint identity."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from threading import Barrier, Event

import pytest

from src.autoslice import provider_admission as shared


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('OFFLINE_NO_NETWORK'))


def test_success_additive_increase_and_no_fixed_ceiling():
    controller = shared.ProviderController()
    route = shared.Route('offline', 'text')
    for _ in range(300):
        controller.acquire(route)
        controller.finish(route, shared.RateFeedback())
    assert controller.windows[('offline', 'text')] == shared.ProviderWindow(320, 0, 0.0)
    with pytest.raises(FrozenInstanceError):
        route.provider = 'other'


@pytest.mark.parametrize('signal', [
    {'provider_http_status': 429, 'provider_retry_after_seconds': 7},
    {'http_status': '429', 'retry_after_seconds': 7},
    {'provider_error_code': 'insufficient_quota', 'retry_after': 7},
    {'quota_exhausted': True, 'retry_after': 7},
    {'nested': [{'metadata': {'error_code': 'RESOURCE_EXHAUSTED', 'retry_after': 7}}]},
])
def test_explicit_rate_limit_halves_window_and_retains_retry_after(signal):
    controller = shared.ProviderController(clock=lambda: 100.0)
    with shared.provider_admission('offline', 'text', controller=controller) as report:
        report(signal)
    assert controller.windows[('offline', 'text')] == shared.ProviderWindow(10, 0, 107.0)


def test_retry_after_blocks_next_admission_until_deadline():
    now = [100.0]
    controller = shared.ProviderController(clock=lambda: now[0])
    route = shared.Route('offline', 'text')
    controller.acquire(route)
    controller.finish(route, shared.RateFeedback(True, 30.0, False))
    started, acquired = Event(), Event()
    def later():
        started.set()
        controller.acquire(route)
        acquired.set()
        controller.finish(route, shared.RateFeedback())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(later)
        try:
            assert started.wait(5)
            assert not acquired.wait(.03)
        finally:
            with controller.condition:
                now[0] = 130.0
                controller.condition.notify_all()
        future.result(timeout=5)
    assert acquired.is_set() and controller.windows[('offline', 'text')].inflight == 0


def test_grown_window_actually_admits_more_than_initial_twenty():
    controller = shared.ProviderController()
    route = shared.Route('offline', 'text')
    for _ in range(10):
        controller.acquire(route)
        controller.finish(route, shared.RateFeedback())
    barrier = Barrier(26)
    def invoke(_):
        controller.acquire(route)
        try:
            barrier.wait(timeout=5)
            barrier.wait(timeout=5)
        finally:
            controller.finish(route, shared.RateFeedback())
    with ThreadPoolExecutor(max_workers=25) as pool:
        futures = [pool.submit(invoke, index) for index in range(25)]
        barrier.wait(timeout=5)
        try:
            assert controller.windows[('offline', 'text')].inflight == 25
        finally:
            barrier.wait(timeout=5)
        for future in futures:
            future.result(timeout=5)


def test_nested_same_owner_uses_one_lease_and_combines_report_with_error():
    controller = shared.ProviderController(clock=lambda: 1.0)
    failure = RuntimeError('429 in arbitrary text is not a signal')
    with pytest.raises(RuntimeError) as caught:
        with shared.provider_admission('offline', 'audio', controller=controller) as outer:
            with shared.provider_admission('offline', 'audio', controller=controller) as inner:
                assert outer is inner
                assert controller.windows[('offline', 'audio')].inflight == 1
                inner({'http_status': 429, 'retry_after': 5})
                raise failure
    assert caught.value is failure
    assert controller.windows[('offline', 'audio')] == shared.ProviderWindow(10, 0, 6.0)


def test_non_rate_error_preserves_window_and_original_exception_without_retry():
    controller = shared.ProviderController()
    calls = []
    failure = RuntimeError('quota 429 Retry-After 9 is only arbitrary text')
    def original(prompt):
        calls.append(prompt)
        raise failure
    call = shared.FinalReviewCall(original, provider='offline', controller=controller)
    with pytest.raises(RuntimeError) as caught:
        call('original prompt')
    assert caught.value is failure and calls == ['original prompt']
    assert controller.windows[('offline', 'text')] == shared.ProviderWindow(20, 0, 0.0)


def test_structured_exception_feedback_is_used():
    error = RuntimeError('unread error text')
    error.provider_diagnostics = {'provider_http_status': 429, 'provider_retry_after_seconds': 12}
    assert shared.rate_feedback(error) == shared.RateFeedback(True, 12, False)
    for delay in (float('nan'), float('inf'), -1, True, '12'):
        error.retry_after = delay
        error.provider_diagnostics = {}
        assert shared.rate_feedback(error).retry_after == 0


def test_closure_and_legacy_share_one_admission_owner_and_failure_taxonomy():
    from src.autoslice import finding_closure_shell as closure
    from src.autoslice.review_errors import ContractRejection, ProviderFailure, review_error_record
    assert closure.ProviderController is shared.ProviderController
    assert closure.PROVIDER_CONTROLLER is shared.PROVIDER_CONTROLLER
    assert closure.provider_admission is shared.provider_admission
    assert closure._feedback is shared.rate_feedback
    controller = shared.ProviderController()
    with closure.provider_admission('offline', 'audio', controller=controller) as outer:
        with shared.provider_admission('offline', 'audio', controller=controller) as inner:
            assert outer is inner
            assert controller.windows[('offline', 'audio')].inflight == 1
    route = shared.Route('offline', 'text')
    controller.acquire(route)
    with pytest.raises(shared.ProviderAdmissionError) as caught:
        controller.finish(route, shared.RateFeedback(retry_after=-1))
    assert isinstance(caught.value, ContractRejection)
    assert isinstance(caught.value, closure.failure_types()[0])
    assert review_error_record(caught.value)['status'] == 'CONTRACT_ERROR'
    controller.finish(route, shared.RateFeedback(successful=False))
    failure = ProviderFailure('RECORDED_RATE_LIMIT', provider='offline', metadata={'http_status': 429})
    wrapped = shared.FinalReviewCall(lambda _: (_ for _ in ()).throw(failure), provider='offline', controller=controller)
    with pytest.raises(ProviderFailure) as propagated:
        wrapped('original prompt')
    assert propagated.value is failure
    assert review_error_record(failure)['status'] == 'CALL_FAILURE'
    assert controller.windows[('offline', 'text')].limit == 10


def test_wrapper_preserves_raw_subclass_metadata_and_existing_checkpoint_binding(tmp_path):
    from tests.test_f1_ruling_reuse import bound_transport
    from src.autoslice.completed_review_checkpoint import CompletedReviewCheckpoint
    original = bound_transport(tmp_path, 'a' * 40)
    wrapped = shared.FinalReviewCall(original, provider='offline')
    args = dict(stage='ruling', prompt='same material', inputs={'audio': 'a' * 64},
                parser_path=Path(__file__), root=tmp_path / 'raw')
    before = CompletedReviewCheckpoint.for_call(llm_call=original, **args)
    after = CompletedReviewCheckpoint.for_call(llm_call=wrapped, **args)
    assert before is not None and after is not None
    assert before.binding == after.binding and before.directory == after.directory
    assert wrapped.cpa_cache_identity is original.cpa_cache_identity
    class OriginalRaw(str):
        pass
    raw = OriginalRaw(' untouched raw bytes \n')
    assert shared.FinalReviewCall(lambda _: raw, provider='offline')('prompt') is raw


def test_factory_returns_admitted_original_canonical_call(monkeypatch, tmp_path):
    from src.autoslice import producer_final_review_transport as transport
    from tests.test_f1_ruling_reuse import provider
    original = provider()
    controller = shared.ProviderController()
    monkeypatch.setattr(shared, 'PROVIDER_CONTROLLER', controller)
    monkeypatch.setattr(transport, '_build_review_llm_call', lambda **_: original)
    wrapped = transport.build_final_review_llm_call(runtime_root=tmp_path)
    assert isinstance(wrapped, shared.FinalReviewCall)
    assert wrapped.primary is original
    assert wrapped('same request') == '{"findings":[]}'
    assert original.calls == ['same request']
    assert controller.windows[('canonical-runtime-cpa:local', 'text')].limit == 21


def test_success_does_not_consume_shared_mutable_diagnostics_from_another_request():
    from tests.test_f1_ruling_reuse import provider
    controller = shared.ProviderController()
    original = provider()
    # The main factory's compatibility diagnostics are shared across requests;
    # only this invocation's own exception can supply failure feedback here.
    original.provider_diagnostics = {'provider_http_status': 429, 'provider_retry_after_seconds': 60}
    wrapped = shared.FinalReviewCall(original, provider='offline', controller=controller)
    assert wrapped('fresh successful request') == '{"findings":[]}'
    assert controller.windows[('offline', 'text')] == shared.ProviderWindow(21, 0, 0.0)
