"""Exact raw-AV transport evidence validation for final-media review."""
from __future__ import annotations

import re
from typing import Mapping


TRANSPORT_EVIDENCE_SCHEMA_VERSION = "final-media-review-raw-av-evidence.v3"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class FinalMediaReviewTransportEvidenceError(ValueError):
    """A malformed or drifted raw-AV transport-evidence envelope."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _fail(detail: str) -> FinalMediaReviewTransportEvidenceError:
    return FinalMediaReviewTransportEvidenceError(detail)


def _sha(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise _fail(f"{label} sha256 missing")
    normalized = value.strip().lower()
    if normalized.startswith("sha256:"):
        normalized = normalized[7:]
    if _SHA256_RE.fullmatch(normalized) is None:
        raise _fail(f"{label} sha256 invalid")
    return normalized


def validate_transport_evidence(
    result: Mapping[str, object],
    assessment: Mapping[str, object],
) -> None:
    """Require exact runtime, sentinel, audio, video and manifest bindings."""

    capability = assessment.get("transport_capability")
    if capability is None:
        return
    if not isinstance(capability, Mapping):
        raise _fail("transport capability assessment is invalid")
    accepts = capability.get("accepts")
    if not (
        isinstance(accepts, Mapping)
        and accepts.get("raw_audio") is True
        and accepts.get("continuous_source_video") is True
    ):
        raise _fail(
            "bound final-media transport must accept raw audio and continuous video"
        )
    audio = assessment.get("audio")
    clock = assessment.get("exact_media_clock")
    manifest = assessment.get("asset_manifest")
    windows = audio.get("windows") if isinstance(audio, Mapping) else None
    full_audio = [
        row
        for row in windows or []
        if isinstance(row, Mapping)
        and row.get("kind") == "exact_full_audio_wav"
        and row.get("declared_start_us") == 0
        and isinstance(clock, Mapping)
        and row.get("declared_end_us") == clock.get("duration_us")
        and isinstance(row.get("delta_us"), int)
        and not isinstance(row.get("delta_us"), bool)
        and isinstance(row.get("duration_tolerance_us"), int)
        and not isinstance(row.get("duration_tolerance_us"), bool)
        and row.get("duration_tolerance_us", 0) >= 0
        and abs(row.get("delta_us", 0)) <= row.get("duration_tolerance_us", -1)
    ]
    if (
        len(full_audio) != 1
        or not isinstance(clock, Mapping)
        or not isinstance(manifest, Mapping)
    ):
        raise _fail("transport result requires one exact full-media WAV binding")
    evidence = result.get("transport_evidence")
    expected_fields = {
        "schema_version",
        "provider",
        "model",
        "endpoint_family",
        "capability_id",
        "runtime_capability_sha256",
        "executable_sha256",
        "model_capability_seal_receipt_sha256",
        "model_capability_attestation_sha256",
        "model_capability_contract_sha256",
        "model_capability_sentinel_result_sha256",
        "model_capability_sentinel_result_self_sha256",
        "model_capability_sentinel_runner_sha256",
        "request_sha256",
        "response_sha256",
        "source_video_sha256",
        "raw_audio_sha256",
        "asset_manifest_sha256",
        "consumed_continuous_source_video",
        "consumed_raw_audio",
    }
    if not isinstance(evidence, Mapping) or set(evidence) != expected_fields:
        raise _fail("raw-AV transport evidence is missing or malformed")
    checks = (
        evidence.get("schema_version") == TRANSPORT_EVIDENCE_SCHEMA_VERSION,
        evidence.get("provider") == capability.get("provider"),
        evidence.get("model") == capability.get("model"),
        evidence.get("endpoint_family") == capability.get("endpoint_family"),
        evidence.get("capability_id") == capability.get("capability_id"),
        _sha(
            evidence.get("runtime_capability_sha256"),
            label="transport runtime capability",
        )
        == capability.get("runtime_capability_sha256"),
        _sha(evidence.get("executable_sha256"), label="transport executable")
        == capability.get("executable_sha256"),
        _sha(
            evidence.get("model_capability_seal_receipt_sha256"),
            label="transport model capability seal receipt",
        )
        == capability.get("model_capability_seal_receipt_sha256"),
        _sha(
            evidence.get("model_capability_attestation_sha256"),
            label="transport model capability attestation",
        )
        == capability.get("model_capability_attestation_sha256"),
        _sha(
            evidence.get("model_capability_contract_sha256"),
            label="transport model capability contract",
        )
        == capability.get("model_capability_contract_sha256"),
        _sha(
            evidence.get("model_capability_sentinel_result_sha256"),
            label="transport model capability sentinel result",
        )
        == capability.get("model_capability_sentinel_result_sha256"),
        _sha(
            evidence.get("model_capability_sentinel_result_self_sha256"),
            label="transport model capability sentinel result self-seal",
        )
        == capability.get("model_capability_sentinel_result_self_sha256"),
        _sha(
            evidence.get("model_capability_sentinel_runner_sha256"),
            label="transport model capability sentinel runner",
        )
        == capability.get("model_capability_sentinel_runner_sha256"),
        bool(_sha(evidence.get("request_sha256"), label="transport request")),
        bool(_sha(evidence.get("response_sha256"), label="transport response")),
        _sha(
            evidence.get("source_video_sha256"), label="transport source video"
        )
        == clock.get("source_video_sha256"),
        _sha(evidence.get("raw_audio_sha256"), label="transport raw audio")
        == full_audio[0].get("sha256"),
        _sha(
            evidence.get("asset_manifest_sha256"),
            label="transport asset manifest",
        )
        == manifest.get("sha256"),
        evidence.get("consumed_continuous_source_video") is True,
        evidence.get("consumed_raw_audio") is True,
    )
    if not all(checks):
        raise _fail(
            "transport evidence does not bind exact runtime, sentinel, audio, "
            "video, and manifest"
        )


__all__ = [
    "FinalMediaReviewTransportEvidenceError",
    "TRANSPORT_EVIDENCE_SCHEMA_VERSION",
    "validate_transport_evidence",
]
