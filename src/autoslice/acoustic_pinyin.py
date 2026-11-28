"""Shared candidate-to-dictation pinyin comparison primitives."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

try:  # production optional dependency; callers fail closed on absence
    from pypinyin import lazy_pinyin as _lazy_pinyin
except Exception:  # pragma: no cover - environment dependent
    _lazy_pinyin = None


@lru_cache(maxsize=1)
def valid_syllables() -> frozenset[str]:
    """Every toneless Mandarin syllable the pinyin library knows (ü written v)."""
    from pypinyin.contrib.tone_convert import to_normal
    from pypinyin.pinyin_dict import pinyin_dict
    out: set[str] = set()
    for raw_values in pinyin_dict.values():
        for tok in raw_values.split(","):
            syl = to_normal(tok).strip().lower().replace("ü", "v")
            if syl.isascii() and syl.isalpha():
                out.add(syl)
    return frozenset(out)


@lru_cache(maxsize=1)
def phrase_words() -> frozenset[str]:
    """The words of the pinyin library's phrase dictionary: a plain list of common Chinese words."""
    from pypinyin.phrases_dict import phrases_dict
    return frozenset(phrases_dict.keys())


def text_pinyin_tokens(text: str) -> list[str] | None:
    if _lazy_pinyin is None:
        return None
    # pypinyin returns punctuation as non-Han tokens too. Those are pause
    # boundaries, not speech syllables; counting them manufactures omissions
    # in an otherwise matching acoustic witness (e.g. 要啊，不要。).
    speech_text = "".join(
        " " if unicodedata.category(character)[0] in {"P", "Z"} else character
        for character in text
    )
    return [
        re.sub(r"\s+", "", token.strip().lower())
        for token in _lazy_pinyin(speech_text)
        if token and token.strip()
    ]


