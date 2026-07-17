"""Final, profile-bound surface invariants.

Ordinary transcript canonicalization is advisory and may be superseded by
better evidence.  Rules marked ``hard-meme-canon`` are different: they are
unbypassable final-output policy and therefore run after every mutable text
stage.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.jingting_chunker import parse_srt_cues


REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(REPO_ROOT)
_HARD_MEME_SURFACE_RULES = tuple(
    rule
    for rule in CHANNEL_PROFILE.canonical_surface_rules
    if rule.authority.endswith("-hard-meme-canon.v1")
)


def canonicalize_hard_meme_surfaces(
    text: str,
) -> tuple[str, list[dict[str, str | int]]]:
    normalized = text
    replacements: list[dict[str, str | int]] = []
    for rule in _HARD_MEME_SURFACE_RULES:
        count = normalized.count(rule.surface)
        if not count:
            continue
        normalized = normalized.replace(rule.surface, rule.canonical)
        replacements.append(
            {
                "surface": rule.surface,
                "canonical": rule.canonical,
                "authority": rule.authority,
                "count": count,
            }
        )
    return normalized, replacements


def _srt_timestamp(ms: int) -> str:
    hours, remainder = divmod(max(0, int(ms)), 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def normalize_hard_meme_surfaces(
    srt_text: str,
) -> tuple[str, dict[str, Any]]:
    """Re-assert unbypassable meme canon without touching SRT timing."""

    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    texts = [cue.text for cue in cues]
    repairs: list[dict[str, Any]] = []
    for offset, before in enumerate(list(texts)):
        after, replacements = canonicalize_hard_meme_surfaces(before)
        if after == before:
            continue
        texts[offset] = after
        repairs.append(
            {
                "cue_index": offset + 1,
                "matched_start_ms": cues[offset].start_ms,
                "matched_end_ms": cues[offset].end_ms,
                "before": before,
                "after": after,
                "replacements": replacements,
            }
        )
    output = srt_text
    if repairs:
        output = "\n\n".join(
            (
                f"{index}\n{_srt_timestamp(cue.start_ms)} --> "
                f"{_srt_timestamp(cue.end_ms)}\n{text.strip()}"
            )
            for index, (cue, text) in enumerate(zip(cues, texts), start=1)
        ) + "\n"
    return output, {
        "schema_version": "hard-meme-surface-audit.v1",
        "status": "APPLIED" if repairs else "NO_CHANGE",
        "rule_count": len(_HARD_MEME_SURFACE_RULES),
        "repairs": repairs,
        "input_srt_sha256": hashlib.sha256(srt_text.encode()).hexdigest(),
        "output_srt_sha256": hashlib.sha256(output.encode()).hexdigest(),
    }
