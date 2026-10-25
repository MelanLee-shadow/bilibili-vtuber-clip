"""Deterministic per-question workflow. No clock, filesystem or provider I/O.

Receipts are content-addressed references. Their authority is checked by the
shell's original consumers, both before an event and during cold replay.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
from typing import Literal, TypeAlias

from .finding_lifecycle import State as State, TERMINAL_STATES as TERMINAL_STATES, TRANSITIONS, ContractRejection
from .jingting_chunker import parse_srt_cues

SCHEMA = "finding-closure.v1"
WorkKind: TypeAlias = Literal["initial_scan", "targeted_scan", "final_scan", "prefetch",
                              "evidence", "decision", "task_stop", "patch", "confirm", "revalidate", "input_update"]


class ClosureContractError(ContractRejection):
    """A declared workflow contract rejection, never an inferred program bug."""
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code.split(':',1)[0]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def valid_sha(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


@dataclass(frozen=True)
class FindingKey:
    physical: str
    materials: str


@dataclass(frozen=True)
class CueVersion:
    cue: int
    text_sha256: str


@dataclass(frozen=True)
class TaskSpec:
    key: FindingKey
    scope_cues: tuple[int, ...]
    local_text_sha256: str
    finding_ref: str
    check_ref: str
    materials_ref: str
    origin_check_ref: str = ""
    presence: Literal['PRESENT','ABSENT'] = 'PRESENT'
    projection_ref: str = ''
    applied_ruling_ref: str = ''


@dataclass(frozen=True)
class Task:
    spec: TaskSpec
    state: State = State.AWAITING_EVIDENCE
    ruling_ref: str = ""
    nomination: TaskSpec | None = None


@dataclass(frozen=True)
class ClosedFinding:
    key: FindingKey
    ruling_ref: str


@dataclass(frozen=True)
class Work:
    work_id: str
    kind: WorkKind
    text_sha256: str
    task: TaskSpec | None = None
    scope_cues: tuple[int, ...] = ()
    closed_ledger: tuple[ClosedFinding, ...] = ()
    scan_channels: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    consumer_revision_ref: str = ""
    discovery_ref: str = ""

    @property
    def scan_n(self) -> int:
        return len(self.scan_channels)


@dataclass(frozen=True)
class Scan:
    work_id: str
    kind: WorkKind
    text_sha256: str
    coverage: tuple[CueVersion, ...]
    lane_refs: tuple[str, ...]
    physical_ids: tuple[str, ...]
    discovery_ref: str = ""


@dataclass(frozen=True)
class Ruling:
    work_id: str
    key: FindingKey
    state: State
    receipt_ref: str


@dataclass(frozen=True)
class Patch:
    work_id: str
    key: FindingKey
    ruling_ref: str
    before_sha256: str
    after_sha256: str
    start: int
    before: str
    after: str
    repair_ref: str


@dataclass(frozen=True)
class ClosureState:
    source_sha256: str
    initial_srt: str
    current_srt: str
    scan_channels: tuple[str, ...]
    tasks: tuple[Task, ...] = ()
    scans: tuple[Scan, ...] = ()
    rulings: tuple[Ruling, ...] = ()
    patches: tuple[Patch, ...] = ()
    pending: tuple[Work, ...] = ()
    completed: tuple[str, ...] = ()
    confirmation_ref: str = ""
    confirmation_sha256: str = ""
    text_ready_ref: str = ""
    text_ready_sha256: str = ""
    started: bool = False
    schema_version: str = SCHEMA
    configuration_ref: str = ""
    prefetch_finished: bool = False
    prefetch_refs: tuple[str, ...] = ()
    prefetch_work_id: str = ""
    discovery_ref: str = ""

    @property
    def scan_n(self) -> int:
        return len(self.scan_channels)


@dataclass(frozen=True)
class Start:
    kind: Literal["start"] = "start"


@dataclass(frozen=True)
class ScanCompleted:
    work_id: str
    findings: tuple[TaskSpec, ...]
    coverage: tuple[CueVersion, ...]
    lane_refs: tuple[str, ...]
    rebased: tuple[TaskSpec, ...] = ()
    kind: Literal["scan_completed"] = "scan_completed"


@dataclass(frozen=True)
class ReviewCompleted:
    work_id: str
    spec: TaskSpec
    state: State
    receipt_ref: str
    kind: Literal["review_completed"] = "review_completed"


@dataclass(frozen=True)
class PatchCompleted:
    work_id: str
    patch: Patch
    findings: tuple[TaskSpec, ...]
    kind: Literal["patch_completed"] = "patch_completed"


@dataclass(frozen=True)
class Confirmed:
    work_id: str
    output_sha256: str
    receipt_ref: str
    kind: Literal["confirmed"] = "confirmed"


@dataclass(frozen=True)
class TextReady:
    work_id: str
    output_sha256: str
    receipt_ref: str
    kind: Literal["text_ready"] = "text_ready"


@dataclass(frozen=True)
class InputsAdvanced:
    work_id: str
    receipt_ref: str
    findings: tuple[TaskSpec, ...]
    kind: Literal['inputs_advanced'] = 'inputs_advanced'


@dataclass(frozen=True)
class WorkFailed:
    work_id: str
    receipt_ref: str
    state: State = State.CALL_FAILURE
    kind: Literal["work_failed"] = "work_failed"


@dataclass(frozen=True)
class RevalidateRequested:
    work_id: str
    physical_id: str
    consumer_revision_ref: str
    kind: Literal["revalidate_requested"] = "revalidate_requested"


@dataclass(frozen=True)
class PrefetchCompleted:
    work_id: str
    receipt_refs: tuple[str, ...]
    kind: Literal["prefetch_completed"] = "prefetch_completed"


Event: TypeAlias = Start | ScanCompleted | ReviewCompleted | PatchCompleted | Confirmed | TextReady | InputsAdvanced | WorkFailed | RevalidateRequested | PrefetchCompleted


def state_bytes(state: ClosureState) -> bytes:
    return (json.dumps(asdict(state), ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def cue_versions(text: str) -> tuple[CueVersion, ...]:
    return tuple(CueVersion(i, sha(c.text)) for i, c in enumerate(
        (cue for cue in parse_srt_cues(text) if cue.text.strip()), 1))


def local_text_sha(text: str, scope: tuple[int, ...]) -> str:
    cues = [c for c in parse_srt_cues(text) if c.text.strip()]
    if (type(scope) is not tuple or not scope or any(type(i) is not int for i in scope)
            or scope != tuple(sorted(set(scope))) or any(i < 1 or i > len(cues) for i in scope)):
        raise ClosureContractError("FINDING_CLOSURE_SCOPE_INVALID")
    return sha(json.dumps([(i, cues[i-1].start_ms, cues[i-1].end_ms, cues[i-1].text)
                           for i in scope], ensure_ascii=False, separators=(",", ":")))


def validate_spec(spec: TaskSpec, text: str) -> None:
    if (not all(valid_sha(x) for x in (spec.key.physical, spec.key.materials,
            spec.finding_ref, spec.check_ref, spec.materials_ref))
            or spec.origin_check_ref and not valid_sha(spec.origin_check_ref)
            or spec.local_text_sha256 != local_text_sha(text, spec.scope_cues)
            or spec.presence not in {'PRESENT','ABSENT'}
            or spec.presence=='ABSENT' and (not valid_sha(spec.projection_ref)
                or not valid_sha(spec.origin_check_ref) or not valid_sha(spec.applied_ruling_ref))
            or spec.projection_ref and not valid_sha(spec.projection_ref)):
        raise ClosureContractError("FINDING_CLOSURE_MATERIAL_DRIFT")


def initial_state(*, source_sha256: str, srt_text: str, scan_channels: tuple[str, ...],
                  configuration_ref: str = "") -> ClosureState:
    if (not valid_sha(source_sha256) or not cue_versions(srt_text)
            or type(scan_channels) is not tuple or not scan_channels
            or any(not isinstance(name,str) or not name for name in scan_channels)
            or len(set(scan_channels)) != len(scan_channels)):
        raise ClosureContractError("FINDING_CLOSURE_INPUT_INVALID")
    if configuration_ref and not valid_sha(configuration_ref):
        raise ClosureContractError("FINDING_CLOSURE_CONFIGURATION_INVALID")
    return ClosureState(source_sha256, srt_text, srt_text, scan_channels, configuration_ref=configuration_ref)


def apply_patch(text: str, patch: Patch) -> str:
    if (sha(text) != patch.before_sha256 or type(patch.start) is not int or not 0 <= patch.start <= len(text)
            or patch.before == patch.after
            or text[patch.start:patch.start + len(patch.before)] != patch.before
            or not valid_sha(patch.repair_ref)):
        raise ClosureContractError("FINDING_CLOSURE_PATCH_BINDING_DRIFT")
    output = text[:patch.start] + patch.after + text[patch.start + len(patch.before):]
    if sha(output) != patch.after_sha256 or not cue_versions(output):
        raise ClosureContractError("FINDING_CLOSURE_PATCH_OUTPUT_DRIFT")
    return output


def replay_patches(state: ClosureState) -> str:
    text = state.initial_srt
    for patch in state.patches:
        text = apply_patch(text, patch)
    if text != state.current_srt:
        raise ClosureContractError("FINDING_CLOSURE_PATCH_REPLAY_DRIFT")
    return text


def text_at(state: ClosureState, text_sha256: str) -> str:
    text = state.initial_srt
    if sha(text) == text_sha256:
        return text
    for patch in state.patches:
        text = apply_patch(text, patch)
        if sha(text) == text_sha256:
            return text
    raise ClosureContractError("FINDING_CLOSURE_TEXT_VERSION_UNKNOWN")


def closed_ledger(state: ClosureState) -> tuple[ClosedFinding, ...]:
    return tuple(ClosedFinding(t.spec.key, t.ruling_ref) for t in state.tasks if task_closed(t))


def task_closed(task: Task) -> bool:
    return (task.spec.presence=='PRESENT' and task.state==State.KEEP) or (task.spec.presence=='ABSENT'
        and task.state==State.EDIT_REVIEW and valid_sha(task.spec.projection_ref)
        and valid_sha(task.ruling_ref) and task.ruling_ref==task.spec.applied_ruling_ref)


def local_content_sha(text: str, scope: tuple[int,...]) -> str:
    local_text_sha(text,scope)
    cues=[c for c in parse_srt_cues(text) if c.text.strip()]
    return sha(json.dumps([(cues[i-1].start_ms,cues[i-1].end_ms,cues[i-1].text) for i in scope],
                         ensure_ascii=False,separators=(',',':')))


def uncovered(state: ClosureState) -> tuple[int, ...]:
    covered = {c for scan in state.scans for c in scan.coverage}
    return tuple(c.cue for c in cue_versions(state.current_srt) if c not in covered)


def stop_reason(state: ClosureState) -> str:
    if any(t.state in {State.CONTRACT_ERROR, State.CALL_FAILURE} for t in state.tasks):
        return "FINAL_REVIEW_ENGINEERING_FAILURE"
    if any(t.state == State.TRUE_BLOCK for t in state.tasks):
        return "FINAL_REVIEW_TRUE_CONTENT_BLOCK"
    if any(not task_closed(t) for t in state.tasks):
        return "FINAL_REVIEW_FINDING_REVIEW_PENDING"
    if not state.prefetch_finished:
        return "FINDING_CLOSURE_PREFETCH_PENDING"
    if not any(s.kind == "initial_scan" for s in state.scans) or uncovered(state):
        return "FINDING_CLOSURE_COVERAGE_INCOMPLETE"
    if not any(s.kind == "final_scan" and s.text_sha256 == sha(state.current_srt)
               and s.discovery_ref == state.discovery_ref for s in state.scans):
        return "FINDING_CLOSURE_FINAL_SCAN_REQUIRED"
    return "READY"


def _work(kind: WorkKind, state: ClosureState, *, task: TaskSpec | None = None,
          scope: tuple[int, ...] = ()) -> Work:
    closed = closed_ledger(state) if kind in {'targeted_scan','final_scan'} else ()
    # Task effects bind local M, not the unrelated whole-film version. Scan
    # and patch effects necessarily bind the exact text being scanned/applied.
    identity = (kind, state.source_sha256, asdict(task.key) if task else None,
                sha(state.current_srt) if kind not in {"evidence", "decision", "task_stop", "prefetch"} else None,
                scope, [asdict(c) for c in closed],
                state.scan_channels if kind in {'initial_scan','targeted_scan','final_scan'} else (),
                state.discovery_ref if kind in {'initial_scan','targeted_scan','final_scan','confirm'} else '')
    dependencies = {p.work_id for p in state.patches}
    if state.prefetch_finished and kind != 'prefetch':
        dependencies.add(state.prefetch_work_id)
    if task is not None:
        dependencies.update(s.work_id for s in state.scans if task.key.physical in s.physical_ids)
        dependencies.update(r.work_id for r in state.rulings if r.key == task.key)
    if kind in {"final_scan", "confirm"}:
        dependencies.update(s.work_id for s in state.scans)
        dependencies.update(r.work_id for r in state.rulings if r.key in {t.spec.key for t in state.tasks})
    return Work(sha(json.dumps(identity, sort_keys=True, separators=(",", ":"))), kind,
                sha(state.current_srt), task, scope, closed, state.scan_channels, tuple(sorted(dependencies)),
                discovery_ref=state.discovery_ref)


def _schedule(state: ClosureState) -> tuple[ClosureState, tuple[Work, ...]]:
    effects: list[Work] = []
    occupied = set(state.completed) | {w.work_id for w in state.pending}
    def offer(work: Work) -> None:
        same_task_pending = work.task is not None and any(
            w.kind == work.kind and w.task is not None and w.task.key == work.task.key for w in state.pending)
        same_scan_pending=work.kind in {'initial_scan','targeted_scan','final_scan'} and any(
            (w.kind,w.text_sha256,w.scope_cues,w.scan_channels)==
            (work.kind,work.text_sha256,work.scope_cues,work.scan_channels) for w in state.pending)
        if work.work_id not in occupied and not same_task_pending and not same_scan_pending:
            effects.append(work)
            occupied.add(work.work_id)
    if state.confirmation_ref or not state.started:
        return state, ()
    if not state.prefetch_finished:
        offer(_work("prefetch",state))
    if not any(s.kind == "initial_scan" for s in state.scans):
        offer(_work("initial_scan", state, scope=tuple(c.cue for c in cue_versions(state.current_srt))))
    else:
        for task in state.tasks:
            if task.spec.presence=='ABSENT':
                continue
            kind: WorkKind | None = None
            if task.state == State.AWAITING_EVIDENCE:
                kind = "evidence"
            elif task.state == State.AWAITING_DECISION:
                kind = "decision"
            elif task.state == State.AWAITING_TERMINAL:
                kind = "task_stop"
            elif task.state == State.EDIT_REVIEW:
                kind = "patch"
            if kind is not None:
                offer(_work(kind, state, task=task.spec))
        missing = uncovered(state)
        if missing:
            offer(_work("targeted_scan", state, scope=missing))
        elif all(task_closed(t) for t in state.tasks) and not state.pending:
            if stop_reason(state) == "READY":
                offer(_work("confirm", state))
            else:
                offer(_work("final_scan", state, scope=tuple(c.cue for c in cue_versions(state.current_srt))))
    return replace(state, pending=(*state.pending, *effects)), tuple(effects)


def _replace_task(state: ClosureState, task: Task) -> ClosureState:
    return replace(state, tasks=tuple(task if t.spec.key.physical == task.spec.key.physical else t
                                      for t in state.tasks))


def revalidation_id(state: ClosureState, task: Task, revision: str) -> str:
    previous = next((r.work_id for r in reversed(state.rulings) if r.key == task.spec.key), "")
    # Read-only revalidation attempts form a causal chain. This orchestration
    # identity never enters M or an external call's idempotency key.
    return sha("revalidate:" + task.spec.key.physical + ':' + task.spec.key.materials + ':' + revision + ':' + previous)


def revalidation_work(state: ClosureState, event: RevalidateRequested) -> Work:
    task = next((t for t in state.tasks if t.spec.key.physical == event.physical_id), None)
    if (task is None or task.state not in {State.CONTRACT_ERROR, State.CALL_FAILURE}
            or not valid_sha(event.consumer_revision_ref)):
        raise ClosureContractError("FINDING_CLOSURE_REVALIDATION_SCOPE_INVALID")
    expected = revalidation_id(state, task, event.consumer_revision_ref)
    if event.work_id != expected:
        raise ClosureContractError("FINDING_CLOSURE_REVALIDATION_KEY_DRIFT")
    return Work(expected, "revalidate", sha(state.current_srt), task.spec,
                dependencies=tuple(r.work_id for r in state.rulings if r.key == task.spec.key),
                consumer_revision_ref=event.consumer_revision_ref)


def work_is_current(state: ClosureState, work: Work) -> bool:
    if work.kind == 'prefetch':
        return True  # The candidate-blind source audio does not change on edit.
    if work.task is None:
        return work.text_sha256 == sha(state.current_srt)
    return any(t.spec.key == work.task.key for t in state.tasks)


def step(state: ClosureState, event: Event) -> tuple[ClosureState, tuple[Work, ...]]:
    """Accept one validated event and describe new effects; never execute one."""
    if isinstance(event, Start):
        return _schedule(replace(state, started=True))
    if isinstance(event, InputsAdvanced):
        if (not valid_sha(event.receipt_ref) or event.receipt_ref == state.discovery_ref
                or event.work_id != sha('discovery-update:' + event.receipt_ref)
                or state.pending or not state.confirmation_ref or type(event.findings) is not tuple):
            raise ClosureContractError('FINDING_CLOSURE_INPUT_UPDATE_BINDING_DRIFT')
        tasks = {t.spec.key.physical:t for t in state.tasks}
        if len({s.key.physical for s in event.findings}) != len(event.findings):
            raise ClosureContractError('FINDING_CLOSURE_INPUT_UPDATE_DUPLICATE_P')
        for spec in event.findings:
            validate_spec(spec,state.current_srt)
            if spec.presence != 'PRESENT':
                raise ClosureContractError('FINDING_CLOSURE_INPUT_UPDATE_CANNOT_RETIRE_QUESTION')
            old=tasks.get(spec.key.physical)
            if old is None or (old.nomination or old.spec).key != spec.key:
                tasks[spec.key.physical]=Task(spec)
        return _schedule(replace(state,tasks=tuple(tasks[p] for p in sorted(tasks)),
            discovery_ref=event.receipt_ref,confirmation_ref='',confirmation_sha256='',
            text_ready_ref='',text_ready_sha256='',completed=(*state.completed,event.work_id)))
    if isinstance(event, RevalidateRequested):
        requested_work = revalidation_work(state, event)
        if event.work_id in state.completed or any(w.work_id == event.work_id for w in state.pending):
            raise ClosureContractError("FINDING_CLOSURE_REVALIDATION_ALREADY_RECORDED")
        return replace(state, pending=(*state.pending, requested_work)), (requested_work,)
    if isinstance(event, (ScanCompleted, PatchCompleted)) and type(event.findings) is not tuple:
        raise ClosureContractError("FINDING_CLOSURE_EVENT_MUTABLE_COLLECTION")
    if isinstance(event, ScanCompleted) and (
            any(type(v) is not tuple for v in (event.coverage, event.lane_refs, event.rebased))
            or any(type(c.cue) is not int for c in event.coverage)):
        raise ClosureContractError("FINDING_CLOSURE_EVENT_MUTABLE_COLLECTION")
    if isinstance(event, (ReviewCompleted, WorkFailed)) and not isinstance(event.state, State):
        raise ClosureContractError("FINDING_LIFECYCLE_UNKNOWN_STATE")
    work = next((w for w in state.pending if w.work_id == event.work_id), None)
    if work is None:
        raise ClosureContractError("FINDING_CLOSURE_EVENT_WITHOUT_PENDING_WORK")
    if isinstance(event, TextReady):
        if (work.kind != "confirm" or stop_reason(state) != "READY" or state.pending != (work,)
                or event.output_sha256 != sha(replay_patches(state)) or not valid_sha(event.receipt_ref)
                or state.confirmation_ref):
            raise ClosureContractError("FINDING_CLOSURE_TEXT_READY_BINDING_DRIFT")
        # Text recovery is an intermediate handoff. Keep confirmation pending
        # so the later source reviewer must still satisfy the original gate.
        return replace(state, text_ready_ref=event.receipt_ref,
                       text_ready_sha256=event.output_sha256), ()
    following = replace(state, pending=tuple(w for w in state.pending if w != work),
                        completed=(*state.completed, work.work_id))
    current = next((t for t in state.tasks if work.task and t.spec.key.physical == work.task.key.physical), None)
    if isinstance(event, PrefetchCompleted):
        if (work.kind != 'prefetch' or type(event.receipt_refs) is not tuple or not event.receipt_refs
                or len(set(event.receipt_refs)) != len(event.receipt_refs)
                or not all(valid_sha(ref) for ref in event.receipt_refs)):
            raise ClosureContractError('FINDING_CLOSURE_PREFETCH_BINDING_DRIFT')
        following = replace(following,prefetch_finished=True,prefetch_refs=event.receipt_refs,
                            prefetch_work_id=work.work_id)
    elif isinstance(event, WorkFailed):
        if event.state not in {State.CALL_FAILURE, State.CONTRACT_ERROR} or not valid_sha(event.receipt_ref):
            raise ClosureContractError("FINDING_CLOSURE_FAILURE_INVALID")
        if not work_is_current(state,work):
            if work.task is not None:
                following = replace(following, rulings=(*state.rulings,
                    Ruling(work.work_id,work.task.key,event.state,event.receipt_ref)))
            return _schedule(following)
        if current is None:
            # Failed scans/confirmation remain explicit pending engineering work.
            return replace(state, completed=state.completed), ()
        following = _replace_task(following, replace(current, state=event.state, ruling_ref=event.receipt_ref))
        following = replace(following, rulings=(*state.rulings,
            Ruling(work.work_id, current.spec.key, event.state, event.receipt_ref)))
    elif isinstance(event, ScanCompleted):
        if work.kind not in {"initial_scan", "targeted_scan", "final_scan"}:
            raise ClosureContractError("FINDING_CLOSURE_EVENT_KIND_DRIFT")
        text = text_at(state, work.text_sha256)
        expected = tuple(c for c in cue_versions(text) if c.cue in work.scope_cues)
        if (event.coverage != expected or len(event.lane_refs) != work.scan_n
                or len(set(event.lane_refs)) != len(event.lane_refs)
                or not all(valid_sha(x) for x in event.lane_refs)):
            raise ClosureContractError("FINDING_CLOSURE_SCAN_BINDING_DRIFT")
        tasks = {t.spec.key.physical: t for t in state.tasks}
        rebase = {s.key.physical: s for s in event.rebased}
        if len(rebase) != len(event.rebased) or (rebase and set(rebase) != {s.key.physical for s in event.findings}):
            raise ClosureContractError("FINDING_CLOSURE_SCAN_REBASE_SET_DRIFT")
        seen: set[str] = set()
        for spec in event.findings:
            validate_spec(spec, text)
            if spec.presence!='PRESENT':
                raise ClosureContractError('FINDING_CLOSURE_SCAN_CANNOT_RETIRE_QUESTION')
            if work.kind == 'final_scan' and spec.key.physical in {c.key.physical for c in work.closed_ledger}:
                raise ClosureContractError('FINDING_CLOSURE_FINAL_SCAN_REOPENS_CLOSED_P')
            if spec.key.physical in seen:
                raise ClosureContractError("FINDING_CLOSURE_SCAN_DUPLICATE_QUESTION")
            seen.add(spec.key.physical)
            if work.kind == "targeted_scan" and not set(spec.scope_cues).intersection(work.scope_cues):
                raise ClosureContractError("FINDING_CLOSURE_SCAN_OUTSIDE_SCOPE")
            old = tasks.get(spec.key.physical)
            if sha(state.current_srt) != work.text_sha256:
                if spec.key.physical not in rebase:
                    raise ClosureContractError("FINDING_CLOSURE_STALE_SCAN_REQUIRES_REBASE")
                spec = rebase[spec.key.physical]
                validate_spec(spec, state.current_srt)
            if old is None:
                tasks[spec.key.physical] = (Task(spec,State.EDIT_REVIEW,spec.applied_ruling_ref)
                                           if spec.presence=='ABSENT' else Task(spec))
            elif task_closed(old):
                # A new discovery summary is not the actual ruling M. Only
                # dependency rebase reopens a previously closed question.
                continue
            elif (old.nomination or old.spec).key != spec.key:
                tasks[spec.key.physical] = (Task(spec,State.EDIT_REVIEW,spec.applied_ruling_ref)
                                           if spec.presence=='ABSENT' else Task(spec))
        scan = Scan(work.work_id, work.kind, work.text_sha256, event.coverage,
                    event.lane_refs, tuple(sorted({s.key.physical for s in event.findings})),work.discovery_ref)
        following = replace(following, tasks=tuple(tasks[p] for p in sorted(tasks)), scans=(*state.scans, scan))
    elif isinstance(event, ReviewCompleted):
        if work.kind not in {"evidence", "decision", "task_stop", "revalidate"} or current is None or work.task is None:
            raise ClosureContractError("FINDING_CLOSURE_EVENT_KIND_DRIFT")
        validate_spec(event.spec, text_at(state, work.text_sha256))
        if event.spec.presence!='PRESENT':
            raise ClosureContractError('FINDING_CLOSURE_ABSENT_TARGET_CANNOT_BE_ASKED')
        if event.spec.key.physical != work.task.key.physical or not valid_sha(event.receipt_ref):
            raise ClosureContractError("FINDING_CLOSURE_RULING_BINDING_DRIFT")
        if current.spec.key != work.task.key:
            # An in-flight result for changed local M is retained in the ruling
            # ledger, and cannot overwrite the rebased task.
            following = replace(following, rulings=(*state.rulings,
                Ruling(work.work_id, event.spec.key, event.state, event.receipt_ref)))
            return _schedule(following)
        selected = event.state
        if current.state != selected and selected not in TRANSITIONS[current.state]:
            raise ClosureContractError("FINDING_LIFECYCLE_TRANSITION_INVALID")
        if selected == current.state and selected not in TERMINAL_STATES:
            raise ClosureContractError("FINDING_CLOSURE_NO_NEW_MATERIAL_OR_DECISION")
        prior = [r for r in state.rulings if r.key == event.spec.key
                 and r.state in {State.KEEP, State.EDIT_REVIEW, State.TRUE_BLOCK}]
        if prior and (prior[-1].state != selected or prior[-1].receipt_ref != event.receipt_ref):
            raise ClosureContractError("FINDING_CLOSURE_RULING_CONFLICT")
        following = _replace_task(following, Task(event.spec, selected, event.receipt_ref,
                                                 current.nomination or current.spec))
        following = replace(following, rulings=(*state.rulings,
            Ruling(work.work_id, event.spec.key, selected, event.receipt_ref)))
    elif isinstance(event, PatchCompleted):
        patch = event.patch
        if (work.kind != "patch" or current is None or current.state != State.EDIT_REVIEW
                or patch.work_id != work.work_id or patch.key != current.spec.key
                or patch.ruling_ref != current.ruling_ref):
            raise ClosureContractError("FINDING_CLOSURE_PATCH_AUTHORITY_DRIFT")
        text = apply_patch(state.current_srt, patch)
        if patch.after_sha256 in {sha(state.initial_srt), *(p.after_sha256 for p in state.patches)}:
            raise ClosureContractError("FINDING_CLOSURE_PATCH_CYCLE")
        specs = {s.key.physical: s for s in event.findings}
        if len(specs) != len(event.findings) or set(specs) != {t.spec.key.physical for t in state.tasks}:
            raise ClosureContractError("FINDING_CLOSURE_PATCH_TASK_SET_DRIFT")
        rebuilt: list[Task] = []
        for task in state.tasks:
            spec = specs[task.spec.key.physical]
            validate_spec(spec, text)
            changed = task.spec.key != spec.key
            if (not changed and local_content_sha(state.current_srt,task.spec.scope_cues)
                    !=local_content_sha(text,spec.scope_cues)):
                raise ClosureContractError("FINDING_CLOSURE_PATCH_MATERIAL_NOT_REBOUND")
            if task == current and not changed:
                raise ClosureContractError("FINDING_CLOSURE_EDIT_MATERIAL_NOT_REBOUND")
            if spec.presence=='ABSENT':
                if task.spec.presence!='ABSENT' and spec.applied_ruling_ref!=patch.ruling_ref:
                    raise ClosureContractError('FINDING_CLOSURE_REMOVAL_RULING_DRIFT')
                rebuilt.append(Task(spec,State.EDIT_REVIEW,
                    task.ruling_ref if task.spec.presence=='ABSENT' else patch.ruling_ref))
            else:
                rebuilt.append(Task(spec, State.AWAITING_EVIDENCE) if changed else replace(task, spec=spec))
        following = replace(following, current_srt=text, tasks=tuple(rebuilt),
                            patches=(*state.patches, patch), confirmation_ref="", confirmation_sha256="",
                            text_ready_ref="", text_ready_sha256="")
        live_keys = {t.spec.key for t in following.tasks}
        # A patch for an obsolete M has not been submitted to any call owner.
        # Discard its local projection work; preserve in-flight call outcomes.
        following = replace(following, pending=tuple(w for w in following.pending
            if not (w.kind == "patch" and w.task is not None and w.task.key not in live_keys)))
    elif isinstance(event, Confirmed):
        if (work.kind != "confirm" or stop_reason(state) != "READY" or state.pending != (work,)
                or event.output_sha256 != sha(replay_patches(state)) or not valid_sha(event.receipt_ref)):
            raise ClosureContractError("FINDING_CLOSURE_CONFIRMATION_BINDING_DRIFT")
        following = replace(following, confirmation_ref=event.receipt_ref,
                            confirmation_sha256=event.output_sha256, text_ready_ref="", text_ready_sha256="")
    return _schedule(following)


def replay(initial: ClosureState, events: tuple[Event, ...]) -> ClosureState:
    state = initial
    for event in events:
        state, _ = step(state, event)
    replay_patches(state)
    return state
