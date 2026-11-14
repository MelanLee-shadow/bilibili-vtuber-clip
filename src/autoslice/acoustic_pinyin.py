"""Shared candidate-to-dictation pinyin comparison primitives."""

from __future__ import annotations

import re
import unicodedata

try:  # production optional dependency; callers fail closed on absence
    from pypinyin import lazy_pinyin as _lazy_pinyin
except Exception:  # pragma: no cover - environment dependent
    _lazy_pinyin = None


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


