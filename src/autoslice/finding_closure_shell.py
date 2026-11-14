"""Durable effects, typed ports and cold validation around finding_closure.

The event journal is the commit point. SRT and state.json are projections that
can be regenerated after a process kill. Replay never invokes a call port.
"""
from __future__ import annotations

from concurrent.futures import Future, wait, FIRST_COMPLETED
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time
from threading import Condition, Thread
from typing import Callable, Iterator, Literal, Protocol, cast

from . import finding_closure as core
from .provider_admission import PROVIDER_CONTROLLER as PROVIDER_CONTROLLER, ProviderController as ProviderController, RateFeedback as RateFeedback, provider_admission as provider_admission, rate_feedback as _feedback
from .finding_closure import ClosureContractError
from .finding_closure_ports import JsonValue, PreparedFinding, RetainedObject, ConsumedRuling


def _object(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ClosureContractError("FINDING_CLOSURE_WIRE_OBJECT_REQUIRED")
    return value


def _text(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise ClosureContractError("FINDING_CLOSURE_WIRE_STRING_REQUIRED")
    return value


def _array(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        raise ClosureContractError("FINDING_CLOSURE_WIRE_ARRAY_REQUIRED")
    return value


def _atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _legacy(function: object, *args: object, **kwargs: object) -> object:
    """The sole call boundary to not-yet-typed existing owners."""
    return cast(Callable[..., object], function)(*args, **kwargs)


@dataclass(frozen=True)
class Route:
    provider: str
    modality: Literal['text', 'audio']
    model_identity: RetainedObject
    key_id: str

    @property
    def model(self) -> str:
        models = _array(self.model_identity.value()['models'])
        if len(models) != 1 or not self.provider or not self.key_id:
            raise ClosureContractError('FINDING_CLOSURE_ROUTE_IDENTITY_REQUIRED')
        return _text(models[0])


@dataclass(frozen=True)
class AttemptReceipt:
    effect_key: str
    route: Route
    channel: str
    started_utc: str
    ended_utc: str
    elapsed_s: float
    status: Literal['REPLIED', 'FAILED']
    raw_sha256: str
    error_type: str
    raw_completion: str | None
    answering_model: str | None
    answering_effect_key: str | None
    cache_hit: bool = False
    schema_version: Literal['routed-attempt.v1'] = 'routed-attempt.v1'


def measured_p95(samples: tuple[float, ...]) -> float:
    values = sorted(v for v in samples if math.isfinite(v) and v >= 0)
    return values[math.ceil(.95*len(values))-1] if len(values) >= 30 else 213.0


@contextmanager
def _routed_admission(controller: ProviderController, route: Route) -> Iterator[Callable[[RateFeedback], None]]:
    # Canonical transports acquire only after their checkpoint lookup, exactly
    # as provider_admission.FinalReviewCall does. They own shared feedback too.
    if controller.shared_runtime and route.provider.startswith('canonical-runtime-cpa:'):
        yield lambda _feedback: None
        return
    with provider_admission(route.provider, route.modality, controller=controller) as report:
        def feedback(value: RateFeedback) -> None:
            report({'http_status': 429 if value.rate_limited else 200,
                    'retry_after_seconds': value.retry_after,
                    'status': 'OK' if value.successful else 'FAILED'})
        yield feedback


class HedgedExecutor:
    """At most one hedge, enabled by default; every late outcome is retained.

    Call factories are explicit owners. This class cannot invent credentials,
    change an owner's model, cancel the losing request, or classify an answer.
    """
    def __init__(self, *, controller: ProviderController,
                 record: Callable[[AttemptReceipt], None], enabled: bool = True,
                 feedback: Callable[[BaseException | None], RateFeedback] = _feedback,
                 retention: Callable[[str],bool] | None = None) -> None:
        self.controller, self.record, self.enabled, self.feedback = controller, record, enabled, feedback
        self.retention=retention
        self.samples: dict[tuple[str, str], tuple[float, ...]] = {}
        self.threads: list[Thread] = []
        self.outcomes: list[Future[tuple[str | None, BaseException | None, float]]] = []
        self.receipts: list[AttemptReceipt] = []
        self.persistence_errors: list[BaseException] = []
        self.condition = Condition()

    def invoke(self, *, materials: RetainedObject, channel: str, primary: Route, alternate: Route,
               call: Callable[[Route], str], key_for_route: Callable[[Route],str] | None = None) -> str:
        from .finding_context import effect_key
        if primary.model == alternate.model and primary.key_id == alternate.key_id:
            raise ClosureContractError('FINDING_CLOSURE_HEDGE_DISTINCT_ROUTE_REQUIRED')
        def start(route: Route) -> Future[tuple[str | None, BaseException | None, float]]:
            future: Future[tuple[str | None, BaseException | None, float]] = Future()
            context = copy_context()
            identity = key_for_route(route) if key_for_route else cast(str, _legacy(effect_key,
                materials.value(), channel=channel, provider=route.provider + ':' + route.key_id,
                model=route.model_identity.value()))
            def invoke_owner() -> None:
                answer: str | None = None
                error: BaseException | None = None
                try:
                    with _routed_admission(self.controller, route) as report:
                        started = time.monotonic()
                        begin = datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
                        try:
                            answer = call(route)
                        except BaseException as exc:
                            error = exc
                        ended = time.monotonic()
                        cache_hit=bool(getattr(answer,'ruling_cache_hit',False))
                        report(RateFeedback(successful=False) if cache_hit else self.feedback(error))
                except BaseException as exc:
                    future.set_result((None, exc, time.monotonic()))
                    return
                try:
                    if answer is not None and self.retention is not None and not self.retention(answer):
                        answer=None
                        declined=ClosureContractError('FINDING_CLOSURE_RETENTION_DECLINED')
                        with self.condition:self.persistence_errors.append(declined)
                        raise declined
                    actual_model=getattr(answer,'answering_model',None)
                    if not isinstance(actual_model,str) or not actual_model:
                        actual_model=None
                    actual_key=None
                    if actual_model is not None:
                        actual_identity=route.model_identity.value()
                        actual_identity['models']=[actual_model]
                        actual_key=(key_for_route(replace(route,model_identity=RetainedObject.from_value(actual_identity)))
                            if key_for_route else cast(str,_legacy(effect_key,materials.value(),channel=channel,
                            provider=route.provider+':'+route.key_id,model=actual_identity)))
                    receipt=AttemptReceipt(identity, route, channel, begin,
                        datetime.now(timezone.utc).isoformat().replace('+00:00','Z'), ended-started,
                        'REPLIED' if error is None else 'FAILED', core.sha(answer) if answer is not None else '',
                        type(error).__name__ if error is not None else '',answer,actual_model,actual_key,cache_hit)
                    with self.condition:
                        self.receipts.append(receipt)
                    try:
                        self.record(receipt)
                    except BaseException as exc:
                        with self.condition:
                            self.persistence_errors.append(exc)
                        raise
                    if error is None and actual_model is not None and not cache_hit:
                        with self.condition:
                            key = actual_model, channel
                            self.samples[key] = (*self.samples.get(key, ()), ended-started)
                except BaseException as exc:
                    error = exc
                future.set_result((answer, error, ended))
            thread = Thread(target=lambda:context.run(invoke_owner), name='finding-hedge-' + identity[:12])
            with self.condition:
                self.threads.append(thread)
                self.outcomes.append(future)
            thread.start()
            return future
        original = start(primary)
        threshold = measured_p95(self.samples.get((primary.model, channel), ()))
        done, _ = wait((original,), timeout=threshold if self.enabled else None)
        if not done:
            hedge = start(alternate)
            done, _ = wait((original, hedge), return_when=FIRST_COMPLETED)
        selected = min(done, key=lambda f: f.result()[2])
        answer, error, _ = selected.result()
        if error is not None:
            raise error
        if answer is None:
            raise ClosureContractError('FINDING_CLOSURE_EMPTY_CALL_OUTCOME')
        return answer

    def drain(self) -> None:
        # Used by the owner at its normal shutdown/collection point. Nothing
        # labels a late reply as cancelled, timeout, or content failure.
        for thread in self.threads:
            thread.join()
        if self.persistence_errors:
            raise self.persistence_errors[0]


class CanonicalCheckpoint(Protocol):
    root: Path
    directory: Path
    binding: object
    hit: bool
    def load(self) -> str | None: ...
    def response(self, call: Callable[[str], str], *, preserve_raw: bool = True) -> str: ...
    def response_checkpoint(self, raw: str) -> CanonicalCheckpoint: ...


@dataclass(frozen=True)
class RulingInvocation:
    stage: str
    prompt: str
    inputs: RetainedObject
    parser_path: Path
    response_schema: object
    name: str


@dataclass
class RulingCapture:
    finding: PreparedFinding | None
    router: FinalReviewCall | None
    records: list[ConsumedRuling]
    retention_declined: bool = False
    failures: list[BaseException] = field(default_factory=list)

    def raise_failure(self) -> None:
        if self.failures:raise self.failures[0]


_RULING_CAPTURE: ContextVar[RulingCapture | None] = ContextVar('closure_ruling_capture',default=None)
_SELECTED_ROUTE: ContextVar[Route | None] = ContextVar('closure_selected_route',default=None)


def current_ruling_capture() -> RulingCapture | None:
    return _RULING_CAPTURE.get()


def _input_check(inputs: RetainedObject) -> RetainedObject | None:
    value=inputs.read_view()
    material=value.get('material')
    if isinstance(material,dict):
        value={**value,**material}
    request=value.get('request')
    if isinstance(request,dict):
        value={**value,**request}
    check=value.get('original_check_request',value.get('check_request'))
    return RetainedObject.from_value(check) if isinstance(check,dict) else None


def _input_materials(inputs: RetainedObject) -> RetainedObject | None:
    value=inputs.read_view()
    material=value.get('material')
    if isinstance(material,dict):
        value={**value,**material}
    request=value.get('request')
    if isinstance(request,dict):
        value={**value,**request}
    materials=value.get('finding_materials')
    return RetainedObject.from_value(materials) if isinstance(materials,dict) else None


def consumed_ruling(*, invocation: RulingInvocation, raw: str, identity: object,
                    origin: RetainedObject, checkpoint: RetainedObject) -> ConsumedRuling:
    from .finding_context import materials_for_model, materials_for_origin
    check,material=_input_check(invocation.inputs),_input_materials(invocation.inputs)
    if check is None or material is None:
        raise ClosureContractError('FINDING_CLOSURE_OWNER_TYPED_MATERIALS_REQUIRED')
    actual=getattr(raw,'answering_model',None)
    if not isinstance(actual,str) or not actual:
        raise ClosureContractError('FINDING_CLOSURE_ANSWER_MODEL_UNKNOWN')
    model=RetainedObject.from_value(identity).value()
    model['models']=[actual]
    bound=_legacy(materials_for_origin,
        _legacy(materials_for_model,material.read_view(),model,_read_only=True),
        origin.read_view(),_read_only=True)
    return ConsumedRuling(invocation.stage,check,RetainedObject.from_value(bound),invocation.inputs,
        RetainedObject.from_value(model),invocation.prompt,str(raw),origin,checkpoint)


class FinalReviewCall:
    """Explicit canonical model routes; no credentials or dispatch implementation here."""
    def __init__(self, *, primary: Callable[[str],str], factory: Callable[[str],Callable[[str],str]],
                 models: tuple[str,...], provider: str, key_id: str,
                 controller: ProviderController = PROVIDER_CONTROLLER) -> None:
        if len(models)<2 or len(set(models))!=len(models):
            raise ClosureContractError('FINDING_CLOSURE_HEDGE_ROUTES_REQUIRED')
        self.factory,self.models,self.controller=factory,models,controller
        self.calls: dict[str,Callable[[str],str]]={models[0]:primary}
        self.cpa_cache_identity=RetainedObject.from_value(getattr(primary,'cpa_cache_identity')).value()
        self.provider_runtime_binding=getattr(primary,'provider_runtime_binding',None)
        self.provider_diagnostics=getattr(primary,'provider_diagnostics',None)
        self.semantic_policy_version=getattr(primary,'semantic_policy_version','producer-final-review-ruling.v1')
        self.terminal_retry_capable=getattr(primary,'terminal_retry_capable',False)
        self.durable_text_transport=getattr(primary,'durable_text_transport',False)
        self.timeout_seconds=getattr(primary,'timeout_seconds',None)
        self.provider,self.key_id=provider,key_id
        self.condition=Condition()
        self.executors: list[HedgedExecutor]=[]
        self.samples: dict[tuple[str,str],tuple[float,...]]={}

    def route(self, model: str) -> Route:
        identity=dict(self.cpa_cache_identity)
        identity['models']=[model]
        return Route(self.provider,'text',RetainedObject.from_value(identity),self.key_id)

    def raw_call(self, route: Route, *, dispatch: bool = False) -> Callable[[str],str]:
        if route.model not in self.models:
            raise ClosureContractError('FINDING_CLOSURE_ROUTE_OUTSIDE_CANONICAL_LADDER')
        with self.condition:
            if route.model not in self.calls:
                self.calls[route.model]=self.factory(route.model)
            result=self.calls[route.model]
            if dispatch and getattr(result,'pinned_model_route',False) is not True:
                raise ClosureContractError('FINDING_CLOSURE_EXACT_MODEL_ROUTE_REQUIRED')
            return result

    def __call__(self, prompt: str) -> str:
        ensure_provider_calls_allowed()
        selected=_SELECTED_ROUTE.get()
        route=selected or self.route(self.models[0])
        call=self.raw_call(route,dispatch=True)
        if selected is not None:
            return call(prompt)  # Admission belongs to this routed attempt.
        with _routed_admission(self.controller, route):
            return call(prompt)

    def drain(self) -> None:
        for executor in tuple(self.executors):
            executor.drain()

    def run_ruling(self, invocation: RulingInvocation, call: object,
                   retention: Callable[[str],bool]) -> tuple[str,bool,CanonicalCheckpoint] | None:
        """Each route uses the existing canonical checkpoint's singleflight lock."""
        from .completed_review_checkpoint import CompletedReviewCheckpoint
        from .structured_output import call_json
        # Do not route an unrelated low-effort adapter found in this scope.
        if RetainedObject.from_value(getattr(call,'cpa_cache_identity',{})).value()!=self.cpa_cache_identity:
            return None
        primary,alternate=(self.route(model) for model in self.models[:2])
        checkpoints: dict[str,CanonicalCheckpoint]={}
        root=getattr(call,'completed_review_root',None)
        def checkpoint(route: Route) -> CanonicalCheckpoint | None:
            if route.model in checkpoints:
                return checkpoints[route.model]
            raw_call=self.raw_call(route)
            args={'stage':invocation.stage,'prompt':invocation.prompt,'llm_call':raw_call,
                'inputs':invocation.inputs.value(),'parser_path':invocation.parser_path,'root':root}
            value=_legacy(CompletedReviewCheckpoint.for_call,**args)
            if value is None:
                value=_legacy(CompletedReviewCheckpoint.for_ruling,**args)
            if value is None:
                return None
            cp=cast(CanonicalCheckpoint,value)
            checkpoints[route.model]=cp
            return cp
        first=checkpoint(primary)
        if first is None:
            return None  # Preserve the owner's declined/unbound retention lane.
        second=checkpoint(alternate)
        if second is None:
            raise ClosureContractError('FINDING_CLOSURE_ALTERNATE_CHECKPOINT_REQUIRED')
        # Route-independent durable selection is telemetry, never a verdict.
        identity=RetainedObject.from_value({'stage':invocation.stage,
            'primary':str(first.directory),'alternate':str(second.directory)})
        directory=first.root/'finding-closure-routing'/identity.sha256
        selection=directory/'winner.json'
        if selection.exists():
            chosen=RetainedObject(selection.read_text().removesuffix('\n')).value()
            if (chosen.get('schema_version')!='routed-attempt.v1'
                    or chosen.get('status')!='REPLIED' or not chosen.get('answering_model')):
                raise ClosureContractError('FINDING_CLOSURE_PRIOR_TRANSPORT_RECOVERY_REQUIRED')
            route=self.route(_text(chosen['answering_model']))
            cp=checkpoint(route)
            answer=cp.load() if cp else None
            if answer is None or core.sha(answer)!=chosen['raw_sha256']:
                raise ClosureContractError('FINDING_CLOSURE_ROUTED_RAW_MISSING')
            if cp is None:
                raise ClosureContractError('FINDING_CLOSURE_ROUTED_RAW_MISSING')
            return answer,True,cp
        cached=[(route,cp.load()) for route,cp in ((primary,first),(alternate,second))]
        present=[(route,raw) for route,raw in cached if raw is not None]
        if len(present)>1:
            raise ClosureContractError('FINDING_CLOSURE_ROUTED_SELECTION_MISSING')
        if present:
            # No dispatch if either canonical route already has the raw answer.
            return present[0][1],True,checkpoints[present[0][0].model]
        ensure_provider_calls_allowed()
        # A requested top model is currently expanded by the canonical bridge
        # into a fallback chain. Do not silently dispatch two routes that can
        # both reach the same actual model/key. Existing raw hits above remain
        # readable; a missing transport pin is an engineering dependency.
        self.raw_call(primary,dispatch=True)
        self.raw_call(alternate,dispatch=True)
        directory.mkdir(parents=True,exist_ok=True)
        lock=Condition()
        timing=first.root/'finding-closure-routing'/'time-spans.jsonl'
        def record(receipt: AttemptReceipt, *, stage: str = invocation.stage) -> None:
            from .review_evidence_store import persist_document
            if receipt.raw_completion is not None and not retention(receipt.raw_completion):
                capture=_RULING_CAPTURE.get()
                if capture is not None:
                    capture.retention_declined=True
                raise ClosureContractError('FINDING_CLOSURE_RETENTION_DECLINED')
            document=RetainedObject.from_value({**asdict(receipt),'requested_model':receipt.route.model})
            _legacy(persist_document,directory/(document.sha256+'.json'),document.read_view())
            with lock:
                install_first_document(selection,document)
                span=RetainedObject.from_value({'schema_version':'run-time-span.v1',
                    'span_id':receipt.effect_key,'kind':'wait','name':'ruling.'+stage,
                    'started_utc':receipt.started_utc,'ended_utc':receipt.ended_utc,
                    'wait_reason':'CANONICAL_PROVIDER_OWNER','status':receipt.status,
                    'model':receipt.answering_model,'stage':stage,
                    'cache_hit':receipt.cache_hit})
                with timing.open('ab') as stream:
                    stream.write((span.json_text+'\n').encode());stream.flush();os.fsync(stream.fileno())
        executor=HedgedExecutor(controller=self.controller,record=record,retention=retention)
        # p95 comes from retained run-time-span.v1 pairs, not audio duration.
        if timing.exists():
            from .run_time_ledger import timestamp
            for line in timing.read_text().splitlines():
                span=RetainedObject.from_value(json.loads(line)).value()
                start,end=timestamp(span.get('started_utc')),timestamp(span.get('ended_utc'))
                if (span.get('schema_version')=='run-time-span.v1' and span.get('status')=='REPLIED'
                        and span.get('cache_hit') is False and isinstance(span.get('model'),str)
                        and isinstance(span.get('stage'),str) and start is not None and end is not None and end>=start):
                    key=_text(span['model']),_text(span['stage'])
                    executor.samples[key]=(*executor.samples.get(key,()),end-start)
        with self.condition:
            self.executors.append(executor)
        def send(route: Route) -> str:
            cp=checkpoint(route)
            if cp is None:
                raise ClosureContractError('FINDING_CLOSURE_CANONICAL_CHECKPOINT_REQUIRED')
            def owned(prompt: str) -> str:
                ensure_provider_calls_allowed()
                token=_SELECTED_ROUTE.set(route)
                try:
                    effective=call
                    recorder=getattr(call,'_semantic_attempt_recorder',None)
                    wrap=getattr(recorder,'wrap',None)
                    if callable(wrap):
                        # The ordinary recorder captures model identity in its
                        # closure. Bind each route through that same owner so
                        # two attempts cannot overwrite one primary-model row.
                        effective=_legacy(wrap,self.raw_call(route,dispatch=True))
                    return cast(str,_legacy(call_json,effective,prompt,response_schema=invocation.response_schema,
                                            name=invocation.name,reuse=False))
                finally:
                    _SELECTED_ROUTE.reset(token)
            return cp.response(owned,preserve_raw=True)
        def effect(route: Route) -> str:
            from .completed_review_checkpoint import _request_material_binding
            cp=checkpoint(route)
            if cp is None:
                raise ClosureContractError('FINDING_CLOSURE_CANONICAL_CHECKPOINT_REQUIRED')
            view = cp._binding_view() if hasattr(cp, '_binding_view') else cp.binding
            return RetainedObject.from_value(_legacy(_request_material_binding,view,invocation.prompt)).sha256
        material=_input_materials(invocation.inputs) or invocation.inputs
        answer=executor.invoke(materials=material,channel=invocation.stage,primary=primary,
            alternate=alternate,call=send,key_for_route=effect)
        # The durable first observation is authoritative for routing even if
        # another thread was scheduled first while Futures were being read.
        chosen=RetainedObject(selection.read_text().removesuffix('\n')).value()
        if chosen.get('status')!='REPLIED':
            raise ClosureContractError('FINDING_CLOSURE_PRIOR_TRANSPORT_RECOVERY_REQUIRED')
        selected_model=chosen.get('answering_model')
        if isinstance(selected_model,str) and selected_model:
            chosen_cp=checkpoint(self.route(selected_model))
            chosen_raw=chosen_cp.load() if chosen_cp else None
            if chosen_raw is None or core.sha(chosen_raw)!=chosen['raw_sha256']:
                raise ClosureContractError('FINDING_CLOSURE_ROUTED_RAW_MISSING')
            # Preserve live-vs-cache provenance: this was a real completed
            # attempt, even though its canonical bytes are read once here.
            from .llm_client import CpaCompletion
            answer=cast(str,_legacy(CpaCompletion,str(chosen_raw),selected_model))
        model=getattr(answer,'answering_model',None)
        requested=_text(chosen['requested_model'])
        selected_cp=checkpoints.get(model,checkpoints[requested]) if isinstance(model,str) else checkpoints[requested]
        return answer,bool(getattr(answer,'ruling_cache_hit',False)),selected_cp


def bind_ruling_inputs(inputs: RetainedObject, finding: PreparedFinding | None) -> RetainedObject:
    from .finding_context import materials_for_origin
    value=inputs.read_view()
    origin=(finding.origin_check or finding.check).read_view() if finding is not None else None
    def anchored(row: dict[str,JsonValue]) -> dict[str,JsonValue]:
        row=dict(row)
        if origin is not None and isinstance(row.get('finding_materials'),dict):
            row['finding_materials']=RetainedObject.from_value(_legacy(
                materials_for_origin,row['finding_materials'],origin,_read_only=True)).read_view()
        return row
    value=anchored(value)
    for key in ('material','request'):
        if isinstance(value.get(key),dict):value[key]=anchored(_object(value[key]))
    for key in ('material','request'):
        nested=value.get(key)
        if isinstance(nested,dict) and isinstance(nested.get('finding_materials'),dict):
            value['finding_materials']=nested['finding_materials']
    return RetainedObject.from_value(value)


def note_checkpoint_ruling(*, invocation: RulingInvocation, raw: str, identity: object,
                           retention_allowed: bool, checkpoint: object | None) -> ConsumedRuling | None:
    capture=_RULING_CAPTURE.get()
    if _input_check(invocation.inputs) is None or _input_materials(invocation.inputs) is None:
        return None
    if not retention_allowed or checkpoint is None:
        if capture is not None:capture.retention_declined=True
        return None
    from .completed_review_checkpoint import _request_material_binding
    from .qixi_transaction_core import stable_regular_snapshot
    from .llm_client import completion_identity, cached_completion
    cp=cast(CanonicalCheckpoint,checkpoint)
    unknown=cp.root/'finding-closure-model-provenance'/(core.sha(str(cp.directory)+':'+core.sha(raw))+'.json')
    if unknown.exists():
        if capture is None or capture.finding is None:return None
        raise ClosureContractError('FINDING_CLOSURE_ANSWER_MODEL_UNKNOWN')
    if not isinstance(getattr(raw,'answering_model',None),str) or not getattr(raw,'answering_model',None):
        _atomic(unknown,(RetainedObject.from_value({'status':'UNKNOWN',
            'response_sha256':core.sha(raw),'checkpoint_directory':str(cp.directory)}).json_text+'\n').encode())
        if capture is None or capture.finding is None:return None
        raise ClosureContractError('FINDING_CLOSURE_ANSWER_MODEL_UNKNOWN')
    actual_identity=RetainedObject.from_value(_legacy(completion_identity,identity,raw)).value()
    actual=cp.response_checkpoint(raw)
    binding=actual._binding_view() if hasattr(actual,'_binding_view') else RetainedObject.from_value(actual.binding).read_view()
    loaded=actual.load()
    if loaded is None or core.sha(loaded)!=core.sha(raw):
        raise ClosureContractError('FINDING_CLOSURE_CONSUMED_CHECKPOINT_MISSING')
    originals: dict[str,RetainedObject]={}
    for path in sorted([*actual.directory.glob('*.json'),*actual.directory.glob('.*.tmp')]):
        snapshot=_legacy(stable_regular_snapshot,path,label='consumed original ruling')
        payload=getattr(snapshot,'payload',None)
        if not isinstance(payload,bytes):continue
        from .review_evidence_store import expand, evidence_store_root
        decoded=_legacy(expand,json.loads(payload), store_root=_legacy(evidence_store_root,actual.directory), _shared=True)
        doc=RetainedObject.from_value(decoded)
        original=doc.read_view()
        if original.get('response_sha256')==core.sha(raw):originals[doc.sha256]=doc
    if len(originals)!=1:
        raise ClosureContractError('FINDING_CLOSURE_CONSUMED_CHECKPOINT_AMBIGUOUS')
    original=next(iter(originals.values())).read_view()
    saved_binding=_object(original['binding'])
    original_prompt=_text(original['request'])
    if (RetainedObject.from_value(_legacy(_request_material_binding,saved_binding,original_prompt))
            != RetainedObject.from_value(_legacy(_request_material_binding,binding,invocation.prompt))):
        raise ClosureContractError('FINDING_CLOSURE_CONSUMED_CHECKPOINT_DRIFT')
    original_invocation=replace(invocation,prompt=original_prompt,
                                inputs=RetainedObject.from_value(saved_binding['inputs']))
    origin = ((capture.finding.origin_check or capture.finding.check)
              if capture is not None and capture.finding is not None else _input_check(original_invocation.inputs))
    if origin is None:
        raise ClosureContractError('FINDING_CLOSURE_OWNER_TYPED_MATERIALS_REQUIRED')
    record=consumed_ruling(invocation=original_invocation,
        raw=cast(str,_legacy(cached_completion,str(raw),actual_identity)),identity=actual_identity,
        origin=origin,checkpoint=RetainedObject.from_value({'root':str(actual.root),'original':original}))
    if capture is not None and capture.finding is not None:capture.records.append(record)
    return record


def install_first_document(path: Path, document: RetainedObject) -> None:
    """Create-only observation pointer using the existing inode primitive."""
    from .qixi_transaction_core import create_staged_inode
    import uuid
    temporary=path.parent/('.'+document.sha256+'.'+uuid.uuid4().hex+'.tmp')
    _legacy(create_staged_inode,temporary,payload=(document.json_text+'\n').encode(),
            mode=0o600,label='routed first observation')
    try:
        try:os.link(temporary,path)
        except FileExistsError:pass
        descriptor=os.open(path.parent,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    finally:
        temporary.unlink()


_PROVIDER_CALLS_ALLOWED: ContextVar[bool] = ContextVar('closure_provider_calls_allowed',default=True)


def ensure_provider_calls_allowed() -> None:
    if not _PROVIDER_CALLS_ALLOWED.get():
        raise ClosureContractError('FINDING_CLOSURE_COLD_REPLAY_FORBIDS_CALL')


