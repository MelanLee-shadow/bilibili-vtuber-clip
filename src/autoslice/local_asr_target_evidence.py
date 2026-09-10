"""Project native ASR from an exact, candidate-free target crop into evidence.

The caller, not the provider, owns the crop geometry.  This is neither a
subtitle authority nor an automatic provider selector.  In particular, a
transcript is not converted into an invented independent pinyin witness.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

MODELS = {"moss": "moss-transcribe-diarize-pro", "mai": "MAI-Transcribe-2"}
SCHEMA = "exact-target-native-asr-evidence.v1"


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def valid_sha(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def exact_target_evidence(
    metadata: Mapping[str, Any], *, audio: bytes, source_sha256: str, start_ms: int, end_ms: int
) -> dict[str, Any]:
    """Validate a native transcript of the entire exact target, with no padding.

    Raw native rows are retained.  Out-of-order/overlapping speech is recorded,
    not silently sorted into a new subtitle.  NO_SPEECH is not deletion authority.
    The sole tolerance is the existing clients' 500-ms encoder-tail allowance.
    """
    if type(start_ms) is not int or type(end_ms) is not int or not 0 <= start_ms < end_ms:
        raise ValueError("EXACT_TARGET_GEOMETRY_INVALID")
    if end_ms - start_ms > 20_000 or not audio or not valid_sha(source_sha256):
        raise ValueError("EXACT_TARGET_INPUT_INVALID")
    provider = metadata.get("provider")
    if provider not in MODELS or metadata.get("model") != MODELS[provider]:
        raise ValueError("EXACT_TARGET_PROVIDER_INVALID")
    audio_sha = hashlib.sha256(audio).hexdigest()
    if metadata.get("input_audio_sha256") != audio_sha or not valid_sha(
        metadata.get("response_sha256")
    ):
        raise ValueError("EXACT_TARGET_SOURCE_BINDING_INVALID")
    native = metadata.get("native_segments")
    if not isinstance(native, list):
        raise ValueError("EXACT_TARGET_NATIVE_ROWS_INVALID")
    texts = []
    spans = []
    for row in native:
        if not isinstance(row, Mapping):
            raise ValueError("EXACT_TARGET_NATIVE_ROWS_INVALID")
        a, b, text = row.get("start_ms"), row.get("end_ms"), row.get("text")
        if (
            type(a) is not int
            or type(b) is not int
            or not 0 <= a < b <= end_ms - start_ms + 500
            or not isinstance(text, str)
            or not text.strip()
        ):
            raise ValueError("EXACT_TARGET_NATIVE_ROWS_INVALID")
        texts.append(text)
        spans.append(
            {
                "source_start_ms": start_ms + a,
                "source_end_ms": start_ms + b,
                "native_start_ms": a,
                "native_end_ms": b,
                "text": text,
            }
        )
    output = {
        "schema_version": SCHEMA,
        "provider": provider,
        "model": MODELS[provider],
        "status": "OBSERVED" if native else "NO_SPEECH_REPORTED",
        "candidate_exposure": "none",
        "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False,
        "source_media_sha256": source_sha256,
        "input_audio_sha256": audio_sha,
        "response_sha256": metadata["response_sha256"],
        "target_start_ms": start_ms,
        "target_end_ms": end_ms,
        "crop_is_exact_target": True,
        "transcript": " ".join(texts),
        "native_segments": native,
        "source_segments": spans,
        "native_timeline": metadata.get("native_timeline"),
        "diagnostics": metadata.get("diagnostics", []),
        "note": "Provider-native text is fallible evidence. No independent phonetic claim or deletion authority.",
    }
    output["receipt_sha256"] = digest(output)
    return output
