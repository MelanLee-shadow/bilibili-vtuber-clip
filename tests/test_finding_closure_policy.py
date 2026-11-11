"""Bounded deterministic policy probes; no model or audio acquisition."""
from __future__ import annotations

from dataclasses import replace
import time
from threading import Event

import pytest

from src.autoslice import finding_closure as c
from src.autoslice.finding_closure_ports import RetainedObject, ReviewResult
from src.autoslice.finding_closure_shell import (HedgedExecutor, ProviderController,
    RateFeedback, Route, ClosureRunner, FileLedger)
from tests.test_finding_closure_release import ValidOracle
from tests.test_finding_closure import SRT


@pytest.mark.parametrize('physical,cue', [('evidence-context', 1), ('new-source-question', 3)])
@pytest.mark.parametrize('terminal_unavailable', [False, True])
def test_evidence_owner_context_decision_routes_to_terminal_and_cold_replays(
    tmp_path, physical, cue, terminal_unavailable,
):
    from src.autoslice.finding_closure import ReviewCompleted
    from src.autoslice.finding_closure_shell import replay_journal
    from src.autoslice.review_errors import ProviderFailure
    from tests.test_finding_closure import prepared

    class ContextDecisionOwner(ValidOracle):
        def first_scan(self, request):
            self.calls.append(('initial_scan', None))
            return self.scan(request, (prepared(request.srt_text, physical, cue),))

        def evidence(self, request):
            self.calls.append(('evidence', request.finding.spec.key))
            # Like the ordinary acoustic owner, one effect may obtain its
            # evidence and context decision together, without a final choice.
            return ReviewResult(request.finding, c.State.AWAITING_TERMINAL,
                RetainedObject.from_value({'physical': physical, 'context_decision': 'COMPLETE',
                    'release_authorized': False}))

        def terminal(self, request):
            if terminal_unavailable:
                raise ProviderFailure('RECORDED_TERMINAL_UNAVAILABLE', provider='recorded')
            return super().terminal(request)

        def revalidate(self, request):
            if isinstance(request.event, ReviewCompleted) and request.event.state == c.State.AWAITING_TERMINAL:
                assert request.work.kind == 'evidence'
                assert request.artifacts.get(request.event.receipt_ref).value() == {
                    'physical': physical, 'context_decision': 'COMPLETE', 'release_authorized': False}
            else:
                super().revalidate(request)

    ports = ContextDecisionOwner()
    initial = c.initial_state(source_sha256='a'*64, srt_text=SRT, scan_channels=('recorded',))
    ledger = FileLedger(tmp_path/'closure', initial)
    runner = ClosureRunner(initial=initial, ledger=ledger, ports=ports, output=tmp_path/'out.srt')
    if terminal_unavailable:
        with pytest.raises(c.ClosureContractError, match='FINAL_REVIEW_ENGINEERING_FAILURE'):
            runner.run()
        state = replay_journal(initial, ledger, ports)
        assert not state.confirmation_ref
        assert state.tasks[0].state == c.State.CALL_FAILURE
    else:
        state = runner.run()
        assert state.confirmation_ref and state.tasks[0].state == c.State.KEEP
        assert [kind for kind, _ in ports.calls].count('evidence') == 1
        assert [kind for kind, _ in ports.calls].count('task_stop') == 1
    before = list(ports.calls)
    assert c.state_bytes(replay_journal(initial, ledger, ports)) == c.state_bytes(state)
    assert ports.calls == before


def route(model, modality='text', provider='recorded', key='account-a'):
    return Route(provider,modality,RetainedObject.from_value({
        'transport':'recorded','models':[model],'effort':'medium'}),key)


def material():
    from tests.test_wpb_finding_context import check,materials
    return RetainedObject.from_value(materials(check()))


def test_production_terminal_requires_discovery_r5_after_original_f2_consumer():
    from src.autoslice.finding_closure_shell import ProductionPorts
    from src.autoslice.finding_closure_ports import ValidationRequest,ArtifactSet
    from tests.test_finding_closure import prepared
    seen=[]
    finding=prepared(SRT,'keep',3)
    state=c.initial_state(source_sha256='a'*64,srt_text=SRT,scan_channels=('recorded',))
    event=c.ReviewCompleted('work',finding.spec,c.State.KEEP,'b'*64)
    request=ValidationRequest(state,c.Work('work','evidence',c.sha(SRT),finding.spec),event,ArtifactSet(()))
    class Owners:
        def revalidate(self,request):seen.append(('F2',request))
    class Discovery:
        def validate_terminal(self,request):
            seen.append(('R5',request))
            raise c.ClosureContractError('ASR_ONLY_CANNOT_SUSTAIN_UNRESOLVABLE')
    ports=ProductionPorts(Discovery(),Owners(),lambda _:None)
    with pytest.raises(c.ClosureContractError,match='ASR_ONLY_CANNOT_SUSTAIN_UNRESOLVABLE'):
        ports.revalidate(request)
    assert seen==[('F2',request),('R5',request)]


def test_discovery_import_program_error_preserves_original_object(monkeypatch):
    import importlib
    from src.autoslice.finding_closure_shell import registered_scan_channels,discovery_factory
    boom=ImportError('a dependency import failed inside the discovery owner')
    def broken(_name):raise boom
    monkeypatch.setattr(importlib,'import_module',broken)
    for operation in (registered_scan_channels,lambda:discovery_factory(None)):
        with pytest.raises(ImportError) as raised:operation()
        assert raised.value is boom


def test_adaptive_provider_windows_do_not_mix_audio_and_text_or_cap_growth():
    now=[10.0]
    controller=ProviderController(clock=lambda:now[0])
    audio,text=route('audio','audio'),route('text')
    controller.acquire(audio)
    controller.acquire(text)
    controller.finish(audio,RateFeedback(True,7))
    controller.finish(text,RateFeedback())
    assert controller.windows[('recorded','audio')].limit==10
    assert controller.windows[('recorded','audio')].retry_at==17
    assert controller.windows[('recorded','text')].limit==21
    controller.acquire(text)
    controller.finish(text,RateFeedback(successful=False))
    assert controller.windows[('recorded','text')].limit==21
    now[0]=17.0
    controller.acquire(audio)
    controller.finish(audio,RateFeedback())
    for _ in range(25):
        controller.acquire(text);controller.finish(text,RateFeedback())
    assert controller.windows[('recorded','text')].limit>40


def test_hedge_defaults_enabled_uses_measured_p95_and_retains_late_answer():
    records=[]
    executor=HedgedExecutor(controller=ProviderController(),record=records.append)
    executor.samples[('primary','terminal')]=(.003,)*30
    calls=[]
    late=Event()
    def call(selected):
        calls.append(selected.model)
        if selected.model=='primary':assert late.wait(5)
        return selected.model+' original bytes'
    output=executor.invoke(materials=material(),channel='terminal',primary=route('primary'),
        alternate=route('alternate'),call=call)
    # The second route returns without waiting for the original tail.
    assert output=='alternate original bytes'
    assert not late.is_set()
    late.set()
    executor.drain()
    assert sorted(calls)==['alternate','primary']
    assert len(records)==2 and all(r.status=='REPLIED' for r in records)
    assert {r.raw_completion for r in records}=={'primary original bytes','alternate original bytes'}
    assert all(r.answering_model is None for r in records)  # Unknown stays unknown.
    assert {r.raw_sha256 for r in records}=={c.sha('primary original bytes'),c.sha('alternate original bytes')}
    assert len({r.effect_key for r in records})==2


def test_fast_call_no_hedge_and_insufficient_samples_fall_back_to_213():
    from src.autoslice.finding_closure_shell import measured_p95
    assert measured_p95((1.0,)*29)==213
    assert measured_p95((1.0,)*29+(7.0,))==1.0
    records=[];calls=[]
    executor=HedgedExecutor(controller=ProviderController(),record=records.append)
    answer=executor.invoke(materials=material(),channel='terminal',primary=route('primary'),
        alternate=route('alternate'),call=lambda selected:(calls.append(selected.model) or 'original'))
    executor.drain()
    assert answer=='original' and calls==['primary'] and len(records)==1
    with pytest.raises(ValueError,match='DISTINCT_ROUTE_REQUIRED'):
        executor.invoke(materials=material(),channel='terminal',primary=route('same'),
            alternate=route('same'),call=lambda _:'must not execute')


def test_hedge_records_actual_model_and_propagates_late_persistence_failure():
    class Completion(str):
        answering_model='actual-recorded-model'
    records=[]
    executor=HedgedExecutor(controller=ProviderController(),record=records.append)
    executor.invoke(materials=material(),channel='terminal',primary=route('primary'),
        alternate=route('alternate'),call=lambda _:Completion('original bytes'))
    executor.drain()
    assert records[0].answering_model=='actual-recorded-model'
    assert records[0].answering_effect_key and records[0].answering_effect_key!=records[0].effect_key
    assert executor.samples[('actual-recorded-model','terminal')]

    late=Event()
    boom=OSError('recorded persistence failure')
    def retain(receipt):
        if receipt.route.model=='primary':raise boom
    executor=HedgedExecutor(controller=ProviderController(),record=retain)
    executor.samples[('primary','terminal')]=(.003,)*30
    def call(selected):
        if selected.model=='primary':assert late.wait(5)
        return selected.model+' retained bytes'
    assert executor.invoke(materials=material(),channel='terminal',primary=route('primary'),
        alternate=route('alternate'),call=call)=='alternate retained bytes'
    late.set()
    with pytest.raises(OSError) as raised:executor.drain()
    assert raised.value is boom
    assert {r.raw_completion for r in executor.receipts}=={'primary retained bytes','alternate retained bytes'}


def test_resume_engineering_failure_revalidates_original_without_new_provider_call(tmp_path):
    class RepairedConsumer(ValidOracle):
        def __init__(self):
            super().__init__();self.repaired=False;self.provider_calls=0
        def evidence(self,request):
            self.provider_calls+=1
            return ReviewResult(request.finding,c.State.CONTRACT_ERROR,
                RetainedObject.from_value({'finding':request.finding.finding.value(),'owner_error':'TEST_CONSUMER'}))
        def recover(self,request):
            assert self.repaired
            return self.result(request)
        def revalidate(self,request):
            if isinstance(request.event,c.ReviewCompleted) and request.event.state==c.State.CONTRACT_ERROR:
                assert request.artifacts.get(request.event.receipt_ref).value()['owner_error']=='TEST_CONSUMER'
            else:
                super().revalidate(request)
    ports=RepairedConsumer()
    initial=c.initial_state(source_sha256='a'*64,srt_text=SRT,scan_channels=('recorded',))
    ledger=FileLedger(tmp_path/'closure',initial)
    runner=lambda:ClosureRunner(initial=initial,ledger=ledger,ports=ports,output=tmp_path/'out.srt').run()
    with pytest.raises(ValueError,match='ENGINEERING_FAILURE'):runner()
    assert ports.provider_calls==1
    ports.repaired=True
    state=runner()
    assert state.confirmation_ref and ports.provider_calls==1
    assert any(r.state==c.State.CONTRACT_ERROR for r in state.rulings)
    assert any(r.state==c.State.KEEP for r in state.rulings)

    # The ordinary recovery records an untimed request and one timed completion
    # under the same work identity. The request must not consume that span.
    import json
    events = [entry.value()['event'] for entry in ledger.events()]
    requested = [event for event in events if event['kind'] == 'revalidate_requested']
    assert len(requested) == 1
    recovery_id = requested[0]['work_id']
    assert sum(event['kind'] == 'review_completed' and event.get('work_id') == recovery_id
               for event in events) == 1
    spans = ledger.root/'time-spans.jsonl'
    recorded = [json.loads(line) for line in spans.read_text().splitlines()]
    assert sum(span['span_id'] == recovery_id for span in recorded) == 1
    original = {path: path.read_bytes() for path in (ledger.root/'events').glob('*.json')}
    original[spans] = spans.read_bytes()

    # Reopen the committed records; timing is a derived report, not a rewrite
    # of the failed attempt, recovered ruling, or original clock observations.
    FileLedger(ledger.root, initial, create=False).summarize(initial)
    summary = json.loads((ledger.root/'timing-summary.json').read_text())
    basis = summary['workflow_critical_path_basis']
    assert basis['missing_effect_spans'] == 0
    assert basis['unmatched_spans'] == 0
    assert basis['complete'] is True
    assert original == {path: path.read_bytes() for path in original}


@pytest.mark.parametrize('tracks_helper', [False, True])
def test_extracted_result_router_revision_is_bound_to_engineering_work(tmp_path, monkeypatch, tracks_helper):
    from pathlib import Path
    from src.autoslice import finding_closure_shell as shell

    class RepairableOwner(ValidOracle):
        def __init__(self):
            super().__init__()
            self.repaired = False
            self.provider_calls = self.recoveries = 0

        def evidence(self, request):
            self.provider_calls += 1
            return ReviewResult(request.finding, c.State.CONTRACT_ERROR,
                RetainedObject.from_value({'finding': request.finding.finding.value(), 'owner_error': 'ROUTER'}))

        def recover(self, request):
            self.recoveries += 1
            return self.result(request) if self.repaired else ReviewResult(
                request.finding, c.State.CONTRACT_ERROR,
                RetainedObject.from_value({'finding': request.finding.finding.value(), 'owner_error': 'ROUTER'}))

        def revalidate(self, request):
            if isinstance(request.event, c.ReviewCompleted) and request.event.state == c.State.CONTRACT_ERROR:
                assert request.artifacts.get(request.event.receipt_ref).value()['owner_error'] == 'ROUTER'
            else:
                super().revalidate(request)

    revision = shell.consumer_revision
    if not tracks_helper:
        def old_dependency_boundary():
            value = revision().value()
            value['consumer_modules'].pop('finding_closure_result_routing.py')
            return RetainedObject.from_value(value)
        monkeypatch.setattr(shell, 'consumer_revision', old_dependency_boundary)
    ports = RepairableOwner()
    initial = c.initial_state(source_sha256='a'*64, srt_text=SRT, scan_channels=('recorded',))
    ledger = FileLedger(tmp_path/'closure', initial)
    run = lambda: ClosureRunner(initial=initial, ledger=ledger, ports=ports, output=tmp_path/'out.srt').run()
    for _ in range(2):
        with pytest.raises(c.ClosureContractError, match='ENGINEERING_FAILURE'):
            run()
    assert ports.recoveries == 1 and ports.provider_calls == 1
    before = ledger.events()
    old_revision = next(row.value()['event']['consumer_revision_ref'] for row in reversed(before)
                        if row.value()['event']['kind'] == 'revalidate_requested')
    read_text = Path.read_text
    helper = Path(shell.__file__).with_name('finding_closure_result_routing.py')

    def changed_router(path, *args, **kwargs):
        text = read_text(path, *args, **kwargs)
        return text + '\n# repaired result consumer\n' if path == helper else text

    monkeypatch.setattr(Path, 'read_text', changed_router)
    ports.repaired = True
    state = run()
    assert state.confirmation_ref and ports.recoveries == 2
    assert any(row.state == c.State.CONTRACT_ERROR for row in state.rulings)
    current_revision = next(row.value()['event']['consumer_revision_ref'] for row in reversed(ledger.events())
                            if row.value()['event']['kind'] == 'revalidate_requested')
    assert current_revision == shell.consumer_revision().sha256
    assert (current_revision != old_revision) is tracks_helper
    assert ports.provider_calls == 1
    assert ledger.events()[:len(before)] == before


@pytest.mark.parametrize('error_type',[TypeError,KeyError,ValueError,OSError,RuntimeError])
def test_program_errors_keep_original_object_and_never_become_content_or_contract(tmp_path,error_type):
    boom=error_type('FINDING_CLOSURE_FAKE_REASON_CODE')
    boom.reason_code='FINDING_CLOSURE_FAKE_REASON_CODE'
    class Broken(ValidOracle):
        def first_scan(self,request):raise boom
    initial=c.initial_state(source_sha256='a'*64,srt_text=SRT,scan_channels=('recorded',))
    ledger=FileLedger(tmp_path/'closure',initial)
    with pytest.raises(error_type) as raised:
        ClosureRunner(initial=initial,ledger=ledger,ports=Broken()).run()
    assert raised.value is boom
    from src.autoslice.finding_closure_shell import decode_event
    events=[decode_event(RetainedObject.from_value(e.value()['event'])) for e in ledger.events()]
    assert not any(isinstance(e,c.WorkFailed) for e in events)
    assert not any(isinstance(e,c.Confirmed) for e in events)
