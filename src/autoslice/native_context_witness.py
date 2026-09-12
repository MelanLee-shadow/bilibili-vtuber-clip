"""Candidate-blind native transcript evidence for an experimental T11 lane.

The ordinary entity verifier remains the fallback for every request outside the
candidate-blind acoustic witness contract.  The native lane receives only the
physical target geometry, so neither subtitle text nor a neutral length hint can
reach MOSS or MAI.  Native observations remain evidence for CPA; they never
become a subtitle or deletion authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.autoslice.acoustic_witness_adjudication import WITNESS_REQUEST_SCHEMA
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
)


NATIVE_WITNESS_KIND = "subtitle_span_acoustic_witness"
NATIVE_CONTEXT_ENVELOPE_SCHEMA = "native-context-witness-envelope.v1"
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


@dataclass(frozen=True)
class _PreparedRequest:
    request: dict[str, Any]
    request_sha256: str
    target_start_ms: int
    target_end_ms: int
    source_offset_ms: int
    physical_start_ms: int
    physical_end_ms: int


class _RequestInvalid(ValueError):
    """A typed, non-provider failure in the candidate-blind request contract."""

    def __init__(self, reason_code: str, detail: str = "") -> None:
        super().__init__(detail or reason_code)
        self.reason_code = reason_code
        self.detail = detail


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


def _observer_failure(exc: Exception) -> tuple[str, str]:
    reason_code = getattr(exc, "reason_code", None)
    if not isinstance(reason_code, str) or not reason_code.strip():
        reason_code = "NATIVE_OBSERVER_FAILED"
    safe_code = re.sub(r"[^A-Za-z0-9_.-]", "_", reason_code.strip())[:100]
    detail = _sanitized_detail(exc)
    if (
        safe_code == "NATIVE_OBSERVER_FAILED"
        and "source" in detail.lower()
        and "changed" in detail.lower()
    ):
        safe_code = "NATIVE_SOURCE_DRIFT"
    return safe_code or "NATIVE_OBSERVER_FAILED", detail


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
    source_stat_binding = _source_identity(source_media)
    source_sha256 = _source_sha256(source_media)
    if _source_identity(source_media) != source_stat_binding:
        raise RuntimeError("native context source changed while hashing")
    native_observer = build_native_foreign_witness(
        source_media=source_media,
        output_dir=output_dir,
        provider=provider,
        max_windows=max_windows,
        max_audio_ms=max_audio_ms,
    )
    successful_memo: dict[str, dict[str, Any]] = {}

    def _source_is_unchanged() -> bool:
        try:
            return _source_identity(source_media) == source_stat_binding
        except (OSError, ValueError):
            return False

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
            native_observation = native_observer(
                start_ms=prepared.physical_start_ms,
                end_ms=prepared.physical_end_ms,
            )
        except Exception as exc:  # provider-specific clients expose typed reason codes
            reason_code, detail = _observer_failure(exc)
            uncertain = _request_uncertain(
                request,
                reason_code,
                detail=detail,
                source_sha256=source_sha256,
                geometry=geometry,
            )
            try:
                _write_envelope(
                    output_dir=output_dir,
                    prepared=prepared,
                    witness=uncertain,
                    native_observation=None,
                    source_sha256=source_sha256,
                )
            except (OSError, TypeError, ValueError):
                pass
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
    for seam in ("exact_source_transcript", "probe_exact_source_transcript_cache"):
        method = getattr(fallback, seam, None)
        if callable(method):
            setattr(verify, seam, method)
    return verify


__all__ = ["NATIVE_CONTEXT_ENVELOPE_SCHEMA", "NATIVE_WITNESS_KIND", "build_native_context_verifier"]
