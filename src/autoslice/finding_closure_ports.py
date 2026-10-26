"""Frozen ports for discovery owners, F2 owners and recorded-answer replay.

Legacy JSON is confined to RetainedObject, with one lossless conversion. Task,
event and patch records use the same types in every producer and consumer.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Callable, Protocol, TypeAlias

from .finding_closure import (ClosureContractError, ClosureState, CueVersion, Event, Patch, State, TaskSpec,
                              Work, sha)

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


def json_value(value: object) -> JsonValue:
    if isinstance(value,float) and not math.isfinite(value):
        raise ClosureContractError('FINDING_CLOSURE_JSON_NONFINITE')
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return {str(k): json_value(v) for k, v in value.items()}
    raise ClosureContractError("FINDING_CLOSURE_JSON_VALUE_INVALID")


@dataclass(frozen=True)
class RetainedObject:
    json_text: str

    def __post_init__(self) -> None:
        try:
            parsed = json_value(json.loads(self.json_text))
        except json.JSONDecodeError as exc:
            raise ClosureContractError('FINDING_CLOSURE_JSON_INVALID') from exc
        if not isinstance(parsed, dict):
            raise ClosureContractError("FINDING_CLOSURE_JSON_OBJECT_REQUIRED")
        canonical = json.dumps(parsed, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
        if canonical != self.json_text:
            raise ClosureContractError("FINDING_CLOSURE_JSON_NOT_CANONICAL")

    @classmethod
    def from_value(cls, value: object) -> RetainedObject:
        return cls(json.dumps(json_value(value), ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"), allow_nan=False))

    @property
    def sha256(self) -> str:
        return sha(self.json_text)

    def value(self) -> dict[str, JsonValue]:
        parsed = json_value(json.loads(self.json_text))
        if not isinstance(parsed, dict):
            raise ClosureContractError("FINDING_CLOSURE_JSON_OBJECT_REQUIRED")
        return parsed


@dataclass(frozen=True)
class PreparedFinding:
    spec: TaskSpec
    finding: RetainedObject
    check: RetainedObject
    materials: RetainedObject
    origin_check: RetainedObject | None = None
    projection: RetainedObject | None = None

    def __post_init__(self) -> None:
        if (self.spec.finding_ref != self.finding.sha256 or self.spec.check_ref != self.check.sha256
                or self.spec.materials_ref != self.materials.sha256):
            raise ClosureContractError("FINDING_CLOSURE_ARTIFACT_BINDING_DRIFT")
        if self.spec.origin_check_ref != (self.origin_check.sha256 if self.origin_check else ""):
            raise ClosureContractError('FINDING_CLOSURE_ORIGIN_BINDING_DRIFT')
        if self.spec.projection_ref != (self.projection.sha256 if self.projection else ''):
            raise ClosureContractError('FINDING_CLOSURE_PROJECTION_BINDING_DRIFT')


@dataclass(frozen=True)
class ScanRequest:
    work: Work
    srt_text: str


@dataclass(frozen=True)
class ScanResult:
    findings: tuple[PreparedFinding, ...]
    coverage: tuple[CueVersion, ...]
    lanes: tuple[RetainedObject, ...]


@dataclass(frozen=True)
class PrefetchRequest:
    work: Work
    source_sha256: str
    srt_text: str


@dataclass(frozen=True)
class PrefetchResult:
    receipts: tuple[RetainedObject, ...]


@dataclass(frozen=True)
class ReviewRequest:
    work: Work
    srt_text: str
    finding: PreparedFinding


@dataclass(frozen=True)
class ReviewResult:
    finding: PreparedFinding
    state: State
    receipt: RetainedObject


@dataclass(frozen=True)
class ConsumedRuling:
    stage: str
    check: RetainedObject
    materials: RetainedObject
    inputs: RetainedObject
    model_identity: RetainedObject
    prompt: str
    raw_completion: str
    origin_check: RetainedObject
    checkpoint: RetainedObject

    def document(self) -> RetainedObject:
        return RetainedObject.from_value({'schema_version':'consumed-ruling.v1',
            'stage':self.stage,'check':self.check.value(),'materials':self.materials.value(),
            'inputs':self.inputs.value(),'model_identity':self.model_identity.value(),
            'prompt':self.prompt,'raw_completion':self.raw_completion,
            'completion_sha256':sha(self.raw_completion),'origin_check':self.origin_check.value(),
            'checkpoint':self.checkpoint.value()})


@dataclass(frozen=True)
class PatchRequest:
    work: Work
    srt_text: str
    finding: PreparedFinding
    ruling: RetainedObject
    all_findings: tuple[PreparedFinding, ...]


@dataclass(frozen=True)
class PatchResult:
    patch: Patch
    findings: tuple[PreparedFinding, ...]
    repair: RetainedObject


@dataclass(frozen=True)
class ProjectionBasis:
    before_srt: str
    after_srt: str
    repair: RetainedObject
    ruling: RetainedObject


@dataclass(frozen=True)
class RebaseRequest:
    srt_text: str
    findings: tuple[PreparedFinding, ...]
    basis: tuple[ProjectionBasis,...] = ()


@dataclass(frozen=True)
class ArtifactSet:
    objects: tuple[RetainedObject, ...]

    def get(self, digest: str) -> RetainedObject:
        for obj in self.objects:
            if obj.sha256 == digest:
                return obj
        raise ClosureContractError("FINDING_CLOSURE_ARTIFACT_MISSING:" + digest)


@dataclass(frozen=True)
class ValidationRequest:
    state: ClosureState
    work: Work
    event: Event
    artifacts: ArtifactSet


@dataclass(frozen=True)
class ConfirmationRequest:
    state: ClosureState
    srt_text: str
    findings: tuple[PreparedFinding, ...]


@dataclass(frozen=True)
class ConfirmationResult:
    output_sha256: str
    audit: RetainedObject
    release_ready: bool = True


@dataclass(frozen=True)
class InputUpdateResult:
    receipt: RetainedObject
    findings: tuple[PreparedFinding, ...]


@dataclass(frozen=True)
class DiscoveryInputs:
    source_sha256: str
    timeline_offset_ms: int
    glossary_text: str
    structured_context_text: str
    candidate_context_text: str
    candidate_context: RetainedObject
    verified_authority: RetainedObject
    correction_audit: RetainedObject
    priority_findings: tuple[RetainedObject, ...]
    draft_fidelity_contexts: tuple[RetainedObject, ...]
    prepare_finding: Callable[[str, RetainedObject], PreparedFinding]
    llm_call: Callable[[str], str]
    extract_json: Callable[[str], object]
    runtime_adapters: object = None
    entity_verifier: object = None
    screen_read_probe: object = None
    source_media_path: str = ""
    output_root: str = ""
    candidate_id: str = ""
    spec: RetainedObject | None = None
    authoritative_chat: tuple[RetainedObject, ...] = ()
    truth_ownership: RetainedObject | None = None


class DiscoveryPorts(Protocol):
    def first_scan(self, request: ScanRequest) -> ScanResult: ...
    def targeted_scan(self, request: ScanRequest) -> ScanResult: ...
    def final_scan(self, request: ScanRequest) -> ScanResult: ...
    def validate_scan(self, request: ValidationRequest) -> None: ...
    def validate_terminal(self, request: ValidationRequest) -> None: ...


class EvidenceAcquirer(Protocol):
    def prefetch(self, request: PrefetchRequest) -> PrefetchResult: ...
    def evidence(self, request: ReviewRequest) -> ReviewResult: ...


class Adjudicator(Protocol):
    def decide(self, request: ReviewRequest) -> ReviewResult: ...
    def terminal(self, request: ReviewRequest) -> ReviewResult: ...
    def recover(self, request: ReviewRequest) -> ReviewResult: ...
    def revalidate(self, request: ValidationRequest) -> None: ...


class Projector(Protocol):
    def rebase(self, request: RebaseRequest) -> tuple[PreparedFinding, ...]: ...
    def patch(self, request: PatchRequest) -> PatchResult: ...
    def confirm(self, request: ConfirmationRequest) -> ConfirmationResult: ...


class ClosurePorts(DiscoveryPorts, EvidenceAcquirer, Adjudicator, Projector, Protocol):
    """Only this protocol is needed by the shell or the replay oracle."""


class Ledger(Protocol):
    def put(self, document: RetainedObject) -> str: ...
    def get(self, digest: str) -> RetainedObject: ...
    def append(self, event: Event, before: ClosureState, after: ClosureState) -> None: ...
    def events(self) -> tuple[RetainedObject, ...]: ...
