"""Shared script distribution for the existing subtitle text protocols.

These are the protocols' existing bounded ranges, not language detection:
ASCII Latin, hiragana/katakana U+3040–30FF, Hangul syllables U+AC00–D7AF,
and Han U+3400–9FFF. Other code points are retained as ``other``. Consumers
choose their own script scope; a script is never evidence of what was said.
"""
from __future__ import annotations


def character_script(char: str) -> str:
    if "A" <= char <= "Z" or "a" <= char <= "z":
        return "latin"
    if "\u3040" <= char <= "\u30ff":
        return "kana"
    if "\uac00" <= char <= "\ud7af":
        return "hangul"
    if "\u3400" <= char <= "\u9fff":
        return "han"
    return "other"


def script_profile(text: str) -> dict[str, int]:
    """Count each supported script (and other) without normalizing the input."""
    counts = dict.fromkeys(("latin", "kana", "hangul", "han", "other"), 0)
    for char in text:
        counts[character_script(char)] += 1
    return counts
