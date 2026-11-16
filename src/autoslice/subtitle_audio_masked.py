"""Masked writings for the subtitle/witness timing check (subtitle_audio_correspondence).

A name the channel writes in Latin letters (kmx, IT达人, 礼墨Sumi) is spoken in a way the witness ASR writes as anything
(QQ熊, GIO胸, 爱迪).  When single and exact composite anchors leave a third of the clip uncovered, a final cue (or
adjacent pair) is an anchor if its text, with each such name standing for 1-6 witness characters and every other
character exact, matches exactly one witness cue or adjacent pair, and matches no other final cue or pair.
, semantictalk_1084420: its first third was all kmx; the middle and end agreed within 42-52 ms.

The writings are recorded in the receipt (`anchor_policy.masked_writings`), so a replay uses the same ones; a receipt
without them was made without any.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from src.autoslice import subtitle_audio_correspondence as c

MASKED_WRITING_SPAN = (1, 6)


def masked_writings_of(receipt: Mapping[str, Any]) -> tuple[str, ...]:
    policy = receipt.get("anchor_policy") if isinstance(receipt, Mapping) else None
    found = policy.get("masked_writings", []) if isinstance(policy, Mapping) else []
    if not isinstance(found, list) or not all(isinstance(item, str) and item for item in found):
        raise c.CorrespondenceInputError("MASKED_WRITINGS_INVALID", f"anchor_policy.masked_writings={found!r}")
    return tuple(found)


def normalized(writings: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted({w for w in map(c.normalize_text, writings) if w}, key=lambda w: (-len(w), w)))


def policy(masked: Sequence[str]) -> dict[str, Any]:
    return {"masked_writings": list(masked), "masked_writing_span": list(MASKED_WRITING_SPAN)} if masked else {}


def pattern(text: str, writings: Sequence[str]) -> re.Pattern[str] | None:
    """``text`` with each writing as a bounded wildcard, or None (no writing, or too few exact characters)."""
    parts = re.split("(" + "|".join(map(re.escape, writings)) + ")", text) if writings else [text]
    literal = "".join(parts[0::2])
    if len(parts) == 1 or len(literal) < c.ANCHOR_MIN_CHARS or len(set(literal)) < c.ANCHOR_MIN_UNIQUE_CHARS:
        return None
    low, high = MASKED_WRITING_SPAN
    return re.compile("".join(re.escape(p) if i % 2 == 0 else f".{{{low},{high}}}" for i, p in enumerate(parts)))


def anchors(final_groups: Sequence[tuple[Any, ...]], final_cues: Sequence[Any], witness_cues: Sequence[Any],
            used_witness: set[str], writings: Sequence[str], intro_offset_ms: int) -> list[Any]:
    def every_group(cues: Sequence[Any]) -> list[tuple[Any, ...]]:
        return [(cue,) for cue in cues] + [pair for pair in zip(cues, cues[1:]) if c._pair_gap_is_eligible(pair)]

    all_final, all_witness, found = every_group(final_cues), every_group(witness_cues), []
    for final_group in final_groups:
        if (shape := pattern(c._group_normalized_text(final_group), writings)) is None:
            continue
        finals = [g for g in all_final if shape.fullmatch(c._group_normalized_text(g))]
        witnesses = [g for g in all_witness if shape.fullmatch(c._group_normalized_text(g))]
        if len(finals) != 1 or len(witnesses) != 1 or any(cue.index in used_witness for cue in witnesses[0]):
            continue
        found.append(c._make_anchor(final_group, witnesses[0], similarity=1.0, intro_offset_ms=intro_offset_ms,
                                    resolution=("unique", 0, 0)))
    return found
