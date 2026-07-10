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

import base64
import binascii
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

LIVE_PERFORMANCE_READY_MODE = "LIVE_STREAMER_SINGING"
LIVE_PERFORMANCE_MODES = {
    LIVE_PERFORMANCE_READY_MODE,
    "ORIGINAL_OR_BACKGROUND_PLAYBACK",
    "OTHER_SINGER",
    "STREAMER_TALKING_OVER_MUSIC",
    "AMBIGUOUS",
}

AGY_AUDIO_LRC_OBSERVATION_SCHEMA_VERSION = "agy-audio-lrc-observation.v3"
LYRIC_VOCAL_SUBJECTS = {
    "LIDOUSHA",
    "OTHER_OR_MIXED_SINGER",
    "RECORDED_OR_PLAYBACK_SINGER",
    "NO_AUDIBLE_LYRIC_VOCAL",
    "AMBIGUOUS",
}
LIDOUSHA_LYRIC_ROLES = {
    "SINGING_THIS_LYRIC",
    "SPEAKING_NOT_SINGING",
    "SILENT_OR_NOT_AUDIBLE",
    "AMBIGUOUS",
}
LYRIC_VOCAL_ASSERTION_KEYS = {
    "lyric_vocal_subject",
    "lidousha_role",
    "same_live_vocal_source_as_lidousha",
    "other_singer_or_harmony_audible",
    "recorded_or_playback_vocal_audible",
}


class LivePerformanceRejected(ValueError):
    """A structurally valid AGY observation that proves this is not a live song.

    Keep this distinct from malformed/unbound AGY output.  The caller still
    fails closed in both cases, but a valid background/original-playback verdict
    must survive song repair so the orchestration layer cannot forget it and
    fall back to treating the seeded song as an ordinary talk candidate.
    """

    def __init__(self, detail: str, performance: Mapping[str, object]) -> None:
        super().__init__(detail)
        self.performance = dict(performance)
        self.reason_codes = live_performance_failure_reason_codes(performance)


def live_performance_failure_reason_codes(performance: object) -> tuple[str, ...]:
    """Map an observed non-live mode to honest user-facing block reasons."""

    mode = performance.get("mode") if isinstance(performance, Mapping) else None
    if mode in {"ORIGINAL_OR_BACKGROUND_PLAYBACK", "STREAMER_TALKING_OVER_MUSIC"}:
        return ("SONG_BACKGROUND_PLAYBACK_ONLY", "SONG_NOT_LIDOUSHA_SINGING")
    if mode == "OTHER_SINGER":
        return ("SONG_NOT_LIDOUSHA_SINGING",)
    return ("SONG_LIVE_PERFORMANCE_UNPROVEN",)

# Credit/production-role terms netease puts in "role : name" metadata lines.
# Matched as a substring of a short pre-colon head (so 音乐制作/贝斯演奏/混音、母带
# are all caught, not just exact prefixes), to keep them out of burned lyrics.
_LRC_CREDIT_KEYWORDS = (
    "作词", "作曲", "编曲", "制作", "监制", "出品", "发行", "混音", "母带", "录音",
    "演唱", "演奏", "和声", "合声", "吉他", "贝斯", "鼓", "键盘", "弦乐", "配唱",
    "统筹", "企划", "策划", "后期", "版权",
)


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
class AudioLrcAlignmentRun:
    """Audio-bound observation bundle returned by the AGY adapter.

    The model output is deliberately not a READY verdict.  ``attempt_song_repair``
    validates the bundle, recomputes the global shift and mints the standard
    hash-bound proof only when every invariant holds.
    """

    payload: Mapping[str, object]
    provider: str
    model: str
    rc: int
    provider_fallback_used: bool
    source_origin_path: str
    source_path: str
    source_sha256: str
    source_duration_ms: int
    lrc_path: str
    lrc_sha256: str
    prompt_path: str
    prompt_sha256: str
    output_path: str
    output_sha256: str
    manifest_path: str
    manifest_sha256: str


AudioLrcAligner = Callable[[Path, LrcResult, str, Path], AudioLrcAlignmentRun]


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
    reason_codes: tuple[str, ...] = ()
    live_performance: Mapping[str, object] | None = None

    def to_manifest(self) -> dict[str, object]:
        return {
            "schema_version": SONG_REPAIR_SCHEMA_VERSION,
            "repaired": self.repaired,
            "attempts": [attempt.to_manifest() for attempt in self.attempts],
            "song_boundary": dict(self.song_boundary) if self.song_boundary else None,
            "lyrics_alignment": dict(self.lyrics_alignment) if self.lyrics_alignment else None,
            "report_path": self.report_path,
            "reason_codes": list(self.reason_codes),
            "live_performance": dict(self.live_performance) if self.live_performance else None,
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
    max_outro_ms: int = 22_000,
    pinned_lrc_results: Sequence[LrcResult] = (),
    source_media_path: Path | None = None,
    audio_lrc_aligner: AudioLrcAligner | None = None,
) -> SongRepairResult:
    """Try to repair a song candidate into a fully-proven full-song boundary.

    ``pinned_lrc_results`` are caller-supplied LRCs (e.g. a known recurring song
    matched by fingerprint) that are added to the candidate pool BEFORE flaky
    LLM/text discovery, so alignment ranking can still pick the right song when
    search misses it.  ``max_outro_ms`` caps the instrumental 后奏 (outro) kept
    after the last sung line up to the next spoken cue.
    """

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

    if lrc_provider is None and not pinned_lrc_results:
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
    for pinned in pinned_lrc_results:
        if isinstance(pinned, LrcResult) and pinned.lines and pinned.source_ref not in seen_refs:
            seen_refs.add(pinned.source_ref)
            candidates.append(pinned)
            attempts.append(
                SongRepairAttempt(
                    "pinned_lrc",
                    "SUCCESS",
                    f"{pinned.song_title!r} ({pinned.source_ref}) pinned ({len(pinned.lines)} LRC lines) — deterministic, ranked against search",
                )
            )
    provider_errors: list[str] = []
    if lrc_provider is not None:
        for query in deduped_queries:
            if len(candidates) >= max_lrc_candidates:
                break
            try:
                found = lrc_provider(query)
            except Exception as exc:  # provider failures must not crash review; they are recorded
                provider_errors.append(f"{query!r}: {type(exc).__name__}: {exc}")
                continue
            found_list = _coerce_lrc_results(found)
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
    audio_alignment_run: AudioLrcAlignmentRun | None = None
    if audio_lrc_aligner is not None:
        matched_ratio, lrc, _alignment = ranked[0]
        if source_media_path is None:
            attempts.append(SongRepairAttempt("agy_audio_lrc_alignment", "FAILED", "source media is required"))
            return _finish(candidate_id, attempts, output_dir)
        try:
            lrc = _choose_audio_lrc_candidate(
                ranked,
                pinned_lrc_results=pinned_lrc_results,
                min_recall_ratio=0.20,
                min_margin=0.08,
            )
        except ValueError as exc:
            attempts.append(SongRepairAttempt("agy_audio_lrc_identity", "FAILED", str(exc)))
            return _finish(candidate_id, attempts, output_dir)
        attempts.append(
            SongRepairAttempt(
                "agy_audio_lrc_identity",
                "SUCCESS",
                f"unique LRC identity {lrc.song_title!r} ({lrc.source_ref}); "
                "verifying current full-window audio and live-performance mode",
            )
        )
        try:
            audio_alignment_run = audio_lrc_aligner(
                Path(source_media_path),
                lrc,
                candidate_id,
                output_dir / "agy_audio_lrc",
            )
            selected = _validated_audio_lrc_selection(
                run=audio_alignment_run,
                lrc=lrc,
                candidate_id=candidate_id,
                source_media_path=Path(source_media_path),
                source_duration_ms=source_duration_ms,
                min_matched_ratio=min_matched_ratio,
            )
        except LivePerformanceRejected as exc:
            attempts.append(
                SongRepairAttempt(
                    "agy_audio_lrc_alignment",
                    "FAILED",
                    f"{type(exc).__name__}: {exc}",
                )
            )
            return _finish(
                candidate_id,
                attempts,
                output_dir,
                reason_codes=exc.reason_codes,
                live_performance=exc.performance,
            )
        except Exception as exc:
            attempts.append(
                SongRepairAttempt(
                    "agy_audio_lrc_alignment",
                    "FAILED",
                    f"{type(exc).__name__}: {exc}",
                )
            )
            return _finish(candidate_id, attempts, output_dir)
        attempts.append(
            SongRepairAttempt(
                "agy_audio_lrc_alignment",
                "SUCCESS",
                f"{lrc.song_title!r}: current audio proves {selected[0]:.0%} of canonical LRC lines, "
                "one global shift, and LIVE_STREAMER_SINGING performance mode",
            )
        )
    elif ranked[0][0] < min_matched_ratio:
        matched_ratio, lrc, _alignment = ranked[0]
        if source_media_path is None or audio_lrc_aligner is None:
            attempts.append(
                SongRepairAttempt(
                    "lyrics_alignment",
                    "FAILED",
                    f"best of {len(ranked)} candidate(s) {lrc.song_title!r} matched only {matched_ratio:.0%} of LRC lines (need >= {min_matched_ratio:.0%}); likely a different song or too-noisy ASR",
                )
            )
            return _finish(candidate_id, attempts, output_dir)

    for raw_matched_ratio, lrc, raw_alignment in ranked if selected is None else ():
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
        # A global shift is not sufficient proof when the implied LRC time zero
        # falls outside the captured source.  In particular, clamping a negative
        # offset to source 0 below would turn a recording that starts after the
        # song's instrumental intro into a false FULL_SONG_READY result.
        if not 0 <= offset_ms < source_duration_ms:
            attempts.append(
                SongRepairAttempt(
                    "song_boundary",
                    "FAILED",
                    f"{lrc.song_title!r}: nominal LRC zero {offset_ms}ms is outside source "
                    f"[0, {source_duration_ms})ms; cannot prove the complete song head",
                )
            )
            continue
        # ``offset_ms`` is where LRC time zero lands in the live source.  Start
        # from that nominal track boundary (plus a small safety lead), not from
        # ``first lyric - pre_roll``: the latter chopped the entire 7.7-second
        # instrumental intro from 2026-07-09 《芽吹くとき》.
        clip_start_ms = max(0, offset_ms - pre_roll_ms)
        # Keep the instrumental outro (后奏): a complete song does not end on the
        # last sung word.  The outro has no lyrics for ASR to anchor on, so extend
        # to just before the next spoken cue after the song (the post-song talk),
        # capped by max_outro_ms.  When the song ends the window, keep the full cap.
        next_cue_start_ms = min(
            (cue.source_start_ms for cue in cues if cue.source_start_ms > last_lyric_end_ms),
            default=None,
        )
        outro_cap_ms = last_lyric_end_ms + max_outro_ms
        outro_end_ms = outro_cap_ms if next_cue_start_ms is None else min(next_cue_start_ms, outro_cap_ms)
        # never cut the last sung word's natural tail
        outro_end_ms = max(outro_end_ms, last_lyric_end_ms + min(post_roll_ms, 1_000))
        clip_end_ms = min(source_duration_ms, outro_end_ms)
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
        "nominal_lrc_zero_ms": offset_ms,
        "lyric_lines": [
            {"lrc_time_ms": line.time_ms, "text": line.text} for line in lrc.lines
        ],
        "first_lyric_start_ms": first_lyric_start_ms,
        "last_lyric_end_ms": last_lyric_end_ms,
        "alignment": alignment,
    }
    if audio_alignment_run is not None:
        report_payload.update(
            {
                "evidence_source": "agy_audio_lrc",
                "audio_alignment_provider": audio_alignment_run.provider,
                "audio_alignment_model": audio_alignment_run.model,
                "spot_checks": audio_alignment_run.payload["spot_checks"],
                "live_performance": audio_alignment_run.payload["live_performance"],
                "post_song_talk_start_ms": audio_alignment_run.payload["post_song_talk_start_ms"],
                "source_media_path": audio_alignment_run.source_origin_path,
                "source_media_sha256": audio_alignment_run.source_sha256,
                "audio_alignment_artifacts": {
                    "source_origin_path": audio_alignment_run.source_origin_path,
                    "source_path": audio_alignment_run.source_path,
                    "source_sha256": audio_alignment_run.source_sha256,
                    "source_duration_ms": audio_alignment_run.source_duration_ms,
                    "lrc_path": audio_alignment_run.lrc_path,
                    "lrc_sha256": audio_alignment_run.lrc_sha256,
                    "prompt_path": audio_alignment_run.prompt_path,
                    "prompt_sha256": audio_alignment_run.prompt_sha256,
                    "raw_output_path": audio_alignment_run.output_path,
                    "raw_output_sha256": audio_alignment_run.output_sha256,
                    "run_manifest_path": audio_alignment_run.manifest_path,
                    "run_manifest_sha256": audio_alignment_run.manifest_sha256,
                },
            }
        )
    report_path = output_dir / f"{candidate_id}.lyrics-alignment-report.json"
    report_path.write_text(json.dumps(report_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
    attempts.append(SongRepairAttempt("alignment_report", "SUCCESS", f"{report_path.name} sha256:{report_sha[:12]}…"))

    song_boundary = {
        "status": "FULL_SONG_READY",
        "source": f"song_repair.{provider_label}",
        "song_title": lrc.song_title,
        "song_start_ms": clip_start_ms,
        "nominal_lrc_zero_ms": offset_ms,
        "first_lyric_start_ms": first_lyric_start_ms,
        "last_lyric_end_ms": last_lyric_end_ms,
        "clip_start_ms": clip_start_ms,
        "clip_end_ms": clip_end_ms,
    }
    lyrics_alignment = {
        "status": "READY",
        "provider": provider_label,
        "model": (
            f"{provider_label}-agy-audio-lrc-global-shift-v1"
            if audio_alignment_run is not None
            else f"{provider_label}-lrc-global-shift-align-v2"
        ),
        "source": f"song_repair.{provider_label}",
        "external_lrc": lrc.source_ref,
        "matched_line_ratio": round(matched_ratio, 4),
        "offset_ms": offset_ms,
        "nominal_lrc_zero_ms": offset_ms,
        "alignment_report_path": str(report_path),
        "alignment_report_sha256": report_sha,
        "source_media_path": audio_alignment_run.source_origin_path if audio_alignment_run is not None else None,
        "source_media_sha256": audio_alignment_run.source_sha256 if audio_alignment_run is not None else None,
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


def build_lrclib_lrc_provider(*, timeout_seconds: float = 20.0, max_results: int = 3) -> LrcProvider:
    """Build an LRCLIB search provider that returns only usable synced LRCs.

    LRCLIB's search response can include plain-only lyrics.  Those are useful
    for reading but cannot prove clip timing, so this provider deliberately
    fails closed unless ``syncedLyrics`` parses into at least eight timed lines.
    Search order remains advisory; :func:`attempt_song_repair` reranks results
    by alignment to the actual performance.
    """

    def provider(query: str) -> list[LrcResult]:
        search_url = "https://lrclib.net/api/search?" + urllib.parse.urlencode({"q": query})
        payload = _http_json_value(search_url, timeout_seconds=timeout_seconds)
        records = payload if isinstance(payload, list) else []
        results: list[LrcResult] = []
        for record in records:
            if len(results) >= max_results:
                break
            if not isinstance(record, Mapping):
                continue
            result = _lrclib_record_to_result(record)
            if result is not None:
                results.append(result)
        return results

    return provider


def build_kugou_lrc_provider(*, timeout_seconds: float = 12.0, max_results: int = 3) -> LrcProvider:
    """Build a no-credential Kugou synced-lyric fallback.

    Kugou exposes search, lyric-candidate and lyric-download endpoints used by
    its web client.  Search ranking is still only discovery: before downloading
    a candidate, this adapter requires its title, artist and duration to agree
    with the selected search row.  The downloaded LRC then enters the same
    audio/alignment proof gates as every other provider result.

    Kugou commonly inserts ``[00:00] title - artist`` as a timed metadata row.
    That row is provider-specific metadata, not a sung lyric, and is removed
    here rather than weakening the generic LRC parser.
    """

    def provider(query: str) -> list[LrcResult]:
        search_url = "https://songsearch.kugou.com/song_search_v2?" + urllib.parse.urlencode(
            {
                "keyword": query,
                "page": 1,
                "pagesize": 6,
                "platform": "WebFilter",
                "userid": -1,
                "iscorrection": 1,
                "privilege_filter": 0,
            }
        )
        payload = _http_json_value(
            search_url,
            timeout_seconds=timeout_seconds,
            request_headers={"Referer": "https://www.kugou.com/"},
        )
        if not isinstance(payload, Mapping) or payload.get("status") != 1:
            return []
        data = payload.get("data")
        tracks = data.get("lists") if isinstance(data, Mapping) else None
        if not isinstance(tracks, list):
            return []

        results: list[LrcResult] = []
        seen_fingerprints: set[str] = set()
        # Three distinct search rows are enough for a fallback.  Bounding the
        # fan-out keeps an unsuccessful ASR-derived query from issuing dozens
        # of lyric downloads before the next independent query/provider runs.
        for track in tracks[:3]:
            if len(results) >= max_results:
                break
            if not isinstance(track, Mapping):
                continue
            song_title = _strip_kugou_markup(track.get("SongName") or track.get("OriSongName"))
            artist = _strip_kugou_markup(track.get("SingerName"))
            file_hash = str(track.get("FileHash") or "").strip().upper()
            duration_s = track.get("Duration")
            if (
                not song_title
                or not artist
                or not re.fullmatch(r"[0-9A-F]{32}", file_hash)
                or isinstance(duration_s, bool)
                or not isinstance(duration_s, (int, float))
                or duration_s <= 0
            ):
                continue
            duration_ms = int(round(float(duration_s) * 1_000))
            lyric_search_url = "https://lyrics.kugou.com/search?" + urllib.parse.urlencode(
                {
                    "ver": 1,
                    "man": "yes",
                    "client": "pc",
                    "keyword": f"{artist} - {song_title}",
                    "hash": file_hash,
                    "timelength": duration_ms,
                }
            )
            try:
                lyric_payload = _http_json_value(
                    lyric_search_url,
                    timeout_seconds=timeout_seconds,
                    request_headers={"Referer": "https://www.kugou.com/"},
                )
            except Exception:
                continue
            if not isinstance(lyric_payload, Mapping) or lyric_payload.get("status") != 200:
                continue
            candidates = lyric_payload.get("candidates")
            if not isinstance(candidates, list):
                continue

            # Candidate scores are advisory.  Only exact normalized identity
            # and a near-exact catalog duration are allowed to reach download.
            # Try at most two exact candidates for this track to bound requests.
            exact_downloads = 0
            for lyric_candidate in candidates:
                if not isinstance(lyric_candidate, Mapping):
                    continue
                if not _kugou_candidate_matches_track(
                    lyric_candidate,
                    song_title=song_title,
                    artist=artist,
                    duration_ms=duration_ms,
                ):
                    continue
                lyric_id = str(lyric_candidate.get("id") or "").strip()
                access_key = str(lyric_candidate.get("accesskey") or "").strip()
                if not lyric_id.isdigit() or not re.fullmatch(r"[0-9A-Fa-f]{32}", access_key):
                    continue
                exact_downloads += 1
                download_url = "https://lyrics.kugou.com/download?" + urllib.parse.urlencode(
                    {
                        "ver": 1,
                        "client": "pc",
                        "id": lyric_id,
                        "accesskey": access_key,
                        "fmt": "lrc",
                        "charset": "utf8",
                    }
                )
                try:
                    download_payload = _http_json_value(
                        download_url,
                        timeout_seconds=timeout_seconds,
                        request_headers={"Referer": "https://www.kugou.com/"},
                    )
                    if not isinstance(download_payload, Mapping) or download_payload.get("status") != 200:
                        raise ValueError("Kugou lyric download returned an invalid response")
                    content = download_payload.get("content")
                    if not isinstance(content, str):
                        raise ValueError("Kugou lyric download did not return base64 content")
                    lrc_text = base64.b64decode(content, validate=True).decode("utf-8")
                except (binascii.Error, UnicodeDecodeError, ValueError, OSError):
                    if exact_downloads >= 2:
                        break
                    continue
                lines = _filter_kugou_timed_metadata(
                    parse_lrc_text(lrc_text),
                    song_title=song_title,
                    artist=artist,
                )
                if len(lines) < 8:
                    if exact_downloads >= 2:
                        break
                    continue
                result = LrcResult(
                    provider="kugou",
                    song_title=song_title,
                    artist=artist,
                    source_ref=download_url,
                    lines=tuple(lines),
                )
                fingerprint = _lrc_fingerprint(result)
                if fingerprint not in seen_fingerprints:
                    seen_fingerprints.add(fingerprint)
                    results.append(result)
                break
        return results

    return provider


def build_composite_lrc_provider(*providers: LrcProvider, max_results: int | None = None) -> LrcProvider:
    """Combine providers as independent fallbacks without erasing provenance.

    A temporary failure in one public lyric service must not prevent another
    provider from supplying evidence.  Results are deduplicated by canonical
    ``source_ref`` while each result's original ``provider`` and ``source_ref``
    are preserved for the alignment report and proof gate.
    """

    def provider(query: str) -> list[LrcResult]:
        results: list[LrcResult] = []
        seen_refs: set[str] = set()
        for candidate_provider in providers:
            try:
                found = candidate_provider(query)
            except Exception:
                continue
            for item in _coerce_lrc_results(found):
                if item.source_ref in seen_refs:
                    continue
                seen_refs.add(item.source_ref)
                results.append(item)
                if max_results is not None and len(results) >= max_results:
                    return results
        return results

    return provider


def fetch_lrclib_lrc(song_ref: str, *, timeout_seconds: float = 20.0) -> LrcResult | None:
    """Fetch one LRCLIB synced lyric by numeric id or canonical LRCLIB ref.

    Accepted forms include ``"33542202"``, ``"lrclib://track/33542202"``,
    and ``"https://lrclib.net/api/get/33542202"``.  Plain, unsynchronised lyrics are rejected:
    the song repair path needs timestamps, not merely lyric text.
    """

    song_id = _parse_lrclib_id(song_ref)
    if song_id is None:
        return None
    try:
        payload = _http_json(
            f"https://lrclib.net/api/get/{song_id}",
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        return None
    if not isinstance(payload, Mapping):
        return None
    return _lrclib_record_to_result(payload, fallback_id=song_id)


def fetch_netease_lrc(song_ref: str, *, timeout_seconds: float = 8.0) -> LrcResult | None:
    """Fetch one NetEase song's LRC by id/ref, bypassing flaky text search.

    Accepts a bare id (``"2615403834"``) or a ref (``"netease://song/2615403834"``).
    Used to pin a known recurring song so its identification is deterministic.
    """

    match = re.search(r"(\d{4,})", str(song_ref or ""))
    if not match:
        return None
    song_id = int(match.group(1))
    try:
        lyric_payload = _http_json(
            f"https://music.163.com/api/song/lyric?id={song_id}&lv=1&kv=0&tv=0",
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        return None
    lrc_text = (((lyric_payload or {}).get("lrc") or {}).get("lyric")) or ""
    lines = parse_lrc_text(lrc_text)
    if len(lines) < 8:
        return None
    song_title = ""
    artist: str | None = None
    try:
        detail = _http_json(
            f"https://music.163.com/api/song/detail?ids=%5B{song_id}%5D",
            timeout_seconds=timeout_seconds,
        )
        songs = (detail or {}).get("songs") or []
        if songs and isinstance(songs[0], Mapping):
            song_title = str(songs[0].get("name") or "")
            artists = songs[0].get("artists") or []
            if artists and isinstance(artists[0], Mapping):
                artist = str(artists[0].get("name") or "") or None
    except Exception:
        pass
    return LrcResult(
        provider="netease",
        song_title=song_title,
        artist=artist,
        source_ref=f"netease://song/{song_id}",
        lines=tuple(lines),
    )


def parse_lrc_text(lrc_text: str) -> list[LrcLine]:
    lines: list[LrcLine] = []
    for raw_line in lrc_text.splitlines():
        matches = re.findall(r"\[(\d+):(\d+)(?:\.(\d+))?\]", raw_line)
        text = re.sub(r"\[[^\]]*\]", "", raw_line).strip()
        if not matches or not text:
            continue
        # Metadata/credit lines are not sung lyrics. netease appends them with
        # their own timestamps (some late, inside the outro) in a "role : name"
        # shape — sung lyrics never use a spaced colon.  The old prefix-only rule
        # missed 音乐制作/贝斯演奏/混音、母带 (they don't START with a listed keyword),
        # so credits got burned as subtitles over the 后奏 (Ivan 2026-07-07 《屑屑》).
        # Match the credit keyword anywhere in a short pre-colon head instead.
        if re.match(r"^(作词|作曲|编曲|制作|混音|母带|录音|监制|出品|词|曲|演唱)\s*[:：]", text):
            continue
        colon_split = re.split(r"[:：]", text, maxsplit=1)
        if len(colon_split) == 2:
            head = colon_split[0].strip()
            if len(head) <= 12 and any(kw in head for kw in _LRC_CREDIT_KEYWORDS):
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


def _choose_audio_lrc_candidate(
    ranked: Sequence[tuple[float, LrcResult, list[dict[str, object]]]],
    *,
    pinned_lrc_results: Sequence[LrcResult],
    min_recall_ratio: float,
    min_margin: float,
) -> LrcResult:
    """Choose one deterministic lyric identity before an expensive audio pass.

    Provider records with the same normalized title+artist are one identity
    even when their synced timestamps differ slightly.  A weak or tied signal
    between *different songs* is intentionally not enough: feeding an
    arbitrary LRC to a multimodal model invites it to force the supplied lyrics
    onto unrelated audio.
    """

    entries = [(ratio, lrc) for ratio, lrc, _alignment in ranked]
    parents = list(range(len(entries)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    identities = [_lrc_identity_key(lrc) for _ratio, lrc in entries]
    fingerprints = [_lrc_fingerprint(lrc) for _ratio, lrc in entries]
    for left in range(len(entries)):
        for right in range(left + 1, len(entries)):
            # Provider records are the same song when either title+artist or
            # the complete canonical timed lyric agrees.  The relation is
            # transitive: an exact-title NetEase row can join an exact-title
            # LRCLIB row, which in turn joins LRCLIB's translated-title alias
            # by identical lyrics.
            if identities[left] == identities[right] or fingerprints[left] == fingerprints[right]:
                union(left, right)

    groups: dict[int, list[tuple[float, LrcResult]]] = {}
    for index, entry in enumerate(entries):
        groups.setdefault(find(index), []).append(entry)
    if not groups:
        raise ValueError("no canonical LRC identity is available for audio alignment")
    pinned_identities = {_lrc_identity_key(item) for item in pinned_lrc_results}
    pinned_fingerprints = {_lrc_fingerprint(item) for item in pinned_lrc_results}
    grouped = [
        (
            max(item[0] for item in group_entries),
            any(
                _lrc_identity_key(item) in pinned_identities or _lrc_fingerprint(item) in pinned_fingerprints
                for _ratio, item in group_entries
            ),
            min(item.source_ref for _ratio, item in group_entries),
            group_entries,
        )
        for group_entries in groups.values()
    ]
    # Recall remains the primary authority.  For an *exact* top-recall tie,
    # prefer the one identity already pinned by >=2 known-song fingerprint
    # lines.  Previously the disjoint-set root index broke ties, so the real
    # 《屑屑》 pin could lose arbitrarily to a Studio Live/provider variant with
    # the same 41/52 ASR recall and the audio verifier was never reached.
    grouped.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    top_ratio, is_curated, _top_key, top_entries = grouped[0]
    second_ratio = grouped[1][0] if len(grouped) > 1 else 0.0
    curated_top_ties = [
        group for group in grouped if group[0] == top_ratio and group[1]
    ]
    if len(curated_top_ties) > 1:
        raise ValueError(
            "ambiguous curated LRC identity: multiple pinned songs share the best ASR recall; "
            "refusing to choose one before audio verification"
        )
    if is_curated:
        if top_ratio < 0.08:
            raise ValueError(
                f"curated LRC identity has only {top_ratio:.0%} ASR recall; refusing to force it onto audio"
            )
    elif top_ratio < min_recall_ratio or top_ratio - second_ratio < min_margin:
        raise ValueError(
            "ambiguous low-ASR LRC identity: "
            f"best={top_ratio:.0%}, runner-up={second_ratio:.0%}, "
            f"need best>={min_recall_ratio:.0%} and margin>={min_margin:.0%}"
        )
    # Prefer the public LRCLIB record when multiple providers expose exactly
    # the same timed lyrics; otherwise retain the strongest discovery record.
    return sorted(
        top_entries,
        key=lambda item: (
            item[1].provider != "lrclib",
            -item[0],
            item[1].source_ref,
        ),
    )[0][1]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_bound_artifact(path_value: str, expected_sha256: str, label: str) -> Path:
    path = Path(path_value)
    if not path.is_file():
        raise ValueError(f"{label} artifact is missing: {path}")
    actual = _sha256_file(path)
    if actual != str(expected_sha256).lower().removeprefix("sha256:"):
        raise ValueError(f"{label} sha256 mismatch")
    return path


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validated_audio_lrc_selection(
    *,
    run: AudioLrcAlignmentRun,
    lrc: LrcResult,
    candidate_id: str,
    source_media_path: Path,
    source_duration_ms: int,
    min_matched_ratio: float,
) -> tuple[float, LrcResult, list[dict[str, object]], list[dict[str, object]], int, int, int, int, int]:
    """Validate an AGY audio observation and convert it into standard proof.

    The validator ignores any model-supplied title, offset, boundary, verdict,
    or completeness claim.  It binds the current media/LRC bytes and derives
    the single global shift from one exact observation per canonical LRC line.
    """

    if run.provider != "agy" or run.model != "Gemini 3.5 Flash (High)":
        raise ValueError(f"audio aligner must be agy Gemini 3.5 Flash (High), got {run.provider} {run.model}")
    if run.rc != 0 or run.provider_fallback_used:
        raise ValueError(f"audio aligner was not a clean no-fallback run (rc={run.rc})")
    if not source_media_path.is_file():
        raise ValueError(f"current source media is missing: {source_media_path}")
    try:
        current_source_origin = str(source_media_path.resolve(strict=True))
        declared_source_origin = str(Path(run.source_origin_path).resolve(strict=True))
    except OSError as exc:
        raise ValueError(f"audio source origin cannot be resolved: {exc}") from exc
    if declared_source_origin != current_source_origin:
        raise ValueError("audio observation source origin is not the current source media")
    source_path = _require_bound_artifact(run.source_path, run.source_sha256, "audio source")
    if _sha256_file(source_media_path) != run.source_sha256 or _sha256_file(source_path) != run.source_sha256:
        raise ValueError("audio observation is not bound to the current source media")
    lrc_path = _require_bound_artifact(run.lrc_path, run.lrc_sha256, "canonical LRC")
    _require_bound_artifact(run.prompt_path, run.prompt_sha256, "audio prompt")
    _require_bound_artifact(run.output_path, run.output_sha256, "raw audio alignment")
    manifest_path = _require_bound_artifact(run.manifest_path, run.manifest_sha256, "audio run manifest")
    try:
        run_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"audio run manifest is invalid JSON: {exc}") from exc
    manifest_artifacts = run_manifest.get("artifacts") if isinstance(run_manifest, Mapping) else None
    if (
        not isinstance(run_manifest, Mapping)
        or run_manifest.get("schema_version") != "agy-audio-lrc-run.v1"
        or run_manifest.get("candidate_id") != candidate_id
        or run_manifest.get("provider") != run.provider
        or run_manifest.get("model") != run.model
        or run_manifest.get("agy_rc") != run.rc
        or run_manifest.get("provider_fallback_used") is not run.provider_fallback_used
        or run_manifest.get("sandbox") is not True
        or not isinstance(manifest_artifacts, Mapping)
        or any(
            manifest_artifacts.get(key) != value
            for key, value in (
                ("source_path", run.source_path),
                ("source_origin_path", run.source_origin_path),
                ("source_sha256", run.source_sha256),
                ("source_duration_ms", run.source_duration_ms),
                ("lrc_path", run.lrc_path),
                ("lrc_sha256", run.lrc_sha256),
                ("prompt_path", run.prompt_path),
                ("prompt_sha256", run.prompt_sha256),
                ("output_path", run.output_path),
                ("output_sha256", run.output_sha256),
            )
        )
    ):
        raise ValueError("audio run manifest is not bound to the current run artifacts")
    parsed_lrc = parse_lrc_text(lrc_path.read_text(encoding="utf-8"))
    if [(line.time_ms, line.text) for line in parsed_lrc] != [(line.time_ms, line.text) for line in lrc.lines]:
        raise ValueError("bound LRC artifact does not equal the selected canonical LRC")
    if not _is_int(run.source_duration_ms) or abs(run.source_duration_ms - source_duration_ms) > 1_000:
        raise ValueError(
            f"audio source duration {run.source_duration_ms}ms does not match job duration {source_duration_ms}ms"
        )
    effective_duration_ms = min(run.source_duration_ms, source_duration_ms)

    payload = run.payload
    required_top = {
        "schema_version",
        "record",
        "observations",
        "spot_checks",
        "live_performance",
        "post_song_talk_start_ms",
    }
    if not isinstance(payload, Mapping) or set(payload) != required_top:
        raise ValueError("audio observation top-level schema/keys are invalid")
    if payload.get("schema_version") != AGY_AUDIO_LRC_OBSERVATION_SCHEMA_VERSION:
        raise ValueError("audio observation schema_version is invalid")
    record = payload.get("record")
    if not isinstance(record, Mapping) or set(record) != {
        "attempt_id",
        "candidate_id",
        "source_sha256",
        "lrc_sha256",
        "source_duration_ms",
    }:
        raise ValueError("audio observation record is invalid")
    if record.get("candidate_id") != candidate_id:
        raise ValueError("audio observation candidate_id mismatch")
    if record.get("source_sha256") != run.source_sha256 or record.get("lrc_sha256") != run.lrc_sha256:
        raise ValueError("audio observation echoed artifact hash mismatch")
    if record.get("source_duration_ms") != run.source_duration_ms:
        raise ValueError("audio observation echoed duration mismatch")
    attempt_id = record.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise ValueError("audio observation attempt_id is missing")

    observations = payload.get("observations")
    if not isinstance(observations, list) or len(observations) != len(lrc.lines) or len(observations) < 8:
        raise ValueError("audio observation must contain exactly one row per canonical LRC line")
    alignment: list[dict[str, object]] = []
    residuals: list[int] = []
    previous_start: int | None = None
    previous_end: int | None = None
    for index, (row, line) in enumerate(zip(observations, lrc.lines)):
        if not isinstance(row, Mapping) or set(row) != {
            "lrc_index",
            "lrc_time_ms",
            "text",
            "heard",
            "live_start_ms",
            "live_end_ms",
            "confidence",
            *LYRIC_VOCAL_ASSERTION_KEYS,
        }:
            raise ValueError(f"audio observation row {index} has invalid keys")
        if row.get("lrc_index") != index or row.get("lrc_time_ms") != line.time_ms or row.get("text") != line.text:
            raise ValueError(f"audio observation row {index} does not exactly echo the canonical LRC")
        # v1 burns every canonical lyric line.  Until the materializer supports
        # a performed-only sequence, skipped/repeated/changed arrangements must
        # block instead of silently burning studio lyrics that were not sung.
        if row.get("heard") is not True:
            raise ValueError(f"canonical LRC line {index} was not affirmatively heard")
        start_ms = row.get("live_start_ms")
        end_ms = row.get("live_end_ms")
        confidence = row.get("confidence")
        if not (_is_int(start_ms) and _is_int(end_ms) and 0 <= start_ms < end_ms <= effective_duration_ms):
            raise ValueError(f"audio observation row {index} timing is invalid")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.8 <= confidence <= 1.0:
            raise ValueError(f"audio observation row {index} confidence is below 0.8")
        if previous_start is not None and start_ms <= previous_start:
            raise ValueError("audio observation starts are not strictly monotonic")
        if previous_end is not None and previous_end - start_ms > 250:
            raise ValueError("adjacent audio observations overlap by more than 250ms")
        residuals.append(start_ms - line.time_ms)
        alignment.append(
            {
                "lrc_time_ms": line.time_ms,
                "lrc_text": line.text,
                "matched_cue_id": f"agy-audio:{run.output_sha256[:12]}:line-{index}",
                "cue_start_ms": start_ms,
                "cue_end_ms": end_ms,
                "match_ratio": round(float(confidence), 4),
                "evidence_source": "agy_audio_lrc",
                "lyric_vocal_subject": row.get("lyric_vocal_subject"),
                "lidousha_role": row.get("lidousha_role"),
                "same_live_vocal_source_as_lidousha": row.get(
                    "same_live_vocal_source_as_lidousha"
                ),
                "other_singer_or_harmony_audible": row.get(
                    "other_singer_or_harmony_audible"
                ),
                "recorded_or_playback_vocal_audible": row.get(
                    "recorded_or_playback_vocal_audible"
                ),
            }
        )
        previous_start = start_ms
        previous_end = end_ms

    offset_ms = sorted(residuals)[len(residuals) // 2]
    if any(abs(residual - offset_ms) > 1_500 for residual in residuals):
        raise ValueError("audio observations do not fit one global shift within ±1500ms")
    lrc_span = lrc.lines[-1].time_ms - lrc.lines[0].time_ms
    live_span = int(alignment[-1]["cue_start_ms"]) - int(alignment[0]["cue_start_ms"])
    if lrc_span < 20_000 or not 0.95 <= live_span / lrc_span <= 1.05:
        raise ValueError("audio observations imply tempo drift; explicit stretch proof is required")
    if not 0 <= offset_ms < effective_duration_ms:
        raise ValueError(f"nominal LRC zero {offset_ms}ms is outside the current source")

    first_lyric_start_ms = int(alignment[0]["cue_start_ms"])
    last_lyric_end_ms = int(alignment[-1]["cue_end_ms"])
    live_performance = payload.get("live_performance")
    performance_schema_error = validate_live_performance_observation(
        live_performance,
        first_lyric_start_ms=first_lyric_start_ms,
        last_lyric_end_ms=last_lyric_end_ms,
        observations=observations,
        require_ready=False,
    )
    if performance_schema_error is not None:
        raise ValueError(performance_schema_error)
    performance_error = validate_live_performance_observation(
        live_performance,
        first_lyric_start_ms=first_lyric_start_ms,
        last_lyric_end_ms=last_lyric_end_ms,
        observations=observations,
        require_ready=True,
    )
    if performance_error is not None:
        assert isinstance(live_performance, Mapping)
        raise LivePerformanceRejected(performance_error, live_performance)
    spot_checks = payload.get("spot_checks")
    required_spots = {"first_line", "chorus", "repeated_section", "longest_instrumental_gap", "tail"}
    if not isinstance(spot_checks, list) or len(spot_checks) != 5:
        raise ValueError("audio observation must include exactly five spot checks")
    spots: dict[str, Mapping[str, object]] = {}
    for spot in spot_checks:
        if not isinstance(spot, Mapping) or set(spot) != {"name", "live_time_ms", "result", "notes"}:
            raise ValueError("audio spot-check schema is invalid")
        name = spot.get("name")
        time_ms = spot.get("live_time_ms")
        if name not in required_spots or name in spots or spot.get("result") != "OK":
            raise ValueError("audio spot-check names/results are invalid")
        if not _is_int(time_ms) or not first_lyric_start_ms <= time_ms <= last_lyric_end_ms:
            raise ValueError(f"audio spot-check {name} time is outside the performed song")
        spots[str(name)] = spot
    if set(spots) != required_spots:
        raise ValueError("audio spot-check set is incomplete")
    if abs(int(spots["first_line"]["live_time_ms"]) - first_lyric_start_ms) > 1_500:
        raise ValueError("first-line spot check does not bind the first observed lyric")
    tail_spot_ms = int(spots["tail"]["live_time_ms"])
    final_lyric_start_ms = int(alignment[-1]["cue_start_ms"])
    final_lyric_end_ms = int(alignment[-1]["cue_end_ms"])
    if not final_lyric_start_ms <= tail_spot_ms < final_lyric_end_ms:
        raise ValueError("tail spot check does not bind the final observed lyric")

    first_index_by_text: dict[str, int] = {}
    later_repeat_starts: list[int] = []
    for index, line in enumerate(lrc.lines):
        normalized = normalize_lyric_text(line.text)
        if not normalized:
            continue
        if normalized in first_index_by_text:
            later_repeat_starts.append(int(alignment[index]["cue_start_ms"]))
        else:
            first_index_by_text[normalized] = index
    if later_repeat_starts:
        repeated_spot_ms = int(spots["repeated_section"]["live_time_ms"])
        if all(abs(repeated_spot_ms - start_ms) > 1_500 for start_ms in later_repeat_starts):
            raise ValueError("repeated-section spot check does not bind a later repeated lyric occurrence")

    post_song_talk_start_ms = payload.get("post_song_talk_start_ms")
    if post_song_talk_start_ms is None:
        clip_end_ms = effective_duration_ms
    elif _is_int(post_song_talk_start_ms) and last_lyric_end_ms <= post_song_talk_start_ms <= effective_duration_ms:
        clip_end_ms = post_song_talk_start_ms
    else:
        raise ValueError("post-song talk boundary is invalid or precedes the last lyric")
    clip_start_ms = offset_ms
    if not 0 <= clip_start_ms <= offset_ms <= first_lyric_start_ms <= last_lyric_end_ms <= clip_end_ms <= effective_duration_ms:
        raise ValueError("derived full-song boundary ordering is invalid")
    matched_ratio = len(alignment) / len(lrc.lines)
    if matched_ratio < min_matched_ratio:
        raise ValueError(f"audio alignment ratio {matched_ratio:.0%} is below {min_matched_ratio:.0%}")
    matched = [dict(entry) for entry in alignment]
    return (
        matched_ratio,
        lrc,
        alignment,
        matched,
        first_lyric_start_ms,
        last_lyric_end_ms,
        clip_start_ms,
        clip_end_ms,
        offset_ms,
    )


def _validate_lyric_vocal_observations(
    observations: object,
    *,
    require_ready: bool,
) -> tuple[bool, bool, bool]:
    """Recompute the singer/role aggregates from every canonical lyric row.

    The AGY top-level summary is never trusted as a substitute for the rows.
    A READY result requires each line to say that the same live vocal source is
    李豆沙 herself singing that line, with no guest/duet/harmony or recorded
    vocal audible.  CAM++ remains an independent speaker-similarity subclaim;
    it is not treated here (or elsewhere) as a singing classifier.
    """

    if (
        not isinstance(observations, Sequence)
        or isinstance(observations, (str, bytes, bytearray))
        or not observations
    ):
        raise ValueError("live performance lyric-source observations are missing")
    all_same_lidousha = True
    any_other_singer = False
    any_recorded_vocal = False
    for index, row in enumerate(observations):
        if not isinstance(row, Mapping) or not LYRIC_VOCAL_ASSERTION_KEYS.issubset(row):
            raise ValueError(f"live performance lyric row {index} singer schema is invalid")
        subject = row.get("lyric_vocal_subject")
        role = row.get("lidousha_role")
        same_lidousha = row.get("same_live_vocal_source_as_lidousha")
        other_singer = row.get("other_singer_or_harmony_audible")
        recorded_vocal = row.get("recorded_or_playback_vocal_audible")
        if subject not in LYRIC_VOCAL_SUBJECTS or role not in LIDOUSHA_LYRIC_ROLES:
            raise ValueError(f"live performance lyric row {index} singer enum is invalid")
        if not all(isinstance(value, bool) for value in (same_lidousha, other_singer, recorded_vocal)):
            raise ValueError(f"live performance lyric row {index} singer assertions are invalid")

        affirmative = (
            subject == "LIDOUSHA"
            and role == "SINGING_THIS_LYRIC"
            and other_singer is False
            and recorded_vocal is False
        )
        if same_lidousha is not affirmative:
            raise ValueError(f"live performance lyric row {index} same-subject assertion is inconsistent")
        if subject == "LIDOUSHA" and role != "SINGING_THIS_LYRIC":
            raise ValueError(f"live performance lyric row {index} Li-Dousha role contradicts its subject")
        if subject == "OTHER_OR_MIXED_SINGER" and other_singer is not True:
            raise ValueError(f"live performance lyric row {index} other-singer assertion is inconsistent")
        if subject == "RECORDED_OR_PLAYBACK_SINGER" and recorded_vocal is not True:
            raise ValueError(f"live performance lyric row {index} recorded-vocal assertion is inconsistent")
        if subject == "NO_AUDIBLE_LYRIC_VOCAL" and (other_singer or recorded_vocal):
            raise ValueError(f"live performance lyric row {index} no-vocal assertion is inconsistent")
        if require_ready and not affirmative:
            raise ValueError(
                f"live performance lyric row {index} does not affirm the same live Li-Dousha singer"
            )
        all_same_lidousha = all_same_lidousha and bool(same_lidousha)
        any_other_singer = any_other_singer or bool(other_singer)
        any_recorded_vocal = any_recorded_vocal or bool(recorded_vocal)
    return all_same_lidousha, any_other_singer, any_recorded_vocal


def validate_live_performance_observation(
    performance: object,
    *,
    first_lyric_start_ms: int,
    last_lyric_end_ms: int,
    observations: object,
    require_ready: bool,
) -> str | None:
    """Validate AGY's anti-background and same-subject singing observation.

    AGY must assert the active lyric vocalist and Li-Dousha's role on every
    canonical line.  Final delivery additionally combines this with the
    independently generated Li-Dousha voiceprint claim on the same lyric rows.
    """

    try:
        if not isinstance(performance, Mapping) or set(performance) != {
            "mode",
            "confidence",
            "continuous_singing",
            "background_recording_likelihood",
            "same_lidousha_live_singer_across_all_lyrics",
            "other_singer_or_harmony_present",
            "recorded_or_playback_vocal_present",
            "evidence",
            "notes",
        }:
            raise ValueError("live performance observation schema is invalid")
        mode = performance.get("mode")
        if mode not in LIVE_PERFORMANCE_MODES:
            raise ValueError("live performance observation mode is invalid")
        confidence = performance.get("confidence")
        background = performance.get("background_recording_likelihood")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0.0 <= float(confidence) <= 1.0
            or isinstance(background, bool)
            or not isinstance(background, (int, float))
            or not 0.0 <= float(background) <= 1.0
            or not isinstance(performance.get("continuous_singing"), bool)
            or not isinstance(performance.get("same_lidousha_live_singer_across_all_lyrics"), bool)
            or not isinstance(performance.get("other_singer_or_harmony_present"), bool)
            or not isinstance(performance.get("recorded_or_playback_vocal_present"), bool)
            or not isinstance(performance.get("notes"), str)
            or not str(performance.get("notes")).strip()
        ):
            raise ValueError("live performance observation values are invalid")
        all_same_lidousha, any_other_singer, any_recorded_vocal = _validate_lyric_vocal_observations(
            observations,
            require_ready=require_ready,
        )
        if (
            performance.get("same_lidousha_live_singer_across_all_lyrics") is not all_same_lidousha
            or performance.get("other_singer_or_harmony_present") is not any_other_singer
            or performance.get("recorded_or_playback_vocal_present") is not any_recorded_vocal
        ):
            raise ValueError("live performance top-level singer assertions do not match lyric rows")
        evidence = performance.get("evidence")
        if not isinstance(evidence, list) or len(evidence) != 3:
            raise ValueError("live performance observation needs exactly three evidence timestamps")
        if not 0 <= first_lyric_start_ms < last_lyric_end_ms:
            raise ValueError("live performance lyric span is invalid")
        span = last_lyric_end_ms - first_lyric_start_ms
        buckets: set[int] = set()
        previous_time = -1
        for index, row in enumerate(evidence):
            if not isinstance(row, Mapping) or set(row) != {"time_ms", "observation"}:
                raise ValueError(f"live performance evidence[{index}] schema is invalid")
            time_ms = row.get("time_ms")
            observation = row.get("observation")
            if (
                not _is_int(time_ms)
                or not first_lyric_start_ms <= time_ms <= last_lyric_end_ms
                or int(time_ms) <= previous_time
                or not isinstance(observation, str)
                or not observation.strip()
            ):
                raise ValueError(f"live performance evidence[{index}] is invalid")
            previous_time = int(time_ms)
            buckets.add(min(2, ((int(time_ms) - first_lyric_start_ms) * 3) // max(1, span)))
        if buckets != {0, 1, 2}:
            raise ValueError("live performance evidence must cover lyric head, middle, and tail")
        if require_ready and (
            mode != LIVE_PERFORMANCE_READY_MODE
            or performance.get("continuous_singing") is not True
            or performance.get("same_lidousha_live_singer_across_all_lyrics") is not True
            or performance.get("other_singer_or_harmony_present") is not False
            or performance.get("recorded_or_playback_vocal_present") is not False
            or float(confidence) < 0.85
            or float(background) > 0.20
        ):
            raise ValueError(f"live performance not proven: mode={mode}")
    except ValueError as exc:
        return str(exc)
    return None


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
    payload = _http_json_value(url, timeout_seconds=timeout_seconds)
    return payload if isinstance(payload, Mapping) else None


def _http_json_value(
    url: str,
    *,
    timeout_seconds: float,
    request_headers: Mapping[str, str] | None = None,
) -> object:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Referer": "https://music.163.com/",
    }
    if request_headers:
        headers.update({str(key): str(value) for key, value in request_headers.items()})
    request = urllib.request.Request(
        url,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _coerce_lrc_results(found: LrcResult | Sequence[LrcResult] | None) -> list[LrcResult]:
    if found is None:
        return []
    if isinstance(found, LrcResult):
        return [found]
    return [item for item in found if isinstance(item, LrcResult)]


def _strip_kugou_markup(value: object) -> str:
    """Remove search-result highlighting while preserving the exact identity text."""

    return re.sub(r"<[^>]*>", "", str(value or "")).strip()


def _kugou_identity_text(value: object) -> str:
    return normalize_lyric_text(_strip_kugou_markup(value))


def _kugou_candidate_matches_track(
    candidate: Mapping[str, object],
    *,
    song_title: str,
    artist: str,
    duration_ms: int,
) -> bool:
    """Fail closed unless the lyric row belongs to this exact catalog track."""

    if _kugou_identity_text(candidate.get("song")) != _kugou_identity_text(song_title):
        return False
    if _kugou_identity_text(candidate.get("singer")) != _kugou_identity_text(artist):
        return False
    candidate_duration = candidate.get("duration")
    if isinstance(candidate_duration, bool) or not isinstance(candidate_duration, (int, float)):
        return False
    return abs(int(candidate_duration) - duration_ms) <= 3_000


def _filter_kugou_timed_metadata(
    lines: Sequence[LrcLine],
    *,
    song_title: str,
    artist: str,
) -> list[LrcLine]:
    """Drop Kugou's timed title card without dropping a real opening lyric."""

    title_identity = _kugou_identity_text(song_title)
    artist_identity = _kugou_identity_text(artist)
    filtered: list[LrcLine] = []
    for line in lines:
        line_identity = _kugou_identity_text(line.text)
        is_timed_title_card = (
            line.time_ms <= 1_000
            and bool(title_identity)
            and bool(artist_identity)
            and title_identity in line_identity
            and artist_identity in line_identity
        )
        if not is_timed_title_card:
            filtered.append(line)
    return filtered


def _parse_lrclib_id(song_ref: str) -> int | None:
    value = str(song_ref or "").strip()
    if value.isdigit():
        return int(value)
    match = re.fullmatch(r"lrclib://(?:track|song|lyrics)/(\d+)", value, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.fullmatch(r"https?://(?:www\.)?lrclib\.net/api/get/(\d+)", value, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _lrclib_record_to_result(record: Mapping[str, object], *, fallback_id: int | None = None) -> LrcResult | None:
    raw_id = record.get("id")
    song_id = raw_id if isinstance(raw_id, int) else fallback_id
    if song_id is None:
        return None
    synced_lyrics = record.get("syncedLyrics")
    if not isinstance(synced_lyrics, str) or not synced_lyrics.strip():
        return None
    lines = parse_lrc_text(synced_lyrics)
    if len(lines) < 8:
        return None
    song_title = str(record.get("trackName") or record.get("name") or "")
    artist = str(record.get("artistName") or "").strip() or None
    return LrcResult(
        provider="lrclib",
        song_title=song_title,
        artist=artist,
        source_ref=f"https://lrclib.net/api/get/{song_id}",
        lines=tuple(lines),
    )


def _finish(
    candidate_id: str,
    attempts: list[SongRepairAttempt],
    output_dir: Path,
    *,
    repaired: bool = False,
    song_boundary: Mapping[str, object] | None = None,
    lyrics_alignment: Mapping[str, object] | None = None,
    reason_codes: Sequence[str] = (),
    live_performance: Mapping[str, object] | None = None,
) -> SongRepairResult:
    report_path = output_dir / f"{candidate_id}.song-repair.json"
    result = SongRepairResult(
        repaired=repaired,
        attempts=tuple(attempts),
        song_boundary=song_boundary,
        lyrics_alignment=lyrics_alignment,
        report_path=str(report_path),
        reason_codes=tuple(dict.fromkeys(str(code) for code in reason_codes if code)),
        live_performance=dict(live_performance) if live_performance else None,
    )
    report_path.write_text(json.dumps(result.to_manifest(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
