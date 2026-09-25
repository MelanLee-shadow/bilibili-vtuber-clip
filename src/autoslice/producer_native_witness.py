"""Ordinary-producer routing for bounded native audio witnesses.

CPA currently decides whether an exact, candidate-blind audio window is
needed; Jev remains research-only until an explicit production-promotion receipt
exists. This module selects one native ASR transport, reuses the shared
budget/cache, and records what the ordinary producer actually consumed. MOSS/MAI
evidence never becomes text or speaker authority.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from functools import wraps
import hashlib
import json
import os
from pathlib import Path


ROUTING_SCHEMA_VERSION = "producer-audio-witness-routing.v1"
DEFAULT_PROVIDER_ORDER = ("mai", "moss")
_NATIVE_PROVIDERS = frozenset(DEFAULT_PROVIDER_ORDER)
_ROUTING_KEYS = frozenset({"enabled", "provider_order"})


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_audio_witness_routing_config(value: object) -> dict[str, object]:
    """Normalize automatic routing configuration, rejecting ambiguous input."""

    if value is None:
        return {"enabled": True, "provider_order": list(DEFAULT_PROVIDER_ORDER)}
    if not isinstance(value, Mapping):
        raise ValueError("audio_witness_routing must be an object")
    unknown = set(value) - _ROUTING_KEYS
    if unknown:
        raise ValueError(
            "audio_witness_routing only allows enabled and provider_order"
        )
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("audio_witness_routing.enabled must be boolean")
    order = value.get("provider_order", DEFAULT_PROVIDER_ORDER)
    if (
        not isinstance(order, (list, tuple))
        or not order
        or any(
            not isinstance(item, str) or item not in _NATIVE_PROVIDERS
            for item in order
        )
        or len(set(order)) != len(order)
    ):
        raise ValueError(
            "audio_witness_routing.provider_order must be a unique non-empty "
            "list of mai/moss"
        )
    return {"enabled": enabled, "provider_order": list(order)}


def automatic_audio_witness_enabled(spec: Mapping[str, object]) -> bool:
    return bool(
        validate_audio_witness_routing_config(
            spec.get("audio_witness_routing")
        )["enabled"]
    )


def _provider_configuration(
    provider: str,
    environ: Mapping[str, str],
) -> dict[str, object]:
    """Probe configuration only; programming errors must remain visible."""

    if provider == "moss":
        from src.autoslice.moss_transcription import (
            MossTranscriptionError,
            _read_api_key,
        )

        try:
            _read_api_key(environ)
        except MossTranscriptionError as exc:
            return {"configured": False, "reason_code": exc.reason_code}
    elif provider == "mai":
        from src.autoslice.mai_transcription import (
            MaiTranscriptionError,
            _endpoint_url,
            _read_api_key,
        )

        try:
            _read_api_key(environ)
            _endpoint_url(environ)
        except MaiTranscriptionError as exc:
            return {"configured": False, "reason_code": exc.reason_code}
    else:  # pragma: no cover - guarded by policy validation
        raise ValueError(f"unsupported native audio provider: {provider}")
    return {"configured": True, "reason_code": "CONFIGURED"}


def _stage_plan(
    *,
    explicit: object,
    policy: Mapping[str, object],
    availability: Mapping[str, Mapping[str, object]],
    call_gate: str,
) -> dict[str, object]:
    common = {
        "call_gate": call_gate,
        "segment_policy": "EXACT_CANDIDATE_BLIND_REQUEST_GEOMETRY",
    }
    if explicit in _NATIVE_PROVIDERS:
        return {
            **common,
            "selection_mode": "EXPLICIT_NATIVE_OVERRIDE",
            "selected_provider": explicit,
            "selected_provider_configured": bool(
                availability[str(explicit)]["configured"]
            ),
            "selection_reason": "PRODUCER_SPEC_EXPLICIT_PROVIDER",
        }
    if explicit == "agy":
        return {
            **common,
            "selection_mode": "EXPLICIT_LEGACY_AGY",
            "selected_provider": None,
            "selected_provider_configured": None,
            "selection_reason": "PRODUCER_SPEC_EXPLICIT_AGY",
        }
    if not policy["enabled"]:
        return {
            **common,
            "selection_mode": "AUTOMATIC_DISABLED",
            "selected_provider": None,
            "selected_provider_configured": None,
            "selection_reason": "AUTOMATIC_ROUTING_DISABLED",
        }
    selected = next(
        (
            provider
            for provider in policy["provider_order"]
            if availability[str(provider)]["configured"] is True
        ),
        None,
    )
    return {
        **common,
        "selection_mode": "AUTOMATIC_CONFIGURED_PROVIDER",
        "selected_provider": selected,
        "selected_provider_configured": True if selected is not None else None,
        "selection_reason": (
            "FIRST_CONFIGURED_PROVIDER_IN_POLICY_ORDER"
            if selected is not None
            else "NO_NATIVE_PROVIDER_CONFIGURED"
        ),
    }


def _seal(document: Mapping[str, object]) -> dict[str, object]:
    result = deepcopy(dict(document))
    result.pop("receipt_sha256", None)
    result["receipt_sha256"] = _digest(result)
    return result


def build_audio_witness_routing(
    spec: Mapping[str, object],
    *,
    candidate_id: str,
    source_media: Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Plan one-provider routing before any optional audio request is made."""

    policy = validate_audio_witness_routing_config(
        spec.get("audio_witness_routing")
    )
    environment = os.environ if environ is None else environ
    source = Path(source_media).resolve(strict=True)
    availability = {
        provider: _provider_configuration(provider, environment)
        for provider in DEFAULT_PROVIDER_ORDER
    }
    return _seal(
        {
            "schema_version": ROUTING_SCHEMA_VERSION,
            "status": "PLANNED",
            "candidate_id": candidate_id,
            "source_media": str(source),
            "source_media_sha256": _sha256_file(source),
            "policy": {
                **policy,
                "native_provider_fallback": "NONE",
                "text_decision_authority": "CPA",
                "dispatch_decision_policy": "CPA_ONLY_UNTIL_JEV_PRODUCTION_PROMOTION",
                "jev_production_status": "NOT_PROMOTED",
                "speaker_identity_authority": "NONE",
                "uniform_host_is_identity_evidence": False,
            },
            "provider_configuration": availability,
            "stages": {
                "local_entity": _stage_plan(
                    explicit=spec.get("local_audio_witness_provider"),
                    policy=policy,
                    availability=availability,
                    call_gate="CPA_TEXT_FIRST_NEEDS_AUDIO_TRUE",
                ),
                "foreign_script": _stage_plan(
                    explicit=spec.get("foreign_script_witness_provider"),
                    policy=policy,
                    availability=availability,
                    call_gate="MIXED_SCRIPT_BLOCK_THEN_CPA_FINAL_ADJUDICATION",
                ),
            },
            "runtime_native_attempts": [],
            "observed_consumption": [],
        }
    )


def selected_native_provider(
    routing: Mapping[str, object] | None,
    stage: str,
) -> str | None:
    if not isinstance(routing, Mapping):
        return None
    stages = routing.get("stages")
    row = stages.get(stage) if isinstance(stages, Mapping) else None
    provider = row.get("selected_provider") if isinstance(row, Mapping) else None
    return str(provider) if provider in _NATIVE_PROVIDERS else None


def write_audio_witness_routing(path: Path, routing: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(_seal(routing), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def start_audio_witness_routing(
    spec: Mapping[str, object], *, candidate_id: str, source_media: Path, out_root: Path
) -> tuple[dict[str, object], Path]:
    routing = build_audio_witness_routing(
        spec, candidate_id=candidate_id, source_media=source_media
    )
    path = Path(out_root) / f"{candidate_id}.audio-witness-routing.json"
    write_audio_witness_routing(path, routing)
    return routing, path


def _runtime_native_attempt_receipt(
    request: Mapping[str, object],
    evidence: Mapping[str, object],
    *,
    expected_provider: str | None,
) -> dict[str, object] | None:
    native = evidence.get("native_observation")
    native = native if isinstance(native, Mapping) else {}
    observed_provider = evidence.get("provider") or native.get("provider")
    provider = (
        observed_provider
        if observed_provider in _NATIVE_PROVIDERS
        else expected_provider
    )
    if provider not in _NATIVE_PROVIDERS:
        return None
    start_ms = evidence.get("audio_start_ms")
    if type(start_ms) is not int:
        start_ms = native.get("target_start_ms")
    end_ms = evidence.get("audio_end_ms")
    if type(end_ms) is not int:
        end_ms = native.get("target_end_ms")
    served_from_cache = evidence.get("served_from_cache")
    if not isinstance(served_from_cache, bool):
        served_from_cache = native.get("served_from_cache")
    return {
        "schema_version": "producer-native-audio-witness-receipt.v1",
        "request_sha256": evidence.get("request_sha256")
        or request.get("request_sha256"),
        "status": evidence.get("status") or native.get("status"),
        "witness_protocol": evidence.get("witness_protocol"),
        "target_audible": evidence.get("target_audible"),
        "candidate_exposure": evidence.get("candidate_exposure")
        or native.get("candidate_exposure"),
        "authority": evidence.get("authority") or native.get("authority"),
        "mutation_authorized": (
            evidence.get("mutation_authorized")
            if "mutation_authorized" in evidence
            else native.get("mutation_authorized")
        ),
        "provider": provider,
        "provider_call_observed": observed_provider in _NATIVE_PROVIDERS,
        "reason_code": evidence.get("reason_code") or native.get("reason_code"),
        "model": evidence.get("model") or native.get("model"),
        "source_media_sha256": evidence.get("source_media_sha256")
        or native.get("source_media_sha256"),
        "input_audio_sha256": evidence.get("input_audio_sha256")
        or evidence.get("audio_clip_sha256")
        or native.get("input_audio_sha256"),
        "provider_response_sha256": evidence.get("provider_response_sha256")
        or evidence.get("response_sha256")
        or native.get("response_sha256"),
        "audio_start_ms": start_ms,
        "audio_end_ms": end_ms,
        "served_from_cache": served_from_cache,
        "native_receipt_sha256": native.get("receipt_sha256"),
    }


def bind_native_attempt_recorder(
    verifier,
    routing: Mapping[str, object] | None,
    *,
    stage: str,
):
    """Record a text-free native attempt before downstream CPA can discard it."""

    if not isinstance(routing, dict):
        return verifier

    expected_provider = selected_native_provider(routing, stage)

    @wraps(verifier)
    def recorded(request):
        evidence = verifier(request)
        if isinstance(request, Mapping) and isinstance(evidence, Mapping):
            receipt = _runtime_native_attempt_receipt(
                request, evidence, expected_provider=expected_provider
            )
            if receipt is not None:
                rows = routing.setdefault("runtime_native_attempts", [])
                if isinstance(rows, list):
                    cue_indexes = request.get("cue_indexes")
                    rows.append(
                        {
                            "schema_version": "producer-native-audio-attempt.v1",
                            "stage": stage,
                            "request_sha256": request.get("request_sha256"),
                            "cue_indexes": (
                                list(cue_indexes) if isinstance(cue_indexes, list) else []
                            ),
                            "acoustic_witness": receipt,
                        }
                    )
        return evidence

    return recorded


def finish_audio_witness_routing(
    routing: Mapping[str, object],
    path: Path,
    chat_authority_audit: dict[str, object],
) -> dict[str, object]:
    finalized = finalize_audio_witness_routing(routing, chat_authority_audit)
    chat_authority_audit["audio_witness_routing"] = finalized
    write_audio_witness_routing(path, finalized)
    if finalized.get("status") == "INVALID_NATIVE_CONSUMPTION":
        raise SystemExit(f"INVALID_NATIVE_AUDIO_WITNESS_CONSUMPTION: {path}")
    return finalized


def _normalized_sha256(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.removeprefix("sha256:").lower()
    if len(raw) != 64:
        return None
    try:
        int(raw, 16)
    except ValueError:
        return None
    return raw


def _record_request_sha256(record: Mapping[str, object]) -> str | None:
    direct = _normalized_sha256(record.get("request_sha256"))
    if direct is not None:
        return direct
    request = record.get("request")
    if isinstance(request, Mapping):
        return _normalized_sha256(request.get("request_sha256"))
    return None


def _dispatch_decision(
    record: Mapping[str, object],
    verdict: Mapping[str, object],
) -> Mapping[str, object] | None:
    for owner in (verdict, record):
        for key in ("text_first_judge", "audio_dispatch_decision"):
            value = owner.get(key)
            if isinstance(value, Mapping):
                return value
    return None


def _witness_from_record(
    record: Mapping[str, object],
) -> tuple[Mapping[str, object] | None, Mapping[str, object]]:
    verdict = record.get("verdict")
    verdict = verdict if isinstance(verdict, Mapping) else {}
    for owner in (record, verdict):
        nested = owner.get("acoustic_witness")
        if isinstance(nested, Mapping) and nested.get("provider") in _NATIVE_PROVIDERS:
            return nested, verdict
    if verdict.get("provider") in _NATIVE_PROVIDERS:
        return verdict, verdict
    direct = record.get("provider")
    if direct in _NATIVE_PROVIDERS:
        return record, verdict
    return None, verdict


def _cue_indexes(record: Mapping[str, object]) -> list[int]:
    raw = record.get("cue_indexes")
    if isinstance(raw, list):
        return [value for value in raw if type(value) is int]
    cue_index = record.get("cue_index")
    return [cue_index] if type(cue_index) is int else []


def _local_dispatch_binding(
    *,
    decision: Mapping[str, object] | None,
    record: Mapping[str, object],
    witness: Mapping[str, object],
) -> dict[str, object]:
    reasons: list[str] = []
    if not isinstance(decision, Mapping):
        return {
            "dispatch_authority": None,
            "dispatch_decision_status": None,
            "dispatch_decision_sha256": None,
            "dispatch_request_binding": None,
            "dispatch_gate_valid": False,
            "dispatch_reason_codes": ["DISPATCH_DECISION_MISSING"],
        }
    status = decision.get("status")
    if status not in {"RESOLVED", "JUDGED", "NEEDS_AUDIO"}:
        reasons.append("DISPATCH_DECISION_STATUS_INVALID")
    if decision.get("needs_audio") is not True:
        reasons.append("DISPATCH_DECISION_DID_NOT_REQUEST_AUDIO")
    prompt_sha = _normalized_sha256(
        decision.get("judge_prompt_sha256") or decision.get("prompt_sha256")
    )
    completion_sha = _normalized_sha256(
        decision.get("judge_completion_sha256") or decision.get("completion_sha256")
    )
    if prompt_sha is None or completion_sha is None:
        reasons.append("DISPATCH_DECISION_HASH_BINDING_MISSING")
    authority = decision.get("decision_authority")
    if authority not in {"CPA_JUDGE", "CPA_CONTEXT_POLICY"}:
        if status == "JUDGED":
            authority = "CPA_TEXT_FIRST_JUDGE"
        else:
            reasons.append("DISPATCH_DECISION_AUTHORITY_INVALID")
    witness_request = _normalized_sha256(witness.get("request_sha256"))
    record_request = _record_request_sha256(record)
    decision_witness_request = _normalized_sha256(
        decision.get("witness_request_sha256")
    )
    decision_request = _normalized_sha256(
        decision.get("request_sha256") or decision.get("check_request_sha256")
    )
    request_binding = None
    if (
        witness_request is not None
        and decision_witness_request is not None
        and witness_request == decision_witness_request
    ):
        request_binding = "WITNESS_REQUEST_SHA256"
    elif (
        record_request is not None
        and decision_request is not None
        and record_request == decision_request
    ):
        request_binding = "PARENT_REQUEST_SHA256"
    else:
        reasons.append("DISPATCH_DECISION_REQUEST_MISMATCH")
    return {
        "dispatch_authority": authority,
        "dispatch_decision_status": status,
        "dispatch_needs_audio": decision.get("needs_audio"),
        "dispatch_decision_sha256": _digest(decision),
        "dispatch_request_binding": request_binding,
        "dispatch_prompt_sha256": prompt_sha,
        "dispatch_completion_sha256": completion_sha,
        "dispatch_gate_valid": not reasons,
        "dispatch_reason_codes": reasons,
    }


def _witness_binding(
    witness: Mapping[str, object],
    *,
    require_request_sha256: bool,
) -> dict[str, object]:
    native = witness.get("native_observation")
    native = native if isinstance(native, Mapping) else {}
    reasons: list[str] = []
    provider = witness.get("provider") or native.get("provider")
    model = witness.get("model") or native.get("model")
    status = witness.get("status") or native.get("status")
    candidate_exposure = witness.get("candidate_exposure") or native.get(
        "candidate_exposure"
    )
    authority = witness.get("authority") or native.get("authority")
    mutation_authorized = (
        witness.get("mutation_authorized")
        if "mutation_authorized" in witness
        else native.get("mutation_authorized")
    )
    source_sha = _normalized_sha256(
        witness.get("source_media_sha256") or native.get("source_media_sha256")
    )
    audio_sha = _normalized_sha256(
        witness.get("input_audio_sha256")
        or witness.get("audio_clip_sha256")
        or native.get("input_audio_sha256")
        or native.get("audio_clip_sha256")
    )
    response_sha = _normalized_sha256(
        witness.get("provider_response_sha256")
        or witness.get("response_sha256")
        or native.get("response_sha256")
    )
    request_sha = _normalized_sha256(witness.get("request_sha256"))
    start_ms = witness.get("audio_start_ms")
    if type(start_ms) is not int:
        start_ms = witness.get("start_ms")
    if type(start_ms) is not int:
        start_ms = native.get("target_start_ms")
    end_ms = witness.get("audio_end_ms")
    if type(end_ms) is not int:
        end_ms = witness.get("end_ms")
    if type(end_ms) is not int:
        end_ms = native.get("target_end_ms")
    if provider not in _NATIVE_PROVIDERS:
        reasons.append("NATIVE_PROVIDER_INVALID")
    if witness.get("provider_call_observed") is False:
        reasons.append("NATIVE_PROVIDER_CALL_NOT_OBSERVED")
    if not isinstance(model, str) or not model.strip():
        reasons.append("NATIVE_MODEL_IDENTITY_MISSING")
    if status != "OBSERVED":
        reasons.append("NATIVE_EVIDENCE_NOT_OBSERVED")
    if candidate_exposure != "none":
        reasons.append("NATIVE_CANDIDATE_EXPOSURE_INVALID")
    if authority != "EVIDENCE_ONLY":
        reasons.append("NATIVE_AUTHORITY_INVALID")
    if mutation_authorized is not False:
        reasons.append("NATIVE_MUTATION_AUTHORITY_INVALID")
    if source_sha is None or audio_sha is None or response_sha is None:
        reasons.append("NATIVE_EVIDENCE_HASH_BINDING_MISSING")
    if require_request_sha256 and request_sha is None:
        reasons.append("NATIVE_REQUEST_BINDING_MISSING")
    if type(start_ms) is not int or type(end_ms) is not int or end_ms <= start_ms:
        reasons.append("NATIVE_GEOMETRY_INVALID")
    served_from_cache = witness.get("served_from_cache")
    if not isinstance(served_from_cache, bool):
        served_from_cache = native.get("served_from_cache")
    if not isinstance(served_from_cache, bool):
        reasons.append("NATIVE_CACHE_STATE_MISSING")
    return {
        "provider": provider,
        "provider_call_observed": witness.get("provider_call_observed"),
        "model": model,
        "status": status,
        "request_sha256": request_sha,
        "audio_start_ms": start_ms,
        "audio_end_ms": end_ms,
        "source_media_sha256": source_sha,
        "input_audio_sha256": audio_sha,
        "provider_response_sha256": response_sha,
        "served_from_cache": served_from_cache,
        "transport_observation_kind": (
            "CACHE_REUSE" if served_from_cache is True else "FRESH_PROVIDER_RESPONSE"
        ),
        "candidate_exposure": candidate_exposure,
        "evidence_authority": authority,
        "mutation_authorized": mutation_authorized,
        "native_receipt_sha256": _normalized_sha256(
            witness.get("native_receipt_sha256") or native.get("receipt_sha256")
        ),
        "evidence_binding_valid": not reasons,
        "evidence_reason_codes": reasons,
    }


def _post_audio_judge(record: Mapping[str, object], verdict: Mapping[str, object]) -> Mapping[str, object]:
    judge = record.get("judge")
    if isinstance(judge, Mapping):
        return judge
    witness_judge = record.get("witness_judge")
    if isinstance(witness_judge, Mapping):
        nested = witness_judge.get("judge")
        if isinstance(nested, Mapping):
            return nested
    return verdict


def _post_audio_binding(
    *,
    stage: str,
    judge: Mapping[str, object],
) -> dict[str, object]:
    reasons: list[str] = []
    authority = judge.get("decision_authority")
    status = judge.get("status")
    resolved = judge.get("resolved")
    choice = judge.get("choice")
    if authority != "CPA_JUDGE":
        reasons.append("POST_AUDIO_CPA_AUTHORITY_MISSING")
    if stage == "foreign_script":
        if resolved is not True:
            reasons.append("POST_AUDIO_CPA_NOT_RESOLVED")
        if choice not in {"CURRENT", "PROPOSED"}:
            reasons.append("POST_AUDIO_CPA_CHOICE_INVALID")
    else:
        if status not in {"RESOLVED", "JUDGED"}:
            reasons.append("POST_AUDIO_CPA_STATUS_INVALID")
    return {
        "post_audio_judge_status": status,
        "post_audio_resolved": resolved,
        "post_audio_decision_authority": authority,
        "post_audio_choice": choice,
        "post_audio_decision_sha256": _digest(judge) if judge else None,
        "post_audio_gate_valid": not reasons,
        "post_audio_reason_codes": reasons,
    }


def _consumption_row(
    witness: Mapping[str, object],
    *,
    stage: str,
    cue_indexes: list[int],
    origin: str,
    dispatch: Mapping[str, object],
    post_audio_judge: Mapping[str, object] | None,
    require_request_sha256: bool,
) -> dict[str, object] | None:
    binding = _witness_binding(
        witness, require_request_sha256=require_request_sha256
    )
    if binding["provider"] not in _NATIVE_PROVIDERS:
        return None
    judge = post_audio_judge if isinstance(post_audio_judge, Mapping) else {}
    post_audio = _post_audio_binding(stage=stage, judge=judge)
    return {
        "stage": stage,
        "origin": origin,
        "cue_indexes": cue_indexes,
        **binding,
        **dict(dispatch),
        # Compatibility aliases retained for existing consumers.  They now
        # refer to the pre-dispatch CPA decision, not a post-audio verdict.
        "cpa_judge_status": dispatch.get("dispatch_decision_status"),
        "cpa_decision_authority": dispatch.get("dispatch_authority"),
        "cpa_needs_audio": dispatch.get("dispatch_needs_audio"),
        **post_audio,
    }


def _iter_local_native_records(value: object, path: tuple[str, ...] = ()):
    if isinstance(value, Mapping):
        witness, verdict = _witness_from_record(value)
        if witness is not None:
            yield value, verdict, witness, ".".join(path) or "chat_authority"
        for key, child in value.items():
            if key in {
                "foreign_script_consistency_audit",
                "final_source_language_preservation_audit",
                "audio_witness_routing",
                "verdict",
                "acoustic_witness",
                "text_first_judge",
                "audio_dispatch_decision",
                "native_observation",
                "witness_native_evidence",
            }:
                continue
            yield from _iter_local_native_records(child, (*path, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_local_native_records(child, (*path, str(index)))


def _foreign_dispatch_binding(
    *,
    witness: Mapping[str, object],
    detector: Mapping[str, object] | None,
) -> dict[str, object]:
    reasons: list[str] = []
    if detector is None:
        reasons.append("FOREIGN_DETECTOR_ROW_MISSING")
    else:
        for key in ("cue_index", "start_ms", "end_ms"):
            if detector.get(key) != witness.get(key):
                reasons.append("FOREIGN_DETECTOR_GEOMETRY_MISMATCH")
                break
    return {
        "dispatch_authority": "DETERMINISTIC_MIXED_SCRIPT_GATE",
        "dispatch_decision_status": "BLOCKED_MIXED_CJK_LATIN_PHRASE",
        "dispatch_decision_sha256": _digest(detector) if detector is not None else None,
        "dispatch_request_binding": "CUE_INDEX_AND_GEOMETRY" if not reasons else None,
        "dispatch_gate_valid": not reasons,
        "dispatch_reason_codes": reasons,
    }


def _foreign_native_witness(witness: Mapping[str, object]) -> Mapping[str, object]:
    native = witness.get("witness_native_evidence")
    native = native if isinstance(native, Mapping) else {}
    return {
        "status": native.get("status"),
        "provider": witness.get("provider") or native.get("provider"),
        "model": witness.get("model") or native.get("model"),
        "source_media_sha256": witness.get("source_media_sha256")
        or native.get("source_media_sha256"),
        "input_audio_sha256": witness.get("audio_sha256")
        or native.get("input_audio_sha256"),
        "provider_response_sha256": witness.get("response_sha256")
        or native.get("response_sha256"),
        "audio_start_ms": witness.get("start_ms"),
        "audio_end_ms": witness.get("end_ms"),
        "served_from_cache": witness.get("served_from_cache")
        if isinstance(witness.get("served_from_cache"), bool)
        else native.get("served_from_cache"),
        "candidate_exposure": native.get("candidate_exposure"),
        "authority": native.get("authority"),
        "mutation_authorized": native.get("mutation_authorized"),
        "native_receipt_sha256": native.get("receipt_sha256"),
    }


def _deduplicate_consumption_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[object, ...]] = set()
    for row in rows:
        identity = (
            row.get("stage"),
            row.get("provider"),
            row.get("request_sha256"),
            row.get("input_audio_sha256"),
            row.get("provider_response_sha256"),
        )
        if identity in seen:
            continue
        seen.add(identity)
        result.append(row)
    return result


def finalize_audio_witness_routing(
    routing: Mapping[str, object],
    chat_authority_audit: Mapping[str, object],
) -> dict[str, object]:
    """Bind the plan to native evidence and the decision that dispatched it."""

    result = deepcopy(dict(routing))
    observed: list[dict[str, object]] = []
    for record, verdict, witness, origin in _iter_local_native_records(
        chat_authority_audit
    ):
        decision = _dispatch_decision(record, verdict)
        dispatch = _local_dispatch_binding(
            decision=decision,
            record=record,
            witness=witness,
        )
        row = _consumption_row(
            witness,
            stage="local_entity",
            cue_indexes=_cue_indexes(record),
            origin=origin,
            dispatch=dispatch,
            post_audio_judge=_post_audio_judge(record, verdict),
            require_request_sha256=True,
        )
        if row is not None:
            observed.append(row)

    runtime_attempts = result.get("runtime_native_attempts")
    for record in runtime_attempts if isinstance(runtime_attempts, list) else []:
        if not isinstance(record, Mapping):
            continue
        witness = record.get("acoustic_witness")
        if not isinstance(witness, Mapping):
            continue
        row = _consumption_row(
            witness,
            stage=str(record.get("stage") or "local_entity"),
            cue_indexes=_cue_indexes(record),
            origin="audio_witness_routing.runtime_native_attempts",
            dispatch=_local_dispatch_binding(
                decision=None,
                record=record,
                witness=witness,
            ),
            post_audio_judge=None,
            require_request_sha256=True,
        )
        if row is not None:
            observed.append(row)

    foreign = chat_authority_audit.get("foreign_script_consistency_audit")
    foreign = foreign if isinstance(foreign, Mapping) else {}
    detectors = foreign.get("mixed_cjk_latin_cues")
    detectors = detectors if isinstance(detectors, list) else []
    detector_by_cue = {
        row.get("cue_index"): row
        for row in detectors
        if isinstance(row, Mapping) and type(row.get("cue_index")) is int
    }
    cpa_rows = foreign.get("cpa_adjudication_rows")
    cpa_rows = cpa_rows if isinstance(cpa_rows, list) else []
    cpa_by_cue = {
        row.get("cue_index"): row
        for row in cpa_rows
        if isinstance(row, Mapping)
    }
    witness_rows = foreign.get("audio_witness_rows")
    for witness in witness_rows if isinstance(witness_rows, list) else []:
        if not isinstance(witness, Mapping):
            continue
        cue_index = witness.get("cue_index")
        normalized_witness = _foreign_native_witness(witness)
        row = _consumption_row(
            normalized_witness,
            stage="foreign_script",
            cue_indexes=[cue_index] if type(cue_index) is int else [],
            origin="foreign_script_consistency_audit.audio_witness_rows",
            dispatch=_foreign_dispatch_binding(
                witness=witness,
                detector=detector_by_cue.get(cue_index),
            ),
            post_audio_judge=cpa_by_cue.get(cue_index),
            require_request_sha256=False,
        )
        if row is not None:
            observed.append(row)

    observed = _deduplicate_consumption_rows(observed)
    stages = result.get("stages")
    if isinstance(stages, dict):
        for stage, plan in stages.items():
            if not isinstance(plan, dict):
                continue
            stage_rows = [row for row in observed if row["stage"] == stage]
            selected = plan.get("selected_provider")
            for row in stage_rows:
                row["observed_route_match"] = row.get("provider") == selected
                row["consumption_valid"] = bool(
                    row.get("evidence_binding_valid") is True
                    and row.get("dispatch_gate_valid") is True
                    and row.get("post_audio_gate_valid") is True
                    and row.get("observed_route_match") is True
                )
            plan["observed_evidence_count"] = len(stage_rows)
            plan["valid_consumption_count"] = sum(
                row.get("consumption_valid") is True for row in stage_rows
            )
            plan["invalid_consumption_count"] = sum(
                row.get("consumption_valid") is not True for row in stage_rows
            )
            # Compatibility field: this now counts evidence receipts, not merely
            # a configured route or a post-audio CPA decision.
            plan["observed_call_count"] = len(stage_rows)
            plan["observed_route_match"] = (
                all(row.get("observed_route_match") is True for row in stage_rows)
                if stage_rows
                else None
            )
    invalid = [row for row in observed if row.get("consumption_valid") is not True]
    valid = [row for row in observed if row.get("consumption_valid") is True]
    result["observed_consumption"] = observed
    result["valid_consumption_count"] = len(valid)
    result["invalid_consumption_count"] = len(invalid)
    result["status"] = (
        "INVALID_NATIVE_CONSUMPTION"
        if invalid
        else "CONSUMED"
        if valid
        else "NO_NATIVE_CALL_CONSUMED"
    )
    return _seal(result)


def _native_provider(
    spec: Mapping[str, object],
    field: str,
) -> str | None:
    provider = spec.get(field)
    return provider if provider in _NATIVE_PROVIDERS else None


def ensure_local_native_audio_budget(
    spec: Mapping[str, object],
    source_media: Path,
    *,
    routing: Mapping[str, object] | None = None,
) -> str | None:
    """Register one shared budget before the optional lane is built."""

    provider = selected_native_provider(routing, "local_entity") or _native_provider(
        spec, "local_audio_witness_provider"
    )
    if provider is None:
        return None
    from src.autoslice.supplement_audio_budget import ensure_budget

    config = spec.get("local_audio_witness_budget")
    limits = dict(config) if isinstance(config, Mapping) else {}
    ensure_budget(source_media, **limits)
    return provider


def build_routed_local_audio_entity_verifier(
    *,
    spec: Mapping[str, object],
    source_media: Path,
    output_dir: Path,
    recording_date: str,
    source_duration_ms: int,
    host: str,
    routing: Mapping[str, object] | None,
    audio_is_locally_resolvable: Callable[..., bool],
):
    """Build the ordinary local witness chain without moving text authority.

    The caller injects the existing availability predicate so producer-level
    monkeypatch seams remain intact. Provider builders are imported from their
    owner modules at call time for the same reason.
    """

    provider = ensure_local_native_audio_budget(
        spec, source_media, routing=routing
    )
    if not audio_is_locally_resolvable(source_media, host=host):
        return None

    from src.autoslice import entity_audio_verifier

    verifier = entity_audio_verifier.build_local_audio_entity_verifier(
        source_media=source_media,
        output_dir=output_dir,
        recording_date=recording_date,
        source_duration_ms=source_duration_ms,
    )
    if provider not in _NATIVE_PROVIDERS:
        return verifier

    from src.autoslice import native_context_witness

    native = native_context_witness.build_native_context_verifier(
        fallback=verifier,
        source_media=source_media,
        output_dir=output_dir,
        provider=provider,
        **dict(spec.get("local_audio_witness_budget") or {}),
    )
    return bind_native_attempt_recorder(native, routing, stage="local_entity")


def native_foreign_script_kwargs(
    spec: Mapping[str, object],
    *,
    routing: Mapping[str, object] | None = None,
) -> dict[str, str]:
    """Preserve the legacy no-keyword call shape when native mode is off."""

    provider = selected_native_provider(routing, "foreign_script") or _native_provider(
        spec, "foreign_script_witness_provider"
    )
    return {"native_provider": provider} if provider is not None else {}


__all__ = [
    "ROUTING_SCHEMA_VERSION",
    "automatic_audio_witness_enabled",
    "bind_native_attempt_recorder",
    "build_audio_witness_routing",
    "build_routed_local_audio_entity_verifier",
    "ensure_local_native_audio_budget",
    "finalize_audio_witness_routing",
    "finish_audio_witness_routing",
    "native_foreign_script_kwargs",
    "selected_native_provider",
    "start_audio_witness_routing",
    "validate_audio_witness_routing_config",
    "write_audio_witness_routing",
]
