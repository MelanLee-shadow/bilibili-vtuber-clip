"""封面文案分行/切词的无损校验（从 cover_generation 抽出的纯函数簇）。

这些校验器保证 LLM 给的分行/切词方案与封面文案逐字等价（维护者
铁律：整条文案必须完整上封面，字号靠多换行放大，断行绝不拆词/hook/专名）。
"""

from __future__ import annotations

import re

_COVER_QUOTED_SPAN_RX = re.compile(r"[“‘「『][^”’」』\n]{1,8}[”’」』]")


def _cover_lines_canon(text: str) -> str:
    """Canonical form for comparing a line split against the cover text.

    Only layout whitespace may disappear.  Punctuation is visible title
    content: accepting a split that drops ``？`` or moves ``，`` onto a lonely
    line produced a visibly broken July 10 cover despite a hash-clean package.
    """
    return re.sub(r"\s+", "", text)


def _validated_cover_lines(value: object, cover_text: str, *, hook_word: str, max_lines: int) -> tuple[str, ...]:
    """Accept an LLM line split only when it is provably lossless and renderable:
    same characters in the same order, 《song》 and the hook word intact within a
    single line, sane line count/length.  Anything else → () → balancer fallback
    (word-blind, but never worse than before)."""
    if not isinstance(value, (list, tuple)) or not (1 <= len(value) <= max_lines):
        return ()
    lines = []
    closing_punctuation = tuple("，,、；;！!？?。）》】”’")
    opening_punctuation = tuple("（(《【“‘")
    for item in value:
        if not isinstance(item, str):
            return ()
        line = item.strip()
        if not line or len(line) > 12:
            return ()
        if line.startswith(closing_punctuation) or line.endswith(opening_punctuation):
            return ()
        lines.append(line)
    if _cover_lines_canon("".join(lines)) != _cover_lines_canon(cover_text):
        return ()
    for atom in [
        *re.findall(r"《[^》]*》", cover_text),
        *_COVER_QUOTED_SPAN_RX.findall(cover_text),
        *([hook_word] if hook_word else []),
    ]:
        if atom and not any(atom in line for line in lines):
            return ()  # a song name / quoted catchphrase / hook must stay whole
    return tuple(lines)


def _validated_cover_words(value: object, cover_text: str, *, hook_word: str) -> tuple[str, ...]:
    """Accept an LLM word segmentation only when it is provably lossless: same
    characters in the same order, every 《song》 and the hook word intact inside
    a single element.  These become wrap ATOMS (维护者: the full title
    stays on the cover, the font grows via MANY line breaks, and a break may
    fall anywhere EXCEPT inside a word / hook / proper noun).  Anything invalid
    → () → the balancer falls back to hook/《song》/ASCII atoms only."""
    if not isinstance(value, (list, tuple)) or not (1 <= len(value) <= 40):
        return ()
    words = []
    closing_punctuation = tuple("，,、；;！!？?。）》】”’")
    opening_punctuation = tuple("（(《【“‘")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return ()
        word = item.strip()
        if len(word) > 12:
            return ()
        if word.startswith(closing_punctuation) or word.endswith(opening_punctuation):
            return ()
        words.append(word)
    if _cover_lines_canon("".join(words)) != _cover_lines_canon(cover_text):
        return ()
    for atom in [
        *re.findall(r"《[^》]*》", cover_text),
        *_COVER_QUOTED_SPAN_RX.findall(cover_text),
        *([hook_word] if hook_word else []),
    ]:
        if atom and not any(atom in word for word in words):
            return ()
    return tuple(words)
