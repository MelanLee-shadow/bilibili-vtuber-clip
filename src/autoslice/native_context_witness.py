"""Candidate-blind native transcript evidence for an experimental T11 lane.

The ordinary entity verifier remains the fallback for every request outside the
candidate-blind acoustic witness contract.  The native lane receives only the
physical target geometry, so neither subtitle text nor a neutral length hint can
reach MOSS or MAI.  Native observations remain evidence for CPA; they never
become a subtitle or deletion authority.  The independent wrapper also exposes
a bounded whole-range context seam.  It reuses the same exact-crop provider
writer and validates its immutable files through the existing range
adjudicator; a range observation never becomes a single-cue witness.
"""

from __future__ import annotations

from .review_errors import (ContractRejection, ProviderFailure, provider_boundary,
                            records_program_errors, review_error_record, contract_boundary)

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.autoslice.acoustic_witness_adjudication import (
    WITNESS_REQUEST_SCHEMA,
    build_witness_request,
)
from src.autoslice.acoustic_witness_protocol import (
    BLIND_PINYIN_PROTOCOL,
    CANDIDATE_BLIND_TRANSCRIPT_PROTOCOL,
    WITNESS_SCHEMA,
)
from src.autoslice.local_asr_target_evidence import (
    MODELS as NATIVE_MODELS,
    SCHEMA as NATIVE_OBSERVATION_SCHEMA,
    digest as native_digest,
)
from src.autoslice.native_foreign_witness import (
    _identity as _source_identity,
    _sha_file as _source_sha256,
    build_native_foreign_witness,
    native_acquisition_identity, native_acquisition_path, read_native_acquisition,
)


NATIVE_WITNESS_KIND = "subtitle_span_acoustic_witness"
NATIVE_CONTEXT_ENVELOPE_SCHEMA = "native-context-witness-envelope.v1"
LEGACY_NATIVE_BUDGET_RECEIPT_MIGRATION_REQUIRED = (
    "LOCAL_AUDIO_BUDGET_LEGACY_RECEIPT_MIGRATION_REQUIRED"
)
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ALLOWED_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "witness_protocol",
        "kind",
        "evidence_id",
        "cue_indexes",
        "matched_start_ms",
        "matched_end_ms",
        "context_start_ms",
        "context_end_ms",
        "source_media_timeline_offset_ms",
        "syllable_count_hint",
        "request_sha256",
    }
)
_REQUIRED_REQUEST_KEYS = _ALLOWED_REQUEST_KEYS - {"syllable_count_hint", "request_sha256"}

# These are the only failures that happen after the exact audio has been
# prepared but before ``before_request`` can reserve a provider attempt.  Do
# not turn an arbitrary uncertain envelope into a retry permission: transport,
# response, extraction, and provider failures have already crossed a different
# evidence boundary and remain immutable failures.
_NATIVE_RUNTIME_CONFIG_REASONS = frozenset(
    {
        "NATIVE_AUDIO_RUNTIME_MISSING",
        "NATIVE_AUDIO_RUNTIME_UNSAFE",
        "NATIVE_AUDIO_RUNTIME_CONFIG_MISSING",
        "NATIVE_AUDIO_RUNTIME_CONFIG_UNSAFE",
        "NATIVE_AUDIO_RUNTIME_CONFIG_INVALID",
        "NATIVE_AUDIO_RUNTIME_CONFIG_FIELDS_MISSING",
    }
)
_PRE_HTTP_CONFIG_FAILURES = frozenset(
    {
        "MAI_ENDPOINT_INVALID",
        "MAI_API_KEY_MISSING",
        "MAI_API_KEY_INVALID",
        "MAI_API_KEY_FILE_INVALID",
        "MAI_API_KEY_FILE_PERMISSIONS",
        "MOSS_API_KEY_MISSING",
        "MOSS_API_KEY_INVALID",
        "MOSS_API_KEY_FILE_INVALID",
        "MOSS_API_KEY_FILE_PERMISSIONS",
    }
    | {
        f"{provider}_{reason}"
        for provider in ("MAI", "MOSS")
        for reason in _NATIVE_RUNTIME_CONFIG_REASONS
    }
)


@dataclass(frozen=True)
class _PreparedRequest:
    request: dict[str, Any]
    request_sha256: str
    target_start_ms: int
    target_end_ms: int
    source_offset_ms: int
    physical_start_ms: int
    physical_end_ms: int


class _RequestInvalid(ContractRejection):
    """A typed, non-provider failure in the candidate-blind request contract."""


class _LegacyBudgetReceiptMigrationRequired(ContractRejection):
    """Independent native lanes must not silently choose a legacy receipt."""

    def __init__(self, detail):
        super().__init__(LEGACY_NATIVE_BUDGET_RECEIPT_MIGRATION_REQUIRED, detail)


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _HEX_SHA256.fullmatch(value) is not None


def _sanitized_detail(value: object, *, limit: int = 300) -> str:
    """Keep a useful provider reason without copying credentials or a traceback."""

    text = str(value or "").replace("\x00", " ")
    text = " ".join(text.split())
    text = re.sub(
        r"(?i)(api[_ -]?key|authorization|bearer|token|secret|password)\s*[:=]\s*\S+",
        r"\1=<redacted>",
        text,
    )
    return text[:limit]


def _request_uncertain(
    request: Mapping[str, Any],
    reason_code: str,
    *,
    detail: str = "",
    source_sha256: str | None = None,
    native_observation: Mapping[str, Any] | None = None,
    geometry: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Build an evidence-only uncertainty without inventing inaudibility."""

    result: dict[str, Any] = {
        "schema_version": WITNESS_SCHEMA,
        "witness_protocol": CANDIDATE_BLIND_TRANSCRIPT_PROTOCOL,
        "request_sha256": request.get("request_sha256"),
        "status": "UNCERTAIN",
        "candidate_exposure": "none",
        "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False,
        "reason_code": reason_code,
    }
    if detail:
        result["reason_detail"] = _sanitized_detail(detail)
    if source_sha256 is not None:
        result["source_media_sha256"] = source_sha256
    if geometry is not None:
        result.update(audio_start_ms=geometry[0], audio_end_ms=geometry[1])
    if native_observation is not None:
        result["native_observation"] = dict(native_observation)
    return result


def _prepare_request(request: Mapping[str, Any]) -> _PreparedRequest:
    if set(request) - _ALLOWED_REQUEST_KEYS:
        raise _RequestInvalid("NATIVE_WITNESS_REQUEST_KEYS_INVALID")
    if not _REQUIRED_REQUEST_KEYS.issubset(request):
        raise _RequestInvalid("NATIVE_WITNESS_REQUEST_FIELDS_MISSING")
    if request.get("schema_version") != WITNESS_REQUEST_SCHEMA:
        raise _RequestInvalid("NATIVE_WITNESS_REQUEST_SCHEMA_INVALID")
    if request.get("kind") != NATIVE_WITNESS_KIND:
        raise _RequestInvalid("NATIVE_WITNESS_REQUEST_KIND_INVALID")
    if request.get("witness_protocol") != BLIND_PINYIN_PROTOCOL:
        raise _RequestInvalid("NATIVE_WITNESS_REQUEST_PROTOCOL_INVALID")

    request_sha256 = request.get("request_sha256")
    if not _valid_sha256(request_sha256):
        raise _RequestInvalid("NATIVE_WITNESS_REQUEST_HASH_INVALID")
    payload = dict(request)
    payload.pop("request_sha256")
    try:
        computed = _canonical_sha256(payload)
    except (TypeError, ValueError):
        raise _RequestInvalid("NATIVE_WITNESS_REQUEST_NOT_CANONICAL") from None
    if computed != request_sha256:
        raise _RequestInvalid("NATIVE_WITNESS_REQUEST_HASH_MISMATCH")

    if not isinstance(request.get("evidence_id"), str):
        raise _RequestInvalid("NATIVE_WITNESS_REQUEST_EVIDENCE_ID_INVALID")
    cue_indexes = request.get("cue_indexes")
    if not isinstance(cue_indexes, list):
        raise _RequestInvalid("NATIVE_WITNESS_REQUEST_CUE_INDEXES_INVALID")

    geometry_values = (
        "matched_start_ms",
        "matched_end_ms",
        "context_start_ms",
        "context_end_ms",
        "source_media_timeline_offset_ms",
    )
    if any(type(request.get(key)) is not int for key in geometry_values):
        raise _RequestInvalid("NATIVE_WITNESS_REQUEST_GEOMETRY_INVALID")
    target_start_ms = request["matched_start_ms"]
    target_end_ms = request["matched_end_ms"]
    context_start_ms = request["context_start_ms"]
    context_end_ms = request["context_end_ms"]
    source_offset_ms = request["source_media_timeline_offset_ms"]
    if target_start_ms >= target_end_ms or context_start_ms >= context_end_ms or source_offset_ms < 0:
        raise _RequestInvalid("NATIVE_WITNESS_REQUEST_GEOMETRY_INVALID")

    if "syllable_count_hint" in request:
        hint = request["syllable_count_hint"]
        if isinstance(hint, bool) or not isinstance(hint, int) or hint <= 0:
            raise _RequestInvalid("NATIVE_WITNESS_REQUEST_HINT_INVALID")

    physical_start_ms = target_start_ms + source_offset_ms
    physical_end_ms = target_end_ms + source_offset_ms
    if physical_start_ms < 0 or physical_end_ms <= physical_start_ms:
        raise _RequestInvalid("NATIVE_WITNESS_REQUEST_PHYSICAL_GEOMETRY_INVALID")
    return _PreparedRequest(
        request=dict(request),
        request_sha256=request_sha256,
        target_start_ms=target_start_ms,
        target_end_ms=target_end_ms,
        source_offset_ms=source_offset_ms,
        physical_start_ms=physical_start_ms,
        physical_end_ms=physical_end_ms,
    )


def _observer_failure(exc: ContractRejection | ProviderFailure) -> tuple[str, str]:
    return exc.reason_code, _sanitized_detail(exc)


def _write_envelope(
    *,
    output_dir: Path,
    prepared: _PreparedRequest,
    witness: Mapping[str, Any],
    native_observation: Mapping[str, Any] | None,
    source_sha256: str,
) -> None:
    context_dir = output_dir / "native-context"
    context_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if context_dir.is_symlink():
        raise OSError("native context output directory is a symlink")
    path = context_dir / f"{prepared.request_sha256}.json"
    if path.is_symlink():
        raise OSError("native context receipt path is a symlink")
    envelope: dict[str, Any] = {
        "schema_version": NATIVE_CONTEXT_ENVELOPE_SCHEMA,
        "request_sha256": prepared.request_sha256,
        "request": prepared.request,
        "witness": dict(witness),
        "source_media_sha256": source_sha256,
        "audio_start_ms": prepared.physical_start_ms,
        "audio_end_ms": prepared.physical_end_ms,
    }
    if native_observation is not None:
        envelope["native_observation"] = dict(native_observation)
    path.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _native_transcript_witness(
    *,
    request: Mapping[str, Any],
    prepared: _PreparedRequest,
    native_observation: Mapping[str, Any],
    source_sha256: str,
    provider: str,
) -> dict[str, Any]:
    """Project one complete native exact-crop receipt into transcript evidence."""

    if native_observation.get("schema_version") != NATIVE_OBSERVATION_SCHEMA:
        return _request_uncertain(
            request,
            "NATIVE_OBSERVATION_SCHEMA_INVALID",
            source_sha256=source_sha256,
            native_observation=native_observation,
            geometry=(prepared.physical_start_ms, prepared.physical_end_ms),
        )
    if native_observation.get("status") != "OBSERVED":
        reason = native_observation.get("reason_code") or "NATIVE_NO_SPEECH"
        return _request_uncertain(
            request,
            _sanitized_detail(reason) or "NATIVE_NO_SPEECH",
            detail=native_observation.get("reason") or "native transcript was empty",
            source_sha256=source_sha256,
            native_observation=native_observation,
            geometry=(prepared.physical_start_ms, prepared.physical_end_ms),
        )
    transcript = native_observation.get("transcript")
    if not isinstance(transcript, str) or not transcript.strip():
        return _request_uncertain(
            request,
            "NATIVE_TRANSCRIPT_EMPTY",
            source_sha256=source_sha256,
            native_observation=native_observation,
            geometry=(prepared.physical_start_ms, prepared.physical_end_ms),
        )
    if native_observation.get("crop_is_exact_target") is not True:
        return _request_uncertain(
            request,
            "NATIVE_OBSERVATION_NOT_EXACT_CROP",
            source_sha256=source_sha256,
            native_observation=native_observation,
            geometry=(prepared.physical_start_ms, prepared.physical_end_ms),
        )
    if (
        native_observation.get("target_start_ms") != prepared.physical_start_ms
        or native_observation.get("target_end_ms") != prepared.physical_end_ms
    ):
        return _request_uncertain(
            request,
            "NATIVE_OBSERVATION_GEOMETRY_MISMATCH",
            source_sha256=source_sha256,
            native_observation=native_observation,
            geometry=(prepared.physical_start_ms, prepared.physical_end_ms),
        )
    if native_observation.get("source_media_sha256") != source_sha256:
        return _request_uncertain(
            request,
            "NATIVE_OBSERVATION_SOURCE_MISMATCH",
            source_sha256=source_sha256,
            native_observation=native_observation,
            geometry=(prepared.physical_start_ms, prepared.physical_end_ms),
        )

    if (
        native_observation.get("candidate_exposure") != "none"
        or native_observation.get("authority") != "EVIDENCE_ONLY"
        or native_observation.get("mutation_authorized") is not False
    ):
        return _request_uncertain(
            request,
            "NATIVE_OBSERVATION_AUTHORITY_INVALID",
            source_sha256=source_sha256,
            native_observation=native_observation,
            geometry=(prepared.physical_start_ms, prepared.physical_end_ms),
        )

    observed_provider = native_observation.get("provider")
    model = native_observation.get("model")
    audio_sha256 = native_observation.get("input_audio_sha256")
    if audio_sha256 is None:
        audio_sha256 = native_observation.get("audio_clip_sha256")
    response_sha256 = native_observation.get("response_sha256")
    if (
        observed_provider != provider
        or observed_provider not in NATIVE_MODELS
        or model != NATIVE_MODELS.get(observed_provider)
        or not _valid_sha256(audio_sha256)
        or not _valid_sha256(response_sha256)
    ):
        return _request_uncertain(
            request,
            "NATIVE_OBSERVATION_BINDING_INVALID",
            source_sha256=source_sha256,
            native_observation=native_observation,
            geometry=(prepared.physical_start_ms, prepared.physical_end_ms),
        )
    receipt_sha256 = native_observation.get("receipt_sha256")
    receipt_payload = dict(native_observation)
    receipt_payload.pop("receipt_sha256", None)
    try:
        receipt_valid = (
            _valid_sha256(receipt_sha256)
            and receipt_sha256 == native_digest(receipt_payload)
        )
    except (TypeError, ValueError):
        receipt_valid = False
    if not receipt_valid:
        return _request_uncertain(
            request,
            "NATIVE_OBSERVATION_RECEIPT_HASH_INVALID",
            source_sha256=source_sha256,
            native_observation=native_observation,
            geometry=(prepared.physical_start_ms, prepared.physical_end_ms),
        )

    result: dict[str, Any] = {
        "schema_version": WITNESS_SCHEMA,
        "witness_protocol": CANDIDATE_BLIND_TRANSCRIPT_PROTOCOL,
        "request_sha256": prepared.request_sha256,
        "status": "OBSERVED",
        "target_audible": True,
        "audibility_basis": "nonempty_provider_transcript",
        "candidate_exposure": "none",
        "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False,
        "exact_transcript": transcript,
        "source_media_sha256": source_sha256,
        "audio_clip_sha256": audio_sha256,
        "audio_start_ms": prepared.physical_start_ms,
        "audio_end_ms": prepared.physical_end_ms,
        "provider": observed_provider,
        "model": model,
        "response_sha256": response_sha256,
        "native_observation": dict(native_observation),
    }
    if isinstance(native_observation.get("served_from_cache"), bool):
        result["served_from_cache"] = native_observation["served_from_cache"]
    return result


def build_native_context_verifier(
    *,
    fallback: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    source_media: Path,
    output_dir: Path,
    provider: str,
    max_windows: object = None,
    max_audio_ms: object = None,
    enforce_limits: object = None,
    budget_receipt_path: Path | None = None,
    native_observation_cache_root: Path | None = None,
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    """Wrap the existing entity verifier with an opt-in native transcript lane.

    Only requests carrying the candidate-blind witness schema are selected. The
    selected native provider receives exact source-media geometry and nothing
    lexical. All other requests, including forced-choice entity requests, are
    delegated to ``fallback`` unchanged.
    """

    if provider not in {"mai", "moss"}:
        raise ValueError("native provider must explicitly be mai or moss")
    source_media = Path(source_media).absolute()
    output_dir = Path(output_dir).absolute()
    native_observation_cache_root = (
        output_dir
        if native_observation_cache_root is None
        else Path(native_observation_cache_root).absolute()
    )
    source_stat_binding = _source_identity(source_media)
    source_sha256 = _source_sha256(source_media)
    if _source_identity(source_media) != source_stat_binding:
        raise RuntimeError("native context source changed while hashing")
    native_observer = build_native_foreign_witness(
        source_media=source_media,
        # Raw exact audio/provider receipts may be shared by the direct and
        # independent lanes.  Keep witness envelopes on ``output_dir`` below;
        # only the provider-facing cache root is shared.
        output_dir=native_observation_cache_root,
        provider=provider,
        max_windows=max_windows,
        max_audio_ms=max_audio_ms,
        enforce_limits=enforce_limits,
        budget_receipt_path=budget_receipt_path,
    )
    successful_memo: dict[str, dict[str, Any]] = {}

    def _source_is_unchanged() -> bool:
        try:
            return _source_identity(source_media) == source_stat_binding
        except (OSError, ValueError):
            return False

    @records_program_errors
    def verify(request: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(request, Mapping) or request.get("schema_version") != WITNESS_REQUEST_SCHEMA:
            return fallback(request)
        try:
            prepared = _prepare_request(request)
        except _RequestInvalid as exc:
            return _request_uncertain(request, exc.reason_code, detail=exc.detail)

        geometry = (prepared.physical_start_ms, prepared.physical_end_ms)
        if not _source_is_unchanged():
            return _request_uncertain(
                request,
                "NATIVE_SOURCE_DRIFT",
                detail="source media stat binding changed",
                source_sha256=source_sha256,
                geometry=geometry,
            )
        cached = successful_memo.get(prepared.request_sha256)
        if cached is not None:
            if not _source_is_unchanged():
                return _request_uncertain(
                    request,
                    "NATIVE_SOURCE_DRIFT",
                    detail="source media stat binding changed",
                    source_sha256=source_sha256,
                    geometry=geometry,
                )
            return {**cached, "served_from_cache": True}

        try:
            with provider_boundary(provider), contract_boundary():
                native_observation = native_observer(
                    start_ms=prepared.physical_start_ms,
                    end_ms=prepared.physical_end_ms,
                )
        except (ContractRejection, ProviderFailure) as exc:
            reason_code, detail = _observer_failure(exc)
            uncertain = _request_uncertain(
                request,
                reason_code,
                detail=detail,
                source_sha256=source_sha256,
                geometry=geometry,
            )
            uncertain["review_error"] = review_error_record(exc)
            _write_envelope(
                output_dir=output_dir,
                prepared=prepared,
                witness=uncertain,
                native_observation=None,
                source_sha256=source_sha256,
            )
            return uncertain
        if not isinstance(native_observation, Mapping):
            uncertain = _request_uncertain(
                request,
                "NATIVE_OBSERVATION_INVALID",
                detail="native observer returned a non-object receipt",
                source_sha256=source_sha256,
                geometry=geometry,
            )
            return uncertain
        if not _source_is_unchanged():
            uncertain = _request_uncertain(
                request,
                "NATIVE_SOURCE_DRIFT",
                detail="source media stat binding changed during observation",
                source_sha256=source_sha256,
                native_observation=native_observation,
                geometry=geometry,
            )
            try:
                _write_envelope(
                    output_dir=output_dir,
                    prepared=prepared,
                    witness=uncertain,
                    native_observation=native_observation,
                    source_sha256=source_sha256,
                )
            except (OSError, TypeError, ValueError):
                pass
            return uncertain

        witness = _native_transcript_witness(
            request=request,
            prepared=prepared,
            native_observation=native_observation,
            source_sha256=source_sha256,
            provider=provider,
        )
        try:
            _write_envelope(
                output_dir=output_dir,
                prepared=prepared,
                witness=witness,
                native_observation=native_observation,
                source_sha256=source_sha256,
            )
        except (OSError, TypeError, ValueError):
            pass
        if witness.get("status") == "OBSERVED":
            successful_memo[prepared.request_sha256] = dict(witness)
        return witness

    def probe_witness_cache(request: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Return only a successful in-memory hit; never read disk or call a provider."""

        if not isinstance(request, Mapping) or request.get("schema_version") != WITNESS_REQUEST_SCHEMA:
            return None
        try:
            prepared = _prepare_request(request)
        except _RequestInvalid:
            return None
        if not _source_is_unchanged():
            return None
        cached = successful_memo.get(prepared.request_sha256)
        if cached is None:
            return None
        return {**cached, "served_from_cache": True}

    verify.probe_witness_cache = probe_witness_cache  # type: ignore[attr-defined]
    for seam in (
        "exact_source_transcript", "probe_exact_source_transcript_cache",
        "observe_secondary_audio_evidence", "observe_secondary_witnesses",
        "observe_gemini_range_context",
    ):
        method = getattr(fallback, seam, None)
        if callable(method):
            setattr(verify, seam, method)
    return verify


def _normalize_independent_providers(providers: Iterable[str]) -> tuple[str, ...]:
    """Validate the explicit native providers without consulting configuration."""

    if isinstance(providers, (str, bytes)):
        raise ValueError("native providers must be an explicit iterable of provider names")
    try:
        values = tuple(providers)
    except TypeError as exc:
        raise ValueError("native providers must be an explicit iterable") from exc
    normalized: list[str] = []
    for provider in values:
        if not isinstance(provider, str) or provider not in {"mai", "moss"}:
            raise ValueError("native provider must explicitly be mai or moss")
        if provider not in normalized:
            normalized.append(provider)
    return tuple(normalized)


def _independent_uncertain(
    request: Mapping[str, Any],
    provider: str,
    reason_code: str,
    *,
    detail: str = "",
    source_sha256: str | None = None,
    geometry: tuple[int, int] | None = None,
    native_observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = _request_uncertain(
        request,
        reason_code,
        detail=detail,
        source_sha256=source_sha256,
        geometry=geometry,
        native_observation=native_observation,
    )
    result["provider"] = provider
    result["model"] = NATIVE_MODELS.get(provider)
    return result


def _validate_independent_witness(
    witness: Mapping[str, Any],
    *,
    prepared: _PreparedRequest,
    provider: str,
    source_sha256: str,
) -> str | None:
    """Return a reason for a malformed result from an already-built verifier."""

    if witness.get("schema_version") != WITNESS_SCHEMA:
        return "schema_version"
    if witness.get("witness_protocol") != CANDIDATE_BLIND_TRANSCRIPT_PROTOCOL:
        return "witness_protocol"
    if witness.get("request_sha256") != prepared.request_sha256:
        return "request_sha256"
    if witness.get("status") not in {"OBSERVED", "UNCERTAIN"}:
        return "status"
    if (
        witness.get("candidate_exposure") != "none"
        or witness.get("authority") != "EVIDENCE_ONLY"
        or witness.get("mutation_authorized") is not False
    ):
        return "authority"
    if witness.get("status") == "OBSERVED":
        if witness.get("provider") != provider or witness.get("model") != NATIVE_MODELS.get(provider):
            return "provider"
    elif (
        witness.get("provider") not in {None, provider}
        or witness.get("model") not in {None, NATIVE_MODELS.get(provider)}
    ):
        return "provider"
    if witness.get("source_media_sha256") != source_sha256:
        return "source_media_sha256"
    if (
        witness.get("audio_start_ms") != prepared.physical_start_ms
        or witness.get("audio_end_ms") != prepared.physical_end_ms
    ):
        return "audio_geometry"
    if witness.get("status") == "OBSERVED":
        if not isinstance(witness.get("exact_transcript"), str) or not witness["exact_transcript"].strip():
            return "exact_transcript"
        if not isinstance(witness.get("native_observation"), Mapping):
            return "native_observation"
    return None


class _RangeArtifactInvalid(ValueError):
    """The native range receipt cannot be assembled without laundered files."""


class _RangeRecoveryInvalid(ValueError):
    """A pre-dispatch configuration failure is not safe to recover."""


class _RangeRecoveryAttemptExists(_RangeRecoveryInvalid):
    """The canonical source budget already records this provider/window."""


class _NativeAcquisitionIdentityMissing(_RangeRecoveryInvalid):
    """No original attempt authorizes replay of this acquisition."""


class _NativeEnvelopeInvalid(_RangeRecoveryInvalid):
    reason_code = "NATIVE_RANGE_ENVELOPE_INVALID"


def _acquisition_replay_reason(exc: Exception) -> str:
    if isinstance(exc, _NativeAcquisitionIdentityMissing):
        return "NATIVE_ACQUISITION_ORIGINAL_IDENTITY_MISSING"
    if isinstance(exc, FileNotFoundError):
        return "NATIVE_ACQUISITION_ARTIFACT_REPLAY_REQUIRED"
    return getattr(exc, "reason_code", None) or "NATIVE_ACQUISITION_LOCAL_CONTRACT_ERROR"


def _path_has_symlink(path: Path) -> bool:
    """Reject a path or any existing parent that would redirect a receipt."""

    return any(item.is_symlink() for item in (path, *path.parents))


def _persist_immutable_bytes(path: Path, data: bytes) -> None:
    """Create a derived range artifact, never overwrite an existing one."""

    if _path_has_symlink(path):
        raise _RangeArtifactInvalid("native range artifact path is a symlink")
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise _RangeArtifactInvalid(str(exc)) from exc
    if path.exists():
        if not path.is_file() or path.read_bytes() != data:
            raise _RangeArtifactInvalid("native range artifact bytes changed")
        return
    try:
        path.write_bytes(data)
    except OSError as exc:
        raise _RangeArtifactInvalid(str(exc)) from exc


def _persist_immutable_json(path: Path, document: Mapping[str, Any]) -> None:
    try:
        data = (
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _RangeArtifactInvalid("native range JSON is not serializable") from exc
    _persist_immutable_bytes(path, data)


def _load_json_mapping(path: Path) -> Mapping[str, Any]:
    if _path_has_symlink(path) or not path.is_file():
        raise _RangeArtifactInvalid("native range provider artifact is missing")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise _RangeArtifactInvalid("native range provider artifact is invalid") from exc
    if not isinstance(value, Mapping):
        raise _RangeArtifactInvalid("native range provider artifact is not an object")
    return value


def _validate_recovery_provider_configuration(provider: str) -> str | None:
    """Return a typed block reason unless the current provider config is ready.

    This intentionally calls the existing runtime resolver and provider
    validators.  It only keeps their values in memory; no credential value is
    returned or included in an evidence row.
    """

    try:
        from src.autoslice.native_audio_runtime import resolve_native_audio_environment
        from src.autoslice.mai_transcription import _endpoint_url, _read_api_key
        from src.autoslice.moss_transcription import _read_api_key as _read_moss_api_key

        ambient = dict(os.environ)
        environment = resolve_native_audio_environment(provider, environ=ambient)
        values = ambient if environment is None else environment
        if provider == "mai":
            _endpoint_url(values)
            _read_api_key(values)
        elif provider == "moss":
            _read_moss_api_key(values)
        else:
            return "NATIVE_RANGE_RECOVERY_CONFIG_NOT_READY"
    except Exception:
        # Do not expose validator messages: they may contain a configured path
        # or provider detail.  The caller only needs a typed readiness gate.
        return "NATIVE_RANGE_RECOVERY_CONFIG_NOT_READY"
    return None


def _read_bound_uncertain_envelope(
    *,
    envelope_path: Path,
    prepared: _PreparedRequest,
    provider: str,
    source_sha256: str,
) -> tuple[bytes, str, Mapping[str, Any]]:
    """Read a bound original failure without granting permission to retry."""

    if _path_has_symlink(envelope_path) or not envelope_path.is_file():
        raise _RangeRecoveryInvalid("native range envelope is missing or unsafe")
    try:
        before = envelope_path.stat()
        raw = envelope_path.read_bytes()
        after = envelope_path.stat()
    except OSError as exc:
        raise _RangeRecoveryInvalid("native range envelope cannot be read") from exc
    identity = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, key) != getattr(after, key) for key in identity):
        raise _RangeRecoveryInvalid("native range envelope changed while reading")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise _RangeRecoveryInvalid("native range envelope is invalid") from exc
    if not isinstance(document, Mapping):
        raise _RangeRecoveryInvalid("native range envelope is not an object")
    if set(document) - {"native_observation"} != {
        "schema_version",
        "request_sha256",
        "request",
        "witness",
        "source_media_sha256",
        "audio_start_ms",
        "audio_end_ms",
    }:
        raise _RangeRecoveryInvalid("native range envelope shape is invalid")
    if (
        document.get("schema_version") != NATIVE_CONTEXT_ENVELOPE_SCHEMA
        or document.get("request_sha256") != prepared.request_sha256
        or document.get("request") != prepared.request
        or document.get("source_media_sha256") != source_sha256
        or document.get("audio_start_ms") != prepared.physical_start_ms
        or document.get("audio_end_ms") != prepared.physical_end_ms
    ):
        raise _RangeRecoveryInvalid("native range envelope binding is invalid")

    witness = document.get("witness")
    if not isinstance(witness, Mapping):
        raise _RangeRecoveryInvalid("native envelope witness is invalid")
    if document.get("native_observation") != witness.get("native_observation"):
        raise _RangeRecoveryInvalid("native envelope observation binding is invalid")
    reason_code = witness.get("reason_code")
    if (
        witness.get("schema_version") != WITNESS_SCHEMA
        or witness.get("witness_protocol") != CANDIDATE_BLIND_TRANSCRIPT_PROTOCOL
        or witness.get("request_sha256") != prepared.request_sha256
        or witness.get("status") != "UNCERTAIN"
        or witness.get("candidate_exposure") != "none"
        or witness.get("authority") != "EVIDENCE_ONLY"
        or witness.get("mutation_authorized") is not False
        or witness.get("provider") != provider
        or witness.get("model") != NATIVE_MODELS.get(provider)
        or witness.get("source_media_sha256") != source_sha256
        or witness.get("audio_start_ms") != prepared.physical_start_ms
        or witness.get("audio_end_ms") != prepared.physical_end_ms
        or not isinstance(reason_code, str)
        or not reason_code
    ):
        raise _RangeRecoveryInvalid("native envelope failure binding is invalid")
    return raw, digest, document


def _read_recoverable_pre_http_envelope(**kwargs) -> tuple[bytes, str]:
    raw, digest, document = _read_bound_uncertain_envelope(**kwargs)
    witness = document['witness']
    reason = witness['reason_code']
    if ('native_observation' in document or 'native_observation' in witness
            or reason not in _PRE_HTTP_CONFIG_FAILURES
            or not reason.startswith(f"{kwargs['provider'].upper()}_")):
        raise _RangeRecoveryInvalid('native envelope is not a recoverable config failure')
    return raw, digest


def _validate_recovery_budget_receipt(
    *,
    source_media: Path,
    source_sha256: str,
    receipt_path: Path,
) -> None:
    """Validate the occupied canonical receipt before constructing a verifier."""

    if _path_has_symlink(receipt_path) or not receipt_path.is_file():
        raise _RangeRecoveryInvalid("native range canonical budget receipt is unavailable")
    if _path_has_symlink(source_media) or not source_media.is_file():
        raise _RangeRecoveryInvalid("native range source media is unavailable")
    from src.autoslice.native_audio_budget_receipt import (
        _load_existing,
        _validate_budget_history,
        _validate_existing,
    )

    try:
        existing = _load_existing(receipt_path)
        if existing is None:
            raise _RangeRecoveryInvalid("native range canonical budget receipt is missing")
        _validate_existing(existing, source_media_sha256=source_sha256)
        _validate_budget_history(existing["budget"])
    except _RangeRecoveryInvalid:
        raise
    except Exception as exc:
        raise _RangeRecoveryInvalid("native range canonical budget receipt is invalid") from exc
    budget = existing.get("budget")
    expected_source = str(source_media.resolve())
    if not isinstance(budget, Mapping) or budget.get("source_media") != expected_source:
        raise _RangeRecoveryInvalid("native range canonical budget source drift")


def _archive_pre_dispatch_envelope(
    *,
    envelope_path: Path,
    prepared: _PreparedRequest,
    raw: bytes,
    raw_sha256: str,
) -> dict[str, str]:
    """Retain exact pre-HTTP bytes beside the provider native-context files."""

    history_path = (
        envelope_path.parent
        / "pre-dispatch-history"
        / f"{prepared.request_sha256}-{raw_sha256}.json"
    )
    try:
        _persist_immutable_bytes(history_path, raw)
    except _RangeArtifactInvalid:
        raise
    return {
        "pre_dispatch_history_path": str(history_path),
        "pre_dispatch_history_sha256": raw_sha256,
    }


def _materialize_native_range_artifacts(
    *,
    check_request: Mapping[str, Any],
    prepared: _PreparedRequest,
    provider: str,
    source_media: Path,
    source_sha256: str,
    output_dir: Path,
    native_observation_cache_root: Path,
    replay_envelope_path: Path | None = None,
    replay_envelope_sha256: str | None = None,
) -> dict[str, Any]:
    """Bind the native writer's original files into the range validator shape.

    ``build_native_foreign_witness`` already owns exact audio extraction and
    provider receipt persistence.  This helper only discovers those existing
    files and writes the small candidate-blind range input, reviewed SRT, and
    parsed raw-response copy needed by ``validate_native_range_context_observation``.
    Provider receipts and native envelopes are read-only here.
    """

    current_indexes = check_request.get("cue_indexes")
    if not isinstance(current_indexes, list) or len(current_indexes) != 1:
        raise _RangeArtifactInvalid("native range current cue is not singular")
    current_index = current_indexes[0]
    if type(current_index) is not int or current_index < 1:
        raise _RangeArtifactInvalid("native range current cue index is invalid")
    srt_text = check_request.get("whole_clip_current_srt")
    if not isinstance(srt_text, str):
        raise _RangeArtifactInvalid("native range reviewed SRT is missing")
    source_path = Path(source_media).absolute()
    if _path_has_symlink(source_path) or not source_path.is_file():
        raise _RangeArtifactInvalid("native range source media is missing")

    acquisition = native_acquisition_identity(source_sha256=source_sha256, provider=provider,
        start_ms=prepared.physical_start_ms, end_ms=prepared.physical_end_ms)
    job = native_acquisition_path(native_observation_cache_root, acquisition)
    audio_path = job / "input.mp3"
    receipt_path = audio_path.with_suffix(f".audio-evidence.{provider}.json")
    envelope_path = (
        output_dir / provider / "native-context" / f"{prepared.request_sha256}.json"
    )
    if replay_envelope_path is not None:
        envelope_path = replay_envelope_path
    for path in (audio_path, receipt_path, envelope_path):
        if _path_has_symlink(path) or not path.is_file():
            raise _RangeArtifactInvalid("native range writer artifact is missing")

    # A blind cache alias retains its original request and immutable envelope.
    # It does not require a newly signed copy under the current metadata key.
    artifact_prepared = prepared
    if replay_envelope_path is not None:
        if replay_envelope_sha256 != _source_sha256(envelope_path):
            raise _RangeArtifactInvalid("native range replay envelope changed")
        envelope = _load_json_mapping(envelope_path)
        original = _prepare_request(envelope["request"])
        geometry_keys = ("matched_start_ms", "matched_end_ms", "context_start_ms",
                         "context_end_ms", "source_media_timeline_offset_ms")
        if any(original.request[k] != prepared.request[k] for k in geometry_keys):
            raise _RangeArtifactInvalid("native range replay geometry changed")
        artifact_prepared = original

    provider_receipt = _load_json_mapping(receipt_path)
    evidence = provider_receipt.get("evidence")
    raw_response = evidence.get("raw_response") if isinstance(evidence, Mapping) else None
    if not isinstance(evidence, Mapping) or not isinstance(raw_response, Mapping):
        raise _RangeArtifactInvalid("native range provider raw response is missing")

    # The range evidence is bound to both the candidate-blind request and the
    # exact current SRT bytes.  A later range or SRT revision must get a new
    # immutable directory; otherwise the old reviewed.srt/input.json would be
    # mistaken for the new request and either reject the request or launder a
    # stale context receipt into it.
    current_srt_sha256 = hashlib.sha256(srt_text.encode("utf-8")).hexdigest()
    range_dir = (
        output_dir
        / "range-validation"
        / f"cue{current_index}"
        / prepared.request_sha256
        / current_srt_sha256
        / provider
    )
    reviewed_srt_path = range_dir / "reviewed.srt"
    input_path = range_dir / "input.json"
    source_raw_response_path = job / "raw-response.json"
    if source_raw_response_path.exists() or source_raw_response_path.is_symlink():
        if _load_json_mapping(source_raw_response_path) != raw_response:
            raise _RangeArtifactInvalid("native range raw response bytes changed")
        raw_response_path = source_raw_response_path
    else:
        raw_response_path = range_dir / "raw-response.json"
    _persist_immutable_bytes(reviewed_srt_path, srt_text.encode("utf-8"))
    if raw_response_path == source_raw_response_path:
        # The provider's original response file is already immutable and is
        # bound directly; do not rewrite it into a normalized JSON spelling.
        pass
    else:
        _persist_immutable_json(raw_response_path, raw_response)

    clip_binding = check_request.get("clip_context_binding")
    candidate_id = clip_binding.get("candidate_id") if isinstance(clip_binding, Mapping) else None
    if not isinstance(candidate_id, str) or not candidate_id:
        raise _RangeArtifactInvalid("native range candidate identity is missing")
    input_doc = {
        "schema_version": "story-range-native-range-evidence-input.v1",
        "candidate_blind": True,
        "candidate_id": candidate_id,
        "candidate_text_in_request": False,
        "delivery_window": {
            "cue_indexes": list(artifact_prepared.request["cue_indexes"]),
            "start_ms": artifact_prepared.target_start_ms,
            "end_ms": artifact_prepared.target_end_ms,
        },
        "publication": "NOT_REQUESTED",
        "reviewed_srt_path": str(reviewed_srt_path),
        "reviewed_srt_sha256": _source_sha256(reviewed_srt_path),
        "source_media_path": str(source_path),
        "source_media_sha256": source_sha256,
        "source_media_timeline_offset_ms": prepared.source_offset_ms,
        "srt_mutation": False,
        "witness_request": dict(artifact_prepared.request),
        "witness_request_sha256": artifact_prepared.request_sha256,
    }
    _persist_immutable_json(input_path, input_doc)

    def binding(path: Path) -> tuple[str, str]:
        return str(path), _source_sha256(path)

    paths = {
        "input": input_path,
        "native_envelope": envelope_path,
        "provider_receipt": receipt_path,
        "audio_clip": audio_path,
        "source_media": source_path,
        "reviewed_srt": reviewed_srt_path,
        "raw_response": raw_response_path,
    }
    bindings: dict[str, Any] = {}
    for role, path in paths.items():
        path_value, path_sha = binding(path)
        bindings[f"{role}_path"] = path_value
        bindings[f"{role}_sha256"] = path_sha
    return {"artifact_bindings": bindings}


def build_independent_context_witnesses(
    fallback: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    source_media: Path,
    output_dir: Path,
    providers: Iterable[str],
    budget_receipt_path: Path | None = None,
    native_observation_cache_root: Path | None = None,
    cached_observation_roots: tuple[Path, ...] = (),
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    """Expose opt-in MAI/MOSS evidence while leaving the ordinary lane untouched.

    Calling the returned verifier always delegates to ``fallback``.  The
    candidate-blind native providers are constructed and invoked only through
    ``observe_independent_witnesses``.  Its memo is keyed by source, physical
    target geometry, and provider/model, so request metadata can be rebound to
    the same already-observed audio without dispatching another provider call.
    ``budget_receipt_path`` lets a normal entity factory share its canonical
    source-budget receipt with this independent lane; omitted keeps the
    historical ``output_dir/native-audio-budget.json`` behavior.
    ``native_observation_cache_root`` lets direct and independent lanes share
    the provider-facing exact-audio receipts while retaining independent
    witness envelopes under their respective output directories.
    """

    provider_names = _normalize_independent_providers(providers)
    source_media = Path(source_media).absolute()
    output_dir = Path(output_dir).absolute()
    native_observation_cache_root = (
        output_dir
        if native_observation_cache_root is None
        else Path(native_observation_cache_root).absolute()
    )
    provider_verifiers: dict[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] = {}
    provider_build_failures: dict[str, ContractRejection | ProviderFailure] = {}
    physical_memo: dict[str, dict[str, Any]] = {}
    shared_budget_receipt = (
        output_dir / "native-audio-budget.json"
        if budget_receipt_path is None
        else Path(budget_receipt_path).absolute()
    )
    default_budget_receipt = (output_dir / "native-audio-budget.json").absolute()
    source_stat_binding: tuple[int, ...] | None = None
    source_sha256: str | None = None
    source_binding_failure: tuple[str, str] | None = None

    def _source_state() -> str:
        """Return ready/drift/unavailable without reusing stale evidence."""

        nonlocal source_stat_binding, source_sha256, source_binding_failure
        if source_binding_failure is not None:
            return "unavailable"
        if source_stat_binding is None:
            try:
                binding = _source_identity(source_media)
                digest = _source_sha256(source_media)
                if _source_identity(source_media) != binding:
                    raise OSError("source media changed while hashing")
            except (OSError, ValueError) as exc:
                source_binding_failure = ("NATIVE_SOURCE_UNAVAILABLE", _sanitized_detail(exc))
                return "unavailable"
            source_stat_binding = binding
            source_sha256 = digest
            return "ready"
        try:
            unchanged = _source_identity(source_media) == source_stat_binding
        except (OSError, ValueError):
            unchanged = False
        return "ready" if unchanged else "drift"

    @records_program_errors
    def _provider_verifier(provider: str):
        if provider in provider_verifiers:
            return provider_verifiers[provider]
        if provider in provider_build_failures:
            return None
        try:
            if (
                budget_receipt_path is not None
                and shared_budget_receipt != default_budget_receipt
                and (
                    default_budget_receipt.exists()
                    or default_budget_receipt.is_symlink()
                )
            ):
                raise _LegacyBudgetReceiptMigrationRequired(
                    "canonical native budget receipt differs while the output "
                    "directory still contains a legacy receipt"
                )
            if not (shared_budget_receipt.exists() or shared_budget_receipt.is_symlink()):
                legacy_receipts = [
                    output_dir / legacy_provider / "native-audio-budget.json"
                    for legacy_provider in ("mai", "moss")
                    if (
                        output_dir / legacy_provider / "native-audio-budget.json"
                    ).exists()
                    or (
                        output_dir / legacy_provider / "native-audio-budget.json"
                    ).is_symlink()
                ]
                if legacy_receipts:
                    raise _LegacyBudgetReceiptMigrationRequired(
                        "independent native budget receipt is missing while legacy "
                        "provider receipt exists"
                    )
            with provider_boundary(provider), contract_boundary():
                verifier = build_native_context_verifier(
                    fallback=fallback,
                    source_media=source_media,
                    output_dir=output_dir / provider,
                    provider=provider,
                    budget_receipt_path=shared_budget_receipt,
                    native_observation_cache_root=native_observation_cache_root,
                )
        except (ContractRejection, ProviderFailure) as exc:
            provider_build_failures[provider] = exc
            return None
        provider_verifiers[provider] = verifier
        return verifier

    def _cache_key(prepared: _PreparedRequest, provider: str) -> str:
        return native_digest(native_acquisition_identity(source_sha256=source_sha256 or "",
            provider=provider, start_ms=prepared.physical_start_ms, end_ms=prepared.physical_end_ms))

    def _cache_recollection(prepared, provider):
        """An inherited missing cache cannot authorize reuse of its envelope.

        Ordinary acquisition still owns dispatch and charging. A separate output
        namespace preserves old envelopes, including their original conclusions.
        The manifest is cache eligibility only; retained ledger identity is checked
        independently before it can select this path.
        """
        manifest_path = native_observation_cache_root / 'parent.json'
        if not manifest_path.exists():
            return None
        manifest = _load_json_mapping(manifest_path)
        identity = native_acquisition_identity(source_sha256=source_sha256 or '', provider=provider,
            start_ms=prepared.physical_start_ms, end_ms=prepared.physical_end_ms)
        job = native_acquisition_path(native_observation_cache_root, identity)
        record = manifest.get('unusable_caches', {}).get(str(job.relative_to(native_observation_cache_root)))
        if record is None:
            return None
        _validate_recovery_budget_receipt(source_media=source_media,
            source_sha256=source_sha256, receipt_path=shared_budget_receipt)
        account = _load_json_mapping(shared_budget_receipt)['budget']
        from src.autoslice.prepared_native_audio_recovery import _unavailable_cache
        try:
            _unavailable_cache(root=native_observation_cache_root,
                relative=str(job.relative_to(native_observation_cache_root)),
                source_sha=source_sha256, budget=account, manifest=manifest)
        except ValueError as exc:
            if str(exc) == 'PREPARED_NATIVE_UNAVAILABLE_CACHE_IDENTITY_MISSING':
                raise _NativeAcquisitionIdentityMissing(str(exc)) from exc
            raise
        if any(any(a[k] != identity[k] for k in ('provider','model','start_ms','end_ms'))
               for a in record['attempts']):
            raise _NativeAcquisitionIdentityMissing('unusable native cache original identity is missing')
        retry_dir = native_observation_cache_root / 'cache-reacquisition' / native_digest(record)
        if output_dir == retry_dir:
            return None
        exact = output_dir / provider / 'native-context' / f'{prepared.request_sha256}.json'
        if exact.exists() or exact.is_symlink():
            # Absence never excuses a damaged or definite failed envelope.
            # Only a valid old success is ineligible evidence due to cache loss.
            try:
                envelope = _load_json_mapping(exact)
                original = _prepare_request(envelope['request'])
                valid = (envelope.get('schema_version') == NATIVE_CONTEXT_ENVELOPE_SCHEMA
                    and original.request == prepared.request
                    and envelope.get('request_sha256') == prepared.request_sha256
                    and envelope.get('source_media_sha256') == source_sha256
                    and envelope.get('audio_start_ms') == prepared.physical_start_ms
                    and envelope.get('audio_end_ms') == prepared.physical_end_ms
                    and _native_transcript_witness(request=original.request, prepared=original,
                        provider=provider, source_sha256=source_sha256,
                        native_observation=envelope['native_observation']).get('status') == 'OBSERVED')
            except (OSError, ValueError, KeyError, TypeError):
                valid = False
            if not valid:
                return None
        return build_independent_context_witnesses(fallback=fallback, source_media=source_media,
            output_dir=retry_dir, providers=(provider,), budget_receipt_path=shared_budget_receipt,
            native_observation_cache_root=native_observation_cache_root)


    def _persist_result(
        *,
        provider: str,
        prepared: _PreparedRequest,
        result: Mapping[str, Any],
        source_sha: str,
    ) -> None:
        native_observation = result.get("native_observation")
        try:
            _write_envelope(
                output_dir=output_dir / provider,
                prepared=prepared,
                witness=result,
                native_observation=(
                    native_observation if isinstance(native_observation, Mapping) else None
                ),
                source_sha256=source_sha,
            )
        except (OSError, TypeError, ValueError):
            # The provider verifier's receipt remains authoritative; an envelope
            # write failure must not trigger a second provider dispatch.
            pass

    def _rebind(
        result: Mapping[str, Any],
        *,
        prepared: _PreparedRequest,
        provider: str,
        source_sha: str,
    ) -> dict[str, Any]:
        rebound = dict(result)
        rebound["request_sha256"] = prepared.request_sha256
        rebound["served_from_cache"] = True
        _persist_result(
            provider=provider,
            prepared=prepared,
            result=rebound,
            source_sha=source_sha,
        )
        return rebound

    @records_program_errors
    def _observe_one_independent(
        request: Mapping[str, Any],
        prepared: _PreparedRequest,
        provider: str,
        source_sha: str,
    ) -> Mapping[str, Any]:
        """Acquire one provider result while preserving the shared memo/writer.

        The range seam needs provider-granular cache recovery: a valid MAI
        envelope must not suppress MOSS's first request, while an existing
        MOSS envelope (including a failed one) must never be overwritten by a
        cold retry.  Keeping this exact ordinary observer body in one helper
        lets both seams share the same physical-window memo and persistence.
        """

        geometry = (prepared.physical_start_ms, prepared.physical_end_ms)
        if _source_state() != "ready":
            return _independent_uncertain(
                request,
                provider,
                "NATIVE_SOURCE_DRIFT",
                detail="source media stat binding changed",
                source_sha256=source_sha,
                geometry=geometry,
            )

        key = _cache_key(prepared, provider)
        cached = physical_memo.get(key)
        if cached is not None:
            return _rebind(
                cached,
                prepared=prepared,
                provider=provider,
                source_sha=source_sha,
            )

        verifier = _provider_verifier(provider)
        if verifier is None:
            failure = provider_build_failures[provider]
            reason, detail = _observer_failure(failure)
            result: Mapping[str, Any] = _independent_uncertain(
                request,
                provider,
                reason,
                detail=detail,
                source_sha256=source_sha,
                geometry=geometry,
            )
            result["review_error"] = review_error_record(failure)
        else:
            try:
                with provider_boundary(provider), contract_boundary():
                    observed = verifier(request)
            except (ContractRejection, ProviderFailure) as exc:
                reason, detail = _observer_failure(exc)
                result = _independent_uncertain(
                    request,
                    provider,
                    reason,
                    detail=detail,
                    source_sha256=source_sha,
                    geometry=geometry,
                )
                result["review_error"] = review_error_record(exc)
            else:
                if not isinstance(observed, Mapping):
                    result = _independent_uncertain(
                        request,
                        provider,
                        "NATIVE_INDEPENDENT_OBSERVATION_INVALID",
                        detail="native verifier returned a non-object witness",
                        source_sha256=source_sha,
                        geometry=geometry,
                    )
                else:
                    invalid_reason = _validate_independent_witness(
                        observed,
                        prepared=prepared,
                        provider=provider,
                        source_sha256=source_sha,
                    )
                    if invalid_reason is not None:
                        result = _independent_uncertain(
                            request,
                            provider,
                            "NATIVE_INDEPENDENT_OBSERVATION_INVALID",
                            detail=f"invalid native witness field: {invalid_reason}",
                            source_sha256=source_sha,
                            geometry=geometry,
                        )
                    else:
                        result = dict(observed)
                        result.setdefault("provider", provider)
                        result.setdefault("model", NATIVE_MODELS.get(provider))

        if _source_state() != "ready":
            result = _independent_uncertain(
                request,
                provider,
                "NATIVE_SOURCE_DRIFT",
                detail="source media stat binding changed during observation",
                source_sha256=source_sha,
                geometry=geometry,
            )
        else:
            physical_memo[key] = dict(result)
            _persist_result(
                provider=provider,
                prepared=prepared,
                result=result,
                source_sha=source_sha,
            )
        return result

    def observe_independent_witnesses(request: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        if not provider_names:
            return []
        request_mapping = request if isinstance(request, Mapping) else {}
        try:
            prepared = _prepare_request(request_mapping)
        except _RequestInvalid as exc:
            return [
                _independent_uncertain(
                    request_mapping,
                    provider,
                    exc.reason_code,
                    detail=exc.detail,
                )
                for provider in provider_names
            ]

        state = _source_state()
        geometry = (prepared.physical_start_ms, prepared.physical_end_ms)
        if state != "ready":
            if state == "drift":
                reason, detail = "NATIVE_SOURCE_DRIFT", "source media stat binding changed"
            else:
                reason, detail = source_binding_failure or (
                    "NATIVE_SOURCE_UNAVAILABLE",
                    "source media is unavailable",
                )
            return [
                _independent_uncertain(
                    request_mapping,
                    provider,
                    reason,
                    detail=detail,
                    source_sha256=source_sha256,
                    geometry=geometry,
                )
                for provider in provider_names
            ]

        assert source_sha256 is not None
        cached_rows = observe_cached_independent_witnesses(request_mapping)
        results = []
        for provider in provider_names:
            recollect = _cache_recollection(prepared, provider)
            if recollect is not None:
                results.extend(recollect.observe_independent_witnesses(request_mapping))
                continue
            cached = [
                row for row in cached_rows
                if isinstance(row, Mapping) and row.get("status") == "OBSERVED"
                and row.get("provider") == provider
            ]
            if cached:
                # Distinct bound acquisitions can disagree. Preserve all of
                # them for CPA; downstream route voting deduplicates copies.
                results.extend(cached)
            else:
                results.append(_observe_independent_with_recovery(
                    request_mapping, prepared, provider, source_sha256))
        return results

    def _observe_independent_with_recovery(request, prepared, provider, source_sha):
        """Use the range lane's zero-attempt recovery for ordinary cue slots.

        Successful observations above remain exact cache hits. An uncertain
        envelope is immutable unless the existing shared recovery contract
        proves pre-HTTP configuration failure and an unspent provider/window.
        """
        path = output_dir / provider / 'native-context' / f'{prepared.request_sha256}.json'
        if not (path.exists() or path.is_symlink()):
            return _observe_one_independent(request, prepared, provider, source_sha)

        def blocked(reason):
            return _independent_uncertain(request, provider, reason,
                source_sha256=source_sha,
                geometry=(prepared.physical_start_ms, prepared.physical_end_ms))

        try:
            recovered, history = _replay_acquisition(prepared, provider, path)
        except (OSError, ValueError, TypeError, KeyError):
            pass
        else:
            return {**recovered, **history}
        try:
            raw, sha = _read_recoverable_pre_http_envelope(envelope_path=path,
                prepared=prepared, provider=provider, source_sha256=source_sha)
        except _RangeRecoveryInvalid:
            try:
                _, _, original = _read_bound_uncertain_envelope(envelope_path=path,
                    prepared=prepared, provider=provider, source_sha256=source_sha)
            except _RangeRecoveryInvalid:
                return blocked('NATIVE_RANGE_ENVELOPE_INVALID')
            # A legitimate provider failure is immutable evidence, not a
            # corrupt envelope and not permission for another HTTP attempt.
            return {**original['witness'], 'served_from_cache': True}
        reason = _validate_recovery_provider_configuration(provider)
        if reason is not None:
            return blocked(reason)
        try:
            observed, history = _recover_pre_http_configuration_failure(
                request=request, prepared=prepared, provider=provider,
                source_sha=source_sha, envelope_path=path,
                old_envelope=raw, old_envelope_sha256=sha)
        except _RangeRecoveryAttemptExists:
            return blocked('NATIVE_RANGE_RECOVERY_ATTEMPT_EXISTS')
        except _RangeRecoveryInvalid:
            return blocked('NATIVE_RANGE_RECOVERY_RECEIPT_INVALID')
        return {**observed, **history}

    def _validate_range_dispatch_inputs(
        *,
        check_request: Mapping[str, Any],
        current_request: Mapping[str, Any],
        current_prepared: _PreparedRequest,
        prepared: _PreparedRequest,
        source_sha: str,
    ) -> tuple[str, str] | None:
        """Reject stale current context before constructing any provider.

        The range adjudicator repeats these bindings after the files exist.  A
        cheap preflight is still required: malformed current SRT/cue/source
        input must not consume a provider budget, and a new request must not
        be mistaken for an old range artifact.  Use its existing cue and
        provenance helpers so both checks have the same geometry semantics.
        """

        from src.autoslice.independent_context_adjudication import (
            _range_cue_rows,
            _range_media_source_binding,
            _same_digest,
        )

        srt_text = check_request.get("whole_clip_current_srt")
        if not isinstance(srt_text, str) or not srt_text:
            return "NATIVE_RANGE_CURRENT_SRT_INVALID", "current whole-clip SRT is missing"

        current_indexes = current_request.get("cue_indexes")
        if (
            not isinstance(current_indexes, list)
            or len(current_indexes) != 1
            or type(current_indexes[0]) is not int
            or current_indexes[0] < 1
        ):
            return "NATIVE_RANGE_CURRENT_CUE_INVALID", "current cue identity is not singular"
        current_index = current_indexes[0]

        clip_binding = check_request.get("clip_context_binding")
        candidate_id = clip_binding.get("candidate_id") if isinstance(clip_binding, Mapping) else None
        sources = clip_binding.get("source_media_sha256s") if isinstance(clip_binding, Mapping) else None
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            return "NATIVE_RANGE_GEOMETRY_OR_CANDIDATE_DRIFT", "candidate identity is missing"
        if not isinstance(sources, list) or not sources or any(
            not isinstance(value, str) or not value for value in sources
        ):
            return "NATIVE_RANGE_CURRENT_SOURCE_DRIFT", "source media identity is missing"

        current_rows = _range_cue_rows(
            srt_text,
            [current_index],
            current_prepared.target_start_ms,
            current_prepared.target_end_ms,
            allow_boundary_gaps=False,
        )
        if not current_rows or current_rows[0].get("coverage") != "FULL":
            return "NATIVE_RANGE_CURRENT_CUE_DRIFT", "current geometry does not cover one actual cue"
        current_row = current_rows[0]
        if (
            current_row.get("start_ms") != current_prepared.target_start_ms
            or current_row.get("end_ms") != current_prepared.target_end_ms
            or check_request.get("current_cue") != current_row.get("text")
        ):
            return "NATIVE_RANGE_CURRENT_CUE_DRIFT", "current cue text or window differs from SRT"

        # Some producer request shapes carry a declared current-SRT or cue
        # digest; when present, bind it to the bytes/text actually parsed.
        current_srt_sha = hashlib.sha256(srt_text.encode("utf-8")).hexdigest()
        for document in (check_request, clip_binding if isinstance(clip_binding, Mapping) else {}):
            for key in ("whole_clip_current_srt_sha256", "current_srt_sha256", "reviewed_srt_sha256"):
                declared = document.get(key)
                if declared is not None and not _same_digest(declared, current_srt_sha):
                    return "NATIVE_RANGE_CURRENT_SRT_HASH_DRIFT", f"{key} does not bind current SRT"
        for key in ("current_cue_sha256", "current_text_sha256", "base_text_sha256", "cue_text_sha256"):
            declared = check_request.get(key)
            if declared is not None and not _same_digest(declared, current_row["text_sha256"]):
                return "NATIVE_RANGE_CURRENT_TEXT_HASH_DRIFT", f"{key} does not bind current cue"

        # A previously materialized range with the same request/SRT identity
        # is immutable.  Compare its candidate binding before any provider
        # path, so a changed current candidate cannot dispatch and only fail
        # later while trying to rewrite input.json.
        for provider in provider_names:
            prior_input = (
                output_dir
                / "range-validation"
                / f"cue{current_index}"
                / prepared.request_sha256
                / current_srt_sha
                / provider
                / "input.json"
            )
            if not (prior_input.exists() or prior_input.is_symlink()):
                continue
            try:
                prior_document = _load_json_mapping(prior_input)
            except _RangeArtifactInvalid as exc:
                return "NATIVE_RANGE_ARTIFACT_BINDING_INVALID", _sanitized_detail(exc)
            if prior_document.get("candidate_id") != candidate_id:
                return "NATIVE_RANGE_GEOMETRY_OR_CANDIDATE_DRIFT", "candidate identity changed for immutable range"

        range_indexes = prepared.request.get("cue_indexes")
        if not isinstance(range_indexes, list) or any(type(index) is not int for index in range_indexes):
            return "NATIVE_RANGE_GEOMETRY_INVALID", "range cue indexes are invalid"
        range_rows = _range_cue_rows(
            srt_text,
            range_indexes,
            prepared.target_start_ms,
            prepared.target_end_ms,
            allow_boundary_gaps=True,
        )
        if range_rows is None or current_index not in range_indexes:
            return "NATIVE_RANGE_GEOMETRY_INVALID", "range does not cover current cue"
        current_range_row = next(
            (row for row in range_rows if row.get("cue_index") == current_index), None
        )
        if current_range_row is None or current_range_row.get("coverage") != "FULL":
            return "NATIVE_RANGE_GEOMETRY_INVALID", "range current cue is not fully covered"

        # This is the same external source/provenance binding used by the
        # existing adjudicator.  A matching hash list alone is insufficient
        # for producer padded media.
        media_binding = _range_media_source_binding(
            check_request, str(source_media), source_sha,
        )
        if media_binding is None:
            return "NATIVE_RANGE_CURRENT_SOURCE_DRIFT", "source media provenance does not bind current request"
        return None

    def _recover_pre_http_configuration_failure(
        *,
        request: Mapping[str, Any],
        prepared: _PreparedRequest,
        provider: str,
        source_sha: str,
        envelope_path: Path,
        old_envelope: bytes,
        old_envelope_sha256: str,
    ) -> tuple[Mapping[str, Any], dict[str, str]]:
        """Archive a proven pre-HTTP failure, then use the ordinary writer once.

        The source RLock and receipt file lock remain held across admission,
        archive and normal observation.  The ordinary observer re-enters them
        at its before-HTTP reservation seam.
        """

        from src.autoslice.native_audio_budget_receipt import (
            exclusive_native_audio_budget,
            require_native_audio_budget_continuity,
        )
        from src.autoslice.supplement_audio_budget import ensure_budget

        # A cold process has no registered ledger yet.  Validate the occupied
        # receipt before normal construction can establish or persist one.
        _validate_recovery_budget_receipt(
            source_media=source_media,
            source_sha256=source_sha,
            receipt_path=shared_budget_receipt,
        )
        budget = ensure_budget(source_media)
        with budget.lock:
            try:
                with exclusive_native_audio_budget(shared_budget_receipt):
                    _validate_recovery_budget_receipt(
                        source_media=source_media,
                        source_sha256=source_sha,
                        receipt_path=shared_budget_receipt,
                    )
                    # The normal constructor restores history without sending
                    # HTTP.  Keep both locks through construction and dispatch.
                    verifier = _provider_verifier(provider)
                    if verifier is None:
                        raise _RangeRecoveryInvalid(
                            "native range provider budget verifier is unavailable"
                        )
                    require_native_audio_budget_continuity(
                        source_media=source_media,
                        source_media_sha256=source_sha,
                        receipt_path=shared_budget_receipt,
                        require_existing=True,
                    )
                    snapshot = budget.snapshot()
                    attempts = snapshot.get("attempts")
                    if not isinstance(attempts, list):
                        raise _RangeRecoveryInvalid("native range budget attempts are invalid")
                    model = NATIVE_MODELS.get(provider)
                    if any(
                        isinstance(row, Mapping)
                        and row.get("provider") == provider
                        and row.get("model") == model
                        and row.get("start_ms") == prepared.physical_start_ms
                        and row.get("end_ms") == prepared.physical_end_ms
                        for row in attempts
                    ):
                        raise _RangeRecoveryAttemptExists(
                            "native range provider/window already has a budget attempt"
                        )
                    if _path_has_symlink(envelope_path) or envelope_path.read_bytes() != old_envelope:
                        raise _RangeRecoveryInvalid(
                            "native range envelope changed before recovery archive"
                        )
                    history = _archive_pre_dispatch_envelope(
                        envelope_path=envelope_path,
                        prepared=prepared,
                        raw=old_envelope,
                        raw_sha256=old_envelope_sha256,
                    )
                    # The same wrapper may have memoized this pre-HTTP failure.
                    # Only the proven zero-attempt config failure can be evicted.
                    physical_memo.pop(_cache_key(prepared, provider), None)
                    # Keep both locks through the ordinary writer.  Its
                    # native observer re-enters the same process-owned file
                    # lock at the real before-request seam, so no competing
                    # source consumer can pass the admission check between
                    # archive and reservation.
                    observed = _observe_one_independent(
                        request, prepared, provider, source_sha,
                    )
            except _RangeRecoveryAttemptExists:
                raise
            except _RangeRecoveryInvalid:
                raise
            except Exception as exc:
                raise _RangeRecoveryInvalid(
                    "native range canonical budget continuity is invalid"
                ) from exc
        return observed, history

    def _replay_acquisition(prepared, provider, envelope_path):
        """Project the winner into a new receipt; original failures/bills stay sealed."""
        original_binding = {}
        if envelope_path.exists() or envelope_path.is_symlink():
            try:
                _, original_sha, original = _read_bound_uncertain_envelope(
                    envelope_path=envelope_path, prepared=prepared, provider=provider,
                    source_sha256=source_sha256)
            except _RangeRecoveryInvalid as exc:
                raise _NativeEnvelopeInvalid(str(exc)) from exc
            witness = original["witness"]
            typed_error = witness.get("review_error")
            typed_recovery = (typed_error.get("material_recovery")
                if isinstance(typed_error, Mapping) else None) or {}
            publication_conflict = (
                isinstance(typed_error, Mapping)
                and typed_error.get("status") == "CONTRACT_ERROR"
                and typed_error.get("reason_code") == witness.get("reason_code")
                and isinstance(typed_recovery, Mapping)
                and typed_recovery.get("kind") == "ARTIFACT_REPLAY_REQUIRED"
                # Read legacy immutable envelopes without rewriting their bytes.
                or typed_error is None
                and witness.get("reason_code") == "NATIVE_OBSERVER_FAILED"
                and "IMMUTABLE_ARTIFACT_CONFLICT" in witness.get("reason_detail", ""))
            if not (publication_conflict or witness.get("reason_code") == "NATIVE_ACQUISITION_OWNER_RECOVERY_REQUIRED"):
                if witness.get("reason_code") in _PRE_HTTP_CONFIG_FAILURES:
                    raise _RangeRecoveryInvalid("native envelope requires pre-dispatch recovery")
                # A valid, definite provider/schema failure stays that failure.
                # It is neither a corrupt envelope nor permission to re-send.
                return {**witness, "served_from_cache": True}, {}
            if original.get("native_observation") is not None:
                raise _RangeRecoveryInvalid("native conflict envelope has an unexpected observation")
            original_binding.update(original_envelope_path=str(envelope_path),
                                    original_envelope_sha256=original_sha)
        _validate_recovery_budget_receipt(source_media=source_media,
            source_sha256=source_sha256, receipt_path=shared_budget_receipt)
        budget_bytes = shared_budget_receipt.read_bytes()
        account = json.loads(budget_bytes)["budget"]
        acquisition = native_acquisition_identity(source_sha256=source_sha256, provider=provider,
            start_ms=prepared.physical_start_ms, end_ms=prepared.physical_end_ms)
        attempts = [row for row in account["attempts"] if all(
            row.get(key) == acquisition[key] for key in ("provider", "model", "start_ms", "end_ms"))]
        if not attempts:
            raise _NativeAcquisitionIdentityMissing("native acquisition original budget identity is missing")
        history = {"budget_receipt_path": str(shared_budget_receipt),
            "budget_receipt_sha256": hashlib.sha256(budget_bytes).hexdigest(),
            "acquisition_identity": acquisition, "new_provider_calls": 0, "new_audio_ms": 0,
            **original_binding}
        native = read_native_acquisition(source_media=source_media,
            root=native_observation_cache_root, identity=acquisition)
        witness = _native_transcript_witness(request=prepared.request, prepared=prepared,
            provider=provider, source_sha256=source_sha256, native_observation=native)
        if witness.get("status") != "OBSERVED":
            raise _RangeRecoveryInvalid("native winner transcript is unavailable")
        document = {"schema_version": NATIVE_CONTEXT_ENVELOPE_SCHEMA,
            "request": prepared.request, "request_sha256": prepared.request_sha256,
            "source_media_sha256": source_sha256, "audio_start_ms": prepared.physical_start_ms,
            "audio_end_ms": prepared.physical_end_ms, "native_observation": native, "witness": witness}
        replay = (output_dir / provider / "acquisition-replay" /
                  native_digest(history) / f"{prepared.request_sha256}.json")
        _persist_immutable_json(replay, document)
        history.update(replay_envelope_path=str(replay), replay_envelope_sha256=_source_sha256(replay))
        _persist_immutable_json(replay.with_suffix(".replay.json"), history)
        return {**witness, "served_from_cache": True}, {"acquisition_replay": history}

    def observe_range_context(
        *,
        check_request: Mapping[str, Any],
        range_witness_request: Mapping[str, Any],
        provider: str = "native",
        context_assistance: Mapping[str, Any] | None = None,
        cache_only: bool = False,
    ) -> list[Mapping[str, Any]]:
        """Observe and adjudicate one complete native range for CPA context.

        The range request is candidate-blind and is sent through the ordinary
        independent observer, so exact-audio cache and budget accounting stay
        shared.  The current check request only binds the current cue, SRT,
        source identity, and candidate identity at the existing adjudicator;
        it is never sent to a provider and the returned row remains
        ``RANGE_CONTEXT_ONLY``/``EVIDENCE_ONLY``.
        """

        if provider == "gemini":
            if cache_only:
                return []  # This owner has no read-only Gemini recovery seam.
            observe_gemini = getattr(fallback, "observe_gemini_range_context", None)
            if not callable(observe_gemini):
                return []
            return observe_gemini(
                check_request=check_request,
                range_witness_request=range_witness_request,
                budget_receipt_path=shared_budget_receipt,
                **({"context_assistance": context_assistance} if context_assistance is not None else {}),
            )
        if context_assistance is not None:
            # MAI/MOSS currently expose candidate-blind audio protocols only.
            # Never silently discard hints or relabel their result assisted.
            return [{"status": "NOT_DISPATCHED", "reason_code": "CONTEXT_ASSISTED_PROVIDER_UNSUPPORTED",
                     "authority": "EVIDENCE_ONLY", "mutation_authorized": False}]
        if provider != "native" and provider not in provider_names:
            return []
        range_provider_names = provider_names if provider == "native" else (provider,)
        if not provider_names:
            return []
        request_mapping = check_request if isinstance(check_request, Mapping) else {}
        range_mapping = (
            range_witness_request
            if isinstance(range_witness_request, Mapping)
            else {}
        )
        try:
            current_request = build_witness_request(request_mapping)
            current_prepared = _prepare_request(current_request)
            prepared = _prepare_request(range_mapping)
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            reason = getattr(exc, "reason_code", None) or "NATIVE_RANGE_REQUEST_INVALID"
            return [
                _independent_uncertain(
                    range_mapping,
                    provider,
                    reason,
                    detail=getattr(exc, "detail", str(exc)),
                )
                for provider in range_provider_names
            ]

        current_indexes = current_request.get("cue_indexes")
        range_indexes = prepared.request.get("cue_indexes")
        if (
            not isinstance(current_indexes, list)
            or len(current_indexes) != 1
            or type(current_indexes[0]) is not int
            or not isinstance(range_indexes, list)
            or not range_indexes
            or any(type(index) is not int for index in range_indexes)
            or range_indexes != list(range(range_indexes[0], range_indexes[-1] + 1))
            or current_indexes[0] not in range_indexes
            or current_prepared.source_offset_ms != prepared.source_offset_ms
            or current_prepared.target_start_ms < prepared.target_start_ms
            or current_prepared.target_end_ms > prepared.target_end_ms
        ):
            return [
                _independent_uncertain(
                    range_mapping,
                    provider,
                    "NATIVE_RANGE_GEOMETRY_INVALID",
                    detail="current cue is outside the complete candidate-blind range",
                )
                for provider in range_provider_names
            ]

        state = _source_state()
        geometry = (prepared.physical_start_ms, prepared.physical_end_ms)
        if state != "ready":
            if state == "drift":
                reason, detail = "NATIVE_SOURCE_DRIFT", "source media stat binding changed"
            else:
                reason, detail = source_binding_failure or (
                    "NATIVE_SOURCE_UNAVAILABLE", "source media is unavailable"
                )
            return [
                _independent_uncertain(
                    range_mapping,
                    provider,
                    reason,
                    detail=detail,
                    source_sha256=source_sha256,
                    geometry=geometry,
                )
                for provider in range_provider_names
            ]

        assert source_sha256 is not None
        preflight = _validate_range_dispatch_inputs(
            check_request=request_mapping,
            current_request=current_request,
            current_prepared=current_prepared,
            prepared=prepared,
            source_sha=source_sha256,
        )
        if preflight is not None:
            reason, detail = preflight
            return [
                _independent_uncertain(
                    range_mapping,
                    provider,
                    reason,
                    detail=detail,
                    source_sha256=source_sha256,
                    geometry=geometry,
                )
                for provider in range_provider_names
            ]

        # A complete on-disk envelope is already an accepted exact-range cache.
        # Read it first so repeated context checks do not rewrite the provider
        # receipt or envelope merely to add a new request binding.
        cached_rows = observe_cached_independent_witnesses(range_mapping)
        cached_by_provider = {
            row.get("provider"): row
            for row in cached_rows
            if isinstance(row, Mapping) and isinstance(row.get("provider"), str)
        }
        observed_rows: list[Mapping[str, Any]] = []
        recollected_rows = []
        recovery_history: dict[str, dict[str, str]] = {}
        for provider in range_provider_names:
            recollect = _cache_recollection(prepared, provider)
            if recollect is not None:
                recollected_rows.extend(recollect.observe_range_context(
                    check_request=check_request, range_witness_request=range_witness_request,
                    provider=provider, cache_only=cache_only))
                continue
            cached = cached_by_provider.get(provider)
            if cached is not None:
                observed_rows.append(cached)
                continue
            envelope_path = (
                output_dir
                / provider
                / "native-context"
                / f"{prepared.request_sha256}.json"
            )
            replay_error = None
            if cache_only or envelope_path.exists() or envelope_path.is_symlink():
                try:
                    recovered, history = _replay_acquisition(prepared, provider, envelope_path)
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    replay_error = exc
                    if cache_only:
                        observed_rows.append(_independent_uncertain(range_mapping, provider,
                            _acquisition_replay_reason(exc), detail=_sanitized_detail(exc),
                            source_sha256=source_sha256, geometry=geometry))
                        continue
                else:
                    observed_rows.append(recovered)
                    recovery_history[provider] = history
                    continue
            if envelope_path.exists() or envelope_path.is_symlink():
                # A no-observation, pre-HTTP configuration failure is the one
                # narrow exception to the immutable-envelope rule.  It may be
                # admitted only after current config and canonical budget
                # continuity prove that this exact provider/window never had
                # an attempt.  Every other uncertain/partial envelope remains
                # a terminal immutable failure.
                try:
                    old_envelope, old_envelope_sha256 = _read_recoverable_pre_http_envelope(
                        envelope_path=envelope_path,
                        prepared=prepared,
                        provider=provider,
                        source_sha256=source_sha256,
                    )
                except _RangeRecoveryInvalid as envelope_error:
                    try:
                        _read_bound_uncertain_envelope(envelope_path=envelope_path,
                            prepared=prepared, provider=provider, source_sha256=source_sha256)
                    except _RangeRecoveryInvalid:
                        reason = "NATIVE_RANGE_ENVELOPE_INVALID"
                    else:
                        reason = _acquisition_replay_reason(replay_error or envelope_error)
                    observed_rows.append(
                        _independent_uncertain(
                            range_mapping,
                            provider,
                            reason,
                            detail=_sanitized_detail(replay_error or envelope_error),
                            source_sha256=source_sha256,
                            geometry=geometry,
                        )
                    )
                    continue

                config_reason = _validate_recovery_provider_configuration(provider)
                if config_reason is not None:
                    observed_rows.append(
                        _independent_uncertain(
                            range_mapping,
                            provider,
                            config_reason,
                            detail="current native provider configuration is not ready",
                            source_sha256=source_sha256,
                            geometry=geometry,
                        )
                    )
                    continue
                try:
                    recovered, history = _recover_pre_http_configuration_failure(
                        request=range_mapping,
                        prepared=prepared,
                        provider=provider,
                        source_sha=source_sha256,
                        envelope_path=envelope_path,
                        old_envelope=old_envelope,
                        old_envelope_sha256=old_envelope_sha256,
                    )
                except _RangeRecoveryAttemptExists:
                    observed_rows.append(
                        _independent_uncertain(
                            range_mapping,
                            provider,
                            "NATIVE_RANGE_RECOVERY_ATTEMPT_EXISTS",
                            detail="canonical budget already records this provider/window",
                            source_sha256=source_sha256,
                            geometry=geometry,
                        )
                    )
                    continue
                except _RangeRecoveryInvalid:
                    observed_rows.append(
                        _independent_uncertain(
                            range_mapping,
                            provider,
                            "NATIVE_RANGE_RECOVERY_RECEIPT_INVALID",
                            detail="canonical budget or immutable recovery binding is invalid",
                            source_sha256=source_sha256,
                            geometry=geometry,
                        )
                    )
                    continue
                recovery_history[provider] = history
                observed_rows.append(recovered)
                continue
            # A missing provider envelope is a first normal request for this
            # provider.  It must not be reported as an external failure merely
            # because another provider already has a valid cache.
            observed_rows.append(
                _observe_one_independent(
                    range_mapping, prepared, provider, source_sha256,
                )
            )

        from src.autoslice.independent_context_adjudication import (
            validate_native_range_context_observation,
        )

        output: list[Mapping[str, Any]] = list(recollected_rows)
        for row in observed_rows:
            provider = row.get("provider") if isinstance(row, Mapping) else None
            if not isinstance(provider, str) or provider not in provider_names:
                output.append(
                    _independent_uncertain(
                        range_mapping,
                        str(provider or "unknown"),
                        "NATIVE_RANGE_PROVIDER_IDENTITY_INVALID",
                        source_sha256=source_sha256,
                        geometry=geometry,
                    )
                )
                continue
            if row.get("status") != "OBSERVED":
                normalized = dict(row)
                normalized.update(recovery_history.get(provider, {}))
                output.append(normalized)
                continue
            try:
                replay_binding = (recovery_history.get(provider, {}).get("acquisition_replay")
                                  or row.get("blind_cache_replay") or {})
                artifacts = _materialize_native_range_artifacts(
                    check_request=request_mapping,
                    prepared=prepared,
                    provider=provider,
                    source_media=source_media,
                    source_sha256=source_sha256,
                    output_dir=output_dir,
                    native_observation_cache_root=native_observation_cache_root,
                    replay_envelope_path=(Path(replay_binding.get("replay_envelope_path")
                        or replay_binding["envelope_path"]) if replay_binding else None),
                    replay_envelope_sha256=(replay_binding.get("replay_envelope_sha256")
                        or replay_binding.get("envelope_sha256")),
                )
                validated, reason = validate_native_range_context_observation(
                    str(request_mapping["whole_clip_current_srt"]),
                    request_mapping,
                    artifacts,
                )
            except (OSError, TypeError, ValueError, KeyError) as exc:
                validated, reason = None, "NATIVE_RANGE_ARTIFACT_BINDING_INVALID"
                detail = _sanitized_detail(exc)
            else:
                detail = reason or "native range context validation failed"
            if validated is None:
                blocked = _independent_uncertain(
                        range_mapping,
                        provider,
                        reason or "NATIVE_RANGE_VALIDATION_FAILED",
                        detail=detail,
                        source_sha256=source_sha256,
                        geometry=geometry,
                        native_observation=(
                            row.get("native_observation")
                            if isinstance(row.get("native_observation"), Mapping)
                            else None
                        ),
                    )
                blocked.update(recovery_history.get(provider, {}))
                output.append(blocked)
                continue
            normalized = dict(validated)
            if row.get("served_from_cache") is True:
                normalized["served_from_cache"] = True
            normalized.update(recovery_history.get(provider, {}))
            output.append(normalized)
        output.sort(key=lambda row: range_provider_names.index(row['provider'])
                    if row.get('provider') in range_provider_names else len(range_provider_names))
        return output

    def wrapper(request: Mapping[str, Any]) -> Mapping[str, Any]:
        return fallback(request)

    wrapper.observe_independent_witnesses = observe_independent_witnesses  # type: ignore[attr-defined]
    def span_source_identity():
        # Reuse the current provider's actual file identity, not a transcript
        # or model-supplied source label. This performs no provider request.
        return source_sha256 if _source_state() == "ready" else None
    wrapper.span_termination_source_identity = span_source_identity  # type: ignore[attr-defined]
    wrapper.span_termination_ordinary_routes = (  # type: ignore[attr-defined]
        "primary", *provider_names, *("range:" + name for name in provider_names),
        *(("range:gemini",) if callable(getattr(fallback, "observe_gemini_range_context", None)) else ()),
    )
    if hasattr(fallback, "span_termination_upgrade_enabled"):
        wrapper.span_termination_upgrade_enabled = fallback.span_termination_upgrade_enabled  # type: ignore[attr-defined]
    if callable(getattr(fallback, "span_termination_upgrade_status", None)):
        wrapper.span_termination_upgrade_status = fallback.span_termination_upgrade_status  # type: ignore[attr-defined]
    observe_range_context.available_providers = (  # type: ignore[attr-defined]
        *(("native", *provider_names) if provider_names else ()),
        *(("gemini",) if callable(getattr(fallback, "observe_gemini_range_context", None)) else ()),
    )
    observe_range_context.context_assisted_providers = (  # type: ignore[attr-defined]
        ("gemini",) if callable(getattr(fallback, "observe_gemini_range_context", None)) else ())
    # Actual configured models, consumed by relisten admission; plan-supplied
    # model/provider labels do not manufacture a new listening opportunity.
    observe_range_context.listening_models = {  # type: ignore[attr-defined]
        **{name: NATIVE_MODELS[name] for name in provider_names},
        **({'native': NATIVE_MODELS[provider_names[0]]} if len(provider_names) == 1 else {}),
        **({'gemini': fallback.model} if isinstance(getattr(fallback, 'model', None), str) else {}),
    }
    wrapper.observe_range_context = observe_range_context  # type: ignore[attr-defined]

    def relisten_budget_state():
        # Read the original owner receipt without restoring, refunding,
        # sealing UNKNOWN, or constructing a second attempt history.
        from src.autoslice.native_audio_budget_receipt import (
            _load_existing, _validate_existing, _validate_budget_history)
        if _source_state() != "ready" or source_sha256 is None:
            raise ContractRejection("RELISTEN_BUDGET_SOURCE_UNAVAILABLE")
        receipt = _load_existing(shared_budget_receipt)
        if receipt is not None:
            _validate_existing(receipt, source_media_sha256=source_sha256)
            _validate_budget_history(receipt["budget"])
        result = {"receipt_path": str(shared_budget_receipt), "receipt": receipt,
                  "source_media_sha256": source_sha256}
        if receipt is None:
            from src.autoslice.targeted_relisten import unstarted_native_owner_state
            result["unstarted_owner_state"] = unstarted_native_owner_state(
                source_media=source_media, receipt_path=shared_budget_receipt, output_dir=output_dir)
        return result

    wrapper.relisten_budget_state = relisten_budget_state  # type: ignore[attr-defined]

    def observe_cached_independent_witnesses(request: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        """Replay verified blind acquisitions across candidate metadata changes.

        The original request/envelope/native receipt are immutable. Candidate
        and evidence IDs are not audio identity; source/target/context/offset,
        provider/model and raw native seals must still agree.
        """
        try:
            prepared = _prepare_request(request)
        except _RequestInvalid:
            return []
        if _source_state() != "ready":
            return []
        results: list[Mapping[str, Any]] = []
        seen = set()
        roots = tuple(dict.fromkeys((output_dir, *map(Path, cached_observation_roots))))
        geometry_keys = ("matched_start_ms", "matched_end_ms", "context_start_ms",
                         "context_end_ms", "source_media_timeline_offset_ms")
        for provider, root in ((provider, root) for provider in provider_names for root in roots):
            recollect = _cache_recollection(prepared, provider)
            if recollect is not None:
                if root == roots[0]:
                    results.extend(recollect.observe_cached_independent_witnesses(request))
                continue
            folder = root / provider / "native-context"
            exact = folder / f"{prepared.request_sha256}.json"
            # A corrupt exact record remains fail-closed; never wash it through
            # another alias. When the exact key is absent, inspect only this
            # explicitly supplied provider cache, not other experiments.
            paths = [exact] if os.path.lexists(exact) else sorted(folder.glob("*.json"))
            for path in paths:
                if _path_has_symlink(path):
                    continue
                try:
                    raw_bytes = path.read_bytes()
                    envelope = json.loads(raw_bytes)
                    original = _prepare_request(envelope["request"])
                except (OSError, ValueError, TypeError, KeyError):
                    continue
                if not (
                    envelope.get("schema_version") == NATIVE_CONTEXT_ENVELOPE_SCHEMA
                    and envelope.get("request_sha256") == original.request_sha256
                    and path.stem == original.request_sha256
                    and all(original.request.get(k) == prepared.request.get(k) for k in geometry_keys)
                    and envelope.get("source_media_sha256") == source_sha256
                    and envelope.get("audio_start_ms") == prepared.physical_start_ms
                    and envelope.get("audio_end_ms") == prepared.physical_end_ms
                    and isinstance(envelope.get("native_observation"), Mapping)
                ):
                    continue
                original_result = _native_transcript_witness(
                    request=original.request, prepared=original,
                    native_observation=envelope["native_observation"],
                    source_sha256=source_sha256, provider=provider,
                )
                if original_result.get("status") != "OBSERVED":
                    continue
                result = _native_transcript_witness(
                    request=request, prepared=prepared,
                    native_observation=envelope["native_observation"],
                    source_sha256=source_sha256, provider=provider,
                )
                identity = (provider, result.get("model"), result.get("audio_clip_sha256"), result.get("response_sha256"))
                if result.get("status") == "OBSERVED" and identity not in seen:
                    seen.add(identity)
                    rebound = {**result, "served_from_cache": True}
                    if (original.request_sha256 != prepared.request_sha256
                            or path != output_dir / provider / "native-context" / f"{prepared.request_sha256}.json"):
                        rebound["blind_cache_replay"] = {
                            "basis": "IDENTICAL_SOURCE_AUDIO_AND_CLOCK",
                            "original_request_sha256": original.request_sha256,
                            "current_request_sha256": prepared.request_sha256,
                            "envelope_path": str(path),
                            "envelope_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                            "new_provider_call": False,
                        }
                    results.append(rebound)
        return results if _source_state() == "ready" else []

    wrapper.observe_cached_independent_witnesses = observe_cached_independent_witnesses  # type: ignore[attr-defined]

    def probe_witness_cache(request: Mapping[str, Any]) -> Mapping[str, Any] | None:
        method = getattr(fallback, "probe_witness_cache", None)
        if callable(method):
            return method(request)
        return None

    wrapper.probe_witness_cache = probe_witness_cache  # type: ignore[attr-defined]
    for seam in (
        "exact_source_transcript", "probe_exact_source_transcript_cache",
        "observe_secondary_audio_evidence", "observe_secondary_witnesses",
    ):
        method = getattr(fallback, seam, None)
        if callable(method):
            setattr(wrapper, seam, method)
    return wrapper


__all__ = [
    "NATIVE_CONTEXT_ENVELOPE_SCHEMA",
    "NATIVE_WITNESS_KIND",
    "LEGACY_NATIVE_BUDGET_RECEIPT_MIGRATION_REQUIRED",
    "build_independent_context_witnesses",
    "build_native_context_verifier",
]
