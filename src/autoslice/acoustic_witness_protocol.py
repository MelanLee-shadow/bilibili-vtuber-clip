"""Typed protocol provenance for candidate-blind acoustic witnesses."""

from __future__ import annotations

import json
import re
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


WITNESS_SCHEMA = "subtitle-span-acoustic-witness.v1"
_LEGACY_PROMPT_COPY_PINYIN = "zhe ge shi he tian yi de lian dong o"

_PINYIN_SYLLABLE_RX = re.compile(r"^(?:[a-zü]+|\?)$")


def _witness_report_error(
    request: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> tuple[str, str] | None:
    """Share the same observation gate between provider fallback and verdicts."""

    heard = str(observed.get("heard_pinyin") or "").strip().lower()
    tokens = heard.split()
    confidence = observed.get("confidence")
    uncertain_positions = observed.get("uncertain_positions")
    syllable_count = observed.get("syllable_count")
    # 静音证词：删除提案的时窗里确实无语音时，
    # 空听写 + target_audible=False 是完整有效的观察，不是报告缺陷——
    # 强制非空会把「确认无声」翻译成 WITNESS_REPORT_INVALID→UNCERTAIN，
    # 删除类发现永久卡死。有声报告仍必须逐音节过拼音正则。
    silence_observation = (
        not tokens and observed.get("target_audible") is False
    )
    # Reject the exact demonstration phrase that contaminated the old prompt.
    # This guard also makes copied v1 artifacts fail closed even if an operator
    # accidentally moves one outside the versioned cache contract.
    if heard == _LEGACY_PROMPT_COPY_PINYIN:
        return (
            "WITNESS_PROMPT_COPY_DETECTED",
            "legacy demonstration phrase was copied instead of dictated",
        )
    report_valid = (
        observed.get("schema_version") == WITNESS_SCHEMA
        and observed.get("status") == "OBSERVED"
        and isinstance(observed.get("target_audible"), bool)
        and (bool(tokens) or silence_observation)
        and all(_PINYIN_SYLLABLE_RX.fullmatch(token) for token in tokens)
        and not contains_han_text(json.dumps(dict(observed), ensure_ascii=False))
        and not any(
            key in observed
            for key in (
                "candidate_id",
                "canonical_entity",
                "proposed_cue",
                "rewritten_text",
                "current_fit",
                "proposed_fit",
            )
        )
        and not isinstance(confidence, bool)
        and isinstance(confidence, (int, float))
        and 0.0 <= float(confidence) <= 1.0
        and isinstance(uncertain_positions, list)
        and all(
            not isinstance(v, bool) and isinstance(v, int) and 0 <= v < len(tokens)
            for v in uncertain_positions
        )
        and not isinstance(syllable_count, bool)
        and isinstance(syllable_count, int)
        and (syllable_count > 0 or silence_observation)
    )
    # The witness's substance is heard_pinyin itself; syllable_count is a
    # redundant self-count that models routinely get off by one (
    # four clean supporting witnesses on 1209_1410 were all invalidated by
    # this arithmetic). The recount below is authoritative; a mismatch is
    # disclosed, never fatal.
    # Physical plausibility backstop: Mandarin peaks near ~9 syllables/s.
    # A rate far above that means the dictation overflowed the target span
    # (1.12s target, 14 syllables) — poisoned evidence, retriable.
    try:
        target_span_s = max(
            0.001,
            (int(request["matched_end_ms"]) - int(request["matched_start_ms"]))
            / 1000.0,
        )
    except (KeyError, TypeError, ValueError):
        target_span_s = None
    if (
        report_valid
        and target_span_s is not None
        and (len(tokens) - 2) / target_span_s > 9.0
    ):
        return (
            "WITNESS_IMPLAUSIBLE_SYLLABLE_RATE",
            f"{len(tokens)} syllables over {target_span_s:.2f}s target",
        )
    if not report_valid:
        return (
            "WITNESS_REPORT_INVALID",
            str(observed.get("reason") or ""),
        )
    return None
