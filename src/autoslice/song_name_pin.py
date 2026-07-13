"""Deterministic talk-lane song-name pinning (Ivan 2026-07-13).

Verified 2026-07-11 delivery bug: a talk clip's first cue shipped as
``下一首歌是爱拉拉爱`` when she actually said 下一首歌是《爱啦啦》 — machine
evidence (the on-screen songlist panel + an earlier ``点歌 爱啦啦`` danmaku)
existed, but the talk-lane subtitle-correction path had zero song-name
context.  The LLM correction lanes now get a ``song_name_candidates`` list as
PROMPT CONTEXT (see ``song_name_candidates_prompt_block``), but a prompt is
advisory, not proof.  This module is the deterministic belt: any cue where the
host signals a song mention (下一首/点歌/想唱/...) gets its trailing mention
span fuzzy-matched against the machine-evidence candidate list and, on a
strong match, rewritten to 《candidate title》.  ASR text alone is NEVER
trusted to invent a song title with no candidate backing it — a cue with no
intent phrase, or whose best candidate match is weak, is left untouched.
"""

from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from typing import Sequence

from src.autoslice.jingting_chunker import SrtCue, parse_srt_cues

# 下一首歌还没想好 must NOT trigger a replacement (no candidate can fuzzy-match
# "还没想好"), but the intent phrase itself is intentionally broad — the
# similarity threshold below is what keeps this pass fail-closed, not a
# narrower regex.
SONG_MENTION_INTENT_RX = re.compile(r"(下一首|点歌|想唱|接下来唱|唱一首|唱个)")
_ALREADY_TITLED_RX = re.compile(r"[《》]")
# A cue's trailing mention span stops at the first clause boundary so a long
# run-on cue can't have unrelated later content swept into the replacement;
# the similarity threshold is the second, stronger guard against that.
_CLAUSE_BREAK_RX = re.compile(r"[。！？!?，,～~]")
_FOLD_STRIP_RX = re.compile(r"[\s·・.]+")
# Small confusable fold table for common ASR homophone slips on stylized
# song-name syllables (啦/拉, etc).  pypinyin is NOT installed in this repo
# (`python3 -c "import pypinyin"` → ModuleNotFoundError, 2026-07-13 check) and
# there is no requirements/setup manifest to add it to, so pinyin-without-tones
# similarity is not used here — this fold table plus char-level
# SequenceMatcher is the whole similarity model, matching Ivan's
# no-new-dependency constraint.
_CONFUSABLE_FOLD = {
    "啦": "拉",
    "咯": "喽",
    "呀": "鸦",
    "哦": "喔",
    "的": "得",
}
MIN_REPLACE_RATIO = 0.75
MIN_CANDIDATE_LEN = 2
SONG_NAME_PIN_AUDIT_SCHEMA_VERSION = "song-name-pin-audit.v1"


def fold_for_similarity(text: str) -> str:
    """Fold ASCII case, drop separator punctuation, and apply the confusable
    table so ``爱拉拉`` and ``爱啦啦`` compare equal."""

    stripped = _FOLD_STRIP_RX.sub("", str(text or "")).lower()
    return "".join(_CONFUSABLE_FOLD.get(ch, ch) for ch in stripped)


def _best_suffix_match(clause: str, candidate_folded: str) -> tuple[int, float]:
    """Best (start_index, ratio) among suffixes ``clause[s:]`` vs the folded
    candidate title.

    The window always runs to the clause's end: a stray trailing ASR echo
    character (the extra ``爱`` in ``爱拉拉爱``) belongs to the same mis-heard
    utterance, not a new independent word, so it is absorbed into the
    replacement rather than left dangling after a closing 《》.  A long or
    unrelated tail is naturally rejected because every extra unmatched
    character lowers the ratio below ``MIN_REPLACE_RATIO``.
    """

    best_start, best_ratio = 0, 0.0
    for start in range(len(clause)):
        window = clause[start:]
        if not window.strip():
            continue
        ratio = SequenceMatcher(None, fold_for_similarity(window), candidate_folded).ratio()
        if ratio > best_ratio:
            best_start, best_ratio = start, ratio
    return best_start, best_ratio


def _render_srt(cues: Sequence[SrtCue], texts: Sequence[str]) -> str:
    blocks = []
    for index, (cue, text) in enumerate(zip(cues, texts), start=1):
        blocks.append(f"{index}\n{_srt_timestamp(cue.start_ms)} --> {_srt_timestamp(cue.end_ms)}\n{text}")
    return "\n\n".join(blocks) + "\n" if blocks else ""


def _srt_timestamp(ms: int) -> str:
    hours, rem = divmod(max(0, int(ms)), 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def pin_song_names_in_srt(
    srt_text: str,
    *,
    candidates: Sequence[str],
) -> tuple[str, dict[str, object]]:
    """Deterministically pin a talk cue's song mention from machine evidence.

    Only cues matching ``SONG_MENTION_INTENT_RX`` are ever touched — a plain
    chatter cue with no intent phrase is always a no-op.  At most one
    replacement is applied per cue, and only when the best candidate match
    clears ``MIN_REPLACE_RATIO``.
    """

    cleaned_candidates = [
        str(c).strip() for c in candidates if isinstance(c, str) and str(c).strip()
    ]
    cleaned_candidates = [c for c in cleaned_candidates if len(c) >= MIN_CANDIDATE_LEN]
    input_sha256 = hashlib.sha256(srt_text.encode("utf-8")).hexdigest()
    audit: dict[str, object] = {
        "schema_version": SONG_NAME_PIN_AUDIT_SCHEMA_VERSION,
        "input_srt_sha256": input_sha256,
        "output_srt_sha256": input_sha256,
        "candidates_considered": len(cleaned_candidates),
        "min_replace_ratio": MIN_REPLACE_RATIO,
        "replacements": [],
    }
    if not cleaned_candidates:
        return srt_text, audit

    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    if not cues:
        return srt_text, audit

    folded_candidates = [(candidate, fold_for_similarity(candidate)) for candidate in cleaned_candidates]
    texts = [cue.text for cue in cues]
    for cue_offset, cue in enumerate(cues):
        text = texts[cue_offset]
        intent_match = SONG_MENTION_INTENT_RX.search(text)
        if intent_match is None:
            continue
        tail_start = intent_match.end()
        tail = text[tail_start:]
        if not tail.strip():
            continue
        clause_break = _CLAUSE_BREAK_RX.search(tail)
        clause_end = clause_break.start() if clause_break else len(tail)
        clause = tail[:clause_end]
        if not clause.strip() or _ALREADY_TITLED_RX.search(clause):
            continue  # nothing left to pin, or already annotated
        best: tuple[str, int, float] | None = None
        for candidate, candidate_folded in folded_candidates:
            start, ratio = _best_suffix_match(clause, candidate_folded)
            if best is None or ratio > best[2]:
                best = (candidate, start, ratio)
        if best is None or best[2] < MIN_REPLACE_RATIO:
            continue
        candidate, start, ratio = best
        span_start = tail_start + start
        span_end = tail_start + clause_end
        matched_span = text[span_start:span_end]
        if not matched_span:
            continue
        new_text = f"{text[:span_start]}《{candidate}》{text[span_end:]}"
        texts[cue_offset] = new_text
        audit["replacements"].append(
            {
                "cue_index": cue_offset + 1,
                "start_ms": cue.start_ms,
                "end_ms": cue.end_ms,
                "before": text,
                "after": new_text,
                "matched_span": matched_span,
                "candidate": candidate,
                "ratio": round(ratio, 4),
            }
        )

    if not audit["replacements"]:
        return srt_text, audit

    output = _render_srt(cues, texts)
    audit["output_srt_sha256"] = hashlib.sha256(output.encode("utf-8")).hexdigest()
    return output, audit


def song_name_candidates_prompt_block(candidates: Sequence[str], *, max_items: int = 20) -> str:
    """Short prompt context block shared by every LLM subtitle-correction lane
    (CPA reconcile/correct and the AGY jingting refine prompt) so a talk cue's
    heard song mention is pinned from screen-songlist/点歌 evidence instead of
    dictated/invented by the model."""

    names = [str(c).strip() for c in candidates if isinstance(c, str) and str(c).strip()]
    if not names:
        return ""
    shown = names[:max_items]
    return (
        "\n当场歌单/点歌候选歌名（谈话提到歌名时优先从此列表选定；不得听写生造歌名）：\n"
        + "、".join(shown)
        + "\n"
    )
