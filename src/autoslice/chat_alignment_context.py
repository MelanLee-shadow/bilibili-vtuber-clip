"""Context ownership helpers for structured-chat span alignment."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from difflib import SequenceMatcher
from typing import Any

from src.autoslice.chat_evidence import normalize_chat_text


def fragment_spoken_in(fragment: str, context: str) -> bool:
    """Whether context covers at least 80% of a normalized fragment."""

    fragment_norm = normalize_chat_text(fragment)
    context_norm = normalize_chat_text(context)
    if len(fragment_norm) < 2 or not context_norm:
        return False
    common = sum(
        block.size
        for block in SequenceMatcher(
            None, fragment_norm, context_norm
        ).get_matching_blocks()
    )
    return common / len(fragment_norm) >= 0.8


def normalize_with_map(
    text: str,
    normalize: Callable[[str], str],
) -> tuple[str, list[int]]:
    """Return normalized text plus each output character's raw index."""

    chars: list[str] = []
    raw_indexes: list[int] = []
    for raw_index, char in enumerate(text):
        for output in normalize(char):
            chars.append(output)
            raw_indexes.append(raw_index)
    return "".join(chars), raw_indexes


def context_owned_internal_gap_rebase(
    authority: str,
    auth_map: Sequence[int],
    blocks: Sequence[Any],
    prev_context: str,
    *,
    normalize: Callable[[str], str],
    fragment_spoken_in: Callable[[str, str], bool],
) -> tuple[int, str]:
    """Find an internal authority gap wholly owned by the previous cue.

    Fuzzy coverage is useful for the gap itself, but it is unsafe for the
    coalesced prefix: a single missing polarity token (for example ``不是``)
    can still score above 0.8 and reverse the meaning.  The complete prefix
    therefore needs normalized exact containment before alignment may rebase
    past it.
    """

    normalized_context = normalize(prev_context)
    rebase_index = 0
    owned_gap = ""
    for index in range(1, len(blocks)):
        previous = blocks[index - 1]
        current = blocks[index]
        previous_end = previous.a + previous.size
        if current.a <= previous_end:
            continue
        gap_raw_lo = auth_map[previous_end - 1] + 1
        gap_raw_hi = auth_map[current.a]
        gap_raw = authority[gap_raw_lo:gap_raw_hi]
        coalesced_prefix = authority[:gap_raw_hi]
        normalized_prefix = normalize(coalesced_prefix)
        if (
            fragment_spoken_in(gap_raw, prev_context)
            and len(normalized_prefix) >= 2
            and normalized_prefix in normalized_context
        ):
            rebase_index = index
            owned_gap = gap_raw
    return rebase_index, owned_gap
