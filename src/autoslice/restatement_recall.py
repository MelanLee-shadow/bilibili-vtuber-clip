"""In-session restatement recall: pair garbled cues with later clean restatements.

维护者 mechanism request: when a line is interrupted / rushed so ASR
garbles it, 李豆沙 often restates the same sentence slowly and completely a few
cues later. The later restatement is then independent structured text support
for repairing the earlier cue — the same trust shape as the danmu read-aloud
lane (session-internal verbatim text, not model imagination).

This module only *detects and proposes* pairs. It never edits text: proposals
flow into the existing candidate adjudication chain, where the candidate-blind
acoustic witness decides whether the early cue's audio actually contains the
restated sentence (保向铁律 — a true disfluency like a bare restart "我是" must
survive unchanged because its audio really is just the fragment).

Flagship case (auto_200736_298_383): cue17 final text
「这是我的小孩就是了」(overlapped, machine-labeled 连线) is repaired by cue29
「这是我今天的宣言」(host, slow). Machine speaker labels are deliberately not a
hard pre-filter on the early side — the garbled cue's label is itself part of
what went wrong.
"""

from __future__ import annotations

import re


try:  # pragma: no cover - exercised via the pinyin-present path in tests
    from pypinyin import lazy_pinyin as _lazy_pinyin
except ImportError:  # pragma: no cover
    _lazy_pinyin = None


_NON_LEXICAL_RE = re.compile(r"[^0-9A-Za-z一-鿿]+")
# Definition + local lexical connectors above: these carry discourse meaning,
# but cannot be prefix anchors for restatement scoring. They are not sounds.
# 然后/就是 and vocal prefixes now read the shared policy-bound scoring roles;
# this local semantic operation must not strip referential 那个 or 了 in 了不起.


def pinyin_tokens(text: str) -> list[str] | None:
    """Toneless pinyin tokens of the lexical content; None when backend absent."""
    if _lazy_pinyin is None:
        return None
    lexical = _NON_LEXICAL_RE.sub(" ", text)
    return [
        re.sub(r"\s+", "", str(token).lower())
        for token in _lazy_pinyin(lexical.split())
        if str(token).strip()
    ]


