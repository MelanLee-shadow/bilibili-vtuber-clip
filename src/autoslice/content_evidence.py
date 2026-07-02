from __future__ import annotations

from typing import Sequence

from src.autoslice.review_evidence import ReviewEvidence, SourceCue

_CONNECTIVE_PREFIXES = ("然后", "所以", "但是", "因为", "结果", "接着", "后来", "而且", "不过")
_PAYOFF_MARKERS = ("哈哈", "笑", "结果", "突然", "离谱", "破防", "绷", "小皇帝", "最后")
_CLOSURE_MARKERS = ("结束", "最后", "完了", "就这样", "哈哈", "笑了")
_OPEN_LOOP_MARKERS = ("为什么", "到底", "怎么", "咋", "?", "？", "问题")
_SONG_MARKERS = ("《", "唱", "歌", "歌词", "アイドル", "言って", "あのね")


def analyze_content_evidence(
    *,
    candidate_id: str,
    cues: Sequence[SourceCue],
    title: str,
    foreground_song_overlap_seconds: float | None = None,
    song_duration_seconds: float | None = None,
    song_complete: bool | None = None,
    lyrics_alignment_ready: bool | None = None,
    duplicate_similarity: float = 0.10,
    subtitle_alignment_p95_ms: float = 120.0,
    actual_cut_error_ms: float = 0.0,
) -> ReviewEvidence:
    """Generate deterministic local content/song/dialogue evidence.

    This is intentionally rule-based and conservative: uncertain songs and open
    dialogue loops are represented as explicit evidence gaps instead of optimistic
    release-ready defaults.
    """

    ordered = sorted(cues, key=lambda cue: (cue.source_start_ms, cue.source_end_ms, cue.cue_id))
    all_text = " ".join(cue.text.strip() for cue in ordered)
    evidence_gaps: list[str] = []

    song_overlap = _song_overlap_seconds(ordered) if foreground_song_overlap_seconds is None else foreground_song_overlap_seconds
    if song_complete is None:
        if song_overlap <= 5.0:
            song_complete_value = True
        elif song_duration_seconds:
            song_complete_value = song_overlap >= song_duration_seconds * 0.85
        else:
            song_complete_value = False
    else:
        song_complete_value = song_complete

    if lyrics_alignment_ready is None:
        lyrics_ready = song_overlap <= 5.0
        if song_overlap > 5.0:
            evidence_gaps.append("LYRICS_AUTO_ALIGNMENT_UNIMPLEMENTED")
    else:
        lyrics_ready = lyrics_alignment_ready

    if song_overlap > 5.0 and not song_complete_value:
        evidence_gaps.append("SONG_PARTIAL")

    start_score = 0.97
    first_text = ordered[0].text.strip() if ordered else ""
    if not ordered:
        start_score = 0.0
        evidence_gaps.append("NO_SOURCE_CUES")
    elif first_text.startswith(_CONNECTIVE_PREFIXES):
        start_score = 0.60
        evidence_gaps.append("STARTS_WITH_CONNECTIVE")

    open_loop_count = _open_loop_count(all_text)
    end_score = 0.98
    last_text = ordered[-1].text.strip() if ordered else ""
    if not ordered:
        end_score = 0.0
    elif open_loop_count > 0 and not _has_payoff_or_closure(last_text):
        end_score = 0.60
        evidence_gaps.append("OPEN_LOOP_OR_NO_CLOSURE")
    elif not _has_payoff_or_closure(last_text) and not _has_payoff_or_closure(all_text):
        end_score = 0.70
        evidence_gaps.append("OPEN_LOOP_OR_NO_CLOSURE")

    payoff_score = 0.96 if _has_payoff_or_closure(title + " " + all_text) else 0.55
    if payoff_score < 0.90:
        evidence_gaps.append("PAYOFF_MISSING")

    standalone_score = 0.94 if len(ordered) >= 2 or song_overlap > 5.0 else 0.86
    if standalone_score < 0.90:
        evidence_gaps.append("STANDALONE_CONTEXT_WEAK")

    editorial_score = _editorial_score(
        start_score=start_score,
        end_score=end_score,
        standalone_score=standalone_score,
        payoff_score=payoff_score,
        song_overlap=song_overlap,
        song_complete=song_complete_value,
        lyrics_ready=lyrics_ready,
    )

    return ReviewEvidence(
        candidate_id=candidate_id,
        foreground_song_overlap_seconds=float(song_overlap),
        song_complete=bool(song_complete_value),
        lyrics_alignment_ready=bool(lyrics_ready),
        start_boundary_score=start_score,
        end_boundary_score=end_score,
        standalone_score=standalone_score,
        payoff_score=payoff_score,
        open_loop_count=open_loop_count,
        editorial_score=editorial_score,
        duplicate_similarity=duplicate_similarity,
        subtitle_alignment_p95_ms=subtitle_alignment_p95_ms,
        actual_cut_error_ms=actual_cut_error_ms,
        evidence_gaps=tuple(dict.fromkeys(evidence_gaps)),
        checks=(
            {"code": "RULE_CONTENT_EVIDENCE", "pass": True, "source_cue_count": len(ordered)},
            {"code": "RULE_SONG_OVERLAP", "pass": song_overlap <= 5.0 or song_complete_value},
        ),
        source_cues=tuple(ordered),
        metadata={"title": title, "song_duration_seconds": song_duration_seconds},
    )


def _song_overlap_seconds(cues: Sequence[SourceCue]) -> float:
    return sum(
        max(0, cue.source_end_ms - cue.source_start_ms) / 1000.0
        for cue in cues
        if cue.kind == "singing" or any(marker in cue.text for marker in _SONG_MARKERS)
    )


def _has_payoff_or_closure(text: str) -> bool:
    return any(marker in text for marker in _PAYOFF_MARKERS + _CLOSURE_MARKERS)


def _open_loop_count(text: str) -> int:
    return sum(1 for marker in _OPEN_LOOP_MARKERS if marker in text)


def _editorial_score(
    *,
    start_score: float,
    end_score: float,
    standalone_score: float,
    payoff_score: float,
    song_overlap: float,
    song_complete: bool,
    lyrics_ready: bool,
) -> float:
    score = 72.0
    score += 8.0 * start_score
    score += 8.0 * end_score
    score += 7.0 * standalone_score
    score += 8.0 * payoff_score
    if song_overlap > 5.0 and not song_complete:
        score -= 18.0
    if song_overlap > 5.0 and not lyrics_ready:
        score -= 8.0
    return round(max(0.0, min(100.0, score)), 2)
