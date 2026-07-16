"""LRC-to-ASR alignment and audio-candidate selection."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Mapping, Sequence
import unicodedata

from src.autoslice.llm_client import LlmCall, extract_json_object
from src.autoslice.review_evidence import SourceCue
from src.autoslice.song_common import (
    MAX_AUDIO_LRC_VARIANT_ATTEMPTS,
    SHIFT_TOLERANCE_MS,
    LrcLine,
    LrcResult,
    _LRC_VARIANT_MARKERS,
    canonicalize_audio_lrc_observation,
    lrc_latin_letter_ratio as _lrc_latin_letter_ratio,
    lrc_timed_structure_agreement as _lrc_timed_structure_agreement,
    normalize_lyric_text,
)
from src.autoslice.song_lrc_provider import parse_lrc_text

def validate_audio_lrc_canonical_projection(
    *,
    provider_payload: object,
    canonical_payload: object,
    lrc_path: Path,
) -> dict[str, object]:
    """Recompute and validate one v2 provider-raw -> canonical projection.

    Callers must hash-bind ``lrc_path`` before entering this function.  Runtime
    verifiers cannot trust a self-consistent manifest alone: they must parse
    the bound LRC, restore text/timestamps by exact row index, and require the
    entire canonical payload (including top-level observations) to equal that
    deterministic result.
    """

    try:
        parsed_lines = parse_lrc_text(Path(lrc_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"bound canonical LRC cannot be read: {exc}") from exc
    if not parsed_lines:
        raise ValueError("bound canonical LRC contains no timed lyric rows")
    bound_lrc = LrcResult(
        provider="bound_artifact",
        song_title="",
        artist=None,
        source_ref=str(Path(lrc_path)),
        lines=tuple(parsed_lines),
    )
    expected = canonicalize_audio_lrc_observation(provider_payload, bound_lrc)
    if canonical_payload != expected:
        raise ValueError(
            "canonicalized audio alignment is not the deterministic exact-index projection"
        )
    return expected


def _performance_window(
    cues: Sequence[SourceCue],
    anchor_start_ms: int,
    anchor_end_ms: int,
    *,
    max_gap_ms: int,
) -> list[SourceCue]:
    ordered = sorted(cues, key=lambda cue: (cue.source_start_ms, cue.source_end_ms))
    overlapping = [
        index
        for index, cue in enumerate(ordered)
        if cue.source_end_ms > anchor_start_ms and cue.source_start_ms < anchor_end_ms
    ]
    if not overlapping:
        return []
    start_index = overlapping[0]
    end_index = overlapping[-1]
    # expand outward across small gaps so a song whose head/tail lies just
    # outside the anchor is still considered part of the same performance
    while start_index > 0 and ordered[start_index].source_start_ms - ordered[start_index - 1].source_end_ms <= max_gap_ms:
        start_index -= 1
    while (
        end_index + 1 < len(ordered)
        and ordered[end_index + 1].source_start_ms - ordered[end_index].source_end_ms <= max_gap_ms
    ):
        end_index += 1
    return list(ordered[start_index : end_index + 1])


def _cjk_clean_score(text: str) -> float:
    """Rank a cue for use as a lyric search query.  A search matches on
    lyric text, so the best query is one clean CJK/kana-dense, distinctive line — not
    the longest.  The longest ASR lines are often the English/rap sections BCUT
    mangles ("chewe now baby just chewe now") which match nothing; a clean line
    like "谁说圆满的人生才能算圆满" returns 《屑屑》 as the #1 hit.  Japanese songs
    also need kana-only lines such as ``ただそばにいてほしい`` to remain
    searchable.  Score = CJK/kana density × capped length; lines with fewer
    than six such characters are unusable (-1)."""
    normalized = normalize_lyric_text(text)
    if len(normalized) < 6:
        return -1.0
    cjk = sum(
        1
        for ch in normalized
        if "㐀" <= ch <= "鿿" or "぀" <= ch <= "ヿ" or "ｦ" <= ch <= "ﾟ"
    )
    if cjk < 6:
        return -1.0
    return (cjk / len(normalized)) * min(len(normalized), 16)


def _build_lyric_queries(
    window: Sequence[SourceCue],
    anchor_start_ms: int | None = None,
    anchor_end_ms: int | None = None,
) -> list[str]:
    # netease search matches on lyric text, so issue the cleanest CJK-dense lines
    # as INDIVIDUAL queries — one distinctive line finds the song even when the
    # garbled ASR defeats title guessing (proven on 《屑屑》: the 3-longest lines
    # were all English mondegreens and found nothing; clean lines all hit).  The
    # anchor cues (the targeted song) are scored first.
    anchor_cues: Sequence[SourceCue] = window
    if anchor_start_ms is not None and anchor_end_ms is not None:
        overlapping = [
            cue for cue in window if cue.source_end_ms > anchor_start_ms and cue.source_start_ms < anchor_end_ms
        ]
        if overlapping:
            anchor_cues = overlapping

    queries: list[str] = []
    for cue in sorted(anchor_cues, key=lambda c: _cjk_clean_score(c.text), reverse=True):
        if _cjk_clean_score(cue.text) <= 0:
            break
        line = cue.text.strip().replace("\n", " ")[:40]
        if line:
            queries.append(line)
        if len(queries) >= 6:
            break
    # safety net: the two longest lines from the full window (covers songs whose
    # ASR is clean enough that raw length is a fine proxy, e.g. the old behavior).
    for cue in sorted(window, key=lambda c: len(normalize_lyric_text(c.text)), reverse=True)[:2]:
        line = cue.text.strip().replace("\n", " ")[:40]
        if line:
            queries.append(line)
    return list(dict.fromkeys(q for q in queries if q))


def generate_llm_song_queries(window: Sequence[SourceCue], llm_call: LlmCall, *, max_lines: int = 18) -> list[str]:
    """Ask an LLM to recognize the song(s) behind garbled ASR lyrics."""

    lines = [cue.text.strip().replace("\n", " ") for cue in window if cue.text.strip()]
    if len(lines) > max_lines:
        # sample evenly across the window: it may span several songs plus talk,
        # and only sampling the head previously hid the target song entirely
        step = len(lines) / max_lines
        lines = [lines[int(i * step)] for i in range(max_lines)]
    sample = lines
    prompt = (
        "下面是直播歌回的语音转写歌词，含大量同音错别字，可能横跨多首歌和聊天内容。"
        "请推断其中演唱过哪些歌（中文流行/古风/网络歌曲/粤语歌都可能）。\n"
        "转写歌词：\n" + "\n".join(sample) + "\n\n"
        '只输出一个 JSON 对象：{"guesses": [{"title": "歌名", "artist": "歌手或空字符串"}]}，'
        "最多给 4 个猜测，按置信度排序；完全无法判断就给空数组。"
    )
    payload = extract_json_object(llm_call(prompt))
    guesses = payload.get("guesses")
    queries: list[str] = []
    if isinstance(guesses, list):
        for guess in guesses[:4]:
            if not isinstance(guess, Mapping):
                continue
            title = str(guess.get("title") or "").strip()
            artist = str(guess.get("artist") or "").strip()
            if not title:
                continue
            queries.append(f"{title} {artist}".strip())
            queries.append(title)
    return list(dict.fromkeys(queries))


def _align_lrc_to_cues(
    lrc_lines: Sequence[LrcLine],
    window: Sequence[SourceCue],
    *,
    match_threshold: float,
    target_offset_ms: int | None = None,
    offset_tolerance_ms: int = 6_000,
) -> list[dict[str, object]]:
    """Monotonic DP alignment of LRC lines to ASR cues.

    Independent greedy best-match per line cannot handle repeated choruses:
    every occurrence of an identical lyric line matches the same single cue,
    which then (correctly) fails the global-shift check even for a genuinely
    complete performance.  The DP assigns each line the best-scoring cue such
    that matched cue times never go backwards, so each chorus occurrence lands
    on its own temporally-appropriate cue.

    With ``target_offset_ms`` set, only (line, cue) pairs consistent with that
    global shift are eligible.  This second constrained pass lets a line whose
    text also appears in a false start / abandoned take find its
    shift-consistent occurrence instead of being discarded.
    """

    ordered_cues = sorted(window, key=lambda cue: (cue.source_start_ms, cue.source_end_ms))
    normalized_cues = [(cue, normalize_lyric_text(cue.text)) for cue in ordered_cues]
    line_count = len(lrc_lines)
    cue_count = len(normalized_cues)

    scores: list[list[float]] = [[0.0] * cue_count for _ in range(line_count)]
    for i, line in enumerate(lrc_lines):
        normalized_line = normalize_lyric_text(line.text)
        if not normalized_line:
            continue
        for j, (cue, normalized_cue) in enumerate(normalized_cues):
            if not normalized_cue:
                continue
            ratio = SequenceMatcher(None, normalized_line, normalized_cue).ratio()
            if len(normalized_cue) > len(normalized_line) * 2:
                # long ASR cues may merge several lyric lines; compare against
                # the best local substring alignment, not whole-cue ratio only
                match = SequenceMatcher(None, normalized_line, normalized_cue).find_longest_match(
                    0, len(normalized_line), 0, len(normalized_cue)
                )
                contained = match.size / len(normalized_line) if normalized_line else 0.0
                ratio = max(ratio, contained)
            if ratio >= match_threshold:
                if target_offset_ms is not None:
                    predicted_ms = line.time_ms + target_offset_ms
                    if not (cue.source_start_ms - offset_tolerance_ms <= predicted_ms <= cue.source_end_ms + offset_tolerance_ms):
                        continue
                scores[i][j] = ratio

    # dp[i][j]: best total ratio for LRC lines 0..i using cues with index <= j
    # (non-decreasing across lines; a merged cue may serve consecutive lines).
    dp = [[0.0] * (cue_count + 1) for _ in range(line_count + 1)]
    choice: list[list[int]] = [[0] * (cue_count + 1) for _ in range(line_count + 1)]  # 0=skip line, 1=match, 2=narrow j
    for i in range(1, line_count + 1):
        for j in range(cue_count + 1):
            best = dp[i - 1][j]  # skip this line
            action = 0
            if j > 0 and dp[i][j - 1] > best:
                best = dp[i][j - 1]
                action = 2
            if j > 0 and scores[i - 1][j - 1] > 0.0:
                candidate = dp[i - 1][j] + scores[i - 1][j - 1]
                if candidate > best:
                    best = candidate
                    action = 1
            dp[i][j] = best
            choice[i][j] = action

    matched_cue_index: list[int | None] = [None] * line_count
    i, j = line_count, cue_count
    while i > 0 and j >= 0:
        action = choice[i][j]
        if action == 2:
            j -= 1
        elif action == 1:
            matched_cue_index[i - 1] = j - 1
            i -= 1
        else:
            i -= 1

    alignment: list[dict[str, object]] = []
    for i, line in enumerate(lrc_lines):
        index = matched_cue_index[i]
        cue = ordered_cues[index] if index is not None else None
        alignment.append(
            {
                "lrc_time_ms": line.time_ms,
                "lrc_text": line.text,
                "matched_cue_id": cue.cue_id if cue else None,
                "cue_start_ms": cue.source_start_ms if cue else None,
                "cue_end_ms": cue.source_end_ms if cue else None,
                "match_ratio": round(scores[i][index], 4) if cue is not None and index is not None else 0.0,
            }
        )
    return alignment


def _median_offset(alignment: Sequence[Mapping[str, object]]) -> int | None:
    residuals = sorted(
        int(entry["cue_start_ms"]) - int(entry["lrc_time_ms"])
        for entry in alignment
        if entry["matched_cue_id"] is not None
    )
    if not residuals:
        return None
    return residuals[len(residuals) // 2]


def enforce_global_shift_alignment(
    *,
    alignment: Sequence[Mapping[str, object]],
    shift_tolerance_ms: int = SHIFT_TOLERANCE_MS,
    min_consistent_matches: int = 4,
    span_ratio_min: float = 0.7,
    span_ratio_max: float = 1.35,
    span_check_min_lrc_span_ms: int = 20_000,
) -> tuple[list[dict[str, object]], int, str | None]:
    """Keep only matches explainable by a single global shift (skill core rule).

    ``clip_time = lrc_time + offset`` with one offset for the whole song.  The
    offset is the median residual of the greedy matches; a match survives only
    if its predicted clip time lands inside its cue (± tolerance).  This kills
    the failure class where many LRC lines pile onto one plausible-sounding
    cue (e.g. four "祝你生日快乐" lines all matching the same 4s cue) and a
    28s window gets promoted to a "complete" multi-minute song.

    Returns (filtered_alignment, offset_ms, failure_reason).
    """

    matched_entries = [entry for entry in alignment if entry["matched_cue_id"] is not None]
    if len(matched_entries) < min_consistent_matches:
        return list(dict(entry) for entry in alignment), 0, (
            f"only {len(matched_entries)} greedy match(es); need >= {min_consistent_matches} to fit a global shift"
        )
    residuals = sorted(int(entry["cue_start_ms"]) - int(entry["lrc_time_ms"]) for entry in matched_entries)
    offset_ms = residuals[len(residuals) // 2]

    filtered: list[dict[str, object]] = []
    consistent: list[dict[str, object]] = []
    for entry in alignment:
        record = dict(entry)
        if record["matched_cue_id"] is not None:
            predicted_ms = int(record["lrc_time_ms"]) + offset_ms
            cue_start = int(record["cue_start_ms"])
            cue_end = int(record["cue_end_ms"])
            if cue_start - shift_tolerance_ms <= predicted_ms <= cue_end + shift_tolerance_ms:
                consistent.append(record)
            else:
                record["matched_cue_id"] = None
                record["cue_start_ms"] = None
                record["cue_end_ms"] = None
                record["rejected_reason"] = "GLOBAL_SHIFT_INCONSISTENT"
        filtered.append(record)

    if len(consistent) < min_consistent_matches:
        return filtered, offset_ms, (
            f"only {len(consistent)} of {len(matched_entries)} greedy match(es) fit offset {offset_ms}ms ± {shift_tolerance_ms}ms; "
            "the matches are not one continuous performance of this song"
        )

    previous_start = None
    for record in consistent:
        cue_start = int(record["cue_start_ms"])
        if previous_start is not None and cue_start < previous_start:
            return filtered, offset_ms, (
                "shift-consistent matches map LRC lines to cues in non-monotonic time order; "
                "cannot prove a valid full-song timeline"
            )
        previous_start = cue_start

    lrc_span_ms = int(consistent[-1]["lrc_time_ms"]) - int(consistent[0]["lrc_time_ms"])
    cue_span_ms = int(consistent[-1]["cue_start_ms"]) - int(consistent[0]["cue_start_ms"])
    if lrc_span_ms >= span_check_min_lrc_span_ms:
        span_ratio = cue_span_ms / lrc_span_ms if lrc_span_ms else 0.0
        if not (span_ratio_min <= span_ratio <= span_ratio_max):
            return filtered, offset_ms, (
                f"performance span {cue_span_ms}ms vs LRC span {lrc_span_ms}ms (ratio {span_ratio:.2f}) is not a plausible "
                "single performance of this song; the capture likely covers only a fragment"
            )
    return filtered, offset_ms, None


# ASR-anchor global shift (2026-07-11 fix): the AGY audio-observation median
# residual can carry a uniform onset-lateness bias (verified: 《恋爱告急》 AGY
# was +860ms late on every line vs the same source's BCUT ASR).  A fresh ASR
# transcript of the SAME source media is an independent, non-AGY timeline —
# the talk lane already burns straight from it — so it anchors the final
# shift when it agrees closely enough with AGY and matches enough lines.
ASR_ANCHOR_MATCH_THRESHOLD = 0.55
ASR_ANCHOR_MIN_MATCH_RATIO = 0.20
ASR_ANCHOR_MIN_MATCHES = 5
ASR_ANCHOR_MAX_RESIDUAL_IQR_MS = 900.0
ASR_ANCHOR_AGY_AGREEMENT_TOLERANCE_MS = 1_500

_SRT_TIMESTAMP_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def _residual_iqr_ms(residuals: Sequence[int]) -> float:
    """Interquartile spread of a residual sample (linear-interpolation percentile).

    Matching residual counts here are small (tens of lines at most), so a
    tiny dependency-free percentile helper is sufficient.
    """

    values = sorted(residuals)
    n = len(values)
    if n < 2:
        return 0.0

    def _percentile(p: float) -> float:
        idx = p * (n - 1)
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return values[lo] + (values[hi] - values[lo]) * frac

    return _percentile(0.75) - _percentile(0.25)


def match_lrc_to_fresh_asr_anchor(
    lrc_lines: Sequence[LrcLine],
    asr_cues: Sequence[SourceCue],
    *,
    match_threshold: float = ASR_ANCHOR_MATCH_THRESHOLD,
    min_match_ratio: float = ASR_ANCHOR_MIN_MATCH_RATIO,
    min_matches: int = ASR_ANCHOR_MIN_MATCHES,
    max_residual_iqr_ms: float = ASR_ANCHOR_MAX_RESIDUAL_IQR_MS,
) -> tuple[int | None, int, str | None]:
    """Anchor the LRC global shift on a fresh (non-AGY) ASR transcript.

    Reuses the existing monotonic DP matcher (``_align_lrc_to_cues``) so ASR
    cues are consumed at most once and matched order stays monotonic, exactly
    like the ordinary LRC-to-ASR alignment path — this is not a separate
    greedy per-line matcher.

    Returns ``(asr_offset_ms, matched_line_count, rejection_reason)``.  The
    offset is ``None`` (with a reason) whenever there are too few matches or
    the matched residuals disagree too much to trust a single fresh-ASR
    shift; callers must then keep the AGY-derived offset rather than force
    this one.
    """

    if not lrc_lines or not asr_cues:
        return None, 0, "no fresh ASR cues available for anchor matching"
    alignment = _align_lrc_to_cues(lrc_lines, asr_cues, match_threshold=match_threshold)
    residuals = [
        int(entry["cue_start_ms"]) - int(entry["lrc_time_ms"])
        for entry in alignment
        if entry["matched_cue_id"] is not None
    ]
    matched_line_count = len(residuals)
    required = max(min_matches, math.ceil(min_match_ratio * len(lrc_lines)))
    if matched_line_count < required:
        return None, matched_line_count, (
            f"only {matched_line_count} fresh-ASR line match(es); need >= {required}"
        )
    spread_ms = _residual_iqr_ms(residuals)
    if spread_ms > max_residual_iqr_ms:
        return None, matched_line_count, (
            f"fresh-ASR residual IQR {spread_ms:.0f}ms exceeds {max_residual_iqr_ms:.0f}ms"
        )
    offset_ms = sorted(residuals)[matched_line_count // 2]
    return offset_ms, matched_line_count, None


def _parse_srt_timestamp_ms(hours: str, minutes: str, seconds: str, millis: str) -> int:
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis.ljust(3, "0")[:3])


def _parse_asr_anchor_srt(path: Path) -> list[SourceCue]:
    """Minimal standalone SRT reader for the fresh-ASR anchor fallback lookup.

    Deliberately self-contained (``src`` must not import from ``scripts``) and
    only extracts cue timing/text; ``match_lrc_to_fresh_asr_anchor`` already
    normalizes both sides before fuzzy comparison, so this does not replicate
    the shadow pipeline's term-lexicon text normalization of the same file.
    """

    try:
        raw = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except OSError:
        return []
    cues: list[SourceCue] = []
    for index, block in enumerate(raw.split("\n\n"), start=1):
        lines = [line for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        timing_line = next((line for line in lines[:2] if "-->" in line), None)
        if timing_line is None:
            continue
        match = _SRT_TIMESTAMP_RE.search(timing_line)
        if match is None:
            continue
        text = "\n".join(line for line in lines if line is not timing_line and "-->" not in line).strip()
        cues.append(
            SourceCue(
                cue_id=f"asr_anchor_{index:06d}",
                source_start_ms=_parse_srt_timestamp_ms(*match.group(1, 2, 3, 4)),
                source_end_ms=_parse_srt_timestamp_ms(*match.group(5, 6, 7, 8)),
                text=text,
                kind="speech",
            )
        )
    return cues


def _sibling_asr_anchor_srt_path(source_media_path: Path) -> Path | None:
    """Best-effort sibling lookup for the fresh full-source ASR SRT.

    The song selector lane writes ``<stem>_full_source.srt`` or
    ``<stem>_source.srt`` next to the source media it analyzed.  Used only
    when a caller has not already threaded parsed cues or an explicit path —
    production callers should prefer passing already-parsed cues.
    """

    stem = source_media_path.stem
    for suffix in ("_full_source.srt", "_source.srt", ".bcut.srt", ".srt"):
        candidate = source_media_path.with_name(f"{stem}{suffix}")
        if candidate.is_file():
            return candidate
    return None


def _lrc_fingerprint(lrc: LrcResult) -> str:
    payload = [
        (line.time_ms, normalize_lyric_text(line.text))
        for line in lrc.lines
        if normalize_lyric_text(line.text)
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _lrc_identity_key(lrc: LrcResult) -> str:
    """Stable song identity across provider-specific timing/text variants."""

    title = normalize_lyric_text(lrc.song_title)
    artist = normalize_lyric_text(lrc.artist or "")
    if title:
        return f"title:{title}|artist:{artist}"
    return f"lyrics:{_lrc_fingerprint(lrc)}"


def _lrc_content_similarity(left: LrcResult, right: LrcResult) -> float:
    """Lyrics-only equivalence signal across cover/provider timing variants.

    Exact timed fingerprints are too strict: the same song is commonly split
    into 23/35/57-line LRCs, includes a repeated chorus in one provider, or has
    a few credit/ad-lib differences.  Compare both full normalized streams and
    exact normalized-line containment; neither uses provider search rank.
    """

    left_lines = [normalize_lyric_text(line.text) for line in left.lines]
    right_lines = [normalize_lyric_text(line.text) for line in right.lines]
    left_lines = [line for line in left_lines if line]
    right_lines = [line for line in right_lines if line]
    if not left_lines or not right_lines:
        return 0.0
    stream_ratio = SequenceMatcher(None, "".join(left_lines), "".join(right_lines)).ratio()
    left_set, right_set = set(left_lines), set(right_lines)
    line_f1 = 2 * len(left_set & right_set) / max(1, len(left_set) + len(right_set))
    return max(stream_ratio, line_f1)


def _lrc_title_family(lrc: LrcResult) -> str:
    """Provider display-title noise stripped for same-song clustering."""

    title = str(lrc.song_title or "")
    title = re.split(r"\s+-\s+|[（(【\[]", title, maxsplit=1)[0]
    title = re.sub(r"(?i)\b(?:cover|live|ver(?:sion)?)\b.*$", "", title)
    return normalize_lyric_text(title)




def _matched_cue_ids(alignment: Sequence[Mapping[str, object]]) -> set[str]:
    return {
        str(row["matched_cue_id"])
        for row in alignment
        if isinstance(row, Mapping) and row.get("matched_cue_id") is not None
    }


def _matched_cue_overlap(
    left: Sequence[Mapping[str, object]],
    right: Sequence[Mapping[str, object]],
) -> tuple[float, float, int]:
    """Return containment, bilateral coverage, and shared ASR-cue count.

    Same-song LRCs routinely split one lyric into different numbers of rows.
    Containment handles line splitting, while coverage against the larger set
    prevents a short unrelated candidate from merging after matching only
    three cherry-picked cues.
    """

    left_ids, right_ids = _matched_cue_ids(left), _matched_cue_ids(right)
    if not left_ids or not right_ids:
        return 0.0, 0.0, 0
    shared = len(left_ids & right_ids)
    return (
        shared / min(len(left_ids), len(right_ids)),
        shared / max(len(left_ids), len(right_ids)),
        shared,
    )


def _preferred_title_keys(preferred_title_hints: Sequence[str]) -> set[str]:
    """Normalize explicit title/search hints without stripping variant labels."""

    return {
        normalized
        for hint in preferred_title_hints
        if isinstance(hint, str) and (normalized := normalize_lyric_text(hint))
    }


def _lrc_title_is_exact_hint(lrc: LrcResult, preferred_title_hints: Sequence[str]) -> bool:
    return normalize_lyric_text(lrc.song_title) in _preferred_title_keys(preferred_title_hints)


def _lrc_variant_penalty(lrc: LrcResult) -> int:
    """Count catalog decorations that usually denote a non-canonical timeline."""

    return len(_LRC_VARIANT_MARKERS.findall(unicodedata.normalize("NFKC", str(lrc.song_title or ""))))


def _lrc_span_ms(lrc: LrcResult) -> int:
    if len(lrc.lines) < 2:
        return 0
    return max(0, int(lrc.lines[-1].time_ms) - int(lrc.lines[0].time_ms))


def _audio_lrc_entry_sort_key(
    entry: tuple[float, LrcResult, list[dict[str, object]]],
    *,
    preferred_title_hints: Sequence[str],
    max_line_count: int,
    max_span_ms: int,
) -> tuple[object, ...]:
    """Evidence-mass-first ordering for expensive audio verification.

    A short remix can match a larger *ratio* simply because its denominator is
    tiny.  Require a sufficiently complete timeline tier first, then use an
    exact explicit title and canonical/non-remix display name when available.
    Absolute distinct cue support and line/span completeness outrank the ratio.
    """

    ratio, lrc, alignment = entry
    line_count = len(lrc.lines)
    span_ms = _lrc_span_ms(lrc)
    enough_lines = line_count >= max(8, (max_line_count * 3 + 4) // 5)
    enough_span = max_span_ms <= 0 or span_ms >= (max_span_ms * 3) // 5
    sufficiently_complete = enough_lines and enough_span
    exact_hint = _lrc_title_is_exact_hint(lrc, preferred_title_hints)
    variant_penalty = _lrc_variant_penalty(lrc)
    matched_cues = len(_matched_cue_ids(alignment))
    matched_rows = sum(
        1 for row in alignment if isinstance(row, Mapping) and row.get("matched_cue_id") is not None
    )
    provider_penalty = 0 if lrc.provider == "lrclib" else 1
    stable_tail = (provider_penalty, lrc.source_ref)
    if _preferred_title_keys(preferred_title_hints):
        return (
            -int(sufficiently_complete),
            -int(exact_hint),
            variant_penalty,
            -matched_cues,
            -matched_rows,
            -line_count,
            -span_ms,
            -float(ratio),
            *stable_tail,
        )
    return (
        -int(sufficiently_complete),
        -matched_cues,
        -matched_rows,
        variant_penalty,
        -line_count,
        -span_ms,
        -float(ratio),
        *stable_tail,
    )


def _audio_lrc_retry_candidates(
    ranked: Sequence[tuple[float, LrcResult, list[dict[str, object]]]],
    *,
    primary: LrcResult,
    preferred_title_hints: Sequence[str],
    max_attempts: int,
) -> list[LrcResult]:
    """Return a deduped, bounded list of independently timed same-song variants."""

    hard_cap = min(MAX_AUDIO_LRC_VARIANT_ATTEMPTS, max(1, int(max_attempts)))
    primary_family = _lrc_title_family(primary)
    preferred_families = {
        normalize_lyric_text(hint)
        for hint in preferred_title_hints
        if isinstance(hint, str) and normalize_lyric_text(hint)
    }
    primary_entry = next(
        (entry for entry in ranked if entry[1].source_ref == primary.source_ref),
        None,
    )
    if primary_entry is None:
        primary_entry = (0.0, primary, [])
    eligible: list[tuple[float, LrcResult, list[dict[str, object]]]] = [primary_entry]
    for entry in ranked:
        ratio, item, alignment = entry
        if item.source_ref == primary.source_ref or len(item.lines) < 8 or ratio < 0.08:
            continue
        item_family = _lrc_title_family(item)
        same_preferred_family = bool(
            primary_family and primary_family in preferred_families and item_family == primary_family
        )
        cue_overlap, cue_coverage, shared_cues = _matched_cue_overlap(
            primary_entry[2], alignment
        )
        independently_same_song = (
            _lrc_identity_key(item) == _lrc_identity_key(primary)
            or _lrc_fingerprint(item) == _lrc_fingerprint(primary)
            or _lrc_content_similarity(item, primary) >= 0.62
            or (
                item_family == primary_family
                and shared_cues >= 5
                and cue_overlap >= 0.80
                and cue_coverage >= 0.25
            )
        )
        if same_preferred_family or independently_same_song:
            eligible.append(entry)

    max_lines = max((len(entry[1].lines) for entry in eligible), default=len(primary.lines))
    max_span = max((_lrc_span_ms(entry[1]) for entry in eligible), default=_lrc_span_ms(primary))
    ordered = [
        primary_entry,
        *sorted(
            eligible[1:],
            key=lambda entry: _audio_lrc_entry_sort_key(
                entry,
                preferred_title_hints=preferred_title_hints,
                max_line_count=max_lines,
                max_span_ms=max_span,
            ),
        ),
    ]
    deduped: list[LrcResult] = []
    seen_fingerprints: set[str] = set()
    for _ratio, item, _alignment in ordered:
        fingerprint = _lrc_fingerprint(item)
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)
        deduped.append(primary if item.source_ref == primary.source_ref else item)
        if len(deduped) >= hard_cap:
            break
    return deduped


def _audio_lrc_infrastructure_reason(exc: Exception) -> str | None:
    """Keep provider outages recoverable instead of spending variant budget."""

    detail = f"{type(exc).__name__}: {exc}".casefold()
    if "agy_and_gemini_api_failed" in detail:
        return "AGY_AND_GEMINI_API_FAILED"
    if "agy_quota_exhausted" in detail or "quota" in detail or "429" in detail or "rate limit" in detail:
        return "AGY_QUOTA_EXHAUSTED"
    if "timeout" in detail or "timed out" in detail:
        return "AGY_TIMEOUT"
    if "agy_empty_output" in detail or "without alignment.json" in detail or "empty output" in detail:
        return "AGY_EMPTY_OUTPUT"
    if "agy_failed_rc" in detail or "failed rc=" in detail:
        return "AGY_FAILED_RC"
    return None


@dataclass(frozen=True)
class AudioLrcGroups:
    entries: list[tuple[float, LrcResult, list[dict[str, object]]]]
    equivalent: list[list[bool]]
    grouped: list[tuple[float, bool, str, list[tuple[float, LrcResult, list[dict[str, object]]]]]]
    preferred_group: list[tuple[float, LrcResult, list[dict[str, object]]]] | None


def _group_audio_lrc_candidates(
    ranked: Sequence[tuple[float, LrcResult, list[dict[str, object]]]],
    *,
    pinned_lrc_results: Sequence[LrcResult],
    preferred_title_hints: Sequence[str],
    min_recall_ratio: float,
) -> AudioLrcGroups:
    entries = list(ranked)
    identities = [_lrc_identity_key(lrc) for _ratio, lrc, _alignment in entries]
    fingerprints = [_lrc_fingerprint(lrc) for _ratio, lrc, _alignment in entries]
    equivalent: list[list[bool]] = [
        [left == right for right in range(len(entries))]
        for left in range(len(entries))
    ]
    for left in range(len(entries)):
        for right in range(left + 1, len(entries)):
            # Provider records are the same song when either title+artist or
            # the complete canonical timed lyric agrees. Fuzzy evidence below
            # is deliberately kept pairwise; clustering enforces complete-link.
            same_title = bool(
                _lrc_title_family(entries[left][1])
                and _lrc_title_family(entries[left][1]) == _lrc_title_family(entries[right][1])
            )
            content_similarity = _lrc_content_similarity(entries[left][1], entries[right][1])
            cue_overlap, cue_coverage, shared_cues = _matched_cue_overlap(
                entries[left][2], entries[right][2]
            )
            same_family = (
                identities[left] == identities[right]
                or fingerprints[left] == fingerprints[right]
                # Same normalized title plus either textual agreement or at
                # least three high-overlap source cues handles provider line
                # splitting without merging same-title homonyms on title alone.
                or (
                    same_title
                    and (
                        content_similarity >= 0.62
                        or (
                            shared_cues >= 5
                            and cue_overlap >= 0.80
                            and cue_coverage >= 0.60
                        )
                    )
                )
                # Close aliases such as 园游会/游园会 require both lyric-text
                # agreement and near-containment of actually matched ASR cues.
                or (
                    content_similarity >= 0.70
                    and shared_cues >= 5
                    and cue_overlap >= 0.90
                    and cue_coverage >= 0.30
                )
            )
            equivalent[left][right] = same_family
            equivalent[right][left] = same_family

    # A partial lyric can be similar to two unrelated full songs, so pairwise
    # similarity is not transitive. Complete-link clusters require direct
    # evidence between every pair and cannot bridge A--B--C when A !~ C.
    group_indices: list[list[int]] = []
    for index in sorted(
        range(len(entries)),
        key=lambda item: (-entries[item][0], entries[item][1].source_ref),
    ):
        compatible = [
            group_index
            for group_index, members in enumerate(group_indices)
            if all(equivalent[index][member] for member in members)
        ]
        if compatible:
            group_indices[compatible[0]].append(index)
        else:
            group_indices.append([index])
    groups = [[entries[index] for index in members] for members in group_indices]
    if not groups:
        raise ValueError("no canonical LRC identity is available for audio alignment")
    exact_title_groups = [
        group
        for group in groups
        if max(item[0] for item in group) >= min_recall_ratio
        and any(_lrc_title_is_exact_hint(item[1], preferred_title_hints) for item in group)
    ]
    if len(exact_title_groups) > 1:
        raise ValueError(
            "ambiguous exact-title LRC identity: multiple disconnected songs match the explicit title hint"
        )
    preferred_group = exact_title_groups[0] if exact_title_groups else None
    pinned_identities = {_lrc_identity_key(item) for item in pinned_lrc_results}
    pinned_fingerprints = {_lrc_fingerprint(item) for item in pinned_lrc_results}
    grouped = [
        (
            max(item[0] for item in group_entries),
            any(
                _lrc_identity_key(item) in pinned_identities or _lrc_fingerprint(item) in pinned_fingerprints
                for _ratio, item, _alignment in group_entries
            ),
            min(item.source_ref for _ratio, item, _alignment in group_entries),
            group_entries,
        )
        for group_entries in groups
    ]
    # Recall remains the primary authority.  For an *exact* top-recall tie,
    # prefer the one identity already pinned by >=2 known-song fingerprint
    # lines.  Previously the disjoint-set root index broke ties, so the real
    # 《屑屑》 pin could lose arbitrarily to a Studio Live/provider variant with
    # the same 41/52 ASR recall and the audio verifier was never reached.
    grouped.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return AudioLrcGroups(
        entries=entries,
        equivalent=equivalent,
        grouped=grouped,
        preferred_group=preferred_group,
    )

def _choose_audio_lrc_candidate(
    ranked: Sequence[tuple[float, LrcResult, list[dict[str, object]]]],
    *,
    pinned_lrc_results: Sequence[LrcResult],
    min_recall_ratio: float,
    min_margin: float,
    preferred_title_hints: Sequence[str] = (),
) -> LrcResult:
    """Choose one deterministic lyric identity before an expensive audio pass.

    Provider records with the same normalized title+artist are one identity
    even when their synced timestamps differ slightly.  A weak or tied signal
    between *different songs* is intentionally not enough: feeding an
    arbitrary LRC to a multimodal model invites it to force the supplied lyrics
    onto unrelated audio.
    """

    groups = _group_audio_lrc_candidates(
        ranked,
        pinned_lrc_results=pinned_lrc_results,
        preferred_title_hints=preferred_title_hints,
        min_recall_ratio=min_recall_ratio,
    )
    entries = groups.entries
    equivalent = groups.equivalent
    grouped = groups.grouped
    preferred_group = groups.preferred_group
    preferred_top = preferred_group is not None
    variant_family_top = False
    variant_family: str | None = None
    if preferred_group is not None:
        preferred_row = next(item for item in grouped if item[3] is preferred_group)
        grouped.remove(preferred_row)
        grouped.insert(0, preferred_row)
    else:
        # A truncated Remix/Live/Piano row can form its own strict clique and
        # win on ratio alone.  If the natural top belongs to a decorated title
        # family, compare every clique in that same base-title family using
        # evidence mass/fullness; this does not merge unrelated plain-title
        # homonyms and other song families still participate in the margin
        # gate below.
        natural_top_entries = grouped[0][3]
        natural_top_entry = max(natural_top_entries, key=lambda item: item[0])
        natural_family = _lrc_title_family(natural_top_entry[1])
        family_rows = [
            entry
            for _ratio, _pinned, _key, group_entries in grouped
            for entry in group_entries
            if natural_family and _lrc_title_family(entry[1]) == natural_family
        ]
        if len(family_rows) > 1 and any(_lrc_variant_penalty(entry[1]) for entry in family_rows):
            family_max_lines = max(len(entry[1].lines) for entry in family_rows)
            family_max_span = max(_lrc_span_ms(entry[1]) for entry in family_rows)
            family_grouped = [
                row
                for row in grouped
                if any(
                    _lrc_title_family(entry[1]) == natural_family
                    for entry in row[3]
                )
            ]
            preferred_family_row = min(
                family_grouped,
                key=lambda row: min(
                    _audio_lrc_entry_sort_key(
                        entry,
                        preferred_title_hints=(),
                        max_line_count=family_max_lines,
                        max_span_ms=family_max_span,
                    )
                    for entry in row[3]
                    if _lrc_title_family(entry[1]) == natural_family
                ),
            )
            grouped.remove(preferred_family_row)
            grouped.insert(0, preferred_family_row)
            variant_family_top = True
            variant_family = natural_family
    top_ratio, is_curated, _top_key, top_entries = grouped[0]
    top_indices_for_margin = {
        index
        for index, entry in enumerate(entries)
        if entry[0] == top_ratio and any(entry is top for top in top_entries)
    }
    competing_groups = []
    for group in grouped[1:]:
        group_ratio, _pinned, _key, group_entries = group
        if variant_family_top and any(
            _lrc_title_family(entry[1]) == variant_family
            for entry in group_entries
        ):
            continue
        # Equal-best disconnected cliques remain a hard ambiguity. A lower-
        # recall provider variant that directly matches any member of the top
        # core is the same song for margin purposes, even if complete-link
        # correctly kept it outside the canonical selection clique.
        if group_ratio < top_ratio:
            max_row_indices = {
                index
                for index, entry in enumerate(entries)
                if entry[0] == group_ratio and any(entry is row for row in group_entries)
            }
            if max_row_indices and all(
                any(
                    equivalent[row_index][top_index]
                    for top_index in top_indices_for_margin
                )
                for row_index in max_row_indices
            ):
                continue
        competing_groups.append(group)
    second_ratio = competing_groups[0][0] if competing_groups else 0.0
    curated_top_ties = [
        group for group in grouped if group[0] == top_ratio and group[1]
    ]
    if len(curated_top_ties) > 1 and not preferred_top:
        tie_entries = [
            entry
            for _ratio, _pinned, _key, group_entries in curated_top_ties
            for entry in group_entries
        ]
        tie_families = {_lrc_title_family(entry[1]) for entry in tie_entries}
        one_timeline = (
            len(tie_families) == 1
            and "" not in tie_families
            and all(
                _lrc_timed_structure_agreement(tie_entries[0][1], entry[1]) >= 0.80
                for entry in tie_entries[1:]
            )
        )
        if not one_timeline:
            raise ValueError(
                "ambiguous curated LRC identity: multiple pinned songs share the best ASR recall; "
                "refusing to choose one before audio verification"
            )
        # One song, several curated script/arrangement rows (2026-07-16
        # 怪獣の花唄: native-JP and romaji lrclib rows share one synced timeline
        # and tie at 0% recall because the Chinese ASR anchor is blind to
        # Japanese).  Same title family + same timeline is ONE identity, not an
        # ambiguity.  Prefer the native-script (lowest latin-letter ratio),
        # fullest variant as primary; the bounded audio proof still arbitrates.
        primary_entry = min(
            tie_entries,
            key=lambda entry: (
                round(_lrc_latin_letter_ratio(entry[1]), 2),
                -len(entry[1].lines),
                entry[1].source_ref,
            ),
        )
        primary_group = next(
            group
            for group in curated_top_ties
            if any(entry is primary_entry for entry in group[3])
        )
        grouped.remove(primary_group)
        grouped.insert(0, primary_group)
        top_ratio, is_curated, _top_key, top_entries = grouped[0]
    if is_curated:
        if top_ratio < 0.08:
            raise ValueError(
                f"curated LRC identity has only {top_ratio:.0%} ASR recall; refusing to force it onto audio"
            )
    elif top_ratio < min_recall_ratio or (
        not preferred_top and top_ratio - second_ratio < min_margin
    ):
        raise ValueError(
            "ambiguous low-ASR LRC identity: "
            f"best={top_ratio:.0%}, runner-up={second_ratio:.0%}, "
            f"need best>={min_recall_ratio:.0%} and margin>={min_margin:.0%}"
        )
    # Always retain the strongest acoustic-discovery member of a fuzzy lyric
    # family.  LRCLIB is only a deterministic tie-break; preferring a weaker
    # truncated LRCLIB subset can move the canonical song boundary.
    max_line_count = max(len(item[1].lines) for item in top_entries)
    max_span_ms = max(_lrc_span_ms(item[1]) for item in top_entries)
    selected_lrc = sorted(
        top_entries,
        key=lambda item: _audio_lrc_entry_sort_key(
            item,
            preferred_title_hints=preferred_title_hints,
            max_line_count=max_line_count,
            max_span_ms=max_span_ms,
        ),
    )[0][1]

    # Catalog aliases can carry the right synced lyrics under a misleading
    # display title (real July 10 example: 园游会 lyrics under
    # ``Owen-只想为你撑伞``). Preserve the selected lyric bytes/source, but use
    # the title family corroborated by the most independent providers. Within
    # that family the shortest display form drops Cover/Live decorations.
    family_rows: dict[str, list[LrcResult]] = {}
    top_indices = {
        index
        for index, entry in enumerate(entries)
        if any(entry is top_entry for top_entry in top_entries)
    }
    for _ratio, item, _alignment in top_entries:
        family = _lrc_title_family(item)
        if family:
            family_rows.setdefault(family, []).append(item)
    # Complete-link is intentionally strict for identity choice, but a lower-
    # recall provider can still corroborate a title already present in the top
    # clique. Admit only same-family rows directly equivalent to at least one
    # top member of that family; this strengthens naming without reopening an
    # A--B--C identity bridge or introducing a new title family.
    for index, (_ratio, item, _alignment) in enumerate(entries):
        if index in top_indices:
            continue
        family = _lrc_title_family(item)
        if family not in family_rows:
            continue
        if any(
            equivalent[index][top_index]
            and _lrc_title_family(entries[top_index][1]) == family
            for top_index in top_indices
        ):
            family_rows[family].append(item)
    if family_rows:
        selected_family = _lrc_title_family(selected_lrc)
        provider_votes = {
            family: len({item.provider for item in rows})
            for family, rows in family_rows.items()
        }
        winning_votes = max(provider_votes.values())
        winners = [
            family for family, votes in provider_votes.items() if votes == winning_votes
        ]
        selected_votes = provider_votes.get(selected_family, 0)
        # Each provider family gets one vote. Rewrite only for a unique winner
        # with >=2 independent providers and strictly more evidence than the
        # acoustically selected title; a 2-vs-2 tie preserves the source name.
        if len(winners) == 1 and winning_votes >= 2 and winning_votes > selected_votes:
            corroborated = family_rows[winners[0]]
            canonical_title = min(
                (
                    str(item.song_title).strip()
                    for item in corroborated
                    if str(item.song_title).strip()
                ),
                key=lambda title: (len(title), title),
                default=str(selected_lrc.song_title),
            )
            if canonical_title != selected_lrc.song_title:
                selected_lrc = LrcResult(
                    provider=selected_lrc.provider,
                    song_title=canonical_title,
                    artist=selected_lrc.artist,
                    source_ref=selected_lrc.source_ref,
                    lines=selected_lrc.lines,
                )
    return selected_lrc
