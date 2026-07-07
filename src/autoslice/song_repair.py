"""Repair-first song completeness: try to *prove* a full song before blocking.

Goal steering (Ivan, 2026-07-02): the pipeline must spend a repair budget before
any BLOCK.  For song candidates the missing evidence is almost always the
lyrics-alignment proof demanded by ``_verify_lyrics_alignment_proof`` in the
shadow pipeline.  This module attempts to *earn* that proof honestly:

1. discover an external LRC for the performed song (pluggable provider,
   e.g. the NetEase public lyric API);
2. fuzzy-align the LRC lines against the ASR cues of the performance;
3. check the alignment actually covers the whole song (head and tail);
4. on success, write an alignment report artifact and return
   ``song_boundary``/``lyrics_alignment`` payloads that pass the existing
   hash-bound proof gate — no score is raised on trust.

Every attempt (including failures) is recorded in a repair report so a final
BLOCK can say what was tried instead of silently giving up.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Mapping, Sequence

from src.autoslice.llm_client import LlmCall, extract_json_object
from src.autoslice.review_evidence import SourceCue

SONG_REPAIR_SCHEMA_VERSION = "song-repair-report.v1"

SHIFT_TOLERANCE_MS = 6_000

_NON_LYRIC_CHARS = re.compile(r"[\s，。！？、,.!?…~〜\-—:：;；\"'“”‘’()（）\[\]【】]")


@dataclass(frozen=True)
class LrcLine:
    time_ms: int
    text: str


@dataclass(frozen=True)
class LrcResult:
    provider: str
    song_title: str
    artist: str | None
    source_ref: str
    lines: tuple[LrcLine, ...]


LrcProvider = Callable[[str], "LrcResult | Sequence[LrcResult] | None"]


@dataclass(frozen=True)
class SongRepairAttempt:
    step: str
    status: str  # SUCCESS | FAILED | SKIPPED
    detail: str

    def to_manifest(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SongRepairResult:
    repaired: bool
    attempts: tuple[SongRepairAttempt, ...]
    song_boundary: Mapping[str, object] | None
    lyrics_alignment: Mapping[str, object] | None
    report_path: str | None

    def to_manifest(self) -> dict[str, object]:
        return {
            "schema_version": SONG_REPAIR_SCHEMA_VERSION,
            "repaired": self.repaired,
            "attempts": [attempt.to_manifest() for attempt in self.attempts],
            "song_boundary": dict(self.song_boundary) if self.song_boundary else None,
            "lyrics_alignment": dict(self.lyrics_alignment) if self.lyrics_alignment else None,
            "report_path": self.report_path,
        }


def normalize_lyric_text(text: str) -> str:
    return _NON_LYRIC_CHARS.sub("", text).lower()


def attempt_song_repair(
    *,
    candidate_id: str,
    cues: Sequence[SourceCue],
    anchor_start_ms: int,
    anchor_end_ms: int,
    source_duration_ms: int,
    output_dir: Path,
    lrc_provider: LrcProvider | None = None,
    provider_name: str | None = None,
    hint_llm_call: LlmCall | None = None,
    extra_queries: Sequence[str] = (),
    max_queries: int = 10,
    max_lrc_candidates: int = 8,
    min_matched_ratio: float = 0.55,
    match_threshold: float = 0.45,
    max_window_gap_ms: int = 30_000,
    pre_roll_ms: int = 1_500,
    post_roll_ms: int = 4_000,
) -> SongRepairResult:
    """Try to repair a song candidate into a fully-proven full-song boundary."""

    attempts: list[SongRepairAttempt] = []
    output_dir.mkdir(parents=True, exist_ok=True)

    window = _performance_window(cues, anchor_start_ms, anchor_end_ms, max_gap_ms=max_window_gap_ms)
    if not window:
        attempts.append(SongRepairAttempt("performance_window", "FAILED", "no cues overlap the anchor window"))
        return _finish(candidate_id, attempts, output_dir)
    attempts.append(
        SongRepairAttempt(
            "performance_window",
            "SUCCESS",
            f"{len(window)} cues {window[0].source_start_ms}..{window[-1].source_end_ms}ms",
        )
    )

    if lrc_provider is None:
        attempts.append(
            SongRepairAttempt(
                "lrc_discovery",
                "SKIPPED",
                "no LRC provider configured (pass lrc_provider / --lrc-provider to enable API repair)",
            )
        )
        return _finish(candidate_id, attempts, output_dir)

    queries: list[str] = [q.strip() for q in extra_queries if isinstance(q, str) and q.strip()]
    if hint_llm_call is not None:
        # Semantic repair: garbled ASR defeats text search, but an LLM can often
        # still recognize the song from misheard lyrics and give clean queries.
        try:
            hints = generate_llm_song_queries(window, hint_llm_call)
        except Exception as exc:
            hints = []
            attempts.append(SongRepairAttempt("llm_song_hint", "FAILED", f"{type(exc).__name__}: {exc}"))
        else:
            if hints:
                attempts.append(SongRepairAttempt("llm_song_hint", "SUCCESS", f"guessed queries: {hints}"))
            else:
                attempts.append(SongRepairAttempt("llm_song_hint", "FAILED", "LLM returned no usable song guesses"))
        queries.extend(hints)
    queries.extend(_build_lyric_queries(window, anchor_start_ms, anchor_end_ms))
    deduped_queries = list(dict.fromkeys(q for q in queries if q))[:max_queries]

    candidates: list[LrcResult] = []
    seen_refs: set[str] = set()
    provider_errors: list[str] = []
    for query in deduped_queries:
        if len(candidates) >= max_lrc_candidates:
            break
        try:
            found = lrc_provider(query)
        except Exception as exc:  # provider failures must not crash review; they are recorded
            provider_errors.append(f"{query!r}: {type(exc).__name__}: {exc}")
            continue
        found_list: list[LrcResult]
        if found is None:
            found_list = []
        elif isinstance(found, LrcResult):
            found_list = [found]
        else:
            found_list = [item for item in found if isinstance(item, LrcResult)]
        for item in found_list:
            if item.lines and item.source_ref not in seen_refs:
                seen_refs.add(item.source_ref)
                candidates.append(item)
    if not candidates:
        detail = f"no LRC found for {len(deduped_queries)} queries {deduped_queries!r}"
        if provider_errors:
            detail += f"; provider errors: {'; '.join(provider_errors[:3])}"
        attempts.append(SongRepairAttempt("lrc_discovery", "FAILED", detail))
        return _finish(candidate_id, attempts, output_dir)
    attempts.append(
        SongRepairAttempt(
            "lrc_discovery",
            "SUCCESS",
            f"{len(candidates)} LRC candidate(s) from {len(deduped_queries)} queries",
        )
    )

    # Rank candidates by how well their lyrics actually align to this
    # performance — search ranking is not evidence, alignment is.
    ranked: list[tuple[float, LrcResult, list[dict[str, object]]]] = []
    for candidate_lrc in candidates:
        candidate_alignment = _align_lrc_to_cues(candidate_lrc.lines, window, match_threshold=match_threshold)
        candidate_matched = [entry for entry in candidate_alignment if entry["matched_cue_id"] is not None]
        ratio = len(candidate_matched) / len(candidate_alignment) if candidate_alignment else 0.0
        ranked.append((ratio, candidate_lrc, candidate_alignment))
        attempts.append(
            SongRepairAttempt(
                "candidate_alignment",
                "SUCCESS" if ratio >= min_matched_ratio else "FAILED",
                f"{candidate_lrc.song_title!r} ({candidate_lrc.source_ref}): {ratio:.0%} of {len(candidate_alignment)} lines matched",
            )
        )
    ranked.sort(key=lambda item: item[0], reverse=True)
    selected: tuple[float, LrcResult, list[dict[str, object]], list[dict[str, object]], int, int, int, int, int] | None = None
    if ranked[0][0] < min_matched_ratio:
        matched_ratio, lrc, _alignment = ranked[0]
        attempts.append(
            SongRepairAttempt(
                "lyrics_alignment",
                "FAILED",
                f"best of {len(ranked)} candidate(s) {lrc.song_title!r} matched only {matched_ratio:.0%} of LRC lines (need >= {min_matched_ratio:.0%}); likely a different song or too-noisy ASR",
            )
        )
        return _finish(candidate_id, attempts, output_dir)

    for raw_matched_ratio, lrc, raw_alignment in ranked:
        if raw_matched_ratio < min_matched_ratio:
            continue
        # Strict process (song-lyrics-timeline-aligner skill): a real performance
        # of this LRC must be explainable by ONE global time shift
        # (clip_time = lrc_time + offset).  Greedy per-line matches that pile
        # onto the same cue or jump around in time are not evidence.
        # Two-pass: estimate the shift from the recall alignment, then re-align
        # constrained to that shift so lines whose text also appears in a false
        # start / abandoned take land on their shift-consistent occurrence.
        estimated_offset_ms = _median_offset(raw_alignment)
        if estimated_offset_ms is not None:
            constrained = _align_lrc_to_cues(
                lrc.lines,
                window,
                match_threshold=match_threshold,
                target_offset_ms=estimated_offset_ms,
                offset_tolerance_ms=SHIFT_TOLERANCE_MS,
            )
        else:
            constrained = raw_alignment
        alignment, offset_ms, shift_error = enforce_global_shift_alignment(alignment=constrained)
        if shift_error is not None:
            attempts.append(
                SongRepairAttempt(
                    "global_shift_check",
                    "FAILED",
                    f"{lrc.song_title!r}: {shift_error}",
                )
            )
            continue
        matched = [entry for entry in alignment if entry["matched_cue_id"] is not None]
        matched_ratio = len(matched) / len(alignment) if alignment else 0.0
        if matched_ratio < min_matched_ratio:
            attempts.append(
                SongRepairAttempt(
                    "global_shift_check",
                    "FAILED",
                    f"{lrc.song_title!r}: only {matched_ratio:.0%} of LRC lines survive the single-global-shift model (raw greedy match was {raw_matched_ratio:.0%})",
                )
            )
            continue
        attempts.append(
            SongRepairAttempt(
                "global_shift_check",
                "SUCCESS",
                f"{lrc.song_title!r}: offset {offset_ms}ms explains {matched_ratio:.0%} of LRC lines",
            )
        )
        head_missing = _leading_unmatched(alignment)
        tail_missing = _trailing_unmatched(alignment)
        if head_missing:
            attempts.append(
                SongRepairAttempt(
                    "song_completeness",
                    "FAILED",
                    f"{lrc.song_title!r}: song head missing from source: first {head_missing} LRC line(s) have no ASR match — the recording window starts mid-song, a complete-song slice is impossible from this source range",
                )
            )
            continue
        # Tolerate a SHORT unmatched tail: the ASR routinely drops the last few
        # sung lines of an outro (quiet fade, applause/'谢谢大家' bleed) even when
        # she sang the song in full (Ivan 2026-07-07 confirmed 《屑屑》 was complete
        # to 4:11 yet the ASR missed 3 tail lines → false BLOCK).  The LRC, not the
        # ASR, is the subtitle authority, so those lines still render.  A GENUINELY
        # cut-off song leaves many more unmatched tail lines and still fails.  The
        # HEAD stays strict — a mid-song start is truly unsliceable.
        tail_limit = max(3, len(alignment) // 12)
        if tail_missing > tail_limit:
            attempts.append(
                SongRepairAttempt(
                    "song_completeness",
                    "FAILED",
                    f"{lrc.song_title!r}: song tail missing from source: last {tail_missing} LRC line(s) have no ASR match (tolerated {tail_limit}) — the performance was cut off or the capture ended early",
                )
            )
            continue
        middle_gap = _longest_unmatched_run(alignment)
        middle_gap_limit = max(4, (len(alignment) * 3) // 10)
        if middle_gap > middle_gap_limit:
            attempts.append(
                SongRepairAttempt(
                    "song_completeness",
                    "FAILED",
                    f"{lrc.song_title!r}: song middle missing from source: {middle_gap} consecutive LRC line(s) have no shift-consistent ASR match (limit {middle_gap_limit}) — the performance is not continuously present in the capture",
                )
            )
            continue
        first_cue = next(entry for entry in alignment if entry["matched_cue_id"] is not None)
        last_cue = next(entry for entry in reversed(alignment) if entry["matched_cue_id"] is not None)
        first_cue_start = first_cue.get("cue_start_ms")
        last_cue_end = last_cue.get("cue_end_ms")
        if not isinstance(first_cue_start, int) or not isinstance(last_cue_end, int):
            attempts.append(
                SongRepairAttempt(
                    "song_boundary",
                    "FAILED",
                    f"{lrc.song_title!r}: matched cue timing is missing or non-integer",
                )
            )
            continue
        first_lyric_start_ms = first_cue_start
        last_lyric_end_ms = last_cue_end
        clip_start_ms = max(0, first_lyric_start_ms - pre_roll_ms)
        clip_end_ms = min(source_duration_ms, last_lyric_end_ms + post_roll_ms)
        if clip_end_ms <= clip_start_ms:
            attempts.append(
                SongRepairAttempt(
                    "song_boundary",
                    "FAILED",
                    f"{lrc.song_title!r}: matched LRC lines map to a non-monotonic cue order; cannot prove a valid full-song start/end boundary",
                )
            )
            continue
        attempts.append(
            SongRepairAttempt(
                "song_completeness",
                "SUCCESS",
                f"{lrc.song_title!r}: {matched_ratio:.0%} of LRC lines matched; head and tail covered",
            )
        )
        selected = (matched_ratio, lrc, alignment, matched, first_lyric_start_ms, last_lyric_end_ms, clip_start_ms, clip_end_ms, offset_ms)
        break

    if selected is None:
        return _finish(candidate_id, attempts, output_dir)

    matched_ratio, lrc, alignment, matched, first_lyric_start_ms, last_lyric_end_ms, clip_start_ms, clip_end_ms, offset_ms = selected

    provider_label = provider_name or lrc.provider
    report_payload = {
        "schema_version": "lyrics-alignment-report.v1",
        "alignment_model": "external_lrc_global_shift.v1",
        "candidate_id": candidate_id,
        "provider": provider_label,
        "song_title": lrc.song_title,
        "artist": lrc.artist,
        "source_ref": lrc.source_ref,
        "matched_line_ratio": round(matched_ratio, 4),
        "line_count": len(alignment),
        "matched_line_count": len(matched),
        "offset_ms": offset_ms,
        "lyric_lines": [
            {"lrc_time_ms": line.time_ms, "text": line.text} for line in lrc.lines
        ],
        "first_lyric_start_ms": first_lyric_start_ms,
        "last_lyric_end_ms": last_lyric_end_ms,
        "alignment": alignment,
    }
    report_path = output_dir / f"{candidate_id}.lyrics-alignment-report.json"
    report_path.write_text(json.dumps(report_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
    attempts.append(SongRepairAttempt("alignment_report", "SUCCESS", f"{report_path.name} sha256:{report_sha[:12]}…"))

    song_boundary = {
        "status": "FULL_SONG_READY",
        "source": f"song_repair.{provider_label}",
        "song_title": lrc.song_title,
        "song_start_ms": clip_start_ms,
        "first_lyric_start_ms": first_lyric_start_ms,
        "last_lyric_end_ms": last_lyric_end_ms,
        "clip_start_ms": clip_start_ms,
        "clip_end_ms": clip_end_ms,
    }
    lyrics_alignment = {
        "status": "READY",
        "provider": provider_label,
        "model": f"{provider_label}-lrc-global-shift-align-v2",
        "source": f"song_repair.{provider_label}",
        "external_lrc": lrc.source_ref,
        "matched_line_ratio": round(matched_ratio, 4),
        "offset_ms": offset_ms,
        "alignment_report_path": str(report_path),
        "alignment_report_sha256": report_sha,
    }
    return _finish(
        candidate_id,
        attempts,
        output_dir,
        repaired=True,
        song_boundary=song_boundary,
        lyrics_alignment=lyrics_alignment,
    )


def build_netease_lrc_provider(*, timeout_seconds: float = 8.0, max_results: int = 3) -> LrcProvider:
    """Real API repair path: NetEase Cloud Music public search + lyric endpoints.

    Returns up to ``max_results`` LRC candidates per query — the caller ranks
    them by actual lyric-to-ASR alignment, so search order is advisory only.
    """

    def provider(query: str) -> list[LrcResult]:
        search_url = "https://music.163.com/api/search/get?" + urllib.parse.urlencode(
            {"s": query, "type": 1, "limit": 6, "offset": 0}
        )
        search_payload = _http_json(search_url, timeout_seconds=timeout_seconds)
        songs = (((search_payload or {}).get("result") or {}).get("songs")) or []
        results: list[LrcResult] = []
        for song in songs:
            if len(results) >= max_results:
                break
            song_id = song.get("id")
            if not isinstance(song_id, int):
                continue
            lyric_url = f"https://music.163.com/api/song/lyric?id={song_id}&lv=1&kv=0&tv=0"
            try:
                lyric_payload = _http_json(lyric_url, timeout_seconds=timeout_seconds)
            except Exception:
                continue
            lrc_text = (((lyric_payload or {}).get("lrc") or {}).get("lyric")) or ""
            lines = parse_lrc_text(lrc_text)
            if len(lines) >= 8:
                artists = song.get("artists") or []
                artist = artists[0].get("name") if artists and isinstance(artists[0], Mapping) else None
                results.append(
                    LrcResult(
                        provider="netease",
                        song_title=str(song.get("name") or ""),
                        artist=str(artist) if artist else None,
                        source_ref=f"netease://song/{song_id}",
                        lines=tuple(lines),
                    )
                )
        return results

    return provider


def parse_lrc_text(lrc_text: str) -> list[LrcLine]:
    lines: list[LrcLine] = []
    for raw_line in lrc_text.splitlines():
        matches = re.findall(r"\[(\d+):(\d+)(?:\.(\d+))?\]", raw_line)
        text = re.sub(r"\[[^\]]*\]", "", raw_line).strip()
        if not matches or not text:
            continue
        # metadata lines such as 作词/作曲/编曲 are not sung lyrics
        if re.match(r"^(作词|作曲|编曲|制作|混音|母带|录音|监制|出品|词|曲|演唱)\s*[:：]", text):
            continue
        for minute, second, fraction in matches:
            fraction_ms = int((fraction or "0").ljust(3, "0")[:3])
            lines.append(LrcLine(time_ms=(int(minute) * 60 + int(second)) * 1000 + fraction_ms, text=text))
    lines.sort(key=lambda line: line.time_ms)
    return lines


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
    """Rank a cue for use as a netease lyric search query.  A search matches on
    LYRIC TEXT, so the best query is one CLEAN, CJK-dense, distinctive line — NOT
    the longest.  The longest ASR lines are often the English/rap sections BCUT
    mangles ("chewe now baby just chewe now") which match nothing; a clean line
    like "谁说圆满的人生才能算圆满" returns 《屑屑》 as the #1 hit.  Score = CJK
    density × capped length; lines with < 6 CJK chars are unusable (-1)."""
    normalized = normalize_lyric_text(text)
    if len(normalized) < 6:
        return -1.0
    cjk = sum(1 for ch in normalized if "一" <= ch <= "鿿")
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


def _longest_unmatched_run(alignment: Sequence[Mapping[str, object]]) -> int:
    longest = 0
    current = 0
    for entry in alignment:
        if entry["matched_cue_id"] is None:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _leading_unmatched(alignment: Sequence[Mapping[str, object]], *, tolerance: int = 1) -> int:
    count = 0
    for entry in alignment:
        if entry["matched_cue_id"] is None:
            count += 1
        else:
            break
    return count if count > tolerance else 0


def _trailing_unmatched(alignment: Sequence[Mapping[str, object]], *, tolerance: int = 1) -> int:
    count = 0
    for entry in reversed(alignment):
        if entry["matched_cue_id"] is None:
            count += 1
        else:
            break
    return count if count > tolerance else 0


def _http_json(url: str, *, timeout_seconds: float) -> Mapping[str, object] | None:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "Referer": "https://music.163.com/",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    return payload if isinstance(payload, Mapping) else None


def _finish(
    candidate_id: str,
    attempts: list[SongRepairAttempt],
    output_dir: Path,
    *,
    repaired: bool = False,
    song_boundary: Mapping[str, object] | None = None,
    lyrics_alignment: Mapping[str, object] | None = None,
) -> SongRepairResult:
    report_path = output_dir / f"{candidate_id}.song-repair.json"
    result = SongRepairResult(
        repaired=repaired,
        attempts=tuple(attempts),
        song_boundary=song_boundary,
        lyrics_alignment=lyrics_alignment,
        report_path=str(report_path),
    )
    report_path.write_text(json.dumps(result.to_manifest(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
