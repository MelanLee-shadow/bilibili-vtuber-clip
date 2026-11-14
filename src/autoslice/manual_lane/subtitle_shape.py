"""Word and protected-span boundaries for the manual lane's subtitle shape."""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import re
from functools import lru_cache
from typing import Any

from src.autoslice.subtitle_rendering import _unsafe_word_cuts
from .name_match import PROPER_NOUNS

_NUMBER = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿点]+)"
_UNIT = r"(?:个小时|分钟|秒钟|小时|毫秒|千克|公斤|公里|千米|厘米|毫米|毫升|个月|星期|块钱|年|月|日|号|天|周|秒|米|克|斤|升|元|岁|次|个|件|张|杯|份|位|只|条|%|％)"
_MEASURE = re.compile(rf"(?:{_NUMBER}\s*{_UNIT})+")
_MIXED_NAME = re.compile(r"[\u3400-\u9fff]{1,3}[A-Za-z][A-Za-z0-9]*")
_PAIRS = {"《": "》", "「": "」"}


@lru_cache(maxsize=1)
def _icu() -> tuple[Any, Any, Any, Any]:
    name = ctypes.util.find_library("icui18n") or ctypes.util.find_library("icucore")
    if not name:
        raise RuntimeError("subtitle word segmentation requires the system ICU library")
    lib = ctypes.CDLL(name)
    match = re.search(r"\.so\.(\d+)", name)
    suffix = "_" + match.group(1) if match else ""
    def fn(name: str, args: list[Any], result: Any) -> Any:
        function = getattr(lib, name + suffix)
        function.argtypes, function.restype = args, result
        return function
    pointer = ctypes.c_void_p
    integer = ctypes.c_int32
    return (fn("ubrk_open", [ctypes.c_int, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint16), integer,
                              ctypes.POINTER(integer)], pointer),
            fn("ubrk_first", [pointer], integer), fn("ubrk_next", [pointer], integer),
            fn("ubrk_close", [pointer], None))


def word_boundaries(text: str) -> set[int]:
    """ICU Chinese word boundaries, converted from UTF-16 offsets to Python offsets."""
    raw = text.encode("utf-16-le")
    units = (ctypes.c_uint16 * (len(raw) // 2)).from_buffer_copy(raw)
    open_break, first, advance, close = _icu()
    error = ctypes.c_int32(0)
    iterator = open_break(1, b"zh", units, len(units), ctypes.byref(error))
    if not iterator or error.value > 0:
        raise RuntimeError(f"ICU word segmentation failed: {error.value}")
    offsets = {0: 0}
    count = 0
    for i, char in enumerate(text, 1):
        count += 2 if ord(char) > 0xFFFF else 1
        offsets[count] = i
    try:
        boundary = first(iterator)
        result: set[int] = set()
        while boundary != -1:
            if boundary in offsets:
                result.add(offsets[boundary])
            boundary = advance(iterator)
        return result
    finally:
        close(iterator)


@lru_cache(maxsize=1)
def _manual_names() -> frozenset[str]:
    rows = json.loads(PROPER_NOUNS.read_text(encoding="utf-8"))
    return frozenset(str(value) for row in rows
                     for value in (row.get("name"), row.get("canonical"),
                                   *(row.get("aliases") or []), *(row.get("variants") or []))
                     if value and len(str(value)) > 1)


RELAXED = (0, 1, 2)      # legal_breaks levels, tried in this order by a caller that must find a break


def legal_breaks(text: str, level: int = 0) -> set[int]:
    """Word boundaries excluding known terms, measured quantities, and bracketed titles.

    Keeping a name or a foreign word whole is a wish, not a law: a line that holds a run longer than a subtitle line
    (a viewer's long Latin user name) has no break under it.  `level` 1 keeps only what a cut would really damage
    (inside a Latin word or a number, a measured quantity, a bracketed title) and gives back the other word
    boundaries; `level` 2 is every position between two characters, a bracketed title excepted: a title is never
    cut, at any level (one that cannot fit is refused, as it always was)."""
    blocked = _unsafe_word_cuts(text) if level == 0 else set() if level >= 2 else {
        i for i in range(1, len(text)) if text[i - 1].isascii() and text[i - 1].isalnum() and text[i].isascii() and text[i].isalnum()}
    for pattern in ((_MEASURE, _MIXED_NAME, *(re.compile(re.escape(term), re.IGNORECASE) for term in _manual_names()))
                    if level == 0 else (_MEASURE,) if level == 1 else ()):
        for match in pattern.finditer(text):
            blocked.update(range(match.start() + 1, match.end()))
    stack: list[str] = []
    title_blocked: set[int] = set()
    for index, char in enumerate(text):
        if char in _PAIRS:
            stack.append(_PAIRS[char])
        if stack and index + 1 < len(text):
            title_blocked.add(index + 1)
        if stack and char == stack[-1]:
            stack.pop()
            if not stack:
                title_blocked.discard(index + 1)
    return (word_boundaries(text) if level < 2 else set(range(1, len(text)))) - blocked - title_blocked - {0, len(text)}
