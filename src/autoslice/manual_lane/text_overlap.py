"""Comparing two texts by their letters: the one place every step compares on.

`letters`: a text's letters and digits (Han characters and kana are letters), nothing else: no punctuation, no spaces.
`key`: the same, lower-cased; what two writings of one sentence are compared on.
`shared`: how many letters of one key are found in another, in order (`min_run`: a shorter run is chance).
`share`: that count as a share of the first key.
`stands`: a word is written in a text, a place where it is only the seam of another word said twice not counted.
Keys with further rules are built on `key` by their owners: doubt_policy.sim_key (filler words removed),
content_compare.content_key (one spelling of digits, one class of pronouns), name_match.sound_key (near sounds).
"""

from __future__ import annotations

from difflib import SequenceMatcher


def letters(text: object) -> str:
    return "".join(ch for ch in str(text) if ch.isalnum())


def key(text: object) -> str:
    return letters(text).lower()


def shared(a: str, b: str, min_run: int = 1) -> int:
    """Letters of `a` found in `b`, in order, counting only runs of at least `min_run`."""
    return sum(m.size for m in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks() if m.size >= min_run)


def share(part: str, whole: str, *, min_run: int = 1, empty: float = 1.0) -> float:
    """`shared` as a share of `part` (`empty` when part has no letters)."""
    return shared(part, whole, min_run) / len(part) if part else empty


def stands(word: str, text: str) -> bool:
    """`word` is written in `text`.  A place where it is only the seam of another word said twice in a row is not
    one: 「晚安晚安」 holds 安晚 where 晚安 meets 晚安 and names nobody (four of the eleven chat lines counted as
    naming 安晚awa were this, shown to the judge of who is in the call as lines about her).  The seam after the second
    saying counts the same (晚安晚安晚...).  The word itself said twice (安晚安晚) stands: its first writing has no such
    word before it; so does a word that merely follows a like character (今晚安晚来吗)."""
    n = len(word)
    at = text.find(word) if n else -1
    while at >= 0:
        seam = False
        for j in range(1, n):                       # the other word starts j characters before this place
            twice = (word[n - j:] + word[:n - j]) * 2
            start = at - j
            seam = seam or (start >= 0 and text[start:start + 2 * n] == twice) or (start >= n and text[start - n:start + n] == twice)
        if not seam:
            return True
        at = text.find(word, at + 1)
    return False
