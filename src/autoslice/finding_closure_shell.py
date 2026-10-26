"""Durable effects, typed ports and cold validation around finding_closure.

The event journal is the commit point. SRT and state.json are projections that
can be regenerated after a process kill. Replay never invokes a call port.
"""
from __future__ import annotations

from concurrent.futures import Future, wait, FIRST_COMPLETED
from contextlib import AbstractContextManager, contextmanager
from contextvars import Context, ContextVar, copy_context
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
from .provider_admission import (
    PROVIDER_CONTROLLER as PROVIDER_CONTROLLER, ProviderController as ProviderController,
    ProviderWindow as ProviderWindow, RateFeedback as RateFeedback,
    observed_rate_feedback as observed_rate_feedback, provider_admission as provider_admission,
    rate_feedback as _feedback,
)
from .finding_closure import (ClosureContractError, ClosureState, Confirmed, CueVersion, Event, FindingKey,
    Patch, PatchCompleted, ReviewCompleted, ScanCompleted, Start, State, TaskSpec,
    Work, WorkFailed, WorkKind, RevalidateRequested, PrefetchCompleted, TextReady, InputsAdvanced)
from .finding_closure_ports import (ArtifactSet, ClosurePorts, ConfirmationRequest,
    ConfirmationResult, DiscoveryInputs, DiscoveryPorts, JsonValue, Ledger, PatchRequest, PatchResult,
    PrefetchRequest, PrefetchResult, PreparedFinding, RebaseRequest, RetainedObject,
    ReviewRequest, ReviewResult, ScanRequest, ScanResult, ValidationRequest, ConsumedRuling, ProjectionBasis,
    InputUpdateResult)


def _object(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ClosureContractError("FINDING_CLOSURE_WIRE_OBJECT_REQUIRED")
    return value


def _text(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise ClosureContractError("FINDING_CLOSURE_WIRE_STRING_REQUIRED")
    return value


def _integer(value: JsonValue) -> int:
    if type(value) is not int:
        raise ClosureContractError("FINDING_CLOSURE_WIRE_INTEGER_REQUIRED")
    return value


def _array(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        raise ClosureContractError("FINDING_CLOSURE_WIRE_ARRAY_REQUIRED")
    return value


def _state(value: JsonValue) -> State:
    try:
        return State(_text(value))
    except ValueError as exc:
        raise ClosureContractError('FINDING_LIFECYCLE_UNKNOWN_STATE') from exc


def failure_types() -> tuple[tuple[type[Exception], ...], tuple[type[Exception], ...]]:
    from .finding_lifecycle import LifecycleError
    from .final_review_contract import FinalReviewContractError
    from .completed_review_checkpoint import CompletedReviewCheckpointError
    from .llm_client import LlmCallError
    contract: tuple[type[Exception], ...] = (ClosureContractError,LifecycleError,FinalReviewContractError,CompletedReviewCheckpointError)
    provider: tuple[type[Exception], ...] = (LlmCallError,)
    # The taxonomy owner's module is not in the AB baseline. Consume its
    # declared types when merged; do not manufacture a second taxonomy or
    # classify arbitrary ValueError/TypeError/OSError by text or attributes.
    from importlib import import_module
    try:
        taxonomy = import_module('src.autoslice.review_errors')
    except ModuleNotFoundError as exc:
        if exc.name != 'src.autoslice.review_errors':
            raise
    else:
        ctype, ptype = getattr(taxonomy,'ContractRejection',None),getattr(taxonomy,'ProviderFailure',None)
        if not isinstance(ctype,type) or not issubclass(ctype,Exception) or not isinstance(ptype,type) or not issubclass(ptype,Exception):
            raise ClosureContractError('FINDING_CLOSURE_ERROR_TYPES_INVALID')
        contract,provider = (*contract,ctype),(*provider,ptype)
    return contract,provider


def _fields(value: JsonValue, names: tuple[str, ...]) -> dict[str, JsonValue]:
    obj = _object(value)
    if set(obj) != set(names):
        raise ClosureContractError("FINDING_CLOSURE_WIRE_FIELDS_INVALID")
    return obj


def _key(value: JsonValue) -> FindingKey:
    obj = _fields(value, ("physical", "materials"))
    return FindingKey(_text(obj["physical"]), _text(obj["materials"]))


def _spec(value: JsonValue) -> TaskSpec:
    obj = _object(value)
    if 'presence' not in obj and 'projection_ref' not in obj:
        obj={**obj,'presence':'PRESENT','projection_ref':''}
    obj.setdefault('applied_ruling_ref','')
    obj = _fields(obj, ("key", "scope_cues", "local_text_sha256", "finding_ref", "check_ref", "materials_ref", "origin_check_ref",'presence','projection_ref','applied_ruling_ref'))
    presence=_text(obj['presence'])
    if presence not in {'PRESENT','ABSENT'}:
        raise ClosureContractError('FINDING_CLOSURE_PRESENCE_INVALID')
    return TaskSpec(_key(obj["key"]), tuple(_integer(i) for i in _array(obj["scope_cues"])),
        _text(obj["local_text_sha256"]), _text(obj["finding_ref"]),
        _text(obj["check_ref"]), _text(obj["materials_ref"]), _text(obj['origin_check_ref']),
        cast(Literal['PRESENT','ABSENT'],presence),_text(obj['projection_ref']),_text(obj['applied_ruling_ref']))


def _patch(value: JsonValue) -> Patch:
    obj = _fields(value, ("work_id", "key", "ruling_ref", "before_sha256", "after_sha256",
                           "start", "before", "after", "repair_ref"))
    return Patch(_text(obj["work_id"]), _key(obj["key"]), _text(obj["ruling_ref"]),
        _text(obj["before_sha256"]), _text(obj["after_sha256"]), _integer(obj["start"]),
        _text(obj["before"]), _text(obj["after"]), _text(obj["repair_ref"]))


def decode_event(document: RetainedObject) -> Event:
    obj = document.value()
    kind = obj.get("kind")
    if kind == "start":
        _fields(obj, ("kind",))
        return Start()
    if kind == 'inputs_advanced':
        _fields(obj,('work_id','receipt_ref','findings','kind'))
        return InputsAdvanced(_text(obj['work_id']),_text(obj['receipt_ref']),
                              tuple(_spec(v) for v in _array(obj['findings'])))
    if kind == "scan_completed":
        _fields(obj, ("work_id", "findings", "coverage", "lane_refs", "rebased", "kind"))
        coverage = []
        for value in _array(obj["coverage"]):
            row = _fields(value, ("cue", "text_sha256"))
            coverage.append(CueVersion(_integer(row["cue"]), _text(row["text_sha256"])))
        return ScanCompleted(_text(obj["work_id"]), tuple(_spec(v) for v in _array(obj["findings"])),
            tuple(coverage), tuple(_text(v) for v in _array(obj["lane_refs"])),
            tuple(_spec(v) for v in _array(obj["rebased"])))
    if kind == "review_completed":
        _fields(obj, ("work_id", "spec", "state", "receipt_ref", "kind"))
        return ReviewCompleted(_text(obj["work_id"]), _spec(obj["spec"]),
                               _state(obj["state"]), _text(obj["receipt_ref"]))
    if kind == "patch_completed":
        _fields(obj, ("work_id", "patch", "findings", "kind"))
        return PatchCompleted(_text(obj["work_id"]), _patch(obj["patch"]),
                               tuple(_spec(v) for v in _array(obj["findings"])))
    if kind == "confirmed":
        _fields(obj, ("work_id", "output_sha256", "receipt_ref", "kind"))
        return Confirmed(_text(obj["work_id"]), _text(obj["output_sha256"]), _text(obj["receipt_ref"]))
    if kind == "text_ready":
        _fields(obj, ("work_id", "output_sha256", "receipt_ref", "kind"))
        return TextReady(_text(obj["work_id"]), _text(obj["output_sha256"]), _text(obj["receipt_ref"]))
    if kind == "work_failed":
        _fields(obj, ("work_id", "receipt_ref", "state", "kind"))
        return WorkFailed(_text(obj["work_id"]), _text(obj["receipt_ref"]), _state(obj["state"]))
    if kind == 'revalidate_requested':
        _fields(obj, ('work_id','physical_id','consumer_revision_ref','kind'))
        return RevalidateRequested(_text(obj['work_id']), _text(obj['physical_id']),
                                   _text(obj['consumer_revision_ref']))
    if kind == 'prefetch_completed':
        _fields(obj,('work_id','receipt_refs','kind'))
        return PrefetchCompleted(_text(obj['work_id']),tuple(_text(v) for v in _array(obj['receipt_refs'])))
    raise ClosureContractError("FINDING_CLOSURE_EVENT_UNKNOWN")


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


class FileLedger:
    """A single-writer, content-addressed journal; snapshots confer no authority."""

    def __init__(self, root: Path, initial: ClosureState, *, create: bool = True) -> None:
        self.root = root
        if create:
            root.mkdir(parents=True, exist_ok=True)
        wire = core.state_bytes(initial)
        path = root / "initial.json"
        if path.exists():
            if path.read_bytes() != wire:
                raise ClosureContractError("FINDING_CLOSURE_INITIAL_IDENTITY_DRIFT")
        else:
            if not create:
                raise ClosureContractError("FINDING_CLOSURE_INITIAL_MISSING")
            _atomic(path, wire)

    def put(self, document: RetainedObject) -> str:
        from .review_evidence_store import persist_document
        digest = document.sha256
        path = self.root / "objects" / (digest + ".json")
        if path.exists():
            if self.get(digest) != document:
                raise ClosureContractError("FINDING_CLOSURE_ARTIFACT_HASH_DRIFT")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Use WP-A's one writer, including whole SRT and nested originals.
            cast(Callable[[Path, object], object], persist_document)(path, document.value())
        return digest

    def get(self, digest: str) -> RetainedObject:
        from .review_evidence_store import load_review
        if not core.valid_sha(digest):
            raise ClosureContractError("FINDING_CLOSURE_ARTIFACT_REFERENCE_INVALID")
        path = self.root / "objects" / (digest + ".json")
        document = RetainedObject.from_value(cast(Callable[[Path], object], load_review)(path))
        if document.sha256 != digest:
            raise ClosureContractError("FINDING_CLOSURE_ARTIFACT_HASH_DRIFT")
        return document

    def events(self) -> tuple[RetainedObject, ...]:
        paths = sorted((self.root / "events").glob("*.json"))
        result = []
        previous = ""
        for index, path in enumerate(paths, 1):
            if path.name != f"{index:08d}.json":
                raise ClosureContractError("FINDING_CLOSURE_EVENT_SEQUENCE_DRIFT")
            doc = RetainedObject(path.read_text(encoding="utf-8").removesuffix("\n"))
            obj = _fields(doc.value(), ("sequence", "previous", "before", "after", "event"))
            if obj["sequence"] != index or obj["previous"] != previous:
                raise ClosureContractError("FINDING_CLOSURE_EVENT_CHAIN_DRIFT")
            previous = doc.sha256
            result.append(doc)
        return tuple(result)

    def append(self, event: Event, before: ClosureState, after: ClosureState) -> None:
        existing = self.events()
        doc = RetainedObject.from_value({"sequence": len(existing) + 1,
            "previous": existing[-1].sha256 if existing else "",
            "before": core.sha(core.state_bytes(before).decode()),
            "after": core.sha(core.state_bytes(after).decode()), "event": asdict(event)})
        path = self.root / "events" / f"{len(existing) + 1:08d}.json"
        if path.exists():
            raise ClosureContractError("FINDING_CLOSURE_CONCURRENT_WRITER")
        _atomic(path, (doc.json_text + "\n").encode())

    def project(self, state: ClosureState, output: Path | None = None) -> None:
        _atomic(self.root / "state.json", core.state_bytes(state))
        if output is not None:
            _atomic(output, core.replay_patches(state).encode("utf-8"))

    def check_snapshot(self, valid_hashes: set[str]) -> None:
        snapshot = self.root / "state.json"
        if snapshot.exists() and core.sha(snapshot.read_text(encoding="utf-8")) not in valid_hashes:
            raise ClosureContractError("FINDING_CLOSURE_SNAPSHOT_DRIFT")

    def record_span(self, span: TimingSpan) -> None:
        document = RetainedObject.from_value(asdict(span))
        with (self.root/'time-spans.jsonl').open('ab') as stream:
            stream.write((document.json_text+'\n').encode())
            stream.flush()
            os.fsync(stream.fileno())

    def summarize(self, initial: ClosureState) -> None:
        from .run_time_ledger import timestamp, collect_sources, build_ledger
        spans = self.root/'time-spans.jsonl'
        if not spans.exists():
            return
        weights: dict[str, float] = {}
        for line in spans.read_text(encoding='utf-8').splitlines():
            row = RetainedObject(line).value()
            begin, end = timestamp(row.get('started_utc')), timestamp(row.get('ended_utc'))
            if begin is not None and end is not None:
                weights[_text(row['span_id'])] = max(0.0,end-begin)
        path: dict[str, float] = {}
        state = initial
        for envelope in self.events():
            event = decode_event(RetainedObject.from_value(envelope.value()['event']))
            if not isinstance(event, Start):
                work = control_work(state,event) if isinstance(event,(RevalidateRequested,InputsAdvanced)) else next(
                    w for w in state.pending if w.work_id == event.work_id)
                path[work.work_id] = weights.get(work.work_id,0.0) + max(
                    (path.get(dep,0.0) for dep in work.dependencies), default=0.0)
            state,_ = core.step(state,event)
        sources = _legacy(collect_sources,self.root)
        result = RetainedObject.from_value(_legacy(build_ledger,sources)).value()
        result['workflow_critical_path_s'] = max(path.values(),default=0.0)
        result['provider_call_source_scope'] = 'Original exchange/native ledgers remain with their owners; these spans are workflow stages.'
        _atomic(self.root/'timing-summary.json',(RetainedObject.from_value(result).json_text+'\n').encode())


def _prepared(ledger: Ledger, spec: TaskSpec) -> PreparedFinding:
    return prepared_from_refs(spec,ledger.get)


def prepared_from_refs(spec: TaskSpec, get: Callable[[str],RetainedObject]) -> PreparedFinding:
    return PreparedFinding(spec,get(spec.finding_ref),get(spec.check_ref),get(spec.materials_ref),
                           get(spec.origin_check_ref) if spec.origin_check_ref else None,
                           get(spec.projection_ref) if spec.projection_ref else None)


def finding_documents(finding: PreparedFinding) -> tuple[RetainedObject, ...]:
    values: tuple[RetainedObject,...] = (finding.finding,finding.check,finding.materials)
    if finding.origin_check is not None:
        values=(*values,finding.origin_check)
    return (*values,finding.projection) if finding.projection is not None else values


def _put_finding(ledger: Ledger, finding: PreparedFinding) -> None:
    for document in finding_documents(finding):
        ledger.put(document)


def event_artifacts(ledger: Ledger, event: Event, work: Work,
                    state: ClosureState | None = None) -> ArtifactSet:
    refs: set[str] = set()
    specs: list[TaskSpec] = []
    if work.discovery_ref:
        refs.add(work.discovery_ref)
    if work.task is not None:
        specs.append(work.task)
    if isinstance(event, InputsAdvanced):
        specs.extend(event.findings)
        refs.add(event.receipt_ref)
    elif isinstance(event, ScanCompleted):
        specs.extend((*event.findings, *event.rebased))
        refs.update(event.lane_refs)
        if event.rebased and state is not None:
            refs.update(ref for patch in state.patches for ref in (patch.repair_ref,patch.ruling_ref))
    elif isinstance(event, ReviewCompleted):
        specs.append(event.spec)
        refs.add(event.receipt_ref)
    elif isinstance(event, PatchCompleted):
        specs.extend(event.findings)
        if state is not None:
            specs.extend(t.spec for t in state.tasks)
        refs.update((event.patch.repair_ref, event.patch.ruling_ref))
    elif isinstance(event, (Confirmed, TextReady, WorkFailed)):
        refs.add(event.receipt_ref)
    elif isinstance(event, RevalidateRequested):
        refs.add(event.consumer_revision_ref)
    elif isinstance(event, PrefetchCompleted):
        refs.update(event.receipt_refs)
    for spec in specs:
        refs.update((spec.finding_ref, spec.check_ref, spec.materials_ref))
        if spec.origin_check_ref:
            refs.add(spec.origin_check_ref)
        if spec.projection_ref:
            refs.add(spec.projection_ref)
        if spec.applied_ruling_ref:
            refs.add(spec.applied_ruling_ref)
    return ArtifactSet(tuple(ledger.get(ref) for ref in sorted(refs)))


def control_work(state: ClosureState, event: RevalidateRequested | InputsAdvanced) -> Work:
    if isinstance(event,RevalidateRequested):
        return core.revalidation_work(state,event)
    return Work(event.work_id,'input_update',core.sha(state.current_srt),discovery_ref=state.discovery_ref)


def replay_journal(initial: ClosureState, ledger: Ledger, ports: ClosurePorts) -> ClosureState:
    state = initial
    valid_snapshots = {core.sha(core.state_bytes(state).decode())}
    for envelope in ledger.events():
        obj = envelope.value()
        if obj["before"] != core.sha(core.state_bytes(state).decode()):
            raise ClosureContractError("FINDING_CLOSURE_EVENT_BEFORE_DRIFT")
        event = decode_event(RetainedObject.from_value(obj["event"]))
        if not isinstance(event, Start):
            work = control_work(state,event) if isinstance(event,(RevalidateRequested,InputsAdvanced)) else next(
                (w for w in state.pending if w.work_id == event.work_id), None)
            if work is None:
                raise ClosureContractError("FINDING_CLOSURE_EVENT_WITHOUT_PENDING_WORK")
            ports.revalidate(ValidationRequest(state, work, event, event_artifacts(ledger, event, work, state)))
        state, _ = core.step(state, event)
        if obj["after"] != core.sha(core.state_bytes(state).decode()):
            raise ClosureContractError("FINDING_CLOSURE_EVENT_AFTER_DRIFT")
        valid_snapshots.add(core.sha(core.state_bytes(state).decode()))
    if isinstance(ledger, FileLedger):
        ledger.check_snapshot(valid_snapshots)
    return state


@dataclass(frozen=True)
class TimingSpan:
    # Exact task_span wire fields owned by run_time_ledger.py. These are
    # workflow stages/waits, not duplicate provider-call intervals.
    span_id: str
    kind: Literal['stage', 'wait']
    name: str
    started_utc: str
    ended_utc: str
    wait_reason: str
    schema_version: Literal['run-time-span.v1'] = 'run-time-span.v1'


@dataclass(frozen=True)
class EffectResult:
    event: Event
    artifacts: tuple[RetainedObject, ...]
    span: TimingSpan | None = None


class ClosureRunner:
    def __init__(self, *, initial: ClosureState, ledger: FileLedger, ports: ClosurePorts,
                 output: Path | None = None, on_projection: Callable[[ClosureState], None] | None = None,
                 text_recovery_only: bool = False,
                 advance_inputs: Callable[[ClosureState],InputUpdateResult | None] | None = None) -> None:
        self.initial, self.ledger, self.ports = initial, ledger, ports
        self.output, self.on_projection = output, on_projection
        self.text_recovery_only = text_recovery_only
        self.advance_inputs = advance_inputs

    def _timed_effect(self, state: ClosureState, work: Work) -> EffectResult:
        from .producer_stage_log import emit
        from .run_time_ledger import utc
        started, wall = time.monotonic(), time.time()
        name = 'finding_closure.' + work.kind
        emit('START', name, task=(work.task.key.physical[:12] if work.task else 'clip'), wait_reason='OWNER_IO')
        contract,provider = failure_types()
        try:
            result = self._effect(state, work)
        except (*contract,*provider) as exc:
            selected = State.CALL_FAILURE if isinstance(exc,provider) else State.CONTRACT_ERROR
            reason = str(getattr(exc,'reason_code',None) or getattr(exc,'safe_reason',None) or type(exc).__name__)
            receipt = RetainedObject.from_value({'work_id': work.work_id,
                'error_type': type(exc).__name__, 'reason_code': reason,
                'state': selected, 'evidence_owner': 'ORIGINAL_OWNER', 'new_response_claimed': False})
            result = EffectResult(WorkFailed(work.work_id, receipt.sha256, selected), (receipt,))
        elapsed = time.monotonic()-started
        emit('END', name, elapsed_s=elapsed, wait_reason='COMPLETED' if not isinstance(result.event,WorkFailed) else 'OWNER_RECOVERY')
        return EffectResult(result.event, result.artifacts,
            TimingSpan(work.work_id, 'stage', name, utc(wall), utc(wall+elapsed),
                       'OWNER_RECOVERY' if isinstance(result.event,WorkFailed) else 'COMPLETED'))

    def _effect(self, state: ClosureState, work: Work) -> EffectResult:
        text = core.text_at(state, work.text_sha256)
        if work.kind == 'prefetch':
            primed = self.ports.prefetch(PrefetchRequest(work,state.source_sha256,text))
            return EffectResult(PrefetchCompleted(work.work_id,tuple(d.sha256 for d in primed.receipts)),
                                primed.receipts)
        if work.kind in {"initial_scan", "targeted_scan", "final_scan"}:
            request = ScanRequest(work, text)
            result: ScanResult
            if work.kind == "initial_scan":
                result = self.ports.first_scan(request)
            elif work.kind == "targeted_scan":
                result = self.ports.targeted_scan(request)
            else:
                result = self.ports.final_scan(request)
            objects = tuple(d for f in result.findings for d in finding_documents(f))
            return EffectResult(ScanCompleted(work.work_id, tuple(f.spec for f in result.findings),
                result.coverage, tuple(d.sha256 for d in result.lanes)), (*objects, *result.lanes))
        if work.task is not None:
            finding = _prepared(self.ledger, work.task)
            if work.kind == "patch":
                current = next(t for t in state.tasks if t.spec.key == work.task.key)
                patched = self.ports.patch(PatchRequest(work, state.current_srt, finding,
                    self.ledger.get(current.ruling_ref), tuple(_prepared(self.ledger, t.spec) for t in state.tasks)))
                objects = tuple(d for f in patched.findings for d in finding_documents(f))
                return EffectResult(PatchCompleted(work.work_id, patched.patch,
                    tuple(f.spec for f in patched.findings)), (*objects, patched.repair))
            review = ReviewRequest(work, text, finding)
            outcome: ReviewResult
            if work.kind == "evidence":
                outcome = self.ports.evidence(review)
            elif work.kind == "decision":
                outcome = self.ports.decide(review)
            elif work.kind == "task_stop":
                outcome = self.ports.terminal(review)
            elif work.kind == 'revalidate':
                outcome = self.ports.recover(review)
            else:
                raise ClosureContractError("FINDING_CLOSURE_WORK_KIND_INVALID")
            return EffectResult(ReviewCompleted(work.work_id, outcome.finding.spec, outcome.state,
                outcome.receipt.sha256), (*finding_documents(outcome.finding),outcome.receipt))
        if work.kind == "confirm":
            confirmed = self.ports.confirm(ConfirmationRequest(state, state.current_srt,
                tuple(_prepared(self.ledger, t.spec) for t in state.tasks)))
            event_type = Confirmed if confirmed.release_ready else TextReady
            return EffectResult(event_type(work.work_id, confirmed.output_sha256,
                confirmed.audit.sha256), (confirmed.audit,))
        raise ClosureContractError("FINDING_CLOSURE_WORK_KIND_INVALID")

    def _commit(self, state: ClosureState, result: EffectResult) -> ClosureState:
        for document in result.artifacts:
            self.ledger.put(document)
        event = result.event
        if isinstance(event, ScanCompleted) and any(w.work_id == event.work_id
                and w.text_sha256 != core.sha(state.current_srt) for w in state.pending):
            rebased = self.ports.rebase(RebaseRequest(state.current_srt,
                tuple(_prepared(self.ledger, s) for s in event.findings),
                tuple(ProjectionBasis(core.text_at(state,p.before_sha256),core.text_at(state,p.after_sha256),
                    self.ledger.get(p.repair_ref),self.ledger.get(p.ruling_ref)) for p in state.patches)))
            for finding in rebased:
                _put_finding(self.ledger, finding)
            event = ScanCompleted(event.work_id, event.findings, event.coverage, event.lane_refs,
                                  tuple(f.spec for f in rebased))
        if not isinstance(event, Start):
            work = control_work(state,event) if isinstance(event,(RevalidateRequested,InputsAdvanced)) else next(
                w for w in state.pending if w.work_id == event.work_id)
            self.ports.revalidate(ValidationRequest(state, work, event,
                                                    event_artifacts(self.ledger, event, work, state)))
        following, _ = core.step(state, event)
        self.ledger.append(event, state, following)
        if result.span is not None:
            self.ledger.record_span(result.span)
        if self.on_projection is not None:
            self.on_projection(following)
        self.ledger.project(following, self.output)
        return following

    def run(self) -> ClosureState:
        state = replay_journal(self.initial, self.ledger, self.ports)
        # A previous process can die after journal commit, before projection.
        if (state.confirmation_ref and self.output is not None and self.output.exists()
                and core.sha(self.output.read_bytes().decode('utf-8',errors='strict')) != state.confirmation_sha256):
            raise ClosureContractError("FINDING_CLOSURE_RELEASE_BYTES_MISMATCH")
        if self.output is not None and self.output.exists() and core.sha(self.output.read_bytes().decode()) not in {
                core.sha(state.initial_srt), *(p.after_sha256 for p in state.patches)}:
            raise ClosureContractError("FINDING_CLOSURE_ACTIVE_TEXT_DRIFT")
        if self.on_projection is not None:
            self.on_projection(state)
        self.ledger.project(state, self.output)
        if not state.started:
            state = self._commit(state, EffectResult(Start(), ()))
        if state.confirmation_ref and self.advance_inputs is not None:
            advanced=self.advance_inputs(state)
            if advanced is not None:
                event=InputsAdvanced(core.sha('discovery-update:'+advanced.receipt.sha256),
                    advanced.receipt.sha256,tuple(f.spec for f in advanced.findings))
                state=self._commit(state,EffectResult(event,(advanced.receipt,
                    *(d for f in advanced.findings for d in finding_documents(f)))))
        if any(t.state in {State.CONTRACT_ERROR,State.CALL_FAILURE} for t in state.tasks):
            revision = consumer_revision()
            self.ledger.put(revision)
            for task in state.tasks:
                if task.state not in {State.CONTRACT_ERROR,State.CALL_FAILURE}:
                    continue
                identity = core.revalidation_id(state, task, revision.sha256)
                if identity not in state.completed and not any(w.work_id == identity for w in state.pending):
                    state = self._commit(state, EffectResult(RevalidateRequested(identity,
                        task.spec.key.physical, revision.sha256), (revision,)))
        active: dict[Future[EffectResult], Work] = {}
        failed_operations: set[str] = set()
        threads: list[Thread] = []
        # No fixed global worker cap. Each actual provider's controller owns
        # admission; the number of independent questions may grow after scans.
        def submit(work: Work) -> Future[EffectResult]:
            future: Future[EffectResult] = Future()
            context = copy_context()
            captured = state
            def invoke() -> None:
                try:
                    future.set_result(context.run(self._timed_effect, captured, work))
                except BaseException as exc:
                    future.set_exception(exc)
            thread = Thread(target=invoke, name="finding-closure-" + work.work_id[:12])
            threads.append(thread)
            thread.start()
            return future
        def dispatch() -> None:
            for work in state.pending:
                if (work.kind not in {"patch", "confirm"} and work.work_id not in failed_operations
                        and work not in active.values()):
                    active[submit(work)] = work
        try:
            while state.pending and not (self.text_recovery_only and state.text_ready_ref):
                dispatch()
                local = next((w for w in state.pending if w.kind in {"patch", "confirm"}
                              and w.work_id not in failed_operations), None)
                if local is not None:
                    result = self._timed_effect(state, local)
                    state = self._commit(state, result)
                    if isinstance(result.event, TextReady) and not self.text_recovery_only:
                        raise ClosureContractError('FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED')
                    if isinstance(result.event, WorkFailed):
                        failed_operations.add(result.event.work_id)
                    dispatch()
                    continue
                if not active:
                    raise ClosureContractError('FINAL_REVIEW_ENGINEERING_FAILURE' if failed_operations else core.stop_reason(state))
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in sorted(done, key=lambda f: active[f].work_id):
                    finished_work = active.pop(future)
                    # Errors do not become content D or a clean scan. Existing
                    # call owners retain raw/UNKNOWN before this exception.
                    result = future.result()
                    state = self._commit(state, result)
                    if isinstance(result.event, WorkFailed) and core.work_is_current(state,finished_work):
                        failed_operations.add(result.event.work_id)
                    # Do not turn a batch of already-returned futures into a
                    # new phase barrier. Start this P's next work immediately.
                    dispatch()
                    patch = next((w for w in state.pending if w.kind == 'patch'
                                  and w.work_id not in failed_operations),None)
                    if patch is not None:
                        applied = self._timed_effect(state,patch)
                        state = self._commit(state,applied)
                        if isinstance(applied.event,WorkFailed):
                            failed_operations.add(applied.event.work_id)
                        dispatch()
        finally:
            for thread in threads:
                thread.join()
            if isinstance(self.ports,ProductionPorts):
                router=self.ports.owners.inputs.judge_llm_call
                if isinstance(router,FinalReviewCall):
                    import sys
                    original=sys.exception()
                    try:
                        router.drain()
                    except BaseException as late:
                        if original is None:raise
                        original.add_note('Late owner failure: '+type(late).__name__+': '+str(late))
            try:
                self.ledger.summarize(self.initial)
            except (ValueError,KeyError,OSError) as exc:
                _atomic(self.ledger.root/'timing-summary-error.json',
                    (RetainedObject.from_value({'error_type':type(exc).__name__}).json_text+'\n').encode())
        if not state.confirmation_ref and not (self.text_recovery_only and state.text_ready_ref):
            raise ClosureContractError(core.stop_reason(state))
        return state


def consumer_revision() -> RetainedObject:
    root = Path(__file__).parent
    names = ('finding_closure_shell.py','finding_lifecycle.py','final_review_contract.py',
             'semantic_terminal_adjudication.py','closed_set_proposal_rebuild.py','targeted_relisten.py',
             'finding_context.py','acoustic_witness_adjudication.py','editorial_subtitle.py',
             'completed_review_checkpoint.py','producer_final_review_transport.py')
    return RetainedObject.from_value({'consumer_modules': {name: core.sha((root/name).read_text(encoding='utf-8'))
                                                            for name in names}})


def validate_text_ready(audit: RetainedObject, output_sha256: str) -> None:
    """Reapply the old intermediate gate; this never authorizes release."""
    from .final_review_contract import FinalReviewContractError, validate_final_review_release
    obj = audit.value()
    expected = 'sha256:' + output_sha256
    if (obj.get('status') != 'TEXT_COMPLETE_BOUNDARY_PENDING' or obj.get('release_gate') != 'BLOCK'
            or obj.get('pre_boundary_text_recovery') != {
                'status': 'TEXT_COMPLETE_BOUNDARY_PENDING', 'reviewed_srt_sha256': expected,
                'release_gate': 'BLOCK'}
            or obj.get('reviewed_srt_sha256') != expected
            or 'finding_closure' in obj or 'final_review_mode' in obj):
        raise ClosureContractError('FINDING_CLOSURE_TEXT_READY_AUDIT_INVALID')
    try:
        _legacy(validate_final_review_release, obj, expected_srt_sha256=expected)
    except FinalReviewContractError as exc:
        if exc.reason_code == 'FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED':
            return
        raise
    raise ClosureContractError('FINDING_CLOSURE_TEXT_READY_REQUIRES_PENDING_BOUNDARY')


def _legacy(function: object, *args: object, **kwargs: object) -> object:
    """The sole call boundary to not-yet-typed existing owners."""
    return cast(Callable[..., object], function)(*args, **kwargs)


@dataclass(frozen=True)
class OwnerInputs:
    source_sha256: str
    timeline_offset_ms: int
    clip_context: RetainedObject
    contracts: RetainedObject
    model_identity: RetainedObject
    entity_verifier: object = None
    judge_llm_call: object = None
    screen_read_probe: object = None
    ranges: tuple[RetainedObject, ...] = ()
    secondary: tuple[RetainedObject, ...] = ()
    caption_audio_roles: RetainedObject | None = None
    protected_cues: frozenset[int] = frozenset()
    prefetch_providers: tuple[str, ...] = ()
    protected_windows: tuple[tuple[int,int],...] = ()
    candidate_id: str = ''

    def cold_document(self) -> RetainedObject:
        return RetainedObject.from_value({"source_sha256": self.source_sha256,
            "timeline_offset_ms": self.timeline_offset_ms, "clip_context": self.clip_context.value(),
            "contracts": self.contracts.value(), "model_identity": self.model_identity.value(),
            "ranges": [d.value() for d in self.ranges], "secondary": [d.value() for d in self.secondary],
            "caption_audio_roles": self.caption_audio_roles.value() if self.caption_audio_roles else None,
            "protected_cues": sorted(self.protected_cues), 'prefetch_providers': self.prefetch_providers,
            'protected_windows':self.protected_windows, 'candidate_id':self.candidate_id})


@dataclass(frozen=True)
class ExistingFindingOwners:
    inputs: OwnerInputs

    def _protected_indexes(self, text: str) -> frozenset[int]:
        if not self.inputs.protected_windows:
            if self.inputs.protected_cues:
                raise ClosureContractError('FINDING_CLOSURE_PROTECTED_WINDOWS_REQUIRED')
            return frozenset()
        from .jingting_chunker import parse_srt_cues
        cues=[c for c in parse_srt_cues(text) if c.text.strip()]
        indexes=frozenset(i for i,c in enumerate(cues,1)
                          if (c.start_ms,c.end_ms) in self.inputs.protected_windows)
        if len(indexes)!=len(self.inputs.protected_windows):
            raise ClosureContractError('HUMAN_LITERAL_PROTECTED_WINDOW_REMOVED')
        return indexes

    def _prefetch_geometry(self, text: str) -> tuple[RetainedObject, RetainedObject]:
        from importlib import import_module
        try:
            channels = import_module('src.autoslice.discovery_channels')
        except ModuleNotFoundError as exc:
            if exc.name != 'src.autoslice.discovery_channels':
                raise
            raise ClosureContractError('FINDING_CLOSURE_DISCOVERY_PORTS_UNAVAILABLE') from exc
        candidate = self.inputs.candidate_id or self.inputs.clip_context.value().get('candidate_id')
        if not isinstance(candidate,str) or not candidate:
            raise ClosureContractError('FINDING_CLOSURE_PREFETCH_CANDIDATE_REQUIRED')
        source = _legacy(channels.SourceIdentity,candidate,(self.inputs.source_sha256.removeprefix('sha256:'),))
        # One owner for scaffold, physical window and WIDE receipt identity.
        # Do not independently rebuild or rehash the two returned documents.
        result = _legacy(channels.fullclip_prefetch_geometry,text,source=source,
                         source_offset_ms=self.inputs.timeline_offset_ms)
        if not isinstance(result,tuple) or len(result)!=2:
            raise ClosureContractError('FINDING_CLOSURE_PREFETCH_GEOMETRY_INVALID')
        return RetainedObject.from_value(result[0]),RetainedObject.from_value(result[1])

    def prefetch(self, request: PrefetchRequest) -> PrefetchResult:
        observe = getattr(self.inputs.entity_verifier,'observe_range_context',None)
        budget = getattr(self.inputs.entity_verifier,'relisten_budget_state',None)
        if not callable(observe) or not callable(budget) or not self.inputs.prefetch_providers:
            raise ClosureContractError('FINDING_CLOSURE_PREFETCH_OWNER_REQUIRED')
        if request.source_sha256 != self.inputs.source_sha256:
            raise ClosureContractError('FINDING_CLOSURE_PREFETCH_SOURCE_DRIFT')
        check,wide = self._prefetch_geometry(request.srt_text)
        contract,provider = failure_types()
        def acquire(name: str) -> RetainedObject:
            before = RetainedObject.from_value(_legacy(budget))
            error: JsonValue = None
            observations: JsonValue = []
            try:
                observations = _array(cast(JsonValue,_legacy(observe,check_request=check.value(),
                    range_witness_request=wide.value(),provider=name)))
            except (*contract,*provider) as exc:
                error = {'error_type':type(exc).__name__,
                         'reason_code':str(getattr(exc,'reason_code',getattr(exc,'safe_reason',type(exc).__name__)))}
            after = RetainedObject.from_value(_legacy(budget))
            return RetainedObject.from_value({'source_sha256':request.source_sha256,'provider':name,
                'check_request':check.value(),'request':wide.value(),'observations':observations,
                'budget_before':before.value(),'budget_after':after.value(),'error':error,
                'authority':'EVIDENCE_ONLY','mutation_authorized':False})
        futures: dict[str,Future[RetainedObject]] = {}
        threads=[]
        for name in self.inputs.prefetch_providers:
            future: Future[RetainedObject] = Future()
            context = copy_context()
            def run_provider(provider_name: str = name,
                             output: Future[RetainedObject] = future,
                             run: Context = context) -> None:
                try:
                    output.set_result(run.run(acquire,provider_name))
                except BaseException as exc:
                    output.set_exception(exc)
            thread=Thread(target=run_provider,name='finding-prefetch-'+name)
            threads.append(thread);futures[name]=future;thread.start()
        try:
            return PrefetchResult(tuple(futures[name].result() for name in sorted(futures)))
        finally:
            for thread in threads:thread.join()

    def prepare_finding(self, text: str, original: RetainedObject,
                        *, origin_check: RetainedObject | None = None) -> PreparedFinding:
        from .exact_final_terminal_funnel import terminal_check
        from . import finding_context as context
        from .targeted_relisten import evidence_projection
        row = original.value()
        variants=tuple(_object(v) for v in _array(row.get('discovery_candidates',[])))
        check = RetainedObject.from_value(_legacy(terminal_check, text, row,
            clip_context=self.inputs.clip_context.value(), offset_ms=self.inputs.timeline_offset_ms,
            ranges=[d.value() for d in self.inputs.ranges],
            secondary=[d.value() for d in self.inputs.secondary],related_findings=variants)).value()
        if origin_check is None and variants:
            # D's physical question is the complete cue, independent of which
            # proposed spelling happens to be its representative. Use the
            # existing non-mutating CURRENT-only acquisition geometry as its
            # immutable origin; preserve the actual proposal check separately.
            from .final_review_auditor import _candidate_blind_missing_proposal_scaffold
            origin_row=_legacy(_candidate_blind_missing_proposal_scaffold,text,
                {'cue_index':row['cue_index'],'suspect':check['current_cue'],'repair_class':'disclosure_only'})
            whole=RetainedObject.from_value(_legacy(terminal_check,text,origin_row,
                clip_context=self.inputs.clip_context.value(),offset_ms=self.inputs.timeline_offset_ms)).value()
            # This is a fresh whole-cue identity record, not a provider check
            # or an old proof. No current spelling/length defines its P span.
            whole.pop('edit_span_binding',None)
            whole['scope_role']='QUESTION_IDENTITY_ONLY'
            whole['mutation_authorized']=False
            whole.pop('request_sha256',None)
            whole['request_sha256']=context.digest(whole)
            origin_check=RetainedObject.from_value(whole)
        previous_context = _object(check.get('finding_context',{}))
        owner_relations = [v for v in _array(previous_context.get('relations',[]))
                           if _object(v).get('kind') in {'speaker_turn','pronoun_chain','evidence'}]
        graph = _legacy(context.relation_graph, text, relations=owner_relations)
        check['finding_context'] = RetainedObject.from_value(_legacy(context.context_for_check,
            check, finding=row, graph=graph)).value()
        check.pop('request_sha256', None)
        check['request_sha256'] = cast(str, _legacy(context.digest, check))
        projection = RetainedObject.from_value(_legacy(evidence_projection, check, include_target_text=True)).value()
        observations = _array(projection['observations'])
        ids = tuple(sorted({_text(_object(value)['observation_id']) for value in observations} |
            {_text(_object(value)['observation_id']) for value in _array(row.get('discovery_nominations',[]))}))
        # P's codepoints are anchored to its original question. A current
        # proposal may locate a different edit after an applied patch. This
        # identity-only view is never a model/check/proof request.
        identity = dict(check)
        if origin_check is not None:
            physical = RetainedObject.from_value(_legacy(context.physical_question,origin_check.value())).value()
            span = physical['codepoint_span']
            if span is None:
                identity.pop('edit_span_binding',None)
            else:
                identity['edit_span_binding']={'codepoint_span':span}
            if RetainedObject.from_value(_legacy(context.physical_question,identity)).value() != physical:
                raise ClosureContractError('FINDING_CLOSURE_REENTRY_PHYSICAL_DRIFT')
        materials = RetainedObject.from_value(_legacy(context.materials_for_check, identity,
            finding=row, graph=graph, observation_ids=ids, contracts=self.inputs.contracts.value(),
            model_identity=self.inputs.model_identity.value(),
            candidates=[check.get('current_cue'),check.get('proposed_cue'),
                        *(v.get('proposed_full_cue') for v in variants)]))
        value = materials.value()
        scope = tuple(_integer(i) for i in _array(_object(check['finding_context'])['scope_cue_indexes']))
        retained_check = RetainedObject.from_value(check)
        origin = origin_check if origin_check is not None else retained_check
        spec = TaskSpec(FindingKey(_text(value['physical_question_sha256']), _text(value['materials_sha256'])),
            scope, core.local_text_sha(text, scope), original.sha256, retained_check.sha256, materials.sha256,origin.sha256)
        return PreparedFinding(spec, original, retained_check, materials,origin)

    def _validate_projection_basis(self, basis: ProjectionBasis) -> None:
        from .producer_package_finalization import _apply_exact_final_cpa_repairs
        actual=_legacy(_apply_exact_final_cpa_repairs,basis.before_srt,
            {'findings':[basis.ruling.value()]},clip_context=self.inputs.clip_context.value(),
            protected_cue_indexes=self._protected_indexes(basis.before_srt))
        if (not isinstance(actual,tuple) or len(actual)!=2 or actual[0]!=basis.after_srt
                or RetainedObject.from_value({'repairs':actual[1]})!=basis.repair):
            raise ClosureContractError('FINDING_CLOSURE_PROJECTION_WITHOUT_LEGAL_PATCH')

    def _absent_finding(self, text: str, finding: PreparedFinding,
                        removal: ProjectionBasis | None) -> PreparedFinding:
        from . import finding_context as context
        from .jingting_chunker import parse_srt_cues
        origin=finding.origin_check or finding.check
        physical=RetainedObject.from_value(_legacy(context.physical_question,origin.value())).value()
        window=_array(physical['source_window_ms'])
        start,end=_integer(window[0]),_integer(window[1])
        cues=[c for c in parse_srt_cues(text) if c.text.strip()]
        if not cues:
            raise ClosureContractError('FINDING_CLOSURE_EMPTY_DELIVERY')
        offset=self.inputs.timeline_offset_ms
        nearest=min(range(1,len(cues)+1),key=lambda i:min(
            abs(cues[i-1].start_ms+offset-end),abs(cues[i-1].end_ms+offset-start)))
        scope=set(range(max(1,nearest-context.LOCAL_CONTEXT_CUES),
                        min(len(cues),nearest+context.LOCAL_CONTEXT_CUES)+1))
        graph=RetainedObject.from_value(_legacy(context.relation_graph,text)).value()
        while True:
            expanded=scope | { _integer(i) for value in _array(graph['relations'])
                for edge in [_object(value)] if scope.intersection(_integer(i) for i in _array(edge['cue_indexes']))
                for i in _array(edge['cue_indexes']) }
            if expanded==scope:break
            scope=expanded
        local={'cues':[[cues[i-1].start_ms+offset,cues[i-1].end_ms+offset,cues[i-1].text] for i in sorted(scope)],
            'relations':[{'kind':_object(edge)['kind'],
                'members':[[cues[_integer(i)-1].start_ms+offset,cues[_integer(i)-1].end_ms+offset]
                    for i in _array(_object(edge)['cue_indexes'])]}
                for edge in _array(graph['relations'])
                if scope.intersection(_integer(i) for i in _array(_object(edge)['cue_indexes']))]}
        if removal is None:
            if finding.projection is None:
                raise ClosureContractError('FINDING_CLOSURE_REMOVAL_AUTHORITY_REQUIRED')
            original=_object(finding.projection.value()['removal_basis'])
        else:
            original={'before_srt':removal.before_srt,'after_srt':removal.after_srt,
                'repair':removal.repair.value(),'ruling':removal.ruling.value()}
        proof=RetainedObject.from_value(original)
        projection=RetainedObject.from_value({'schema_version':'question-current-projection.v1',
            'presence':'ABSENT','physical_question':physical,'current_srt':text,
            'scope_cues':sorted(scope),'local_context':local,'removal_basis':proof.value()})
        version=context.MaterialVersion(context.physical_question_value(origin.value()),
            RetainedObject.from_value(local).json_text,(),(),
            RetainedObject.from_value({'applied_removal_sha256':proof.sha256}).json_text,
            RetainedObject.from_value(finding.materials.value()['model_identity']).json_text)
        material=RetainedObject.from_value(version.to_document())
        row=finding.finding.value()
        row['current_question_projection']={'presence':'ABSENT','projection_ref':projection.sha256}
        retained=RetainedObject.from_value(row)
        value=material.value()
        spec=TaskSpec(FindingKey(_text(value['physical_question_sha256']),_text(value['materials_sha256'])),
            tuple(sorted(scope)),core.local_text_sha(text,tuple(sorted(scope))),retained.sha256,
            projection.sha256,material.sha256,origin.sha256,'ABSENT',projection.sha256,
            RetainedObject.from_value(original['ruling']).sha256)
        return PreparedFinding(spec,retained,projection,material,origin,projection)

    def rebase(self, request: RebaseRequest) -> tuple[PreparedFinding, ...]:
        from . import finding_context as context
        from .jingting_chunker import parse_srt_cues
        cues=[c for c in parse_srt_cues(request.srt_text) if c.text.strip()]
        by_window={(c.start_ms,c.end_ms):i for i,c in enumerate(cues,1)}
        if len(by_window)!=len(cues):
            raise ClosureContractError('FINDING_CLOSURE_AMBIGUOUS_CURRENT_GRID')
        for basis in request.basis:self._validate_projection_basis(basis)
        output=[]
        for finding in request.findings:
            if finding.spec.presence=='ABSENT':
                output.append(self._absent_finding(request.srt_text,finding,None))
                continue
            old=finding.check.value()
            old_cues=[c for c in parse_srt_cues(_text(old['whole_clip_current_srt'])) if c.text.strip()]
            start,end=_integer(old['matched_start_ms']),_integer(old['matched_end_ms'])
            index=by_window.get((start,end))
            if index is None:
                removal=next((basis for basis in request.basis for repair in _array(basis.repair.value()['repairs'])
                    if _object(repair).get('action') in {'DROP_CUE','OMIT_CUE_DISPLAY'}
                    and _object(repair).get('matched_start_ms')==start
                    and _object(repair).get('matched_end_ms')==end),None)
                if removal is None:
                    raise ClosureContractError('FINDING_CLOSURE_PROJECTION_WITHOUT_LEGAL_PATCH')
                output.append(self._absent_finding(request.srt_text,finding,removal))
                continue
            if [(c.start_ms,c.end_ms) for c in old_cues] != list(by_window):
                if not request.basis or request.basis[-1].after_srt!=request.srt_text:
                    raise ClosureContractError('FINDING_CLOSURE_PROJECTION_WITHOUT_LEGAL_PATCH')
            relations=[]
            for value in _array(_object(old['finding_context'])['relations']):
                edge=_object(value)
                if edge.get('kind') not in {'speaker_turn','pronoun_chain','evidence'}:continue
                members=tuple(sorted({by_window[(old_cues[_integer(i)-1].start_ms,old_cues[_integer(i)-1].end_ms)]
                    for i in _array(edge['cue_indexes'])
                    if (old_cues[_integer(i)-1].start_ms,old_cues[_integer(i)-1].end_ms) in by_window}))
                if len(members)>1:relations.append({'kind':edge['kind'],'cue_indexes':list(members)})
            current=dict(old)
            current.update(whole_clip_current_srt=request.srt_text,cue_indexes=[index])
            graph=_legacy(context.relation_graph,request.srt_text,relations=relations)
            projected=_legacy(context.context_for_check,current,finding={},graph=graph)
            scope=tuple(_integer(i) for i in _array(RetainedObject.from_value(projected).value()['scope_cue_indexes']))
            if _legacy(context.local_material_view,projected)==_legacy(context.local_material_view,old['finding_context']):
                # Cue ordinals are addresses. Preserve the answered M and raw
                # originals when only those addresses changed after a drop.
                output.append(replace(finding,spec=replace(finding.spec,scope_cues=scope,
                    local_text_sha256=core.local_text_sha(request.srt_text,scope))))
                continue
            row=finding.finding.value()
            changed=cues[index-1].text != old['current_cue']
            for key in ('exact_release_adjudication','context_audio_adjudication','terminal_funnel_error',
                        'lifecycle','current_content_risk','candidate_content_risk','consumed_ruling',
                        'consumed_rulings','next_finding','next_finding_materials'):
                row.pop(key,None)
            row['previous_finding_ref']=finding.finding.sha256
            row['cue_index']=index
            row['base_text_sha256']=core.sha(cues[index-1].text)
            row['finding_context']=RetainedObject.from_value({'relations':relations}).value()
            if changed:
                row.update(suspect=cues[index-1].text,suggestion=cues[index-1].text,
                    proposed_full_cue=cues[index-1].text,span_start_codepoint=0,
                    span_end_codepoint=len(cues[index-1].text),candidate_memory_id=None,
                    candidate_blind_observation_scaffolding=True,
                    candidate_provenance={'kind':'candidate_blind_observation_scaffolding',
                        'basis':'CURRENT_ONLY_DUPLICATED','mutation_authorized':False})
            output.append(self.prepare_finding(request.srt_text,RetainedObject.from_value(row),
                origin_check=finding.origin_check or finding.check))
        return tuple(output)

    def _bound_owner_materials(self, row: RetainedObject, finding: PreparedFinding) -> None:
        """An unported owner cannot claim that its whole-prompt key is local M."""
        value = row.value()
        adjudication = _object(value.get('exact_release_adjudication'))
        terminal = adjudication.get('terminal_review')
        request = _object(_object(terminal).get('request')) if isinstance(terminal, dict) else _object(adjudication.get('request'))
        bound = request.get('finding_materials')
        if not isinstance(bound, dict):
            raise ClosureContractError('FINDING_CLOSURE_OWNER_TYPED_MATERIALS_REQUIRED')
        from .finding_context import validate_materials
        actual = RetainedObject.from_value(_legacy(validate_materials, bound)).value()
        if (actual['physical_question_sha256'] != finding.spec.key.physical
                or actual['materials_sha256'] != finding.spec.key.materials):
            raise ClosureContractError('FINDING_CLOSURE_OWNER_MATERIAL_DRIFT')

    def _result(self, text: str, row: RetainedObject, *, terminal: bool = False,
                origin_check: RetainedObject | None = None) -> ReviewResult:
        from .finding_lifecycle import classify
        finding = self.prepare_finding(text, row,origin_check=origin_check)
        view = RetainedObject.from_value(_legacy(classify, row.value(), terminal=terminal)).value()
        selected = State(_text(view['state']))
        members=_array(row.value().get('related_question_results',[]))
        if members:
            states=[State(_text(RetainedObject.from_value(_legacy(classify,member,terminal=terminal)).value()['state']))
                    for member in members]
            for engineering in (State.CONTRACT_ERROR,State.CALL_FAILURE):
                if engineering in states:selected=engineering;break
            else:
                if State.TRUE_BLOCK in states:selected=State.TRUE_BLOCK
                elif any(s not in {State.KEEP,State.EDIT_REVIEW} for s in states):selected=State.AWAITING_TERMINAL
        consumed=row.value().get('consumed_ruling')
        if isinstance(consumed,dict):
            record=consumed_ruling_from_document(RetainedObject.from_value(consumed))
            if record.raw_completion not in original_ruling_bytes(row.value()):
                raise ClosureContractError('FINDING_CLOSURE_RULING_RAW_NOT_CONSUMED')
            from .finding_context import local_material_view
            material=record.materials.value()
            current=RetainedObject.from_value(_legacy(local_material_view,
                finding.check.value()['finding_context'])).value()
            if material['local_context']!=current or _text(material['physical_question_sha256'])!=finding.spec.key.physical:
                raise ClosureContractError('FINDING_CLOSURE_CONSUMED_CURRENT_MATERIAL_DRIFT')
            # The answered M is retained separately from the next nomination.
            # Candidate pruning and actual fallback cannot rename this ruling.
            spec=replace(finding.spec,key=FindingKey(_text(material['physical_question_sha256']),
                _text(material['materials_sha256'])),check_ref=record.check.sha256,
                materials_ref=record.materials.sha256,origin_check_ref=record.origin_check.sha256)
            finding=PreparedFinding(spec,row,record.check,record.materials,record.origin_check)
            proposals={_text(_object(v)['proposed_full_cue']) for v in _array(row.value().get('discovery_candidates',[]))
                       if isinstance(_object(v).get('proposed_full_cue'),str)}
            if (selected in {State.KEEP,State.EDIT_REVIEW,State.TRUE_BLOCK}
                    and not proposals.issubset(set(_text(v) for v in _array(material['candidates'])))):
                selected=State.AWAITING_TERMINAL
            nomination_ids={_text(_object(v)['observation_id'])
                for v in _array(row.value().get('discovery_nominations',[]))}
            if (selected in {State.KEEP,State.EDIT_REVIEW,State.TRUE_BLOCK} and not nomination_ids.issubset(
                    set(_text(v) for v in _array(material['bound_observation_ids'])))):
                selected=State.AWAITING_TERMINAL
            return ReviewResult(finding,selected,row)
        if selected in {State.KEEP, State.EDIT_REVIEW, State.TRUE_BLOCK}:
            try:
                self._bound_owner_materials(row, finding)
            except ClosureContractError as exc:
                # Retain the actual answer and expose a missing owner interface
                # as engineering failure; never resample it or turn it into D.
                return ReviewResult(finding, State.CONTRACT_ERROR,
                    RetainedObject.from_value({'finding': row.value(), 'owner_error': str(exc)}))
        return ReviewResult(finding, selected, row)

    def _adjudicate(self, request: ReviewRequest, *, terminal: bool = False,
                    provider_calls_allowed: bool = True) -> ReviewResult:
        with provider_permission(provider_calls_allowed), ruling_capture(self.inputs.judge_llm_call,request.finding) as captured:
            result=self._adjudicate_captured(request,terminal=terminal,
                                            provider_calls_allowed=provider_calls_allowed)
            captured.raise_failure()
            return result

    def _adjudicate_captured(self, request: ReviewRequest, *, terminal: bool,
                             provider_calls_allowed: bool) -> ReviewResult:
        from .final_review_auditor import adjudicate_exact_release_findings
        from .exact_final_terminal_funnel import capture_terminal_adapters
        # The ordinary owner retains budget, UNKNOWN and completed-response
        # checkpoints. C never makes its own audio or model request.
        with cast(AbstractContextManager[object], _legacy(capture_terminal_adapters)) as adapters:
            result = _legacy(adjudicate_exact_release_findings, request.srt_text,
                [request.finding.finding.value()], entity_verifier=(self.inputs.entity_verifier
                    if provider_calls_allowed else CacheOnlyVerifier(self.inputs.entity_verifier)),
                clip_context=self.inputs.clip_context.value(),
                source_media_timeline_offset_ms=self.inputs.timeline_offset_ms,
                judge_llm_call=self.inputs.judge_llm_call, screen_read_probe=self.inputs.screen_read_probe,
                provider_calls_allowed=provider_calls_allowed,
                prior_range_acoustic_observations=[d.value() for d in self.inputs.ranges],
                prior_secondary_audio_observations=[d.value() for d in self.inputs.secondary],
                caption_audio_roles=self.inputs.caption_audio_roles.value() if self.inputs.caption_audio_roles else None)
            if not isinstance(result, tuple) or len(result) != 2:
                raise ClosureContractError('FINDING_CLOSURE_OWNER_RESULT_INVALID')
            rows = [*_array(cast(JsonValue, result[0])), *_array(cast(JsonValue, result[1]))]
            if len(rows) != 1:
                raise ClosureContractError('FINDING_CLOSURE_OWNER_TASK_CARDINALITY')
            row = attach_consumed_rulings(RetainedObject.from_value(rows[0]),self,request.srt_text)
            if terminal:
                selected = self._result(request.srt_text,row,origin_check=request.finding.origin_check)
                if selected.state in core.TERMINAL_STATES:
                    return selected
                return self._terminal_from_snapshot(request.srt_text,row,adapters,request.finding.origin_check)
        return self._result(request.srt_text,row,origin_check=request.finding.origin_check)

    def evidence(self, request: ReviewRequest) -> ReviewResult:
        return self._adjudicate(request)

    def decide(self, request: ReviewRequest) -> ReviewResult:
        return self._adjudicate(request)

    def recover(self, request: ReviewRequest) -> ReviewResult:
        # Consumers may have been repaired since the original answer. The
        # ordinary owner reopens its original cache with all new calls disabled.
        cleaned = request.finding.finding.value()
        cleaned.pop('terminal_funnel_error',None)
        cleaned['revalidated_original_ref'] = request.finding.finding.sha256
        current = self._result(request.srt_text,RetainedObject.from_value(cleaned),terminal=True,
                               origin_check=request.finding.origin_check)
        if current.state in {State.KEEP,State.EDIT_REVIEW,State.TRUE_BLOCK}:
            return current
        result = self._adjudicate(ReviewRequest(request.work,request.srt_text,current.finding),
                                  provider_calls_allowed=False)
        if result.state not in core.TERMINAL_STATES:
            return ReviewResult(result.finding, State.CALL_FAILURE, RetainedObject.from_value({
                'finding': result.finding.finding.value(),
                'owner_error': 'FINDING_CLOSURE_ORIGINAL_CALL_OWNER_RECOVERY_REQUIRED'}))
        return result

    def terminal(self, request: ReviewRequest) -> ReviewResult:
        # Reconstruct the original budget/terminal owner through its cache-only
        # replay path rather than persisting a live callback in task JSON.
        return self._adjudicate(request, terminal=True)

    def _terminal_from_snapshot(self, text: str, row: RetainedObject, adapters: object,
                                origin: RetainedObject | None) -> ReviewResult:
        from .exact_final_terminal_funnel import terminalize_review
        primary=row.value()
        candidates=_array(primary.get('discovery_candidates',[]))
        agenda=[primary]
        if len(candidates)>1:
            agenda=[]
            for candidate in candidates:
                member={**primary,**_object(candidate)}
                member['exact_release_adjudication']={'status':'UNCERTAIN',
                    'reason_code':'PER_FINDING_ALL_CANDIDATES_REQUIRE_TERMINAL',
                    'prior_adjudication':primary.get('exact_release_adjudication')}
                for key in ('consumed_ruling','consumed_rulings','related_question_results'):
                    member.pop(key,None)
                agenda.append(member)
        audit = {'findings': agenda, 'unresolved_findings_disclosed': [],
                 'reason_codes': [], 'discovery': {'status': 'COMPLETE'}}
        def task_materials(check: object, original: object) -> object:
            from .finding_context import materials_for_check
            from .targeted_relisten import evidence_projection
            current = RetainedObject.from_value(check).value()
            proposal = RetainedObject.from_value(original).value()
            projection = RetainedObject.from_value(_legacy(evidence_projection, current,
                include_target_text=True)).value()
            return _legacy(materials_for_check, current, finding=proposal,
                contracts=self.inputs.contracts.value(), model_identity=self.inputs.model_identity.value(),
                candidates=[current.get('current_cue'), current.get('proposed_cue'),
                    *(_object(v).get('proposed_full_cue') for v in candidates)],
                observation_ids=[_object(v)['observation_id'] for v in _array(projection['observations'])]
                    + [_object(v)['observation_id'] for v in _array(current.get('discovery_nomination_evidence',[]))])
        result = RetainedObject.from_value(_legacy(terminalize_review, text, audit,
            pass_receipts=[], adapters=adapters, task_materials=task_materials)).value()
        rows = [*_array(result.get('findings', [])), *_array(result.get('unresolved_findings_disclosed', []))]
        if not rows or len(rows)!=len(agenda):
            raise ClosureContractError('FINDING_CLOSURE_OWNER_TASK_CARDINALITY')
        from .finding_lifecycle import classify
        ranked={State.CONTRACT_ERROR:0,State.CALL_FAILURE:1,State.TRUE_BLOCK:2,State.EDIT_REVIEW:3,State.KEEP:5}
        chosen=min(rows,key=lambda value:ranked.get(State(_text(
            RetainedObject.from_value(_legacy(classify,value,terminal=True)).value()['state'])),4))
        combined=dict(_object(chosen))
        if len(rows)>1:combined['related_question_results']=rows
        row=attach_consumed_rulings(RetainedObject.from_value(combined),self,text)
        return self._result(text,row,terminal=True,origin_check=origin)

    def patch(self, request: PatchRequest) -> PatchResult:
        from .producer_package_finalization import _apply_exact_final_cpa_repairs
        result = _legacy(_apply_exact_final_cpa_repairs, request.srt_text,
            {'findings': [request.finding.finding.value()]}, clip_context=self.inputs.clip_context.value(),
            protected_cue_indexes=self._protected_indexes(request.srt_text))
        if not isinstance(result, tuple) or len(result) != 2 or not isinstance(result[0], str):
            raise ClosureContractError('FINDING_CLOSURE_PATCH_OWNER_RESULT_INVALID')
        output, repairs = result
        if output == request.srt_text or not isinstance(repairs, list) or not repairs:
            raise ClosureContractError('FINDING_CLOSURE_EDIT_NOT_APPLIED')
        repair = RetainedObject.from_value({'repairs': repairs})
        left = 0
        while left < min(len(output), len(request.srt_text)) and output[left] == request.srt_text[left]:
            left += 1
        right = 0
        while (right < len(output)-left and right < len(request.srt_text)-left
               and output[-1-right] == request.srt_text[-1-right]):
            right += 1
        before = request.srt_text[left:len(request.srt_text)-right if right else None]
        after = output[left:len(output)-right if right else None]
        patch = Patch(request.work.work_id, request.finding.spec.key, request.ruling.sha256,
            core.sha(request.srt_text), core.sha(output), left, before, after, repair.sha256)
        return PatchResult(patch, self.rebase(RebaseRequest(output, request.all_findings,
            (ProjectionBasis(request.srt_text,output,repair,request.ruling),))), repair)

    def revalidate(self, request: ValidationRequest) -> None:
        event = request.event
        if isinstance(event,ScanCompleted) and event.rebased:
            originals=tuple(prepared_from_refs(spec,request.artifacts.get) for spec in event.findings)
            basis=tuple(ProjectionBasis(core.text_at(request.state,p.before_sha256),core.text_at(request.state,p.after_sha256),
                request.artifacts.get(p.repair_ref),request.artifacts.get(p.ruling_ref)) for p in request.state.patches)
            rebased_findings=self.rebase(RebaseRequest(request.state.current_srt,originals,basis))
            if tuple(f.spec for f in rebased_findings)!=event.rebased:
                raise ClosureContractError('FINDING_CLOSURE_STALE_SCAN_PROJECTION_DRIFT')
            for finding in rebased_findings:
                for document in finding_documents(finding):
                    if request.artifacts.get(document.sha256)!=document:
                        raise ClosureContractError('FINDING_CLOSURE_STALE_SCAN_PROJECTION_DRIFT')
        elif isinstance(event, PrefetchCompleted):
            from .independent_context_adjudication import validate_range_acoustic_observation
            from .native_audio_budget_receipt import _require_budget_history_extension
            text = core.text_at(request.state,request.work.text_sha256)
            check,wide = self._prefetch_geometry(text)
            seen=[]
            for ref in event.receipt_refs:
                record = request.artifacts.get(ref).value()
                if (record['source_sha256'] != self.inputs.source_sha256
                        or RetainedObject.from_value(record['check_request']) != check
                        or RetainedObject.from_value(record['request']) != wide
                        or record['authority'] != 'EVIDENCE_ONLY' or record['mutation_authorized'] is not False):
                    raise ClosureContractError('FINDING_CLOSURE_PREFETCH_BINDING_DRIFT')
                seen.append(_text(record['provider']))
                before,after=_object(record['budget_before']),_object(record['budget_after'])
                if before.get('receipt') is not None:
                    if after.get('receipt') is None:
                        raise ClosureContractError('RELISTEN_DISPATCH_LEDGER_DISAPPEARED')
                    _legacy(_require_budget_history_extension,before['receipt'],after['receipt'])
                for row in _array(record['observations']):
                    if _object(row).get('status') == 'OBSERVED':
                        verdict = _legacy(validate_range_acoustic_observation,text,check.value(),row)
                        if not isinstance(verdict,tuple) or not verdict[0]:
                            raise ClosureContractError('FINDING_CLOSURE_PREFETCH_OBSERVATION_REJECTED')
            if tuple(sorted(seen)) != tuple(sorted(set(self.inputs.prefetch_providers))):
                raise ClosureContractError('FINDING_CLOSURE_PREFETCH_PROVIDER_SET_DRIFT')
        elif isinstance(event, RevalidateRequested):
            revisions = _object(request.artifacts.get(event.consumer_revision_ref).value()['consumer_modules'])
            if not revisions or not all(core.valid_sha(_text(value)) for value in revisions.values()):
                raise ClosureContractError('FINDING_CLOSURE_REVALIDATION_REVISION_INVALID')
        elif isinstance(event, ReviewCompleted):
            prepared = prepared_from_refs(event.spec,request.artifacts.get)
            text = core.text_at(request.state, request.work.text_sha256)
            rebuilt = (self._result(text,prepared.finding,terminal=request.work.kind=='task_stop',
                       origin_check=prepared.origin_check).finding
                       if isinstance(prepared.finding.value().get('consumed_ruling'),dict)
                       else self.prepare_finding(text,prepared.finding,origin_check=prepared.origin_check))
            if rebuilt != prepared:
                raise ClosureContractError('FINDING_CLOSURE_COLD_MATERIAL_DRIFT')
            if event.state in {State.CONTRACT_ERROR,State.CALL_FAILURE}:
                # Historical engineering outcomes are not content authority.
                # Retain the original answer even when today's consumer can
                # now validate it; only a new revalidation event can close P.
                receipt = request.artifacts.get(event.receipt_ref)
                value = receipt.value()
                if 'owner_error' in value:
                    if RetainedObject.from_value(value.get('finding')) != prepared.finding:
                        raise ClosureContractError('FINDING_CLOSURE_COLD_FAILURE_ORIGINAL_DRIFT')
                elif receipt != prepared.finding:
                    raise ClosureContractError('FINDING_CLOSURE_COLD_FAILURE_ORIGINAL_DRIFT')
                return
            actual = self._result(text,prepared.finding,terminal=request.work.kind == 'task_stop',
                                  origin_check=prepared.origin_check)
            if actual.state != event.state or actual.receipt != request.artifacts.get(event.receipt_ref):
                raise ClosureContractError('FINDING_CLOSURE_COLD_RULING_DRIFT')
        elif isinstance(event, PatchCompleted):
            if request.work.task is None:
                raise ClosureContractError('FINDING_CLOSURE_PATCH_TASK_REQUIRED')
            spec = request.work.task
            finding = prepared_from_refs(spec,request.artifacts.get)
            # Reapply through the existing mutation consumer; a forged patch
            # with freshly recomputed hashes is not a valid typed repair.
            actual_patch = self.patch(PatchRequest(request.work, request.state.current_srt, finding,
                request.artifacts.get(event.patch.ruling_ref),
                tuple(prepared_from_refs(t.spec,request.artifacts.get) for t in request.state.tasks)))
            if actual_patch.patch != event.patch or tuple(f.spec for f in actual_patch.findings) != event.findings:
                raise ClosureContractError('FINDING_CLOSURE_COLD_PATCH_DRIFT')
        elif isinstance(event, TextReady):
            validate_text_ready(request.artifacts.get(event.receipt_ref), event.output_sha256)
        elif isinstance(event, Confirmed):
            from .final_review_contract import validate_final_review_release
            _legacy(validate_final_review_release, request.artifacts.get(event.receipt_ref).value(),
                    expected_srt_sha256='sha256:' + event.output_sha256)


@dataclass(frozen=True)
class ProductionPorts:
    discovery: DiscoveryPorts
    owners: ExistingFindingOwners
    final_gate: Callable[[ConfirmationRequest], RetainedObject]

    def first_scan(self, request: ScanRequest) -> ScanResult:
        with ruling_capture(self.owners.inputs.judge_llm_call) as captured:
            result=self.discovery.first_scan(request)
            captured.raise_failure()
            return result

    def prefetch(self, request: PrefetchRequest) -> PrefetchResult:
        return self.owners.prefetch(request)

    def targeted_scan(self, request: ScanRequest) -> ScanResult:
        with ruling_capture(self.owners.inputs.judge_llm_call) as captured:
            result=self.discovery.targeted_scan(request)
            captured.raise_failure()
            return result

    def final_scan(self, request: ScanRequest) -> ScanResult:
        with ruling_capture(self.owners.inputs.judge_llm_call) as captured:
            result=self.discovery.final_scan(request)
            captured.raise_failure()
            return result

    def validate_scan(self, request: ValidationRequest) -> None:
        self.discovery.validate_scan(request)

    def validate_terminal(self, request: ValidationRequest) -> None:
        self.discovery.validate_terminal(request)

    def evidence(self, request: ReviewRequest) -> ReviewResult:
        return self.owners.evidence(request)

    def decide(self, request: ReviewRequest) -> ReviewResult:
        return self.owners.decide(request)

    def terminal(self, request: ReviewRequest) -> ReviewResult:
        return self.owners.terminal(request)

    def recover(self, request: ReviewRequest) -> ReviewResult:
        return self.owners.recover(request)

    def rebase(self, request: RebaseRequest) -> tuple[PreparedFinding, ...]:
        return self.owners.rebase(request)

    def patch(self, request: PatchRequest) -> PatchResult:
        return self.owners.patch(request)

    def revalidate(self, request: ValidationRequest) -> None:
        if isinstance(request.event, InputsAdvanced):
            from .discovery_channels import validate_input_update
            _legacy(validate_input_update,request,self.discovery)
        elif isinstance(request.event, ScanCompleted):
            self.validate_scan(request)
            self.owners.revalidate(request)
        else:
            self.owners.revalidate(request)
            if (isinstance(request.event,ReviewCompleted)
                    and request.event.state in {State.KEEP,State.EDIT_REVIEW,State.TRUE_BLOCK}):
                # Discovery owns ASR provenance/R5. It must reconstruct the
                # actual exhaustion evidence after F2's original consumer
                # validates it; C never fills these booleans from a summary.
                self.validate_terminal(request)

    def confirm(self, request: ConfirmationRequest) -> ConfirmationResult:
        router=self.owners.inputs.judge_llm_call
        if isinstance(router,FinalReviewCall):
            router.drain()  # Late raw/persistence failures must precede release.
        with ruling_capture(router) as captured:
            audit=self.final_gate(request)
            captured.raise_failure()
        if isinstance(router,FinalReviewCall):
            router.drain()
        output_sha = core.sha(request.srt_text)
        ready = audit.value().get('pre_boundary_text_recovery') is None
        if not ready:
            validate_text_ready(audit, output_sha)
        return ConfirmationResult(output_sha,audit,release_ready=ready)


def discovery_document(inputs: DiscoveryInputs) -> RetainedObject:
    return RetainedObject.from_value({'source_sha256': inputs.source_sha256,
        'timeline_offset_ms': inputs.timeline_offset_ms, 'glossary_text': inputs.glossary_text,
        'structured_context_text': inputs.structured_context_text,
        'candidate_context_text': inputs.candidate_context_text,
        'candidate_context': inputs.candidate_context.value(),
        'verified_authority': inputs.verified_authority.value(),
        'correction_audit': inputs.correction_audit.value(),
        'priority_findings': [d.value() for d in inputs.priority_findings],
        'draft_fidelity_contexts': [d.value() for d in inputs.draft_fidelity_contexts],
        'source_media_path': inputs.source_media_path, 'output_root': inputs.output_root,
        'candidate_id': inputs.candidate_id, 'spec': inputs.spec.value() if inputs.spec else None,
        'authoritative_chat': [d.value() for d in inputs.authoritative_chat],
        'truth_ownership': inputs.truth_ownership.value() if inputs.truth_ownership else None})


def discovery_factory(inputs: DiscoveryInputs) -> DiscoveryPorts:
    from importlib import import_module
    try:
        module = import_module('src.autoslice.discovery_channels')
        factory = getattr(module, 'build_closure_discovery_ports', None)
    except ModuleNotFoundError as exc:
        if exc.name != 'src.autoslice.discovery_channels':
            raise
        raise ClosureContractError('FINDING_CLOSURE_DISCOVERY_PORTS_UNAVAILABLE') from exc
    if not callable(factory):
        raise ClosureContractError('FINDING_CLOSURE_DISCOVERY_PORTS_UNAVAILABLE')
    return cast(Callable[[DiscoveryInputs], DiscoveryPorts], factory)(inputs)


def registered_scan_channels() -> tuple[str, ...]:
    from importlib import import_module
    # The core receives this immutable value, avoiding I/O imports there.
    # D's registry is the sole production definition of scan strength.
    try:
        module = import_module('src.autoslice.discovery_channels')
    except ModuleNotFoundError as exc:
        if exc.name != 'src.autoslice.discovery_channels':
            raise
        raise ClosureContractError('FINDING_CLOSURE_DISCOVERY_PORTS_UNAVAILABLE') from exc
    channels = _legacy(getattr(module,'scan_channels'))
    if not isinstance(channels,tuple) or any(not isinstance(v,str) for v in channels):
        raise ClosureContractError('FINDING_CLOSURE_SCAN_CHANNELS_INVALID')
    return cast(tuple[str,...],channels)


def _no_call(_prompt: str) -> str:
    raise ClosureContractError('FINDING_CLOSURE_COLD_REPLAY_FORBIDS_CALL')


def _extract(text: str) -> object:
    from .llm_client import extract_json_object
    return _legacy(extract_json_object, text)


def load_owner_inputs(document: RetainedObject, *, live: OwnerInputs | None = None) -> OwnerInputs:
    obj = document.value()
    caption = obj['caption_audio_roles']
    return OwnerInputs(_text(obj['source_sha256']), _integer(obj['timeline_offset_ms']),
        RetainedObject.from_value(obj['clip_context']), RetainedObject.from_value(obj['contracts']),
        RetainedObject.from_value(obj['model_identity']),
        live.entity_verifier if live else None, live.judge_llm_call if live else _no_call,
        live.screen_read_probe if live else None,
        tuple(RetainedObject.from_value(v) for v in _array(obj['ranges'])),
        tuple(RetainedObject.from_value(v) for v in _array(obj['secondary'])),
        RetainedObject.from_value(caption) if caption is not None else None,
        frozenset(_integer(v) for v in _array(obj['protected_cues'])),
        tuple(_text(v) for v in _array(obj['prefetch_providers'])),
        tuple((_integer(_array(v)[0]),_integer(_array(v)[1])) for v in _array(obj.get('protected_windows',[]))),
        _text(obj.get('candidate_id','')))


def load_discovery_inputs(document: RetainedObject, owners: ExistingFindingOwners,
                          *, live: DiscoveryInputs | None = None) -> DiscoveryInputs:
    obj = document.value()
    return DiscoveryInputs(_text(obj['source_sha256']), _integer(obj['timeline_offset_ms']),
        _text(obj['glossary_text']), _text(obj['structured_context_text']),
        _text(obj['candidate_context_text']), RetainedObject.from_value(obj['candidate_context']),
        RetainedObject.from_value(obj['verified_authority']), RetainedObject.from_value(obj['correction_audit']),
        tuple(RetainedObject.from_value(v) for v in _array(obj['priority_findings'])),
        tuple(RetainedObject.from_value(v) for v in _array(obj['draft_fidelity_contexts'])),
        owners.prepare_finding, live.llm_call if live else _no_call, _extract,
        live.runtime_adapters if live else None, live.entity_verifier if live else None,
        live.screen_read_probe if live else None,
        _text(obj['source_media_path']), _text(obj['output_root']), _text(obj['candidate_id']),
        RetainedObject.from_value(obj['spec']) if obj['spec'] is not None else None,
        tuple(RetainedObject.from_value(v) for v in _array(obj['authoritative_chat'])),
        RetainedObject.from_value(obj['truth_ownership']) if obj['truth_ownership'] is not None else None)


def cold_ports(configuration: RetainedObject) -> ClosurePorts:
    obj = configuration.value()
    owners = ExistingFindingOwners(load_owner_inputs(RetainedObject.from_value(obj['owners'])))
    inputs = load_discovery_inputs(RetainedObject.from_value(obj['discovery']), owners)
    def forbidden(_request: ConfirmationRequest) -> RetainedObject:
        raise ClosureContractError('FINDING_CLOSURE_COLD_REPLAY_FORBIDS_CALL')
    return ProductionPorts(discovery_factory(inputs), owners, forbidden)


def load_initial(root: Path) -> ClosureState:
    obj = RetainedObject((root/'initial.json').read_text(encoding='utf-8').removesuffix('\n')).value()
    initial = core.initial_state(source_sha256=_text(obj['source_sha256']),
        srt_text=_text(obj['initial_srt']),
        scan_channels=tuple(_text(v) for v in _array(obj['scan_channels'])),
        configuration_ref=_text(obj['configuration_ref']))
    if core.state_bytes(initial) != (root/'initial.json').read_bytes():
        raise ClosureContractError('FINDING_CLOSURE_INITIAL_STATE_INVALID')
    return initial


def validate_published_closure(audit: RetainedObject, *, expected_srt_sha256: str | None) -> ClosureState:
    obj = audit.value()
    intermediate = obj.get('finding_closure')
    if isinstance(intermediate, dict) and intermediate.get('schema_version') == 'finding-closure-text-ready.v1':
        raise ClosureContractError('FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED')
    receipt = _fields(obj.get('finding_closure'), ('schema_version', 'ledger_root', 'source_sha256',
        'initial_srt_sha256', 'state_sha256', 'configuration_ref', 'confirmation_ref', 'output_sha256',
        'patch_count', 'task_count'))
    if receipt['schema_version'] != 'finding-closure-release.v1':
        raise ClosureContractError('FINDING_CLOSURE_RELEASE_SCHEMA_INVALID')
    output_sha = _text(receipt['output_sha256'])
    if (not core.valid_sha(output_sha) or obj.get('reviewed_srt_sha256') != 'sha256:' + output_sha
            or expected_srt_sha256 is not None and expected_srt_sha256 != 'sha256:' + output_sha):
        raise ClosureContractError('FINDING_CLOSURE_RELEASE_BYTES_MISMATCH')
    root = Path(_text(receipt['ledger_root']))
    if not root.is_absolute() or not root.is_dir():
        raise ClosureContractError('FINDING_CLOSURE_LEDGER_MISSING')
    initial = load_initial(root)
    ledger = FileLedger(root, initial, create=False)
    if (receipt['source_sha256'] != initial.source_sha256
            or receipt['initial_srt_sha256'] != core.sha(initial.initial_srt)
            or receipt['configuration_ref'] != initial.configuration_ref):
        raise ClosureContractError('FINDING_CLOSURE_RELEASE_ORIGIN_DRIFT')
    configuration = ledger.get(initial.configuration_ref)
    config = configuration.value()
    if config.get('scan_channels') != list(initial.scan_channels):
        raise ClosureContractError('FINDING_CLOSURE_SCAN_CHANNELS_CHANGED')
    prefix = config.get('legacy_prefix')
    if prefix is not None:
        from .final_review_contract import _validate_exact_final_cpa_self_heal
        _legacy(_validate_exact_final_cpa_self_heal, {'exact_final_cpa_self_heal': prefix},
            expected_srt_sha256='sha256:' + core.sha(initial.initial_srt), allow_pending=True)
    state = replay_journal(initial, ledger, cold_ports(configuration))
    if (not state.confirmation_ref or state.pending or core.stop_reason(state) != 'READY'
            or state.confirmation_ref != receipt['confirmation_ref']
            or state.confirmation_sha256 != output_sha
            or core.sha(core.state_bytes(state).decode()) != receipt['state_sha256']
            or type(receipt['patch_count']) is not int or receipt['patch_count'] != len(state.patches)
            or type(receipt['task_count']) is not int or receipt['task_count'] != len(state.tasks)):
        raise ClosureContractError('FINDING_CLOSURE_RELEASE_STATE_DRIFT')
    original = ledger.get(state.confirmation_ref).value()
    comparable = {k: v for k, v in obj.items() if k not in {'finding_closure', 'final_review_mode'}}
    if original != comparable:
        raise ClosureContractError('FINDING_CLOSURE_CONFIRMED_AUDIT_DRIFT')
    return state


@dataclass(frozen=True)
class ReviewerRequest:
    srt_text: str
    authority: RetainedObject
    authority_reader: Callable[[], RetainedObject]
    timeline_offset_ms: int
    source_end_ms: int
    protected_cues: frozenset[int]
    retained_configuration: RetainedObject | None = None


@dataclass(frozen=True)
class ClosureSession:
    source_sha256: str
    ports: ClosurePorts
    configuration: RetainedObject
    scan_channels: tuple[str, ...]
    advance_inputs: Callable[[ClosureState],InputUpdateResult | None] | None = None


@dataclass(frozen=True)
class PerFindingReviewer:
    prepare: Callable[[ReviewerRequest], ClosureSession]

    def __call__(self, _text: str, _authority: object, _offset: int, _end: int) -> dict[str, JsonValue]:
        raise ClosureContractError('FINDING_CLOSURE_PACKAGE_OWNER_REQUIRED')


def make_session(request: ReviewerRequest, *, owners: ExistingFindingOwners,
                 discovery: DiscoveryInputs,
                 final_gate: Callable[[ConfirmationRequest], RetainedObject]) -> ClosureSession:
    channels = registered_scan_channels()
    from .jingting_chunker import parse_srt_cues
    cues=[c for c in parse_srt_cues(request.srt_text) if c.text.strip()]
    if any(type(i) is not int or not 1<=i<=len(cues) for i in request.protected_cues):
        raise ClosureContractError('HUMAN_LITERAL_PROTECTED_CUE_INVALID')
    windows=tuple((cues[i-1].start_ms,cues[i-1].end_ms) for i in sorted(request.protected_cues))
    owners=ExistingFindingOwners(replace(owners.inputs,protected_windows=windows))
    latest_discovery=discovery
    if request.retained_configuration is not None:
        configuration = request.retained_configuration
        retained = configuration.value()
        if retained.get('scan_channels') != list(channels):
            raise ClosureContractError('FINDING_CLOSURE_SCAN_CHANNELS_CHANGED')
        old_inputs = load_owner_inputs(RetainedObject.from_value(retained['owners']), live=owners.inputs)
        if (old_inputs.source_sha256 != owners.inputs.source_sha256
                or old_inputs.contracts != owners.inputs.contracts
                or old_inputs.model_identity != owners.inputs.model_identity
                or old_inputs.clip_context != owners.inputs.clip_context
                or old_inputs.protected_windows != owners.inputs.protected_windows
                or old_inputs.candidate_id != owners.inputs.candidate_id
                or old_inputs.timeline_offset_ms != owners.inputs.timeline_offset_ms):
            raise ClosureContractError('FINDING_CLOSURE_CONFIGURATION_MATERIAL_CHANGED')
        owners = ExistingFindingOwners(old_inputs)
        discovery = load_discovery_inputs(RetainedObject.from_value(retained['discovery']), owners, live=discovery)
    else:
        configuration = RetainedObject.from_value({'owners': owners.inputs.cold_document().value(),
            'discovery': discovery_document(discovery).value(),
            'scan_channels': list(channels),
            'legacy_prefix': request.authority.value().get('exact_final_cpa_self_heal')})
    def advance(state: ClosureState) -> InputUpdateResult | None:
        from .discovery_channels import completed_postburn_delta, discovery_for_work
        prior=discovery_for_work(discovery,state.discovery_ref)
        return cast(InputUpdateResult | None,_legacy(completed_postburn_delta,latest_discovery,
            state.current_srt,previous=prior,previous_ref=state.discovery_ref,state=state))
    return ClosureSession(owners.inputs.source_sha256,
        ProductionPorts(discovery_factory(discovery), owners, final_gate),configuration,channels,advance)


@dataclass(frozen=True)
class PackageCall:
    reviewer: PerFindingReviewer
    cid: str
    out_root: Path
    subtitle_path: Path
    authority: object
    authority_path: Path
    timeline_offset_ms: int
    source_end_ms: int
    baseline: object = None
    baseline_path: Path | None = None
    before_review: Callable[[], None] | None = None
    text_recovery_only: bool = False


def run_package_closure(call: PackageCall) -> dict[str, JsonValue]:
    from .producer_package_finalization import _register_exact_final_cpa_repairs
    from .review_evidence_store import persist_document
    from .human_sequence_alignment import protected_indexes
    if not isinstance(call.authority, dict):
        raise ClosureContractError('FINDING_CLOSURE_AUTHORITY_OBJECT_REQUIRED')
    authority = cast(dict[str, JsonValue], call.authority)
    def read_authority() -> RetainedObject:
        return RetainedObject.from_value(authority)
    text = call.subtitle_path.read_bytes().decode('utf-8', errors='strict')
    if call.before_review is not None:
        call.before_review()
        if call.subtitle_path.read_bytes() != text.encode():
            raise ClosureContractError('ACOUSTIC_ROLE_REFRESH_MUTATED_SUBTITLES')
    root = call.out_root / (call.cid + '.finding-closure')
    previous: ClosureState | None = load_initial(root) if (root/'initial.json').exists() else None
    initial_text = previous.initial_srt if previous else text
    retained = FileLedger(root, previous, create=False).get(previous.configuration_ref) if previous else None
    protected = frozenset(cast(list[int], _legacy(protected_indexes,
        authority.get('human_sequence_alignment') or {}, initial_text,
        timeline_offset_ms=call.timeline_offset_ms)))
    session = call.reviewer.prepare(ReviewerRequest(initial_text, read_authority(), read_authority,
        call.timeline_offset_ms, call.source_end_ms, protected, retained))
    initial = core.initial_state(source_sha256=session.source_sha256, srt_text=initial_text,
                                  scan_channels=session.scan_channels,
                                  configuration_ref=session.configuration.sha256)
    ledger = FileLedger(root, initial)
    ledger.put(session.configuration)
    prefix = session.configuration.value().get('legacy_prefix')
    if prefix is not None:
        from .final_review_contract import _validate_exact_final_cpa_self_heal
        _legacy(_validate_exact_final_cpa_self_heal, {'exact_final_cpa_self_heal': prefix},
            expected_srt_sha256='sha256:' + core.sha(initial_text), allow_pending=True)

    def project(state: ClosureState) -> None:
        staged = read_authority().value()
        marker = staged.get('finding_closure_progress')
        done = _integer(_object(marker)['applied_patch_count']) if isinstance(marker, dict) else 0
        if not 0 <= done <= len(state.patches):
            raise ClosureContractError('FINDING_CLOSURE_AUTHORITY_PROJECTION_DRIFT')
        for patch in state.patches[done:]:
            repairs = _array(ledger.get(patch.repair_ref).value()['repairs'])
            _legacy(_register_exact_final_cpa_repairs, staged, repairs, delivery_start_ms=call.timeline_offset_ms)
        staged['finding_closure_progress'] = {'ledger_root': str(root.absolute()),
            'applied_patch_count': len(state.patches), 'state_sha256': core.sha(core.state_bytes(state).decode())}
        staged['final_output_srt_sha256'] = core.sha(state.current_srt)
        _legacy(persist_document, call.authority_path, staged)
        if isinstance(call.baseline, dict):
            baseline = RetainedObject.from_value(call.baseline).value()
            baseline['post_exact_final_cpa_output_sha256'] = core.sha(state.current_srt)
            baseline['finding_closure_progress'] = staged['finding_closure_progress']
            if call.baseline_path is not None:
                _legacy(persist_document, call.baseline_path, baseline)
            call.baseline.clear()
            call.baseline.update(baseline)
        authority.clear()
        authority.update(staged)

    state = ClosureRunner(initial=initial, ledger=ledger, ports=session.ports,
                          output=call.subtitle_path, on_projection=project,
                          text_recovery_only=call.text_recovery_only,advance_inputs=session.advance_inputs).run()
    if not state.confirmation_ref:
        audit = ledger.get(state.text_ready_ref).value()
        validate_text_ready(RetainedObject.from_value(audit), core.sha(state.current_srt))
        audit['final_review_mode'] = 'per_finding'
        audit['finding_closure'] = {'schema_version': 'finding-closure-text-ready.v1',
            'ledger_root': str(root.absolute()), 'source_sha256': state.source_sha256,
            'initial_srt_sha256': core.sha(initial_text), 'configuration_ref': state.configuration_ref,
            'state_sha256': core.sha(core.state_bytes(state).decode()),
            'confirmation_ref': '', 'text_ready_ref': state.text_ready_ref,
            'output_sha256': state.text_ready_sha256,
            'patch_count': len(state.patches), 'task_count': len(state.tasks)}
        authority['final_review_audit'] = audit
        authority['finding_closure'] = audit['finding_closure']
        _legacy(persist_document, call.out_root / (call.cid + '.review-flags.json'), audit)
        _legacy(persist_document, call.authority_path, authority)
        return audit
    audit = ledger.get(state.confirmation_ref).value()
    audit['final_review_mode'] = 'per_finding'
    audit['finding_closure'] = {'schema_version': 'finding-closure-release.v1',
        'ledger_root': str(root.absolute()), 'source_sha256': state.source_sha256,
        'initial_srt_sha256': core.sha(initial_text), 'configuration_ref': state.configuration_ref,
        'state_sha256': core.sha(core.state_bytes(state).decode()),
        'confirmation_ref': state.confirmation_ref, 'output_sha256': state.confirmation_sha256,
        'patch_count': len(state.patches), 'task_count': len(state.tasks)}
    from .final_review_contract import validate_final_review_release
    _legacy(validate_final_review_release, audit, expected_srt_sha256='sha256:' + core.sha(call.subtitle_path.read_bytes().decode()))
    authority['final_review_audit'] = audit
    authority['finding_closure'] = audit['finding_closure']
    _legacy(persist_document, call.out_root / (call.cid + '.review-flags.json'), audit)
    _legacy(persist_document, call.authority_path, authority)
    return audit


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


@contextmanager
def ruling_capture(call: object, finding: PreparedFinding | None = None) -> Iterator[RulingCapture]:
    capture=RulingCapture(finding,call if isinstance(call,FinalReviewCall) else None,[])
    token=_RULING_CAPTURE.set(capture)
    try:
        yield capture
    finally:
        _RULING_CAPTURE.reset(token)


def current_ruling_capture() -> RulingCapture | None:
    return _RULING_CAPTURE.get()


def _input_check(inputs: RetainedObject) -> RetainedObject | None:
    value=inputs.value()
    material=value.get('material')
    if isinstance(material,dict):
        value={**value,**material}
    request=value.get('request')
    if isinstance(request,dict):
        value={**value,**request}
    check=value.get('original_check_request',value.get('check_request'))
    return RetainedObject.from_value(check) if isinstance(check,dict) else None


def _input_materials(inputs: RetainedObject) -> RetainedObject | None:
    value=inputs.value()
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
    bound=_legacy(materials_for_origin,_legacy(materials_for_model,material.value(),model),origin.value())
    return ConsumedRuling(invocation.stage,check,RetainedObject.from_value(bound),invocation.inputs,
        RetainedObject.from_value(model),invocation.prompt,str(raw),origin,checkpoint)


def note_consumed_ruling(*, invocation: RulingInvocation, raw: str, identity: object,
                         retention_allowed: bool, checkpoint: RetainedObject) -> None:
    capture=_RULING_CAPTURE.get()
    if capture is None or capture.finding is None:
        return
    if not retention_allowed:
        capture.retention_declined=True
        return
    record=consumed_ruling(invocation=invocation,raw=raw,identity=identity,
                          origin=capture.finding.origin_check or capture.finding.check,checkpoint=checkpoint)
    capture.records.append(record)


def consumed_ruling_from_document(document: RetainedObject) -> ConsumedRuling:
    value=document.value()
    if value.get('schema_version')!='consumed-ruling.v1':
        raise ClosureContractError('FINDING_CLOSURE_CONSUMED_SCHEMA_INVALID')
    record=ConsumedRuling(_text(value['stage']),RetainedObject.from_value(value['check']),
        RetainedObject.from_value(value['materials']),RetainedObject.from_value(value['inputs']),
        RetainedObject.from_value(value['model_identity']),_text(value['prompt']),
        _text(value['raw_completion']),RetainedObject.from_value(value['origin_check']),
        RetainedObject.from_value(value['checkpoint']))
    from .llm_client import cached_completion
    invocation=RulingInvocation(record.stage,record.prompt,record.inputs,Path(__file__),None,'cold')
    rebuilt=consumed_ruling(invocation=invocation,
        raw=cast(str,_legacy(cached_completion,record.raw_completion,record.model_identity.value())),
        identity=record.model_identity.value(),origin=record.origin_check,checkpoint=record.checkpoint)
    if rebuilt!=record or record.document()!=document:
        raise ClosureContractError('FINDING_CLOSURE_CONSUMED_BINDING_DRIFT')
    validate_consumed_checkpoint(record)
    return record


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
        def record(receipt: AttemptReceipt) -> None:
            from .review_evidence_store import persist_document
            if receipt.raw_completion is not None and not retention(receipt.raw_completion):
                capture=_RULING_CAPTURE.get()
                if capture is not None:
                    capture.retention_declined=True
                raise ClosureContractError('FINDING_CLOSURE_RETENTION_DECLINED')
            document=RetainedObject.from_value({**asdict(receipt),'requested_model':receipt.route.model})
            _legacy(persist_document,directory/(document.sha256+'.json'),document.value())
            with lock:
                install_first_document(selection,document)
                span=RetainedObject.from_value({'schema_version':'run-time-span.v1',
                    'span_id':receipt.effect_key,'kind':'wait','name':'ruling.'+invocation.stage,
                    'started_utc':receipt.started_utc,'ended_utc':receipt.ended_utc,
                    'wait_reason':'CANONICAL_PROVIDER_OWNER','status':receipt.status,
                    'model':receipt.answering_model,'stage':invocation.stage,
                    'cache_hit':receipt.cache_hit})
                with (first.root/'finding-closure-routing'/'time-spans.jsonl').open('ab') as stream:
                    stream.write((span.json_text+'\n').encode());stream.flush();os.fsync(stream.fileno())
        executor=HedgedExecutor(controller=self.controller,record=record,retention=retention)
        # p95 comes from retained run-time-span.v1 pairs, not audio duration.
        timing=first.root/'finding-closure-routing'/'time-spans.jsonl'
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
            return RetainedObject.from_value(_legacy(_request_material_binding,cp.binding,invocation.prompt)).sha256
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


def original_ruling_bytes(value: JsonValue) -> tuple[str,...]:
    """Read actual F2 answers, excluding C's own derived consumption envelope."""
    if isinstance(value,list):
        return tuple(raw for child in value for raw in original_ruling_bytes(child))
    if not isinstance(value,dict):
        return ()
    found=tuple(child for key,child in value.items()
        if key in {'raw_completion','span_review_raw_completion','raw_response'} and isinstance(child,str))
    return (*found,*(raw for key,child in value.items()
        if key not in {'consumed_ruling','consumed_rulings','next_finding_materials','next_finding'}
        for raw in original_ruling_bytes(child)))


def attach_consumed_rulings(row: RetainedObject, owners: ExistingFindingOwners,
                            text: str) -> RetainedObject:
    capture=_RULING_CAPTURE.get()
    if capture is None:
        return row
    if capture.retention_declined:
        raise ClosureContractError('FINDING_CLOSURE_RETENTION_DECLINED')
    value=row.value()
    records=[record for record in capture.records if record.raw_completion in original_ruling_bytes(value)]
    if not records:
        return row
    original=owners.prepare_finding(text,row,origin_check=(capture.finding.origin_check or capture.finding.check)
                                   if capture.finding else None)
    value['next_finding_materials']={'check':original.check.value(),'materials':original.materials.value()}
    value['consumed_rulings']=[record.document().value() for record in capture.records]
    value['consumed_ruling']=records[-1].document().value()
    return RetainedObject.from_value(value)


def attach_f2_consumption(value: object) -> dict[str,JsonValue]:
    row=RetainedObject.from_value(value).value()
    capture=_RULING_CAPTURE.get()
    if capture is None:
        return row
    if capture.retention_declined:
        raise ClosureContractError('FINDING_CLOSURE_RETENTION_DECLINED')
    matches=[record for record in capture.records if record.raw_completion in original_ruling_bytes(row)]
    if matches:
        next_finding=RetainedObject.from_value(row)
        row['consumed_rulings']=[r.document().value() for r in capture.records]
        row['consumed_ruling']=matches[-1].document().value()
        row['next_finding']=next_finding.value()
    return row


def bind_ruling_inputs(inputs: RetainedObject, finding: PreparedFinding | None) -> RetainedObject:
    from .finding_context import materials_for_origin
    value=inputs.value()
    origin=(finding.origin_check or finding.check).value() if finding is not None else None
    def anchored(row: dict[str,JsonValue]) -> dict[str,JsonValue]:
        row=dict(row)
        if origin is not None and isinstance(row.get('finding_materials'),dict):
            row['finding_materials']=RetainedObject.from_value(_legacy(
                materials_for_origin,row['finding_materials'],origin)).value()
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
    from .completed_review_checkpoint import CompletedReviewCheckpoint, _request_material_binding
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
    binding=RetainedObject.from_value(cp.binding).value()
    provider=_object(binding['provider'])
    actual_identity=RetainedObject.from_value(_legacy(completion_identity,identity,raw)).value()
    provider['routing']=actual_identity
    runtime=provider.get('runtime')
    if isinstance(runtime,dict) and 'provider_model' in runtime:runtime['provider_model']=actual_identity['models']
    binding['provider']=provider
    actual=cast(CanonicalCheckpoint,_legacy(CompletedReviewCheckpoint,cp.root,binding,invocation.prompt))
    loaded=actual.load()
    if loaded is None or core.sha(loaded)!=core.sha(raw):
        raise ClosureContractError('FINDING_CLOSURE_CONSUMED_CHECKPOINT_MISSING')
    originals: dict[str,RetainedObject]={}
    for path in sorted([*actual.directory.glob('*.json'),*actual.directory.glob('.*.tmp')]):
        snapshot=_legacy(stable_regular_snapshot,path,label='consumed original ruling')
        payload=getattr(snapshot,'payload',None)
        if not isinstance(payload,bytes):continue
        doc=RetainedObject.from_value(json.loads(payload))
        original=doc.value()
        if original.get('response_sha256')==core.sha(raw):originals[doc.sha256]=doc
    if len(originals)!=1:
        raise ClosureContractError('FINDING_CLOSURE_CONSUMED_CHECKPOINT_AMBIGUOUS')
    original=next(iter(originals.values())).value()
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
        origin=origin,checkpoint=RetainedObject.from_value({'root':str(cp.root),'original':original}))
    if capture is not None and capture.finding is not None:capture.records.append(record)
    return record


def validate_consumed_checkpoint(record: ConsumedRuling) -> None:
    from .completed_review_checkpoint import CompletedReviewCheckpoint
    data=record.checkpoint.value()
    root=Path(_text(data['root']))
    original=_object(data['original'])
    binding=_object(original['binding'])
    if (_text(original['request'])!=record.prompt or original['response']!=record.raw_completion
            or original.get('response_sha256')!=core.sha(record.raw_completion)
            or RetainedObject.from_value(binding['inputs'])!=record.inputs):
        raise ClosureContractError('FINDING_CLOSURE_CONSUMED_CHECKPOINT_DRIFT')
    cp=cast(CanonicalCheckpoint,_legacy(CompletedReviewCheckpoint,root,binding,record.prompt))
    actual=cp.load()
    if (actual is None or actual!=record.raw_completion or
            _object(_object(binding['provider'])['routing'])!=record.model_identity.value()):
        raise ClosureContractError('FINDING_CLOSURE_CONSUMED_ORIGINAL_REJECTED')
    validate_ruling_materials(record)


def validate_ruling_materials(record: ConsumedRuling) -> None:
    """Rebuild actual stage materials from its inputs, never a returned row."""
    from .finding_context import materials_for_origin, decision_materials
    value=record.inputs.value()
    material: object
    if record.stage=='semantic-terminal':
        from .semantic_terminal_adjudication import terminal_finding_materials
        material=_legacy(terminal_finding_materials,_object(value['request']),record.model_identity.value())
    elif record.stage=='word-choice':
        from .acoustic_witness_adjudication import _semantic_fingerprint_payload,WordChoiceDependencies
        kwargs=dict(_object(value['consumption_input']))
        dependency=_object(kwargs['dependencies'])
        dependencies=_legacy(WordChoiceDependencies,_text(dependency['terminology']),
            tuple(_text(v) for v in _array(dependency['registered_name_decisions'])),
            _text(dependency['caption_audio_roles']),
            json.dumps(dependency['candidate_pinyin_similarities'],ensure_ascii=False,sort_keys=True),
            _text(dependency['consistency_context']))
        payload=RetainedObject.from_value(_legacy(_semantic_fingerprint_payload,
            check_request=kwargs['check_request'],witness=kwargs['witness'],
            context_before=kwargs['context_before'],context_after=kwargs['context_after'],
            structured_chat_context=kwargs['structured_chat_context'],decision_contract=kwargs['decision_contract'],
            allowed_choices=set(_text(v) for v in _array(kwargs['allowed_choices'])),
            candidates=kwargs['candidates'],require_complete_support=kwargs['require_complete_support'],
            policy_version=kwargs['policy_version'],model_identity=record.model_identity.value(),
            dependencies=dependencies)).value()
        material=payload['finding_materials']
    elif record.stage=='proposal-rebuild':
        from .closed_set_proposal_rebuild import (_PROPOSAL_REBUILD_SCHEMA,
            _PROPOSAL_REBUILD_PROMPT,TOPIC_PHONEME_POLICY)
        material=decision_materials(record.check.value(),data=value['consumption_input'],
            contracts={'proposal_schema':_PROPOSAL_REBUILD_SCHEMA,'instructions':_PROPOSAL_REBUILD_PROMPT,
                       'policy':TOPIC_PHONEME_POLICY},model_identity=record.model_identity.value())
    elif record.stage=='editorial-rendering':
        from .editorial_subtitle import editorial_model_data,SCHEMA,PROMPT,OMISSION_PROMPT,REFERENCE_PROMPT
        from .editorial_edit_contract import edit_contract
        request=_object(value['request'])
        material=decision_materials(record.check.value(),data=_legacy(editorial_model_data,request),
            contracts={'editorial_schema':SCHEMA,'instructions':PROMPT+OMISSION_PROMPT+REFERENCE_PROMPT,
                       'edit_contract':_legacy(edit_contract,request['current_cue'])},
            model_identity=record.model_identity.value())
    else:
        raise ClosureContractError('FINDING_CLOSURE_UNKNOWN_RULING_CONSUMER')
    rebuilt=RetainedObject.from_value(materials_for_origin(material,record.origin_check.value()))
    if rebuilt!=record.materials or _input_check(record.inputs)!=record.check:
        raise ClosureContractError('FINDING_CLOSURE_RULING_MATERIAL_CONSUMER_REJECTED')


def validate_ruling_projection(document: RetainedObject, inputs: RetainedObject) -> ConsumedRuling:
    """Compare a current question with an independently cold-validated old R."""
    from .llm_client import CpaCompletion
    record=consumed_ruling_from_document(document)
    models=_array(record.model_identity.value()['models'])
    raw=cast(str,_legacy(CpaCompletion,record.raw_completion,_text(models[0])))
    current=consumed_ruling(invocation=RulingInvocation(record.stage,record.prompt,inputs,Path(__file__),None,''),
        raw=raw,identity=record.model_identity.value(),origin=record.origin_check,checkpoint=record.checkpoint)
    validate_ruling_materials(current)
    if current.materials!=record.materials:
        raise ClosureContractError('FINDING_CLOSURE_RULING_PROJECTION_MATERIAL_DRIFT')
    return current


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


@contextmanager
def provider_permission(allowed: bool) -> Iterator[None]:
    token=_PROVIDER_CALLS_ALLOWED.set(_PROVIDER_CALLS_ALLOWED.get() and allowed)
    try:yield
    finally:_PROVIDER_CALLS_ALLOWED.reset(token)


def ensure_provider_calls_allowed() -> None:
    if not _PROVIDER_CALLS_ALLOWED.get():
        raise ClosureContractError('FINDING_CLOSURE_COLD_REPLAY_FORBIDS_CALL')


class CacheOnlyVerifier:
    """Use only the existing owner's explicit local-cache read entry points."""
    argon_escalation: None = None
    def __init__(self, owner: object) -> None:self.owner=owner
    def __call__(self, request: object) -> object:
        probe=getattr(self.owner,'probe_witness_cache',None)
        if not callable(probe):raise ClosureContractError('FINDING_CLOSURE_COLD_CACHE_OWNER_REQUIRED')
        return _legacy(probe,request)
    def observe_range_context(self, **kwargs: object) -> object:
        read=getattr(self.owner,'observe_range_context',None)
        if not callable(read):raise ClosureContractError('FINDING_CLOSURE_COLD_CACHE_OWNER_REQUIRED')
        return _legacy(read,**{**kwargs,'cache_only':True})
    def __getattr__(self, name: str) -> object:
        value=getattr(self.owner,name,None)
        if callable(value) and name not in {'probe_witness_cache','relisten_budget_state','span_termination_source_identity'}:
            def forbidden(*_args: object,**_kwargs: object) -> object:
                raise ClosureContractError('FINDING_CLOSURE_COLD_REPLAY_FORBIDS_CALL')
            return forbidden
        return value
