"""H104 durable text dispatch claims; raw answers remain in the existing checkpoint store."""

from __future__ import annotations

from contextvars import ContextVar
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
import weakref
from functools import wraps
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import (
    TYPE_CHECKING,
    Literal,
    NoReturn,
    NotRequired,
    ParamSpec,
    Protocol,
    TypeAlias,
    TypeVar,
    TypedDict,
    cast,
)
from _thread import LockType

from .argon_escalation_storage import atomic_write
from .cpa_retry_grant import RetryGrant, issue_retry_grant, load_retry_grant
from .infra_retry_policy import (
    CPA_TEXT_MAX_ATTEMPTS,
    CPA_TEXT_RETRY_EXHAUSTED,
    cpa_text_retry_delay_seconds,
    terminal_failure_is_transient,
    terminal_retry_remaining_seconds,
    TERMINAL_RETRY_DEFAULT_WALLCLOCK_SECONDS,
)

if TYPE_CHECKING:
    from .finding_context import JsonValue
    from .completed_review_checkpoint import CompletedReviewCheckpoint


class ContractReceipt(TypedDict):
    json_mode: bool
    schema_name: str | None
    schema_sha256: str | None
    reason_code: NotRequired[str]


class ProviderBinding(TypedDict, total=False):
    provider_transport: JsonValue
    provider_endpoint_host: JsonValue
    provider_endpoint_path: JsonValue
    provider_runtime_host: JsonValue


class RequestBinding(TypedDict):
    policy: str
    issue: str | None
    model: dict[str, JsonValue]
    provider: ProviderBinding
    request_material_sha256: str
    contract: dict[str, JsonValue]
    owner_materials_sha256: str


class PhysicalBinding(TypedDict):
    logical_request: str
    input_sha256: str
    model: dict[str, JsonValue]
    contract: dict[str, JsonValue]


class LegacyUnknown(TypedDict):
    request_sha256: str
    record_sha256: str
    status: str
    physical_sends: int | None


class AttemptIdentity(TypedDict):
    attempt: int
    input_identity: str


class StartedEvent(AttemptIdentity):
    status: Literal["STARTED"]
    pid: int
    started_ns: int
    possible_duplicate_billing: bool
    physical_binding: NotRequired[PhysicalBinding]


FailureStatus: TypeAlias = Literal["NOT_DISPATCHED", "OUTCOME_UNKNOWN", "PERMANENT_FAILURE"]


class FailureEvent(AttemptIdentity):
    status: FailureStatus
    error_type: str
    provider_calls: int | None
    delay_seconds: float
    billing: Literal["UNKNOWN"]


class CompletedEvent(AttemptIdentity):
    status: Literal["COMPLETED"]
    completion_sha256: str
    billing: Literal["UNKNOWN"]


DispatchEvent: TypeAlias = StartedEvent | FailureEvent | CompletedEvent


class RequestState(TypedDict):
    policy: str
    binding: RequestBinding
    max_attempts: int
    reserved_attempts: int
    events: list[DispatchEvent]
    legacy_unknown: list[LegacyUnknown]
    legacy_physical_sends: int | None
    upstream_deduplication_guaranteed: bool
    next_not_before_epoch: float
    retry_grant_sha256: NotRequired[str]


class TextRetryObservation(TypedDict):
    kind: Literal["text"]
    logical_call_id: str
    wire_request_identity: str
    attempt: int
    max_attempts: int
    status: Literal["UNKNOWN", "NOT_DISPATCHED", "PERMANENT_FAILURE"]
    error_type: str
    reason_code: str
    error_category: str | None
    retry_scheduled: bool
    delay_seconds: float | None
    possible_duplicate_billing: bool
    authorization_sha256: NotRequired[str]
    base_max_attempts: NotRequired[int]


class TextRetryExhaustion(TypedDict):
    reason: str
    attempts: int
    max_attempts: int
    error_category: str | None
    retryable: Literal[True]


class _CurrentContract(Protocol):
    def __call__(
        self, response_schema: object = None, *, json_mode: bool | None = None
    ) -> object: ...


class _ContractReceipt(Protocol):
    def __call__(self, contract: object = None, *, prompt: str | None = None) -> object: ...


_P = ParamSpec("_P")
_R = TypeVar("_R")


RECOVERY_POLICY = "h104-cpa-text-v1"
_active: ContextVar[TextRequest | None] = ContextVar("persistent_cpa_text_request", default=None)
_meaning: ContextVar[dict[str, JsonValue] | None] = ContextVar(
    "persistent_cpa_request_meaning", default=None
)
_mutexes: weakref.WeakValueDictionary[str, LockType] = weakref.WeakValueDictionary()
_mutex_guard = threading.Lock()


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def refuse(reason: str) -> NoReturn:
    from .semantic_retry_progress import RetryNeedsPlan

    raise RetryNeedsPlan(reason)


def active_request() -> bool:
    return _active.get() is not None


def json_object(value: object, reason: str) -> dict[str, JsonValue]:
    """Snapshot a legacy JSON boundary, retaining all extension fields.

    The shared codec accepts legacy tuples as JSON arrays, exactly as the
    existing identity serializer does. Known internal records use TypedDict.
    """
    from .finding_context import json_value

    document = json_value(value)
    if not isinstance(document, dict):
        refuse(reason)
    return document


def typed_contract_receipt(
    *,
    prompt: str | None = None,
    response_schema: object = None,
    json_mode: bool | None = None,
) -> ContractReceipt:
    """Keep structured_output's contract semantics at one checked boundary."""
    from .structured_output import contract_receipt, current_contract

    # An active contract may contain a validator callable. Only its public
    # receipt is JSON; do not attempt to serialize the live contract itself.
    contract = cast(_CurrentContract, current_contract)(response_schema, json_mode=json_mode)
    value = json_object(
        cast(_ContractReceipt, contract_receipt)(contract, prompt=prompt),
        "CPA_TEXT_CONTRACT_RECEIPT_INVALID",
    )
    if (
        not {"json_mode", "schema_name", "schema_sha256"} <= value.keys()
        or type(value["json_mode"]) is not bool
        or not (value["schema_name"] is None or isinstance(value["schema_name"], str))
        or not (value["schema_sha256"] is None or isinstance(value["schema_sha256"], str))
        or ("reason_code" in value and not isinstance(value["reason_code"], str))
    ):
        refuse("CPA_TEXT_CONTRACT_RECEIPT_INVALID")
    return cast(ContractReceipt, value)


def legacy_unknown(value: object) -> LegacyUnknown:
    document = json_object(value, "CPA_TEXT_LEGACY_UNKNOWN_INVALID")
    sends = document.get("physical_sends")
    if (
        not all(isinstance(document.get(key), str) for key in (
            "request_sha256", "record_sha256", "status"
        ))
        or "physical_sends" not in document
        or not (sends is None or type(sends) is int and sends >= 0)
    ):
        refuse("CPA_TEXT_LEGACY_UNKNOWN_INVALID")
    return cast(LegacyUnknown, document)


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _validated_event(value: JsonValue, reserved: int) -> DispatchEvent:
    reason = "CPA_TEXT_REQUEST_STATE_INVALID"
    if not isinstance(value, dict):
        refuse(reason)
    attempt = value.get("attempt")
    if (
        type(attempt) is not int
        or not 1 <= attempt <= reserved
        or not isinstance(value.get("input_identity"), str)
    ):
        refuse(reason)
    status = value.get("status")
    if status == "STARTED":
        if (
            type(value.get("pid")) is not int
            or type(value.get("started_ns")) is not int
            or type(value.get("possible_duplicate_billing")) is not bool
        ):
            refuse(reason)
        physical = value.get('physical_binding')
        if physical is not None:
            if not isinstance(physical, dict):
                refuse(reason)
            sha = physical.get('input_sha256')
            if (set(physical) != {'logical_request', 'input_sha256', 'model', 'contract'}
                    or not isinstance(sha, str) or len(sha) != 64
                    or any(c not in '0123456789abcdef' for c in sha)
                    or digest(physical) != value['input_identity']):
                refuse(reason)
        return cast(StartedEvent, value)
    if status in ("NOT_DISPATCHED", "OUTCOME_UNKNOWN", "PERMANENT_FAILURE"):
        calls = value.get("provider_calls")
        if (
            not isinstance(value.get("error_type"), str)
            or "provider_calls" not in value
            or not (calls is None or type(calls) is int and calls >= 0)
            or not _finite_number(value.get("delay_seconds"))
            or value.get("billing") != "UNKNOWN"
        ):
            refuse(reason)
        return cast(FailureEvent, value)
    if status == "COMPLETED":
        if not isinstance(value.get("completion_sha256"), str) or value.get("billing") != "UNKNOWN":
            refuse(reason)
        return cast(CompletedEvent, value)
    refuse(reason)


def _validated_state(
    value: object, binding: RequestBinding, *, limit: int = CPA_TEXT_MAX_ATTEMPTS
) -> RequestState:
    reason = "CPA_TEXT_REQUEST_STATE_INVALID"
    document = json_object(value, reason)
    reserved = document.get("reserved_attempts")
    # Retain the original policy, binding, budget and contiguous reservation
    # checks. Check the remaining shapes before treating the document as state.
    if (
        document.get("binding") != binding
        or document.get("policy") != RECOVERY_POLICY
        or type(document.get("max_attempts")) is not int
        or document.get("max_attempts") != CPA_TEXT_MAX_ATTEMPTS
        or type(reserved) is not int
        or not 0 <= reserved <= limit
    ):
        refuse(reason)
    raw_events = document.get("events")
    if not isinstance(raw_events, list):
        refuse(reason)
    events = [_validated_event(event, reserved) for event in raw_events]
    if any(event['status'] == 'STARTED' and event.get('physical_binding') is not None
           and event['physical_binding']['logical_request'] != digest(binding) for event in events):
        refuse(reason)
    if [event["attempt"] for event in events if event["status"] == "STARTED"] != list(
        range(1, reserved + 1)
    ):
        refuse(reason)
    legacy = document.get("legacy_unknown")
    if not isinstance(legacy, list):
        refuse(reason)
    for record in legacy:
        legacy_unknown(record)
    physical_sends = document.get("legacy_physical_sends")
    if (
        "legacy_physical_sends" not in document
        or not (physical_sends is None or type(physical_sends) is int and physical_sends >= 0)
        or type(document.get("upstream_deduplication_guaranteed")) is not bool
        or not _finite_number(document.get("next_not_before_epoch"))
    ):
        refuse(reason)
    # Returning the validated snapshot retains unknown extension fields rather
    # than reconstructing (and possibly dropping) historical evidence.
    return cast(RequestState, document)


def bind_request_meaning(function: Callable[_P, _R]) -> Callable[_P, _R]:
    """Use the existing checkpoint's typed stage/input identity before rendering."""

    @wraps(function)
    def bound(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        from .completed_review_checkpoint import _request_material_binding, SCHEMA

        value = json_object(
            cast(Callable[[object, object], object], _request_material_binding)(
                {
                    "schema_version": SCHEMA,
                    "stage": kwargs["stage"],
                    "inputs": kwargs["inputs"],
                    "provider": {"routing": getattr(kwargs["llm_call"], "cpa_cache_identity", {})},
                },
                kwargs["prompt"],
            ),
            "CPA_TEXT_REQUEST_MATERIAL_INVALID",
        )
        token = _meaning.set(value)
        try:
            return function(*args, **kwargs)
        finally:
            _meaning.reset(token)

    return bound


def request_environment() -> dict[str, str]:
    owner = _active.get()
    if owner is None or owner.dispatch_key is None:
        return {}
    result = {"AUTOSLICE_CPA_LOGICAL_REQUEST_ID": owner.dispatch_key}
    if owner.grant is not None:
        result["AUTOSLICE_CPA_TEXT_ATTEMPT_LIMIT"] = str(owner.limit)
    return result


def request_headers() -> dict[str, str]:
    key = request_environment().get("AUTOSLICE_CPA_LOGICAL_REQUEST_ID")
    return {} if key is None else {"Idempotency-Key": key, "X-Client-Request-Id": key[:32]}


def binding_for(
    prompt: str,
    model: object,
    issue: str | None = None,
    provider: object = None,
    contract: object = None,
) -> RequestBinding:
    from .completed_review_checkpoint import ruling_owner_materials
    from .prompt_materials import canonical_materials, prompt_material_sha256

    provider_value = provider or {}
    if not isinstance(provider_value, Mapping):
        refuse("CPA_TEXT_PROVIDER_BINDING_INVALID")
    # Preserve the old filter-before-serialization order: unrelated runtime
    # diagnostics need not be JSON, because they never participate in identity.
    provider_binding = cast(ProviderBinding, json_object({
        key: value
        for key, value in provider_value.items()
        if key in {
            "provider_transport", "provider_endpoint_host",
            "provider_endpoint_path", "provider_runtime_host",
        }
    }, "CPA_TEXT_PROVIDER_BINDING_INVALID"))
    return {
        "policy": RECOVERY_POLICY,
        "issue": issue,
        "model": json_object(model, "CPA_TEXT_MODEL_BINDING_INVALID"),
        "provider": provider_binding,
        "request_material_sha256": digest(_meaning.get())
        if _meaning.get() is not None
        else cast(Callable[[str], str], prompt_material_sha256)(prompt),
        "contract": json_object(
            contract if contract is not None else typed_contract_receipt(prompt=prompt),
            "CPA_TEXT_CONTRACT_BINDING_INVALID",
        ),
        "owner_materials_sha256": digest(
            cast(Callable[[object], object], canonical_materials)(
                cast(Callable[[], object], ruling_owner_materials)()
            )
        ),
    }


def completed_response_scope(raw: str, prompt: str) -> str | None:
    """Validate the durable dispatch which returned these exact bytes.

    This is observation ownership, not permission to treat a fallback as the
    requested model. Generic model caches keep their original isolation.
    """
    proof = getattr(raw, 'completed_text_request', None)
    if proof is None:
        return None
    from .qixi_transaction_core import stable_regular_snapshot
    if not isinstance(proof, dict) or set(proof) not in ({'folder', 'binding', 'physical'},
                                                        {'folder', 'binding', 'physical', 'original_prompt'}):
        refuse('CPA_TEXT_COMPLETION_OWNER_INVALID')
    binding = cast(RequestBinding, json_object(proof['binding'], 'CPA_TEXT_COMPLETION_OWNER_INVALID'))
    physical = json_object(proof['physical'], 'CPA_TEXT_COMPLETION_OWNER_INVALID')
    if _meaning.get() is not None and binding['request_material_sha256'] != digest(_meaning.get()):
        refuse('CPA_TEXT_COMPLETION_OWNER_INVALID')
    from .completed_review_checkpoint import ruling_owner_materials
    from .prompt_materials import canonical_materials
    if binding['owner_materials_sha256'] != digest(
            cast(Callable[[object], object], canonical_materials)(ruling_owner_materials())):
        refuse('CPA_TEXT_COMPLETION_OWNER_INVALID')
    folder = Path(proof['folder'])
    key = digest(physical)
    original_prompt = proof.get('original_prompt', prompt)
    if not isinstance(original_prompt, str):
        refuse('CPA_TEXT_COMPLETION_OWNER_INVALID')
    if original_prompt != prompt:
        from .prompt_materials import same_prompt_materials
        if not cast(Callable[[str, str], bool], same_prompt_materials)(original_prompt, prompt):
            refuse('CPA_TEXT_COMPLETION_OWNER_INVALID')
    if (folder.name != 'text-' + digest(binding)
            or physical.get('logical_request') != digest(binding)
            or physical.get('input_sha256') != hashlib.sha256(original_prompt.encode()).hexdigest()
            or physical.get('model') != binding.get('model')
            or physical.get('contract') != binding.get('contract')):
        refuse('CPA_TEXT_COMPLETION_OWNER_INVALID')
    snapshot = stable_regular_snapshot(folder / 'state', label='completed text owner')
    if snapshot is None:
        refuse('CPA_TEXT_COMPLETION_OWNER_MISSING')
    document = json_object(json.loads(snapshot.payload), 'CPA_TEXT_REQUEST_STATE_INVALID')
    grant = load_retry_grant(folder, digest(binding), document)
    state = _validated_state(document, binding,
                             limit=grant.max_attempts if grant is not None else CPA_TEXT_MAX_ATTEMPTS)
    completed = {e['completion_sha256'] for e in state['events']
                 if e['status'] == 'COMPLETED' and e['input_identity'] == key}
    if completed != {hashlib.sha256(raw.encode()).hexdigest()}:
        refuse('CPA_TEXT_COMPLETION_BINDING_DRIFT')
    return key


class TextRequest:
    def __init__(
        self, root: Path | str, binding: RequestBinding, legacy: Sequence[object] = ()
    ) -> None:
        self.binding = binding
        self.key = digest(binding)
        self.dispatch_key: str | None = None
        self.grant: RetryGrant | None = None
        self.folder = Path(root) / ".dispatch-claims" / ("text-" + self.key)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "state"
        self.legacy = [legacy_unknown(record) for record in legacy]
        self.state: RequestState = self._new_state()
        self.deadline = time.monotonic() + TERMINAL_RETRY_DEFAULT_WALLCLOCK_SECONDS
        with _mutex_guard:
            self.mutex = _mutexes.setdefault(str(self.folder.resolve()), threading.Lock())

    def _new_state(self) -> RequestState:
        return {
            "policy": RECOVERY_POLICY,
            "binding": self.binding,
            "max_attempts": CPA_TEXT_MAX_ATTEMPTS,
            "reserved_attempts": 0,
            "events": [],
            "legacy_unknown": self.legacy,
            "legacy_physical_sends": None if self.legacy else 0,
            "upstream_deduplication_guaranteed": False,
            "next_not_before_epoch": 0,
        }

    def save(self) -> None:
        atomic_write(
            self.path,
            json.dumps(self.state, ensure_ascii=False, sort_keys=True, allow_nan=False).encode(),
        )

    @property
    def limit(self) -> int:
        return self.grant.max_attempts if self.grant is not None else CPA_TEXT_MAX_ATTEMPTS

    def authorize_additional_attempts(
        self, prompt: str, model: object, *, additional_attempts: int,
        authority: Mapping[str, str], contract: object = None,
    ) -> None:
        """An operator's verified human grant, under the existing exclusive claim."""
        if _active.get() is not self:
            refuse("CPA_TEXT_RETRY_GRANT_REQUIRES_CLAIM")
        physical = _physical_binding(self.key, prompt, model, contract)
        self.grant = issue_retry_grant(
            self.folder, self.key, self.state,
            input_identity=digest(physical), prompt_sha256=physical["input_sha256"],
            additional_attempts=additional_attempts, authority=authority,
        )
        self.state["retry_grant_sha256"] = self.grant.sha256
        self.save()

    @contextmanager
    def claim(self) -> Iterator[None]:
        if not self.mutex.acquire(blocking=False):
            refuse("CPA_TEXT_REQUEST_IN_FLIGHT")
        try:
            with (self.folder / "lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    refuse("CPA_TEXT_REQUEST_IN_FLIGHT")
                if self.path.is_symlink():
                    refuse("CPA_TEXT_REQUEST_STATE_INVALID")
                if self.path.exists():
                    document = json_object(json.loads(self.path.read_text()), "CPA_TEXT_REQUEST_STATE_INVALID")
                    self.grant = load_retry_grant(self.folder, self.key, document)
                    self.state = _validated_state(document, self.binding, limit=self.limit)
                else:
                    self.state = self._new_state()
                    self.grant = load_retry_grant(self.folder, self.key, self.state)
                    self.save()
                token = _active.set(self)
                try:
                    yield
                finally:
                    _active.reset(token)
        finally:
            self.mutex.release()

    def physical(
        self,
        prompt: str,
        model: object,
        call: Callable[[str], str],
        contract: object = None,
        record: Callable[[TextRetryObservation], None] | None = None,
        *, allow_dispatch: bool = True,
    ) -> str:
        from .completed_review_checkpoint import (
            CompletedReviewCheckpoint,
            SCHEMA,
            ruling_cache_allowed,
        )
        from .infra_retry_policy import _cpa_text_attempt
        from .semantic_retry_progress import CallNotDispatched

        physical = _physical_binding(self.key, prompt, model, contract)
        model_document = physical["model"]
        key = digest(physical)
        self.dispatch_key = key
        base = os.environ.get("AUTOSLICE_BASE") or os.environ.get(
            "AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT"
        )
        cache = Path(base) / "cache/completed-reviews" if base else self.folder.parent.parent
        checkpoint = CompletedReviewCheckpoint(
            cache,
            {
                "schema_version": SCHEMA,
                "stage": "cpa-text-physical",
                "semantic_policy": RECOVERY_POLICY,
                "inputs": physical,
                "provider": {"routing": model_document},
            },
            prompt,
        )
        def owned(raw: str, *, original_physical: PhysicalBinding = physical,
                  original_prompt: str = prompt) -> str:
            from .llm_client import CpaCompletion, completion_identity
            identity = completion_identity(model_document, raw)
            models = identity.get('models') if isinstance(identity, Mapping) else None
            if not isinstance(models, list) or len(models) != 1 or not isinstance(models[0], str):
                return raw
            result = CpaCompletion(str(raw), models[0])
            if hasattr(raw, 'ruling_cache_hit'):
                setattr(result, 'ruling_cache_hit', getattr(raw, 'ruling_cache_hit'))
            proof = {'folder': str(self.folder), 'binding': self.binding, 'physical': original_physical}
            if original_prompt != prompt:
                proof['original_prompt'] = original_prompt
            setattr(result, 'completed_text_request', proof)
            return result
        while True:
            completed = {
                e['completion_sha256'] for e in self.state['events']
                if e['status'] == 'COMPLETED' and e['input_identity'] == key
            }
            raw = checkpoint.load() if ruling_cache_allowed() else None
            if raw is not None:
                if completed and completed != {hashlib.sha256(raw.encode()).hexdigest()}:
                    refuse('CPA_TEXT_COMPLETION_BINDING_DRIFT')
                return owned(raw) if completed else raw
            if len(completed) == 1 and ruling_cache_allowed():
                raw = checkpoint.recover_request_response(next(iter(completed)))
                if raw is not None:
                    return owned(raw)
            if not completed and ruling_cache_allowed():
                equivalent = find_equivalent_completion(self, checkpoint, physical, prompt)
                if equivalent is not None:
                    raw, original_physical, original_prompt = equivalent
                    return owned(raw, original_physical=original_physical, original_prompt=original_prompt)
            if not allow_dispatch:
                refuse('CPA_TEXT_COMPLETED_RESPONSE_REQUIRED')
            if any(
                e.get("input_identity") == key and e["status"] in {"COMPLETED", "PERMANENT_FAILURE"}
                for e in self.state["events"]
            ):
                refuse("CPA_TEXT_COMPLETED_OR_REJECTED_RESPONSE_NOT_REUSABLE")
            if (self.grant is not None
                    and not self.grant.accepts(key, physical["input_sha256"])):
                refuse("CPA_TEXT_RETRY_GRANT_INPUT_MISMATCH")
            if self.state["reserved_attempts"] >= self.limit:
                refuse(CPA_TEXT_RETRY_EXHAUSTED)
            delay = max(0, self.state["next_not_before_epoch"] - time.time())
            remaining = terminal_retry_remaining_seconds()
            remaining = min(
                self.deadline - time.monotonic(),
                remaining if remaining is not None else float("inf"),
            )
            if delay >= remaining:
                refuse("CPA_TEXT_RETRY_NOT_DUE_WITHIN_BUDGET")
            if delay or self.state["reserved_attempts"]:
                time.sleep(delay)
            ordinal = self.state["reserved_attempts"] + 1
            self.state["reserved_attempts"] = ordinal
            event: StartedEvent = {
                "attempt": ordinal,
                "input_identity": key,
                "physical_binding": physical,
                "status": "STARTED",
                "pid": os.getpid(),
                "started_ns": time.time_ns(),
                "possible_duplicate_billing": bool(self.state["legacy_unknown"])
                or any(
                    e["status"] == "OUTCOME_UNKNOWN"
                    or e["status"] == "STARTED"
                    and not any(
                        done["attempt"] == e["attempt"] and done["status"] != "STARTED"
                        for done in self.state["events"]
                    )
                    for e in self.state["events"]
                ),
            }
            self.state["events"].append(event)
            # A process can exit inside the transport without running the handler.
            # Reserve its ordinary retry window with the attempt, before sending.
            self.state["next_not_before_epoch"] = time.time() + (
                cpa_text_retry_delay_seconds(ordinal, TimeoutError())
                if ordinal < self.limit else 0
            )
            self.save()
            token = _cpa_text_attempt.set(ordinal)
            try:
                raw = (
                    checkpoint.response(call, preserve_raw=True)
                    if ruling_cache_allowed()
                    else call(prompt)
                )
            except BaseException as exc:
                not_sent = type(exc) is CallNotDispatched
                from .infra_retry_policy import cpa_text_failure_category
                from .llm_client import LlmCallError

                category = cpa_text_failure_category(exc)
                transient = isinstance(exc, Exception) and (
                    category is not None
                    or (not isinstance(exc, LlmCallError) and terminal_failure_is_transient(exc))
                )
                if transient:
                    setattr(exc, "dispatch_outcome", "UNKNOWN")
                status: FailureStatus = (
                    "NOT_DISPATCHED"
                    if not_sent
                    else "OUTCOME_UNKNOWN"
                    if transient or getattr(exc, "response_received", None) is not True
                    else "PERMANENT_FAILURE"
                )
                delay = (
                    cpa_text_retry_delay_seconds(ordinal, exc)
                    if (transient or status == "OUTCOME_UNKNOWN")
                    and ordinal < self.limit
                    else 0
                )
                self.state["events"].append(
                    {
                        "attempt": ordinal,
                        "input_identity": key,
                        "status": status,
                        "error_type": type(exc).__name__,
                        "provider_calls": 0 if not_sent else None,
                        "delay_seconds": delay,
                        "billing": "UNKNOWN",
                    }
                )
                self.state["next_not_before_epoch"] = time.time() + delay
                self.save()
                from .cpa_runtime import record_cpa_event

                retry_scheduled = transient and ordinal < self.limit
                detail: TextRetryObservation = {
                    "kind": "text",
                    "logical_call_id": self.key,
                    "wire_request_identity": key,
                    "attempt": ordinal,
                    "max_attempts": self.limit,
                    "status": "UNKNOWN" if status == "OUTCOME_UNKNOWN" else status,
                    "error_type": type(exc).__name__,
                    "reason_code": CPA_TEXT_RETRY_EXHAUSTED
                    if transient and ordinal >= self.limit
                    else (getattr(exc, "safe_reason", None) or category or type(exc).__name__),
                    "error_category": category,
                    "retry_scheduled": retry_scheduled,
                    "delay_seconds": delay if retry_scheduled else None,
                    "possible_duplicate_billing": event["possible_duplicate_billing"],
                }
                if self.grant is not None:
                    detail["authorization_sha256"] = self.grant.sha256
                    detail["base_max_attempts"] = CPA_TEXT_MAX_ATTEMPTS
                if record is not None:
                    record(detail)
                else:
                    record_cpa_event("text_retry_attempt", detail)
                if transient and ordinal >= self.limit:
                    setattr(exc, "cpa_text_retry_reason", CPA_TEXT_RETRY_EXHAUSTED)
                    exhaustion: TextRetryExhaustion = {
                        "reason": CPA_TEXT_RETRY_EXHAUSTED,
                        "attempts": ordinal,
                        "max_attempts": self.limit,
                        "error_category": category,
                        "retryable": True,
                    }
                    setattr(exc, "cpa_text_retry", exhaustion)
                    if CPA_TEXT_RETRY_EXHAUSTED not in str(exc):
                        exc.args = (f"{exc}: {CPA_TEXT_RETRY_EXHAUSTED}", *exc.args[1:])
                if not transient or ordinal >= self.limit:
                    raise
            else:
                self.state["events"].append(
                    {
                        "attempt": ordinal,
                        "input_identity": key,
                        "status": "COMPLETED",
                        "completion_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                        "billing": "UNKNOWN",
                    }
                )
                self.state["next_not_before_epoch"] = 0
                self.save()
                return owned(raw)
            finally:
                _cpa_text_attempt.reset(token)


def _physical_binding(
    logical_request: str, prompt: str, model: object, contract: object
) -> PhysicalBinding:
    return {
        "logical_request": logical_request,
        "input_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "model": json_object(model, "CPA_TEXT_MODEL_BINDING_INVALID"),
        "contract": json_object(
            contract if contract is not None else typed_contract_receipt(prompt=prompt),
            "CPA_TEXT_CONTRACT_BINDING_INVALID",
        ),
    }


def persistent_text_call(
    call: Callable[[str], str],
    prompt: str,
    *,
    model: object,
    root: Path | str,
    issue: str | None = None,
    legacy: Sequence[object] = (),
    managed: bool = False,
    provider: object = None,
    contract: object = None,
    record: Callable[[TextRetryObservation], None] | None = None,
) -> str:
    owner = TextRequest(
        root,
        binding_for(
            prompt,
            model,
            issue,
            provider or getattr(call, "provider_runtime_binding", {}),
            contract,
        ),
        legacy,
    )
    with owner.claim():
        return call(prompt) if managed else owner.physical(prompt, model, call, contract, record)


def physical_text_call(
    call: Callable[[str], str],
    prompt: str,
    *,
    model: object,
    root: Path | str,
    provider: object = None,
    contract: object = None,
    record: Callable[[TextRetryObservation], None] | None = None,
) -> str:
    owner = _active.get()
    if owner is not None:
        return owner.physical(prompt, model, call, contract, record)
    return persistent_text_call(
        call, prompt, model=model, root=root, provider=provider, contract=contract, record=record
    )


def replay_completed_text_request(
    call: Callable[[str], str], prompt: str, *, model: object,
    root: Path | str, issue: str,
) -> str:
    """Read this request's durable answer without opening another attempt."""
    binding = binding_for(prompt, model, issue, getattr(call, 'provider_runtime_binding', {}), None)
    path = Path(root) / '.dispatch-claims' / ('text-' + digest(binding)) / 'state'
    if not path.is_file():
        refuse('CPA_TEXT_COMPLETED_RESPONSE_REQUIRED')
    owner = TextRequest(root, binding)
    with owner.claim():
        raw = owner.physical(prompt, model, call, allow_dispatch=False)
        if completed_response_scope(raw, prompt) is None:
            refuse('CPA_TEXT_COMPLETED_RESPONSE_REQUIRED')
        return raw



def _legacy_physical_candidates(owner: TextRequest, checkpoint: CompletedReviewCheckpoint,
                                keys: set[str]) -> dict[str, tuple[PhysicalBinding, str]]:
    """Bounded metadata discovery for older ledgers lacking physical inputs.

    Only matching logical-owner records are decoded; checkpoint.load reopens
    and authenticates every candidate before it may be used. Limits fail closed.
    """
    from .cpa_request_lifecycle import digest, refuse
    from .qixi_transaction_core import stable_regular_snapshot
    from .review_evidence_store import expand, evidence_store_root

    root = checkpoint.root
    candidates: dict[str, tuple[PhysicalBinding, str]] = {}
    bytes_read = files_read = 0
    directories = [root / 'requested-model-responses', root]
    for parent in directories:
        if not parent.is_dir():
            continue
        for directory in parent.iterdir():
            if len(directory.name) != 64 or any(c not in '0123456789abcdef' for c in directory.name):
                continue
            if directory.is_symlink() or not directory.is_dir():
                continue
            files = [directory / 'response.json'] if parent != root else directory.glob('*.json')
            for path in files:
                if not path.exists():
                    continue
                files_read += 1
                size = path.stat().st_size
                bytes_read += size
                if files_read > 4096 or size > 8*1024*1024 or bytes_read > 128*1024*1024:
                    refuse('CPA_TEXT_LEGACY_DISCOVERY_LIMIT')
                snapshot = stable_regular_snapshot(path, label='completed text legacy discovery')
                if snapshot is None or owner.key.encode() not in snapshot.payload:
                    continue
                try:
                    value = cast(Callable[..., object], expand)(json.loads(snapshot.payload),
                        store_root=cast(Callable[..., Path], evidence_store_root)(directory), _shared=True)
                    if not isinstance(value, dict):
                        continue
                    binding = value.get('requested_binding') or value.get('binding')
                    if not isinstance(binding, dict):
                        continue
                    original = binding.get('inputs')
                    old_prompt = value.get('request')
                    if (binding.get('stage') != 'cpa-text-physical' or not isinstance(original, dict)
                            or original.get('logical_request') != owner.key or not isinstance(old_prompt, str)):
                        continue
                    key = digest(original)
                    if key not in keys:
                        continue
                    previous = candidates.get(key)
                    if previous is not None and previous != (original, old_prompt):
                        refuse('CPA_TEXT_COMPLETION_AMBIGUOUS')
                    candidates[key] = (cast(PhysicalBinding, original), old_prompt)
                except (ValueError, TypeError, AttributeError, KeyError):
                    refuse('CPA_TEXT_COMPLETION_BINDING_DRIFT')
    return candidates


def find_equivalent_completion(
    owner: TextRequest, checkpoint: CompletedReviewCheckpoint,
    physical: PhysicalBinding, prompt: str,
) -> tuple[str, PhysicalBinding, str] | None:
    from .cpa_request_lifecycle import digest, refuse
    from .prompt_materials import same_prompt_materials

    events = owner.state['events']
    completed = [event for event in events if event['status'] == 'COMPLETED']
    missing = {event['input_identity'] for event in completed
               if not any(row['status'] == 'STARTED' and row['attempt'] == event['attempt']
                          and row['input_identity'] == event['input_identity']
                          and row.get('physical_binding') is not None for row in events)
               and not (owner.grant is not None and owner.grant.input_identity == event['input_identity'])}
    legacy = _legacy_physical_candidates(owner, checkpoint, missing) if missing else {}
    matches: list[tuple[str, PhysicalBinding, str]] = []
    unresolved = False
    for event in completed:
        key = event['input_identity']
        starts = [row for row in events if row['status'] == 'STARTED'
                  and row['attempt'] == event['attempt'] and row['input_identity'] == key]
        if len(starts) != 1:
            refuse('CPA_TEXT_COMPLETION_OWNER_INVALID')
        original = starts[0].get('physical_binding')
        known_prompt = None
        # Old events did not save the input digest. A validated grant may supply
        # that digest as a lookup hint; the COMPLETED event remains the owner.
        if original is None and owner.grant is not None and owner.grant.input_identity == key:
            original = cast(PhysicalBinding, {**physical, 'input_sha256': owner.grant.prompt_sha256})
        elif original is None and key in legacy:
            original, known_prompt = legacy[key]
        if not isinstance(original, dict):
            unresolved = True
            continue
        if (digest(original) != key or original.get('logical_request') != owner.key
                or original.get('model') != physical['model']
                or original.get('contract') != physical['contract']):
            refuse('CPA_TEXT_COMPLETION_OWNER_INVALID')
        original_checkpoint = type(checkpoint)(checkpoint.root, {
            **checkpoint.binding, 'inputs': original,
        }, known_prompt or prompt)
        raw = original_checkpoint.load(bound_only=True)
        if raw is None:
            unresolved = True
            continue
        old_prompt = original_checkpoint.original_prompt
        if (not isinstance(old_prompt, str)
                or hashlib.sha256(old_prompt.encode()).hexdigest() != original.get('input_sha256')
                or not cast(Callable[[str, str], bool], same_prompt_materials)(old_prompt, prompt)
                or hashlib.sha256(raw.encode()).hexdigest() != event['completion_sha256']):
            refuse('CPA_TEXT_COMPLETION_BINDING_DRIFT')
        matches.append((raw, original, old_prompt))
    if len(matches) > 1:
        refuse('CPA_TEXT_COMPLETION_AMBIGUOUS')
    if unresolved:
        refuse('CPA_TEXT_COMPLETED_OR_REJECTED_RESPONSE_NOT_REUSABLE')
    if matches:
        return matches[0]
    # A completed but unlocatable old observation cannot authorize a resend.
    if completed:
        refuse('CPA_TEXT_COMPLETED_OR_REJECTED_RESPONSE_NOT_REUSABLE')
    return None
