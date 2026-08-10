"""Writing-system classification for LRC candidates against a Chinese ASR.

The song lane's identity gate ranks LRC candidates by how much of the sheet the
performance ASR literally reproduces.  That ratio is a real signal for Chinese
songs and **meaningless** for a Japanese or Korean one: the source ASR only
emits Chinese, so a correct 花の塔 / 夜に駆ける / 마리아 sheet scores exactly
0%, while Chinese homophone noise from the same garbled transcript scores 4-19%
and wins.  Measured on the 2026-08-08 session (``song_selector_full`` repair
reports, read-only):

* ``song_200130_1012``  正主 ``花の塔`` 0%  → 选中噪声 ``乌鲁木齐九月`` 19%
* ``song_213135_1073``  正主日文候选 0%    → 选中噪声 ``无照青春``     16%
* ``song_213135_1541``  正主 ``少女レイ`` 0% → 选中噪声 ``踏浪``          4%

This module answers only "is comparing this sheet against a Chinese ASR
meaningful?".  It never decides identity — the audio model still does, and it
has repeatedly proven able to reject a wrong sheet (``canonical LRC line 0 was
not affirmatively heard``).
"""

from __future__ import annotations

import re
from typing import Iterable

from src.autoslice.song_common import LrcResult, is_lrc_non_lyric_metadata

# Kana and Hangul are the decisive marks: Japanese lyrics carry kanji too, so
# Han presence alone cannot separate them from Chinese, but a Chinese sheet
# never carries kana or Hangul.
_KANA_HANGUL = re.compile(r"[぀-ゟ゠-ヿㇰ-ㇿ가-힯ᄀ-ᇿ㄰-㆏]")
_HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_LATIN = re.compile(r"[A-Za-z]")

# A single stray kana in an otherwise Chinese sheet (a borrowed 「の」 in a
# title line, an emoticon) must not unlock the bypass.
MIN_FOREIGN_SCRIPT_RATIO = 0.15
# Pure-Latin sheets have no Han at all; require enough letters that a two-word
# romanized title line cannot qualify a Chinese song's credit block.
MIN_LATIN_ONLY_CHARS = 40


def _lyric_body(lrc: LrcResult) -> str:
    return "".join(
        line.text for line in lrc.lines if not is_lrc_non_lyric_metadata(line.text)
    )


def lrc_is_cross_script_for_chinese_asr(lrc: LrcResult) -> bool:
    """Whether a Chinese ASR literally cannot match this sheet's lyric body."""

    body = _lyric_body(lrc)
    if not body:
        return False
    foreign = len(_KANA_HANGUL.findall(body))
    han = len(_HAN.findall(body))
    if foreign:
        return foreign / (foreign + han) >= MIN_FOREIGN_SCRIPT_RATIO if (foreign + han) else False
    if han:
        return False
    return len(_LATIN.findall(body)) >= MIN_LATIN_ONLY_CHARS


def group_is_cross_script_for_chinese_asr(group: Iterable[tuple]) -> bool:
    """Whether every LRC in a clustered identity group is cross-script.

    ``all`` rather than ``any``: a group that mixes a Chinese sheet with a
    foreign one is exactly the case where the ASR ratio still carries
    information, so it must keep the ordinary non-zero requirement.
    """

    members = [item[1] for item in group]
    return bool(members) and all(
        lrc_is_cross_script_for_chinese_asr(member) for member in members
    )
