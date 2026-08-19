"""Shared candidate-to-dictation pinyin comparison primitives."""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Sequence

try:  # production optional dependency; callers fail closed on absence
    from pypinyin import lazy_pinyin as _lazy_pinyin
except Exception:  # pragma: no cover - environment dependent
    _lazy_pinyin = None


def text_pinyin_tokens(text: str) -> list[str] | None:
    if _lazy_pinyin is None:
        return None
    return [
        re.sub(r"\s+", "", token.strip().lower())
        for token in _lazy_pinyin(text)
        if token and token.strip()
    ]


def heard_pinyin_tokens(value: object) -> list[str]:
    return [token for token in str(value or "").strip().lower().split() if token]


def pinyin_similarity(
    candidate: Sequence[str],
    heard: Sequence[str],
    *,
    uncertain_positions: Sequence[int] = (),
    character_level: bool = False,
) -> float:
    """Shared similarity; character mode preserves microcue discovery."""

    if not candidate or not heard:
        return 0.0
    uncertain = {
        int(value)
        for value in uncertain_positions
        if isinstance(value, int) and not isinstance(value, bool)
    }
    normalized_heard = list(heard)
    for index, token in enumerate(normalized_heard):
        if index < len(candidate) and (token == "?" or index in uncertain):
            normalized_heard[index] = candidate[index]
    if character_level:
        return difflib.SequenceMatcher(
            None,
            " ".join(candidate),
            " ".join(normalized_heard),
        ).ratio()
    matcher = difflib.SequenceMatcher(None, normalized_heard, list(candidate))
    blocks = matcher.get_matching_blocks()
    matched = sum(block.size for block in blocks)
    matched_positions = {
        position
        for block in blocks
        for position in range(block.a, block.a + block.size)
    }
    wildcard_credit = sum(
        1
        for index, token in enumerate(heard)
        if index not in matched_positions
        and (token == "?" or index in uncertain)
    )
    effective = matched + min(wildcard_credit, max(0, len(candidate) - matched))
    return (2.0 * effective) / (len(candidate) + len(heard))


def pinyin_compatibility(
    candidate_text: str,
    *,
    heard_pinyin: str,
    uncertain_positions: Sequence[int] = (),
) -> float | None:
    candidate = text_pinyin_tokens(candidate_text)
    if candidate is None:
        return None
    return pinyin_similarity(
        candidate,
        heard_pinyin_tokens(heard_pinyin),
        uncertain_positions=uncertain_positions,
    )


def candidate_pinyin_similarities(
    *,
    current_text: str,
    proposed_text: str,
    heard_pinyin: str,
    uncertain_positions: Sequence[int] = (),
) -> dict[str, float | None]:
    return {
        "current": pinyin_compatibility(
            current_text,
            heard_pinyin=heard_pinyin,
            uncertain_positions=uncertain_positions,
        ),
        "proposed": pinyin_compatibility(
            proposed_text,
            heard_pinyin=heard_pinyin,
            uncertain_positions=uncertain_positions,
        ),
    }


def neutral_syllable_count_hint(current_text: str, proposed_text: str) -> int | None:
    """Expose length only when both closed-set candidates imply the same count."""

    current = _safe_speech_syllable_count(current_text)
    proposed = _safe_speech_syllable_count(proposed_text)
    if current is not None and current == proposed:
        return current
    return None


def _safe_speech_syllable_count(text: str) -> int | None:
    """Count Han speech units while refusing ambiguous mixed-script input."""

    if _lazy_pinyin is None:
        return None
    han: list[str] = []
    for character in text:
        codepoint = ord(character)
        is_han = (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
            or 0x20000 <= codepoint <= 0x2FA1F
        )
        if is_han:
            han.append(character)
            continue
        if character.isspace() or unicodedata.category(character)[0] in {"P", "Z"}:
            continue
        return None
    tokens = [str(token).strip().lower() for token in _lazy_pinyin("".join(han))]
    if not tokens or any(re.fullmatch(r"[a-züv]+", token) is None for token in tokens):
        return None
    return len(tokens)
