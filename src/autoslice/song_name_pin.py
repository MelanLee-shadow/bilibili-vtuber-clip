"""Deterministic talk-lane song-name pinning (维护者).

Verified delivery bug: a talk clip's first cue shipped as
``下一首歌是爱拉拉爱`` when she actually said 下一首歌是《爱啦啦》 — machine
evidence (the on-screen songlist panel + an earlier ``点歌 爱啦啦`` danmaku)
existed, but the talk-lane subtitle-correction path had zero song-name
context.  The correction lanes get ``song_name_candidates`` as advisory prompt
context, but this mutator accepts only a hash-bound
``song-name-semantic-verification.v1`` with a unique lyric-context winner.  A
song-intent cue must also have a strong surface match to some member of the
closed candidate set; this lets lyric semantics correct a franchise-like
mishearing without turning an unrelated tail into a song title.
"""

from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from typing import Mapping, Sequence

from src.autoslice.jingting_chunker import SrtCue, parse_srt_cues
from src.autoslice.song_name_semantic_verification import (
    clean_song_name_candidates,
    validate_song_name_semantic_verification,
)

# 下一首歌还没想好 must NOT trigger a replacement (no candidate can fuzzy-match
# "还没想好"), but the intent phrase itself is intentionally broad — the
# similarity threshold below is what keeps this pass fail-closed, not a
# narrower regex.
SONG_MENTION_INTENT_RX = re.compile(r"(下一首|点歌|想唱|接下来唱|唱一首|唱个)")
_TITLED_NAME_RX = re.compile(r"《([^《》]+)》")
# A cue's trailing mention span stops at the first clause boundary so a long
# run-on cue can't have unrelated later content swept into the replacement;
# the similarity threshold is the second, stronger guard against that.
_CLAUSE_BREAK_RX = re.compile(r"[。！？!?，,～~]")
_FOLD_STRIP_RX = re.compile(r"[\s·・.]+")
# Small confusable fold table for common ASR homophone slips on stylized
# song-name syllables (啦/拉, etc).  pypinyin 已装（守卫/审片员
# 同用，可选依赖），作为副路：折叠表+字符 SequenceMatcher 不过阈时，去声调
# 音节序列相等即接受；折叠表保持第一优先，旧回归行为不变。
try:
    from pypinyin import lazy_pinyin as _lazy_pinyin
except Exception:  # pragma: no cover - 依赖缺失环境退回纯折叠表
    _lazy_pinyin = None
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
        if (
            ratio < 1.0
            and _lazy_pinyin is not None
            and _toneless(fold_for_similarity(window)) == _toneless(candidate_folded)
        ):
            # 拼音副路：折叠表没覆盖的同音写法（如 爱啦啦 vs
            # 爱辣辣）按去声调音节等价直接满分——同音即同名。
            ratio = 1.0
        if ratio > best_ratio:
            best_start, best_ratio = start, ratio
    return best_start, best_ratio


def _toneless(value: str) -> tuple[str, ...]:
    if not value or _lazy_pinyin is None:
        return ()
    return tuple(s.lower() for s in _lazy_pinyin(value) if s)


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
    semantic_verification: Mapping[str, object] | None = None,
) -> tuple[str, dict[str, object]]:
    """Deterministically pin a talk cue's song mention from machine evidence.

    Only cues matching ``SONG_MENTION_INTENT_RX`` are touched.  At most one
    replacement is applied per cue; a unique lyric-semantic winner and a
    closed-set surface match clearing ``MIN_REPLACE_RATIO`` are both required.
    """

    cleaned_candidates = clean_song_name_candidates(candidates)
    all_cleaned_candidates = list(cleaned_candidates)
    input_sha256 = hashlib.sha256(srt_text.encode("utf-8")).hexdigest()
    audit: dict[str, object] = {
        "schema_version": SONG_NAME_PIN_AUDIT_SCHEMA_VERSION,
        "input_srt_sha256": input_sha256,
        "output_srt_sha256": input_sha256,
        "candidates_considered": len(cleaned_candidates),
        "min_replace_ratio": MIN_REPLACE_RATIO,
        "semantic_gate_status": "NOT_APPLICABLE",
        "semantic_verification_sha256": None,
        "semantic_candidates": [],
        "replacements": [],
    }
    if not cleaned_candidates:
        return srt_text, audit
    if semantic_verification is None:
        audit["semantic_gate_status"] = "BLOCKED_MISSING_VERIFICATION"
        return srt_text, audit
    receipt = validate_song_name_semantic_verification(
        semantic_verification,
        srt_text=srt_text,
        candidates=cleaned_candidates,
    )
    semantic_rows = {
        str(row["candidate"]): row
        for row in receipt["candidate_results"]
        if isinstance(row, Mapping)
    }
    audit["semantic_verification_sha256"] = receipt["receipt_sha256"]
    audit["semantic_candidates"] = [
        {
            "candidate": candidate,
            "verdict": semantic_rows[candidate]["verdict"],
            "semantic_score": semantic_rows[candidate]["semantic_score"],
            "reason_code": semantic_rows[candidate]["reason_code"],
        }
        for candidate in cleaned_candidates
    ]
    preferred_candidate = receipt.get("preferred_candidate")
    if not isinstance(preferred_candidate, str):
        audit["semantic_gate_status"] = "BLOCKED_NO_UNIQUE_MATCH"
        return srt_text, audit
    preferred_row = semantic_rows.get(preferred_candidate)
    if preferred_row is None or preferred_row.get("verdict") != "MATCH":
        audit["semantic_gate_status"] = "BLOCKED_INVALID_PREFERRED_MATCH"
        return srt_text, audit
    cleaned_candidates = [preferred_candidate]
    audit["semantic_gate_status"] = "VERIFIED_UNIQUE_MATCH"

    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    if not cues:
        return srt_text, audit

    folded_candidates = [(candidate, fold_for_similarity(candidate)) for candidate in cleaned_candidates]
    all_folded_candidates = [
        (candidate, fold_for_similarity(candidate)) for candidate in all_cleaned_candidates
    ]
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
        titled_match = _TITLED_NAME_RX.search(tail)
        clause_break = _CLAUSE_BREAK_RX.search(tail)
        clause_end = clause_break.start() if clause_break else len(tail)
        clause = tail[:clause_end]
        if not clause.strip():
            continue
        if titled_match is not None:
            current_name = titled_match.group(1).strip()
            if fold_for_similarity(current_name) == fold_for_similarity(preferred_candidate):
                continue
            ranked_surfaces = sorted(
                (
                    SequenceMatcher(
                        None,
                        fold_for_similarity(current_name),
                        fold_for_similarity(candidate),
                    ).ratio(),
                    candidate,
                )
                for candidate in all_cleaned_candidates
            )
            surface_ratio, surface_candidate = ranked_surfaces[-1]
            semantic_score = float(preferred_row.get("semantic_score") or 0.0)
            if surface_ratio < MIN_REPLACE_RATIO or semantic_score < 0.8:
                continue
            span_start = tail_start + titled_match.start(1)
            span_end = tail_start + titled_match.end(1)
            new_text = f"{text[:span_start]}{preferred_candidate}{text[span_end:]}"
            texts[cue_offset] = new_text
            audit["replacements"].append(
                {
                    "cue_index": cue_offset + 1,
                    "start_ms": cue.start_ms,
                    "end_ms": cue.end_ms,
                    "before": text,
                    "after": new_text,
                    "matched_span": current_name,
                    "candidate": preferred_candidate,
                    "ratio": round(
                        SequenceMatcher(
                            None,
                            fold_for_similarity(current_name),
                            fold_for_similarity(preferred_candidate),
                        ).ratio(),
                        4,
                    ),
                    "surface_candidate": surface_candidate,
                    "surface_ratio": round(surface_ratio, 4),
                    "semantic_verdict": preferred_row["verdict"],
                    "semantic_score": semantic_score,
                    "decision_surface": "lyrics_semantic_override",
                    "combined_confidence": round(
                        min(1.0, max(surface_ratio, semantic_score) + 0.1 * semantic_score),
                        4,
                    ),
                }
            )
            continue
        best: tuple[str, int, float] | None = None
        for candidate, candidate_folded in folded_candidates:
            start, ratio = _best_suffix_match(clause, candidate_folded)
            if best is None or ratio > best[2]:
                best = (candidate, start, ratio)
        surface_best: tuple[str, int, float] | None = None
        for surface_candidate, candidate_folded in all_folded_candidates:
            surface_start, surface_ratio = _best_suffix_match(clause, candidate_folded)
            if surface_best is None or surface_ratio > surface_best[2]:
                surface_best = (surface_candidate, surface_start, surface_ratio)
        semantic_score = float(preferred_row.get("semantic_score") or 0.0)
        surface_ratio = surface_best[2] if surface_best is not None else 0.0
        if best is None or (
            best[2] < MIN_REPLACE_RATIO
            and (semantic_score < 0.8 or surface_ratio < MIN_REPLACE_RATIO)
        ):
            continue
        candidate, start, ratio = best
        if ratio < MIN_REPLACE_RATIO and surface_best is not None:
            start = surface_best[1]
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
                "surface_candidate": surface_best[0] if surface_best is not None else candidate,
                "surface_ratio": round(surface_ratio, 4),
                "semantic_verdict": preferred_row["verdict"],
                "semantic_score": semantic_score,
                "decision_surface": (
                    "fuzzy_name_plus_lyrics" if ratio >= MIN_REPLACE_RATIO else "lyrics_semantic_override"
                ),
                "combined_confidence": round(
                    min(1.0, max(ratio, semantic_score) + 0.1 * semantic_score), 4
                ),
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
