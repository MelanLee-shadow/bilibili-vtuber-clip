"""How two readings of one sentence are compared on their content (TASK-28; COACH-ADVICE e05 §0 第2步, §1.1, §2, §4, §11).

- content_key: the characters that carry the meaning.  Punctuation and filler words go (doubt_policy.sim_key), the
  spelling of numbers is one (五 / 5), and 他/她/它 are one class: those differences are settled by the final
  normalisation, never by listening.  Repeats are NOT folded here, so a repeat difference stays visible.
- differs_in_content: the two keys differ somewhere other than in the small words (了的就那对呢啦嘛吗).
- repeat_variant: the two differ only in how often a word or a run of words is repeated (我我我 / 我); the repeat rule
  (ASR count, BCUT by default) decides those, not a listening.
- overlap_share: how much of a reading's content the other reading carries, in order (the "一半" of §2).
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from . import doubt_policy
from .lane_rules import rules

_DIGIT_OF = {ch: str(i) for i, ch in enumerate("零一二三四五六七八九")} | {"〇": "0", "两": "2"}
_REPEAT = re.compile(r"(.{1,4}?)\1+")


def content_key(text: str) -> str:
    """sim_key with one spelling of digits and one class for the third-person pronouns."""
    cfg = rules("detection")
    key = doubt_policy.sim_key(text)
    key = "".join(_DIGIT_OF.get(ch, ch) for ch in key)
    pron = str(cfg.get("pronouns_one_class") or "")
    if pron:
        key = re.sub(f"[{re.escape(pron)}]", pron[0], key)
    return key


def collapse_repeats(key: str) -> str:
    """A run said several times in a row is kept once (我我我觉得 -> 我觉得, 就是就是 -> 就是), until nothing changes."""
    while True:
        new = _REPEAT.sub(r"\1", key)
        if new == key:
            return key
        key = new


def repeat_variant(a: str, b: str) -> bool:
    """a and b differ, but only in how often something is repeated."""
    ka, kb = content_key(a), content_key(b)
    return bool(ka) and ka != kb and collapse_repeats(ka) == collapse_repeats(kb)


def differs_in_content(a: str, b: str) -> bool:
    """The content keys differ somewhere other than in the small words (§11: 两家只在这类字上不同时不算疑点)."""
    ka, kb = content_key(a), content_key(b)
    if ka == kb:
        return False
    small = set(str(rules("detection").get("small_words") or ""))
    for op, i1, i2, j1, j2 in SequenceMatcher(None, ka, kb, autojunk=False).get_opcodes():
        if op != "equal" and not set(ka[i1:i2] + kb[j1:j2]) <= small:
            return True
    return False


def overlap_share(part: str, other: str) -> float:
    """The share of `part`'s content characters that `other` also has, in order (1.0 when part has none)."""
    p, o = content_key(part), content_key(other)
    if not p:
        return 1.0
    return sum(b.size for b in SequenceMatcher(None, p, o, autojunk=False).get_matching_blocks()) / len(p)


def unsupported_additions(text: str, current: str, independent: list[str], min_chars: int) -> list[str]:
    """Large added runs with no independent reading of their words (not a whole-sentence similarity vote).

    Compare content without folding repeats, then exempt actual repeat variants and runs already in current.
    A shared prefix cannot license an unrelated tail; independent sources may corroborate different pieces.
    Replacements of the same length are local corrections, not additions. No chosen CPA text is checked here.
    """
    key, base = content_key(text), content_key(current)
    if min_chars <= 0 or repeat_variant(text, current):
        return []
    others = [content_key(t) for t in independent]
    out = []
    for op, i, z, j, end in SequenceMatcher(None, base, key, autojunk=False).get_opcodes():
        if op == "equal" or end - j <= z - i or end - j < min_chars:
            continue
        part = key[j:end]
        supported = [False] * len(part)
        # A block needs the same content floor as the addition. Scattered common characters are not a witness.
        for other in [base, *others]:
            for block in SequenceMatcher(None, part, other, autojunk=False).get_matching_blocks():
                if block.size >= min_chars:
                    supported[block.a:block.a + block.size] = [True] * block.size
        start = 0
        for pos in range(len(part) + 1):
            if pos == len(part) or supported[pos]:
                if pos - start >= min_chars:
                    out.append(part[start:pos])
                start = pos + 1
    return out


def content_chars(text: str) -> int:
    """Content characters of a line: laughter and filler words do not count."""
    from .normalize import strip_laughter

    return len(doubt_policy.sim_key(strip_laughter(text)))
