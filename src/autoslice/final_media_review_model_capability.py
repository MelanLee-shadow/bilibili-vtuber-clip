"""Independent model-modality authority for final-media raw-AV review.

A raw-AV adapter can prove which bytes it opened, but it cannot prove that the
upstream model actually perceived those modalities. The accepted runtime
binding is therefore a create-only sentinel seal receipt, not an operator-
authored capability JSON or adapter self-report.

Known text/image-only models are rejected before any provider call. Unknown
models remain unverified until a valid dual-media sentinel seal exists. This
module never creates or refreshes evidence; creation belongs to
``final_media_review_model_capability_seal``.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Mapping

from src.autoslice.final_media_review_model_capability_seal import (
    FinalMediaReviewModelCapabilitySealError,
    MODEL_CAPABILITY_ATTESTATION_SCHEMA_VERSION,
    MODEL_CAPABILITY_AUTHORITY,
    MODEL_CAPABILITY_SEAL_RECEIPT_SCHEMA_VERSION,
    MODEL_CAPABILITY_VERIFICATION_METHOD,
    validate_model_capability_seal_receipt,
)


KNOWN_UNSUPPORTED_DUAL_RAW_AV_MODELS = frozenset(
    {
        "gpt-6-sol",
        "gpt-6.1-sol",
        "gpt-6-astra",
        "gpt-6.1-astra",
    }
)


class FinalMediaReviewModelCapabilityError(ValueError):
    """A typed, provider-free model-capability rejection."""

    def __init__(self, reason_code: str, detail: str):
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


def _error(
    reason_code: str, detail: str
) -> FinalMediaReviewModelCapabilityError:
    return FinalMediaReviewModelCapabilityError(reason_code, detail)


def validate_model_capability_attestation(
    raw_binding: object,
    *,
    runtime_root: str | Path,
    provider: str,
    model: str,
    endpoint_family: str,
    executable_sha256: str,
    authority_uid: int | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Validate one independently sealed dual-media capability receipt."""

    if model in KNOWN_UNSUPPORTED_DUAL_RAW_AV_MODELS:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_MODALITY_UNSUPPORTED",
            f"{model} has no raw-audio plus continuous-video input contract",
        )
    if not isinstance(raw_binding, Mapping):
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_MODALITY_UNVERIFIED",
            "runtime raw-AV capability has no independent sentinel seal",
        )
    try:
        sealed = validate_model_capability_seal_receipt(
            raw_binding,
            runtime_root=runtime_root,
            provider=provider,
            model=model,
            endpoint_family=endpoint_family,
            executable_sha256=executable_sha256,
            authority_uid=authority_uid,
            now=now,
        )
    except FinalMediaReviewModelCapabilitySealError as exc:
        raise _error(exc.reason_code, exc.detail) from exc
    return {
        "path": sealed["attestation_path"],
        "file_sha256": sealed["attestation_file_sha256"],
        "bytes": sealed["attestation_bytes"],
        "attestation_sha256": sealed["attestation_sha256"],
        "seal_receipt_path": sealed["seal_receipt_path"],
        "seal_receipt_file_sha256": sealed["seal_receipt_file_sha256"],
        "seal_receipt_bytes": sealed["seal_receipt_bytes"],
        "seal_receipt_sha256": sealed["seal_receipt_sha256"],
        "sentinel_result_sha256": sealed["sentinel_result_sha256"],
        "sentinel_result_self_sha256": sealed[
            "sentinel_result_self_sha256"
        ],
        "sentinel_runner_sha256": sealed["sentinel_runner_sha256"],
        "contract_sha256": sealed["contract_sha256"],
        "provider": sealed["provider"],
        "model": sealed["model"],
        "endpoint_family": sealed["endpoint_family"],
        "accepts": dict(sealed["accepts"]),
        "verification_method": sealed["verification_method"],
        "verified_at": sealed["verified_at"],
        "expires_at": sealed["expires_at"],
        "challenges": dict(sealed["challenges"]),
    }


__all__ = [
    "FinalMediaReviewModelCapabilityError",
    "KNOWN_UNSUPPORTED_DUAL_RAW_AV_MODELS",
    "MODEL_CAPABILITY_ATTESTATION_SCHEMA_VERSION",
    "MODEL_CAPABILITY_AUTHORITY",
    "MODEL_CAPABILITY_SEAL_RECEIPT_SCHEMA_VERSION",
    "MODEL_CAPABILITY_VERIFICATION_METHOD",
    "validate_model_capability_attestation",
]
