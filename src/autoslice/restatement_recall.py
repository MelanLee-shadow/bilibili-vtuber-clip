"""In-session restatement recall: pair garbled cues with later clean restatements.

Ivan 2026-08-08 mechanism request: when a line is interrupted / rushed so ASR
garbles it, 李豆沙 often restates the same sentence slowly and completely a few
cues later. The later restatement is then independent structured text support
for repairing the earlier cue — the same trust shape as the danmu read-aloud
lane (session-internal verbatim text, not model imagination).

This module only *detects and proposes* pairs. It never edits text: proposals
flow into the existing candidate adjudication chain, where the candidate-blind
acoustic witness decides whether the early cue's audio actually contains the
restated sentence (保向铁律 — a true disfluency like a bare restart "我是" must
survive unchanged because its audio really is just the fragment).

Flagship case (2026-08-07 auto_200736_298_383): cue17 final text
「这是我的小孩就是了」(overlapped, machine-labeled 连线) is repaired by cue29
「这是我今天的宣言」(host, slow). Machine speaker labels are deliberately not a
hard pre-filter on the early side — the garbled cue's label is itself part of
what went wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

try:  # pragma: no cover - exercised via the pinyin-present path in tests
    from pypinyin import lazy_pinyin as _lazy_pinyin
except ImportError:  # pragma: no cover
    _lazy_pinyin = None

_NON_LEXICAL_RE = re.compile(r"[^0-9A-Za-z一-鿿]+")
_INTERJECTION_RE = re.compile(r"^[哈啊呃哦嗯呀哎唉噢喔诶欸]+$")
# 她的 cue 开头高频语篇连接词/填充词。只影响配对打分的锚定，绝不参与改字：
# 「然后我…」「但是我…」这类开场会制造大量假前缀锚（探针实测 8/7 五候选
# sim>=0.45+prefix>=3 的 7 对里 4 对是纯连接词锚），剥掉后 prefix_run 只认
# 内容音节。迭代剥离，直到开头不再命中。
_LEADING_CONNECTOR_RE = re.compile(
    r"^(?:然后呢?|但是|就是说?|所以说?|因为|反正|可是|而且|其实|我操|"
    r"[呃嗯啊哦哎唉噢喔诶欸])"
)


@dataclass(frozen=True)
class RestatementCue:
    index: int
    start_seconds: float
    label: str | None
    text: str


@dataclass(frozen=True)
class RestatementPair:
    early_index: int
    late_index: int
    early_text: str
    late_text: str
    similarity: float
    prefix_run: int
    gap_seconds: float


def _lexical(text: str) -> str:
    return _NON_LEXICAL_RE.sub("", text)


def strip_leading_connectors(text: str) -> str:
    """Drop stacked discourse connectors/fillers from the front (scoring only)."""
    lexical = _lexical(text)
    while True:
        stripped = _LEADING_CONNECTOR_RE.sub("", lexical, count=1)
        if stripped == lexical:
            return lexical
        lexical = stripped


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


def _prefix_run(early: list[str], late: list[str]) -> int:
    run = 0
    for a, b in zip(early, late):
        if a != b:
            break
        run += 1
    return run


def score_pair(early_text: str, late_text: str) -> tuple[float, int] | None:
    """(similarity, prefix_run) on toneless pinyin; None when backend absent.

    Similarity is measured on the full lexical content; the prefix anchor is
    measured after connector stripping so ubiquitous openers (然后/但是/…)
    cannot fake an interrupted-start anchor.
    """
    early = pinyin_tokens(early_text)
    late = pinyin_tokens(late_text)
    if early is None or late is None:
        return None
    if not early or not late:
        return (0.0, 0)
    early_anchor = pinyin_tokens(strip_leading_connectors(early_text)) or []
    late_anchor = pinyin_tokens(strip_leading_connectors(late_text)) or []
    return (
        SequenceMatcher(None, early, late).ratio(),
        _prefix_run(early_anchor, late_anchor),
    )


def find_restatement_pairs(
    cues: list[RestatementCue],
    *,
    min_early_chars: int = 4,
    min_late_chars: int = 6,
    min_cue_gap: int = 2,
    max_gap_seconds: float = 90.0,
    min_similarity: float = 0.45,
    min_prefix_run: int = 3,
    host_label: str | None = "李豆沙",
) -> list[RestatementPair]:
    """Ordered restatement proposals; empty when the pinyin backend is absent.

    A pair is proposed when a later, longer-window cue reads as a phonetically
    close restatement of an earlier cue: pinyin similarity over the threshold
    AND a shared spoken prefix (interrupted starts anchor at the front). Exact
    repeats are skipped (nothing to repair), as are pure interjection cues.
    When ``host_label`` is set and the transcript carries labels, only host
    cues qualify as the restatement side; the early side is never label
    filtered.

    Defaults are probe-tuned on the 2026-08-07 five-candidate harvest (408
    cues, scripts/probe_restatement_recall.py): sim>=0.45 with a >=3-syllable
    content-prefix anchor keeps 4 proposals across 5 slices — the flagship
    cue17->29 true positive plus three witness-rejectable benign pairs — with
    zero wrong-repair surface.
    """

    pairs: list[RestatementPair] = []
    for late_pos, late in enumerate(cues):
        if len(_lexical(late.text)) < min_late_chars:
            continue
        if _INTERJECTION_RE.match(_lexical(late.text)):
            continue
        if host_label is not None and late.label is not None and late.label != host_label:
            continue
        for early in cues[:late_pos]:
            if late.index - early.index < min_cue_gap:
                continue
            gap = late.start_seconds - early.start_seconds
            if gap <= 0 or gap > max_gap_seconds:
                continue
            early_lexical = _lexical(early.text)
            if len(early_lexical) < min_early_chars:
                continue
            if _INTERJECTION_RE.match(early_lexical):
                continue
            if early_lexical == _lexical(late.text):
                continue
            scored = score_pair(early.text, late.text)
            if scored is None:
                return []
            similarity, prefix_run = scored
            if similarity < min_similarity or prefix_run < min_prefix_run:
                continue
            pairs.append(
                RestatementPair(
                    early_index=early.index,
                    late_index=late.index,
                    early_text=early.text,
                    late_text=late.text,
                    similarity=round(similarity, 4),
                    prefix_run=prefix_run,
                    gap_seconds=round(gap, 3),
                )
            )
    return pairs
