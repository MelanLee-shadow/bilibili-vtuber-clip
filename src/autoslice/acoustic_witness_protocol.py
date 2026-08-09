"""Typed protocol provenance for candidate-blind acoustic witnesses."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


BLIND_PINYIN_PROTOCOL = "blind_pinyin"
LEGACY_SIGHTED_PROTOCOL = "legacy_sighted"
_HAN_CODEPOINT_RANGES = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x20000, 0x2FA1F),
    (0x30000, 0x323AF),
)


def contains_han_text(text: str) -> bool:
    """Recognize unified/compatibility Han, including astral extensions."""

    return "〇" in text or any(
        start <= ord(char) <= end
        for char in text
        for start, end in _HAN_CODEPOINT_RANGES
    )


def witness_protocol(witness: Mapping[str, Any]) -> str:
    declared = witness.get("witness_protocol")
    return (
        str(declared)
        if isinstance(declared, str) and declared
        else LEGACY_SIGHTED_PROTOCOL
    )


def supported_witness_protocol(witness: Mapping[str, Any]) -> bool:
    return witness_protocol(witness) in {
        BLIND_PINYIN_PROTOCOL,
        LEGACY_SIGHTED_PROTOCOL,
    }


def bind_blind_witness_protocol(
    witness: Mapping[str, Any],
    *,
    witness_request: Mapping[str, Any],
) -> dict[str, Any]:
    """Annotate a fresh response from a hash-bound blind request.

    Frozen receipts never pass through this function, so an absent field on
    historical evidence remains legacy_sighted rather than being relabelled.
    """

    bound = dict(witness)
    if witness_request.get("witness_protocol") == BLIND_PINYIN_PROTOCOL:
        bound.setdefault("witness_protocol", BLIND_PINYIN_PROTOCOL)
    return bound
