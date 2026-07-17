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
