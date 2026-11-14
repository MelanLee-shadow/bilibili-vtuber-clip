"""Comparing two texts by their letters: the one place every step compares on.

`letters`: a text's letters and digits (Han characters and kana are letters), nothing else: no punctuation, no spaces.
`key`: the same, lower-cased; what two writings of one sentence are compared on.
`shared`: how many letters of one key are found in another, in order (`min_run`: a shorter run is chance).
`share`: that count as a share of the first key.
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
