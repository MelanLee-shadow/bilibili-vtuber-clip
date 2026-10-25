"""Provider-neutral runtime callbacks for nested-media acoustic review.

The normal runtime supplies an already-bound ``generate_content`` callable and
an already-bound CPA callable.  This adapter only assembles one three-part,
candidate-blind request and converts their JSON into the existing observer
protocol.  It never loads credentials, selects a fallback, retries, or grants
subtitle/publication authority.
"""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
from src.autoslice.structured_output import call_json

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from src.autoslice.llm_client import extract_json_object
from src.autoslice.nested_media_acoustic_observer import (
    CPA_SCHEMA,
    OBSERVER_SCHEMA,
    _canonical_hash,
    _normalize_cpa_identity,
    _runtime_identity,
    bound_observer_prompt,
    observer_target_scope,
    VOICE_REFERENCE_MODE,
)

ROLE_ORDER = ("host_enrollment", "independent_source", "current_target")
ROLE_STATES = frozenset({"PRESENT", "ABSENT", "UNKNOWN"})
_TARGET_SCOPE_KEYS = ("target_interval_ms", "target_cue_indexes", "target_audio_offset_ms")
_SAFE_LABEL = re.compile(r"[A-Za-z0-9_./:-]{1,256}\Z")

OBSERVER_PROMPT = """You are a blind acoustic observer, not a transcriber.
Three audio parts are attached in this exact order:
1. a trusted host enrollment reference;
2. an independently identified watched-source reference from the aligned window;
3. the current mixed target from the outer recording.

Compare the voices and timing in the audio only. Do not use subtitles, captions,
candidate wording, filenames, visual identity, or an assumed person. A non-host
voice is not automatically the watched source. Sequential turns are not overlap;
mark overlap PRESENT only when simultaneous speech is actually audible. Use
PRESENT, ABSENT, or UNKNOWN for source_media_speech, host_speech, and
overlap_speech. If the audio is unavailable or unclear, return UNKNOWN states.
Return one JSON object with status OBSERVED or UNKNOWN, those three states, and a
short evidence object. Do not return a transcript or a person name.
When TARGET_SCOPE_JSON is supplied, judge only that target interval in part 3.
The rest of parts 2 and 3 is context for recognizing voices, not role evidence
for the target. Return the exact target_interval_ms and target_cue_indexes from
that object. If speech cannot be localized to that interval, use UNKNOWN."""

CPA_PROMPT = """You are the bounded CPA judge for one acoustic role observation.
The supplied object contains only hash-bound request metadata and the observer's
evidence. Preserve each observer role state or downgrade it to UNKNOWN. Never
create PRESENT or ABSENT, reverse a state, infer a speaker from labels, or use
subtitles/candidate text. Sequential speech is not overlap without simultaneous
audio evidence. Return one JSON object with source_media_speech, host_speech,
overlap_speech, and a short evidence object. States are PRESENT, ABSENT, or
UNKNOWN; use UNKNOWN whenever the observer evidence is insufficient.
When a target interval is bound, require the observer evidence to concern that
interval, not merely the whole context audio. Downgrade context-only evidence to
UNKNOWN. Return the exact bound target_interval_ms and target_cue_indexes.
Check that the observer used target_scope.target_audio_offset_ms in part 3,
whose local clock starts at zero. Outer times are identity only. Downgrade
evidence explicitly based on another time interval to UNKNOWN. Return the
exact target_audio_offset_ms too."""

VOICE_REFERENCE_OBSERVER_PROMPT = """You are a blind acoustic observer for one current caption cue.
Three audio parts are attached in this exact order:
1. a trusted host enrollment reference;
2. an independently identified source recording used only as an UNALIGNED VOICE REFERENCE;
3. the current mixed target from the outer recording.

Part 2 is not time-aligned to part 3. Do not perform timing alignment, NCC,
source subtraction, or any inference from filenames, captions, subtitles, visual
identity, or person names. A voice match identifies a speaker, not the origin
of the utterance: the host may also be the speaker in the independent recording.
Matching the enrollment does not by itself establish live host_speech, and
matching the source voice does not by itself establish source_media_speech.
Without a playback locator, compare voice characteristics only and retain UNKNOWN
for origins that these references cannot distinguish.
When PLAYBACK_LOCATOR_HINT_JSON is supplied, listen to part 2 and the hinted
portion of part 3 to compare the actual recorded performance: cadence, breaths,
intonation and recording sounds, not merely the words or the same vocal timbre.
The locator only focuses listening; its score is not an observer role decision.
Report source_media_speech PRESENT only with audible evidence of replay of that
independent recording. Report host_speech PRESENT only for a separately audible
live utterance, not for the recorded host's voice. Describe the audible evidence
and its part-3 local intervals. Judge the whole bound target, including outside
the hinted portion; do not infer ABSENT from the reference duration or match.
If replay and live speech cannot be distinguished, keep the affected origins
UNKNOWN. A reference voice match is EVIDENCE_ONLY context, not caption authority.
Use PRESENT, ABSENT, or UNKNOWN for source_media_speech, host_speech, and
overlap_speech. Use UNKNOWN whenever the voice cannot be uniquely identified or
simultaneous overlap is unclear. Return one JSON object with status OBSERVED or
UNKNOWN, those states, and a short evidence object. Do not return a transcript or
person name. When TARGET_SCOPE_JSON is supplied, judge only that single target
cue in part 3, while the rest of parts 2 and 3 is context. Return its exact
target_interval_ms and target_cue_indexes."""

VOICE_REFERENCE_CPA_PROMPT = """You are the bounded CPA judge for an UNALIGNED VOICE REFERENCE observation.
Part 2 is an independently identified reference recording, not a time-aligned
source track. Preserve observer states or downgrade them to UNKNOWN; never create
PRESENT or ABSENT, infer a speaker from labels, perform NCC/source subtraction,
deduplicate captions, delete audio, change subtitle text, or authorize a DROP.
Only a strict single current cue is in scope. Voice evidence can supply a logical
KEEP context role (source_media, host, overlap, or unknown); if the reference
cannot uniquely identify the voice, use UNKNOWN. Simultaneous overlap requires
actual target audio evidence, not sequential turns. Return exact bound
target_interval_ms and target_cue_indexes and a short evidence object.
Check that the observer used target_scope.target_audio_offset_ms in part 3,
whose local clock starts at zero. Outer times are identity only. Downgrade
evidence explicitly based on another time interval to UNKNOWN. Return the
exact target_audio_offset_ms too. Part 2 is unaligned; its ending time never
establishes absence in part 3. Speaker identity and utterance origin are separate:
the same person may speak in the enrollment and the independent recording.
A shared timbre alone cannot establish either live host or replay origin. When
a playback locator is supplied, preserve a source or live state only if the
observer describes audible recorded-performance or distinct live-speech evidence
at the bound part-3 local interval. Do not downgrade such evidence merely because
the recorded speaker is the host; do downgrade a claim based only on timbre,
words, the locator score, or a conclusion about the unmatched target remainder.
Preserve UNKNOWN whenever the observer cannot distinguish these origins."""


class NestedMediaAcousticRuntimeError(ValueError):
    """A safe runtime assembly or response error."""


@dataclass(frozen=True)
class RuntimeAcousticCallbacks:
    observe_audio: Callable[[Mapping[str, object]], Mapping[str, object]]
    cpa_judge: Callable[[Mapping[str, object], Mapping[str, object]], Mapping[str, object]]
    runtime_identity: Mapping[str, object] | None


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _hash_text(value: str) -> str:
    return _hash_bytes(value.encode("utf-8"))


def _hash_file(path: Path) -> str:
    digest = _evidence_file_sha256(path)
    return "sha256:" + digest


def _safe_hash(value: object) -> str:
    if not isinstance(value, str):
        raise NestedMediaAcousticRuntimeError("RUNTIME_HASH_INVALID")
    raw = value.removeprefix("sha256:").lower()
    if len(raw) != 64 or any(char not in "0123456789abcdef" for char in raw):
        raise NestedMediaAcousticRuntimeError("RUNTIME_HASH_INVALID")
    return "sha256:" + raw


def _safe_label(value: object, code: str) -> str:
    if not isinstance(value, str) or not _SAFE_LABEL.fullmatch(value.strip()):
        raise NestedMediaAcousticRuntimeError(code)
    return value.strip()


def _classify(states: Mapping[str, object]) -> str:
    source = states.get("source_media_speech")
    host = states.get("host_speech")
    overlap = states.get("overlap_speech")
    if source == "PRESENT" and host == "ABSENT" and overlap == "ABSENT":
        return "source_media"
    if source == "PRESENT" and overlap == "PRESENT":
        return "overlap"
    if source == "ABSENT" and host == "PRESENT" and overlap == "ABSENT":
        return "host"
    return "unknown"


def _safe_error(exc: Exception) -> dict[str, object]:
    """Keep only typed, non-secret diagnostics from a provider exception."""

    result: dict[str, object] = {"error_type": type(exc).__name__}
    for name in ("status_code", "http_status", "code"):
        value = getattr(exc, name, None)
        if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
            result["http_status"] = value
            break
    reason = getattr(exc, "reason_code", None)
    if isinstance(reason, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", reason):
        # The surrounding observer/CPA evidence owns ``reason_code``.  Keep
        # terminal parser/provider failures in a separate typed field so a
        # provider failure cannot be relabelled as a schema failure.  Older
        # adapter/transport failures retain their existing reason contract.
        if getattr(exc, "provider_error", False) is True:
            result["structured_reason_code"] = reason
        else:
            result["reason_code"] = reason
    terminal_status = getattr(exc, "terminal_status", None)
    if isinstance(terminal_status, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", terminal_status):
        result["terminal_status"] = terminal_status
    return result


def _all_unknown() -> dict[str, str]:
    return {role: "UNKNOWN" for role in ("source_media_speech", "host_speech", "overlap_speech")}


def _state_payload(payload: Mapping[str, object]) -> dict[str, str] | None:
    roles = ("source_media_speech", "host_speech", "overlap_speech")
    declarations: list[dict[str, str]] = []
    containers: list[Mapping[str, object]] = []
    if any(role in payload for role in roles):
        containers.append(payload)
    if "states" in payload:
        nested = payload["states"]
        if not isinstance(nested, Mapping):
            return None
        containers.append(nested)
    # Some native structured completions put the role declaration beside
    # explanatory evidence rather than at the response root.  Treat the
    # complete role object as another declaration of the same response.
    # An evidence object can also explain each role in prose.  That prose is
    # not a second state declaration when a complete state declaration already
    # exists.  Partial, mixed, enum-like, and conflicting declarations still
    # fail closed; explanatory prose never supplies missing states.
    has_primary_states = any(
        all(isinstance(container.get(role), str) and container[role] in ROLE_STATES
            for role in roles)
        for container in containers
    )
    for field in ("evidence", "instance_evidence"):
        evidence = payload.get(field)
        if isinstance(evidence, Mapping) and any(role in evidence for role in roles):
            descriptions = [evidence.get(role) for role in roles]
            if has_primary_states and all(
                isinstance(value, str) and value.strip()
                and not re.fullmatch(r"[A-Z_]+", value.strip())
                for value in descriptions
            ):
                continue
            containers.append(evidence)
    for container in containers:
        values = {role: container.get(role) for role in roles}
        if any(not isinstance(value, str) or value not in ROLE_STATES for value in values.values()):
            return None
        declarations.append({key: str(value) for key, value in values.items()})
    if not declarations or any(value != declarations[0] for value in declarations[1:]):
        return None
    return declarations[0]


def _evidence_payload(payload: Mapping[str, object]) -> dict[str, object] | None:
    value = payload.get("evidence", payload.get("instance_evidence"))
    if isinstance(value, Mapping) and value:
        return dict(value)
    if isinstance(value, str) and value.strip():
        return {"summary": value.strip()}
    return None


def _extract_role_object(completion: str) -> dict[str, object]:
    """Ignore echoed scope/tool prose, but reject conflicting role answers.

    Native audio completions can include a scope object and malformed tool
    notation before their actual JSON answer. Repeated answer drafts are only
    usable when their states and target scope agree; evidence and all ordinary
    scope checks are still validated by the caller and sent to CPA.
    """
    decoder = json.JSONDecoder()
    roles = ("source_media_speech", "host_speech", "overlap_speech")
    answers: list[dict[str, object]] = []
    offset = 0
    while (start := completion.find("{", offset)) >= 0:
        try:
            value, end = decoder.raw_decode(completion, start)
        except ValueError:
            offset = start + 1
            continue
        offset = end
        evidence_role_container = any(
            isinstance(value.get(field), Mapping)
            and any(role in value[field] for role in roles)
            for field in ("evidence", "instance_evidence")
        ) if isinstance(value, dict) else False
        if isinstance(value, dict) and (
            any(role in value for role in roles)
            or "states" in value
            or evidence_role_container
        ):
            answers.append(value)
    if not answers:
        # Preserve existing invalid/missing-role diagnostics for plain replies.
        return extract_json_object(completion)
    if any(_state_payload(answer) is None for answer in answers):
        raise NestedMediaAcousticRuntimeError("ROLE_RESPONSE_INVALID")
    signatures = {
        _canonical_hash({
            "states": _state_payload(answer),
            "status": answer.get("status"),
            "target_echo": [
                {key: part.get(key) for key in _TARGET_SCOPE_KEYS}
                for part in (answer, answer.get("evidence"), answer.get("instance_evidence"))
                if isinstance(part, Mapping)
            ],
        })
        for answer in answers
    }
    if len(signatures) != 1:
        raise NestedMediaAcousticRuntimeError("ROLE_RESPONSE_AMBIGUOUS")
    return answers[-1]


def _target_scope(request: Mapping[str, object]) -> dict[str, object]:
    return observer_target_scope(
        interval_ms=request.get("interval_ms"),
        target_interval_ms=request.get("target_interval_ms"),
        target_cue_indexes=request.get("target_cue_indexes"),
        target_audio_window_ms=request.get("target_audio_window_ms"),
    )


def _scope_echo_matches(payload: Mapping[str, object], scope: Mapping[str, object]) -> bool:
    """Require an exact target echo from the response or its evidence object.

    Providers may place the target echo under ``evidence`` or
    ``instance_evidence``.  A missing top-level
    echo may therefore be recovered from one complete nested echo, but an
    explicit incomplete or conflicting declaration must fail closed rather
    than being hidden by a nested value.
    """

    if not scope:
        return True
    expected = {key: scope[key] for key in _TARGET_SCOPE_KEYS}
    declarations: list[dict[str, object]] = []

    def collect(value: Mapping[str, object]) -> bool:
        present = [key in value for key in _TARGET_SCOPE_KEYS]
        if not any(present):
            return True
        # A declaration containing only one target field is explicit but
        # incomplete; it must not fall through to another nested value.
        if not all(present):
            return False
        declarations.append({key: value[key] for key in _TARGET_SCOPE_KEYS})
        return True

    if not collect(payload):
        return False
    for field in ("evidence", "instance_evidence"):
        value = payload.get(field)
        if isinstance(value, Mapping) and not collect(value):
            return False

    if not declarations:
        return False
    expected_hash = _canonical_hash(expected)
    return all(_canonical_hash(value) == expected_hash for value in declarations)


def _parts_from_request(
    request: Mapping[str, object],
) -> tuple[list[tuple[bytes, str]], list[dict[str, object]], dict[str, object] | None]:
    raw_parts = request.get("audio_parts")
    if not isinstance(raw_parts, Sequence) or isinstance(raw_parts, (str, bytes)):
        return [], [], {"reason_code": "AUDIO_PARTS_INVALID"}
    if len(raw_parts) != len(ROLE_ORDER):
        return [], [], {"reason_code": "AUDIO_PART_ORDER_INVALID"}
    parts: list[tuple[bytes, str]] = []
    descriptors: list[dict[str, object]] = []
    for expected_role, raw in zip(ROLE_ORDER, raw_parts, strict=True):
        if not isinstance(raw, Mapping) or raw.get("role") != expected_role:
            return [], [], {"reason_code": "AUDIO_PART_ORDER_INVALID"}
        path_value = raw.get("path")
        if not isinstance(path_value, str) or not path_value:
            return [], [], {"reason_code": "AUDIO_PART_UNAVAILABLE"}
        path = Path(path_value)
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError("not a regular file")
            data = path.read_bytes()
            actual = _hash_bytes(data)
            expected = _safe_hash(raw.get("sha256"))
            if actual != expected:
                return [], [], {"reason_code": "AUDIO_PART_HASH_MISMATCH", "role": expected_role}
        except (OSError, ValueError, NestedMediaAcousticRuntimeError):
            return [], [], {"reason_code": "AUDIO_PART_UNAVAILABLE", "role": expected_role}
        mime = "audio/wav" if path.suffix.lower() == ".wav" else "audio/mpeg"
        descriptors.append({"role": expected_role, "sha256": actual, "mime_type": mime})
        parts.append((data, mime))
    return parts, descriptors, None


def build_runtime_acoustic_callbacks(
    *,
    audio_generate: Callable[..., object],
    cpa_call: Callable[[str], object],
    provider: str,
    model: str,
    transport: str,
    transport_module: str,
    transport_module_sha256: str,
    cpa_call_identity: Mapping[str, object] | None = None,
    transport_binding: Mapping[str, object] | None = None,
    observer_prompt: str = OBSERVER_PROMPT,
    cpa_prompt: str = CPA_PROMPT,
    audio_attempts: Callable[[], list[dict[str, object]]] | None = None,
    playback_locator: Mapping[str, object] | None = None,
) -> RuntimeAcousticCallbacks:
    """Bind existing normal transports to the nested acoustic observer seam.

    Credentials and model selection stay in the injected callables/arguments.
    The returned identity is intentionally non-secret and is part of the
    observer request hash, so a changed model, prompt, or transport cannot
    replay an older receipt.
    """

    if not callable(audio_generate) or not callable(cpa_call):
        raise NestedMediaAcousticRuntimeError("RUNTIME_CALLBACK_INVALID")
    provider = _safe_label(provider, "RUNTIME_PROVIDER_INVALID")
    model = _safe_label(model, "RUNTIME_MODEL_INVALID")
    transport = _safe_label(transport, "RUNTIME_TRANSPORT_INVALID")
    transport_module = _safe_label(transport_module, "RUNTIME_MODULE_INVALID")
    transport_module_sha256 = _safe_hash(transport_module_sha256)
    if not isinstance(observer_prompt, str) or not observer_prompt.strip():
        raise NestedMediaAcousticRuntimeError("OBSERVER_PROMPT_INVALID")
    if not isinstance(cpa_prompt, str) or not cpa_prompt.strip():
        raise NestedMediaAcousticRuntimeError("CPA_PROMPT_INVALID")
    if cpa_call_identity is None:
        cpa_call_identity = getattr(cpa_call, "cpa_cache_identity", None)
    try:
        normalized_cpa_identity = _normalize_cpa_identity(cpa_call_identity)
    except Exception:
        normalized_cpa_identity = None
    if normalized_cpa_identity is None:
        runtime_identity = None
    else:
        identity_base: dict[str, object] = {
            "provider": provider,
            "model": model,
            "transport": transport,
            "transport_module": transport_module,
            "transport_module_sha256": transport_module_sha256,
            "observer_prompt_sha256": _hash_text(observer_prompt),
            "cpa_prompt_sha256": _hash_text(cpa_prompt),
            "cpa_call_identity": normalized_cpa_identity,
            "cpa_call_identity_sha256": _canonical_hash(normalized_cpa_identity),
            "response_adapter_sha256": _hash_file(Path(__file__)),
        }
        if transport_binding is not None:
            identity_base["transport_binding"] = dict(transport_binding)
        runtime_identity = _runtime_identity(identity_base)

    def _identity_matches(request: Mapping[str, object]) -> bool:
        return request.get("runtime_identity") == runtime_identity

    def _identity_sha() -> str | None:
        if isinstance(runtime_identity, Mapping):
            value = runtime_identity.get("identity_sha256")
            return value if isinstance(value, str) else None
        return None

    # A real provider limit applies to the transport, not only the current
    # cue. Keep subsequent observations unknown without spending another
    # request in this consumer run. This is not a per-clip engineering cap.
    provider_limit: dict[str, object] | None = None

    def _unknown(
        request: Mapping[str, object],
        *,
        reason_code: str,
        extra: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        evidence: dict[str, object] = {
            "part_order": list(ROLE_ORDER),
            "reason_code": reason_code,
        }
        identity_sha = _identity_sha()
        if identity_sha is not None:
            evidence["runtime_identity_sha256"] = identity_sha
        if extra:
            evidence.update(dict(extra))
        return {
            "schema_version": OBSERVER_SCHEMA,
            "status": "UNKNOWN",
            "authority": "EVIDENCE_ONLY",
            "candidate_id": request.get("candidate_id"),
            "observation_id": request.get("observation_id"),
            "request_sha256": request.get("request_sha256"),
            **{key: request[key] for key in ("target_interval_ms", "target_cue_indexes")
               if request.get(key) is not None},
            **_all_unknown(),
            "evidence": evidence,
        }

    def observe_audio(request: Mapping[str, object]) -> Mapping[str, object]:
        nonlocal provider_limit
        if runtime_identity is None:
            return _unknown(request if isinstance(request, Mapping) else {}, reason_code="CPA_RUNTIME_IDENTITY_REQUIRED")
        if not isinstance(request, Mapping) or not _identity_matches(request):
            return _unknown(request if isinstance(request, Mapping) else {}, reason_code="RUNTIME_IDENTITY_MISMATCH")
        if provider_limit is not None:
            return _unknown(request, reason_code="PROVIDER_LIMIT_BLOCKED_CURRENT_RUN",
                            extra={**provider_limit, "provider_dispatched": False})
        try:
            scope = _target_scope(request)
        except (TypeError, ValueError):
            return _unknown(request, reason_code="TARGET_SCOPE_INVALID")
        parts, descriptors, failure = _parts_from_request(request)
        if failure is not None:
            return _unknown(request, reason_code=str(failure.get("reason_code") or "AUDIO_PARTS_INVALID"), extra=failure)
        provider_request = {
            "provider": provider,
            "model": model,
            "transport": transport,
            "observer_prompt_sha256": runtime_identity["observer_prompt_sha256"],
            "part_order": list(ROLE_ORDER),
            "parts": descriptors,
        }
        if scope:
            provider_request["target_scope"] = scope
        request_playback_locator = (
            playback_locator
            if request.get("observer_mode") == VOICE_REFERENCE_MODE
            else None
        )
        if request_playback_locator is not None:
            provider_request["playback_locator"] = dict(request_playback_locator)
        bound_prompt = bound_observer_prompt(
            observer_prompt=observer_prompt,
            interval_ms=request.get("interval_ms"),
            target_interval_ms=request.get("target_interval_ms"),
            target_cue_indexes=request.get("target_cue_indexes"),
            target_audio_window_ms=request.get("target_audio_window_ms"),
            playback_locator=request_playback_locator,
        )
        provider_request_sha = _canonical_hash(provider_request)
        try:
            completion = audio_generate(
                prompt=bound_prompt,
                model=model,
                inline_parts=parts,
                response_mime_type="application/json",
                max_output_tokens=2048,
                degrade_on_400=False,
            )
        except Exception as exc:
            error = _safe_error(exc)
            if error.get("http_status") == 429:
                provider_limit = {"http_status": 429,
                                  "originating_request_sha256": request.get("request_sha256"),
                                  "originating_provider_request_sha256": provider_request_sha}
            return _unknown(
                request,
                reason_code="PROVIDER_ERROR",
                extra={"provider_request_sha256": provider_request_sha, **error,
                       **({"provider_attempts": audio_attempts()} if audio_attempts else {})},
            )
        provider_trace = {"provider_attempts": audio_attempts()} if audio_attempts else {}
        if not isinstance(completion, str) or not completion.strip():
            return _unknown(
                request,
                reason_code="PROVIDER_RESPONSE_EMPTY",
                extra={"provider_request_sha256": provider_request_sha, **provider_trace},
            )
        response_sha = _hash_text(completion)
        try:
            payload = _extract_role_object(completion)
        except Exception:
            return _unknown(
                request,
                reason_code="PROVIDER_RESPONSE_INVALID",
                extra={
                    "provider_request_sha256": provider_request_sha,
                    **provider_trace,
                    "provider_response_sha256": response_sha,
                    "provider_completion": completion,
                },
            )
        states = _state_payload(payload)
        if not _scope_echo_matches(payload, scope):
            return _unknown(request, reason_code="PROVIDER_TARGET_SCOPE_MISMATCH",
                extra={"provider_request_sha256": provider_request_sha, **provider_trace,
                       "provider_response_sha256": response_sha,
                       "provider_completion": completion, "provider_payload": payload})
        if states is None or payload.get("status") not in {"OBSERVED", "UNKNOWN"}:
            return _unknown(
                request,
                reason_code="PROVIDER_ROLE_RESPONSE_INVALID",
                extra={
                    "provider_request_sha256": provider_request_sha,
                    **provider_trace,
                    "provider_response_sha256": response_sha,
                    "provider_completion": completion,
                    "provider_payload": payload,
                },
            )
        provider_evidence = _evidence_payload(payload)
        if provider_evidence is None:
            return _unknown(
                request,
                reason_code="PROVIDER_EVIDENCE_MISSING",
                extra={
                    "provider_request_sha256": provider_request_sha,
                    **provider_trace,
                    "provider_response_sha256": response_sha,
                    "provider_completion": completion,
                    "provider_payload": payload,
                },
            )
        evidence = {
            "runtime_identity_sha256": runtime_identity["identity_sha256"],
            "provider_request_sha256": provider_request_sha,
            **provider_trace,
            "provider_response_sha256": response_sha,
            "part_order": list(ROLE_ORDER),
            "role_states": states,
            "provider_completion": completion,
            "provider_payload": payload,
            "provider_evidence": dict(provider_evidence),
            "provider_evidence_field": "evidence" if "evidence" in payload else "instance_evidence",
            "bound_observer_prompt_sha256": _hash_text(bound_prompt),
        }
        if payload.get("status") == "UNKNOWN" or all(value == "UNKNOWN" for value in states.values()):
            return _unknown(request, reason_code="PROVIDER_UNKNOWN_KEEP", extra=evidence)
        return {
            "schema_version": OBSERVER_SCHEMA,
            "status": "OBSERVED",
            "authority": "EVIDENCE_ONLY",
            "candidate_id": request["candidate_id"],
            "observation_id": request["observation_id"],
            "request_sha256": request["request_sha256"],
            **{key: scope[key] for key in _TARGET_SCOPE_KEYS if scope},
            **states,
            "evidence": evidence,
        }

    def _cpa_unknown(
        request: Mapping[str, object],
        observation: Mapping[str, object],
        *,
        reason_code: str,
        extra: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        evidence: dict[str, object] = {
            "reason_code": reason_code,
            "role_states": _all_unknown(),
        }
        identity_sha = _identity_sha()
        if identity_sha is not None:
            evidence["runtime_identity_sha256"] = identity_sha
        if extra:
            evidence.update(dict(extra))
        return {
            "schema_version": CPA_SCHEMA,
            "status": "JUDGED",
            "authority": "CPA_JUDGE",
            "candidate_id": request.get("candidate_id"),
            "observation_id": request.get("observation_id"),
            "request_sha256": request.get("request_sha256"),
            "observation_sha256": _canonical_hash(observation),
            **{key: request[key] for key in ("target_interval_ms", "target_cue_indexes")
               if request.get(key) is not None},
            **_all_unknown(),
            "final_class": "unknown",
            "evidence": evidence,
        }

    def cpa_judge(
        request: Mapping[str, object], observation: Mapping[str, object]
    ) -> Mapping[str, object]:
        if runtime_identity is None:
            return _cpa_unknown(request if isinstance(request, Mapping) else {}, observation, reason_code="CPA_RUNTIME_IDENTITY_REQUIRED")
        if not isinstance(request, Mapping) or not _identity_matches(request):
            return _cpa_unknown(request if isinstance(request, Mapping) else {}, observation, reason_code="RUNTIME_IDENTITY_MISMATCH")
        try:
            scope = _target_scope(request)
        except (TypeError, ValueError):
            return _cpa_unknown(request, observation, reason_code="TARGET_SCOPE_INVALID")
        if not _scope_echo_matches(observation, scope):
            return _cpa_unknown(request, observation, reason_code="OBSERVER_TARGET_SCOPE_MISMATCH")
        observed_states = _state_payload(observation)
        if observed_states is None:
            return _cpa_unknown(request, observation, reason_code="OBSERVER_STATES_INVALID")
        binding = {
            "request_sha256": request.get("request_sha256"),
            "runtime_identity_sha256": runtime_identity["identity_sha256"],
            "cpa_call_identity": runtime_identity["cpa_call_identity"],
            "interval_ms": request.get("interval_ms"),
            "part_order": list(ROLE_ORDER),
            "audio_parts": [
                {"role": item.get("role"), "sha256": item.get("sha256")}
                for item in request.get("audio_parts", [])
                if isinstance(item, Mapping)
            ],
            "mapping": request.get("mapping_receipt"),
            "observer": {
                "request_sha256": observation.get("request_sha256"),
                "status": observation.get("status"),
                **observed_states,
                "evidence": observation.get("evidence"),
            },
        }
        if scope:
            binding["target_scope"] = scope
        if request.get("observer_mode") == VOICE_REFERENCE_MODE:
            voice_binding = request.get("voice_reference_binding")
            if isinstance(voice_binding, Mapping):
                binding["voice_reference_binding"] = dict(voice_binding)
        prompt = cpa_prompt + "\nBOUND_OBSERVATION_JSON:\n" + json.dumps(
            binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        cpa_bound_prompt_sha = _hash_text(prompt)
        try:
            completion = call_json(cpa_call, prompt)
        except Exception as exc:
            return _cpa_unknown(request, observation, reason_code="CPA_PROVIDER_ERROR", extra=_safe_error(exc))
        if not isinstance(completion, str) or not completion.strip():
            return _cpa_unknown(request, observation, reason_code="CPA_RESPONSE_EMPTY")
        response_sha = _hash_text(completion)
        try:
            payload = _extract_role_object(completion)
        except Exception:
            return _cpa_unknown(
                request, observation, reason_code="CPA_RESPONSE_INVALID",
                extra={"cpa_bound_prompt_sha256": cpa_bound_prompt_sha, "cpa_response_sha256": response_sha, "cpa_completion": completion},
            )
        states = _state_payload(payload)
        if not _scope_echo_matches(payload, scope):
            return _cpa_unknown(request, observation, reason_code="CPA_TARGET_SCOPE_MISMATCH",
                extra={"cpa_bound_prompt_sha256": cpa_bound_prompt_sha,
                       "cpa_response_sha256": response_sha,
                       "cpa_completion": completion, "cpa_payload": payload})
        if states is None:
            return _cpa_unknown(
                request, observation, reason_code="CPA_ROLE_RESPONSE_INVALID",
                extra={"cpa_bound_prompt_sha256": cpa_bound_prompt_sha, "cpa_response_sha256": response_sha, "cpa_completion": completion, "cpa_payload": payload},
            )
        if any(states[role] not in {observed_states[role], "UNKNOWN"} for role in observed_states):
            return _cpa_unknown(
                request, observation, reason_code="CPA_STATE_NOT_MONOTONIC",
                extra={"cpa_bound_prompt_sha256": cpa_bound_prompt_sha, "cpa_response_sha256": response_sha, "cpa_completion": completion, "cpa_payload": payload},
            )
        cpa_evidence = _evidence_payload(payload)
        if cpa_evidence is None:
            return _cpa_unknown(
                request,
                observation,
                reason_code="CPA_EVIDENCE_MISSING",
                extra={
                    "cpa_bound_prompt_sha256": cpa_bound_prompt_sha,
                    "cpa_response_sha256": response_sha,
                    "cpa_completion": completion,
                    "cpa_payload": payload,
                },
            )
        return {
            "schema_version": CPA_SCHEMA,
            "status": "JUDGED",
            "authority": "CPA_JUDGE",
            "candidate_id": request["candidate_id"],
            "observation_id": request["observation_id"],
            "request_sha256": request["request_sha256"],
            "observation_sha256": _canonical_hash(observation),
            **{key: scope[key] for key in _TARGET_SCOPE_KEYS if scope},
            **states,
            "final_class": _classify(states),
            "evidence": {
                "runtime_identity_sha256": runtime_identity["identity_sha256"],
                "cpa_prompt_sha256": runtime_identity["cpa_prompt_sha256"],
                "cpa_bound_prompt_sha256": cpa_bound_prompt_sha,
                "cpa_response_sha256": response_sha,
                "role_states": states,
                "cpa_completion": completion,
                "cpa_payload": payload,
                "cpa_evidence": dict(cpa_evidence),
                "cpa_evidence_field": "evidence" if "evidence" in payload else "instance_evidence",
            },
        }

    return RuntimeAcousticCallbacks(
        observe_audio=observe_audio,
        cpa_judge=cpa_judge,
        runtime_identity=runtime_identity,
    )


__all__ = [
    "CPA_PROMPT",
    "OBSERVER_PROMPT",
    "VOICE_REFERENCE_CPA_PROMPT",
    "VOICE_REFERENCE_OBSERVER_PROMPT",
    "ROLE_ORDER",
    "RuntimeAcousticCallbacks",
    "NestedMediaAcousticRuntimeError",
    "bound_observer_prompt",
    "build_runtime_acoustic_callbacks",
]
