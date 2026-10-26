"""Conservative full-row metadata labels found in timed lyric streams."""

from __future__ import annotations

import re
import unicodedata


_LRC_SECTION_MARKER = re.compile(
    r"""(?ix)
    ^[\s\[(【{<]*(
        (?:(?:guitar|keyboard|drum|bass)\s+)?solo
        |instrumental(?:\s+(?:break|bridge|interlude|intro|outro|section))?
        |interlude|intro|outro
        |(?:吉他|键盘|鼓|贝斯)?(?:独奏|solo)
        |前奏|间奏|过门|尾奏|器乐(?:段|间奏)?
        |(?:ギター|キーボード|ドラム|ベース)?ソロ
        |インスト(?:ゥルメンタル)?|間奏|前奏|後奏
    )[\s\])】}>.!！。:：-]*$
    """
)


def is_lrc_section_metadata(text: str) -> bool:
    """Match a complete non-vocal label without deleting lyric sentences."""

    normalized = unicodedata.normalize("NFKC", text).strip()
    return bool(_LRC_SECTION_MARKER.fullmatch(normalized))


_PERFORMER_ROLE = r"(?:男|女|合|合唱|齐唱|独唱|男声|女声|童声|和声|合声|主声|主唱|伴唱|领唱|高声部|低声部|[SATB]|male|female|all|duet|choir)"
_PERFORMER_HANDLE = r"[A-Za-z][A-Za-z0-9_.-]*"
_PERFORMER_LABEL = re.compile(
    rf"(?:{_PERFORMER_ROLE}(?:\s+{_PERFORMER_HANDLE}主)?|{_PERFORMER_HANDLE}"
    rf"(?:\s*(?:主|(?:这里)?要录[一二两三四0-9]+遍主声))?)",
    re.IGNORECASE,
)


def is_lrc_performer_metadata(text: str) -> bool:
    """A complete bracketed role/handle is a label, never a lyric prefix.

    Round brackets accept known voice roles only: parenthesized lyric words
    remain lyrics. Square/Chinese brackets also accept a single Latin handle
    and explicit vocal-production directions. Arbitrary Chinese sentences,
    multiword English lyrics and any text after the closing bracket survive.
    """
    normalized = unicodedata.normalize("NFKC", text).strip()
    pairs = {"[": "]", "【": "】", "(": ")"}
    if not normalized or normalized[0] not in pairs or normalized[-1] != pairs[normalized[0]]:
        return False
    body = normalized[1:-1].strip()
    if normalized[0] == "(":
        return re.fullmatch(_PERFORMER_ROLE, body, re.IGNORECASE) is not None
    return _PERFORMER_LABEL.fullmatch(body) is not None
